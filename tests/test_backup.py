from __future__ import annotations

import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import REGISTRATION, FakeRuntime, record  # noqa: E402
from shiplord.backup import (BackupError, BackupInputError, BackupManager, _components, _failed, _record,  # noqa: E402
                             archive_name, extract_archive, inspect_archive, read_manifest, replace_data,
                             require_free_space, scan_data, validate_archive_name, verify_archive, write_archive)
from shiplord.layout import Layout  # noqa: E402
from shiplord.runtime import RuntimeFailure, release_id  # noqa: E402

STAMP = "20240101T000000Z"
NAME = "shiplord-backup-orders-20240101T000000Z.tar.gz"


class FakeState:
    """The two state reads a backup or restore makes; journal presence gates both."""

    def __init__(self, current: dict | None = None, journal: dict | None = None):
        self.current, self.journal = current, journal

    def read_state(self) -> dict:
        return {"current": self.current, "previous": None}

    def read_journal(self) -> dict | None:
        return self.journal


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.layout = Layout(self.root)
        self.data = self.root / "data" / "orders"
        self.data.mkdir(parents=True)
        self.out = self.root / "out"
        self.out.mkdir(mode=0o700)
        self.runtime = FakeRuntime()
        self.runtime.data_root_path = self.data
        self.state = FakeState()

    def archive(self, name: str = NAME, *, source: Path | None = None, created_at: str = STAMP):
        return write_archive(source or self.data, self.out, application_id="orders", runtime="docker",
                             name=name, created_at=created_at)

    def manager(self, *, clock=lambda: 1700000000) -> BackupManager:
        return BackupManager(registration=REGISTRATION, state=self.state, runtime=self.runtime, layout=self.layout,
                             clock=clock)


class ArchiveNameTests(unittest.TestCase):
    def test_a_name_is_one_safe_component(self):
        self.assertEqual(validate_archive_name(NAME), NAME)
        for bad in ("../x.tar.gz", "/etc/x.tar.gz", "a/b.tar.gz", "x.tar", "", None, "x" * 500 + ".tar.gz"):
            with self.subTest(bad=bad), self.assertRaises(BackupInputError):
                validate_archive_name(bad)

    def test_archive_names_are_timestamped_and_disambiguated(self):
        self.assertEqual(archive_name("orders", STAMP), NAME)
        self.assertEqual(archive_name("orders", STAMP, 1), "shiplord-backup-orders-20240101T000000Z-2.tar.gz")
        with self.assertRaises(ValueError):
            archive_name("../orders", STAMP)


class EntryNameTests(unittest.TestCase):
    def test_only_relative_components_without_escape_are_accepted(self):
        self.assertEqual(_components("a/b/c"), ["a", "b", "c"])
        for bad in ("/etc/passwd", "../x", "a/../b", "a//b", "./a", "a\\b", "a\x00b", "", None):
            with self.subTest(bad=bad), self.assertRaises(BackupError) as caught:
                _components(bad)
            self.assertEqual(caught.exception.kind, "unsafe_entry")


