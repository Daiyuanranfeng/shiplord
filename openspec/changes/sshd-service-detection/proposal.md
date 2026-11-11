# Proposal

## Why

Bootstrap writes the sshd drop-in that confines deployment accounts, then reloads `ssh.service`. That unit
name exists only on Debian-derived hosts, so on a host that runs `sshd.service` the reload fails or reloads
nothing. Worse, bootstrap never checks that the main `sshd_config` actually reads the drop-in directory, so
a host whose config has no matching `Include` reports a successful bootstrap while every deployment login
is refused. That is the worst failure mode Shiplord has: it looks like an authorization bug in the helper,
not a missing configuration line.

## What Changes

- Bootstrap discovers the installed sshd unit (`ssh.service` or `sshd.service`) instead of assuming one.
- Bootstrap refuses, before writing the drop-in, when no sshd service unit can be found.
- Bootstrap refuses, before writing the drop-in, when the main `sshd_config` does not include the drop-in
  directory, and names the exact `Include` line to add.
- A socket-activated sshd (`ssh.socket`/`sshd.socket`) is not restarted when its service unit is inactive:
  the next connection starts a fresh `sshd` that reads the new configuration.
- The verification runs on every bootstrap, including a repeat that changes nothing, because the host's
  `sshd_config` can lose the `Include` line between runs.
- The deep doctor reports the discovered unit and whether the drop-in is included, and fails its `sshd`
  check when it is not.
- Error messages name the unit and the missing include fact only; no host path is included.

## Capabilities

### New Capabilities

- `ssh-access-policy`: how bootstrap installs, verifies, and reloads the sshd configuration that confines
  deployment accounts, across the unit names and activation modes Linux distributions use.

### Modified Capabilities

None; the project has no specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/installer.py`: sshd unit discovery, drop-in include verification, and the reload decision.
- `src/shiplord/serverdoctor.py`: the `sshd` check reports the unit and the include result.
- `tests/test_installer.py`, `tests/test_serverdoctor.py`: unit coverage for both unit names, socket
  activation, a missing unit, and a missing include.
- `tests/test_host_integration.py`, `tests/linux/shims.sh`: the real-host suite answers unit discovery, so
  `tests/linux/run.sh host` exercises both unit names.
- No CLI, protocol, manifest, or exit-code change: a refusal is already a `BootstrapError`, which the
  bootstrap command reports as a failed step.
