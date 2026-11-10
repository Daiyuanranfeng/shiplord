# Proposal

## Why

Five small leaks each accumulate on a long-lived host: a native release directory survives retention when one binary
ships under several commits, a prepared artifact survives a journal that could not be opened, an interrupted job
record is never pruned and keeps its request ID burned, job unit names truncate the request ID so two of them can
collide, and `deprovision` leaves the job directory and a stale route file behind. Individually each is small; together
they fill the host with dead state and make a job unit ambiguous.

## What Changes

- Retention compares the resource a runtime actually removes. For a native release that is its own directory, named
  for the release ID, so two commits carrying the identical binary no longer leave the older directory behind. A
  release that is still referenced is never discarded.
- A prepared artifact is removed when the transaction journal cannot be opened, unless a journal record exists that
  still references it.
- A queued or running job record whose worker has outlived the systemd `RuntimeMaxSec` bound is pruned, so the record
  no longer accumulates and its request ID can be used again. Live jobs are kept.
- A job unit name keeps the readable request ID when it fits and uses a hash of the whole request ID when it does not,
  so two request IDs that share their first 24 characters can no longer produce the same unit.
- `deprovision` removes the application's job directory and its managed route file, including an interrupted-change
  backup, and reports them under `removed` only when they existed.

## Capabilities

### New Capabilities

- `release-retention`: which resources a release owns, when a release that leaves the retention window may be
  discarded, and what happens to a prepared artifact whose transaction never started.
- `operation-jobs`: the durable job record lifecycle (creation, phases, pruning) and the systemd unit name a job is
  launched under.
- `application-removal`: what `deprovision` removes from the host, and what it keeps.

### Modified Capabilities

<!-- No existing capabilities; this project has no main specs yet. -->

## Impact

- `src/shiplord/deploy.py`: retention and abandon compare the removed resource, and a failed journal open abandons
  the prepared artifact.
- `src/shiplord/jobs.py`: interrupted records are pruned and unit names hash a long request ID.
- `src/shiplord/provision.py`: `deprovision` removes the job directory and the managed route file.
- `tests/test_deploy.py`, `tests/test_jobs.py`, `tests/test_provision.py`: regression coverage, one test per leak.
- No manifest, protocol, CLI, or exit-code change. Existing job unit names for request IDs of 24 characters or fewer
  stay identical, so a job launched by an older helper is still recognised.
