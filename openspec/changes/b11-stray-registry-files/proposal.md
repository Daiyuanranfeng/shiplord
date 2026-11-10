# Proposal

## Why

`Registry.list` in `src/shiplord/registry.py` raises `RegistryError("unexpected file in the registry")` for any
filename in `/etc/shiplord/apps` that does not end in `.json`. A single stray file — an editor backup such as
`orders.json~`, a swap file, a `.txt` left by a person — therefore takes out `apps`, `doctor`, `status`, and every
deploy at once. The cause is invisible: the operator sees "application registry is unreadable" and has no reason to
suspect a backup file.

## What Changes

- `Registry.list` skips names that cannot be registrations and records them in `Registry.stray` instead of raising.
- A `.json` record that is corrupt, unsafe, or not a valid application name still fails closed. Only noise is
  skipped, never a registration.
- `application_inventory` reports the skipped names as `ignored_files` in the `apps` result.
- `shiplord apps` prints the skipped names once, in both the text and `--json` output.
- `register` no longer fails because of a stray file, so a new application can still be provisioned.

## Capabilities

### New Capabilities

- `registry-inventory`: what the registration directory is allowed to contain, and how a file that is not a
  registration is reported rather than fatal.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/registry.py`, `src/shiplord/serverdoctor.py`, `src/shiplord/cli.py`, and their tests.
- No protocol change: `ignored_files` is an additive field on the `apps` result, which older clients ignore.
- No exit-code change. A registry with only stray noise now exits 0 instead of 3.
