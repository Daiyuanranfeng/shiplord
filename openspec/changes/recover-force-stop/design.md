# Design

## Context

See `proposal.md` for the motivation and `specs/docker-runtime/spec.md` for the requirements.

The stop path is already a durable state machine. `DockerRuntime._stop_gracefully` persists a stop record in the
transaction journal before each Docker side effect and walks it through `intent`, `restart_disabled`,
`signal_pending`, `signalled`, `kill_pending`, `killed`. When a `fail` policy reaches its deadline it raises a
`RuntimeFailure` with `kind: shutdown_timeout` and `intervention_required`, so the deployer keeps the journal and
nothing restarts the process. `Deployer.recover` reconciles to the committed release and cannot clear the journal
while a stop record is present. `deprovision --retire` stops the application through the same runtime path.

Recovery, deploy, and retirement therefore all block on the same journal record and the same policy refusal.

## Goals / Non-Goals

**Goals:**

- One explicit, auditable way out of a `fail`-policy deadline, reachable only through the application's own
  authenticated helper request.
- Reuse the existing stop machine so a forced stop cannot skip a phase the journal depends on.
- Leave the host in a state where deploy, recover, and retire work without a second intervention.

**Non-Goals:**

- Changing the default or configured shutdown policy, or making any stop implicit.
- Force-stopping a paused container (it cannot be signalled at all; the existing refusal and its hint stand).
- Adding a second escape hatch to `deprovision --retire`; a completed forced stop already leaves retirement able to
  proceed, because no container is running.
- Changing the protocol version, the job record schema, or any exit code.

## Decisions

### Reuse the graceful-stop machine with a `force` flag

`_remove_containers` and `_stop_gracefully` gain a keyword-only `force` argument. With it, the wait after the signal
is zero and the `fail` deadline refusal is skipped, so execution falls into the existing `kill_pending` → `killed`
branch that already sends `SIGKILL`, verifies the exit, and only then clears the stop record. The journal therefore
records a forced stop exactly as it records a `force`-policy stop, and every existing interruption/retry guarantee
still holds.

Alternative: a separate kill-and-remove routine. Rejected: it would duplicate the phase ordering, the restart-policy
step, the ambiguity check, and the verified-exit rule that the Linux suites exist to protect.

Alternative: rewrite the stop record's `action` to `force`. Rejected: the record must match the container's labels,
and rewriting it would misrepresent the policy the release was deployed with and would lose the audit trail of the
override. The override belongs in the job result, not in the release state.

### A legacy stop with no saved record may be forced

A container whose restart policy was already disabled by an older helper, with no stop record in the journal, is
refused as `shutdown_in_progress` today. Under `force` it is killed from an in-memory record instead: the persisted
record type does not admit `restart_policy: "no"`, so nothing is written and the journal keeps no stop record to
clear. This is the only state in which a forced stop does not persist a record, and it is a state that only an
older helper can leave behind.

### Recovery leaves the application stopped

A forced stop does not restart the committed release. The release that ignores its stop signal is exactly the
release a restart would bring back, and the next deploy or retire would then block on the same deadline again. The
recovery instead removes the route and clears the journal, so the recorded release is still the committed one and
the application is verifiably down.

Alternative: restart the committed release and let the operator force-stop it again. Rejected: it does not satisfy
"deploy and retire proceed afterwards", and it recreates the condition the operator just asked to end.

The outcome stays `recovered` with `running: false` and a message naming the next step; the forced stop is a
recovery to a consistent, verified state, not a new status for clients to learn.

### `--force-stop` is an application action, not an admin action

The option travels in the `recover` request to the privileged helper, where `authorize_application` binds the
request to the application's registration and the deployment account's sudo identity. That is the same boundary
every deploy, rollback, and recovery already crosses, and the deployment account is already trusted to replace its
own application. `admin.py` stays untouched: it refuses deployment accounts, so routing the escape hatch through it
would make the option unusable by the account that runs recover. No new unauthenticated entry point is added.

### The helper advertises the capability

`shutdown.FORCE_STOP_FEATURE` is added to the helper's advertised features. When `--force-stop` is requested, the
client checks it in the capabilities response and reports an upgrade hint (exit 4) if it is missing, instead of
sending a request that an older helper answers with a bare `invalid_request`.

### The job payload carries the option

`recover` jobs store `{"force_stop": true}` (or `{}`) in their existing `request` field, which already round-trips
through the job record and makes a repeated request ID content-identical. The worker passes it to
`Deployer.recover(force_stop=...)`. No job schema change is needed.

## Risks / Trade-offs

- **An operator can leave an application down.** A forced stop removes the route and starts nothing. → The outcome
  says `running: false` and names deploy/retire as the next step, and `shiplord status` reports the committed
  release with `running: false`.
- **A forced stop is destructive to in-flight work.** The process is killed without draining. → It requires an
  explicit option on an explicit command, is recorded in the job result, and is the documented last resort.
- **A kill that is not confirmed keeps the journal.** → The existing `kill_unconfirmed` path is reused, so the
  outcome is `intervention_required` and nothing claims success.
- **Two live containers are still ambiguous.** → The forced stop refuses before signalling, exactly as an ordinary
  stop does; killing the wrong process is worse than refusing.
- **A paused container cannot be forced.** → It stays `intervention_required` with its existing message. Unpausing is
  a separate, deliberate operation.

## Migration Plan

The change is additive and needs no data migration: an older journal has no forced-stop state, and the new
`request.force_stop` field is absent from existing job records. Upgrade the helper with
`shiplord server bootstrap` before using the option; a client that asks an older helper for it reports the upgrade
hint. Rolling the helper back is safe once no `recover --force-stop` job is unfinished.

## Open Questions

None.
