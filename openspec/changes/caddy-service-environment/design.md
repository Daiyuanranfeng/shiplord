# Design

## Context

See proposal.md for why. The current state that shapes the approach:

- `SubprocessCaddy` is constructed at three root entry points: `installer.main` (bootstrap), `worker.production_services`
  (a deploy job), and `admin` (retire). None of them is given the host's service configuration, and only
  `installer.py` is in this change's file scope.
- `SubprocessCaddy.validate` runs `caddy validate` through `system.run`, which takes an explicit environment. Today
  that environment is `SubprocessCaddy.ENVIRONMENT` (`PATH`, `LANG`) plus a temporary `HOME`, `XDG_DATA_HOME`, and
  `XDG_CONFIG_HOME`.
- `RoutingError.detail` carries capped command output and reaches the deployment account through the job result, so
  it is a place a secret can leak, even though `str(error)` never contains output.
- The server modules must stay stdlib-only; `routing.py` already imports `system.run_command` and
  `release_state`, so a `System`-based probe adds no dependency.

## Goals / Non-Goals

**Goals:**

- A variable the Caddy service has is present during validation, from the sources systemd itself uses.
- `SubprocessCaddy` remains a subprocess wrapper: the environment is an injected input, not something `validate`
  discovers by reading process-global state.
- Tests can script the environment without a host, and can assert the throwaway storage and the fallback.
- Failure to read the service environment is invisible to the operator: validation falls back to today's behaviour.

**Non-Goals:**

- Parsing the unit file directly, or reproducing every systemd environment feature (specifier expansion, drop-ins,
  `PassEnvironment`, PAM). `systemctl show` already reports the resolved values.
- Changing the environment of `systemctl reload caddy`, or of any command other than validation.
- Making `serverdoctor` report the validation environment. It does not validate a Caddyfile today, so it has no use
  for the source of truth.
- A general environment-file parser for other services. The reader is Caddy-specific and lives in `routing.py`.

## Decisions

### The environment is an injected provider, defaulted to a documented reader

`SubprocessCaddy.__init__` gains an optional `service_environment` callable returning a mapping or `None`. The
default is `lambda: service_environment(HostSystem())`, a module-level function in `routing.py` that reads the
documented sources. A caller that already knows the environment can pass it; tests pass a scripted callable.

Alternative considered: a required constructor argument supplied only by `installer.main`. Rejected because the
two other root entry points that change routes — `worker.py` and `admin.py` — are outside this change's file scope,
and leaving them without the environment would keep the reported bug alive on the deploy and retire paths. A
defaulted provider fixes all three without editing the out-of-scope modules.

Alternative considered: read the environment inside `validate` with no injection. Rejected: it would make the
subprocess wrapper reach into host state, and the fallback and throwaway-home behaviour could not be tested without
a host.

### Read the unit systemd reports, then the packaged defaults

`service_environment(system, files)` runs
`systemctl show caddy --property=Environment --property=EnvironmentFiles` through the `System` abstraction, parses
`Environment=` (shell-like words, honouring quotes and backslash escapes) and reads each `EnvironmentFiles=` path
(the property is rendered as `<path> (ignore_errors=yes|no)`). When systemd cannot be read — missing executable,
non-zero exit, or output cut short — it reads the packaged defaults `/etc/default/caddy` and `/etc/sysconfig/caddy`
instead. An environment file is read only when it is a regular file no larger than 64 KiB; malformed assignments are
dropped the way systemd drops them.

`systemctl show` is preferred over parsing `/etc/systemd/system/caddy.service` because it reports the resolved
values, including drop-ins and specifier expansion, and never reports the caller's environment.

A truncated `systemctl show` answer is treated as unreadable rather than parsed: a half-reported value could be a
prefix of a secret, and validating with a prefix would be worse than falling back.

### The throwaway storage is applied last

`validate` builds the environment as fixed `PATH`/`LANG`, then the service variables, then `HOME`,
`XDG_DATA_HOME`, and `XDG_CONFIG_HOME` pointing at one temporary directory. Applying the throwaway paths last is
what keeps a service that sets `HOME=/var/lib/caddy` from making validation write real Caddy storage.

### Service values are removed from captured detail

`_run` accepts the values that came from the service and replaces each value of at least four characters with a
fixed marker in the captured output before it becomes `RoutingError.detail`. `str(error)` is unchanged and still
names only the failed command.

The four-character floor is deliberate: a value such as `C` or `1` would otherwise redact unrelated text and destroy
the diagnostics that make a refused route debuggable. Values that short are not credentials in practice, and Caddy
reports the placeholder `{$NAME}` rather than its value in the common "variable is empty" failure this change fixes.

Alternative considered: drop `detail` entirely when a service environment is present. Rejected: it removes the only
diagnostic for an ordinary syntax error and would make a misconfigured Caddyfile harder to fix.

## Risks / Trade-offs

- **The reader reports an environment Caddy does not actually run with** (for example, the unit was edited but not
  reloaded). → Validation then behaves like the service would after the next reload, which is the configuration
  being validated anyway; a failed reload still restores the previous route.
- **A service value is a substring of ordinary output.** → Only values of four or more characters are replaced, and
  only in captured detail; the operator sees a marker where the value was, which is the intended trade.
- **`systemctl show` costs one subprocess per validation.** → It is one bounded call beside the `caddy validate`
  call it feeds, and only on the root paths that already run several commands.
- **An environment file is unreadable to the helper.** → The variables are simply absent, and validation fails the
  way it did before this change; the message still blames the Caddyfile, which is the pre-existing behaviour, not a
  new failure mode.
- **`systemctl show` output grows with a large environment.** → The answer is capped at 64 KiB and a cut-short
  answer is refused rather than parsed.

## Migration Plan

Nothing to migrate. The next bootstrap, deploy, or retire validates with the service environment when it is
readable and exactly as before when it is not. Rolling back is a code revert; no on-disk state changes.
