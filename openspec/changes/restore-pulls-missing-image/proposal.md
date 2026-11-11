# Proposal

## Why

Starting a release never fetches its image. `DockerRuntime._start` runs `docker run <image>` and relies on Docker's
implicit pull, which is anonymous. An image prune (`docker image prune -a`) is routine host maintenance, and after one
a restore of a private image cannot start the previous release: Docker asks the registry with no login and the rollback
fails. A rollback that cannot start the previous release is not a rollback, and the failure it reports is a Docker
error rather than an explanation.

## What Changes

- A start makes the release's image available on the host before anything is stopped: it inspects the image locally and,
  only when it is missing, fetches the pinned digest with this application's own read-only registry login. An image
  that is already present costs no registry round trip, so the normal deploy path is unchanged.
- The fetch happens **before** the previous container is stopped or removed. A start that has to pull still leaves the
  old container running when the pull fails.
- One pull helper serves `prepare`, the worker's pre-pull, and the start path: the same disk-space check, the same
  `--config <login>` selection, and the same digest. `prepare` keeps its current messages and ordering.
- A pull that fails during a start raises a runtime failure whose message names the registry host from the image
  reference (for example `ghcr.io`) and says the release's image could not be fetched. Raw Docker output is not
  repeated, because a pull can echo what it was given and a login is not the only thing in it.
- No exit code, `rolled_back`, `intervention_required`, journal, protocol, or CLI change. A restore that cannot fetch
  the image reports the failure it already reports and does not claim to have restored service.

## Capabilities

### New Capabilities

- `release-image-availability`: what Shiplord must do when a release it is about to start has no image on the host -
  when it may fetch, with which credentials, what must not be touched before the fetch succeeds, and what a failure
  must say.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/runtime.py`: one `_pull` helper behind `pull` and `prepare`, and an image-availability step in `_start`
  before `_remove_containers`. No signature change to `prepare`, `pull`, `start`, `start_with_state`, or
  `start_with_provenance`.
- Tests: `tests/test_runtime.py` (a local image is not re-pulled, a missing one is pulled with the login before
  anything is removed, a failed pull leaves the old container alone and names the registry, the disk-space refusal is
  shared), and `tests/test_deploy_integration.py` (a real registry, a pruned image, and a recovery/restore that must
  fetch it, plus the honest failure when the stored login cannot be used).
- No manifest, provisioning, protocol, exit-code, or dependency change. Server modules stay stdlib-only.
