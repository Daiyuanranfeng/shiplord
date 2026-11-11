"""Root-owned application registrations and loopback port allocation.

A registration fixes an application's identity, runtime, public hostname,
allowed image repository, and loopback host port. Routine deploy requests
cannot change these values; changing them needs a separate reviewed operation.
"""

from __future__ import annotations

import os
import re
import socket
from typing import Callable, Sequence

from . import registries
from .manifest import ManifestError, validate_app_name, validate_hostname, validate_hostnames
from .release_state import HostLock, StateError, _directory, _read, _write

PORT_RANGE = range(20000, 30000)
DEFAULT_MAX_MEMORY_MB = 4096  # the ceiling a deploy request may ask for; the administrator sets it at provisioning
MAX_REGISTRATION_BYTES = 4096
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_FIELDS = {"schema_version", "application_id", "deployment_uid", "deployment_user", "runtime",
           "api_domain", "hostnames", "host_port", "image_repository", "max_memory_mb"}
# Registrations written before an application could answer on more than one hostname have no "hostnames".
_OPTIONAL_FIELDS = {"hostnames"}
_IDENTITY_FIELDS = _FIELDS - {"host_port"}
# An update may change the public hostname and the memory ceiling; everything else is fixed at provisioning.
_IMMUTABLE_FIELDS = {"schema_version", "application_id", "deployment_uid", "deployment_user", "runtime",
                     "image_repository"}


class RegistryError(StateError):
    """A registration is invalid, conflicts with another, or cannot be stored."""


