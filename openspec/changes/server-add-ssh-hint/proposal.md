# Proposal

## Why

`shiplord server add` is the first command a new user runs, and its most common failure is an unknown host key:
Shiplord connects with `StrictHostKeyChecking=yes`, so OpenSSH refuses the login until the key is trusted. Today
`register_server` throws the SSH stderr away and prints "verify host trust", which tells a newcomer what is wrong
but not what to type. The success message compounds the dead end by claiming CI host-key setup is not implemented,
which is no longer true.

## What Changes

- A failed `server add` whose SSH stderr shows an untrusted host key prints the exact `ssh user@host` command to run
  once, using the target the user supplied, and says to retry afterwards.
- The underlying OpenSSH host-trust lines (for example `Host key verification failed.`) are preserved in that message,
  so the user can see what OpenSSH actually objected to.
- Only a small allowlist of host-trust lines is repeated, and a changed host key is reported without the "accept it
  once" hint and without quoting the known-hosts path, because running `ssh` again would not fix it.
- Any other SSH failure keeps the existing generic message instead of dumping raw stderr.
- The success message becomes `Server alias saved. Run \`shiplord server bootstrap\` next.` and no longer claims CI
  host-key setup is unimplemented.

## Capabilities

### New Capabilities

- `server-registration`: registering a server alias after verifying that the supplied SSH target already has a
  trusted host key and a working login, including the guidance printed when that check fails.

### Modified Capabilities

None; the project has no specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/servers.py`: `register_server` captures bounded SSH stderr and classifies host-trust failures.
- `src/shiplord/cli.py`: the `server add` success message only.
- `tests/test_servers.py`: new coverage for the host-trust hint, the changed-key case, the generic fallback, and the
  success message.
- No manifest, protocol, exit-code, or command-line changes: a failed check is still `SSHCheckError` and exit code 3.
