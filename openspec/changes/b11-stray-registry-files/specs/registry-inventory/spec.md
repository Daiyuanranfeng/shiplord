# Spec Delta

## Purpose

Defines what the registration directory is allowed to contain, what happens to a file that is not a registration,
and what is still treated as a fatal inconsistency.

## ADDED Requirements

### Requirement: A file that cannot be a registration is skipped and reported

The registry SHALL skip any entry whose name does not end in `.json` (other than the `.tmp-` files it writes
itself), and SHALL report the skipped names rather than failing.

#### Scenario: An editor backup sits next to a registration

- **GIVEN** a registration directory containing `orders.json` and `orders.json~`
- **WHEN** a client lists applications
- **THEN** `orders` is listed
- **AND** `orders.json~` is reported in `ignored_files`
- **AND** the command succeeds

#### Scenario: A stray file does not block provisioning

- **GIVEN** a registration directory containing a stray file
- **WHEN** a new application is registered
- **THEN** the registration is created
- **AND** the stray file is still reported, not deleted

### Requirement: A corrupt or unsafe registration still fails closed

A file ending in `.json` that is corrupt, unsafe, or not a valid application name SHALL still be an error. Skipping
applies only to files that cannot be registrations.

#### Scenario: A corrupt record is not silently ignored

- **GIVEN** `orders.json` contains invalid JSON
- **WHEN** a client lists applications
- **THEN** the request fails with a registry error
- **AND** no application is reported as absent

#### Scenario: A record with an unusable name is not silently ignored

- **GIVEN** a file named `Not An App.json` in the registration directory
- **WHEN** a client lists applications
- **THEN** the request fails with a registry error
