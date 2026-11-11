# Tasks

## 1. Fallback in the doctor command

- [x] 1.1 Extract the local result in `src/shiplord/cli.py` into one helper that returns `ok`, `scope`, `checks`, and
      `client`, and use it for `--local`; verify `tests/test_cli.py` still passes unchanged for the `--local` cases
      (`test_doctor_local_reports_only_local_checks`, `test_doctor_reports_missing_prerequisites`).
- [x] 1.2 Change the default `doctor` path to run that helper when the current directory has no `ship.toml`, and keep
      a present-but-invalid manifest on the existing error path; verify with a test in `tests/test_cli.py` that an
      invalid `ship.toml` still exits 2 with the manifest error, and that a directory without one does not.
- [x] 1.3 Print the fallback's two human lines - server readiness not checked, and the next command
      (`shiplord configure` or `shiplord doctor --server <alias>`) - after `_render_doctor`; verify with a test that
      asserts both lines in the human output and that the manifest-not-found error is absent.

## 2. Exit codes and JSON shape

- [x] 2.1 Keep the fallback's exit code at 0 when every tool is present and 3 when one is missing; verify with a test
      that runs `doctor` without a manifest and with an empty `PATH` and asserts exit 3 plus `ssh: missing`.
- [x] 2.2 Assert that `doctor --json` without a manifest prints exactly the `ok`, `scope`, `checks`, and `client` keys,
      that `scope` says server readiness was not checked, and that the key set equals `doctor --local --json`; verify
      with that test in `tests/test_cli.py`.
- [x] 2.3 Remove `doctor` from `test_commands_that_need_a_manifest_fail_cleanly_without_one_and_write_nothing` and
      verify the test still asserts exit 2 for the commands that do need a manifest and that no file is written.

## 3. Integration check

- [x] 3.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is OK with 815 tests (the 810
      baseline plus the five added here) and 68 skips; record that `tests/linux/run.sh` was not run because the shared
      Docker daemon races other agents' suites.
- [x] 3.2 Run `openspec validate doctor-outside-an-application --strict` and confirm the change validates.

## Workflow follow-up

- Archive the change after review and merge.
