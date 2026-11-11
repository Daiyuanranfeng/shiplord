# Spec Delta

## Purpose

Decides and records the non-secret values of an application's `ship.toml`, so the manifest a person or an agent
creates is one the next command - `provision`, `plan`, or `deploy` - can use without extra flags.

## ADDED Requirements

### Requirement: A Docker manifest names the image repository

For the Docker runtime, `configure` SHALL record the application's image repository in the manifest's `[github]`
table as an `owner/repository` pair, so the application is provisioned without `--image-repository`. The value SHALL
be the `--image-repository` option when given, otherwise the repository inferred from the git remote, otherwise the
answer to a question.

#### Scenario: The repository comes from the git remote

- **WHEN** the directory is a git repository whose `origin` remote is a GitHub URL such as
  `git@github.com:owner/orders.git` or `https://github.com/owner/orders.git`
- **AND** no `--image-repository` is given
- **THEN** the written manifest names `owner/orders`, and `shiplord provision` reads
  `ghcr.io/owner/orders` from it with no extra flags

#### Scenario: The repository is asked for when no remote names one

- **WHEN** there is no usable GitHub `origin` remote and the command runs in a terminal
- **THEN** `configure` asks for an `owner/repository` pair, rejects an answer the manifest would reject, and
  records the accepted answer

#### Scenario: An explicit repository wins over the remote

- **WHEN** `--image-repository other/orders` is given while the git remote names `owner/orders`
- **THEN** the written manifest names `other/orders`

#### Scenario: A native application has no image repository

- **WHEN** the runtime is `native`
- **THEN** `configure` does not ask for an image repository, records none, and refuses `--image-repository`

### Requirement: Inference is limited to a GitHub remote

Inference SHALL read only the `origin` remote's URL, SHALL accept it only when its host is `github.com` and its
path is exactly one `owner/repository` pair (optionally ending in `.git`), and SHALL treat every other outcome -
no git, no repository, no `origin`, an unsupported host, a malformed URL - as "no repository known" rather than as
an error. No message SHALL contain the remote URL.

#### Scenario: A remote on another host is not guessed

- **WHEN** the `origin` remote is `https://gitlab.com/owner/orders.git`
- **THEN** no repository is inferred, and the terminal question is asked instead

#### Scenario: A missing or odd remote does not crash

- **WHEN** the directory is not a git repository, or `git` is not installed, or the remote URL is unparseable
- **THEN** `configure` continues to its question or to its flag-only behaviour and writes or refuses the manifest
  as usual

#### Scenario: A remote URL never appears in output

- **WHEN** the `origin` remote carries a credential, such as `https://user:token@github.com/owner/orders.git`
- **THEN** the detected repository is reported as `owner/orders` and the URL, user, and token appear in no output

### Requirement: A scripted run without a repository says so

Without a terminal, `configure` SHALL NOT ask and SHALL NOT fail for a missing image repository: it SHALL write the
manifest and warn on stderr that `provision` will need `--image-repository`, unless the flag or the git remote
supplied one.

#### Scenario: No terminal, no remote, no flag

- **WHEN** `configure` runs without a terminal for a Docker application, no `--image-repository` is given, and no
  GitHub `origin` remote exists
- **THEN** the manifest is written without a `[github]` table and stderr names `--image-repository` as what
  `provision` will need

#### Scenario: No terminal with a flag or a remote

- **WHEN** `configure` runs without a terminal and either the flag or a GitHub `origin` remote names the repository
- **THEN** the manifest records it and no warning is printed
