# Design

## Context

See proposal.md - Why. The state that matters is one record per application:

```
state.json   { generation, transaction_id, current: release, previous: release, seen_requests }
journal.json { request_id, phase, prior_state, candidate, ... }        # only mid-transaction
```

`ReleaseState._candidate_state` derives the next committed state from the journal: `current` becomes the candidate
and `previous` becomes the prior `current`. That derivation is also the recovery check — `reconciliation_target`
compares the committed state with `_candidate_state(journal)` and refuses to reconcile anything else, so a state the
helper cannot re-derive is treated as corruption.

`Deployer._transition` writes that state with `record_commit`, removes the journal, and then calls `_retire` with the
*prior* state to discard the artifact that left the two-release window. `Deployer.deploy` takes the fast path
(`unchanged`) only when the candidate is the committed release **and** it is running and ready; every other
redeploy goes through the full transition.

## Goals / Non-Goals

**Goals:**

- The committed `previous` is always a release distinct from `current` that the application can return to.
- Retirement is decided from the state the transaction actually committed, never from an assumption about how the
  window moved.
- A legitimate redeploy of a down or slow release still restarts it.
- An application interrupted mid-transaction stays recoverable across a helper upgrade.

**Non-Goals:**

- Changing what a failed transition restores. A failed redeploy still restores the committed release, which is the
  release that was redeployed; only the rollback target is protected here.
- Retaining more than two releases. The window stays `current` + `previous`.
- Repairing a state that already lost its rollback target. There is nothing to recover from; the change stops the
  loss and makes the remaining state honest.

## Decisions

### Derive "same release" from the journal, not from a new field

`_candidate_state` keeps `prior["previous"]` when `journal["candidate"] == prior["current"]`, and rotates
`prior["current"]` into `previous` otherwise.

The comparison is the full release record — commit, sequence, artifact, and configuration — which is the same
equality `deploy` already uses for its `unchanged` shortcut. A redeploy with a changed configuration is therefore a
different release record and rotates normally, which is what rollback to a previous *profile* relies on.

Alternative considered: a `keep_previous` flag on the journal. Rejected: it changes the journal schema, and
`_journal` rejects unknown keys, so an older helper could not read a journal written by a newer one at all. Deriving
the rule from data the journal already holds keeps both directions readable and adds no state that could disagree
with the release records.

### Retire against the committed state

`record_commit` now returns the state it wrote, and `_retire(before, after)` keeps an artifact when `after["current"]`
or `after["previous"]` names it. In the ordinary rotation `after["previous"]` is `before["current"]` and the result is
identical to the old `[candidate, before["current"]]` set. In a redeploy of the current release, `after["previous"]`
is the real previous release, so its artifact is kept and nothing is discarded.

Alternative considered: keep passing the candidate and let `_retire` recompute whether the window moved. Rejected:
that duplicates the rotation rule in a second place, and the two copies would drift.

The outcome's `previous` field now reports `committed["previous"]` instead of `before["current"]`, so a redeploy no
longer claims the release it just restarted was its own rollback target.

### Refuse a rollback whose previous release is the current release

A state written by the older helper for a redeploy of a down release has `previous == current`. Rolling back to it
restarts the release that is already deployed and reports success. `rollback` now treats that record the same as a
missing one: `no_previous_release`, nothing started, nothing changed. The check is on the committed records only, so
it cannot fire for a state this version writes.

### Accept the older helper's commit when recovering

`reconciliation_target` accepts the committed state if it equals `_candidate_state(journal)` **or** the same state
with `previous` set to `prior_state["current"]` — exactly what the older helper wrote for this journal. Everything
else still fails closed as corruption, so the recovery check keeps its purpose.

This exists because the alternative is worse: an upgrade between `record_commit` and the journal's removal would
otherwise leave the application in `intervention_required` with a valid, internally consistent state, and the
operator would have to edit `state.json` by hand. The window is narrow, but the fix is three lines and is verified by
constructing the state the older helper wrote.

## Risks / Trade-offs

- **A release record that compares equal but is semantically different.** → Equality covers the whole record,
  including configuration, so only an identical request is treated as a redeploy; anything else rotates.
- **A state that already has `previous == current` can no longer be rolled back.** → It never had a rollback target;
  the next deploy rotates normally and keeps the artifact that record names. The refusal names the condition instead
  of restarting the broken release.
- **The legacy commit accepted during recovery keeps `previous == current`.** → Recovery only needs `current` from
  that state, and the rollback guard handles the rest; the next deploy rewrites `previous`.
- **Retirement can now leave an artifact behind when the window did not move.** → Only for the redeploy case, where
  the artifact belongs to a retained release. `deprovision --retire` still removes both retained releases.

## Migration Plan

Nothing to migrate. The next deploy writes the corrected `previous`; existing artifacts and records stay valid, and
a host that already lost its rollback target is left with an honest `previous == current` record instead of a
misleading rollback. Rolling back is a code revert.
