from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import REGISTRATION, FakeCaddy, FakeProbes, FakeRuntime, configuration, record, request  # noqa: E402
from shiplord.deploy import CANDIDATE_LOG_BYTES, CANDIDATE_LOG_LINES, Deployer, identity  # noqa: E402
from shiplord.layout import Layout  # noqa: E402
from shiplord.probes import ProbeFailure  # noqa: E402
from shiplord.registry import validate_registration  # noqa: E402
from shiplord.release_state import MAX_RECORD_BYTES, HostLock, ReleaseState, StateError  # noqa: E402
from shiplord.routing import RouteManager, RoutingError  # noqa: E402
from shiplord.runtime import DockerRuntime, RuntimeFailure, release_id  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402


class FakeDockerCLI:
    """The Docker CLI calls a prepare makes: pull, one image inspect, and the discard of a refused image."""

    def __init__(self, stop_signal):
        self.stop_signal = stop_signal
        self.calls: list[list[str]] = []

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        argv = list(argv)
        self.calls.append(argv)
        command = " ".join(argv[1:3]) if argv[1] == "image" else argv[1]
        if command == "pull":
            return CommandResult(0, "")
        if command == "image inspect":
            return CommandResult(0, f"linux/amd64|{'c' * 40}|{self.stop_signal}\n")
        if command == "image rm":
            return CommandResult(0, "")
        raise AssertionError(f"unexpected command: {argv}")


NATIVE_REGISTRATION = validate_registration({
    "schema_version": 1, "application_id": "orders", "deployment_uid": 1001, "deployment_user": "ship-orders",
    "runtime": "native", "api_domain": "orders.example.com", "host_port": 20007, "image_repository": None,
    "max_memory_mb": 4096})


def native_request(request_id="req-1", commit="a", sequence=1000, sha256="b"):
    return {"request_id": request_id, "commit": commit * 40, "sequence": sequence,
            "artifact": {"kind": "native", "sha256": sha256 * 64, "size": 10, "architecture": "linux-amd64"},
            "configuration": dict(configuration(), runtime="native")}


def native_record(**kwargs):
    value = native_request(**kwargs)
    return {k: value[k] for k in ("commit", "sequence", "artifact", "configuration")}


class FakeNativeRuntime(FakeRuntime):
    """A native release is discarded by release ID: its directory is named for the release, not for the binary."""

    def discard(self, registration, release):
        self.discarded.append(release_id(release))
        return []


