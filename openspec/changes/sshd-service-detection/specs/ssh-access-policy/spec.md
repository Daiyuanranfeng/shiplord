# Spec Delta

## Purpose

Installs, verifies, and reloads the sshd configuration that confines deployment accounts, across the unit
names and activation modes Linux distributions use.

## ADDED Requirements

### Requirement: The sshd unit is discovered, not assumed

Bootstrap SHALL discover the sshd service unit installed on the host from the set `ssh.service` and
`sshd.service`, and SHALL use the discovered unit to reload sshd. It SHALL NOT hard-code either name.

#### Scenario: A host that runs sshd.service

- **WHEN** bootstrap runs on a host whose only sshd service unit is `sshd.service`
- **THEN** the drop-in is installed and sshd is reloaded through `sshd.service`

#### Scenario: A host that runs ssh.service

- **WHEN** bootstrap runs on a host whose only sshd service unit is `ssh.service`
- **THEN** the drop-in is installed and sshd is reloaded through `ssh.service`

#### Scenario: A host with no sshd service unit

- **WHEN** neither `ssh.service` nor `sshd.service` is installed
- **THEN** bootstrap fails with an error that names both units it looked for
- **AND** the drop-in is not written

### Requirement: Bootstrap verifies the drop-in is included before writing it

Bootstrap SHALL read the main sshd configuration and confirm that an `Include` directive makes sshd read the
Shiplord drop-in file, before it writes the drop-in. The check SHALL follow `Include` directives recursively
and SHALL resolve relative patterns the way sshd does.

#### Scenario: The drop-in directory is included

- **WHEN** the main sshd configuration contains `Include sshd_config.d/*.conf`
- **AND** the Shiplord drop-in lives in that directory
- **THEN** bootstrap installs the drop-in

#### Scenario: The drop-in directory is included through another file

- **WHEN** the main sshd configuration includes a file that in turn includes the drop-in directory
- **THEN** bootstrap installs the drop-in

#### Scenario: The drop-in directory is not included

- **WHEN** the main sshd configuration has no `Include` that reaches the Shiplord drop-in
- **THEN** bootstrap fails with an error that says the drop-in is not included and names the exact
  `Include` line to add
- **AND** the drop-in is not written

#### Scenario: The include is removed after a successful bootstrap

- **WHEN** the drop-in is already installed and current
- **AND** the main sshd configuration no longer includes the drop-in directory
- **THEN** the next bootstrap fails with the same error instead of reporting that nothing changed

### Requirement: A socket-activated sshd is reloaded correctly

When a socket unit activates sshd, bootstrap SHALL NOT restart the sshd service unit while that service is
inactive, because the next connection starts a fresh sshd that reads the new configuration. When the service
is active, bootstrap SHALL reload it through the discovered unit.

#### Scenario: Socket active, service inactive

- **WHEN** `ssh.socket` is active and `ssh.service` is not running
- **THEN** bootstrap installs the drop-in without restarting the service
- **AND** the bootstrap reports that the drop-in changed

#### Scenario: Socket active, service running

- **WHEN** both the socket and the service unit are active
- **THEN** bootstrap reloads the service through the discovered unit

### Requirement: The doctor reports whether the sshd policy is in force

The deep doctor SHALL report the discovered sshd unit and whether the drop-in is included by the main sshd
configuration. Its `sshd` check SHALL fail when the drop-in is not included, even if the drop-in file is
current and sshd accepts the configuration.

#### Scenario: A healthy host

- **WHEN** the drop-in is current, sshd accepts the configuration, and the drop-in is included
- **THEN** the `sshd` check passes and reports the discovered unit

#### Scenario: The drop-in is current but not included

- **WHEN** the drop-in file matches what Shiplord writes but the main configuration does not include it
- **THEN** the `sshd` check fails and reports that the drop-in is not included

### Requirement: Refusals never expose host paths

A bootstrap or doctor error about sshd SHALL name the unit and the missing-include fact only. It SHALL NOT
contain an absolute host path, a secret, or an environment value.

#### Scenario: A missing include is reported

- **WHEN** bootstrap refuses a host whose configuration does not include the drop-in
- **THEN** the message names the unit or the include fact and contains no absolute path
