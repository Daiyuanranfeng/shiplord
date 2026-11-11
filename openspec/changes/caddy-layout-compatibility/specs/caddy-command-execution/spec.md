# Spec Delta

## Purpose

Locates the Caddy binary Shiplord validates with, so a host that installs Caddy outside the packaged path can
bootstrap and take route changes without a code change.

## ADDED Requirements

### Requirement: The Caddy binary path is resolved once

Shiplord SHALL resolve the Caddy binary through one documented rule used by both bootstrap and route
validation: `SHIPLORD_CADDY_BINARY` when it holds an absolute path, otherwise `/usr/bin/caddy`.

#### Scenario: The packaged default is used when nothing is set

- **WHEN** `SHIPLORD_CADDY_BINARY` is unset or empty
- **THEN** bootstrap and route validation run `/usr/bin/caddy`

#### Scenario: An absolute override is used by both

- **WHEN** `SHIPLORD_CADDY_BINARY` holds an absolute path such as `/opt/caddy/caddy`
- **THEN** the bootstrap probe and every later validation run that binary

#### Scenario: A value that is not an absolute path is ignored

- **WHEN** `SHIPLORD_CADDY_BINARY` holds a relative path
- **THEN** the packaged default is used, so a typo cannot change the working directory a privileged process
  starts Caddy in

### Requirement: The override is read by the process that runs the helper

Shiplord SHALL read the override from the environment of the process that runs the server helper. It SHALL NOT
forward it from the local CLI over SSH, and it SHALL NOT pass it to the Caddy child process.

#### Scenario: The local CLI environment does not reach the server

- **WHEN** an operator sets the variable only in the shell that runs `shiplord server bootstrap`
- **THEN** the server helper does not see it unless that variable is preserved by the host's sudo policy

#### Scenario: The Caddy child does not inherit the override

- **WHEN** a validation runs with the override set
- **THEN** the Caddy child receives the fixed validation environment, not the override
