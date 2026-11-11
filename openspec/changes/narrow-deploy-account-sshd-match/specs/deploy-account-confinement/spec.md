# Spec Delta

## Purpose

Defines which host accounts the Shiplord sshd drop-in confines to the forced-command dispatcher, how those accounts
stay covered across bootstrap and provisioning, and which authentication methods are refused for them.

## ADDED Requirements

### Requirement: The sshd policy applies only to Shiplord deployment accounts

The sshd drop-in SHALL apply its confinement to exactly the deployment accounts Shiplord creates for applications, and
SHALL NOT apply it to any other account by name pattern. An account Shiplord did not create that happens to share the
deployment name prefix SHALL keep the host's default sshd policy.

#### Scenario: A human account with the deployment prefix is not confined

- **WHEN** a host account named `ship-human` exists that Shiplord did not create
- **THEN** sshd applies the host's default policy to it, including its own `AuthorizedKeysFile` and TTY permission

#### Scenario: A provisioned deployment account is confined

- **WHEN** an application is provisioned and its deployment account exists
- **THEN** sshd applies the Shiplord policy to that account, using the Shiplord `AuthorizedKeysFile` path

### Requirement: Deployment accounts cannot authenticate with a certificate or a keys command

Inside the confined block, the drop-in SHALL disable `TrustedUserCAKeys` and `AuthorizedKeysCommand`, so a deployment
account can authenticate only with a key from its own Shiplord-managed `AuthorizedKeysFile`, whose entries carry the
forced command. This SHALL hold on a host that globally configures an SSH certificate authority or an
`AuthorizedKeysCommand`.

#### Scenario: A certificate signed by the host CA is refused

- **WHEN** the host trusts an SSH CA and a client presents a certificate for the deployment account, signed by that CA
  with the deployment account as a principal
- **THEN** sshd refuses the authentication and the client never reaches the forced command

#### Scenario: A key offered only by the host keys command is refused

- **WHEN** the host configures an `AuthorizedKeysCommand` that returns a key for the deployment account
- **THEN** sshd refuses a login with that key and does not execute the command for that account

### Requirement: An account provisioned after bootstrap is covered

An account created after the sshd policy was installed SHALL be confined from the moment it can authenticate. Its
membership SHALL be established before any authorized key for it is installed, so a failure to establish it leaves an
account that cannot log in rather than one that can log in without the forced command.

#### Scenario: A later application is confined without re-bootstrapping

- **WHEN** a host is bootstrapped and an application is provisioned afterwards
- **THEN** sshd applies the Shiplord policy to the new deployment account without another bootstrap

#### Scenario: A failure to establish coverage stops the provisioning

- **WHEN** the membership of a new deployment account cannot be established
- **THEN** provisioning fails, reports the step, and installs no authorized key and no registration for it

### Requirement: Existing deployment accounts stay covered across an upgrade

Bootstrap SHALL bring every deployment account that already exists on the host under the current policy, so replacing
an earlier policy cannot silently drop an account back to the host's default sshd policy.

#### Scenario: Bootstrap covers accounts created under an earlier policy

- **WHEN** bootstrap runs on a host whose existing deployment accounts were created before the current policy
- **THEN** those accounts are confined after bootstrap and the command reports that it changed the host

### Requirement: The confinement is not weakened

The confined block SHALL continue to disable TTY allocation, TCP, stream-local, and agent forwarding, X11 forwarding,
tunnelling, and password and keyboard-interactive authentication, and SHALL keep the Shiplord key file path and the
forced-command requirement on the keys it authorizes.

#### Scenario: The existing restrictions remain in effect

- **WHEN** sshd reports the effective configuration for a confined deployment account
- **THEN** TTY allocation, forwarding, tunnelling, password authentication, and keyboard-interactive authentication are
  all disabled and the key file path is Shiplord's
