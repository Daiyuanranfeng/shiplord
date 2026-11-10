# Proposal

## Why

`manage_environment` reads the current `app.env` before it can replace or remove it, so an environment
file that is unreadable (wrong mode, wrong owner, a symlink, invalid UTF-8, or an oversized file) makes
`shiplord env set` and `shiplord env clear` fail. The tool cannot repair the very state it manages, and
an operator is left with a manual root shell on the server to fix a one-command problem.

## What Changes

- `environment clear` removes the environment file even when the current file is unreadable, and reports
  that it removed it.
- `environment set` replaces an unreadable environment file with the new one, and reports that the old
  contents were discarded.
- `environment keys` keeps failing on an unreadable file, but as a clear one-line error that names the
  recovery command, never as a traceback.
- Values are never echoed, logged, or returned, and the existing atomic write (private temporary file,
  `fsync`, `os.replace`) is unchanged.

## Capabilities

### New Capabilities

- `runtime-environment`: managing an application's runtime environment file through the privileged
  helper — setting, clearing, and listing key names, including recovery from a file that cannot be read.

### Modified Capabilities

None; the project has no specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/provision.py`: `manage_environment` only.
- `tests/test_provision.py`: new coverage for the three repair paths.
- No CLI, protocol, manifest, or exit-code changes: an unreadable file for `keys` is already a
  `ProvisionError`, which the admin helper reports as `status: failed` with exit code 1.
