"""Idempotent, root-run host bootstrap.

Installs the helper code under a content-addressed version directory, switches
``current`` to it atomically, creates the managed directories, makes Caddy import
the managed route directory, and installs an sshd drop-in that confines
deployment accounts to the forced-command dispatcher. Before the drop-in is
written, the host's sshd unit is discovered and the main sshd configuration is
checked to make sure it reads the drop-in directory. Re-running changes nothing
when everything is already in place.
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import os
import re
import secrets
import stat
from pathlib import Path

from . import __version__
from .layout import Layout
from .osbaseline import OS_RELEASE, check_os
from .release_state import HostLock
from .routing import CaddyCommands, RouteManager, RoutingError, caddy_binary, probe_caddy
from .system import TRUNCATION_MARKER, CommandError, CommandResult, System

CLIENT_MODULES = frozenset({"build.py", "cli.py", "client.py", "commands.py", "configure.py", "detect.py",
                            "github.py", "output.py", "servers.py", "setup.py", "tailscale.py", "template.py",
                            "transport.py", "workflow.py"})
LAUNCHERS = {
    "host_launcher.py": "shiplord.privileged_host",
    "admin_launcher.py": "shiplord.admin",
    "worker_launcher.py": "shiplord.worker",
    "dispatch_launcher.py": "shiplord.dispatch",
}
LAUNCHER_TEMPLATE = (
    "import os\nimport sys\n\n"
    "sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))\n"
    "from {module} import main\n\nmain()\n"
)
MANAGED_MARKER = b"# Managed by Shiplord."  # first bytes of every file bootstrap writes into a shared directory
PLACEHOLDER = b"# Managed by Shiplord. Generated route files live in this directory.\n"
COMPLETE_MARKER = ".complete"  # written last, so its presence marks a version that finished installing
VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}-[0-9a-f]{12}\Z")  # "<version>-<digest prefix>"
KEPT_VERSIONS = 2  # the current version and the one before it, so a rollback target survives an upgrade
DEPLOY_GROUP = "shiplord-deploy"  # the only accounts the sshd drop-in matches; provisioning adds each new one
DEPLOY_MARKER = "shiplord-deploy"  # prefix of the passwd comment that marks an account as a deployment account
SSHD_DROPIN = """\
# Managed by Shiplord. Confines deployment accounts to the forced-command dispatcher.
Match Group {group}
    AuthorizedKeysFile {keys}/%u
    AuthorizedKeysCommand none
    TrustedUserCAKeys none
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
"""
SSHD = "/usr/sbin/sshd"
SYSTEMCTL = "/usr/bin/systemctl"
SSHD_SERVICE_UNITS = ("ssh.service", "sshd.service")
SSHD_SOCKET_UNITS = ("ssh.socket", "sshd.socket")
SSHD_INCLUDE_DEPTH = 8
SSHD_CONFIG = "sshd_config"
GROUPADD, GETENT, USERMOD = "/usr/sbin/groupadd", "/usr/bin/getent", "/usr/sbin/usermod"
ACCOUNT_DATABASE_BYTES = 1 << 20  # room for a whole passwd or group dump; a cut-short one is refused, not guessed
SERVER_PYTHON = "/usr/bin/python3"  # the interpreter the installed launchers run, and the one bootstrap checks
REQUIRED_PYTHON = (3, 11)  # manifest.py imports tomllib, which exists only from Python 3.11
PYTHON_PROBE = "import sys;print(sys.version_info[0],sys.version_info[1])"  # two integers, whatever the locale


class BootstrapError(Exception):
    """Bootstrap could not complete; the message names the failed step."""


def helper_modules(source: Path) -> list[str]:
    """Modules installed on the server: everything except client-only modules."""
    return sorted(path.name for path in source.glob("*.py") if path.name not in CLIENT_MODULES)


def python_version(answer: str) -> tuple[int, int] | None:
    """The major and minor version a ``PYTHON_PROBE`` answer reports, or None when it reports none."""
    fields = answer.split()
    if len(fields) != 2 or not all(field.isdigit() and len(field) <= 2 for field in fields):
        return None
    return int(fields[0]), int(fields[1])


def python_supported(answer: str) -> bool:
    """Whether the interpreter that answered ``PYTHON_PROBE`` is new enough to run the helper."""
    version = python_version(answer)
    return version is not None and version >= REQUIRED_PYTHON


def _chown_root(path: Path | int) -> None:
    if os.geteuid() == 0:
        os.chown(path, 0, 0)


def _ensure_dir(path: Path, mode: int) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise BootstrapError(f"{path} exists and is not a directory")
        if info.st_uid != os.geteuid():
            raise BootstrapError(f"{path} is not owned by the installing account")
        os.chmod(path, mode)
        _chown_root(path)
    except OSError:
        raise BootstrapError(f"could not prepare {path}") from None


def _write_atomic(path: Path, data: bytes, mode: int) -> bool:
    """Write ``path`` atomically; return False without touching it if identical."""
    try:
        if path.is_symlink():
            raise BootstrapError(f"{path} is a symbolic link")
        if path.exists() and path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == mode:
            return False
        temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fchmod(file.fileno(), mode)
                _chown_root(file.fileno())
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        _sync_directory(path.parent)
        return True
    except OSError:
        raise BootstrapError(f"could not write {path}") from None


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _build_payload(source: Path) -> tuple[str, dict[str, bytes]]:
    """Return the version directory name and every file (relative path -> bytes) to install."""
    files: dict[str, bytes] = {}
    for name in helper_modules(source):
        files[f"shiplord/{name}"] = (source / name).read_bytes()
    for launcher, module in LAUNCHERS.items():
        files[launcher] = LAUNCHER_TEMPLATE.format(module=module).encode()
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return f"{__version__}-{digest.hexdigest()[:12]}", files


def _install_version(layout: Layout, name: str, files: dict[str, bytes]) -> bool:
    version = layout.lib / name
    marker = version / ".complete"
    if marker.exists():
        for relative, data in files.items():
            try:
                if (version / relative).read_bytes() != data:
                    raise BootstrapError(f"installed files in {version} do not match their version")
            except OSError:
                raise BootstrapError(f"installed files in {version} are incomplete") from None
        return False
    _ensure_dir(version, 0o755)
    _ensure_dir(version / "shiplord", 0o755)
    for relative, data in files.items():
        _write_atomic(version / relative, data, 0o644)
    _write_atomic(marker, b"", 0o644)
    return True


def _switch_current(layout: Layout, name: str) -> bool:
    current = layout.lib / "current"
    try:
        if current.is_symlink() and os.readlink(current) == name:
            return False
        if current.exists() and not current.is_symlink():
            raise BootstrapError(f"{current} exists and is not a symbolic link")
        link = layout.lib / f".current-{secrets.token_hex(8)}"
        os.symlink(name, link)
        os.replace(link, current)
        _sync_directory(layout.lib)
        return True
    except OSError:
        raise BootstrapError("could not switch the current helper version") from None


def _install_wrappers(layout: Layout) -> bool:
    changed = False
    for target, launcher in launcher_targets(layout).items():
        _ensure_dir(target.parent, 0o755)
        changed |= _write_atomic(target, wrapper_text(layout, launcher), 0o755)
    return changed


def launcher_targets(layout: Layout) -> dict[Path, str]:
    """The launcher script bootstrap installs at each entry-point path."""
    return {layout.host: "host_launcher.py", layout.admin: "admin_launcher.py",
            layout.worker: "worker_launcher.py", layout.dispatch: "dispatch_launcher.py"}


def wrapper_text(layout: Layout, launcher: str) -> bytes:
    """The exact text of the launcher bootstrap installs; uninstall recognises its own by this text."""
    return f'#!/bin/sh\nexec {SERVER_PYTHON} -I -S -B {layout.lib}/current/{launcher} "$@"\n'.encode()


def _helper_directory(layout: Layout) -> int:
    """Open the helper directory, refusing one this account does not own or others may write."""
    try:
        descriptor = os.open(layout.lib, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise BootstrapError("could not open the helper directory") from None
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        os.close(descriptor)
        raise BootstrapError("the helper directory is not trusted")
    return descriptor


def running_version(layout: Layout) -> str | None:
    """The installed version directory this module is executing from, if it is running from one."""
    try:
        relative = Path(__file__).resolve().relative_to(layout.lib.resolve())
    except (OSError, ValueError):
        return None
    return relative.parts[0] if relative.parts else None


def live_versions(directory: int, layout: Layout) -> set[str]:
    """The versions a symlink in the helper directory points at, plus the running one."""
    live = set()
    for name in os.listdir(directory):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISLNK(info.st_mode):
                continue
            target = Path(os.readlink(name, dir_fd=directory))
        except OSError:
            continue
        if target.is_absolute():
            try:
                target = target.relative_to(layout.lib)
            except ValueError:
                continue
        if target.parts:
            live.add(target.parts[0])
    running = running_version(layout)
    if running is not None:
        live.add(running)
    return live


def complete_versions(directory: int) -> list[tuple[int, str]]:
    """Every finished helper version in ``directory``, newest first, as (marker mtime, name).

    Only a directory whose name bootstrap generates and that holds a ``.complete`` marker qualifies, so a
    hand-made directory or an interrupted install is never mistaken for a version to collect.
    """
    versions = []
    for name in os.listdir(directory):
        if not VERSION_PATTERN.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            marker = os.stat(f"{name}/{COMPLETE_MARKER}", dir_fd=directory, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(marker.st_mode):
            versions.append((marker.st_mtime_ns, name))
    return sorted(versions, reverse=True)


def remove_version_tree(parent: int, name: str) -> None:
    """Remove one version directory without following a link, refusing anything this account does not own."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        raise BootstrapError("could not open an old helper version") from None
    try:
        if os.fstat(descriptor).st_uid != os.geteuid():
            raise BootstrapError("an old helper version is not owned by the installing account")
        for child in sorted(os.listdir(descriptor)):
            try:
                info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                raise BootstrapError("could not inspect an old helper version") from None
            if stat.S_ISDIR(info.st_mode):
                remove_version_tree(descriptor, child)
                continue
            try:
                os.unlink(child, dir_fd=descriptor)
            except OSError:
                raise BootstrapError("could not remove an old helper version") from None
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent)
    except OSError:
        raise BootstrapError("could not remove an old helper version") from None


