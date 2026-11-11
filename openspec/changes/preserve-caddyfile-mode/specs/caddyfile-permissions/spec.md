# Spec Delta

## Purpose

Defines the owner and mode the Caddy configuration files must have, so a credential in the Caddyfile is never
exposed by a route change or by the account the helper happens to run under.

## ADDED Requirements

### Requirement: A rewritten Caddyfile keeps the owner and mode it had

When the main Caddyfile is replaced, the replacement SHALL have the same owner, group, and permission bits as the
file it replaces, whatever they were.

#### Scenario: A hardened Caddyfile stays hardened

- **GIVEN** a main Caddyfile owned by the helper account with mode `0640`
- **WHEN** a route change rewrites the Caddyfile to add the managed import
- **THEN** the rewritten Caddyfile has mode `0640`
- **AND** its owner and group are unchanged

#### Scenario: A temporary copy of the Caddyfile is no more readable than the Caddyfile

- **GIVEN** a main Caddyfile with mode `0640`
- **WHEN** a route change writes a temporary copy for Caddy to validate
- **THEN** that copy has mode `0640` for as long as it exists
- **AND** it is removed afterwards

### Requirement: A rewrite that cannot keep the owner is refused

If the owner or group of the replacement cannot be set to match the existing Caddyfile, the rewrite SHALL fail
without publishing anything, and the existing Caddyfile SHALL remain in place with its original owner and mode.

#### Scenario: An owner that cannot be preserved stops the rewrite

- **GIVEN** a main Caddyfile that the helper cannot give its owner to a new file
- **WHEN** a route change tries to rewrite the Caddyfile
- **THEN** the change fails before the live file is replaced
- **AND** the Caddyfile is unchanged in content, owner, and mode
- **AND** Caddy is not reloaded

### Requirement: Route files are created with an explicit mode

A managed route file SHALL be created with one fixed permission mode regardless of the umask the helper inherits.

#### Scenario: A restrictive umask does not change route permissions

- **GIVEN** the helper runs with umask `0077`
- **WHEN** a route file is created
- **THEN** the route file has mode `0644`

#### Scenario: A permissive umask does not widen route permissions

- **GIVEN** the helper runs with umask `0000`
- **WHEN** a route file is created
- **THEN** the route file has mode `0644`
- **AND** it is not group- or world-writable
