"""Load and validate nonsecret application configuration."""

from __future__ import annotations

import ipaddress
import math
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from . import registries
from .probes import ReadinessError, validate_interval, validate_status
from .shutdown import ShutdownError, validate_action, validate_timeout
from .storage import StorageError, validate_storage


class ManifestError(ValueError):
    """A manifest cannot be used safely."""


_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
GENERIC_DOCKER_FEATURE = "docker_generic_v1"
DEFAULT_DEPLOYMENT_BRANCH = "main"
RUNTIME_KNOBS_FEATURE = "runtime_knobs_v1"  # capability a helper advertises when it honours the knobs below
MAX_READINESS_SECONDS = 900  # the ceiling for deploy.readiness_timeout_seconds
MIN_CPUS, MAX_CPUS = 0.01, 64.0


def _table(value: object, name: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"{name} must be a table")
    if set(value) - allowed:
        raise ManifestError(f"{name} contains unknown fields")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManifestError(f"{name} must be a nonempty string without surrounding whitespace")
    return value


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ManifestError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ManifestError(f"{name} must be true or false")
    return value


def _cpus(value: object) -> float:
    """A number of CPUs, as Docker's ``--cpus`` takes it (fractions are allowed)."""
    if type(value) not in (int, float):
        raise ManifestError(f"resources.cpu_limit must be a number between {MIN_CPUS:g} and {MAX_CPUS:g}")
    cpus = float(value)
    if not math.isfinite(cpus) or not MIN_CPUS <= cpus <= MAX_CPUS:
        raise ManifestError(f"resources.cpu_limit must be a number between {MIN_CPUS:g} and {MAX_CPUS:g}")
    return cpus


def validate_app_name(value: object) -> str:
    """Validate an application name before using it as a path component."""
    name = _string(value, "name")
    if len(name) > 48 or not _NAME.fullmatch(name):
        raise ManifestError("name must be a lowercase application name")
    return name


def validate_deployment_branch(value: object) -> str:
    """Validate the branch releases deploy from, with git's own restrictions where TOML can express them."""
    branch = _string(value, "github.deployment_branch")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or any(x in branch for x in ("..", "//", "@{"))
            or branch.endswith(("/", ".", ".lock")) or any(part.startswith(".") for part in branch.split("/"))):
        raise ManifestError("github.deployment_branch must be a valid branch name")
    return branch


def configured_branch(manifest: dict) -> str:
    """The branch releases deploy from: ``[github] deployment_branch``, or main when it is not set."""
    github = manifest.get("github")
    return github.get("deployment_branch", DEFAULT_DEPLOYMENT_BRANCH) if isinstance(github, dict) else DEFAULT_DEPLOYMENT_BRANCH


def _hostname(value: object, name: str) -> str:
    host = _string(value, name)
    if len(host) > 253 or host != host.lower() or host.endswith("."):
        raise ManifestError(f"{name} must be a lowercase DNS hostname")
    labels = host.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ManifestError(f"{name} must be a lowercase DNS hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ManifestError(f"{name} must be a DNS hostname, not an IP address")


def validate_hostname(value: object, name: str) -> str:
    """Validate a lowercase DNS hostname (never an IP address)."""
    return _hostname(value, name)


def validate_hostnames(primary: object, value: object, name: str) -> list[str]:
    """Validate the additional hostnames an application answers on, given its canonical hostname.

    Every entry is a lowercase DNS hostname that is neither the canonical one nor a repeat. An entry whose
    first label is ``www`` must be a subdomain of the canonical hostname: that alias is the web address of the
    same site, so redirecting it to the canonical hostname stays inside the name the site was registered for.
    """
    canonical = _hostname(primary, "api_domain")
    if not isinstance(value, list):
        raise ManifestError(f"{name} must be an explicit list")
    validated: list[str] = []
    for entry in value:
        host = _hostname(entry, f"{name} item")
        if host == canonical:
            raise ManifestError(f"{name} must not repeat the canonical hostname")
        if host.split(".")[0] == "www" and not host.endswith("." + canonical):
            raise ManifestError(f"{name} must not contain a www hostname outside the canonical hostname")
        if host in validated:
            raise ManifestError(f"{name} must not contain duplicates")
        validated.append(host)
    return validated


def validate_origin(value: object) -> str:
    """Validate one frontend origin (scheme, host, optional port; no path or credentials)."""
    return _origin(value)


def validate_readiness_path(value: object) -> str:
    """Validate the absolute URL path the server polls until the application is ready."""
    path = _string(value, "http.readiness_path")
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]*", path) or "//" in path:
        raise ManifestError("http.readiness_path must be an absolute URL path")
    return path


