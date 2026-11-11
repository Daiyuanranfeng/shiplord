from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shiplord.environment import (MAX_ENVIRONMENT_BYTES, MAX_VALUE_BYTES, EnvironmentError_, edit_environment,
                                  load_environment_file, load_environment_text, parse_assignment, parse_environment,
                                  render_environment)


class ParseTests(unittest.TestCase):
    def test_valid_files_parse_literally(self):
        text = "# secrets\n\nDATABASE_URL=postgres://u:p@h/db?x=1&y=2\nEMPTY=\nQUOTED=\"a b\"\nSPACED = not-a-key-format-is-ok\n".replace(
            "SPACED = not-a-key-format-is-ok\n", "")
        self.assertEqual(parse_environment(text), {"DATABASE_URL": "postgres://u:p@h/db?x=1&y=2", "EMPTY": "",
                                                   "QUOTED": '"a b"'})

    def test_values_are_never_interpreted(self):
        value = "$(touch /x) `id` ${HOME} ; rm -rf / && echo $PATH"
        self.assertEqual(parse_environment(f"TOKEN={value}\n"), {"TOKEN": value})

    def test_invalid_input_is_rejected(self):
        cases = {
            "bare key passes through the host environment": "AWS_SECRET_ACCESS_KEY\n",
            "space before equals": "A =1\n",
            "bad key": "1A=x\n",
            "dash key": "A-B=x\n",
            "duplicate": "A=1\nA=2\n",
            "managed key": "ALLOW_ORIGINS=[]\n",
            "managed port": "PORT=1\n",
            "nul": "A=x\0y\n",
            "carriage return": "A=x\ry=1\n",
            "huge value": "A=" + "x" * (MAX_VALUE_BYTES + 1) + "\n",
            "huge file": "".join(f"K{n}=" + "x" * 100 + "\n" for n in range(MAX_ENVIRONMENT_BYTES // 100)),
            "shell export syntax": "export A=1\n",
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(EnvironmentError_):
                parse_environment(text)

    def test_crlf_line_endings_are_accepted_and_python_only_separators_stay_inside_values(self):
        self.assertEqual(parse_environment("A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})
        self.assertEqual(parse_environment("A=x\u2028PORT=1\n"), {"A": "x\u2028PORT=1"})
        self.assertEqual(parse_environment("A=x\x0bB\n"), {"A": "x\x0bB"})

    def test_render_round_trips_and_is_sorted(self):
        values = {"B": "2", "A": "x=y"}
        self.assertEqual(render_environment(values), "A=x=y\nB=2\n")
        self.assertEqual(parse_environment(render_environment(values)), values)


class EditTests(unittest.TestCase):
    def test_set_changes_one_line_and_keeps_every_other_byte(self):
        text = ("# runtime settings\r\n"
                "\r\n"
                "B_TOKEN=old\r\n"
                "A_URL=\"postgres://u:p@h/db?x=1\"\r\n"
                "EMPTY=\r\n")
        updated = edit_environment(text, "B_TOKEN", "new=value#literal")
        self.assertEqual(updated, "# runtime settings\r\n"
                                  "\r\n"
                                  "B_TOKEN=new=value#literal\r\n"
                                  "A_URL=\"postgres://u:p@h/db?x=1\"\r\n"
                                  "EMPTY=\r\n")
        self.assertEqual(parse_environment(updated)["A_URL"], '"postgres://u:p@h/db?x=1"')

    def test_set_appends_a_new_key_and_preserves_the_final_newline(self):
        self.assertEqual(edit_environment("A=1\n", "B", "2"), "A=1\nB=2\n")
        self.assertEqual(edit_environment("A=1", "B", "2"), "A=1\nB=2")
        self.assertEqual(edit_environment("", "B", "2"), "B=2\n")
        self.assertEqual(edit_environment("A=1\r\n", "B", "2"), "A=1\r\nB=2\r\n")

    def test_setting_the_same_value_returns_the_same_text(self):
        text = "# comment\nA=1\n"
        self.assertEqual(edit_environment(text, "A", "1"), text)

    def test_unset_removes_one_line_and_is_idempotent(self):
        self.assertEqual(edit_environment("# c\nA=1\nB=2\nC=3\n", "B", None), "# c\nA=1\nC=3\n")
        self.assertEqual(edit_environment("# c\nA=1\nC=3\n", "B", None), "# c\nA=1\nC=3\n")
        self.assertEqual(edit_environment("A=1\n", "A", None), "")
        self.assertEqual(edit_environment("A=1", "A", None), "")
        self.assertEqual(edit_environment("A=1\r\nB=2\r\n", "A", None), "B=2\r\n")

    def test_a_comment_that_mentions_a_key_is_not_the_key(self):
        self.assertEqual(edit_environment("# A=old\nA=1\n", "A", "2"), "# A=old\nA=2\n")
        self.assertEqual(edit_environment("# B=1\nA=1\n", "B", None), "# B=1\nA=1\n")

    def test_a_file_this_module_cannot_rewrite_is_refused(self):
        for text in ("export A=1\n", "A=1\nA=2\n", "A =1\n", "1A=1\n", "A=1\rB=2\n"):
            with self.subTest(text=text), self.assertRaises(EnvironmentError_):
                edit_environment(text, "A", "2")

    def test_assignments_are_parsed_literally_and_reject_injection(self):
        self.assertEqual(parse_assignment("A=b=c#d"), ("A", "b=c#d"))
        self.assertEqual(parse_assignment('A=two words "quoted" # not a comment'),
                         ("A", 'two words "quoted" # not a comment'))
        self.assertEqual(parse_assignment("A="), ("A", ""))
        for bad in ("A", "", "A=1\nB=2", "A=1\rB=2", "A=x\0y", "A-B=1", "1A=1", "PORT=1",
                    "A=" + "x" * (MAX_VALUE_BYTES + 1), None, 1):
            with self.subTest(bad=str(bad)[:20]), self.assertRaises(EnvironmentError_):
                parse_assignment(bad)

    def test_errors_never_contain_the_value(self):
        secret = "postgres://user:s3cr3t@db/app"
        for assignment in (f"BAD LINE {secret}", f"PORT={secret}", f"A-B={secret}", f"A={secret}\nB=2"):
            with self.subTest(assignment=assignment[:12]), self.assertRaises(EnvironmentError_) as caught:
                parse_assignment(assignment)
            self.assertNotIn("s3cr3t", str(caught.exception))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app.env"

    def test_missing_file_is_none_and_private_file_loads(self):
        self.assertIsNone(load_environment_file(self.path))
        self.path.write_text("A=1\n")
        self.path.chmod(0o600)
        self.assertEqual(load_environment_file(self.path), {"A": "1"})

    def test_text_loading_preserves_bytes_and_applies_the_same_checks(self):
        self.assertIsNone(load_environment_text(self.path))
        self.path.write_text("# c\r\nA=1\r\n")
        self.path.chmod(0o600)
        self.assertEqual(load_environment_text(self.path), "# c\r\nA=1\r\n")
        self.path.chmod(0o644)
        with self.assertRaises(EnvironmentError_):
            load_environment_text(self.path)

    def test_readable_symlinked_or_non_utf8_files_are_refused(self):
        self.path.write_text("A=1\n")
        self.path.chmod(0o644)
        with self.assertRaises(EnvironmentError_):
            load_environment_file(self.path)
        self.path.chmod(0o600)
        link = Path(self.temp.name) / "link.env"
        link.symlink_to(self.path)
        with self.assertRaises(EnvironmentError_):
            load_environment_file(link)
        self.path.write_bytes(b"A=\xff\xfe\n")
        with self.assertRaises(EnvironmentError_):
            load_environment_file(self.path)

    def test_a_directory_in_place_of_the_file_is_refused_as_an_environment_error(self):
        self.path.mkdir()
        with self.assertRaises(EnvironmentError_):
            load_environment_file(self.path)


if __name__ == "__main__":
    unittest.main()
