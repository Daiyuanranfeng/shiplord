from __future__ import annotations

import io
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeProbes, configuration  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.client import AdminClient, ClientError, HelperClient  # noqa: E402
from shiplord.commands import CommandError  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.manifest import validate_manifest  # noqa: E402
from shiplord.protocol import IncompatibleProtocol  # noqa: E402
from shiplord.runtime import RuntimeFailure  # noqa: E402
from shiplord.servers import ServerError  # noqa: E402
from shiplord.transport import TransportError  # noqa: E402
from shiplord.worker import run_job  # noqa: E402
from test_client import ClientTestCase, FakeSsh, frame, ok  # noqa: E402
from test_provision import ed25519  # noqa: E402

COMMIT, OTHER = "a" * 40, "c" * 40
IMAGE = "ghcr.io/example/orders@sha256:" + "b" * 64


def manifest(**changes):
    value = validate_manifest(configuration())
    value.update(changes)
    return value


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / ".ssh").mkdir()

    def test_public_key_comes_from_the_explicit_file_else_the_first_standard_key(self):
        (self.home / ".ssh" / "id_rsa.pub").write_text(ed25519(2) + "\n")
        (self.home / ".ssh" / "id_ed25519.pub").write_text(ed25519(1) + "\n")
        text, fingerprint = commands.read_public_key(None, self.home)
        self.assertEqual(text, ed25519(1))
        self.assertTrue(fingerprint.startswith("SHA256:"))
        explicit = self.home / "other.pub"
        explicit.write_text(ed25519(3))
        self.assertEqual(commands.read_public_key(explicit, self.home)[0], ed25519(3))

    def test_a_missing_or_unusable_key_is_a_clear_error(self):
        with self.assertRaises(CommandError) as caught:
            commands.read_public_key(None, self.home)
        self.assertEqual(caught.exception.code, 3)
        bad = self.home / "bad.pub"
        bad.write_text('command="x" ' + ed25519(1))
        with self.assertRaisesRegex(CommandError, "not a usable public key"):
            commands.read_public_key(bad, self.home)

    def test_the_image_repository_is_derived_from_github_and_lowercased(self):
        github = {"github": {"repository": "QueryPlanner/Orders", "deployment_branch": "main"}}
        self.assertEqual(commands.image_repository_for(manifest(**github), None), "ghcr.io/queryplanner/orders")
        self.assertEqual(commands.image_repository_for(manifest(), "ghcr.io/x/y"), "ghcr.io/x/y")
        with self.assertRaisesRegex(CommandError, "image repository"):
            commands.image_repository_for(manifest(), None)
        self.assertIsNone(commands.image_repository_for(manifest(runtime="native"), None))

    def test_the_release_sequence_is_the_commit_time_so_ci_and_local_agree(self):
        repo = self.home / "repo"
        repo.mkdir()
        environment = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                           GIT_COMMITTER_EMAIL="t@e", GIT_COMMITTER_DATE="2026-01-02T03:04:05+00:00",
                           GIT_AUTHOR_DATE="2026-01-02T03:04:05+00:00")
        run = lambda *args: subprocess.run(["git", *args], cwd=repo, env=environment, check=True, capture_output=True,  # noqa: E731
                                           text=True).stdout.strip()
        run("init", "-q")
        (repo / "f").write_text("x")
        run("add", "f")
        run("commit", "-q", "-m", "c", "--no-gpg-sign")
        sha = run("rev-parse", "HEAD")
        self.assertEqual(commands.commit_sequence(sha, None, repo), 1767323045)
        self.assertEqual(commands.commit_sequence(sha, 77, repo), 77)
        with self.assertRaises(CommandError):
            commands.commit_sequence(sha, -1, repo)
        with self.assertRaisesRegex(CommandError, "--sequence"):
            commands.commit_sequence("f" * 40, None, repo)
        with self.assertRaises(CommandError):
            commands.commit_sequence(sha, None, self.home)  # not a repository

    def test_deploy_inputs_are_validated_without_any_connection(self):
        commands.validate_deploy_inputs(manifest(), IMAGE, COMMIT)
        for image, commit in (("ghcr.io/example/orders:latest", COMMIT), (IMAGE, "HEAD"), (IMAGE, COMMIT[:39]),
                              ("ghcr.io/example/orders@sha256:abc", COMMIT), ("x; rm -rf /@sha256:" + "a" * 64, COMMIT)):
            with self.subTest(image=image, commit=commit), self.assertRaises(CommandError):
                commands.validate_deploy_inputs(manifest(), image, commit)
        with self.assertRaisesRegex(CommandError, "native"):
            commands.validate_deploy_inputs(manifest(runtime="native"), IMAGE, COMMIT)


class AdminCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / "k.pub"
        self.key.write_text(ed25519(1))

    def test_provision_sends_the_registration_inputs_and_explains_the_next_step(self):
        result = {"application_id": "orders", "deployment_user": "ship-orders", "host_port": 20000,
                  "api_domain": "orders.example.com", "runtime": "docker", "key_fingerprint": "SHA256:x", "created": {}}
        ssh = FakeSsh((ok(result), b"", 0))
        code, output = commands.provision_application(
            manifest(github={"repository": "Example/Orders", "deployment_branch": "main"}),
            AdminClient("appuser@server.example.com", runner=ssh), public_key=self.key)
        sent = read_frame(io.BytesIO(ssh.calls[0][1]))
        self.assertEqual(sent["action"], "provision")
        self.assertEqual(sent["arguments"]["image_repository"], "ghcr.io/example/orders")
        self.assertEqual(sent["arguments"]["public_key"], ed25519(1))
        self.assertRegex(sent["arguments"]["key_label"], r"^local-[a-z0-9]{8}$")
        self.assertEqual((code, output["deploy_login"]), (0, "ship-orders@server.example.com"))
        self.assertIn("DNS", output["next"])

    def test_provision_update_sends_the_flag_and_explains_what_was_kept(self):
        result = {"application_id": "orders", "deployment_user": "ship-orders", "host_port": 20000,
                  "api_domain": "new.example.com", "runtime": "docker", "max_memory_mb": 8192,
                  "key_fingerprint": "SHA256:x", "created": {}, "updated": True, "route_changed": True,
                  "changes": {"api_domain": {"from": "orders.example.com", "to": "new.example.com"},
                              "max_memory_mb": {"from": 4096, "to": 8192}}}
        github = {"github": {"repository": "Example/Orders", "deployment_branch": "main"}}
        ssh = FakeSsh((ok(result), b"", 0))
        code, output = commands.provision_application(
            manifest(**github), AdminClient("appuser@server.example.com", runner=ssh), public_key=self.key, update=True)
        sent = read_frame(io.BytesIO(ssh.calls[0][1]))
        self.assertTrue(sent["arguments"]["update"])
        self.assertEqual((code, output["updated"]), (0, True))
        self.assertIn("public route was moved", output["next"])
        self.assertIn("DNS", output["next"])

        memory_only = dict(result, api_domain="orders.example.com", route_changed=False,
                           changes={"max_memory_mb": {"from": 4096, "to": 8192}})
        ssh = FakeSsh((ok(memory_only), b"", 0))
        code, output = commands.provision_application(
            manifest(**github), AdminClient("appuser@server.example.com", runner=ssh), public_key=self.key, update=True)
        self.assertIn("memory ceiling", output["next"])
        self.assertNotIn("route", output["next"])

        unchanged = dict(result, api_domain="orders.example.com", route_changed=False, updated=False, changes={})
        ssh = FakeSsh((ok(unchanged), b"", 0))
        code, output = commands.provision_application(
            manifest(**github), AdminClient("appuser@server.example.com", runner=ssh), public_key=self.key, update=True)
        self.assertEqual((code, output["updated"]), (0, False))
        self.assertIn("nothing changed", output["next"])

    def test_empty_or_invalid_environment_input_is_refused_before_any_connection(self):
        ssh = FakeSsh()
        client = AdminClient("a@h", runner=ssh)
        for text, expected in (("", "no variables"), ("# only a comment\n\n", "no variables"), (None, "no variables"),
                               ("PORT=1\n", "managed by Shiplord"), ("BAD LINE\n", "not KEY=VALUE")):
            with self.subTest(text=text), self.assertRaisesRegex(CommandError, expected):
                commands.set_environment(manifest(), client, "set", text)
        self.assertEqual(ssh.calls, [])

    def test_deprovision_sends_the_application_and_only_the_retire_flag_when_asked(self):
        for retire, arguments in ((False, {}), (True, {"retire": True})):
            with self.subTest(retire=retire):
                ssh = FakeSsh((ok({"removed": {"registration": True}}), b"", 0))
                code, output = commands.deprovision_application("orders", AdminClient("a@h", runner=ssh), retire=retire)
                sent = read_frame(io.BytesIO(ssh.calls[0][1]))
                self.assertEqual((sent["action"], sent["application_id"], sent["arguments"]),
                                 ("deprovision", "orders", arguments))
                self.assertEqual((code, output["ok"]), (0, True))
                self.assertIn("data", output["note"])

    def test_environment_values_travel_in_the_frame_and_never_come_back(self):
        secret = "postgres://u:s3cr3t@db/x"
        ssh = FakeSsh((ok({"keys": ["DATABASE_URL"], "changed": True}), b"", 0))
        code, output = commands.set_environment(manifest(), AdminClient("a@h", runner=ssh), "set", f"DATABASE_URL={secret}\n")
        self.assertIn(secret.encode(), ssh.calls[0][1])
        self.assertNotIn("s3cr3t", str(ssh.calls[0][0]) + str(output))
        self.assertIn("shiplord restart", output["note"])
        _, listing = commands.set_environment(manifest(), AdminClient("a@h", runner=FakeSsh((ok({"keys": ["A"]}), b"", 0))), "keys")
        self.assertNotIn("note", listing)

    def test_one_key_edits_send_the_value_only_in_the_frame_and_never_return_it(self):
        secret = "postgres://u:s3cr3t@db/x"
        ssh = FakeSsh((ok({"keys": ["DATABASE_URL"], "changed": True}), b"", 0))
        code, output = commands.edit_environment(manifest(), AdminClient("a@h", runner=ssh), "set-key",
                                                 assignment=f"DATABASE_URL={secret}")
        sent = read_frame(io.BytesIO(ssh.calls[0][1]))
        self.assertEqual(sent["arguments"], {"operation": "set-key", "key": "DATABASE_URL", "value": secret})
        self.assertIn(secret.encode(), ssh.calls[0][1])
        self.assertNotIn("s3cr3t", str(ssh.calls[0][0]) + str(output))
        self.assertEqual((code, output["keys"]), (0, ["DATABASE_URL"]))
        self.assertIn("next time a release starts", output["note"])

        ssh = FakeSsh((ok({"keys": [], "changed": True}), b"", 0))
        code, output = commands.edit_environment(manifest(), AdminClient("a@h", runner=ssh), "unset", key="OLD")
        self.assertEqual(read_frame(io.BytesIO(ssh.calls[0][1]))["arguments"], {"operation": "unset", "key": "OLD"})
        self.assertEqual((code, output["changed"]), (0, True))
        self.assertIn("next time a release starts", output["note"])

    def test_invalid_one_key_edits_are_refused_before_any_connection(self):
        ssh = FakeSsh()
        client = AdminClient("a@h", runner=ssh)
        cases = ((("set-key",), {"assignment": "PORT=1"}, "managed by Shiplord"),
                 (("set-key",), {"assignment": "BAD LINE s3cr3t"}, "KEY=VALUE"),
                 (("set-key",), {"assignment": "A=1\nB=2"}, "invalid"),
                 (("set-key",), {"assignment": "A-B=1"}, "variable name"),
                 (("set-key",), {"assignment": None}, "assignment is required"),
                 (("unset",), {"key": "A B"}, "variable name"),
                 (("unset",), {"key": "PORT"}, "managed by Shiplord"),
                 (("unset",), {"key": None}, "variable name"),
                 (("merge",), {"key": "A"}, "unknown environment operation"))
        for operation, arguments, expected in cases:
            with self.subTest(operation=operation, arguments=str(arguments)[:20]), \
                    self.assertRaisesRegex(CommandError, expected) as caught:
                commands.edit_environment(manifest(), client, *operation, **arguments)
            self.assertNotIn("s3cr3t", str(caught.exception))

    def test_environment_diff_reports_only_key_name_changes(self):
        secret, other = "postgres://u:first-secret@db/x", "postgres://u:second-secret@db/y"
        ssh = FakeSsh((ok({"keys": ["KEEP", "REMOVE"]}), b"", 0))
        code, output = commands.diff_environment(manifest(), AdminClient("a@h", runner=ssh),
                                                 f"KEEP={other}\nADD={secret}\n")
        self.assertEqual(code, 0)
        self.assertEqual(output["added"], ["ADD"])
        self.assertEqual(output["removed"], ["REMOVE"])
        self.assertEqual(output["operation"], "diff")
        self.assertIn("key names only", output["note"].lower())
        sent = read_frame(io.BytesIO(ssh.calls[0][1]))
        self.assertEqual(sent["action"], "environment")
        self.assertEqual(sent["arguments"], {"operation": "keys"})
        self.assertNotIn(secret.encode(), ssh.calls[0][1])
        self.assertNotIn(other.encode(), ssh.calls[0][1])
        self.assertNotIn("secret", str(output).lower())

    def test_environment_diff_with_only_a_value_change_is_empty(self):
        ssh = FakeSsh((ok({"keys": ["KEEP"]}), b"", 0))
        code, output = commands.diff_environment(manifest(), AdminClient("a@h", runner=ssh), "KEEP=another-secret\n")
        self.assertEqual((code, output["added"], output["removed"]), (0, [], []))
        self.assertEqual(read_frame(io.BytesIO(ssh.calls[0][1]))["arguments"], {"operation": "keys"})
        self.assertNotIn("another-secret", str(output))

    def test_invalid_environment_diff_input_is_refused_before_any_connection(self):
        ssh = FakeSsh()
        client = AdminClient("a@h", runner=ssh)
        for text, expected in (("", "no variables"), ("# only a comment\n\n", "no variables"),
                               ("PORT=1\n", "managed by Shiplord"), ("BAD LINE\n", "not KEY=VALUE"),
                               ("A=1\nA=2\n", "set more than once")):
            with self.subTest(text=text), self.assertRaisesRegex(CommandError, expected):
                commands.diff_environment(manifest(), client, text)
        self.assertEqual(ssh.calls, [])


