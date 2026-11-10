"""Remove the host artifacts ``bootstrap`` installed, and nothing else.

The paths come from :mod:`shiplord.layout`; this module never globs a directory Shiplord shares with the host.
A file is removed only when its content still carries the marker ``bootstrap`` wrote, a version directory only
when its name is one ``bootstrap`` generates and it holds a ``.complete`` marker, and a directory only when it
is empty. Application data (volumes, native data directories, environment files, release binaries) is never
touched: removing an application is ``deprovision``'s job.

Each step is idempotent and reports what it changed, so a repeated uninstall is harmless. A failure stops the
run; the steps already done stay done, and ``shiplord server bootstrap`` restores them.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Callable

from .installer import (BootstrapError, COMPLETE_MARKER, MANAGED_MARKER, SSHD, SYSTEMCTL, VERSION_PATTERN,
                        _unit_active, _write_atomic, launcher_targets, remove_version_tree, sshd_units,
                        wrapper_text)
from .layout import ETC, Layout
from .registry import Registry
from .release_state import HostLock, StateError
from .routing import CaddyCommands, RouteManager, RoutingError
from .system import CommandError, System

MAX_ARTIFACT_BYTES = 65536  # a launcher or drop-in is a few hundred bytes; anything larger is not ours
PLACEHOLDER_NAME = "00-shiplord.caddy"
# Directories bootstrap creates for Shiplord's own state. Each is removed only when it is empty.
MANAGED_DIRECTORIES = ("registrations", "environments", "registry_logins", "authorized_keys", "app_state", "jobs",
                       "data", "releases")


class UninstallError(Exception):
    """Uninstall could not complete; the message names the failed step."""


class UninstallRefused(UninstallError):
    """A precondition failed; nothing was removed."""


def _open(path: Path, message: str) -> int | None:
    """Open a directory descriptor without following a link; None when the directory does not exist."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise UninstallError(message) from None


def _trusted(descriptor: int, what: str) -> None:
    """Refuse a directory another account could rewrite; ``what`` names it without a host path."""
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise UninstallError(f"{what} is not trusted")


