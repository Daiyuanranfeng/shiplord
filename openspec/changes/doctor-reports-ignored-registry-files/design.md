# Design

## Context

See proposal.md — Why.

`server_doctor()` and `application_inventory()` both enumerate `layout.registrations` with `Registry.list()`, but
through two independent copies of the same open-list-close block. Only `application_inventory()` copies
`registry.stray` into its result. The CLI has one renderer, `_render_ignored_files`, already used by `shiplord apps`
for exactly this list.

The two source files are also being edited by other open changes, so the diff has to stay small and local.

## Goals / Non-Goals

**Goals:**

- The deep health report names the same skipped files that `shiplord apps` names, in text and `--json`.
- The applications check keeps its current pass/fail meaning: only a corrupt registration or unreadable state fails.
- The change is a few lines in two files, with tests proving the old behaviour was wrong.

**Non-Goals:**

- No shared refactor of registry enumeration between the two callers. That is a larger change, it would touch the
  same lines other open changes are editing, and the two results have different shapes.
- No new warning channel, no change to `Registry`, and no protocol change.
- No attempt to remove or rename a stray file. It is reported, not managed.

## Decisions

**Capture `stray` in the existing block.** Inside the existing `try` in `server_doctor()`, hold the `Registry` in a
local, call `list()`, then copy `registry.stray`. This mirrors `application_inventory()` line for line and keeps the
same `except (OSError, StateError)` fallback: a corrupt `.json` still yields `problems = 1` and an unreadable
directory still yields the same check failure. Alternative: call `application_inventory()` from `server_doctor()` and
translate its shape. Rejected — the two entries carry different fields (`domain` vs `deployed`), so translating would
be more code and would couple the report to a second enumeration.

**Always emit `ignored_files`, even when empty.** The field lives in the check's dict literal, so `--json` consumers
can read one stable key instead of testing for its presence. `application_inventory()` only adds the key when the
list is non-empty; that asymmetry is deliberate here because the doctor result is a schema-shaped report of checks,
not a list of applications. The renderer treats an empty list as "print nothing".

**Reuse `_render_ignored_files`.** It already prints the count, the singular/plural noun, and the names joined with
commas, with no host path, and it returns early on an empty list. Reusing it is one call inside the existing
`applications` branch of `_render_doctor`, keeps the wording identical to `shiplord apps`, and avoids a second
renderer that could drift. Alternative: a doctor-specific line. Rejected — duplicate wording for the same fact.

**No exit-code change.** The report's `ok` is `all(check["ok"])`, and the applications check's `ok` stays
`problems == 0`. Since `ignored_files` is not folded into `problems`, a stray file cannot make `doctor` non-zero.
This matches the issue's recommendation: noise is reported, not fatal.

## Risks / Trade-offs

- **A client that parses the doctor result strictly** → the new key is additive inside an existing check; every
  existing key keeps its name and meaning.
- **File names are host data** → only the base names from `Registry.stray` are printed, never a full path, and the
  line is omitted entirely when the list is empty.
- **Two enumerations could drift again** → both now copy `stray`; the tests assert the doctor result directly, so a
  future change that drops it fails a test.
