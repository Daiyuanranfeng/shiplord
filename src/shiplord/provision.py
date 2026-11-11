"""Idempotent application provisioning, run by root on behalf of an administrator.

Each step can be repeated. The registration is written last, because it is what
turns on the application's access through the privileged helper; a failure part
way leaves an inert application that a repeat run completes.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable, Sequence

from . import registries
from .environment import (EnvironmentError_, edit_environment, load_environment_file, load_environment_text,
                          parse_environment, render_environment, validate_key, validate_value)
from .installer import DEPLOY_GROUP, DEPLOY_MARKER
from .keys import KeyError_, KeyStore, PublicKey, parse_public_key, validate_label
from .layout import Layout, deployment_user, runtime_user
from .manifest import validate_app_name
from .registry import DEFAULT_MAX_MEMORY_MB, Registry, registration_request, validate_registration
from .release_state import HostLock, ReleaseState, StateError, _directory
from .routing import RoutingError
from .system import CommandError, System

USERADD, USERMOD, USERDEL = "/usr/sbin/useradd", "/usr/sbin/usermod", "/usr/sbin/userdel"
LOGINCTL = "/usr/bin/loginctl"
USERDEL_LOGGED_IN = 8  # userdel(8): the account is still logged in or has running processes
USERDEL_ATTEMPTS = 10
GETENT, VISUDO = "/usr/bin/getent", "/usr/sbin/visudo"
SUDOERS_RULE = '{user} ALL=(root) NOPASSWD: NOSETENV: {helper} ""\n'


class ProvisionError(Exception):
    """Provisioning failed; the message names the step."""


def _marker(application_id: str) -> str:
    """The account comment that marks an account as ours; passwd comments cannot contain a colon."""
    return f"{DEPLOY_MARKER}-{application_id}"


def _run(system: System, argv: list[str], step: str):
    try:
        return system.run(argv)
    except CommandError:
        raise ProvisionError(f"could not run {step}") from None


def _account(system: System, name: str) -> dict | None:
    result = _run(system, [GETENT, "passwd", name], "getent")
    if result.code == 2:
        return None
    fields = result.output.strip().split(":")
    if result.code != 0 or len(fields) != 7:
        raise ProvisionError("could not look up the deployment account")
    return {"name": fields[0], "uid": int(fields[2]), "comment": fields[4], "home": fields[5], "shell": fields[6]}


def _is_ours(account: dict, marker: str, home: str, shell: str) -> bool:
    """Whether an existing account is the one provisioning would have created for this application."""
    return (account["comment"] == marker and account["home"] == home and account["shell"] == shell
            and account["uid"] >= 1)


def _existing_user(system: System, name: str, marker: str, home: str, shell: str) -> dict | None:
    """Read an account without changing it, refusing one that is not ours; None when it does not exist."""
    account = _account(system, name)
    if account is not None and not _is_ours(account, marker, home, shell):
        raise ProvisionError("an account with that name exists but is not a Shiplord account")
    return account


def _ensure_user(system: System, name: str, marker: str, home: str, shell: str) -> tuple[dict, bool]:
    """Create a system account, or verify that an existing one is ours; never adopt a foreign account."""
    account = _account(system, name)
    created = False
    if account is None:
        for argv, step in (([USERADD, "--system", "--user-group", "--no-create-home", "--home-dir", home,
                             "--shell", shell, "--comment", marker, name], "useradd"),
                           ([USERMOD, "--password", "*", name], "usermod")):
            if _run(system, argv, step).code != 0:
                raise ProvisionError(f"{step} failed for the account {name}")
        account, created = _account(system, name), True
    if account is None or not _is_ours(account, marker, home, shell):
        raise ProvisionError("an account with that name exists but is not a Shiplord account")
    return account, created


def _existing_account(system: System, application_id: str) -> dict | None:
    return _existing_user(system, deployment_user(application_id), _marker(application_id), "/", "/bin/sh")


def _ensure_account(system: System, application_id: str) -> tuple[dict, bool]:
    return _ensure_user(system, deployment_user(application_id), _marker(application_id), "/", "/bin/sh")


def _ensure_group_membership(system: System, name: str) -> bool:
    """Put a deployment account in the group the sshd drop-in matches; idempotent.

    This runs before the account's first key is installed, so a failure here leaves an account that cannot log in
    rather than one that can log in without the forced command.
    """
    result = _run(system, [GETENT, "group", DEPLOY_GROUP], "getent")
    if result.code == 2:
        raise ProvisionError("the deployment group is missing; bootstrap the server again")
    fields = result.output.strip().split(":")
    if result.code != 0 or len(fields) < 4 or fields[0] != DEPLOY_GROUP:
        raise ProvisionError("could not read the deployment group")
    if name in fields[3].split(","):
        return False
    if _run(system, [USERMOD, "--append", "--groups", DEPLOY_GROUP, name], "usermod").code != 0:
        raise ProvisionError("could not add the deployment account to the deployment group")
    return True


def _runtime_marker(application_id: str) -> str:
    return f"shiplord-runtime-{application_id}"


def _existing_runtime_account(system: System, application_id: str) -> dict | None:
    return _existing_user(system, runtime_user(application_id), _runtime_marker(application_id), "/nonexistent",
                          "/usr/sbin/nologin")


def _ensure_runtime(layout: Layout, system: System, application_id: str) -> dict[str, bool]:
    """A native application runs as its own unprivileged account, with writable data outside releases."""
    account, created = _ensure_user(system, runtime_user(application_id), _runtime_marker(application_id),
                                    "/nonexistent", "/usr/sbin/nologin")
    data = layout.data / application_id
    made = not data.exists()
    try:
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        if data.is_symlink():
            raise ProvisionError(f"{data} is a symbolic link")
        os.chmod(data, 0o700)
        if os.geteuid() == 0:
            os.chown(data, account["uid"], int(_run(system, [GETENT, "passwd", account["name"]], "getent")
                                              .output.split(":")[3]))
        (layout.releases / application_id).mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chmod(layout.releases / application_id, 0o755)
    except OSError:
        raise ProvisionError("could not prepare the application's data and release directories") from None
    return {"runtime_account": created, "data_directory": made}


def _private_directory(path: Path) -> bool:
    created = not path.exists()
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(path)
        if path.is_symlink() or info.st_uid != os.geteuid():
            raise ProvisionError(f"{path} is not a directory owned by the installing account")
        os.chmod(path, 0o700)
    except OSError:
        raise ProvisionError(f"could not prepare {path}") from None
    return created


def _install_sudoers(layout: Layout, system: System, application_id: str, account: str) -> bool:
    target = layout.sudoers_dir / f"shiplord-{application_id}"
    desired = SUDOERS_RULE.format(user=account, helper=layout.host)
    if target.exists() and not target.is_symlink() and target.read_text() == desired:
        return False
    # A name containing a dot is never loaded by sudo, so an interrupted write is inert.
    temporary = target.with_name(f"shiplord.{application_id}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o440)
        with os.fdopen(descriptor, "w") as file:
            file.write(desired)
            file.flush()
            os.fchmod(file.fileno(), 0o440)
            if os.geteuid() == 0:
                os.fchown(file.fileno(), 0, 0)
            os.fsync(file.fileno())
        if _run(system, [VISUDO, "-cf", str(temporary)], "visudo").code != 0:
            raise ProvisionError("sudo rejected the generated policy")
        os.replace(temporary, target)
    except OSError:
        raise ProvisionError("could not install the sudo policy") from None
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ProvisionError(f"{path} is unavailable; has the server been bootstrapped?") from None


def _preflight_update(layout: Layout, system: System, lock: HostLock, app: str, *, runtime: str, api_domain: str,
                      image_repository: str | None, max_memory_mb: int | None, hostnames: Sequence[str] = (),
                      apply_route: Callable[[dict], object] | None) -> tuple[dict, dict, dict | None]:
    """Validate an update before its first side effect; return (previous, planned, deployed release)."""
    account = _account(system, deployment_user(app))
    if account is None or account["comment"] != _marker(app):
        raise ProvisionError("the application is not provisioned; run `shiplord provision` first")
    state_path = layout.app_state / app
    if not state_path.is_dir() or state_path.is_symlink():
        raise ProvisionError("the application's state is missing; run `shiplord provision` first")
    state_fd = _open_directory(state_path)
    try:
        store = ReleaseState(app, state_fd, lock)
        if store.read_journal() is not None:
            raise ProvisionError("an unfinished transaction exists; recover it before updating the application")
        current = store.read_state()["current"]
    except StateError as error:
        raise ProvisionError(f"release state is not usable: {error}") from None
    finally:
        os.close(state_fd)
    registry_fd = _open_directory(layout.registrations)
    try:
        registry = Registry(registry_fd, lock)
        previous = registry.read(app)
        if previous is None:
            raise ProvisionError("the application is not registered; run `shiplord provision` first")
        if previous["deployment_uid"] != account["uid"]:
            raise ProvisionError("the deployment account does not match the registration; "
                                 "deprovision and provision the application again")
        ceiling = previous["max_memory_mb"] if max_memory_mb is None else max_memory_mb
        planned, _ = registry.update(app, deployment_uid=previous["deployment_uid"],
                                     deployment_user=previous["deployment_user"], runtime=runtime,
                                     api_domain=api_domain, hostnames=hostnames,
                                     image_repository=image_repository,
                                     max_memory_mb=ceiling, dry_run=True)
    except StateError as error:
        raise ProvisionError(f"registration failed: {error}") from None
    finally:
        os.close(registry_fd)
    route_moves = planned["api_domain"] != previous["api_domain"] \
        or (planned.get("hostnames") or []) != (previous.get("hostnames") or [])
    if route_moves and current is not None and apply_route is None:
        raise ProvisionError("the public route cannot be moved by this server; run `shiplord server bootstrap` first")
    return previous, planned, current


def provision(layout: Layout, system: System, lock: HostLock, *, application_id: str, runtime: str, api_domain: str,
              image_repository: str | None, public_key: str, key_label: str,
              hostnames: Sequence[str] = (), max_memory_mb: int | None = None, update: bool = False,
              apply_route: Callable[[dict], object] | None = None) -> dict:
    """Create or complete an application's host resources. The caller holds the host lock.

    With ``update`` an existing application keeps its account, port, state, environment, and keys; only its public
    hostname, additional hostnames, and memory ceiling may change. ``max_memory_mb`` left unset keeps the current
    ceiling on an update and uses the default at provisioning. ``apply_route`` moves the public route of a deployed
    application when a hostname changes; it is required for that case and receives the updated registration.
    """
    if not lock.held:
        raise ProvisionError("host lock is required")
    app = validate_app_name(application_id)
    try:
        key = parse_public_key(public_key)
        label = validate_label(key_label)
    except KeyError_ as error:
        raise ProvisionError(str(error)) from None
    ceiling = DEFAULT_MAX_MEMORY_MB if max_memory_mb is None else max_memory_mb
    try:  # reject bad input before the first side effect; uid and port are placeholders here
        validate_registration(registration_request(app, deployment_uid=1, deployment_user=deployment_user(app),
                                                   runtime=runtime, api_domain=api_domain, hostnames=hostnames,
                                                   image_repository=image_repository, max_memory_mb=ceiling))
    except StateError as error:
        raise ProvisionError(f"invalid application settings: {error}") from None
    # Everything that can refuse this provisioning is checked before the first side effect, so a refused
    # re-provision or update leaves no account, key, sudoers rule, directory, or registration behind.
    previous = planned = current = None
    existing = None
    if update:
        previous, planned, current = _preflight_update(layout, system, lock, app, runtime=runtime,
                                                       api_domain=api_domain, hostnames=hostnames,
                                                       image_repository=image_repository,
                                                       max_memory_mb=max_memory_mb, apply_route=apply_route)
        max_memory_mb = planned["max_memory_mb"]
    else:
        max_memory_mb = ceiling
        account = _existing_account(system, app)
        if runtime == "native":
            _existing_runtime_account(system, app)
        registry_fd = _open_directory(layout.registrations)
        try:
            existing = Registry(registry_fd, lock).check(
                app, deployment_uid=None if account is None else account["uid"],
                deployment_user=deployment_user(app), runtime=runtime, api_domain=api_domain,
                hostnames=hostnames, image_repository=image_repository, max_memory_mb=max_memory_mb)
        except StateError as error:
            raise ProvisionError(f"registration failed: {error}") from None
        finally:
            os.close(registry_fd)
    created: dict[str, bool] = {}
    account, created["account"] = _ensure_account(system, app)
    created["deployment_group"] = _ensure_group_membership(system, account["name"])
    if runtime == "native":
        created.update(_ensure_runtime(layout, system, app))
    created["state_directory"] = _private_directory(layout.app_state / app)
    created["environment_directory"] = _private_directory(layout.environments / app)
    state_fd = _open_directory(layout.app_state / app)
    try:
        store = ReleaseState(app, state_fd, lock)
        if not (layout.app_state / app / "state.json").exists():
            store.initialize()
            created["release_state"] = True
        else:
            store.read_state()
            created["release_state"] = False
    except StateError as error:
        raise ProvisionError(f"release state is not usable: {error}") from None
    finally:
        os.close(state_fd)
    try:
        created["key"] = KeyStore(layout.authorized_keys, app, account["name"]).add(label, key)
    except KeyError_ as error:
        raise ProvisionError(str(error)) from None
    created["sudo_policy"] = _install_sudoers(layout, system, app, account["name"])
    route_changed = False
    changed = False
    if update and previous is not None and planned is not None and current is not None \
            and (planned["api_domain"] != previous["api_domain"]
                 or (planned.get("hostnames") or []) != (previous.get("hostnames") or [])):
        try:
            route_changed = bool(getattr(apply_route(planned), "changed", True))
        except RoutingError as error:
            raise ProvisionError(f"could not change the public route: {error}") from None
    registry_fd = _open_directory(layout.registrations)
    try:
        registry = Registry(registry_fd, lock)
        if update:
            record, changed = registry.update(app, deployment_uid=account["uid"], deployment_user=account["name"],
                                              runtime=runtime, api_domain=api_domain, hostnames=hostnames,
                                              image_repository=image_repository, max_memory_mb=max_memory_mb)
            created["registration"] = False
        else:
            record = registry.register(app, deployment_uid=account["uid"], deployment_user=account["name"],
                                       runtime=runtime, api_domain=api_domain, hostnames=hostnames,
                                       image_repository=image_repository, max_memory_mb=max_memory_mb)
            created["registration"] = existing is None
    except StateError as error:
        raise ProvisionError(f"registration failed: {error}") from None
    finally:
        os.close(registry_fd)
    result = {"application_id": app, "deployment_user": account["name"], "host_port": record["host_port"],
              "api_domain": record["api_domain"], "hostnames": record.get("hostnames") or [],
              "runtime": record["runtime"], "max_memory_mb": record["max_memory_mb"],
              "key_fingerprint": key.fingerprint,
              "created": created}
    if update:
        result["updated"] = changed
        result["route_changed"] = route_changed
        changes = {}
        for field in ("api_domain", "hostnames", "max_memory_mb"):
            before, after = previous.get(field), record.get(field)
            if field == "hostnames":
                before, after = before or [], after or []
            if before != after:
                changes[field] = {"from": before, "to": after}
        result["changes"] = changes
    return result


def _require_provisioned(layout: Layout, application_id: str) -> None:
    """Refuse an application that provision has not created; its environment directory marks it as provisioned."""
    directory = layout.environments / application_id
    if not directory.is_dir() or directory.is_symlink():
        raise ProvisionError("the application is not provisioned")


def manage_key(layout: Layout, application_id: str, operation: str, *, label: str | None = None,
               public_key: str | None = None) -> dict:
    """Add, remove, or list keys for a provisioned application."""
    app = validate_app_name(application_id)
    _require_provisioned(layout, app)
    store = KeyStore(layout.authorized_keys, app, deployment_user(app))
    try:
        if operation == "list":
            return {"keys": [{"label": name, "fingerprint": fingerprint} for name, fingerprint in store.list()]}
        label = validate_label(label)
        if operation == "add":
            return {"changed": store.add(label, parse_public_key(public_key))}
        if operation == "remove":
            if len(store.list()) == 1 and store.list()[0][0] == label:
                raise ProvisionError("refusing to remove the last key; add the replacement first")
            return {"changed": store.remove(label)}
    except KeyError_ as error:
        raise ProvisionError(str(error)) from None
    raise ProvisionError("unknown key operation")


def _write_environment_file(path: Path, text: str) -> None:
    """Replace an environment file atomically with a private, owner-only temporary file."""
    temporary = path.with_name(f".app.env.tmp-{secrets.token_hex(6)}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fchmod(file.fileno(), 0o600)
            if os.geteuid() == 0:
                os.fchown(file.fileno(), 0, 0)
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ProvisionError("could not write the environment file") from None
    finally:
        temporary.unlink(missing_ok=True)


def manage_environment(layout: Layout, application_id: str, operation: str, *, text: str | None = None,
                       key: str | None = None, value: str | None = None) -> dict:
    """Set, clear, list, or edit the key names of an application's runtime environment file.

    Values are never returned. They take effect the next time a release is started. ``set-key`` and
    ``unset`` change one line and leave every other line byte-identical; they refuse a file they cannot
    rewrite safely, because discarding it would also discard variables this command was not asked about.
    A whole-file ``set`` or a ``clear`` still repairs an unreadable file, so it never needs a manual root shell.
    """
    app = validate_app_name(application_id)
    if operation in ("set-key", "unset"):  # reject bad input before the directory check or any file read
        try:
            key = validate_key(key)
            if operation == "set-key":
                validate_value(value)
        except EnvironmentError_ as error:
            raise ProvisionError(str(error)) from None
    _require_provisioned(layout, app)
    path = layout.environments / app / "app.env"
    if operation == "clear":
        try:
            load_environment_file(path)  # only to report that the file it removes could not be read
            notice: dict = {}
        except EnvironmentError_:
            notice = {"warnings": ["the previous environment file was unreadable; it was removed"]}
        existed = path.exists() or path.is_symlink()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise ProvisionError("could not remove the environment file") from None
        return {"keys": [], "changed": existed, **notice}
    if operation == "keys":
        try:
            return {"keys": sorted(load_environment_file(path) or {})}
        except EnvironmentError_ as error:
            raise ProvisionError(f"{error}; use `shiplord env clear` to replace it") from None
    if operation in ("set-key", "unset"):
        try:
            current = load_environment_text(path)
        except EnvironmentError_ as error:
            raise ProvisionError(f"{error}; use `shiplord env clear` to replace it") from None
        try:
            updated = edit_environment(current or "", key, value if operation == "set-key" else None)
        except EnvironmentError_ as error:
            raise ProvisionError(str(error)) from None
        changed = updated != (current or "")
        if changed:
            _write_environment_file(path, updated)
        return {"keys": sorted(parse_environment(updated)), "changed": changed}
    if operation != "set":
        raise ProvisionError("unknown environment operation")
    if not isinstance(text, str):
        raise ProvisionError("environment text is required")
    try:
        values = parse_environment(text)
    except EnvironmentError_ as error:
        raise ProvisionError(str(error)) from None
    discarded = False
    try:
        current = load_environment_file(path)
    except EnvironmentError_:
        current, discarded = None, True
    if not discarded and values == current and path.exists():
        return {"keys": sorted(values), "changed": False}
    _write_environment_file(path, render_environment(values))
    notice = ({"warnings": ["the previous environment file was unreadable; its contents were discarded"]}
              if discarded else {})
    return {"keys": sorted(values), "changed": True, **notice}


REGISTRY_CONFIG = "config.json"


def _registry_username(entry: object) -> str:
    """Decode one ``auth`` value; the error names the problem, never the value or a host path."""
    if not isinstance(entry, dict) or not isinstance(entry.get("auth"), str):
        raise ProvisionError(f"the registry login file {REGISTRY_CONFIG} is invalid: an auth entry has no auth value")
    try:
        decoded = base64.b64decode(entry["auth"], validate=True).decode("utf-8")
    except ValueError:
        raise ProvisionError(f"the registry login file {REGISTRY_CONFIG} is invalid: an auth value is not valid "
                             "base64") from None
    username, separator, _ = decoded.partition(":")
    if separator:
        try:
            return registries.validate_username(username)
        except registries.RegistryPolicyError:
            pass
    raise ProvisionError(f"the registry login file {REGISTRY_CONFIG} is invalid: an auth value is not a valid "
                         "username:token")


def _registry_auths(path: Path) -> dict:
    """Read the login file's raw ``auths`` mapping; a hand-edited file is an error, not a traceback."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        raise ProvisionError(f"the registry login file {REGISTRY_CONFIG} is unreadable") from None
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise ProvisionError(f"the registry login file {REGISTRY_CONFIG} is unreadable: it is not valid JSON") from None
    auths = document.get("auths") if isinstance(document, dict) else None
    if not isinstance(auths, dict):
        raise ProvisionError(f"the registry login file {REGISTRY_CONFIG} is invalid: auths is not an object")
    for entry in auths.values():
        _registry_username(entry)
    return auths


