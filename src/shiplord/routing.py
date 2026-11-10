"""Caddy route transactions for registered applications.

Each application owns one file, ``<root>/ship/<app>.caddy``, pulled into the main
Caddyfile by a single top-level import. A candidate route is validated inside a
temporary Caddyfile before it replaces the live file, so the file Caddy would
read after a restart has always been validated. The previous file is kept as a
hard link until the reload succeeds, which makes the backup durable.

Assumptions not verified against a real Caddy yet (a Linux integration job must
confirm them): explicit imports of dot-named files, validation behavior under
the invoking account, and the reload command. Other writers of ``<root>`` are
not excluded by the host lock; validation is the backstop for that race.
"""

from __future__ import annotations

import glob
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Protocol, Sequence

from .manifest import validate_app_name, validate_hostname
from .registry import PORT_RANGE, validate_registration
from .release_state import HostLock, _directory
from .system import TRUNCATION_MARKER, CommandError, HostSystem, System, run_command

MAX_CONFIG_BYTES = 1024 * 1024
MAX_IMPORTED_FILES = 64
MAX_IMPORT_DEPTH = 3
COMMAND_TIMEOUT_SECONDS = 30
# A route file holds only a hostname and a loopback port, so it is world-readable like the installer's placeholder
# route; the Caddyfile, which can hold a token or a password hash, keeps whatever mode it already had instead.
ROUTE_FILE_MODE = 0o644
_TEMP_PREFIX = ".tmp-"
_PRIOR_PREFIX = ".prior-"
_PRIOR_MAIN = ".prior-Caddyfile"
_VALIDATE_PREFIX = ".shiplord-validate-"
SYSTEMCTL = "/usr/bin/systemctl"
CADDY_UNIT = "caddy"
PACKAGED_ENVIRONMENT_FILES = (Path("/etc/default/caddy"), Path("/etc/sysconfig/caddy"))
MAX_ENVIRONMENT_BYTES = 64 * 1024
_REDACTED = "[shiplord:redacted]"
MIN_REDACTED_LENGTH = 4  # shorter values would replace ordinary words and destroy the diagnostic
# A fixed, valid configuration that proves Caddy can run at all; it is never written to the host's Caddy root.
CADDY_PROBE = b'http://127.0.0.1:1 {\n\trespond "ok"\n}\n'


class RoutingError(Exception):
    """A routing operation failed; ``kind`` classifies the failure."""

    def __init__(self, message: str, kind: str, *, intervention_required: bool = False, detail: str = ""):
        super().__init__(message)
        self.kind = kind
        self.intervention_required = intervention_required
        self.detail = detail  # capped command output; never part of str(self)


@dataclass(frozen=True)
class RouteResult:
    changed: bool


class CaddyCommands(Protocol):
    def validate(self, config: Path) -> None: ...
    def reload(self) -> None: ...


# --- The service environment ----------------------------------------------

def _escape(char: str) -> str:
    """The character a systemd escape sequence stands for; an unknown escape stands for its character."""
    return {"n": "\n", "t": "\t", "r": "\r", "s": " "}.get(char, char)


def _words(text: str) -> Iterator[str]:
    """Split a value the way systemctl quotes it: whitespace separates words, quotes and backslashes group."""
    word: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'" and index + 1 < len(text):
            word.append(_escape(text[index + 1]))
            index += 2
            continue
        if quote is None and char.isspace():
            if word:
                yield "".join(word)
                word = []
        elif quote is None and char in "\"'":
            quote = char
        elif quote == char:
            quote = None
        else:
            word.append(char)
        index += 1
    if word:
        yield "".join(word)


def parse_environment_list(text: str) -> dict[str, str]:
    """The assignments in a systemd ``Environment=`` property value."""
    variables = {}
    for word in _words(text):
        name, separator, value = word.partition("=")
        if separator and name and "\0" not in value:
            variables[name] = value
    return variables


