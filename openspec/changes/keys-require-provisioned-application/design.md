# Design

## Context

See proposal.md for motivation. The relevant facts from the current code:

- `manage_key` derives everything it needs from the application name: the `KeyStore` path is
  `layout.authorized_keys / deployment_user(app)`. `KeyStore.__init__` validates the name and computes that
  path but touches no file; only `add`, `remove`, and `delete` write or unlink, and `_write` creates its
  temporary file inside the existing `authorized_keys` directory. So nothing is created until an operation
  is accepted.
- `manage_environment` already refuses an application that is not provisioned: it checks that
  `layout.environments / app` is a directory and not a symlink, and raises
  `ProvisionError("the application is not provisioned")`. `provision` creates that directory for every
  application it provisions, before it writes any key.
- `manage_registry` has a different guard, `layout.registrations / f"{app}.json"`, because it manages a
  per-application docker config directory. The registry-login path is out of scope here.
- The admin helper maps `ProvisionError` to `status: failed` with exit code 1, so a raised error is a clean
  one-line failure for the client, never a traceback.

## Goals / Non-Goals

**Goals:**

- `add`, `list`, and `remove` all refuse an unprovisioned application, before anything is created.
- One definition of "provisioned" in this module, used by both `manage_key` and `manage_environment`.
- The error message stays a fixed sentence with no host path and no key material.

**Non-Goals:**

- Changing the `manage_registry` guard, the key file format, the protocol, or the CLI.
- Making an unprovisioned application provisionable, or auto-creating its directory.
- Changing any accepted behaviour for a provisioned application.

## Decisions

**Extract the existing check instead of writing a second one.** The environment path's condition becomes a
module-level helper, `_require_provisioned(layout, application_id)`, and both callers use it. The helper
keeps the exact condition, the exact message, and the exact `ProvisionError` type, so the environment path
behaves identically. An alternative was to copy the two lines into `manage_key`; that leaves two copies of a
security check to drift apart, and the issue exists because one caller had it and the other did not.

**Use the environment directory, not the registration file, as the marker.** The registration file is the
strongest marker, but choosing it would change `manage_environment`'s behaviour (it would start refusing an
application that has an environment directory but no registration yet) and would introduce a competing
notion of "provisioned" in a module that already has two. The environment directory is the check the issue
names, it is created by `provision` before the key file, and it is what the neighbouring command already
trusts.

**Check after name validation, before constructing the store.** `validate_app_name` still runs first so an
unsafe name is rejected as invalid rather than as unprovisioned. The check then runs before `KeyStore(...)`
is built, so the refusal cannot be confused with anything the store does. Nothing is created either way,
because the store creates nothing on construction, but ordering the check first makes that a property of
the code rather than of the store's current internals.

**Assert the no-side-effect property on the whole tree, not just the key file.** A refused call must not
create a file or a directory anywhere. The test compares a snapshot of every file under the layout prefix
before and after each refused operation, plus the directory listing of `layout.authorized_keys`, so a future
change that writes a temporary file or creates the key directory is caught.

## Risks / Trade-offs

- [An application with an environment directory but no registration (an interrupted provision or
  deprovision) counts as provisioned] -> The narrow window already exists for the environment path, and the
  key file `provision` writes is not removed by this change. Closing it needs one definition of provisioned
  across keys, environment, and registry, which is a separate change with its own migration; the issue's
  typo scenario is fully closed by reusing the existing check.
- [An application whose environment directory was deleted by hand is refused] -> It is refused for
  `env` today too, and re-running `provision` recreates the directory. The error names no path, so it stays
  safe to show a deployment account.
- [Extracting the helper touches `manage_environment`] -> The condition, message, and error type are moved
  verbatim; the existing environment tests pin that behaviour.

## Migration Plan

No migration. Nothing on disk changes; the new refusal only applies to names that were never provisioned.
Rollback is reverting the two call sites and the helper.
