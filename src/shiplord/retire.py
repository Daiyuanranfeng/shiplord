"""Take a deployed application out of service so that it can be removed (``deprovision --retire``)."""

from __future__ import annotations

from .layout import Layout
from .native import NativeRuntime
from .provision import ProvisionError
from .release_state import HostLock
from .routing import CaddyCommands, RouteManager, RoutingError
from .runtime import DockerRuntime, Runtime, RuntimeFailure
from .system import System

_UNUSABLE_STATE = ("could not retire the application: its release state is unusable, so nothing was changed; "
                   "inspect and repair the release state, then retry")


def _releases(state: dict | None) -> list[tuple[str, dict]]:
    """The recorded releases to discard, validated before anything is stopped; a missing state has none."""
    if state is None:
        return []
    if not isinstance(state, dict):
        raise ProvisionError(_UNUSABLE_STATE)
    releases = []
    for label in ("current", "previous"):
        release = state.get(label)
        if release is None:
            continue
        if not isinstance(release, dict):
            raise ProvisionError(_UNUSABLE_STATE)
        releases.append((label, release))
    return releases


def make_retirer(layout: Layout, system: System, lock: HostLock, caddy: CaddyCommands,
                 runtime_for=None):
    """Return the ``retire`` callback for ``deprovision``: stop, unroute, then discard artifacts.

    The order matters: the public route goes first, so nothing is proxied to a stopped application, and anything that
    fails stops the removal with the registration (and so the application's access) still intact. The release state is
    checked before either step, and the callback reports what it removed and what it kept.
    """
    def default_runtime(registration: dict) -> Runtime:
        return NativeRuntime(system, layout) if registration["runtime"] == "native" else DockerRuntime(system, layout)

    def retire(registration: dict, state: dict | None) -> dict:
        releases = _releases(state)  # reject an unusable state before the route or the runtime is touched
        runtime = (runtime_for or default_runtime)(registration)
        try:
            RouteManager(layout.caddy_root, lock, caddy).remove(registration["application_id"])
            runtime.stop(registration)
        except (RoutingError, RuntimeFailure) as failure:
            if isinstance(failure, RuntimeFailure) and failure.intervention_required:
                raise ProvisionError(f"could not retire the application: {failure}. Its public route was already removed "
                                     "and its registration and data are kept; once it has exited, retry the retirement") from None
            raise ProvisionError(f"could not retire the application: {failure}") from None
        removed = ["public route", "running release"]
        retained = ["application data (the data directory and its volume)"]
        if state is None:
            retained.append("release artifacts (no release state was recorded, so none were discarded)")
        for label, release in releases:
            warnings = runtime.discard(registration, release) or []  # best effort: warnings only
            if warnings:
                retained.extend(warnings)
            else:
                removed.append(f"the {label} release's artifacts")
        return {"removed": removed, "retained": retained}

    return retire
