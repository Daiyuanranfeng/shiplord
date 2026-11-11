# Spec Delta

## Purpose

Defines which host resources a release owns, when a release that leaves the retention window may be discarded, and
what happens to a prepared artifact whose transaction never started.

## ADDED Requirements

### Requirement: Retention discards the resource a release owns
After a successful deployment the release that leaves the two-release retention window MUST be discarded unless a
retained release still owns the resource the discard would remove. A Docker release owns its image; a native release
owns a directory named for its release ID, so two commits carrying the identical binary own distinct directories.

#### Scenario: One native binary under several commits does not accumulate release directories
- **WHEN** three native releases are deployed in sequence and all three carry the same binary
- **THEN** the release that leaves the retention window is discarded even though its artifact equals the retained one
- **AND** only the two retained release directories remain

#### Scenario: A native release that is still retained is never discarded
- **WHEN** a deployment's candidate is the same release record as the rollback target
- **THEN** retention discards nothing and the candidate's directory is kept

#### Scenario: A Docker image shared by a retained release is kept
- **WHEN** the release leaving the window uses the same image as a retained release
- **THEN** the image is not removed

### Requirement: A prepared artifact does not outlive a transaction that never started
When the transaction journal cannot be opened, the artifact prepared for that transaction MUST be discarded, unless a
journal record exists that still references it or a retained release owns the same resource. The outcome MUST keep
reporting `journal_unavailable`.

#### Scenario: A failed journal open removes the prepared artifact
- **WHEN** preparation succeeds and opening the journal then fails with no journal present
- **THEN** the prepared artifact is discarded and the outcome is `journal_unavailable`

#### Scenario: A journal that may exist keeps the artifact
- **WHEN** the journal open fails but a journal record is readable
- **THEN** the prepared artifact is kept for recovery and no discard is attempted
