# Spec Delta

## Purpose

Defines what Shiplord preserves from a Docker candidate that failed its transition: the candidate's own bounded log
tail, where it is reported, and how an unreadable or absent container is handled without changing the rollback result.

## ADDED Requirements

### Requirement: A failed candidate's own logs are captured before its container is removed

When a Docker candidate fails during a deploy transaction, the deploy outcome detail SHALL contain the last 100 lines
of that candidate's own container logs, captured before the restore stops or removes the container. The captured text
SHALL be reported in a `candidate_logs` object that states the tail size (`tail`) and the byte bound (`max_bytes`).

#### Scenario: A candidate that exits during readiness

- **WHEN** a Docker candidate starts, writes a crash message, and fails readiness
- **THEN** the `rolled_back` outcome detail contains `candidate_logs` whose `output` ends with that crash message

#### Scenario: A candidate that fails after readiness

- **WHEN** a Docker candidate is ready but a later transition step fails and the deploy rolls back
- **THEN** the outcome detail still contains the candidate's own log tail

### Requirement: Only the candidate's container is read

The capture SHALL read the failed candidate's container specifically, by its own identity, so that it can never return
the restored release's logs. Log retrieval after a rollback (`shiplord logs`) SHALL remain unchanged and SHALL keep
reporting the restored release.

#### Scenario: Logs after a rollback

- **WHEN** a deploy rolls back to the previous release
- **THEN** the captured `candidate_logs` come from the failed candidate, and `shiplord logs` reports the restored
  release

### Requirement: Nothing is captured when the candidate has no container

A failure that never produced a candidate container SHALL NOT report `candidate_logs`. No placeholder, empty, or
invented output SHALL appear in the outcome.

#### Scenario: A prepare failure

- **WHEN** a deploy fails before the candidate container is created
- **THEN** the outcome detail has no `candidate_logs` field

### Requirement: The capture is bounded

The captured text SHALL keep the newest bytes, SHALL be stripped of control characters the same way application logs
are, and SHALL NOT exceed the stated `max_bytes`. The bound SHALL be small enough that the outcome still fits the job
record that persists it.

#### Scenario: A noisy candidate

- **WHEN** the candidate wrote far more than the byte bound
- **THEN** the stored `output` is at most `max_bytes` characters, begins with the existing truncation marker, and ends
  with the newest output

### Requirement: A capture failure never changes the rollback result

If the candidate's logs cannot be read, the outcome SHALL keep the status and exit code the restore produced, and the
`candidate_logs` object SHALL report the capture error instead of output. A capture failure SHALL NOT be reported as a
restore failure and SHALL NOT cause `intervention_required`.

#### Scenario: Docker cannot return the logs

- **WHEN** the candidate container exists but reading its logs fails
- **THEN** the outcome is still `rolled_back` with the restored release reported, and `candidate_logs` contains an
  `error` note with no `output`
