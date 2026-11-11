"""Build and push a Docker image from the local checkout, and report the pushed digest.

The build runs ``docker buildx build --push`` so the registry, not the local image store,
holds the artifact the server will pull. The pushed digest is read from buildx's metadata
file; the image is then deployed by digest through the normal deploy path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

DEFAULT_PLATFORM = "linux/amd64"
PLATFORMS = ("linux/amd64", "linux/arm64")
REVISION_LABEL = "org.opencontainers.image.revision"
BUILD_TIMEOUT_SECONDS = 3600
GIT_TIMEOUT_SECONDS = 30
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

DockerRunner = Callable[..., subprocess.CompletedProcess]
GitRunner = Callable[[list[str], Path], subprocess.CompletedProcess]


class BuildError(Exception):
    """The local build cannot proceed; ``code`` is the CLI exit status (1 failed, 2 input, 3 prerequisite)."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def validate_tag(tag: str) -> str:
    """Reject a tag Docker would refuse before any build starts."""
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise BuildError("--tag must be a Docker image tag: letters, digits, dots, dashes, and underscores", 2)
    return tag


def validate_platform(platform: str) -> str:
    """The server runs amd64 or arm64; other platforms would be refused at deploy time."""
    if platform not in PLATFORMS:
        raise BuildError("--platform must be linux/amd64 or linux/arm64", 2)
    return platform


def parse_digest(text: str) -> str:
    """The pushed manifest digest from buildx's ``--metadata-file`` output."""
    try:
        metadata = json.loads(text)
    except ValueError:
        raise BuildError("docker's build metadata was not valid JSON; nothing was deployed", 1) from None
    digest = metadata.get("containerimage.digest") if isinstance(metadata, dict) else None
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise BuildError("docker did not report a sha256 image digest; nothing was deployed", 1)
    return digest


def build_arguments(prepared: dict, metadata: Path) -> list[str]:
    """The exact buildx invocation: push for one server platform, labelled with the revision."""
    return ["docker", "buildx", "build",
            "--platform", prepared["platform"],
            "--push",
            "--tag", f"{prepared['repository']}:{prepared['tag']}",
            "--label", f"{REVISION_LABEL}={prepared['revision']}",
            "--metadata-file", str(metadata),
            prepared["context"]]


def _run_docker(argv: list[str], *, timeout: float, stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, timeout=timeout, check=False, stdout=stdout)


def _run_git(argv: list[str], directory: Path) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=directory, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
                          check=False)


class Builder:
    """A local Docker build whose process calls can be replaced in tests."""

    def __init__(self, *, runner: DockerRunner | None = None, git_runner: GitRunner | None = None,
                 directory: Path | None = None, timeout: float = BUILD_TIMEOUT_SECONDS):
        self.docker = runner or _run_docker
        self.git = git_runner or _run_git
        self.directory = Path(directory or Path.cwd())
        self.timeout = timeout

    def plan(self, *, repository: str, tag: str, platform: str, commit: str, context: Path | None = None) -> dict:
        """Validate the build inputs and describe the image without touching Docker or the registry."""
        validate_tag(tag)
        validate_platform(platform)
        return {"repository": repository, "tag": tag, "platform": platform, "revision": commit,
                "context": str(context or self.directory)}

    def require_clean_tree(self) -> None:
        """Refuse to build from a checkout that does not match the commit about to be deployed."""
        try:
            result = self.git(["git", "status", "--porcelain", "--untracked-files=normal"], self.directory)
        except subprocess.TimeoutExpired:
            raise BuildError("git did not answer while checking the working tree; nothing was built", 3) from None
        except OSError:
            raise BuildError("could not run git; install git and retry", 3) from None
        if result.returncode != 0:
            raise BuildError("could not read the working tree; run `shiplord release` inside the application's "
                             "Git repository", 3)
        if result.stdout.strip():
            raise BuildError("the working tree has uncommitted changes; commit or stash them so the image matches "
                             "the commit", 2)

    def build_and_push(self, prepared: dict) -> dict:
        """Build and push, then return the immutable image reference read from buildx's metadata."""
        with tempfile.TemporaryDirectory(prefix="shiplord-build-") as temporary:
            metadata = Path(temporary) / "metadata.json"
            try:
                result = self.docker(build_arguments(prepared, metadata), timeout=self.timeout, stdout=sys.stderr)
            except subprocess.TimeoutExpired:
                raise BuildError("the image build timed out; nothing was deployed", 1) from None
            except OSError:
                raise BuildError("could not run docker; install Docker with buildx and retry", 3) from None
            if result.returncode != 0:
                raise BuildError(f"the image build failed (docker exit {result.returncode}); nothing was deployed", 1)
            try:
                text = metadata.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise BuildError("docker reported success but wrote no build metadata; the image may have been "
                                 "pushed, but nothing was deployed", 1) from None
        digest = parse_digest(text)
        return {"repository": prepared["repository"], "tag": prepared["tag"], "platform": prepared["platform"],
                "revision": prepared["revision"], "digest": digest,
                "image": f"{prepared['repository']}@{digest}"}
