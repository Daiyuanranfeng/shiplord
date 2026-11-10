# Proposal

## Why

Four common Caddy layouts make route inspection refuse or, worse, miss a real conflict. The most damaging is
Debian's default: `sites-enabled/*.conf` is a directory of symlinks into `sites-available`, and the import
reader refuses every symlink as uninspectable, so the most common host cannot take a route change at all.
Two more layouts are misread rather than refused: an `import` that passes arguments is treated as a list of
file paths, and a site address written as `{$VAR}` is never resolved from the environment the Caddy service
actually runs with. Finally, the Caddy binary is hardcoded at `/usr/bin/caddy`, so a host that installs Caddy
elsewhere cannot bootstrap.

## What Changes

- An imported route file may be a symlink. The link target must be a regular file owned by the account the
  trusted checks require, no larger than the existing limit, and not group- or world-writable; anything else
  is still refused as uninspectable. The live Caddyfile, its prior copy, and every file the transaction
  writes keep `O_NOFOLLOW`.
- A top-level `import` that passes arguments is inspected instead of refused. A snippet import has its body
  inspected with the arguments substituted: a body line that opens a block declares the addresses before its
  `{`, and a line without an opening brace is a directive and declares no site, so a directive-only snippet
  keeps working. A file-pattern import is followed with `{args[N]}` substituted, as Caddy does. Shiplord
  refuses only an address it cannot determine: an argument the import does not provide, `{block}`, an
  `{args[:]}` that is not one address, or a name that is neither a known snippet nor a file.
- A site address written as `{$VAR}` or `{$VAR:default}` is expanded from the Caddy **service** environment
  before it is compared, never from the caller's environment. A variable the service does not set is refused
  as uninspectable with a message naming the variable and never its value.
- The Caddy binary is resolved by one documented function, `routing.caddy_binary`, used by both
  `SubprocessCaddy` and bootstrap. `SHIPLORD_CADDY_BINARY` overrides the packaged `/usr/bin/caddy` when it is
  an absolute path; a relative or empty value keeps the default.
- **Out of scope:** Caddy running in Docker. The issue summary lists it, but the passing criteria do not, and
  a container-only deployment has no host unit, host Caddyfile, or `systemctl reload caddy` to transact
  against. Half-supporting it would be worse than naming it as unsupported.

## Capabilities

### New Capabilities

- `route-conflict-detection`: how Shiplord decides whether a hostname is already served by a Caddy
  configuration before it installs or replaces a managed route, including imported files, snippet imports,
  and site addresses that read the service environment.
- `caddy-command-execution`: how Shiplord locates and runs the Caddy binary for validation and bootstrap.

### Modified Capabilities

None; the project has no committed specs, so both capabilities are introduced by this change.

## Impact

- `src/shiplord/routing.py`: `_read_path` follows an imported symlink with the trust checks above;
  `_Inspection` records snippet bodies and import statements, substitutes `{args[N]}` and `{$VAR}` in an
  address before comparing it, and `SubprocessCaddy` resolves its binary through `caddy_binary`.
- `src/shiplord/installer.py`: the bootstrap refusal names the resolved binary instead of a hardcoded path;
  `CADDY_BINARY` is replaced by the shared resolution.
- `docs/guide.md`: documents the override and honestly states which processes read it.
- `tests/test_routing.py`, `tests/test_installer.py`, `tests/test_caddy_integration.py`: unit and real-Caddy
  coverage, including a symlinked `sites-enabled` layout in `tests/linux/run.sh caddy`.
- No protocol, manifest, exit-code, or route-file-format change. A conflict is still a `RoutingError` of kind
  `conflict`; a refusal is still `uninspectable` and names no file content.
