# Proposal

## Why

`test_a_corrupted_upload_is_refused_by_the_server_and_reported_as_invalid_input` corrupts the uploaded binary by
writing `b"X"` over its final byte. The payload is `elf(body=os.urandom(2048))`, so the final byte is random: when it
is already `0x58`, the "corrupted" copy is byte-identical to the original, the server correctly accepts it, and the
test fails with `AssertionError: ClientError not raised`. That is a 1-in-256 false red on changes unrelated to the
test, which teaches people to re-run CI instead of reading it.

## What Changes

- Corrupt the upload by complementing the final byte (`last ^ 0xFF`) instead of overwriting it with a constant, so the
  damaged copy can never equal the original whatever the original final byte is.
- Assert inside the test that the corrupted bytes differ from the payload that was read, so a future no-op corruption
  fails loudly at the point of corruption instead of looking like a server regression.
- Add a deterministic regression test that exercises every possible final byte value `0x00`-`0xFF` through the same
  deploy path, each with its own request id, and asserts the server refuses each one as an invalid payload with a
  checksum message.
- No production code changes: the server's checksum rejection is already correct, and `src/` is untouched.

## Capabilities

### New Capabilities

- `deterministic-upload-corruption`: how the test suite proves a corrupted native upload is refused - the corruption
  always changes the payload, and every possible final byte value is covered deterministically.

### Modified Capabilities

None; the project has no existing specs yet, and no product behaviour changes.

## Impact

- `tests/test_native_commands.py`: the flaky test and one new regression test; no other test module is affected.
- `src/shiplord/payload.py` and the rest of `src/` are unchanged; the server still rejects a checksum mismatch with
  `invalid_payload` and exit code 2.
- No protocol, exit-code, manifest, CLI-argument, or runtime behaviour change.
- `tests/linux/run.sh` is not applicable to this change and cannot run here because Docker is unavailable.
