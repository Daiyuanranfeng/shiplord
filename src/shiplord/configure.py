"""Create a nonsecret application manifest without replacing existing files."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from . import detect
from .manifest import (ManifestError, validate_app_name, validate_hostname, validate_manifest, validate_origin,
                       validate_readiness_path)

DEFAULT_CONTAINER_PORT = 8080
DEFAULT_READINESS_PATH = "/ready"
DEFAULT_MEMORY_MB = 512
DEPLOYMENT_BRANCH = "main"
# Only a GitHub remote justifies the ghcr.io repository that a [github] table means; a Docker Hub image is
# named explicitly in the manifest's [image] table instead.
GITHUB_HOST = "github.com"
# Deploys above the ceiling the administrator registered at provisioning are refused (4096 MiB by default), so the
# interactive prompt stays inside it; an explicit --memory flag is checked only by the manifest rules.
INTERACTIVE_MEMORY_RANGE = (16, 4096)
TEMPLATES = ("generic", "google-adk")
ATTEMPTS = 3
# validate_repository asks the manifest for its own rule by wrapping the candidate in an otherwise valid manifest, so
# configure cannot accept a repository that load_manifest would reject and the rule is written down only once.
_REPOSITORY_PROBE = {
    "schema_version": 1, "name": "app", "server": "server", "template": "generic", "runtime": "docker",
    "http": {"api_domain": "api.example.com", "frontend_origins": [], "container_port": DEFAULT_CONTAINER_PORT,
             "readiness_path": DEFAULT_READINESS_PATH},
    "resources": {"memory_mb": DEFAULT_MEMORY_MB},
    "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2},
}


class ConfigurationError(ValueError):
    """The manifest could not be created safely."""


class ConfigurationCancelled(ConfigurationError):
    """The person declined the summary; nothing was written."""
def validate_repository(value: object) -> str:
    """An ``owner/repository`` pair, checked by the manifest's own ``github.repository`` rule."""
    return validate_manifest({**_REPOSITORY_PROBE, "github": {"repository": value,
                                                              "deployment_branch": DEPLOYMENT_BRANCH}})["github"]["repository"]


def repository_from_url(url: str) -> str | None:
    """``owner/repository`` from a GitHub remote URL, or None when the URL names nothing Shiplord can justify.

    Both forms git writes are accepted: ``git@github.com:owner/orders.git`` and ``https://github.com/owner/orders.git``.
    Only github.com is accepted, because a GitHub repository is what the [github] table means. The URL itself is
    never returned or reported: a remote can carry a credential.
    """
    text = url.strip()
    if not text:
        return None
    if "://" in text:
        try:
            parts = urlsplit(text)
            host, path = parts.hostname, parts.path
        except ValueError:
            return None
    else:  # the scp-like form: [user@]host:owner/repository
        authority, separator, path = text.partition(":")
        if not separator:
            return None
        host = authority.rpartition("@")[2]
    if not host or host.lower() != GITHUB_HOST:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    owner, slash, name = path.partition("/")
    if not slash or not owner or not name or "/" in name:
        return None
    try:
        return validate_repository(f"{owner}/{name}")
    except ManifestError:
        return None


def repository_from_git_remote(root: Path) -> str | None:
    """The ``owner/repository`` named by the ``origin`` remote, when git and a GitHub remote are both there."""
    try:
        result = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True,
                                errors="replace", timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return repository_from_url(result.stdout)


