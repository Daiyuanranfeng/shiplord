from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shiplord.layout import Layout
from shiplord.runtime import (MAX_FOLLOW_SECONDS, MAX_LOG_BYTES, DockerRuntime, RuntimeFailure, release_id,
                              validate_follow_timeout, validate_since)
from shiplord.system import CommandError, CommandResult, run_command

ID_A, ID_B = "a" * 64, "b" * 64


def release(commit="a", image="b", sequence=1000, port=8080, origins=(), memory=512, storage=None, template="google-adk"):
    value = {"commit": commit * 40, "sequence": sequence,
            "artifact": {"kind": "docker", "image": "ghcr.io/example/orders@sha256:" + image * 64},
            "configuration": {"schema_version": 1, "name": "orders", "server": "production", "template": template,
                              "runtime": "docker", "http": {"api_domain": "orders.example.com",
                              "frontend_origins": list(origins), "container_port": port, "readiness_path": "/ready"},
                              "resources": {"memory_mb": memory},
                              "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2}}}
    if storage is not None:
        value["configuration"]["storage"] = storage
    return value


LEGACY_VOLUME = CommandResult(0, "local|null|null|2026-10-30T00:00:00Z|/var/lib/docker/volumes/shiplord-orders-artifacts/_data\n")
LABELLED_VOLUME = CommandResult(0, 'local|null|{"shiplord.app":"orders"}|2026-10-30T00:00:00Z|'
                               '/var/lib/docker/volumes/shiplord-orders-artifacts/_data\n')
EXITED_LEGACY = CommandResult(0, "exited unless-stopped |<no value>|<no value>\n")  # a stopped container from before the policy
REGISTRATION = {"application_id": "orders", "host_port": 20007, "api_domain": "orders.example.com"}


class FakeDocker:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.max_outputs: list[int] = []
        self.results: dict[str, CommandResult] = {}
        self.raise_for: dict[str, CommandError] = {}

    @staticmethod
    def _key(argv):
        """The command a call names; a pull with the application's login still names its own command."""
        rest = list(argv[1:])
        if rest[:1] == ["--config"]:
            rest = rest[2:]
        return " ".join(rest[:2]) if rest[0] in ("image", "volume") else rest[0]

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        argv = list(argv)
        self.calls.append(argv)
        self.max_outputs.append(max_output)
        assert argv[0] == "/usr/bin/docker"
        key = self._key(argv)
        if key in self.raise_for:
            raise self.raise_for[key]
        return self.results.get(key, CommandResult(0, ""))

    def sub(self, name):
        return [c for c in self.calls if self._key(c) == name]


class StreamingDocker(FakeDocker):
    """A Docker whose follow stream is scripted; the argv and deadline are what the test inspects."""

    def __init__(self):
        super().__init__()
        self.stream_calls: list[tuple[list[str], float]] = []
        self.chunks = ["one\n", "two\n"]
        self.reason = "timeout"

    def stream(self, argv, *, timeout, on_chunk):
        self.stream_calls.append((list(argv), timeout))
        for chunk in self.chunks:
            on_chunk(chunk)
        return self.reason


class LabelledDocker:
    """Answers ``docker ps`` from the labels in its own argv, so release selection is what is under test."""

    def __init__(self, containers):
        self.containers = containers  # (id, labels) newest first, like docker ps -aq
        self.calls: list[list[str]] = []

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1] != "ps":
            return CommandResult(0, "the current release\n")
        wanted = {}
        for part in argv:
            if part.startswith("label="):
                key, value = part[len("label="):].split("=", 1)
                wanted[key] = value
        matches = [identifier for identifier, labels in self.containers
                   if all(labels.get(key) == value for key, value in wanted.items())]
        return CommandResult(0, "\n".join(matches) + ("\n" if matches else ""))


class RuntimeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Layout(Path(self.temp.name).resolve())
        (self.layout.environments / "orders").mkdir(parents=True)
        self.docker = FakeDocker()
        self.docker.results["volume inspect"] = LABELLED_VOLUME
        self.docker.results["inspect"] = EXITED_LEGACY
        self.runtime = DockerRuntime(self.docker, self.layout, platform_name="linux/amd64")


class FakeStopState:
    def __init__(self, fail_phase=None):
        self.record = None
        self.fail_phase = fail_phase

    def read_journal(self):
        return None if self.record is None else {"stop": dict(self.record)}

    def write_stop(self, record):
        if record["phase"] == self.fail_phase:
            self.fail_phase = None
            raise RuntimeError("simulated worker interruption")
        self.record = dict(record)

    def clear_stop(self, container_id):
        if self.record is None or self.record["container_id"] != container_id:
            raise AssertionError("clearing an unknown stop record")
        self.record = None


class StatefulStopDocker:
    def __init__(self, *, exit_on_signal=False, action="force", signal="", container_id=ID_A):
        self.status = "running"
        self.restart_policy = "unless-stopped"
        self.calls = []
        self.exit_on_signal = exit_on_signal
        self.exit_on_kill = True
        self.action = action
        self.signal = signal
        self.container_id = container_id
        self.exists = True

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        argv = list(argv)
        self.calls.append(argv)
        command = argv[1]
        if command == "ps":
            return CommandResult(0, self.container_id + "\n" if self.exists else "")
        if command == "update":
            self.restart_policy = "no"
            return CommandResult(0, "")
        if command == "kill":
            if argv[2] == "--signal" and argv[3] == "SIGTERM" and self.exit_on_signal:
                self.status = "exited"
            if argv[2] == "--signal" and argv[3] == "KILL" and self.exit_on_kill:
                self.status = "exited"
            return CommandResult(0, "")
        if command == "inspect":
            if any("RestartPolicy" in argument for argument in argv):
                return CommandResult(0, f"{self.status} {self.restart_policy} {self.signal}|2|{self.action}\n")
            return CommandResult(0, self.status + "\n")
        if command == "rm":
            if self.status == "running":
                return CommandResult(1, "cannot remove a running container")
            self.exists = False
            return CommandResult(0, "")
        raise AssertionError(f"unexpected command: {argv}")


class DurableShutdownTests(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.stateful = StatefulStopDocker(exit_on_signal=True)
        self.clock = [0.0]
        self.wall = [100.0]
        self.runtime = DockerRuntime(self.stateful, self.layout, platform_name="linux/amd64",
                                     clock=lambda: self.clock[0], sleep=lambda _: None,
                                     wall_clock=lambda: self.wall[0])

    @staticmethod
    def container(*, action="force", restart_policy="unless-stopped", timeout=2):
        return {"id": ID_A, "status": "running", "restart_policy": restart_policy, "signal": "SIGTERM",
                "timeout": timeout, "action": action}

    def test_recovery_reconciles_restart_policy_update_before_signalling(self):
        state = FakeStopState(fail_phase="restart_disabled")
        with self.assertRaisesRegex(RuntimeError, "simulated worker interruption"):
            self.runtime._stop_gracefully(self.container(), state)
        self.assertEqual(state.record["phase"], "intent")
        self.assertEqual(self.stateful.restart_policy, "no")

        self.runtime._stop_gracefully(self.container(restart_policy="no"), state)
        updates = [call for call in self.stateful.calls if call[1] == "update"]
        signals = [call for call in self.stateful.calls if call[1] == "kill"]
        self.assertEqual(len(updates), 1)
        self.assertEqual([call[3] for call in signals], ["SIGTERM"])
        self.assertEqual(state.record["phase"], "signalled")

    def test_recovery_retries_uncertain_signal_and_fail_policy_never_kills(self):
        self.stateful.exit_on_signal = False
        state = FakeStopState(fail_phase="signalled")
        with self.assertRaisesRegex(RuntimeError, "simulated worker interruption"):
            self.runtime._stop_gracefully(self.container(action="fail"), state)
        self.assertEqual(state.record["phase"], "signal_pending")

        self.wall[0] = 102.0  # the persisted deadline expired while the worker was down
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime._stop_gracefully(self.container(action="fail", restart_policy="no"), state)
        self.assertEqual(caught.exception.kind, "shutdown_timeout")
        signals = [call[3] for call in self.stateful.calls if call[1] == "kill"]
        self.assertEqual(signals, ["SIGTERM", "SIGTERM"])
        self.assertEqual(state.record["phase"], "signalled")

    def test_recovery_retries_kill_when_the_worker_stops_before_recording_it(self):
        self.stateful.exit_on_signal = False
        self.stateful.exit_on_kill = False
        self.wall[0] = 102.0
        record = {"container_id": ID_A, "restart_policy": "unless-stopped", "timeout_seconds": 2,
                  "action": "force", "signal": "SIGTERM", "deadline_epoch": 102, "phase": "kill_pending"}
        state = FakeStopState(fail_phase="killed")
        state.record = dict(record)
        with self.assertRaisesRegex(RuntimeError, "simulated worker interruption"):
            self.runtime._stop_gracefully(self.container(restart_policy="no"), state)
        self.assertEqual(state.record["phase"], "kill_pending")

        self.stateful.exit_on_kill = True
        self.runtime._stop_gracefully(self.container(restart_policy="no"), state)
        kills = [call for call in self.stateful.calls if call[1] == "kill" and call[3] == "KILL"]
        self.assertEqual(len(kills), 2)
        self.assertEqual(state.record["phase"], "killed")


class ForcedStopTests(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.clock = [0.0]
        self.wall = [100.0]
        self.stateful = StatefulStopDocker(action="fail")
        self.runtime = DockerRuntime(self.stateful, self.layout, platform_name="linux/amd64",
                                     clock=lambda: self.clock[0], sleep=lambda _: None,
                                     wall_clock=lambda: self.wall[0])

    def kills(self):
        return [call[3] for call in self.stateful.calls if call[1] == "kill"]

    def test_a_legacy_container_without_a_saved_record_is_killed_and_removed(self):
        self.stateful.restart_policy = "no"  # an older helper disabled restart but saved no stop intent
        self.runtime.force_stop(REGISTRATION)
        self.assertEqual(self.kills(), ["KILL"])  # restart "no" means a previous helper already signalled it
        self.assertFalse(self.stateful.exists)
        self.assertTrue(self.runtime.stop_report[0]["forced"])

    def test_a_paused_container_is_still_refused_before_any_signal(self):
        self.stateful.status, self.stateful.restart_policy = "paused", "no"
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime.force_stop(REGISTRATION)
        self.assertEqual(caught.exception.kind, "paused")
        self.assertEqual([call for call in self.stateful.calls if call[1] in ("kill", "update", "rm")], [])

    def test_two_live_containers_are_refused_before_anything_is_signalled(self):
        docker = FakeDocker()
        docker.results["ps"] = CommandResult(0, f"{ID_A}\n{ID_B}\n")
        docker.results["inspect"] = CommandResult(0, "running unless-stopped |2|fail\n")
        runtime = DockerRuntime(docker, self.layout, platform_name="linux/amd64")
        with self.assertRaises(RuntimeFailure) as caught:
            runtime.force_stop(REGISTRATION)
        self.assertEqual(caught.exception.kind, "ambiguous")
        self.assertEqual(docker.sub("kill") + docker.sub("update") + docker.sub("rm"), [])


class IdentityTests(unittest.TestCase):
    def test_release_ids_are_stable_short_and_sensitive_to_every_input(self):
        base = release_id(release())
        self.assertEqual(base, release_id(release()))
        self.assertRegex(base, r"^[0-9a-f]{12}$")
        for changed in (release(commit="c"), release(image="d"), release(sequence=1001)):
            self.assertNotEqual(base, release_id(changed))
        self.assertEqual(base, release_id(release(port=9000)))  # configuration is not part of the identity


class PullTests(RuntimeTestCase):
    def test_pulls_by_digest_and_changes_nothing_else(self):
        self.runtime.pull(REGISTRATION, release())
        self.assertEqual(self.docker.calls, [["/usr/bin/docker", "pull", "--quiet",
                                              "ghcr.io/example/orders@sha256:" + "b" * 64]])
        self.assertEqual(self.docker.sub("rm"), [])  # nothing is stopped or removed while fetching

    def test_a_private_image_is_pulled_with_that_applications_own_login_and_nothing_else_uses_it(self):
        login = self.layout.registry_logins / "orders"
        login.mkdir(parents=True)
        (login / "config.json").write_text('{"auths": {}}')
        self.runtime.pull(REGISTRATION, release())
        self.assertEqual(self.docker.calls[0][:4], ["/usr/bin/docker", "--config", str(login), "pull"])
        other = dict(REGISTRATION, application_id="billing")  # another application has no login and uses none
        self.docker.calls.clear()
        self.runtime.pull(other, release())
        self.assertNotIn("--config", self.docker.calls[0])
        self.docker.calls.clear()
        self.docker.results["ps"] = CommandResult(0, "")
        self.runtime.start(REGISTRATION, release())
        self.assertTrue(all("--config" not in call for call in self.docker.calls))

    def test_a_refused_pull_is_a_prepare_failure_with_its_output(self):
        self.docker.results["pull"] = CommandResult(1, "denied: requested access")
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime.pull(REGISTRATION, release())
        self.assertEqual((caught.exception.phase, caught.exception.detail), ("prepare", "denied: requested access"))

    def test_a_nearly_full_disk_stops_the_pull_before_anything_is_pulled(self):
        with patch("shiplord.runtime.shutil.disk_usage", return_value=type("U", (), {"free": 10 * 2**20})()):
            with self.assertRaisesRegex(RuntimeFailure, "not enough free disk space.*10 MiB free") as caught:
                self.runtime.pull(REGISTRATION, release())
        self.assertEqual(caught.exception.phase, "prepare")
        self.assertEqual(self.docker.calls, [])
        with patch("shiplord.runtime.shutil.disk_usage", side_effect=OSError):  # unmeasurable: docker decides
            self.runtime.pull(REGISTRATION, release())
        self.assertTrue(self.docker.calls)

    def test_a_timed_out_pull_is_reported_with_its_output(self):
        self.docker.raise_for["pull"] = CommandError("timed out", "timeout", "layer 3/9")
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime.pull(REGISTRATION, release())
        self.assertEqual((caught.exception.phase, caught.exception.detail), ("prepare", "layer 3/9"))


class PrepareTests(RuntimeTestCase):
    def test_verifies_the_pulled_image_by_digest_without_pulling_again(self):
        self.docker.results["image inspect"] = CommandResult(0, f"linux/amd64|{'a' * 40}|\n")
        result = self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(result, {"platform": "linux/amd64", "revision_label": "a" * 40})
        self.assertEqual(self.docker.sub("pull"), [])  # a locked transaction never waits on a registry
        self.assertEqual(self.docker.calls[0][:3], ["/usr/bin/docker", "image", "inspect"])
        self.assertIn("ghcr.io/example/orders@sha256:" + "b" * 64, self.docker.calls[0])
        self.assertEqual(self.docker.sub("rm"), [])  # nothing is stopped or removed while preparing

    def test_missing_revision_label_is_allowed_but_reported(self):
        self.docker.results["image inspect"] = CommandResult(0, "linux/amd64|<no value>|\n")
        self.assertIsNone(self.runtime.prepare(REGISTRATION, release())["revision_label"])

    def test_an_image_that_is_not_on_the_host_is_a_prepare_failure_that_changes_nothing(self):
        # A caller that did not pre-pull (or whose pre-pull failed) gets one fetch, never a bare refusal.
        self.docker.results["image inspect"] = CommandResult(1, "Error: No such image: ghcr.io/example/orders")
        with self.assertRaises(RuntimeFailure) as caught:
            self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(caught.exception.phase, "prepare")
        self.assertIn("not available on this host", str(caught.exception))
        self.assertIn("nothing was changed", str(caught.exception))
        self.assertEqual(len(self.docker.sub("pull")), 1)  # one fetch attempt, then the same failure
        self.assertEqual((self.docker.sub("rm"), self.docker.sub("run")), ([], []))

    def test_each_verification_failure_is_a_prepare_failure(self):
        cases = {
            "inspect fails": ("image inspect", CommandResult(1, "no such image")),
            "unreadable inspect output": ("image inspect", CommandResult(0, "linux/amd64\n")),
            "wrong platform": ("image inspect", CommandResult(0, "linux/arm64||\n")),
            "wrong revision": ("image inspect", CommandResult(0, f"linux/amd64|{'f' * 40}|\n")),
        }
        for label, (key, result) in cases.items():
            with self.subTest(label):
                self.docker.results = {"image inspect": CommandResult(0, "linux/amd64||\n"), key: result}
                with self.assertRaises(RuntimeFailure) as caught:
                    self.runtime.prepare(REGISTRATION, release())
                self.assertEqual(caught.exception.phase, "prepare")

    def test_a_nearly_full_disk_stops_the_deploy_before_anything_is_pulled_or_changed(self):
        with patch("shiplord.runtime.shutil.disk_usage", return_value=type("U", (), {"free": 10 * 2**20})()):
            with self.assertRaisesRegex(RuntimeFailure, "not enough free disk space.*10 MiB free") as caught:
                self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(caught.exception.phase, "prepare")
        self.assertEqual(self.docker.calls, [])
        with patch("shiplord.runtime.shutil.disk_usage", side_effect=OSError):  # unmeasurable: docker decides
            self.docker.results["image inspect"] = CommandResult(0, "linux/amd64||\n")
            self.runtime.prepare(REGISTRATION, release())
        self.assertTrue(self.docker.calls)

    def test_an_image_that_cannot_be_stopped_gracefully_is_refused_before_anything_else(self):
        image = release()["artifact"]["image"]
        for configured in ("SIGKILL", "KILL", "9", "SIGSTOP", "19", "SIGFOO"):
            with self.subTest(configured):
                self.docker.calls.clear()
                self.docker.results["image inspect"] = CommandResult(0, f"linux/amd64|{'a' * 40}|{configured}\n")
                with self.assertRaises(RuntimeFailure) as caught:
                    self.runtime.prepare(REGISTRATION, release())
                self.assertEqual(caught.exception.phase, "prepare")
                self.assertIn(f"STOPSIGNAL {configured}", str(caught.exception))
                self.assertIn(image, str(caught.exception))
                # nothing is inspected, started or removed after the refusal
                self.assertEqual((self.docker.sub("volume inspect"), self.docker.sub("run"), self.docker.sub("rm")),
                                 ([], [], []))

    def test_an_image_with_a_catchable_or_absent_stop_signal_still_prepares(self):
        for configured in ("SIGTERM", "SIGINT", "SIGQUIT", "15", "", "<no value>"):
            with self.subTest(configured):
                self.docker.results["image inspect"] = CommandResult(0, f"linux/amd64|{'a' * 40}|{configured}\n")
                result = self.runtime.prepare(REGISTRATION, release())
                self.assertEqual(result, {"platform": "linux/amd64", "revision_label": "a" * 40})

class StartTests(RuntimeTestCase):
    def start(self, **kwargs):
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n" + ID_B + "\n")
        self.runtime.start(REGISTRATION, release(**kwargs))
        return self.docker.sub("run")[0]

    def test_generic_runs_with_only_the_shared_managed_environment(self):
        argv = self.start(template="generic")
        environment = {argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--env"}
        self.assertEqual(environment, {"HOST=0.0.0.0", "PORT=8080", "LOG_LEVEL=INFO"})
        self.assertIn("shiplord.template=generic", argv)

    def test_replaces_existing_containers_then_runs_the_release_with_confinement(self):
        argv = self.start(origins=["https://orders.example.com"])
        names = [c[1] for c in self.docker.calls]
        self.assertLess(names.index("rm"), names.index("run"))
        self.assertEqual(self.docker.sub("rm"), [["/usr/bin/docker", "rm", ID_A], ["/usr/bin/docker", "rm", ID_B]])  # never --force
        self.assertEqual(self.docker.sub("ps")[0][-1], "label=shiplord.app=orders")
        self.assertEqual(argv[-1], "ghcr.io/example/orders@sha256:" + "b" * 64)
        pairs = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
        self.assertEqual(pairs["--publish"], "127.0.0.1:20007:8080")  # loopback only
        self.assertEqual((pairs["--memory"], pairs["--memory-swap"]), ("512m", "512m"))
        self.assertEqual(pairs["--restart"], "unless-stopped")
        self.assertEqual(pairs["--volume"], "shiplord-orders-artifacts:/app/src/.adk")
        self.assertEqual(pairs["--security-opt"], "no-new-privileges")
        self.assertEqual(pairs["--cap-drop"], "ALL")
        self.assertIn("max-size=10m", argv)
        self.assertEqual(argv[argv.index("--name") + 1], DockerRuntime.container_name(REGISTRATION, release()))
        environment = {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
                       for i in range(len(argv) - 1) if argv[i] == "--env"}
        self.assertEqual(environment["ALLOW_ORIGINS"], '["https://orders.example.com"]')
        self.assertEqual((environment["HOST"], environment["PORT"], environment["SERVE_WEB_INTERFACE"],
                          environment["RELOAD_AGENTS"]), ("0.0.0.0", "8080", "false", "false"))
        self.assertNotIn("--env-file", argv)

    def test_no_frontend_serializes_an_explicit_empty_origin_list(self):
        argv = self.start()
        self.assertIn("ALLOW_ORIGINS=[]", argv)

    def test_environment_file_is_passed_before_managed_values_so_they_win(self):
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("DATABASE_URL=postgres://x\n")
        path.chmod(0o600)
        argv = self.start()
        self.assertEqual(argv[argv.index("--env-file") + 1], str(path))
        self.assertLess(argv.index("--env-file"), argv.index("--env"))

    def test_an_unusable_environment_file_fails_before_the_old_app_is_touched(self):
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("PORT=1\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeFailure, "environment file is unusable"):
            self.runtime.start(REGISTRATION, release())
        self.assertEqual(self.docker.calls, [])
        path.write_text("OK=1\n")
        path.chmod(0o644)
        with self.assertRaises(RuntimeFailure):
            self.runtime.start(REGISTRATION, release())
        self.assertEqual(self.docker.calls, [])

    def test_failures_to_list_remove_or_run_are_reported(self):
        for key, result, phase in (("ps", CommandResult(1, "daemon down"), "stop"), ("rm", CommandResult(1, "busy"), "stop"),
                                   ("run", CommandResult(125, "bad flag"), "start")):
            with self.subTest(key):
                self.docker.calls.clear()
                self.docker.results = {"volume inspect": LABELLED_VOLUME, "inspect": EXITED_LEGACY,
                                       "ps": CommandResult(0, ID_A + "\n"), key: result}
                with self.assertRaises(RuntimeFailure) as caught:
                    self.runtime.start(REGISTRATION, release())
                self.assertEqual(caught.exception.phase, phase)

    def test_unexpected_container_listing_output_is_not_passed_to_docker_rm(self):
        self.docker.results["ps"] = CommandResult(0, "--all\n")
        with self.assertRaisesRegex(RuntimeFailure, "unexpected container listing"):
            self.runtime.start(REGISTRATION, release())
        self.assertEqual(self.docker.sub("rm"), [])

    def test_stop_removes_only_labelled_containers(self):
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        self.runtime.stop(REGISTRATION)
        self.assertEqual(self.docker.sub("rm")[0], ["/usr/bin/docker", "rm", ID_A])
        self.docker.calls.clear()
        self.docker.results["ps"] = CommandResult(0, "")
        self.runtime.stop(REGISTRATION)
        self.assertEqual(self.docker.sub("rm"), [])


class StartImageTests(RuntimeTestCase):
    """A start must fetch a release's image itself: an implicit `docker run` pull is anonymous."""

    IMAGE = "ghcr.io/example/orders@sha256:" + "b" * 64

    def start(self, value=None):
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        self.runtime.start(REGISTRATION, release() if value is None else value)

    def login(self):
        login = self.layout.registry_logins / "orders"
        login.mkdir(parents=True, exist_ok=True)
        (login / "config.json").write_text('{"auths": {}}')
        return login

    def test_an_image_that_is_on_the_host_is_never_fetched_again(self):
        self.docker.results["image inspect"] = CommandResult(0, "sha256:" + ID_A + "\n")
        self.start()
        self.assertEqual(self.docker.sub("pull"), [])  # a deploy already pulled it in prepare: no registry round trip
        self.assertEqual(self.docker.calls[0], ["/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", self.IMAGE])
        self.assertEqual(self.docker.sub("run")[0][-1], self.IMAGE)

    def test_a_missing_image_is_fetched_with_this_applications_login_before_anything_is_stopped(self):
        login = self.login()
        self.docker.results["image inspect"] = CommandResult(1, "Error: No such image")
        self.start()
        self.assertEqual(self.docker.sub("pull"),
                         [["/usr/bin/docker", "--config", str(login), "pull", "--quiet", self.IMAGE]])
        fetched = self.docker.calls.index(self.docker.sub("pull")[0])
        self.assertLess(fetched, self.docker.calls.index(self.docker.sub("rm")[0]))  # the old release keeps serving
        self.assertLess(fetched, self.docker.calls.index(self.docker.sub("run")[0]))
        self.assertEqual(self.docker.sub("run")[0][-1], self.IMAGE)

    def test_a_start_that_cannot_fetch_the_image_leaves_the_previous_release_alone(self):
        self.login()
        for host in ("ghcr.io", "localhost:5000"):
            with self.subTest(host):
                self.docker.calls.clear()
                value = release()
                value["artifact"]["image"] = f"{host}/example/orders@sha256:" + "b" * 64
                self.docker.results = {"volume inspect": LABELLED_VOLUME, "ps": CommandResult(0, ID_A + "\n"),
                                       "image inspect": CommandResult(1, "Error: No such image"),
                                       "pull": CommandResult(1, "unauthorized: authentication required")}
                with self.assertRaises(RuntimeFailure) as caught:
                    self.runtime.start(REGISTRATION, value)
                self.assertEqual(caught.exception.phase, "start")
                self.assertIn(f"the release's image could not be fetched from {host}", str(caught.exception))
                self.assertIn("the previous release is still running", str(caught.exception))
                self.assertEqual(caught.exception.detail, "")  # raw pull output can echo what the pull was given
                self.assertEqual((self.docker.sub("rm"), self.docker.sub("run")), ([], []))

    def test_a_nearly_full_disk_stops_a_start_before_it_pulls_or_touches_anything(self):
        self.docker.results["image inspect"] = CommandResult(1, "Error: No such image")
        with patch("shiplord.runtime.shutil.disk_usage", return_value=type("U", (), {"free": 10 * 2**20})()):
            with self.assertRaisesRegex(RuntimeFailure, "not enough free disk space.*10 MiB free") as caught:
                self.runtime.start(REGISTRATION, release())
        self.assertEqual(caught.exception.phase, "start")
        self.assertEqual((self.docker.sub("pull"), self.docker.sub("volume inspect"), self.docker.sub("rm"),
                          self.docker.sub("run")), ([], [], [], []))


class InspectTests(RuntimeTestCase):
    MANAGED = ("HOST=0.0.0.0", "PORT=8080", "LOG_LEVEL=INFO", "AGENT_DIR=/app/src", "AGENT_NAME=orders",
               "RELOAD_AGENTS=false", "SERVE_WEB_INTERFACE=false", "ALLOW_ORIGINS=[]")

    def environment(self, *extra, **overrides):
        values = dict(item.partition("=")[::2] for item in (*self.MANAGED, *extra))
        values.update(overrides)
        return json.dumps([f"{key}={value}" for key, value in values.items()])

    def inspect_output(self, state="true false", image="b", mounts=("volume:shiplord-orders-artifacts:/app/src/.adk",),
                       timeout="<no value>", action="<no value>", template="google-adk",
                       memory=512 * 1024 * 1024, memory_swap=512 * 1024 * 1024, pids=512,
                       restart="unless-stopped", security='["no-new-privileges"]', capabilities='["ALL"]',
                       log_driver="json-file", log_options='{"max-size":"10m","max-file":"3"}',
                       ports='{"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"20007"}]}',
                       environment=None, repository="ghcr.io/example/orders"):
        environment = self.environment() if environment is None else environment
        return CommandResult(0, f"{state} {repository}@sha256:{image * 64}|{timeout}|{action}|{template}|"
                                f" {' '.join(mounts)}|{memory}|{memory_swap}|{pids}|{restart}|{security}|"
                                f"{capabilities}|{log_driver}|{log_options}|{ports}|{environment}\n")

    def test_profile_changes_require_a_new_container_even_with_the_same_image_and_identity(self):
        for actual in ("generic", "google-adk", "", "<no value>", "unknown", "google-adk|generic"):
            with self.subTest(actual=actual):
                self.docker.results["inspect"] = self.inspect_output(template=actual)
                self.assertEqual(self.runtime.is_running(REGISTRATION, release()), actual in {"google-adk", "", "<no value>"})
                self.assertEqual(self.runtime.is_running(REGISTRATION, release(template="generic")), actual == "generic")
        self.docker.results["inspect"] = CommandResult(0, self.inspect_output().output.split("|")[0])
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_is_running_reads_the_release_container_state(self):
        self.docker.results["inspect"] = self.inspect_output()
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))
        self.assertEqual(self.docker.calls[-1][-1], DockerRuntime.container_name(REGISTRATION, release()))
        for result in (self.inspect_output("false false"), self.inspect_output("true true"),  # exited; crash-looping
                       CommandResult(1, "No such object"), CommandResult(0, "true\n")):
            self.docker.results["inspect"] = result
            self.assertFalse(self.runtime.is_running(REGISTRATION, release()), result)

    def test_a_container_with_the_wrong_image_or_mount_is_not_the_release(self):
        # Same name, same readiness answer: only the inspected image and mount prove which configuration runs.
        wrong = {"another image": self.inspect_output(image="c"),
                 "old mount path": self.inspect_output(mounts=("volume:shiplord-orders-artifacts:/data",)),
                 "another volume": self.inspect_output(mounts=("volume:shiplord-other-artifacts:/app/src/.adk",)),
                 "a bind mount": self.inspect_output(mounts=("bind::/app/src/.adk",)),
                 "a duplicate Shiplord mount": self.inspect_output(mounts=("volume:shiplord-orders-artifacts:/app/src/.adk",
                                                                             "volume:shiplord-orders-artifacts:/app/src/.adk")),
                 "no mount": self.inspect_output(mounts=())}
        for label, result in wrong.items():
            with self.subTest(label):
                self.docker.results["inspect"] = result
                self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_an_image_declared_volume_does_not_hide_the_shiplord_mount(self):
        self.docker.results["inspect"] = self.inspect_output(
            mounts=("volume:shiplord-orders-artifacts:/app/src/.adk", "volume::/scratch"))
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))

    def test_a_registry_alias_does_not_change_the_identity_but_a_digest_does(self):
        self.docker.results["inspect"] = self.inspect_output(repository="localhost:5000/example/orders")
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = self.inspect_output(image="c", repository="localhost:5000/example/orders")
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = CommandResult(0, self.inspect_output(repository="orders:latest").output
                                                       .replace("orders:latest@sha256:" + "b" * 64, "orders:latest"))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_shutdown_policy_is_part_of_running_identity(self):
        candidate = release()
        candidate["configuration"]["deploy"].update(shutdown_timeout_seconds=120, shutdown_timeout_action="fail")
        self.docker.results["inspect"] = self.inspect_output(timeout="1", action="force")
        self.assertFalse(self.runtime.is_running(REGISTRATION, candidate))
        self.docker.results["inspect"] = self.inspect_output(timeout="120", action="fail")
        self.assertTrue(self.runtime.is_running(REGISTRATION, candidate))
        self.docker.results["inspect"] = self.inspect_output(timeout="120", action="bad")
        self.assertFalse(self.runtime.is_running(REGISTRATION, candidate))

    def test_memory_and_memory_swap_are_part_of_running_identity(self):
        self.docker.results["inspect"] = self.inspect_output(memory=768 * 1024 * 1024)
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = self.inspect_output(memory_swap=768 * 1024 * 1024)
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = self.inspect_output(memory=256 * 1024 * 1024, memory_swap=256 * 1024 * 1024)
        self.assertTrue(self.runtime.is_running(REGISTRATION, release(memory=256)))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_every_fixed_run_value_is_part_of_running_identity(self):
        changed = {
            "another pids limit": {"pids": 256},
            "another restart policy": {"restart": "always"},
            "another security option": {"security": '["no-new-privileges:false"]'},
            "no security option": {"security": "null"},
            "another dropped capability": {"capabilities": '["CHOWN"]'},
            "no dropped capability": {"capabilities": "null"},
            "another log driver": {"log_driver": "local"},
            "another log size": {"log_options": '{"max-size":"1m","max-file":"3"}'},
            "another log file count": {"log_options": '{"max-size":"10m","max-file":"1"}'},
            "another published port": {"ports": '{"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"20008"}]}'},
            "another container port": {"ports": '{"8081/tcp":[{"HostIp":"127.0.0.1","HostPort":"20007"}]}'},
            "a public bind": {"ports": '{"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"20007"}]}'},
        }
        for label, fields in changed.items():
            with self.subTest(label):
                self.docker.results["inspect"] = self.inspect_output(**fields)
                self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_the_environment_the_container_was_created_with_is_part_of_running_identity(self):
        self.docker.results["inspect"] = self.inspect_output(
            environment=self.environment(ALLOW_ORIGINS='["https://old.example"]'))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release(origins=["https://new.example"])))
        self.docker.results["inspect"] = self.inspect_output(environment=self.environment(PORT="8081"))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_a_custom_environment_value_is_compared_and_image_keys_are_tolerated(self):
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("CUSTOM_TOKEN=placeholder\n")
        path.chmod(0o600)
        self.docker.results["inspect"] = self.inspect_output(
            environment=self.environment("PATH=/usr/bin", "VERSION=v1", CUSTOM_TOKEN="placeholder"))
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = self.inspect_output(
            environment=self.environment("PATH=/usr/bin", CUSTOM_TOKEN="old"))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = self.inspect_output(environment=self.environment("PATH=/usr/bin"))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_an_environment_value_containing_the_field_separator_does_not_shift_the_other_fields(self):
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("CUSTOM_TOKEN=a|b\n")
        path.chmod(0o600)
        self.docker.results["inspect"] = self.inspect_output(
            environment=self.environment("PATH=/usr/bin", CUSTOM_TOKEN="a|b"))
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))

    def test_an_unusable_environment_file_is_not_a_mismatch_by_itself(self):
        """A start would fail on the same file, so a restart cannot fix it; the record still decides."""
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("PORT=1\n")  # a managed key: the parser refuses it
        path.chmod(0o600)
        self.docker.results["inspect"] = self.inspect_output()
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))
        path.write_text("OK=1\n")
        path.chmod(0o644)  # not private to the installing account
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))
        # The record-derived values are still compared, so a candidate that replaced them is caught.
        self.docker.results["inspect"] = self.inspect_output(memory=768 * 1024 * 1024)
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_absent_or_unreadable_fields_are_a_mismatch(self):
        for fields in ({"pids": 0}, {"restart": ""}, {"security": "not json"}, {"capabilities": "null"},
                       {"log_options": "null"}, {"ports": "null"}, {"environment": "not json"},
                       {"environment": '["no-separator"]'}):
            with self.subTest(fields=fields):
                self.docker.results["inspect"] = self.inspect_output(**fields)
                self.assertFalse(self.runtime.is_running(REGISTRATION, release()))

    def test_an_image_declared_mount_does_not_change_release_identity(self):
        self.docker.results["inspect"] = self.inspect_output(
            mounts=("volume:shiplord-orders-artifacts:/app/src/.adk", "volume:anonymous:/scratch"))
        self.assertTrue(self.runtime.is_running(REGISTRATION, release()))

    def test_a_configured_path_is_the_one_verified_and_a_legacy_container_matches_the_default(self):
        configured = release(storage={"container_path": "/data"})
        self.docker.results["inspect"] = self.inspect_output(mounts=("volume:shiplord-orders-artifacts:/data",))
        self.assertTrue(self.runtime.is_running(REGISTRATION, configured))
        self.assertFalse(self.runtime.is_running(REGISTRATION, release()))
        self.docker.results["inspect"] = self.inspect_output()
        self.assertFalse(self.runtime.is_running(REGISTRATION, configured))

    def test_logs_are_bounded_and_empty_without_a_container(self):
        self.assertEqual(self.runtime.logs(REGISTRATION, None, 50), "")
        self.assertEqual(self.docker.sub("ps"), [])  # no release means nothing to look for
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        self.docker.results["logs"] = CommandResult(0, "line1\nline2\n")
        self.assertEqual(self.runtime.logs(REGISTRATION, release(), 10**9), "line1\nline2\n")
        self.assertEqual(self.docker.sub("logs")[0][2:4], ["--tail", "1000"])
        self.runtime.logs(REGISTRATION, release(), -5)
        self.assertEqual(self.docker.sub("logs")[1][2:4], ["--tail", "1"])

    def test_logs_pass_since_and_timestamps_to_docker(self):
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        self.docker.results["logs"] = CommandResult(0, "2026-11-10T12:00:00Z line\n")
        self.runtime.logs(REGISTRATION, release(), 25, since="15m", timestamps=True)
        self.assertEqual(self.docker.sub("logs")[0][1:], ["logs", "--tail", "25", "--since", "15m", "--timestamps", ID_A])

    def test_follow_streams_the_current_release_for_a_bounded_time(self):
        docker = StreamingDocker()
        docker.results["ps"] = CommandResult(0, ID_A + "\n")
        runtime = DockerRuntime(docker, self.layout, platform_name="linux/amd64")
        received = []
        reason = runtime.follow_logs(REGISTRATION, release(), tail=50, since="15m", timestamps=True, duration=120,
                                     emit=received.append)
        self.assertEqual((reason, received), ("timeout", ["one\n", "two\n"]))
        argv, timeout = docker.stream_calls[0]
        self.assertEqual(timeout, 120)
        self.assertEqual(argv[1:], ["logs", "--follow", "--tail", "50", "--since", "15m", "--timestamps", ID_A])

    def test_follow_without_the_current_release_returns_immediately(self):
        self.assertEqual(self.runtime.follow_logs(REGISTRATION, None, tail=10, duration=120), "unavailable")
        self.assertEqual(self.docker.sub("logs"), [])

    def test_logs_read_only_the_current_releases_container(self):
        current, stale = release(), release(commit="c", sequence=900)
        docker = LabelledDocker([(ID_A, {"shiplord.app": "orders", "shiplord.release": release_id(stale)}),
                                 (ID_B, {"shiplord.app": "orders", "shiplord.release": release_id(current)})])
        runtime = DockerRuntime(docker, self.layout, platform_name="linux/amd64")
        self.assertEqual(runtime.logs(REGISTRATION, current, 50), "the current release\n")
        self.assertEqual(docker.calls[-1][-1], ID_B)  # not the older release listed first
        self.assertEqual(docker.calls[0][4:], ["--filter", "label=shiplord.app=orders",
                                               "--filter", f"label=shiplord.release={release_id(current)}"])

    def test_a_stopped_container_from_an_older_release_is_never_read(self):
        stale = release(commit="c", sequence=900)
        docker = LabelledDocker([(ID_A, {"shiplord.app": "orders", "shiplord.release": release_id(stale)})])
        runtime = DockerRuntime(docker, self.layout, platform_name="linux/amd64")
        self.assertEqual(runtime.logs(REGISTRATION, release(), 50), "")
        self.assertEqual([call for call in docker.calls if call[1] == "logs"], [])
        self.assertEqual(runtime.follow_logs(REGISTRATION, release(), tail=50), "unavailable")

    def test_release_logs_read_that_release_own_container_by_name(self):
        self.docker.results["logs"] = CommandResult(0, "boom\n")
        self.assertEqual(self.runtime.release_logs(REGISTRATION, release(), 10**9), "boom\n")
        (call,) = self.docker.sub("logs")
        self.assertEqual(call[1:], ["logs", "--tail", "1000", self.runtime.container_name(REGISTRATION, release())])
        self.assertEqual(self.docker.max_outputs, [MAX_LOG_BYTES])
        self.runtime.release_logs(REGISTRATION, release(), -5)
        self.assertEqual(self.docker.sub("logs")[1][2:4], ["--tail", "1"])

    def test_release_logs_are_none_only_when_the_container_is_gone(self):
        self.docker.results["logs"] = CommandResult(1, "Error: No such container: shiplord-orders-missing\n")
        self.assertIsNone(self.runtime.release_logs(REGISTRATION, release(), 50))
        self.docker.results["logs"] = CommandResult(1, "permission denied while reading logs\n")
        with self.assertRaises(RuntimeFailure):
            self.runtime.release_logs(REGISTRATION, release(), 50)

    def test_discard_reports_but_does_not_raise_when_the_image_is_in_use(self):
        self.assertEqual(self.runtime.discard(REGISTRATION, release()), [])
        self.docker.results["image rm"] = CommandResult(1, "conflict: image is being used")
        self.assertEqual(len(self.runtime.discard(REGISTRATION, release())), 1)


