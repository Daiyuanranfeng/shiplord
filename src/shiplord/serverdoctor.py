"""Read-only health report of a bootstrapped server, run as root through the admin entry point."""

from __future__ import annotations

import os
import shutil
from http.client import HTTPException
from pathlib import Path

from .environment import EnvironmentError_, load_environment_file
from .installer import DEPLOY_GROUP, SSHD_DROPIN, sshd_dropin_included, sshd_units
from .layout import Layout
from .native import NativeRuntime
from .osbaseline import OS_RELEASE, check_os
from .probes import http_get
from .provision import GETENT
from .registry import Registry
from .release_state import HostLock, ReleaseState, StateError
from .routing import RouteManager, RoutingError, SubprocessCaddy, _Inspection  # the same tokenizer that decides whether the managed import is present
from .runtime import DOCKER_ROOT, MIN_FREE_BYTES, DockerRuntime, RuntimeFailure
from .system import CommandError, System

PORTS = (80, 443)
SSHD_CONFINEMENT = {  # the settings the managed drop-in must impose on a ship-* account
    "authenticationmethods": "publickey",
    "authorizedkeyscommand": "none",
    "trustedusercakeys": "none",
    "passwordauthentication": "no",
    "kbdinteractiveauthentication": "no",
    "permittty": "no",
    "allowtcpforwarding": "no",
    "allowstreamlocalforwarding": "no",
    "allowagentforwarding": "no",
    "x11forwarding": "no",
    "permittunnel": "no",
}
SYSTEMCTL = "/usr/bin/systemctl"
SSHD = "/usr/sbin/sshd"


def _check(ok: bool, *, next_step: str, **details) -> dict:
    """A named check result; a failing check always carries the remedy shown in text output."""
    check = {"ok": ok, **details}
    if not ok:
        check["next_step"] = next_step
    return check


def _run(system: System, argv: list[str], *, max_output: int | None = None) -> tuple[bool, str]:
    try:
        result = system.run(argv, timeout=30, **({} if max_output is None else {"max_output": max_output}))
    except CommandError:
        return False, "could not run"
    return result.code == 0, result.output.strip()


