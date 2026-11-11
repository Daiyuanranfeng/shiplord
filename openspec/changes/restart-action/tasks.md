# Tasks

## 1. Protocol and dispatcher accept the action

- [x] 1.1 Add `restart` to `protocol._ACTIONS` and `privileged_host.ACTIONS` (after `rollback`, before `recover`) and verify `tests/test_helper_actions.py` reports the new capabilities list
- [x] 1.2 Add `restart` to `jobs.ACTIONS` and verify a stored restart job validates through `JobStore`
- [x] 1.3 Add the `restart` branch to `privileged_host._submit` calling `Deployer.restart_preflight` and verify a refusal returns a `finished` outcome with no job record and exit 2 when nothing is deployed

## 2. Deployer transaction

- [x] 2.1 Implement `Deployer.restart` (`_gate`, read state, refuse a missing release) and verify the nothing-deployed test asserts exit 2 with no host work
- [x] 2.2 Thread `restart=True` through `_transition` and verify a successful restart keeps the commit, the sequence, and the previous release as the rollback target
- [x] 2.3 Implement `_restart_failed` (journal retained, `intervention_required`, exit 6, message naming `shiplord recover`, no environment value) and verify a failed restart never reports `rolled_back` while a failed `deploy` still does

## 3. Worker dispatch

- [x] 3.1 Dispatch `restart` explicitly in `worker.run_job` and verify a restart job calls `deployer.restart` rather than `deployer.recover`
- [x] 3.2 Treat `restart` like `rollback` in `worker._pull_image` (committed release image, `None`-safe) and verify the worker test asserts the pulled reference

## 4. Client command

- [x] 4.1 Guard `commands.run_job_command` on the advertised `restart` action with `CommandError(..., 4)` naming `shiplord server bootstrap`, and verify an old-helper capabilities response raises it without submitting a job
- [x] 4.2 Add the `shiplord restart` Typer command next to `rollback` and verify `tests/test_cli.py` asserts it reaches `run_job_command` with `"restart"`
- [x] 4.3 Name `shiplord restart` in the `env set` / `env clear` note and verify the JSON shape and exit codes are unchanged and no value is printed

## 5. Documentation

- [x] 5.1 Document `restart` in `docs/guide.md` (command table and the environment workflow) and verify the documented command matches the CLI
- [x] 5.2 Document the `restart` helper action in `docs/protocol.md` (action table and Jobs section) and verify it matches `privileged_host.ACTIONS`
- [x] 5.3 Mention the environment/restart workflow in `README.md` and verify the text names `shiplord restart`

## 6. End-to-end verification

- [x] 6.1 Add the real Docker set-env-then-restart test in `tests/test_deploy_integration.py` (criterion 1: the process sees the new value with the same commit) and verify it passes under `tests/linux/run.sh docker`
- [x] 6.2 Assert in that test that redeploying the same release after the environment change is `succeeded`, not `unchanged` (criterion 2)
- [x] 6.3 Run `.venv/bin/python -m unittest discover -s tests` and verify it is OK at the pre-change count plus the new tests
- [x] 6.4 Run `tests/linux/run.sh docker` and verify the restart test passes in the real host suite

## Workflow follow-up

- Archive the change with `openspec archive restart-action` after the PR merges.