def create_manifest(directory: Path, name: str, server: str, api_domain: str,
                    origins: list[str], runtime: str = "docker", template: str = "google-adk", *,
                    hostnames: Sequence[str] | None = None, www_redirect: bool = False,
                    container_port: int = DEFAULT_CONTAINER_PORT, readiness_path: str = DEFAULT_READINESS_PATH,
                    memory_mb: int = DEFAULT_MEMORY_MB, image_repository: str | None = None) -> Path:
    raw = {
        "schema_version": 1, "name": name, "server": server,
        "template": template, "runtime": runtime,
        "http": {"api_domain": api_domain, "hostnames": list(hostnames or []), "www_redirect": www_redirect,
                 "frontend_origins": origins, "container_port": container_port, "readiness_path": readiness_path},
        "resources": {"memory_mb": memory_mb},
        "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2},
    }
    if image_repository is not None:
        raw["github"] = {"repository": image_repository, "deployment_branch": DEPLOYMENT_BRANCH}
    config = validate_manifest(raw)
    quote = json.dumps
    origin_list = ", ".join(quote(origin) for origin in config["http"]["frontend_origins"])
    # `www_redirect` is expanded by validation, so the file records the aliases it produces and no second rule.
    hostname_list = ", ".join(quote(host) for host in config["http"].get("hostnames") or [])
    content = (
        f'schema_version = 1\nname = {quote(config["name"])}\n'
        f'server = {quote(config["server"])}\ntemplate = {quote(config["template"])}\n'
        f'runtime = {quote(config["runtime"])}\n\n'
        f'[http]\napi_domain = {quote(config["http"]["api_domain"])}\n'
        + (f'hostnames = [{hostname_list}]\n' if hostname_list else "") +
        f'frontend_origins = [{origin_list}]\ncontainer_port = {config["http"]["container_port"]}\n'
        f'readiness_path = {quote(config["http"]["readiness_path"])}\n\n'
        f'[resources]\nmemory_mb = {config["resources"]["memory_mb"]}\n\n'
        '[deploy]\nreadiness_timeout_seconds = 90\nretain_successful_releases = 2\n'
    )
    if "github" in config:  # the same table `shiplord github setup` writes, so provision needs no extra flags
        content += (f'\n[github]\nrepository = {quote(config["github"]["repository"])}\n'
                    f'deployment_branch = {quote(config["github"]["deployment_branch"])}\n')
    destination = directory / "ship.toml"
    try:
        # A same-directory hard link publishes the complete file atomically and
        # fails if another process created the destination first.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".shiplord-", delete=True) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.link(temporary.name, destination)
    except FileExistsError:
        raise ConfigurationError("ship.toml already exists; no file was replaced") from None
    except OSError:
        raise ConfigurationError("could not create ship.toml") from None
    return destination


Ask = Callable[[str, "str | None"], str]


def configure_application(directory: Path, given: dict, *, interactive: bool,
                          servers: list[dict[str, str]] | None = None, ask: Ask | None = None,
                          say: Callable[[str], None] = lambda text: None,
                          confirm: Callable[[dict], bool] | None = None,
                          hostnames: Sequence[str] = (), www_redirect: bool = False) -> dict:
    """Write ``ship.toml`` from the given values, asking for the missing ones only at a terminal.

    ``given`` holds the ``configure`` options (None when not given). ``hostnames`` and ``www_redirect`` come from
    flags, so they need no question. Returns the answers. Nothing is written when the directory already has a
    manifest, an answer is invalid, or ``confirm`` declines the summary.
    """
    destination = directory / "ship.toml"
    if destination.exists() or destination.is_symlink():
        raise ConfigurationError("ship.toml already exists; no file was replaced")
    if interactive:
        answers = collect_answers(given, root=directory, servers=servers or [], ask=ask, say=say)
        answers["hostnames"], answers["www_redirect"] = list(hostnames), www_redirect
        if confirm is not None and not confirm(answers):
            raise ConfigurationCancelled("no file was created")
    else:
        if given["server"] is None or given["api_domain"] is None:
            raise ConfigurationError("--server and --api-domain are required (run `shiplord configure` in a terminal "
                                     "to be asked instead)")
        if given["origins"] is None:
            raise ConfigurationError("specify --frontend or explicitly choose --no-frontend")
        answers = dict(given, name=given["name"] or directory.name, runtime=given["runtime"] or "docker",
                       template=given["template"] or "google-adk")
        answers["image_repository"] = resolve_image_repository(directory, answers["runtime"],
                                                               answers.get("image_repository"), ask=None, say=say)
        answers["hostnames"], answers["www_redirect"] = list(hostnames), www_redirect
    create_manifest(directory, answers["name"], answers["server"], answers["api_domain"], answers["origins"],
                    answers["runtime"], answers["template"], hostnames=answers["hostnames"],
                    www_redirect=answers["www_redirect"],
                    **{key: answers[key] for key in ("container_port", "readiness_path", "memory_mb")
                       if answers[key] is not None},
                    image_repository=answers.get("image_repository"))
    return answers


def _ask_valid(ask: Ask, say: Callable[[str], None], prompt: str, default: str | None, parse: Callable[[str], object]):
    """Ask until ``parse`` accepts the answer; give up after a few invalid answers so a script cannot loop forever."""
    for _ in range(ATTEMPTS):
        answer = ask(prompt, default).strip()
        try:
            return parse(answer)
        except (ManifestError, ConfigurationError) as error:
            say(f"  {error}")
    raise ConfigurationError("too many invalid answers; no file was created")


def _integer(low: int, high: int, name: str) -> Callable[[str], int]:
    def parse(answer: str) -> int:
        if not answer.isdigit() or not low <= int(answer) <= high:
            raise ConfigurationError(f"{name} must be a whole number from {low} to {high}")
        return int(answer)
    return parse