class WriteArchiveTests(BackupTestCase):
    def test_an_archive_is_written_private_and_can_be_read_back(self):
        (self.data / "state.db").write_bytes(b"sqlite")
        (self.data / "uploads").mkdir(mode=0o750)
        (self.data / "uploads" / "a.txt").write_text("hello")
        archive = self.archive()
        path = self.out / NAME
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.out / (NAME + ".json")).stat().st_mode), 0o600)
        self.assertEqual(archive, read_manifest(self.out / (NAME + ".json")))
        self.assertEqual((archive.entries, archive.bytes), (3, len(b"sqlite") + len(b"hello")))
        verify_archive(path, archive)
        self.assertEqual(inspect_archive(path), (archive.entries, archive.bytes))

    def test_a_symlink_in_the_data_is_refused_and_nothing_is_published(self):
        (self.data / "link").symlink_to("/etc/passwd")
        with self.assertRaises(BackupError) as caught:
            self.archive()
        self.assertEqual(caught.exception.kind, "unsupported_entry")
        self.assertEqual(os.listdir(self.out), [])

    def test_too_many_entries_are_refused(self):
        (self.data / "a").write_text("a")
        (self.data / "b").write_text("b")
        with patch("shiplord.backup.MAX_ENTRIES", 1):
            with self.assertRaises(BackupError) as caught:
                self.archive()
        self.assertEqual(caught.exception.kind, "too_large")
        self.assertEqual(os.listdir(self.out), [])

    def test_too_much_data_is_refused(self):
        (self.data / "a").write_text("abcd")
        with patch("shiplord.backup.MAX_DATA_BYTES", 3):
            with self.assertRaises(BackupError) as caught:
                self.archive()
        self.assertEqual(caught.exception.kind, "too_large")

    def test_free_space_is_checked_before_writing(self):
        with patch("shiplord.backup.shutil.disk_usage", lambda path: SimpleNamespace(free=0)):
            with self.assertRaises(BackupError) as caught:
                self.archive()
        self.assertEqual((caught.exception.kind, caught.exception.phase), ("insufficient_space", "prepare"))
        self.assertEqual(os.listdir(self.out), [])

    def test_an_archive_whose_record_cannot_be_written_is_removed(self):
        (self.data / "state.db").write_bytes(b"sqlite")
        with patch("shiplord.backup._write_private", side_effect=BackupError("no record", "archive", "write_failed")):
            with self.assertRaises(BackupError):
                self.archive()
        self.assertEqual(os.listdir(self.out), [])  # an archive without its record cannot be restored

    def test_a_backup_directory_that_is_not_private_is_tightened(self):
        directory = self.layout.backups / "orders"
        directory.mkdir(parents=True)
        directory.chmod(0o755)
        self.assertEqual(self.manager()._backup_directory(create=False), directory)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_a_symlinked_backup_directory_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.layout.backups).mkdir(parents=True)
        (self.layout.backups / "orders").symlink_to(elsewhere)
        with self.assertRaises(BackupError) as caught:
            self.manager()._backup_directory(create=False)
        self.assertEqual(caught.exception.phase, "prepare")


class VerifyArchiveTests(BackupTestCase):
    def setUp(self):
        super().setUp()
        (self.data / "state.db").write_bytes(b"sqlite")
        self.record = self.archive()
        self.path = self.out / NAME

    def test_a_changed_byte_is_a_checksum_mismatch(self):
        with self.path.open("r+b") as file:
            file.seek(20)
            file.write(b"\x00")
        with self.assertRaises(BackupError) as caught:
            verify_archive(self.path, self.record)
        self.assertEqual(caught.exception.kind, "checksum_mismatch")

    def test_a_truncated_archive_is_refused(self):
        with self.path.open("r+b") as file:
            file.truncate(self.path.stat().st_size - 1)
        with self.assertRaises(BackupError) as caught:
            verify_archive(self.path, self.record)
        self.assertEqual(caught.exception.kind, "truncated_archive")

    def test_a_record_that_does_not_describe_the_archive_is_refused(self):
        with self.assertRaises(BackupError) as caught:
            verify_archive(self.path, replace(self.record, entries=self.record.entries + 1))
        self.assertEqual(caught.exception.kind, "unreadable_archive")

    def test_a_world_readable_record_is_refused(self):
        manifest = self.out / (NAME + ".json")
        manifest.chmod(0o644)
        with self.assertRaises(BackupError) as caught:
            read_manifest(manifest)
        self.assertEqual(caught.exception.kind, "unsafe_backup")

    def test_a_symlinked_record_is_refused(self):
        manifest = self.out / (NAME + ".json")
        link = self.out / "link.json"
        link.symlink_to(manifest)
        with self.assertRaises(BackupError) as caught:
            read_manifest(link)
        self.assertEqual(caught.exception.kind, "missing_backup")

    def test_an_unusable_record_is_refused(self):
        manifest = self.out / (NAME + ".json")
        original = json.loads(manifest.read_text())
        for field, value in (("schema_version", 2), ("sha256", "x"), ("created_at", "yesterday"),
                             ("runtime", "chroot"), ("size", -1), ("extra", 1)):
            with self.subTest(field=field):
                manifest.write_text(json.dumps({**original, field: value}))
                manifest.chmod(0o600)
                with self.assertRaises(BackupError):
                    read_manifest(manifest)

    def test_a_record_is_only_built_from_the_exact_schema(self):
        value = {"schema_version": 1, "application_id": "orders", "runtime": "docker", "archive": NAME,
                 "sha256": "a" * 64, "size": 1, "entries": 1, "bytes": 1, "root_mode": 0o755, "root_uid": 0,
                 "root_gid": 0, "created_at": STAMP}
        self.assertEqual(_record(value).application_id, "orders")
        with self.assertRaises(BackupError):
            _record({**value, "unknown": True})


