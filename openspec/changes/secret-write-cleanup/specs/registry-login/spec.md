# Spec Delta

## Purpose

Defines how one application's read-only registry credentials are stored, listed, and removed, and what an
administrator is told when the login file was edited by hand.

## ADDED Requirements

### Requirement: A registry login is written atomically and never leaves a token behind

The system SHALL write an application's registry login through a temporary file in the same directory, publish it
with a replace, and sync the directory. On any failure the temporary file SHALL be removed before the error is
reported, so no file containing the token outlives the command.

#### Scenario: A write that fails after the temporary file is created

- **GIVEN** a provisioned application with no registry login
- **WHEN** `registry login` creates its temporary file and the publish fails
- **THEN** the command reports that the registry login could not be written
- **AND** the login directory is empty
- **AND** no file in it contains the token

#### Scenario: A successful login is durable

- **GIVEN** a provisioned application
- **WHEN** `registry login` succeeds
- **THEN** `config.json` contains the new entry
- **AND** the login directory has been synced
- **AND** the temporary file is gone

#### Scenario: Logout removes the file and syncs the directory

- **GIVEN** a provisioned application with one registry login
- **WHEN** `registry logout` removes the last entry
- **THEN** `config.json` no longer exists
- **AND** the login directory has been synced
- **AND** a repeated logout succeeds without changing anything

### Requirement: A hand-edited login file is reported clearly

The system SHALL validate the login file before using or rewriting it. A document that is not JSON, a top level
that is not an object, an `auths` value that is not an object, an entry that is not an object, an `auth` value that
is not a string, base64, UTF-8, or `username:token` SHALL produce an error that names `config.json` and the
problem. The error SHALL NOT be a traceback and SHALL NOT contain a token or a host path.

#### Scenario: An unreadable file is not silently replaced

- **GIVEN** a provisioned application whose `config.json` is not valid JSON
- **WHEN** an administrator lists or changes the registry login
- **THEN** the command fails with an error naming `config.json`
- **AND** the file is left exactly as it was
- **AND** the message contains no token

#### Scenario: A malformed auth entry is reported, not raised

- **GIVEN** a provisioned application whose `config.json` has an entry without a usable `auth` value
- **WHEN** an administrator lists the registry logins
- **THEN** the command fails with an error naming `config.json` and the problem
- **AND** no traceback reaches the administrator
- **AND** the message contains no token