def resolve_image_repository(root: Path, runtime: str, given: str | None, *, ask: Ask | None,
                             say: Callable[[str], None],
                             remote: Callable[[Path], str | None] = repository_from_git_remote) -> str | None:
    """The repository a Docker application publishes to: the flag, else the git remote, else a question.

    Without ``ask`` (no terminal) an unknown repository stays None, so the caller can say what the next command will
    need instead of guessing a registry path. A native application has no image.
    """
    if runtime != "docker":
        return given
    if given is not None:
        return given
    detected = remote(root)
    if detected is not None:
        say(f"Detected the image repository {detected} from the git remote origin.")
        return detected
    if ask is None:
        return None
    say("Shiplord pulls this application's image from GitHub Container Registry (ghcr.io/<owner>/<repository>).")
    return _ask_valid(ask, say, "Image repository (owner/repository)", None, validate_repository)


def collect_answers(given: dict, *, root: Path, servers: list[dict[str, str]], ask: Ask,
                    say: Callable[[str], None],
                    remote: Callable[[Path], str | None] = repository_from_git_remote) -> dict:
    """Fill every missing ``create_manifest`` argument by asking, with defaults detected from the repository.

    ``given`` holds the values from flags (None when not given). Each answer is validated as it is entered, so the
    final manifest validation in ``create_manifest`` only repeats checks that already passed.
    """
    answers = dict(given)

    if answers["server"] is None:
        if not servers:
            raise ConfigurationError("no servers are registered; run `shiplord server add <alias> --ssh <user>@<host>` "
                                     "first")
        aliases = [server["alias"] for server in servers]
        say("Servers:")
        for number, server in enumerate(servers, 1):
            say(f"  {number}. {server['alias']}  ({server['ssh']})")

        def server_choice(answer: str) -> str:
            if answer.isdigit() and 1 <= int(answer) <= len(aliases):
                return aliases[int(answer) - 1]
            if answer in aliases:
                return answer
            raise ConfigurationError("choose a listed server by number or alias")
        answers["server"] = _ask_valid(ask, say, "Server", "1" if len(aliases) == 1 else None, server_choice)

    if answers["name"] is None:
        answers["name"] = _ask_valid(ask, say, "Application name", detect.suggest_name(root.name), validate_app_name)

    if answers["template"] is None:
        def template_choice(answer: str) -> str:
            if answer not in TEMPLATES:
                raise ConfigurationError("template must be generic (your own Dockerfile) or google-adk")
            return answer
        answers["template"] = _ask_valid(ask, say, "Template (generic = your own Dockerfile, google-adk)",
                                         detect.suggest_template(root), template_choice)
    if answers["runtime"] is None:
        answers["runtime"] = "docker"  # native binaries are opt-in with --runtime native

    if answers.get("image_repository") is None:
        answers["image_repository"] = resolve_image_repository(root, answers["runtime"], None, ask=ask, say=say,
                                                               remote=remote)

    if answers["api_domain"] is None:
        answers["api_domain"] = _ask_valid(ask, say, "Public hostname (e.g. api.example.com)", None,
                                           lambda answer: validate_hostname(answer, "the hostname"))

    if answers["origins"] is None:
        if answers["template"] == "google-adk":  # ALLOW_ORIGINS is only passed to ADK applications
            def origins(answer: str) -> list[str]:
                values = [validate_origin(item.strip()) for item in answer.split(",") if item.strip()]
                if len(set(values)) != len(values):
                    raise ConfigurationError("frontend origins must not repeat")
                return values
            answers["origins"] = _ask_valid(ask, say, "Frontend origins (comma-separated, blank for none)", "", origins)
        else:
            answers["origins"] = []

    if answers["container_port"] is None:
        if answers["runtime"] == "docker":
            detected = detect.dockerfile_port(root)
            if detected is not None:
                say(f"Detected port {detected} from EXPOSE in {detect.dockerfile(root)}.")
            say("Shiplord sets PORT to this value; it is the port your application listens on inside the container.")
            answers["container_port"] = _ask_valid(ask, say, "Container port",
                                                   str(detected or DEFAULT_CONTAINER_PORT),
                                                   _integer(1, 65535, "the port"))
        else:
            answers["container_port"] = DEFAULT_CONTAINER_PORT

    if answers["readiness_path"] is None:
        default = DEFAULT_READINESS_PATH if answers["template"] == "google-adk" else detect.HEALTH_PATH_SUGGESTIONS[0]
        say("The server polls this path until it returns 200 before routing traffic to a new release "
            f"(common: {', '.join(detect.HEALTH_PATH_SUGGESTIONS)}).")
        answers["readiness_path"] = _ask_valid(ask, say, "Health check path", default, validate_readiness_path)

    if answers["memory_mb"] is None:
        low, high = INTERACTIVE_MEMORY_RANGE
        answers["memory_mb"] = _ask_valid(ask, say, f"Memory limit in MiB ({low}-{high})", str(DEFAULT_MEMORY_MB),
                                          _integer(low, high, "the memory limit"))
    return answers
