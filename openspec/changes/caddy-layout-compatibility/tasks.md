# Tasks

## 1. Imported symlinks

- [x] 1.1 Change `routing._read_path` to open the imported path and trust only the descriptor: regular file,
  at most `MAX_CONFIG_BYTES`, owned by `os.geteuid()`, no group or world write bit. Verify with
  `tests/test_routing.py` cases for a followed symlink, a directory, a loop, an oversized target, a
  group-writable target, and another owner's target, all refused as uninspectable.
- [x] 1.2 Add a real-Caddy case in `tests/test_caddy_integration.py` for a `sites-available` file reached
  through a symlinked `sites-enabled/*.conf`, proving a conflict is found and a free hostname still applies.
  Verify with `tests/linux/run.sh caddy`.

## 2. Import statements and snippet arguments

- [x] 2.1 Make `_Inspection.scan` return import statements (pattern and arguments) and record each snippet
  definition with its body, so an argument is never followed as a file path. Verify with a unit test that a
  file named like an argument is not read, and that a snippet body declaring `{args[0]}` is compared.
- [x] 2.2 Substitute `{args[N]}` into a snippet body and into a followed file pattern, comparing a body line's
  tokens before its opening brace as addresses and treating a brace-less body line as a directive. Verify with
  unit tests for a directive-only snippet imported with an argument (must not raise), a snippet argument that
  is the managed hostname (conflict), one that is another hostname (applies), and a file pattern with an
  argument (conflict).
- [x] 2.3 Refuse only an address that cannot be determined: an argument the import does not provide, `{block}`,
  an `{args[:]}` that is not one address, or a name that is neither a known snippet nor a file. Verify with
  unit tests that name the snippet or pattern and never echo an argument value.
- [x] 2.4 Keep a no-argument snippet import working and a no-argument file import followed. Verify with the
  existing import tests plus a case where a snippet name contains a dot.
- [x] 2.5 Add a real-Caddy case for a directive-only snippet imported with an argument (route applies), and
  for a snippet that declares the managed hostname from an argument (conflict). Verify with
  `tests/linux/run.sh caddy`.

## 3. Service-variable site addresses

- [x] 3.1 Expand `{$NAME}` and `{$NAME:default}` in an address token from the Caddy service environment, read
  from the command runner's `service_environment`, before comparing. Verify with unit tests for a conflict, a
  default value, and that the caller's `os.environ` is never consulted.
- [x] 3.2 Refuse an unresolved variable, an unreadable service environment, an empty value, and a multi-token
  value as uninspectable, naming the variable and never its value. Verify with unit tests that assert the
  message names the variable and that the value does not appear in the error or detail.

## 4. Configurable Caddy binary

- [x] 4.1 Add `routing.caddy_binary`, make `SubprocessCaddy` default to it, and replace
  `installer.CADDY_BINARY` with the same resolution in the bootstrap refusal. Verify with unit tests for the
  default, an absolute override, an ignored relative override, and a bootstrap refusal that names the
  override.
- [x] 4.2 Document the override in `docs/guide.md`, including that the helper process reads it, that the local
  CLI does not forward it, and that sudo must preserve it. Verify the documented command is the one the code
  reads.

## 5. Integration checks

- [x] 5.1 Run `.venv/bin/python -m unittest discover -s tests` and confirm the suite is OK with the baseline
  skip count.
- [x] 5.2 Run `tests/linux/run.sh caddy` and confirm every case passes, including the symlinked
  `sites-enabled` case.

## Workflow follow-up

- Archive the change after the pull request is reviewed and merged.
