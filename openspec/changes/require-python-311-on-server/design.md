# Design

## Context

See proposal.md - Why. The facts from the current code that shape the approach:

- `bootstrap_server` (`client.py`) uploads a tar archive, then runs `sudo -n /usr/bin/python3 -I -S -B -c
  '...from shiplord.installer import main;main()'` with the unpacked directory as an argument.
- `installer.py` imports `layout.py`, which imports `manifest.py`, which imports `tomllib`. On Python 3.10
  the `from shiplord.installer import main` in that `-c` string raises `ModuleNotFoundError` before
  `installer.main` can run, so no response frame is written and the client reports the protocol-level
  `the server did not return a valid response` with exit code 3.
- The installed wrappers hard-code `/usr/bin/python3`, so the interpreter that runs the installer and the
  one that runs the helper afterwards are the same path on the same host.
- `server add` already proves that a login can run a command (`ssh target true`), so a failing probe is a
  fact about the interpreter, not about the login.

## Goals / Non-Goals

**Goals:**

- Turn the unnamed protocol failure into one message that names the interpreter path and the version.
- Run the check before the archive is built, uploaded, or switched, and before any bootstrap write.
- Keep one definition of the interpreter path and the required version.

**Non-Goals:**

- Making the helper run on Python 3.10 (a lazy `tomllib` import, or a TOML parser). The requirement is
  documentation and a clear refusal, not a compatibility layer.
- A second check inside `installer.bootstrap` or a version guard inside the `-c` string: on an old
  interpreter that guard would have to run before importing `shiplord`, duplicating the rule in shell
  quoting where it cannot be unit-tested. The client-side probe runs first and always.
- Checking Python in `doctor`. `doctor --server` calls the installed helper, which by definition already
  runs; it does not bootstrap.

## Decisions

**The check lives in `bootstrap_server`, not in the installer.** The installer is the process that cannot
start on an old interpreter, so it can never report the problem. The client is the only side that can both
run and write a frame, and `bootstrap_server` is the only entry point that uploads the helper.

**Probe with `-c 'import sys;print(sys.version_info[0],sys.version_info[1])'`, not `--version`.** The
answer is two integers with no prefix, no locale, and no dependence on which stream a Python version writes
its banner to; `--version` moved between stderr and stdout across major versions and has been localised.
The probe uses the same `-I -S -B` flags as the install command, so it proves the interpreter works under
exactly those flags. `%`-free and f-string-free formatting keeps the snippet valid on any Python 3.

**One definition of the path and the version, in `installer.py`.** `SERVER_PYTHON` and `REQUIRED_PYTHON`
live next to `helper_modules`, and both the probe and `_install_wrappers` use `SERVER_PYTHON`, so a future
change of interpreter path cannot leave the probe checking a different file than the wrapper runs. A
`python_supported(answer)` helper keeps the comparison a pure function that `tests/test_installer.py` can
pin without SSH.

**Fail closed on an unreadable answer.** A non-zero status, empty output, or anything that is not two
integers is a refusal. The alternative - continuing because the answer was merely odd - would reproduce the
original unnamed failure, and the message says the interpreter is missing or did not report a version.

**Exit code 3, no new `error_code`.** The CLI already maps exit 3 to "a prerequisite is missing: SSH trust,
credentials, permissions, sudo", which is what an unusable interpreter is. The protocol-version refusal
stays exit 4 and is unchanged; the two cannot be confused because the probe speaks no protocol. No
`error_code` is added, so `--json` consumers see the existing shape.

**The message names only the path, the requirement, and the parsed version.** The interpreter's answer is
parsed to `major.minor` before it is echoed, so arbitrary server output never reaches the operator, and no
temporary directory, home directory, or environment value is included.

## Risks / Trade-offs

- [One extra SSH round trip per bootstrap] → Bootstrap runs once per server change, and the probe shares
  the connection settings of the upload; the cost is a single command against the existing login.
- [A host that has Python 3.11 only outside `/usr/bin`] → Still refused, correctly: the installed wrappers
  hard-code `/usr/bin/python3`, so such a host would fail on the first helper call anyway.
- [A `python3` that is a wrapper script with unexpected output] → Refused as unreadable rather than guessed;
  the message says the interpreter did not report a version.
- [The probe's interpreter could differ from the one `sudo` runs] → Not possible in practice: the install
  command passes the absolute path `/usr/bin/python3`, not a `PATH` lookup.
