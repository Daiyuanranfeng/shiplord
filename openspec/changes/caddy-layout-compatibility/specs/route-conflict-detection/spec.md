# Spec Delta

## Purpose

Decides whether a hostname is already served by a Caddy configuration before Shiplord installs or replaces a
managed route, including the imported files, snippet imports, and environment-variable site addresses a real
host's Caddyfile uses.

## ADDED Requirements

### Requirement: An imported route file may be a symlink

Shiplord SHALL follow a symlink that an inspected `import` resolves to, so a `sites-enabled` directory of links
into `sites-available` is inspected like any other imported file. The target SHALL be a regular file, owned by
the account the trusted checks require, no larger than the configuration limit, and not group- or
world-writable.

#### Scenario: A symlinked sites-enabled file declares the hostname

- **WHEN** the main Caddyfile imports a `sites-enabled/*.conf` glob whose entry is a symlink to a regular file
  declaring `orders.example.com`
- **AND** Shiplord installs a route for `orders.example.com`
- **THEN** the install is refused with a conflict and nothing is written

#### Scenario: A symlink to an untrusted target is refused

- **WHEN** an imported symlink resolves to a directory, a device, a symlink loop, a group- or world-writable
  file, a file owned by another account, or a file larger than the configuration limit
- **THEN** the install is refused as uninspectable, and no file content appears in the message

#### Scenario: The transaction never follows a symlink it did not import

- **WHEN** the live Caddyfile, its prior copy, or a managed route file is a symlink
- **THEN** it is refused exactly as before; only an imported file may be a link

### Requirement: A top-level import with arguments is inspected

Shiplord SHALL inspect a top-level `import` that passes arguments instead of refusing it or reading an
argument as a file path. For a snippet it SHALL substitute the arguments into the body and compare the
addresses a body line declares before an opening brace; a line without one is a directive. For a file pattern
it SHALL follow the matched files with `{args[N]}` substituted. It SHALL refuse only an address it cannot
determine, naming the snippet or pattern, as uninspectable.

#### Scenario: A directive-only snippet imported with an argument still deploys

- **WHEN** the main Caddyfile defines `(common) { encode gzip }` and imports `common` with an argument
- **THEN** the route change is applied exactly as it was before this change

#### Scenario: A snippet body declares the managed hostname from an argument

- **WHEN** the main Caddyfile defines `(sites) { {args[0]} { ... } }` and imports `sites orders.example.com`
- **AND** Shiplord installs a route for `orders.example.com`
- **THEN** the install is refused with a conflict

#### Scenario: A snippet argument for another hostname still applies

- **WHEN** the same snippet is imported as `sites legacy.example.net`
- **THEN** the route for `orders.example.com` is applied

#### Scenario: A file pattern is followed with its argument substituted

- **WHEN** an imported file declares `{args[0]}` as its address and the import passes `orders.example.com`
- **THEN** the install is refused with a conflict

#### Scenario: An argument the import does not provide is refused

- **WHEN** a snippet or file body reads `{args[1]}` and the import provides no such argument
- **THEN** the install is refused as uninspectable and the message names the snippet or pattern

#### Scenario: A name that is neither a snippet nor a file is refused

- **WHEN** a top-level import passes arguments to a name that is not a known snippet and not a file
- **THEN** the install is refused as uninspectable and the message names that name

#### Scenario: A snippet imported without arguments is unchanged

- **WHEN** the main Caddyfile imports a snippet name with no arguments
- **THEN** a directive-only body is still ignored for site addresses, and a body that declares an address is
  compared

### Requirement: A site address that reads a service variable is resolved before comparison

Shiplord SHALL expand `{$NAME}` and `{$NAME:default}` in a site address from the Caddy **service** environment
before comparing the address, and SHALL NOT consult the caller's own environment. The expansion SHALL affect
only the comparison; the bytes Shiplord writes are unchanged.

#### Scenario: A variable address is a real conflict

- **WHEN** an inspected Caddyfile declares `{$SITE_ADDRESS}` and the Caddy service environment sets it to
  `orders.example.com`
- **AND** Shiplord installs a route for `orders.example.com`
- **THEN** the install is refused with a conflict

#### Scenario: A default value is used when the variable is unset

- **WHEN** an inspected Caddyfile declares `{$SITE_ADDRESS:orders.example.com}` and the service does not set it
- **THEN** the address is compared as `orders.example.com`

#### Scenario: A value the caller sets is not used

- **WHEN** the caller's environment sets the variable but the Caddy service environment does not
- **THEN** the install is refused as uninspectable, not treated as a conflict

### Requirement: An unresolved service variable is refused, naming the variable

Shiplord SHALL refuse to install a route when a site address reads a service variable that cannot be resolved,
and the message SHALL name the variable and never its value.

#### Scenario: The service does not set the variable

- **WHEN** an inspected Caddyfile declares `{$SITE_ADDRESS}` and the service environment does not set it
- **THEN** the install is refused as uninspectable and the message names `SITE_ADDRESS`

#### Scenario: The service environment cannot be read

- **WHEN** the Caddy service environment cannot be read at all
- **THEN** an address that reads a variable is refused as uninspectable and the message names the variable

#### Scenario: A value cannot be a single address

- **WHEN** a service variable used as a site address expands to an empty value or to several tokens
- **THEN** the install is refused as uninspectable and the message names the variable
