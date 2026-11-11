# Design

## Context

See `proposal.md` - Why. The two files live on the server side and must stay stdlib-only. `release_state.py`
already owns the trusted-directory check (`_directory`) and the write pattern used by the registry and jobs stores:
open a temporary file with `O_EXCL | O_NOFOLLOW`, sync it, publish it, sync the directory, and remove the temporary
file when anything fails. `registry.py` already imports `_directory`, `_read`, and `_write` from `release_state.py`,
so reusing the check in `keys.py` follows an established pattern rather than adding a new one.

`provision.py`'s registry login path currently keeps the decoded document as a raw mapping of registry to entry.
That matters: `registry login` for one host must preserve other entries, including their tokens, so the validated
representation has to keep the original `auth` strings, not a username-only map.

## Goals / Non-Goals

**Goals:**

- Validate the login document before any mutation, so a malformed file is never rewritten and no traceback escapes.
- Make the token's temporary file disappear on every failure path and sync the login directory on success.
- Give the key store the same descriptor-relative, symlink-refusing, directory-syncing discipline as
  `release_state.py`.

**Non-Goals:**

- Changing the deprovision path, the environment file path, or the admin protocol.
- Supporting registry hosts other than `ghcr.io`.
- Reworking `release_state.py` or exposing new public helpers from it.

## Decisions

- **Validate at read time, not at summary time.** A single `_registry_auths(path)` reader parses the document and
  validates every entry, then returns the raw `auths` mapping. Login mutates that mapping and writes it. If
  validation happened only in `summary()`, a login would publish a rewritten file and then fail on an unrelated
  malformed entry, leaving the file changed by a command that reported failure.
- **Keep raw entries.** The reader returns `dict[str, dict]` and validates each entry with a separate
  `_registry_username(entry)` decoder. A username map would lose the other registries' tokens on the next rewrite.
- **Name the file, never the value or the path.** Errors say `config.json` and a category such as "an auth value is
  not valid base64". The absolute path is a host path, and `AGENTS.md` forbids host paths in messages; the token is
  the secret itself. Registry names are not echoed either, because they are untrusted text from a hand-edited file.
- **`finally` cleanup with a `created` flag.** The temporary file is unlinked in `finally` only while it has not
  been published, mirroring `release_state._write`. Cleanup failure is swallowed in the registry path so it cannot
  mask the real error, and surfaced in the key path where a leftover file is a directory-consistency problem.
- **One directory descriptor per key operation.** `KeyStore` gains a context manager that opens the directory with
  `O_DIRECTORY | O_NOFOLLOW`, runs `_directory` on it, and closes it. `_read`, `_write`, and `delete` take that
  descriptor and use `dir_fd=` for `os.open`, `os.replace`, and `os.unlink`. `list`, `remove`, and `delete` tolerate
  a missing directory so their existing idempotency is preserved.
- **Sync file, then publish, then sync directory.** A replace is only durable once the directory entry is synced.
  `remove` goes through `_write`; `delete` unlinks and syncs. A directory sync failure is reported, not ignored.

## Risks / Trade-offs

- **The key directory check is stricter than before.** `_directory` rejects a group- or world-writable directory.
  The installer creates `authorized_keys` as `0o755`, which passes. A site that widened it to `0o775` would now get
  an error instead of silently using it; that is the intended fail-closed behavior.
- **`os.fsync` on a directory is filesystem-dependent.** On Linux ext4 and overlayfs it succeeds; the host suite
  exercises the real call. Treating a failure as an error is the honest choice, because the alternative is to claim
  durability that was not obtained.
- **The failure tests patch `os.replace` and `json.dump`.** That is implementation-coupled, but the assertion is
  behavioral: after the failure the login directory is empty. The host suite covers the real filesystem path.
- **Error messages are less specific about which entry is bad.** Not echoing registry names avoids injecting
  untrusted text into a message. Only `ghcr.io` is accepted, so ambiguity is small in practice.