class DeployTestCase(unittest.TestCase):
    def setUp(self):
        # Durability (fsync ordering) is verified in the journal and routing tests; here it only costs time.
        patcher = patch("os.fsync", lambda descriptor: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.caddy_root = base / "caddy"
        (self.caddy_root / "ship").mkdir(parents=True)
        for directory in (self.caddy_root, self.caddy_root / "ship"):
            directory.chmod(0o755)
        main = self.caddy_root / "Caddyfile"
        main.write_text(f"other.example.org {{\n}}\nimport {self.caddy_root}/ship/*.caddy\n")
        main.chmod(0o644)
        for name in ("lock", "state"):
            (base / name).mkdir()
            (base / name).chmod(0o700)
        self.lock_fd = os.open(base / "lock", os.O_RDONLY | os.O_DIRECTORY)
        self.state_fd = os.open(base / "state", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.lock_fd)
        self.addCleanup(os.close, self.state_fd)
        self.lock = HostLock(self.lock_fd)
        self.state = ReleaseState("orders", self.state_fd, self.lock)
        self.caddy = FakeCaddy()
        self.runtime = FakeRuntime()
        self.probes = FakeProbes(self.runtime)
        self.progress = []
        with self.lock.acquire():
            self.state.initialize()
        self.build()

    def build(self, registration=None):
        self.routes = RouteManager(self.caddy_root, self.lock, self.caddy)
        self.deployer = Deployer(registration=registration or REGISTRATION, state=self.state, routes=self.routes,
                                 runtime=self.runtime, probes=self.probes,
                                 progress=lambda phase, info: self.progress.append(phase))

    def run_locked(self, function, *args):
        with self.lock.acquire():
            return function(*args)

    def deploy(self, **kwargs):
        return self.run_locked(self.deployer.deploy, request(**kwargs))

    ALIASES = ["legacy.example.com", "www.orders.example.com"]

    def alias_deployer(self, hostnames=None):
        self.build(validate_registration(dict(REGISTRATION, hostnames=hostnames or self.ALIASES)))
        return self.deployer

    def alias_request(self, hostnames=None, **kwargs):
        return request(configuration=configuration(hostnames=hostnames or self.ALIASES), **kwargs)

    @property
    def route_file(self):
        return self.caddy_root / "ship" / "orders.caddy"


class DeployFlowTests(DeployTestCase):
    def test_aliases_are_routed_on_the_first_deploy_and_reported(self):
        self.alias_deployer()
        outcome = self.run_locked(self.deployer.deploy, self.alias_request())
        self.assertEqual((outcome.status, outcome.exit_code), ("succeeded", 0))
        route = self.route_file.read_bytes()
        for alias in self.ALIASES:
            self.assertIn(f"\n{alias} {{\n\tredir https://orders.example.com{{uri}} 308\n}}\n".encode(), route)
        self.assertTrue(outcome.detail["probes"]["route_changed"])
        self.assertEqual(self.deployer.status()["hostnames"], self.ALIASES)

    def test_a_deploy_whose_aliases_differ_from_the_registration_is_refused_before_any_side_effect(self):
        self.alias_deployer()
        before = self.state.read_state()
        cases = {
            "aliases dropped": request(),
            "other alias": request(configuration=configuration(hostnames=["other.example.com"])),
            "one alias fewer": request(configuration=configuration(hostnames=self.ALIASES[:1])),
        }
        for label, value in cases.items():
            with self.subTest(label):
                outcome = self.run_locked(self.deployer.deploy, value)
                self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2), outcome.detail)
                self.assertIn("additional hostnames", outcome.detail["message"])
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.state.read_state(), before)
        self.assertFalse(self.route_file.exists())

    def test_a_registered_alias_cannot_be_dropped_by_a_manifest_that_omits_it(self):
        self.build(validate_registration(dict(REGISTRATION, hostnames=["legacy.example.com"])))
        outcome = self.run_locked(self.deployer.deploy, request())
        self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2))
        self.assertFalse(self.route_file.exists())

    def test_first_deploy_commits_routes_and_reports_each_probe_separately(self):
        outcome = self.deploy()
        self.assertEqual((outcome.status, outcome.exit_code, outcome.ok), ("succeeded", 0, True))
        committed = self.state.read_state()
        self.assertEqual(committed["current"], record())
        self.assertIsNone(committed["previous"])
        self.assertIsNone(self.state.read_journal())
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertIn(b"127.0.0.1:20007", self.route_file.read_bytes())
        probes = outcome.detail["probes"]
        self.assertEqual((probes["local_ready"]["ready"], probes["route_changed"], probes["proxy_routing"]),
                         (True, True, True))
        self.assertEqual(probes["https"]["verified"], False)  # reported, never claimed
        self.assertEqual(probes["dns"], ["192.0.2.1"])
        self.assertEqual(self.progress, ["prepare", "runtime-changing", "runtime-ready", "routing-changing",
                                         "routing-ready"])
        self.assertEqual(self.runtime.names(), ["prepare", "start"])  # nothing stopped before the new one starts

    def test_a_second_deploy_keeps_the_first_as_the_rollback_target_without_touching_the_route(self):
        self.deploy()
        reloads = self.caddy.reload_calls
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(outcome.status, "succeeded")
        committed = self.state.read_state()
        self.assertEqual((committed["current"]["commit"], committed["previous"]["commit"]), ("c" * 40, "a" * 40))
        self.assertFalse(outcome.detail["probes"]["route_changed"])
        self.assertEqual(self.caddy.reload_calls, reloads)  # unchanged route: no reload
        self.assertEqual(self.runtime.discarded, [])

    def test_the_image_leaving_the_retention_window_is_discarded_only_after_success(self):
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        outcome = self.deploy(request_id="req-3", commit="e", image="f", sequence=1002)
        self.assertEqual(self.runtime.discarded, ["ghcr.io/example/orders@sha256:" + "b" * 64])
        self.assertEqual(outcome.detail["warnings"], [])

    def test_an_image_still_used_by_a_retained_release_is_never_discarded(self):
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="b", sequence=1001)  # same image, new commit
        self.deploy(request_id="req-3", commit="e", image="d", sequence=1002)
        self.assertEqual(self.runtime.discarded, [])

    def test_stale_queued_release_cannot_replace_a_newer_one_unless_explicitly_allowed(self):
        self.deploy(sequence=2000)
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1999)
        self.assertEqual((outcome.status, outcome.detail["error"], outcome.exit_code), ("failed", "stale_release", 1))
        self.assertEqual(self.state.read_state()["current"]["commit"], "a" * 40)
        self.assertEqual(self.runtime.names(), ["prepare", "start"])
        allowed = self.deploy(request_id="req-3", commit="c", image="d", sequence=1999, allow_older=True)
        self.assertEqual(allowed.status, "succeeded")
        equal = self.deploy(request_id="req-4", commit="e", image="f", sequence=1999)
        self.assertEqual(equal.status, "succeeded")  # the same second is not older

    def test_memory_above_the_registered_ceiling_is_refused_before_anything_runs(self):
        self.deployer.registration = dict(REGISTRATION, max_memory_mb=256)
        outcome = self.deploy()  # the fixture asks for 512 MiB
        self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2))
        self.assertIn("exceeds the 256 MiB", outcome.detail["message"])
        self.assertEqual(self.runtime.calls, [])
        self.deployer.registration = dict(REGISTRATION, max_memory_mb=512)
        self.assertEqual(self.deploy(request_id="req-2").status, "succeeded")  # the ceiling itself is allowed

    def test_redeploying_the_identical_release_is_unchanged_while_healthy_and_recreates_it_when_not(self):
        self.deploy()
        calls = len(self.runtime.calls)
        self.assertEqual(self.deploy(request_id="req-2").status, "unchanged")
        self.assertEqual(len(self.runtime.calls), calls)
        self.runtime.running = set()
        self.assertEqual(self.deploy(request_id="req-3").status, "succeeded")
        self.assertEqual(self.runtime.running, {release_id(record())})

    def test_request_ids_are_idempotent_and_never_reused_after_failure(self):
        self.assertEqual(self.deploy().status, "succeeded")
        calls = len(self.runtime.calls)
        again = self.deploy()
        self.assertEqual((again.status, again.exit_code), ("already_applied", 0))
        self.assertEqual(len(self.runtime.calls), calls)
        self.runtime.start_errors[release_id(record(commit="c", image="d", sequence=1001))] = RuntimeFailure("boom", "start")
        self.assertEqual(self.deploy(request_id="req-2", commit="c", image="d", sequence=1001).status, "rolled_back")
        reused = self.deploy(request_id="req-2", commit="e", image="f", sequence=1002)
        self.assertEqual((reused.status, reused.exit_code, reused.detail["error"]), ("rejected", 2, "request_id_reused"))

    def test_invalid_requests_are_rejected_with_no_side_effects(self):
        cases = {
            "other repository": request(artifact={"kind": "docker", "image": "ghcr.io/example/other@sha256:" + "b" * 64}),
            "tag instead of digest": request(artifact={"kind": "docker", "image": "ghcr.io/example/orders:latest"}),
            "other hostname": request(configuration=configuration(api_domain="evil.example.com")),
            "native artifact": request(artifact={"kind": "native", "sha256": "b" * 64, "size": 10,
                                                 "architecture": "linux-amd64"}),
            "bad commit": request(commit="z"),
            "no artifact": {k: v for k, v in request().items() if k != "artifact"},
            "wrong app": request(configuration=dict(configuration(), name="billing")),
            "bad sequence": request(sequence=-1),
            "bad origins": request(configuration=configuration(frontend_origins=["https://x.example.com/path"])),
        }
        before = self.state.read_state()
        for label, value in cases.items():
            with self.subTest(label):
                outcome = self.run_locked(self.deployer.deploy, value)
                self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2), outcome.detail)
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.state.read_state(), before)
        self.assertFalse(self.route_file.exists())

    def test_a_routing_probe_that_cannot_see_an_alias_redirect_rolls_back(self):
        self.alias_deployer()
        self.assertEqual(self.run_locked(self.deployer.deploy, self.alias_request()).status, "succeeded")
        self.probes.routing_result = False
        outcome = self.run_locked(self.deployer.deploy, self.alias_request(request_id="req-2", commit="c", image="d",
                                                                          sequence=1001))
        self.assertEqual((outcome.status, outcome.exit_code, outcome.detail["phase"]), ("rolled_back", 5, "routing"))
        self.assertIn("hostnames as rendered", outcome.detail["message"])
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertEqual(identity(self.state.read_state()["current"]), identity(record()))

    def test_a_route_change_that_only_adds_an_alias_is_detected_as_changed(self):
        self.deploy()
        reloads = self.caddy.reload_calls
        self.alias_deployer()
        outcome = self.run_locked(self.deployer.deploy, self.alias_request(request_id="req-2", commit="c", image="d",
                                                                          sequence=1001))
        self.assertEqual(outcome.status, "succeeded")
        self.assertTrue(outcome.detail["probes"]["route_changed"])
        self.assertEqual(self.caddy.reload_calls, reloads + 1)


