# Design

## Context

See `proposal.md` for motivation and `specs/caddyfile-change-recovery/spec.md` for the required behaviour.

`ensure_import` is the only writer of the main Caddyfile. Its transaction is:

```
read Caddyfile ─> append "import <root>/ship/*.caddy" ─> validate a copy
      ─> create .tmp-Caddyfile-<token>
      ─> os.link("Caddyfile", ".prior-Caddyfile")      <-- crash window A opens
      ─> os.replace(".tmp-Caddyfile-<token>", "Caddyfile")  <-- crash window B opens
      ─> reload ─> unlink ".prior-Caddyfile"
```

So a crash can leave the live file in exactly two states:

```
A: Caddyfile == prior, no managed import      (the replace never ran)
B: Caddyfile == prior + "import ..."          (the replace ran, the unlink did not)
```

Both are readable from the two files alone, because the candidate is a pure function of the prior: the prior
content, a newline if the file did not end with one, and the import line. Nothing else is needed to decide, and no
state outside these two files is consulted.

## Goals / Non-Goals

**Goals:**

- Settle window A and window B with no operator involvement.
- Make every remaining refusal self-describing: the file name and the step that unblocks the host.
- Keep the normal transaction byte-for-byte identical.

**Non-Goals:**

- Repairing a Caddyfile, choosing between the two files, or validating/reloading during recovery.
- Reconciling the per-application `.prior-<app>` file; `reconcile(force=True)` already owns that path and is out of
  scope here.
- Changing how the import is rendered, validated, or reloaded.

## Decisions

### Prove the state by comparing bytes, not by counting imports

The spec's window B is described by "imports the managed routes exactly once". Counting imports alone would accept
any file that happens to contain one import line, including one another writer produced, and the prior would then be
deleted without ever being compared. The implementation therefore requires both:

- the prior has no managed import at all (the transaction only links a Caddyfile that lacks one), and
- the live file equals `prior`, or equals `prior` with the import appended, byte for byte.

This is strictly narrower than counting imports: it can only turn a false "reconciled" into a refusal, which is the
direction the issue asks for. The candidate builder is factored into one method used by both the transaction and the
recovery, so the two can never drift apart.

### Read both files with the existing trusted-descriptor helper

`_read_file(root, name, trusted=True)` already refuses a symlink (`O_NOFOLLOW`), a non-regular file, an oversized
file, a foreign owner, and a group- or world-writable file. Reusing it means every "the prior is not a regular file"
case is one refusal, with no new `open` on a privileged path and no new stat logic. A missing prior is `None`, which
is the ordinary no-prior path; a name that exists but cannot be opened as a trusted regular file is a refusal, so a
dangling symlink is not mistaken for absence.

### Settle before the import scan, not after

The reconciliation needs the prior only. Reading it first keeps the failure messages unambiguous: an unreadable or
mismatched pair reports the prior, not "the main Caddyfile imports the managed routes more than once". The helper
returns "already present" or "continue", and the existing precondition checks (missing Caddyfile, more than one
import) run unchanged for the cases where no prior is involved.

### Do not reload when window B is settled

Window B cannot say whether the reload that follows the replace happened. Reloading would be a guess in the other
direction and adds a failure mode (`reload` raises intervention-required when Caddy cannot reload). It is also
unnecessary: the appended line only pulls in `<root>/ship/*.caddy`, and every route change validates the whole
Caddyfile and reloads, so the import takes effect with the first route transaction. The report stays "unchanged",
which is what the file state actually is.

### One refusal constructor

Every unreconcilable case raises the same `RoutingError(kind="interrupted", intervention_required=True)` built by one
small module function, so the message cannot drift between cases and cannot grow a host path: it names
`.prior-Caddyfile`, says to compare it with the Caddyfile, keep the one to serve, delete the prior, and retry.

## Risks / Trade-offs

- [A live Caddyfile with exactly one import but a different prior is refused] → Intended: it is not provably one of
  the two states, and deleting the prior would discard the only copy of a file that was never compared.
- [Another writer changes the Caddyfile between the link and the recovery] → The pair no longer matches a crash
  state, so recovery refuses instead of deleting the prior. The host lock excludes Shiplord, not other writers, and
  the module already treats validation as the backstop for that race.
- [Window B settles without a reload] → The import is inert until a route file exists, and any route change reloads;
  see the decision above.
- [The recovery path is exercised only on a crash] → Both windows are simulated in the unit tests with the same
  `os.link` the transaction uses, and in the caddy suite against a real Caddy process.

## Migration Plan

No migration. A host that already carries a reconcilable prior bootstraps on the next run; one that carries an
unreconcilable prior keeps exiting 6 with the new message. Rollback is a code revert; the on-disk files are untouched
by the refusal path.
