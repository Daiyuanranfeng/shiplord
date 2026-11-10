# Design

## Context

See proposal.md for why. The current state that shapes the approach:

- `CaddyCommands` is a two-method protocol (`validate`, `reload`) implemented by `SubprocessCaddy` and by fakes in
  `tests/test_installer.py`, `tests/test_routing.py`, `tests/test_caddy_integration.py`, and `tests/fakes.py`.
- `SubprocessCaddy._run` already distinguishes a `CommandError` of kind `start` from one of kind `timeout`, but
  reports both under the caller's kind, so a missing binary becomes a `validation` failure.
- `RouteManager.ensure_import` catches every `RoutingError` from validation and relabels it
  `"Caddy rejected the Caddyfile with the managed import"`; `RouteManager._validate` relabels every failure as a
  rejected candidate or an already-invalid existing configuration.
- `installer.bootstrap` installs the version, switches `current`, and writes wrappers before it checks the Caddy
  configuration directory, writes the managed route directory, and calls `ensure_import`. Its only Caddy knowledge
  is `layout.caddy_root`; the binary path lives in `SubprocessCaddy`.
- The Caddy integration block runs under the host lock (`HostLock` on `layout.var`), which is acquired after the
  wrappers are installed.

## Goals / Non-Goals

**Goals:**

- One classification (`unavailable`) that means "the Caddy command could not be started", used everywhere a
  Caddyfile would otherwise be blamed.
- An up-front bootstrap check that runs the real Caddy command before any Shiplord file is written.
- A bootstrap ordering in which every Caddy failure leaves the previous helper and the host's files untouched.
- Tests that prove the ordering, not just the message.

**Non-Goals:**

- Detecting a Caddy binary by path (`shutil.which`, `os.access`, `/usr/bin/caddy`). The installer does not own the
  binary path, and a file that exists but cannot run (wrong architecture, `noexec`, missing loader) would pass.
- Making `doctor` report Caddy availability. It has its own probe and is outside this change's file scope.
- Changing the reload path's classification. `reload` runs `systemctl`, not `caddy`; a reload that cannot start is
  still reported as a reload failure, and the existing restore path handles it.
- Changing the managed route directory, placeholder, import format, or the `changed` dict's keys.

## Decisions

### A distinct error kind, not a new protocol method

`SubprocessCaddy._run` raises `RoutingError(kind="unavailable")` when `run_command` reports a `start` failure, and
keeps the caller's kind for a timeout. `RouteManager.ensure_import` and `RouteManager._validate` re-raise an
`unavailable` error instead of relabelling it.

The bootstrap preflight needs to make Caddy run, and the only protocol operation that runs Caddy is `validate`.
`probe_caddy(commands)` writes a fixed minimal Caddyfile to a throwaway directory and validates it through the
existing protocol. No method is added, so every fake still satisfies `CaddyCommands`, and a test scripts the
failure with `raise RoutingError(..., "unavailable")` — the same shape the real implementation produces.

Alternative considered: add `probe()`/`available()` to `CaddyCommands`. Rejected: it grows a two-method protocol for
one caller and forces four fakes to grow a method that would only ever return or raise. The error-kind approach
also keeps the probe honest, because it exercises the same `validate` path a route change uses.

Alternative considered: inspect the binary path with `os.access`. Rejected as a false negative source and because
the installer would have to learn a path the protocol deliberately hides.

### The probe validates a fixed minimal Caddyfile

`probe_caddy` validates:

```
http://127.0.0.1:1 {
	respond "ok"
}
```

from a `tempfile.TemporaryDirectory`. The content is fixed and independent of the host, so a refusal means "this
Caddy cannot validate anything", never "your Caddyfile is wrong". An `http://` site avoids TLS provisioning, and
validation does not bind the port, so it needs no privilege. `SubprocessCaddy.validate` already gives the child a
throwaway `HOME`/`XDG_*`, so the probe writes no real Caddy storage.

Alternative considered: validate the live `/etc/caddy/Caddyfile`. Rejected: it may be invalid for reasons unrelated
to this bootstrap (including an import of a route directory that does not exist yet), and the resulting message
would blame the operator's configuration for a check that is only meant to prove the binary runs.

Alternative considered: probe only when the managed import is missing. Rejected: the issue is a host with no Caddy
at all, and bootstrap should not depend on which half of a previous attempt completed to notice.

### Bootstrap orders the Caddy work before the helper work

`bootstrap` becomes:

1. root check, then `_caddy_preflight(caddy)`, then `_sshd_preflight`;
2. create the Shiplord directories and open `layout.var` for the host lock;
3. under the lock: Caddy configuration directory check, managed route directory, placeholder, `ensure_import`;
4. still under the lock: build payload, `_install_version`, `_switch_current`, `_install_wrappers`;
5. still under the lock: deployment group and sshd drop-in.

The preflight is before step 2, so a missing binary leaves no directory, version, wrapper, or symlink behind. Moving
the Caddy block before the version install means a Caddy failure at step 3 also leaves no new version directory and
no wrapper pointing at a helper that was never switched. The host lock is held for the code install as well; the
lock already covered the sshd work, and installing the payload is a bounded local write.

Alternative considered: move only `_switch_current` after the Caddy block. Rejected: `_install_version` would still
leave a complete version directory behind, and `_install_wrappers` (which references `current`) would run before
the symlink it depends on exists on a first bootstrap.

### The refusal names Caddy, the path, and the package

`_caddy_preflight` maps an `unavailable` probe to:

```
Caddy is not installed or cannot be started: install the caddy package or put the binary at /usr/bin/caddy
```

`/usr/bin/caddy` is the default `SubprocessCaddy` path and the one the issue names; the message carries no host path
beyond it, no environment value, and no command output. A probe that fails for any other reason is reported as
`Caddy rejected a minimal configuration (<kind>)` and never as the operator's Caddyfile.

## Risks / Trade-offs

- **Every bootstrap now runs one extra `caddy validate`.** → It is bounded by the same 30-second timeout and only
  runs on the root bootstrap path; a working host pays one short process, and the tests update their call counts.
- **The probe could fail for a reason other than a missing binary** (a broken installation, an unreadable Caddy
  storage directory). → That is still an unusable Caddy, and the message says so without blaming the Caddyfile;
  it is a better failure than a misleading validation error.
- **The host lock is now held during the payload install.** → The work is local file writes already performed by
  bootstrap; the lock's timeout is unchanged and no new lock is taken.
- **A fake that does not raise `unavailable` cannot exercise the preflight refusal.** → The refusal test scripts the
  kind explicitly, and `SubprocessCaddy` is covered against a real absent path in unit and real-Caddy tests.
- **The message hardcodes `/usr/bin/caddy`.** → It is `SubprocessCaddy`'s default and the path the issue asks to
  name; a caller that injects another path still gets the same install instruction, and the probe itself uses the
  injected path.

## Migration Plan

Nothing to migrate. The next bootstrap on a working host behaves as before, with one extra probe. Rolling back is a
code revert; no on-disk state changes.
