# Design

## Context

See proposal.md for motivation. The facts from the current code that shape the approach:

- `installer._install_sshd` writes the drop-in, runs `sshd -t`, then reloads the hard-coded unit
  `ssh.service`. It returns early, without any verification, when the drop-in already matches.
- `layout.SSHD_DROPIN` is `/etc/ssh/sshd_config.d/50-shiplord.conf`, so the main configuration and the
  drop-in directory are fixed siblings. `Layout.prefix` maps all of them under a temporary root in tests.
- The doctor already reads the drop-in and runs `sshd -t`; it never asks whether sshd reads the drop-in.
- `System.run` returns a capped `CommandResult` and raises `CommandError` when the command cannot be run.
- Every module the server runs must be stdlib-only; `installer.py` and `serverdoctor.py` are both shipped.

## Goals / Non-Goals

**Goals:**

- One discovery result shared by bootstrap and the doctor, so both agree on the unit.
- A read-only preflight (unit discovery plus include verification) that runs before the drop-in is written.
- Verification on every bootstrap, including one that changes nothing.
- No new dependency, no change to the bootstrap result shape, and no host path in a refusal.

**Non-Goals:**

- Replacing `sshd -t` or adding an `sshd -T` policy probe; the include check plus `sshd -t` is the contract
  the issue asks for.
- Discovering sshd on a host without systemd (Shiplord's baseline is a systemd host).
- Changing the drop-in contents, the forced-command dispatcher, or the deployment-account model.

## Decisions

**Discover with one `systemctl list-unit-files` call and filter in Python.** The four candidate names are
passed as patterns, so the output is a handful of lines (the `System` layer caps output at 4096 bytes, and an
unfiltered listing would be truncated before reaching the `ssh*` entries). The first column is the unit file
name; a missing unit simply does not appear. Alternatives considered: `systemctl cat` per candidate (up to
four processes, and its non-zero exit has to be told apart from a real failure) and `systemctl show
--property=LoadState` per candidate (same cost, and relies on the stub-unit output for unknown names).
`list-unit-files` has a documented, stable tabular format and a single non-zero exit that means "no match".

**Prefer `ssh.service`, then `sshd.service`.** On Debian-derived hosts the two names are aliases of the same
unit, so the choice is cosmetic; preferring the name the current code already used keeps the existing
behaviour for those hosts. A host with only `sshd.service` now reloads that unit.

**Verify the include by parsing, not by globbing the directory.** On a first bootstrap the drop-in file does
not exist yet, so `glob` would never match it. Instead each `Include` pattern is compared component by
component with `fnmatch` against the drop-in's absolute path: `*` does not cross a separator, so
`/etc/ssh/*` does not falsely match `/etc/ssh/sshd_config.d/50-shiplord.conf`. `Include` lines are read from
the main configuration and from files it includes (recursively, depth-limited, with a visited set), because a
distribution may reach the drop-in directory through an intermediate file. Relative patterns resolve against
the main configuration's directory, which is what sshd does.

**Verify before writing, and verify again on an unchanged run.** The preflight is read-only, so a host that
cannot be verified is refused before the drop-in exists. It also runs before the early "already current"
return: the host's `sshd_config` can lose its `Include` between two bootstraps, and reporting "nothing
changed" in that state is exactly the false success the issue describes. The cost is one `systemctl` call per
bootstrap, which is a rare root operation.

**Skip the restart only for the socket-activated, service-inactive case.** With `Accept=yes` socket
activation the service unit is not running and is in conflict with the socket, so `try-reload-or-restart`
would fail or start a competing listener; the next connection starts a fresh `sshd` that reads the new
configuration. When the service is running, the discovered unit is reloaded exactly as before. Two
`is-active --quiet` probes are only made when the drop-in actually changed.

**Report the include fact in the doctor's existing `sshd` check.** The doctor gains `unit` and
`dropin_included` keys next to `dropin_current` and `config_valid`, and `ok` requires both the unit and the
include. No new check name is introduced, so the CLI and the e2e assertions that iterate the check names keep
working.

**Name the unit and the missing line, never a path.** The refusal reads: the drop-in is not included, add
`Include sshd_config.d/*.conf`. The relative form is what sshd resolves against its configuration directory,
so it is the exact line to add, and it carries no absolute host path. The unit refusal names
`ssh.service` and `sshd.service` only.

## Risks / Trade-offs

- [The `list-unit-files` tabular format is assumed stable] -> Only the first column is read, and the unit
  names are exact; a format change would make discovery fail loudly (a refusal), never silently succeed.
- [A parser can miss an exotic `Include` form] -> Patterns are matched component-wise with glob semantics and
  followed recursively; anything the parser cannot see is reported as "not included", which is a refusal,
  not a false success. The operator's fix is the line the error names.
- [An `Include` that is present but never reached (inside an unmatched `Match` block) still passes] -> The
  check answers "is the drop-in read at all", which is what the issue requires; `sshd -t` still validates the
  file, and the real-host suite proves the `Match` block takes effect for deployment accounts.
- [Skipping the restart on socket activation delays the change until the next connection] -> That is the
  correct behaviour for `Accept=yes`; each connection already gets a new `sshd` process.
- [Discovery runs on every bootstrap, adding a command to an idempotent run] -> It is one read-only command
  on a rare root operation, and it is the only way to notice a removed `Include`.

## Migration Plan

No migration. A host that was bootstrapping successfully keeps doing so; a host that was silently
unprotected now fails bootstrap with the line to add. Rollback is reverting the installer and doctor change.

## Open Questions

None. The real-host evidence (`tests/linux/run.sh host`) could not be executed on this machine because Docker
is unavailable; it is recorded as unverified in the pull request.
