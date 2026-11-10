# Design

## Context

See `proposal.md` — Why. The relevant current state:

- `JobStore` (`src/shiplord/jobs.py`) keeps one root-owned JSON record per application and request ID. A worker moves
  it `queued` → `running` → `finished`; `finish()` writes the outcome, then the process returns and its transient
  unit goes inactive.
- `job_view` (`src/shiplord/privileged_host.py`) receives a record that a caller already read, and only asks the
  launcher `is_active` when that record is `queued` or `running`.
- The write order is fixed and durable: the record write (fsynced, `create=False`) happens before the unit exits.
  So if a *fresh* read after observing an inactive unit still says `queued`/`running`, the worker really is gone.

## Goals / Non-Goals

**Goals:**

- A finished job is never reported as `interrupted`, whichever way the poll interleaves with the worker.
- The report stays truthful for a worker that died without recording an outcome.

**Non-Goals:**

- Changing job state names, the protocol shape, or the exit-code contract.
- Recovering the outcome of a worker that died mid-transaction; `recover` owns that.
- Making the poll cheaper: one extra record read happens only when the unit is inactive.

## Decisions

**Re-read the record after `is_active` says the unit is gone, and trust the newer read.** The alternative — checking
`is_active` first and then reading the record — still leaves a window: a worker could finish between the launcher
check and the read, which is fine (finished wins), but the reverse ordering in the current code lets a stale
`running` snapshot win. Re-reading after the liveness check makes the decision monotone: the read is at least as new
as the observation that the unit is gone, and the worker writes its outcome before it exits.

**Treat a record that cannot be re-read as `interrupted`.** If `JobStore.get` returns `None` (pruned, or removed
between reads) no outcome can be confirmed, so the honest answer is `interrupted`, which is what the current code
already says. This keeps a vanished worker reporting `interrupted` and does not invent a success.

**Keep the change inside `job_view`.** `JobStore.get` already exists and validates the record; no helper is needed in
`jobs.py`. `job_view` takes the session, so it can re-read through `session.jobs` without changing its signature or
any caller (`_submit`, `_job`).

**Do not retry or wait.** A single re-read is enough given the write-before-exit ordering. Sleeping or polling would
add latency to a status call and still would not prove anything more.

## Risks / Trade-offs

- [A re-read hits an unreadable or invalid record] → `JobStore.get` raises `JobError` (a `StateError`), which the
  dispatcher already turns into `status: error`, `exit_code: 1`, without host paths. This is the same failure the
  caller's first read would have produced.
- [An extra filesystem read on every inactive-unit poll] → One small `read` of a root-owned file, only on the
  interrupted path; the finished path never re-reads.
- [The race is not reproduced on a real systemd host in this environment] → Covered by unit tests that hand
  `job_view` a stale snapshot while the store holds a finished record; the real-host suite that would exercise it
  (`tests/linux/run.sh`) cannot run here because Docker is unavailable.

## Migration Plan

None: no record, protocol, or on-disk format change. Rollback is reverting the commit; old records stay readable.
