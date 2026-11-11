"""Recover interrupted transactions at boot, without a person running ``recover``.

Bootstrap installs ``shiplord-boot-recovery.service``: a oneshot unit that runs once per
boot, after Docker and Caddy are up, and then exits. It looks for applications whose
release journal is still open and recovers each one with the same transaction code an
operator reaches through ``shiplord recover``.

It is safe to run at any time. It takes the host lock per application, defers when another
operation holds the lock or a job's worker is still alive, and does nothing when every
journal is closed. Every outcome is written to stdout, which the unit sends to the journal,
and a recovery that did not finish makes the unit fail so ``systemctl status`` shows it.
"""

from __future__ import annotations

import os

from .jobs import Launcher
from .registry import Registry
from .release_state import HostLock, StateError, _directory, _read
from .services import AppSession, HostServices

LOCK_TIMEOUT_SECONDS = 30  # a live operation gets this long to finish before recovery defers to it
NOTHING, RECOVERED, DEFERRED, FAILED = "nothing", "recovered", "deferred", "failed"


def _open_directory(path) -> int | None:
    """Open a Shiplord state directory, or return None when it does not exist."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:  # no path in the message: it reaches the journal, and paths are host detail
        raise StateError("a Shiplord state directory is unsafe") from None


def registered_applications(services: HostServices) -> list[str]:
    """Every registered application, in name order."""
    var = _open_directory(services.layout.var)
    registrations = _open_directory(services.layout.registrations)
    if var is None or registrations is None:
        if var is not None:
            os.close(var)
        raise StateError("the application registry is missing")
    try:
        lock = HostLock(var)
        try:
            return sorted(record["application_id"] for record in Registry(registrations, lock).list())
        finally:
            os.close(registrations)
    finally:
        os.close(var)


def journal_open(services: HostServices, application_id: str) -> bool:
    """Whether an application has an unfinished transaction journal; never creates anything."""
    directory = _open_directory(services.layout.app_state / application_id)
    if directory is None:
        return False  # a registered application that was never deployed has no state to recover
    try:
        _directory(directory)
        return _read(directory, "journal.json", missing_ok=True) is not None
    finally:
        os.close(directory)


def _live_job(session: AppSession) -> bool:
    """Whether a job for this application still has a worker.

    A record left behind by a restart is not live: its transient unit died with the boot, so its
    application is exactly what this unit exists to recover.
    """
    launcher: Launcher = session.services.launcher
    return any(launcher.is_active(session.application_id, request_id) for request_id in session.jobs.unfinished())


def _report(outcome, application_id: str, say) -> str:
    if outcome.ok:
        say(f"shiplord: {application_id}: {outcome.status}")
        return RECOVERED
    say(f"shiplord: {application_id}: recovery failed ({outcome.detail.get('error', outcome.status)})")
    return FAILED


def recover_application(services: HostServices, application_id: str, say,
                        lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> str:
    """Recover one application under the host lock; return NOTHING, RECOVERED, DEFERRED, or FAILED."""
    try:
        with AppSession(services, application_id) as session:
            with session.lock.acquire(timeout=lock_timeout):
                if session.state.read_journal() is None:
                    return NOTHING  # recovered while this unit waited: there is nothing left to do
                if _live_job(session):
                    say(f"shiplord: {application_id}: deferred, an operation is still running")
                    return DEFERRED
                outcome = session.deployer().recover()
    except StateError as error:
        if "host lock is busy" in str(error):
            say(f"shiplord: {application_id}: deferred, another operation holds the host lock")
            return DEFERRED
        say(f"shiplord: {application_id}: recovery failed ({error})")
        return FAILED
    except Exception as error:  # noqa: BLE001 - one application must not stop the others; the type names the cause
        say(f"shiplord: {application_id}: recovery failed ({type(error).__name__})")
        return FAILED
    return _report(outcome, application_id, say)


def recover_interrupted(services: HostServices, say, lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> int:
    """Recover every application with an open journal; return the unit's exit code."""
    try:
        applications = registered_applications(services)
    except StateError as error:
        say(f"shiplord: the application registry could not be read ({error})")
        return 1
    found = failed = 0
    for application_id in applications:
        try:
            open_journal = journal_open(services, application_id)
        except StateError as error:
            say(f"shiplord: {application_id}: the transaction journal could not be read ({error})")
            failed += 1
            continue
        if not open_journal:
            continue
        found += 1
        if recover_application(services, application_id, say, lock_timeout) == FAILED:
            failed += 1
    if not found and not failed:
        say("shiplord: no interrupted transaction to recover")
    return 1 if failed else 0


def journal_line(text: str) -> None:
    """One line for the journal, flushed as it is written: a timeout must not swallow the record."""
    print(text, flush=True)


def main() -> None:
    if os.geteuid() != 0:
        journal_line("shiplord: boot recovery must run as root")
        raise SystemExit(2)
    from .worker import production_services  # the same wiring a job uses, imported late to keep this module light
    raise SystemExit(recover_interrupted(production_services(), journal_line))


if __name__ == "__main__":
    main()