def _origin(value: object) -> str:
    origin = _string(value, "http.frontend_origins item")
    try:
        parts = urlsplit(origin)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise ManifestError("http.frontend_origins contains an invalid origin") from None
    if (parts.scheme not in {"http", "https"} or not hostname or parts.username is not None
            or parts.password is not None or parts.path or parts.query or parts.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in origin)):
        raise ManifestError("http.frontend_origins must contain HTTP(S) origins without paths or credentials")
    _hostname(hostname, "http.frontend_origins hostname")
    if port is not None and not 1 <= port <= 65535:
        raise ManifestError("http.frontend_origins contains an invalid port")
    # Reject netloc forms urlsplit accepts but which are not canonical origins.
    if parts.netloc != hostname + (f":{port}" if port is not None else ""):
        raise ManifestError("http.frontend_origins contains a noncanonical origin")
    return origin


def load_manifest(path: Path = Path("ship.toml")) -> dict:
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError:
        raise ManifestError("ship.toml was not found") from None
    except (OSError, tomllib.TOMLDecodeError):
        raise ManifestError("ship.toml could not be read as valid TOML") from None

    return validate_manifest(raw)


def validate_manifest(raw: dict) -> dict:
    """Validate parsed manifest data and return its nonsecret configuration."""

    root = _table(raw, "manifest", {"schema_version", "name", "server", "template", "runtime", "http", "resources",
                                    "deploy", "github", "image", "storage"})
    if type(root.get("schema_version")) is not int or root["schema_version"] != 1:
        raise ManifestError("schema_version must be 1")
    name = validate_app_name(root.get("name"))
    server = _string(root.get("server"), "server")
    if len(server) > 48 or not _NAME.fullmatch(server):
        raise ManifestError("server must be a lowercase server alias")
    template = _string(root.get("template"), "template")
    if template not in {"google-adk", "generic"}:
        raise ManifestError("template must be google-adk or generic")
    runtime = _string(root.get("runtime"), "runtime")
    if runtime not in {"docker", "native"}:
        raise ManifestError("runtime must be docker or native")
    if template == "generic" and runtime != "docker":
        raise ManifestError("the generic template requires the docker runtime")

    http = _table(root.get("http"), "http", {"api_domain", "hostnames", "www_redirect", "frontend_origins",
                                             "container_port", "readiness_path"})
    domain = _hostname(http.get("api_domain"), "http.api_domain")
    additional = http.get("hostnames", [])
    if not isinstance(additional, list):
        raise ManifestError("http.hostnames must be an explicit list")
    www = http.get("www_redirect", False)
    if type(www) is not bool:
        raise ManifestError("http.www_redirect must be true or false")
    # The alias is expanded here so a release record and a registration hold one list and no second source of truth.
    hostnames = validate_hostnames(domain, additional + (["www." + domain] if www else []), "http.hostnames")
    origins = http.get("frontend_origins")
    if not isinstance(origins, list):
        raise ManifestError("http.frontend_origins must be an explicit list")
    validated_origins = [_origin(value) for value in origins]
    if len(set(validated_origins)) != len(validated_origins):
        raise ManifestError("http.frontend_origins must not contain duplicates")
    port = _integer(http.get("container_port"), "http.container_port", 1, 65535)
    readiness_path = validate_readiness_path(http.get("readiness_path"))

    resources = _table(root.get("resources"), "resources", {"memory_mb", "cpu_limit"})
    memory = _integer(resources.get("memory_mb"), "resources.memory_mb", 1, 1048576)
    cpus = None
    if "cpu_limit" in resources:
        if runtime != "docker":
            raise ManifestError("resources.cpu_limit applies only to Docker applications")
        cpus = _cpus(resources["cpu_limit"])
    deploy = _table(root.get("deploy"), "deploy", {"readiness_timeout_seconds", "retain_successful_releases",
                                                   "shutdown_timeout_seconds", "shutdown_timeout_action",
                                                   "readiness_status", "readiness_interval_seconds", "init"})
    timeout = _integer(deploy.get("readiness_timeout_seconds"), "deploy.readiness_timeout_seconds", 1,
                       MAX_READINESS_SECONDS)
    retain = _integer(deploy.get("retain_successful_releases"), "deploy.retain_successful_releases", 2, 2)
    readiness_knobs = {}
    for field, check in (("readiness_status", validate_status), ("readiness_interval_seconds", validate_interval)):
        if field in deploy:
            try:
                readiness_knobs[field] = check(deploy[field])
            except ReadinessError as error:
                raise ManifestError(str(error)) from None
    # Only the current and the previous release are kept (rollback needs exactly those), so other values would mislead.
    result = {"schema_version": 1, "name": name, "server": server, "template": template, "runtime": runtime,
            "http": {"api_domain": domain, "frontend_origins": validated_origins, "container_port": port,
                     "readiness_path": readiness_path},
            "resources": {"memory_mb": memory}, "deploy": {"readiness_timeout_seconds": timeout, "retain_successful_releases": retain}}
    if hostnames:  # a record without additional hostnames stays byte-identical to the manifest that declared none
        result["http"]["hostnames"] = hostnames
    if cpus is not None:
        result["resources"]["cpu_limit"] = cpus
    result["deploy"].update(readiness_knobs)
    if "init" in deploy:
        if runtime != "docker":
            raise ManifestError("deploy.init applies only to Docker applications")
        result["deploy"]["init"] = _boolean(deploy["init"], "deploy.init")
    for field, check in (("shutdown_timeout_seconds", validate_timeout), ("shutdown_timeout_action", validate_action)):
        if field in deploy:
            if runtime != "docker":
                raise ManifestError(f"deploy.{field} applies only to Docker applications")
            try:
                result["deploy"][field] = check(deploy[field])
            except ShutdownError as error:
                raise ManifestError(str(error)) from None
    if "storage" in root:
        if runtime != "docker":
            raise ManifestError("storage applies only to Docker applications")
        try:
            result["storage"] = validate_storage(root["storage"])
        except StorageError as error:
            raise ManifestError(str(error)) from None
    if "image" in root:
        if runtime != "docker":
            raise ManifestError("image applies only to Docker applications")
        image = _table(root["image"], "image", {"repository"})
        try:
            result["image"] = {"repository": registries.validate_repository(image.get("repository"))}
        except registries.RegistryPolicyError as error:
            raise ManifestError(str(error)) from None
    if "github" in root:
        github = _table(root["github"], "github", {"repository", "deployment_branch"})
        repository = _string(github.get("repository"), "github.repository")
        if not _REPOSITORY.fullmatch(repository) or any(part in {".", ".."} or part.startswith("-") for part in repository.split("/")):
            raise ManifestError("github.repository must be an owner/repository pair")
        branch = validate_deployment_branch(github.get("deployment_branch", DEFAULT_DEPLOYMENT_BRANCH))
        result["github"] = {"repository": repository, "deployment_branch": branch}
        if "image" in result and result["image"]["repository"] != registries.github_repository(repository):
            raise ManifestError("ship.toml names two image repositories; keep [image] or [github], not both")
    return result
