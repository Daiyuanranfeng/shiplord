# Design

## Context

See `proposal.md` - Why. `DockerRuntime` has three ways into a container:

- `prepare(registration, release)` - the locked verification. The worker has already pre-pulled the candidate
  (`worker.run_job` -> `DockerRuntime.pull`), so `prepare` normally only inspects a local image. When the image is
  absent it fetches it once, with the application's login.
- `_start(...)` - reached by `start`, `start_with_state`, and `start_with_provenance`. `Deployer._transition` uses it
  for a candidate after `prepare`; `Deployer._reconcile` uses it for a restore after a failed candidate and for
  `shiplord recover`. It stopped whatever ran for the application and then ran `docker run <image>` with no pull and
  no login.
- `discard` / `logs` / `release_logs` - unaffected.

The gap is `_start`: Docker's implicit pull during `docker run` is anonymous, so a private image that host maintenance
pruned cannot be started again. `layout.registry_logins/<application_id>/config.json` exists (written by
`provision.manage_registry`, mode 0600 in a 0700 root-owned directory) and is passed to `docker pull` as
`--config <dir>`; it is usable as-is. The runtime runs as root, so no ownership change is needed.

## Goals / Non-Goals

**Goals:**

- A restore, a recovery, or any other start of a release whose image is missing fetches it with the application's own
  login, before anything is stopped.
- A start that fails to fetch leaves the previous release serving and says where the image comes from.
- No extra registry round trip on the normal deploy path.
- One pull implementation, so the login selection and disk-space check cannot drift between `prepare` and a start.

**Non-Goals:**

- Changing `prepare`'s messages, ordering, or the worker's pre-pull.
- Changing outcomes, exit codes, journal phases, or `rolled_back` / `intervention_required` semantics.
- Fetching for a native release: an upload is local, not a registry pull.
- Retrying a failed pull, or falling back to another digest or tag.

## Decisions

### One `_pull` helper behind `pull`, `prepare`, and the start path

`_pull(registration, image, phase="prepare", failure="")` holds the disk-space check, the login selection
(`--config <login>` when the login file exists) and `docker pull --quiet <image>` with `PULL_TIMEOUT_SECONDS`.

- `pull(registration, release)` - the worker's pre-pull and `prepare`'s fallback - keeps its signature and its message
  ("the image could not be pulled") and its output detail, so `prepare` behaves exactly as before.
- `_ensure_image(registration, image)` - the start path - calls `_pull` with `phase="start"` and a message naming the
  registry, which makes `_pull` raise without the raw output.

The disk-space refusal stays `_require_free_disk`'s own message ("not enough free disk space to pull an image ... ;
nothing was changed"), which is true on both paths because the fetch happens before anything is touched. It gains the
phase of its caller, so a start reports `start` rather than `prepare`.

Alternative considered: a separate start-path pull. Rejected: two copies of the login selection and the disk check is
exactly how the restore path fell behind `prepare` in the first place.

Alternative considered: reusing `prepare` inside `_start`. Rejected: `prepare` also verifies platform, revision label,
`STOPSIGNAL`, and volume provenance, and those are the locked transaction's job. A restore target is already recorded
and validated; re-running the whole verification would change `prepare`'s meaning and duplicate checks in a place that
must not fail for a reason the committed release never had.

### The start checks locally, then fetches, then stops

`_start` gains one step after the environment file is read and before the volume is created or any container is
stopped:

```
read the environment file        (fails before anything is touched)
ensure the image is on the host  (inspect; pull only when missing)
check/accept the volume
stop and remove the previous container
docker run
```

`docker image inspect --format {{.Id}} <image>` is the presence check. It is a local daemon call, so a present image
never costs a registry round trip; the normal deploy path already pulled the digest in `prepare`.

The fetch is before `_remove_containers`, which is the whole point: a start that must pull keeps the previous release
serving while the bytes arrive, and a failed pull leaves it running. It is also before `check_volume(create=True)` so
the order is "validate, then create", and a nearly full disk refuses before the volume exists.

Alternative considered: pulling between `check_volume` and `_remove_containers`. Rejected: it would create the volume
before the disk-space check that the pull performs.

### The failure names the registry and drops the output

`_registry(image)` returns the part of the reference before the first `/` (`ghcr.io`, `localhost:5000`). A failed
start-path fetch raises:

```
the release's image could not be fetched from ghcr.io; the previous release is still running
```

with `phase="start"`, no `detail`, and no `kind`. Raw pull output is deliberately dropped: `Deployer._describe` copies
`detail` into the job result, and a failed pull can echo what it was given. `prepare` keeps its output detail because
that is its existing behaviour and the invariant for this change is about the new start-path failure.

No `kind` is set, so nothing about `intervention_required` changes. A restore that cannot fetch goes through
`Deployer._restore` exactly as before: `_reconcile` raises, `_restore` reports `intervention_required` with
`restore_error`, the journal is kept, and the operator runs `shiplord recover` once the image can be fetched.

Alternative considered: mapping the failure to a new `prepare_failed`-style outcome. Rejected: it would change exit
codes, and the transaction has already stopped the candidate by then - the honest outcome is the existing one.

## Risks / Trade-offs

- [An extra `docker image inspect` on every start] -> One local daemon call, no network. Accepted for a restore that
  works after an image prune.
- [A daemon that is down reports "could not be fetched from <registry>"] -> The message says the image could not be
  fetched and that the previous release still runs, which is true; the daemon's own error is not repeated. Accepted
  because the alternative is pasting output that can carry a login.
- [The start path and `prepare` can both fetch the same image in one transaction] -> Only if the image is pruned
  between `prepare` and `_start`, which is not a case worth a second code path; the second fetch uses the same login.
- [A pull inside `_start` runs while the host lock is held] -> Only when the image is missing, which is the case that
  cannot work at all today. The worker's pre-pull still covers the normal deploy path.

## Migration Plan

No migration. The image is the same pinned digest with the same login; only the place that may fetch it grows. The
real-host Docker suite must be run, because this is runtime behaviour against a real daemon.
