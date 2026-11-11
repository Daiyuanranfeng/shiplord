# Spec Delta

## Purpose

Defines the authorized operation that applies the application's current runtime environment to the deployed release
without a new commit, what it refuses, what it verifies, and how a failure is reported.

## ADDED Requirements

### Requirement: A restart is an authorized action with the same checks as a deploy
The helper MUST accept `restart` as a protocol action, advertise it in `capabilities`, and run it through the same
authorization and dispatcher path as `deploy`: per-application authorization, the protocol version check, refusal of
unknown arguments and of a non-null `commit`, the live-job limit, and idempotency by `request_id`.

#### Scenario: An authorized restart becomes a job
- **WHEN** an authorized client sends a `restart` request with no arguments and a null `commit`
- **THEN** the helper accepts it and starts one worker job for that request ID

#### Scenario: An unauthorized or malformed restart is refused
- **WHEN** a request names another application, carries an unknown argument, carries a non-null `commit`, or sends
  bytes after its frame
- **THEN** it is refused before any host state is opened, exactly as the equivalent `deploy` request is

#### Scenario: A repeated request ID does not restart twice
- **WHEN** the same `restart` request ID is submitted again
- **THEN** the recorded job is returned and no second job is launched

#### Scenario: An old helper is reported as needing an upgrade
- **WHEN** the client asks a helper whose `capabilities` do not advertise `restart`
- **THEN** the command fails with the prerequisite exit code and a message to re-run `shiplord server bootstrap`,
  without submitting a job

### Requirement: A restart applies the current environment to the deployed release
A restart MUST start the committed release again through the runtime's normal start path, so the application's current
environment file is read and the running process sees the new values. It MUST take the host lock, journal the attempt,
replace the runtime, apply the route, verify readiness, and only then commit and clear the journal. It MUST NOT change
the recorded release, its commit, or its sequence.

#### Scenario: A rotated value reaches the running process
- **WHEN** an operator changes an environment value and restarts
- **THEN** the process serving the application sees the new value and the recorded release is unchanged

#### Scenario: A restart keeps the rollback target
- **WHEN** a restart succeeds
- **THEN** the recorded previous release is still the release that was there before the restart

#### Scenario: A restart is recoverable after a crash
- **WHEN** a restart job is interrupted after it opened its journal
- **THEN** the journal remains and `recover` reconciles the committed release

### Requirement: A restart without a deployed release is refused
A restart MUST be refused with the invalid-input exit code and a message naming the missing release, before a job is
created and before any host work is done.

#### Scenario: Nothing is deployed
- **WHEN** the application has no recorded release and a restart is requested
- **THEN** the command fails with exit code 2, no job record is created, and nothing on the host changes

### Requirement: A restart during an unfinished transaction requires recovery
A restart MUST be refused with `recovery_required` when the application has an unfinished transaction, before any host
work is done.

#### Scenario: A journal exists
- **WHEN** an interrupted deployment left a journal and a restart is requested
- **THEN** the result is `recovery_required` and no restart job is created

### Requirement: A failed restart is never reported as a rollback
When a restart fails after its journal is opened, the helper MUST report `intervention_required` with exit code 6, keep
the journal, and state that the application did not come back and that `shiplord recover` is the next step after the
value is fixed. It MUST NOT report `rolled_back`, MUST NOT claim that a previous release was restored, and MUST NOT put
an environment value in the outcome.

#### Scenario: The new value fails readiness
- **WHEN** the restarted release does not become ready
- **THEN** the outcome is `intervention_required`, the journal remains, and the message names `shiplord recover`

#### Scenario: A deploy still rolls back
- **WHEN** a `deploy` of a different release fails readiness
- **THEN** it still reports `rolled_back` with the previous release restored, exactly as before

### Requirement: An environment change is visible to a redeploy
A redeploy of the release that is already committed MUST NOT be reported as `unchanged` when the application's
environment file no longer matches the environment the running process was created with; it MUST apply the change.

#### Scenario: The same release after an environment change
- **WHEN** the environment file changes and the same release is deployed again
- **THEN** the deploy reports success rather than `unchanged` and the process sees the new value
