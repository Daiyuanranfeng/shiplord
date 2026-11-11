# Tasks

## 1. Classify an unstartable Caddy honestly

- [x] 1.1 Add `tests/test_routing.py` cases: a `SubprocessCaddy` pointed at an absent executable raises
      `unavailable` and names the path, a timeout keeps its operation kind, `ensure_import` propagates
      `unavailable` without saying "rejected", and candidate validation propagates it without saying the existing
      configuration is invalid; run `python -m unittest tests.test_routing` and confirm the new cases fail
- [x] 1.2 Raise `unavailable` from `SubprocessCaddy._run` on a start failure, and re-raise it unchanged in
      `ensure_import` and `_validate`; confirm 1.1 passes
- [x] 1.3 Add the `probe_caddy` helper with a fixed minimal Caddyfile and verify a unit case shows the fixed text is
      what reaches `validate` and that the throwaway file is removed

## 2. Refuse an unusable Caddy before the helper changes

- [x] 2.1 Add `tests/test_installer.py` cases: a missing binary fails with a message naming Caddy, the expected
      binary path, and the caddy package; a first bootstrap leaves `layout.lib` without a `current` symlink; a
      repeat bootstrap leaves the recorded `current` target and the sshd drop-in byte-identical; an `unavailable`
      failure inside `ensure_import` still reports the distinct kind and leaves the helper unchanged; update the
      successful-bootstrap validate count for the added probe; confirm the new cases fail before the change
- [x] 2.2 Add `_caddy_preflight` to `installer.bootstrap` before the first directory write, move the Caddy
      integration block before `_install_version`/`_switch_current`, and keep the existing missing configuration
      directory refusal; confirm 2.1 passes and a second bootstrap still reports every `changed` value false
- [x] 2.3 Add the Caddy install instruction to `docs/guide.md` and verify the named path and package match the
      bootstrap message

## 3. Prove it against a real Caddy

- [x] 3.1 Add `tests/test_caddy_integration.py` cases: the probe validates against a real Caddy, and an absent
      binary is reported as `unavailable`
- [x] 3.2 Run `tests/linux/run.sh caddy` and confirm every case passes

## 4. Verify the change

- [x] 4.1 Run `python -m unittest discover -s tests` and confirm `OK` with 1032 tests and 85 skips
- [x] 4.2 Run `openspec validate missing-caddy-preflight --strict` and confirm the change is valid

## Workflow follow-up

- Commit only `openspec/changes/missing-caddy-preflight/` with the implementation and tests.
- Open the pull request against `main` with the unit and real-Caddy results.
