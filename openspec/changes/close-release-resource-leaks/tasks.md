# Tasks

## 1. Release retention

- [x] 1.1 Add a deployer helper that answers whether discarding a release would remove a resource a retained release owns (native: `release_id`, Docker: artifact), and use it in `_retire`; verify a new test deploys three native releases with the identical binary and asserts the oldest `release_id` is the only discard (it fails with an empty discard list before the change)
- [x] 1.2 Use the same helper in `_abandon`; verify the existing rollback and failed-deploy tests still assert no discard of a retained resource

## 2. A prepared artifact with no journal

- [x] 2.1 Abandon the prepared artifact after a failed journal open unless a journal record exists, and report the abandon warnings in the outcome; verify a new test patches the journal open to fail and asserts the artifact is discarded and the outcome is `journal_unavailable` (it fails with no discard before the change)
- [x] 2.2 Keep the artifact when a journal is readable; verify a new test writes a journal record before the failed open and asserts no discard happens

## 3. Job records

- [x] 3.1 Prune queued or running records older than the launcher's runtime bound plus a grace margin at the start of `create`, so the request ID is reusable; verify a new test creates a started job, advances the store clock past the bound, creates the same request ID again and asserts it is created anew (it fails as `created=False` before the change)
- [x] 3.2 Keep recent unfinished records; verify the new test asserts a job started within the bound is still present after another create, and the existing prune test still passes

## 4. Job unit names

- [x] 4.1 Use the request ID verbatim when it fits and `sha256` of the full ID when it does not; verify a new test asserts two IDs sharing their first 24 characters produce different unit names and that short names keep the existing shape

## 5. Deprovision leftovers

- [x] 5.1 Remove the application's job directory in the existing removal loop and report it only when it existed; verify a new test writes a job record, deprovisions, and asserts the directory is gone and reported
- [x] 5.2 Remove the managed route file and its interrupted-change backup through a `dir_fd` with `O_NOFOLLOW` and a directory fsync, tolerating a missing Caddy root; verify a new test writes a route file, deprovisions without a retirement callback, and asserts it is gone and reported
- [x] 5.3 Verify an application with no job directory and no route still deprovisions with every reported removal true

## 6. Verification

- [x] 6.1 Run `.venv/bin/python -m unittest discover -s tests` and verify the summary is `OK` with at least the baseline 810 tests and 68 skips
- [x] 6.2 Run `openspec validate close-release-resource-leaks --strict` and verify it reports the change is valid
- [x] 6.3 Record that `tests/linux/run.sh` was not run because the Docker daemon is shared with other agents and the suites race

## Workflow follow-up

- Archive the change after review and merge.
