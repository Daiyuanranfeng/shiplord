# Design

## Context

See `proposal.md` - Why. Two facts shape the approach:

- `run_command` is the single place that captures command output; `HostSystem.run` is the only production `System`, and
  `runtime.logs` / `native.logs` reach it through `system.run(..., max_output=MAX_LOG_BYTES)`. Neither log path does
  any truncation of its own.
- `CommandResult.output` is documented as "capped and stripped of control characters", and the output of *every*
  command - not just logs - flows through the same buffer. Some callers parse that text (`docker ps -aq` is split on
  whitespace, `systemctl show` is split on `=`).

## Goals / Non-Goals

**Goals:**

- Keep the newest bytes when the cap is exceeded, with bounded memory regardless of how much the command writes.
- Make truncation visible in the returned text, without breaking the cap or the `CommandResult` contract.
- Cover both log paths with evidence, not only the shared runner.

**Non-Goals:**

- Changing `MAX_OUTPUT_BYTES`, `MAX_LOG_BYTES`, `MAX_LOG_LINES`, or the `--tail` contract.
- Adding a caller-visible option to choose head- or tail-keeping.
- Changing how the CLI renders or counts log lines.
- Making the marker machine-readable or adding a structured "truncated" field to `CommandResult`.

## Decisions

**Keep a rolling tail in the read loop, not a slice at the end.** After each `os.read`, the buffer is trimmed back to
`max_output` from the left and a `truncated` flag is set. The alternative - buffer everything and slice once - would let
a runaway command consume memory proportional to its output, which is exactly what the cap exists to prevent. The trim
is `del kept[: len(kept) - max_output]`, bounded by `max_output` plus one 64 KiB read, so its cost is negligible.

**Truncation happens at byte level, sanitization at text level.** The buffer stores raw bytes; `sanitize` decodes the
retained window with `errors="replace"` and then replaces non-printable characters. A window that starts mid-character
therefore yields U+FFFD (printable, so it survives) instead of raising, which keeps the existing decode behaviour.

**The marker is a single ASCII first line, counted against the cap.** `sanitize` gains a keyword `truncated`; when set
it prepends `[shiplord: earlier output truncated]\n` and shortens the byte budget by the marker's length, so the
returned text is still at most `max_output` characters (UTF-8 decoding never yields more characters than bytes).
Alternatives rejected: no marker (the complaint in the issue is that truncated output looks complete), and a trailing
marker (it would displace the newest line and put the annotation in the wrong place).

**Fix the shared runner, not the two log call sites.** Both log paths already pass `max_output=MAX_LOG_BYTES`; changing
`run_command` fixes them without touching `runtime.py` or `native.py`. A log-only flag would leave the same silent
truncation in every other capture and would put log knowledge in the generic runner.

**Guard the zero/negative cap explicitly.** Slicing with `data[-0:]` returns the whole buffer, so the retained window is
computed as `data[len(data) - size:]` with `size` clamped to at least zero.

## Risks / Trade-offs

- [A marker line appears in output that other code parses] -> Only when a capture exceeds its cap, where the parsed
  value was already wrong; the marker makes that visible rather than silent. Command outputs that are parsed today are
  small (`ps -aq`, `systemctl show`, `inspect` fields).
- [`shiplord logs` counts the marker as a log line in its "retrieved N lines" notice] -> Accepted: the marker is real
  text in the output being counted, and it tells the operator the tail is incomplete.
- [The retained window may start mid-character, so the first character is a replacement character] -> Accepted and
  specified; the alternative is dropping a whole valid character to align the boundary.
- [Tests that asserted the first bytes survive truncation change meaning] -> Updated to assert the trailing bytes and
  the marker instead; the control-character test is made small enough not to truncate so it keeps testing sanitization
  alone.

## Migration Plan

None required: no protocol, manifest, exit-code, or CLI-argument change. Rolling back restores the previous head-keeping
behaviour with no state to clean up.
