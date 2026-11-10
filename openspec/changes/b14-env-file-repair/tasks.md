# Tasks

## 1. Prove the defect

- [x] 1.1 Add failing tests in `tests/test_provision.py::EnvironmentTests` for `clear` on an unreadable file (mode, invalid UTF-8, symlink), `set` on the same three, and `keys` on an unreadable file; run `.venv/bin/python -m unittest tests.test_provision.EnvironmentTests` and confirm the new tests fail with the current code
- [x] 1.2 Add a failing test that `set` over a symlinked `app.env` replaces the link instead of writing through it, and that the link target keeps its original contents

## 2. Repair the environment file

- [x] 2.1 Restructure `manage_environment` in `src/shiplord/provision.py` so `clear` unlinks without needing a readable file, `set` tolerates an unreadable file, and only `keys` requires one; verify tasks 1.1 and 1.2 now pass
- [x] 2.2 Add the repair notices to the result (`warnings` only when a file was discarded or removed unread) and the recovery hint to the `keys` error; verify the messages contain no value and no host path by asserting on the secret and on `layout.environments` in the tests
- [x] 2.3 Wrap the `clear` unlink so an `OSError` becomes a `ProvisionError`; verify a directory in place of `app.env` fails as a `ProvisionError` rather than raising `OSError`
- [x] 2.4 Add an admin-entry-point test that `environment keys` on an unreadable file returns `status: error` with exit code 1 and no traceback; verify with `run(encode(envelope("environment", arguments={"operation": "keys"})), ...)`

## 3. Confirm nothing else moved

- [x] 3.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is `OK` with 68 skipped: 791 tests, the original 783 plus the 8 added here
- [x] 3.2 Run `.venv/bin/python -m unittest tests.test_provision` and confirm the readable-file results are unchanged (`set` still returns exactly `{"keys": [...], "changed": ...}`)

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
- Note in the pull request that `tests/linux/run.sh` was not run because Docker is unavailable on this host.
