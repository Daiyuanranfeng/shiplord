# Tasks

## 1. Prove the defect

- [x] 1.1 Add a failing test in `tests/test_provision.py` that a re-provision with a changed domain, a
  changed runtime, and a changed image repository is refused and leaves the key file, the sudoers rule, the
  registration, the account database, and the group membership byte-identical, including no key under the new
  label; run `.venv/bin/python -m unittest tests.test_provision` and confirm it fails because the new key is
  added before the refusal
- [x] 1.2 Add a failing test that provisioning a second application onto the first application's hostname
  leaves no account, no key file, no sudoers rule, no state or environment directory, and no registration;
  confirm it fails against the current order
- [x] 1.3 Add a failing test that a native provisioning whose runtime account is foreign leaves no
  deployment account, group membership, key, or sudoers rule; confirm it fails while the deployment account
  is created first

## 2. Split registry validation from the write

- [x] 2.1 Add `Registry.check()` plus the private record and comparison helpers, keeping `register()`'s
  signature, messages, host-lock requirement, and port scan; verify
  `.venv/bin/python -m unittest tests.test_registry` still passes unchanged
- [x] 2.2 Add `tests/test_registry.py` coverage that `check()` returns the identical existing record, refuses
  a changed identity, a taken hostname, and a taken deployment account with the same messages, requires the
  host lock, and leaves the registry directory listing unchanged in every case
- [x] 2.3 Add `tests/test_registry.py` coverage that an unknown deployment UID is accepted when no
  registration exists and refused as a difference when one does

## 3. Validate before the first side effect

- [x] 3.1 Extract the "is this our account" condition into `_is_ours()` and add the read-only
  `_existing_user()`/`_existing_account()`/`_existing_runtime_account()` helpers, keeping the existing error
  message verbatim; verify `.venv/bin/python -m unittest tests.test_provision` still passes
- [x] 3.2 Run the account pre-checks and `Registry.check()` at the top of `provision()`, before the first
  account, group, directory, or key side effect, and report the existing registration from the pre-check in
  `created["registration"]`; verify tasks 1.1 to 1.3 now pass
- [x] 3.3 Update `test_repeating_provision_changes_nothing_and_reuses_the_account` to the new read-only call
  list and confirm it still asserts that nothing changed

## 4. Prove it on a real host

- [x] 4.1 Add `tests/test_host_integration.py` cases that a changed-domain re-provision and a hostname
  conflict are refused with no new key, account, group membership, sudoers rule, environment directory, or
  registration, and that the existing files are byte-identical
- [x] 4.2 Run `tests/linux/run.sh host` and confirm the suite passes with the new cases

## 5. Confirm nothing else moved

- [x] 5.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is `OK` with the same
  skips as before plus the new tests
- [x] 5.2 Run `openspec validate validate-before-provisioning --strict` and confirm the change validates

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
