# Design

## Context

See proposal.md for motivation. Facts from the current code that shape the approach:

- `commands.image_repository_for` derives the allowlisted repository as `ghcr.io/` + `manifest["github"]["repository"]`
  in lower case, and raises when a Docker manifest has no `[github]` table. It already accepts an explicit
  `--image-repository` override at `provision`.
- `manifest.validate_manifest` accepts an optional `[github]` table with exactly `repository` (`owner/name`) and
  `deployment_branch` (a valid branch name). `github._ensure_manifest_github` writes that table as
  `repository` plus `deployment_branch = "main"`, so `configure` should write the same shape and the same default.
- `configure.create_manifest` validates a complete manifest dict, then renders the TOML by hand and publishes it with
  a same-directory hard link, so an existing `ship.toml` is never replaced.
- `configure.collect_answers` fills missing values in a terminal; the CLI's no-terminal branch builds its answers
  from flags only. Both end in `create_manifest`.
- `detect` reads repository files for hints but never runs git; `commands.commit_sequence` already shells out to git
  with `capture_output` and a timeout, which is the local pattern for that.

## Goals / Non-Goals

**Goals:**

- A Docker manifest created by `configure` names an image repository whenever one can be determined, and asks when it
  cannot, so `provision` needs no flags.
- Inference cannot invent a registry: only a `github.com` remote is used, because `ghcr.io` is the only registry
  Shiplord knows.
- Every new code path is testable without a terminal and without network access.

**Non-Goals:**

- Changing `manifest.py`, `provision.py`, `github.py`, or the protocol. The `[github]` table is the existing contract.
- Making `configure` fail when a scripted run cannot determine a repository; see the trade-off below.
- Inferring anything else from git (branch, commit, tags).

## Decisions

**Infer from `origin` only, and only from `github.com`.** `git remote get-url origin` in the application directory is
one subprocess call with a timeout; a non-zero exit, an `OSError`, or a timeout all mean "no repository known". The
URL is parsed with `urlsplit` when it has a scheme and with a single `host:path` split for the scp-like
`git@host:owner/name.git` form. The host must be `github.com` and the path exactly `owner/name` after stripping a
leading `/` and a trailing `.git`; anything else returns `None`. Alternatives considered: accepting any host and
recording it (would put a non-GitHub repository into a table that means GitHub and derive a `ghcr.io` name for an
image that is not there), and asking `gh` (needs a network and an authenticated CLI for a value git already holds).

**Reuse the manifest's own repository rule instead of copying the regex.** `configure.validate_repository` validates a
candidate by passing a fixed, otherwise-valid manifest containing that candidate to `manifest.validate_manifest` and
returning the normalised value. `manifest.py` is not in scope for this change, and a second copy of the rule would
drift from it; the probe keeps one source of truth and makes the interactive parser reject exactly what
`load_manifest` would reject.

**Ask only when inference fails.** The detected repository is announced and used as-is; the summary that precedes
"Write ship.toml?" shows it, and `--image-repository` overrides it. This matches the issue's wording ("inferred ...
when one is present, and prompted for when it is not") and keeps the question count low. The container-port question
keeps its existing detect-then-ask-with-default shape, which is different because a wrong port is silent while a
wrong repository is visible in the summary.

**Write the same `[github]` block `github setup` writes.** `repository` plus `deployment_branch = "main"`, appended
after `[deploy]`. A manifest created by `configure` is therefore already what `github setup --repo` would have
written, and `github setup` on the same repository does not conflict.

**Do not fail a scripted run for a missing repository.** A no-terminal `configure` with no flag and no GitHub remote
still writes the manifest and warns on stderr. Failing was the alternative and is cleaner in isolation, but it would
break the documented flag-only flow in `docs/guide.md` and the Linux end-to-end suites, which call `configure` that
way before adding their own `[github]` table - and `docs/guide.md` is outside this change's scope. The warning names
the option, so the failure the issue reports is at least visible one command earlier.

**Refuse `--image-repository` with `--runtime native` in the CLI.** A native application deploys a binary; recording
an image repository for it would be a value no command reads. The check runs before any file is written.

## Risks / Trade-offs

- [A scripted run without a remote still writes a manifest `provision` rejects] -> It warns on stderr and names
  `--image-repository`; the README shows the flag. Closing that path fully means failing the run, which needs
  `docs/guide.md` and the e2e suites updated in the same change.
- [Inference picks the wrong repository when the app is a subdirectory of a monorepo] -> The summary shows the
  detected value before writing, and `--image-repository` overrides it; git's own upward discovery is what a person
  would expect from "the repository I am in".
- [A remote URL can carry a credential] -> Only the parsed `owner/repository` is ever printed or stored; the URL is
  never included in a message, a result, or an error.
- [`git` may be missing or slow] -> The call is bounded by a timeout and every failure is treated as "no repository";
  `configure` then behaves exactly as it does today.
- [Two sources of truth for the repository default branch] -> `configure` writes `main`, the same literal
  `github.py` writes; a repository whose default branch is not `main` can still be edited, and `github setup` does
  not overwrite the table when the repository matches.

## Migration Plan

No migration. Manifests written before this change keep working; a Docker manifest without `[github]` still needs
`provision --image-repository` or a hand-added table, exactly as today. Rollback is reverting the two source files.

## Open Questions

None that change the specs, the approach, or the tasks.
