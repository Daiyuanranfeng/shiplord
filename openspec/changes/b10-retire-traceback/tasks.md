# Tasks

## 1. Guard the release state

- [x] 1.1 Add a state-shape helper that accepts `None` and a mapping of optional `current`/`previous` releases, and verify `tests/test_retire.py` covers a missing, empty, and unusable state
- [x] 1.2 Call the helper before the route removal and runtime stop, and verify the unusable-state test asserts the route and runtime were untouched
- [x] 1.3 Raise `ProvisionError` with a clear message for an unusable state, and verify the test asserts no traceback and no host path in the message

## 2. Report the outcome

- [x] 2.1 Return `{"removed": [...], "retained": [...]}` from the retirement callback, and verify the tests assert the route and running release are under `removed` and application data is under `retained`
- [x] 2.2 List a discarded release only when `runtime.discard` returned no warnings, and verify the warning test asserts the release is kept and its warning is reported
- [x] 2.3 Update the callback docstring and verify the full unit suite still passes

## 3. Verification

- [x] 3.1 Run `.venv/bin/python -m unittest discover -s tests` and verify the summary is `OK` with the baseline count
- [x] 3.2 Record that `tests/linux/run.sh` was not run because Docker is unavailable