class RegistryCommandTests(unittest.TestCase):
    TOKEN = "ghp_exampleTOKEN1234567890"

    def test_the_token_travels_only_in_the_frame_and_is_never_returned(self):
        ssh = FakeSsh((ok({"registries": [{"registry": "ghcr.io", "username": "bot"}]}), b"", 0))
        code, result = commands.registry_login(manifest(), AdminClient("a@h", runner=ssh), registry="ghcr.io",
                                               username="bot", token=self.TOKEN)
        command, payload = ssh.calls[0]
        self.assertIn(self.TOKEN.encode(), payload)
        self.assertNotIn(self.TOKEN, " ".join(command))
        self.assertNotIn(self.TOKEN, str(result))
        self.assertEqual((code, result["registries"]), (0, [{"registry": "ghcr.io", "username": "bot"}]))
        self.assertIn("read-only", result["note"])

    def test_invalid_input_is_refused_before_any_connection(self):
        ssh = FakeSsh()
        client = AdminClient("a@h", runner=ssh)
        for kwargs in ({"registry": "docker.io"}, {"username": "a b"}, {"token": "has space"}, {"token": ""},
                       {"token": "x" * 513}, {"token": "a\nb"}):
            options = dict(registry="ghcr.io", username="bot", token=self.TOKEN)
            options.update(kwargs)
            with self.subTest(kwargs=str(kwargs)[:30]), self.assertRaises(CommandError) as caught:
                commands.registry_login(manifest(), client, **options)
            self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertEqual(ssh.calls, [])

    def test_logout_and_status_send_no_secret_material(self):
        ssh = FakeSsh((ok({"registries": []}), b"", 0), (ok({"registries": []}), b"", 0))
        commands.registry_other(manifest(), AdminClient("a@h", runner=ssh), "logout")
        commands.registry_other(manifest(), AdminClient("a@h", runner=ssh), "list")
        sent = [read_frame(io.BytesIO(payload))["arguments"] for _, payload in ssh.calls]
        self.assertEqual(sent, [{"operation": "logout", "registry": "ghcr.io"}, {"operation": "list"}])