def collect_old_versions(layout: Layout) -> list[str]:
    """Remove helper versions that are neither current nor the previous one; returns what it removed.

    The current version, the version the running helper executes from, and every version a symlink in the
    helper directory points at are always kept, together with the newest other finished version. Repeating
    it removes nothing more.
    """
    directory = _helper_directory(layout)
    try:
        versions = complete_versions(directory)
        keep = live_versions(directory, layout)
        kept = sum(1 for _age, name in versions if name in keep)
        for _age, name in versions:
            if kept >= KEPT_VERSIONS:
                break
            if name in keep:
                continue
            keep.add(name)  # the newest version nothing points at: the rollback target
            kept += 1
        removed = []
        for _age, name in versions:
            if name in keep:
                continue
            remove_version_tree(directory, name)
            removed.append(name)
        if removed:
            os.fsync(directory)
        return sorted(removed)
    finally:
        os.close(directory)


def _installed_sshd_units(system: System) -> set[str]:
    """The sshd unit files systemd knows about; empty when none are installed."""
    result = system.run([SYSTEMCTL, "list-unit-files", "--no-legend", "--no-pager",
                         *SSHD_SERVICE_UNITS, *SSHD_SOCKET_UNITS])
    if result.code != 0:
        return set()
    installed = set()
    for line in result.output.splitlines():
        fields = line.split()
        if fields and fields[0] in SSHD_SERVICE_UNITS + SSHD_SOCKET_UNITS:
            installed.add(fields[0])
    return installed


