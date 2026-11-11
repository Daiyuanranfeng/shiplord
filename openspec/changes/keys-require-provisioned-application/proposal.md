# Proposal

## Why

`manage_key` builds a `KeyStore` from the application name and the derived account name, and never asks
whether that application exists. A misspelled name therefore writes an authorized-keys file that nothing
reviews: it stays inert until someone provisions an application with that name, and then the typo'd key is
live. `manage_environment`, a few lines below, refuses an unprovisioned application; the key path does not.

## What Changes

- `keys add`, `keys list`, and `keys remove` refuse an application that is not provisioned, with the same
  one-line `the application is not provisioned` error the environment path already uses.
- The refusal happens before any key file or directory is created, and names no host path.
- The provisioned-application check becomes one helper that both `manage_key` and `manage_environment` call,
  so there is a single definition of "provisioned" in this module.
- Behaviour for a provisioned application is unchanged, including the refusal to remove the last key.

## Capabilities

### New Capabilities

- `application-keys`: managing an application's authorized keys through the privileged helper — adding,
  listing, and removing keys, and refusing any operation on an application that is not provisioned.

### Modified Capabilities

None; the project has no archived specs yet, and the environment behaviour this change touches is not
changing (it keeps the check it already had, now shared).

## Impact

- `src/shiplord/provision.py`: `manage_key`, plus the extracted provisioned-application helper shared with
  `manage_environment`.
- `tests/test_provision.py`: new coverage that `add`, `list`, and `remove` are refused for an unknown
  application and leave the host tree untouched.
- No CLI, protocol, manifest, or exit-code changes: a `ProvisionError` is already reported by the admin
  helper as `status: failed` with exit code 1.
