# Spec Delta

## Purpose

Checks a server's prerequisites and installs, switches and wires the Shiplord helper there, so that a host
either receives a working helper or is refused before anything on it changes.

## ADDED Requirements

### Requirement: Refusing a server whose interpreter cannot run the helper

A bootstrap SHALL confirm that `/usr/bin/python3` on the server is Python 3.11 or later before it builds,
uploads or switches the helper. A server that does not answer, has no interpreter at that path, or reports
an older version SHALL be refused.

#### Scenario: An interpreter older than 3.11

- **WHEN** `/usr/bin/python3` on the server reports Python 3.10 (Ubuntu 22.04) or 3.9 (Debian 11)
- **AND** a bootstrap of that server is requested
- **THEN** the bootstrap fails with exit code 3 and a message that names the interpreter path and the
  required version
- **AND** no helper archive is uploaded and no installed helper is switched

#### Scenario: No usable interpreter at the expected path

- **WHEN** `/usr/bin/python3` is missing or does not report a version
- **AND** a bootstrap of that server is requested
- **THEN** the bootstrap fails with exit code 3 and the same message naming the path and the version
- **AND** nothing is uploaded

#### Scenario: A version that is new enough

- **WHEN** `/usr/bin/python3` on the server reports Python 3.11 or later
- **THEN** the bootstrap proceeds to upload and install the helper as before

### Requirement: Checking before the helper is touched

The interpreter check SHALL run over the same SSH login the upload uses and SHALL complete before the
helper archive is built, uploaded, or switched. A refused bootstrap SHALL leave the installed helper, the
`current` version, the wrappers, the Caddy import and the sshd drop-in exactly as they were.

#### Scenario: A refusal leaves an existing installation alone

- **WHEN** a server already running an installed helper reports an interpreter older than 3.11
- **THEN** the bootstrap is refused with no upload, no temporary directory, and no change to the
  installed helper

### Requirement: Naming the requirement without leaking the host

The refusal message SHALL name the interpreter path and the required version and SHALL NOT contain any
other server path, environment value, or secret. The reported version SHALL be the interpreter's own
`major.minor` answer.

#### Scenario: The message is actionable

- **WHEN** a bootstrap is refused for its interpreter
- **THEN** the message contains `/usr/bin/python3` and `Python 3.11 or later`
- **AND** it contains no temporary directory, home directory, or environment value
