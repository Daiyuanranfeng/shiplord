# Design

## Context

See proposal.md - Why. The facts from the current code that shape the approach:

- `DockerRuntime.prepare` already pulls the image by digest and inspects it once for its platform and its
  `org.opencontainers.image.revision` label. It is the last step before `Deployer._transition` calls
  `state.begin`, which is the first durable write of a transaction.
- A `RuntimeFailure` from `prepare` is already reported as `status: failed`, `error: prepare_failed`, exit
  code 1, and the existing `_abandon` path removes the pulled image unless a retained release uses it.
- `stop_signal` is the single definition of the accepted set. `_stop_gracefully` calls it before changing the
  restart policy or sending anything, and `_outgoing` reads `.Config.StopSignal` from the container, which
  Docker copied from the image when the container was created.
- The server runs stdlib-only modules; `runtime.py` imports nothing beyond the standard library and its own
  package.

## Goals / Non-Goals

**Goals:**

- Refuse an unusable stop signal while the transaction has changed nothing.
- Keep one definition of which signals are acceptable, shared by prepare and stop.
- Keep the prepare result and the deploy output shape unchanged.

**Non-Goals:**

- Repairing an application already deployed with an unusable stop signal; that state still needs an operator.
- Changing the accepted signal set, the shutdown policy, or the exit-code contract.
- Reading the registry manifest separately to avoid the pull (see Decisions).

## Decisions

**The check lives in `prepare`, not in `deploy.py`.** `prepare` is the only place that already holds the
inspected image, and a prepare failure is already the "nothing has changed" path. Putting it in
`Deployer._transition` would duplicate the image inspection and widen the blast radius in a module other work
is touching. The check runs after the platform and revision checks, so the existing verification order and
its messages are unchanged, and before `check_volume`, so nothing else is inspected.

**Reuse the existing inspect call by adding `.Config.StopSignal` as a third field.** One `docker image
inspect` per deploy stays one. A second inspect would be a redundant round-trip against the same local image.
The parse becomes a three-way unpack and a malformed result stays a prepare failure.

**Factor the rule into `parse_stop_signal`, keep `stop_signal` as the stop-time wrapper.** `parse_stop_signal`
returns the normalised signal name or raises `ValueError`; `stop_signal` turns that into today's
`RuntimeFailure` with phase `stop` and its exact message, and `prepare` turns it into a phase `prepare`
failure that names the signal and the image. Both callers therefore share one rule and cannot drift, while
each keeps the message that fits its phase. A second copy of the accepted set was rejected: the two copies
would eventually disagree, and the disagreement would only show up on a live application.

**Do not add the validated signal to the prepare result.** `deploy --json` reports the prepare result under
`image`, so a new key changes an output contract for no benefit. The authoritative value at stop time is the
container's own `.Config.StopSignal`, which `_outgoing` already reads.

**The message names the signal and the image, and nothing else.** Both are facts the operator needs to fix
the image; neither is a secret and neither is a host path. The failure carries no `detail`, so there is no
command output to sanitise.

**The pull still precedes the check, and that is the limit of "nothing stored".** A `STOPSIGNAL` lives in the
image config blob, which cannot be read from the registry manifest alone, so the value is only available
after the pull. The check is therefore the first thing after the inspect, ahead of the journal and of any
volume inspection, and the image pulled for the refused attempt is removed by the existing `_abandon` path.
No release record, journal, container, or route is created.

## Risks / Trade-offs

- [An application already deployed with an unusable stop signal stays stuck] → Out of scope by design; this
  change stops new ones from reaching that state. Such a host still needs the operator action it needs today.
- [A registry or Docker version that reports the signal differently, e.g. `<no value>`] → Treated as "no stop
  signal", i.e. `SIGTERM`, which is what Docker itself does; the existing parser already handles it.
- [A refused deploy leaves image layers in Docker's store] → `_abandon` removes the image; the layers belong to
  Docker's cache, not to release state, and a later pull reuses them.
- [A test that only exercises `prepare` could miss the deploy wiring] → Covered by a deploy test that runs the
  real `DockerRuntime.prepare` against a fake Docker CLI and asserts the journal is absent and the committed
  release is unchanged.