class DeployCommandTests(ClientTestCase):
    def deploy(self, **overrides):
        options = dict(image=IMAGE, commit=COMMIT, sequence=1000, request_id="req-1", timeout=60,
                       public_check=lambda domain, path: {"ok": True, "status": 200, "error": None})
        options.update(overrides)
        import shiplord.client as client_module
        original = client_module.wait_for_job
        run = lambda *a, **k: (run_job(self.services, "orders", options["request_id"]),  # noqa: E731
                               original(*a, **dict(k, interval=0, sleep=lambda s: None)))[1]
        client_module.wait_for_job = run
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        return commands.deploy_application(manifest(), self.client, **options)

    def test_a_successful_deploy_reports_the_release_and_the_public_check(self):
        code, result = self.deploy()
        self.assertEqual((code, result["ok"], result["status"]), (0, True, "succeeded"))
        self.assertEqual(result["release"]["commit"], COMMIT)
        self.assertEqual(result["public"], {"ok": True, "status": 200, "error": None})
        self.assertEqual((result["request_id"], result["url"]), ("req-1", "https://orders.example.com"))

    def test_public_verification_is_reported_but_only_fails_the_command_when_required(self):
        failing = lambda domain, path: {"ok": False, "status": None, "error": "dns_unresolved"}  # noqa: E731
        code, result = self.deploy(public_check=failing)
        self.assertEqual((code, result["ok"], result["public"]["error"]), (0, True, "dns_unresolved"))
        code, result = self.deploy(public_check=failing, request_id="req-2", require_public=True)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("public HTTPS check failed", result["error"])
        code, result = self.deploy(request_id="req-3", verify_public=False)
        self.assertNotIn("public", result)

    def test_failed_releases_return_the_documented_nonzero_exit_codes(self):
        self.deploy()
        from fakes import record
        from shiplord.runtime import release_id
        self.runtime.start_errors[release_id(record(commit="c", image="d", sequence=1001))] = RuntimeFailure("boom", "start")
        code, result = self.deploy(request_id="req-2", image="ghcr.io/example/orders@sha256:" + "d" * 64,
                                   commit=OTHER, sequence=1001)
        self.assertEqual((code, result["ok"], result["status"]), (5, False, "rolled_back"))
        self.assertNotIn("url", result)  # a failed release reports no live URL
        stale_code, stale = self.deploy(request_id="req-3", sequence=1)
        self.assertEqual((stale_code, stale["error"]), (1, "stale_release"))

    def test_invalid_input_is_rejected_before_any_remote_call(self):
        self.commands.clear()
        for kwargs in ({"commit": "main"}, {"image": "ghcr.io/example/orders:latest"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CommandError):
                self.deploy(**kwargs)
        self.assertEqual(self.commands, [])

    def test_a_timed_out_deploy_reports_a_copyable_lookup_command(self):
        import shiplord.client as client_module
        original = client_module.wait_for_job
        seen = {}
        ticks = iter(range(0, 1000, 10))

        def wait(client, job_id, **options):
            seen.update(options)
            return original(client, job_id, **dict(options, interval=0, sleep=lambda seconds: None,
                                                    clock=lambda: next(ticks)))
        client_module.wait_for_job = wait
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        with self.assertRaises(ClientError) as caught:
            commands.deploy_application(manifest(), self.client, image=IMAGE, commit=COMMIT, sequence=1000,
                                        request_id="req-1", timeout=25)
        lookup = "shiplord status orders --server production --job-id req-1"
        self.assertEqual(seen["lookup_command"], lookup)
        self.assertIn(lookup, str(caught.exception))
        self.assertIn("may still be running", str(caught.exception))

    def test_a_finished_rollback_can_be_looked_up_with_a_zero_exit_code(self):
        self.deploy()
        from fakes import record
        from shiplord.runtime import release_id
        self.runtime.start_errors[release_id(record(commit="c", image="d", sequence=1001))] = RuntimeFailure("boom", "start")
        code, result = self.deploy(request_id="req-2", image="ghcr.io/example/orders@sha256:" + "d" * 64,
                                   commit=OTHER, sequence=1001)
        self.assertEqual((code, result["status"]), (5, "rolled_back"))
        code, status = commands.application_status(self.client, "req-2")
        self.assertEqual((code, status["job"]["state"], status["job"]["outcome"]["status"],
                          status["job"]["outcome"]["exit_code"]), (0, "finished", "rolled_back", 5))


def begin_unfinished_transaction(case):
    """Leave the journal of an interrupted deployment behind, as a killed worker would."""
    from fakes import record
    from shiplord.release_state import HostLock, ReleaseState
    var_fd = os.open(case.layout.var, os.O_RDONLY | os.O_DIRECTORY)
    state_fd = os.open(case.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
    case.addCleanup(os.close, var_fd)
    case.addCleanup(os.close, state_fd)
    lock = HostLock(var_fd)
    with lock.acquire():
        ReleaseState("orders", state_fd, lock).begin("req-x", record())


class QueryCommandTests(ClientTestCase):
    def test_plan_status_and_logs_use_read_only_actions(self):
        code, plan = commands.plan_application(manifest(), self.client, image=IMAGE, commit=COMMIT, sequence=1000)
        self.assertEqual((code, plan["action"], plan["route_change"]), (0, "deploy", "create"))
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.runtime.log_output = "tail=5\n"
        self.assertEqual(commands.application_logs(self.client, 5)[1]["logs"], "tail=5\n")
        self.assertEqual(self.runtime.log_arguments, {"release": None, "tail": 5, "since": None, "timestamps": False})
        for tail in (0, 1001):
            with self.assertRaises(CommandError):
                commands.application_logs(self.client, tail)
        code, status = commands.application_status(self.client)
        self.assertEqual((code, status["application"]["current"], status["job"]), (0, None, None))

    def test_follow_passes_bounded_arguments_and_returns_the_stop_reason(self):
        self.runtime.follow_chunks, self.runtime.follow_stopped = ["line\n"], "timeout"
        chunks = []
        code, result = commands.application_logs(self.client, 5, since="15m", timestamps=True, follow=True,
                                                 timeout=30, on_chunk=chunks.append)
        self.assertEqual((code, chunks, result["stopped"], result["release"]), (0, ["line\n"], "timeout", None))
        self.assertEqual(self.runtime.follow_arguments,
                         {"release": None, "tail": 5, "since": "15m", "timestamps": True, "duration": 30})

    def test_log_options_are_validated_before_any_connection(self):
        options = ({"since": "1d"}, {"since": 5}, {"follow": True, "timeout": 0},
                   {"follow": True, "timeout": 3601}, {"follow": True, "on_chunk": None})
        for choice in options:
            with self.subTest(choice=choice), self.assertRaises(CommandError):
                commands.application_logs(self.client, 5, **choice)
        self.assertEqual(self.commands, [])


    def test_job_lookup_is_read_only_and_reports_the_recorded_outcome(self):
        commit, arguments = self.deploy_arguments()
        self.client.call("deploy", request_id="req-1", commit=commit, arguments=arguments)
        launched = len(self.launcher.launched)
        code, status = commands.application_status(self.client, "req-1")
        self.assertEqual((code, status["job"]["state"], status["job"]["phase"]), (0, "queued", "queued"))
        self.assertEqual(len(self.launcher.launched), launched)  # a lookup starts no new work
        run_job(self.services, "orders", "req-1")
        code, status = commands.application_status(self.client, "req-1")
        self.assertEqual((code, status["job"]["state"], status["job"]["outcome"]["status"]),
                         (0, "finished", "succeeded"))

    def test_job_lookup_reports_an_unknown_job_and_rejects_a_malformed_id(self):
        code, status = commands.application_status(self.client, "missing")
        self.assertEqual((code, status["job"]), (0, None))
        before = len(self.commands)
        with self.assertRaises(CommandError) as malformed:
            commands.application_status(self.client, "../etc/passwd")
        self.assertEqual(malformed.exception.code, 2)
        self.assertEqual(len(self.commands), before)  # rejected before any remote request

    def test_rollback_and_recover_run_as_jobs_and_return_their_exit_codes(self):
        import shiplord.client as client_module
        original = client_module.wait_for_job
        calls = []

        def wait(client, job_id, **options):
            run_job(self.services, "orders", job_id)
            calls.append(job_id)
            return original(client, job_id, **dict(options, interval=0, sleep=lambda s: None))
        client_module.wait_for_job = wait
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        code, result = commands.run_job_command(manifest(), self.client, "recover", request_id="rc-1")
        self.assertEqual((code, result["status"]), (0, "nothing_to_recover"))
        code, result = commands.run_job_command(manifest(), self.client, "rollback", request_id="rb-1")
        self.assertEqual((code, result["ok"], result["error"]), (1, False, "no_previous_release"))
        self.assertEqual(calls, ["rc-1", "rb-1"])

    def recover_job(self, manifest_value, request_id):
        """Run the real recovery job through the helper, as a polling client would."""
        import shiplord.client as client_module
        original = client_module.wait_for_job

        def wait(client, job_id, **options):
            run_job(self.services, "orders", job_id)
            return original(client, job_id, **dict(options, interval=0, sleep=lambda s: None))
        client_module.wait_for_job = wait
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        return commands.run_job_command(manifest_value, self.client, "recover", request_id=request_id)

    def test_an_interrupted_deployment_recovers_without_a_local_manifest(self):
        begin_unfinished_transaction(self)
        code, result = self.recover_job(None, "rc-explicit")
        self.assertEqual((code, result["ok"], result["status"]), (0, True, "recovered"))
        self.assertIsNone(self.state.read_journal())
        self.assertEqual(self.runtime.calls, [("stop",)])

    def test_a_recovery_that_cannot_reconcile_keeps_its_exit_code_and_journal(self):
        begin_unfinished_transaction(self)
        self.routes_reload_failure()
        code, result = self.recover_job(None, "rc-failed")
        self.assertEqual((code, result["ok"], result["status"]), (6, False, "intervention_required"))
        self.assertIsNotNone(self.state.read_journal())

    def routes_reload_failure(self):
        self.caddy.fail_reload.add(self.caddy.reload_calls + 1)

    def test_a_named_recovery_without_a_manifest_waits_for_every_runtime(self):
        native_max = commands.job_timeout({"runtime": "native", "deploy": {"readiness_timeout_seconds": 900}})
        self.assertGreaterEqual(commands.explicit_job_timeout(), commands.job_timeout({"runtime": "docker"}))
        self.assertGreaterEqual(commands.explicit_job_timeout(), native_max)
        outcome = {"status": "recovered", "exit_code": 0, "detail": {}}
        with patch("shiplord.commands.submit_and_wait", return_value=outcome) as submit:
            code, result = commands.run_job_command(None, self.client, "recover", request_id="rc-explicit")
        self.assertEqual((code, result["ok"], result["status"]), (0, True, "recovered"))
        self.assertEqual(submit.call_args.kwargs["timeout"], commands.explicit_job_timeout())

    def test_a_manifest_recovery_keeps_the_manifest_wait_bound(self):
        outcome = {"status": "nothing_to_recover", "exit_code": 0, "detail": {}}
        with patch("shiplord.commands.submit_and_wait", return_value=outcome) as submit:
            code, result = commands.run_job_command(manifest(), self.client, "recover", request_id="rc-manifest")
        self.assertEqual((code, result["status"]), (0, "nothing_to_recover"))
        self.assertEqual(submit.call_args.kwargs["timeout"], commands.job_timeout(manifest()))

    def test_a_restart_runs_as_a_job_and_reports_the_unchanged_release(self):
        import shiplord.client as client_module

        original = client_module.wait_for_job

        def wait(client, job_id, **options):
            run_job(self.services, "orders", job_id)
            return original(client, job_id, **dict(options, interval=0, sleep=lambda s: None))
        client_module.wait_for_job = wait
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        code, result = commands.run_job_command(manifest(), self.client, "restart", request_id="rs-1")
        self.assertEqual((code, result["status"], result["exit_code"]), (0, "succeeded", 0))
        self.assertEqual(result["release"]["commit"], COMMIT)
        self.assertEqual(self.state.read_state()["current"]["commit"], COMMIT)

    def test_a_restart_with_nothing_deployed_returns_exit_two_without_a_job(self):
        code, result = commands.run_job_command(manifest(), self.client, "restart", request_id="rs-1")
        self.assertEqual((code, result["status"], result["error"]), (2, "rejected", "nothing_deployed"))
        self.assertIn("nothing to restart", result["message"])
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.assertEqual(self.launcher.launched, [])

    def test_restart_requires_the_helper_capability_before_any_job(self):
        old = {"status": "ok", "request_id": "x", "version": 1, "helper_version": "0.1.0",
               "actions": ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover",
                           "docker_storage_v1", "docker_shutdown_v1", "docker_generic_v1"]}
        with patch.object(self.client, "call", return_value=old) as call:
            with self.assertRaises(CommandError) as caught:
                commands.run_job_command(manifest(), self.client, "restart", request_id="rs-1")
            self.assertEqual(caught.exception.code, 4)
            self.assertIn("does not support restart", str(caught.exception))
            self.assertIn("shiplord server bootstrap", str(caught.exception))
            self.assertEqual([c.args[0] for c in call.call_args_list], ["capabilities"])  # no job was submitted

    def test_force_stop_requires_the_helper_capability_before_any_job(self):
        old = {"status": "ok", "request_id": "x", "version": 1, "helper_version": "0.1.0",
               "actions": ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover",
                           "docker_storage_v1", "docker_shutdown_v1", "docker_generic_v1"]}
        with patch.object(self.client, "call", return_value=old) as call:
            with self.assertRaisesRegex(CommandError, "does not support --force-stop"):
                commands.run_job_command(manifest(), self.client, "recover", force_stop=True)
            self.assertEqual([c.args[0] for c in call.call_args_list], ["capabilities"])

    def test_force_stop_is_recorded_in_the_recovery_outcome(self):
        import shiplord.client as client_module
        original = client_module.wait_for_job

        def wait(client, job_id, **options):
            run_job(self.services, "orders", job_id)
            return original(client, job_id, **dict(options, interval=0, sleep=lambda s: None))
        client_module.wait_for_job = wait
        self.addCleanup(setattr, client_module, "wait_for_job", original)
        code, result = commands.run_job_command(manifest(), self.client, "recover", request_id="rc-force",
                                                force_stop=True)
        self.assertEqual((code, result["status"]), (0, "recovered"))
        self.assertTrue(result["forced_stop"]["forced"])
        self.assertEqual(result["forced_stop"]["signal"], "SIGKILL")
        self.assertFalse(result["running"])


