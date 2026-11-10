# Tasks

## 1. Prove the defect

- [x] 1.1 Add `tests/test_runtime.py::PrepareTests` cases for an image whose stop signal is `SIGKILL`, `SIGSTOP`, `9`, `19` or an unknown name, asserting `RuntimeFailure` with phase `prepare`, a message naming the signal and the image, and that no volume is inspected, no container is started and none is removed; run `.venv/bin/python -m unittest tests.test_runtime.PrepareTests` and confirm the new cases fail with the current code
- [x] 1.2 Add `tests/test_deploy.py` a case that runs the real `DockerRuntime.prepare` against a fake Docker CLI and asserts the deploy fails with `error: prepare_failed`, that no journal exists, that the committed release and its container are unchanged, and that no route file is written; run `.venv/bin/python -m unittest tests.test_deploy` and confirm it fails with the current code

## 2. Refuse the image at prepare

- [x] 2.1 Factor the accepted-signal rule into `parse_stop_signal` in `src/shiplord/runtime.py`, keep `stop_signal` as the stop-time wrapper with its exact message and `stop` phase, and verify `tests/test_shutdown.py::SignalTests` still passes unchanged
- [x] 2.2 Read `.Config.StopSignal` in the existing image inspect of `DockerRuntime.prepare` and refuse an unusable value with a `prepare` failure that names the signal and the image; verify task 1.1 now passes
- [x] 2.3 Add `tests/test_shutdown.py` coverage that `parse_stop_signal` returns the normalised name for a catchable value and raises `ValueError` for an unusable one, so the shared rule is pinned independently of either caller
- [x] 2.4 Add `tests/test_runtime.py` coverage that an image with `SIGTERM`, `SIGINT`, `SIGQUIT`, `15` or no stop signal still prepares successfully; verify with `.venv/bin/python -m unittest tests.test_runtime.PrepareTests`

## 3. Confirm nothing else moved

- [x] 3.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm `OK` with 68 skipped: the baseline 810 tests plus the cases added here
- [x] 3.2 Run `.venv/bin/python -m unittest tests.test_runtime tests.test_shutdown tests.test_deploy` and confirm the deploy, shutdown and runtime suites pass together

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
- Note in the pull request that `tests/linux/run.sh` was not run, because the shared Docker daemon is in use by
  other work in this session.
