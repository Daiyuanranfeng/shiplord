# Tasks

## 1. Read one release's own container logs

- [x] 1.1 Add `tests/test_runtime.py` cases for `release_logs`: it reads the candidate's own container by name with
      `--tail` clamped to `MAX_LOG_LINES`, returns `None` when Docker says the container does not exist, returns empty
      text for an existing container with no output, and raises `RuntimeFailure` for any other failure; confirm each
      fails before the method exists (`python -m unittest tests.test_runtime`)
- [x] 1.2 Add a `tests/test_runtime.py` truncation case that `release_logs(..., max_bytes=N)` keeps the newest bytes,
      stays within `N`, and carries the truncation marker; confirm it fails before the method exists
- [x] 1.3 Implement `DockerRuntime.release_logs(registration, release, tail, *, max_bytes=MAX_LOG_BYTES)` using
      `docker logs` by container name, and re-run `python -m unittest tests.test_runtime`

## 2. Report the failed candidate's tail in the deploy outcome

- [x] 2.1 Add `tests/test_deploy.py` cases with the fake runtime: a readiness failure reports `candidate_logs` with
      the candidate's text, `tail`, and `max_bytes`; a routing failure after readiness does too; confirm both fail
      before the deploy change
- [x] 2.2 Add `tests/test_deploy.py` cases: a start failure before the container exists reports no `candidate_logs`,
      a native candidate reports none, and a capture failure is an `error` note while the outcome stays `rolled_back`
      with its restored release; confirm they fail before the deploy change
- [x] 2.3 Add `tests/test_deploy.py` cases that the capture is the candidate's text while `runtime.logs` still returns
      the restored release's text, and that the serialized outcome detail fits `MAX_RECORD_BYTES`
- [x] 2.4 Capture in `Deployer._restore` before `_reconcile` removes the candidate, and merge `candidate_logs` into the
      detail; re-run `python -m unittest tests.test_deploy`

## 3. Verify against a real daemon

- [x] 3.1 Add `tests/test_deploy_integration.py` assertions: the crashing fixture's captured output contains its crash
      message, the unhealthy fixture's capture names its own version, and `runtime.logs` after the rollback reports the
      restored release
- [x] 3.2 Run `python -m unittest discover -s tests` and confirm `OK` with the baseline test count and skips
- [x] 3.3 Run `tests/linux/run.sh docker` and confirm the integration assertions pass against the host daemon and
      registry

## Workflow follow-up

- Archive the change after the pull request is reviewed and merged.
