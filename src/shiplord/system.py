"""Run host commands without a shell, with a deadline and bounded captured output."""

from __future__ import annotations

import codecs
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

MAX_OUTPUT_BYTES = 4096
MAX_STREAM_CHUNK = 65536
STREAM_POLL_SECONDS = 0.1
DEFAULT_TIMEOUT_SECONDS = 60
FIXED_ENVIRONMENT = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
TRUNCATION_MARKER = "[shiplord: earlier output truncated]\n"


@dataclass(frozen=True)
class CommandResult:
    code: int
    output: str  # capped and stripped of control characters


class CommandError(Exception):
    """The command could not be run to completion; ``kind`` is "start" or "timeout"."""

    def __init__(self, message: str, kind: str, output: str = ""):
        super().__init__(message)
        self.kind = kind
        self.output = output


class System(Protocol):
    def run(self, argv: Sequence[str], *, input: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT_SECONDS, max_output: int = MAX_OUTPUT_BYTES) -> CommandResult: ...

    def stream(self, argv: Sequence[str], *, timeout: float, on_chunk: Callable[[str], None]) -> str: ...


def sanitize(data: bytes | bytearray, limit: int = MAX_OUTPUT_BYTES, *, truncated: bool = False) -> str:
    """Decode the newest ``limit`` bytes; ``truncated`` says older bytes were dropped and labels them."""
    limit = max(0, limit)
    marker = TRUNCATION_MARKER if truncated and len(TRUNCATION_MARKER) < limit else ""
    size = limit - len(marker)
    text = (bytes(data[len(data) - size:]) if size else b"").decode("utf-8", "replace")
    return marker + "".join(char if char == "\n" or char.isprintable() else "?" for char in text)


def run_command(argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT_SECONDS, input: bytes | None = None,
                env: Mapping[str, str] | None = None, cwd: str = "/",
                max_output: int = MAX_OUTPUT_BYTES) -> CommandResult:
    """Run ``argv`` directly (never through a shell) and return its capped output."""
    argv = [str(part) for part in argv]
    if not argv or any("\0" in part for part in argv):
        raise CommandError("invalid command", "start")
    try:
        process = subprocess.Popen(argv, shell=False, env=dict(env if env is not None else FIXED_ENVIRONMENT),
                                   stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                                   start_new_session=True)
    except OSError:
        raise CommandError("could not start the command", "start") from None
    kept = bytearray()
    truncated = False
    deadline = time.monotonic() + timeout
    writer = None
    if input is not None:
        def feed() -> None:
            try:
                process.stdin.write(input)
                process.stdin.close()
            except OSError:
                pass
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError("the command timed out", "timeout", sanitize(kept, max_output, truncated=truncated))
            if select.select([process.stdout], [], [], min(0.1, remaining))[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                kept.extend(chunk)
                if len(kept) > max_output:
                    del kept[: len(kept) - max_output]
                    truncated = True
            elif process.poll() is not None and not select.select([process.stdout], [], [], 0)[0]:
                break
        try:
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise CommandError("the command timed out", "timeout",
                               sanitize(kept, max_output, truncated=truncated)) from None
        return CommandResult(code, sanitize(kept, max_output, truncated=truncated))
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            process.wait()
        process.stdout.close()
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        if writer is not None:
            writer.join(timeout=1)


def _stream_text(text: str) -> str:
    """Keep line breaks and printable text; other control characters cannot reach a terminal."""
    return "".join(char if char == "\n" or char.isprintable() else "?" for char in text)


def stream_command(argv: Sequence[str], *, timeout: float, on_chunk: Callable[[str], None],
                   env: Mapping[str, str] | None = None, cwd: str = "/") -> str:
    """Run ``argv`` and forward its output as it arrives; returns "ended" or "timeout".

    Output is handed to ``on_chunk`` immediately, so a long stream is never held in memory. The
    deadline is enforced here: when it expires the process group is killed, not merely abandoned.
    """
    argv = [str(part) for part in argv]
    if not argv or any("\0" in part for part in argv) or timeout <= 0:
        raise CommandError("invalid command", "start")
    try:
        process = subprocess.Popen(argv, shell=False, env=dict(env if env is not None else FIXED_ENVIRONMENT),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   cwd=cwd, start_new_session=True)
    except OSError:
        raise CommandError("could not start the command", "start") from None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    deadline = time.monotonic() + timeout
    reason = "ended"
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            if select.select([process.stdout], [], [], min(STREAM_POLL_SECONDS, remaining))[0]:
                chunk = os.read(process.stdout.fileno(), MAX_STREAM_CHUNK)
                if not chunk:
                    break
                text = _stream_text(decoder.decode(chunk))
                if text:
                    on_chunk(text)
            elif process.poll() is not None and not select.select([process.stdout], [], [], 0)[0]:
                break
        if reason == "ended":
            text = _stream_text(decoder.decode(b"", True))
            if text:
                on_chunk(text)
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "timeout"
        return reason
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
            process.wait()
        process.stdout.close()


class HostSystem:
    """The production ``System``: fixed environment, no shell."""

    def run(self, argv: Sequence[str], *, input: bytes | None = None,
            timeout: float = DEFAULT_TIMEOUT_SECONDS, max_output: int = MAX_OUTPUT_BYTES) -> CommandResult:
        return run_command(argv, timeout=timeout, input=input, max_output=max_output)

    def stream(self, argv: Sequence[str], *, timeout: float, on_chunk: Callable[[str], None]) -> str:
        return stream_command(argv, timeout=timeout, on_chunk=on_chunk)
