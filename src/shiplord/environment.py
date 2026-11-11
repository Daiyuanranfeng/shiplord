"""Runtime environment files: strict parsing, managed-key protection, and safe loading.

Values are literal. They are never evaluated by a shell, and Docker's env-file
format does not unquote them, so they are stored and passed exactly as given.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

MAX_ENVIRONMENT_BYTES = 65536
MAX_VALUE_BYTES = 8192
RESERVED = frozenset({"HOST", "PORT", "AGENT_DIR", "AGENT_NAME", "LOG_LEVEL", "RELOAD_AGENTS", "SERVE_WEB_INTERFACE",
                      "ALLOW_ORIGINS", "IMAGE"})
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class EnvironmentError_(ValueError):
    """An environment file is invalid or unsafe."""


def validate_key(key: object) -> str:
    """Reject a variable name this module and Docker would not agree on; never echo a value."""
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise EnvironmentError_("the variable name must start with a letter or underscore and contain only "
                                "letters, digits, and underscores")
    if key in RESERVED:
        raise EnvironmentError_(f"{key} is managed by Shiplord and cannot be changed")
    return key


def validate_value(value: object) -> str:
    """Values are literal one-line text: a line break would inject a second assignment."""
    if (not isinstance(value, str) or "\0" in value or "\r" in value or "\n" in value
            or len(value.encode("utf-8", "replace")) > MAX_VALUE_BYTES):
        raise EnvironmentError_("the value is invalid")
    return value


def parse_assignment(assignment: object) -> tuple[str, str]:
    """Split one ``KEY=VALUE`` argument. Errors name the shape, never the text that was passed."""
    if not isinstance(assignment, str):
        raise EnvironmentError_("an assignment is required: KEY=VALUE")
    key, separator, value = assignment.partition("=")
    if not separator:
        raise EnvironmentError_("the assignment must be KEY=VALUE")
    return validate_key(key), validate_value(value)


def parse_environment(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines; comments and blank lines are ignored, anything else is rejected."""
    if len(text.encode("utf-8", "replace")) > MAX_ENVIRONMENT_BYTES:
        raise EnvironmentError_("environment is too large")
    values: dict[str, str] = {}
    # Split on "\n" only, exactly as Docker's env-file reader does; str.splitlines() also splits on
    # \r, \x0b, \u2028 and others, which would let this parser and Docker disagree about a line.
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, 1):
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not _KEY.fullmatch(key):
            raise EnvironmentError_(f"line {number} is not KEY=VALUE")
        if key in RESERVED:
            raise EnvironmentError_(f"{key} is managed by Shiplord and cannot be set")
        if key in values:
            raise EnvironmentError_(f"{key} is set more than once")
        if "\0" in value or "\r" in value or len(value.encode("utf-8", "replace")) > MAX_VALUE_BYTES:
            raise EnvironmentError_(f"the value of {key} is invalid")
        values[key] = value
    return values


def render_environment(values: dict[str, str]) -> str:
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


def edit_environment(text: str, key: str, value: str | None) -> str:
    """Set (``value``) or remove (``None``) one variable, leaving every other line byte-identical.

    The file must already parse, because a line this module cannot account for is a line it must not
    rewrite. Comments, blank lines, order, line endings and the presence of a final newline are kept.
    A value is written literally: it is never quoted, unquoted, or interpreted.
    """
    validate_key(key)
    if value is not None:
        validate_value(value)
    parse_environment(text)  # never rewrite a file whose meaning this module cannot reproduce
    lines = text.split("\n")
    count = len(lines) - 1 if text.endswith("\n") else len(lines)
    found = None
    for index in range(count):
        line = lines[index][:-1] if lines[index].endswith("\r") else lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.partition("=")[0] == key:
            found = index  # parse_environment has already rejected a second assignment of this key
    if value is None:
        if found is None:
            return text
        del lines[found]
        return "\n".join(lines)
    if found is not None:
        lines[found] = f"{key}={value}" + ("\r" if lines[found].endswith("\r") else "")
        result = "\n".join(lines)
    elif text == "":
        result = f"{key}={value}\n"
    elif text.endswith("\n"):
        terminator = "\r\n" if text.endswith("\r\n") else "\n"
        result = f"{text}{key}={value}{terminator}"
    else:  # no final newline: add the new line without inventing one at the end
        result = f"{text}\n{key}={value}"
    parse_environment(result)
    return result


def load_environment_text(path: Path) -> str | None:
    """Read a private, trusted environment file as text; return None when it does not exist."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise EnvironmentError_("could not open the environment file safely") from None
    try:
        file = os.fdopen(descriptor, "rb")
    except OSError:  # a directory in place of the file opens but cannot be read
        os.close(descriptor)
        raise EnvironmentError_("could not open the environment file safely") from None
    with file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_ENVIRONMENT_BYTES):
            raise EnvironmentError_("the environment file is not private to the installing account")
        try:
            return file.read(MAX_ENVIRONMENT_BYTES + 1).decode("utf-8")
        except UnicodeDecodeError:
            raise EnvironmentError_("the environment file is not valid UTF-8") from None


def load_environment_file(path: Path) -> dict[str, str] | None:
    """Read a private, trusted environment file; return None when it does not exist."""
    text = load_environment_text(path)
    return None if text is None else parse_environment(text)