def parse_environment_file(text: str) -> dict[str, str]:
    """The assignments in a systemd environment file; a malformed line is dropped, as systemd drops it."""
    variables = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        name, separator, value = (part.strip() for part in line.partition("="))
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        if value.startswith(('"', "'")):
            quote, index, characters = value[0], 1, []
            while index < len(value) and value[index] != quote:
                if quote == '"' and value[index] == "\\" and index + 1 < len(value):
                    characters.append(_escape(value[index + 1]))
                    index += 2
                    continue
                characters.append(value[index])
                index += 1
            if index != len(value) - 1:
                continue  # no closing quote at the end of the line: systemd drops the assignment
            value = "".join(characters)
        if "\0" not in value:
            variables[name] = value
    return variables


def _environment_file(path: Path) -> dict[str, str]:
    """The assignments in one environment file; unreadable, oversized, and non-regular files yield nothing."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENVIRONMENT_BYTES:
            return {}
        with open(path, "rb") as file:
            return parse_environment_file(file.read(MAX_ENVIRONMENT_BYTES).decode("utf-8", "replace"))
    except OSError:
        return {}


def _systemd_environment(system: System) -> dict[str, str] | None:
    """The environment systemd reports for the Caddy unit, or None when that answer cannot be read."""
    try:
        result = system.run([SYSTEMCTL, "show", CADDY_UNIT, "--property=Environment",
                             "--property=EnvironmentFiles"], max_output=MAX_ENVIRONMENT_BYTES)
    except CommandError:
        return None
    if result.code != 0 or result.output.startswith(TRUNCATION_MARKER):
        return None  # a half-reported value could be a prefix of a secret, which is worse than no value
    variables: dict[str, str] = {}
    files: list[Path] = []
    for line in result.output.splitlines():
        for property in ("Environment=", "EnvironmentFile=", "EnvironmentFiles="):  # older systemctl used the singular
            if not line.startswith(property):
                continue
            entry = line[len(property):]
            if property == "Environment=":
                variables.update(parse_environment_list(entry))
            else:
                path = Path(entry.rsplit(" (ignore_errors=", 1)[0])
                if path.is_absolute():
                    files.append(path)
            break
    for path in files:
        variables.update(_environment_file(path))
    return variables


def service_environment(system: System, files: Sequence[Path] = PACKAGED_ENVIRONMENT_FILES) -> dict[str, str]:
    """The variables the Caddy service runs with, from the unit systemd reports or the packaged defaults.

    Empty when no documented source can be read, so validation falls back to its fixed environment. The caller's
    own environment is never consulted: it is not the service's.
    """
    reported = _systemd_environment(system)
    if reported is not None:
        return reported
    variables: dict[str, str] = {}
    for path in files:
        variables.update(_environment_file(path))
    return variables


def _host_service_environment() -> dict[str, str]:
    return service_environment(HostSystem())


def _redact(text: str, values: Iterable[str]) -> str:
    """Remove service-environment values from captured output, longest first, so a secret cannot reach a result."""
    for value in sorted({value for value in values if len(value) >= MIN_REDACTED_LENGTH}, key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    return text


class SubprocessCaddy:
    """Run Caddy and systemd through fixed executables; no shell, fixed defaults, and the service's variables."""

    ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}

    def __init__(self, caddy: str = "/usr/bin/caddy", systemctl: str = SYSTEMCTL,
                 timeout: float = COMMAND_TIMEOUT_SECONDS,
                 service_environment: Callable[[], Mapping[str, str] | None] | None = None):
        self.caddy, self.systemctl, self.timeout = caddy, systemctl, timeout
        # Injected so a caller that already knows the service environment can supply it; the default reads only
        # the documented service sources, never this process's environment.
        self.service_environment = service_environment or _host_service_environment

    def validate(self, config: Path) -> None:
        # Validation provisions modules (e.g. a local CA), which writes to Caddy's storage. Without HOME, Caddy falls
        # back to ./caddy in the current directory; give it a throwaway home so it never touches real storage or cwd.
        # The service's variables are applied first and the throwaway directories last, so a service that sets HOME
        # or XDG_* still cannot make validation write the service's storage.
        with tempfile.TemporaryDirectory(prefix="shiplord-caddy-validate-") as home:
            service = dict(self.service_environment() or {})
            environment = {**self.ENVIRONMENT, **service, "HOME": home, "XDG_DATA_HOME": home,
                           "XDG_CONFIG_HOME": home}
            self._run([self.caddy, "validate", "--config", str(config), "--adapter", "caddyfile"], "validation",
                      env=environment, secrets=service.values())

    def reload(self) -> None:
        self._run([self.systemctl, "reload", "caddy"], "reload")

    def _run(self, argv: list[str], kind: str, env: dict[str, str] | None = None,
             secrets: Iterable[str] = ()) -> None:
        try:
            result = run_command(argv, timeout=self.timeout, env=env or self.ENVIRONMENT)
        except CommandError as error:
            if error.kind == "timeout":
                raise RoutingError("Caddy command timed out", kind, detail=_redact(error.output, secrets)) from None
            # A command that never started proves nothing about a Caddyfile, so it is not the caller's kind.
            raise RoutingError(f"could not start {argv[0]}", "unavailable",
                               detail=_redact(error.output, secrets)) from None
        if result.code != 0:
            raise RoutingError("Caddy command failed", kind, detail=_redact(result.output, secrets))


