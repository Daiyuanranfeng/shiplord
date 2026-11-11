from __future__ import annotations

import io
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeCaddy, FakeProbes, FakeRuntime, configuration, record, request  # noqa: E402
from shiplord.admin import run as admin_run  # noqa: E402
from shiplord.host import HostDeadline  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.jobs import JobError, JobStore  # noqa: E402
from shiplord.layout import Layout  # noqa: E402
from shiplord.probes import ProbeFailure  # noqa: E402
from shiplord.privileged_host import dispatch, job_view, serve  # noqa: E402
from shiplord.protocol import PROTOCOL_VERSION, ProtocolError  # noqa: E402
from shiplord.registry import Registry  # noqa: E402
from shiplord.release_state import HostLock, ReleaseState, StateError  # noqa: E402
from shiplord.runtime import RuntimeFailure, release_id  # noqa: E402
from shiplord.services import AppSession, HostServices  # noqa: E402
from shiplord.worker import run_job  # noqa: E402


class FakeLauncher:
    def __init__(self):
        self.launched: list[tuple[str, str]] = []
        self.active = True  # systemd-run returns once the unit is running
        self.error = None

    def launch(self, application_id, request_id):
        if self.error:
            raise self.error
        self.launched.append((application_id, request_id))

    def is_active(self, application_id, request_id):
        return self.active


class HelperTestCase(unittest.TestCase):
    RUNTIME = "docker"

    def setUp(self):
        # Serving an upload re-arms the process-wide deadline timer, which only run_timed() normally cancels.
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        patcher = patch("os.fsync", lambda descriptor: None)  # durability is covered by the journal tests
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Layout(Path(self.temp.name).resolve())
        for directory, mode in ((self.layout.var, 0o755), (self.layout.registrations, 0o700), (self.layout.app_state, 0o700),
                                (self.layout.jobs, 0o700), (self.layout.caddy_root / "ship", 0o755)):
            directory.mkdir(parents=True)
            directory.chmod(mode)
        self.layout.caddy_root.chmod(0o755)
        main = self.layout.caddy_root / "Caddyfile"
        main.write_text(f"import {self.layout.caddy_root}/ship/*.caddy\n")
        main.chmod(0o644)
        var_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        registry_fd = os.open(self.layout.registrations, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, var_fd)
        self.addCleanup(os.close, registry_fd)
        lock = HostLock(var_fd)
        with lock.acquire():
            self.registration = Registry(registry_fd, lock).register(
                "orders", deployment_uid=1001, deployment_user="ship-orders", runtime=self.RUNTIME,
                api_domain="orders.example.com",
                image_repository="ghcr.io/example/orders" if self.RUNTIME == "docker" else None, probe=lambda port: True)
            (self.layout.app_state / "orders").mkdir(mode=0o700)
            state_fd = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
            ReleaseState("orders", state_fd, lock).initialize()
            os.close(state_fd)
        self.launcher, self.caddy, self.runtime = FakeLauncher(), FakeCaddy(), FakeRuntime()
        self.probes = FakeProbes(self.runtime)
        self.services = HostServices(self.layout, system=None, launcher=self.launcher, caddy=self.caddy,
                                     probes=self.probes, runtime_factory=lambda services, registration: self.runtime, lock_timeout=0.3)
        self.factory_calls = 0

    def factory(self):
        self.factory_calls += 1
        return self.services

    def envelope(self, action, request_id="req-1", commit=None, **arguments):
        return {"version": PROTOCOL_VERSION, "request_id": request_id, "action": action, "application_id": "orders",
                "commit": commit, "arguments": arguments}

    def call(self, action, request_id="req-1", commit=None, **arguments):
        return dispatch(self.envelope(action, request_id, commit, **arguments), self.registration, self.factory)

    def deploy_arguments(self, **overrides):
        value = request(**overrides)
        return value["commit"], {"sequence": value["sequence"], "artifact": value["artifact"],
                                 "configuration": value["configuration"]}

    def submit_deploy(self, request_id="req-1", **overrides):
        commit, arguments = self.deploy_arguments(**overrides)
        return self.call("deploy", request_id, commit, **arguments)

    @property
    def state(self):
        descriptor = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        return ReleaseState("orders", descriptor, HostLock(descriptor))


