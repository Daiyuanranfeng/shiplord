# Proposal

## Why

The worker takes the host-wide lock before it runs the deployment transaction, and the transaction pulls the image
inside it. A registry that answers slowly therefore holds the one host-wide lock for up to the pull timeout, which
blocks every other application's deploy **and** every admin command. That includes the read-only commands an operator
uses to diagnose the stall: `doctor` and `apps` wait behind the pull too, so one slow registry makes the whole host
look broken.

## What Changes

- Pull the pinned image before the host-wide lock is taken, so a slow or unreachable registry delays only the job that
  needs that image.
- Make the locked transaction verify the image on the host instead of fetching it: `prepare` keeps its platform,
  revision label, `STOPSIGNAL`-adjacent and storage checks, and fails cleanly when the verified image is not present.
- Keep the lock covering every state transition: journal begin/advance/commit/clear, runtime change, routing, and
  retirement all still run under it, and a test proves each one observed the lock held.
- Take the host lock only for admin actions that change host state. `doctor` and `apps` are read-only and run without
  it; `provision`, `keys`, `environment`, `registry` and `deprovision` keep it.
- Keep the image choice correct under concurrency: only a fully validated, digest-pinned artifact of the registered
  repository is ever pulled, the pull happens outside the lock, and the locked `prepare` verifies the exact digest
  that the transaction will start, so a concurrent pull can never make one deploy use another's bytes.

## Capabilities

### New Capabilities

- `host-lock-scope`: what the host-wide lock must and must not cover. It owns the requirement that slow, state-free
  work (an image pull) happens outside the lock, that read-only administration takes no lock, and that every state
  transition happens inside it.

### Modified Capabilities

<!-- No existing specs in this repository; this change introduces the first capability. -->

## Impact

- `src/shiplord/runtime.py`: `DockerRuntime.prepare` no longer pulls; a new `pull` performs the network fetch and the
  disk-space check it guarded.
- `src/shiplord/worker.py`: the job validates its image and pulls it before `session.lock.acquire`, then runs the
  transaction under the lock as before.
- `src/shiplord/admin.py`: an enumerated read-only action set runs without the host lock.
- `tests/test_runtime.py`, `tests/test_helper_actions.py`, `tests/test_serverdoctor.py`: unit coverage for the split,
  the lock-free read-only actions, and the lock covering every state transition.
- `tests/test_deploy_integration.py`: a deliberately slow registry proves in `tests/linux/run.sh docker` that a pull
  in progress blocks neither the host lock nor read-only administration, and that the deploy then succeeds with the
  registry gone.
- Behaviour for a failed pull is unchanged in outcome: the job still fails with `prepare_failed` and nothing is
  changed. Its message now says the image is not available on the host, because the locked transaction verifies
  instead of fetching.
