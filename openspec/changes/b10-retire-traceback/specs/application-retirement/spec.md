# Spec Delta

## Purpose

Takes a deployed application out of service for `deprovision --retire`, and reports honestly what the retirement
removed and what it kept.

## ADDED Requirements

### Requirement: Retirement validates the release state before it changes anything
The retirement callback MUST validate the release state it is given before it removes the public route or stops the
runtime. A state that is present but not a mapping, or a recorded release that is not a mapping, is unusable. An
unusable state MUST produce a clear, actionable error that names no host path and no environment value, and the
route and the running release MUST be left as they were.

#### Scenario: An unusable release state is refused before the route is removed
- **WHEN** retirement is called with a release state that is not a mapping
- **THEN** it raises a provisioning error that says the state is unusable and nothing was changed
- **AND** the public route is still routed and the runtime is not stopped

#### Scenario: A malformed release record is refused before the route is removed
- **WHEN** retirement is called with a release state whose current release is not a mapping
- **THEN** it raises the same actionable error and nothing is changed

### Requirement: Retirement completes when no release state was recorded
A missing release state MUST NOT abort the retirement. The callback MUST remove the public route and stop the
runtime, MUST NOT attempt to discard releases it cannot identify, and MUST report that those artifacts were kept.

#### Scenario: An application with no recorded release state is retired
- **WHEN** retirement is called with no release state
- **THEN** the route is removed and the runtime is stopped without a traceback
- **AND** the report says the route and the running release were removed and that release artifacts were kept

#### Scenario: An empty release state is retired
- **WHEN** retirement is called with an empty mapping as the release state
- **THEN** the route is removed and the runtime is stopped without a traceback
- **AND** no discard is attempted

### Requirement: Retirement reports what it removed and what it kept
A successful retirement MUST return a report with a `removed` list and a `retained` list. The report MUST include
the public route and the running release under `removed`, MUST include application data under `retained`, and MUST
list each discarded release only when the discard returned no warnings.

#### Scenario: Both recorded releases are discarded and reported
- **WHEN** retirement is called with a current and a previous release and both discards return no warnings
- **THEN** the report lists the public route, the running release, and both releases' artifacts as removed

#### Scenario: A discard that produced a warning is reported as kept
- **WHEN** a release's discard returns a warning
- **THEN** the report lists that warning under `retained` and does not claim that release's artifacts were removed

### Requirement: The command that retires an application returns the retirement report
`deprovision --retire` MUST include the retirement report in its own result, under `retired`, and MUST merge the
retirement's retained entries into its `retained` list. A report computed and then discarded does not satisfy the
requirement that the operator is told what was removed and what was kept.

#### Scenario: The operator sees what retirement removed
- **WHEN** a deployed application is deprovisioned with a retirement callback that reports its work
- **THEN** the `deprovision` result contains `retired` with that report
- **AND** its `retained` list includes the retirement's retained entries

#### Scenario: A callback that reports nothing changes nothing
- **WHEN** the retirement callback returns no report
- **THEN** the `deprovision` result has no `retired` key and its `retained` list is unchanged
