# Proposal

## Why

`stop_signal` validates the container's signal only inside `_stop_gracefully`, which runs when something
is already being stopped. Every deploy, rollback and retire stops the outgoing container first, so an
image whose `STOPSIGNAL` is `SIGKILL`, `SIGSTOP` or an unknown name deploys successfully and then makes the
application permanently un-replaceable and un-removable: the refusal arrives after the release is live, when
nothing can be undone.

## What Changes

- `DockerRuntime.prepare` reads the pulled image's `STOPSIGNAL` and refuses an image whose signal no process
  can handle, with a message that names the signal and the image.
- The refusal happens inside `prepare`, before the transaction journal is opened and before the release
  record is stored, so a refused deploy leaves the committed release, the journal and the routing untouched.
- The signal rule itself stays a single definition: the parser shared with `_stop_gracefully` is factored
  out and both callers use it, so prepare and stop cannot disagree about which signals are acceptable.
- No change to the accepted set: empty or `<no value>` still means `SIGTERM`, catchable names and numbers
  1-31 except 9 and 19 are still accepted.

## Capabilities

### New Capabilities

- `docker-runtime`: preparing, starting, stopping and inspecting Docker releases — including the rule that a
  release whose image cannot be stopped gracefully is refused before it is recorded.

### Modified Capabilities

None; the project has no specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/runtime.py`: `stop_signal` gains a shared parser, and `DockerRuntime.prepare` gains the check.
- `tests/test_runtime.py`, `tests/test_shutdown.py`: coverage for the shared parser and for prepare refusing
  the image before anything else runs.
- `tests/test_deploy.py`: coverage that the deploy is refused with no journal, no state change and no route
  change.
- No `deploy.py`, CLI, manifest, protocol or exit-code change: a prepare refusal already becomes
  `status: failed`, `error: prepare_failed`, exit code 1.
