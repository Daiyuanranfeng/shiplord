# Tasks

## 1. Runtime forced stop

- [x] 1.1 Add the keyword-only `force` path to `_remove_containers` / `_stop_gracefully` and `DockerRuntime.force_stop`, and verify in `tests/test_shutdown.py` that a `fail` record at `signalled` is killed, removed and cleared, that a plain stop still refuses, and that a legacy no-record container is killed without persisting a record
- [x] 1.2 Verify in `tests/test_runtime.py` that a forced stop of a legacy container with no record and `restart_policy: no` succeeds, that a paused container is still refused, and that two live containers are still refused before anything is signalled
- [x] 1.3 Verify in `tests/test_shutdown.py` that an unconfirmed kill keeps the journal and reports `intervention_required` with `kind: kill_unconfirmed`

## 2. Recovery

- [x] 2.1 Add `Deployer.recover(force_stop=False)`: force-stop, reconcile to nothing, clear the journal, and report `forced_stop` / `running: false`; verify in `tests/test_deploy.py` that the journal is cleared, the runtime is stopped, the route is removed, and the outcome is `recovered`
- [x] 2.2 Verify in `tests/test_deploy.py` that a plain recovery keeps today's behaviour, that a forced stop with no journal still stops a running process, that a native runtime is rejected, and that a runtime failure keeps the journal and reports `intervention_required`

## 3. Helper, worker, command, and CLI

- [x] 3.1 Accept and validate the `force_stop` argument for `recover` in `privileged_host._prepare`, store it in the job payload, and pass it in `worker.run_job`; verify in `tests/test_helper_actions.py` that a job runs with the option, and that an unknown argument and a non-boolean value are refused before any host state is opened
- [x] 3.2 Advertise `shutdown.FORCE_STOP_FEATURE` from the helper and check it in `commands.run_job_command`; verify in `tests/test_helper_actions.py` (capabilities list) and `tests/test_commands.py` (upgrade hint, exit 4, no job submitted)
- [x] 3.3 Add `--force-stop` to `shiplord recover` and pass it through `commands.run_job_command`; verify in `tests/test_cli.py` that the option reaches the command with `force_stop=True` and that `--help` lists it

## 4. Documentation and real Docker

- [x] 4.1 Document the escape hatch in `AGENTS.md` (when to use it, what it does, and that it leaves the application stopped) and verify the command it names exists via `shiplord recover --help`
- [x] 4.2 Add a real-Docker case in `tests/test_deploy_integration.py` with the `ignore` fixture: a blocked `fail` deploy, a refused plain recovery, a successful `recover --force-stop` that records the forced stop, a following deploy that succeeds, and a retirement that succeeds; verify with `tests/linux/run.sh docker`
- [x] 4.3 Run `python -m unittest discover -s tests` and verify it is OK with the same skip count as the baseline

## Workflow follow-up

- Archive the change after review.
