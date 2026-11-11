# Tasks

## 1. Reconcile a leftover prior in `ensure_import`

- [x] 1.1 Add unit tests in `tests/test_routing.py` that simulate both crash windows with the same `os.link` the
      transaction uses: (a) a hard-linked prior with a live Caddyfile that lacks the import, (b) a prior left beside a
      live Caddyfile that is the prior plus the managed import. Assert the prior is gone, no other leftover remains,
      and the result is `changed=True` with one reload for (a) and `changed=False` with no reload for (b). Confirm
      both fail before the fix with `python -m unittest tests.test_routing`
- [x] 1.2 Add unit tests for every refusal: a missing live Caddyfile, a symlinked prior, a directory prior, a prior
      that imports the managed routes, and a live Caddyfile that differs from the prior by more than the import.
      Assert `kind == "interrupted"`, `intervention_required`, both files unchanged, no reload, and a message that
      names `.prior-Caddyfile` and the recovery step. Confirm they fail before the fix
- [x] 1.3 Factor the candidate builder into one method, add the reconciliation helper that compares the prior and the
      live file through `_read_file(..., trusted=True)`, and call it from `ensure_import` before the import scan.
      Verify `python -m unittest tests.test_routing` passes and the existing happy-path, restore-on-reload-failure,
      and owner/mode tests are unchanged
- [x] 1.4 Add the two crash windows to `tests/test_caddy_integration.py` against a real Caddy process and verify a
      route still serves traffic after each recovery

## 2. Verify the change

- [x] 2.1 Run `python -m unittest discover -s tests` and confirm `OK` with no failures and the 79 expected skips
- [x] 2.2 Run `tests/linux/run.sh caddy` and confirm the real-Caddy suite passes
- [x] 2.3 Run `openspec validate reconcile-prior-caddyfile --strict` and confirm the change validates

## Workflow follow-up

- Open the pull request for `fix/73-prior-caddyfile-recovery` and link issue #73.
- Archive the change after review.
