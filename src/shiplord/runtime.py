"""Runtime adapters: how a release is prepared, started, stopped, and inspected.

Releases are immutable. Starting one always recreates its container from the
release record, so a rollback is the same operation as a deploy of an older
record. Only resources labelled for the application are ever touched.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Protocol

from .environment import EnvironmentError_, load_environment_file
from .layout import Layout
from .shutdown import DEFAULT_ACTION, DEFAULT_TIMEOUT_SECONDS, FORCE_EXIT_SECONDS, policy
from .storage import container_path
from .system import CommandError, System

DOCKER = "/usr/bin/docker"
PULL_TIMEOUT_SECONDS = 900
COMMAND_TIMEOUT_SECONDS = 120
MAX_LOG_BYTES = 262144
MAX_LOG_LINES = 1000
DEFAULT_FOLLOW_SECONDS = 300
MAX_FOLLOW_SECONDS = 3600
MIN_FREE_BYTES = 1024 * 1024 * 1024  # refuse to pull when less than this is free: a full disk fails a deploy halfway
DOCKER_ROOT = Path("/var/lib/docker")
REVISION_LABEL = "org.opencontainers.image.revision"
# Everything `docker run` sets that is not derived from the release record. The start and the running check both use
# these, so a change here can never make the check disagree with what was started.
PIDS_LIMIT = 512
RESTART_POLICY = "unless-stopped"
LOG_DRIVER = "json-file"
LOG_OPTIONS = {"max-size": "10m", "max-file": "3"}
SECURITY_OPTIONS = ("no-new-privileges",)
CAP_DROP = ("ALL",)

_SINCE = re.compile(r"[0-9]{1,5}[smh]\Z")
_MACHINE_PLATFORMS = {"x86_64": "linux/amd64", "amd64": "linux/amd64", "aarch64": "linux/arm64", "arm64": "linux/arm64"}


class RuntimeFailure(Exception):
    """A runtime step failed; ``phase`` names it and ``detail`` is capped, sanitized output."""

    def __init__(self, message: str, phase: str, detail: str = "", *, intervention_required: bool = False,
                 kind: str = ""):
        super().__init__(message)
        self.phase = phase
        self.detail = detail
        self.intervention_required = intervention_required  # the process may still be running: nothing may restart it
        self.kind = kind


def release_id(release: dict) -> str:
    """A short stable identity for a release record, used in resource names and labels."""
    artifact = release["artifact"]
    identity = artifact.get("image") or artifact.get("sha256")
    payload = json.dumps([release["commit"], identity, release["sequence"]], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


_SIGNALS = {"HUP": 1, "INT": 2, "QUIT": 3, "USR1": 10, "USR2": 12, "TERM": 15, "ALRM": 14, "WINCH": 28}
_UNCATCHABLE = {9, 19}  # SIGKILL and SIGSTOP cannot be handled, so they are not a graceful stop
POLL_SECONDS = 0.5


def parse_stop_signal(configured: str) -> str:
    """The signal name to send for a container's stop signal (empty means SIGTERM); only catchable signals
    qualify as graceful. Raises ``ValueError`` when the value cannot be used."""
    text = configured.strip()
    if text in ("", "<no value>"):
        return "SIGTERM"
    name = text[3:] if text.upper().startswith("SIG") else text
    if name.isdigit() and 1 <= int(name) <= 31 and int(name) not in _UNCATCHABLE:
        return name
    if name.upper() in _SIGNALS:
        return "SIG" + name.upper()
    raise ValueError(text)


def stop_signal(configured: str) -> str:
    """Validate a container's stop signal (empty means SIGTERM); only catchable signals qualify as graceful."""
    try:
        return parse_stop_signal(configured)
    except ValueError:
        raise RuntimeFailure("the container's stop signal is not one that a process can handle gracefully",
                             "stop") from None


def _digest(image: str) -> str:
    """The immutable part of an image reference; a registry alias or mirror must not change which image is meant."""
    return image.rpartition("@")[2]


def _registry(image: str) -> str:
    """The registry host an image reference names, so a message can say where an image has to come from."""
    return image.split("/", 1)[0]


def _managed_environment(registration: dict, configuration: dict) -> dict[str, str]:
    """The environment Shiplord sets on every release; the start and the running check share this one definition."""
    http = configuration["http"]
    managed = {"HOST": "0.0.0.0", "PORT": str(http["container_port"]), "LOG_LEVEL": "INFO"}
    if configuration["template"] == "google-adk":
        managed.update(AGENT_DIR="/app/src", AGENT_NAME=registration["application_id"], RELOAD_AGENTS="false",
                       SERVE_WEB_INTERFACE="false", ALLOW_ORIGINS=json.dumps(http["frontend_origins"]))
    return managed


