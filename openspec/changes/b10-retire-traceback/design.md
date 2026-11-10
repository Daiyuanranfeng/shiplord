# Design

## Context

See `proposal.md` - Why. The retirement callback is built by `make_retirer` and called from `deprovision` only when
`state["current"]` is not `None`. `ReleaseState.read_state()` already validates a state file before it reaches the
callback, but the callback is a standalone boundary: it is handed a mapping and currently indexes it directly. The
callback has already removed the route and stopped the runtime by the time it iterates the releases, so any failure
there leaves the application down and the operator without a report.

The callback must stay stdlib-only and must not gain a dependency on `release_state` internals, because it runs on the
server. Its current contract is `retire(registration, state) -> None`; `deprovision` ignores the return value, and the
exit-code contract in `AGENTS.md` must not change.

## Goals / Non-Goals

**Goals:**

- Make the release-state read total: no input to the callback can raise `TypeError` or `KeyError`.
- Validate an unusable state before the route or the runtime is touched.
- Return an honest report of what was removed and what was kept.
- Keep the change inside `retire.py` and its tests.

**Non-Goals:**

- Re-validating a release record field by field. `release_state` owns that contract; the callback only checks the
  shape it must index.
- Changing `deprovision`, its result shape, or its exit codes.
- Making artifact discards transactional or retryable.

## Decisions

### Validate the state shape, not the whole release record

`_releases(state)` accepts `None` (no recorded state) and a mapping whose `current`/`previous` entries are either
absent, `None`, or mappings. Anything else is unusable. Deep-validating commits, artifacts, and sequences would
duplicate `release_state._release` on the server for no operational gain: `deprovision` already refuses an invalid
state before the callback runs.

Alternative considered: catch `TypeError`/`KeyError` around the discard loop. Rejected because it reports a crash
instead of preventing one, and it cannot tell an operator whether the route is still in place.

### Validate before the first side effect

The check runs before `RouteManager.remove` and `runtime.stop`, so an unusable state leaves the application routed and
running. This follows the project rule to reject bad input before the first side effect and gives the operator a state
they can inspect and repair.

Alternative considered: retire anyway and report the unreadable state afterwards. Rejected because it turns a
recoverable refusal into a half-retired application.

### Return a report instead of `None`

The callback returns `{"removed": [...], "retained": [...]}`. Existing callers ignore the return value, so this is
backward compatible. Discard warnings become `retained` entries and the release is not listed as removed, which keeps
the report to verified outcomes only.

Alternative considered: raise when a discard warns. Rejected because discards are explicitly best effort and the route
and runtime are already gone; failing would leave the registration in place for a leftover image.

## Risks / Trade-offs

- [A caller passes a state mapping with a release that is a mapping but lacks `artifact`] -> `runtime.discard` may
  still raise; `release_state` is the only producer of release records in the real path, and the design deliberately
  does not duplicate its validation.
- [The report is not surfaced by `deprovision` today] -> The callback returns it and the tests pin it; surfacing it in
  the `deprovision` result is a separate change outside this scope.
- [Returning a value changes the callback's type] -> `deprovision` ignores it and every existing caller and test still
  passes.

## Migration Plan

No migration. The change is internal to the retirement callback and is covered by unit tests; the real-host suites
must be re-run where Docker is available.
