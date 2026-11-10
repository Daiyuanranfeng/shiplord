# Proposal

## Why

When a captured command produces more than `max_output` bytes, `run_command` fills its buffer from the front and
discards everything after the cap. `docker logs` and `journalctl` are read with `max_output=MAX_LOG_BYTES`, so a noisy
application's newest lines - the ones that explain a crash - are exactly the lines thrown away, and the surviving
output looks complete.

## What Changes

- The captured-output buffer keeps the **trailing** `max_output` bytes (the newest output) instead of the leading ones.
- Truncated output gains a single ASCII marker line at the front saying earlier output was dropped, so a truncated
  capture never looks complete. The marker counts against `max_output`, so the cap still holds.
- Control-character sanitization and the `CommandResult(code, output)` contract are unchanged.
- `docker logs` (`runtime.logs`) and `journalctl` (`native.logs`) both pick this up through `run_command`; neither call
  site needs a behavioural change.

## Capabilities

### New Capabilities

- `bounded-command-output`: how Shiplord bounds captured command output - the cap, which bytes survive truncation, the
  marker that announces dropped output, and the sanitization applied to what is returned.

### Modified Capabilities

None; the project has no existing specs yet.

## Impact

- `src/shiplord/system.py`: `run_command` buffering and `sanitize`.
- `src/shiplord/runtime.py` and `src/shiplord/native.py`: unchanged call sites, now returning trailing log lines.
- Tests: `tests/test_system.py` (core behaviour), plus log-path coverage in `tests/test_runtime.py` and
  `tests/test_native.py`.
- No protocol, exit-code, manifest, or CLI-argument change.
