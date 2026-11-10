# Proposal

## Why

`SubprocessCaddy.validate` runs `caddy validate` with a fixed `PATH` and `LANG` and a throwaway `HOME`, so a
Caddyfile that reads a variable the Caddy service has — `{$CF_API_TOKEN}`, for example — is validated without that
variable. Caddy substitutes the placeholder with an empty string, the candidate no longer parses, and the route
change is refused with an error that blames the Caddyfile. An application whose Caddyfile needs a variable can
therefore never be routed, even though the running service would accept the same file.

## What Changes

- Validation receives the environment the Caddy service runs with, read from the unit systemd reports
  (`Environment=` and `EnvironmentFiles=`) and, only when systemd cannot be read, from the packaged defaults
  `/etc/default/caddy` and `/etc/sysconfig/caddy`.
- `SubprocessCaddy` accepts an injectable provider for that environment, so a caller can supply it and tests can
  script it; the default provider reads only the documented service sources, never the caller's `os.environ`.
- The throwaway `HOME`, `XDG_DATA_HOME`, and `XDG_CONFIG_HOME` still override anything the service sets, so
  validation never writes real Caddy storage.
- Captured command output placed in an error's `detail` has the service's variable values removed, because that
  detail reaches the deployment account. The message itself still never carries a value.
- When the service environment cannot be read, validation behaves exactly as it does today: fixed `PATH` and
  `LANG`, throwaway home.
- Nothing else changes: no new commands, no protocol change, no exit-code change, no new dependency.

## Capabilities

### New Capabilities

- `caddy-validation-environment`: how validation obtains the environment the Caddy service runs with, how
  throwaway storage and secret redaction are preserved, and what happens when no service environment can be read.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/routing.py`: a stdlib-only service-environment reader and the `SubprocessCaddy` constructor and
  `validate` path.
- `tests/test_routing.py`: unit tests for the reader, the injected environment, the throwaway storage, the
  fallback, and redaction.
- `tests/test_caddy_integration.py`: a variable-using Caddyfile validated by a real Caddy through the packaged
  defaults path.
- No API, dependency, or protocol change. A host whose Caddy unit sets no variables validates exactly as before.
