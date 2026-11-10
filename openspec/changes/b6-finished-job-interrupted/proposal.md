# Proposal

## Why

`job_view` in `src/shiplord/privileged_host.py` treats any `queued`/`running` job record whose systemd unit is
inactive as `interrupted`. A worker records its outcome before its unit exits, so a status poll that reads the record
just before the worker finishes and then asks systemd just after the unit is collected sees a stale `running` record
plus an inactive unit, and reports a successful deploy as `interrupted` (exit 6, "run `recover`"). That is a false
failure: it hides a successful release behind an error exit code and sends the operator to `recover` for nothing.

## What Changes

- `job_view` re-reads the job record before concluding `interrupted`, so a job whose worker already recorded a
  finished outcome is reported as `finished` with that outcome.
- A genuinely vanished worker (record still `queued`/`running`, or the record is gone) still reports `interrupted`.
- No change to the exit-code contract: `interrupted` is still exit 6, and `finished` still returns the recorded
  outcome's own exit code.

## Capabilities

### New Capabilities

- `job-status`: how a durable job record is presented to a client through `status` and the `deploy`/`rollback`/
  `recover` responses, including when an inactive worker unit means `interrupted`.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/privileged_host.py` — `job_view` only.
- `tests/test_helper_actions.py` — new cases for the finished-record/inactive-unit race and for a vanished worker.
- No protocol, exit-code, manifest, or CLI change. `tests/linux/run.sh` is unaffected by this change but cannot be
  run in this environment (Docker is unavailable).
