# Tasks

## 1. Prove the defect

- [x] 1.1 Add failing tests in `tests/test_installer.py` that a host whose only unit is `sshd.service` is reloaded through `sshd.service`, that no unit is a clear refusal, and that a main config without an `Include` reaching the drop-in is refused; run `.venv/bin/python -m unittest tests.test_installer` and confirm they fail against the current code
- [x] 1.2 Add a failing test that a second bootstrap refuses once the `Include` line has been removed from the main config, even though the drop-in is already current
- [x] 1.3 Add a failing test in `tests/test_serverdoctor.py` that the `sshd` check reports the discovered unit and fails when the drop-in is current but not included

## 2. Discover the unit and verify the include

- [x] 2.1 In `src/shiplord/installer.py`, add sshd unit discovery over `ssh.service`/`sshd.service` and the matching socket names using one `systemctl list-unit-files` call; verify with the unit-name tests from 1.1 and confirm no unit name is hard-coded in the reload path
- [x] 2.2 Add the recursive `Include` check that matches the drop-in path component-wise with `fnmatch` and resolves relative patterns against the main configuration directory; verify with the include tests from 1.1, including a nested include and a decoy directory
- [x] 2.3 Run the preflight before the drop-in is written and before the "already current" early return, so a host that cannot be verified is refused before any file is written; verify with 1.2 and by asserting the drop-in does not exist after a refusal
- [x] 2.4 Reload through the discovered unit, and skip the restart only when a socket is active and the service is not; verify with the socket-activation tests from 1.1
- [x] 2.5 Assert the refusals name the unit or the include fact and contain no absolute path; verify by checking the error text against the layout prefix

## 3. Report it in the doctor

- [x] 3.1 Add `unit` and `dropin_included` to the `sshd` check in `src/shiplord/serverdoctor.py` and require both for `ok`; verify with the doctor tests from 1.3 and the existing healthy-server test

## 4. Real-host suite

- [x] 4.1 Teach the host suite's `systemctl` stub and `tests/linux/shims.sh` to answer unit discovery and `is-active`, so the e2e and host suites do not lose their bootstrap
- [x] 4.2 Add a `tests/test_host_integration.py` case that bootstraps once per unit name (`ssh.service`, `sshd.service`) and asserts the reload went through the discovered unit
- [x] 4.3 Run `.venv/bin/python -m unittest tests.test_host_integration` and confirm it skips cleanly off a disposable host (the real run needs `tests/linux/run.sh host`, which needs Docker)

## 5. Confirm nothing else moved

- [x] 5.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is `OK` with 69 skipped (825 tests: the original 810 plus the 15 added here, one of them the skipped host case)
- [x] 5.2 Run `.venv/bin/python -m unittest tests.test_installer tests.test_serverdoctor` and confirm the drop-in contents, idempotent second bootstrap, and Caddy-before-sshd ordering are unchanged

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
- Note in the pull request that `tests/linux/run.sh` was not run because Docker is unavailable on this host.
