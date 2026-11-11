# Proposal

## Why

`shiplord configure` writes a Docker manifest with no image repository, so the very next command,
`shiplord provision`, stops with "the image repository is unknown: add a [github] section to ship.toml or pass
--image-repository". The first two commands a newcomer runs therefore fail in sequence, and the quick start never
mentions the flag that would have avoided it.

## What Changes

- `configure` records the image repository for a Docker application: `--image-repository owner/repository` when
  given, otherwise the repository inferred from the git `origin` remote, otherwise a question in a terminal.
- Inference reads only the remote URL, accepts only a GitHub host, and never echoes the URL (it can carry a
  credential). A remote that is missing, unreadable, or shaped unexpectedly is not an error: `configure` falls back
  to the question, or to the flag-only behaviour without a terminal.
- The recorded value is the existing `[github]` table (`repository` plus `deployment_branch = "main"`), so
  `provision` derives `ghcr.io/<owner>/<repository>` with no extra flags and `github setup` finds it already there.
- Without a terminal, no remote, and no flag, `configure` still writes the manifest but warns on stderr that
  `provision` will need `--image-repository`; it does not fail, because the documented scripted flow
  (`docs/guide.md`) and the existing suites call `configure` that way.
- `--image-repository` with `--runtime native` is refused before anything is written: a native application has no
  image.
- The README quick start shows the resulting flow.

## Capabilities

### New Capabilities

- `application-configure`: how `shiplord configure` decides and records the non-secret manifest values for an
  application, including the image repository a Docker application publishes to.

### Modified Capabilities

None; the project has no existing specs yet.

## Impact

- `src/shiplord/configure.py`: git-remote parsing and inference, the repository question, and the `[github]` block
  written by `create_manifest`.
- `src/shiplord/cli.py`: the `configure` command only - a new `--image-repository` option, the summary line, and the
  no-terminal resolution.
- `README.md`: the quick start and the existing-Docker-app paragraph.
- Tests: `tests/test_configure.py` (parsing, inference, the question) and `tests/test_cli.py` (flag, inference, the
  no-terminal warning, the native refusal, and that `provision` accepts the result).
- No change to `manifest.py`, `provision.py`, `github.py`, the protocol, or the exit-code contract.
