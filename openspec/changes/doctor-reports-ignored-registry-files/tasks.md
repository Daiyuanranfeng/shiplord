# Tasks

## 1. Report ignored registry files from the doctor

- [x] 1.1 Add a test in `tests/test_serverdoctor.py` that places `orders.json~` beside a provisioned registration,
      runs the doctor action, and asserts `checks["applications"]["ignored_files"] == ["orders.json~"]`, the
      registration is still listed, the report is ok, and the action exits 0; confirm it fails before the fix
      (`python -m unittest tests.test_serverdoctor`)
- [x] 1.2 Add a test in `tests/test_serverdoctor.py` that corrupts `orders.json` and asserts the applications check
      and the report are not ok; confirm it passes before and after the fix
- [x] 1.3 Copy `registry.stray` in `server_doctor()` and add `ignored_files` to the applications check without
      folding it into `problems`, then verify the tests from 1.1 and 1.2 pass
- [x] 1.4 Add a test in `tests/test_cli.py` that runs `doctor --server <alias> --deep` with a stubbed server result
      and asserts the ignored file is printed once, that an empty list prints no line, and that `--json` keeps the
      field; verify it fails before the renderer change
- [x] 1.5 Render `ignored_files` once in the `applications` branch of `_render_doctor` through the existing
      `_render_ignored_files` helper, then verify the test from 1.4 passes

## 2. Verify the change

- [x] 2.1 Run `python -m unittest discover -s tests` and confirm `OK` with the 810 baseline tests plus the new ones
      and 68 skips
- [x] 2.2 Revert `src/` temporarily and confirm the new tests fail, so they are regression tests and not assertions
      of existing behaviour

## Workflow follow-up

- Archive the change after review; `tests/linux/run.sh` is not run in this worktree because other agents are using
  the Docker daemon and the suites race.