class ScriptedDocker:
    """Runs a local script through the real command runner, so its output cap is the one under test."""

    def __init__(self, script):
        self.script = script

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        if argv[1] == "ps":
            return CommandResult(0, ID_A + "\n")
        return run_command([self.script], max_output=max_output)


class LogTruncationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.script = self.directory / "docker"
        self.script.write_text('#!/bin/sh\nhead -c 300000 /dev/zero | tr "\\0" x; printf "the failure\\n"\n')
        self.script.chmod(0o755)
        self.runtime = DockerRuntime(ScriptedDocker(str(self.script)), Layout(self.directory.resolve()),
                                     platform_name="linux/amd64")

    def test_the_newest_log_lines_survive_the_log_cap(self):
        logs = self.runtime.logs(REGISTRATION, release(), 50)
        self.assertLessEqual(len(logs), MAX_LOG_BYTES)
        self.assertTrue(logs.endswith("the failure\n"))

    def test_a_release_log_capture_keeps_the_newest_bytes_within_its_own_bound(self):
        logs = self.runtime.release_logs(REGISTRATION, release(), 50, max_bytes=4096)
        self.assertLessEqual(len(logs), 4096)
        self.assertTrue(logs.startswith("[shiplord: earlier output truncated]"))
        self.assertTrue(logs.endswith("the failure\n"))


