# Tasks

## 1. Prove the defect

- [x] 1.1 Add a failing test in `tests/test_routing.py` that a `reverse_proxy` upstream and a `header` value naming the registered hostname do not conflict; run `.venv/bin/python -m unittest tests.test_routing` and confirm it fails against the current code (it raised `conflict` for both)
- [x] 1.2 Add a failing test that a `host` matcher, a `handle` body, and an upstream inside another managed route do not conflict, and that a one-line site block still conflicts (the four non-conflict cases failed, the one-line conflict passed as a guard)
- [x] 1.3 Add a failing test that an `import` inside a site block is not followed as a site-address source, while a top-level import still is (the directive import failed by following the file and reporting a conflict; `test_followed_imports_are_inspected` passed as a guard)
- [x] 1.4 Add a failing test that a brace-less single-site file has only its first line read as an address, and that an address list continued with a trailing comma is read in full (the brace-less cases failed; the comma-continuation conflict passed as a guard)

## 2. Restrict the match to site-address positions

- [x] 2.1 Replace the tokenizer's unquoted-token list with a per-token quoted flag and update the three call sites; verify with `.venv/bin/python -m unittest tests.test_routing tests.test_serverdoctor`
- [x] 2.2 Extract site-address tokens from a line up to its opening brace, skip snippet and named-route definition names, and refuse a placeholder or a brace inside an address; verify with 1.1, 1.2 and the existing uninspectable cases
- [x] 2.3 Track the brace-less single-site body and the trailing-comma address continuation in `_Inspection.scan`; verify with 1.4
- [x] 2.4 Collect top-level imports only, and stop following an import inside a block; verify with 1.3
- [x] 2.5 Run the wildcard overlap check on the same address tokens; verify that a wildcard upstream does not conflict and a wildcard site still does

## 3. Real-Caddy evidence

- [x] 3.1 Add a case to `tests/test_caddy_integration.py` where a real Caddy accepts a site whose upstream and header value name the registered hostname, and Shiplord then installs the route and serves it; verify with `tests/linux/run.sh caddy` (7/7 OK, and the new case failed against the old `routing.py`)
- [x] 3.2 Confirm the existing real-Caddy duplicate-address refusal still passes in the same run (`test_tls_style_or_scheme_duplicates_that_caddy_misses_are_still_refused` OK)

## 4. Confirm nothing else moved

- [x] 4.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is `OK` with no new skips (970 run, OK, 77 skipped: the baseline 960/76 plus the ten tests added here, one of them the skipped real-Caddy case)
- [x] 4.2 Confirm `render_route`, `own_import_lines`, the transaction ordering, and the error kinds are unchanged by the full suite

## Workflow follow-up

- Archive the change after review, following the repository's OpenSpec workflow.
- Record the `tests/linux/run.sh caddy` result in the pull request.
