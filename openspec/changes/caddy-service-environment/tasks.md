# Tasks

## 1. Read the service environment

- [x] 1.1 Add `tests/test_routing.py` cases for the reader: `Environment=` quoting and escapes, an
      `EnvironmentFiles=` entry, a packaged-defaults fallback when systemd fails, an empty answer when nothing is
      readable, and a truncated answer being refused; run `python -m unittest tests.test_routing` and confirm they
      fail before the reader exists
- [x] 1.2 Add the stdlib-only reader to `src/shiplord/routing.py` (`service_environment(system, files)` plus its
      parsers) and confirm the cases from 1.1 pass

## 2. Validate with it

- [x] 2.1 Add `tests/test_routing.py` cases: a scripted service variable reaches `caddy validate`; the throwaway
      `HOME`/`XDG_*` override a service that sets them; a service variable overrides the fixed `LANG`; an unreadable
      environment leaves the fixed environment; a service value is absent from `detail` but present in the child
      process; confirm the first two fail before the fix
- [x] 2.2 Add the optional `service_environment` provider to `SubprocessCaddy` and use it in `validate`, applying
      the throwaway storage last, and redact service values from captured detail; confirm 2.1 passes
- [x] 2.3 Confirm no error message, `str(error)`, or `detail` contains the scripted value by asserting on both in
      the redaction case

## 3. Prove it against a real Caddy

- [x] 3.1 Add a `tests/test_caddy_integration.py` case that validates a `{$VARIABLE}` site address through the
      packaged-defaults path, and asserts the same file is refused when the variable is absent
- [x] 3.2 Run `tests/linux/run.sh caddy` and confirm the new case and the existing ones pass against a real Caddy

## 4. Verify the change

- [x] 4.1 Run `python -m unittest discover -s tests` and confirm `OK` with 994 tests and 80 skips (the new Caddy
      case is skipped without `SHIPLORD_TEST_CADDY`)
- [x] 4.2 Revert `src/shiplord/routing.py` only and confirm the new unit tests fail, so they are regression tests
- [x] 4.3 Run `openspec validate caddy-service-environment --strict` and confirm the change is valid
