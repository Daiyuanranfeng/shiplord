# Design

## Context

See proposal.md for motivation. The relevant facts from the current code:

- `load_environment_file` is the only reader. It refuses a file that is not a regular file owned by the
  installing account with mode `0600` or stricter, a symlink (opened with `O_NOFOLLOW`), an oversized file,
  invalid UTF-8, or content that is not `KEY=VALUE`. Every refusal is an `EnvironmentError_` with a fixed
  message that contains no path and no value.
- `manage_environment` currently calls it once, before it decides what to do, so both repair paths inherit
  the refusal.
- Writing already goes through a private temporary file, `fsync`, and `os.replace`; that is unchanged.
- The admin helper maps `ProvisionError` to `status: failed` with exit code 1, so a raised error is a clean
  one-line failure for the CLI, never a traceback.

## Goals / Non-Goals

**Goals:**

- `set` and `clear` succeed on any file `load_environment_file` refuses, and say so in the result.
- `keys` still refuses such a file, with the reason plus the recovery command.
- No value, and no host path, in any result or error.

**Non-Goals:**

- Reading a file that is deliberately unreadable; repair replaces or removes it, it never interprets it.
- Changing the manifest, the admin protocol, CLI rendering, or the exit-code contract.
- The registry-login and key-removal problems in `provision.py`; they belong to a separate issue.

## Decisions

**Read lazily and per operation, instead of once up front.** `clear` does not need the contents; it needs
to know whether a file is there and whether it could be read (for the notice). `set` needs the contents only
to decide "unchanged". `keys` is the only operation that genuinely requires a readable file. Splitting the
single load into those three uses is what removes the coupling.

**Tolerate the read failure only where the file is about to be replaced or removed.** A `set` that cannot
read the old file always writes, and reports `changed: true` with a warning; it does not compare against
guessed content. A `clear` unlinks regardless and reports `changed` from the filesystem, with a warning when
the file could not be read first. `keys` has no repair to offer, so it raises.

**Report repairs as `warnings`, a list of fixed sentences.** `deploy.py` already returns `warnings` for
non-fatal notes and `cli.py` already knows that key, so this reuses an existing shape instead of inventing a
flag. The ordinary (readable) path keeps the exact result it has today: `warnings` appears only on a repair,
so existing exact-equality tests stay meaningful. The sentences are constants that name no path and no value.

**`clear` decides `changed` with `path.exists() or path.is_symlink()`.** `Path.exists()` is false for a
dangling symlink, which `clear` can still remove; using `os.path.lexists` semantics keeps the report honest.
The unlink itself is wrapped so an `OSError` (for example a directory where the file should be) becomes a
`ProvisionError` rather than a traceback.

**Keep `O_NOFOLLOW` and `os.replace` as they are.** A symlinked `app.env` is replaced as a directory entry,
never written through, so a repair cannot be redirected to a file outside the application's environment
directory. `set` therefore fixes a symlink rather than following it.

## Risks / Trade-offs

- [Repairing a file silently discards values the operator may still need] -> The result carries an explicit
  warning that the contents were discarded, and the CLI prints every result field.
- [A tolerant `clear` could remove a file an operator meant to keep] -> `clear` is already an explicit
  destructive command; the change only lets it succeed where it used to fail, and it says what it removed.
- [A wider group or wrong owner can mean someone else wrote the file] -> Shiplord still refuses to interpret
  it (`keys` fails) and replaces it with a `0600` root-owned file; no unverified content is ever passed to a
  release.
- [`warnings` in the human output is printed as a JSON list] -> Acceptable: the CLI renders env results
  generically, and adding a renderer is a separate, wider change.

## Migration Plan

No migration. The behaviour only widens for files that were already unreadable; readable files behave
exactly as before. Rollback is reverting the single function.
