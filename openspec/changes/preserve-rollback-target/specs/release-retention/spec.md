# Spec Delta

## Purpose

Defines which releases the committed state keeps as the current and previous release, when a deploy counts as the
same release, and which artifacts retirement may remove, so a rollback always has a release it can actually return
to.

## ADDED Requirements

### Requirement: Redeploying the committed release keeps the rollback target

When a transaction commits the release that is already the committed current release, the committed previous
release SHALL remain the previous release.

#### Scenario: A redeploy of a release that is down keeps the release it replaced

- **GIVEN** release A is current and release B is previous
- **WHEN** release B is deployed again while it is not running
- **THEN** B is the current release and A is still the previous release
- **AND** a rollback returns to A rather than restarting B

#### Scenario: A redeploy of a release that is not ready keeps the release it replaced

- **GIVEN** release A is current and release B is previous
- **WHEN** release B is deployed again while it is running but not ready
- **THEN** B is the current release and A is still the previous release

#### Scenario: A redeploy that fails leaves the rollback target alone

- **GIVEN** release A is current and release B is previous
- **WHEN** a redeploy of B fails and is restored
- **THEN** B is still current and A is still previous

### Requirement: A different release rotates the rollback target

Deploying a release record that differs from the committed current release SHALL make that current release the new
previous release.

#### Scenario: A new release after a redeploy still rotates

- **GIVEN** release B is current and release A is previous after a redeploy of B
- **WHEN** release C is deployed
- **THEN** C is the current release and B is the previous release

### Requirement: Retirement keeps every artifact a retained release names

After a transaction succeeds, only the artifact of the release that left the two-release window SHALL be removed.
An artifact named by the committed current or previous release SHALL NOT be removed.

#### Scenario: A redeploy keeps the previous release's artifact

- **GIVEN** release B is current, release A is previous, and A's artifact is present
- **WHEN** B is deployed again and succeeds
- **THEN** A's artifact is still present

#### Scenario: A third release removes only the release that left the window

- **GIVEN** release B is current, release A is previous, and both artifacts are present
- **WHEN** release C is deployed and succeeds
- **THEN** A's artifact is removed and B's artifact is kept

### Requirement: A rollback needs a distinct previous release

A rollback SHALL run only when the committed previous release differs from the committed current release.
Otherwise it SHALL report that there is no previous release and SHALL change nothing.

#### Scenario: A previous release that is the current release is refused

- **GIVEN** a committed state whose previous release is the same release as its current release
- **WHEN** a rollback is requested
- **THEN** it fails with `no_previous_release`
- **AND** the running release is not restarted

### Requirement: Recovery accepts a commit written before this rule existed

Recovery SHALL accept a committed state that matches the transaction except that an older helper recorded the
current release as the previous release, so an application interrupted before its journal was removed stays
recoverable across an upgrade.

#### Scenario: An upgraded helper recovers a legacy commit

- **GIVEN** a transaction that committed the current release, recorded with that release as its previous release
- **WHEN** recovery runs after the helper was upgraded
- **THEN** the committed release is reconciled and the journal is removed
