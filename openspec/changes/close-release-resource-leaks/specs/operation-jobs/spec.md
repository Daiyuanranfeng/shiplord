# Spec Delta

## Purpose

Defines the durable record of a long-running operation: how it is created and pruned, and the systemd unit name it is
launched under.

## ADDED Requirements

### Requirement: Interrupted job records are pruned and their request IDs are reusable
A queued or running job record whose worker cannot still be alive MUST be pruned, so records do not accumulate and
the request ID can be submitted again. A record is interrupted when it is older than the runtime bound the launcher
enforces. Recent queued or running records MUST be kept, and finished records MUST keep the existing count limit.

#### Scenario: A stale unfinished record is pruned and its request ID reused
- **WHEN** a job was created and started longer ago than the runtime bound, and a new job is created
- **THEN** the stale record is removed
- **AND** submitting the same request ID again creates a new job instead of returning the stale one

#### Scenario: A recent unfinished record is kept
- **WHEN** a job was created and started within the runtime bound, and another job is created
- **THEN** the recent record is still present

### Requirement: A job unit name is unique for the full request ID
A job's systemd unit name MUST identify the full request ID. A request ID that fits MUST be used verbatim so existing
names stay stable; a longer one MUST be represented by a hash of the whole ID. Two request IDs that share a prefix
MUST NOT produce the same unit name, and the name MUST stay within the systemd unit-name length limit.

#### Scenario: Request IDs sharing a 24-character prefix get distinct unit names
- **WHEN** two request IDs share their first 24 characters and differ afterwards
- **THEN** their unit names differ

#### Scenario: A short request ID keeps the readable unit name
- **WHEN** a request ID is at most 24 characters long
- **THEN** its unit name contains that request ID verbatim

#### Scenario: A long application and request ID stay within the limit
- **WHEN** the application and request ID are both at their maximum length
- **THEN** the unit name is no longer than 100 characters
