# Tasks

## 1. Tail-keeping capture in the shared runner

- [x] 1.1 Change `run_command` in `src/shiplord/system.py` to keep a rolling tail of at most `max_output` bytes and to
      record that earlier bytes were dropped; verify with a test that runs a command writing 300000 bytes ending in a
      sentinel and asserts the returned text ends with the sentinel.
- [x] 1.2 Extend `sanitize` with a `truncated` keyword that prepends the `[shiplord: earlier output truncated]` line and
      shortens the byte budget by the marker's length; verify with tests asserting the marker is the first line, that the
      returned text never exceeds `max_output`, and that `max_output=0` returns an empty string.
- [x] 1.3 Verify an untruncated capture is returned verbatim with no marker, and that a window cut inside a multi-byte
      UTF-8 character decodes to a replacement character instead of raising.
- [x] 1.4 Keep the existing control-character test meaningful by shrinking its output below the cap, and verify it still
      asserts that escape sequences become `?`.

## 2. Log paths

- [x] 2.1 Verify `DockerRuntime.logs` returns the trailing lines when `docker logs` exceeds `MAX_LOG_BYTES`, using a
      fake system that delegates to the real `run_command` against a script writing more than the cap; add the test to
      `tests/test_runtime.py`.
- [x] 2.2 Verify `NativeRuntime.logs` returns the trailing lines when the journal output exceeds `MAX_LOG_BYTES` the
      same way; add the test to `tests/test_native.py`. Confirm neither `runtime.py` nor `native.py` needs a code change.

## 3. Integration check

- [x] 3.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is OK with the baseline 68 skips;
      record that `tests/linux/run.sh` cannot run here because Docker is unavailable.
- [x] 3.2 Run `openspec validate b5-log-truncation --strict` and confirm the change validates.

## Workflow follow-up

- Archive the change after review and merge.