class FailureTests(DeployTestCase):
    def test_prepare_failure_changes_nothing_and_keeps_the_running_release(self):
        self.deploy()
        self.runtime.calls.clear()
        self.runtime.prepare_error = RuntimeFailure("denied", "prepare", "requested access denied")
        before = self.state.read_state()
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.exit_code), ("failed", 1))
        self.assertEqual(outcome.detail["error"], "prepare_failed")
        self.assertEqual(self.runtime.names(), ["prepare"])  # the old app was never stopped
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertEqual(self.state.read_state(), before)
        self.assertIsNone(self.state.read_journal())

    def test_an_image_that_cannot_be_stopped_gracefully_is_refused_before_the_journal_opens(self):
        self.deploy()
        before = self.state.read_state()
        route = self.route_file.read_bytes()
        docker = FakeDockerCLI("SIGKILL")
        self.deployer.runtime = DockerRuntime(docker, Layout(Path(self.temp.name).resolve()),
                                              platform_name="linux/amd64")
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.exit_code, outcome.detail["error"]), ("failed", 1, "prepare_failed"))
        self.assertIn("STOPSIGNAL SIGKILL", outcome.detail["message"])
        self.assertIn("ghcr.io/example/orders@sha256:" + "d" * 64, outcome.detail["message"])
        self.assertEqual(self.state.read_state(), before)
        self.assertIsNone(self.state.read_journal())
        self.assertEqual(self.route_file.read_bytes(), route)
        self.assertEqual([call[1:3] for call in docker.calls if call[1] == "image"],
                         [["image", "inspect"], ["image", "rm"]])  # the refused image is discarded

    def test_candidate_failing_to_start_restores_the_previous_release(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("container exited", "start", "OOM")
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.exit_code), ("rolled_back", 5))
        self.assertEqual(outcome.detail["restored_release"], identity(record()))
        self.assertEqual(outcome.detail["detail"], "OOM")
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertEqual(self.state.read_state()["current"], record())
        self.assertEqual(self.state.read_state()["generation"], 1)
        self.assertIsNone(self.state.read_journal())

    def test_readiness_timeout_and_exit_both_roll_back_with_their_cause(self):
        self.deploy()
        for number, kind in enumerate(("timeout", "exited"), start=2):
            with self.subTest(kind):
                candidate = record(commit=str(number), image=str(number), sequence=1000 + number)
                self.probes.not_ready[release_id(candidate)] = ProbeFailure(f"not ready: {kind}", kind)
                outcome = self.deploy(request_id=f"req-{number}", commit=str(number), image=str(number), sequence=1000 + number)
                self.assertEqual((outcome.status, outcome.detail["kind"]), ("rolled_back", kind))
                self.assertEqual(self.runtime.running, {release_id(record())})
                self.assertIsNone(self.state.read_journal())

    def test_first_release_failure_removes_the_runtime_and_route_and_reports_no_active_release(self):
        self.probes.not_ready[release_id(record())] = ProbeFailure("timeout", "timeout")
        outcome = self.deploy()
        self.assertEqual((outcome.status, outcome.exit_code), ("failed", 1))
        self.assertIsNone(outcome.detail["restored_release"])
        self.assertEqual(self.runtime.running, set())
        self.assertFalse(self.route_file.exists())
        self.assertIsNone(self.state.read_state()["current"])
        self.assertIsNone(self.state.read_journal())
        self.assertIn("req-1", self.state.read_state()["seen_requests"])

    def test_routing_failure_after_the_candidate_is_ready_restores_runtime_and_route(self):
        self.deploy()
        original = self.route_file.read_bytes()
        self.probes.routing_result = False
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.detail["phase"]), ("rolled_back", "routing"))
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertEqual(self.route_file.read_bytes(), original)

    def test_a_reload_failure_on_the_first_release_is_cleaned_up_when_caddy_recovers(self):
        self.caddy.fail_reload = {1, 2}  # apply's reload and its restoring reload fail; the reconcile reload works
        outcome = self.deploy()
        self.assertEqual((outcome.status, outcome.exit_code), ("failed", 1))
        self.assertFalse(self.route_file.exists())
        self.assertIsNone(self.state.read_journal())

    def test_a_dead_caddy_during_the_first_release_requires_intervention_and_keeps_the_journal(self):
        self.caddy.fail_reload = {1, 2, 3}  # even the reconciling reload fails
        outcome = self.deploy()
        self.assertEqual((outcome.status, outcome.exit_code), ("intervention_required", 6))
        self.assertIsNotNone(self.state.read_journal())
        self.assertEqual(self.deploy(request_id="req-2").detail["error"], "recovery_required")

    def test_unknown_routing_is_a_warning_not_a_failure(self):
        self.probes.routing_result = None
        outcome = self.deploy()
        self.assertEqual(outcome.status, "succeeded")
        self.assertIsNone(outcome.detail["probes"]["proxy_routing"])
        self.assertIn("not verified", outcome.detail["probes"]["warning"])

    def test_a_failed_restore_requires_intervention_keeps_the_journal_and_blocks_deploys_until_recovered(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        self.runtime.start_errors[release_id(record())] = RuntimeFailure("docker is down", "start")
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.exit_code), ("intervention_required", 6))
        self.assertEqual(outcome.detail["restore_error"]["message"], "docker is down")
        self.assertIsNotNone(self.state.read_journal())
        blocked = self.deploy(request_id="req-3", commit="e", image="f", sequence=1002)
        self.assertEqual((blocked.status, blocked.detail["error"]), ("failed", "recovery_required"))
        self.assertEqual(self.run_locked(self.deployer.recover).status, "intervention_required")
        del self.runtime.start_errors[release_id(record())]
        recovered = self.run_locked(self.deployer.recover)
        self.assertEqual((recovered.status, recovered.exit_code), ("recovered", 0))
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertIsNone(self.state.read_journal())
        self.assertEqual(self.deploy(request_id="req-3", commit="e", image="f", sequence=1002).status, "succeeded")

    def test_a_forced_recovery_stops_the_process_removes_the_route_and_clears_the_journal(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.runtime.start_errors[release_id(record(commit="b", image="b", sequence=1001))] = RuntimeFailure(
            "did not stop", "stop", intervention_required=True, kind="shutdown_timeout")
        self.deploy(request_id="req-2", image="b", commit="b", sequence=1001)
        self.runtime.stop_in_progress = lambda registration: True  # a plain recover would still refuse
        outcome = self.run_locked(lambda: self.deployer.recover(force_stop=True))
        self.assertEqual((outcome.status, outcome.exit_code), ("recovered", 0))
        self.assertIn("force_stop", self.runtime.names())
        self.assertEqual(self.runtime.running, set())
        self.assertFalse(self.route_file.exists())
        self.assertIsNone(self.state.read_journal())
        self.assertEqual(outcome.detail["target"], identity(record()))
        self.assertFalse(outcome.detail["running"])
        self.assertEqual(outcome.detail["forced_stop"]["signal"], "SIGKILL")
        self.assertEqual(outcome.detail["forced_stop"]["policy"], {"timeout_seconds": 10, "action": "fail"})
        self.assertIn("forced", outcome.detail["forced_stop"]["stages"])

    def test_a_forced_recovery_without_a_journal_still_stops_a_running_process(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.assertTrue(self.runtime.running)
        outcome = self.run_locked(lambda: self.deployer.recover(force_stop=True))
        self.assertEqual((outcome.status, outcome.exit_code), ("recovered", 0))
        self.assertIn("force_stop", self.runtime.names())
        self.assertEqual(self.runtime.running, set())
        self.assertFalse(self.route_file.exists())
        self.assertEqual(outcome.detail["target"], identity(record()))
        self.assertFalse(outcome.detail["running"])

    def test_a_forced_recovery_is_refused_for_a_native_runtime(self):
        native = Deployer(registration=NATIVE_REGISTRATION, state=self.state, routes=self.routes,
                          runtime=FakeNativeRuntime(), probes=self.probes)
        outcome = self.run_locked(lambda: native.recover(force_stop=True))
        self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2))
        self.assertEqual(outcome.detail["error"], "invalid_request")
        self.assertIsNone(self.state.read_journal())

    def test_a_failed_forced_stop_keeps_the_journal_and_requires_intervention(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.runtime.start_errors[release_id(record(commit="b", image="b", sequence=1001))] = RuntimeFailure(
            "did not stop", "stop", intervention_required=True, kind="shutdown_timeout")
        self.deploy(request_id="req-2", image="b", commit="b", sequence=1001)
        self.runtime.force_stop_error = RuntimeFailure(
            "the container did not exit after being killed", "stop", intervention_required=True, kind="kill_unconfirmed")
        outcome = self.run_locked(lambda: self.deployer.recover(force_stop=True))
        self.assertEqual((outcome.status, outcome.exit_code), ("intervention_required", 6))
        self.assertEqual(outcome.detail["kind"], "kill_unconfirmed")
        self.assertIsNotNone(self.state.read_journal())

    def test_a_stop_deadline_names_the_forced_recovery_escape_hatch(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.runtime.start_errors[release_id(record(commit="b", image="b", sequence=1001))] = RuntimeFailure(
            "did not stop", "stop", intervention_required=True, kind="shutdown_timeout")
        outcome = self.deploy(request_id="req-2", image="b", commit="b", sequence=1001)
        self.assertIn("shiplord recover --force-stop", outcome.detail["recovery"])

    def test_a_commit_whose_journal_cleanup_fails_is_flagged_and_recoverable(self):
        with patch.object(ReleaseState, "clear_after_reconciliation", side_effect=StateError("sync failed")):
            outcome = self.deploy()
        self.assertEqual((outcome.status, outcome.detail["error"]), ("intervention_required", "commit_unconfirmed"))
        recovered = self.run_locked(self.deployer.recover)
        self.assertEqual(recovered.status, "recovered")
        self.assertEqual(recovered.detail["target"], identity(record()))  # the committed release stays
        self.assertEqual(self.state.read_state()["current"], record())


class SequenceBoundTests(DeployTestCase):
    def test_a_far_future_sequence_is_rejected_so_it_cannot_freeze_the_application(self):
        outcome = self.deploy(sequence=2**40)
        self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2))
        self.assertIn("in the future", outcome.detail["message"])
        self.assertEqual(self.deploy(request_id="req-2", sequence=int(time.time()) + 3600).status, "succeeded")  # skew is fine
        self.assertEqual(self.state.read_state()["current"]["sequence"] > 1000, True)
        self.assertEqual(self.runtime.calls and self.runtime.calls[0][0], "prepare")

    def test_a_stale_release_is_refused_by_the_preflight_before_any_upload_could_be_accepted(self):
        self.deploy(sequence=2000)
        refusal = self.run_locked(self.deployer.preflight, request(request_id="req-2", commit="c", image="d", sequence=1999))
        self.assertEqual((refusal.status, refusal.detail["error"]), ("failed", "stale_release"))
        self.assertIsNone(self.run_locked(self.deployer.preflight, request(request_id="req-3", commit="c", image="d",
                                                                           sequence=1999, allow_older=True)))

    def test_a_stale_or_unchanged_deploy_cleans_up_whatever_an_upload_left(self):
        forgotten = []
        self.runtime.forget_upload = lambda registration, release: forgotten.append(release["commit"])
        self.deploy(sequence=2000)
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1000)  # stale: candidate artifact is discarded
        self.assertEqual(self.runtime.discarded, ["ghcr.io/example/orders@sha256:" + "d" * 64])
        self.assertEqual(self.deploy(request_id="req-3", sequence=2000).status, "unchanged")
        self.assertEqual(forgotten, ["a" * 40])  # an unchanged release's upload is forgotten, not installed


