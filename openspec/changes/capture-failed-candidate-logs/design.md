# Design

## Context

See `proposal.md` - Why. The relevant flow in `Deployer._restore`:

```
_restore(failure, probes, candidate, before)
  |
  +-> _reconcile(target, route)          # target None: stop_with_state -> _remove_containers
  |                                      # otherwise: _start -> _remove_containers, then docker run target
  +-> state.clear_after_reconciliation()
  +-> _abandon(candidate, before)        # runtime.discard -> docker image rm only
  +-> Outcome("rolled_back" | "failed")
```

The candidate's *container* is removed by `_reconcile`, not by `_abandon`: `discard` removes only the image. So the
capture must happen before `_reconcile`; anything after it can no longer read the container.

`runtime.logs(registration, tail)` is not usable for this: it lists `docker ps -aq` and reads the first container it
finds, which after a restore is the restored release and before one is ambiguous when two containers exist.

The outcome is persisted by the worker in a job record through `release_state._write`, which refuses a record larger
than `MAX_RECORD_BYTES` (65536). A capture bounded only by `MAX_LOG_BYTES` (262144) could make `jobs.finish` raise
after a successful rollback, losing the outcome entirely.

## Goals / Non-Goals

**Goals:**

- Read the failed candidate's own container by name, bounded, before any restore step removes it.
- Report the tail in the outcome detail with the tail size and byte bound visible.
- Keep a capture failure a note in the detail, never a restore failure.
- Keep `shiplord logs` and every status/exit-code contract unchanged.

**Non-Goals:**

- Capturing logs for a native candidate (systemd journal) or for `intervention_required` paths where nothing is
  removed yet.
- Redacting application log content. Shiplord has no list of environment values at this point (it deliberately does
  not read `app.env` in the deployer) and does not add any value itself; the capture is the application's own output.
- Changing `MAX_LOG_BYTES`, `MAX_LOG_LINES`, `logs`, or the job-record size limit.

## Decisions

### A by-name, bounded runtime read

`DockerRuntime.release_logs(registration, release, tail, *, max_bytes=MAX_LOG_BYTES)` runs
`docker logs --tail <tail> shiplord-<app>-<release-id>` with `max_output=max_bytes` and returns `None` when Docker says
the container does not exist, `""` for an existing container with no output, and the sanitized text otherwise. A
failure other than "no such container" raises `RuntimeFailure("the release's container logs could not be read", "logs")`.

Using the container name makes the read unambiguous and ties it to the release record. The default keeps the `logs`
bound; the deployer passes its own smaller bound. `sanitize` already keeps the newest bytes and prepends the
`[shiplord: earlier output truncated]` marker, so the capture inherits the log truncation discipline exactly.

The method is optional on the runtime (`getattr`), like `force_stop`, `forget_upload`, and `start_with_state`, so a
native runtime that has no container simply does not capture. The `Runtime` protocol is not extended, because a native
runtime cannot honour it.

### Capture before `_reconcile`, report in `_restore`

`_restore` calls `_capture_candidate_logs(candidate)` before its `try:` block and merges the result into the detail
immediately. This is the only point where the container still exists, and doing it before the `try` means a capture
error cannot be mistaken for a restore error. The helper catches `RuntimeFailure` and turns it into
`{"candidate_logs": {"tail": ..., "max_bytes": ..., "error": "<message>"}}`; a successful read produces the same object
with `"output"` instead. A `None` result (no container) adds nothing, and a non-Docker candidate adds nothing.

### Bound the stored tail to 16 KiB

`CANDIDATE_LOG_LINES = 100` and `CANDIDATE_LOG_BYTES = 16384`. The line tail is enough for a crash traceback; the byte
bound leaves the rest of the 64 KiB job record for the request, probes, and release identities even when every line is
long. The number is passed to the runtime read rather than trimming afterwards, so a noisy container never buffers
256 KiB on the way to a 16 KiB result. The detail states both numbers so a reader knows the tail is not complete.

Alternative rejected: capture at `MAX_LOG_BYTES` and let the job record refuse the write. That would turn a verified
rollback into an unrecorded job, which is worse than the bug.

## Risks / Trade-offs

- [A capture that fails is only a note, so an operator may still lose the crash output] -> The note is in the detail
  and names the failure, so it is visible rather than silent; the rollback result itself stays truthful.
- [16 KiB is smaller than the 256 KiB `logs` bound] -> The outcome must fit a job record; the tail size and byte bound
  are stated in the detail so no one mistakes it for a complete log.
- [Reading a running candidate's logs takes a moment under the host lock] -> `docker logs --tail 100` on a local
  container is a single fast call, made only on a failed deploy.
- [The candidate's logs may themselves contain values the application chose to print] -> Shiplord adds no environment
  value, and the capture is the application's own output, which is what the operator needs; redaction is out of scope
  and would require reading the environment file the deployer deliberately does not touch.

## Migration Plan

None required: no protocol, manifest, exit-code, or state change. An older client ignores the new detail field. Rolling
back the helper simply stops adding it.
