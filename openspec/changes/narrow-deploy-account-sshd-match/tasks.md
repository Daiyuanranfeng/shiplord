# Tasks

## 1. Narrow the sshd policy

- [x] 1.1 Replace the `Match User ship-*` criterion with `Match Group shiplord-deploy` and add
      `AuthorizedKeysCommand none` and `TrustedUserCAKeys none` to the block, keeping every existing restriction;
      verify the rendered text with a new exact-text test in `tests/test_installer.py`
- [x] 1.2 Export the group name as a module constant beside the template and confirm `tests/test_serverdoctor.py` and
      the existing installer tests still pass, since both compare the drop-in verbatim

## 2. Create and populate the group at bootstrap

- [x] 2.1 Create the group idempotently at bootstrap, reporting whether it changed; verify with a unit test that a
      second bootstrap reports no change and runs no `groupadd`
- [x] 2.2 Add every existing `shiplord-deploy-*` account from `getent passwd` to the group, skipping runtime accounts
      and foreign accounts; verify with a unit test that seeds a deployment account, a runtime account, and a human
      account and asserts only the deployment account is added
- [x] 2.3 Fail bootstrap with a clear, value-free message when the group or the membership cannot be established, and
      verify with a unit test that no drop-in is written in that case

## 3. Cover accounts provisioned after bootstrap

- [x] 3.1 Add a new deployment account to the group in `provision` before its key is installed, reporting the change;
      verify with a unit test that a provisioned account is a member and that a repeat provision changes nothing
- [x] 3.2 Verify that a membership failure stops provisioning before any key or registration is written, with a unit
      test in `tests/test_provision.py`

## 4. Real-host verification

- [x] 4.1 Extend `tests/test_host_integration.py` to assert the effective sshd configuration confines an account
      provisioned after bootstrap and leaves a human `ship-` account on the default policy; verify with
      `tests/linux/run.sh host`
- [x] 4.2 Add a host-suite case that starts an sshd with a trusted SSH CA and an `AuthorizedKeysCommand`, and asserts a
      CA certificate and a keys-command key are both refused for a deployment account while the registered key still
      reaches the forced command; verify with `tests/linux/run.sh host`
- [x] 4.3 Run `python -m unittest discover -s tests` and `tests/linux/run.sh host` and confirm both are green

## Workflow follow-up

- Archive the change after review.
