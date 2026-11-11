# Design

## Context

`github.py` owns the client-side setup: it generates a CI key, registers and proves it through the admin
channel, stores it with `gh secret set`, sets repository variables, and writes one workflow. `workflow.py`
renders that workflow from the manifest, the pinned tooling commit, and the platforms. `cli.py` exposes
`github setup|status|check-ci|rotate-key` and no key management. The admin channel already implements a
`keys` action with `add`, `remove`, and `list` (`provision.manage_key`), so nothing server-side is missing.

Two constraints shape the approach. First, the only template-specific thing about setup was a gate in
`validate_setup_profile`; workflow rendering already depends on `runtime`, not on `template`, and a native
application already gets credentials without a workflow. Second, a job that calls a reusable workflow is
reported by GitHub as `<caller job> / <called job>`, which `check_ci` currently does not expect.

## Goals / Non-Goals

**Goals:**

- One code path for setup across templates, with the early validation that gate used to provide.
- A generated pipeline whose deploy half is reusable by another workflow and whose quality half is opt-in.
- A supported command for registering a deploy key when setup is done by hand.

**Non-Goals:**

- Changing the manifest format, the admin protocol, or `provision.py`.
- Making the quality job mandatory, or shipping a quality workflow of Shiplord's own.
- Running the Linux suites in this change (Docker is unavailable on this machine).

## Decisions

**Two generated files instead of one job graph.** `.github/workflows/shiplord-deploy.yml` declares
`on: workflow_call` and holds `build` and `deploy`; `.github/workflows/shiplord-release.yml` keeps the
`push`/`workflow_dispatch` triggers, the concurrency group, the optional `quality` job, and a `deploy` job
that calls the first file. This is the GitHub "reusing workflows" mechanism the issue links to, and it lets
a repository with its own CI call the deploy workflow directly. Alternative considered: keeping one file
and only dropping the quality job. That satisfies the broken-reference criterion but leaves nothing
reusable, so it does not satisfy the split.

**Explicit secret passing, not `secrets: inherit`.** The deploy workflow declares
`on.workflow_call.secrets.SHIPLORD_SSH_PRIVATE_KEY` and the trigger maps it explicitly. This keeps the
secret set visible in both files and avoids handing a called workflow every repository secret. The token is
not declared: `secrets.GITHUB_TOKEN` is provided to called workflows automatically.

**Permissions at the caller are the union, restricted again per job.** A called workflow cannot exceed the
permissions granted to the calling job, so the trigger's `deploy` job grants `contents: read`,
`id-token: write`, and `packages: write`. Inside the deploy workflow each job still narrows further
(`build`: contents+packages, `deploy`: contents+id-token), so no job gets more than it uses. The existing
"only where OIDC is used" invariant becomes per file.

**Condition placement moves with the jobs.** `build` now carries `if: ${{ !inputs.check_only }}` because the
`quality` job that used to skip it lives in the other file. The trigger's `deploy` job carries
`if: ${{ !cancelled() && (inputs.check_only || needs.quality.result == 'success') }}` when a quality job
exists, so a skipped quality job in check-only mode does not skip the caller, while a failed quality job
still blocks deploy. Without a quality job it is `if: ${{ !cancelled() }}`.

**The quality job is decided from the project, not guessed.** `render_workflow` takes
`quality_workflow: str | None`, so it stays deterministic for a given input. `_write_workflow` chooses the
value: the explicit `--quality-workflow` path, or `.github/workflows/code-quality.yml` when that file
exists, or nothing. `--no-quality-workflow` forces nothing. `workflow.validate_quality_workflow` accepts
only `.github/workflows/<name>.ya?ml` and refuses the two generated files, so a caller can never point the
pipeline at itself. Alternative considered: always emit the job and let the user create the file. That is
exactly the broken reference the issue reports.

**`validate_setup_profile` becomes `validate_setup_request`.** The template gate disappears; the checks
that must precede the first side effect (repository shape, contradiction with an existing `[github]`
section) move into it and are now also run by the CLI before it prompts for Tailscale values. Both
`github_setup` and the CLI call it, so the guarantee is unchanged for the templates that were already
supported and extended to the generic one.

**`check_ci` matches the deploy job by its last name segment.** A called job is reported as
`deploy / deploy`; matching `"deploy"` exactly would silently find nothing and report every check as a
failure. Matching the segment after the last `" / "` keeps step reporting intact.

**Key management lives in `cli.py` over the existing admin action.** `keys add|list|remove` validate the
label and the public key with `keys.validate_label`/`keys.parse_public_key` before connecting, then call
the `keys` action. `provision.py` is shared with other work and already implements the operation, so the
client adds no server code. The public key comes from exactly one of `--public-key-file` or
`--public-key-stdin`, matching how `registry login` and `env set` take sensitive input.

**Both generated files are guarded by their own recorded digest.** `state.json` gains a `workflows` map
from path to SHA-256. The old `workflow_sha256` is still honoured for `shiplord-release.yml`, so an existing
installation is not forced to regenerate, and a `shiplord-deploy.yml` that Shiplord did not write is never
overwritten without `--force-workflow`.

## Risks / Trade-offs

- [A reusable workflow adds a second file the user must commit] → both are written by one command, both are
  reported in `steps.workflow`, and the edited-file guard names whichever file was changed.
- [The caller's permission union is broader than either called job] → each called job narrows it again, and
  the union is exactly what the two jobs need.
- [A quality workflow that exists but is not reusable would fail at run time] → the path is validated and
  the file is only auto-detected by name; a user who points at a non-reusable workflow sees the failure on
  the first run, which is reported by `check_ci` and `github status`.
- [Older generated pipelines keep a single file] → `state.json` compatibility keeps them working until the
  next `--force-workflow`, and `github status` reports a missing deploy workflow as a problem.
- [Docker is unavailable here] → the workflow files are validated by parsing the rendered YAML and, when
  `actionlint` is installed, by actionlint; the Linux suites remain unrun and the PR says so.

## Migration Plan

`shiplord github setup --repo owner/name --force-workflow` regenerates both files for an existing
installation; without `--force-workflow`, an untouched previously generated file is replaced only when its
digest matches what Shiplord recorded, and an edited one is refused. Rolling back is deleting
`.github/workflows/shiplord-deploy.yml` and re-running setup at the previous version.
