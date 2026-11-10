# Proposal

## Why

A Docker candidate that fails readiness is removed by the restore before anything reads its `docker logs`, so the
crash output that explains the failure is gone. The deploy result says `rolled_back`, `shiplord logs` afterwards shows
only the restored release, and every failed deploy becomes a manual investigation on a host the operator may not be
able to reach. The whole point of a rollback is to preserve service **and** explain what broke.

## What Changes

- Before the restore stops and removes a failed Docker candidate, Shiplord reads that candidate's own container logs
  (last 100 lines, bounded the same way `shiplord logs` is bounded) and puts them in the deploy outcome's detail as
  `candidate_logs`.
- The capture is keyed by the candidate's container name, not by "whichever container is listed first", so it can
  never read the restored release's logs. `shiplord logs` is unchanged and still shows the restored release.
- A candidate that never produced a container (prepare failure, a `docker run` that created nothing) reports no
  `candidate_logs` field at all; no logs are invented.
- A log capture that fails is reported in the detail as an error note. It never turns a verified restore into a
  failure: the outcome stays `rolled_back` (exit 5) or `failed` (exit 1) exactly as before.
- The stored output is bounded below `MAX_LOG_BYTES` so the outcome still fits the job record that persists it
  (`MAX_RECORD_BYTES`); the detail states the tail size and the byte bound.
- No exit code, status, `intervention_required`, protocol, or CLI-argument change.

## Capabilities

### New Capabilities

- `failed-candidate-diagnostics`: what Shiplord captures from a failed Docker candidate before the restore removes it,
  where it is reported, how it is bounded, and how a capture failure is reported without changing the rollback result.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/runtime.py`: a by-name, bounded log read for one release's container (`release_logs`).
- `src/shiplord/deploy.py`: capture in `_restore` before `_reconcile` removes the candidate, and the outcome field.
- Tests: `tests/test_deploy.py` (fake runtime, outcome detail), `tests/test_runtime.py` (by-name read, bound, absence,
  failure), and `tests/test_deploy_integration.py` (a real crashing fixture, and `shiplord logs` after the rollback).
- No manifest, protocol, exit-code, or dependency change. Server modules stay stdlib-only.