def sshd_units(system: System) -> tuple[str | None, str | None]:
    """Return the sshd service unit and its socket unit installed on this host, if any."""
    installed = _installed_sshd_units(system)
    service = next((unit for unit in SSHD_SERVICE_UNITS if unit in installed), None)
    socket = next((unit for unit in SSHD_SOCKET_UNITS if unit in installed), None)
    if service is not None and service.replace(".service", ".socket") in installed:
        socket = service.replace(".service", ".socket")
    return service, socket


def _unit_active(system: System, unit: str) -> bool | None:
    """Whether systemd reports ``unit`` active; None when that cannot be determined."""
    try:
        return system.run([SYSTEMCTL, "is-active", "--quiet", unit]).code == 0
    except CommandError:
        return None


def _include_patterns(text: str) -> list[str]:
    """The arguments of every ``Include`` directive in an sshd configuration file."""
    patterns = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # sshd_config separates a keyword from its arguments with a space, a tab, or "="; splitting on a
        # space alone would miss a tab-separated Include, and on "=" alone would truncate a path containing one.
        match = re.fullmatch(r"([A-Za-z]+)(?:\s*=\s*|\s+)(.*)", stripped)
        if match is None:
            continue
        keyword, arguments = match.groups()
        if keyword.lower() == "include":
            patterns.extend(argument.strip('"') for argument in arguments.split())
    return patterns


