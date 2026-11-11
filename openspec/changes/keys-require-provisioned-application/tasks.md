# Tasks

## 1. Prove the defect

- [x] 1.1 Add failing tests in `tests/test_provision.py::KeyManagementTests` that `add`, `list`, and `remove` for an unprovisioned name raise `ProvisionError` matching `not provisioned`; run `.venv/bin/python -m unittest tests.test_provision.KeyManagementTests` and confirm the new tests fail against the current code
- [x] 1.2 Add a failing test that each refused call leaves the layout unchanged — a file snapshot of the whole prefix and the `authorized_keys` listing are identical before and after, and no `ship-<name>` key file exists — and confirm it fails while the key file is still written
- [x] 1.3 Add a failing test that a stray authorized-keys file for an unprovisioned name does not make it provisioned: `add` is still refused and the stray file keeps its bytes

## 2. Refuse unprovisioned applications

- [x] 2.1 Extract the environment path's provisioned check into a module-level `_require_provisioned(layout, application_id)` helper that keeps the exact condition and message, and call it from `manage_environment`; verify `.venv/bin/python -m unittest tests.test_provision.EnvironmentTests` still passes unchanged
- [x] 2.2 Call the helper from `manage_key` after `validate_app_name` and before the `KeyStore` is constructed, with a docstring that states the precondition; verify tasks 1.1 to 1.3 now pass
- [x] 2.3 Assert in the refusal tests that the error text contains no host path by checking it against `str(self.layout.authorized_keys)` and `str(self.layout.prefix)`, and that no key material appears

## 3. Confirm nothing else moved

- [x] 3.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is `OK` with 68 skipped: the baseline 810 tests plus the new ones
- [x] 3.2 Run `.venv/bin/python -m unittest tests.test_provision` and confirm a provisioned application still adds, re-adds, lists, removes, and refuses to remove its last key with the same results as before
- [x] 3.3 Run `openspec validate keys-require-provisioned-application --strict` and confirm the change validates

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
- Note in the pull request that `tests/linux/run.sh` was not run because other suites hold the shared Docker
  daemon, and that the refusal is covered by unit tests only.
