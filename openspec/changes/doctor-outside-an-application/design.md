# Design

## Context

See `proposal.md` - Why. Three facts shape the approach:

- The `doctor` command has four paths: `--local`, `--server` (protocol probe), `--server --deep` (admin health
  report), and the default application path. Only the default path loads a manifest, and it does so before anything
  else, so a missing `ship.toml` ends the command with exit 2.
- The local checks are three `shutil.which` lookups in `cli.py` that build `{"ok", "scope", "checks", "client"}` and
  exit 3 when a tool is missing. `scope` already says "local prerequisites only; server readiness was not checked".
- `_render_doctor` renders `client` and `checks` for every doctor target; it does not print `scope`, and it is shared
  with the application and server paths.

## Goals / Non-Goals

**Goals:**

- Make the default path useful with no manifest, without contacting anything new.
- Keep one definition of the local result so the fallback and `--local` cannot drift.
- Say what was not checked and what to run next, in human output.
- Leave `--json` byte-compatible in shape with today's local result.

**Non-Goals:**

- Searching parent directories for a manifest, or accepting a manifest path argument.
- Tailoring the next-step hint to the locally registered servers (that would make a diagnostic depend on server
  configuration being readable).
- Changing `--local`, `--server`, or `--server --deep`.
- Documenting the fallback in `docs/guide.md`; that file is outside this change's edit scope.

## Decisions

**Decide "no manifest" by file presence, not by the error message.** The default path checks
`Path("ship.toml").exists() or Path("ship.toml").is_symlink()` before calling `_manifest`, mirroring the guard
`configure` already uses before it refuses to overwrite. A directory named `ship.toml` or a link that does not resolve
therefore still reaches `load_manifest` and reports a manifest error (a dangling link reports "was not found") instead
of being silently treated as absent. The alternative -
catching `ManifestError` and matching "not found" as `_app_context` does for `logs` - was rejected: it couples the
fallback to error wording and would swallow any future error that happens to contain those words.

**Emit the local result itself, not a fallback-specific variant.** Both paths build the result through one small
helper in `cli.py`, so the fallback's `--json` object is exactly the local object: `ok`, `scope`, `checks`, `client`.
Adding a `next` or `reason` key was rejected because the issue requires the JSON shape to stay as it is, and callers
already read `scope` to learn the checks were local-only. The scope string is the machine-readable statement that
server readiness was not checked.

**Print the next command in human output only, after the shared renderer.** `_render_doctor` is used by every doctor
target and does not print `scope`; teaching it about fallback state would change the application and server reports.
Instead the fallback echoes two lines after `_render_doctor`: one saying server readiness was not checked because the
directory has no `ship.toml`, and one naming `shiplord configure` and `shiplord doctor --server <alias>`. The hint is
kept option-free so it cannot go stale as `configure` gains flags.

**Keep exit codes as they are.** The fallback ends with the local path's code: 0 when every tool is present, 3 when one
is missing. Exit 2 stays reserved for a manifest that exists but is invalid, which still fails before any check runs.

**No new `commands.py` helper.** The work is argument dispatch and presentation in `cli.py`; `commands.py` holds
command bodies that talk to a server or the filesystem, and nothing here does either. The only extraction is a
`cli.py`-local function for the local result.

## Risks / Trade-offs

- [A script that relied on exit 2 with no manifest now sees 0 or 3] -> Accepted and specified; 2 means "invalid
  configuration", and 0/3 match what `--local` has always returned for the same checks.
- [A user in a subdirectory of an application now gets local checks instead of an error] -> The next-step line names
  `shiplord configure` and `shiplord doctor --server <alias>`; running `doctor` in the application directory was and
  remains the way to get the full report.
- [The next-step hint could drift from the real setup flow] -> It names commands without options, and the setup flow
  itself (`README`, `docs/guide.md`) is unchanged by this change.
- [The fallback's `scope` is identical to `--local`, so JSON consumers cannot tell the two apart] -> They do not need
  to: both report the same checks with the same limits, and the human output explains the fallback.

## Migration Plan

None required: no protocol, manifest, exit-code, or argument change, and no state to clean up. Rolling back restores
the previous "ship.toml was not found" exit 2.
