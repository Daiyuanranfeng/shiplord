# Proposal

## Why

A redeploy of the release that is already committed rotates the release records: `_candidate_state` writes
`previous = prior_state["current"]`, which is the same release, and `_retire` then discards the real previous
artifact because it is no longer in the two-release window. A later `rollback` "succeeds" by restarting the broken
release, and the release it should have returned to is gone. The `unchanged` shortcut only fires when the current
release is running **and** ready, so a release that is down or slow takes the full transition and destroys the only
good rollback target. A CI re-run triggers it, because each workflow attempt uses a new request ID.

## What Changes

- Committing a transaction whose candidate is the release already committed keeps the existing `previous` release
  instead of rotating `current` into it. A genuinely different release still rotates `previous` normally.
- Retirement decides what to drop from the state the transaction actually committed, so an artifact that `previous`
  or `current` still names is never removed.
- `record_commit` returns the state it committed, so the transaction reports the real `previous` release and
  retires against it without a second read.
- `rollback` refuses a committed `previous` that is the `current` release. A host that already ran the older helper
  has no rollback target left; reporting `no_previous_release` is honest, where restarting the broken release and
  calling it a rollback is not.
- Recovery still accepts the state the older helper wrote for such a transaction, so upgrading the helper between
  a commit and the journal's removal does not turn a recoverable application into `intervention_required`.
- Nothing else changes: no new command, no protocol or exit-code change, and a legitimate redeploy of a down release
  still restarts it.

## Capabilities

### New Capabilities

- `release-retention`: which releases the committed state retains as `current` and `previous`, when a redeploy
  counts as the same release, and which artifacts retirement may remove.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/release_state.py` (the committed-state rule and the recovery comparison) and
  `src/shiplord/deploy.py` (retirement, the reported `previous`, and the rollback guard).
- `tests/test_deploy.py`, `tests/test_release_state.py`, and `tests/test_deploy_integration.py`; the docker suite
  exercises the same path against a real daemon and registry.
- No API, dependency, or protocol change. A host whose state already has `previous == current` keeps that record
  until its next deploy; nothing deletes an artifact that record still names.
