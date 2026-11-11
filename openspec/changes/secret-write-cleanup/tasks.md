# Tasks

## 1. Make the registry login file safe to read and write

- [x] 1.1 Add tests in `tests/test_provision.py` that write a hand-edited `config.json` (not JSON, top level not an
      object, `auths` not an object, entry not an object, missing or non-string `auth`, invalid base64, non-UTF-8,
      no colon, invalid username) and assert `list` and `login` raise `ProvisionError` naming `config.json` with no
      token, no traceback, and an unchanged file; verify they fail before the fix with
      `.venv/bin/python -m unittest tests.test_provision.RegistryLoginTests`
- [x] 1.2 Replace the unchecked `json.loads(...).get("auths", {})` decode with a `_registry_auths` reader and a
      `_registry_username` validator, keeping the raw entries so a login preserves other registries; verify task 1.1
      passes
- [x] 1.3 Add a test that patches `shiplord.provision.os.replace` and `shiplord.provision.json.dump` to fail during
      `registry login` and asserts the login directory is empty afterwards; verify it fails before the write fix
- [x] 1.4 Write and remove `config.json` relative to a directory descriptor, sync the directory after a replace or an
      unlink, and unlink the temporary file in `finally` on every failure path; verify task 1.3 passes

## 2. Give the key store the release_state discipline

- [x] 2.1 Add tests in `tests/test_keys.py` that a symlinked key file is refused for add/remove and unlinked (not
      followed) by delete, and that a symlinked key directory is refused; verify they fail before the fix with
      `.venv/bin/python -m unittest tests.test_keys`
- [x] 2.2 Add a test that records `os.fsync` descriptors during `remove` and `delete` and asserts a directory
      descriptor was synced; verify it fails before the fix
- [x] 2.3 Open the key directory once with `O_DIRECTORY | O_NOFOLLOW`, validate it with `release_state._directory`,
      and make `_read`, `_write`, and `delete` use `dir_fd=` for open, replace, and unlink, syncing the directory
      after a replace or an unlink; verify tasks 2.1 and 2.2 pass and the existing key tests still pass

## 3. Verify the change end to end

- [x] 3.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm `OK` with the 810 baseline tests plus the
      new ones and 68 skips
- [x] 3.2 Revert `src/shiplord/provision.py` and `src/shiplord/keys.py` in a scratch copy and confirm every new test
      fails, so they are regression tests rather than assertions of existing behaviour
- [x] 3.3 Run `tests/linux/run.sh host` and confirm it passes against the real sshd/sudo helper image
- [x] 3.4 Run `openspec validate secret-write-cleanup --strict` and confirm the change is valid
