# Proposal

## Why

`RouteManager.ensure_import` rewrites the main Caddyfile through `_create`, which opens every file with a fixed
`0o644` and lets the helper's umask decide the rest. A Caddyfile that an operator had tightened to `0640` — because
it holds an ACME DNS API token or a basic-auth hash — silently becomes world-readable on the next route change.
Route files inherit whatever umask `sudo` happened to give the helper, so the same code produces different
permissions on different hosts.

## What Changes

- Rewriting the main Caddyfile keeps the owner and permission bits of the file it replaces, captured from the same
  descriptor that was read, so the mode and owner cannot be swapped out between inspection and rewrite.
- If that owner cannot be applied to the replacement, the rewrite is refused before anything is published and the
  existing Caddyfile is left in place. A wrong owner with the right mode still exposes the file, so it is not
  treated as a partial success.
- Temporary copies of the Caddyfile made for validation keep the same owner and mode, so the token is not exposed
  through a side file in the same directory.
- Route files are created with one explicit mode, `0644`, regardless of the helper's umask. Route files hold only a
  hostname and a loopback port, and the installer already writes the placeholder route at `0644`.
- Nothing else changes: no new commands, no protocol change, no exit-code change.

## Capabilities

### New Capabilities

- `caddyfile-permissions`: the owner and mode the routing files must have, and what happens when they cannot be
  preserved.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/routing.py` and `tests/test_routing.py`.
- `tests/linux/run.sh caddy` exercises the same path against a real Caddy process.
- No API, dependency, or protocol change. A Caddyfile whose mode was already `0644` is rewritten exactly as before.
