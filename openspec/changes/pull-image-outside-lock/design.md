# Design

## Context

See `proposal.md` - Why. Today the worker (`worker.run_job`) acquires the host-wide `HostLock` and only then builds the
deployer and runs the transaction. `Deployer._transition` calls `DockerRuntime.prepare`, which pulls the image by
digest (`PULL_TIMEOUT_SECONDS = 900`) and then inspects it. So the lock is held across a network round trip whose
duration the host does not control.

`admin.run` takes the same lock for every admin request, including `doctor` and `apps`, which only read the registry
and release state. `Registry.list`/`read` and `ReleaseState.read_state`/`read_journal` do not require the lock;
only writes do (`Registry.register`, `ReleaseState.begin`, `advance`, `write_stop`, `clear_stop`, `record_commit`,
`clear_after_reconciliation`, and the routing/provision paths).

## Goals / Non-Goals

**Goals:**

- No network pull while the host-wide lock is held on the normal path: the worker pulls first, and the locked
  `prepare` only inspects a local image.
- The lock still covers every state transition, provably.
- Read-only admin actions never take the lock; mutating ones always do.
- A concurrent pull can never make a deploy start bytes other than the digest its release names.
- Keep the change inside `worker.py`, `admin.py`, `runtime.py`, and tests.

**Non-Goals:**

- Changing the transaction's ordering, journal phases, or outcomes.
- Making a pull retry inside the lock, or making a failed pull fall back to a cached image.
- Pulling for `recover`: recovery starts a recorded release and has never pulled (`_reconcile` calls `start`, not
  `prepare`).
- Any per-image or per-application lock. The digest-pinned verification makes one unnecessary.

## Decisions

### Split `DockerRuntime.prepare` into `pull` and `prepare`

`pull(registration, release)` does the disk-space check and the `docker pull --quiet` with the application's own
read-only registry login. `prepare(registration, release)` keeps everything that must happen under the lock: the `docker image inspect` by the
pinned digest, the platform and revision-label checks, and the read-only volume provenance check. It also keeps the
disk-space check, because a full disk must still fail a deploy *before* the running release is stopped, with the
message it has today.

`prepare` inspects the local image first and contacts the registry only when the image is not on the host at all. In
the normal worker path the pre-pull has already put the pinned digest in the local store, so the locked step is a
local inspect with no network wait. The fallback exists because `prepare` is also a public entry point: the real-daemon
suite calls `Deployer.deploy` directly, and a worker whose pre-pull failed must still report the honest outcome
instead of failing for a reason the caller did not cause. When it does fetch, it fetches the same pinned digest with
the same application login, so the fallback cannot change which bytes a release names.

Alternative considered: pull unconditionally in `prepare`. Rejected: `docker pull` contacts the registry even when the
digest is already local, so the lock would still wait on the registry. Inspecting first is what removes that wait.

### The worker pulls after validating, before locking

`worker.run_job` builds the deployer first, then asks it for the release this job will need, pulls it, and only then
acquires the lock. The release is chosen with existing, public, read-only API:

- a deploy: `Deployer.preflight` - the same cheap validation the helper already runs before it creates a job
  (`_candidate` for digest pinning, registered repository, budget and hostname; `_gate`; `_stale`). A refusal means
  nothing is pulled.
- a rollback: `ReleaseState.read_state()["previous"]`, the release the transaction will restore.
- a recover: nothing, because recovery never prepares an image.

The pull is best effort: a `RuntimeFailure` or `StateError` there is swallowed, because the locked transaction
re-decides every condition and reports the real outcome. A failed pull therefore ends exactly as it does today - a
`prepare_failed` outcome with nothing changed - and a refused request is still refused with its own reason rather
than a pull error.

Alternative considered: a `Deployer.prefetch` method that owns candidate selection. Rejected: it would move deploy
logic into a method that runs outside the lock, in a file other changes are editing, and `preflight` already exposes
exactly the validation needed.

Alternative considered: pulling from the worker without validation. Rejected: it would let a malformed or hostile
request make the host pull an arbitrary reference before the deploy is refused.

### Correctness under concurrency: verify, never trust the pull

Every deploy and rollback runs `prepare` under the lock, which inspects the *pinned digest* the release names and
checks platform and revision label. Docker content-addresses images, so two concurrent pulls of different digests
cannot make one transaction see the other's bytes; a concurrent `docker image rm` of the same digest makes `prepare`
fail cleanly instead of starting something else. The transaction never uses a tag, and it never reuses a "prepared"
result computed outside the lock.

`is_running` compares digests for the same reason, and it is unchanged.

### Enumerate the read-only admin actions

`admin.py` gains one explicit set, `READ_ONLY_ACTIONS = frozenset({"doctor", "apps"})`. `admin.run` opens the var
directory (for the lock file) as it does today, checks the caller (a read of the registry, no lock needed), and then
takes the lock only for an action outside that set. Anything not enumerated as read-only takes the lock, so a new
action is mutating by default. A test pins the set against `protocol.ADMIN_ACTIONS` so adding an action forces a
decision.

Alternative considered: naming pattern (`action.startswith(...)`) or deriving from `ADMIN_HOSTWIDE_ACTIONS`. Rejected:
`hostwide` means "names no application", which is a different property that happens to coincide today.

### Proving the lock covers state transitions

`ReleaseState` already refuses writes without the lock (`_require_lock`), so the property is enforced structurally.
The test does not rely on that alone: it wraps `begin`, `advance`, `record_commit`, and `clear_after_reconciliation`
and probes a real `flock` on the lock file at each call, so a future change that moved a write outside the lock would
fail it.

## Risks / Trade-offs

- [A deploy of an already running release now pulls first] -> Only latency, and no lock is held. The unchanged path
  still returns `unchanged` without preparing, even when the pull failed, because the pull is best effort.
- [The pull's error detail is no longer the outcome's `detail`] -> The locked `prepare` reports "the image is not
  available on this host" with docker's inspect output. The outcome (`prepare_failed`, nothing changed) is unchanged;
  the registry's own message is lost. Accepted because keeping one code path that produces deployment outcomes matters
  more than that string.
- [A state change between the pull and the lock can leave the needed image absent] -> `prepare` inspects first and
  fetches the same pinned digest once, so the deploy still works; only that rare fetch holds the lock. The bug is not
  reintroduced for the normal path, where the image is already local and no network call is made.
- [A *failed* pre-pull is retried under the lock] -> Yes: `prepare` cannot tell a failed pre-pull from a caller that
  never pulled, so it makes one fetch attempt. A registry that is slow *and* failing can therefore still hold the lock
  for up to `PULL_TIMEOUT_SECONDS` on that path. Fixing it needs a way to tell `prepare` "already tried", which would
  change its signature and the direct-`Deployer` contract; recorded as a known limitation rather than hidden.
- [Native releases] -> They keep installing in `prepare` under the lock; an upload is local, not a registry pull, and
  is out of this change's scope.

## Migration Plan

No migration. The image is pulled by the same pinned digest with the same login; only its position relative to the
lock changes. The real-host Docker suite must be run, because this is runtime and lock behaviour.
