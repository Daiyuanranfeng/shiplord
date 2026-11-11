# Proposal

## Why

`shiplord doctor` is the command an operator reaches for when something is wrong. Run outside an application
directory - the first-run case, before any `ship.toml` exists - the default path loads a manifest unconditionally and
exits 2 with "ship.toml was not found", so the one diagnostic that could still help refuses to run. The local
prerequisite checks it needs already exist behind `--local`.

## What Changes

- The default `doctor` path falls back to the local prerequisite checks when the current directory has no `ship.toml`
  instead of exiting 2.
- The fallback states plainly that server readiness was not checked and prints the next command to run.
- A present but unreadable or invalid `ship.toml` still reports its manifest error and exits 2; only a missing manifest
  falls back.
- `--json` keeps the existing local result shape (`ok`, `scope`, `checks`, `client`); no key is added and no key is
  renamed.
- Exit codes are unchanged: 0 when every local tool is present, 3 when one is missing.

## Capabilities

### New Capabilities

- `doctor-diagnostics`: how `shiplord doctor` reports what it checked, what it did not check, and what to run next.

### Modified Capabilities

None; the project has no existing specs.

## Impact

- `src/shiplord/cli.py`: the `doctor` command and its human rendering.
- Tests: `tests/test_cli.py` - the no-manifest case, the JSON shape, and the invalid-manifest case; the existing
  "commands that need a manifest" list drops `doctor`.
- No protocol, manifest, exit-code, or CLI-argument change, and no new server contact.
