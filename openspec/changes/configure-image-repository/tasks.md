# Tasks

## 1. Prove the defect

- [x] 1.1 Add a failing test in `tests/test_cli.py` that `configure --image-repository owner/orders` records the
  repository and that `commands.image_repository_for(load_manifest(...), None)` then returns
  `ghcr.io/owner/orders`; run `.venv/bin/python -m unittest tests.test_cli` and confirm it fails with the current
  code (no `--image-repository` option, no `[github]` table)
- [x] 1.2 Add a failing test that `configure` in a git repository whose `origin` is a GitHub URL records the inferred
  repository without the flag, and confirm it fails the same way

## 2. Repository inference and validation

- [x] 2.1 Add `repository_from_url` and `repository_from_git_remote` to `src/shiplord/configure.py` and verify with
  tests for `git@github.com:owner/orders.git`, `https://github.com/owner/orders.git`,
  `https://github.com/owner/orders`, `ssh://git@github.com/owner/orders.git`, a `gitlab.com` remote, a malformed
  remote, an empty remote, a directory that is not a git repository, and a URL carrying a credential (asserting the
  URL, user, and token appear in no message)
- [x] 2.2 Add `validate_repository` that reuses `manifest.validate_manifest` and verify it accepts `owner/orders` and
  rejects `orders`, `owner/`, `../owner/orders`, and a non-string
- [x] 2.3 Add `resolve_image_repository` (flag, else remote, else a question when a terminal is available) and verify
  in `tests/test_configure.py` that the flag wins, that a detected remote is used without a question, that the
  question is asked and re-asked on an invalid answer when no remote exists, and that a native runtime asks nothing

## 3. Write the repository into the manifest

- [x] 3.1 Extend `create_manifest` with a keyword-only `image_repository` that adds `[github]` (`repository` plus
  `deployment_branch = "main"`) and verify a round trip through `load_manifest` plus rejection of an invalid value
  with no file created
- [x] 3.2 Wire the option into `collect_answers` and the interactive summary in `cli.py`, and verify with the
  scripted-keyboard tests in `tests/test_configure.py` that accepting the summary writes the repository and that
  declining still writes nothing
- [x] 3.3 Add `--image-repository` to the `configure` command for the no-terminal path, refuse it with
  `--runtime native` before writing, and verify both in `tests/test_cli.py`
- [x] 3.4 Warn on stderr when a no-terminal run has no flag and no GitHub remote, and verify the warning names
  `--image-repository`, the manifest is still written, and no warning appears when the flag or the remote supplied one

## 4. Documentation and whole-suite checks

- [x] 4.1 Update the README quick start and the existing-Docker-app paragraph to show the repository being recorded
  (inferred, asked, or `--image-repository`), and verify every command shown matches the CLI's options
- [x] 4.2 Run `.venv/bin/python -m unittest discover -s tests` and confirm `OK` with the baseline 810 tests plus the
  new ones and 68 skipped
- [x] 4.3 Run `.venv/bin/python -m unittest tests.test_configure tests.test_cli tests.test_commands tests.test_provision`
  and confirm the existing configure, provision, and image-repository behaviour is unchanged

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
- Note in the pull request that `tests/linux/run.sh` was not run because Docker is unavailable on this host.
