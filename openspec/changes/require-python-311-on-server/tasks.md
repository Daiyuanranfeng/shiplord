# Tasks

## 1. Prove the defect

- [x] 1.1 Add `tests/test_client.py::BootstrapTests` cases where the probe answers `3 10` and where the interpreter is missing (`b"", b"", 127`), asserting `ClientError` with exit code 3, a message naming `/usr/bin/python3` and `Python 3.11 or later`, exactly one SSH call, and an empty payload on that call; run `.venv/bin/python -m unittest tests.test_client.BootstrapTests` and confirm the new cases fail with the current code
- [x] 1.2 Add `tests/test_installer.py` cases for the pure version check: `3.10`, `2.7`, an empty answer, `3`, and free text are unsupported; `3.11`, `3.12` and `4.0` are supported; run `.venv/bin/python -m unittest tests.test_installer` and confirm the new cases fail with the current code

## 2. Refuse the server before the helper is touched

- [x] 2.1 Add `SERVER_PYTHON`, `REQUIRED_PYTHON` and `PYTHON_PROBE` to `src/shiplord/installer.py`, plus a pure `python_supported(answer)` that parses the probe's `major.minor` answer, and make `_install_wrappers` render `SERVER_PYTHON`; verify `tests/test_installer.py` still expects the same wrapper text and the new version cases pass
- [x] 2.2 Add the probe to `bootstrap_server` in `src/shiplord/client.py`, ahead of `package_helper`, over `ssh_command` with the upload's own connection settings, raising `ClientError(..., 3)` with the path, the requirement and the parsed version; verify task 1.1 now passes
- [x] 2.3 Use `SERVER_PYTHON` in the `sudo` install command in `bootstrap_server` so the probe and the installer cannot check different paths; verify `tests/test_client.py::BootstrapTests` still asserts the `sudo -n /usr/bin/python3 -I -S -B -c ` prefix and passes
- [x] 2.4 Add a `tests/test_client.py` case that a server reporting `3.11` or later still uploads, installs and cleans up, and that the probe is the first SSH call with no payload; run `.venv/bin/python -m unittest tests.test_client` and confirm it passes

## 3. Document the server requirement

- [x] 3.1 Add a requirements section to `docs/guide.md` next to the supported operating systems, naming Python 3.11 or later at `/usr/bin/python3`, the two images that are too old (Ubuntu 22.04, Debian 11), and that bootstrap checks it before uploading; verify the section reads correctly against `docs/operations.md` and contains no unverified claim

## 4. Confirm nothing else moved

- [x] 4.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm `OK` with 77 skipped: the baseline 951 tests plus the nine added here, two of which are the real-host cases that skip without `SHIPLORD_TEST_HOST`
- [x] 4.2 Run `tests/linux/run.sh host` and confirm the real bootstrap, provisioning and forced-command suite still passes with the interpreter check in place
- [x] 4.3 Run `.venv/bin/python -m unittest tests.test_client tests.test_installer tests.test_cli` and confirm the bootstrap, installer and CLI paths pass together

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