def _pattern_covers(pattern: str, path: Path) -> bool:
    """Whether a glob pattern covers ``path``; a wildcard does not cross a path separator."""
    parts = Path(pattern).parts
    return len(parts) == len(path.parts) and all(
        fnmatch.fnmatchcase(actual, expected) for actual, expected in zip(path.parts, parts))


def _includes(path: Path, target: Path, base: Path, seen: set[Path], depth: int) -> bool:
    """Whether ``path``, or a file it includes, reads ``target``."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    for pattern in _include_patterns(text):
        candidate = Path(pattern) if Path(pattern).is_absolute() else base / pattern
        if _pattern_covers(str(candidate), target):
            return True
        if depth <= 0:
            continue
        for match in sorted(glob.glob(str(candidate))):
            included = Path(match)
            if included in seen:
                continue
            seen.add(included)
            if _includes(included, target, base, seen, depth - 1):
                return True
    return False


def sshd_dropin_included(layout: Layout) -> bool:
    """Whether the main sshd configuration makes sshd read the Shiplord drop-in file."""
    dropin = layout.sshd_dropin
    config = dropin.parent.parent / SSHD_CONFIG
    return _includes(config, dropin, config.parent, {config}, SSHD_INCLUDE_DEPTH)


def _require_sshd_include(layout: Layout) -> None:
    if sshd_dropin_included(layout):
        return
    raise BootstrapError(
        "the main sshd configuration does not include the Shiplord drop-in: "
        f"add `Include {layout.sshd_dropin.parent.name}/*.conf` and re-run bootstrap")


def _sshd_preflight(layout: Layout, system: System) -> tuple[str, str | None]:
    """Refuse a host whose sshd policy cannot be verified, before anything is written."""
    _require_sshd_include(layout)
    try:
        unit, socket = sshd_units(system)
    except CommandError:
        raise BootstrapError("could not run sshd checks") from None
    if unit is None:
        raise BootstrapError("could not find the sshd service unit (looked for ssh.service and sshd.service)")
    return unit, socket


def _account_database(system: System, *arguments: str) -> CommandResult:
    """A ``getent`` answer, read with room for a whole listing rather than the default short window."""
    try:
        return system.run([GETENT, *arguments], max_output=ACCOUNT_DATABASE_BYTES)
    except CommandError:
        raise BootstrapError("could not read the account database") from None


def _untruncated(result: CommandResult) -> str:
    """The output of a listing, refusing one that was cut short: a missed account would stay unconfined."""
    if result.output.startswith(TRUNCATION_MARKER):
        raise BootstrapError("the account database is too large to inspect")
    return result.output


def _group_members(system: System, group: str) -> set[str] | None:
    """The members of ``group``, or None when the group does not exist."""
    result = _account_database(system, "group", group)
    if result.code != 0:
        return None
    fields = _untruncated(result).strip().split(":")
    if len(fields) < 4 or fields[0] != group:
        raise BootstrapError("could not read the account database")
    return {name for name in fields[3].split(",") if name}


def _deployment_accounts(system: System) -> list[str]:
    """Every account this host marks as a Shiplord deployment account, by its passwd comment."""
    result = _account_database(system, "passwd")
    if result.code != 0:
        raise BootstrapError("could not read the account database")
    accounts = []
    for line in _untruncated(result).splitlines():
        fields = line.split(":")
        if len(fields) == 7 and fields[4].startswith(DEPLOY_MARKER + "-"):
            accounts.append(fields[0])
    return accounts


def _ensure_deployment_group(system: System) -> bool:
    """Create the group the sshd policy matches on and put every existing deployment account in it.

    The policy is replaced as a whole, so an account created under an earlier policy would otherwise fall back to
    the host's default sshd policy. This runs before the drop-in is written and reloaded.
    """
    created = False
    members = _group_members(system, DEPLOY_GROUP)
    if members is None:
        try:
            result = system.run([GROUPADD, "--system", DEPLOY_GROUP])
        except CommandError:
            raise BootstrapError("could not create the deployment group") from None
        if result.code != 0:
            raise BootstrapError("could not create the deployment group")
        created, members = True, set()
    changed = False
    for account in _deployment_accounts(system):
        if account in members:
            continue
        try:
            result = system.run([USERMOD, "--append", "--groups", DEPLOY_GROUP, account])
        except CommandError:
            raise BootstrapError("could not add a deployment account to the deployment group") from None
        if result.code != 0:
            raise BootstrapError("could not add a deployment account to the deployment group")
        changed = True
    return created or changed


def _install_sshd(layout: Layout, system: System, unit: str, socket: str | None) -> bool:
    path = layout.sshd_dropin
    desired = SSHD_DROPIN.format(keys=layout.authorized_keys, group=DEPLOY_GROUP).encode()
    _ensure_dir(path.parent, 0o755)
    previous = path.read_bytes() if path.exists() else None
    if previous == desired:
        return False
    _write_atomic(path, desired, 0o644)
    try:
        result = system.run([SSHD, "-t"])
        if result.code != 0:
            raise BootstrapError("sshd rejected the Shiplord configuration: " + (result.output.strip() or "no output"))
        socket_activated = (socket is not None and _unit_active(system, socket) is True
                            and _unit_active(system, unit) is False)
        if not socket_activated:
            result = system.run([SYSTEMCTL, "try-reload-or-restart", unit])
            if result.code != 0:
                raise BootstrapError(f"sshd could not reload {unit}: " + (result.output.strip() or "no output"))
    except (BootstrapError, CommandError) as error:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _write_atomic(path, previous, 0o644)
        if isinstance(error, CommandError):
            raise BootstrapError("could not run sshd checks") from None
        raise
    return True


def _caddy_binary(caddy: CaddyCommands) -> str:
    """The Caddy path a refusal names: the runner's own when it has one, else the documented resolution."""
    configured = getattr(caddy, "caddy", None)
    return configured if isinstance(configured, str) and configured else caddy_binary()


