"""Boot-time recovery: an interrupted transaction is finished without a person running `recover`."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeCaddy, FakeProbes, FakeRuntime, record  # noqa: E402
from shiplord.boot import (FAILED, NOTHING, RECOVERED, journal_open, recover_application,  # noqa: E402
                           recover_interrupted, registered_applications)
from shiplord.layout import Layout  # noqa: E402
from shiplord.registry import Registry  # noqa: E402
from shiplord.release_state import HostLock, ReleaseState  # noqa: E402
from shiplord.runtime import RuntimeFailure, release_id  # noqa: E402
from shiplord.services import AppSession, HostServices  # noqa: E402


class FakeLauncher:
    def __init__(self):
        self.live: set[str] = set()

    def launch(self, application_id, request_id):
        raise AssertionError("boot recovery never launches a job")

    def is_active(self, application_id, request_id):
        return request_id in self.live


class BootRecoveryTestCase(unittest.TestCase):
    APPLICATIONS = ("orders", "reports")

    def setUp(self):
        # Durability (fsync ordering) is covered by the journal tests; here it only costs time.
        patcher = patch("os.fsync", lambda descriptor: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = Layout(Path(self.temp.name).resolve())
        for directory, mode in ((self.layout.var, 0o755), (self.layout.registrations, 0o700),
                                (self.layout.app_state, 0o700), (self.layout.jobs, 0o700),
                                (self.layout.caddy_root / "ship", 0o755)):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(mode)
        self.layout.caddy_root.chmod(0o755)
        main = self.layout.caddy_root / "Caddyfile"
        main.write_text(f"import {self.layout.caddy_root}/ship/*.caddy\n")
        main.chmod(0o644)
        var = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, var)
        self.lock = HostLock(var)
        registrations = os.open(self.layout.registrations, os.O_RDONLY | os.O_DIRECTORY)
        try:
            registry = Registry(registrations, self.lock)
            with self.lock.acquire():
                for index, application in enumerate(self.APPLICATIONS):
                    registry.register(application, deployment_uid=1001 + index,
                                      deployment_user=f"ship-{application}", runtime="docker",
                                      api_domain=f"{application}.example.com",
                                      image_repository=f"ghcr.io/example/{application}", probe=lambda port: True)
                    (self.layout.app_state / application).mkdir(mode=0o700)
                    self.initialize(application)
        finally:
            os.close(registrations)
        self.launcher, self.caddy, self.runtime = FakeLauncher(), FakeCaddy(), FakeRuntime()
        self.probes = FakeProbes(self.runtime)
        self.services = HostServices(self.layout, system=None, launcher=self.launcher, caddy=self.caddy,
                                     probes=self.probes, runtime_factory=lambda services, registration: self.runtime,
                                     lock_timeout=0.3)
        self.lines: list[str] = []

    # helpers ------------------------------------------------------------------
    def say(self, text):
        self.lines.append(text)

    def session(self, application="orders"):
        return AppSession(self.services, application)

    def initialize(self, application):
        descriptor = os.open(self.layout.app_state / application, os.O_RDONLY | os.O_DIRECTORY)
        try:
            ReleaseState(application, descriptor, self.lock).initialize()
        finally:
            os.close(descriptor)

    def deploy(self, application="orders", request_id="req-1", commit="a", image="b", sequence=1000):
        with self.session(application) as session, session.lock.acquire():
            return session.deployer().deploy({**self.record(application, request_id, commit, image, sequence),
                                              "request_id": request_id})

    def record(self, application, request_id="req-2", commit="c", image="c", sequence=2000) -> dict:
        """A release record for ``application``; the fixture's configuration is named for the first application."""
        value = record(request_id=request_id, commit=commit, image=image, sequence=sequence)
        value["configuration"] = dict(value["configuration"], name=application)
        return value

    def leave_journal(self, application="orders", request_id="req-2", commit="c", image="c", sequence=2000):
        """Interrupt a second transaction after the runtime phase, exactly as a killed worker leaves it."""
        with self.session(application) as session, session.lock.acquire():
            session.state.begin(request_id, self.record(application, request_id, commit, image, sequence))
            session.state.advance("runtime-changing")

    def status(self, application="orders"):
        with self.session(application) as session, session.lock.acquire():
            return session.deployer().status()

    def journal(self, application="orders") -> bool:
        return journal_open(self.services, application)

    def run_recovery(self, **kwargs) -> int:
        return recover_interrupted(self.services, self.say, **kwargs)

    # the no-op path -----------------------------------------------------------
    def test_a_boot_with_no_open_journal_recovers_nothing(self):
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(self.lines, ["shiplord: no interrupted transaction to recover"])
        self.assertEqual(self.runtime.calls, [])  # no runtime was touched
        self.assertEqual(self.caddy.reload_calls, 0)

    def test_nothing_is_created_for_an_application_that_was_never_deployed(self):
        (self.layout.app_state / "reports" / "state.json").unlink()
        (self.layout.app_state / "reports").rmdir()
        self.assertEqual(self.run_recovery(), 0)
        self.assertFalse((self.layout.app_state / "reports").exists())
        self.assertFalse((self.layout.jobs / "reports").exists())

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.deploy()
        self.leave_journal()
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(self.lines[-1], "shiplord: no interrupted transaction to recover")
        self.assertEqual(self.lines[-2], "shiplord: orders: recovered")

    # the recovery path --------------------------------------------------------
    def test_an_open_journal_is_recovered_without_a_person_running_recover(self):
        self.assertEqual(self.deploy().status, "succeeded")
        self.leave_journal()
        self.assertTrue(self.journal())
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(self.lines, ["shiplord: orders: recovered"])
        self.assertFalse(self.journal())  # the journal is cleared only after a verified recovery
        self.assertIsNone(self.status()["unfinished_transaction"])
        self.assertEqual(self.status()["current"]["commit"], "a" * 40)
        self.assertTrue(self.runtime.is_running(None, record(commit="a", image="b")))

    def test_a_first_release_that_never_finished_is_cleaned_up(self):
        self.leave_journal(request_id="req-1", commit="c", image="c", sequence=1000)
        self.assertEqual(self.run_recovery(), 0)
        self.assertFalse(self.journal())
        self.assertIsNone(self.status()["current"])
        self.assertIn(("stop",), self.runtime.calls)

    def test_only_the_application_with_an_open_journal_is_recovered(self):
        self.deploy()
        self.leave_journal()
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(self.lines, ["shiplord: orders: recovered"])
        self.assertFalse(self.journal("orders"))
        self.assertFalse(self.journal("reports"))
        self.assertEqual(self.status("reports")["generation"], 0)

    def test_an_application_whose_registration_vanished_is_left_alone(self):
        self.leave_journal()
        registrations = os.open(self.layout.registrations, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.lock.acquire():
                Registry(registrations, self.lock).unregister("orders")
        finally:
            os.close(registrations)
        self.assertEqual(self.run_recovery(), 0)
        self.assertTrue(self.journal())  # nothing ran, and nothing was deleted
        self.assertEqual(self.lines, ["shiplord: no interrupted transaction to recover"])

    # deferring to a live operation --------------------------------------------
    def test_a_live_job_defers_recovery_and_leaves_the_journal_alone(self):
        self.deploy()
        self.leave_journal()
        with self.session() as session, session.lock.acquire():
            session.jobs.create("job-1", "deploy", {})
        self.launcher.live.add("job-1")
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(self.lines, ["shiplord: orders: deferred, an operation is still running"])
        self.assertTrue(self.journal())

    def test_a_job_whose_worker_died_with_the_boot_does_not_block_recovery(self):
        self.deploy()
        self.leave_journal()
        with self.session() as session, session.lock.acquire():
            session.jobs.create("job-1", "deploy", {})
            session.jobs.start("job-1")  # the record says running; its transient unit died with the boot
        self.assertEqual(self.run_recovery(), 0)
        self.assertEqual(self.lines, ["shiplord: orders: recovered"])
        self.assertFalse(self.journal())

    def test_a_busy_host_lock_defers_recovery(self):
        self.deploy()
        self.leave_journal()
        calls = list(self.runtime.calls)
        descriptor = os.open(self.layout.var / "host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.run_recovery(lock_timeout=0.1), 0)
        self.assertEqual(self.lines, ["shiplord: orders: deferred, another operation holds the host lock"])
        self.assertTrue(self.journal())
        self.assertEqual(self.runtime.calls, calls)  # nothing was started or stopped under the other operation

    # failure is visible, and never silent -------------------------------------
    def test_a_recovery_that_cannot_finish_fails_the_unit_and_keeps_the_journal(self):
        self.deploy()
        self.leave_journal()
        self.runtime.running = set()
        self.runtime.start_errors[release_id(record(commit="a", image="b"))] = RuntimeFailure("start failed", "start")
        self.assertEqual(self.run_recovery(), 1)
        self.assertEqual(self.lines, ["shiplord: orders: recovery failed (recovery_failed)"])
        self.assertTrue(self.journal())  # only a verified recovery may clear it

    def test_one_failed_application_does_not_stop_the_others(self):
        self.deploy("orders")
        self.leave_journal("orders")
        self.leave_journal("reports", request_id="req-9", commit="d", image="d", sequence=3000)
        self.runtime.running = set()
        self.runtime.start_errors[release_id(record(commit="a", image="b"))] = RuntimeFailure("start failed", "start")
        self.assertEqual(self.run_recovery(), 1)
        self.assertEqual(self.lines, ["shiplord: orders: recovery failed (recovery_failed)",
                                      "shiplord: reports: recovered"])
        self.assertTrue(self.journal("orders"))
        self.assertFalse(self.journal("reports"))

    def test_an_unreadable_journal_is_reported_rather_than_skipped(self):
        path = self.layout.app_state / "orders" / "journal.json"
        path.write_text("{}\n")
        path.chmod(0o644)  # group- and other-readable state is refused, not guessed at
        self.assertEqual(self.run_recovery(), 1)
        self.assertEqual(self.lines, ["shiplord: orders: the transaction journal could not be read "
                                      "(could not read valid state)"])
        self.assertTrue(path.exists())  # nothing was deleted and no transaction was attempted
        self.assertEqual(self.runtime.calls, [])

    def test_an_invalid_journal_record_is_a_visible_failure(self):
        path = self.layout.app_state / "orders" / "journal.json"
        path.write_text("{}\n")
        path.chmod(0o600)
        self.assertEqual(self.run_recovery(), 1)
        self.assertEqual(self.lines, ["shiplord: orders: recovery failed (invalid transaction journal)"])
        self.assertTrue(path.exists())
        self.assertEqual(self.runtime.calls, [])

    def test_a_missing_registry_is_reported(self):
        for name in ("orders.json", "reports.json"):
            (self.layout.registrations / name).unlink()
        os.rmdir(self.layout.registrations)
        self.assertEqual(self.run_recovery(), 1)
        self.assertEqual(self.lines, ["shiplord: the application registry could not be read "
                                      "(the application registry is missing)"])

    def test_no_message_carries_a_host_path(self):
        self.leave_journal()
        path = self.layout.app_state / "orders" / "journal.json"
        path.write_text("{}\n")
        path.chmod(0o600)
        self.run_recovery()
        for line in self.lines:
            self.assertNotIn(self.temp.name, line)
            self.assertNotIn(str(self.layout.app_state), line)

    # the per-application entry point -----------------------------------------
    def test_recovering_one_application_reports_the_status_it_reached(self):
        self.assertEqual(recover_application(self.services, "orders", self.say), NOTHING)
        self.deploy()
        self.leave_journal()
        self.assertEqual(recover_application(self.services, "orders", self.say), RECOVERED)
        self.assertFalse(self.journal())

    def test_an_application_that_cannot_be_opened_is_a_failure(self):
        (self.layout.app_state / "orders" / "journal.json").write_text("{}\n")
        self.assertEqual(recover_application(self.services, "orders", self.say), FAILED)

    def test_the_registry_lists_every_application_in_name_order(self):
        self.assertEqual(registered_applications(self.services), ["orders", "reports"])

    def test_the_journal_check_is_read_only(self):
        self.assertFalse(self.journal())
        self.assertFalse((self.layout.jobs / "orders").exists())
        before = sorted(path.name for path in (self.layout.app_state / "orders").iterdir())
        self.assertFalse(self.journal())
        self.assertEqual(sorted(path.name for path in (self.layout.app_state / "orders").iterdir()), before)

    def test_the_interrupted_transaction_is_the_one_that_was_recorded(self):
        self.deploy()
        self.leave_journal()
        value = json.loads((self.layout.app_state / "orders" / "journal.json").read_text())
        self.assertEqual((value["request_id"], value["phase"]), ("req-2", "runtime-changing"))
        self.assertTrue(self.journal())


if __name__ == "__main__":
    unittest.main()
