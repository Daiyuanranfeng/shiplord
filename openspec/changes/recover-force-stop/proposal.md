# Proposal

## Why

A release configured with `deploy.shutdown_timeout_action = "fail"` is never force-killed: when its process ignores
the stop signal, the stop expires, the transaction stays in the journal, and every later deploy, recover and retire
is refused. `AGENTS.md` forbids killing the process by hand on the server, so the policy has no escape hatch at all.

## What Changes

- `shiplord recover` gains an explicit `--force-stop` option. It sends `SIGKILL` to the process that outlived its
  shutdown deadline, removes it, and reconciles the journal, instead of waiting for the process to exit.
- The forced stop is an explicit opt-in: a plain `shiplord recover` keeps today's `fail` behaviour and still refuses
  while the process is draining.
- `recover --force-stop` leaves the application stopped and its route removed. Restarting the release that ignores
  its stop signal would immediately block the next deploy or retire, so the recovery does not start it.
- The job result records the forced stop (`forced_stop`: the policy that was overridden, the signal sent, the stage
  list and the elapsed time) so the action is auditable after the fact.
- The forced stop is authorized exactly like every other application action: it travels in an authenticated helper
  request bound to the application's deployment account, and only an explicit `force_stop: true` argument reaches it.
- The helper advertises a new optional feature so an older helper refuses the option with an upgrade hint instead of
  a bare protocol error.
- `AGENTS.md` documents the escape hatch and when to use it.

## Capabilities

### New Capabilities

- `docker-runtime`: preparing, starting, stopping, and reconciling Docker releases, including the explicit forced
  stop that ends a process which ignores its configured stop signal.

### Modified Capabilities

None; the project has no main specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/runtime.py`: `DockerRuntime.force_stop` and a `force` path through the existing graceful-stop
  machine, so the journal phase and the restart-policy step are reused rather than duplicated.
- `src/shiplord/deploy.py`: `Deployer.recover(force_stop=...)`.
- `src/shiplord/shutdown.py`, `src/shiplord/privileged_host.py`, `src/shiplord/commands.py`, `src/shiplord/cli.py`,
  `src/shiplord/worker.py`: the option, its validation, the job payload, and the capability name.
- `AGENTS.md`: the documented escape hatch.
- Tests: unit coverage for the runtime, the engine, the helper arguments, the command and the CLI; real-Docker
  coverage in `tests/linux/run.sh docker` with a process that ignores `SIGTERM`.
- No manifest, protocol-version, exit-code, or default-policy change: an ordinary stop still honours `force`/`fail`.