class AbandonedCandidateTests(DeployTestCase):
    """A candidate that failed must not leave its artifact behind, but retained releases keep theirs."""

    def image(self, letter):
        return "ghcr.io/example/orders@sha256:" + letter * 64

    def test_a_rolled_back_candidate_is_discarded_after_the_verified_restore(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(outcome.status, "rolled_back")
        self.assertEqual(self.runtime.discarded, [self.image("d")])
        self.assertEqual(outcome.detail["warnings"], [])

    def test_a_failed_first_release_and_a_failed_prepare_are_discarded_too(self):
        self.probes.not_ready[release_id(record())] = ProbeFailure("timeout", "timeout")
        self.assertEqual(self.deploy().status, "failed")
        self.assertEqual(self.runtime.discarded, [self.image("b")])
        self.runtime.discarded.clear()
        self.runtime.prepare_error = RuntimeFailure("wrong platform", "prepare")
        self.assertEqual(self.deploy(request_id="req-2", commit="c", image="d", sequence=1001).status, "failed")
        self.assertEqual(self.runtime.discarded, [self.image("d")])

    def test_an_artifact_shared_with_a_retained_release_is_never_discarded(self):
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        for image in ("b", "d"):  # the same image as the previous and the current release, with a failing start
            bad = record(commit="e", image=image, sequence=1002)
            self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
            self.assertEqual(self.deploy(request_id=f"req-{image}", commit="e", image=image, sequence=1002).status,
                             "rolled_back")
        self.assertEqual(self.runtime.discarded, [])

    def test_nothing_is_discarded_when_the_outcome_is_uncertain(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        self.runtime.start_errors[release_id(record())] = RuntimeFailure("docker is down", "start")
        self.assertEqual(self.deploy(request_id="req-2", commit="c", image="d", sequence=1001).status, "intervention_required")
        self.assertEqual(self.runtime.discarded, [])  # the journal stays; recovery may still need that artifact

    def test_a_failing_discard_is_a_warning_not_a_failure(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        self.runtime.discard = lambda registration, release: (_ for _ in ()).throw(RuntimeFailure("disk error", "cleanup"))
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.detail["warnings"]), ("rolled_back", ["disk error"]))

    def test_rolling_back_never_discards_the_release_being_restored(self):
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(self.run_locked(self.deployer.rollback, "req-3").status, "succeeded")
        self.assertEqual(self.runtime.discarded, [])


class FailedCandidateLogTests(DeployTestCase):
    """A failed candidate's crash output must be captured before the restore removes its container."""

    def failed_candidate(self, **kwargs):
        bad = record(**kwargs)
        self.runtime.candidate_logs[release_id(bad)] = "Traceback (most recent call last):\nboom\n"
        self.probes.not_ready[release_id(bad)] = ProbeFailure("timeout", "timeout")
        return bad, self.deploy(request_id="req-2", **kwargs)

    def test_a_candidate_that_fails_readiness_reports_its_last_log_lines(self):
        self.deploy()
        self.runtime.calls.clear()
        bad, outcome = self.failed_candidate(commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.exit_code), ("rolled_back", 5))
        captured = outcome.detail["candidate_logs"]
        self.assertEqual(captured["output"], "Traceback (most recent call last):\nboom\n")
        self.assertEqual((captured["tail"], captured["max_bytes"]), (CANDIDATE_LOG_LINES, CANDIDATE_LOG_BYTES))
        self.assertEqual(self.runtime.release_log_calls, [(release_id(bad), CANDIDATE_LOG_LINES, CANDIDATE_LOG_BYTES)])
        # The candidate is read after it starts and before the restore starts the previous release.
        self.assertEqual(self.runtime.names()[:3], ["prepare", "start", "release_logs"])

    def test_a_candidate_that_fails_after_readiness_is_captured_too(self):
        self.deploy()
        self.probes.routing_result = False
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.candidate_logs[release_id(bad)] = "ready, then the route failed\n"
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.detail["phase"]), ("rolled_back", "routing"))
        self.assertIn("the route failed", outcome.detail["candidate_logs"]["output"])

    def test_a_failure_before_the_candidate_container_exists_reports_no_logs(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(outcome.status, "rolled_back")
        self.assertNotIn("candidate_logs", outcome.detail)

    def test_a_native_candidate_never_reports_container_logs(self):
        runtime = FakeNativeRuntime()
        self.deployer = Deployer(registration=NATIVE_REGISTRATION, state=self.state, routes=self.routes,
                                 runtime=runtime, probes=FakeProbes(runtime))
        self.assertEqual(self.run_locked(self.deployer.deploy, native_request()).status, "succeeded")
        bad = native_record(commit="c", sequence=1001)
        runtime.candidate_logs[release_id(bad)] = "native candidate\n"
        self.deployer.probes.not_ready[release_id(bad)] = ProbeFailure("timeout", "timeout")
        outcome = self.run_locked(self.deployer.deploy, native_request(request_id="req-2", commit="c", sequence=1001))
        self.assertEqual(outcome.status, "rolled_back")
        self.assertNotIn("candidate_logs", outcome.detail)
        self.assertEqual(runtime.release_log_calls, [])

    def test_a_capture_failure_is_a_note_and_the_rollback_still_stands(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.probes.not_ready[release_id(bad)] = ProbeFailure("timeout", "timeout")
        self.runtime.log_errors[release_id(bad)] = RuntimeFailure("docker is down", "logs")
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual((outcome.status, outcome.exit_code), ("rolled_back", 5))
        self.assertEqual(outcome.detail["restored_release"], identity(record()))
        self.assertEqual(outcome.detail["candidate_logs"]["error"], "docker is down")
        self.assertNotIn("output", outcome.detail["candidate_logs"])
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertIsNone(self.state.read_journal())

    def test_the_capture_is_the_candidates_and_logs_after_report_the_restored_release(self):
        self.deploy()
        restored = release_id(record())
        self.runtime.candidate_logs[restored] = "restored release line\n"
        self.runtime.logs = lambda registration, tail: self.runtime.candidate_logs.get(
            next(iter(self.runtime.running), ""), "")
        _, outcome = self.failed_candidate(commit="c", image="d", sequence=1001)
        self.assertIn("boom", outcome.detail["candidate_logs"]["output"])
        self.assertEqual(self.runtime.logs(REGISTRATION, 50), "restored release line\n")

    def test_the_captured_tail_still_fits_in_a_job_record(self):
        self.deploy()
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.candidate_logs[release_id(bad)] = "x" * CANDIDATE_LOG_BYTES
        self.probes.not_ready[release_id(bad)] = ProbeFailure("timeout", "timeout")
        outcome = self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(len(outcome.detail["candidate_logs"]["output"]), CANDIDATE_LOG_BYTES)
        self.assertLess(len(json.dumps(outcome.detail)), MAX_RECORD_BYTES)


class RedeployRetentionTests(DeployTestCase):
    """Redeploying the release that is already committed must never replace the real rollback target."""

    def deploy_the_pair(self):
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(self.state.read_state()["previous"], record())

    def test_a_redeploy_of_the_current_release_that_is_down_keeps_the_rollback_target(self):
        self.deploy_the_pair()
        self.runtime.running = set()  # the release is down, so the unchanged shortcut cannot fire
        outcome = self.deploy(request_id="req-3", commit="c", image="d", sequence=1001)
        self.assertEqual(outcome.status, "succeeded")
        committed = self.state.read_state()
        self.assertEqual((committed["current"]["commit"], committed["previous"]["commit"]), ("c" * 40, "a" * 40))
        self.assertEqual(outcome.detail["previous"], identity(record()))  # reported as it was committed
        self.assertEqual(self.runtime.discarded, [])  # the previous release's image is still there
        rolled_back = self.run_locked(self.deployer.rollback, "req-4")
        self.assertEqual(rolled_back.status, "succeeded")
        self.assertEqual(self.runtime.running, {release_id(record())})  # back on A, not the redeployed B
        self.assertEqual(self.state.read_state()["current"], record())

    def test_a_redeploy_of_a_current_release_that_is_not_ready_keeps_the_rollback_target(self):
        self.deploy_the_pair()
        answers = iter([False])  # running, but not ready when the shortcut is considered
        self.probes.ready_once = lambda registration, release: next(answers, True)
        outcome = self.deploy(request_id="req-3", commit="c", image="d", sequence=1001)
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(self.state.read_state()["previous"], record())
        self.assertEqual(self.runtime.discarded, [])

    def test_a_new_release_after_a_redeploy_still_rotates_the_rollback_target(self):
        self.deploy_the_pair()
        self.runtime.running = set()
        self.deploy(request_id="req-3", commit="c", image="d", sequence=1001)
        self.deploy(request_id="req-4", commit="e", image="f", sequence=1002)
        committed = self.state.read_state()
        self.assertEqual((committed["current"]["commit"], committed["previous"]["commit"]), ("e" * 40, "c" * 40))
        self.assertEqual(self.runtime.discarded, ["ghcr.io/example/orders@sha256:" + "b" * 64])  # A left the window


class NativeRetentionTests(DeployTestCase):
    """A native release owns a directory named for its release ID, even when the binary is unchanged."""

    def build(self):
        self.routes = RouteManager(self.caddy_root, self.lock, self.caddy)
        self.deployer = Deployer(registration=NATIVE_REGISTRATION, state=self.state, routes=self.routes,
                                 runtime=self.runtime, probes=self.probes,
                                 progress=lambda phase, info: self.progress.append(phase))

    def setUp(self):
        super().setUp()
        self.runtime = FakeNativeRuntime()
        self.probes = FakeProbes(self.runtime)
        self.build()

    def deploy_native(self, **kwargs):
        return self.run_locked(self.deployer.deploy, native_request(**kwargs))

    def test_one_binary_under_several_commits_does_not_accumulate_release_directories(self):
        self.assertEqual(self.deploy_native().status, "succeeded")
        self.assertEqual(self.deploy_native(request_id="req-2", commit="c", sequence=1001).status, "succeeded")
        self.assertEqual(self.deploy_native(request_id="req-3", commit="e", sequence=1002).status, "succeeded")
        self.assertEqual(self.runtime.discarded, [release_id(native_record())])

    def test_a_rolled_back_native_release_is_never_discarded(self):
        self.deploy_native()
        self.deploy_native(request_id="req-2", commit="c", sequence=1001)
        self.assertEqual(self.run_locked(self.deployer.rollback, "req-3").status, "succeeded")
        self.assertEqual(self.runtime.discarded, [])

    def test_redeploying_the_committed_native_release_keeps_the_previous_directory(self):
        self.deploy_native()
        self.deploy_native(request_id="req-2", commit="c", sequence=1001)
        self.runtime.running = set()  # the release is down: the unchanged shortcut cannot fire
        self.assertEqual(self.deploy_native(request_id="req-3", commit="c", sequence=1001).status, "succeeded")
        self.assertEqual(self.state.read_state()["previous"], native_record())
        self.assertEqual(self.runtime.discarded, [])  # A's release directory is still there for a rollback


class JournalOpenFailureTests(DeployTestCase):
    """A prepared artifact must not outlive a transaction whose journal could not be opened."""

    def test_a_prepared_artifact_is_removed_when_the_journal_cannot_be_opened(self):
        with patch.object(self.state, "begin", side_effect=StateError("journal is unavailable")):
            outcome = self.deploy()
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "journal_unavailable"))
        self.assertEqual(self.runtime.discarded, ["ghcr.io/example/orders@sha256:" + "b" * 64])
        self.assertEqual(outcome.detail["warnings"], [])
        self.assertIsNone(self.state.read_journal())

    def test_a_prepared_artifact_is_kept_when_a_journal_references_it(self):
        begin = self.state.begin

        def begin_then_fail(request_id, candidate, **kwargs):
            begin(request_id, candidate, **kwargs)
            raise StateError("journal is unavailable")

        with patch.object(self.state, "begin", side_effect=begin_then_fail):
            outcome = self.deploy()
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "journal_unavailable"))
        self.assertEqual(self.runtime.discarded, [])  # recovery may still need the candidate
        self.assertEqual(self.state.read_journal()["request_id"], "req-1")


