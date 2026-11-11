# Spec Delta

## Purpose

Defines the environment a Caddyfile candidate is validated with, so a configuration that reads a variable the Caddy
service has is judged the way the running service would judge it.

## ADDED Requirements

### Requirement: Validation uses the service environment

A Caddyfile candidate SHALL be validated with the variables the Caddy service runs with, so a placeholder the
service can resolve is resolved during validation too.

#### Scenario: A Caddyfile that needs a service variable validates

- **WHEN** the Caddy service has a variable set and the candidate Caddyfile reads it as `{$NAME}`
- **THEN** validation succeeds and the route change proceeds

#### Scenario: The same Caddyfile fails without the variable

- **WHEN** the service environment cannot be read and the candidate Caddyfile reads `{$NAME}`
- **THEN** validation fails and the route change is refused, as it was before this capability

### Requirement: Only documented service sources are read

The service environment SHALL be read from the unit systemd reports for Caddy (`Environment=` and
`EnvironmentFiles=`), and from the packaged defaults `/etc/default/caddy` and `/etc/sysconfig/caddy` only when the
systemd answer cannot be read. The environment of the process performing validation SHALL NOT be used.

#### Scenario: A variable set in an environment file is applied

- **WHEN** the Caddy unit lists an environment file that sets a variable
- **THEN** validation receives that variable

#### Scenario: A variable in the caller's environment is not applied

- **WHEN** the process performing validation has a variable in its own environment that the service does not have
- **THEN** validation does not receive that variable

### Requirement: Validation never writes real Caddy storage

Validation SHALL use a throwaway home directory and throwaway `XDG_DATA_HOME` and `XDG_CONFIG_HOME` directories,
overriding any value the service sets, and SHALL remove them afterwards.

#### Scenario: The service sets its own home

- **WHEN** the service environment sets `HOME` or an `XDG_*` directory
- **THEN** validation runs with the throwaway directories instead and leaves the service's directories untouched

### Requirement: Service values never reach the deployment account

No error message, job result, or captured command detail SHALL contain a value of the service environment.

#### Scenario: Caddy repeats a value in its output

- **WHEN** a failing Caddy command prints a service variable's value
- **THEN** the value is removed from the captured detail and the error names only the command that failed

### Requirement: An unreadable service environment is not a failure

When the service environment cannot be read, validation SHALL continue with the fixed `PATH` and `LANG` rather
than failing the bootstrap or the route change.

#### Scenario: systemd is unavailable

- **WHEN** systemd cannot report the Caddy unit and no packaged defaults are readable
- **THEN** validation still runs, with the fixed `PATH` and `LANG` and the throwaway home