class DoctorTests(ClientTestCase):
    def doctor(self, **kwargs):
        options = dict(which=lambda name: "/usr/bin/" + name, resolve=lambda domain: [(0, 0, 0, "", ("192.0.2.1", 443))])
        options.update(kwargs)
        return commands.application_doctor(manifest(), self.client, **options)

    def test_all_checks_pass_and_are_reported_individually(self):
        code, result = self.doctor()
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual({name for name in result["checks"]}, {"ssh", "git", "gh", "server_access", "helper_actions",
                                                              "application", "dns"})
        self.assertEqual(result["checks"]["dns"]["addresses"], ["192.0.2.1"])

    def test_each_failure_is_named_and_returns_a_prerequisite_exit_code(self):
        code, result = self.doctor(which=lambda name: None if name == "gh" else "/usr/bin/" + name)
        self.assertEqual((code, result["checks"]["gh"]["ok"], result["checks"]["ssh"]["ok"]), (3, False, True))
        code, result = self.doctor(resolve=lambda domain: (_ for _ in ()).throw(socket.gaierror()))
        self.assertEqual((code, result["checks"]["dns"]["ok"]), (3, False))
        self.assertIn("DNS record", result["checks"]["dns"]["hint"])
        self.client.runner = lambda c, p, t: (frame({"status": "error", "error": "access_denied"}), b"", 3)
        code, result = self.doctor()
        self.assertEqual((code, result["checks"]["server_access"]["ok"]), (3, False))

    def test_an_unfinished_transaction_is_flagged_with_the_recovery_hint(self):
        self.state_begin()
        code, result = self.doctor()
        self.assertEqual(code, 3)
        self.assertEqual(result["checks"]["application"]["hint"], "run `shiplord recover`")

    def state_begin(self):
        begin_unfinished_transaction(self)