def _remove_registry_config(directory: Path) -> None:
    """Remove the login file and sync its directory; a missing file or directory is not an error."""
    if directory.is_symlink():
        raise ProvisionError(f"{directory} is a symbolic link")
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError:
        raise ProvisionError("could not remove the registry login") from None
    try:
        try:
            os.unlink(REGISTRY_CONFIG, dir_fd=descriptor)
            os.fsync(descriptor)
        except FileNotFoundError:
            pass
    except OSError:
        raise ProvisionError("could not remove the registry login") from None
    finally:
        os.close(descriptor)


def _write_registry_config(directory: Path, auths: dict) -> None:
    """Publish the login file atomically and sync its directory; a failure leaves no temporary file."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ProvisionError(f"{directory} is a symbolic link")
        os.chmod(directory, 0o700)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ProvisionError("could not write the registry login") from None
    temporary = f".config.tmp-{secrets.token_hex(6)}"
    created = False
    try:
        file_descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                                  dir_fd=descriptor)
        created = True
        with os.fdopen(file_descriptor, "w") as file:
            json.dump({"auths": auths}, file)
            file.flush()
            if os.geteuid() == 0:
                os.fchown(file.fileno(), 0, 0)
            os.fsync(file.fileno())
        os.replace(temporary, REGISTRY_CONFIG, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        created = False
        os.fsync(descriptor)
    except OSError:
        raise ProvisionError("could not write the registry login") from None
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=descriptor)
        except OSError:
            pass
        os.close(descriptor)


def manage_registry(layout: Layout, application_id: str, operation: str, *, registry: str | None = None,
                    username: str | None = None, token: str | None = None) -> dict:
    """Keep one application's read-only registry login in a root-only docker config directory.

    The login is per application, so a token cannot be reused by another application's deployments. Tokens
    are never returned; ``list`` shows registries and usernames only.
    """
    app = validate_app_name(application_id)
    if not (layout.registrations / f"{app}.json").is_file():
        raise ProvisionError("the application is not provisioned")
    directory = layout.registry_logins / app
    current = _registry_auths(directory / REGISTRY_CONFIG)

    def summary() -> dict:  # registries and usernames only; never a token
        return {"registries": [{"registry": registries.host_for_key(name), "username": _registry_username(entry)}
                               for name, entry in sorted(current.items())]}

    if operation == "list":
        return summary()
    try:
        host = registries.validate_host(registry)
    except registries.RegistryPolicyError as error:
        raise ProvisionError(str(error)) from None
    if operation == "logout":
        for key in registries.login_keys(host):
            current.pop(key, None)
    elif operation == "login":
        try:
            registries.validate_login(host, username, token)
        except registries.RegistryPolicyError as error:
            raise ProvisionError(str(error)) from None
        for key in registries.login_keys(host):  # replace any hand-written bare docker.io entry
            current.pop(key, None)
        current[registries.login_key(host)] = {"auth": base64.b64encode(f"{username}:{token}".encode()).decode()}
    else:
        raise ProvisionError("unknown registry operation")
    if not current:
        _remove_registry_config(directory)
        return summary()
    _write_registry_config(directory, current)
    return summary()


def _remove_account(system: System, name: str, sleep: Callable[[float], None]) -> None:
    """Delete the account; end what keeps it logged in first (its access is already revoked by now).

    After an SSH session closes, pam_systemd's per-user manager can outlive it for a while, and userdel refuses an
    account that still has processes.
    """
    for attempt in range(USERDEL_ATTEMPTS):
        code = _run(system, [USERDEL, name], "userdel").code
        if code == 0:
            return
        if code != USERDEL_LOGGED_IN:
            break
        if attempt == 0:
            try:
                system.run([LOGINCTL, "terminate-user", name])
            except CommandError:
                pass  # no logind: the processes may still exit by themselves
        sleep(1.0)
    raise ProvisionError("could not remove the deployment account; end its sessions and retry")


def _remove_route(layout: Layout, application_id: str) -> bool:
    """Remove the application's managed route and any interrupted-change backup; a missing route directory is fine."""
    try:
        ship = os.open(layout.caddy_root / "ship", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False  # a host without Caddy has nothing routed
    except OSError:
        raise ProvisionError("could not open the managed route directory") from None
    removed = False
    try:
        try:
            _directory(ship)
        except StateError:
            raise ProvisionError("the managed route directory is not trusted") from None
        for name in (f"{application_id}.caddy", f".prior-{application_id}"):
            try:
                os.unlink(name, dir_fd=ship)
                removed = True
            except FileNotFoundError:
                pass
        if removed:
            os.fsync(ship)
    except OSError:
        raise ProvisionError("could not remove the application's route") from None
    finally:
        os.close(ship)
    return removed


def deprovision(layout: Layout, system: System, lock: HostLock, application_id: str, *,
                retire: Callable[[dict, dict], dict | None] | None = None,
                sleep: Callable[[float], None] = time.sleep) -> dict:
    """Remove an application's access and host resources.

    While a release is deployed this refuses, unless ``retire(registration, state)`` is given: it stops the runtime,
    removes the route and discards the releases first, and any failure of it leaves the application registered.
    A report from it is returned under ``retired`` and its retained entries join the result's ``retained`` list.
    """
    if not lock.held:
        raise ProvisionError("host lock is required")
    app = validate_app_name(application_id)
    name = deployment_user(app)
    state_path = layout.app_state / app
    if (state_path / "journal.json").exists():
        raise ProvisionError("an unfinished transaction exists; recover it before removing the application")
    state_file = state_path / "state.json"
    retired = None
    if state_file.exists():
        state_fd = _open_directory(state_path)
        try:
            state = ReleaseState(app, state_fd, lock).read_state()
            if state["current"] is not None:
                if retire is None:
                    raise ProvisionError("a release is deployed; retire it before removing the application")
                registry_fd = _open_directory(layout.registrations)
                try:
                    registration = Registry(registry_fd, lock).read(app)
                finally:
                    os.close(registry_fd)
                if registration is None:
                    raise ProvisionError("the application is not registered")
                retired = retire(registration, state)
        except StateError as error:
            raise ProvisionError(f"release state is not usable: {error}") from None
        finally:
            os.close(state_fd)
    removed: dict[str, bool] = {}
    registry_fd = _open_directory(layout.registrations)
    try:
        removed["registration"] = Registry(registry_fd, lock).unregister(app)  # first: disables access
    except StateError as error:
        raise ProvisionError(str(error)) from None
    finally:
        os.close(registry_fd)
    sudoers = layout.sudoers_dir / f"shiplord-{app}"
    removed["sudo_policy"] = sudoers.exists()
    sudoers.unlink(missing_ok=True)
    keys = layout.authorized_keys / name
    removed["keys"] = keys.exists()
    KeyStore(layout.authorized_keys, app, name).delete()
    account = _account(system, name)
    if account is not None:
        if account["comment"] != _marker(app):
            raise ProvisionError("refusing to remove an account that is not a Shiplord deployment account")
        _remove_account(system, name, sleep)
    removed["account"] = account is not None
    for directory, key in ((layout.app_state / app, "state_directory"), (layout.environments / app, "environment_directory"),
                           (layout.registry_logins / app, "registry_login"), (layout.jobs / app, "jobs_directory")):
        if directory.is_symlink():
            raise ProvisionError(f"{directory} is a symbolic link")
        if directory.exists():
            shutil.rmtree(directory)
            removed[key] = True
        elif key not in ("registry_login", "jobs_directory"):  # an optional item is reported only when it existed
            removed[key] = False
    retained = []
    if isinstance(retired, dict):  # what retirement kept is part of what this command kept
        retained.extend(retired.get("retained", []))
    if (layout.data / app).exists():
        retained.append("data directory and runtime account (they hold application data)")
    if (layout.releases / app).is_symlink():
        raise ProvisionError(f"{layout.releases / app} is a symbolic link")
    if (layout.releases / app).exists():
        shutil.rmtree(layout.releases / app)
        removed["release_binaries"] = True
    if _remove_route(layout, app):
        removed["route"] = True
    unit = layout.systemd_units / f"shiplord-{app}.service"
    if unit.exists():
        _run(system, ["/usr/bin/systemctl", "disable", "--now", unit.name], "systemctl")
        unit.unlink(missing_ok=True)
        _run(system, ["/usr/bin/systemctl", "daemon-reload"], "systemctl")
        removed["service_unit"] = True
    return {"application_id": app, "removed": removed, "retained": retained,
            **({"retired": retired} if isinstance(retired, dict) else {})}
