# Spec Delta

## Purpose

Prepares, starts, stops, and reconciles Docker releases, including an explicit forced stop that ends a process
which ignores its configured stop signal so the application can be deployed, recovered, or retired again.

## ADDED Requirements

### Requirement: An explicit forced stop ends a process that ignores its stop signal

`shiplord recover --force-stop` SHALL stop a process that outlived its shutdown deadline with `SIGKILL`, even when
the release's recorded policy is `fail`. The forced stop SHALL be opt-in: a recovery without the option SHALL keep
the `fail` behaviour and refuse while the process is still draining.

#### Scenario: A fail-policy process ignores the stop signal

- **WHEN** a deploy, recover, or retire has left a process running past its `fail` deadline
- **AND** an operator runs `shiplord recover --force-stop`
- **THEN** the process is sent `SIGKILL` and removed, and the recovery exits 0 instead of reporting the
  `shutdown_timeout` refusal

#### Scenario: A recovery without the option keeps the policy

- **WHEN** the same process is still draining
- **AND** an operator runs `shiplord recover` without `--force-stop`
- **THEN** the recovery reports `intervention_required` with `kind: shutdown_timeout` and nothing is killed

### Requirement: A forced stop is recorded in the job result

The job result of a forced stop SHALL record that a process was killed: the policy that was overridden, the signal
that was sent, the stages that were performed, and the elapsed time. The record SHALL NOT contain environment
values, secrets, or host paths.

#### Scenario: Reading the forced stop afterwards

- **WHEN** a forced stop has finished
- **THEN** the job outcome contains a `forced_stop` object that reports `forced: true`, `SIGKILL`, and the
  overridden policy and stages

### Requirement: A forced stop leaves the application stopped and reconciled

After a forced stop the journal SHALL be cleared only once the process has exited and been removed, and the
application SHALL be left stopped with its route removed rather than restarting the release that ignores its stop
signal. A later deploy, recover, or retire SHALL then proceed instead of reporting the fail-policy refusal.

#### Scenario: Deploying after the forced stop

- **WHEN** a forced stop has completed and the journal is cleared
- **THEN** a deploy of a new release starts without waiting for a stop that cannot finish

#### Scenario: Retiring after the forced stop

- **WHEN** a forced stop has completed and no container is running
- **THEN** a retirement stops nothing, removes the route, and reports the application's artifacts

#### Scenario: The forced stop itself fails

- **WHEN** the process cannot be killed or does not exit after `SIGKILL`
- **THEN** the recovery reports `intervention_required` and keeps the journal, so no success is claimed

### Requirement: Only an authorized, explicit request force-stops

A forced stop SHALL be reachable only through an authenticated request from the application's deployment account
that carries an explicit `force_stop: true` argument. The helper SHALL reject an unknown argument, a non-boolean
value, and a forced stop for a runtime that has no shutdown policy. A helper that does not support forced stops
SHALL advertise that it cannot, so the client reports an upgrade hint instead of a protocol error.

#### Scenario: A malformed request

- **WHEN** a request names `recover` with an unknown argument, or with `force_stop` that is not a boolean
- **THEN** the helper refuses it before any host state is opened

#### Scenario: An older helper

- **WHEN** the client asks for `--force-stop` and the helper does not advertise the capability
- **THEN** the client reports that the helper must be upgraded and no job is submitted

#### Scenario: A native application

- **WHEN** `--force-stop` is requested for an application whose runtime is a native binary
- **THEN** the recovery is rejected with an invalid-request outcome and nothing is stopped
