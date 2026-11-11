# Tasks

## 1. One pull path

- [x] 1.1 Extract `_pull(registration, image, phase, failure)` with the disk-space check, the `--config <login>`
      selection, and `docker pull --quiet` by digest, and make `pull(registration, release)` delegate to it, and verify
      the existing `PullTests` still assert the digest, the login, the refused pull's output, the timed-out pull, and
      the disk refusal
- [x] 1.2 Give the disk-space refusal the caller's phase, and verify a deploy still fails with `prepare` and the same
      message
- [x] 1.3 Verify `prepare` keeps its ordering, its messages, and its single fetch when the image is absent

## 2. A start fetches a missing image before anything is stopped

- [x] 2.1 Add `_ensure_image` to `_start` after the environment file check and before the volume check and
      `_remove_containers`, and verify a test asserts a start with the image present performs no pull
- [x] 2.2 Verify a test asserts a start with the image absent pulls that digest with the application's login, and that
      the pull precedes the first `docker rm` and the `docker run`
- [x] 2.3 Verify a test asserts a start that cannot fetch reports a `start` failure naming the registry host, carries
      no raw pull output, and issues no `docker rm` or `docker run`
- [x] 2.4 Verify a test asserts the disk-space refusal stops a start before any pull, removal, or run, and that the
      registry host is read from the reference (`ghcr.io` and `localhost:5000`)

## 3. Real-host evidence: a pruned image, a real registry

- [x] 3.1 Add a test to `tests/test_deploy_integration.py` that deploys two releases, removes the current release's
      container, prunes its image, and recovers: the recovery must fetch the image with the stored login and report
      `recovered`
- [x] 3.2 NOT done as a real-host failure: the fixture registry is anonymous, so no pull against it can fail for want
      of a login, and an image that no longer exists in it cannot be produced without deleting the manifest every later
      test needs. The honest failure is covered by `StartImageTests` (message, no output detail, no removal, no run)
      and by `test_a_failed_restore_requires_intervention_keeps_the_journal_and_blocks_deploys_until_recovered`
      (`intervention_required`, journal kept, `restore_error`). Recorded as a limitation, not claimed.
- [x] 3.3 Run `tests/linux/run.sh docker` and record the real result: `Ran 10 tests in 118.255s`, `OK`
- [x] 3.4 Confirm the negative test fails without the fix: with `_ensure_image` removed from `_start`, the same suite
      reports `FAILED (errors=1)` on `test_pruned_image_is_fetched_with_the_stored_login_when_the_release_is_started` -
      `docker run` pulled the pruned image anonymously and the stored login was never consulted

## 4. Verification

- [x] 4.1 Run `.venv/bin/python -m unittest discover -s tests` and verify the summary is `OK`
- [x] 4.2 Run `openspec validate restore-pulls-missing-image --strict` and verify it passes
