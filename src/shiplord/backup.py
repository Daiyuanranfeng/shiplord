"""Verified backup and restore of an application's persistent data.

A backup is only a backup once it can be read back: the archive is written to a private
temporary file, synced, renamed into place, reopened, and compared with the data it was
made from. A restore is only a restore once it is verified: the record, the checksum, and
every entry are validated, the contents are extracted beside the live data and counted,
and only then is the live data replaced. The previous data is kept until the replacement
has been verified, and put back if it cannot be.

The application is stopped while its data is copied and while it is replaced, so a copy
is consistent (a live SQLite file is not a backup). It is started again afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .deploy import Outcome
from .layout import Layout
from .manifest import ManifestError, validate_app_name
from .protocol import _invalid_constant, _unique_keys
from .release_state import ReleaseState, StateError
from .runtime import Runtime, RuntimeFailure

ARCHIVE_PREFIX = "shiplord-backup-"
ARCHIVE_SUFFIX = ".tar.gz"
MANIFEST_SUFFIX = ".json"
SCHEMA_VERSION = 1
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
MIN_FREE_BYTES = 64 * 1024 ** 2  # a nearly full disk is refused even when the data is small
MAX_DATA_BYTES = 8 * 1024 ** 3
MAX_ENTRIES = 200_000
MAX_MANIFEST_BYTES = 65536
MAX_NAME_BYTES = 4096
MAX_ARCHIVES_LISTED = 500
MAX_ATTEMPTS = 100
MAX_DEPTH = 256
MODE_MASK = 0o777
_RUNTIMES = ("docker", "native")
_STAMP = re.compile(r"\d{8}T\d{6}Z\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.tar\.gz\Z")
_MANIFEST_KEYS = {"schema_version", "application_id", "runtime", "archive", "sha256", "size", "entries", "bytes",
                  "root_mode", "root_uid", "root_gid", "created_at"}


class BackupError(ValueError):
    """A backup or restore cannot proceed; ``phase`` names the step and ``kind`` classifies it."""

    def __init__(self, message: str, phase: str, kind: str = "backup_failed", *, intervention_required: bool = False):
        super().__init__(message)
        self.phase = phase
        self.kind = kind
        self.intervention_required = intervention_required


class BackupInputError(BackupError):
    """The request itself is unusable (exit code 2); nothing on the host was examined."""


@dataclass(frozen=True)
class Archive:
    """What a backup record says about one archive."""

    application_id: str
    runtime: str
    archive: str
    sha256: str
    size: int
    entries: int
    bytes: int
    root_mode: int
    root_uid: int
    root_gid: int
    created_at: str


# names and records ---------------------------------------------------------
def validate_archive_name(name: object) -> str:
    """A backup file name is one safe path component; the application prefix is checked separately."""
    if not isinstance(name, str) or len(name.encode()) > MAX_NAME_BYTES or not _ARCHIVE_NAME.fullmatch(name):
        raise BackupInputError("the archive name is not a backup file name; nothing was changed", "restore",
                               "invalid_archive")
    return name


def archive_name(application_id: str, stamp: str, attempt: int = 0) -> str:
    suffix = "" if attempt == 0 else f"-{attempt + 1}"
    return f"{ARCHIVE_PREFIX}{validate_app_name(application_id)}-{stamp}{suffix}{ARCHIVE_SUFFIX}"


def _name_sequence(name: str) -> int:
    """The disambiguating suffix of an archive name, so two backups in the same second still order correctly."""
    match = re.search(r"-(\d+)\.tar\.gz\Z", name)
    return int(match.group(1)) if match else 0


def _record(value: object) -> Archive:
    if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
        raise BackupError("the backup record is not usable; nothing was changed", "verify", "invalid_record")
    if value["schema_version"] != SCHEMA_VERSION:
        raise BackupError("the backup record was written by an incompatible version", "verify", "invalid_record")
    try:
        application_id = validate_app_name(value["application_id"])
        archive = validate_archive_name(value["archive"])
    except (ManifestError, BackupError):
        raise BackupError("the backup record is not usable; nothing was changed", "verify", "invalid_record") from None
    if value["runtime"] not in _RUNTIMES:
        raise BackupError("the backup record names an unknown runtime", "verify", "invalid_record")
    for field in ("size", "entries", "bytes", "root_mode", "root_uid", "root_gid"):
        if type(value[field]) is not int or value[field] < 0:
            raise BackupError("the backup record is not usable; nothing was changed", "verify", "invalid_record")
    if (value["size"] > MAX_DATA_BYTES * 2 or value["bytes"] > MAX_DATA_BYTES or value["entries"] > MAX_ENTRIES
            or value["root_mode"] > MODE_MASK or value["root_uid"] > 2 ** 32 - 1 or value["root_gid"] > 2 ** 32 - 1):
        raise BackupError("the backup record is outside the limits this helper can restore", "verify", "invalid_record")
    if not isinstance(value["sha256"], str) or not _SHA256.fullmatch(value["sha256"]):
        raise BackupError("the backup record has no usable checksum", "verify", "invalid_record")
    if not isinstance(value["created_at"], str) or not _STAMP.fullmatch(value["created_at"]):
        raise BackupError("the backup record has no usable timestamp", "verify", "invalid_record")
    return Archive(application_id=application_id, runtime=value["runtime"], archive=archive,
                   sha256=value["sha256"], size=value["size"], entries=value["entries"], bytes=value["bytes"],
                   root_mode=value["root_mode"], root_uid=value["root_uid"], root_gid=value["root_gid"],
                   created_at=value["created_at"])


def _read_private(path: Path, limit: int) -> bytes:
    """Read a root-only regular file without following a link; refuse anything a secret may not be."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise BackupError("the backup file is missing or cannot be read; nothing was changed", "verify",
                          "missing_backup") from None
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > limit):
            raise BackupError("the backup file is not a private regular file; nothing was changed", "verify",
                              "unsafe_backup")
        data = file.read(limit + 1)
    if len(data) > limit:
        raise BackupError("the backup file is too large; nothing was changed", "verify", "unsafe_backup")
    return data


