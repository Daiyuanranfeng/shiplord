# Spec Delta

## Purpose

Manages an application's authorized keys through the privileged helper, so an administrator can add, list,
and remove the keys that may open a deployment login, and no key is ever stored for an application that does
not exist.

## ADDED Requirements

### Requirement: Key operations require a provisioned application

Adding, listing, or removing an application's keys SHALL refuse an application that is not provisioned, with
a single error that states the application is not provisioned. The error SHALL NOT contain a host path, a key
value, or a fingerprint. The refusal SHALL happen before any file or directory is created or changed, so a
refused call leaves the host exactly as it was.

#### Scenario: Adding a key for an application that was never provisioned

- **WHEN** an administrator adds a key for an application name that is not provisioned
- **THEN** the command fails with an error that the application is not provisioned
- **AND** no authorized-keys file and no directory for that name exists afterwards

#### Scenario: Listing keys for an application that was never provisioned

- **WHEN** an administrator lists keys for an application name that is not provisioned
- **THEN** the command fails with an error that the application is not provisioned

#### Scenario: Removing a key for an application that was never provisioned

- **WHEN** an administrator removes a key for an application name that is not provisioned
- **THEN** the command fails with an error that the application is not provisioned
- **AND** no authorized-keys file is created

#### Scenario: A stray key file does not make an application provisioned

- **WHEN** an authorized-keys file exists for a name that is not a provisioned application
- **AND** an administrator adds a key for that name
- **THEN** the command fails with an error that the application is not provisioned
- **AND** the stray file is left unchanged

### Requirement: Provisioned applications keep their key behaviour

For an application that is provisioned, adding, listing, and removing keys SHALL behave as before: an
identical key added twice reports no change, removing a label that is absent reports no change, and removing
the last remaining key is refused.

#### Scenario: Repeating an add and a remove

- **WHEN** an administrator adds a key that is already registered, or removes a label that is not registered
- **THEN** the command succeeds and reports that nothing changed

#### Scenario: The last key cannot be removed

- **WHEN** an administrator removes the only key an application has
- **THEN** the command fails and the key remains registered
