# Tasks

## 1. Split the pull out of the locked verification

- [x] 1.1 Add `DockerRuntime.pull` that runs the disk-space check and the `docker pull --quiet` with the
      application's own login, and verify `tests/test_runtime.py` asserts it pulls by digest, uses that login, and
      reports a timed-out pull with its output
- [x] 1.2 Make `DockerRuntime.prepare` inspect the local image first and fetch the pinned digest only when it is
      absent, keeping the disk-space check, the platform and revision checks, and verify tests assert `prepare`
      issues no `pull` when the image is present and exactly one when it is absent
- [x] 1.3 Verify the disk-space test still asserts nothing is pulled and the running release is untouched

## 2. Pull before the lock in the worker

- [x] 2.1 Build the deployer before the lock and pull the release the job will need (deploy via `preflight`,
      rollback via the committed previous release, nothing for recover), and verify a test asserts the pull happened
      while a real `flock` probe shows the host lock free
- [x] 2.2 Keep the pull best effort so a failed pull is still reported by the locked transaction as
      `prepare_failed`, and verify a test asserts the deploy still proceeds when the pull fails
- [x] 2.3 Verify a refused deploy request pulls nothing, and that a rollback pulls the previous release
- [x] 2.4 Verify the lock still covers every state transition by probing the lock file at `begin`, `advance`,
      `record_commit`, and `clear_after_reconciliation`
- [x] 2.5 Update the busy-lock test to assert the image was already pulled when `host_busy` is reported

## 3. Read-only administration without the lock

- [x] 3.1 Add an enumerated `READ_ONLY_ACTIONS` set and take the host lock only for the other actions, and verify a
      test asserts `doctor` and `apps` never call `HostLock.acquire` while `provision`, `keys`, `environment`,
      `registry`, and `deprovision` do
- [x] 3.2 Verify a test asserts a read-only request is answered while another process holds the lock, and that the
      enumerated set covers `protocol.ADMIN_ACTIONS` exactly
- [x] 3.3 Verify the existing provisioning, serverdoctor, and host-integration admin tests still pass

## 4. Evidence for a deliberately slow pull

- [x] 4.1 Verify a slow pull cannot hold the host lock: `WorkerTests` runs the real worker in a thread with a real
      `flock`, blocks `runtime.pull` on an event, and probes the lock file from the test thread
- [x] 4.2 Verify a read-only admin action answers during that pull: `test_a_slow_pull_does_not_block_a_read_only_admin_action`
      calls `admin.run("apps")` from a second thread and fails if it has to wait for the pull
- [x] 4.3 Run `tests/linux/run.sh docker` and record the real result: `Ran 7 tests in 97.335s`, `OK`. It caught the
      design flaw first - `Deployer.deploy` called directly (as that suite does) never pre-pulls, so a `prepare` that
      refused to fetch failed 7/7. The inspect-first fallback above is the fix, and the suite passes with it.
- [ ] 4.4 NOT done: a slow registry proxy in `tests/test_deploy_integration.py`. A deterministic delay needs a
      forwarding proxy and a second registration, and the property it would prove - the worker does not hold the lock
      while pulling - is already probed against a real `flock` in 4.1/4.2. Recorded as a limitation, not claimed.

## 5. Verification

- [x] 5.1 Run `.venv/bin/python -m unittest discover -s tests` and verify the summary is `OK` with the new tests
- [x] 5.2 Run `openspec validate pull-image-outside-lock` and verify it passes
