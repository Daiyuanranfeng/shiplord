# Design

## Context

See proposal.md — Why. The facts from the current code that shape the approach:

- `provision()` validates its own arguments first, then performs its side effects in this order: deployment
  account, group membership, native runtime account and directories, the application's state and environment
  directories, the release state, the authorized key, the sudoers rule, and finally the registration.
- `Registry.register()` both validates and writes. Its validation is: the record's shape
  (`validate_registration`), the identity comparison against the application's existing registration, the
  hostname conflict against every other application, the deployment-account conflict against every other
  application, and the port scan. Only the port scan and `_write` are not pure reads.
- The identity comparison compares every field except `host_port`, including `deployment_uid`. That UID is
  only known once the account exists.
- `provision()` already calls `validate_registration` with a placeholder UID before its first side effect, so
  the request's shape is already checked early; it is the identity and conflict checks that run late.
- `HostLock` is held for the whole provisioning, so no other Shiplord process can change the registry between
  the pre-check and the write. A human editing the host concurrently is re-checked by the write.

## Goals / Non-Goals

**Goals:**

- Every check that can refuse a provisioning runs before the first account, group, sudoers, directory, or key
  side effect.
- One implementation of the registration rules, shared by the read-only check and the write, so they cannot
  drift.
- The write still happens last, after the key and the sudoers rule.

**Non-Goals:**

- Changing the registration rules, the messages, the port range, or the port allocation strategy.
- Pre-checking port availability. Port exhaustion is host capacity, not a property of the request, and a
  pre-check would probe a port that the write then re-probes; the caller could still lose the race.
- Reordering the steps that follow validation (state directory, release state, key, sudoers).
- Making a provisioning roll back a step that fails after validation (for example a rejected sudo policy).
  That behaviour is unchanged and documented: the registration, which grants access, is written last.
- Changing `manage_key`, `manage_environment`, `manage_registry`, `deprovision`, the admin protocol, or the
  CLI.

## Decisions

**Split `Registry.register()` into a read-only `check()` and the write.** `register()` keeps its signature,
its host-lock requirement, its messages, and its port scan. Both it and `check()` build the prospective
record with one private helper and compare it with the registry through a second private helper; `register()`
then scans for a port and writes. The alternative — a `dry_run` flag on `register()` — makes a security check
conditional on a boolean, which is exactly the kind of branch that later gets the wrong default. A separate
method that cannot write is a property of the code rather than of its arguments.

**Let `check()` take an unknown deployment UID as `None`.** On a first provisioning the account does not exist
yet, so its UID is not known until `useradd` runs. The request's shape is still validated by substituting a
placeholder UID, and then the UID is cleared before the comparison. A cleared UID never equals an existing
registration's UID, so "a registration exists but its account does not" is refused as a difference instead of
silently re-creating the account under a new UID — the same outcome as today, one step earlier. The
alternative — requiring the UID — would force the pre-check to run after the account exists, which is the
defect. The alternative of a separate "check the fields except the UID" path would leave the identity rule
implemented twice.

**Read the accounts before the registry check, not after.** The registry comparison needs the deployment
UID, so `provision()` reads the account with `getent` first, refuses a foreign account there, and passes the
UID (or `None`) into `check()`. The read is repeated by `_ensure_user` when it creates or verifies the
account; the duplicate read is deliberate, because it keeps the pre-check independent of the code that
creates the account and costs one `getent`.

**Share the "is this our account" predicate instead of copying it.** `_ensure_user`'s marker/home/shell/UID
condition becomes `_is_ours()`, used by both `_ensure_user` and the read-only `_existing_user`. The error
message and the condition are moved verbatim, so an existing test that pins the message still passes.

**Pre-check the native runtime account too.** A foreign runtime account is refused by `_ensure_user` after
the deployment account has been created, which leaves that account behind for the same reason the issue
describes. Reading it in the pre-check costs one `getent` for a native provisioning and nothing for Docker.

**Keep the early `validate_registration` call.** It reports a malformed request as
`invalid application settings`, before the registry directory is even opened; the registry's own shape check
would report the same problem as `registration failed`. Both messages are kept as they are.

## Risks / Trade-offs

- [The pre-check reads the account, then `_ensure_user` reads it again] → Two `getent` calls on a repeat
  instead of one. The extra call is read-only and keeps the check independent of the creation path; the
  existing "a repeat changes nothing" test is updated to the new call list, and it still asserts that no file
  changed.
- [A registration whose deployment account was deleted by hand is now refused earlier] → It was already
  refused, after creating an account; the message ("registration differs") is unchanged and nothing is left
  behind. Recovering needs `deprovision` and a fresh `provision`, which is the explicit update the message
  names.
- [Port exhaustion is still discovered after the side effects] → Documented as a non-goal: it is not a
  property of the request, and probing early would not reserve the port.
- [The registry pre-check reads the whole registry, so a stray or corrupt record now fails earlier] → It
  failed before too, with the same message; now it fails before anything is created.

## Migration Plan

No migration. Nothing on disk changes; the new refusal only applies to provisionings that were going to be
refused anyway. Rollback is reverting the call order and the split method.
