# Spec Delta

## Purpose

Defines how Shiplord bounds the output it captures from host commands: the cap, which bytes survive truncation, the
marker that announces dropped output, and the sanitization applied before the text is returned.

## ADDED Requirements

### Requirement: Captured output is capped

Captured command output SHALL be capped at the `max_output` limit requested by the caller, so a runaway command cannot
exhaust memory or flood a client. The returned text SHALL NOT exceed `max_output` characters, and the cap applies to
the sanitized text the caller receives, not only to the raw bytes buffered.

#### Scenario: A command that prints more than the cap

- **WHEN** a command writes more than `max_output` bytes and exits successfully
- **THEN** the returned output is at most `max_output` characters long and the exit code is unchanged

#### Scenario: A zero cap

- **WHEN** a caller requests `max_output` of zero
- **THEN** the returned output is empty and no command bytes are echoed back

### Requirement: Truncation keeps the newest output

When a command produces more than `max_output` bytes, the returned text SHALL end with the final bytes the command
wrote, and the oldest bytes SHALL be the ones dropped. A capture cut inside a multi-byte UTF-8 sequence SHALL decode
with a replacement character instead of failing.

#### Scenario: The tail of a long capture survives

- **WHEN** a command writes 300000 bytes ending with `END` and `max_output` is 4096
- **THEN** the returned output ends with `END`

#### Scenario: A cut multi-byte character

- **WHEN** the retained window begins in the middle of a multi-byte UTF-8 character
- **THEN** the returned text decodes with a replacement character and the command still reports its own exit code

### Requirement: Truncated output announces the dropped output

When any bytes were dropped, the returned text SHALL begin with a single ASCII marker line stating that earlier output
was truncated, so a truncated capture cannot be mistaken for a complete one. The marker SHALL count against the cap,
and it SHALL be absent when nothing was dropped.

#### Scenario: A truncated capture is labelled

- **WHEN** the output was truncated
- **THEN** the first line of the returned text says earlier output was dropped

#### Scenario: A complete capture is not labelled

- **WHEN** the command produced fewer than `max_output` bytes
- **THEN** the returned text is the command's own output with no marker added

### Requirement: Returned output is stripped of control characters

The returned text SHALL preserve newlines and printable characters and replace every other character with `?`, so a
command cannot inject terminal control sequences into Shiplord's own output.

#### Scenario: An ANSI escape in the output

- **WHEN** a command prints an ANSI escape sequence
- **THEN** the escape character appears as `?` in the returned text

### Requirement: Application log tails end with the newest lines

Application log retrieval SHALL return the newest captured lines when the log output exceeds the log cap, for both
container logs and native service logs.

#### Scenario: A noisy container

- **WHEN** `docker logs` emits more than the log cap and the last line records the failure
- **THEN** the log text returned to the client ends with that last line

#### Scenario: A noisy native service

- **WHEN** the journal for a native service emits more than the log cap and the last line records the failure
- **THEN** the log text returned to the client ends with that last line