def read_manifest(path: Path) -> Archive:
    data = _read_private(path, MAX_MANIFEST_BYTES)
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise BackupError("the backup record is not readable; nothing was changed", "verify", "invalid_record") from None
    return _record(value)


def _write_private(directory: Path, name: str, data: bytes, *, replace: bool) -> None:
    temporary = directory / (".tmp-" + secrets.token_hex(16))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if replace:
            os.replace(temporary, directory / name)
        else:
            os.link(temporary, directory / name, follow_symlinks=False)
    except OSError:
        raise BackupError("the backup record could not be written; nothing was changed", "archive",
                          "write_failed") from None
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    _sync_directory(directory)


def _sync_directory(directory: Path, *, message: str = "the directory could not be synced; the result is not confirmed",
                    phase: str = "archive") -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise BackupError(message, phase, "write_failed") from None


def digest_file(path: Path) -> tuple[str, int]:
    """The SHA-256 and size of a private regular file, read without following a link."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise BackupError("the backup file is missing or cannot be read; nothing was changed", "verify",
                          "missing_backup") from None
    digest, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise BackupError("the backup file is not a private regular file; nothing was changed", "verify",
                              "unsafe_backup")
        while chunk := file.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# the data tree -------------------------------------------------------------
def _open_directory(path: Path | None = None, *, parent: int | None = None, name: str | None = None) -> int:
    """Open a directory without following a link; a directory swapped for a link cannot redirect the walk."""
    try:
        if parent is None:
            return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        raise BackupError("the application data could not be read; nothing was changed", "archive",
                          "unreadable_data") from None


def _walk_directory(prefix: str, descriptor: int, depth: int) -> Iterator[tuple[str, int, str, os.stat_result]]:
    """Yield ``(relative name, parent descriptor, name, stat)`` for one directory, then recurse into its children."""
    if depth > MAX_DEPTH:
        raise BackupError("the application data is nested too deeply for this backup; nothing was changed", "archive",
                          "too_large")
    try:
        with os.scandir(descriptor) as listing:
            names = sorted(entry.name for entry in listing)
    except OSError:
        raise BackupError("the application data could not be read; nothing was changed", "archive",
                          "unreadable_data") from None
    children = []
    for name in names:
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            raise BackupError("the application data could not be read; nothing was changed", "archive",
                              "unreadable_data") from None
        relative = prefix + name
        yield relative, descriptor, name, info
        if stat.S_ISDIR(info.st_mode):
            children.append((relative + "/", name))
    for relative, name in children:
        child = _open_directory(parent=descriptor, name=name)
        try:
            yield from _walk_directory(relative, child, depth + 1)
        finally:
            os.close(child)


def _walk(root: Path) -> Iterator[tuple[str, int, str, os.stat_result]]:
    """Every entry below ``root``, parents before children, names sorted; a link is never followed."""
    descriptor = _open_directory(root)
    try:
        yield from _walk_directory("", descriptor, 0)
    finally:
        os.close(descriptor)


def scan_data(root: Path) -> tuple[int, int]:
    """The entry count and byte total of a data tree; links and special files are refused."""
    entries = total = 0
    for _relative, _descriptor, _name, info in _walk(root):
        entries += 1
        if entries > MAX_ENTRIES:
            raise BackupError(f"the application data has more than {MAX_ENTRIES} entries, which this backup does not "
                              "support; nothing was changed", "archive", "too_large")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("the application data contains a symbolic link or special file, which this backup "
                              "cannot restore safely; nothing was changed", "archive", "unsupported_entry")
        total += info.st_size
        if total > MAX_DATA_BYTES:
            raise BackupError(f"the application data is larger than the {MAX_DATA_BYTES // 2 ** 30} GiB backup limit; "
                              "nothing was changed", "archive", "too_large")
    return entries, total


def require_free_space(directory: Path, needed: int) -> None:
    """Refuse before writing rather than filling the disk halfway through."""
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        raise BackupError("the free disk space could not be measured; nothing was changed", "prepare",
                          "space_unknown") from None
    required = max(MIN_FREE_BYTES, needed + max(1 << 20, needed // 100))
    if free < required:
        raise BackupError(f"not enough free disk space ({free // 2 ** 20} MiB free, {required // 2 ** 20} MiB "
                          "needed); nothing was changed", "prepare", "insufficient_space")


def _add_tree(archive: tarfile.TarFile, root: Path) -> None:
    entries = 0
    for relative, descriptor, name, info in _walk(root):
        entries += 1
        if entries > MAX_ENTRIES:
            raise BackupError(f"the application data has more than {MAX_ENTRIES} entries, which this backup does not "
                              "support; nothing was changed", "archive", "too_large")
        entry = tarfile.TarInfo(relative)
        entry.mode = stat.S_IMODE(info.st_mode) & MODE_MASK
        entry.uid, entry.gid = info.st_uid, info.st_gid
        entry.mtime = int(info.st_mtime)
        if stat.S_ISDIR(info.st_mode):
            entry.type = tarfile.DIRTYPE
            archive.addfile(entry)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise BackupError("the application data contains a symbolic link or special file, which this backup "
                              "cannot restore safely; nothing was changed", "archive", "unsupported_entry")
        entry.type = tarfile.REGTYPE
        entry.size = info.st_size
        try:
            file_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        except OSError:
            raise BackupError("the application data changed while it was being archived; nothing was changed",
                              "archive", "unreadable_data") from None
        with os.fdopen(file_descriptor, "rb") as data:
            opened = os.fstat(data.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size != info.st_size:
                raise BackupError("the application data changed while it was being archived; nothing was changed",
                                  "archive", "unreadable_data")
            archive.addfile(entry, data)


# archive validation --------------------------------------------------------
def _components(name: object) -> list[str]:
    """Split a member name, refusing anything that could escape the extraction root."""
    if not isinstance(name, str) or not name or len(name.encode()) > MAX_NAME_BYTES:
        raise BackupError("the archive contains an entry with an unusable name", "verify", "unsafe_entry")
    if name.startswith("/") or "\x00" in name or "\\" in name:
        raise BackupError("the archive contains an absolute or unusable path", "verify", "unsafe_entry")
    components = name.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise BackupError("the archive contains a path that leaves the extraction root", "verify", "unsafe_entry")
    return components


def _inspect(archive: tarfile.TarFile) -> tuple[int, int]:
    entries = total = 0
    seen: set[str] = set()
    for member in archive:
        _components(member.name)
        if member.name in seen:
            raise BackupError("the archive contains a duplicate entry", "verify", "unsafe_entry")
        seen.add(member.name)
        if not (member.isreg() or member.isdir()):
            raise BackupError("the archive contains a link or special file, which a restore refuses", "verify",
                              "unsafe_entry")
        entries += 1
        if entries > MAX_ENTRIES:
            raise BackupError("the archive contains more entries than a restore accepts", "verify", "too_large")
        if member.isreg():
            if member.size < 0:
                raise BackupError("the archive contains an entry of unknown size", "verify", "unsafe_entry")
            total += member.size
            if total > MAX_DATA_BYTES:
                raise BackupError("the archive is larger than a restore accepts", "verify", "too_large")
    return entries, total


def inspect_archive(path: Path) -> tuple[int, int]:
    """The entry count and byte total of an archive; every entry is validated."""
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            return _inspect(archive)
    except (tarfile.TarError, OSError, EOFError):
        raise BackupError("the archive could not be read; nothing was changed", "verify", "unreadable_archive") from None


def verify_archive(path: Path, record: Archive) -> None:
    """Refuse a corrupt, truncated, or substituted archive before any live data is touched."""
    digest, size = digest_file(path)
    if size != record.size:
        raise BackupError("the archive is not the size its record says; nothing was changed", "verify",
                          "truncated_archive")
    if digest != record.sha256:
        raise BackupError("the archive does not match its recorded checksum; nothing was changed", "verify",
                          "checksum_mismatch")
    if inspect_archive(path) != (record.entries, record.bytes):
        raise BackupError("the archive does not contain the recorded contents; nothing was changed", "verify",
                          "unreadable_archive")


def extract_archive(path: Path, staging: Path, record: Archive) -> tuple[int, int]:
    """Write a verified archive into a fresh directory; refuse anything unsafe, and count what was written."""
    try:
        os.mkdir(staging, 0o700)
    except OSError:
        raise BackupError("the restore staging directory could not be created; nothing was changed", "extract",
                          "write_failed") from None
    directories: list[tuple[Path, int, int, int]] = []
    entries = total = 0
    seen: set[str] = set()
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                components = _components(member.name)
                if member.name in seen:
                    raise BackupError("the archive contains a duplicate entry", "extract", "unsafe_entry")
                seen.add(member.name)
                if not (member.isreg() or member.isdir()):
                    raise BackupError("the archive contains a link or special file, which a restore refuses",
                                      "extract", "unsafe_entry")
                entries += 1
                if entries > MAX_ENTRIES:
                    raise BackupError("the archive contains more entries than a restore accepts", "extract", "too_large")
                target = staging.joinpath(*components)
                mode = member.mode & MODE_MASK
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    directories.append((target, mode, member.uid, member.gid))
                    continue
                if member.size < 0 or member.size > MAX_DATA_BYTES:
                    raise BackupError("the archive contains an entry of an unusable size", "extract", "unsafe_entry")
                total += member.size
                if total > MAX_DATA_BYTES:
                    raise BackupError("the archive is larger than a restore accepts", "extract", "too_large")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _extract_file(archive, member, target)
                _apply(target, mode, member.uid, member.gid)
    except (tarfile.TarError, EOFError):
        raise BackupError("the archive could not be read to the end; nothing was changed", "extract",
                          "truncated_archive") from None
    except OSError:
        raise BackupError("the archive could not be extracted; nothing was changed", "extract", "write_failed") from None
    for directory, mode, uid, gid in reversed(directories):  # modes last: a read-only directory still took its children
        _apply(directory, mode, uid, gid)
    _apply(staging, record.root_mode, record.root_uid, record.root_gid)
    if (entries, total) != (record.entries, record.bytes):
        raise BackupError("the archive does not contain the recorded contents; nothing was changed", "extract",
                          "unreadable_archive")
    return entries, total


def _extract_file(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise BackupError("the archive entry could not be read", "extract", "unreadable_archive")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    written = 0
    with os.fdopen(descriptor, "wb") as file, source:
        while True:
            chunk = source.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > member.size:
                raise BackupError("the archive entry is longer than its header says", "extract", "truncated_archive")
            file.write(chunk)
        if written != member.size:
            raise BackupError("the archive entry is shorter than its header says", "extract", "truncated_archive")
        file.flush()
        os.fsync(file.fileno())


def _apply(path: Path, mode: int, uid: int, gid: int) -> None:
    try:
        os.chmod(path, mode)
        if os.geteuid() == 0:
            os.chown(path, uid, gid, follow_symlinks=False)
    except OSError:
        raise BackupError("the restored permissions could not be applied; nothing was changed", "extract",
                          "write_failed") from None


def write_archive(source: Path, directory: Path, *, application_id: str, runtime: str, name: str,
                  created_at: str) -> Archive:
    """Write a timestamped archive of ``source``, then reopen it and prove it holds that data."""
    entries, total = scan_data(source)
    require_free_space(directory, total)
    try:
        root = os.lstat(source)
    except OSError:
        raise BackupError("the application data could not be read; nothing was changed", "archive",
                          "unreadable_data") from None
    if not stat.S_ISDIR(root.st_mode):
        raise BackupError("the application data is not a directory; nothing was changed", "archive", "unreadable_data")
    final = directory / name
    temporary = directory / (".tmp-" + secrets.token_hex(16))
    published = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as file:
                with tarfile.open(fileobj=file, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                    _add_tree(archive, source)
                file.flush()
                os.fsync(file.fileno())
        except (tarfile.TarError, OSError):
            raise BackupError("the archive could not be written; nothing was changed", "archive",
                              "write_failed") from None
        try:
            os.replace(temporary, final)
            published = True
        except OSError:
            raise BackupError("the archive could not be published; nothing was changed", "archive",
                              "write_failed") from None
        _sync_directory(directory)
        digest, size = digest_file(final)
        if inspect_archive(final) != (entries, total):
            raise BackupError("the archive could not be read back as the data it was made from; it was removed",
                              "archive", "unreadable_archive")
    except BaseException:
        for path in (temporary, final if published else None):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        raise
    record = Archive(application_id=validate_app_name(application_id), runtime=runtime, archive=name,
                     sha256=digest, size=size, entries=entries, bytes=total,
                     root_mode=stat.S_IMODE(root.st_mode) & MODE_MASK, root_uid=root.st_uid, root_gid=root.st_gid,
                     created_at=created_at)
    data = (json.dumps(_manifest(record), sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        _write_private(directory, name + MANIFEST_SUFFIX, data, replace=False)
    except BaseException:
        # An archive without its record cannot be restored; do not leave an unusable file behind.
        try:
            os.unlink(final)
        except OSError:
            pass
        raise
    return record


def _manifest(record: Archive) -> dict:
    return {"schema_version": SCHEMA_VERSION, "application_id": record.application_id, "runtime": record.runtime,
            "archive": record.archive, "sha256": record.sha256, "size": record.size, "entries": record.entries,
            "bytes": record.bytes, "root_mode": record.root_mode, "root_uid": record.root_uid,
            "root_gid": record.root_gid, "created_at": record.created_at}


def replace_data(data_root: Path, staging: Path, record: Archive) -> None:
    """Swap verified staging data into place; the previous data is kept until the swap is verified."""
    parent = data_root.parent
    previous = parent / (".shiplord-previous-" + secrets.token_hex(8))
    try:
        os.rename(data_root, previous)
    except OSError:
        raise BackupError("the live data could not be set aside; nothing was changed", "swap", "swap_failed") from None
    try:
        os.rename(staging, data_root)
    except OSError:
        try:
            os.rename(previous, data_root)
        except OSError:
            raise BackupError("the restored data could not be moved into place and the previous data could not be "
                              "put back; manual intervention is required", "swap", "swap_failed",
                              intervention_required=True) from None
        raise BackupError("the restored data could not be moved into place; the previous data is intact", "swap",
                          "swap_failed") from None
    try:
        if scan_data(data_root) != (record.entries, record.bytes):
            raise BackupError("the restored data does not match the archive", "verify", "restore_unverified")
    except (BackupError, OSError) as failure:
        failed = parent / (".shiplord-failed-" + secrets.token_hex(8))
        try:
            os.rename(data_root, failed)
            os.rename(previous, data_root)
        except OSError:
            raise BackupError("the restored data could not be verified and the previous data could not be put back; "
                              "manual intervention is required", "verify", "restore_unverified",
                              intervention_required=True) from None
        shutil.rmtree(failed, ignore_errors=True)
        raise BackupError("the restored data could not be verified; the previous data was put back", "verify",
                          "restore_unverified") from None
    shutil.rmtree(previous, ignore_errors=True)
    _sync_directory(parent, message="the restored data is in place but could not be synced to disk; a crash may lose "
                                    "it", phase="swap")


class BackupManager:
    """Backup and restore for one registered application. The caller holds the host lock."""

    def __init__(self, *, registration: dict, state: ReleaseState, runtime: Runtime, layout: Layout,
                 progress: Callable[[str], None] = lambda phase: None, clock: Callable[[], float] = time.time):
        self.registration, self.state, self.runtime, self.layout = registration, state, runtime, layout
        self.application_id = validate_app_name(registration["application_id"])
        self.progress, self.clock = progress, clock

    # locations -------------------------------------------------------------
    def _backup_directory(self, *, create: bool) -> Path:
        root = self.layout.backups
        directory = root / self.application_id
        try:
            if create:
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                directory.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise BackupError("the backup directory is not usable; nothing was changed", "prepare",
                              "write_failed") from None
        for path in (root, directory):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                if create:
                    raise BackupError("the backup directory is not usable; nothing was changed", "prepare",
                                      "write_failed") from None
                continue  # nothing has been backed up yet; the caller reports that
            except OSError:
                raise BackupError("the backup directory is not usable; nothing was changed", "prepare",
                                  "write_failed") from None
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise BackupError("the backup directory is not usable; nothing was changed", "prepare",
                                  "write_failed")
            if stat.S_IMODE(info.st_mode) & 0o077:  # an archive is secret: never leave it world-readable
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    raise BackupError("the backup directory is not private; nothing was changed", "prepare",
                                      "write_failed") from None
        return directory

    def _next_name(self, directory: Path, stamp: str) -> str:
        for attempt in range(MAX_ATTEMPTS):
            name = archive_name(self.application_id, stamp, attempt)
            if not (directory / name).exists() and not (directory / (name + MANIFEST_SUFFIX)).exists():
                return name
        raise BackupError("too many backups were taken in the same second; try again", "prepare", "write_failed")

    def _locate(self, request: dict) -> tuple[Archive, Path]:
        directory = self._backup_directory(create=False)
        if request.get("latest") is True:
            record = self._latest(directory)
        else:
            name = validate_archive_name(request.get("archive"))
            if not name.startswith(f"{ARCHIVE_PREFIX}{self.application_id}-"):
                raise BackupInputError("this archive was not made for this application; nothing was changed",
                                       "restore", "wrong_application")
            record = read_manifest(directory / (name + MANIFEST_SUFFIX))
            if record.archive != name:
                raise BackupError("the backup record does not describe this archive; nothing was changed", "verify",
                                  "invalid_record")
        if record.application_id != self.application_id:
            raise BackupError("this archive belongs to a different application; nothing was changed", "verify",
                              "wrong_application")
        if record.runtime != self.registration["runtime"]:
            raise BackupError("this archive was made for a different runtime; nothing was changed", "verify",
                              "wrong_runtime")
        return record, directory / record.archive

    def _latest(self, directory: Path) -> Archive:
        try:
            names = sorted(name for name in os.listdir(directory) if name.endswith(MANIFEST_SUFFIX))
        except FileNotFoundError:
            names = []  # nothing has been backed up yet
        except OSError:
            raise BackupError("the backup directory could not be read; nothing was changed", "verify",
                              "missing_backup") from None
        found: list[Archive] = []
        for name in names[:MAX_ARCHIVES_LISTED]:
            try:
                record = read_manifest(directory / name)
            except BackupError:
                continue  # an unreadable record is not a restorable backup
            if record.application_id == self.application_id:
                found.append(record)
        if not found:
            raise BackupError("no backup of this application exists; run `shiplord backup` first", "verify",
                              "no_backup")
        return max(found, key=lambda record: (record.created_at, _name_sequence(record.archive)))

    # the application -------------------------------------------------------
    def _quiesce(self) -> tuple[dict | None, bool]:
        current = self.state.read_state()["current"]
        running = current is not None and self.runtime.is_running(self.registration, current)
        self.progress("stopping")
        self.runtime.stop(self.registration)
        return current, running

    def _resume(self, current: dict | None, was_running: bool) -> dict:
        if current is None or not was_running:
            return {"restarted": False, "running": False}
        self.progress("starting")
        try:
            self.runtime.start(self.registration, current)
            return {"restarted": True, "running": self.runtime.is_running(self.registration, current)}
        except RuntimeFailure as failure:
            return {"restarted": True, "running": False, "error": str(failure)}

    def _gate(self) -> None:
        if self.state.read_journal() is not None:
            raise BackupError("an unfinished deployment transaction exists; run `shiplord recover` first", "prepare",
                              "recovery_required")

    # operations ------------------------------------------------------------
    def backup(self) -> Outcome:
        try:
            self._gate()
            data_root = self.runtime.data_root(self.registration)
            directory = self._backup_directory(create=True)
            # Refuse a too-large or too-full job before the application is stopped, not halfway through.
            _entries, total = scan_data(data_root)
            require_free_space(directory, total)
            current, was_running = self._quiesce()
        except RuntimeFailure as failure:
            return _failed(failure)
        except (BackupError, StateError) as error:
            return _failed(error)
        record, error = None, None
        try:
            self.progress("archiving")
            stamp = time.strftime(STAMP_FORMAT, time.gmtime(self.clock()))
            record = write_archive(data_root, directory, application_id=self.application_id,
                                   runtime=self.registration["runtime"], name=self._next_name(directory, stamp),
                                   created_at=stamp)
        except (BackupError, RuntimeFailure, StateError, OSError) as failure:
            error = failure
        application = self._resume(current, was_running)
        if error is not None:
            return _failed(error, application)
        detail = {"archive": record.archive, "sha256": record.sha256, "size": record.size,
                  "entries": record.entries, "bytes": record.bytes, "verified": True, "application": application,
                  "scope": "the archive was reopened and matched the data it was made from"}
        if was_running and not application["running"]:
            return Outcome("intervention_required", 6, {
                "error": "restart_failed", "archive": record.archive, "verified": True, "application": application,
                "message": "the backup is written and verified, but the application did not start again; run "
                           "`shiplord status` and `shiplord recover`"})
        return Outcome("succeeded", 0, detail)

    def restore(self, request: dict) -> Outcome:
        staging = None
        try:
            record, path = self._locate(request)
            self._gate()
            data_root = self.runtime.data_root(self.registration)
            self.progress("verifying")
            verify_archive(path, record)
            require_free_space(data_root.parent, record.bytes)
            staging = data_root.parent / (".shiplord-restore-" + secrets.token_hex(8))
            self.progress("extracting")
            extract_archive(path, staging, record)
        except (BackupError, RuntimeFailure, StateError) as error:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            return _failed(error)
        current = None
        was_running = False
        stopped = False
        try:
            current, was_running = self._quiesce()
            stopped = True
            self.progress("swapping")
            replace_data(data_root, staging, record)
        except (BackupError, RuntimeFailure, StateError) as error:
            shutil.rmtree(staging, ignore_errors=True)
            return _failed(error, self._resume(current, was_running) if stopped else None)
        application = self._resume(current, was_running)
        detail = {"archive": record.archive, "sha256": record.sha256, "size": record.size,
                  "entries": record.entries, "bytes": record.bytes, "verified": True, "application": application,
                  "scope": "the archive was verified, extracted, and swapped in; the previous data was removed "
                           "after the replacement was verified"}
        if was_running and not application["running"]:
            return Outcome("intervention_required", 6, {
                "error": "restart_failed", "archive": record.archive, "verified": True, "application": application,
                "message": "the data was restored and verified, but the application did not start again; run "
                           "`shiplord status` and `shiplord recover`"})
        return Outcome("succeeded", 0, detail)


def _failed(failure: Exception, application: dict | None = None) -> Outcome:
    """Map a failure to an outcome without disclosing host paths or data."""
    if isinstance(failure, (BackupError, RuntimeFailure, StateError)):
        kind, message = getattr(failure, "kind", "") or "backup_failed", str(failure)
        phase = getattr(failure, "phase", None)
    else:  # an unexpected host error must not be echoed: its text may contain a path
        kind, message, phase = "backup_failed", "the operation failed; nothing was changed", None
    detail = {"error": kind, "message": message}
    if phase:
        detail["phase"] = phase
    if application is not None:
        detail["application"] = application
    if getattr(failure, "intervention_required", False):
        detail["intervention_required"] = True
        return Outcome("intervention_required", 6, detail)
    if isinstance(failure, BackupInputError):
        return Outcome("rejected", 2, detail)
    return Outcome("failed", 1, detail)
