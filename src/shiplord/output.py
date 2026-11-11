"""Human-readable renderers for command results.

``cli.py`` chooses between the stable JSON contract (``--json``) and one renderer
here. Every renderer prints whole lines of prose, so no command ever shows a
person a Python dictionary, list, or JSON blob. Renderers take an optional
``say`` callable so tests can read the lines without a terminal.
"""

from __future__ import annotations

from typing import Callable

import typer

Say = Callable[[str], None]


def _say(say: Say | None) -> Say:
    return say or typer.echo


def _humanise(name: object) -> str:
    return str(name).replace("_", " ")


def _scalar(value: object) -> str:
    """A single readable value. A structure is never dumped; ``--json`` is its home."""
    if value is None:
        return "none"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        return ", ".join(_scalar(item) for item in value) or "none"
    return "see --json"


def _details(value: dict) -> str:
    """``key: value`` pairs of a structure, without the surrounding punctuation."""
    return "; ".join(f"{_humanise(key)}: {_scalar(item)}" for key, item in value.items())


def _check_value(value: object) -> str:
    """One check's state and its readable details."""
    if not isinstance(value, dict):
        return "available" if value is True else "missing" if value is False else _scalar(value)
    state = "passed" if value.get("ok") is True else "failed" if value.get("ok") is False else "unknown"
    details = [f"{_humanise(key)}: {_scalar(item)}" for key, item in value.items()
               if key != "ok" and not isinstance(item, (dict, list))]
    for key, item in value.items():
        if isinstance(item, dict) and item.get("url"):
            details.append(f"{_humanise(key)}: {item['url']}")
    return state + (" (" + "; ".join(details) + ")" if details else "")


def short_release(release: dict | None) -> str:
    if release is None:
        return "none"
    commit = str(release.get("commit") or "unknown")[:12]
    artifact = release.get("artifact")
    if isinstance(artifact, str) and len(artifact) > 48:
        artifact = artifact[:45] + "..."
    return f"{commit} ({artifact or 'artifact unknown'})"


def render_failure(result: dict, say: Say | None = None) -> None:
    """An error in prose: the message first, then any guidance that changes what to do next."""
    say = _say(say)
    say(f"error: {result.get('error') or 'the command failed'}")
    if result.get("error_code"):
        say(f"code: {result['error_code']}")
    if result.get("aliases"):
        say(f"registered servers: {_scalar(result['aliases'])}")


def render_result(result: dict, say: Say | None = None) -> None:
    """The safety net for a result without a dedicated renderer: readable, never a structure dump."""
    say = _say(say)
    if "ok" in result:
        say(f"Outcome: {'succeeded' if result['ok'] else 'failed'}")
    for key, value in result.items():
        if key == "ok":
            continue
        label = _humanise(key).capitalize()
        if isinstance(value, dict):
            say(f"{label}:")
            if not value:
                say("  none")
            for name, item in value.items():
                say(f"  {_humanise(name)}: {_scalar(item)}")
        elif isinstance(value, list):
            say(f"{label}:")
            if not value:
                say("  none")
            for item in value:
                say(f"  - {_scalar(item)}")
        else:
            say(f"{label}: {_scalar(value)}")


def upgrade_message(result: dict) -> str:
    alias = result["server"]
    return (f"`{alias}` is running helper {result['helper_version']}. {result['error']} "
            f"Run `{result['upgrade_command']}`, then retry `shiplord apps {alias}`.")


def render_ignored_files(names: list[str], say: Say | None = None) -> None:
    """Report registry files that were skipped, so a stray backup never disappears silently."""
    if not names:
        return
    say = _say(say)
    noun = "file" if len(names) == 1 else "files"
    say(f"  Ignored {len(names)} {noun} in the registry that is not a registration: " + ", ".join(names))


