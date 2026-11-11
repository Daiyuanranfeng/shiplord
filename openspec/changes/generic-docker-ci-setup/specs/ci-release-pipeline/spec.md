# Spec Delta

## Purpose

Gives an application's GitHub Actions pipeline the credentials and the workflow it needs to deploy, for
every supported template, while keeping the generated files reproducible and free of references to files
the user has not created.

## ADDED Requirements

### Requirement: CI setup completes for every supported template

`shiplord github setup` SHALL complete for any manifest whose template is supported by the manifest
schema, including `generic`, when the runtime is Docker: it SHALL create a dedicated CI key, register it
on the server, prove it over SSH against the pinned host keys, store it as the repository secret, set the
repository variables, write the generated workflows, and record the `[github]` section in `ship.toml`. No
template SHALL be rejected on template grounds.

#### Scenario: Setup for a generic Docker manifest reaches the key step

- **WHEN** the manifest has `template = "generic"` and `runtime = "docker"`
- **AND** an administrator runs `shiplord github setup --repo owner/name`
- **THEN** the command succeeds, a CI key is registered on the server, the secret and the variables are
  created in the repository, and the workflows are written

#### Scenario: Setup for a native application still gets credentials only

- **WHEN** the manifest has `runtime = "native"`
- **THEN** the CI key, the secret, and the variables are created and no workflow is generated

### Requirement: Validation before the first side effect

Setup SHALL reject an unusable request before any GitHub call, key generation, server change, or file
write. A repository that is not `owner/name`, or one that contradicts an existing `[github]` section,
SHALL fail with exit code 2 and change nothing.

#### Scenario: A repository that is not owner/name

- **WHEN** `--repo` is not of the form `owner/name`
- **THEN** the command exits with code 2 and no secret, variable, key, or file is created

### Requirement: The generated pipeline separates a reusable deploy workflow

The pipeline SHALL be two files: `.github/workflows/shiplord-deploy.yml`, a reusable workflow declaring
`on: workflow_call` with the build and deploy jobs, and `.github/workflows/shiplord-release.yml`, which
runs on pushes to the deployment branch and on manual dispatch and calls it. The trigger SHALL pass the
check-only input and the CI secret explicitly, and grant the calling job only the permissions the called
jobs need.

#### Scenario: A push to the deployment branch builds and deploys through the reusable workflow

- **WHEN** the generated files are parsed as YAML
- **THEN** the trigger workflow's deploy job uses the local `shiplord-deploy.yml` workflow
- **AND** the deploy workflow declares a `workflow_call` trigger and contains the build and deploy jobs

#### Scenario: A check-only dispatch deploys nothing

- **WHEN** the trigger workflow is dispatched with `check_only=true`
- **THEN** the build job is skipped and the deploy job runs `shiplord status --json` instead of a deploy

### Requirement: The quality job is optional and never broken

The trigger workflow SHALL include a quality job only when the repository contains the reusable workflow
it names. Shiplord SHALL use `.github/workflows/code-quality.yml` when it exists, SHALL accept
`--quality-workflow <path>` for another file, and SHALL omit the job with `--no-quality-workflow`. Every
local `uses:` SHALL name a generated file or a file that exists.

#### Scenario: No quality workflow in the repository

- **WHEN** the project has no `.github/workflows/code-quality.yml`
- **THEN** the trigger workflow has no quality job and references no local workflow other than the
  generated deploy workflow

#### Scenario: A quality workflow named explicitly

- **WHEN** `--quality-workflow .github/workflows/checks.yml` is given
- **THEN** the trigger workflow runs that workflow before deploy, and deploy runs after it succeeds or is
  skipped for a check-only dispatch

### Requirement: Quality workflow paths are validated

A quality workflow path SHALL be rejected with exit code 2 unless it is a reusable workflow file under
`.github/workflows/` and is not one of the generated files.

#### Scenario: An unusable quality workflow path

- **WHEN** the path is not under `.github/workflows/`, or is the generated release or deploy workflow
- **THEN** setup exits with code 2 and no workflow file is written

### Requirement: Existing pipeline guarantees are preserved

The generated workflows SHALL stay deterministic for the same inputs, pin every external action to a full
commit, keep the least permissions each job needs, and never interpolate a secret, variable, or event value
into a shell script. Shiplord SHALL NOT overwrite a generated workflow that has been edited since it wrote
it unless `--force-workflow` is passed, and SHALL record the digest of each file it writes.

#### Scenario: An edited generated file is protected

- **WHEN** either generated workflow differs from the digest Shiplord recorded
- **AND** `--force-workflow` is not passed
- **THEN** setup fails with exit code 2, names the file, and leaves it untouched

### Requirement: Deploy keys can be managed without a template

`shiplord keys add` SHALL register a public key for the application through the administrator channel,
`shiplord keys list` SHALL report each label and fingerprint, and `shiplord keys remove` SHALL revoke one
only with `--yes`. The key SHALL come from exactly one of a file or stdin and SHALL be validated before any
connection, failing with exit code 2 without echoing it. The server's refusal to remove the last key SHALL
be reported as an error.

#### Scenario: Add a CI key without a template

- **WHEN** an administrator runs `shiplord keys add --label ci --public-key-file ./ci.pub`
- **THEN** the key is registered for the application and the result names the label and fingerprint

#### Scenario: Two key sources at once

- **WHEN** both `--public-key-file` and `--public-key-stdin` are given, or neither
- **THEN** the command exits with code 2 before connecting

#### Scenario: Removing without confirmation

- **WHEN** `shiplord keys remove --label ci` is run without `--yes`
- **THEN** the command exits with code 2 and no key is removed