def _environment_dict(encoded: str) -> dict[str, str]:
    """Parse the JSON list of ``KEY=VALUE`` entries Docker records; raises ``ValueError`` on anything unexpected."""
    entries = json.loads(encoded)
    if not isinstance(entries, list):
        raise ValueError("environment is not a list")
    values = {}
    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError("environment entry is not a string")
        key, separator, value = entry.partition("=")
        if not separator:
            raise ValueError("environment entry is not KEY=VALUE")
        values[key] = value
    return values


def _environment_matches(actual: dict[str, str], expected: dict[str, str]) -> bool:
    """Every value the start sets must be exactly what the container was created with.

    Extra keys are tolerated: ``.Config.Env`` also carries the image's own ``ENV`` and the base image's ``PATH``,
    which the start neither sets nor can distinguish from a custom key the operator removed. Values never leave here.
    """
    return all(actual.get(key) == value for key, value in expected.items())


def _security_options(values: list) -> set:
    """Docker writes ``no-new-privileges`` or, on some versions, ``no-new-privileges:true``."""
    return {value[:-len(":true")] if isinstance(value, str) and value.endswith(":true") else value for value in values}


def _integer(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


class Runtime(Protocol):
    def prepare(self, registration: dict, release: dict) -> dict: ...
    def start(self, registration: dict, release: dict) -> None: ...
    def stop(self, registration: dict) -> None: ...
    def is_running(self, registration: dict, release: dict) -> bool: ...
    def logs(self, registration: dict, release: dict | None, tail: int, *, since: str | None = None,
             timestamps: bool = False) -> str: ...
    def follow_logs(self, registration: dict, release: dict | None, *, tail: int, since: str | None = None,
                    timestamps: bool = False, duration: int = DEFAULT_FOLLOW_SECONDS,
                    emit: Callable[[str], None] = lambda chunk: None) -> str: ...
    def discard(self, registration: dict, release: dict) -> list[str]: ...


def validate_since(value: str) -> str:
    """Only relative durations both Docker and journalctl read the same way are accepted."""
    if not isinstance(value, str) or not _SINCE.fullmatch(value):
        raise ValueError("--since must be a duration such as 30s, 15m or 2h")
    return value


def validate_follow_timeout(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_FOLLOW_SECONDS:
        raise ValueError(f"--timeout must be between 1 and {MAX_FOLLOW_SECONDS} seconds")
    return value


class DockerRuntime:
    """Docker via the CLI. The server's platform is derived from the host unless given."""

    def __init__(self, system: System, layout: Layout, *, platform_name: str | None = None,
                 clock=time.monotonic, sleep=time.sleep, wall_clock=time.time):
        self.system, self.layout = system, layout
        self.clock, self.sleep = clock, sleep
        self.wall_clock = wall_clock
        self.stop_report: list[dict] = []  # what stopping did: stages, elapsed time, policy, whether force was needed
        self.platform = platform_name or _MACHINE_PLATFORMS.get(platform.machine().lower(), "linux/amd64")

    # naming ---------------------------------------------------------------
    @staticmethod
    def container_name(registration: dict, release: dict) -> str:
        return f"shiplord-{registration['application_id']}-{release_id(release)}"

    @staticmethod
    def volume_name(registration: dict) -> str:
        return f"shiplord-{registration['application_id']}-artifacts"

    def _docker(self, argv: list[str], phase: str, *, timeout: float = COMMAND_TIMEOUT_SECONDS,
                max_output: int = 4096):
        try:
            return self.system.run([DOCKER, *argv], timeout=timeout, max_output=max_output)
        except CommandError as error:
            raise RuntimeFailure(f"docker could not complete ({error.kind})", phase, error.output) from None

    # operations -----------------------------------------------------------
    def _require_free_disk(self, phase: str = "prepare") -> None:
        """Refuse to work when the disk is nearly full: a full disk fails a deploy halfway."""
        probe = DOCKER_ROOT if self.layout.prefix == Path("/") and DOCKER_ROOT.exists() else self.layout.var
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = MIN_FREE_BYTES  # cannot measure: let docker report its own failure
        if free < MIN_FREE_BYTES:
            raise RuntimeFailure(f"not enough free disk space to pull an image ({free // 2**20} MiB free, "
                                 f"{MIN_FREE_BYTES // 2**20} MiB needed); nothing was changed", phase)

    def _inspect_image(self, image: str):
        """Read the platform, revision label, and stop signal of a local image; non-zero means it is absent."""
        return self._docker(["image", "inspect", "--format",
                             '{{.Os}}/{{.Architecture}}|{{index .Config.Labels "' + REVISION_LABEL + '"}}|'
                             '{{.Config.StopSignal}}', image],
                            "prepare")

    def _pull(self, registration: dict, image: str, phase: str = "prepare", failure: str = "") -> None:
        """Fetch one pinned image with this application's own read-only login.

        This is the one place a pull happens: ``pull`` runs it before the host-wide lock, ``prepare`` reuses it when
        the verified image is absent, and a start reuses it when a restore finds the image pruned. ``failure`` is the
        message to raise instead of the generic one, for a caller that must not repeat the pull's raw output.
        """
        self._require_free_disk(phase)
        login = self.layout.registry_logins / registration["application_id"]
        config = ["--config", str(login)] if (login / "config.json").is_file() else []  # this app's read-only login
        pulled = self._docker([*config, "pull", "--quiet", image], phase, timeout=PULL_TIMEOUT_SECONDS)
        if pulled.code == 0:
            return
        if failure:  # a pull can echo what it was given, so this caller shows no output at all
            raise RuntimeFailure(failure, phase)
        raise RuntimeFailure("the image could not be pulled", phase, pulled.output)

    def pull(self, registration: dict, release: dict) -> None:
        """Fetch the pinned image with this application's own read-only login.

        This is the slow, network-bound step, so the worker runs it before taking the host-wide lock: a slow
        registry may delay this job, never every other application and every admin command. ``prepare`` verifies
        the result under the lock and only fetches again when the image is not there at all.
        """
        self._pull(registration, release["artifact"]["image"])

    def _ensure_image(self, registration: dict, image: str) -> None:
        """Make the release's image available on this host before anything is stopped.

        A deploy has already fetched the candidate in ``prepare``, so a local image is the usual case and costs one
        ``docker image inspect``, never a registry round trip. A restore - or any start after ``docker image prune -a``
        - fetches the pinned digest with this application's own login while the previous release keeps serving. The
        fetch's raw output is dropped: it can echo what it was given, and the message already says where the image
        has to come from.
        """
        present = self._docker(["image", "inspect", "--format", "{{.Id}}", image], "start")
        if present.code == 0:
            return
        self._pull(registration, image, "start",
                   f"the release's image could not be fetched from {_registry(image)}; "
                   "the previous release is still running")

    def prepare(self, registration: dict, release: dict) -> dict:
        """Verify the image on this host, its platform, revision, and stop signal before anything is stopped.

        The worker has already pulled the pinned digest, so the usual path only inspects a local image and never
        waits on a registry. A caller that did not pre-pull - a direct ``Deployer`` use, or a pre-pull that failed -
        still works: the image is fetched here, exactly as before, rather than failing for a reason that is not the
        caller's fault.
        """
        image = release["artifact"]["image"]
        self._require_free_disk()  # a nearly full disk must fail the deploy before the running release is touched
        inspect = self._inspect_image(image)
        if inspect.code != 0:
            self.pull(registration, release)
            inspect = self._inspect_image(image)
        if inspect.code != 0:
            raise RuntimeFailure("the image is not available on this host; nothing was changed", "prepare",
                                 inspect.output)
        try:
            image_platform, revision, configured_signal = inspect.output.strip().split("|", 2)
        except ValueError:
            raise RuntimeFailure("the image could not be understood", "prepare") from None
        if image_platform != self.platform:
            raise RuntimeFailure(f"the image is {image_platform}, but the server needs {self.platform}", "prepare")
        revision = "" if revision == "<no value>" else revision
        if revision and revision != release["commit"]:
            raise RuntimeFailure("the image was built from a different commit than requested", "prepare")
        try:  # every later transition stops the outgoing container first, so refuse this before recording it
            parse_stop_signal(configured_signal)
        except ValueError:
            raise RuntimeFailure(f"the image sets STOPSIGNAL {configured_signal.strip()}, which no process can handle, "
                                 f"so this release could never be stopped; nothing was changed ({image})",
                                 "prepare") from None
        volume_provenance = self.check_volume(registration, create=False)  # read-only: a plan creates nothing
        result = {"platform": image_platform, "revision_label": revision or None}
        if volume_provenance is not None:
            # The deployer persists this proof in the journal before replacing the retained container.
            result["storage_provenance"] = volume_provenance
        return result

    def check_volume(self, registration: dict, *, create: bool, provenance: dict | None = None) -> dict | None:
        """Accept this application's volume, with retained-container proof for legacy unlabelled volumes."""
        name = self.volume_name(registration)
        inspect = self._docker(["volume", "inspect", "--format",
                                "{{.Driver}}|{{json .Options}}|{{json .Labels}}|{{.CreatedAt}}|{{.Mountpoint}}", name],
                               "storage")
        if inspect.code != 0:
            if "no such volume" not in inspect.output.lower():
                raise RuntimeFailure("the application volume could not be inspected", "storage", inspect.output)
            if create:
                made = self._docker(["volume", "create", "--driver", "local", "--label",
                                     f"shiplord.app={registration['application_id']}", name], "storage")
                if made.code != 0:
                    raise RuntimeFailure("the application volume could not be created", "storage", made.output)
            return None
        try:
            driver, options, labels, created_at, mountpoint = inspect.output.strip().split("|", 4)
            options, labels = json.loads(options) or {}, json.loads(labels) or {}
        except (ValueError, TypeError):
            raise RuntimeFailure("the application volume could not be understood", "storage") from None
        if (driver != "local" or options or not isinstance(labels, dict) or not created_at
                or not mountpoint.startswith("/")):
            raise RuntimeFailure("the application volume does not match what Shiplord manages; nothing was changed", "storage")
        owner = labels.get("shiplord.app")
        if owner == registration["application_id"]:
            return None
        if owner is not None or labels:
            raise RuntimeFailure("the application volume does not match what Shiplord manages; nothing was changed", "storage")

        fingerprint = hashlib.sha256((created_at + "\0" + mountpoint).encode()).hexdigest()
        if (isinstance(provenance, dict) and provenance.get("volume_name") == name
                and provenance.get("fingerprint") == fingerprint):
            return provenance
        retained = self._legacy_volume_container(registration, name)
        if retained is None:
            raise RuntimeFailure("the unlabelled application volume has no retained Shiplord container evidence", "storage")
        return {"volume_name": name, "fingerprint": fingerprint, "container_id": retained}

    def _legacy_volume_container(self, registration: dict, volume_name: str) -> str | None:
        """Return a retained Shiplord container ID only when its labels and exact volume mount agree."""
        for identifier in self._container_ids(registration, "storage"):
            inspected = self._docker(["inspect", "--format",
                                      "{{.Name}}|{{index .Config.Labels \"shiplord.app\"}}|"
                                      "{{index .Config.Labels \"shiplord.release\"}}|"
                                      "{{range .Mounts}}{{.Type}}:{{.Name}}:{{.Destination}} {{end}}", identifier],
                                     "storage")
            if inspected.code != 0:
                raise RuntimeFailure("a retained container could not be inspected for volume provenance", "storage",
                                     inspected.output)
            try:
                retained_name, app_label, release_label, mounts = inspected.output.strip().split("|", 3)
            except ValueError:
                raise RuntimeFailure("a retained container could not be understood for volume provenance", "storage") from None
            if (app_label == registration["application_id"]
                    and len(release_label) == 12 and all(char in "0123456789abcdef" for char in release_label)
                    and retained_name.lstrip("/") == f"shiplord-{registration['application_id']}-{release_label}"
                    and any(mount.startswith(f"volume:{volume_name}:") for mount in mounts.split())):
                return identifier
        return None

    def _container_ids(self, registration: dict, phase: str = "stop") -> list[str]:
        listing = self._docker(["ps", "-aq", "--no-trunc", "--filter",
                                f"label=shiplord.app={registration['application_id']}"], phase)
        if listing.code != 0:
            raise RuntimeFailure("existing containers could not be listed", phase, listing.output)
        identifiers = listing.output.split()
        if any(len(identifier) != 64 or any(char not in "0123456789abcdef" for char in identifier)
               for identifier in identifiers):
            raise RuntimeFailure("unexpected container listing", phase)
        return identifiers

    _INSPECT = ("{{.State.Status}} {{.HostConfig.RestartPolicy.Name}} {{.Config.StopSignal}}"
                "|{{index .Config.Labels \"shiplord.shutdown_timeout\"}}|{{index .Config.Labels \"shiplord.shutdown_action\"}}")

    def _outgoing(self, identifier: str) -> dict:
        result = self._docker(["inspect", "--format", self._INSPECT, identifier], "stop")
        if result.code != 0:
            raise RuntimeFailure("an existing container could not be inspected", "stop", result.output)
        try:
            head, timeout, action = result.output.strip().split("|")
            status, restart, *signal = head.split(" ", 2)
            signal = signal[0] if signal else ""
            if status not in ("created", "running", "paused", "restarting", "removing", "exited", "dead"):
                raise ValueError
        except ValueError:
            raise RuntimeFailure("an existing container reported unusable shutdown metadata", "stop") from None
        absent = ("<no value>", "")
        if timeout in absent and action in absent:  # a container from before the policy existed
            timeout, action = str(DEFAULT_TIMEOUT_SECONDS), DEFAULT_ACTION
        if not (timeout.isdigit() and 1 <= int(timeout) <= 300 and action in ("force", "fail")):
            raise RuntimeFailure("an existing container reported unusable shutdown metadata", "stop")
        return {"id": identifier, "status": status, "restart_policy": restart, "signal": signal,
                "timeout": int(timeout), "action": action}

    def _state(self, identifier: str) -> str:
        result = self._docker(["inspect", "--format", "{{.State.Status}}", identifier], "stop")
        if result.code != 0:
            if "no such" in result.output.lower():
                return "gone"
            raise RuntimeFailure("an existing container could not be inspected", "stop", result.output)
        return result.output.strip()

    def _wait_exit(self, identifier: str, seconds: float) -> bool:
        deadline = self.clock() + seconds
        while True:
            if self._state(identifier) in ("exited", "dead", "created", "gone"):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(POLL_SECONDS)

    def _remove(self, identifier: str) -> None:
        removal = self._docker(["rm", identifier], "stop")  # no --force, no -v: only a stopped container; volumes persist
        if removal.code != 0 and self._state(identifier) != "gone":
            raise RuntimeFailure("a stopped container could not be removed", "stop", removal.output)

    def stop_in_progress(self, registration: dict) -> bool:
        """Is a container still draining after a stop attempt? Containers are created with a restart policy, and the
        stop attempt turns it off before signalling, so a live container without one is mid-shutdown."""
        for identifier in self._container_ids(registration, "inspect"):
            info = self._outgoing(identifier)
            if info["status"] in ("running", "paused", "restarting") and info["restart_policy"] == "no":
                return True
        return False

    def _remove_containers(self, registration: dict, state=None, *, force: bool = False) -> None:
        """Gracefully stop whatever runs for the application (under its own recorded policy), then remove it."""
        self.stop_report = []
        journal = state.read_journal() if state is not None else None
        pending = None if journal is None else journal.get("stop")
        if pending is not None:
            identifier = pending["container_id"]
            if self._state(identifier) == "gone":
                state.clear_stop(identifier)
            else:
                container = self._outgoing(identifier)
                self._stop_gracefully(container, state, pending, force=force)
                self._remove(identifier)
                state.clear_stop(identifier)

        outgoing = [self._outgoing(identifier) for identifier in self._container_ids(registration)
                    if pending is None or identifier != pending["container_id"]]
        live = [c for c in outgoing if c["status"] in ("running", "paused", "restarting")]
        if len(live) > 1:  # nothing is signalled while it is unclear which process is the application
            raise RuntimeFailure("more than one container is running for this application", "stop",
                                 intervention_required=True, kind="ambiguous")
        for container in outgoing:
            if container["status"] in ("running", "paused", "restarting"):
                self._stop_gracefully(container, state, force=force)
            self._remove(container["id"])
            if state is not None:
                journal = state.read_journal()
                record = None if journal is None else journal.get("stop")
                if record is not None and record["container_id"] == container["id"]:
                    state.clear_stop(container["id"])

    def _stop_gracefully(self, container: dict, state=None, record: dict | None = None, *, force: bool = False) -> None:
        identifier, started = container["id"], self.clock()
        signal = stop_signal(container["signal"])  # validate before changing restart policy or signalling
        if container["status"] == "paused":
            raise RuntimeFailure("the container is paused and cannot handle a shutdown signal", "stop",
                                 intervention_required=True, kind="paused")
        if record is None and state is not None:
            journal = state.read_journal()
            record = None if journal is None else journal.get("stop")
        if record is not None:
            if (record["container_id"] != identifier or record["timeout_seconds"] != container["timeout"]
                    or record["action"] != container["action"] or record["signal"] != signal):
                raise RuntimeFailure("the saved shutdown intent does not match the container", "stop",
                                     intervention_required=True, kind="shutdown_record_mismatch")
        elif container["restart_policy"] == "no" and state is not None and not force:
            # A prior helper may have disabled restart before this version added durable stop intent.
            raise RuntimeFailure("the container has a shutdown in progress without a saved intent; manual recovery is required",
                                 "stop", intervention_required=True, kind="shutdown_in_progress")
        elif container["restart_policy"] == "no":
            # Preserve the standalone runtime behavior for callers without a transaction store. Deployer uses the
            # durable path above, so production recovery always has a saved deadline and phase. A forced stop may
            # reach this legacy state: its intent is in memory only, because a record cannot say restart policy "no".
            deadline = self.wall_clock() if container["action"] == "fail" else self.wall_clock() + container["timeout"]
            record = {"container_id": identifier, "restart_policy": "no", "timeout_seconds": container["timeout"],
                      "action": container["action"], "signal": signal, "deadline_epoch": math.ceil(deadline),
                      "phase": "signalled"}
            state = None  # nothing to advance or clear: this intent is never persisted
        else:
            record = {"container_id": identifier, "restart_policy": container["restart_policy"],
                      "timeout_seconds": container["timeout"], "action": container["action"], "signal": signal,
                      "deadline_epoch": math.ceil(self.wall_clock() + container["timeout"]), "phase": "intent"}
            if state is not None:
                state.write_stop(record)
        report = {"timeout_seconds": container["timeout"], "action": container["action"], "stages": ["stop_requested"],
                  "forced": False}
        self.stop_report.append(report)
        phase = record["phase"]
        if phase == "intent":
            if container["restart_policy"] != "no":
                update = self._docker(["update", "--restart=no", identifier], "stop")
                if update.code != 0:
                    raise RuntimeFailure("the container's restart policy could not be disabled", "stop", update.output)
            record = self._advance_stop(state, record, "restart_disabled")
            phase = record["phase"]
        if phase == "restart_disabled":
            record = self._advance_stop(state, record, "signal_pending")
            phase = record["phase"]
        if phase == "signal_pending":
            current = self._state(identifier)
            if current not in ("exited", "dead", "gone", "created"):
                sent = self._docker(["kill", "--signal", signal, identifier], "stop")
                if sent.code != 0 and self._state(identifier) not in ("exited", "dead", "gone", "created"):
                    raise RuntimeFailure("the shutdown signal could not be sent", "stop", sent.output,
                                         intervention_required=True, kind="signal_unconfirmed")
            record = self._advance_stop(state, record, "signalled")
            phase = record["phase"]
            report["stages"].append("signalled")
        report["stages"].append("waiting")
        remaining = 0 if force else max(0, min(record["timeout_seconds"], record["deadline_epoch"] - self.wall_clock()))
        if self._wait_exit(identifier, remaining):
            report["stages"].append("exited")
            report["elapsed_seconds"] = round(self.clock() - started, 1)
            return
        if record["action"] == "fail" and not force:
            self._expired(record, report, started)
        if force:
            report["stages"].append("force_requested")
        if phase in ("signalled", "kill_pending"):
            if phase != "kill_pending":
                record = self._advance_stop(state, record, "kill_pending")
            current = self._state(identifier)
            if current not in ("exited", "dead", "gone", "created"):
                killed = self._docker(["kill", "--signal", "KILL", identifier], "stop")
                if killed.code != 0 and self._state(identifier) not in ("exited", "dead", "gone", "created"):
                    raise RuntimeFailure("the container could not be killed after its shutdown deadline", "stop",
                                         killed.output, intervention_required=True, kind="kill_unconfirmed")
            record = self._advance_stop(state, record, "killed")
            report["forced"] = True
            report["stages"].append("forced")
        elif phase == "killed":
            # A worker may have stopped after KILL was sent but before exit was confirmed.
            current = self._state(identifier)
            if current not in ("exited", "dead", "gone", "created"):
                killed = self._docker(["kill", "--signal", "KILL", identifier], "stop")
                if killed.code != 0 and self._state(identifier) not in ("exited", "dead", "gone", "created"):
                    raise RuntimeFailure("the container could not be killed after its shutdown deadline", "stop",
                                         killed.output, intervention_required=True, kind="kill_unconfirmed")
            report["forced"] = True
        else:
            raise RuntimeFailure("the saved shutdown phase is invalid", "stop", intervention_required=True,
                                 kind="shutdown_record_mismatch")
        if not self._wait_exit(identifier, FORCE_EXIT_SECONDS):
            raise RuntimeFailure("the container did not exit after being killed", "stop", intervention_required=True,
                                 kind="kill_unconfirmed")
        report["elapsed_seconds"] = round(self.clock() - started, 1)

    @staticmethod
    def _advance_stop(state, record: dict, phase: str) -> dict:
        updated = dict(record, phase=phase)
        if state is not None:
            state.write_stop(updated)
        return updated

    def _expired(self, record: dict, report: dict, started: float) -> None:
        report["stages"].append("deadline_expired")
        report["elapsed_seconds"] = round(self.clock() - started, 1)
        raise RuntimeFailure(f"the application did not stop within {record['timeout_seconds']} seconds and its policy is not "
                             "to force it; it was signalled and may still exit. Run `shiplord recover` once it has, or "
                             "`shiplord recover --force-stop` to kill it",
                             "stop", intervention_required=True, kind="shutdown_timeout")



    def stop(self, registration: dict) -> None:
        self._remove_containers(registration)

    def stop_with_state(self, registration: dict, state) -> None:
        """Resume or begin a journaled graceful stop for a locked deployment transaction."""
        self._remove_containers(registration, state)

    def force_stop(self, registration: dict, state=None) -> None:
        """Kill and remove whatever runs for the application, overriding a ``fail`` shutdown deadline.

        This is the explicit escape hatch behind ``shiplord recover --force-stop``: the graceful signal is still
        sent first, but the wait is zero and the deadline refusal is skipped, so the existing ``kill_pending`` ->
        ``killed`` path sends ``SIGKILL`` and confirms the exit before the stop record is cleared.
        """
        self._remove_containers(registration, state, force=True)

    def start(self, registration: dict, release: dict) -> None:
        self._start(registration, release)

    def start_with_provenance(self, registration: dict, release: dict, provenance: dict) -> None:
        """Start a release using legacy-volume proof persisted before its retained container was removed."""
        self._start(registration, release, provenance)

    def start_with_state(self, registration: dict, release: dict, state, provenance: dict | None = None) -> None:
        """Start a release while persisting shutdown progress in its transaction journal."""
        self._start(registration, release, provenance, state)

    def _start(self, registration: dict, release: dict, provenance: dict | None = None, state=None) -> None:
        """Replace whatever runs for this application with a container for ``release``."""
        config = release["configuration"]
        http = config["http"]
        try:
            custom = load_environment_file(self.layout.environments / registration["application_id"] / "app.env")
        except EnvironmentError_ as error:
            raise RuntimeFailure(f"the environment file is unusable: {error}", "start") from None
        # Before the running release is touched: a restore must fetch a pruned image while the old one still serves.
        self._ensure_image(registration, release["artifact"]["image"])
        self.check_volume(registration, create=True, provenance=provenance)  # before the running release is touched
        self._remove_containers(registration, state)
        timeout, action = policy(config)
        memory = f"{config['resources']['memory_mb']}m"
        argv = ["run", "--detach", "--name", self.container_name(registration, release),
                "--label", f"shiplord.app={registration['application_id']}",
                "--label", f"shiplord.release={release_id(release)}",
                "--label", f"shiplord.template={config['template']}",
                "--label", f"shiplord.shutdown_timeout={timeout}", "--label", f"shiplord.shutdown_action={action}",
                "--restart", RESTART_POLICY, "--memory", memory, "--memory-swap", memory,
                "--pids-limit", str(PIDS_LIMIT), "--log-driver", LOG_DRIVER]
        for key, value in LOG_OPTIONS.items():
            argv += ["--log-opt", f"{key}={value}"]
        for option in SECURITY_OPTIONS:
            argv += ["--security-opt", option]
        for capability in CAP_DROP:
            argv += ["--cap-drop", capability]
        argv += ["--stop-timeout", str(timeout),
                 "--publish", f"127.0.0.1:{registration['host_port']}:{http['container_port']}",
                 "--volume", f"{self.volume_name(registration)}:{container_path(config)}"]
        if custom is not None:
            argv += ["--env-file", str(self.layout.environments / registration["application_id"] / "app.env")]
        for key, value in _managed_environment(registration, config).items():  # after --env-file, so they win
            argv += ["--env", f"{key}={value}"]
        argv.append(release["artifact"]["image"])
        result = self._docker(argv, "start")
        if result.code != 0:
            raise RuntimeFailure("the container could not be started", "start", result.output)

    _RUNNING_INSPECT = (
        "{{.State.Running}} {{.State.Restarting}} {{.Config.Image}}"
        "|{{index .Config.Labels \"shiplord.shutdown_timeout\"}}"
        "|{{index .Config.Labels \"shiplord.shutdown_action\"}}"
        "|{{index .Config.Labels \"shiplord.template\"}}"
        "|{{range .Mounts}} {{.Type}}:{{.Name}}:{{.Destination}}{{end}}"
        "|{{.HostConfig.Memory}}"
        "|{{.HostConfig.MemorySwap}}"
        "|{{if .HostConfig.PidsLimit}}{{.HostConfig.PidsLimit}}{{else}}0{{end}}"
        "|{{.HostConfig.RestartPolicy.Name}}"
        "|{{json .HostConfig.SecurityOpt}}"
        "|{{json .HostConfig.CapDrop}}"
        "|{{.HostConfig.LogConfig.Type}}"
        "|{{json .HostConfig.LogConfig.Config}}"
        "|{{json .HostConfig.PortBindings}}"
        "|{{json .Config.Env}}"
    )
    _RUNNING_FIELDS = 14  # separators before the JSON environment, which is last so a value may contain "|"

    def is_running(self, registration: dict, release: dict) -> bool:
        """Is the container running with every value this release's start consumes?

        The container name does not encode configuration, so a name and a readiness answer alone could accept a
        container that still runs the previous memory limit, port, origins, or environment. A crash-looping container
        reports Running=true while Docker waits to restart it; that is not "running" either. Anything absent or
        unreadable is a mismatch: restarting is always safe and writes the committed configuration.

        The one exception is the operator's own environment file: when it cannot be read there is nothing to compare
        against, and a start would fail on that same file. The record-derived values above still decide, so a
        candidate that already replaced them is caught; a running release is not reported down for a file a restart
        could not fix.
        """
        result = self._docker(["inspect", "--format", self._RUNNING_INSPECT,
                               self.container_name(registration, release)], "inspect")
        if result.code != 0:
            return False
        parts = result.output.strip().split("|", self._RUNNING_FIELDS)
        if len(parts) != self._RUNNING_FIELDS + 1:
            return False
        (head, timeout_label, action_label, template, mount_list, memory, memory_swap, pids_limit, restart_policy,
         security_opt, cap_drop, log_driver, log_options, ports, environment) = parts
        fields = head.split()
        if len(fields) != 3:
            return False
        running, restarting, image = fields
        absent = ("<no value>", "")
        if timeout_label in absent and action_label in absent:
            actual_policy = (DEFAULT_TIMEOUT_SECONDS, DEFAULT_ACTION)
        elif timeout_label.isdigit() and 1 <= int(timeout_label) <= 300 and action_label in ("force", "fail"):
            actual_policy = (int(timeout_label), action_label)
        else:
            return False
        # Containers from helpers predating profiles have no label and use the ADK contract.
        template = "google-adk" if template in absent else template
        configuration = release["configuration"]
        expected = f"volume:{self.volume_name(registration)}:{container_path(configuration)}"
        # Docker adds one anonymous mount for every VOLUME instruction in an image. Those are compatible when the
        # Shiplord-managed volume is still present exactly once at the configured path.
        try:
            custom = load_environment_file(self.layout.environments / registration["application_id"] / "app.env")
        except EnvironmentError_:
            custom = None  # unreadable: only the record-derived values can decide; the next start reports the file
        expected_environment = {**(custom or {}), **_managed_environment(registration, configuration)}
        try:
            security = json.loads(security_opt) or []
            capabilities = json.loads(cap_drop) or []
            log_config = json.loads(log_options) or {}
            bindings = json.loads(ports)
            actual_environment = _environment_dict(environment)
        except (TypeError, ValueError):
            return False
        if not isinstance(security, list) or not isinstance(capabilities, list) or not isinstance(log_config, dict):
            return False
        memory_mb = configuration["resources"]["memory_mb"]
        return ((running, restarting) == ("true", "false")
                and _digest(image) == _digest(release["artifact"]["image"])
                and actual_policy == policy(configuration)
                and template == configuration["template"]
                and mount_list.split().count(expected) == 1
                and _integer(memory) == memory_mb * 1024 * 1024
                and _integer(memory_swap) == memory_mb * 1024 * 1024
                and _integer(pids_limit) == PIDS_LIMIT
                and restart_policy == RESTART_POLICY
                and _security_options(security) == set(SECURITY_OPTIONS)
                and set(capabilities) == set(CAP_DROP)
                and log_driver == LOG_DRIVER
                and log_config == LOG_OPTIONS
                and bindings == {f"{configuration['http']['container_port']}/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": str(registration["host_port"])}]}
                and _environment_matches(actual_environment, expected_environment))

    def logs(self, registration: dict, release: dict | None, tail: int, *, since: str | None = None,
             timestamps: bool = False) -> str:
        """The current release's container only: a stopped container of an older release is never read."""
        tail = max(1, min(int(tail), MAX_LOG_LINES))
        identifier = self._current_container(registration, release)
        if identifier is None:
            return ""
        argv = ["logs", "--tail", str(tail)]
        if since is not None:
            argv += ["--since", since]
        if timestamps:
            argv += ["--timestamps"]
        argv.append(identifier)
        result = self._docker(argv, "logs", max_output=MAX_LOG_BYTES)
        return result.output

    def follow_logs(self, registration: dict, release: dict | None, *, tail: int, since: str | None = None,
                    timestamps: bool = False, duration: int = DEFAULT_FOLLOW_SECONDS,
                    emit: Callable[[str], None] = lambda chunk: None) -> str:
        """Stream the current release's container until ``duration`` expires; returns the stop reason."""
        tail = max(1, min(int(tail), MAX_LOG_LINES))
        identifier = self._current_container(registration, release)
        if identifier is None:
            return "unavailable"
        argv = ["logs", "--follow", "--tail", str(tail)]
        if since is not None:
            argv += ["--since", since]
        if timestamps:
            argv += ["--timestamps"]
        argv.append(identifier)
        try:
            return self.system.stream([DOCKER, *argv], timeout=duration, on_chunk=emit)
        except CommandError as error:
            raise RuntimeFailure(f"docker could not complete ({error.kind})", "logs") from None

    def _current_container(self, registration: dict, release: dict | None, phase: str = "logs") -> str | None:
        """Select exactly the current release's container by label; None means there is nothing to read."""
        if release is None:
            return None
        listing = self._docker(["ps", "-aq", "--no-trunc",
                                "--filter", f"label=shiplord.app={registration['application_id']}",
                                "--filter", f"label=shiplord.release={release_id(release)}"], phase)
        identifiers = listing.output.split()
        if listing.code != 0 or not identifiers:
            return None
        if any(len(identifier) != 64 or any(char not in "0123456789abcdef" for char in identifier)
               for identifier in identifiers):
            return None
        return identifiers[0]


    def release_logs(self, registration: dict, release: dict, tail: int, *, max_bytes: int = MAX_LOG_BYTES) -> str | None:
        """Read this release's own container's last ``tail`` lines; ``None`` means it has no container.

        ``logs`` reports whichever container is listed first, which after a rollback is the restored release. A failed
        candidate's container is removed by the restore, so its output is only readable by name, before that happens.
        """
        tail = max(1, min(int(tail), MAX_LOG_LINES))
        result = self._docker(["logs", "--tail", str(tail), self.container_name(registration, release)], "logs",
                              max_output=max_bytes)
        if result.code == 0:
            return result.output
        if "no such" in result.output.lower():
            return None
        raise RuntimeFailure("the release's container logs could not be read", "logs")

    def discard(self, registration: dict, release: dict) -> list[str]:
        """Best-effort removal of a release's image after it left the retained set; returns warnings."""
        result = self._docker(["image", "rm", release["artifact"]["image"]], "cleanup")
        return [] if result.code == 0 else ["an old image could not be removed (it may still be in use)"]
