# Spec Delta

## Purpose

Defines when a runtime may report that a committed release is already running: every value the start consumes must
match, so a restore never accepts a process that runs a different configuration.

## ADDED Requirements

### Requirement: A Docker release is running only when every consumed value matches
A Docker release MUST be reported as running only when the container was created with the committed release's image,
shutdown policy, template, storage mount, memory limit, memory-swap, pids limit, restart policy, security options,
dropped capabilities, log driver and log options, published port, and managed environment.

#### Scenario: A changed memory limit is not the committed release
- **WHEN** the same image, commit, and sequence were started with a different memory limit
- **THEN** the committed release is not reported as running
- **AND** a restore restarts it with the committed memory limit

#### Scenario: A changed container port is not the committed release
- **WHEN** the same image, commit, and sequence were started with a different container port
- **THEN** the published loopback port binding is compared and the committed release is not reported as running

#### Scenario: Changed frontend origins are not the committed release
- **WHEN** the same image, commit, and sequence were started with different frontend origins
- **THEN** the managed environment the container was created with is compared and the release is not reported as
  running

#### Scenario: An unchanged release is still running
- **WHEN** a container was created with exactly the values the committed release consumes
- **THEN** the release is reported as running and a restore does not restart it

### Requirement: A native release is running only when its unit and environment match
A native release MUST be reported as running only when the installed unit file equals the unit rendered for the
committed release and the installed environment file equals the environment rendered for it, in addition to the
service being active and not restarting.

#### Scenario: A changed memory limit is not the committed release
- **WHEN** the same binary, commit, and sequence were started with a different memory limit
- **THEN** the rendered unit's memory limit is compared and the committed release is not reported as running

#### Scenario: Changed frontend origins are not the committed release
- **WHEN** the same binary, commit, and sequence were started with different frontend origins
- **THEN** the rendered environment's origins are compared and the committed release is not reported as running

#### Scenario: The socket bind is compared
- **WHEN** the committed release is checked
- **THEN** the unit's socket bind must equal the registered host port the release serves on

### Requirement: Missing host state makes a release not running, never an error
When a value the start consumes is absent from the container or the unit, or the inspect itself cannot be read, the
running check MUST report the release as not running so a restart can produce the committed configuration. It MUST
NOT raise.

#### Scenario: A value an older helper did not record
- **WHEN** a container or unit lacks a value this check compares
- **THEN** the release is reported as not running
- **AND** restarting it writes the committed configuration

### Requirement: An unreadable environment file is not a mismatch by itself
The application's own environment file is operator input, not part of the release record. When it cannot be read, the
running check MUST NOT report the release as not running on that basis alone, because a start would fail on the same
file. The record-derived values MUST still be compared, and the check MUST NOT raise.

#### Scenario: A corrupt environment file
- **WHEN** the environment file is corrupt or is not private to the installing account
- **THEN** the record-derived values are still compared
- **AND** a release whose record-derived values match is reported as running, so a deploy that cannot start a
  candidate reports `rolled_back` with the existing environment-file failure instead of a failed restore

#### Scenario: A candidate replaced the record-derived values
- **WHEN** the environment file cannot be read and the running container or unit was started with another memory
  limit, port, or origins value
- **THEN** the committed release is not reported as running

### Requirement: The running check never exposes configuration values
The running check MUST compare environment values in memory only and MUST NOT put any environment value into a
message, a failure detail, a job result, or a test fixture.

#### Scenario: A mismatch reports only that the release is not running
- **WHEN** the managed or custom environment differs from the committed configuration
- **THEN** the check reports only that the release is not running
- **AND** no environment value appears in any message or detail
