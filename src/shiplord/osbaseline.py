"""The server operating systems Shiplord supports, decided from ``/etc/os-release``.

Shiplord installs an apt-provided Caddy and runs its helper with ``/usr/bin/python3``, which must be Python 3.11
or later. Rather than guess from a failed command later, bootstrap reads the release file directly and refuses
a host outside the supported set before it changes anything. Reading through a ``Layout`` prefix lets tests
exercise the same code against a fixture instead of the machine they run on.
"""

from __future__ import annotations

import re
from pathlib import Path

OS_RELEASE = "/etc/os-release"
SUPPORTED = "Ubuntu 24.04 or later and Debian 12 or later"
MINIMUM_VERSIONS = {"ubuntu": (24, 4), "debian": (12, 0)}
MAX_LABEL = 32  # a distribution field is echoed in an error, so it stays short and free of separators
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _label(value: str | None) -> str | None:
    """A release field that is safe to name in a message, or None when it is absent or unusable."""
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > MAX_LABEL or not _LABEL.fullmatch(value):
        return None
    return value


def _version(value: str | None) -> tuple[int, int] | None:
    """A ``VERSION_ID`` as a comparable major/minor pair; None when it is not a dotted number."""
    if value is None:
        return None
    fields = value.split(".")
    if not 1 <= len(fields) <= 3 or not all(field.isdigit() and len(field) <= 3 for field in fields):
        return None
    numbers = [int(field) for field in fields]
    return numbers[0], numbers[1] if len(numbers) > 1 else 0


def parse_os_release(text: str) -> dict[str, str]:
    """The ``KEY=VALUE`` fields of an os-release body, with matching quotes removed.

    Blank lines, comments, and lines without an ``=`` are ignored: a release file with one unfamiliar line
    should not be refused for that alone.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            fields[key] = value
    return fields


def _refused(identity: str | None, version_id: str | None, reason: str) -> dict:
    """A refusal that names the supported set and what was detected, and nothing else from the host."""
    return {"ok": False, "id": identity, "version_id": version_id,
            "message": f"unsupported server operating system: {reason}; Shiplord supports {SUPPORTED}"}


def baseline(text: str | None) -> dict:
    """Decide whether an os-release body describes a supported host; None means it could not be read."""
    if text is None:
        return _refused(None, None, "the host's release information could not be read")
    fields = parse_os_release(text)
    identity, version_id = _label(fields.get("ID")), _label(fields.get("VERSION_ID"))
    identity = identity.lower() if identity else None  # os-release specifies lowercase, but a mixed one still works
    detected = " ".join(part for part in (identity, version_id) if part)
    if identity is None:
        return _refused(None, version_id, "the host did not identify its distribution")
    minimum = MINIMUM_VERSIONS.get(identity)
    if minimum is None:
        return _refused(identity, version_id, f"{detected} is not a supported distribution")
    version = _version(version_id)
    if version is None:
        return _refused(identity, version_id, f"{identity} did not report a usable version")
    if version < minimum:
        return _refused(identity, version_id, f"{detected} is older than the supported minimum")
    return {"ok": True, "id": identity, "version_id": version_id}


def read_os_release(path: Path) -> str | None:
    """The text of an os-release file, or None when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def check_os(path: Path = Path(OS_RELEASE)) -> dict:
    """The support decision for the os-release file at ``path`` (``/etc/os-release`` on a real host)."""
    return baseline(read_os_release(path))
