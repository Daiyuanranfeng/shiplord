# Spec Delta

## Purpose

Defines what replaces an application's CI key, how the key to replace is discovered without relying on the machine
that created it, what must be proven before the old key is revoked, and how the CI client state directory is keyed.

## ADDED Requirements

### Requirement: The key to replace is discovered from the server

Rotation SHALL determine the CI key of an application from the server's authorized-keys list, not from local state.
A key is the CI key only when its label is exactly the label derived from that key's own fingerprint.

#### Scenario: Rotation from a machine that never ran setup

- **GIVEN** an application whose CI key is registered on the server and whose GitHub secret is set
- **AND** the client machine has no Shiplord CI state for that application
- **WHEN** an administrator runs `shiplord github rotate-key`
- **THEN** the new key replaces the registered one
- **AND** the old key is removed from the server

#### Scenario: A key Shiplord cannot identify is never removed

- **GIVEN** the server holds a key whose label is not the label derived from its fingerprint
- **WHEN** an administrator runs `shiplord github rotate-key`
- **THEN** that key is left in place
- **AND** it is not reported as the rotated key

#### Scenario: Nothing to rotate is refused before anything changes

- **GIVEN** the server holds no identifiable CI key for the application
- **WHEN** an administrator runs `shiplord github rotate-key`
- **THEN** the command fails with an error naming `shiplord github setup`
- **AND** no key is generated, registered, or stored in GitHub

### Requirement: A rotation proves the new key before revoking the old one

Rotation SHALL register the new key, prove it authenticates over SSH with the pinned host keys, and store it in
GitHub before removing any old key. If any step fails, the old key SHALL remain the only registered CI key.

#### Scenario: The new key fails verification

- **GIVEN** a new key that the server does not accept
- **WHEN** an administrator rotates the CI key
- **THEN** the new key is unregistered, the GitHub secret is unchanged, and the old key still works

#### Scenario: The GitHub secret cannot be updated

- **GIVEN** GitHub refuses to store the new secret
- **WHEN** an administrator rotates the CI key
- **THEN** the unused new key is unregistered and the old key is still the only CI key

### Requirement: A rotation leaves exactly one CI key

After a successful rotation SHALL be exactly one identifiable CI key registered for the application, even when
earlier failures left more than one behind. The result SHALL name every label it revoked.

#### Scenario: Two CI keys from an earlier failed revocation

- **GIVEN** two identifiable CI keys are registered for the application
- **WHEN** an administrator rotates the CI key successfully
- **THEN** both old labels are revoked
- **AND** only the new key remains

#### Scenario: An old key cannot be removed

- **GIVEN** the new key is live and stored in GitHub
- **WHEN** the server refuses to remove an old key
- **THEN** the command fails and names the label that is still registered

### Requirement: CI client state is keyed by server and application

Any CI client state that remains on disk SHALL be stored per server alias and application, so the same application
name on two servers does not share state.

#### Scenario: The same application name on two servers

- **GIVEN** `orders` is set up against the `production` server and against the `staging` server
- **WHEN** either setup or rotation records state
- **THEN** each server keeps its own state
- **AND** rotating one server's key does not affect the other's

### Requirement: State from the earlier layout keeps working

State written at the old location SHALL be migrated to the server-scoped location on first use, so a machine that
already ran setup does not have to run it again. An interrupted setup's retained private key SHALL remain usable
after migration.

#### Scenario: Setup is repeated on a machine with old state

- **GIVEN** a completed setup recorded at the old location
- **WHEN** the same application is set up again for the same server
- **THEN** the existing key is reused
- **AND** no second key is registered

#### Scenario: An interrupted setup is resumed after migration

- **GIVEN** a setup that failed after generating a key, leaving the private key at the old location
- **WHEN** setup runs again
- **THEN** the same key is resumed rather than replaced