def _entry(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise UninstallError("could not inspect a Shiplord artifact") from None


def _read(directory: int, name: str) -> bytes | None:
    """Read one small file through the directory descriptor, never following a link."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise UninstallError("could not read a Shiplord artifact") from None
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            raise UninstallError("a Shiplord artifact is not a regular file")
        return file.read(MAX_ARTIFACT_BYTES)


def _remove_artifact(path: Path, matches: Callable[[bytes], bool], what: str) -> bool:
    """Remove one file only when its current content still satisfies ``matches``.

    Returns True when it was removed, and False when it is absent or is not the file bootstrap wrote. The
    decision is made on the content read through the same descriptor that is used to remove it. ``what``
    names the directory in a refusal, without a host path.
    """
    directory = _open(path.parent, "a Shiplord directory is unavailable")
    if directory is None:
        return False
    try:
        _trusted(directory, what)
        entry = _entry(directory, path.name)
        if entry is None or not stat.S_ISREG(entry.st_mode):
            return False  # a link or a directory at this path is not the file bootstrap wrote
        data = _read(directory, path.name)
        if data is None or not matches(data):
            return False
        try:
            os.unlink(path.name, dir_fd=directory)
            os.fsync(directory)
        except OSError:
            raise UninstallError("could not remove a Shiplord artifact") from None
        return True
    finally:
        os.close(directory)


def _remove_empty(path: Path, what: str) -> bool:
    """Remove a directory Shiplord created when it is empty; a directory with content is kept."""
    directory = _open(path.parent, "a Shiplord directory is unavailable")
    if directory is None:
        return False
    try:
        _trusted(directory, what)
        try:
            os.rmdir(path.name, dir_fd=directory)
        except FileNotFoundError:
            return False
        except OSError as error:
            if error.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                return False  # still holds something: it is reported as retained
            raise UninstallError("could not remove an empty Shiplord directory") from None
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def _applications(layout: Layout, lock: HostLock) -> list[str]:
    """The registered applications, read before anything is removed."""
    directory = _open(layout.registrations, "the application registry is unavailable")
    if directory is None:
        return []
    try:
        _trusted(directory, "the application registry")
        try:
            return sorted(record["application_id"] for record in Registry(directory, lock).list())
        except StateError as error:
            raise UninstallRefused(f"the application registry cannot be read: {error}; nothing was removed") from None
    finally:
        os.close(directory)


def _sshd_units(layout: Layout, system: System) -> tuple[str | None, str | None]:
    """Discover the sshd units before the first change, so a host that cannot be reloaded is refused early."""
    if not os.path.lexists(layout.sshd_dropin):
        return None, None
    try:
        return sshd_units(system)
    except CommandError:
        raise UninstallError("could not run sshd checks; nothing was removed") from None


def _remove_sshd(layout: Layout, system: System, unit: str | None, socket: str | None) -> bool:
    """Remove the drop-in and reload sshd; put the drop-in back if sshd then rejects the configuration."""
    path = layout.sshd_dropin
    directory = _open(path.parent, "the sshd drop-in directory is unavailable")
    if directory is None:
        return False
    try:
        _trusted(directory, "the sshd drop-in directory")
        entry = _entry(directory, path.name)
        if entry is None or not stat.S_ISREG(entry.st_mode):
            return False
        previous = _read(directory, path.name)
        if previous is None or not previous.startswith(MANAGED_MARKER):
            return False
        try:
            os.unlink(path.name, dir_fd=directory)
            os.fsync(directory)
        except OSError:
            raise UninstallError("could not remove the sshd drop-in") from None
        try:
            result = system.run([SSHD, "-t"])
            if result.code != 0:
                raise UninstallError("sshd rejected the configuration after the Shiplord drop-in was removed; "
                                     "the drop-in was restored")
            socket_activated = (unit is not None and socket is not None
                                and _unit_active(system, socket) is True and _unit_active(system, unit) is False)
            if unit is not None and not socket_activated:
                result = system.run([SYSTEMCTL, "try-reload-or-restart", unit])
                if result.code != 0:
                    raise UninstallError("sshd could not reload after the Shiplord drop-in was removed; "
                                         "the drop-in was restored")
        except (UninstallError, CommandError) as error:
            try:
                _write_atomic(path, previous, 0o644)
            except BootstrapError:
                raise UninstallError("the sshd drop-in could not be restored; inspect the host") from None
            if isinstance(error, CommandError):
                raise UninstallError("could not run sshd checks; the drop-in was restored") from None
            raise
        return True
    finally:
        os.close(directory)


def _remove_caddy(layout: Layout, lock: HostLock, caddy: CaddyCommands) -> dict:
    """Remove the managed import, then the placeholder route; returns what changed."""
    result = {"import": False, "placeholder": False}
    if not layout.caddy_root.is_dir():
        return result
    try:
        result["import"] = RouteManager(layout.caddy_root, lock, caddy).remove_import().changed
    except RoutingError as error:
        raise UninstallError(f"Caddy integration failed ({error.kind}): {error}") from None
    result["placeholder"] = _remove_artifact(layout.caddy_root / "ship" / PLACEHOLDER_NAME,
                                             lambda data: data.startswith(MANAGED_MARKER),
                                             "the managed Caddy directory")
    return result


def _remove_versions(layout: Layout) -> int:
    """Remove the ``current`` link and every finished helper version; returns how many versions went."""
    directory = _open(layout.lib, "the helper directory is unavailable")
    if directory is None:
        return 0
    removed = 0
    try:
        _trusted(directory, "the helper directory")
        for name in sorted(os.listdir(directory)):
            entry = _entry(directory, name)
            if entry is None:
                continue
            if stat.S_ISLNK(entry.st_mode):
                if name == "current":
                    try:
                        os.unlink(name, dir_fd=directory)  # unlink removes the link, never what it points at
                    except OSError:
                        raise UninstallError("could not remove the current helper link") from None
                continue
            if not (stat.S_ISDIR(entry.st_mode) and VERSION_PATTERN.fullmatch(name)):
                continue
            marker = _entry(directory, f"{name}/{COMPLETE_MARKER}")
            if marker is None or not stat.S_ISREG(marker.st_mode):
                continue  # an interrupted install is left for the next bootstrap to complete
            try:
                remove_version_tree(directory, name)
            except BootstrapError as error:
                raise UninstallError(str(error)) from None
            removed += 1
        if removed:
            os.fsync(directory)
        return removed
    finally:
        os.close(directory)


def _remove_directories(layout: Layout) -> list[str]:
    """Remove the empty managed directories, children before parents; returns the layout names that went."""
    removed = [name for name in MANAGED_DIRECTORIES
               if _remove_empty(getattr(layout, name), f"the {name.replace('_', ' ')} directory")]
    if _remove_empty(layout.caddy_root / "ship", "the managed Caddy directory"):
        removed.append("caddy_ship")
    if _remove_empty(layout.lib, "the helper directory"):
        removed.append("lib")
    if _remove_empty(layout.path(ETC), "the Shiplord configuration directory"):
        removed.append("etc")
    return removed


def _retained(layout: Layout, applications: list[str], removed: dict) -> list[str]:
    """What is still on the host, named so the report never shows a host path."""
    notes = []
    if applications:
        notes.append(f"{len(applications)} provisioned application(s): their registrations, keys, sudoers rules, "
                     "routes, accounts, and data were left in place")
    if any(os.path.lexists(path) for path in launcher_targets(layout) if path.name not in removed["wrappers"]):
        notes.append("a launcher that Shiplord did not write")
    if not removed["sshd"] and os.path.lexists(layout.sshd_dropin):
        notes.append("an sshd drop-in that Shiplord did not write")
    if not removed["caddy"]["placeholder"] and os.path.lexists(layout.caddy_root / "ship" / PLACEHOLDER_NAME):
        notes.append("a managed Caddy file that Shiplord did not write")
    if os.path.lexists(layout.lib):
        notes.append("the helper directory, which still holds files Shiplord did not write")
    leftovers = [name for name in MANAGED_DIRECTORIES if os.path.lexists(getattr(layout, name))]
    if leftovers:
        notes.append("managed state directories that still hold files: " + ", ".join(leftovers))
    notes.append("the state directory that holds the host lock")
    return notes


def uninstall(layout: Layout, system: System, lock: HostLock, caddy: CaddyCommands, *, force: bool = False) -> dict:
    """Remove the host artifacts ``bootstrap`` installed. The caller holds the host lock.

    Refuses while an application is still provisioned unless ``force`` is given; with ``force`` those
    applications keep every artifact of their own, so re-bootstrapping restores them. Application data is
    never removed either way.
    """
    if not lock.held:
        raise UninstallError("host lock is required")
    applications = _applications(layout, lock)
    if applications and not force:
        raise UninstallRefused(
            f"{len(applications)} application{' is' if len(applications) == 1 else 's are'} still provisioned ("
            + ", ".join(applications) + "); deprovision them first, or pass --force to remove the host and leave "
            "them registered")
    unit, socket = _sshd_units(layout, system)  # refuse a host that cannot be reloaded before the first change
    removed = {"caddy": _remove_caddy(layout, lock, caddy)}
    removed["sshd"] = _remove_sshd(layout, system, unit, socket)
    removed["wrappers"] = [path.name for path, launcher in launcher_targets(layout).items()
                           if _remove_artifact(path, lambda data, text=wrapper_text(layout, launcher): data == text,
                                               "the launcher directory")]
    removed["versions"] = _remove_versions(layout)
    removed["directories"] = _remove_directories(layout)
    return {"status": "uninstalled", "removed": removed,
            "retained": _retained(layout, applications, removed), "applications": applications}
