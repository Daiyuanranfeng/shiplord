# Design

## Context

See `proposal.md` - Why. The relevant current state:

- `installer.bootstrap` writes `/etc/ssh/sshd_config.d/50-shiplord.conf` and reloads sshd, once per host, before any
  application exists. `installer.SSHD_DROPIN` is also compared verbatim by `serverdoctor` to report a stale drop-in.
- `provision.provision` creates the deployment account with `useradd --system --user-group`, then installs the
  application's keys, its sudo policy, and its registration, in that order, holding the host lock.
- `deprovision` verifies the account's passwd comment against `shiplord-deploy-<app>` before removing it, so the
  comment is already the durable mark of an account Shiplord owns.
- Deployment accounts are `ship-<app>`; `layout.deployment_user` owns that name.

## Goals / Non-Goals

**Goals:**

- The policy targets exactly the accounts Shiplord created, without listing names in the drop-in.
- An account created after bootstrap is confined, with no second bootstrap and no window in which it can authenticate
  outside the policy.
- A host upgraded from the prefix policy keeps every existing deployment account confined.
- `sshd -t` accepts the drop-in on a stock Ubuntu host and on a host configured with an SSH CA.

**Non-Goals:**

- Changing the forced command, the key file path, or the helper's authorization checks.
- Managing the host's global `TrustedUserCAKeys` or `AuthorizedKeysCommand`; only the confined accounts are affected.
- Removing the group when the last application is deprovisioned. It is a system group, it holds no access by itself,
  and leaving it keeps bootstrap and provisioning idempotent.

## Decisions

### Match on a dedicated group, not on a rendered account list

`Match Group shiplord-deploy` is the criterion. The group is root-controlled, exists before any account, and covers
an account the moment provisioning adds it, so no drop-in rewrite is needed when applications come and go.

Alternatives:

- *Render the exact `Match User a,b,c` list at bootstrap.* Rejected: bootstrap runs before accounts exist, so the
  list is empty on a fresh host, and every later `provision` would have to rewrite the drop-in and reload sshd while
  creating an account. A failure between creating the account and reloading leaves an account under the default
  policy - the regression this change exists to prevent.
- *Keep `Match User ship-*` and add exclusions.* Rejected: sshd has no negative `Match User` list, and the prefix is
  exactly the ambiguity being removed.

The group name is a module constant in `installer.py`, next to the policy that depends on it; `provision.py` imports it
so the two cannot drift.

### Membership is set before the account's key exists

`provision` adds the account to the group immediately after `useradd`/verification and before `KeyStore.add`. If the
membership step fails, provisioning fails with the account still keyless: the account cannot authenticate at all,
which fails closed. Setting membership after the key is installed would open the window the fix is meant to close.

### Bootstrap reconciles accounts by their passwd comment

Bootstrap reads `getent passwd`, selects entries whose comment starts with `shiplord-deploy-`, and adds them to the
group when they are not members. The comment is the same mark `deprovision` already trusts, so bootstrap covers
accounts even if a registration is unreadable, and it does not adopt a foreign account that merely shares a name. It
also never adds a runtime account (`shiplord-runtime-*`), which must not be confined.

Order inside bootstrap: create the group and reconcile membership first, then write and validate the drop-in, then
reload. sshd resolves group membership at connection time, so no reload is needed for a membership change, but doing
the membership work before the reload means the new policy is never live while an existing account is uncovered.

### Disable CA authentication and the keys command inside the block

`TrustedUserCAKeys none` and `AuthorizedKeysCommand none` are both legal in a `Match` block and are the documented way
to clear those global settings for the matched accounts. OpenSSH resolves them per connection: `TrustedUserCAKeys`
names a file that is read when a certificate is presented, so `none` makes every certificate untrusted for those
accounts, and `AuthorizedKeysCommand` set to `none` stops sshd from executing the host's command for them. Neither
requires the named path to exist, so `sshd -t` still passes.

## Risks / Trade-offs

- [A host upgraded without re-running bootstrap keeps the old drop-in] → The old drop-in still confines `ship-*`, so
  behaviour is unchanged until the operator upgrades; `shiplord doctor` reports the stale drop-in, and bootstrap is
  the documented upgrade step.
- [`Match Group` needs the account's groups from NSS] → Deployment accounts are local system accounts in the local
  group database; sshd resolves them at connection time like any other local account.
- [A human account added to `shiplord-deploy` by an administrator becomes confined] → Fail-safe direction, and the
  group is root-controlled; only Shiplord adds members.
- [Bootstrap now runs `getent passwd` and possibly `usermod`] → Both are read-mostly and idempotent; a repeated
  bootstrap reports no change, which the existing idempotency test enforces.
- [The `AuthorizedKeysCommand` is still attempted as a program named `none` on some OpenSSH versions] → It fails to
  execute, so no key is returned; the login is refused either way.

## Migration Plan

1. Upgrade the helper and run `shiplord server bootstrap`. Bootstrap creates `shiplord-deploy`, adds existing
   `shiplord-deploy-*` accounts, writes the new drop-in, runs `sshd -t`, and reloads.
2. Rollback: restore the previous helper version and run bootstrap again; the drop-in reverts with it. The group and
   its members are inert without the drop-in.
