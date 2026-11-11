# Proposal

## Why

The sshd drop-in that confines deployment accounts matches `Match User ship-*`, a prefix. Any human account on the
host whose name begins with `ship-` therefore loses its interactive login, and the block does not turn off
`AuthorizedKeysCommand` or `TrustedUserCAKeys`, so on a host that uses an SSH CA (or a keys command) a deployment
account can authenticate by a path that never reaches the forced command. The forced command is the entire
authorization boundary for a deployment account, so that is a P1 hole.

## What Changes

- Match the policy on a dedicated group, `shiplord-deploy`, instead of the `ship-*` prefix. Only accounts Shiplord
  creates are members, so a human account that happens to start with `ship-` keeps the host's normal sshd policy.
- Disable CA-based authentication and `AuthorizedKeysCommand` for those accounts inside the same block, so a
  deployment account can only authenticate with a key from its own `AuthorizedKeysFile`, which carries the forced
  command.
- Create the group at bootstrap and add every existing Shiplord deployment account to it, so a host upgraded from the
  prefix policy never loses confinement for accounts that already exist.
- Add a newly created deployment account to the group when it is provisioned, before any key for it is installed, so
  an account created after bootstrap is covered. Membership is set before the key exists, so a failure leaves an
  account that cannot log in rather than one that can log in unconfined.
- Keep the existing confinement: no TTY, no forwarding, no password or keyboard-interactive authentication, and the
  existing `AuthorizedKeysFile` path.

## Capabilities

### New Capabilities

- `deploy-account-confinement`: which accounts the sshd drop-in applies to, how those accounts are identified and kept
  covered across bootstrap and provisioning, and which authentication methods are refused for them.

### Modified Capabilities

<!-- No existing capabilities; this project has no specs yet. -->

## Impact

- `src/shiplord/installer.py`: the drop-in template, group creation, and reconciliation of existing accounts at
  bootstrap.
- `src/shiplord/provision.py`: a new deployment account joins the group before its key is installed.
- `tests/test_installer.py`, `tests/test_provision.py`, `tests/test_host_integration.py`: unit coverage of the rendered
  drop-in, the group and membership, and real-host coverage of an account provisioned after bootstrap plus a host
  configured with an SSH CA.
- Host behaviour: a host that already runs Shiplord gains a system group. Bootstrap must run again before the new
  drop-in is installed; the drop-in itself is unchanged in every other respect.
