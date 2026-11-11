# Proposal

## Why

`RouteManager.ensure_import` keeps the previous main Caddyfile as `.prior-Caddyfile` while it publishes and reloads
the version that carries the managed import. A crash between those steps leaves the prior behind, and the next
bootstrap refuses with "an interrupted Caddyfile change requires manual inspection" — unconditionally, forever,
because the mere presence of the file is treated as unrecoverable. The tool created that file; the operator is
blocked on a state the tool can usually read and settle itself.

## What Changes

- A leftover `.prior-Caddyfile` is reconciled automatically when the live Caddyfile is provably one of the two
  states the interrupted transaction could have left:
  - it is byte-identical to the prior and does not import the managed routes — the publish never happened, so the
    prior is dropped and the normal append path continues;
  - it is exactly the prior with the managed import appended — the publish finished, so the prior is dropped and the
    import is reported as already present.
- Anything else is still refused, but the refusal now names `.prior-Caddyfile` and states the step that unblocks the
  host. That covers a missing or unreadable Caddyfile, a prior that is not a trusted regular file, a prior that
  itself imports the managed routes, and a live Caddyfile that matches neither state.
- Reconciliation only removes a prior it has compared byte-for-byte. It never rewrites the live Caddyfile from the
  prior and never guesses which file to serve.
- The happy path is unchanged: the same link, publish, reload, and restore-on-reload-failure ordering, the same
  ownership and mode preservation, and no new file is written by the recovery path.

## Capabilities

### New Capabilities

- `caddyfile-change-recovery`: how an interrupted main-Caddyfile change is settled on the next bootstrap, and when
  it must be refused instead.

### Modified Capabilities

None: `openspec/specs/` is empty, so there is no existing capability to modify.

## Impact

- `src/shiplord/routing.py` (`ensure_import` and one reconciliation helper) and `tests/test_routing.py`; the caddy
  suite gains a real-Caddy case for both crash windows.
- No protocol, exit-code, or manifest change. A host that already carries a reconcilable prior simply bootstraps
  instead of exiting 6; an unreconcilable one still exits 6, with a message that says what to do.
