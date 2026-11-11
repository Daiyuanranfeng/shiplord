# Spec Delta

## Purpose

Defines how Shiplord decides that Caddy is usable, how it reports a Caddy command it cannot run, and the ordering
that keeps a host's previous helper active when bootstrap cannot wire Caddy.

## ADDED Requirements

### Requirement: A missing or unstartable Caddy is reported as unavailable

When a Caddy command cannot be started at all, the failure SHALL be classified as `unavailable` and SHALL NOT be
reported as a rejected Caddyfile or an invalid existing configuration. A command that starts and then exceeds its
deadline SHALL keep the classification of the operation it interrupted.

#### Scenario: The Caddy binary is absent during bootstrap

- **WHEN** bootstrap validates a Caddyfile on a host where the Caddy command cannot be started
- **THEN** the failure is classified `unavailable` and the message names Caddy rather than blaming the Caddyfile

#### Scenario: The Caddy binary is absent during a route change

- **WHEN** a route is applied or the managed import is ensured and the Caddy command cannot be started
- **THEN** the failure is classified `unavailable` and no Caddyfile is reported as rejected

#### Scenario: A slow Caddy is not called unavailable

- **WHEN** the Caddy command starts but does not finish before its deadline
- **THEN** the failure keeps the classification of the interrupted operation and says it timed out

### Requirement: Bootstrap refuses an unusable Caddy before it changes the host

Bootstrap SHALL establish that Caddy is usable before it creates Shiplord directories, installs a helper version,
switches the current helper, or writes wrappers. A host whose Caddy is unusable SHALL keep the helper it was
already running.

#### Scenario: A missing Caddy binary leaves the previous helper active

- **WHEN** a host that already runs a helper is bootstrapped and the Caddy command cannot be started
- **THEN** bootstrap fails and the current helper version is the one that was active before the attempt

#### Scenario: A missing Caddy configuration directory is refused up front

- **WHEN** the Caddy configuration directory is missing
- **THEN** bootstrap refuses with the existing missing-Caddy message and no helper version is installed

#### Scenario: A working Caddy still bootstraps and repeats cleanly

- **WHEN** Caddy is usable and the host is already bootstrapped
- **THEN** bootstrap succeeds, switches the helper as before, and a second run changes nothing

### Requirement: A refused Caddy names how to install it

A bootstrap refusal caused by an unavailable Caddy SHALL name Caddy, the binary path it expects, and a concrete
install instruction, without including any secret, environment value, or unrelated host path.

#### Scenario: The refusal tells the operator what to do

- **WHEN** bootstrap refuses because the Caddy command cannot be started
- **THEN** the message names Caddy, the expected binary path, and the package to install
