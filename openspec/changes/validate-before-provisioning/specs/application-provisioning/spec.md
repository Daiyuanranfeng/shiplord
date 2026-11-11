# Spec Delta

## Purpose

Provisions an application on the host: it establishes the deployment account, its key, its sudoers rule, its
private directories, and its registration, and it must be impossible for a refused provisioning to leave any
of that behind.

## ADDED Requirements

### Requirement: A refused provisioning creates nothing

Provisioning SHALL complete every validation it can perform read-only before it creates, changes, or removes
any host resource. A provisioning that is refused SHALL leave no deployment account, no group membership, no
sudoers rule, no authorized-keys file, no application directory, and no registration behind, and SHALL leave
the existing resources of the host byte-identical.

#### Scenario: A re-provision with a changed domain

- **WHEN** an administrator re-provisions an existing application with a different API domain
- **THEN** the provisioning fails with an error that the registration differs
- **AND** no account, group membership, sudoers rule, directory, or key is created or changed
- **AND** the existing key file, sudoers rule, and registration are byte-identical

#### Scenario: A re-provision with a changed runtime or image repository

- **WHEN** an administrator re-provisions an existing application with a different runtime or image repository
- **THEN** the provisioning fails with an error that the registration differs
- **AND** the host is left exactly as it was, including no new key

#### Scenario: A hostname owned by another application

- **WHEN** an administrator provisions a new application onto the API domain of a registered application
- **THEN** the provisioning fails with an error that the hostname is already registered
- **AND** no account, group membership, sudoers rule, directory, or key exists for the refused application

#### Scenario: A deployment account owned by another application

- **WHEN** an administrator provisions a new application that would reuse a registered deployment account
- **THEN** the provisioning fails before the account is created or changed

#### Scenario: An account that is not a Shiplord account

- **WHEN** the deployment account name already belongs to an account Shiplord did not create
- **THEN** the provisioning is refused before any other host resource is created

#### Scenario: An invalid request

- **WHEN** the public key, key label, runtime, domain, or image repository is invalid
- **THEN** the provisioning is refused before any command runs and before any file is written

### Requirement: An accepted provisioning still writes the registration last

A provisioning that passes every validation SHALL create its host resources as before and SHALL write the
registration last, because the registration is what enables deployment access. Repeating an accepted
provisioning SHALL change nothing and SHALL report that nothing was created.

#### Scenario: The first provisioning

- **WHEN** an administrator provisions a new application with valid settings
- **THEN** the account, group membership, key, sudoers rule, directories, and registration are created
- **AND** the registration is written after the key and the sudoers rule

#### Scenario: Repeating the same provisioning

- **WHEN** an administrator repeats a provisioning with unchanged settings
- **THEN** every resource is reported as not created
- **AND** the host is byte-identical

#### Scenario: A failure part way leaves an inert application

- **WHEN** a step after validation fails, such as a rejected sudo policy
- **THEN** no registration is written
- **AND** repeating the provisioning completes it

### Requirement: Registration validation is available without writing

The registration store SHALL offer the identity comparison, the hostname conflict check, and the deployment
account conflict check as a read-only operation that requires the host lock, reports the identical existing
registration when there is one, and uses the same error messages as a write.

#### Scenario: Checking an identical re-registration

- **WHEN** the read-only check is given the identity of an already-registered application
- **THEN** it reports that existing registration and writes nothing

#### Scenario: Checking a conflicting registration

- **WHEN** the read-only check is given a different identity, a taken hostname, or a taken deployment account
- **THEN** it fails with the same error a write would report and leaves the registry directory unchanged

#### Scenario: Checking a first registration whose account does not exist yet

- **WHEN** the read-only check is given no deployment UID because the account has not been created
- **THEN** it accepts the request when no registration exists for the application
- **AND** it refuses the request when a registration exists, because an unknown UID is never identical