def probe_caddy(commands: CaddyCommands) -> None:
    """Prove the Caddy command can run, before anything on the host is written.

    The configuration is fixed and trivial, so a refusal means Caddy itself is unusable rather than that an
    operator's Caddyfile is wrong. A missing or unstartable binary is reported as ``unavailable``.
    """
    with tempfile.TemporaryDirectory(prefix="shiplord-caddy-probe-") as directory:
        config = Path(directory) / "Caddyfile"
        config.write_bytes(CADDY_PROBE)
        commands.validate(config)


def render_route(domain: str, port: int) -> bytes:
    """Render a route from validated values only."""
    domain = validate_hostname(domain, "api_domain")
    if type(port) is not int or port not in PORT_RANGE:
        raise RoutingError("upstream port is outside the managed range", "precondition")
    return f"{domain} {{\n\treverse_proxy 127.0.0.1:{port}\n}}\n".encode()


# --- Caddyfile inspection -------------------------------------------------

def _lines(text: str) -> Iterator[tuple[int, list[str], list[bool], int]]:
    """Yield (line number, tokens, whether each token was quoted, depth before the line)."""
    depth = 0
    heredoc: str | None = None
    for number, raw in enumerate(text.splitlines()):
        if heredoc is not None:
            if raw.split(None, 1)[:1] == [heredoc]:  # the marker may be followed by more arguments
                heredoc = None
            continue
        tokens: list[str] = []
        quoted: list[bool] = []
        structural: list[str] = []
        i, n = 0, len(raw)
        while i < n:
            char = raw[i]
            if char.isspace():
                i += 1
            elif char == "#":
                break
            elif char in "\"`":
                j, buffer = i + 1, []
                while j < n and raw[j] != char:
                    if char == '"' and raw[j] == "\\" and j + 1 < n:
                        j += 1
                    buffer.append(raw[j])
                    j += 1
                if j >= n:
                    raise RoutingError("cannot inspect the Caddy configuration", "uninspectable")
                tokens.append("".join(buffer))
                quoted.append(True)
                i = j + 1
            else:
                j = i
                while j < n and not raw[j].isspace():
                    j += 1
                tokens.append(raw[i:j])
                quoted.append(False)
                structural.append(raw[i:j])
                i = j
        if tokens and not quoted[-1] and tokens[-1].startswith("<<") and len(tokens[-1]) > 2:
            heredoc = tokens[-1][2:]
        yield number, tokens, quoted, depth
        for token in structural:
            if token == "{":
                depth += 1
            elif token == "}":
                depth -= 1
                if depth < 0:
                    raise RoutingError("cannot inspect the Caddy configuration", "uninspectable")
    if depth != 0 or heredoc is not None:
        raise RoutingError("cannot inspect the Caddy configuration", "uninspectable")