def _caddy_preflight(caddy: CaddyCommands) -> None:
    """Refuse a host whose Caddy cannot run, before any Shiplord directory or helper is written."""
    try:
        probe_caddy(caddy)
    except RoutingError as error:
        if error.kind == "unavailable":
            raise BootstrapError(
                "Caddy is not installed or cannot be started: install the caddy package or put the binary at "
                f"{_caddy_binary(caddy)}") from None
        raise BootstrapError(
            f"Caddy rejected a minimal configuration ({error.kind}): check the Caddy installation") from None


def bootstrap(layout: Layout, source: Path, system: System, caddy: CaddyCommands, *,
              lock_timeout: float = 20) -> dict:
    """Install or update the host, refusing an unsupported operating system first.

    Safe to repeat; returns which steps changed something.
    """
    if layout.prefix == Path("/") and os.geteuid() != 0:
        raise BootstrapError("bootstrap must run as root")
    supported = check_os(layout.path(OS_RELEASE))
    if not supported["ok"]:
        raise BootstrapError(supported["message"])
    _caddy_preflight(caddy)
    unit, socket = _sshd_preflight(layout, system)
    changed: dict[str, bool] = {}
    for directory, mode in ((layout.path("/etc/shiplord"), 0o755), (layout.registrations, 0o700),
                            (layout.environments, 0o700), (layout.authorized_keys, 0o755),
                            (layout.var, 0o755), (layout.app_state, 0o700), (layout.jobs, 0o700),
                            (layout.data, 0o755), (layout.releases, 0o755), (layout.lib, 0o755)):
        _ensure_dir(directory, mode)

    descriptor = os.open(layout.var, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock = HostLock(descriptor)
        with lock.acquire(timeout=lock_timeout):
            ship = layout.caddy_root / "ship"
            if not layout.caddy_root.is_dir():
                raise BootstrapError("Caddy is not installed: its configuration directory is missing")
            _ensure_dir(ship, 0o755)
            changed["caddy_placeholder"] = _write_atomic(ship / "00-shiplord.caddy", PLACEHOLDER, 0o644)
            try:
                changed["caddy_import"] = RouteManager(layout.caddy_root, lock, caddy).ensure_import().changed
            except RoutingError as error:
                raise BootstrapError(f"Caddy integration failed ({error.kind}): {error}") from None
            name, files = _build_payload(source)
            changed["code"] = _install_version(layout, name, files)
            changed["current"] = _switch_current(layout, name)
            changed["wrappers"] = _install_wrappers(layout)
            changed["sshd_group"] = _ensure_deployment_group(system)
            changed["sshd"] = _install_sshd(layout, system, unit, socket)
            collected, warnings = _collect_old_versions(layout)
    finally:
        os.close(descriptor)
    return {"version": name, "changed": changed, "collected": collected,
            **({"warnings": warnings} if warnings else {})}


def _collect_old_versions(layout: Layout) -> tuple[list[str], list[str]]:
    """Garbage-collect old helper versions after a successful install; a cleanup failure is a warning.

    The host is installed and serving by the time this runs, so a version that cannot be removed must not turn a
    successful bootstrap into a reported failure. It is reported instead, and the next bootstrap retries it.
    """
    try:
        return collect_old_versions(layout), []
    except BootstrapError as error:
        return [], [str(error)]
    except OSError:
        return [], ["old helper versions could not be collected"]


def main() -> None:
    """Entry point for ``sudo python3 -I -S -c`` from an unpacked client upload: one frame out."""
    import sys

    from .framing import write_frame
    from .routing import SubprocessCaddy
    from .system import HostSystem
    try:
        result = bootstrap(Layout(), Path(__file__).resolve().parent, HostSystem(), SubprocessCaddy())
        response, code = {"status": "ok", "result": result}, 0
    except BootstrapError as error:
        response, code = {"status": "error", "message": str(error)}, 1
    write_frame(sys.stdout.buffer, response)
    sys.stdout.buffer.flush()
    raise SystemExit(code)
