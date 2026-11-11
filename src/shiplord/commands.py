"""Command logic for the CLI. Each function returns ``(exit_code, result)`` and prints nothing."""

from __future__ import annotations

import hashlib
import http.client
import re
import shlex
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import build
from .environment import EnvironmentError_, parse_environment
from .client import AdminClient, ClientError, HelperClient, new_request_id, submit_and_wait
from .jobs import valid_job_id
from .keys import KeyError_, parse_public_key
from .native import elf_architecture
from .manifest import GENERIC_DOCKER_FEATURE, MAX_READINESS_SECONDS
from .runtime import RuntimeFailure
from .servers import ServerError
from .shutdown import FEATURE as SHUTDOWN_FEATURE, FORCE_STOP_FEATURE
from .storage import FEATURE as STORAGE_FEATURE
from .protocol import IncompatibleProtocol, PROTOCOL_VERSION
from .transport import TransportError, probe_capabilities

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
DEFAULT_KEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")
MINIMUM_INVENTORY_HELPER_VERSION = "0.2.0"
FLEET_PROBE_TIMEOUT_SECONDS = 5.0
# Only stable SemVer releases have an unambiguous compatibility ordering.
_STABLE_RELEASE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                             r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
_MINIMUM_INVENTORY = tuple(int(part) for part in MINIMUM_INVENTORY_HELPER_VERSION.split("."))