def _address_tokens(tokens: list[str], quoted: list[bool]) -> tuple[list[str], bool]:
    """Return the tokens on this line that Caddy reads as a site address, and whether it opens a block.

    A site block begins with its addresses and ends them at the opening brace, so a directive
    argument such as an upstream, a header value, or a request matcher is never an address, and a
    hostname inside one does not claim the site. A token that cannot be classified is refused.
    """
    addresses: list[str] = []
    for token, was_quoted in zip(tokens, quoted):
        if "{" in token or "}" in token:
            if not was_quoted and token in {"{", "}"}:
                return addresses, token == "{"  # the block body starts here; the rest is directives
            raise RoutingError("cannot determine hostnames from a placeholder", "uninspectable")
        if not was_quoted and token.startswith(("(", "&(")) and token.endswith(")"):
            continue  # a snippet or named-route definition names no site
        addresses.append(token)
    return addresses, False


def _addresses(token: str) -> Iterator[str]:
    for part in token.split(","):
        part = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", part.strip()).split("/")[0]
        yield _canonical_host(re.sub(r":[0-9]+$", "", part))


def _canonical_host(host: str) -> str:
    """Lowercase, drop a trailing root dot, and punycode (Caddy treats these spellings as one site)."""
    host = host.strip('"').lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


class _Inspection:
    def __init__(self, domain: str, own_glob: str, allow_wildcard_overlap: bool):
        self.domain, self.own_glob, self.allow_wildcard = domain, own_glob, allow_wildcard_overlap

    def scan(self, text: str) -> list[str]:
        """Check one file for hostname takeover; return path-style imports.

        Only a site block declares addresses, so a hostname is compared only where one can be
        declared: on a depth-0 line, before its opening brace. A file whose single site block
        omits the braces reads its first line as the address list and the rest as directives.
        """
        imports: list[str] = []
        body = continuing = False
        for _number, tokens, quoted, depth in _lines(text):
            if depth != 0 or body or not tokens:
                continue
            if tokens[0] == "import" and not quoted[0]:
                imports.extend(tokens[1:])  # a top-level import substitutes site blocks in place
                continue
            addresses, opened = _address_tokens(tokens, quoted)
            for token in addresses:
                for address in _addresses(token):
                    if address == self.domain:
                        raise RoutingError("hostname is already served by another configuration", "conflict")
                    if address.startswith("*.") and self.domain.endswith(address[1:]) and not self.allow_wildcard:
                        raise RoutingError("hostname overlaps an existing wildcard site", "conflict")
            continuing = not opened and tokens[-1].endswith(",")  # Caddy continues the address list
            body = not opened and not continuing  # braces are optional for a single site block
        return imports

    def own_import_lines(self, text: str) -> list[int]:
        found = []
        for number, tokens, quoted, depth in _lines(text):
            if depth == 0 and tokens == ["import", self.own_glob] and not any(quoted):
                found.append(number)
        return found


@dataclass(frozen=True)
class _CaddyFile:
    """A file read through a directory descriptor, plus the owner and mode a rewrite of it must keep."""

    data: bytes
    mode: int  # permission bits only; set-id and sticky bits are never carried
    uid: int
    gid: int


def _read_file(directory: int, name: str, *, trusted: bool) -> _CaddyFile | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise RoutingError("could not open a Caddy file safely", "uninspectable") from None
    try:
        info = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise RoutingError("a Caddy file cannot be read", "uninspectable") from None
    if not stat.S_ISREG(info.st_mode):  # a directory opens fine; fdopen would raise and leak the descriptor
        os.close(descriptor)
        raise RoutingError("a Caddy file is not safe to use", "uninspectable")
    with os.fdopen(descriptor, "rb") as file:
        if (info.st_size > MAX_CONFIG_BYTES
                or (trusted and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022))):
            raise RoutingError("a Caddy file is not safe to use", "uninspectable")
        # Taken from the descriptor that was read, so a replacement cannot swap the identity between the two.
        return _CaddyFile(file.read(MAX_CONFIG_BYTES + 1)[:MAX_CONFIG_BYTES], stat.S_IMODE(info.st_mode) & 0o777,
                          info.st_uid, info.st_gid)


