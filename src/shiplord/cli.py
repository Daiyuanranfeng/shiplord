"""Command-line entry point for the first local milestone."""

from __future__ import annotations

import json
import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, NoReturn, Optional

import typer

from . import __version__
from . import commands, github, output, tailscale
from .client import AdminClient, ClientError, HelperClient, bootstrap_server
from .commands import CommandError
from .configure import ConfigurationError, collect_answers, create_manifest
from .manifest import ManifestError, load_manifest
from .layout import deployment_user
from .protocol import IncompatibleProtocol
from .servers import (InvalidTarget, ServerError, SshOptions, SSHCheckError, list_servers, load_server, register_server,
                      validate_target)
from .template import TemplateError, create_project
from .transport import TransportError, probe_capabilities

app = typer.Typer(no_args_is_help=True, help="Create, provision, and ship applications to a server.")
server_app = typer.Typer(no_args_is_help=True, help="Manage servers.")
github_app = typer.Typer(no_args_is_help=True, help="Configure GitHub Actions deployments.")
env_app = typer.Typer(no_args_is_help=True, help="Manage an application's runtime environment on the server.")
app.add_typer(env_app, name="env", rich_help_panel="Administration")
registry_app = typer.Typer(no_args_is_help=True, help="Manage the server's read-only pull login for private images.")
app.add_typer(registry_app, name="registry", rich_help_panel="Administration")
app.add_typer(server_app, name="server", rich_help_panel="Setup")
app.add_typer(github_app, name="github", rich_help_panel="Administration")


def _emit(result: dict) -> None:
    """The machine contract: sorted JSON, one document, stable key order."""
    typer.echo(json.dumps(result, sort_keys=True))


def _report(result: dict, json_output: bool) -> None:
    """Print a result that has no command-specific renderer: JSON, an error, or readable key lines."""
    if json_output:
        _emit(result)
    elif result.get("ok") is False or result.get("error"):
        output.render_failure(result)
    else:
        output.render_result(result)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"shiplord {__version__}")
        raise typer.Exit()


def _client_details() -> dict[str, str]:
    invoked = Path(sys.argv[0])
    executable = str(invoked.resolve()) if invoked.exists() else (shutil.which(sys.argv[0]) or shutil.which("shiplord") or sys.argv[0])
    return {"version": __version__, "executable": executable}