class CrashRecoveryTests(DeployTestCase):
    class Killed(BaseException):
        """Stands in for the worker process dying; deliberately not caught by the engine."""

    def crash_during_start(self, **kwargs):
        candidate = record(**kwargs)
        self.runtime.start_errors[release_id(candidate)] = self.Killed()
        with self.assertRaises(self.Killed):
            self.deploy(**kwargs)
        del self.runtime.start_errors[release_id(candidate)]

    def test_a_killed_update_leaves_a_journal_and_recover_restores_the_committed_release(self):
        self.deploy()
        self.crash_during_start(request_id="req-2", commit="c", image="d", sequence=1001)
        journal = self.state.read_journal()
        self.assertEqual(journal["phase"], "runtime-changing")
        self.assertEqual(self.runtime.running, set())  # the old container was removed before the kill
        self.assertEqual(self.deploy(request_id="req-3", commit="e", image="f", sequence=1002).detail["error"],
                         "recovery_required")
        outcome = self.run_locked(self.deployer.recover)
        self.assertEqual((outcome.status, outcome.detail["target"]), ("recovered", identity(record())))
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertIsNone(self.state.read_journal())
        self.assertEqual(self.state.read_state()["current"], record())

    def test_recovery_discards_the_interrupted_candidate_but_keeps_a_committed_one(self):
        self.deploy()
        self.crash_during_start(request_id="req-2", commit="c", image="d", sequence=1001)
        outcome = self.run_locked(self.deployer.recover)
        self.assertEqual(self.runtime.discarded, ["ghcr.io/example/orders@sha256:" + "d" * 64])
        self.assertEqual(outcome.detail["warnings"], [])

    def test_recovery_after_a_kill_following_the_commit_discards_nothing(self):
        self.deploy()
        candidate = record(commit="c", image="d", sequence=1001)
        with self.lock.acquire():
            self.state.begin("req-2", candidate)
            for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                self.state.advance(phase)
            self.state.record_commit()
        self.runtime.running = {release_id(candidate)}
        self.run_locked(self.deployer.recover)
        self.assertEqual(self.runtime.discarded, [])  # the candidate is now the current release

    def test_a_killed_first_release_recovers_to_nothing_and_removes_the_route(self):
        self.crash_during_start()
        self.route_file.write_text("orders.example.com {\n\treverse_proxy 127.0.0.1:20007\n}\n")  # as if routing began
        self.route_file.chmod(0o644)  # whatever the umask: a group-writable route file is correctly refused
        self.runtime.running = {release_id(record())}
        outcome = self.run_locked(self.deployer.recover)
        self.assertEqual((outcome.status, outcome.detail["target"]), ("recovered", None))
        self.assertEqual(self.runtime.running, set())
        self.assertFalse(self.route_file.exists())
        self.assertIsNone(self.state.read_journal())

    def test_recovery_after_a_kill_following_the_commit_keeps_the_new_release(self):
        self.deploy()
        candidate = record(commit="c", image="d", sequence=1001)
        with self.lock.acquire():
            self.state.begin("req-2", candidate)
            for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                self.state.advance(phase)
            self.state.record_commit()  # killed before the journal was cleared
        self.runtime.running = {release_id(candidate)}
        outcome = self.run_locked(self.deployer.recover)
        self.assertEqual((outcome.status, outcome.detail["target"]), ("recovered", identity(candidate)))
        self.assertEqual(self.runtime.names().count("start"), 1)  # already running and ready: not restarted
        self.assertEqual(self.state.read_state()["current"], candidate)

    def test_recover_reconciles_routing_even_when_the_runtime_is_already_correct(self):
        self.deploy()
        reloads = self.caddy.reload_calls
        with self.lock.acquire():
            self.state.begin("req-2", record(commit="c", image="d", sequence=1001))
        self.run_locked(self.deployer.recover)
        self.assertEqual(self.caddy.reload_calls, reloads + 1)

    def test_nothing_to_recover_without_a_journal(self):
        outcome = self.run_locked(self.deployer.recover)
        self.assertEqual((outcome.status, outcome.exit_code), ("nothing_to_recover", 0))

    def test_disagreeing_state_and_journal_require_manual_intervention(self):
        self.deploy()
        with self.lock.acquire():
            self.state.begin("req-2", record(commit="c", image="d", sequence=1001))
        (Path(self.temp.name) / "state" / "state.json").write_text("{}")
        outcome = self.run_locked(self.deployer.recover)
        self.assertEqual((outcome.status, outcome.exit_code), ("intervention_required", 6))


