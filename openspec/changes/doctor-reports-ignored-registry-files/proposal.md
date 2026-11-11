# Proposal

## Why

`shiplord apps` skips a stray file in `/etc/shiplord/apps` and names it, but `shiplord doctor` builds its
applications check through a separate enumeration that drops `Registry.stray`. The one command an operator runs when
something is wrong therefore hides the file that the registry already decided to ignore, which is the exact
"ignored with no record that it exists" failure #31 was about.

## What Changes

- `server_doctor()` captures `Registry.stray` while it lists the registry, the same way `application_inventory()`
  does, and exposes the names as `checks["applications"]["ignored_files"]`.
- `shiplord doctor` prints the skipped names once in its text output, reusing the existing `apps` renderer so the
  wording and indentation match.
- A stray file does **not** change the applications check or the exit code: it is noise, not a fault. A corrupt
  `.json` registration still makes the applications check not ok.
- The `--json` result keeps every existing key; `ignored_files` is additive on the applications check.

## Capabilities

### New Capabilities

- `server-doctor`: the deep, read-only server health report — which checks it runs, what each check reports, and
  which host conditions make the report fail.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/serverdoctor.py`, the `doctor` text renderer in `src/shiplord/cli.py`, and their tests.
- No protocol change: `ignored_files` is an additive field inside the existing `doctor` result.
- No exit-code change for stray files; a corrupt registration continues to exit non-zero.
