# Proposal

## Why

Three secret-writing paths fail the rules in `AGENTS.md`. A failed `registry login` can leave a temporary
`config.json` containing the registry token on disk, a hand-edited `config.json` raises a `KeyError`, `TypeError`, or
`binascii.Error` traceback instead of an error, and the key store removes and rewrites files with plain path
operations that neither use a directory descriptor nor sync the directory. A token left in a predictable root-owned
directory is a credential exposure, and an unsynced write can be lost by a crash after the command reported success.

## What Changes

- The registry login file is decoded through one validated reader. A malformed document, a non-object `auths`
  field, an entry without a string `auth`, an `auth` value that is not base64, not UTF-8, or not `username:token`
  is reported as a clear error that names `config.json` and the problem, never a traceback and never a value.
- `registry login` and `registry logout` write and remove `config.json` through a directory descriptor, sync the
  directory after the change, and remove the temporary file on every failure path, so no token outlives the command.
- `KeyStore` opens its directory once, validates it with the `release_state` directory check, and performs every
  read, replace, and unlink relative to that descriptor with `O_NOFOLLOW`. Writes and removals sync the directory.
- A symlinked key file or key directory is refused or unlinked as a link, never followed.

## Capabilities

### New Capabilities

- `registry-login`: how one application's read-only registry credentials are stored, listed, and removed, including
  what happens to a file a person edited by hand.
- `deployment-keys`: how an application's `authorized_keys` file is written and removed safely.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/provision.py` (the registry login path only; deprovision is untouched), `src/shiplord/keys.py`, and
  their tests in `tests/test_provision.py` and `tests/test_keys.py`.
- No protocol or command-line change. Error messages name `config.json` without a host path and never contain a
  token.
- The key store now imports the directory check from `release_state.py`, which is stdlib-only like the rest of the
  server modules.
