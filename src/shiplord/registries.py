"""The image registries Shiplord pulls from, and the reference forms it accepts.

This is the one place that decides what a registry is. The manifest default, `registry login`,
deploy validation, the release record, and the server's registration all use it.

Accepted image repository forms:

* ``ghcr.io/<owner>/<name>`` - GitHub Container Registry. A manifest ``[github]`` table means this.
* ``docker.io/<namespace>/<name>`` - Docker Hub, named with its host.
* ``<name>`` - a Docker Hub official image, stored as ``docker.io/library/<name>``.

A bare ``<namespace>/<name>`` without a host is refused: it could be a GitHub owner/repository or a
Docker Hub namespace, and guessing would allowlist an image the operator did not name. No other
registry is supported yet, and a repository never carries a tag, a port, a scheme, or credentials.

A deployed image is always ``<repository>@sha256:<64 lowercase hex digits>``. Tags and other digest
algorithms are refused before the server is contacted.

Docker looks up login credentials by key: ``ghcr.io`` for GHCR and ``https://index.docker.io/v1/``
for Docker Hub. `docker login` writes the same keys, so a stored credential is found at pull time.
"""

from __future__ import annotations

import re

GHCR = "ghcr.io"
DOCKER_HUB = "docker.io"
DOCKER_HUB_LIBRARY = "library"
DOCKER_HUB_LOGIN_KEY = "https://index.docker.io/v1/"
HOSTS = (GHCR, DOCKER_HUB)

# One repository path component as Docker accepts it: lowercase alphanumerics separated by ".", "_",
# "__", or one or more "-".
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_COMPONENT_RE = re.compile(_COMPONENT + r"\Z")
_PATH_RE = re.compile(rf"{_COMPONENT}(?:/{_COMPONENT})+\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,512}\Z")

_REPOSITORY_MESSAGE = ("an image repository must be ghcr.io/owner/name, docker.io/namespace/name, or a single "
                       "Docker Hub image name")
_PINNED_MESSAGE = "an image must be an immutable reference: registry/name@sha256:<64 lowercase hex digits>"


class RegistryPolicyError(ValueError):
    """An image reference or registry login does not match the supported policy."""


def validate_host(value: object) -> str:
    """A supported registry host, as typed for `registry login --registry`."""
    if not isinstance(value, str) or value not in HOSTS:
        raise RegistryPolicyError("registry logins are supported for ghcr.io and docker.io only")
    return value


def login_key(host: str) -> str:
    """The key Docker looks up in the login file for this registry."""
    validate_host(host)
    return DOCKER_HUB_LOGIN_KEY if host == DOCKER_HUB else host


def login_keys(host: str) -> tuple[str, ...]:
    """Every key that means this registry, so a bare ``docker.io`` entry is replaced, not duplicated."""
    key = login_key(host)
    return (key,) if key == host else (key, host)


def host_for_key(key: str) -> str:
    """The user-facing host for a login-file key; an unknown key is returned unchanged."""
    return DOCKER_HUB if key == DOCKER_HUB_LOGIN_KEY else key


def validate_username(value: object) -> str:
    """A registry account name; the same rule for both registries, never echoed in an error."""
    if not isinstance(value, str) or not _USER_RE.fullmatch(value):
        raise RegistryPolicyError("the username may contain letters, digits, dots, dashes, and underscores")
    return value


def validate_login(host: object, username: object, token: object) -> str:
    """Check a username/token pair for a supported registry; returns the registry host."""
    host = validate_host(host)
    validate_username(username)
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise RegistryPolicyError("the token must be 1-512 printable characters without spaces or line breaks")
    return host


def validate_repository(value: object) -> str:
    """A supported image repository without a tag or digest, in its one stored spelling."""
    # A colon is a tag or a host port; an "@" is a digest. Neither belongs in an allowlisted repository.
    if not isinstance(value, str) or not value or value != value.strip() or ":" in value or "@" in value:
        raise RegistryPolicyError(_REPOSITORY_MESSAGE)
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise RegistryPolicyError(_REPOSITORY_MESSAGE)
    host = parts[0]
    if host in HOSTS:
        path = "/".join(parts[1:])
        if host == DOCKER_HUB and "/" not in path and _COMPONENT_RE.fullmatch(path):
            # docker.io/name would give the same image two stored spellings; name it explicitly.
            raise RegistryPolicyError("name a Docker Hub official image as docker.io/library/name or just name")
        if not _PATH_RE.fullmatch(path):
            raise RegistryPolicyError(_REPOSITORY_MESSAGE)
        return f"{host}/{path}"
    if len(parts) > 1:
        raise RegistryPolicyError("an image repository without a host is ambiguous: use ghcr.io/owner/name or "
                                  "docker.io/namespace/name")
    if not _COMPONENT_RE.fullmatch(value):
        raise RegistryPolicyError(_REPOSITORY_MESSAGE)
    return f"{DOCKER_HUB}/{DOCKER_HUB_LIBRARY}/{value}"


def validate_pinned(value: object) -> str:
    """A repository pinned to a sha256 digest, in its one stored spelling."""
    if not isinstance(value, str) or value.count("@") != 1:
        raise RegistryPolicyError(_PINNED_MESSAGE)
    repository, _, digest = value.partition("@")
    if not _DIGEST_RE.fullmatch(digest):
        raise RegistryPolicyError(_PINNED_MESSAGE)
    return f"{validate_repository(repository)}@{digest}"


def repository_of(image: str) -> str:
    """The repository part of a pinned image reference."""
    return validate_pinned(image).partition("@")[0]


def github_repository(value: str) -> str:
    """The GHCR repository that publishes a GitHub ``owner/repository`` pair."""
    return f"{GHCR}/{value.lower()}"
