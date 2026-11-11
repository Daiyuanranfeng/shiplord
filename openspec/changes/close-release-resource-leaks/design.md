# Design

## Context

See `proposal.md` - Why. The five leaks live in three modules that run on the server and must stay stdlib-only.

- `Deployer._retire` and `Deployer._abandon` decide whether a discard would remove something a retained release still
  uses, by comparing the artifact mapping. That is right for Docker, where the artifact is the image, and wrong for
  native releases, where the artifact is a binary and the removed resource is a directory named for `release_id`.
- `_transition` prepares the artifact before `ReleaseState.begin`, and returns `journal_unavailable` without touching
  the artifact when the journal cannot be opened.
- `JobStore._prune` only counts `finished` records. `unit_name` embeds `request_id[:24]`.
- `deprovision` removes the state, environment, registry-login, release, and unit paths, but not the job directory or
  the managed route file.

The host lock is held for every transaction and for `deprovision`, and `ReleaseState`/`JobStore` already expose the
`dir_fd` and `O_NOFOLLOW` primitives this change reuses.

## Goals / Non-Goals

**Goals:**

- Remove only resources that are provably unreferenced, before or after a verified step.
- Keep the public result shapes and exit codes unchanged; only add a `warnings` list where a failure already returns a
  detail mapping, and `removed` keys that are reported only when the item existed.
- One regression test per leak, each failing before the change.
- Keep short job unit names byte-identical so a unit launched by an older helper is still found.

**Non-Goals:**

- Changing `runtime.py`/`native.py`. The runtime keeps one `discard` contract; the deployer decides what "the same
  resource" means.
- Reclaiming release directories that predate this change and are not reachable from state. There is no record that
  would let the helper prove they are unused.
- Reloading Caddy after `deprovision` removes a route file. `deprovision` has no Caddy command object, and the route
  file is what the issue names; the next Caddy reload no longer sees the file.
- Pruning job records on a timer. Pruning happens when a new job is created, which is the only moment the leak grows.

## Decisions

### Compare the removed resource, not the artifact, in the deployer

Add a small helper to `Deployer` that answers "would discarding this release remove a resource a retained release
still uses". A native release owns its `release_id` directory, so the comparison is by `release_id`; any other runtime
keeps the artifact comparison, which is the image. Both `_retire` and `_abandon` use it, so a failed candidate under a
new commit but the same binary is also cleaned up.

Alternative considered: add a `resource_id` method to each runtime. Rejected because it would change `runtime.py` and
`native.py`, which this change must not touch, and the deployer already imports `release_id`.

### Abandon a prepared artifact only when no journal references it

After a failed `begin`, read the journal: if one is present or unreadable, keep the artifact, because recovery may
need it and the rule is to never delete what the journal references. Otherwise call the existing `_abandon`, which
already refuses to remove a resource a retained release owns, and report its warnings in the outcome.

Alternative considered: always abandon. Rejected because `_write` can publish a journal and still raise ("may have
been published; recovery is required"), and deleting the candidate then would make recovery's cleanup report a
failure for a directory that is already gone.

### Treat a job record older than the launcher's runtime bound as interrupted

`SystemdLauncher` starts every worker with `RuntimeMaxSec=MAX_RUNTIME_SECONDS`, so a worker older than that bound plus
a grace margin cannot still be running. `JobStore._prune_interrupted` removes such queued or running records at the
start of `create`, before the request ID is looked up, so the ID is reusable. It also keeps the record when its
timestamp is missing or not an integer, which leaves the existing validation to reject it.

Alternative considered: pass the launcher's `is_active` into the store. Rejected because the store is constructed in
`services.py`, outside this change's scope, and a liveness callback would make pruning depend on systemd in tests.

### Hash the full request ID only when it does not fit

`unit_name` keeps `request_id` verbatim up to 24 characters and otherwise uses `sha256(request_id)[:32]`. The
existing shape (`shiplord-job-<app>:<id>`) and every short name stay identical, which keeps the launch and
`systemctl` tests and any in-flight unit valid. A collision now requires a 128-bit hash collision, not a shared
prefix.

Alternative considered: always hash. Rejected because it changes the unit name for every existing request, and the
issue only asks that the truncation collision be closed.

### Remove the job directory and route file in the existing removal loop

The job directory joins the state/environment/registry-login loop with the same symlink refusal, and is reported only
when it existed, matching how the optional registry login is reported. The route file is removed through a directory
file descriptor opened with `O_NOFOLLOW`, unlinking `<app>.caddy` and `.prior-<app>`, then fsyncing the directory, the
same order routing uses. A missing Caddy root or `ship` directory returns "nothing removed" rather than an error, so a
server bootstrapped without Caddy can still be deprovisioned.

Alternative considered: call `RouteManager.remove`. Rejected because `deprovision` has no `CaddyCommands`, `routing.py`
is out of scope, and `remove` refuses an interrupted-change backup instead of clearing it.

## Risks / Trade-offs

- [A detached worker without systemd outlives the bound and its record is pruned while it still runs] -> Production
  uses the systemd launcher with `RuntimeMaxSec`; the detached launcher is for tests and containers. The grace margin
  keeps a normal job well inside the bound.
- [A job record pruned at the wrong moment could let two deploys share a request ID] -> The deploy gate still rejects a
  reused ID from `seen_requests` once `begin` has recorded it, and the host lock serialises transactions.
- [Removing a native release directory by `release_id` could remove a directory still in use] -> The comparison runs
  against both retained releases and, for `_abandon`, the candidate, and only after a verified restore or a successful
  commit.
- [Deprovision deletes a route file without reloading Caddy] -> The file is gone from the import glob; the next reload
  drops the route. Retirement still removes and reloads when a release was deployed.
- [A corrupt job record still raises from `_prune`] -> Unchanged behavior; `_prune_interrupted` guards the timestamp
  and does not add a new failure mode.

## Migration Plan

No migration. The change is internal to the helper; existing state, job records, unit names, and result shapes remain
readable. Rollback is a revert of the three modules and their tests; a pruned interrupted record is not needed by any
recovery path.
