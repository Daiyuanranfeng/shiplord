# Tasks

## 1. Preserve the Caddyfile's identity when it is rewritten

- [x] 1.1 Add `tests/test_routing.py` cases: a `0640` Caddyfile is still `0640` after `ensure_import`, and the
      temporary validation copy is `0640` too; confirm both fail before the fix
      (`python -m unittest tests.test_routing`)
- [x] 1.2 Make `_read_file` return the bytes with the owner, group, and permission bits taken from the same
      descriptor, and update its callers to read `.data`
- [x] 1.3 Teach `_create` to create the file `0600`, apply a preserved owner and the exact mode with `fchown` and
      `fchmod` before writing, and remove the new file when that fails; pass the main Caddyfile to it from
      `ensure_import` and `_validate`
- [x] 1.4 Add a `tests/test_caddy_integration.py` case that a `0640` Caddyfile stays `0640` and still serves
      traffic after the import is rewritten against a real Caddy; confirm it fails before the fix
      (`tests/linux/run.sh caddy`)

## 2. Give route files an explicit mode

- [x] 2.1 Add a `tests/test_routing.py` case that creates a route under umask `0077` and `0000` and asserts the
      route file is `0644` in both; confirm the `0077` case fails before the fix
- [x] 2.2 Create route files at `ROUTE_FILE_MODE` (`0644`, matching the installer's placeholder) instead of the
      helper's umask, with the choice recorded in a comment

## 3. Refuse a rewrite whose owner cannot be kept

- [x] 3.1 Add a `tests/test_routing.py` case that makes `fchown` fail and asserts the change fails with
      `precondition`, the Caddyfile is byte-identical with its original mode, Caddy is not reloaded, and no
      temporary file is left behind
- [x] 3.2 Verify the refusal path leaves the previous Caddyfile live by running the case from 3.1 against the
      implemented `_create`

## 4. Verify the change

- [x] 4.1 Run `python -m unittest discover -s tests` and confirm `OK` with 816 tests and 69 skips
- [x] 4.2 Revert `src/shiplord/routing.py` only and confirm every new test fails, so they are regression tests
- [x] 4.3 Run `tests/linux/run.sh caddy` and confirm `Ran 6 tests, OK` against a real Caddy process