def render_apps(applications: list[dict], error: str | None = None, say: Say | None = None) -> None:
    say = _say(say)
    if error:
        say(f"  Error: {error}")
    if not applications and error:
        return
    if not applications:
        say("  No applications registered.")
        return
    columns = ("Application", "Domain", "Runtime", "Release", "Unfinished transaction")
    rows = []
    for item in applications:
        release = "yes" if item.get("release_recorded") is True else "no" if item.get("release_recorded") is False else "unknown"
        transaction = item.get("unfinished_transaction") if item.get("ok", True) else "unknown"
        if isinstance(transaction, dict):
            transaction = transaction.get("phase") or "pending"
        rows.append((item["application"], item["domain"], item["runtime"], release, transaction or "none"))
    widths = [max(len(column), *(len(row[index]) for row in rows)) for index, column in enumerate(columns)]
    say("  " + "  ".join(column.ljust(widths[index]) for index, column in enumerate(columns)))
    say("  " + "  ".join("-" * width for width in widths))
    for row in rows:
        say("  " + "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    for item in applications:
        if item.get("error"):
            say(f"  {item['application']}: {item['error']}")


def render_server_list(servers: list[dict], invalid: int, say: Say | None = None) -> None:
    say = _say(say)
    if not servers:
        say("No servers registered. Add one with `shiplord server add <alias> --ssh <user>@<host>`.")
    width = max((len(server["alias"]) for server in servers), default=0)
    for server in servers:
        say(f"{server['alias'].ljust(width)}  {server['ssh']}")
    if invalid:
        say(f"{invalid} invalid {'entry' if invalid == 1 else 'entries'} ignored in the server directory.")


def render_server_add(result: dict, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Server alias saved: {result['alias']} ({result['ssh']})")
    say("Run `shiplord server bootstrap` next.")


def render_init(result: dict, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Created {result['name']} from the pinned {result['template']} template.")


def render_configure(result: dict, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {result['name']} on {result['server']}")
    say("Outcome: ship.toml created")
    say(f"API domain: {result['api_domain']}")
    say(f"Runtime: {result['runtime']}")
    say(f"Template: {result['template']}")
    say(f"Frontend origins: {_scalar(result.get('frontend_origins'))}")
    for label, key, suffix in (("Container port", "container_port", ""), ("Health path", "readiness_path", ""),
                               ("Memory limit", "memory_mb", " MiB")):
        if result.get(key) is not None:
            say(f"{label}: {result[key]}{suffix}")
    if result.get("registered"):
        say(f"Server alias {result['server']} is registered locally; SSH access was not checked.")
    else:
        say("The server alias has not been checked.")


def render_provision(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say(f"Outcome: {'provisioned' if result.get('ok') else 'provisioning failed'}")
    for label, key in (("Application", "application_id"), ("Deployment login", "deploy_login"),
                       ("API domain", "api_domain"), ("Runtime", "runtime"), ("Host port", "host_port")):
        if result.get(key) is not None:
            say(f"{label}: {result[key]}")
    if result.get("max_memory_mb") is not None:
        say(f"Memory limit: {result['max_memory_mb']} MiB")
    if result.get("key_label") or result.get("key_fingerprint"):
        key = result.get("key_label") or "deployment key"
        fingerprint = result.get("key_fingerprint")
        say(f"Deployment key: {key}" + (f" ({fingerprint})" if fingerprint else ""))
    created = result.get("created")
    if isinstance(created, dict):
        added = [_humanise(name) for name, changed in created.items() if changed]
        present = [_humanise(name) for name, changed in created.items() if not changed]
        say("Created: " + (", ".join(added) or "nothing (the application was already provisioned)"))
        if present:
            say("Already present: " + ", ".join(present))
    if result.get("next"):
        say(f"Next: {result['next']}")


def render_deprovision(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say("Outcome: access removed")
    say(f"Application: {result.get('application_id', target)}")
    removed = result.get("removed")
    if isinstance(removed, dict):
        gone = [_humanise(name) for name, was_removed in removed.items() if was_removed]
        say("Removed: " + (", ".join(gone) or "nothing (already absent)"))
    retired = result.get("retired")
    if isinstance(retired, dict) and retired.get("status"):
        say(f"Retired the running release: {_humanise(retired['status'])}")
    for item in result.get("retained") or []:
        say(f"Retained: {_scalar(item)}")
    if result.get("note"):
        say(f"Note: {result['note']}")


def render_bootstrap(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say("Outcome: helper installed or updated")
    say(f"Helper version: {result.get('version', 'unknown')}")
    changed = result.get("changed")
    if isinstance(changed, dict):
        updated = [_humanise(name) for name, was_changed in changed.items() if was_changed]
        unchanged = [_humanise(name) for name, was_changed in changed.items() if not was_changed]
        say("Changed: " + (", ".join(updated) or "nothing (already up to date)"))
        if unchanged:
            say("Already up to date: " + ", ".join(unchanged))


def render_job(result: dict, target: str, action: str, say: Say | None = None) -> None:
    """Rollback and recover report a durable job's outcome, which is a release transition."""
    say = _say(say)
    status = result.get("status") or ("succeeded" if result.get("ok") else "failed")
    say(f"Target: {target}")
    say(f"Outcome: {_humanise(status)}")
    if result.get("request_id"):
        say(f"Request ID: {result['request_id']}")
    if isinstance(result.get("release"), dict):
        say(f"Release: {short_release(result['release'])}")
    if isinstance(result.get("previous"), dict):
        say(f"Previous release: {short_release(result['previous'])}")
    if isinstance(result.get("target"), dict):
        say(f"Recovered to: {short_release(result['target'])}")
    if isinstance(result.get("current"), dict):
        say(f"Current release: {short_release(result['current'])}")
    if result.get("error") is not None:
        say(f"Reason: {_humanise(result['error'])}")
    if result.get("message") is not None:
        say(f"Message: {result['message']}")
    probes = result.get("probes")
    if isinstance(probes, dict) and probes.get("warning"):
        say(f"Warning: {probes['warning']}")
    for warning in result.get("warnings") or []:
        say(f"Warning: {_scalar(warning)}")
    if result.get("recovery"):
        say(f"Next: {result['recovery']}")
    if status in {"recovery_required", "intervention_required", "interrupted"}:
        say("Why: an unfinished deployment transaction blocks this operation.")
        say("Next: run `shiplord recover` from this application's repository to reconcile it.")


def render_environment(result: dict, target: str, operation: str, say: Say | None = None) -> None:
    """Environment output names variables and counts them; a value is never printed."""
    say = _say(say)
    keys = result.get("keys") or []
    noun = "variable" if len(keys) == 1 else "variables"
    say(f"Target: {target}")
    if operation == "keys":
        say(f"Outcome: {len(keys)} {noun} configured")
        for name in keys:
            say(f"  {name}")
        if not keys:
            say("  No variables are configured.")
        return
    if operation == "clear":
        say("Outcome: environment removed" if result.get("changed", True) else "Outcome: no environment to remove")
    else:
        say("Outcome: environment saved" if result.get("changed", True) else "Outcome: environment unchanged")
    say(f"Variables: {len(keys)}" + (f" ({', '.join(keys)})" if keys else ""))
    for warning in result.get("warnings") or []:
        say(f"Warning: {_scalar(warning)}")
    if result.get("note"):
        say(f"Note: {result['note']}")


def render_registry(result: dict, target: str, operation: str, say: Say | None = None) -> None:
    """Registry output shows registries and usernames only; the token is never printed."""
    say = _say(say)
    registries = result.get("registries") or []
    noun = "registry login" if len(registries) == 1 else "registry logins"
    say(f"Target: {target}")
    if operation == "list":
        say(f"Outcome: {len(registries)} {noun}" if registries else "Outcome: no registry logins")
    elif operation == "login":
        say("Outcome: registry login saved")
    else:
        say("Outcome: registry login removed")
    if registries:
        say("Registry logins:")
    for item in registries:
        if isinstance(item, dict):
            say(f"  {item.get('registry', 'unknown')}: {item.get('username', 'unknown')}")
    if result.get("note"):
        say(f"Note: {result['note']}")


def render_github_setup(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say("Outcome: CI configured")
    for label, key in (("Repository", "repository"), ("GitHub account", "github_account"),
                       ("Server host", "server_host")):
        if result.get(key) is not None:
            say(f"{label}: {result[key]}")
    if result.get("key_label"):
        fingerprint = result.get("key_fingerprint")
        say(f"CI key: {result['key_label']}" + (f" ({fingerprint})" if fingerprint else ""))
    steps = result.get("steps")
    if isinstance(steps, dict) and steps:
        say("Steps:")
        for name, value in steps.items():
            say(f"  {_humanise(name)}: {_scalar(value)}")
    tailscale = result.get("tailscale")
    if isinstance(tailscale, dict) and tailscale.get("source"):
        say(f"Tailscale settings: {tailscale['source']}")
        for warning in tailscale.get("warnings") or []:
            say(f"Warning: {_scalar(warning)}")
    guidance = result.get("tailscale_setup")
    if isinstance(guidance, dict):
        say("Tailscale setup:")
        for number, step in enumerate(guidance.get("steps") or [], 1):
            say(f"  {number}. {_scalar(step)}")
        if guidance.get("warning"):
            say(f"  Warning: {guidance['warning']}")
    remaining = result.get("remaining")
    if isinstance(remaining, list) and remaining:
        say("Remaining:")
        for item in remaining:
            say(f"  - {_scalar(item)}")


def render_github_status(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say(f"Outcome: {'ready' if result.get('ok') else 'attention required'}")
    if result.get("repository") is not None:
        say(f"Repository: {result['repository']}")
    checks = result.get("checks")
    if isinstance(checks, dict) and checks:
        say("Checks:")
        for name, value in checks.items():
            say(f"  {_humanise(name)}: {_check_value(value)}")


def render_github_check_ci(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say(f"Outcome: {'check run succeeded' if result.get('ok') else 'check run failed'}")
    if result.get("run") is not None:
        say(f"Run: {result['run']}")
    if result.get("conclusion") is not None:
        say(f"Conclusion: {result['conclusion']}")
    if result.get("status") is not None:
        say(f"Status: {result['status']}")
    steps = result.get("steps")
    if isinstance(steps, dict) and steps:
        say("Steps:")
        for name, conclusion in steps.items():
            say(f"  {name}: {_scalar(conclusion)}")
    if result.get("failed_step") is not None:
        say(f"Failed step: {result['failed_step']}")
    if result.get("hint") is not None:
        say(f"Hint: {result['hint']}")
    if result.get("subject_expected") is not None:
        say(f"Expected subject: {result['subject_expected']}")
    if result.get("error") is not None:
        say(f"Reason: {result['error']}")


def render_github_rotate_key(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say("Outcome: CI key replaced")
    if result.get("repository") is not None:
        say(f"Repository: {result['repository']}")
    if result.get("old_label") is not None:
        say(f"Previous key: {result['old_label']} (revoked)")
    if result.get("key_label") is not None:
        fingerprint = result.get("key_fingerprint")
        say(f"New key: {result['key_label']}" + (f" ({fingerprint})" if fingerprint else ""))
    steps = result.get("steps")
    if isinstance(steps, dict) and steps:
        say("Steps:")
        for name, value in steps.items():
            say(f"  {_humanise(name)}: {_scalar(value)}")


def _plan_outcome(result: dict) -> str:
    error = result.get("error")
    if error == "recovery_required":
        return "recovery required"
    if error == "stale_release":
        return "blocked by a stale release"
    if result.get("action") == "deploy":
        return "ready"
    if result.get("action") == "unchanged":
        return "no changes required"
    if result.get("action") == "none":
        return "blocked"
    return "plan available"


def _plan_release(release: dict | None) -> str:
    summary = short_release(release)
    if release is not None and release.get("sequence") is not None:
        summary += f"; sequence {release['sequence']}"
    return summary


def _shutdown_policy(policy: object) -> str:
    if not isinstance(policy, dict):
        return "none"
    details = []
    if policy.get("timeout_seconds") is not None:
        details.append(f"{policy['timeout_seconds']} seconds")
    if policy.get("action") is not None:
        details.append(str(policy["action"]))
    return ", ".join(details) if details else "unknown"


def render_plan_details(result: dict, say: Say | None = None) -> None:
    say = _say(say)
    if result.get("action") is not None:
        say(f"Plan action: {result['action']}")
    reason = result.get("error") or result.get("reason")
    if reason is not None:
        say(f"Reason: {str(reason).replace('_', ' ')}")
    if result.get("message") is not None:
        say(f"Message: {result['message']}")
    if "current" in result:
        say(f"Current release: {_plan_release(result['current'])}")
    if "candidate" in result:
        say(f"Candidate release: {_plan_release(result['candidate'])}")
    if result.get("restarts_application") is not None:
        restart = "yes" if result["restarts_application"] else "no"
        say(f"Restarts application: {restart}")
    if result.get("route_change") is not None:
        say(f"Route change: {result['route_change']}")
    if result.get("domain") is not None:
        say(f"Domain: {result['domain']}")

    storage = result.get("storage")
    if isinstance(storage, dict):
        current_path = storage.get("current_path") or "none"
        candidate_path = storage.get("candidate_path") or "unknown"
        say(f"Storage path: {current_path} to {candidate_path}")
        if storage.get("same_volume") is not None:
            reused = "yes" if storage["same_volume"] else "no"
            say(f"Application volume reused: {reused}")
        if storage.get("note") is not None:
            say(f"Storage note: {storage['note']}")

    shutdown = result.get("shutdown")
    if isinstance(shutdown, dict):
        if "outgoing_policy" in shutdown:
            say(f"Outgoing shutdown policy: {_shutdown_policy(shutdown['outgoing_policy'])}")
        if "candidate_policy" in shutdown:
            say(f"Candidate shutdown policy: {_shutdown_policy(shutdown['candidate_policy'])}")
        if shutdown.get("note") is not None:
            say(f"Shutdown note: {shutdown['note']}")


def render_public_check(public: object, say: Say | None = None) -> None:
    if not isinstance(public, dict):
        return
    say = _say(say)
    state = "passed" if public.get("ok") is True else "failed" if public.get("ok") is False else "unknown"
    details = []
    if public.get("status") is not None:
        details.append(f"HTTP {public['status']}")
    if public.get("error") is not None:
        details.append(str(public["error"]).replace("_", " "))
    suffix = " (" + "; ".join(details) + ")" if details else ""
    say(f"Public HTTPS: {state}{suffix}")


def render_status(application: str, server: str | None, result: dict, say: Say | None = None) -> None:
    say = _say(say)
    status = result.get("application", {})
    current = status.get("current")
    previous = status.get("previous")
    transaction = status.get("unfinished_transaction")
    if transaction:
        outcome = "recovery required"
    elif current is None:
        outcome = "no release recorded"
    else:
        outcome = "running" if status.get("running") else "stopped"
    target = f"{application} on {server}" if server else application
    say(f"Target: {target}")
    say(f"Outcome: {outcome}")
    job = result.get("job")
    if isinstance(job, dict):
        render_job_record(job, say)
    say(f"Current release: {short_release(current)}")
    if previous is not None:
        say(f"Previous release: {short_release(previous)}")
    if current is not None:
        running = status.get("running")
        say(f"Runtime: {'running' if running else 'stopped' if running is False else 'unknown'}")
    if transaction:
        phase = transaction.get("phase", "unknown")
        say(f"Unfinished transaction: {phase} ({transaction.get('request_id', 'request ID unavailable')})")
        say("Why: the server has an unfinished deployment transaction that must be reconciled before another deploy.")
        if server:
            say(f"Next: run `shiplord recover {application} --server {server}` to reconcile it.")
        else:
            say("Next: run `shiplord recover` from this application's repository.")


def render_operation(code: int, result: dict, target: str, operation: str, say: Say | None = None) -> None:
    say = _say(say)
    status = result.get("status")
    public = result.get("public")
    if operation == "plan":
        outcome = _plan_outcome(result)
    elif operation == "deployment" and isinstance(public, dict) and public.get("ok") is False:
        outcome = "deployed; public HTTPS check failed"
    elif status in {"recovery_required", "intervention_required", "interrupted"} or result.get("error") == "recovery_required":
        outcome = "recovery required"
    elif code == 0:
        outcome = "ready" if operation == "plan" else (status or "succeeded").replace("_", " ")
    else:
        outcome = (status or "failed").replace("_", " ")
    say(f"Target: {target}")
    say(f"Outcome: {outcome}")
    for name in ("commit", "artifact", "url", "error", "message"):
        if operation == "plan" and name in {"error", "message"}:
            continue
        if result.get(name) is not None:
            label = "URL" if name == "url" else name.capitalize()
            say(f"{label}: {result[name]}")
    if operation == "plan":
        render_plan_details(result, say)
    elif operation == "deployment":
        if result.get("request_id") is not None:
            say(f"Request ID: {result['request_id']}")
        release = result.get("release")
        if isinstance(release, dict):
            for name in ("commit", "artifact"):
                if release.get(name) is not None:
                    say(f"{name.capitalize()}: {release[name]}")
        probes = result.get("probes")
        if isinstance(probes, dict) and probes.get("warning"):
            say(f"Warning: {probes['warning']}")
        for warning in result.get("warnings") or []:
            say(f"Warning: {_scalar(warning)}")
        render_public_check(public, say)
    if outcome == "recovery required":
        say("Why: an unfinished deployment transaction blocks this operation.")
        say("Next: run `shiplord recover` from this application's repository to reconcile it.")


def _doctor_value(value: object) -> str:
    if isinstance(value, dict):
        if "ok" in value:
            state = "passed" if value["ok"] else "failed"
            details = []
            for key in ("helper_version", "version", "protocol", "id", "version_id", "current", "running",
                        "unfinished_transaction", "hint", "message"):
                if value.get(key) is not None:
                    details.append(f"{key.replace('_', ' ')}: {_scalar(value[key])}")
            return state + (" (" + "; ".join(details) + ")" if details else "")
        return _details(value) or "unknown"
    return "available" if value is True else "missing" if value is False else _scalar(value)


def render_doctor(result: dict, target: str, say: Say | None = None) -> None:
    say = _say(say)
    say(f"Target: {target}")
    say(f"Outcome: {'ready' if result.get('ok') else 'attention required'}")
    client = result.get("client")
    if client:
        say("Client:")
        say(f"  shiplord {client['version']}")
        say(f"  executable: {client['executable']}")
    checks = result.get("checks", {})
    groups = (
        ("Local tools", ("ssh", "git", "gh")),
        ("Server baseline", ("os",)),
        ("Server access", ("server_access", "helper_actions", "helper")),
        ("Application", ("application", "applications")),
        ("Network", ("dns",)),
        ("Server services", ("caddy", "sshd", "docker")),
        ("Storage", ("disk",)),
    )
    shown = set()
    for title, names in groups:
        present = [(name, checks[name]) for name in names if name in checks]
        if not present:
            continue
        say(f"{title}:")
        for name, value in present:
            shown.add(name)
            say(f"  {name.replace('_', ' ')}: {_doctor_value(value)}")
            if name == "applications" and isinstance(value, dict):
                for application in value.get("applications", []):
                    label = application.get("application", "application")
                    release = "yes" if application.get("deployed") else "no"
                    state = application.get("unfinished_transaction") or "none"
                    say(f"    {label}: release {release}; unfinished transaction {state}")
                render_ignored_files(value.get("ignored_files", []), say)
    for name, value in checks.items():
        if name not in shown:
            say(f"Checks ({name.replace('_', ' ')}): {_doctor_value(value)}")


def render_job_record(job: dict, say: Say | None = None) -> None:
    """A durable job record attached to a status result; it is not a release transition."""
    say = _say(say)
    say(f"Job: {job.get('request_id', 'unknown')}")
    state = str(job.get("state", "unknown"))
    say(f"Job state: {state}")
    say(f"Job phase: {job.get('phase', 'unknown')}")
    outcome = job.get("outcome")
    if state == "finished" and isinstance(outcome, dict):
        say(f"Job outcome: {outcome.get('status', 'unknown')} (exit {outcome.get('exit_code', 'unknown')})")


def render_environment_diff(result: dict, say: Say | None = None) -> None:
    """Preview only key names; values are never printed or compared."""
    say = _say(say)
    for label in ("added", "removed"):
        names = result.get(label) or []
        say(f"{label.capitalize()}: {', '.join(names) if names else 'none'}")
    say(result.get("note") or "Key names only.")


def render_fleet_doctor(result: dict, say: Say | None = None) -> None:
    say = _say(say)
    say("Target: all registered servers")
    say(f"Outcome: {'compatible' if result.get('ok') else 'attention required'}")
    client = result.get("client")
    if client:
        say("Client:")
        say(f"  shiplord {client['version']}")
        say(f"  executable: {client['executable']}")
    for item in result.get("servers", []):
        if item.get("reachable"):
            version = item.get("helper_version") or "unknown"
            say(f"{item['server']}: reachable, helper {version}, inventory {item['inventory_support']}")
        else:
            say(f"{item['server']}: unreachable, inventory unknown")
        if item.get("error_code") == "helper_upgrade_required":
            say("  " + upgrade_message(item))
        elif item.get("error"):
            say(f"  Error: {item['error']}")
    if result.get("invalid_entries"):
        count = result["invalid_entries"]
        say(f"Ignored {count} invalid server {'entry' if count == 1 else 'entries'}.")


def render_keys(result: dict, target: str, operation: str, say: Say | None = None) -> None:
    """Key labels and fingerprints only; a public key line is never printed."""
    say = _say(say)
    keys = result.get("keys") or []
    noun = "key" if len(keys) == 1 else "keys"
    say(f"Target: {target}")
    if operation == "add":
        say(f"Outcome: key {result.get('label', 'unknown')} registered")
    elif operation == "remove":
        say(f"Outcome: key {result.get('label', 'unknown')} removed")
    else:
        say(f"Outcome: {len(keys)} {noun}" if keys else "Outcome: no keys")
    if keys:
        say("Deploy keys:")
        for item in keys:
            if isinstance(item, dict):
                say(f"  {item.get('label', 'unknown')}: {item.get('fingerprint', 'unknown')}")
    if result.get("note"):
        say(f"Note: {result['note']}")


def render_setup(result: dict, say: Say | None = None) -> None:
    """The first-run sequence: each step's status, and where a stopped run will resume."""
    say = _say(say)
    if result.get("application"):
        say(f"Target: {result['application']} on {result['server']}")
    labels = {"ran": "done"}
    for item in result.get("steps", []):
        line = f"{item['step']}: {labels.get(item['status'], item['status'])}"
        if item.get("detail"):
            line += f" ({item['detail']})"
        say(line)
        check = item.get("result")
        if item["step"] == "check-ci" and isinstance(check, dict) and check.get("run"):
            say(f"  CI run: {check['run']}")
    if result.get("ok"):
        say("Outcome: ready")
    else:
        say(f"Outcome: stopped at {result.get('failed_step')}")
        if result.get("error"):
            say(f"Error: {result['error']}")
