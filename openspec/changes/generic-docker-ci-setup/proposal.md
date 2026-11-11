# Proposal

## Why

`shiplord github setup` refuses `template = "generic"` before it does anything, so an existing Docker
application — the most likely thing someone brings to Shiplord — cannot get a CI key, a repository
secret, or the repository variables. The generated release workflow also hard-references
`./.github/workflows/code-quality.yml`, a file only the Google ADK template happens to contain, so even a
supported application breaks on its first push. Only the ADK template has one-command CI setup, and there
is no supported command for handing CI a deploy key without that template.

## What Changes

- `github setup` runs to completion for a generic Docker manifest: it creates the CI key, registers and
  proves it, stores the secret, sets the variables, writes the workflow, and records `[github]` in
  `ship.toml`. Nothing template-specific is rejected any more.
- The generated pipeline is split into two files: `.github/workflows/shiplord-deploy.yml`, a reusable
  workflow (`on: workflow_call`) holding the build and deploy jobs, and `.github/workflows/shiplord-release.yml`,
  the trigger that calls it and runs an optional quality job first.
- The quality job is emitted only when the repository really has that reusable workflow: Shiplord uses
  `.github/workflows/code-quality.yml` when it exists, honours `--quality-workflow <path>` for a different
  file, and `--no-quality-workflow` omits it. No generated workflow references a file the user has not
  created.
- New `shiplord keys` commands (`add`, `list`, `remove`) register, inspect, and revoke the SSH keys that
  may deploy an application, so CI can be given deploy access without any template.
- The existing guarantees are kept: actions pinned to full commits, least permissions per job, no secret
  or variable interpolated into a shell script, deterministic output, and no generated file overwritten
  unless Shiplord wrote it or `--force-workflow` is passed.

## Capabilities

### New Capabilities

- `ci-release-pipeline`: setting up GitHub Actions access for an application (CI key, secret, variables,
  generated workflows) for every supported template, the shape of the generated release pipeline, and
  managing the deploy keys CI uses when setup is done by hand.

### Modified Capabilities

None; the project has no specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/github.py`: the profile gate becomes repository validation, both workflow files are
  written and guarded, and `check_ci` follows the deploy job into the called workflow.
- `src/shiplord/workflow.py`: renders the reusable deploy workflow plus the trigger workflow, and
  validates the optional quality workflow path.
- `src/shiplord/cli.py`: `github setup` gains `--quality-workflow`/`--no-quality-workflow`, and a new
  `keys` command group is registered.
- `tests/test_github.py`, `tests/test_workflow.py`, `tests/test_cli.py`: coverage for the generic path,
  the two-file pipeline, the optional quality job, and the new commands.
- No manifest, protocol, server-helper, or `ship.toml` format change; `provision.py` is untouched.
