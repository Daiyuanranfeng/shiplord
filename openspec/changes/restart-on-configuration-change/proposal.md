# Proposal

## Why

A release record is not part of a container's or a unit's name. `release_id` covers the commit, the artifact identity,
and the sequence, so redeploying the same image/commit/sequence with a changed configuration reuses the same
container name and the same systemd unit. `DockerRuntime.is_running` compares only the image digest, shutdown policy,
template, and storage mount; `NativeRuntime.is_running` compares only the release marker in the unit file and the
active state. Neither notices memory, container port, or frontend origins.

When such a redeploy fails at the routing step, the restore asks whether the committed release is already running,
gets "yes" for the *candidate's* process, and skips the restart. State then records configuration A while the process
runs configuration B. A rollback that leaves the wrong configuration running is not a rollback.

## What Changes

- The Docker running check compares everything `docker run` consumes: the image digest, shutdown policy, template,
  storage mount, memory and memory-swap, pids limit, restart policy, security options and dropped capabilities, log
  driver and its `max-size`/`max-file`, the published port, and the environment the container was created with.
- The native running check compares the installed unit file with the rendered unit and the installed environment file
  with the rendered environment, in addition to the active state. That covers memory, the socket bind, and origins.
- The managed environment dict is built once and used by both `_start` and `is_running`, so the two cannot drift.
- Absent or unreadable host state is a mismatch, not an error: a restart is always safe and leaves the committed
  configuration running. The one exception is the operator's own environment file: when it cannot be read there is
  nothing to compare against and a start would fail on that same file, so the record-derived values still decide and a
  running release is not reported down for something a restart could not fix.
- No exit code, `rolled_back` / `intervention_required` semantics, journal, protocol, or CLI change.

## Capabilities

### New Capabilities

- `runtime-configuration-identity`: what must be true before a runtime may report that a release is already running -
  every value the start consumes, for both Docker and native releases - and what a mismatch or an unreadable input
  means.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/runtime.py`: one managed-environment helper and a wider `is_running` inspect; `_start` reuses the
  helper and the fixed run constants. `src/shiplord/native.py`: `is_running` compares the rendered unit and
  environment files. `src/shiplord/deploy.py` needs no change: its `_reconcile` already restarts when the check says
  the target is not running.
- Tests: `tests/test_runtime.py` (each consumed value is part of running identity, absent fields, corrupt environment
  file), `tests/test_native.py` (unit/environment drift, corrupt environment file), `tests/test_deploy_integration.py`
  (a same-commit redeploy with changed memory/port/origins and a forced routing failure rolls back to the committed
  configuration), and `tests/test_e2e_systemd.py` (the same scenario through the real CLI and systemd).
- No manifest, provisioning, protocol, exit-code, or dependency change. Server modules stay stdlib-only.
