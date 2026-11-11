# Proposal

## Why

On a host with no `caddy` binary, bootstrap reports `Caddy rejected the Caddyfile with the managed import`, which
sends the operator to debug a Caddyfile that is not the problem. The failure also happens after `_switch_current`
has activated the new helper, so a bootstrap that never wired Caddy still replaced the helper the host was running.

## What Changes

- A Caddy command that cannot be started is reported as `RoutingError` kind `unavailable`, not as a validation
  failure. A timeout keeps the kind of the operation it interrupted.
- `ensure_import` and the candidate-validation path propagate `unavailable` instead of relabelling it as a rejected
  Caddyfile or an already-invalid existing configuration.
- `installer.bootstrap` runs a Caddy preflight before it creates any directory, installs a version, or switches
  `current`: it validates a fixed minimal Caddyfile through the injected `CaddyCommands`, so a missing or
  unstartable binary is refused up front with a message naming Caddy, the binary path, and how to install it.
- The Caddy integration block (configuration directory, managed route directory, placeholder, import) runs before
  `_install_version` and `_switch_current`, so a Caddy failure leaves the previous helper active and no version
  directory behind.
- The existing "Caddy is not installed: its configuration directory is missing" refusal still works, now before
  the helper is installed too.
- No new method on `CaddyCommands`; the preflight uses `validate`, so every existing fake still satisfies the
  protocol and a fake can script the distinct error kind.

## Capabilities

### New Capabilities

- `caddy-availability`: how bootstrap decides that Caddy is usable, how a missing or unstartable Caddy command is
  classified and reported, and the ordering that keeps the previous helper active when Caddy is unavailable.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/routing.py`: the `unavailable` error kind, the probe helper, and propagation in `ensure_import` and
  candidate validation.
- `src/shiplord/installer.py`: a Caddy preflight before the first side effect and the reordered bootstrap steps.
- `docs/guide.md`: the server requirement names the Caddy package and the `/usr/bin/caddy` path bootstrap expects.
- `tests/test_routing.py`, `tests/test_installer.py`, `tests/test_caddy_integration.py`: the distinct kind, the
  preflight message, the unchanged helper after a missing-Caddy failure, and the real-Caddy probe.
- No protocol, API, dependency, exit-code, or on-disk format change. A host with a working Caddy bootstraps as
  before, with one extra `caddy validate` of a fixed minimal configuration.
