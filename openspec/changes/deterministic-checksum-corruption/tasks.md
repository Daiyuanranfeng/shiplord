# Tasks

## 1. Deterministic corruption in the flaky test

- [x] 1.1 Add a `corrupt_payload` helper in `tests/test_native_commands.py` that complements the final byte, use it in
      the corrupted-upload test, and assert there that the damaged bytes differ from the payload read; verify with
      `.venv/bin/python -m unittest tests.test_native_commands.NativeCommandTests.test_a_corrupted_upload_is_refused_by_the_server_and_reported_as_invalid_input`
- [x] 1.2 Add a regression test that runs the corrupted upload through the deploy path for every final byte value
      `0x00`-`0xFF` with a distinct request id, asserting exit code 2 and a checksum message each time; verify with
      `.venv/bin/python -m unittest tests.test_native_commands.NativeCommandTests.test_a_corrupted_upload_is_refused_whatever_its_final_byte_is`

## 2. Verification

- [x] 2.1 Temporarily restore the old `payload[:-1] + b"X"` corruption, confirm the new regression test fails on the
      `0x58` subtest, then restore the complement; verify the restored test passes
- [x] 2.2 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is OK with the baseline 810 tests
      and 68 skips; run `openspec validate deterministic-checksum-corruption --strict` and confirm the change validates

## Workflow follow-up

- Archive the change after review and merge.
