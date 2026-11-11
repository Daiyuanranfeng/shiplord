"""Run the first-run steps in order and resume at the first incomplete one.

Each step wraps an existing command implementation. A step is skipped when the state it would produce is already
observable (a manifest, a registration, the environment names, a registry login, the CI setup), so re-running
``shiplord setup`` continues where the previous run stopped. Observation is local or read-only; when it cannot be
read the step runs again, because every step here is safe to repeat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import commands, github
from .client import ClientError
from .commands import CommandError
from .configure import ConfigurationCancelled, ConfigurationError, configure_application
from .environment import EnvironmentError_, parse_environment
from .github import GitHubError
from .manifest import ManifestError
from .servers import InvalidTarget, ServerError

STEP_ORDER = ("configure", "provision", "environment", "registry", "github", "check-ci")
Run = Callable[[], tuple[int, dict]]
Done = Callable[[], bool]
Report = Callable[[int, int, str, str], None]


@dataclass(frozen=True)
class Step:
    """One first-run step: ``run`` is the existing command, ``done`` observes whether it already happened."""

    name: str
    run: Run
    done: Done | None = None
    skip_reason: str | None = None
    repeatable: bool = True


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CommandError(f"could not read the {what}") from None


def configure_step(directory: Path, given: dict, *, interactive: bool,
                   servers: Callable[[], list[dict[str, str]]] | None = None,
                   ask=None, say: Callable[[str], None] = lambda text: None,
                   confirm: Callable[[dict], bool] | None = None) -> Step:
    """Create ``ship.toml`` when the directory does not already have one."""
    def done() -> bool:
        return (directory / "ship.toml").exists() or (directory / "ship.toml").is_symlink()

    def run() -> tuple[int, dict]:
        answers = configure_application(directory, given, interactive=interactive,
                                        servers=servers() if (interactive and servers is not None) else None,
                                        ask=ask, say=say, confirm=confirm if interactive else None)
        return 0, {"answers": {key: answers[key] for key in ("name", "server", "api_domain", "template", "runtime")}}

    return Step("configure", run=run, done=done, repeatable=False)


def provision_step(manifest: Callable[[], dict], admin: Callable[[], object], *,
                   public_key: Path | None = None, image_repository: str | None = None,
                   max_memory_mb: int | None = None) -> Step:
    """Create the application's account, port, state, and key access when the server does not list it."""
    def done() -> bool:
        code, result = commands.server_applications(admin())
        name = manifest()["name"]
        return code == 0 and any(item.get("application") == name for item in result.get("applications", []))

    def run() -> tuple[int, dict]:
        return commands.provision_application(manifest(), admin(), public_key=public_key,
                                              image_repository=image_repository, max_memory_mb=max_memory_mb)

    return Step("provision", run=run, done=done)


def environment_step(manifest: Callable[[], dict], admin: Callable[[], object], env_file: Path | None) -> Step:
    """Set the runtime environment from ``--env-file``; without one there is nothing to do."""
    if env_file is None:
        return Step("environment", run=lambda: (0, {}),
                    skip_reason="no --env-file given; runtime environment was not changed")

    def names() -> set[str]:
        try:
            return set(parse_environment(_read(env_file, "environment file")))
        except EnvironmentError_ as error:
            raise CommandError(str(error)) from None

    def done() -> bool:
        code, result = commands.set_environment(manifest(), admin(), "keys")
        return code == 0 and names() <= set(result.get("keys", []))

    def run() -> tuple[int, dict]:
        return commands.set_environment(manifest(), admin(), "set", _read(env_file, "environment file"))

    return Step("environment", run=run, done=done)


def registry_step(manifest: Callable[[], dict], admin: Callable[[], object], *, registry: str,
                  username: str | None, token: str | None) -> Step:
    """Store a read-only registry login; without ``--registry-username`` there is nothing to do."""
    if username is None:
        return Step("registry", run=lambda: (0, {}),
                    skip_reason="no --registry-username given; registry login was not changed")

    def done() -> bool:
        code, result = commands.registry_other(manifest(), admin(), "list")
        return code == 0 and any(item.get("registry") == registry and item.get("username") == username
                                 for item in result.get("registries", []))

    def run() -> tuple[int, dict]:
        return commands.registry_login(manifest(), admin(), registry=registry, username=username, token=token or "")

    return Step("registry", run=run, done=done)


def github_step(manifest: Callable[[], dict], *, run: Callable[[dict], tuple[int, dict]],
                complete: Callable[[dict], bool]) -> Step:
    """Create the CI key, GitHub secret, variables, and workflow unless the local state says that is done."""
    return Step("github", run=lambda: run(manifest()), done=lambda: complete(manifest()))


def check_ci_step(manifest: Callable[[], dict], run: Callable[[dict], tuple[int, dict]]) -> Step:
    """Prove the whole path with a check-only workflow run; a verification, so it always runs."""
    return Step("check-ci", run=lambda: run(manifest()))


def _code_for(error: Exception) -> int:
    if isinstance(error, (CommandError, ClientError, GitHubError)):
        code = getattr(error, "code", 1)
        return int(code) if isinstance(code, int) and code > 0 else 1
    if isinstance(error, ConfigurationCancelled):
        return 1
    if isinstance(error, (ConfigurationError, ManifestError, InvalidTarget)):
        return 2
    if isinstance(error, ServerError):
        return 3
    return 1


def _complete(step: Step) -> bool:
    if step.done is None:
        return False
    try:
        return bool(step.done())
    except Exception:  # an unreadable observation is not evidence that the step finished; every step is repeatable
        return False


def run_setup(steps: list[Step], *, rerun: bool = False, report: Report | None = None) -> tuple[int, dict]:
    """Run the steps in order, stopping at the first failure, and report what each one did."""
    outcomes: list[dict] = []
    resumed_at: str | None = None
    total = len(steps)
    for index, step in enumerate(steps, 1):
        if step.skip_reason is not None:
            outcomes.append({"step": step.name, "status": "not requested", "detail": step.skip_reason})
            if report:
                report(index, total, step.name, "not requested")
            continue
        if not (rerun and step.repeatable) and _complete(step):
            outcomes.append({"step": step.name, "status": "already complete"})
            if report:
                report(index, total, step.name, "already complete")
            continue
        resumed_at = resumed_at or step.name
        if report:
            report(index, total, step.name, "running")
        try:
            code, result = step.run()
        except (CommandError, ClientError, GitHubError, ConfigurationCancelled, ConfigurationError,
                ManifestError, ServerError) as error:
            outcomes.append({"step": step.name, "status": "failed", "error": str(error)})
            return _code_for(error), {"ok": False, "steps": outcomes, "resumed_at": resumed_at,
                                      "failed_step": step.name, "error": str(error)}
        outcomes.append({"step": step.name, "status": "ran" if code == 0 else "failed", "result": result})
        if code:
            return code, {"ok": False, "steps": outcomes, "resumed_at": resumed_at, "failed_step": step.name,
                          "error": result.get("error") or f"`{step.name}` did not succeed"}
    return 0, {"ok": True, "steps": outcomes, "resumed_at": resumed_at}
