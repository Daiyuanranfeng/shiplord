# Spec Delta

## Purpose

Defines what `shiplord doctor` reports when it cannot check an application - which local prerequisites it still
checks, what it says was not checked, and which command it names as the next step.

## ADDED Requirements

### Requirement: Doctor runs local checks when there is no manifest

`shiplord doctor` run in a directory without `ship.toml` SHALL run the local prerequisite checks instead of refusing
to run. It SHALL report whether `ssh`, `git`, and `gh` are available, using the same result the `--local` mode
produces.

#### Scenario: No ship.toml in the current directory

- **WHEN** `shiplord doctor` runs in a directory that has no `ship.toml`
- **THEN** it reports the local prerequisite checks and does not exit with the manifest-not-found error

#### Scenario: A local prerequisite is missing

- **WHEN** `shiplord doctor` runs without a manifest and a checked tool is not on `PATH`
- **THEN** the report marks that tool as missing and the command exits 3

### Requirement: The fallback states that the server was not checked

When `shiplord doctor` falls back to the local checks, its report SHALL state plainly that server readiness was not
checked, so the result cannot be read as a clean bill of health for a server.

#### Scenario: A fallback report names the unchecked scope

- **WHEN** `shiplord doctor` reports from the fallback path
- **THEN** the output says server readiness was not checked

### Requirement: The fallback names the next command to run

When `shiplord doctor` falls back to the local checks, it SHALL print the next command to run so the operator is not
left at a dead end: creating an application in this directory with `shiplord configure`, or checking a registered
server with `shiplord doctor --server <alias>`.

#### Scenario: The fallback prints a next step

- **WHEN** `shiplord doctor` reports from the fallback path in a terminal
- **THEN** the output names `shiplord configure` and `shiplord doctor --server <alias>` as the next commands

### Requirement: Fallback JSON keeps the local result shape

`shiplord doctor --json` without a manifest SHALL print the same result shape as the local checks: the `ok`, `scope`,
`checks`, and `client` keys, with no key added or renamed.

#### Scenario: JSON output without a manifest

- **WHEN** `shiplord doctor --json` runs in a directory that has no `ship.toml`
- **THEN** the JSON object has exactly the `ok`, `scope`, `checks`, and `client` keys and `scope` says server readiness
  was not checked

### Requirement: A present manifest is still authoritative

A `ship.toml` that exists but cannot be read or is invalid SHALL still be reported as a manifest error with exit code
2; the fallback applies only when the file is absent.

#### Scenario: An invalid manifest

- **WHEN** `shiplord doctor` runs in a directory whose `ship.toml` is not valid configuration
- **THEN** it reports the manifest error and exits 2 instead of running the local checks
