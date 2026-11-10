# Spec Delta

## Purpose

Defines what a client is told about a durable job record: its state, phase, timestamps, and outcome, and when an
inactive worker means the job was interrupted.

## ADDED Requirements

### Requirement: A job with a recorded outcome is reported as finished

When a job's worker has recorded an outcome, the helper SHALL report the job as `finished` with that outcome, even if
the worker's unit is no longer active. A recorded outcome is the authoritative sign that the job ended.

#### Scenario: The poll races the worker finishing

- **GIVEN** a status poll read a `running` job record
- **AND** the worker recorded a finished outcome and its unit exited before the poll asked whether the unit is active
- **WHEN** the helper answers that poll
- **THEN** the job is reported as `finished`, with the recorded outcome, phase, and finish time
- **AND** the client returns the outcome's own exit code instead of reporting `interrupted`

#### Scenario: A finished job whose unit is already gone

- **GIVEN** a job record in state `finished`
- **AND** its worker unit is inactive
- **WHEN** a client asks for the job
- **THEN** the job is reported as `finished` with its recorded outcome

### Requirement: A job whose worker vanished is reported as interrupted

When a job record is still `queued` or `running` and its worker unit is inactive, and the record still says so when
re-read, the helper SHALL report the job as `interrupted`.

#### Scenario: The worker died before recording an outcome

- **GIVEN** a job record in state `queued` or `running`
- **AND** its worker unit is inactive
- **AND** the record still reports the same unfinished state when re-read
- **WHEN** a client asks for the job
- **THEN** the job is reported as `interrupted`
- **AND** the client returns exit code 6 with the `worker_vanished` detail and the instruction to run `shiplord recover`

#### Scenario: The record disappears before it can be re-read

- **GIVEN** a job record in state `queued` or `running`
- **AND** its worker unit is inactive
- **AND** the record is no longer readable when re-read
- **WHEN** a client asks for the job
- **THEN** the job is reported as `interrupted`, because no recorded outcome can be confirmed
