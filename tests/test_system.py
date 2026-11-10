from __future__ import annotations

import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from shiplord.system import CommandError, HostSystem, run_command, stream_command


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def script(self, body):
        path = self.directory / "tool"
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    def test_returns_code_and_combined_output(self):
        result = run_command([self.script('echo out; echo err >&2; exit 3\n')])
        self.assertEqual((result.code, result.output), (3, "out\nerr\n"))

    def test_arguments_are_never_interpreted_by_a_shell(self):
        marker = self.directory / "injected"
        tool = self.script('printf "%s" "$1"\n')
        result = run_command([tool, f"; touch {marker}; $(touch {marker})"])
        self.assertEqual(result.code, 0)
        self.assertFalse(marker.exists())
        self.assertIn("touch", result.output)

    def test_stdin_is_delivered_and_large_payloads_do_not_deadlock(self):
        payload = b"x" * 500_000
        result = run_command([self.script("wc -c\n")], input=payload)
        self.assertEqual(result.output.strip(), "500000")

    def test_output_is_capped_to_the_newest_bytes(self):
        result = run_command([self.script('head -c 300000 /dev/zero | tr "\\0" a; printf "END"\n')])
        self.assertLessEqual(len(result.output), 4096)
        self.assertTrue(result.output.endswith("END"))

    def test_truncated_output_says_the_earlier_output_was_dropped(self):
        result = run_command([self.script('head -c 300000 /dev/zero | tr "\\0" a\n')])
        self.assertTrue(result.output.startswith("[shiplord: earlier output truncated]\n"))
        self.assertEqual(len(result.output), 4096)

    def test_output_within_the_cap_is_returned_whole(self):
        result = run_command([self.script('printf "first\\nlast\\n"\n')])
        self.assertEqual(result.output, "first\nlast\n")

    def test_a_zero_cap_returns_no_output(self):
        self.assertEqual(run_command([self.script('printf "xxxx"\n')], max_output=0).output, "")

    def test_a_window_cut_inside_a_character_decodes_with_a_replacement(self):
        result = run_command([self.script('printf "\\303\\251%.0s" $(seq 3000)\n')], max_output=4094)
        self.assertLessEqual(len(result.output), 4094)
        self.assertIn("\ufffd", result.output)
        self.assertTrue(result.output.endswith("\u00e9"))

    def test_control_characters_are_replaced(self):
        result = run_command([self.script('printf "a\\033[0mb\\n"\n')])
        self.assertEqual(result.output, "a?[0mb\n")

    def test_environment_is_fixed_not_inherited(self):
        os.environ["SHIPLORD_LEAK_TEST"] = "secret"
        self.addCleanup(os.environ.pop, "SHIPLORD_LEAK_TEST", None)
        names = {line.split("=", 1)[0] for line in run_command([self.script("env\n")]).output.splitlines()}
        self.assertNotIn("SHIPLORD_LEAK_TEST", names)
        custom = run_command([self.script("env\n")], env={"ONLY": "1"}).output
        self.assertIn("ONLY=1", custom)

    def test_timeout_kills_the_whole_process_group(self):
        marker = self.directory / "survived"
        tool = self.script(f"(sleep 2; touch {marker}) &\nsleep 30\n")
        started = time.monotonic()
        with self.assertRaises(CommandError) as caught:
            run_command([tool], timeout=0.5)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 5)
        time.sleep(2.5)
        self.assertFalse(marker.exists())

    def test_missing_executable_and_invalid_arguments(self):
        for argv in ([str(self.directory / "absent")], [], ["tool\0x"]):
            with self.subTest(argv=argv), self.assertRaises(CommandError) as caught:
                run_command(argv)
            self.assertEqual(caught.exception.kind, "start")

    def test_host_system_runs_commands(self):
        self.assertEqual(HostSystem().run(["/bin/echo", "ok"]).output, "ok\n")


class StreamCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def script(self, body):
        path = self.directory / "tool"
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    def test_output_is_forwarded_while_the_command_still_runs(self):
        chunks = []
        reason = stream_command([self.script('printf "one\\n"; sleep 0.2; printf "two\\n"\n')], timeout=5,
                                on_chunk=chunks.append)
        self.assertEqual((reason, "".join(chunks)), ("ended", "one\ntwo\n"))

    def test_the_deadline_stops_a_stream_that_never_ends(self):
        started = time.monotonic()
        chunks = []
        reason = stream_command([self.script('printf "start\\n"; while true; do sleep 0.05; done\n')], timeout=0.3,
                                on_chunk=chunks.append)
        self.assertEqual((reason, "".join(chunks)), ("timeout", "start\n"))
        self.assertLess(time.monotonic() - started, 5)

    def test_the_deadline_kills_the_whole_process_group(self):
        marker = self.directory / "survived"
        tool = self.script(f"(sleep 2; touch {marker}) &\nwhile true; do sleep 0.05; done\n")
        self.assertEqual(stream_command([tool], timeout=0.3, on_chunk=lambda chunk: None), "timeout")
        time.sleep(2.5)
        self.assertFalse(marker.exists())

    def test_control_characters_are_replaced_but_newlines_are_kept(self):
        chunks = []
        stream_command([self.script('printf "a\\033[0mb\\nsecond\\n"\n')], timeout=5, on_chunk=chunks.append)
        self.assertEqual("".join(chunks), "a?[0mb\nsecond\n")

    def test_a_character_split_across_reads_is_not_replaced(self):
        chunks = []
        stream_command([self.script('printf "\\303\\251%.0s" $(seq 40000)\n')], timeout=30, on_chunk=chunks.append)
        text = "".join(chunks)
        self.assertNotIn("\ufffd", text)
        self.assertEqual(text, "\u00e9" * 40000)

    def test_missing_executable_and_invalid_arguments_are_start_errors(self):
        for argv, timeout in (([str(self.directory / "absent")], 5), ([], 5), (["tool\0x"], 5),
                              (["/bin/true"], 0)):
            with self.subTest(argv=argv), self.assertRaises(CommandError) as caught:
                stream_command(argv, timeout=timeout, on_chunk=lambda chunk: None)
            self.assertEqual(caught.exception.kind, "start")

    def test_host_system_streams_commands(self):
        chunks = []
        self.assertEqual(HostSystem().stream(["/bin/echo", "ok"], timeout=5, on_chunk=chunks.append), "ended")
        self.assertEqual("".join(chunks), "ok\n")


if __name__ == "__main__":
    unittest.main()
