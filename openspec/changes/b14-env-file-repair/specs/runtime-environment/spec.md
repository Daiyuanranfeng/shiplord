# Spec Delta

## Purpose

Manages an application's runtime environment file through the privileged helper, so an administrator can
set, clear, and inspect the key names of the variables a release receives, including when the current file
can no longer be read safely.

## ADDED Requirements

### Requirement: Setting an environment replaces an unreadable file

When the current environment file cannot be read safely, setting an environment SHALL replace it with the
submitted values instead of failing, and SHALL report that the previous contents were discarded. The
replacement SHALL use the same private, atomic write as any other set: a new file created with mode `0600`,
flushed, and moved into place in one step.

#### Scenario: Set over a file that is not private to the installing account

- **WHEN** the existing environment file is readable only by a wider group or owned by another account
- **AND** an administrator sets a new environment
- **THEN** the command succeeds, the file holds only the submitted values, and the result reports that the
  previous contents were discarded

#### Scenario: Set over a file that is not valid UTF-8

- **WHEN** the existing environment file cannot be decoded as UTF-8
- **AND** an administrator sets a new environment
- **THEN** the command succeeds and reports that the previous contents were discarded

### Requirement: Clearing an environment removes an unreadable file

Clearing an environment SHALL remove the environment file without reading it, so an unreadable file can be
repaired with one command. The result SHALL report that a file was removed, and a repeat clear of an absent
file SHALL report that nothing changed.

#### Scenario: Clear over an unreadable file

- **WHEN** the existing environment file cannot be read safely
- **AND** an administrator clears the environment
- **THEN** the command succeeds, the file is gone, and the result reports that a file was removed

#### Scenario: Clear with no environment file

- **WHEN** no environment file exists
- **AND** an administrator clears the environment
- **THEN** the command succeeds and reports that nothing changed

### Requirement: Listing key names requires a readable file

Listing the configured key names SHALL fail with a single clear error when the environment file cannot be
read safely. The error SHALL name the recovery command and SHALL NOT include file paths or environment
values, and SHALL NOT surface as an unhandled exception or traceback.

#### Scenario: Keys over an unreadable file

- **WHEN** the existing environment file cannot be read safely
- **AND** an administrator lists the key names
- **THEN** the command fails with an error that names the recovery command and no traceback is shown

### Requirement: Environment values never leave the server

No result, error, log line, or diagnostic SHALL contain an environment value. Results SHALL contain key
names, a changed flag, and repair notices only.

#### Scenario: A repair never echoes the discarded values

- **WHEN** an unreadable environment file that held a secret is replaced or removed
- **THEN** neither the result nor any error message contains that secret
