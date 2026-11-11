# Proposal

## Why

`shiplord env set` writes the application's environment file, but nothing applies it to the process that is already
running. Redeploying the same release is the only existing path, and that path is exactly the one that reports
`unchanged` when the release is healthy - so a rotated secret is silently not applied and the operator cannot tell.
`privileged_host.ACTIONS` has no `restart` action at all.

## What Changes

- A new authorized helper action, `restart`, creates a job with the same dispatcher checks as `deploy`: the protocol
  action set, the per-application authorization, argument validation, a null `commit`, the live-job limit, and
  request-ID idempotency. `capabilities` advertises it.
- The worker runs `restart` through the existing deployment transaction, so the environment file is genuinely re-read
  by the runtime's existing start path. It takes the host lock, journals the attempt, replaces the container or unit,
  re-applies the (idempotent) route, runs the readiness probes, commits, and only then clears the journal.
- `Deployer.restart` refuses a restart when no release is deployed (exit 2, before any job exists) and while an
  unfinished transaction exists (`recovery_required`, before any host work).
- A restart that fails after the journal is opened reports `intervention_required` (exit 6) with the journal retained
  and a message naming `shiplord recover`. It never reports `rolled_back` and never claims a service restoration:
  the environment file belongs to the application, so the "previous" release record would run the same failed value.
- A new `shiplord restart` command, guarded by the helper's advertised actions, so an older helper gives an upgrade
  instruction instead of a bare protocol refusal.
- `env set` / `env clear` name `shiplord restart` in their note.
- A redeploy of the same release after an environment change is no longer `unchanged`; the merged running check
  already compares the container's environment with the file, and this change proves it on a real host.

## Capabilities

### New Capabilities

- `restart-action`: an authorized operation that applies the current environment to the deployed release without a
  new commit - what it refuses, what it verifies, and how a failed restart is reported.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/protocol.py`, `privileged_host.py`, `jobs.py`: the action is accepted, advertised, validated, and
  recorded.
- `src/shiplord/worker.py`: `restart` is dispatched explicitly instead of falling into `recover`; the committed
  release's image is pulled before the host lock, as `rollback` does.
- `src/shiplord/deploy.py`: `restart`, `restart_preflight`, and a failure path that retains the journal instead of
  restoring. `deploy`'s outcomes, exit codes, and `rolled_back` meaning are unchanged.
- `src/shiplord/commands.py`, `cli.py`: the `restart` command and the environment note.
- Docs: `docs/guide.md`, `docs/protocol.md`, `README.md`.
- Tests: protocol/dispatcher/worker unit tests, deploy transaction tests, command and CLI tests, and a real Docker
  set-env-then-restart test in `tests/test_deploy_integration.py` (run by `tests/linux/run.sh docker`).
- No new dependency, no protocol envelope change, no journal format change, no exit-code change. Server modules stay
  stdlib-only.
