# Proposal

## Why

`provision()` adds the SSH key and creates the deployment account, its group membership, its sudoers rule and
its environment directory before `Registry.register()` compares the request with the existing registration.
A re-provision that is then refused — because the domain, runtime, or image repository changed, or because the
hostname already belongs to another application — leaves all of that behind, and the newly added key can
deploy. The refusal is supposed to gate that access; instead it grants it and then complains.

## What Changes

- `provision()` performs every check it can read-only **before the first side effect**: the input shape, an
  existing deployment account, an existing native runtime account, and the registry's identity and conflict
  checks. A refused provisioning therefore creates no account, no group membership, no sudoers rule, no
  directory, and no key.
- `Registry` grows a read-only `check()` that runs the same validation `register()` does — the same messages,
  the same host-lock requirement — and writes nothing. `register()` keeps its current signature and
  behaviour and is implemented on the same validation, so the two cannot drift.
- `check()` accepts an unknown deployment UID for a first provision, where the account does not exist yet;
  an unknown UID is never identical to an existing registration, so a registration whose account has
  disappeared is still refused rather than silently re-created under a new UID.
- `provision()` still writes the registration last, after the key and the sudoers rule, so an interrupted
  provisioning still leaves an application that cannot deploy.
- Successful provisioning is unchanged: the same resources, the same `created` report, the same host port,
  and the same error messages. The only new work on a repeat is one read-only account lookup.

## Capabilities

### New Capabilities

- `application-provisioning`: provisioning an application on the host — which checks must pass before any
  host resource is created, what a refused provisioning leaves behind (nothing), and how the registration is
  written last so an interrupted run is inert.

### Modified Capabilities

None; the project has no archived specs yet, and the registration rules themselves (identity is immutable,
a hostname belongs to one application, a deployment account belongs to one application) are not changing.

## Impact

- `src/shiplord/registry.py`: the validation inside `register()` is split into a read-only `check()` plus
  private helpers; `register()` keeps its signature, lock requirement, messages, and port allocation.
- `src/shiplord/provision.py`: `provision()` runs the account and registry checks before its first side
  effect; the account-marker check is shared with the code that creates the account.
- `tests/test_provision.py` and `tests/test_registry.py`: new coverage that a refused re-provision and a
  hostname conflict leave the host byte-identical, and that `check()` never writes.
- `tests/test_host_integration.py`: the same two refusals against a real host, sshd, and sudo, asserting the
  account, key, sudoers rule, environment directory, and registration are all absent afterwards.
- No CLI, protocol, manifest, or exit-code changes: `ProvisionError` is already reported by the admin helper
  as `status: failed` with exit code 1.
