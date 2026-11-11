# Tasks

## 1. One definition of what a Docker start consumes

- [x] 1.1 Extract `_managed_environment(registration, configuration)` and make `_start` build its `--env` arguments
      from it, and verify the existing `StartTests` managed-environment assertions still pass unchanged
- [x] 1.2 Move the fixed run values (pids limit, restart policy, log driver and options, security options, dropped
      capabilities) to module constants used by `_start`, and verify the existing start argv test still asserts the
      same `docker run` values
- [x] 1.3 Extend the `is_running` inspect template with memory, memory-swap, pids limit, restart policy, security
      options, dropped capabilities, log driver/options, port bindings, and `.Config.Env`, parsing environment last
      with a bounded split, and verify a unit test covers each field's mismatch
- [x] 1.4 Compare the environment as a subset of managed plus custom keys, and verify a test asserts a changed managed
      value and a changed custom value both make the release not running while image-declared keys are tolerated
- [x] 1.5 Verify a test asserts an unreadable environment file does not report a matching release as down and does
      not raise, while a changed record-derived value is still a mismatch, and that no environment value appears in
      the result

## 2. A native release is running only with its committed unit and environment

- [x] 2.1 Compare the installed unit bytes with `render_unit(...)` and the installed environment bytes with
      `render_environment(...)` using `load_environment_file`, and verify a test asserts a changed memory limit, a
      changed socket bind, and changed origins each make `is_running` return `False`
- [x] 2.2 Verify a test asserts an unchanged release is still reported running, and that an unreadable environment
      file neither raises nor hides a changed unit

## 3. Real-host evidence: the committed configuration is what runs after a rollback

- [x] 3.1 Add a Docker real-host test that deploys a release, redeploys the same commit/image/sequence with a changed
      memory limit, container port, and origins, forces the routing step to fail, and asserts `docker inspect` memory,
      the loopback port binding, and `ALLOW_ORIGINS` are the committed configuration
- [x] 3.2 Add a systemd real-host test that does the same through the real CLI with a planted interrupted route, and
      asserts the unit's `MemoryMax`, `SocketBindAllow`, and the environment file's `ALLOW_ORIGINS`
- [x] 3.3 Prove both are regression tests: with the old `is_running`, the new Docker test fails on the restored
      memory/port/origins and the new systemd test fails on the restored unit/environment
- [x] 3.4 Run `tests/linux/run.sh docker` and record the real result
- [x] 3.5 Run `tests/linux/run.sh systemd` and record the real result

## 4. Verification

- [x] 4.1 Run `.venv/bin/python -m unittest discover -s tests` and verify the summary is `OK`
- [x] 4.2 Run `openspec validate restart-on-configuration-change --strict` and verify it passes