@app.callback()
def root(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version.")) -> None:
    """Shiplord CLI."""


@app.command(rich_help_panel="Quick start")
def apps(server: Optional[str] = typer.Argument(None, metavar="SERVER"),
         all_servers: bool = typer.Option(False, "--all-servers", help="List applications across every registered server."),
         json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """List registered applications and their release state."""
    try:
        servers, invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)

    if all_servers and server is not None:
        _fail("choose either a server alias or --all-servers", 2, json_output)
    aliases = [entry["alias"] for entry in servers]
    if all_servers:
        if not servers:
            result = {"ok": False, "partial": bool(invalid), "servers": [], "error": "no readable servers are registered", "invalid_entries": invalid}
            _report(result, json_output)
            raise typer.Exit(3)
        results = []
        for entry in servers:
            try:
                code, inventory = _server_inventory(entry, json_output)
                results.append({"server": entry["alias"], **inventory})
                if code:
                    results[-1]["ok"] = False
            except (CommandError, ClientError, ManifestError, ServerError) as error:
                item = {"server": entry["alias"], "ok": False, "applications": [],
                        "error": _inventory_error(error, entry["alias"])}
                if isinstance(error, ClientError) and error.error_code:
                    item["error_code"] = error.error_code
                results.append(item)
        partial = bool(invalid) or any(not result["ok"] for result in results)
        payload = {"ok": not partial, "partial": partial, "servers": results}
        if invalid:
            payload["invalid_entries"] = invalid
        if json_output:
            _emit(payload)
        else:
            for result in results:
                typer.echo(f"Server: {result['server']}  Outcome: {'partial' if not result['ok'] else 'complete'}")
                if result.get("error_code") == "helper_upgrade_required":
                    typer.echo(output.upgrade_message(result))
                else:
                    output.render_apps(result.get("applications", []), result.get("error"))
                    output.render_ignored_files(result.get("ignored_files", []))
            if invalid:
                typer.echo(f"Ignored {invalid} invalid server {'entry' if invalid == 1 else 'entries'}.")
        if partial:
            raise typer.Exit(3)
        return

    if server is None:
        if len(servers) != 1:
            if not servers:
                _fail("No servers are registered. Run `shiplord server add`, then `shiplord apps`.", 3, json_output)
            _fail("Choose a server with `shiplord apps <server>`; registered aliases: " + ", ".join(aliases),
                  3, json_output, aliases=aliases)
        selected = servers[0]
    else:
        selected = next((entry for entry in servers if entry["alias"] == server), None)
        if selected is None:
            _fail(f"Server {server!r} is not registered. Registered aliases: " + (", ".join(aliases) or "none"),
                  3, json_output, aliases=aliases)
    try:
        code, inventory = _server_inventory(selected, json_output)
    except (CommandError, ClientError, ManifestError, ServerError) as error:
        _fail(_inventory_error(error, selected["alias"]), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    payload = {**inventory, "server": selected["alias"]}
    if invalid:
        payload["invalid_entries"] = invalid
    if json_output:
        _emit(payload)
    elif inventory.get("error_code") == "helper_upgrade_required":
        typer.echo(output.upgrade_message(payload))
    else:
        typer.echo(f"Server: {selected['alias']}  Outcome: {'complete' if inventory['ok'] else 'incomplete'}")
        output.render_apps(inventory.get("applications", []), inventory.get("error"))
        output.render_ignored_files(inventory.get("ignored_files", []))
        if invalid:
            typer.echo(f"Ignored {invalid} invalid server {'entry' if invalid == 1 else 'entries'}.")
    if code:
        raise typer.Exit(code)


def _server_inventory(server: dict, json_output: bool) -> tuple[int, dict]:
    """Reject confirmed old helpers before attempting the privileged request."""
    try:
        response = probe_capabilities(server["ssh"], timeout=5)
        version = response.get("helper_version")
    except (TransportError, IncompatibleProtocol, ServerError):
        version = None
    # Only stable SemVer releases have an unambiguous compatibility ordering.
    stable_version = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    match = re.fullmatch(stable_version, version) if isinstance(version, str) else None
    if match and tuple(map(int, match.groups())) < (0, 2, 0):
        return 4, {"ok": False, "error": "App inventory requires helper 0.2.0 or later.",
                   "error_code": "helper_upgrade_required", "helper_version": version,
                   "minimum_helper_version": "0.2.0", "upgrade_command": f"shiplord server bootstrap {server['alias']}"}
    prompt = None if json_output else _interactive_admin_prompt(False)
    return commands.server_applications(AdminClient(server["ssh"], prompt=prompt))


def _inventory_error(error: Exception, alias: str) -> str:
    message = _recovery_hint(str(error))
    if isinstance(error, ClientError) and error.error_code == "invalid_request":
        message += (f"; the server helper may not support inventory. Run `shiplord doctor --server {alias} --deep` "
                    f"to inspect it, or `shiplord server bootstrap {alias}` to update the helper.")
    return message


@app.command(rich_help_panel="Quick start")
def plan(image: Optional[str] = typer.Option(None, "--image", help="Ask the server what deploying this image would do."),
         binary: Optional[Path] = typer.Option(None, "--binary", help="Ask what deploying this binary would do (not uploaded)."),
         commit: Optional[str] = typer.Option(None, "--commit"),
         sequence: Optional[int] = typer.Option(None, "--sequence", rich_help_panel="Advanced options"),
         allow_older: bool = typer.Option(False, "--allow-older", rich_help_panel="Advanced options"),
         json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Preview changes: locally, or with --image, what the server would do (read-only)."""
    manifest = _manifest(json_output)
    if image is None and binary is None:
        result = {"ok": True, "scope": "local configuration preview",
                  "remote_changes": "unknown; pass --image or --binary to ask the server", "configuration": manifest}
        if json_output:
            _emit(result)
        else:
            typer.echo("Target: local configuration")
            typer.echo("Outcome: preview ready")
            typer.echo("Remote changes: unknown; pass --image or --binary to ask the server")
        return

    def action() -> tuple[int, dict]:
        sha = commit or _head_commit()
        commands.validate_deploy_inputs(manifest, binary if binary is not None else image, sha)
        return commands.plan_application(manifest, _helper(manifest), image=image, binary=binary, commit=sha,
                                         sequence=commands.commit_sequence(sha, sequence), allow_older=allow_older)
    _execute(action, json_output, lambda code, result: output.render_operation(
        code, result, f"{manifest['name']} on {manifest['server']}", "plan"))


@app.command(rich_help_panel="Troubleshooting")
def doctor(local: bool = typer.Option(False, "--local", help="Check local prerequisites."),
           server: Optional[str] = typer.Option(None, "--server", help="Server alias."),
           deep: bool = typer.Option(False, "--deep", help="With --server: also check Caddy, sshd, Docker, disk, and applications (sudo)."),
           json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Check local prerequisites or a server's protocol capabilities."""
    if local and server:
        _report({"ok": False, "error": "choose either --local or --server"}, json_output)
        raise typer.Exit(2)
    if server and deep:
        def action() -> tuple[int, dict]:
            code, result = commands.server_doctor(_admin_target(load_server(server), json_output))
            result["client"] = _client_details()
            return code, result
        _execute(action, json_output, lambda code, result: output.render_doctor(result, f"server {server}"))
        return
    if server:
        try:
            response = probe_capabilities(load_server(server))
        except ManifestError as exc:
            _report({"ok": False, "error": str(exc)}, json_output)
            raise typer.Exit(2) from None
        except IncompatibleProtocol:
            _report({"ok": False, "error": "incompatible helper protocol version"}, json_output)
            raise typer.Exit(4) from None
        except ServerError:
            _report({"ok": False, "error": "server capabilities check failed; verify the alias, SSH trust, login, and dispatcher"}, json_output)
            raise typer.Exit(3) from None
        except TransportError as exc:
            _report({"ok": False, "error": str(exc)}, json_output)
            raise typer.Exit(3) from None
        result = {"ok": True, "scope": "protocol capabilities only; server readiness and deployment access were not checked",
                  "helper_version": response["helper_version"], "client": _client_details()}
        if json_output:
            _emit(result)
        else:
            typer.echo(f"Target: server {server}")
            typer.echo("Outcome: protocol reachable")
            typer.echo(f"Client: shiplord {__version__} at {result['client']['executable']}")
            typer.echo(f"Server helper: {response['helper_version']}")
        return
    if not local:
        manifest = _manifest(json_output)
        def action() -> tuple[int, dict]:
            code, result = commands.application_doctor(manifest, _helper(manifest))
            result["client"] = _client_details()
            return code, result
        _execute(action, json_output, lambda code, result: output.render_doctor(
            result, f"{manifest['name']} on {manifest['server']}"))
        return
    checks = {name: bool(shutil.which(name)) for name in ("ssh", "git", "gh")}
    ok = all(checks.values())
    result = {"ok": ok, "scope": "local prerequisites only; server readiness was not checked",
              "checks": checks, "client": _client_details()}
    if json_output:
        _emit(result)
    else:
        output.render_doctor(result, "local machine")
    if not ok:
        raise typer.Exit(3)


@server_app.command("add", rich_help_panel="Setup")
def server_add(alias: str, ssh: str = typer.Option(..., "--ssh"),
               json_output: bool = typer.Option(False, "--json")) -> None:
    """Register a server using an already trusted OpenSSH login."""
    try:
        register_server(alias, ssh)
    except (ManifestError, ServerError) as exc:
        code = 2 if isinstance(exc, (ManifestError, InvalidTarget)) else 3 if isinstance(exc, SSHCheckError) else 1
        _setup_fail(str(exc), code, json_output)
    result = {"ok": True, "alias": alias, "ssh": ssh}
    if json_output:
        _emit(result)
    else:
        output.render_server_add(result)


@server_app.command("list", rich_help_panel="Administration")
def server_list(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the registered servers (local only; nothing is contacted)."""
    try:
        servers, invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)
    if json_output:
        _emit({"ok": True, "servers": servers, "invalid_entries": invalid})
        return
    output.render_server_list(servers, invalid)


def _fail(message: str, code: int, json_output: bool = False, **details) -> None:
    result = {"ok": False, "error": message, **details}
    if json_output:
        _emit(result)
    else:
        output.render_failure(result)
    raise typer.Exit(code)


def _setup_fail(message: str, code: int, json_output: bool) -> NoReturn:
    """Fail a setup command (init, configure, server add) in the form the caller asked for."""
    if json_output:
        _emit({"ok": False, "error": message})
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _manifest(json_output: bool = False) -> dict:
    try:
        return load_manifest()
    except ManifestError as error:
        _fail(str(error), 2, json_output)


def _helper(manifest: dict) -> HelperClient:
    """The application's client. CI sets SHIPLORD_SSH_TARGET (the deployment account) and pinned SSH files."""
    options = SshOptions.from_environment(os.environ)
    target = os.environ.get("SHIPLORD_SSH_TARGET")
    if target:
        return HelperClient(manifest["name"], validate_target(target), ssh_options=options, explicit_target=True)
    return HelperClient(manifest["name"], load_server(manifest["server"]), ssh_options=options)


def _interactive_admin_prompt(json_output: bool) -> Callable[[str], str] | None:
    return getpass.getpass if sys.stdin.isatty() and sys.stderr.isatty() else None


def _admin_target(target: str, json_output: bool) -> AdminClient:
    return AdminClient(target, prompt=_interactive_admin_prompt(json_output))


def _admin(manifest: dict, json_output: bool = False) -> AdminClient:
    return _admin_target(load_server(manifest["server"]), json_output)


def _recovery_hint(message: str) -> str:
    """Add recovery guidance only when the reported failure identifies unfinished state."""
    if (re.search(r"recovery_required|unfinished transaction|recovery is required|reconciliation", message, re.I)
            and "shiplord recover" not in message):
        return message + "; an unfinished deployment transaction is blocking this operation. " \
               "Run `shiplord recover` from the application's directory to reconcile it."
    return message


def _select_server(server: str | None, preferred: str | None, json_output: bool) -> tuple[str, str]:
    try:
        servers, _invalid = list_servers()
    except ServerError as error:
        _fail(str(error), 3, json_output)
    aliases = [entry["alias"] for entry in servers]
    selected_alias = server or preferred
    if selected_alias is not None:
        selected = next((entry for entry in servers if entry["alias"] == selected_alias), None)
        if selected is None:
            _fail(f"Server {selected_alias!r} is not registered. Registered aliases: " + (", ".join(aliases) or "none"),
                  3, json_output, aliases=aliases)
        return selected["alias"], selected["ssh"]
    if len(servers) == 1:
        return servers[0]["alias"], servers[0]["ssh"]
    if not servers:
        _fail("No servers are registered. Run `shiplord server add` or use a configured app repository.",
              3, json_output, aliases=[])
    _fail("Choose a server with `--server`; registered aliases: " + ", ".join(aliases), 3, json_output,
          aliases=aliases)


def _app_helper(application: str, server_target: str | None, *, json_output: bool,
                allow_environment_target: bool = True) -> HelperClient:
    options = SshOptions.from_environment(os.environ)
    pinned = os.environ.get("SHIPLORD_SSH_TARGET") if allow_environment_target else None
    if pinned:
        target = validate_target(pinned)
        if target.split("@", 1)[0] != deployment_user(application):
            raise CommandError("SHIPLORD_SSH_TARGET does not match the selected application's deployment account", 3)
        if server_target is not None and target.split("@", 1)[1] != server_target.split("@", 1)[1]:
            raise CommandError("SHIPLORD_SSH_TARGET does not match the selected server", 3)
        return HelperClient(application, target, ssh_options=options, explicit_target=True)
    if server_target is None:
        raise CommandError("a registered server is required; run `shiplord apps` to discover servers", 3)
    return HelperClient(application, server_target, ssh_options=options)


def _app_context(application: str | None, server: str | None, json_output: bool, command: str):
    """Resolve an app repo's manifest or a named app/server pair without changing app-scoped SSH access."""
    manifest = None
    if application is None:
        try:
            manifest = load_manifest()
        except ManifestError as error:
            if command == "logs" and "not found" in str(error):
                _fail("ship.toml was not found. Run `shiplord logs <app> --server <alias>` from anywhere, or run "
                      "`shiplord apps` to discover registered applications and servers.", 2, json_output)
            _fail(str(error), 2, json_output)
        application = manifest["name"]
    else:
        try:
            application = validate_target_application(application)
        except ManifestError as error:
            _fail(str(error), 2, json_output)
        try:
            candidate = load_manifest()
            if candidate["name"] == application:
                manifest = candidate
        except ManifestError:
            pass

    preferred = manifest["server"] if manifest and manifest["name"] == application else None
    if server is None and application == (manifest or {}).get("name") and os.environ.get("SHIPLORD_SSH_TARGET"):
        return application, preferred, _app_helper(application, None, json_output=json_output)
    selected_alias, target = _select_server(server, preferred, json_output)
    if manifest is not None and manifest["name"] == application and server is None:
        client = _helper(manifest)
    else:
        client = _app_helper(application, target, json_output=json_output)
    return application, selected_alias, client


def validate_target_application(application: str) -> str:
    from .manifest import validate_app_name
    return validate_app_name(application)


def _progress(json_output: bool) -> Callable[[str], None]:
    return (lambda phase: None) if json_output else (lambda phase: typer.echo(f"  ... {phase}", err=True))


def _execute(action: Callable[[], tuple[int, dict]], json_output: bool,
             human_renderer: Callable[[int, dict], None] | None = None) -> None:
    """Run a command function, print its result, and exit with its code; map known errors to exit codes."""
    try:
        code, result = action()
    except (CommandError, ClientError, github.GitHubError) as error:
        _fail(_recovery_hint(str(error)), error.code, json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    except (ManifestError, ServerError) as error:
        _fail(str(error), 2 if isinstance(error, (ManifestError, InvalidTarget)) else 3, json_output)
    if json_output or human_renderer is None:
        _report(result, json_output)
    else:
        human_renderer(code, result)
    if code:
        raise typer.Exit(code)


@server_app.command("bootstrap", rich_help_panel="Setup")
def server_bootstrap(alias: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Install or update the helper on a registered server (asks for the sudo password if needed)."""
    try:
        result = bootstrap_server(load_server(alias), Path(__file__).resolve().parent,
                                  prompt=_interactive_admin_prompt(json_output))
    except (ClientError, ServerError, ManifestError) as error:
        _fail(_recovery_hint(str(error)), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    payload = {"ok": True, **result}
    if json_output:
        _emit(payload)
    else:
        output.render_bootstrap(payload, f"server {alias}")


@app.command("init", rich_help_panel="Setup")
def init(name: str, template: str = typer.Option(..., "--template"),
         json_output: bool = typer.Option(False, "--json")) -> None:
    """Create an application from the pinned template snapshot."""
    try:
        create_project(Path.cwd(), name, template)
    except (ManifestError, TemplateError) as exc:
        _setup_fail(str(exc), 1 if isinstance(exc, TemplateError) else 2, json_output)
    result = {"ok": True, "name": name, "template": template}
    if json_output:
        _emit(result)
    else:
        output.render_init(result)


def _interactive() -> bool:
    """Prompt only when a person is at a terminal; agents, CI, and pipes keep the flag-only behaviour."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str, default: str | None) -> str:
    if default is None:
        return typer.prompt(prompt)
    return typer.prompt(prompt, default=default, show_default=bool(default))


@app.command(rich_help_panel="Setup")
def configure(server: Optional[str] = typer.Option(None, "--server", help="Registered server alias."),
              api_domain: Optional[str] = typer.Option(None, "--api-domain", help="Public hostname of the application."),
              frontend: Optional[list[str]] = typer.Option(None, "--frontend"),
              no_frontend: bool = typer.Option(False, "--no-frontend"),
              name: Optional[str] = typer.Option(None, "--name"),
              runtime: Optional[str] = typer.Option(None, "--runtime", help="docker (default) or native."),
              template: Optional[str] = typer.Option(None, "--template", help="google-adk (default) or generic."),
              container_port: Optional[int] = typer.Option(None, "--container-port",
                                                           help="Port the application listens on in its container."),
              health_path: Optional[str] = typer.Option(None, "--health-path",
                                                        help="Path that returns 200 when the application is ready."),
              memory: Optional[int] = typer.Option(None, "--memory", help="Memory limit in MiB."),
              json_output: bool = typer.Option(False, "--json")) -> None:
    """Create ship.toml in the current directory without overwriting it. At a terminal, missing values are asked."""
    if frontend and no_frontend:
        _setup_fail("specify --frontend or explicitly choose --no-frontend", 2, json_output)
    given = {"server": server, "api_domain": api_domain, "name": name, "runtime": runtime, "template": template,
             "origins": list(frontend) if frontend else ([] if no_frontend else None),
             "container_port": container_port, "readiness_path": health_path, "memory_mb": memory}
    directory = Path.cwd()
    if _interactive() and not json_output:  # --json never prompts: its output must be deterministic
        if (directory / "ship.toml").exists() or (directory / "ship.toml").is_symlink():
            _setup_fail("ship.toml already exists; no file was replaced", 2, json_output)
        try:
            servers = list_servers()[0] if server is None else []
            answers = collect_answers(given, root=directory, servers=servers, ask=_ask, say=typer.echo)
            typer.echo("\nship.toml will contain:")
            for label, key in (("name", "name"), ("server", "server"), ("template", "template"), ("runtime", "runtime"),
                               ("hostname", "api_domain"), ("frontend origins", "origins"),
                               ("container port", "container_port"), ("health path", "readiness_path"),
                               ("memory (MiB)", "memory_mb")):
                value = answers[key]
                typer.echo(f"  {label}: {', '.join(value) or '(none)' if isinstance(value, list) else value}")
            if not typer.confirm("Write ship.toml?", default=True):
                raise typer.Abort()
        except typer.Abort:
            _setup_fail("cancelled; no file was created", 1, json_output)
        except ServerError as error:
            _setup_fail(str(error), 3, json_output)
        except ConfigurationError as error:
            _setup_fail(str(error), 2, json_output)
    else:
        if server is None or api_domain is None:
            _setup_fail("--server and --api-domain are required (run `shiplord configure` in a terminal to be "
                        "asked instead)", 2, json_output)
        if given["origins"] is None:
            _setup_fail("specify --frontend or explicitly choose --no-frontend", 2, json_output)
        answers = dict(given, name=name or directory.name, runtime=runtime or "docker",
                       template=template or "google-adk")
    try:
        destination = create_manifest(directory, answers["name"], answers["server"], answers["api_domain"],
                                      answers["origins"], answers["runtime"], answers["template"],
                                      **{key: answers[key] for key in ("container_port", "readiness_path", "memory_mb")
                                         if answers[key] is not None})
        written = load_manifest(destination)
    except (ConfigurationError, ManifestError) as exc:
        _setup_fail(str(exc), 2, json_output)
    try:
        registered = any(entry["alias"] == answers["server"] for entry in list_servers()[0])
    except ServerError:
        registered = False
    result = {"ok": True, "name": written["name"], "server": written["server"],
              "api_domain": written["http"]["api_domain"], "frontend_origins": written["http"]["frontend_origins"],
              "runtime": written["runtime"], "template": written["template"],
              "container_port": written["http"]["container_port"], "readiness_path": written["http"]["readiness_path"],
              "memory_mb": written["resources"]["memory_mb"], "registered": registered}
    if json_output:
        _emit(result)
    else:
        output.render_configure(result)


@app.command(rich_help_panel="Setup")
def provision(public_key: Optional[Path] = typer.Option(None, "--public-key", help="Public key to register."),
              image_repository: Optional[str] = typer.Option(None, "--image-repository"),
              max_memory_mb: Optional[int] = typer.Option(None, "--max-memory-mb", help="Ceiling for memory_mb (default 4096)."),
              json_output: bool = typer.Option(False, "--json")) -> None:
    """Create the application's account, route-ready port, state, and key access on the server."""
    manifest = _manifest(json_output)
    target = f"{manifest['name']} on {manifest['server']}"
    _execute(lambda: commands.provision_application(manifest, _admin(manifest, json_output), public_key=public_key,
                                                    image_repository=image_repository,
                                                    max_memory_mb=max_memory_mb), json_output,
             lambda code, result: output.render_provision(result, target))


def _head_commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise CommandError("could not run git; pass --commit", 3) from None
    if result.returncode != 0:
        raise CommandError("could not read HEAD; pass --commit", 2)
    return result.stdout.strip()


@app.command(rich_help_panel="Quick start")
def deploy(image: Optional[str] = typer.Option(None, "--image", help="Immutable image: registry/name@sha256:<digest>."),
           binary: Optional[Path] = typer.Option(None, "--binary"), commit: Optional[str] = typer.Option(None, "--commit"),
           sequence: Optional[int] = typer.Option(None, "--sequence", help="Release ordering key; default: the commit time.",
                                                  rich_help_panel="Advanced options"),
           request_id: Optional[str] = typer.Option(None, "--request-id", help="Reuse to retry a deployment safely.",
                                                    rich_help_panel="Advanced options"),
           allow_older: bool = typer.Option(False, "--allow-older", rich_help_panel="Advanced options"),
           timeout: Optional[int] = typer.Option(None, "--timeout", rich_help_panel="Advanced options"),
           require_public: bool = typer.Option(False, "--require-public", help="Fail if the public HTTPS check fails.",
                                               rich_help_panel="Advanced options"),
           verify_public: bool = typer.Option(True, "--verify-public/--no-verify-public",
                                              rich_help_panel="Advanced options"),
           json_output: bool = typer.Option(False, "--json")) -> None:
    """Deploy an already built artifact; nothing is built or pushed."""
    if (image is None) == (binary is None):
        _fail("specify exactly one of --image and --binary", 2, json_output)
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        sha = commit or _head_commit()
        commands.validate_deploy_inputs(manifest, binary if binary is not None else image, sha)
        return commands.deploy_application(
            manifest, _helper(manifest), image=image, binary=binary, commit=sha,
            sequence=commands.commit_sequence(sha, sequence),
            request_id=request_id, timeout=timeout, allow_older=allow_older, on_progress=_progress(json_output),
            verify_public=verify_public, require_public=require_public)
    _execute(action, json_output, lambda code, result: output.render_operation(
        code, result, f"{manifest['name']} on {manifest['server']}", "deployment"))


@app.command(rich_help_panel="Quick start")
def status(application: Optional[str] = typer.Argument(None, metavar="APP"),
           server: Optional[str] = typer.Option(None, "--server", help="Registered server alias."),
           json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the deployed release, the previous release, and any unfinished transaction."""
    try:
        app_name, server_alias, client = _app_context(application, server, json_output, "status")
    except (CommandError, ClientError, ManifestError, ServerError) as error:
        _fail(_recovery_hint(str(error)), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    try:
        _code, result = commands.application_status(client)
    except (CommandError, ClientError) as error:
        _fail(_recovery_hint(str(error)), error.code, json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    if json_output:
        _emit(result)
    else:
        output.render_status(app_name, server_alias, result)


@app.command(rich_help_panel="Quick start")
def logs(application: Optional[str] = typer.Argument(None, metavar="APP"),
         server: Optional[str] = typer.Option(None, "--server", help="Registered server alias."),
         tail: int = typer.Option(100, "--tail"),
         json_output: bool = typer.Option(False, "--json", help="Output JSON.")) -> None:
    """Show recent application logs (bounded)."""
    try:
        app_name, server_alias, client = _app_context(application, server, json_output, "logs")
    except (CommandError, ClientError, ManifestError, ServerError) as error:
        _fail(_recovery_hint(str(error)), getattr(error, "code", 3), json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    try:
        _code, result = commands.application_logs(client, tail)
    except (CommandError, ClientError) as error:
        _fail(_recovery_hint(str(error)), error.code, json_output,
              **({"error_code": error.error_code} if isinstance(error, ClientError) and error.error_code else {}))
    if json_output:
        _emit(result)
    else:
        lines = len(result["logs"].splitlines())
        target = f"{app_name} on {server_alias}" if server_alias else app_name
        typer.echo(f"Target: {target}", err=True)
        typer.echo(f"Outcome: retrieved {lines} log {'line' if lines == 1 else 'lines'}", err=True)
        sys.stdout.write(result["logs"])


@app.command(rich_help_panel="Administration")
def rollback(json_output: bool = typer.Option(False, "--json")) -> None:
    """Restore the previous successful release and its managed configuration."""
    manifest = _manifest(json_output)
    target = f"{manifest['name']} on {manifest['server']}"
    _execute(lambda: commands.run_job_command(manifest, _helper(manifest), "rollback",
                                              on_progress=_progress(json_output)), json_output,
             lambda code, result: output.render_job(result, target, "rollback"))


@app.command(rich_help_panel="Troubleshooting")
def recover(json_output: bool = typer.Option(False, "--json")) -> None:
    """Reconcile an interrupted deployment to the last committed release."""
    manifest = _manifest(json_output)
    target = f"{manifest['name']} on {manifest['server']}"
    _execute(lambda: commands.run_job_command(manifest, _helper(manifest), "recover",
                                              on_progress=_progress(json_output)), json_output,
             lambda code, result: output.render_job(result, target, "recover"))


@env_app.command("set")
def env_set(from_file: Path = typer.Option(..., "--from-file", help="A KEY=VALUE file; use - for stdin."),
            json_output: bool = typer.Option(False, "--json")) -> None:
    """Replace the application's runtime environment. Values are never printed."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        try:
            text = sys.stdin.read() if str(from_file) == "-" else from_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise CommandError("could not read the environment file") from None
        return commands.set_environment(manifest, _admin(manifest, json_output), "set", text)
    _execute(action, json_output, lambda code, result: output.render_environment(
        result, f"{manifest['name']} on {manifest['server']}", "set"))


@env_app.command("keys")
def env_keys(json_output: bool = typer.Option(False, "--json")) -> None:
    """List the names (never the values) of the configured environment variables."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.set_environment(manifest, _admin(manifest, json_output), "keys"), json_output,
             lambda code, result: output.render_environment(
                 result, f"{manifest['name']} on {manifest['server']}", "keys"))


@app.command("deprovision", rich_help_panel="Administration")
def deprovision(retire: bool = typer.Option(False, "--retire", help="Stop the running release and remove its route first."),
                yes: bool = typer.Option(False, "--yes", help="Confirm that this application's access is removed."),
                json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove this application's server-side account, keys, registration and state. Data is kept."""
    manifest = _manifest(json_output)
    if not yes:
        _fail(f"this removes {manifest['name']}'s deployment access from the server; rerun with --yes to confirm", 2, json_output)
    _execute(lambda: commands.deprovision_application(manifest, _admin(manifest, json_output), retire=retire),
             json_output, lambda code, result: output.render_deprovision(
                 result, f"{manifest['name']} on {manifest['server']}"))


@env_app.command("clear")
def env_clear(json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove the application's runtime environment file."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.set_environment(manifest, _admin(manifest, json_output), "clear"), json_output,
             lambda code, result: output.render_environment(
                 result, f"{manifest['name']} on {manifest['server']}", "clear"))


def _ask_tailscale(instructions: dict) -> tuple[str, str] | None:
    """Show the filled-in Tailscale steps and read the two identifiers (not secrets) from the terminal."""
    typer.echo("Tailscale is not set up for CI yet. Shiplord cannot create the trust credential for you, but here is "
               "exactly what to enter:\n")
    for number, step in enumerate(instructions["steps"], 1):
        typer.echo(f"  {number}. {step}")
    if instructions.get("warning"):
        typer.echo(f"\n  warning: {instructions['warning']}")
    typer.echo("\nPaste the values below, or press Enter to skip and add them later.")
    client_id = typer.prompt("Tailscale client ID", default="", show_default=False).strip()
    if not client_id:
        return None
    return client_id, typer.prompt("Tailscale audience", default="", show_default=False).strip()


def _tailscale_settings(client_id: Optional[str], audience: Optional[str], tags: Optional[str]) -> dict[str, str]:
    values = {"SHIPLORD_TS_CLIENT_ID": client_id, "SHIPLORD_TS_AUDIENCE": audience, "SHIPLORD_TS_TAGS": tags}
    settings = {name: value for name, value in values.items() if value}
    for name, value in settings.items():
        if not re.fullmatch(r"[\x21-\x7e]{1,512}", value):
            raise CommandError(f"the value for {name} must be printable text without spaces")
    if tags and not re.fullmatch(r"tag:[a-z0-9-]+(,tag:[a-z0-9-]+)*", tags):
        raise CommandError("--tailscale-tags must look like tag:ci or tag:ci,tag:deploy")
    return settings


@registry_app.command("login", rich_help_panel="Administration")
def registry_login_command(username: str = typer.Option(..., "--username"),
                           token_file: Optional[Path] = typer.Option(None, "--token-file", help="File holding the token."),
                           token_stdin: bool = typer.Option(False, "--token-stdin", help="Read the token from stdin."),
                           registry: str = typer.Option("ghcr.io", "--registry"),
                           json_output: bool = typer.Option(False, "--json")) -> None:
    """Give this application a read-only registry login. The token is never taken from the command line."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        if token_stdin == (token_file is not None):
            raise CommandError("provide the token with exactly one of --token-file and --token-stdin")
        try:
            token = (sys.stdin.read() if token_stdin else token_file.read_text(encoding="utf-8")).strip()
        except (OSError, UnicodeDecodeError):
            raise CommandError("could not read the token") from None
        commands.validate_registry_login(registry, username, token)
        return commands.registry_login(manifest, _admin(manifest, json_output), registry=registry, username=username, token=token)
    _execute(action, json_output, lambda code, result: output.render_registry(
        result, f"{manifest['name']} on {manifest['server']}", "login"))


@registry_app.command("logout", rich_help_panel="Administration")
def registry_logout_command(registry: str = typer.Option("ghcr.io", "--registry"),
                            json_output: bool = typer.Option(False, "--json")) -> None:
    """Remove this application's registry login from the server."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.registry_other(manifest, _admin(manifest, json_output), "logout", registry), json_output,
             lambda code, result: output.render_registry(
                 result, f"{manifest['name']} on {manifest['server']}", "logout"))


@registry_app.command("status", rich_help_panel="Administration")
def registry_status_command(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show which registries have a login (usernames only; tokens are never shown)."""
    manifest = _manifest(json_output)
    _execute(lambda: commands.registry_other(manifest, _admin(manifest, json_output), "list"), json_output,
             lambda code, result: output.render_registry(
                 result, f"{manifest['name']} on {manifest['server']}", "list"))


@github_app.command("setup", rich_help_panel="Administration")
def github_setup_command(repo: str = typer.Option(..., "--repo", help="owner/repository"),
                         ci_host: Optional[str] = typer.Option(None, "--ci-host", help="Server name CI should use (default: the alias host)."),
                         tooling_ref: Optional[str] = typer.Option(None, "--tooling-ref", help="Commit of Shiplord to pin in CI."),
                         platforms: str = typer.Option("linux/amd64", "--platforms"),
                         tailscale_client_id: Optional[str] = typer.Option(None, "--tailscale-client-id"),
                         tailscale_audience: Optional[str] = typer.Option(None, "--tailscale-audience"),
                         tailscale_tags: Optional[str] = typer.Option(None, "--tailscale-tags"),
                         force_workflow: bool = typer.Option(False, "--force-workflow"),
                         json_output: bool = typer.Option(False, "--json")) -> None:
    """Create the CI key, store it in GitHub, set the repository variables, and write the release workflow."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        github.validate_setup_profile(manifest)
        given = _tailscale_settings(tailscale_client_id, tailscale_audience, tailscale_tags)
        gh = github.GitHub()
        interactive = sys.stdin.isatty() and not json_output
        try:
            settings, origin = tailscale.resolve(
                given, ask=_ask_tailscale if interactive else None,
                guide=lambda: github.tailscale_guidance(gh, repo, manifest))
        except tailscale.TailscaleError as error:
            raise CommandError(str(error)) from None
        target = load_server(manifest["server"])
        result = github.github_setup(manifest, repo, project=Path.cwd(), admin=_admin_target(target, json_output), gh=gh,
                                     admin_target=target, ci_host=ci_host, tooling_ref=tooling_ref, platforms=platforms,
                                     tailscale=settings, force_workflow=force_workflow)
        result["tailscale"] = origin
        return 0, result
    _execute(action, json_output, lambda code, result: output.render_github_setup(
        result, f"{manifest['name']} on {manifest['server']}"))


@github_app.command("check-ci", rich_help_panel="Administration")
def github_check_ci_command(timeout: int = typer.Option(1200, "--timeout", help="Seconds to wait for the run."),
                            json_output: bool = typer.Option(False, "--json")) -> None:
    """Run the release workflow in check-only mode: join Tailscale, log in with the CI key, deploy nothing."""
    manifest = _manifest(json_output)
    _execute(lambda: github.check_ci(manifest, gh=github.GitHub(), timeout=timeout), json_output,
             lambda code, result: output.render_github_check_ci(
                 result, f"{manifest['name']} on {manifest['server']}"))


@github_app.command("status", rich_help_panel="Administration")
def github_status_command(check_server: bool = typer.Option(False, "--check-server", help="Also confirm the key on the server."),
                          json_output: bool = typer.Option(False, "--json")) -> None:
    """Check the repository access, secret, variables, workflow, CI key, and the last workflow run."""
    manifest = _manifest(json_output)
    _execute(lambda: github.github_status(manifest, project=Path.cwd(), gh=github.GitHub(),
                                          admin=_admin(manifest, json_output) if check_server else None), json_output,
             lambda code, result: output.render_github_status(
                 result, f"{manifest['name']} on {manifest['server']}"))


@github_app.command("rotate-key", rich_help_panel="Administration")
def github_rotate_key(json_output: bool = typer.Option(False, "--json")) -> None:
    """Replace the CI key: add and prove the new one, update GitHub, then revoke the old one."""
    manifest = _manifest(json_output)

    def action() -> tuple[int, dict]:
        target = load_server(manifest["server"])
        return 0, github.rotate_key(manifest, admin=_admin_target(target, json_output), gh=github.GitHub(), admin_target=target)
    _execute(action, json_output, lambda code, result: output.render_github_rotate_key(
        result, f"{manifest['name']} on {manifest['server']}"))


def _order_help() -> None:
    panel_order = {"Quick start": 0, "Setup": 1, "Troubleshooting": 2, "Administration": 3}
    quick_order = {name: index for index, name in enumerate(("apps", "status", "logs", "plan", "deploy"))}
    app.registered_commands.sort(key=lambda info: (
        panel_order.get(info.rich_help_panel, 4),
        quick_order.get(info.name or info.callback.__name__, 99),
        info.name or info.callback.__name__,
    ))
    app.registered_groups.sort(key=lambda info: (panel_order.get(info.rich_help_panel, 4), info.name or ""))


_order_help()


def main() -> None:
    app()