class InventoryCompatibilityTests(unittest.TestCase):
    def test_only_stable_releases_older_than_the_minimum_are_confirmed_unsupported(self):
        cases = {"0.0.9": "unsupported", "0.1.99": "unsupported", "0.2.0": "supported",
                 "0.10.0": "supported", "1.0.0": "supported", "0.2.0+abc.1": "supported",
                 "0.2.0-rc1": "unknown", "01.2.0": "unknown", "v0.2.0": "unknown", "": "unknown",
                 None: "unknown", 2: "unknown"}
        for version, expected in cases.items():
            with self.subTest(version=version):
                self.assertEqual(commands.inventory_support(version), expected)

    def test_upgrade_guidance_names_only_the_confirmed_old_helper(self):
        self.assertIsNone(commands.inventory_upgrade("alpha", "0.2.0"))
        self.assertIsNone(commands.inventory_upgrade("alpha", "0.2.0-rc1"))
        self.assertIsNone(commands.inventory_upgrade("alpha", None))
        self.assertEqual(commands.inventory_upgrade("alpha", "0.1.0"),
                         {"error": "App inventory requires helper 0.2.0 or later.",
                          "error_code": "helper_upgrade_required", "helper_version": "0.1.0",
                          "minimum_helper_version": "0.2.0",
                          "upgrade_command": "shiplord server bootstrap alpha"})


