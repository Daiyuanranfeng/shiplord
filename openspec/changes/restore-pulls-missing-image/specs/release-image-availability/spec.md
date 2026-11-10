# Spec Delta

## Purpose

Defines what Shiplord does when a release it is about to start has no image on the host: when it fetches one, with
which credentials, what must stay untouched until the fetch succeeds, and what a failed fetch reports.

## ADDED Requirements

### Requirement: A start fetches a missing release image with the application's own login
Starting a release MUST make its pinned image available on the host before any container is stopped or removed. When
the image is absent, Shiplord MUST fetch that exact digest using the application's own read-only registry login, and
MUST start the container from it only after the fetch succeeds.

#### Scenario: A pruned image is fetched before the previous release is stopped
- **WHEN** a restore or recovery starts a release whose image is no longer on the host
- **THEN** the image is fetched by its pinned digest with the application's stored login
- **AND** the container that is still serving is stopped or removed only after the fetch succeeds

#### Scenario: A private image is restored without depending on the old image being on disk
- **WHEN** the host has pruned a private release's image and that release must be started again
- **THEN** the fetch uses the stored login for that application and the release starts
- **AND** no anonymous pull is relied on

#### Scenario: A failed fetch leaves the previous release serving
- **WHEN** the fetch of a missing image fails during a start
- **THEN** no container is stopped, removed, or replaced
- **AND** the start reports a runtime failure

### Requirement: A start does not fetch an image that is already on the host
A start MUST check for the image locally first and MUST NOT contact a registry when it is present, so the normal
deploy path - whose image was already fetched while the candidate was prepared - costs no network round trip.

#### Scenario: A present image costs no registry request
- **WHEN** a start runs for a release whose image is already on the host
- **THEN** the image is inspected locally and no pull is performed

### Requirement: A failed fetch reports the registry and no raw pull output
A start that cannot fetch the release's image MUST fail with a message that names the registry host from the image
reference and says the release's image could not be fetched. It MUST NOT include raw pull output, which can contain
what the pull was given.

#### Scenario: A failed fetch names the registry
- **WHEN** the fetch of a missing release image fails
- **THEN** the reported message names the registry host of the image reference, such as `ghcr.io`
- **AND** it states that the release's image could not be fetched
- **AND** it contains no raw pull output

#### Scenario: A restore that cannot fetch does not claim to have restored service
- **WHEN** a restore cannot fetch the release it is restoring
- **THEN** the outcome reports the failure it already reports for an unrestorable release, with no success status
- **AND** no exit code or `rolled_back` / `intervention_required` meaning changes

### Requirement: One pull path serves preparation and starting
Fetching an image MUST use a single mechanism for the worker's pre-pull, the locked verification, and a start: the same
disk-space check, the same application login selection, and the same pinned digest.

#### Scenario: The disk-space refusal applies to a start too
- **WHEN** the host has too little free disk space to fetch an image
- **THEN** a start that needs to fetch one refuses before pulling and before touching any container
- **AND** the same refusal still fails a deploy before the running release is changed