class LogOptionTests(unittest.TestCase):
    def test_only_relative_durations_both_runtimes_read_are_accepted(self):
        for value in ("0s", "30s", "15m", "2h", "99999h"):
            self.assertEqual(validate_since(value), value)
        for value in ("", "1d", "5", "15 m", "-15m", "2026-11-10T12:00:00Z", "1h; rm -rf /"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_since(value)

    def test_the_follow_timeout_is_bounded_and_never_a_boolean(self):
        self.assertEqual(validate_follow_timeout(1), 1)
        self.assertEqual(validate_follow_timeout(MAX_FOLLOW_SECONDS), MAX_FOLLOW_SECONDS)
        for value in (0, MAX_FOLLOW_SECONDS + 1, True, "60", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_follow_timeout(value)


class StorageMountTests(RuntimeTestCase):
    def run_argv(self, **kwargs):
        self.docker.results["ps"] = CommandResult(0, "")
        self.runtime.start(REGISTRATION, release(**kwargs))
        return self.docker.sub("run")[0]

    def test_the_configured_path_is_mounted_from_the_same_named_volume(self):
        argv = self.run_argv(storage={"container_path": "/var/lib/handwriting"})
        self.assertEqual(argv[argv.index("--volume") + 1], "shiplord-orders-artifacts:/var/lib/handwriting")
        self.assertEqual(argv.count("--volume"), 1)

    def test_a_missing_volume_is_created_labelled_and_local_before_anything_is_removed(self):
        self.docker.results["volume inspect"] = CommandResult(1, "Error: No such volume: shiplord-orders-artifacts")
        self.run_argv()
        names = [" ".join(c[1:3]) if c[1] == "volume" else c[1] for c in self.docker.calls]
        self.assertLess(names.index("volume create"), names.index("run"))
        self.assertEqual(self.docker.sub("volume create")[0][2:], ["create", "--driver", "local", "--label",
                         "shiplord.app=orders", "shiplord-orders-artifacts"])

    def test_prepare_never_creates_a_volume(self):
        self.docker.results["volume inspect"] = CommandResult(1, "Error: No such volume: x")
        self.docker.results["image inspect"] = CommandResult(0, "linux/amd64||\n")
        self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(self.docker.sub("volume create"), [])

    def test_a_foreign_or_unexplained_volume_is_refused_before_any_container_is_touched(self):
        bad = {"another driver": "nfs|null|null|2026-10-30T00:00:00Z|/var/lib/docker/volumes/x/_data",
               "driver options": 'local|{"device":"/etc"}|null|2026-10-30T00:00:00Z|/var/lib/docker/volumes/x/_data',
               "another application's label": 'local|null|{"shiplord.app":"billing"}|2026-10-30T00:00:00Z|'
                                                '/var/lib/docker/volumes/x/_data',
               "unrelated volume labels": 'local|null|{"owner":"manual"}|2026-10-30T00:00:00Z|'
                                            '/var/lib/docker/volumes/shiplord-orders-artifacts/_data',
               "unexplained same-name volume": "local|null|null|2026-10-30T00:00:00Z|"
                                                "/var/lib/docker/volumes/shiplord-orders-artifacts/_data",
               "garbage": "nonsense"}
        for label, output in bad.items():
            with self.subTest(label):
                self.docker.calls.clear()
                self.docker.results["volume inspect"] = CommandResult(0, output)
                with self.assertRaises(RuntimeFailure) as caught:
                    self.run_argv()
                self.assertEqual(caught.exception.phase, "storage")
                self.assertEqual((self.docker.sub("rm"), self.docker.sub("run")), ([], []))

    def test_an_inspection_failure_other_than_not_found_is_not_treated_as_absence(self):
        self.docker.results["volume inspect"] = CommandResult(1, "Cannot connect to the Docker daemon")
        with self.assertRaises(RuntimeFailure):
            self.run_argv()
        self.assertEqual(self.docker.sub("volume create"), [])

    def test_a_labelled_volume_of_this_application_is_accepted(self):
        self.docker.results["volume inspect"] = LABELLED_VOLUME
        self.run_argv()

    def test_a_legacy_volume_needs_a_retained_shiplord_container_and_returns_durable_proof(self):
        self.docker.results["volume inspect"] = LEGACY_VOLUME
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        release_id = "a" * 12
        self.docker.results["inspect"] = CommandResult(
            0, f"/shiplord-orders-{release_id}|orders|{release_id}|"
               "volume:shiplord-orders-artifacts:/app/src/.adk\n")
        self.docker.results["image inspect"] = CommandResult(0, "linux/amd64||\n")

        prepared = self.runtime.prepare(REGISTRATION, release())
        self.assertEqual(prepared["storage_provenance"]["volume_name"], "shiplord-orders-artifacts")
        self.assertEqual(prepared["storage_provenance"]["container_id"], ID_A)
        self.assertRegex(prepared["storage_provenance"]["fingerprint"], r"^[0-9a-f]{64}$")

    def test_a_retained_container_proves_only_the_exact_expected_volume(self):
        self.docker.results["volume inspect"] = LEGACY_VOLUME
        self.docker.results["ps"] = CommandResult(0, ID_A + "\n")
        release_id = "a" * 12
        self.docker.results["inspect"] = CommandResult(
            0, f"/shiplord-orders-{release_id}|orders|{release_id}|volume:shiplord-other-artifacts:/app/src/.adk\n")
        with self.assertRaisesRegex(RuntimeFailure, "no retained Shiplord container evidence"):
            self.runtime.check_volume(REGISTRATION, create=False)

    def test_persisted_legacy_volume_proof_allows_recovery_after_the_container_is_gone(self):
        import hashlib

        self.docker.results["volume inspect"] = LEGACY_VOLUME
        self.docker.results["ps"] = CommandResult(0, "")
        created_at, mountpoint = "2026-10-30T00:00:00Z", "/var/lib/docker/volumes/shiplord-orders-artifacts/_data"
        proof = {"volume_name": "shiplord-orders-artifacts",
                 "fingerprint": hashlib.sha256((created_at + "\0" + mountpoint).encode()).hexdigest(),
                 "container_id": ID_A}
        self.runtime.start_with_provenance(REGISTRATION, release(), proof)
        self.assertEqual(len(self.docker.sub("run")), 1)

    def test_removing_containers_never_removes_volumes(self):
        self.run_argv()
        self.assertNotIn("-v", self.docker.sub("volume inspect")[0])
        self.assertTrue(all("-v" not in c and "--volumes" not in c for c in self.docker.sub("rm")))
        self.assertEqual(self.docker.sub("volume rm"), [])


if __name__ == "__main__":
    unittest.main()