class CommandError(Exception):
    """A command cannot proceed; ``code`` is the CLI exit status (2 invalid input, 3 prerequisite)."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def read_public_key(path: Path | None, home: Path | None = None) -> tuple[str, str]:
    """The public key to register: an explicit file, else the first standard key in ~/.ssh."""
    home = home or Path.home()
    candidates = [path] if path else [home / ".ssh" / name for name in DEFAULT_KEYS]
    for candidate in candidates:
        if candidate.is_file():
            text = candidate.read_text().strip()
            try:
                key = parse_public_key(text)
            except KeyError_ as error:
                raise CommandError(f"{candidate} is not a usable public key: {error}") from None
            return text, key.fingerprint
    raise CommandError("no public key found; pass --public-key (a file such as ~/.ssh/id_ed25519.pub)", 3)


def image_repository_for(manifest: dict, override: str | None) -> str | None:
    """The one image repository the server will accept, ``ghcr.io/<owner>/<repository>`` in lower case."""
    if manifest["runtime"] != "docker":
        return None
    if override:
        return override
    if "github" not in manifest:
        raise CommandError("the image repository is unknown: add a [github] section to ship.toml or pass --image-repository")
    return "ghcr.io/" + manifest["github"]["repository"].lower()


def commit_sequence(commit: str, override: int | None, directory: Path | None = None) -> int:
    """The release ordering key: the commit's committer timestamp, so CI and local deploys agree."""
    if override is not None:
        if override < 0:
            raise CommandError("--sequence must not be negative")
        return override
    try:
        result = subprocess.run(["git", "show", "-s", "--format=%ct", commit], capture_output=True, text=True, timeout=30,
                                cwd=directory or Path.cwd(), check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CommandError("could not run git to order the release; pass --sequence", 3) from None
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise CommandError("could not read the commit time from git; pass --sequence", 2)
    return int(result.stdout.strip())


def provision_application(manifest: dict, admin: AdminClient, *, public_key: Path | None = None,
                          image_repository: str | None = None, key_label: str | None = None,
                          max_memory_mb: int | None = None, update: bool = False) -> tuple[int, dict]:
    text, fingerprint = read_public_key(public_key)
    label = key_label or "local-" + fingerprint.split(":", 1)[1][:8].lower().replace("+", "x").replace("/", "y")
    arguments = {"runtime": manifest["runtime"], "api_domain": manifest["http"]["api_domain"],
                 "image_repository": image_repository_for(manifest, image_repository), "public_key": text,
                 "key_label": label}
    if max_memory_mb is not None:
        arguments["max_memory_mb"] = max_memory_mb  # the ceiling a deploy request may ask for (default 4096)
    if update:
        arguments["update"] = True
    result = admin.call("provision", manifest["name"], arguments)
    host = admin.target.partition("@")[2]
    if update:
        if not result.get("updated"):
            next_step = "The application already matches the manifest; nothing changed."
        elif "api_domain" not in result.get("changes", {}):
            next_step = "The memory ceiling is registered; it applies to the next deployment."
        elif result.get("route_changed"):
            next_step = "The hostname is registered and the public route was moved; point its DNS record at the server."
        else:
            next_step = ("The hostname is registered; point its DNS record at the server, then run `shiplord deploy` "
                         "to move the route.")
    else:
        next_step = "Point DNS for the API hostname at the server, then run `shiplord deploy`."
    return 0, {"ok": True, **result, "deploy_login": f"{result['deployment_user']}@{host}", "key_label": label,
               "next": next_step}


def deprovision_application(application_id: str, admin: AdminClient, *, retire: bool = False) -> tuple[int, dict]:
    """Remove the application's server-side access; with ``retire``, first stop it and take down its route."""
    result = admin.call("deprovision", application_id, {"retire": True} if retire else {})
    return 0, {"ok": True, **result, "note": "Application data is never deleted; remove it by hand when you are sure."}


def set_environment(manifest: dict, admin: AdminClient, operation: str, text: str | None = None) -> tuple[int, dict]:
    arguments = {"operation": operation}
    if operation == "set":
        try:  # validated here too, so mistakes are caught before any SSH connection is made
            values = parse_environment(text or "")
        except EnvironmentError_ as error:
            raise CommandError(str(error)) from None
        if not values:
            raise CommandError("the file defines no variables; use `shiplord env clear` to remove the environment")
        arguments["text"] = text
    result = admin.call("environment", manifest["name"], arguments)
    note = ("Takes effect when the application next starts; run `shiplord restart` to apply it to the running release."
            if operation in ("set", "clear") else None)
    return 0, {"ok": True, **result, **({"note": note} if note else {})}


def diff_environment(manifest: dict, admin: AdminClient, text: str) -> tuple[int, dict]:
    """Preview the key-name changes ``env set`` would make; only the read-only key list is requested."""
    try:  # validated here too, so a bad file is caught before any SSH connection is made
        values = parse_environment(text)
    except EnvironmentError_ as error:
        raise CommandError(str(error)) from None
    if not values:
        raise CommandError("the file defines no variables; use `shiplord env clear` to remove the environment")
    _code, listing = set_environment(manifest, admin, "keys")
    current, proposed = set(listing.get("keys") or []), set(values)
    return 0, {"ok": True, "operation": "diff", "added": sorted(proposed - current),
               "removed": sorted(current - proposed),
               "note": "Key names only; a name present in both files may still have a different value."}


def native_artifact(binary: Path) -> dict:
    """Describe a local binary (size, checksum) after checking that it is a Linux x86-64 ELF executable."""
    try:
        size = binary.stat().st_size
        with binary.open("rb") as file:
            header = file.read(64)
            digest = hashlib.sha256(header)
            while chunk := file.read(1 << 20):
                digest.update(chunk)
    except OSError:
        raise CommandError(f"could not read {binary}") from None
    if not 1 <= size <= 1024 * 1024 * 1024:
        raise CommandError("the binary must be between 1 byte and 1 GiB")
    try:
        architecture = elf_architecture(header)
    except RuntimeFailure as failure:
        raise CommandError(f"{failure}; build a Linux binary for the server's architecture (for example in CI)") from None
    return {"kind": "native", "sha256": digest.hexdigest(), "size": size, "architecture": architecture}


_LOGIN_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_LOGIN_TOKEN = re.compile(r"[\x21-\x7e]{1,512}\Z")


def validate_registry_login(registry: str, username: str, token: str) -> None:
    """Reject bad input before any server alias is read or any connection is made."""
    if registry != "ghcr.io":
        raise CommandError("registry logins are supported for ghcr.io")
    if not _LOGIN_USER.fullmatch(username):
        raise CommandError("the username may contain letters, digits, dots, dashes, and underscores")
    if not _LOGIN_TOKEN.fullmatch(token):
        raise CommandError("the token must be 1-512 printable characters without spaces or line breaks")


def registry_login(manifest: dict, admin: AdminClient, *, registry: str, username: str, token: str) -> tuple[int, dict]:
    """Store a read-only registry login for this application on the server (the token travels only in the frame)."""
    validate_registry_login(registry, username, token)
    result = admin.call("registry", manifest["name"], {"operation": "login", "registry": registry, "username": username,
                                                       "token": token})
    return 0, {"ok": True, **result, "note": "Used for pulling this application's private images. Use a read-only token."}


def registry_other(manifest: dict, admin: AdminClient, operation: str, registry: str = "ghcr.io") -> tuple[int, dict]:
    arguments = {"operation": operation}
    if operation == "logout":
        arguments["registry"] = registry
    return 0, {"ok": True, **admin.call("registry", manifest["name"], arguments)}


def _deploy_arguments(manifest: dict, artifact: str | dict, sequence: int, allow_older: bool) -> dict:
    if isinstance(artifact, str):
        if manifest["runtime"] != "docker":
            raise CommandError("this manifest is for a native binary; use --binary", 2)
        if not _IMAGE.fullmatch(artifact):
            raise CommandError("--image must be an immutable reference: registry/name@sha256:<digest>")
        artifact = {"kind": "docker", "image": artifact}
    elif manifest["runtime"] != "native":
        raise CommandError("this manifest is for a Docker image; use --image", 2)
    arguments = {"sequence": sequence, "artifact": artifact, "configuration": manifest}
    if allow_older:
        arguments["allow_older"] = True
    return arguments


def check_public_https(domain: str, path: str, *, timeout: float = 60, interval: float = 5,
                       sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                       context: ssl.SSLContext | None = None, port: int = 443, host: str | None = None) -> dict:
    """Does the public name answer 200 over verified TLS? Retries while DNS or certificates settle."""
    deadline, last = clock() + timeout, {"ok": False, "status": None, "error": "not_checked"}
    context = context or ssl.create_default_context()
    address = host or domain

    class Connection(http.client.HTTPSConnection):
        def connect(self) -> None:  # dial ``address`` but always verify the certificate against ``domain``
            raw = socket.create_connection((address, port), timeout=10)
            self.sock = context.wrap_socket(raw, server_hostname=domain)

    while True:
        try:
            connection = Connection(domain, port, timeout=10, context=context)
            try:
                connection.request("GET", path)
                status = connection.getresponse().status
            finally:
                connection.close()
            last = {"ok": status == 200, "status": status, "error": None if status == 200 else "unexpected_status"}
            if last["ok"]:
                return last
        except ssl.SSLCertVerificationError:
            last = {"ok": False, "status": None, "error": "certificate_not_trusted"}
        except socket.gaierror:
            last = {"ok": False, "status": None, "error": "dns_unresolved"}
        except (OSError, http.client.HTTPException):
            last = {"ok": False, "status": None, "error": "connection_failed"}
        if clock() >= deadline:
            return last
        sleep(interval)


def validate_deploy_inputs(manifest: dict, artifact: str | Path, commit: str) -> dict:
    """Reject malformed input before any server alias is read or any connection is made; returns the artifact."""
    if not _COMMIT.fullmatch(commit):
        raise CommandError("--commit must be a full 40-character lowercase commit SHA")
    if isinstance(artifact, Path) and manifest["runtime"] != "native":  # before reading the file
        raise CommandError("this manifest is for a Docker image; use --image", 2)
    resolved = native_artifact(artifact) if isinstance(artifact, Path) else artifact
    _deploy_arguments(manifest, resolved, 0, False)
    return resolved


def uses_shutdown_policy(manifest: dict) -> bool:
    return any(key in manifest["deploy"] for key in ("shutdown_timeout_seconds", "shutdown_timeout_action"))


def require_feature_support(manifest: dict, client: HelperClient) -> None:
    """Refuse settings an older helper would ignore, before submitting a plan or a job."""
    required = {}
    if "storage" in manifest:
        required[STORAGE_FEATURE] = "[storage]"
    if uses_shutdown_policy(manifest):
        required[SHUTDOWN_FEATURE] = "shutdown_timeout_*"
    if manifest["template"] == "generic":
        required[GENERIC_DOCKER_FEATURE] = "template = generic"
    if not required:
        return
    supported = client.call("capabilities")["actions"]
    missing = [setting for feature, setting in required.items() if feature not in supported]
    if missing:
        raise CommandError("the server helper does not support " + ", ".join(missing) +
                           "; re-run `shiplord server bootstrap` to upgrade it, then retry", 4)


def job_timeout(manifest: dict) -> float:
    """Wait through the server's full Docker job bound, since stored releases are not in the local manifest."""
    if manifest["runtime"] == "docker":
        from .shutdown import MAX_JOB_SECONDS
        return MAX_JOB_SECONDS + 30  # leave time to read the job result after the worker's hard limit
    return manifest["deploy"]["readiness_timeout_seconds"] + 900


def explicit_job_timeout() -> float:
    """Wait bound for a named target with no local manifest: cover the larger of both supported runtimes."""
    docker = job_timeout({"runtime": "docker"})
    native = job_timeout({"runtime": "native", "deploy": {"readiness_timeout_seconds": MAX_READINESS_SECONDS}})
    return max(docker, native)


def deploy_application(manifest: dict, client: HelperClient, *, image: str | None = None, binary: Path | None = None,
                       commit: str, sequence: int, request_id: str | None = None, timeout: float | None = None, allow_older: bool = False,
                       on_progress: Callable[[str], None] = lambda phase: None, verify_public: bool = True,
                       require_public: bool = False, public_check=check_public_https) -> tuple[int, dict]:
    if (image is None) == (binary is None):
        raise CommandError("specify exactly one of --image and --binary")
    artifact = validate_deploy_inputs(manifest, binary if binary is not None else image, commit)
    arguments = _deploy_arguments(manifest, artifact, sequence, allow_older)
    require_feature_support(manifest, client)
    request_id = request_id or new_request_id("deploy")
    timeout = timeout or job_timeout(manifest)
    outcome = submit_and_wait(client, "deploy", request_id=request_id, commit=commit, arguments=arguments,
                              timeout=timeout, on_progress=on_progress, payload=binary,
                              lookup_command=status_lookup_command(manifest, request_id))
    result = {"ok": outcome["exit_code"] == 0, "request_id": request_id, "status": outcome["status"],
              "exit_code": outcome["exit_code"], **outcome["detail"]}
    code = outcome["exit_code"]
    if code == 0:
        result["url"] = "https://" + manifest["http"]["api_domain"]
    if code == 0 and outcome["status"] in ("succeeded", "unchanged", "already_applied") and verify_public:
        result["public"] = public_check(manifest["http"]["api_domain"], manifest["http"]["readiness_path"])
        if require_public and not result["public"]["ok"]:
            result["ok"], code = False, 1
            result["error"] = "the deployment is live on the server, but the public HTTPS check failed"
    return code, result


def release_application(manifest: dict, client: HelperClient, *, commit: str, image_repository: str | None = None,
                        tag: str | None = None, platform: str | None = None, builder: build.Builder | None = None,
                        **deploy_options) -> tuple[int, dict]:
    """Build and push this checkout's image, then deploy the pushed digest through the normal deploy path."""
    repository = image_repository_for(manifest, image_repository)
    if repository is None:
        raise CommandError("`shiplord release` builds a Docker image; use `shiplord deploy --binary` for a native "
                           "application", 2)
    tag = tag or commit
    # Apply deploy's own rules to the repository and commit before anything is built or pushed.
    validate_deploy_inputs(manifest, f"{repository}@sha256:{'0' * 64}", commit)
    builder = builder or build.Builder()
    try:
        prepared = builder.plan(repository=repository, tag=tag, platform=platform or build.DEFAULT_PLATFORM,
                                commit=commit)
        builder.require_clean_tree()
        artifact = builder.build_and_push(prepared)
    except build.BuildError as error:
        raise CommandError(str(error), error.code) from None
    code, result = deploy_application(manifest, client, image=artifact["image"], commit=commit, **deploy_options)
    return code, {**result, "build": artifact}


def plan_application(manifest: dict, client: HelperClient, *, image: str | None = None, binary: Path | None = None,
                     commit: str, sequence: int, allow_older: bool = False) -> tuple[int, dict]:
    if (image is None) == (binary is None):
        raise CommandError("specify exactly one of --image and --binary")
    artifact = validate_deploy_inputs(manifest, binary if binary is not None else image, commit)
    arguments = _deploy_arguments(manifest, artifact, sequence, allow_older)
    require_feature_support(manifest, client)
    response = client.call("plan", request_id=new_request_id("plan"), commit=commit, arguments=arguments)
    return 0, {"ok": True, "scope": "server plan; nothing was changed", **response["result"]}


def status_lookup_command(manifest: dict, job_id: str) -> str | None:
    """A copyable command that reads one job back, when the application and registered alias are known."""
    name, server = manifest.get("name"), manifest.get("server")
    if not isinstance(name, str) or not isinstance(server, str):
        return None
    return f"shiplord status {name} --server {server} --job-id {shlex.quote(job_id)}"


def application_status(client: HelperClient, job_id: str | None = None) -> tuple[int, dict]:
    if job_id is not None and not valid_job_id(job_id):
        raise CommandError("--job-id must start with a letter or digit and contain only letters, digits, '-' and '_'")
    response = client.call("status", arguments={"job_id": job_id} if job_id is not None else {})
    return 0, {"ok": True, **response["result"]}


def application_logs(client: HelperClient, tail: int) -> tuple[int, dict]:
    if not 1 <= tail <= 1000:
        raise CommandError("--tail must be between 1 and 1000")
    return 0, {"ok": True, "logs": client.call("logs", arguments={"tail": tail})["result"]["logs"]}


def run_job_command(manifest: dict | None, client: HelperClient, action: str, *, request_id: str | None = None,
                    force_stop: bool = False,
                    on_progress: Callable[[str], None] = lambda phase: None) -> tuple[int, dict]:
    """Submit one job and wait for it.

    ``manifest`` is None for a named target resolved without a local manifest; the wait bound then has to cover
    every runtime the server may report for that application.
    """
    request_id = request_id or new_request_id(action)
    timeout = explicit_job_timeout() if manifest is None else job_timeout(manifest)
    arguments = {"force_stop": True} if force_stop else {}
    if action == "restart" and "restart" not in client.call("capabilities")["actions"]:
        # An older helper answers a bare "invalid action" with exit 2, which would blame the operator's input.
        raise CommandError("the server helper does not support restart; re-run `shiplord server bootstrap` "
                           "to upgrade it, then retry", 4)
    if force_stop and FORCE_STOP_FEATURE not in client.call("capabilities")["actions"]:
        raise CommandError("the server helper does not support --force-stop; re-run `shiplord server bootstrap` "
                           "to upgrade it, then retry", 4)
    outcome = submit_and_wait(client, action, request_id=request_id, arguments=arguments,
                              timeout=timeout, on_progress=on_progress,
                              lookup_command=status_lookup_command(manifest, request_id) if manifest else None)
    return outcome["exit_code"], {"ok": outcome["exit_code"] == 0, "request_id": request_id, "status": outcome["status"],
                                  "exit_code": outcome["exit_code"], **outcome["detail"]}


def application_doctor(manifest: dict, client: HelperClient, *, which=None, resolve=None) -> tuple[int, dict]:
    """Check local tools, the deployment login, the helper, the registration, and DNS."""
    import shutil
    which = which or shutil.which
    checks: dict[str, dict] = {name: {"ok": bool(which(name))} for name in ("ssh", "git", "gh")}
    domain = manifest["http"]["api_domain"]
    try:
        capabilities = client.call("capabilities")
        actions = capabilities["actions"]
        checks["server_access"] = {"ok": True, "helper_version": capabilities["helper_version"],
                                   "protocol": PROTOCOL_VERSION}
        checks["helper_actions"] = {"ok": "deploy" in actions, "actions": actions}
        status = client.call("status")["result"]["application"]
        checks["application"] = {"ok": True, "current": status["current"], "unfinished_transaction":
                                 status["unfinished_transaction"], "running": status["running"]}
        if status["unfinished_transaction"]:
            checks["application"]["ok"] = False
            checks["application"]["hint"] = "run `shiplord recover`"
    except ClientError as error:
        checks["server_access"] = {"ok": False, "error": str(error)}
        return error.code, {"ok": False, "checks": checks}
    try:
        addresses = sorted({info[4][0] for info in (resolve or (lambda d: socket.getaddrinfo(d, 443, type=socket.SOCK_STREAM)))(domain)})
        checks["dns"] = {"ok": bool(addresses), "domain": domain, "addresses": addresses}
    except OSError:
        checks["dns"] = {"ok": False, "domain": domain, "hint": "create the DNS record for this hostname"}
    ok = all(check["ok"] for check in checks.values())
    return (0 if ok else 3), {"ok": ok, "scope": "local tools, SSH access, helper, application, and DNS", "checks": checks}


def server_doctor(admin: AdminClient) -> tuple[int, dict]:
    """Deep server health: helper, Caddy wiring, sshd confinement, Docker, disk, and each application's state."""
    result = admin.call("doctor", None, {})
    return (0 if result["ok"] else 3), {"ok": result["ok"], "scope": "server health (read-only)", "checks": result["checks"]}


def server_applications(admin: AdminClient) -> tuple[int, dict]:
    """Read the registered applications from the server's root-only inventory."""
    result = admin.call("apps", None, {})
    return (0 if result["ok"] else 3), result


def inventory_support(version: object) -> str:
    """Classify a helper version against the app-inventory minimum.

    A version without an unambiguous ordering (a pre-release, or anything that is not stable
    SemVer) is ``unknown``: it is never reported as a confirmed lack of inventory support.
    """
    match = _STABLE_RELEASE.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        return "unknown"
    return "supported" if tuple(map(int, match.groups())) >= _MINIMUM_INVENTORY else "unsupported"


def inventory_upgrade(alias: str, version: object) -> dict | None:
    """Upgrade guidance only when a stable helper release is confirmed too old for app inventory."""
    if inventory_support(version) != "unsupported":
        return None
    return {"error": f"App inventory requires helper {MINIMUM_INVENTORY_HELPER_VERSION} or later.",
            "error_code": "helper_upgrade_required", "helper_version": version,
            "minimum_helper_version": MINIMUM_INVENTORY_HELPER_VERSION,
            "upgrade_command": f"shiplord server bootstrap {alias}"}


def doctor_all_servers(servers: list[dict[str, str]], probe: Callable[..., dict] | None = None,
                       timeout: float = FLEET_PROBE_TIMEOUT_SECONDS) -> tuple[int, dict]:
    """Probe every registered server read-only; one failed probe never stops the sweep.

    Reachability and inventory support are reported separately, so an unreachable server is
    never mistaken for a confirmed old helper. Nothing here asks for sudo or runs bootstrap.
    """
    probe = probe or probe_capabilities
    results = []
    for entry in servers:
        alias = entry["alias"]
        try:
            response = probe(entry["ssh"], timeout=timeout)
        except IncompatibleProtocol:
            results.append({"server": alias, "ok": False, "reachable": True, "helper_version": None,
                            "inventory_support": "unknown", "error": "incompatible helper protocol version",
                            "error_code": "incompatible_protocol"})
            continue
        except (TransportError, ServerError):
            results.append({"server": alias, "ok": False, "reachable": False, "helper_version": None,
                            "inventory_support": "unknown",
                            "error": "server capabilities check failed; verify the alias, SSH trust, login, and dispatcher"})
            continue
        version = response.get("helper_version")
        upgrade = inventory_upgrade(alias, version)
        item = {"server": alias, "ok": upgrade is None, "reachable": True,
                "helper_version": version if isinstance(version, str) else None,
                "inventory_support": inventory_support(version)}
        if upgrade is not None:
            item.update(upgrade)
        results.append(item)
    partial = any(not item["ok"] for item in results)
    return (3 if partial else 0), {
        "ok": not partial, "partial": partial,
        "scope": "protocol capabilities only; server readiness and deployment access were not checked",
        "servers": results}
