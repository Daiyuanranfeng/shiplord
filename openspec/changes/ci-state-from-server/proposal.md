# Proposal

## Why

`shiplord github rotate-key` reads the label of the key it has to replace from `~/.config/shiplord/ci/<app>/state.json`,
which exists only on the machine that ran `github setup`. From a second machine the command refuses with "no completed
CI setup was found", even though the server's authorized-keys list already says which key belongs to the application.
The v1.0 goal explicitly includes running `rotate-key` from a second machine, and the fix also has to stop the state
directory from colliding when the same application name is deployed to two servers.

## What Changes

- `rotate-key` derives the keys to replace from the server's authorized-keys list: a key is the CI key when its label
  is the one Shiplord derives from that key's own fingerprint (`ci-<8 hex>`). No local file is required.
- `rotate-key` refuses before creating anything when the server has no such key, so the command cannot invent a
  replacement for an application that was never set up.
- Local state that remains (the resumable private key, the generated-workflow digest, the last known label) is keyed
  by server alias and application: `ci/<server>/<app>` instead of `ci/<app>`.
- State written by an earlier version at `ci/<app>` is migrated on first use, including the recorded path of a private
  key left by an interrupted setup, so nobody has to rerun `github setup`.
- Rotation still adds the new key, proves it over SSH with the pinned host keys, stores it in GitHub, and only then
  removes the old key. When more than one identifiable CI key is left over from an earlier failed revocation, all of
  them are removed and the result names every label it revoked.

## Capabilities

### New Capabilities

- `ci-key-rotation`: what replaces an application's CI key, where the key to replace is discovered, how the rotation
  proves the new key before the old one is removed, and how the CI client state directory is keyed.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/github.py`: `ci_directory`, a small migration helper, and `rotate_key`; `github_setup` and
  `github_status` pass the server alias to `ci_directory` and otherwise behave as before.
- `tests/test_github.py`: rotation from an empty config directory, server-scoped directories, legacy migration, and
  multiple leftover keys.
- No protocol, admin-helper, manifest, or exit-code change: rotation keeps using the existing `keys` action with the
  existing `list`/`add`/`remove` operations.
- The on-disk layout of the client state directory changes; the old layout keeps working through migration.