def _free(path: Path) -> dict:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return {"ok": False, "path": str(path), "error": "could not measure"}
    return {"ok": free >= MIN_FREE_BYTES, "path": str(path), "free_mib": free // 2**20}


def _admin_api_reachable() -> bool:
    try:
        return http_get("127.0.0.1", 2019, "/config/", timeout=2.0, read_body=False)[0] == 200
    except (OSError, HTTPException):
        return False


# --- ports ----------------------------------------------------------------

def _listening_ports(system: System) -> dict[int, list[str]] | None:
    """The local addresses listening on each watched port; None when the socket table cannot be read."""
    ok, output = _run(system, ["ss", "-H", "-ltn"], max_output=64 * 1024)  # a busy host has many sockets
    if not ok:
        return None
    listening: dict[int, list[str]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:  # state, receive queue, send queue, local address, peer address
            continue
        address, _, port = fields[3].rpartition(":")
        if port.isdigit() and int(port) in PORTS:
            listening.setdefault(int(port), []).append(address)
    return listening


def _beyond_loopback(address: str) -> bool:
    """Whether a listening address accepts connections from more than the loopback interface."""
    address = address.strip("[]")
    return address != "::1" and not address.startswith("127.")


def _ports_check(system: System, *, required: bool) -> dict:
    listening = _listening_ports(system)
    ports = []
    for port in PORTS:
        addresses = [] if listening is None else listening.get(port, [])
        ports.append({"port": port, "listening": bool(addresses),
                      "external": any(_beyond_loopback(address) for address in addresses)})
    if listening is None:
        return _check(False, next_step="install iproute2 so `ss` can report listeners, then rerun doctor",
                      required=required, ports=ports, error="could not inspect listening sockets")
    # A cloud firewall or NAT can still block these ports; the check proves the local bind, not the path.
    if required:
        return _check(all(entry["external"] for entry in ports),
                      next_step="start Caddy and allow inbound TCP 80 and 443 in the host and cloud firewall",
                      required=True, ports=ports)
    # Nothing is deployed, so Caddy has no HTTPS site and does not bind 443 yet; a listener on loopback only is
    # still a fault, because the next deploy would be unreachable.
    loopback_only = any(entry["listening"] and not entry["external"] for entry in ports)
    return _check(not loopback_only, next_step="make Caddy listen on all interfaces rather than loopback",
                  required=False, ports=ports)


# --- sshd -----------------------------------------------------------------

def _effective_sshd(system: System, user: str) -> dict[str, str] | None:
    """The sshd settings that apply to ``user``; None when sshd cannot report them."""
    ok, output = _run(system, [SSHD, "-T", "-C", f"user={user},host=localhost,addr=127.0.0.1"],
                      max_output=64 * 1024)  # the effective policy is well over the default output cap
    if not ok:
        return None
    policy = {}
    for line in output.splitlines():
        keyword, _, value = line.partition(" ")
        if keyword:
            policy[keyword.lower()] = value.strip()
    return policy


def _sshd_check(layout: Layout, system: System, user: str | None) -> dict:
    dropin_ok = layout.sshd_dropin.is_file() and layout.sshd_dropin.read_text() == SSHD_DROPIN.format(
        keys=layout.authorized_keys, group=DEPLOY_GROUP)
    valid, _ = _run(system, [SSHD, "-t"])
    try:
        unit, socket = sshd_units(system)
    except CommandError:
        unit, socket = None, None
    included = sshd_dropin_included(layout)
    keys = f"{layout.authorized_keys}/%u"
    # `sshd -T` resolves group membership, so only a real deployment account can prove the Match block applies.
    policy = None if user is None else _effective_sshd(system, user)
    confined = None if user is None else (
        policy is not None and policy.get("authorizedkeysfile") == keys and all(
            policy.get(keyword) == value for keyword, value in SSHD_CONFINEMENT.items()))
    # The Match block must not confine unrelated accounts; a synthetic name outside ship-* proves that.
    other = _effective_sshd(system, "shiplord-doctor-probe")
    isolated = other is not None and other.get("authorizedkeysfile") != keys
    service_active = any(unit is not None and _run(system, [SYSTEMCTL, "is-active", "--quiet", candidate])[0]
                         for candidate in (unit, socket) if candidate is not None)
    return _check(dropin_ok and valid and included and unit is not None and confined is not False
                  and isolated and service_active,
                  next_step="run `shiplord server bootstrap <alias>` to restore sshd confinement, then reload sshd",
                  dropin_current=dropin_ok, config_valid=valid, dropin_included=included, unit=unit,
                  service_active=service_active, policy_in_effect=confined,
                  confined_to_deployment_accounts=isolated)


# --- applications ---------------------------------------------------------

def _route_state(layout: Layout, lock: HostLock, registration: dict) -> str:
    try:
        return RouteManager(layout.caddy_root, lock, SubprocessCaddy()).inspect_route(registration)
    except (OSError, RoutingError):
        return "uninspectable"


def _environment_state(layout: Layout, registration: dict) -> str:
    """Whether the runtime environment file is present and usable; values are never read out."""
    app = registration["application_id"]
    if registration["runtime"] == "native":
        return "ok" if (layout.environments / app / "native.env").is_file() else "absent"
    try:
        return "absent" if load_environment_file(layout.environments / app / "app.env") is None else "ok"
    except EnvironmentError_:
        return "invalid"


def _account_uid(system: System, user: str) -> int | None:
    ok, output = _run(system, [GETENT, "passwd", user])
    fields = output.split(":")
    return int(fields[2]) if ok and len(fields) == 7 and fields[2].isdigit() else None


def _running(system: System, layout: Layout, registration: dict, release: dict) -> bool | None:
    runtime = DockerRuntime(system, layout) if registration["runtime"] == "docker" else NativeRuntime(system, layout)
    try:
        return runtime.is_running(registration, release)
    except (RuntimeFailure, OSError):
        return None


def _next_step(mismatches: list[str]) -> str:
    steps = []
    if "account" in mismatches:
        steps.append("re-provision the application to restore its deployment account")
    if {"release", "runtime", "route"} & set(mismatches):
        steps.append("redeploy the application to restore its release, runtime, and route")
    if "environment" in mismatches:
        steps.append("fix the environment file with `shiplord env set`, then redeploy")
    return "; ".join(steps)


def _consistency(layout: Layout, system: System, lock: HostLock, registration: dict, state: dict) -> dict:
    """Compare one application's registration, committed release, runtime, route, and environment."""
    app = registration["application_id"]
    release = state["current"]
    deployed = release is not None
    route = _route_state(layout, lock, registration)
    environment = _environment_state(layout, registration)
    running = None if release is None else _running(system, layout, registration, release)
    mismatches = []
    if _account_uid(system, registration["deployment_user"]) != registration["deployment_uid"]:
        mismatches.append("account")
    if deployed and (release["configuration"]["http"]["api_domain"] != registration["api_domain"]
                     or release["configuration"]["runtime"] != registration["runtime"]):
        mismatches.append("release")
    if deployed and running is not True:
        mismatches.append("runtime")
    if route != ("identical" if deployed else "absent"):
        mismatches.append("route")
    if environment == "invalid" or (deployed and registration["runtime"] == "native" and environment != "ok"):
        mismatches.append("environment")
    entry = {"application": app, "ok": not mismatches, "commit": None if release is None else release["commit"],
             "route": route, "running": running, "environment": environment, "mismatches": mismatches}
    if mismatches:
        entry["next_step"] = _next_step(mismatches)
    return entry


def _unreadable_consistency(app: str) -> dict:
    return {"application": app, "ok": False, "commit": None, "route": "uninspectable", "running": None,
            "environment": "uninspectable", "mismatches": ["state"],
            "next_step": "inspect the application state as an administrator"}


def application_inventory(layout: Layout, lock: HostLock) -> dict:
    """List registered applications and their recorded release state, without contacting app runtimes."""
    try:
        descriptor = os.open(layout.registrations, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            registry = Registry(descriptor, lock)
            registrations = registry.list()
            ignored = list(registry.stray)
        finally:
            os.close(descriptor)
    except (OSError, StateError):
        return {"ok": False, "applications": [], "error": "application registry is unreadable"}

    applications = []
    for registration in registrations:
        app = registration["application_id"]
        entry = {"application": app, "domain": registration["api_domain"], "runtime": registration["runtime"],
                 "host_port": registration["host_port"], "release_recorded": None,
                 "unfinished_transaction": None}
        try:
            state_fd = os.open(layout.app_state / app, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                store = ReleaseState(app, state_fd, lock)
                state, journal = store.read_state(), store.read_journal()
            finally:
                os.close(state_fd)
            entry["release_recorded"] = state["current"] is not None
            entry["unfinished_transaction"] = (None if journal is None else
                                                {"request_id": journal["request_id"], "phase": journal["phase"]})
            entry["ok"] = True
        except (OSError, StateError):
            entry.update(ok=False, error="application state is unreadable")
        applications.append(entry)
    result = {"ok": all(entry["ok"] for entry in applications), "applications": applications}
    if ignored:
        result["ignored_files"] = ignored
    return result


def server_doctor(layout: Layout, system: System, lock: HostLock) -> dict:
    checks: dict[str, dict] = {"os": check_os(layout.path(OS_RELEASE))}
    registrations, registry_error, ignored = [], None, []
    try:
        descriptor = os.open(layout.registrations, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            registry = Registry(descriptor, lock)
            registrations = registry.list()
            ignored = list(registry.stray)
        finally:
            os.close(descriptor)
    except (OSError, StateError):
        registry_error = "application registry is unreadable"

    current = layout.lib / "current"
    checks["helper"] = _check(current.is_symlink() and layout.host.exists() and layout.worker.exists()
                              and layout.dispatch.exists(),
                              next_step="run `shiplord server bootstrap <alias>` to reinstall the helper",
                              version=os.readlink(current) if current.is_symlink() else None)
    try:
        main = (layout.caddy_root / "Caddyfile").read_text()
        wired = len(_Inspection("", f"{layout.caddy_root}/ship/*.caddy", False).own_import_lines(main)) == 1
    except Exception:  # an unreadable or uninspectable Caddyfile is a failed check, not a crash
        wired = False
    active, _ = _run(system, [SYSTEMCTL, "is-active", "--quiet", "caddy"])
    checks["caddy"] = _check(wired and active,
                             next_step="run `shiplord server bootstrap <alias>` to restore the managed import, then start Caddy",
                             import_present=wired, service_active=active,
                             admin_api=_admin_api_reachable())  # informational: deploys cannot verify routing without it
    user = next((registration["deployment_user"] for registration in registrations), None)
    checks["sshd"] = _sshd_check(layout, system, user)
    available, version = _run(system, ["/usr/bin/docker", "info", "--format", "{{.ServerVersion}}"])
    required = any(registration["runtime"] == "docker" for registration in registrations)
    checks["docker"] = _check(available or not required,
                              next_step="start Docker, or deprovision the Docker application that needs it",
                              required=required, available=available, version=version if available else None)
    paths = [layout.var, layout.releases] + ([DOCKER_ROOT] if layout.prefix == Path("/") and DOCKER_ROOT.exists() else [])
    checks["disk"] = _check(all(entry["ok"] for entry in map(_free, paths)),
                            next_step="free disk space on the Shiplord paths before the next deploy",
                            paths=[_free(path) for path in paths])
    applications, consistency, problems = [], [], 0
    for registration in registrations:
        app = registration["application_id"]
        entry = {"application": app, "runtime": registration["runtime"], "host_port": registration["host_port"]}
        try:
            state_fd = os.open(layout.app_state / app, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                store = ReleaseState(app, state_fd, lock)
                state, journal = store.read_state(), store.read_journal()
            finally:
                os.close(state_fd)
            entry.update(ok=journal is None, deployed=state["current"] is not None,
                         unfinished_transaction=None if journal is None else journal["phase"])
        except (OSError, StateError):
            entry.update(ok=False, error="state is unreadable")
            consistency.append(_unreadable_consistency(app))
        else:
            consistency.append(_consistency(layout, system, lock, registration, state))
        problems += 0 if entry["ok"] else 1
        applications.append(entry)
    if registry_error is not None:
        checks["applications"] = _check(False, next_step="inspect the application registry as an administrator",
                                        applications=[], ignored_files=ignored, error=registry_error)
        checks["consistency"] = _check(False, next_step="inspect the application registry as an administrator",
                                       applications=[], error=registry_error)
    else:
        checks["applications"] = _check(problems == 0,
                                        next_step="run `shiplord recover` for the application with an unfinished transaction",
                                        applications=applications, ignored_files=ignored)
        checks["consistency"] = _check(all(entry["ok"] for entry in consistency),
                                       next_step="redeploy or re-provision the applications that report a mismatch",
                                       applications=consistency)
    # Only a deployed application needs Caddy's HTTPS listener; a freshly bootstrapped host binds just :80.
    checks["ports"] = _ports_check(system, required=any(entry.get("deployed") for entry in applications))
    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}
