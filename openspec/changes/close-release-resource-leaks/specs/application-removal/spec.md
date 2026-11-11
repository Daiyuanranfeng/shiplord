# Spec Delta

## Purpose

Defines what removing a provisioned application takes off the host, and what it deliberately keeps.

## ADDED Requirements

### Requirement: Deprovision removes the application's job records
Removing an application MUST remove its job directory and every job record in it, and MUST report the removal when
the directory existed. A missing job directory MUST NOT be reported as a removal and MUST NOT fail the command.

#### Scenario: The job directory is removed with the application
- **WHEN** a provisioned application with job records is deprovisioned
- **THEN** the job directory no longer exists
- **AND** the result reports its removal

#### Scenario: An application with no job directory is removed
- **WHEN** a provisioned application that never created a job is deprovisioned
- **THEN** the command succeeds without reporting a job-directory removal

### Requirement: Deprovision removes the application's managed route
Removing an application MUST remove its managed route file and any interrupted-change backup for that application,
and MUST report the removal when either existed. A missing route directory or route file MUST NOT fail the command.

#### Scenario: A route file that retirement did not remove is removed
- **WHEN** an application with a managed route file is deprovisioned without a retirement callback
- **THEN** the route file no longer exists
- **AND** the result reports its removal

#### Scenario: An application with no route is removed
- **WHEN** an application without a managed route is deprovisioned
- **THEN** the command succeeds without reporting a route removal
