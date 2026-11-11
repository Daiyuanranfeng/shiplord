# Spec Delta

## Purpose

Settles a main-Caddyfile change that a crash interrupted, so a leftover backup file does not block every later
bootstrap, and refuses the cases where the files cannot be compared.

## ADDED Requirements

### Requirement: A leftover prior is dropped when the live Caddyfile proves the change finished

When `.prior-Caddyfile` is present and the live Caddyfile is exactly that prior with the managed import appended,
Shiplord SHALL remove the prior and report the import as already present, without rewriting the live file.

#### Scenario: The publish finished and the prior was not removed

- **WHEN** `.prior-Caddyfile` holds the Caddyfile as it was before the change
- **AND** the live Caddyfile is that content with the managed import appended once
- **THEN** the prior is removed
- **AND** the change reports that the import was already present
- **AND** the live Caddyfile is not modified

### Requirement: A leftover prior is dropped when the publish never happened

When `.prior-Caddyfile` is present, the live Caddyfile is byte-identical to it, and neither imports the managed
routes, Shiplord SHALL remove the prior and continue the normal append path, including its validation and reload.

#### Scenario: The crash happened between the link and the publish

- **WHEN** `.prior-Caddyfile` is a hard link to a live Caddyfile that does not import the managed routes
- **THEN** the prior is removed
- **AND** the managed import is appended, validated, and reloaded exactly as on a first bootstrap
- **AND** no prior is left behind afterwards

### Requirement: A prior that cannot be proven redundant is refused

When `.prior-Caddyfile` is present and the live Caddyfile matches neither state above, Shiplord SHALL refuse the
change with an intervention-required interruption, leave both files as they are, and name `.prior-Caddyfile` and
the step that unblocks the host. It SHALL NOT rewrite the live Caddyfile from the prior.

#### Scenario: The live Caddyfile is missing

- **WHEN** `.prior-Caddyfile` is present and the main Caddyfile does not exist
- **THEN** the change is refused with an intervention-required interruption

#### Scenario: The prior is not a trusted regular file

- **WHEN** `.prior-Caddyfile` is a symbolic link, a directory, or is owned or writable by another account
- **THEN** the change is refused with an intervention-required interruption

#### Scenario: The prior already imports the managed routes

- **WHEN** `.prior-Caddyfile` itself imports the managed routes
- **THEN** the change is refused with an intervention-required interruption

#### Scenario: The live Caddyfile is a different file

- **WHEN** the live Caddyfile differs from the prior by anything other than the appended managed import
- **THEN** the change is refused with an intervention-required interruption

#### Scenario: The refusal names the file and the recovery step

- **WHEN** any refusal above is reported
- **THEN** its message names `.prior-Caddyfile`
- **AND** it states that the two files must be compared, one kept, and the prior removed before retrying
- **AND** it contains no other host path
