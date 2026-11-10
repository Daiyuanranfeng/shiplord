# Tasks

## 1. Skip and report stray registry files

- [x] 1.1 Add a test in `tests/test_registry.py` that places `app.json~`, `.app.json.swp` and `stray.txt` beside a
      registration, asserts the registration still lists, and asserts the skipped names are reported; confirm it fails
      before the fix (`python -m unittest tests.test_registry`)
- [x] 1.2 Skip non-`.json` names in `Registry.list`, record them in `Registry.stray`, and keep `.json` records failing
      closed including a `.json` file whose name is not a valid application
- [x] 1.3 Report `ignored_files` from `application_inventory` and print it once in `shiplord apps`, for both the text
      and `--json` forms, and add a test in `tests/test_serverdoctor.py` that drives the `apps` action with a stray
      file present

## 2. Verify the change

- [x] 2.1 Run `python -m unittest discover -s tests` and confirm `OK` with the 783 baseline tests plus the new ones
      and 68 skips
- [x] 2.2 Confirm the new tests fail when `src/` is reverted, so they are regression tests and not assertions of
      existing behaviour
- [x] 2.3 Confirm a corrupt `.json` record and a group-writable record still raise `StateError`
