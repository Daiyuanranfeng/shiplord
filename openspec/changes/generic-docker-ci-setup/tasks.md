# Tasks

## 1. Workflow rendering

- [x] 1.1 Add `DEPLOY_WORKFLOW_PATH` and `DEFAULT_QUALITY_WORKFLOW` to `workflow.py`, plus shared input
      validation for both renderers.
- [x] 1.2 Render the reusable deploy workflow (`on: workflow_call`, `build`, `deploy`, secret declaration).
- [x] 1.3 Render the trigger workflow: triggers, concurrency, optional `quality` job, `deploy` caller job
      with explicit secret mapping and permissions.
- [x] 1.4 Add `validate_quality_workflow` (path shape, refuses the generated files).
- [x] 1.5 Rewrite `tests/test_workflow.py` for both documents: job split, permissions per file, check-only
      path, determinism, no shell interpolation, pinned actions, quality job present/absent/refused.

## 2. Setup for a generic manifest

- [x] 2.1 Replace `validate_setup_profile` with `validate_setup_request` (repository shape and `[github]`
      contradiction only); call it first in `github_setup`.
- [x] 2.2 Write and guard both workflow files, recording a digest per path while honouring the legacy
      `workflow_sha256`.
- [x] 2.3 Resolve the quality workflow from the project or the explicit request.
- [x] 2.4 Make `check_ci` find the deploy job in a called workflow (`deploy / deploy`).
- [x] 2.5 Report both workflow files in `github_status`.
- [x] 2.6 Update `tests/test_github.py`: generic setup reaches the key/secret/variables/workflow steps,
      both files are written and guarded, no quality job without the file, `deploy / deploy` is matched.

## 3. Commands

- [x] 3.1 `github setup`: `--quality-workflow PATH` and `--no-quality-workflow`, mutually exclusive, with
      the early request validation before the Tailscale prompt.
- [x] 3.2 Add the `keys` command group: `add` (`--label`, exactly one of `--public-key-file` /
      `--public-key-stdin`), `list`, `remove` (requires `--yes`).
- [x] 3.3 Cover the commands in `tests/test_cli.py`: generic setup is no longer rejected before the server
      lookup, key input rules, label validation, and `--yes`.

## 4. Verification

- [x] 4.1 `cd /private/tmp/shiplord-bug-37 && .venv/bin/python -m unittest discover -s tests` passes with
      810 tests, 68 skipped (plus the new tests), re-running once if the known 1-in-256 flake fails.
- [x] 4.2 Run the rendered workflows through the independent YAML parser in the tests and through
      `actionlint` when it is installed.
- [x] 4.3 Record that `tests/linux/run.sh` was not run because Docker is unavailable on this machine.
- [x] 4.4 Commit only this change directory and the source/test edits, then open the pull request.
