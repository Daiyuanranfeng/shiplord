# Tasks

## 1. Keep the rollback target across a redeploy

- [x] 1.1 Add `tests/test_release_state.py` cases: a second commit of the already-committed release keeps the prior
      `previous`, and `record_commit` returns the state it wrote; confirm the `previous` assertion fails before the
      fix (`python -m unittest tests.test_release_state`)
- [x] 1.2 Keep `prior["previous"]` in `_candidate_state` when the journal's candidate equals the prior `current`,
      and return the committed state from `record_commit`

## 2. Retire only what the committed window no longer names

- [x] 2.1 Add `tests/test_deploy.py` cases: deploy A, deploy B, stop B, redeploy B — the committed `previous` is
      still A, the outcome reports A, nothing is discarded, and `rollback` runs A; a release that is running but not
      ready behaves the same; confirm each fails before the fix (`python -m unittest tests.test_deploy`)
- [x] 2.2 Pass the committed state to `_retire` and keep any artifact the committed `current` or `previous` names;
      report `committed["previous"]` in the success outcome
- [x] 2.3 Add a `tests/test_deploy.py` case that a genuinely different release after such a redeploy still rotates
      `previous` and discards the release that left the window

## 3. Make a rollback and a recovery tell the truth about a lost target

- [x] 3.1 Add a `tests/test_deploy.py` case that a committed `previous` equal to `current` is refused with
      `no_previous_release` and restarts nothing; confirm it fails before the fix
- [x] 3.2 Refuse a rollback whose committed `previous` equals the committed `current`
- [x] 3.3 Add a `tests/test_release_state.py` case that `reconciliation_target` accepts the state the older helper
      wrote for a redeploy of the committed release; confirm it fails before the fix
- [x] 3.4 Accept that legacy committed state in `reconciliation_target`, and only that one

## 4. Verify the change against a real daemon

- [x] 4.1 Add a `tests/test_deploy_integration.py` case: deploy v1, deploy v2, stop v2, redeploy v2 — `previous` is
      still v1, v1's image is still inspectable, and `rollback` serves v1 again
- [x] 4.2 Run `python -m unittest discover -s tests` and confirm `OK` with 952 tests and 74 skips (the docker case
      skips without `SHIPLORD_TEST_DOCKER=1`)
- [x] 4.3 Revert `src/shiplord/release_state.py` and `src/shiplord/deploy.py` only and confirm every new test fails,
      so they are regression tests
- [x] 4.4 Run `tests/linux/run.sh docker` and confirm the new case passes against the host daemon and registry

## Workflow follow-up

- Archive the change after review.
