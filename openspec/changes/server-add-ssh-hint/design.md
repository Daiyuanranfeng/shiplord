# Design

## Context

See `proposal.md` - Why. Three facts shape the approach:

- `ssh_command` already pins `BatchMode=yes` and `StrictHostKeyChecking=yes`, so an unknown host key is a plain
  non-zero exit with a short, well-known OpenSSH message on stderr. Nothing in that path is interactive.
- `register_server` runs the check with `stderr=subprocess.DEVNULL`, which is why the only trace of the real cause
  is lost before the CLI can print it. `SSHCheckError` is rendered by `cli.server_add` as `error: <message>` and
  exit code 3.
- `system.sanitize` already defines the project's contract for captured output: newest bytes within a cap, UTF-8
  decoded with replacement, non-printable characters replaced by `?`. `servers.py` is a client-only module, so
  reusing it adds no server-side dependency.

## Goals / Non-Goals

**Goals:**

- Keep the SSH stderr long enough to classify the failure, with bounded memory.
- Repeat only text that is provably a host-trust line, never the whole buffer.
- Keep the failure a single-line `error:` message, so the existing CLI rendering and exit codes are unchanged.

**Non-Goals:**

- Making `server add` repair known-hosts state, or adding a `--trust`/`--yes` flag.
- Adding JSON output, a new exit code, or a new command-line option.
- Recognising every SSH implementation; the allowlist targets OpenSSH, which `ssh_command` already assumes.

## Decisions

**Capture stderr into a temporary file, then read its tail.** `subprocess.run` cannot bound a `PIPE`, and a hostile or
broken server can write arbitrary amounts to stderr. The check writes to `tempfile.TemporaryFile`, seeks to the last
4096 bytes, and reads that window. The alternative - `stderr=subprocess.PIPE` and slice afterwards - is shorter but
buffers the whole stream in memory, which is exactly what the bounded-output work elsewhere in this project avoids.

**Reuse `system.sanitize` for the captured bytes.** It already caps, decodes, and replaces control characters, so an
ANSI escape in stderr cannot repaint the terminal and a cut multi-byte character cannot raise. A private copy in
`servers.py` would drift from that contract.

**Match whole host-trust lines against an allowlist.** The classifier strips each line and accepts only exact OpenSSH
phrasings: `Host key verification failed.`, `No <type> host key is known for <host> and you have requested strict
checking.`, `The authenticity of host ... can't be established.`, and `... key fingerprint is ...`. Everything else is
dropped. A denylist ("remove paths and keys") cannot be audited and would leak whatever it forgot; an allowlist can
only ever repeat a host name, a key type, or a fingerprint, which the issue names as the point of the message.

**Treat a changed host key as its own case.** `REMOTE HOST IDENTIFICATION HAS CHANGED` means the key no longer matches
what is already trusted. Running `ssh user@host` would fail the same way and would not clear it, so that branch says the
key changed and stops there, without the `ssh` hint and without the banner's `known_hosts:line` path. The remedy
(`ssh-keygen -R`) is deliberately left to the operator rather than guessed at from unparsed banner text.

**Interpolate the validated target into the hint.** `validate_target` has already restricted the target to `user@host`
with a conservative character set, so the target cannot carry whitespace, control characters, or shell metacharacters
into the message. The hint is built from that same value, not from a placeholder or from `ssh_command`'s argument list,
so it reads as the command the user can paste.

**Keep the generic message as the fallback.** An unrecognised failure (refused login, DNS failure, a non-OpenSSH ssh)
still gets the current "verify host trust, login, and any locked SSH key" text. This makes the change additive: the
only failures that change shape are the ones the classifier positively recognises.

## Risks / Trade-offs

- [OpenSSH changes its wording or the user's locale translates it] -> The classifier misses, and the generic message
  is printed; behaviour is no worse than today and never echoes unrecognised text.
- [A legitimate host name is embedded in the message] -> It is the host the operator just typed, and it was validated
  before use; the fingerprint is likewise public data.
- [The hint is long for a narrow terminal] -> It stays one line like the current error; a two-line layout would break
  the `error: <message>` shape that other failures use.
- [The temporary file needs a writable temp directory] -> Same requirement as the existing alias write path; a failure
  there is an `OSError` and keeps the current "SSH check could not be completed" message.

## Migration Plan

None required: no protocol, manifest, state, or exit-code change. Rolling back restores the discarded stderr and the
old success message with no state to clean up.