def _read_path(path: str) -> str:
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise RoutingError("an imported Caddy file cannot be inspected", "uninspectable")
        with open(path, "rb") as file:
            return file.read(MAX_CONFIG_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise RoutingError("an imported Caddy file cannot be inspected", "uninspectable") from None


# --- Transactions ---------------------------------------------------------

def _discard(directory: int, name: str) -> None:
    """Remove a file this module just created; the caller reports why it is going away."""
    try:
        os.unlink(name, dir_fd=directory)
    except OSError:
        pass


def _unreconciled_prior() -> RoutingError:
    """The one refusal a leftover prior produces when the live Caddyfile cannot be proven to match it.

    It names the file and the step that unblocks the host, and nothing else from the host's filesystem.
    """
    return RoutingError(
        "an interrupted Caddyfile change left .prior-Caddyfile behind and the live Caddyfile is neither the previous "
        "file nor that file with the managed import; compare .prior-Caddyfile with the Caddyfile, keep the one to "
        "serve, remove .prior-Caddyfile, and retry", "interrupted", intervention_required=True)


class RouteManager:
    """Apply, remove, and reconcile managed routes under the host lock."""

    def __init__(self, root: Path, lock: HostLock, commands: CaddyCommands, *,
                 allow_wildcard_overlap: bool = False):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise RoutingError("Caddy root must be absolute", "precondition")
        self.lock, self.commands = lock, commands
        self.allow_wildcard_overlap = allow_wildcard_overlap
        self.own_glob = f"{self.root}/ship/*.caddy"

    # public operations
    def apply(self, registration: dict) -> RouteResult:
        """Install the app's route; skip all work only if the file is already identical."""
        return self._transition(registration, force=False)

    def remove(self, application_id: str) -> RouteResult:
        """Remove the app's route (idempotent when the file is already absent)."""
        return self._transition(application_id, force=False)

    def reconcile(self, application_id: str, registration: dict | None) -> RouteResult:
        """Drive disk and active Caddy to the desired state, always validating and reloading."""
        return self._transition(registration if registration is not None else application_id, force=True,
                                application_id=application_id)

    def inspect_route(self, registration: dict) -> str:
        """Read-only: "absent", "identical", or "different" for the app's route file. Needs no lock."""
        registration = validate_registration(registration)
        root, ship = self._open()
        try:
            current = _read_file(ship, f"{registration['application_id']}.caddy", trusted=True)
        finally:
            os.close(root)
            os.close(ship)
        if current is None:
            return "absent"
        rendered = render_route(registration["api_domain"], registration["host_port"])
        return "identical" if current.data == rendered else "different"

    def ensure_import(self) -> RouteResult:
        """Append the managed import to the main Caddyfile once, validated and restorable."""
        if not self.lock.held:
            raise RoutingError("host lock is required", "precondition")
        root, ship = self._open()
        try:
            token = secrets.token_hex(8)
            main, completed = self._settle_prior(root)
            if completed:
                return RouteResult(changed=False)
            if main is None:
                raise RoutingError("the main Caddyfile is missing", "precondition")
            text = main.data.decode("utf-8", "replace")
            found = _Inspection("", self.own_glob, False).own_import_lines(text)
            if len(found) > 1:
                raise RoutingError("the main Caddyfile imports the managed routes more than once", "precondition")
            if found:
                return RouteResult(changed=False)
            candidate = self._with_import(main.data)
            name = _VALIDATE_PREFIX + token
            self._create(root, name, candidate, preserve=main)
            try:
                self.commands.validate(self.root / name)
            except RoutingError as error:
                if error.kind == "unavailable":
                    raise  # Caddy never ran, so it cannot have rejected the Caddyfile
                raise RoutingError("Caddy rejected the Caddyfile with the managed import", "validation",
                                   detail=error.detail) from None
            finally:
                self._unlink(root, name)
            temp = _TEMP_PREFIX + "Caddyfile-" + token
            self._create(root, temp, candidate, preserve=main)
            os.link("Caddyfile", _PRIOR_MAIN, src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False)
            self._publish(root, temp, "Caddyfile")
            try:
                self.commands.reload()
            except RoutingError as error:
                self._restore(root, "Caddyfile", _PRIOR_MAIN, True, error)
                raise RoutingError("Caddy rejected the managed import; the Caddyfile was restored", "reload",
                                   detail=error.detail) from None
            self._unlink(root, _PRIOR_MAIN)
            return RouteResult(changed=True)
        finally:
            os.close(root)
            os.close(ship)

    # implementation
    def _with_import(self, data: bytes) -> bytes:
        """The main Caddyfile as ``ensure_import`` publishes it: the same text with the managed import appended."""
        text = data.decode("utf-8", "replace")
        return (text + ("" if not text or text.endswith("\n") else "\n") + f"import {self.own_glob}\n").encode()

    def _settle_prior(self, root: int) -> tuple[_CaddyFile | None, bool]:
        """Read the live Caddyfile and drop a leftover ``.prior-Caddyfile`` when it proves where the change stopped.

        The transaction links the live Caddyfile to the prior and then replaces the live file with the candidate
        built from it, so a crash leaves the live file either byte-identical to the prior (the replace never ran) or
        exactly that candidate (the replace ran). Both are settled here: the first drops the prior and lets the
        append run, the second reports the import as already present. A pair that cannot be compared is refused
        rather than guessed at, and the live Caddyfile is never rewritten from the prior.

        Returns the live Caddyfile (``None`` when it is missing) and whether it already carries the managed import.
        """
        try:
            prior = _read_file(root, _PRIOR_MAIN, trusted=True)
        except RoutingError:
            raise _unreconciled_prior() from None
        try:
            main = _read_file(root, "Caddyfile", trusted=True)
        except RoutingError:
            if prior is not None:
                raise _unreconciled_prior() from None
            raise
        if prior is None:
            return main, False
        try:
            redundant = not _Inspection("", self.own_glob, False).own_import_lines(
                prior.data.decode("utf-8", "replace"))
        except RoutingError:
            redundant = False
        if not redundant or main is None or main.data not in (prior.data, self._with_import(prior.data)):
            raise _unreconciled_prior()
        try:
            os.unlink(_PRIOR_MAIN, dir_fd=root)
        except FileNotFoundError:
            pass  # another writer removed it, which is the state this path is trying to reach
        except OSError:
            raise RoutingError("could not remove .prior-Caddyfile; remove it and retry", "interrupted",
                               intervention_required=True) from None
        return main, main.data != prior.data

    def _open(self) -> tuple[int, int]:
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise RoutingError("Caddy directory is unavailable", "precondition") from None
        try:
            ship = os.open("ship", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except OSError:
            os.close(root)
            raise RoutingError("Caddy directory is not bootstrapped", "precondition") from None
        try:
            _directory(root)
            _directory(ship)
        except ValueError:
            os.close(root)
            os.close(ship)
            raise RoutingError("Caddy directories are not trusted", "precondition") from None
        return root, ship

    def _transition(self, subject: dict | str, *, force: bool, application_id: str | None = None) -> RouteResult:
        if not self.lock.held:
            raise RoutingError("host lock is required", "precondition")
        if isinstance(subject, dict):
            registration = validate_registration(subject)
            app = registration["application_id"]
            candidate: bytes | None = render_route(registration["api_domain"], registration["host_port"])
            domain: str | None = registration["api_domain"]
            if application_id is not None and application_id != app:
                raise RoutingError("registration does not match the application", "precondition")
        else:
            app, candidate, domain = validate_app_name(subject), None, None
        root, ship = self._open()
        try:
            return self._run(root, ship, app, domain, candidate, force)
        finally:
            os.close(root)
            os.close(ship)

    def _run(self, root: int, ship: int, app: str, domain: str | None, candidate: bytes | None,
             force: bool) -> RouteResult:
        live, prior = f"{app}.caddy", f"{_PRIOR_PREFIX}{app}"
        names = os.listdir(ship)
        for name in names:
            if name.startswith(_TEMP_PREFIX):
                self._unlink(ship, name)  # never read by Caddy; safe to discard
        interrupted = prior in names
        if interrupted and not force:
            raise RoutingError("an interrupted route change requires reconciliation", "interrupted")

        main = _read_file(root, "Caddyfile", trusted=True)
        if main is None:
            raise RoutingError("the main Caddyfile is missing", "precondition")
        main_text = main.data.decode("utf-8", "replace")
        inspection = _Inspection(domain or "", self.own_glob, self.allow_wildcard_overlap)
        import_lines = inspection.own_import_lines(main_text)
        if len(import_lines) != 1:
            raise RoutingError("the main Caddyfile must import the managed routes exactly once", "precondition")

        # Caddy's "*" glob matches dot-named files, so a stray ".x.caddy" is live configuration too.
        others = sorted(name for name in names if name.endswith(".caddy") and name != live)
        other_texts = {}
        for name in others:
            route = _read_file(ship, name, trusted=True)
            if route is None:
                raise RoutingError("a managed route disappeared during inspection", "uninspectable")
            other_texts[name] = route.data.decode("utf-8", "replace")
        if domain is not None:
            self._scan_hostnames(inspection, main_text, other_texts)

        current = _read_file(ship, live, trusted=True)
        current_data = current.data if current is not None else None
        if not force and current_data == candidate:
            return RouteResult(changed=False)

        temp = None
        if candidate is not None:
            temp = _TEMP_PREFIX + secrets.token_hex(8)
            self._create(ship, temp, candidate)
        try:
            self._validate(root, main, import_lines[0], [f"{self.root}/ship/{name}" for name in others]
                           + ([f"{self.root}/ship/{temp}"] if temp else []))
        except RoutingError:
            if temp:
                self._unlink(ship, temp)
            raise

        changed = current_data != candidate
        if changed:
            if current is not None:
                self._remove_if_present(ship, prior)
                os.link(live, prior, src_dir_fd=ship, dst_dir_fd=ship, follow_symlinks=False)
            self._publish(ship, temp, live)
        elif temp:
            self._unlink(ship, temp)

        try:
            self.commands.reload()
        except RoutingError as reload_error:
            if not changed:
                raise RoutingError("Caddy could not reload; the active configuration may be stale", "reload",
                                   intervention_required=True, detail=reload_error.detail) from None
            self._restore(ship, live, prior, current is not None, reload_error)
            raise RoutingError("Caddy rejected the new route; the previous route was restored", "reload",
                               detail=reload_error.detail) from None
        self._remove_if_present(ship, prior)
        return RouteResult(changed=changed)

    def _scan_hostnames(self, inspection: _Inspection, main_text: str, others: dict[str, str]) -> None:
        for text in others.values():
            inspection.scan(text)
        seen: set[str] = set()
        queue = [(main_text, str(self.root), 0)]
        while queue:
            text, base, depth = queue.pop()
            for argument in inspection.scan(text):
                if "/" not in argument and "*" not in argument and "." not in argument:
                    continue  # snippet name
                pattern = os.path.normpath(argument if os.path.isabs(argument) else os.path.join(base, argument))
                if pattern == self.own_glob:
                    continue
                if depth + 1 > MAX_IMPORT_DEPTH:
                    raise RoutingError("Caddy imports are nested too deeply to inspect", "uninspectable")
                for path in sorted(glob.glob(pattern)):
                    if path in seen:
                        continue
                    seen.add(path)
                    if len(seen) > MAX_IMPORTED_FILES:
                        raise RoutingError("Caddy imports too many files to inspect", "uninspectable")
                    queue.append((_read_path(path), os.path.dirname(path), depth + 1))

    def _validate(self, root: int, main: _CaddyFile, import_line: int, imports: list[str]) -> None:
        lines = main.data.decode("utf-8", "replace").splitlines()
        lines[import_line:import_line + 1] = [f"import {path}" for path in imports]
        name = _VALIDATE_PREFIX + secrets.token_hex(8)
        # The candidate embeds the whole Caddyfile, so it is written with the Caddyfile's own owner and mode.
        self._create(root, name, ("\n".join(lines) + "\n").encode(), preserve=main)
        try:
            try:
                self.commands.validate(self.root / name)
            except RoutingError as error:
                if error.kind == "unavailable":
                    raise  # Caddy never ran, so it cannot have rejected the candidate or the existing file
                baseline_ok = True
                try:
                    self.commands.validate(self.root / "Caddyfile")
                except RoutingError:
                    baseline_ok = False
                if not baseline_ok:
                    raise RoutingError("the existing Caddy configuration is already invalid", "validation",
                                       detail=error.detail) from None
                raise RoutingError("Caddy rejected the candidate route", "validation", detail=error.detail) from None
        finally:
            self._unlink(root, name)

    def _restore(self, ship: int, live: str, prior: str, had_prior: bool, cause: RoutingError) -> None:
        try:
            if had_prior:
                os.replace(prior, live, src_dir_fd=ship, dst_dir_fd=ship)
            else:
                os.unlink(live, dir_fd=ship)
            os.fsync(ship)
        except OSError:
            raise RoutingError("the previous route could not be restored", "reload", intervention_required=True,
                               detail=cause.detail) from None
        try:
            self.commands.reload()
        except RoutingError:
            raise RoutingError("Caddy could not reload after restoring the previous route", "reload",
                               intervention_required=True, detail=cause.detail) from None

    @staticmethod
    def _create(directory: int, name: str, data: bytes, *, preserve: _CaddyFile | None = None) -> None:
        """Create ``name`` exclusively with a mode this module chooses, never one the helper's umask chooses.

        A route file gets ``ROUTE_FILE_MODE``. A copy of the main Caddyfile passes ``preserve`` and keeps the owner,
        group, and mode of the file it was copied from: the Caddyfile routinely holds an ACME DNS token or a
        basic-auth hash, and a copy another account can read would expose it. If that owner cannot be kept the copy
        is refused, so the caller never publishes it.
        """
        mode = ROUTE_FILE_MODE if preserve is None else preserve.mode
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except OSError:
            raise RoutingError("could not write a Caddy file", "precondition") from None
        try:
            if preserve is not None:
                os.fchown(descriptor, preserve.uid, preserve.gid)
            os.fchmod(descriptor, mode)  # the mode given to os.open is masked by the umask; this one is exact
        except OSError:
            os.close(descriptor)
            _discard(directory, name)
            if preserve is not None:
                raise RoutingError("the Caddyfile owner cannot be preserved", "precondition") from None
            raise RoutingError("could not write a Caddy file", "precondition") from None
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.fsync(directory)
        except OSError:
            _discard(directory, name)
            raise RoutingError("could not write a Caddy file", "precondition") from None

    @staticmethod
    def _publish(ship: int, temp: str | None, live: str) -> None:
        try:
            if temp is None:
                os.unlink(live, dir_fd=ship)
            else:
                os.replace(temp, live, src_dir_fd=ship, dst_dir_fd=ship)
            os.fsync(ship)
        except OSError:
            raise RoutingError("could not publish the route; reconciliation is required", "interrupted",
                               intervention_required=True) from None

    @staticmethod
    def _unlink(directory: int, name: str) -> None:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            raise RoutingError("could not remove a temporary Caddy file", "interrupted") from None

    _remove_if_present = _unlink