class FleetDoctorTests(unittest.TestCase):
    SERVERS = [{"alias": "alpha", "ssh": "admin@alpha.example.com"},
               {"alias": "beta", "ssh": "admin@beta.example.com"},
               {"alias": "gamma", "ssh": "admin@gamma.example.com"}]

    def probe(self, versions=None, failures=None, calls=None):
        versions, failures = versions or {}, failures or {}

        def run(target, timeout):
            if calls is not None:
                calls.append((target, timeout))
            alias = target.split("@")[1].split(".")[0]
            if alias in failures:
                raise failures[alias]
            return {"helper_version": versions.get(alias, "0.2.0")}
        return run

    def test_a_mixed_fleet_reports_every_alias_and_keeps_probing(self):
        calls = []
        probe = self.probe(versions={"gamma": "0.1.0"}, failures={"beta": TransportError("timed out")}, calls=calls)
        code, result = commands.doctor_all_servers(self.SERVERS, probe=probe)
        self.assertEqual(code, 3)
        self.assertEqual((result["ok"], result["partial"]), (False, True))
        self.assertEqual([item["server"] for item in result["servers"]], ["alpha", "beta", "gamma"])
        self.assertEqual(len(calls), 3)  # a failed probe never stops the sweep
        self.assertTrue(all(timeout == commands.FLEET_PROBE_TIMEOUT_SECONDS for _, timeout in calls))
        alpha, beta, gamma = result["servers"]
        self.assertEqual((alpha["ok"], alpha["reachable"], alpha["inventory_support"]), (True, True, "supported"))
        self.assertEqual(alpha["helper_version"], "0.2.0")
        self.assertNotIn("upgrade_command", alpha)
        self.assertEqual((beta["ok"], beta["reachable"], beta["inventory_support"]), (False, False, "unknown"))
        self.assertNotIn("upgrade_command", beta)
        self.assertEqual((gamma["ok"], gamma["reachable"], gamma["inventory_support"]), (False, True, "unsupported"))
        self.assertEqual(gamma["error_code"], "helper_upgrade_required")
        self.assertEqual(gamma["upgrade_command"], "shiplord server bootstrap gamma")

    def test_a_complete_compatible_fleet_exits_zero(self):
        code, result = commands.doctor_all_servers(self.SERVERS, probe=self.probe())
        self.assertEqual((code, result["ok"], result["partial"]), (0, True, False))
        self.assertTrue(all(item["inventory_support"] == "supported" for item in result["servers"]))

    def test_an_incompatible_protocol_is_reachable_but_not_confirmed_old(self):
        code, result = commands.doctor_all_servers(
            [self.SERVERS[0]], probe=self.probe(failures={"alpha": IncompatibleProtocol("unsupported version")}))
        self.assertEqual(code, 3)
        item = result["servers"][0]
        self.assertEqual((item["reachable"], item["inventory_support"]), (True, "unknown"))
        self.assertEqual(item["error_code"], "incompatible_protocol")
        self.assertIsNone(item["helper_version"])
        self.assertNotIn("upgrade_command", item)

    def test_an_unreadable_registration_is_unreachable_not_old(self):
        code, result = commands.doctor_all_servers(
            [self.SERVERS[0]], probe=self.probe(failures={"alpha": ServerError("unreadable")}))
        self.assertEqual(code, 3)
        self.assertEqual((result["servers"][0]["reachable"], result["servers"][0]["inventory_support"]),
                         (False, "unknown"))
        self.assertNotIn("upgrade_command", result["servers"][0])

    def test_an_unorderable_version_is_not_reported_as_unsupported(self):
        code, result = commands.doctor_all_servers(
            [self.SERVERS[0]], probe=self.probe(versions={"alpha": "0.2.0-rc1"}))
        item = result["servers"][0]
        self.assertEqual((code, item["ok"], item["inventory_support"]), (0, True, "unknown"))
        self.assertNotIn("upgrade_command", item)


