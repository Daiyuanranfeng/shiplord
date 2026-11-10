# Design

## Context

See proposal.md - Why. The facts from the current code that shape the approach:

- `routing._read_file` opens the live `Caddyfile`, `.prior-Caddyfile`, and managed route files with
  `O_NOFOLLOW` and checks the descriptor's owner and mode. `_read_path` reads imported files with `os.lstat`
  and a plain `open`, so a symlink is not a regular file and is refused.
- `_Inspection.scan` returns `tokens[1:]` for a depth-0 `import` line. `RouteManager._scan_hostnames` treats
  each returned token as a path, skipping tokens without `/`, `*`, or `.`. An import argument that looks like a
  hostname (it has a `.`) is therefore globbed as a file and silently ignored.
- `_address_tokens` refuses any address token containing `{` or `}`, so `{$SITE_ADDRESS}` is refused rather
  than compared.
- `routing.service_environment(system)` already reports the variables the Caddy service runs with, from
  systemd or the packaged environment files, and never reads the caller's environment. `SubprocessCaddy`
  exposes it as the callable `service_environment`.
- `SubprocessCaddy.__init__` defaults `caddy` to `/usr/bin/caddy`; `installer.CADDY_BINARY` repeats the same
  literal for the bootstrap refusal message.
- Every module the server runs must be stdlib-only. The transaction's crash-safety, `O_NOFOLLOW` use, and
  error kinds are unchanged unless this change says otherwise.

## Goals / Non-Goals

**Goals:**

- Inspect an imported symlink while keeping every other privileged file open `O_NOFOLLOW`.
- Stop reading an import argument as a file path, and inspect a snippet or file import with its arguments
  instead of refusing every one.
- Compare a `{$VAR}` site address using the service environment only.
- Resolve the Caddy binary through one function both bootstrap and validation use.

**Non-Goals:**

- Expanding `{block}` or `{blocks.*}`.
- Supporting a multi-token or empty environment value as a site address.
- Supporting Caddy in Docker, an override passed over SSH, or a per-application Caddy path.

## Decisions

**Follow an imported symlink by opening it and trusting only the descriptor.** `_read_path` opens the path
without `O_NOFOLLOW` and checks `os.fstat` on the descriptor: regular file, at most `MAX_CONFIG_BYTES`, owned
by `os.geteuid()`, and no group or world write bit. A loop, directory, device, FIFO, oversized target, or
untrusted target is refused as uninspectable with the existing message, and no content is echoed. Reading from
the descriptor closes the race between a `stat` and an `open`; resolving with `realpath` and then opening
`O_NOFOLLOW` was the alternative, but it would reject a target reachable through a symlinked directory and
needs a second path walk. The trust checks apply to every imported file, not only a link, so a link cannot
smuggle in a file the trusted checks would refuse. `_read_file`, `_create`, `_publish`, and `_restore` are not
touched.

**Inspect a top-level import that passes arguments instead of refusing it.** `scan` returns an import
statement (pattern plus arguments), so an argument is never mistaken for a path. `_Inspection` records each
snippet definition with its body lines, and an import of a known snippet is resolved against that body with
`{args[N]}` substituted: a body line that opens a block declares the addresses before its `{`, and a body
line with no opening brace is a directive and declares no site. That rule keeps the common
`(common) { encode gzip }` + `import common 5s` layout working, which a blanket refusal broke, while still
comparing the address a `(sites) { {args[0]} { ... } }` body declares. A file-pattern import is followed with
the same substitution, which is what a real Caddy does (verified with `caddy validate` in the Linux image:
a file whose first line is `{args[0]} {` is imported with the argument as its address). A directive-only
snippet imported at top level is only valid in Caddy as a global option and must be the last statement; the
real-Caddy suite covers exactly that. Imports are followed depth-first in textual order, as Caddy reads them,
so a snippet defined by an earlier import is known when it is used; a snippet defined later is not (Caddy
refuses that too). The refusal is now narrow and names the import: an argument the import does not provide,
`{block}`, an `{args[:]}` that is not one address, or a name that is neither a known snippet nor a file.
Treating every snippet body as an address list is still rejected, because a directive line such as
`encode gzip` is not an address and doing so produced the false conflicts the previous change was about.

**Expand a service variable token before classifying it as an address.** `_Inspection` gains an `environment`
mapping. `_address_tokens` expands `{$NAME}` and `{$NAME:default}` in a non-structural address token (quoted or
not) before the existing placeholder refusal, then compares the result. `RouteManager` reads the mapping from
the command runner's `service_environment` callable once per transition; when the runner exposes none, or the
callable returns `None`, the environment is unknown and any variable is refused naming it. A value is used only
when it is a single, non-empty token: whitespace, `{`, `}`, or a newline would change Caddy's token structure,
and an empty value cannot be an address, so both are refused naming the variable. The value never reaches a
message, a detail, or the job result, and nothing written to disk changes because expansion happens only in
the comparison.

**Resolve the binary once with a total function.** `routing.caddy_binary(environ=None)` returns
`SHIPLORD_CADDY_BINARY` when it is an absolute path with no NUL byte, and `/usr/bin/caddy` otherwise.
`SubprocessCaddy.__init__` defaults `caddy` to `None` and calls it; `installer._caddy_preflight` names
`getattr(caddy, "caddy", None) or caddy_binary()` in its refusal. The function is total because the helper
constructs `SubprocessCaddy()` inside paths (`admin.handle`) that do not catch `RoutingError`; a constructor
that raises would turn a typo into a crashed protocol response. A relative value is therefore ignored and the
refusal message names the default that was actually tried. The child environment stays the fixed validation
environment, so the override never reaches Caddy. Honest scope: the variable is read by the process that runs
the helper. The local CLI never sends it, `sudo` resets the environment by default, and the dispatcher execs
sudo with a fixed environment, so an operator must place it in the root helper's environment (for example
`sudo -E`, an `env_keep` entry, or the unit's `Environment=`) for deploy-time validation to see it. Bootstrap
is the case the issue asks for, and it works with `sudo -E`.

## Risks / Trade-offs

- [An imported file that is group-writable was previously read and is now refused] → This is the same trust
  rule the transaction already applies to route files; the refusal is uninspectable and names no content, so
  the operator fixes the mode.
- [A host that imports a snippet with arguments can no longer take a route change] → It could not be inspected
  correctly before either; the refusal names the snippet and the fix, and the change is documented.
- [One more `systemctl show caddy` call per route transition] → `SubprocessCaddy.validate` already reads the
  service environment per validation; the extra read is once per transition and bounded by the existing
  timeout.
- [A variable that expands to several tokens is refused rather than supported] → Supporting it would mean
  re-tokenizing the file, and a wrong guess could miss a conflict; fail closed and name the variable.
- [A relative override is ignored] → The refusal message names the path actually used, and the guide documents
  that the value must be absolute.

## Migration Plan

No migration. The route file format, the managed import, the transaction, the error kinds, and the exit codes
are unchanged. Rollback is reverting `routing.py`, `installer.py`, and the guide paragraph.

## Open Questions

None. `tests/linux/run.sh caddy` is the real-Caddy evidence and must pass, including the symlinked
`sites-enabled` case, before the pull request.
