# Proposal

## Why

The helper is started as `/usr/bin/python3` and imports `tomllib`, which exists only in Python 3.11 and
later. On Ubuntu 22.04 (3.10) and Debian 11 (3.9) the installer dies on its own import, before it can
write a response frame, so `shiplord server bootstrap` reports `the server did not return a valid
response`. That message names neither Python nor a version, and the operator has no way to know what to
install or that the host is unsupported.

## What Changes

- `shiplord server bootstrap` probes `/usr/bin/python3` on the server over the same SSH connection it
  will upload through, before the helper archive is built, uploaded, or switched.
- A server whose interpreter is missing, does not answer, or is older than 3.11 is refused with exit code
  3 and a message that names the interpreter path, the required version, and why the helper needs it.
- The interpreter path and the required version become one definition in `installer.py`, used by the
  probe, by the `sudo` install command, and by the installed launcher wrappers, so they cannot drift.
- `docs/guide.md` states the server requirement next to the operating systems Shiplord supports.
- The existing protocol-version check, its exit codes, and the upload/cleanup flow are unchanged.

## Capabilities

### New Capabilities

- `server-bootstrap`: checking a server's prerequisites and installing, switching and wiring the helper,
  including the rule that a host whose interpreter cannot run the helper is refused before anything is
  uploaded or changed.

### Modified Capabilities

None; the project has no specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/client.py`: `bootstrap_server` gains the pre-upload probe.
- `src/shiplord/installer.py`: `SERVER_PYTHON`, `REQUIRED_PYTHON` and a pure version check, reused by
  `_install_wrappers`.
- `docs/guide.md`: a requirements section naming the server's Python.
- `tests/test_client.py`, `tests/test_installer.py`: coverage for an old interpreter, a missing one, and
  for the probe running before the upload.
- No manifest, protocol, CLI flag, or exit-code change. `manifest.py` keeps its `tomllib` import.
