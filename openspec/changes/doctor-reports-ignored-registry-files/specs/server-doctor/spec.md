# Spec Delta

## Purpose

Defines the deep, read-only server health report: which checks it runs, what each check reports, and which host
conditions make the report fail.

## ADDED Requirements

### Requirement: The applications check reports registry files it ignored

The server health report SHALL name every registry file that the registry skipped as not a registration, so a stray
file is visible to the operator instead of silently disappearing.

#### Scenario: A stray file beside a valid registration is reported

- **GIVEN** a registration directory containing `orders.json` and `orders.json~`
- **WHEN** the deep server health report runs
- **THEN** the applications check reports `orders.json~` in `ignored_files`
- **AND** `orders` is still listed

#### Scenario: A clean registry reports no ignored files

- **GIVEN** a registration directory containing only registrations
- **WHEN** the deep server health report runs
- **THEN** the applications check reports an empty `ignored_files` list
- **AND** the text output prints no ignored-file line

### Requirement: An ignored registry file does not fail the health report

A file that the registry skipped SHALL be reported as information, not as a fault: it MUST NOT make the applications
check fail and MUST NOT make the report or the command non-zero.

#### Scenario: A stray file keeps the report ready

- **GIVEN** a healthy server with one registration and one stray registry file
- **WHEN** the deep server health report runs
- **THEN** the applications check is ok
- **AND** the report is ok and the command exits 0

### Requirement: A corrupt registration still fails the applications check

A file ending in `.json` that cannot be read as a registration SHALL still make the applications check not ok, even
though non-registration noise is reported without failing.

#### Scenario: A corrupt record is a fault

- **GIVEN** a registration directory whose `orders.json` contains invalid JSON
- **WHEN** the deep server health report runs
- **THEN** the applications check is not ok
- **AND** the report is not ok
