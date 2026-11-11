# Proposal

## Why

Before installing a route, Shiplord reads every Caddyfile it can see and refuses a hostname that another
configuration already serves. That scan currently treats *every* token on every line as a site address, so a
hostname that merely appears as a value — `reverse_proxy https://api.example.com`, or a `header` value — is
read as if it were the site's own address. A Caddyfile that proxies to `api.example.com` therefore blocks a
route for `example.com`, and a legitimate route change fails until an operator edits the Caddyfile by hand.

## What Changes

- The hostname comparison considers only the tokens Caddy reads as a site address: the header of a top-level
  site block, before its opening brace. Directives inside a block (an upstream, a header value, a `handle`
  body, a host matcher) are never site addresses.
- A site address that spans several lines still matches, and a one-line site block (`example.com { ... }`)
  still matches, so every form Shiplord writes or must recognise keeps working.
- The wildcard-overlap check uses the same site-address positions, so it stops firing on a wildcard inside a
  directive value and keeps firing on a real wildcard site.
- The uninspectable refusal is unchanged: a placeholder address, an unbalanced block, an unterminated quote,
  or an open heredoc is still refused rather than guessed at.
- Genuine duplicate site addresses, including ones in another managed route or a followed import, still
  conflict, and a hostname owned by another application is still refused.

## Capabilities

### New Capabilities

- `route-conflict-detection`: how Shiplord decides whether a hostname is already served by a Caddy
  configuration before it installs or replaces a managed route.

### Modified Capabilities

None; the project has no specs yet, so this capability is introduced by this change.

## Impact

- `src/shiplord/routing.py`: site-address extraction in `_Inspection`, and the wildcard and conflict checks
  that use it. `own_import_lines`, the managed-block detection and replacement, and `render_route` are
  unchanged.
- `tests/test_routing.py`: the hostname-form conflict table loses the header-value case (it is now correctly
  not a conflict) and gains cases for upstreams, header values, host matchers, one-line blocks, and
  multi-line address lists.
- `tests/test_caddy_integration.py`: a real-Caddy case proves Caddy accepts a site whose upstream and header
  value name the registered hostname, and that Shiplord then installs the route and serves it.
- No CLI, protocol, manifest, exit-code, or error-message change: a conflict is still a `RoutingError` of
  kind `conflict`, and a refusal still names no host path.
