# Spec Delta

## Purpose

Defines how an application's `authorized_keys` file is read, replaced, and removed without following a symbolic
link and without losing a completed change to a crash.

## ADDED Requirements

### Requirement: Key file operations use a verified directory descriptor

The system SHALL open the key directory once, verify that it is a directory owned by the running account and not
writable by anyone else, and perform every read, replace, and unlink relative to that descriptor with
`O_NOFOLLOW`. A symbolic link at the key file or the key directory SHALL never be followed.

#### Scenario: A symlinked key file is not followed

- **GIVEN** an application whose key file name is a symbolic link to a file elsewhere
- **WHEN** an administrator adds or removes a key
- **THEN** the command fails because the key file is not safe to use
- **AND** the file the link points at is unchanged

#### Scenario: A symlinked key directory is refused

- **GIVEN** the key directory path is a symbolic link to a directory
- **WHEN** an administrator lists the application's keys
- **THEN** the command fails because the key directory is not safe to use

#### Scenario: Deleting removes the link, not its target

- **GIVEN** an application whose key file name is a symbolic link to a file elsewhere
- **WHEN** the application is removed
- **THEN** the link is removed
- **AND** the file the link points at is unchanged

### Requirement: A key change is durable before it is reported

The system SHALL sync the key file before publishing it and sync the key directory after a replace or a removal.
A removal SHALL report success only after the directory sync has completed.

#### Scenario: Removal syncs the directory

- **GIVEN** an application with two keys
- **WHEN** an administrator removes one key
- **THEN** the remaining key file is published
- **AND** the key directory has been synced
- **AND** a repeated removal of the same label changes nothing

#### Scenario: Delete syncs the directory

- **GIVEN** an application with a key file
- **WHEN** the application is removed
- **THEN** the key file is unlinked
- **AND** the key directory has been synced
- **AND** a repeated delete succeeds without changing anything
