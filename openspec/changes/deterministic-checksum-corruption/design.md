# Design

## Context

See `proposal.md` - Why. Three facts shape the approach:

- `elf(body=os.urandom(2048))` returns `ident + header + body`, so the final byte of every uploaded binary is random.
  The test cannot choose it without controlling the random source or rebuilding the payload.
- The corruption lives inside the test's `payload_runner` wrapper, so it sees the exact bytes the client is about to
  upload, not the path alone. That is the only place where the "damaged differs from original" property can be
  checked without duplicating the corruption.
- A deploy that fails the checksum is rejected before any job is recorded, but the server binds a request id to the
  request it accepted. Repeated attempts must therefore use distinct request ids, or the server answers "the request
  ID was already used for a different request" instead of reaching the checksum check.

## Goals / Non-Goals

**Goals:**

- Make the corruption unconditional for every possible final byte, with no dependence on a random draw.
- Fail at the corruption itself if it ever becomes a no-op, so the failure names the real cause.
- Cover all 256 final byte values through the real deploy path in one fast, deterministic test.

**Non-Goals:**

- Changing the server's checksum handling, the payload protocol, or any `src/` module.
- Replacing `os.urandom` globally or adding a random-source seam to production code.
- Changing any other test or the shared `elf()` helper.

## Decisions

**Complement the final byte instead of writing a constant.** `payload[:-1] + bytes([payload[-1] ^ 0xFF])` is
guaranteed to differ from the original in exactly one byte, whatever the original was. Writing any fixed byte (even a
different constant) leaves the same 1-in-256 hole. XOR is preferred over incrementing or subtracting because it needs
no modulo arithmetic and no branch at the byte boundary.

**Extract the corruption into a module-level helper.** The test and the regression test must exercise the same
corruption, otherwise the regression test can pass while the flaky test is still broken. A pure `bytes -> bytes`
function keeps the property testable without a deploy, and keeps the `payload_runner` wrapper thin.

**Assert the difference where the bytes are produced.** The wrapper compares the damaged bytes with the payload it
read and fails with an explicit message before calling the real runner. A no-op corruption then fails as a broken
test, not as "the server accepted the upload", which is the misleading shape of the original flake.

**Cover all 256 values end to end, with a request id per value.** A pure helper loop would not prove the server still
refuses the corrupted payload. The regression test runs `self.deploy(request_id=f"req-{final}", sequence=1000 + final)`
for each value, so each attempt is independent and the existing idempotency check does not interfere. The loop takes
about 0.1 seconds on this machine, so it stays in the unit suite.

**Construct the payload directly instead of patching `os.urandom`.** `elf(body=b"\0" * 2047 + bytes([final]))` produces
the same final byte with no global patch to restore, so a failure cannot leak a fake random source into other tests.

## Risks / Trade-offs

- [256 deploys in one test could be slow or leave state behind] -> Measured at ~0.1 s; every attempt is rejected before
  a job or release is recorded, and the existing `setUp` temporary directory is cleaned up by the test case.
- [A future edit changes the corruption back to a constant] -> The helper loop fails on the value that matches that
  constant, and the end-to-end loop fails on the same value, so the hole cannot return silently.
- [Unique request ids hide a real idempotency problem] -> Idempotency is covered by its own tests; this test is only
  about checksum rejection and must not be coupled to request-id reuse.
