# Spec Delta

## Purpose

Defines what the host-wide deployment lock must and must not cover, so that slow, state-free work cannot block the
host while every state transition stays serialised.

## ADDED Requirements

### Requirement: The host-wide lock is not held during an image pull
A deployment MUST fetch the pinned image before it takes the host-wide lock. The pull MUST be performed only for a
release record that has been validated for the application: a digest-pinned Docker artifact of the registered image
repository. An invalid or refused request MUST NOT cause a pull.

#### Scenario: A slow pull does not hold the host lock
- **WHEN** a deployment's image pull is in progress
- **THEN** another operation can take the host-wide lock without waiting for that pull
- **AND** the pull completes before the deployment takes the lock for itself

#### Scenario: A refused request pulls nothing
- **WHEN** a deploy request names an artifact of a repository other than the registered one, or is stale
- **THEN** no image is pulled and the request is refused with the same outcome as before

#### Scenario: A rollback fetches the release it will restore
- **WHEN** a rollback job starts and the previous committed release's image is not on the host
- **THEN** that pinned image is pulled before the host lock is taken

### Requirement: The locked transaction verifies the image it will start
The locked deployment transaction MUST verify the image on the host before it changes anything: the exact digest the
release names, its platform, and its recorded revision label. It MUST NOT fetch from a registry while the lock is held.
If the image is not available, the deployment MUST fail as a prepare failure and MUST NOT stop, start, or route
anything.

#### Scenario: The pulled image is verified under the lock
- **WHEN** a deployment proceeds past validation and takes the host lock
- **THEN** the image is inspected on the host by its pinned digest and its platform and revision label are checked
- **AND** no registry request is made while the lock is held

#### Scenario: An image that is not on the host fails as a prepare failure
- **WHEN** the pinned image is absent when the locked transaction verifies it
- **THEN** the deployment reports a prepare failure and the running release, its route, and the journal are untouched

#### Scenario: Concurrent pulls cannot substitute another image
- **WHEN** two deployments pull different images for the same or different applications at the same time
- **THEN** each locked transaction verifies and starts only the digest its own release names

### Requirement: The host-wide lock covers every state transition
Every change to durable deployment state MUST happen while the host-wide lock is held: the transaction journal
(begin, phase advance, commit, and clear after reconciliation), the runtime change, the routing change, and the
retirement of an old release. A failed prepare MUST leave no state change behind.

#### Scenario: Every state transition observes the lock held
- **WHEN** a deployment runs to success
- **THEN** each journal write, the runtime start, the route change, and the retirement of the dropped release happen
  while the host-wide lock is held

#### Scenario: A busy host lock is reported instead of running a transaction
- **WHEN** another process holds the host-wide lock
- **THEN** the job is recorded as failed with `host_busy` and no state transition is attempted

### Requirement: Read-only administration takes no host lock
Administration actions that only read host state MUST run without the host-wide lock: `doctor` and `apps`. Every
action that changes host state MUST still take it: `provision`, `keys`, `environment`, `registry`, and `deprovision`.
The read-only set MUST be enumerated explicitly, and an action that is neither read-only nor listed as mutating MUST
be treated as mutating.

#### Scenario: Read-only administration answers while a deploy holds the lock
- **WHEN** another operation holds the host-wide lock and an operator asks for `doctor` or `apps`
- **THEN** the request is answered without waiting for the lock

#### Scenario: A mutating action waits for the host lock
- **WHEN** another operation holds the host-wide lock and an operator asks to provision, manage keys, manage the
  environment, manage a registry login, or deprovision
- **THEN** the request takes the lock and fails as busy if it cannot
