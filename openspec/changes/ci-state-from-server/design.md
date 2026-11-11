# Design

See `proposal.md` for motivation and `specs/ci-key-rotation/spec.md` for the behaviour contract.

## Context

`rotate_key` needs three things today: the application (from `ship.toml`), the repository (from `[github]` in
`ship.toml`), and the label of the key it must replace. Only the label came from local state, and that state lived at
`~/.config/shiplord/ci/<app>/state.json`. The server already answers `keys list` with every label and fingerprint it
has registered for the application, and `github_setup` writes each key's label as `label_for(fingerprint)` — the
first eight alphanumeric characters of the key's own SHA-256 fingerprint, prefixed `ci-`. The label is therefore
derivable from the key itself, which makes the server's list a complete source of truth.

The state directory also carried two things rotation does not need but setup does: `private_key` (the retained key of
an interrupted setup) and `workflow_sha256` (the digest of the generated workflow, so an edited workflow is not
overwritten silently). Those stay local; only their location changes.

## Goals / Non-Goals

- Goal: rotation needs nothing but `ship.toml`, GitHub access, and an admin connection to the server.
- Goal: a machine with state from the earlier layout keeps working without rerunning setup.
- Non-goal: `github setup` from a second machine. Setup must own the private key to store it in GitHub, so it
  cannot adopt a key it never had; a second machine is expected to rotate, not to set up again.
- Non-goal: `github status` on a second machine. Its `ci_key` check still reads local state, because the issue is
  about rotation and `--check-server` already compares the local label with the server's list. Extending status to
  derive everything would be a separate change with its own output contract.
- Non-goal: a server-side change. The existing `keys` action already lists, adds, and removes by label.

## Decisions

### Identify the CI key by its label being the one derived from its fingerprint

A label is not a claim of ownership by itself — anybody can add a key with any label through `shiplord env keys`.
What cannot be faked is the pair: the label must equal `label_for(fingerprint)` for that same key. A key that
satisfies it was written by Shiplord, because the label is computed from the key and a human has no reason to type
one. Rotation only ever removes keys that satisfy the pair, which keeps the existing "never delete a key you cannot
identify as the CI key" property while removing the local file from the critical path.

Alternative rejected: store the label in a server-side marker. It would need a protocol and provisioning change
(shared with other work), and the fingerprint-derived label already encodes the same information.

### Remove every identifiable CI key, not just one

Earlier versions could leave two CI keys behind when revocation failed. Once the new key is proven and stored,
every other identifiable CI key is stale, so all of them are revoked. The result keeps `old_label` when exactly one
label was revoked, and always reports `old_labels` and a `steps.revoke` text naming each label. This makes rotation
self-healing instead of preserving the "two CI keys" state.

### Server-scoped directory: `ci/<server>/<app>`

`manifest["server"]` is a validated lowercase alias, so it is safe as a path segment and is the identifier a person
already uses for "which server". The alternative — hashing the resolved host — would move whenever DNS or an SSH
alias changes, and would not match the alias the user passes to other commands. Scoping only by application was the
bug.

### Migrate the old directory on first use instead of versioning the layout

`ci_directory` creates the server-scoped directory and then, only when it has no `state.json`, moves every regular
file out of the legacy `ci/<app>` directory and removes the now-empty directory. A private key left by an interrupted
setup moves with it, so the recorded absolute path is rewritten to the new location; otherwise a resumed setup would
find a missing file and generate a second key. Migration is best-effort: an unmovable legacy directory is ignored,
which costs at most one extra key, never a failed command and never a secret. The alternative — reading both
locations forever — leaves two write targets and keeps the collision alive.

The migration does not touch a symlink or a non-directory at the legacy path, and it never overwrites an existing
`state.json` in the new location.

### Keep writing local state after rotation

Rotation records `label`, `fingerprint`, `repository`, and `secret_set=True` in the server-scoped directory. This is
truthful (rotation did set the secret) and keeps `github setup` and `github status` on that machine working as
before. It is a cache of what happened, never an input to which key gets replaced.

## Risks / Trade-offs

- [A user hand-adds a key whose label happens to equal `ci-` plus eight characters of its fingerprint] → Rotation
  would treat it as a CI key. The label is derived from the key's own fingerprint, so this requires deliberately
  reproducing Shiplord's derivation; the worst case is removing a key the user deliberately disguised as the CI key,
  after a working replacement exists.
- [Two machines rotate at the same time] → Both add a key, both verify, both store a secret, and both revoke what
  they listed, possibly leaving the other's new key. This is not new (the earlier version had the same race through
  its own state), and the admin helper's host lock serialises each call, not the whole sequence. A later rotation
  removes every leftover identifiable key, so the state is recoverable.
- [Migration cannot move a file] → Setup generates a new key; the old one stays registered. No secret is exposed and
  no command fails.
- [The local cache cannot be written after the secret is stored] → The revocation still runs, because a failed cache
  write must never leave two CI keys behind. The cache is corrected by the next rotation or setup.
- [Removing all identifiable keys on a server where a person used the CI label by hand] → They lose that key. The
  result names every revoked label, so the loss is visible and the key can be re-added.

## Migration Plan

1. Deploy the client. The first `github setup`, `github status`, or `github rotate-key` for an application moves its
   legacy state into the server-scoped directory.
2. No server, protocol, or secret change is needed. Rollback means reinstalling the previous client, which reads
   `ci/<app>` and would find it empty after migration; `github setup` then registers a new key, and a rotation
   performed by the previous client refuses until setup has run. Nothing on the server is left inconsistent, and no
   secret is exposed.
