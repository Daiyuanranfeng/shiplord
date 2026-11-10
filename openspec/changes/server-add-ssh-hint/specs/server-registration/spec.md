# Spec Delta

## Purpose

Registers a server alias on this machine only after proving that the supplied SSH target already has a trusted host
key and a working login, and tells the operator exactly what to do when that proof fails.

## ADDED Requirements

### Requirement: An alias is saved only after a trusted SSH check

`server add` SHALL run a non-interactive SSH login check against the target the operator supplied and SHALL save the
alias only when that check exits zero. A failed check SHALL leave no alias file behind.

#### Scenario: A trusted login is saved

- **WHEN** the SSH check exits zero
- **THEN** the alias is saved and the command exits zero

#### Scenario: A failed check saves nothing

- **WHEN** the SSH check exits non-zero
- **THEN** the command exits 3 and no alias file is created

### Requirement: An untrusted host key names the command to run

When the SSH check fails because the host key is not known yet, the error SHALL print the exact `ssh user@host`
command for the target the operator supplied, exactly once, and SHALL say to retry `server add` after accepting the
key.

#### Scenario: A first-time host key

- **WHEN** the SSH check fails with `No ED25519 host key is known for ...` and `Host key verification failed.`
- **THEN** the error contains the command `ssh appuser@ubuntu-4gb-hel1-1` once and names no other target

### Requirement: Only host-trust lines of the SSH error are repeated

The error SHALL preserve the OpenSSH lines that describe the host-trust problem, and SHALL NOT repeat any other
captured stderr, so a key, a token, a password, or a local filesystem path cannot reach the terminal.

#### Scenario: Unrelated stderr is withheld

- **WHEN** the SSH check fails with a host-trust line followed by output containing a local file path
- **THEN** the error contains the host-trust line and not the path

### Requirement: A changed host key is not presented as a first-time key

When the captured stderr reports that the remote host identification has changed, the error SHALL say the host key
changed, SHALL NOT suggest running `ssh user@host` as the remedy, and SHALL NOT quote the known-hosts path.

#### Scenario: A host key that no longer matches

- **WHEN** the SSH check fails with `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED`
- **THEN** the error reports a changed host key and contains no `ssh ` hint and no known-hosts path

### Requirement: Unrecognised SSH failures keep the generic guidance

When the captured stderr is not recognisable as a host-trust failure, the error SHALL keep the existing generic
guidance instead of echoing the raw output.

#### Scenario: A refused login

- **WHEN** the SSH check fails with `Permission denied (publickey).`
- **THEN** the error tells the operator to verify host trust, login, and any locked SSH key

### Requirement: The success message names the next command

A successful `server add` SHALL tell the operator to run `shiplord server bootstrap` next and SHALL NOT claim that CI
host-key setup is unimplemented.

#### Scenario: Registration succeeds

- **WHEN** the alias is saved
- **THEN** the success message mentions `shiplord server bootstrap` and does not mention unimplemented CI setup
