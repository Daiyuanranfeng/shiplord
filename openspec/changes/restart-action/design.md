# Design

## Context

See `proposal.md` - Why, and `specs/restart-action/spec.md` for the requirements.

Three existing pieces shape the approach:

- `privileged_host.ACTIONS` is what `capabilities` advertises; `protocol._ACTIONS` is what `validate_request` accepts.
  They are separate lists, so an action missing from either is unusable in a different way.
- `jobs.ACTIONS` validates job records, and `worker.run_job` dispatches `deploy` and `rollback` explicitly and sends
  every other action to `deployer.recover`. An action that is not added explicitly would silently run recovery.
- `Deployer._transition(request_id, candidate, before)` is the only transaction: prepare, journal, take the lock,
  replace the runtime through `_start_runtime` (which re-reads the environment file), apply the route (idempotent),
  probe readiness, commit, clear the journal. `release_state._candidate_state` already keeps the rollback target when
  the candidate equals the committed release, so a transaction whose candidate is the current release is a legitimate
  restart and needs no journal change.

`runtime.is_running` compares `.Config.Env` (the `--env-file` expansion plus the managed `-e` values) with the current
`app.env`, so a changed value already prevents the `unchanged` shortcut from firing. Nothing needs to be weakened; the
change only needs a test that proves it end to end.

## Goals / Non-Goals

**Goals:**

- One transaction for restart and deploy, so the journal, host lock, readiness probes, route reconciliation, and
  recovery behaviour are shared rather than duplicated.
- The runtime's existing start path, so the environment file is genuinely re-read for both Docker and native releases.
- A failed restart that says what actually happened.

**Non-Goals:**

- A new runtime entry point, a second dispatcher, or a separate lock.
- Changing the protocol envelope, the journal format, exit codes, or `deploy`'s outcomes.
- Restarting on a timer or watching the environment file for changes.

## Decisions

### The action is added in all four places, explicitly

`restart` joins `protocol._ACTIONS`, `privileged_host.ACTIONS`, and `jobs.ACTIONS`, and `worker.run_job` gets an
explicit `elif record["action"] == "restart"` branch. The alternative - letting the `_prepare` fallthrough and the
worker's `else: recover` handle it - would record a job whose action is rejected by `JobStore._validate`, and, if that
were relaxed, would run recovery instead of a restart. `privileged_host._prepare` keeps its fallthrough (it already
handles `rollback` and `recover` the same way: no arguments, null commit), and `_submit` grows one branch for the
restart preflight.

### The dispatcher refuses bad restarts before the job exists

`Deployer.restart_preflight` mirrors `Deployer.preflight`: it runs `_gate` (journal → `recovery_required`, repeated
request ID → `already_applied` / `request_id_reused`) and refuses a missing release. `_submit` calls it for `restart`
the way it calls `preflight` for `deploy`, so the refusal is a `finished` response with no job record, no worker, and
no host work. The same checks run again inside `restart` under the lock, because the state can change between
submission and execution.

### A restart is the existing transaction with the current release as its candidate

`Deployer.restart` reads the committed release and calls `_transition(request_id, current, committed, restart=True)`.
Everything the transaction already does is what a restart needs: `prepare` verifies the image or binary without
touching the running release, `begin` journals, `_start_runtime` replaces the container or unit (reading `app.env`),
`routes.apply` returns `changed=False` when the route is already right, readiness is verified, `record_commit` keeps
the rollback target because the candidate equals the prior current, and `clear_after_reconciliation` removes the
journal only after all of that. The alternative - a new `Deployer.restart` that stops, starts, and probes on its own -
would duplicate the journal and probe ordering and drift from the deploy path.

### A failed restart retains the journal and reports intervention, never a rollback

`_transition`'s failure path calls `_restore`, which reconciles the committed release and reports `rolled_back` with
"the previous release is serving". For a restart that statement is false: the environment file is application-wide, so
the "previous" release record would run the same new value, and there is no older release to return to. A restart
failure therefore takes a distinct path (`_restart_failed`) that reports `intervention_required`, exit 6, keeps the
journal, and says the application did not come back and that `shiplord recover` is the next step after fixing the
value. The failure is chosen by the `restart` flag, not by "candidate equals current", so a *deploy* of the same
release keeps its existing `rolled_back` behaviour.

The journal must stay: the runtime may have been replaced and may not be ready, so clearing it would claim a
reconciliation that was not verified. `recover` then re-runs the committed release (with the corrected value) and
verifies readiness before clearing it.

### The worker pre-pulls the committed release's image

`worker._pull_image` treats `restart` like `rollback`: it pulls the release the job will start, before taking the
host-wide lock. `prepare` still verifies the image under the lock, and a failed pre-pull is reported there as the
prepare failure it is. A missing release is not an error here; the preflight and the transaction refuse it.

### The client guards on the advertised action

`commands.run_job_command` already checks `capabilities` for `--force-stop`. It does the same for `restart`, raising
`CommandError(..., 4)` with "re-run `shiplord server bootstrap`". The alternative - letting the old helper answer
`invalid action` with exit 2 - tells the operator to fix their input when the real fix is an upgrade.

## Risks / Trade-offs

- A restart briefly stops the application (the transaction replaces the runtime, as a deploy does) → the readiness
  probe and route reconciliation still decide the outcome, and a failure is reported honestly rather than hidden.
- A failed restart leaves the journal, so later deploys and restarts are refused until `recover` runs → that is the
  existing contract for any interrupted transaction, and the message names the command.
- `restart` is idempotent by request ID, not by effect: two different request IDs restart twice → the same behaviour
  as two deploys, and the client generates a fresh ID per invocation.
- The environment note now names `shiplord restart` → `set`/`clear` still print no values and keep the JSON shape.
