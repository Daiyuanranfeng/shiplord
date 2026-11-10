# Design

## Where the decision lives

`Registry.list` is the single place that enumerates the directory, so it is the only place that has to distinguish a
registration from noise. Every caller — `register`, `_check_caller`, `application_inventory`, `server_doctor` —
goes through it, so fixing it once fixes all of them.

## Skipping versus failing closed

The existing docstring promises to "fail closed on anything unexpected". That promise is right for a `.json` record:
a corrupt, group-writable, symlinked, or misnamed record could mean a registration was tampered with, and treating it
as absent would silently widen access. It is wrong for a file that cannot be a registration at all.

The rule implemented:

- `.tmp-*` — already skipped; these are the in-progress writes of `_write`, not user files.
- any other name not ending in `.json` — skipped, and recorded in `self.stray`.
- `*.json` — read and validated exactly as before. `ManifestError` from an invalid application name is wrapped as
  `RegistryError` so callers keep seeing one `StateError` family.
- a `.json` record that disappears between `listdir` and `read` — still an error, because the directory changed under
  a held lock.

## Reporting

`Registry.stray` is reset at the start of every `list()` call so it always describes the last enumeration; a stale
list would report a file that has since been deleted. `application_inventory` copies it into the result as
`ignored_files`, and the CLI prints it once per server. The field is additive, so a client that predates it is
unaffected.

## Why not a warning channel

A general warning channel would be a larger change and would invite unrelated uses. One field on one result is
enough for the one case that exists, and it keeps the diff reviewable.
