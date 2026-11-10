# Proposal

## Why

`deprovision --retire` removes the public route and stops the runtime before it reads the recorded releases. The
retirement callback then indexes `state["current"]` and `state["previous"]` without checking the state it was given,
so a missing or malformed release state raises an uncaught `TypeError` after the application is already down and
unrouted. The operator sees a traceback instead of a report of what the retirement did and what it kept.

## What Changes

- Validate the release state before the first side effect, so an unusable state is refused while the route and the
  running release are still intact.
- Treat an absent release state as "no releases to discard": the route and runtime are retired, and the report says
  which artifacts could not be identified.
- Return a retirement report that lists what was removed and what was kept, including discards that only produced
  warnings.
- Add regression tests for a missing state, an empty state, an unusable state, and a discard that is not verified.

## Capabilities

### New Capabilities

- `application-retirement`: how `deprovision --retire` takes an application out of service, what it validates first,
  and what it reports as removed or kept.

### Modified Capabilities

<!-- No existing capabilities; this project has no specs yet. -->

## Impact

- `src/shiplord/retire.py`: the retirement callback validates the state and returns a report.
- `tests/test_retire.py`: regression coverage for the new behavior.
- The `deprovision` exit-code contract is unchanged. Callers that ignore the callback's return value keep working.
