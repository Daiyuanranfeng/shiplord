"""Validation of the persistent-storage configuration of a Docker application.

Stdlib-only: the server imports this module, so the privileged helper and the client
reject the same inputs. Invalid input is refused, never normalised.
"""

from __future__ import annotations

import re

DEFAULT_CONTAINER_PATH = "/app/src/.adk"
MAX_PATH_BYTES = 4096
FEATURE = "docker_storage_v1"  # capability a helper advertises when it can honour [storage]

_COMPONENT = re.compile(r"[A-Za-z0-9._-]+\Z")
# Matched per path component, so /data/etc is allowed and /etc/data is not.
_RESERVED = ("/proc", "/sys", "/dev", "/run", "/etc", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/boot", "/root",
             "/var/run", "/var/lock")


class StorageError(ValueError):
    """A storage setting cannot be used safely."""


def validate_container_path(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or len(value.encode()) > MAX_PATH_BYTES:
        raise StorageError("storage.container_path must be an ASCII absolute directory path")
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        raise StorageError("storage.container_path must be a canonical absolute path other than / without a trailing slash")
    components = value[1:].split("/")
    for component in components:
        if not component or component in (".", "..") or not _COMPONENT.fullmatch(component):
            raise StorageError("storage.container_path components may contain only letters, digits, '.', '_' and '-'")
    for reserved in _RESERVED:
        reserved_parts = reserved[1:].split("/")
        if components[:len(reserved_parts)] == reserved_parts:
            raise StorageError(f"storage.container_path must not be {reserved} or inside it")
    return value


def validate_storage(raw: object) -> dict:
    """Validate the ``[storage]`` table and return only the fields that were given.

    Ownership is refused here, at manifest validation, rather than at deploy: establishing it safely needs a
    pinned initializer image, so accepting the setting would only move the refusal to the worst possible place.
    An image that establishes the mount directory's ownership during its build needs no setting.
    """
    if not isinstance(raw, dict):
        raise StorageError("storage must be a table with only container_path")
    if "owner_uid" in raw or "owner_gid" in raw:
        raise StorageError("storage.owner_uid and storage.owner_gid are not supported: "
                           "the image must establish the mount directory's ownership")
    if set(raw) - {"container_path"}:
        raise StorageError("storage must be a table with only container_path")
    if "container_path" not in raw:
        raise StorageError("storage.container_path is required")
    return {"container_path": validate_container_path(raw["container_path"])}


def container_path(configuration: dict) -> str:
    """The effective mount path: defaults are applied here, never written into stored records."""
    return configuration.get("storage", {}).get("container_path", DEFAULT_CONTAINER_PATH)
