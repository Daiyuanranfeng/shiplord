# Design

## Context

`RouteManager` writes every file through one helper, `_create`, which opens with `O_WRONLY | O_CREAT | O_EXCL |
O_NOFOLLOW` and a fixed mode of `0o644`; the umask then decides what actually lands on disk. It is used for three
different kinds of file:

- a managed route file in `<root>/ship`, which holds a hostname and a loopback port;
- a temporary copy of the main Caddyfile written so Caddy can validate a candidate, in `<root>`;
- the temporary file that is renamed over the main Caddyfile, in `<root>`.

The main Caddyfile is read with `_read_file`, which already opens through a directory descriptor with
`O_NOFOLLOW`, checks that the result is a regular file owned by the effective user and not group- or
other-writable, and reads it. Nothing about that descriptor survives the call today, so the writer has no way to
know what owner or mode to reproduce.

See proposal.md for why this matters.

## Goals / Non-Goals

**Goals:**

- The main Caddyfile's owner, group, and permission bits survive a rewrite, including a `0640` or `0600` mode.
- The identity used for the rewrite comes from the descriptor that was inspected, not from a second lookup.
- A rewrite that cannot reproduce the owner fails before the live file changes.
- Route file permissions stop depending on the helper's umask.
- Temporary copies of the Caddyfile are never more readable than the Caddyfile.

**Non-Goals:**

- Changing the modes of the Caddy root and `ship` directories; `installer` and `_directory` already own that.
- A general atomic-write helper shared with `release_state` or `installer`. See the decision below.
- Preserving set-id or sticky bits. A Caddyfile never legitimately needs them, and carrying them would let a
  misconfiguration survive a rewrite.

## Decisions

### Read once, and carry the identity with the bytes

`_read_file` returns a small frozen record (`data`, `mode`, `uid`, `gid`) taken from `os.fstat` on the same
descriptor it read, instead of bare bytes. Every caller already has to check for `None` and now reads `.data`.

Alternative considered: `os.stat(name, dir_fd=..., follow_symlinks=False)` inside the writer. Rejected: the
Caddyfile could be replaced between the read and the stat, so the writer would copy the mode of a different file —
exactly the swap the directory-descriptor reads exist to prevent. Alternative considered: `shutil.copystat`.
Rejected: it takes paths, follows symlinks, and has no `dir_fd`.

Only the low nine permission bits are carried (`stat.S_IMODE(...) & 0o777`). The trusted-file check already
rejects group- or other-writable files, so a carried mode is always one of `0644`, `0640`, `0604`, `0600`,
`0440`, `0400`, and so on — never a mode that would let another account modify the configuration.

### Create private, then set owner and mode, then write

`_create` opens the new file `0600`, calls `os.fchown` and `os.fchmod` on the descriptor, and only then writes and
fsyncs the content. This mirrors `installer._write_atomic`, and it means the file never exists with content at a
wider mode than its final one. `fchmod` is required even when `os.open` was given a mode, because the umask masks
that argument; `fchmod` sets the exact bits.

`preserve` is an optional parameter. When it is absent the file is a route file and gets `ROUTE_FILE_MODE`; when it
is present the file is a copy of the Caddyfile and gets the source's owner, group, and mode.

### Refuse rather than fall back

If `fchown` or `fchmod` fails, the new file is removed and the operation fails with a `precondition` error. The
caller has not published anything yet — `ensure_import` validates the candidate first and only then links the
backup and renames — so the old Caddyfile stays live with its original identity. Falling back to `0644` would turn
a permissions problem into the credential exposure this change exists to prevent.

The error message names no path, no owner, and no mode.

### `0644` for route files

A route file contains `domain { reverse_proxy 127.0.0.1:<port> }` and nothing secret. `0644` is what the installer
already uses for the `00-shiplord.caddy` placeholder, so the two writers agree, and it stays readable if Caddy is
ever run as a dedicated account instead of root. `0600` was considered and rejected: it would break that
non-root case without protecting anything, since the file has no credential in it.

Because the mode is now explicit, a route file that someone had manually tightened to `0600` is widened to `0644`
on its next change. That is the documented behaviour of "one explicit mode": the alternative — remembering the
previous mode per route — would let a stale permission outlive the reason for it and makes the result depend on
history rather than on policy.

### No helper in `release_state`

`release_state._write` writes JSON state at a fixed `0600` and validates the record it replaces by re-reading it; it
never copies an existing file's identity, and it has no `preserve` concept. Adding one there would put routing's
requirement into a module that does not share it. The helpers that do apply — `_directory` and `HostLock` — are
already imported and reused.

## Risks / Trade-offs

- **A preserved mode that Caddy cannot read.** → Caddy is started and reloaded by root through systemd, and the old
  file already had that mode, so the rewrite cannot make readability worse than it was.
- **Owner preservation is usually a no-op today.** → The trusted-file check already requires the Caddyfile to be
  owned by the effective user, so `fchown` normally re-applies the same owner. It is not redundant: a setgid Caddy
  root gives new files the directory's group, and the check does not constrain the group. It also keeps the writer
  correct if that check is ever relaxed.
- **A failed `fchown` on a host where the Caddyfile's group is one the helper does not belong to.** → The change
  fails with `precondition` and the old file stays. That is the intended outcome; the operator sees a refused
  change instead of a silently widened file.
- **The validation copy in `<root>` is now written at the Caddyfile's mode.** → If the Caddyfile is `0600` and
  Caddy is asked to validate as a non-root account, validation could fail where it previously passed. In practice
  validation runs as the same account that reads the file, and failing closed is preferable to writing a
  world-readable copy of an ACME token.

## Migration Plan

Nothing to migrate. The next route change or `shiplord server bootstrap` rewrites the Caddyfile with its current
identity, which is a no-op for a file that was already `0644`. Rolling back is a code revert; files on disk are
already in a valid state either way.
