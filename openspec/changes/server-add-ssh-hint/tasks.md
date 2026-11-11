# Tasks

## 1. Capture and classify the SSH failure

- [x] 1.1 Capture the check's stderr into a temporary file and read at most its newest 4096 bytes; verify an existing
      `server add` test still passes and the captured buffer is never printed raw.
- [x] 1.2 Add the host-trust line allowlist and the changed-host-key marker to `servers.py`, and build the failure
      message from them; verify with unit tests that only allowlisted lines survive sanitization.
- [x] 1.3 Raise `SSHCheckError` with the host-trust message, including `ssh <validated target>` exactly once, and keep
      the generic message for unrecognised stderr; verify `tests/test_servers.py` covers both paths.

## 2. CLI messages

- [x] 2.1 Replace the `server add` success message so it no longer claims CI host-key setup is unimplemented; verify
      `tests/test_servers.py` asserts `Run \`shiplord server bootstrap\` next` and the absence of the old claim.

## 3. Tests

- [x] 3.1 Extend the fake `ssh` in `tests/test_servers.py` to write controlled stderr and assert the first-time host
      key hint uses the real target once and exits 3.
- [x] 3.2 Add coverage for a changed host key (no `ssh` hint, no known-hosts path) and for an unrecognised failure
      (generic guidance, no raw output); verify each test fails before the fix.
- [x] 3.3 Run `python -m unittest discover -s tests` and verify the suite is OK with no new skips; record that
      `tests/linux/run.sh` cannot run because Docker is unavailable on this machine.

## Workflow follow-up

- Archive the change after review.
