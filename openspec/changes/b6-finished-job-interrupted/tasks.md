# Tasks

## 1. Report a finished job as finished

- [x] 1.1 Add a test in `tests/test_helper_actions.py` that hands `job_view` a stale `running` record while the store
      holds a finished one and the unit is inactive, and asserts `finished` plus the recorded outcome; confirm it
      fails before the fix (`python -m unittest tests.test_helper_actions -k finished_job`)
- [x] 1.2 Re-read the record in `job_view` when the unit is inactive and report `finished` when the fresh record is
      finished, keeping `interrupted` for a record that is still unfinished or unreadable; confirm the new test and
      `test_a_job_whose_worker_vanished_is_reported_as_interrupted` both pass
- [x] 1.3 Assert through `status` that a job whose worker finished and whose unit is gone is reported `finished`, and
      that a queued record with an inactive unit is still `interrupted`; confirm both tests pass

## 2. Verify the change

- [x] 2.1 Run `python -m unittest discover -s tests` and confirm `OK` with 783 baseline tests plus the new ones and
      68 skips
- [x] 2.2 Confirm `AGENTS.md`'s exit-code table still holds: `finished` returns the recorded outcome's exit code and
      only `interrupted` maps to 6, by reading `src/shiplord/client.py` and the untouched dispatch paths