def validate_registration(value: object, application_id: str | None = None) -> dict:
    """Validate a parsed registration; optionally require a specific application."""
    if not isinstance(value, dict) or not _FIELDS - _OPTIONAL_FIELDS <= set(value) <= _FIELDS:
        raise RegistryError("invalid registration fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RegistryError("incompatible registration version")
    try:
        app = validate_app_name(value["application_id"])
        domain = validate_hostname(value["api_domain"], "api_domain")
        hostnames = validate_hostnames(domain, value.get("hostnames", []), "hostnames")
    except ManifestError:
        raise RegistryError("invalid registration identity") from None
    if application_id is not None and app != application_id:
        raise RegistryError("registration does not match the application")
    uid = value["deployment_uid"]
    if type(uid) is not int or not 1 <= uid <= 2**31 - 1:
        raise RegistryError("invalid deployment UID")
    user = value["deployment_user"]
    if not isinstance(user, str) or not _USER.fullmatch(user):
        raise RegistryError("invalid deployment user")
    runtime = value["runtime"]
    repository = value["image_repository"]
    if runtime == "docker":
        try:
            repository = registries.validate_repository(repository)
        except registries.RegistryPolicyError:
            raise RegistryError("a Docker application needs an allowed image repository") from None
    elif runtime == "native":
        if repository is not None:
            raise RegistryError("a native application has no image repository")
    else:
        raise RegistryError("invalid runtime")
    port = value["host_port"]
    if type(port) is not int or port not in PORT_RANGE:
        raise RegistryError("host port is outside the managed range")
    memory = value["max_memory_mb"]
    if type(memory) is not int or not 16 <= memory <= 1048576:
        raise RegistryError("the memory ceiling must be between 16 and 1048576 MiB")
    result = {"schema_version": 1, "application_id": app, "deployment_uid": uid, "deployment_user": user,
              "runtime": runtime, "api_domain": domain, "host_port": port, "image_repository": repository,
              "max_memory_mb": memory}
    if hostnames:  # absent means none, so a registration written before aliases existed reads back unchanged
        result["hostnames"] = hostnames
    return result


def _hostnames(record: dict) -> list[str]:
    """Every hostname a registration claims: the canonical one and the aliases that redirect to it."""
    return [record["api_domain"], *record.get("hostnames", [])]


def registration_request(application_id: str, *, deployment_uid: int, deployment_user: str, runtime: str,
                         api_domain: str, image_repository: str | None,
                         max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
                         hostnames: Sequence[str] = (), host_port: int = PORT_RANGE.start) -> dict:
    """Build the record a registration request asks for; the caller supplies a real port when it allocates one."""
    requested = {"schema_version": 1, "application_id": application_id, "deployment_uid": deployment_uid,
                 "deployment_user": deployment_user, "runtime": runtime, "api_domain": api_domain,
                 "host_port": host_port, "image_repository": image_repository, "max_memory_mb": max_memory_mb}
    if hostnames:  # absent means none, so a record without aliases stays byte-identical
        requested["hostnames"] = list(hostnames)
    return requested


def validate_update(existing: dict, requested: dict) -> dict:
    """Return the record an update would write; refuse a change to anything fixed at provisioning."""
    if existing is None:
        raise RegistryError("the application is not registered; run `shiplord provision` first")
    for field in sorted(_IMMUTABLE_FIELDS):
        if existing[field] != requested[field]:
            name = field.replace("_", " ")
            raise RegistryError(f"registration {name} differs; an update cannot change it")
    return validate_registration(dict(requested, host_port=existing["host_port"]), existing["application_id"])


def port_is_free(port: int) -> bool:
    """Report whether nothing currently listens on the loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class Registry:
    """Registrations in a directory whose full path was verified by the caller."""

    def __init__(self, directory: int, lock: HostLock):
        _directory(directory)
        self.directory = directory
        self.lock = lock
        self.stray: list[str] = []

    def read(self, application_id: str) -> dict | None:
        app = validate_app_name(application_id)
        value = _read(self.directory, app + ".json", missing_ok=True)
        return None if value is None else validate_registration(value, app)

    def list(self) -> list[dict]:
        """Return every registration; files that cannot be registrations are skipped and named in ``stray``.

        A stray file (an editor backup, a swap file, anything not ending in ``.json``) must not take out every
        administrative command. It is reported by name instead. A ``.json`` record that is corrupt or unsafe still
        fails closed: only noise is skipped, never a registration.
        """
        records = []
        self.stray = []
        for name in sorted(os.listdir(self.directory)):
            if name.startswith(".tmp-"):
                continue
            if not name.endswith(".json"):
                self.stray.append(name)
                continue
            try:
                record = self.read(name[: -len(".json")])
            except ManifestError:
                raise RegistryError("unexpected file in the registry") from None
            if record is None:
                raise RegistryError("registration disappeared while listing")
            records.append(record)
        return records

    def register(self, application_id: str, *, deployment_uid: int, deployment_user: str, runtime: str,
                 api_domain: str, hostnames: Sequence[str] = (), image_repository: str | None,
                 max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
                 probe: Callable[[int], bool] = port_is_free) -> dict:
        """Create a registration, or return the identical existing one."""
        if not self.lock.held:
            raise RegistryError("host lock is required")
        requested = self._requested(application_id, deployment_uid=deployment_uid, deployment_user=deployment_user,
                                    runtime=runtime, api_domain=api_domain, hostnames=hostnames,
                                    image_repository=image_repository, max_memory_mb=max_memory_mb)
        existing, others = self._compared(requested)
        if existing is not None:
            return existing
        used = {record["host_port"] for record in others}
        for port in PORT_RANGE:
            if port not in used and probe(port):
                record = validate_registration(dict(requested, host_port=port), requested["application_id"])
                _write(self.directory, requested["application_id"] + ".json", record, create=True)
                return record
        raise RegistryError("no loopback port is available")

    def check(self, application_id: str, *, deployment_uid: int | None, deployment_user: str, runtime: str,
              api_domain: str, hostnames: Sequence[str] = (), image_repository: str | None,
              max_memory_mb: int = DEFAULT_MAX_MEMORY_MB) -> dict | None:
        """Validate a prospective registration without writing; return the identical existing one, if any.

        This is the read half of ``register``, for a caller that must refuse before its first side effect. A
        ``None`` UID means the deployment account does not exist yet: a new account cannot be identical to an
        existing registration, so an unknown UID never matches one.
        """
        if not self.lock.held:
            raise RegistryError("host lock is required")
        requested = self._requested(application_id, deployment_uid=deployment_uid, deployment_user=deployment_user,
                                    runtime=runtime, api_domain=api_domain, hostnames=hostnames,
                                    image_repository=image_repository, max_memory_mb=max_memory_mb)
        return self._compared(requested)[0]

    def _requested(self, application_id: str, *, deployment_uid: int | None, deployment_user: str, runtime: str,
                   api_domain: str, hostnames: Sequence[str], image_repository: str | None,
                   max_memory_mb: int) -> dict:
        """The prospective record, validated; an unknown UID is validated as a placeholder and then cleared."""
        app = validate_app_name(application_id)
        requested = registration_request(app, deployment_uid=deployment_uid, deployment_user=deployment_user,
                                         runtime=runtime, api_domain=api_domain, hostnames=hostnames,
                                         image_repository=image_repository, max_memory_mb=max_memory_mb)
        # Keep the validated record: it holds the registry policy's stored spelling, so a comparison with an
        # existing registration cannot fail merely because the same image was typed in another accepted form.
        requested = validate_registration(dict(requested, deployment_uid=1 if deployment_uid is None else deployment_uid))
        requested["deployment_uid"] = deployment_uid
        return requested

    def _compared(self, requested: dict) -> tuple[dict | None, list[dict]]:
        """Refuse a conflicting record; return the identical existing one (or None) and the other records."""
        app = requested["application_id"]
        others = [record for record in self.list() if record["application_id"] != app]
        existing = self.read(app)
        if existing is not None:
            if any(existing.get(field) != requested.get(field) for field in _IDENTITY_FIELDS):
                raise RegistryError("registration differs; changing it needs an explicit update")
        else:
            claimed = set(_hostnames(requested))
            for record in others:
                if claimed & set(_hostnames(record)):
                    raise RegistryError("hostname is already registered to another application")
                if (record["deployment_uid"] == requested["deployment_uid"]
                        or record["deployment_user"] == requested["deployment_user"]):
                    raise RegistryError("deployment account is already registered to another application")
        return existing, others

    def update(self, application_id: str, *, deployment_uid: int, deployment_user: str, runtime: str,
               api_domain: str, image_repository: str | None, max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
               hostnames: Sequence[str] = (), dry_run: bool = False) -> tuple[dict, bool]:
        """Change an existing registration's hostname or memory ceiling, keeping its identity and port.

        With ``dry_run`` every check runs and the record that would be written is returned, but nothing is written;
        a caller uses it to refuse an update before its first side effect.
        """
        if not self.lock.held:
            raise RegistryError("host lock is required")
        app = validate_app_name(application_id)
        existing = self.read(app)
        requested = registration_request(app, deployment_uid=deployment_uid, deployment_user=deployment_user,
                                         runtime=runtime, api_domain=api_domain, hostnames=hostnames,
                                         image_repository=image_repository, max_memory_mb=max_memory_mb)
        updated = validate_update(existing, requested)
        claimed = set(_hostnames(updated))
        for record in self.list():
            if record["application_id"] != app and claimed & set(_hostnames(record)):
                raise RegistryError("hostname is already registered to another application")
        if updated == existing:
            return existing, False
        if not dry_run:
            _write(self.directory, app + ".json", updated, create=False)
        return updated, True

    def unregister(self, application_id: str) -> bool:
        """Remove a registration, which disables the application's access; idempotent."""
        if not self.lock.held:
            raise RegistryError("host lock is required")
        app = validate_app_name(application_id)
        try:
            os.unlink(app + ".json", dir_fd=self.directory)
            os.fsync(self.directory)
        except FileNotFoundError:
            return False
        except OSError:
            raise RegistryError("could not remove the registration") from None
        return True