class ExtractTests(BackupTestCase):
    def test_files_and_directories_keep_their_modes(self):
        (self.data / "sub").mkdir(mode=0o750)
        (self.data / "sub" / "a.txt").write_text("hello")
        (self.data / "sub" / "a.txt").chmod(0o640)
        archive = self.archive()
        staging = self.root / "staging"
        self.assertEqual(extract_archive(self.out / NAME, staging, archive), (archive.entries, archive.bytes))
        self.assertEqual((staging / "sub" / "a.txt").read_text(), "hello")
        self.assertEqual(stat.S_IMODE((staging / "sub" / "a.txt").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((staging / "sub").stat().st_mode), 0o750)

    def _malicious(self, member: tarfile.TarInfo) -> Path:
        path = self.out / "evil.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            archive.addfile(member)
        return path

    def test_an_entry_that_escapes_the_root_is_refused(self):
        member = tarfile.TarInfo("../escape")
        member.size = 0
        path = self._malicious(member)
        with self.assertRaises(BackupError) as caught:
            extract_archive(path, self.root / "staging", replace(self.archive(), entries=1, bytes=0))
        self.assertEqual(caught.exception.kind, "unsafe_entry")
        self.assertFalse((self.root / "escape").exists())

    def test_a_link_entry_is_refused(self):
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        path = self._malicious(member)
        with self.assertRaises(BackupError) as caught:
            inspect_archive(path)
        self.assertEqual(caught.exception.kind, "unsafe_entry")
        with self.assertRaises(BackupError):
            extract_archive(path, self.root / "staging", replace(self.archive(), entries=1, bytes=0))

    def test_a_duplicate_entry_is_refused(self):
        first, second = tarfile.TarInfo("a.txt"), tarfile.TarInfo("a.txt")
        first.size = second.size = 0
        path = self.out / "evil.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            archive.addfile(first)
            archive.addfile(second)
        with self.assertRaises(BackupError) as caught:
            inspect_archive(path)
        self.assertEqual(caught.exception.kind, "unsafe_entry")
        with self.assertRaises(BackupError) as caught:
            extract_archive(path, self.root / "staging", replace(self.archive(), entries=2, bytes=0))
        self.assertEqual(caught.exception.kind, "unsafe_entry")


class ReplaceDataTests(BackupTestCase):
    def test_verified_data_replaces_the_live_directory(self):
        live = self.root / "live"
        live.mkdir()
        (live / "old").write_text("old")
        staging = self.root / "staging"
        staging.mkdir()
        (staging / "new").write_text("new")
        entries, total = scan_data(staging)
        record_value = replace(self.archive(), entries=entries, bytes=total)
        replace_data(live, staging, record_value)
        self.assertEqual(sorted(p.name for p in live.iterdir()), ["new"])
        self.assertFalse(staging.exists())
        self.assertEqual([p for p in self.root.iterdir() if p.name.startswith(".shiplord-")], [])

    def test_unverified_data_is_put_back(self):
        live = self.root / "live"
        live.mkdir()
        (live / "old").write_text("old")
        staging = self.root / "staging"
        staging.mkdir()
        (staging / "new").write_text("new")
        record_value = replace(self.archive(), entries=99, bytes=99)
        with patch("shiplord.backup.scan_data", lambda path: (0, 0)):
            with self.assertRaises(BackupError) as caught:
                replace_data(live, staging, record_value)
        self.assertEqual(caught.exception.kind, "restore_unverified")
        self.assertEqual(sorted(p.name for p in live.iterdir()), ["old"])
        self.assertEqual([p for p in self.root.iterdir() if p.name.startswith(".shiplord-")], [])


class BackupManagerTests(BackupTestCase):
    def test_a_backup_stops_the_application_and_starts_it_again(self):
        current = record()
        self.state = FakeState(current=current)
        self.runtime.running = {release_id(current)}
        (self.data / "state.db").write_bytes(b"sqlite")
        outcome = self.manager().backup()
        self.assertEqual((outcome.status, outcome.exit_code), ("succeeded", 0))
        self.assertTrue(outcome.detail["verified"])
        self.assertEqual(self.runtime.names(), ["data_root", "stop", "start"])
        self.assertEqual(outcome.detail["application"], {"restarted": True, "running": True})
        self.assertEqual(read_manifest(self.layout.backups / "orders" / (outcome.detail["archive"] + ".json")).bytes,
                         len(b"sqlite"))

    def test_a_stopped_application_is_not_started(self):
        self.state = FakeState(current=record())  # recorded but not running
        outcome = self.manager().backup()
        self.assertEqual((outcome.status, outcome.detail["application"]), ("succeeded", {"restarted": False,
                                                                                         "running": False}))
        self.assertEqual(self.runtime.names(), ["data_root", "stop"])

    def test_missing_data_fails_before_the_application_is_stopped(self):
        self.runtime.data_root_path = None
        outcome = self.manager().backup()
        self.assertEqual((outcome.status, outcome.exit_code, outcome.detail["error"]), ("failed", 1, "no_data"))
        self.assertEqual(self.runtime.names(), ["data_root"])

    def test_a_failed_restart_requires_intervention(self):
        current = record()
        self.state = FakeState(current=current)
        self.runtime.running = {release_id(current)}
        self.runtime.start_errors[release_id(current)] = RuntimeFailure("boom", "start")
        outcome = self.manager().backup()
        self.assertEqual((outcome.status, outcome.exit_code), ("intervention_required", 6))
        self.assertTrue(outcome.detail["verified"])  # the archive is safe even though the app did not return

    def test_an_unfinished_transaction_blocks_a_backup(self):
        self.state = FakeState(journal={"phase": "prepared"})
        outcome = self.manager().backup()
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "recovery_required"))
        self.assertEqual(self.runtime.calls, [])

    def test_a_full_disk_fails_before_the_application_is_stopped(self):
        (self.data / "state.db").write_bytes(b"sqlite")
        with patch("shiplord.backup.shutil.disk_usage", lambda path: SimpleNamespace(free=0)):
            outcome = self.manager().backup()
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "insufficient_space"))
        self.assertEqual(self.runtime.names(), ["data_root"])  # the application never stopped

    def test_a_failed_swap_starts_the_application_again(self):
        (self.data / "state.db").write_bytes(b"first")
        name = self.manager().backup().detail["archive"]
        current = record()
        self.state = FakeState(current=current)
        self.runtime.running = {release_id(current)}
        with patch("shiplord.backup.replace_data", side_effect=BackupError("no swap", "swap", "swap_failed")):
            outcome = self.manager().restore({"archive": name})
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "swap_failed"))
        self.assertIn("start", self.runtime.names())
        self.assertEqual(outcome.detail["application"], {"restarted": True, "running": True})

    def test_an_unexpected_host_error_does_not_echo_a_path(self):
        outcome = _failed(OSError("/var/lib/shiplord/secret: permission denied"))
        self.assertEqual((outcome.status, outcome.exit_code), ("failed", 1))
        self.assertNotIn("/var/lib/shiplord", json.dumps(outcome.detail))
        self.assertEqual(outcome.detail["message"], "the operation failed; nothing was changed")

    def test_restore_by_name_replaces_the_data_and_restarts_the_application(self):
        (self.data / "state.db").write_bytes(b"first")
        name = self.manager().backup().detail["archive"]
        (self.data / "state.db").write_bytes(b"second")
        (self.data / "extra").write_text("x")
        current = record()
        self.state = FakeState(current=current)
        self.runtime.running = {release_id(current)}
        outcome = self.manager().restore({"archive": name})
        self.assertEqual((outcome.status, outcome.exit_code), ("succeeded", 0))
        self.assertEqual((self.data / "state.db").read_bytes(), b"first")
        self.assertFalse((self.data / "extra").exists())
        self.assertIn("start", self.runtime.names())

    def test_restore_latest_picks_the_newest_backup(self):
        (self.data / "state.db").write_bytes(b"first")
        first = self.manager(clock=lambda: 1700000000).backup().detail["archive"]
        (self.data / "later").write_text("later")
        second = self.manager(clock=lambda: 1700000100).backup().detail["archive"]
        self.assertNotEqual(first, second)
        (self.data / "later").unlink()
        outcome = self.manager().restore({"latest": True})
        self.assertEqual((outcome.status, outcome.detail["archive"]), ("succeeded", second))
        self.assertTrue((self.data / "later").exists())

    def test_restore_latest_orders_two_backups_taken_in_the_same_second(self):
        (self.data / "state.db").write_bytes(b"first")
        first = self.manager(clock=lambda: 1700000000).backup().detail["archive"]
        (self.data / "later").write_text("later")
        second = self.manager(clock=lambda: 1700000000).backup().detail["archive"]
        self.assertEqual(second, first.replace(".tar.gz", "-2.tar.gz"))
        (self.data / "later").unlink()
        outcome = self.manager().restore({"latest": True})
        self.assertEqual((outcome.status, outcome.detail["archive"]), ("succeeded", second))

    def test_a_corrupt_archive_is_refused_and_the_live_data_is_untouched(self):
        (self.data / "state.db").write_bytes(b"first")
        name = self.manager().backup().detail["archive"]
        self.runtime.calls.clear()
        archive_path = self.layout.backups / "orders" / name
        with archive_path.open("r+b") as file:
            file.seek(20)
            file.write(b"\x00")
        (self.data / "state.db").write_bytes(b"live")
        outcome = self.manager().restore({"archive": name})
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "checksum_mismatch"))
        self.assertEqual((self.data / "state.db").read_bytes(), b"live")
        self.assertNotIn("stop", self.runtime.names())  # verification comes before the application is touched

    def test_an_archive_for_another_application_is_rejected(self):
        self.manager().backup()
        outcome = self.manager().restore({"archive": "shiplord-backup-billing-20240101T000000Z.tar.gz"})
        self.assertEqual((outcome.status, outcome.exit_code), ("rejected", 2))
        self.assertEqual(outcome.detail["error"], "wrong_application")

    def test_an_archive_for_another_runtime_is_refused(self):
        name = self.manager().backup().detail["archive"]
        manifest = self.layout.backups / "orders" / (name + ".json")
        value = json.loads(manifest.read_text())
        value["runtime"] = "native"
        manifest.write_text(json.dumps(value))
        manifest.chmod(0o600)
        outcome = self.manager().restore({"archive": name})
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "wrong_runtime"))

    def test_restore_without_a_backup_is_refused(self):
        (self.layout.backups / "orders").mkdir(parents=True, mode=0o700)
        outcome = self.manager().restore({"latest": True})
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "no_backup"))

    def test_restore_latest_with_no_backup_directory_at_all_says_there_is_no_backup(self):
        outcome = self.manager().restore({"latest": True})
        self.assertEqual((outcome.status, outcome.detail["error"]), ("failed", "no_backup"))
        self.assertIn("shiplord backup", outcome.detail["message"])

    def test_an_unreadable_record_is_not_offered_as_the_latest_backup(self):
        (self.data / "state.db").write_bytes(b"first")
        first = self.manager(clock=lambda: 1700000000).backup().detail["archive"]
        (self.data / "later").write_text("later")
        second = self.manager(clock=lambda: 1700000100).backup().detail["archive"]
        (self.layout.backups / "orders" / (second + ".json")).chmod(0o644)  # not private: it cannot be trusted
        outcome = self.manager().restore({"latest": True})
        self.assertEqual((outcome.status, outcome.detail["archive"]), ("succeeded", first))
        self.assertFalse((self.data / "later").exists())


if __name__ == "__main__":
    unittest.main()