class RollbackAndStatusTests(DeployTestCase):
    def test_rollback_restores_the_previous_release_and_swaps_the_pair(self):
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        outcome = self.run_locked(self.deployer.rollback, "req-3")
        self.assertEqual((outcome.status, outcome.exit_code), ("succeeded", 0))
        committed = self.state.read_state()
        self.assertEqual((committed["current"]["commit"], committed["previous"]["commit"]), ("a" * 40, "c" * 40))
        self.assertEqual(self.runtime.running, {release_id(record())})
        self.assertEqual(self.runtime.discarded, [])  # the image rolled back to is never discarded
        newer = self.deploy(request_id="req-4", commit="e", image="f", sequence=1002)
        self.assertEqual(newer.status, "succeeded")  # the older sequence no longer blocks a newer release

    def test_rollback_without_a_previous_release_or_with_an_unfinished_transaction_is_refused(self):
        self.assertEqual(self.run_locked(self.deployer.rollback, "req-9").detail["error"], "no_previous_release")
        self.deploy()
        self.assertEqual(self.run_locked(self.deployer.rollback, "req-9").detail["error"], "no_previous_release")
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        with self.lock.acquire():
            self.state.begin("req-5", record(commit="e", image="f", sequence=1002))
        self.assertEqual(self.run_locked(self.deployer.rollback, "req-9").detail["error"], "recovery_required")

    def test_a_failed_rollback_restores_the_release_that_was_current(self):
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(record())] = RuntimeFailure("image gone", "start")
        outcome = self.run_locked(self.deployer.rollback, "req-3")
        self.assertEqual(outcome.status, "rolled_back")  # nonzero: the rollback itself failed
        self.assertEqual(outcome.exit_code, 5)
        self.assertEqual(self.state.read_state()["current"]["commit"], "c" * 40)

    def test_rollback_refuses_a_previous_release_that_is_the_current_one(self):
        # A host that ran the older helper can hold `previous == current`: the real rollback target is already gone.
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        committed = self.state.read_state()
        state = Path(self.temp.name) / "state" / "state.json"
        state.write_text(json.dumps(dict(committed, previous=committed["current"])) + "\n")
        state.chmod(0o600)
        calls = list(self.runtime.calls)
        outcome = self.run_locked(self.deployer.rollback, "req-3")
        self.assertEqual((outcome.status, outcome.exit_code, outcome.detail["error"]),
                         ("failed", 1, "no_previous_release"))
        self.assertEqual(self.runtime.calls, calls)  # the broken release is not restarted and called a rollback

    def test_rollback_renders_the_registered_aliases_again(self):
        self.alias_deployer()
        self.assertEqual(self.run_locked(self.deployer.deploy, self.alias_request()).status, "succeeded")
        self.assertEqual(self.run_locked(self.deployer.deploy, self.alias_request(
            request_id="req-2", commit="c", image="d", sequence=1001)).status, "succeeded")
        self.route_file.write_text("orders.example.com {\n\treverse_proxy 127.0.0.1:1\n}\n")  # drifted
        outcome = self.run_locked(self.deployer.rollback, "req-3")
        self.assertEqual((outcome.status, outcome.exit_code), ("succeeded", 0))
        route = self.route_file.read_bytes()
        self.assertIn(b"\nlegacy.example.com {\n\tredir https://orders.example.com{uri} 308\n}\n", route)
        self.assertIn(b"\nwww.orders.example.com {\n\tredir https://orders.example.com{uri} 308\n}\n", route)

    def test_status_reports_current_previous_running_and_unfinished_work(self):
        self.assertEqual(self.run_locked(self.deployer.status)["current"], None)
        self.deploy()
        self.deploy(request_id="req-2", commit="c", image="d", sequence=1001)
        status = self.deployer.status()
        self.assertEqual((status["current"]["commit"], status["previous"]["commit"], status["running"], status["generation"]),
                         ("c" * 40, "a" * 40, True, 2))
        self.assertIsNone(status["unfinished_transaction"])
        with self.lock.acquire():
            self.state.begin("req-3", record(commit="e", image="f", sequence=1002))
        self.assertEqual(self.deployer.status()["unfinished_transaction"], {"request_id": "req-3", "phase": "prepared"})


if __name__ == "__main__":
    unittest.main()