class ReadOnlyActionTests(HelperTestCase):
    def test_capabilities_advertise_every_supported_action_without_touching_the_host(self):
        response, code = self.call("capabilities")
        self.assertEqual((code, response["status"]), (0, "ok"))
        self.assertEqual(response["actions"], ["capabilities", "plan", "deploy", "status", "logs", "rollback", "restart", "recover", "docker_storage_v1", "docker_shutdown_v1", "docker_generic_v1", "recover_force_stop_v1"])
        self.assertEqual(self.factory_calls, 0)

    def test_malformed_arguments_are_rejected_before_any_host_state_is_opened(self):
        bad = [("status", {"unexpected": 1}), ("status", {"job_id": "../x"}), ("status", {"job_id": 5}),
               ("logs", {"tail": 0}), ("logs", {"tail": 1001}), ("logs", {"tail": "5"}), ("logs", {"tail": True}),
               ("deploy", {}), ("deploy", {"sequence": 1, "artifact": {}}), ("plan", {"bogus": 1}),
               ("rollback", {"x": 1}), ("restart", {"x": 1}), ("recover", {"x": 1}),
               ("recover", {"force_stop": "yes"}), ("recover", {"force_stop": 1})]
        for action, arguments in bad:
            with self.subTest(action=action, arguments=arguments), self.assertRaises(ProtocolError):
                self.call(action, **arguments)
        with self.assertRaises(ProtocolError):
            self.call("rollback", commit="a" * 40)
        with self.assertRaises(ProtocolError):
            self.call("restart", commit="a" * 40)
        with self.assertRaises(ProtocolError):
            self.call("deploy", commit="a" * 40, sequence=1, artifact={}, configuration={}, allow_older="yes")
        self.assertEqual(self.factory_calls, 0)

    def test_status_reports_the_application_and_an_optional_job(self):
        response, code = self.call("status")
        self.assertEqual(code, 0)
        self.assertEqual(response["result"]["application"]["current"], None)
        self.assertIsNone(response["result"]["job"])
        self.assertIsNone(self.call("status", job_id="nope")[0]["result"]["job"])
        with self.assertRaises(Exception):
            self.call("status", job_id="../etc/passwd")

    def test_logs_return_the_runtime_output_with_the_requested_options(self):
        self.runtime.log_output = "tail=7\n"
        self.assertEqual(self.call("logs", tail=7, since="15m", timestamps=True)[0]["result"], {"logs": "tail=7\n"})
        self.assertEqual(self.runtime.log_arguments,
                         {"release": None, "tail": 7, "since": "15m", "timestamps": True})
        self.assertEqual(self.call("logs")[0]["result"], {"logs": "tail=7\n"})
        self.assertEqual(self.runtime.log_arguments,
                         {"release": None, "tail": 100, "since": None, "timestamps": False})

    def test_logs_read_the_release_that_is_current(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.call("logs", tail=5)
        self.assertEqual(self.runtime.log_arguments["release"], self.state.read_state()["current"])

    def test_logs_never_take_the_host_lock(self):
        output = io.BytesIO()
        self.runtime.follow_chunks = ["line\n"]
        with patch.object(HostLock, "acquire", side_effect=AssertionError("logs must not lock the host")):
            self.assertEqual(self.call("logs")[0]["result"], {"logs": ""})
            response, code = dispatch(self.envelope("logs", follow=True, timeout=5), self.registration, self.factory,
                                      destination=output)
        self.assertEqual((response, code), (None, 0))
        self.assertEqual(read_frame(io.BytesIO(output.getvalue()))["status"], "stream")

    def test_follow_streams_frames_and_a_final_result(self):
        self.runtime.follow_chunks, self.runtime.follow_stopped = ["one\n", "two\n"], "timeout"
        output = io.BytesIO()
        response, code = dispatch(self.envelope("logs", tail=5, follow=True, timeout=30), self.registration,
                                  self.factory, destination=output)
        self.assertEqual((response, code), (None, 0))
        stream = io.BytesIO(output.getvalue())
        frames = [read_frame(stream), read_frame(stream), read_frame(stream)]
        self.assertEqual(frames[0], {"status": "stream", "request_id": "req-1", "chunk": "one\n"})
        self.assertEqual(frames[1]["chunk"], "two\n")
        self.assertEqual(frames[2], {"status": "ok", "request_id": "req-1",
                                     "result": {"stopped": "timeout", "release": None}})
        self.assertEqual(stream.read(1), b"")  # nothing follows the final frame
        self.assertEqual(self.runtime.follow_arguments,
                         {"release": None, "tail": 5, "since": None, "timestamps": False, "duration": 30})

    def test_a_follow_request_needs_a_destination_stream(self):
        with self.assertRaises(ProtocolError):
            self.call("logs", follow=True)
        self.assertEqual(self.factory_calls, 0)

    def test_log_options_are_validated_before_any_host_state_is_opened(self):
        bad = [{"since": "1d"}, {"since": 5}, {"timestamps": "yes"}, {"follow": "yes"},
               {"follow": True, "timeout": 0}, {"timeout": 3601}, {"timeout": "60"}, {"unexpected": 1}]
        for arguments in bad:
            with self.subTest(arguments=arguments), self.assertRaises(ProtocolError):
                self.call("logs", **arguments)
        self.assertEqual(self.factory_calls, 0)


    def test_status_job_lookup_is_scoped_to_the_application(self):
        billing = self.layout.jobs / "billing"
        billing.mkdir(mode=0o700)
        descriptor = os.open(billing, os.O_RDONLY | os.O_DIRECTORY)
        try:
            JobStore("billing", descriptor).create("billing-1", "deploy", request())
        finally:
            os.close(descriptor)
        self.assertIsNone(self.call("status", job_id="billing-1")[0]["result"]["job"])

    def test_plan_describes_the_change_and_changes_nothing(self):
        commit, arguments = self.deploy_arguments()
        response, code = self.call("plan", "plan-1", commit, **arguments)
        plan = response["result"]
        self.assertEqual((code, plan["action"], plan["route_change"], plan["restarts_application"]),
                         (0, "deploy", "create", False))
        self.assertEqual((plan["domain"], plan["host_port"]), ("orders.example.com", self.registration["host_port"]))
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(os.listdir(self.layout.caddy_root / "ship"), [])
        self.assertEqual(self.state.read_state()["generation"], 0)

    def test_plan_reports_refusals_instead_of_raising(self):
        commit, arguments = self.deploy_arguments()
        arguments["artifact"] = {"kind": "docker", "image": "ghcr.io/example/other@sha256:" + "b" * 64}
        plan = self.call("plan", "plan-1", commit, **arguments)[0]["result"]
        self.assertEqual((plan["action"], plan["reason"]), ("none", "rejected"))


class DeployJobTests(HelperTestCase):
    def test_deploy_accepts_a_job_launches_the_worker_once_and_is_idempotent(self):
        response, code = self.submit_deploy()
        self.assertEqual((code, response["status"], response["job"]["state"]), (0, "accepted", "queued"))
        self.assertEqual(self.launcher.launched, [("orders", "req-1")])
        self.assertEqual(self.runtime.calls, [])  # the helper itself deploys nothing
        again, code = self.submit_deploy()
        self.assertEqual((code, again["status"]), (0, "accepted"))
        self.assertEqual(self.launcher.launched, [("orders", "req-1")])  # not launched twice

    def test_retrying_a_failed_request_returns_its_recorded_outcome_not_a_reuse_error(self):
        self.runtime.prepare_error = RuntimeFailure("denied", "prepare")
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        response, code = self.submit_deploy()
        self.assertEqual((code, response["status"], response["job"]["outcome"]["detail"]["error"]),
                         (0, "accepted", "prepare_failed"))
        self.assertEqual(len(self.launcher.launched), 1)

    def test_the_same_request_id_with_different_content_is_refused(self):
        self.submit_deploy()
        with self.assertRaises(JobError):
            self.submit_deploy(commit="c")

    def test_invalid_requests_are_rejected_immediately_without_creating_a_job(self):
        for overrides in ({"artifact": {"kind": "docker", "image": "ghcr.io/example/other@sha256:" + "b" * 64}},
                          {"configuration": configuration(api_domain="evil.example.com")}):
            with self.subTest(overrides=str(overrides)[:50]):
                response, code = self.submit_deploy(**overrides)
                self.assertEqual((code, response["status"], response["outcome"]["status"]), (2, "finished", "rejected"))
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.assertEqual(self.launcher.launched, [])

    def test_a_launch_failure_finishes_the_job_as_failed_and_is_reported(self):
        self.launcher.error = JobError("systemd unavailable")
        response, code = self.submit_deploy()
        self.assertEqual((code, response["error"], response["job"]["state"]), (1, "launch_failed", "finished"))
        self.assertEqual(response["job"]["outcome"]["exit_code"], 1)

    def test_a_job_whose_worker_vanished_is_reported_as_interrupted(self):
        self.submit_deploy()
        self.assertEqual(self.call("status", job_id="req-1")[0]["result"]["job"]["state"], "queued")
        self.launcher.active = False
        self.assertEqual(self.call("status", job_id="req-1")[0]["result"]["job"]["state"], "interrupted")

    def test_a_job_that_finished_while_its_unit_vanished_is_not_reported_as_interrupted(self):
        self.submit_deploy()
        with AppSession(self.services, "orders") as session:
            stale = session.jobs.get("req-1")  # a status poll read this while the worker was still queued
            self.assertEqual(stale["state"], "queued")
            self.assertEqual(run_job(self.services, "orders", "req-1"), 0)  # the worker records the outcome, then exits
            self.launcher.active = False  # the unit is gone before the poll asks systemd whether it is active
            job = job_view(session, stale)
        self.assertEqual((job["state"], job["outcome"]["status"], job["outcome"]["exit_code"]), ("finished", "succeeded", 0))

    def test_a_finished_job_is_reported_as_finished_even_when_its_unit_is_gone(self):
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        self.launcher.active = False
        job = self.call("status", job_id="req-1")[0]["result"]["job"]
        self.assertEqual((job["state"], job["outcome"]["exit_code"]), ("finished", 0))

    def test_a_job_whose_record_disappears_with_its_worker_is_reported_as_interrupted(self):
        self.submit_deploy()
        with AppSession(self.services, "orders") as session:
            stale = session.jobs.get("req-1")
            (self.layout.jobs / "orders" / "req-1.json").unlink()  # no recorded outcome can be confirmed
            self.launcher.active = False
            job = job_view(session, stale)
        self.assertEqual(job["state"], "interrupted")

    def test_a_flood_of_distinct_requests_is_capped_but_dead_jobs_do_not_count(self):
        from shiplord.jobs import MAX_PENDING_JOBS
        for number in range(MAX_PENDING_JOBS):
            self.assertEqual(self.submit_deploy(f"req-{number}", sequence=1000 + number)[1], 0)
        response, code = self.submit_deploy("one-too-many", sequence=2000)
        self.assertEqual((code, response["error"]), (1, "too_many_pending"))
        self.assertEqual(len(self.launcher.launched), MAX_PENDING_JOBS)
        self.assertEqual(self.submit_deploy("req-0", sequence=1000)[1], 0)  # a repeat of a known request is not new work
        self.launcher.active = False  # their workers are gone: they are interrupted, not pending
        self.assertEqual(self.submit_deploy("after-the-crash", sequence=3000)[1], 0)

    def test_rollback_and_recover_become_jobs_too(self):
        for action in ("rollback", "recover"):
            response, code = self.call(action, f"{action}-1")
            self.assertEqual((code, response["job"]["action"]), (0, action))
        self.assertEqual([r for _, r in self.launcher.launched], ["rollback-1", "recover-1"])


class RestartJobTests(HelperTestCase):
    """`restart` is submitted, refused, and recorded like the other state-changing actions."""

    def deploy_a_release(self):
        with AppSession(self.services, "orders") as session:
            with session.lock.acquire():
                self.assertEqual(session.deployer().deploy(request(request_id="seed")).status, "succeeded")
        self.runtime.calls.clear()

    def test_a_restart_becomes_a_job_and_a_repeated_request_id_launches_nothing_new(self):
        self.deploy_a_release()
        response, code = self.call("restart", "restart-1")
        self.assertEqual((code, response["status"], response["job"]["action"], response["job"]["state"]),
                         (0, "accepted", "restart", "queued"))
        self.assertEqual(self.launcher.launched, [("orders", "restart-1")])
        again, code = self.call("restart", "restart-1")
        self.assertEqual((code, again["status"], again["job"]["state"]), (0, "accepted", "queued"))
        self.assertEqual(len(self.launcher.launched), 1)  # idempotent by request ID

    def test_a_restart_with_nothing_deployed_is_refused_before_a_job_exists(self):
        response, code = self.call("restart", "restart-1")
        self.assertEqual((code, response["status"], response["outcome"]["status"],
                          response["outcome"]["detail"]["error"]), (2, "finished", "rejected", "nothing_deployed"))
        self.assertIn("nothing to restart", response["outcome"]["detail"]["message"])
        self.assertEqual(self.launcher.launched, [])
        self.assertEqual(os.listdir(self.layout.jobs / "orders"), [])
        self.assertEqual(self.runtime.calls, [])

    def test_a_restart_while_a_journal_exists_requires_recovery_before_any_host_work(self):
        self.deploy_a_release()
        with AppSession(self.services, "orders") as session:
            with session.lock.acquire():
                session.state.begin("interrupted", record(commit="c", image="d", sequence=1001))
        response, code = self.call("restart", "restart-1")
        self.assertEqual((code, response["status"], response["outcome"]["detail"]["error"]),
                         (1, "finished", "recovery_required"))
        self.assertEqual(self.launcher.launched, [])
        self.assertEqual(self.runtime.calls, [])


class WorkerTests(HelperTestCase):
    def finished(self, request_id="req-1"):
        return self.call("status", job_id=request_id)[0]["result"]["job"]

    def lock_state(self):
        """Is the host-wide lock held right now? Probed with a real flock, as another process would see it."""
        descriptor = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with HostLock(descriptor).acquire(timeout=0):
                return "free"
        except StateError:
            return "held"
        finally:
            os.close(descriptor)

    def test_the_image_is_pulled_before_the_host_lock_is_taken(self):
        seen = []
        self.runtime.pull = lambda registration, release: seen.append(("pull", self.lock_state()))
        original_prepare = self.runtime.prepare
        self.runtime.prepare = lambda registration, release: (seen.append(("prepare", self.lock_state())),
                                                              original_prepare(registration, release))[1]
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        self.assertEqual(seen[0], ("pull", "free"))  # a slow registry must never hold the host-wide lock
        self.assertEqual(seen[1], ("prepare", "held"))

    def test_a_slow_pull_does_not_block_a_read_only_admin_action(self):
        """A registry that takes minutes must not stop an operator reading the host's state."""
        started, release = threading.Event(), threading.Event()

        def slow_pull(registration, release_value):
            started.set()
            release.wait(10)

        self.runtime.pull = slow_pull
        self.submit_deploy()
        worker = threading.Thread(target=lambda: run_job(self.services, "orders", "req-1"))
        answered = []
        message = {"version": PROTOCOL_VERSION, "request_id": "read-1", "action": "apps", "application_id": None,
                   "commit": None, "arguments": {}}
        reader = None
        try:
            worker.start()
            self.assertTrue(started.wait(5), "the pull never started")
            reader = threading.Thread(target=lambda: answered.append(admin_run(message, self.layout, None, {})))
            reader.start()
            reader.join(2)
            # Without the fix this blocks on the host lock until the pull finishes, which is the whole bug.
            self.assertEqual(len(answered), 1, "a read-only admin action blocked behind a slow pull")
            self.assertEqual(answered[0][0]["status"], "ok", answered[0])
            self.assertEqual(answered[0][1], 0)
        finally:
            release.set()
            worker.join(10)
            if reader is not None:
                reader.join(10)

    def test_a_rollback_pulls_the_release_it_restores_before_the_lock(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.submit_deploy("req-2", commit="c", image="d", sequence=1001)
        run_job(self.services, "orders", "req-2")
        seen = []
        self.runtime.pull = lambda registration, release: seen.append((release_id(release), self.lock_state()))
        self.call("rollback", "rb-1")
        self.assertEqual(run_job(self.services, "orders", "rb-1"), 0)
        self.assertEqual(seen, [(release_id(record()), "free")])  # the previous committed release, unlocked

    def test_a_restart_pulls_the_committed_release_before_the_lock(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        seen = []
        self.runtime.pull = lambda registration, release: seen.append((release_id(release), self.lock_state()))
        self.call("restart", "rs-1")
        self.assertEqual(run_job(self.services, "orders", "rs-1"), 0)
        self.assertEqual(seen, [(release_id(record()), "free")])  # the committed release, unlocked

    def test_a_restart_job_restarts_the_committed_release_instead_of_recovering(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.submit_deploy("req-2", commit="c", image="d", sequence=1001)
        run_job(self.services, "orders", "req-2")
        before = self.state.read_state()
        self.runtime.calls.clear()
        self.call("restart", "rs-1")
        self.assertEqual(run_job(self.services, "orders", "rs-1"), 0)
        job = self.finished("rs-1")
        self.assertEqual((job["action"], job["outcome"]["status"], job["outcome"]["exit_code"]),
                         ("restart", "succeeded", 0))
        self.assertEqual(self.runtime.names(), ["pull", "prepare", "start"])  # not a recover, which starts nothing
        committed = self.state.read_state()
        self.assertEqual((committed["current"], committed["previous"]), (before["current"], before["previous"]))
        self.assertEqual(self.runtime.running, {release_id(before["current"])})

    def test_a_failed_restart_keeps_the_journal_and_never_claims_a_rollback(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.probes.not_ready[release_id(record())] = ProbeFailure("the application exited", "exited")
        self.call("restart", "rs-1")
        self.assertEqual(run_job(self.services, "orders", "rs-1"), 6)
        outcome = self.finished("rs-1")["outcome"]
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("intervention_required", 6))
        self.assertEqual(outcome["detail"]["error"], "restart_failed")
        self.assertNotIn("restored_release", outcome["detail"])
        self.assertIn("shiplord recover", outcome["detail"]["recovery"])
        self.assertIsNotNone(self.state.read_journal())  # retained: only a verified recovery may clear it
        self.assertEqual(self.state.read_state()["current"], record())  # the recorded release never moved

    def test_an_interrupted_restart_keeps_the_journal_and_recovers_to_the_committed_release(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        original = self.runtime.start
        self.runtime.start = lambda registration, release: (_ for _ in ()).throw(RuntimeError("killed"))
        self.call("restart", "rs-1")
        self.assertEqual(run_job(self.services, "orders", "rs-1"), 6)  # a half-done transaction, not a success
        self.assertIsNotNone(self.state.read_journal())
        self.runtime.start = original
        self.call("recover", "rc-1")
        self.assertEqual(run_job(self.services, "orders", "rc-1"), 0)
        self.assertEqual(self.finished("rc-1")["outcome"]["status"], "recovered")
        self.assertIsNone(self.state.read_journal())
        committed = self.state.read_state()
        self.assertEqual((committed["current"], committed["previous"]), (record(), None))
        self.assertEqual(self.runtime.running, {release_id(record())})

    def test_a_request_that_became_stale_pulls_nothing(self):
        self.submit_deploy("req-1", sequence=1001)  # queued while it was still the newest release
        with AppSession(self.services, "orders") as session:
            with session.lock.acquire():
                session.deployer().deploy(request(request_id="req-2", commit="c", image="d", sequence=2000))
        self.runtime.calls.clear()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        self.assertEqual(self.finished("req-1")["outcome"]["detail"]["error"], "stale_release")
        self.assertEqual(self.runtime.names(), [])  # a refused request never reaches a registry

    def test_a_failed_pull_is_reported_by_the_locked_transaction_as_a_prepare_failure(self):
        self.runtime.pull_error = RuntimeFailure("the image could not be pulled", "prepare")
        self.runtime.prepare_error = RuntimeFailure("the image is not available on this host; nothing was changed",
                                                    "prepare")
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        self.assertEqual(self.finished()["outcome"]["detail"]["error"], "prepare_failed")
        self.assertEqual(self.runtime.names(), ["pull", "prepare"])  # nothing was started

    def test_every_state_transition_happens_under_the_host_lock(self):
        transitions = []

        def spy(name, original):
            def call(*args, **kwargs):
                transitions.append((name, self.lock_state()))
                return original(*args, **kwargs)
            return call

        for name in ("begin", "advance", "record_commit", "clear_after_reconciliation"):
            patcher = patch.object(ReleaseState, name, spy(name, getattr(ReleaseState, name)))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        self.assertEqual([name for name, _ in transitions],
                         ["begin", "advance", "advance", "advance", "advance", "record_commit",
                          "clear_after_reconciliation"])
        self.assertTrue(all(state == "held" for _, state in transitions), transitions)

    def test_the_worker_runs_the_deploy_and_records_the_outcome_for_a_reconnecting_client(self):
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        job = self.finished()
        self.assertEqual((job["state"], job["outcome"]["status"], job["outcome"]["exit_code"]), ("finished", "succeeded", 0))
        self.assertEqual(job["outcome"]["detail"]["release"]["commit"], "a" * 40)
        self.assertEqual(self.state.read_state()["current"], record())
        self.assertEqual(self.runtime.running, {release_id(record())})
        # a duplicate worker, or a status poll, never reruns or alters a finished job
        self.assertEqual(run_job(self.services, "orders", "req-1"), 0)
        self.assertEqual(self.runtime.names(), ["pull", "prepare", "start"])

    def test_a_deploy_resent_after_success_is_answered_without_a_new_job(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        launched = len(self.launcher.launched)
        response, code = self.submit_deploy()
        self.assertEqual(code, 0)
        self.assertEqual(response["status"], "accepted")  # the existing finished job is returned as is
        self.assertEqual(response["job"]["state"], "finished")
        self.assertEqual(len(self.launcher.launched), launched)

    def test_failure_exit_codes_are_recorded_and_returned(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        bad = record(commit="c", image="d", sequence=1001)
        self.runtime.start_errors[release_id(bad)] = RuntimeFailure("boom", "start")
        self.submit_deploy("req-2", commit="c", image="d", sequence=1001)
        self.assertEqual(run_job(self.services, "orders", "req-2"), 5)
        job = self.finished("req-2")
        self.assertEqual((job["outcome"]["status"], job["outcome"]["exit_code"]), ("rolled_back", 5))

    def test_phases_are_visible_while_the_job_runs(self):
        seen = []
        original = self.runtime.start

        def spying_start(registration, release):
            seen.append(self.finished()["phase"])
            return original(registration, release)
        self.runtime.start = spying_start
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.assertEqual(seen, ["runtime-changing"])

    def test_a_busy_host_lock_is_reported_and_nothing_is_changed(self):
        self.submit_deploy()
        descriptor = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        with HostLock(descriptor).acquire():
            self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        job = self.finished()
        self.assertEqual(job["outcome"]["detail"]["error"], "host_busy")
        # the image is already on the host when the lock is found busy: the pull is not part of the transaction
        self.assertEqual(self.runtime.names(), ["pull"])

    def test_an_unexpected_error_before_any_change_is_a_plain_failure(self):
        self.runtime.prepare = lambda registration, release: (_ for _ in ()).throw(KeyError("surprise"))
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 1)
        self.assertEqual(self.finished()["outcome"]["detail"], {"error": "worker_error", "message": "KeyError"})

    def test_an_unexpected_error_mid_transaction_requires_intervention_and_keeps_the_journal(self):
        self.runtime.start = lambda registration, release: (_ for _ in ()).throw(RuntimeError("bug"))
        self.submit_deploy()
        self.assertEqual(run_job(self.services, "orders", "req-1"), 6)
        self.assertEqual(self.finished()["outcome"]["status"], "intervention_required")
        self.assertIsNotNone(self.state.read_journal())

    def test_unknown_or_unqueued_jobs_do_nothing(self):
        self.assertEqual(run_job(self.services, "orders", "never-created"), 2)
        self.assertEqual(self.runtime.calls, [])

    def test_recover_and_rollback_jobs_run_through_the_same_engine(self):
        self.submit_deploy()
        run_job(self.services, "orders", "req-1")
        self.submit_deploy("req-2", commit="c", image="d", sequence=1001)
        run_job(self.services, "orders", "req-2")
        self.call("rollback", "rb-1")
        self.assertEqual(run_job(self.services, "orders", "rb-1"), 0)
        self.assertEqual(self.state.read_state()["current"]["commit"], "a" * 40)
        self.call("recover", "rc-1")
        self.assertEqual(run_job(self.services, "orders", "rc-1"), 0)
        self.assertEqual(self.finished("rc-1")["outcome"]["status"], "nothing_to_recover")

    def test_a_forced_stop_is_carried_into_the_job_and_its_outcome(self):
        response, code = self.call("recover", "rc-force", force_stop=True)
        self.assertEqual((code, response["job"]["action"]), (0, "recover"))
        stored = json.loads((self.layout.jobs / "orders" / "rc-force.json").read_text())
        self.assertEqual(stored["request"], {"force_stop": True})
        self.assertEqual(run_job(self.services, "orders", "rc-force"), 0)
        outcome = self.finished("rc-force")["outcome"]
        self.assertEqual((outcome["status"], outcome["exit_code"]), ("recovered", 0))
        self.assertTrue(outcome["detail"]["forced_stop"]["forced"])
        self.assertFalse(outcome["detail"]["running"])

    def test_a_plain_recovery_job_carries_no_force_stop(self):
        self.call("recover", "rc-plain")
        stored = json.loads((self.layout.jobs / "orders" / "rc-plain.json").read_text())
        self.assertEqual(stored["request"], {})


class NativeRecoverTests(HelperTestCase):
    RUNTIME = "native"

    def test_a_forced_stop_is_refused_for_a_native_application_before_a_job_exists(self):
        response, code = self.call("recover", "rc-native", force_stop=True)
        self.assertEqual((code, response["error"]), (2, "invalid_request"))
        self.assertEqual(self.launcher.launched, [])
        self.assertFalse((self.layout.jobs / "orders" / "rc-native.json").exists())


class ServeTests(HelperTestCase):
    def serve(self, message):
        encoded, output = io.BytesIO(), io.BytesIO()
        write_frame(encoded, message)
        with patch("shiplord.privileged_host.authorize_application", return_value=self.registration):
            code = serve(io.BytesIO(encoded.getvalue()), output, self.factory)
        return code, read_frame(io.BytesIO(output.getvalue()))

    def test_frames_round_trip_for_a_deploy_submission_and_a_status_poll(self):
        commit, arguments = self.deploy_arguments()
        code, response = self.serve(self.envelope("deploy", "req-1", commit, **arguments))
        self.assertEqual((code, response["status"], response["request_id"]), (0, "accepted", "req-1"))
        run_job(self.services, "orders", "req-1")
        code, response = self.serve(self.envelope("status", "poll-1", job_id="req-1"))
        self.assertEqual((code, response["result"]["job"]["outcome"]["status"]), (0, "succeeded"))

    def test_protocol_and_state_errors_map_to_exit_codes_without_leaking_internals(self):
        code, response = self.serve(self.envelope("logs", tail=0))
        self.assertEqual((code, response["error"]), (2, "invalid_request"))
        code, response = self.serve(dict(self.envelope("status"), version=2))
        self.assertEqual((code, response["status"]), (4, "incompatible"))
        self.services.layout = Layout(Path(self.temp.name) / "missing")
        code, response = self.serve(self.envelope("status"))
        self.assertEqual((code, response["error"]), (1, "failed"))
        self.assertNotIn(self.temp.name, str(response))  # host paths are not disclosed to the deployment account

    def test_the_deadline_is_not_swallowed_by_the_generic_failure_handler(self):
        def expired():
            raise HostDeadline()
        encoded = io.BytesIO()
        write_frame(encoded, self.envelope("status"))
        with patch("shiplord.privileged_host.authorize_application", return_value=self.registration):
            with self.assertRaises(HostDeadline):
                serve(io.BytesIO(encoded.getvalue()), io.BytesIO(), expired)

    def test_an_unforeseen_exception_is_a_generic_failure_without_a_traceback_or_paths(self):
        def broken():
            raise RuntimeError(f"boom at {self.temp.name}/secret")
        encoded, output = io.BytesIO(), io.BytesIO()
        write_frame(encoded, self.envelope("status"))
        with patch("shiplord.privileged_host.authorize_application", return_value=self.registration):
            code = serve(io.BytesIO(encoded.getvalue()), output, broken)
        response = read_frame(io.BytesIO(output.getvalue()))
        self.assertEqual((code, response["error"]), (1, "failed"))
        self.assertNotIn("boom", str(response))
        self.assertNotIn(self.temp.name, str(response))


if __name__ == "__main__":
    unittest.main()
