# Spec Delta

## Purpose

Decides whether a hostname is already served by a Caddy configuration before Shiplord installs or replaces a
managed route, so two applications never claim the same site.

## ADDED Requirements

### Requirement: Only site addresses are compared against registered hostnames

Before installing a route for a hostname, Shiplord SHALL compare that hostname against the site addresses
declared by the Caddyfiles it inspects. It SHALL NOT compare it against values inside a directive.

#### Scenario: A hostname in an upstream is not a site address

- **WHEN** an inspected Caddyfile contains `reverse_proxy https://api.example.com`
- **AND** Shiplord installs a route for `example.com`
- **THEN** the route is installed without a conflict

#### Scenario: A hostname in a header value is not a site address

- **WHEN** an inspected Caddyfile contains `header Link https://orders.example.com/x`
- **AND** Shiplord installs a route for `orders.example.com`
- **THEN** the route is installed without a conflict

#### Scenario: A hostname in a host matcher is not a site address

- **WHEN** an inspected site block contains `@api host orders.example.com`
- **AND** Shiplord installs a route for `orders.example.com`
- **THEN** the route is installed without a conflict

#### Scenario: A wildcard inside a directive value does not overlap

- **WHEN** an inspected Caddyfile proxies to `https://*.example.com` inside a site for another hostname
- **AND** Shiplord installs a route for `orders.example.com`
- **THEN** the route is installed without a conflict

### Requirement: Every declared site address still conflicts

Shiplord SHALL refuse to install a route whose hostname is already declared as a site address, in any Caddyfile
form Caddy accepts and in any file the managed import reaches.

#### Scenario: A duplicate site address in the main Caddyfile

- **WHEN** the main Caddyfile declares `orders.example.com` as a site address
- **AND** Shiplord installs a route for `orders.example.com`
- **THEN** the install is refused with a conflict and nothing is written

#### Scenario: Address spellings Caddy treats as the same site

- **WHEN** the existing site address is written with a scheme, a port, a trailing dot, mixed case,
  a quoted label, or as one of a comma-separated list
- **THEN** each of those forms is refused as a conflict

#### Scenario: A duplicate site address in another managed route

- **WHEN** another application's managed route declares `orders.example.com` as a site address
- **THEN** the install is refused with a conflict and the other route file is not modified

#### Scenario: A one-line site block

- **WHEN** an inspected Caddyfile declares `orders.example.com { respond ok }` on one line
- **THEN** the install is refused as a conflict

#### Scenario: A site address list spread over lines

- **WHEN** the site addresses before the opening brace are split across several lines
- **THEN** every address in the list is compared against the registered hostname

#### Scenario: A wildcard site that covers the hostname

- **WHEN** an inspected Caddyfile declares a wildcard site such as `*.example.com`
- **AND** wildcard overlap is not explicitly allowed
- **THEN** the install is refused with a conflict

### Requirement: A configuration the scan cannot classify is refused, not guessed

Shiplord SHALL refuse to install a route when an inspected Caddyfile contains a form whose site addresses
cannot be determined, rather than assume the hostname is free.

#### Scenario: A placeholder site address

- **WHEN** an inspected Caddyfile declares its site address through a placeholder such as `{$SITE_ADDRESS}`
- **THEN** the install is refused as uninspectable

#### Scenario: A malformed configuration

- **WHEN** an inspected Caddyfile has an unbalanced block, an unterminated quote, or an open heredoc
- **THEN** the install is refused as uninspectable

### Requirement: A managed route is still recognised and replaced exactly once

Restricting where a hostname is matched SHALL NOT change how Shiplord finds and replaces the route it wrote.

#### Scenario: Reapplying an unchanged route

- **WHEN** the managed route file already holds exactly what Shiplord would write
- **THEN** the route is reported unchanged and Caddy is neither validated nor reloaded

#### Scenario: Replacing a managed route

- **WHEN** a managed route file holds a different upstream
- **THEN** it is replaced exactly once, validated, and reloaded

#### Scenario: The managed import is counted

- **WHEN** the main Caddyfile imports the managed route glob a number of times other than once
- **THEN** the install is refused before anything is written