def tls_server(cert: Path, key: Path, statuses: list[int]) -> int:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    calls = iter(statuses + [statuses[-1]] * 50)

    def run():
        while True:
            try:
                raw, _ = listener.accept()
                with context.wrap_socket(raw, server_side=True) as tls:
                    tls.recv(2048)
                    tls.sendall(f"HTTP/1.1 {next(calls)} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            except (OSError, ssl.SSLError):
                if listener.fileno() == -1:
                    return
    threading.Thread(target=run, daemon=True).start()
    tls_server.listener = listener
    return listener.getsockname()[1]


class PublicCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-days", "1", "-subj", "/CN=orders.example.com", "-addext", "subjectAltName=DNS:orders.example.com",
                        "-keyout", str(directory / "k.pem"), "-out", str(directory / "c.pem")], check=True, capture_output=True)
        cls.cert, cls.key = directory / "c.pem", directory / "k.pem"

    def check(self, statuses, trusted=True, **kwargs):
        port = tls_server(self.cert, self.key, statuses)
        self.addCleanup(tls_server.listener.close)
        context = ssl.create_default_context(cafile=str(self.cert)) if trusted else None
        options = dict(timeout=30, interval=0, sleep=lambda s: None, context=context, port=port, host="127.0.0.1")
        options.update(kwargs)
        return commands.check_public_https("orders.example.com", "/ready", **options)

    def test_a_verified_200_passes_and_retries_while_the_service_settles(self):
        self.assertEqual(self.check([200]), {"ok": True, "status": 200, "error": None})
        self.assertTrue(self.check([503, 502, 200])["ok"])

    def test_failures_are_classified_after_the_deadline(self):
        ticks = iter(range(0, 10000, 20))
        self.assertEqual(self.check([503], clock=lambda: next(ticks)), {"ok": False, "status": 503, "error": "unexpected_status"})
        ticks = iter(range(0, 10000, 20))
        self.assertEqual(self.check([200], trusted=False, clock=lambda: next(ticks))["error"], "certificate_not_trusted")
        ticks = iter(range(0, 10000, 20))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        result = commands.check_public_https("orders.example.com", "/ready", timeout=10, interval=0, sleep=lambda s: None,
                                             clock=lambda: next(ticks), port=closed, host="127.0.0.1")
        self.assertEqual(result["error"], "connection_failed")
        ticks = iter(range(0, 10000, 20))
        unresolved = commands.check_public_https("no-such-host.invalid", "/ready", timeout=10, interval=0,
                                                 sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(unresolved["error"], "dns_unresolved")


if __name__ == "__main__":
    unittest.main()
