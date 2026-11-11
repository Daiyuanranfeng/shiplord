# Shiplord

Shiplord ships tested releases of an application to your own Linux server over SSH. A coding agent or a
developer creates an application from the pinned template, provisions it once, and then deploys already-built
artifacts with one command, from a terminal or from GitHub Actions. Nothing is built on the server, no
deployment daemon runs, and every release is verified, journaled, and reversible.

The design and acceptance criteria are in [ship-cli-plan.md](../ship-cli-plan.md). The executable is `shiplord`
and the application manifest is `ship.toml`.

## Status

Built and verified (see [evidence.md](evidence.md) for what ran, where, and what did not):

- Application creation from the pinned template, manifest validation, server registration.
- Server bootstrap, application provisioning, SSH keys, runtime environment, port allocation.
- Docker and native-binary deployment, readiness and routing checks, rollback, recovery, status, logs, plan.
- Caddy route transactions with validate-before-publish and restore on failure.

Also run on a real Hetzner server (Ubuntu, x86-64, behind Cloudflare): native deploys, rollback, a failed release
restored automatically, Let's Encrypt HTTPS, and a release deployed by GitHub Actions through Tailscale OIDC. Not
verified: the plan's controlled install on *your* production server (§12E). See [evidence.md](evidence.md).

## Requirements

- **The server:** **Ubuntu 24.04 or later, or Debian 12 or later**, with systemd, OpenSSH, and Caddy, and
  **Python 3.11 or later at `/usr/bin/python3`**. These are the only supported operating systems: Shiplord's
  sshd drop-in, Caddy layout, and apt assumptions are Debian-family ones, and the helper reads TOML with
  `tomllib`, which only exists from Python 3.11. `shiplord server bootstrap` reads `/etc/os-release` and
  refuses any other distribution or an older version (Ubuntu 22.04, Debian 11) with a message naming the
  supported set and the detected release; it also checks the interpreter over SSH before it builds or uploads
  anything, and refuses the server with a message naming the path and the version instead of failing later with
  an unreadable response. Install Caddy from its official package repository (the `caddy` package) so the binary
  is at `/usr/bin/caddy`; `shiplord server bootstrap` validates a fixed minimal Caddyfile before it installs or
  switches the helper, and refuses a host without Caddy with a message naming the package and that path. A Caddy
  binary elsewhere is configured with `SHIPLORD_CADDY_BINARY`, which must be an absolute path; a relative or
  malformed value is ignored and `/usr/bin/caddy` is used. Only the processes that run the helper read it: the
  bootstrap preflight, the administrator operations, and the deploy worker. The local CLI never forwards it, and
  `sudo` drops it unless the host keeps it (`sudo -E`, or `env_keep` for that variable). The worker is started
  with `systemd-run`, so it inherits the systemd manager's environment rather than your shell: for a
  non-standard path at deploy time, set `DefaultEnvironment=SHIPLORD_CADDY_BINARY=...` in the manager
  configuration. The Caddy child process itself never receives the variable.
- **Your machine:** Python 3.11 or later, Git, and OpenSSH.

## Install the CLI

Install the CLI on the machine you work from:

```sh
python -m pip install .
shiplord --help
```

## How it fits together

```
your machine ──ssh──▶ ship-<app>@server ──forced command──▶ dispatcher ──sudo──▶ helper (root, one app)
                                                                                    │ starts a detached job
      administrator ──ssh + sudo──▶ shiplord-admin (bootstrap, provision, keys, env)   ▼
                                                                    worker: journal → runtime → ready → route → commit
```

- **Administrator operations** (`server bootstrap`, `provision`, `env`) use your own SSH login and your own sudo
  (you are asked for the sudo password if one is needed; it is sent on stdin, never in a command line).
- **Application operations** (`deploy`, `status`, `logs`, `rollback`, `recover`, `plan`) use a per-application
  account whose key can run exactly one forced command. The root helper binds every request to that account's
  registration, so an application's deployment account can operate only that application. (The running
  application itself is only confined as described under *Security model*.)
- A deployment is a **job** that outlives your SSH session. A transient systemd unit runs it; you can disconnect
  and read the result later.

## Quick start

```sh
# once per machine: register a server you can already SSH into with a trusted host key
shiplord server add production --ssh appuser@server.example.com

# once per server: installs the helper, wires Caddy and sshd (re-running changes nothing)
shiplord server bootstrap production

# once per application
shiplord init orders --template google-adk
cd orders
shiplord configure --server production --api-domain orders-api.example.com \
  --frontend https://orders.example.com           # or --no-frontend
shiplord provision                                   # registers your SSH key for this application
shiplord env set --from-file .env.production         # runtime secrets; values are never printed
shiplord restart                                     # apply the environment to the running release
shiplord registry login --username bot --token-file t   # only for a private image: a read-only pull login

# every release
shiplord plan --image ghcr.io/owner/orders@sha256:<digest> --commit <sha>   # read-only
shiplord deploy --image ghcr.io/owner/orders@sha256:<digest> --commit <sha>
shiplord status
shiplord logs --tail 100
shiplord rollback
shiplord recover
```

Create the DNS record for `--api-domain` at your DNS provider; Shiplord checks DNS and reports it but never
changes it.

### Releasing a native binary

For a native Linux binary, configure with `--runtime native` once, then build and deploy. Shiplord does not
build anything; it uploads the file you name and verifies it.

```sh
# 1. build a Linux ELF for the server's CPU (x86-64 shown; use arm64 on an arm64 server)
GOOS=linux GOARCH=amd64 go build -trimpath -o dist/orders ./cmd/orders

# 2. deploy that exact file, tagged with the commit it was built from
shiplord deploy --binary ./dist/orders --commit "$(git rev-parse HEAD)"
```

The second command is the whole release. `--commit` must be the full 40-character lowercase commit SHA of a
SHA-1 repository; `git rev-parse HEAD` prints exactly that. Shiplord accepts exactly 40 lowercase hex characters,
so a SHA-256 repository's 64-character id is refused. If you omit `--commit`, `shiplord` runs `git rev-parse HEAD`
itself in the application directory and refuses with `could not read HEAD; pass --commit` when that fails; pass it
explicitly when the binary was built from another checkout. `--binary` is
refused for a Docker manifest, and `--image` is refused for a native one.

Before it connects, the CLI reads the file and refuses it unless all of these hold:

- It is between 1 byte and 1 GiB inclusive.
- It starts with the ELF magic (`0x7f` then `ELF`), is 64-bit (ELF class 2), and is little-endian (ELF data 1).
- Its ELF type is 2 (executable) or 3 (position-independent executable).
- Its `e_machine` is `0x3e` (x86-64) or `0xb7` (arm64). Any other CPU architecture is refused.

The server maps `x86_64`/`amd64` to `linux-amd64` and `aarch64`/`arm64` to `linux-arm64` (anything else defaults
to `linux-amd64`), compares that with the binary's architecture, and refuses the upload before accepting it when
they differ. It checks the declared size and SHA-256, and the same ELF header, again before installing the
release. A macOS or Windows binary, a 32-bit binary, or a binary built for the other architecture cannot be
deployed.

To see what the server would do without uploading anything, run the read-only preview. Releases are ordered by
the commit's committer timestamp (`git show -s --format=%ct <sha>`); override it with `--sequence` only when two
releases need a different order. `--sequence` must not be negative.

```sh
shiplord plan --binary ./dist/orders --commit "$(git rev-parse HEAD)"
shiplord deploy --binary ./dist/orders --commit "$(git rev-parse HEAD)" --sequence 1763000000
```

The binary is uploaded over the same SSH connection, and runs as an unprivileged account under a confined
systemd unit.

## Existing Docker applications

Use the generic profile to deploy an existing image without Google ADK settings. In a terminal, run
`shiplord configure` with no options in the repository. It lists your registered servers and asks only for what is
missing. When the repository has a `Dockerfile` (or `Containerfile`), it suggests `generic` and the port from the
final stage's `EXPOSE`. You confirm a summary before anything is written. Without a terminal (agents, CI, pipes)
nothing is asked, and every value comes from options:

```sh
shiplord configure --name handwriting --server production \
  --api-domain handwriting.example.com --no-frontend --template generic \
  --container-port 3000 --health-path /health --memory 512
```

This creates `template = "generic"` and `runtime = "docker"`. Without those options the defaults remain 8080,
`/ready`, and 512 MiB, and the repository is not inspected. Shiplord picks the server-side port and routes the
hostname to it, so `--container-port` is only the port your application listens on inside its container. The
image must start its own HTTP service, listen on `0.0.0.0` at `http.container_port` (Shiplord also sets `PORT` to
that value), and return HTTP 200 at `http.readiness_path`.
An image can serve both its website and API at the configured hostname.

Both profiles receive `HOST`, `PORT`, and `LOG_LEVEL`. Only `google-adk` receives Shiplord's `AGENT_DIR`,
`AGENT_NAME`, `RELOAD_AGENTS`, `SERVE_WEB_INTERFACE`, and `ALLOW_ORIGINS` settings. Generic images keep
their own defaults for those five variables and must implement their own CORS behavior when needed.
All managed variable names remain reserved in `shiplord env set`, including ADK names, because environment
files belong to the application and can survive a profile change.

`configure` defaults to `google-adk` for compatibility. `init` still scaffolds only `google-adk`, and native
binaries still require that profile. `github setup` generates the ADK template's workflow and refuses generic
applications before creating keys or changing GitHub; use a custom workflow that builds your image, runs
your application's checks, and invokes `shiplord deploy` by digest.

The CLI requires the helper capability `docker_generic_v1` before submitting a generic plan or deploy.
Upgrade with `shiplord server bootstrap` when it is missing. Do not downgrade to a helper that cannot read
generic release records. Changing the profile replaces the container even with the same image, commit,
and sequence; rollback and journal recovery restore the recorded profile. Containers without a profile
label are interpreted as legacy `google-adk` containers.

## Persistent storage (Docker)

Each Docker application has one named volume, `shiplord-<application>-artifacts`, mounted at `/app/src/.adk` by
default. To mount it elsewhere, add a table to `ship.toml`:

```toml
[storage]
container_path = "/data"
```

The path must be a canonical absolute directory path (letters, digits, `.`, `_`, `-`; no `..`, trailing or repeated
slashes), and may not be, or be inside, `/proc`, `/sys`, `/dev`, `/run`, `/etc`, `/bin`, `/sbin`, `/usr`, `/lib`,
`/lib64`, `/boot`, `/root`, `/var/run` or `/var/lock`. Native applications reject `[storage]`.

- Changing the path mounts the **same** volume at the new location. No files move and nothing is migrated:
  relative file names, including SQLite databases, stay where they were. `shiplord plan` shows the old and new path.
- A mount can hide files the image keeps at that path; check the destination against your image.
- Rollback restores the path the earlier release recorded. It does not roll back data, permissions or schema.
- Shiplord refuses a volume that is not a plain local volume labelled for the application. It creates the volume on
  the first deploy (never during `plan`) and never removes it.
- Using `[storage]` needs a helper that advertises `docker_storage_v1`; a newer CLI refuses to submit it to an
  older helper (re-run `shiplord server bootstrap`). A helper must not be downgraded after deploying records that
  use `[storage]`.
- `owner_uid` / `owner_gid` are validated but **not yet supported**: the helper rejects them, because establishing
  ownership safely needs a pinned initializer image that has not been built and reviewed.
- An image can establish the mount directory's ownership during its build. Verify a fresh volume is writable
  by the image's application user with real Docker before deployment. Shiplord does not repair existing
  volume permissions, and changing the mount path does not migrate data from another application.

## Application data

Each application gets exactly one persistent location, and Shiplord leaves it alone. This is the supported
data story for 1.0; [section 16 of the plan](../ship-cli-plan.md) records the decision and why a database
container is not part of it.

- **Docker:** the named volume `shiplord-<application>-artifacts`, mounted at `/app/src/.adk` by default or at
  `[storage].container_path`.
- **Native:** `/var/lib/shiplord/data/<application>`, which is the service's working directory and its only
  persistent writable path.

A SQLite database and any files the app writes belong there. Shiplord creates the location (the volume at the
first deploy, the directory at provision), mounts it at the same path on every release, and never deletes it:
not on a failed candidate, a rollback, `deprovision --retire`, or a helper upgrade. `shiplord deprovision`
reports the data it kept.

The application is responsible for:

- Keeping the database and its files under the mount. Anything written elsewhere in a container is lost when
  the container is replaced.
- Opening SQLite in WAL mode with a busy timeout. There is one writer; WAL lets readers and the writer
  work at the same time.
- Owning the mount, or creating its files as the user the image runs as. Shiplord does not initialise
  ownership: `[storage] owner_uid` / `owner_gid` are validated and then refused (#64). An image that runs as
  non-root should create the directory with the right owner in its build.
- Running its own migrations at startup or in a build step. Shiplord never runs a migration and never rolls one
  back. A rollback restores the previous image, which must still be able to read the current schema, so keep
  migrations backward compatible for at least the two releases Shiplord keeps.

What this does **not** cover:

- Concurrent writers, a second process, or more than one instance of the app. Shiplord runs one container or
  one service per application and refuses a second live container; two writers would corrupt the file.
- Postgres, MySQL, or any database server. Point the app at a database outside the server (a managed service,
  or one you run yourself) and set its connection string with `shiplord env set`. Shiplord passes the value
  and does not manage the database.
- Sharing data between applications. There is one location per application and no shared volume.
- Encryption at rest, replication, high availability, or point-in-time recovery.
- Moving data when `[storage].container_path` changes. The same volume is mounted at the new path; nothing is
  copied.

### Backup and restore

There is **no supported backup command yet**. #62 adds `shiplord backup` and `shiplord restore`; this section
will be updated when it ships. Do not work around it by copying a live SQLite file with `tar`, `cp`, or a
volume plugin while the app is writing: the copy can capture a torn write and will not restore cleanly.
`shiplord rollback` does not undo data, so take a copy before a release that changes the schema.

The contract those commands will implement is a **cold copy**, because that is the only safe way to copy a
SQLite database without a database-aware snapshot:

1. Stop the application, with the same journaled stop a deploy uses.
2. Copy the volume or data directory into a timestamped archive with a checksum.
3. Start the application again and verify readiness.

A backup is therefore a short outage, and the archive lands on the server. A copy on the same host protects
against an application or operator mistake, not against losing the disk or the server; copy the archive
somewhere else yourself.

On a default Docker install the volume's files are under
`/var/lib/docker/volumes/shiplord-<application>-artifacts/_data`, and a native app's files are under
`/var/lib/shiplord/data/<application>`. Both are owned by the user the application runs as, not by your
login, so inspect them with the privileges you already use for that host.

## Graceful replacement (Docker)

Replacing a Docker release now signals the old container, waits, and only then removes it and starts the new one:

```toml
[deploy]
shutdown_timeout_seconds = 120   # 1-300; default 10
shutdown_timeout_action = "fail" # "force" (default) or "fail"
```

- The container's stop signal (image `STOPSIGNAL`, default SIGTERM) is sent after its restart policy is turned off.
  Recovery may resend it if a worker stops before recording that Docker accepted it. SIGKILL/SIGSTOP are refused as
  graceful signals.
- At the deadline, `force` sends an explicit SIGKILL and reports `forced: true`; `fail` kills and removes nothing, starts no
  candidate, keeps the journal and exits 6 (`intervention_required`). Run `shiplord recover` once the process has exited;
  recovery will not clear the journal while it is still draining. Daemon or inspection errors never trigger force.
- The **outgoing** container's recorded policy governs its stop (containers carry it as labels; older containers are 10 s /
  `force`). `shiplord plan` shows both. Neither policy guarantees an application's jobs complete.
- Two live containers for one application are refused before anything is signalled.
- Retirement removes the route first; if the stop then fails, the route stays gone, registration and data are kept.
- An explicit policy is refused if its worst-case time (lock wait, pull, both stops, both readiness waits) exceeds the
  3600 s job limit. Needs helper capability `docker_shutdown_v1`; do not downgrade a helper after using it.
- The shutdown journal persists the original deadline. Recovery uses the time remaining rather than starting a new
  timeout period.

## Deploying from GitHub Actions

```sh
shiplord github setup --repo owner/orders --tooling-ref <commit of Shiplord pushed to GitHub>
git add .github/workflows/shiplord-release.yml ship.toml && git commit   # merge to the deployment branch
shiplord github check-ci            # joins Tailscale and logs in with the CI key; builds and deploys nothing
shiplord github status --check-server
shiplord github rotate-key
```

`github setup` uses your authenticated `gh`. It creates a dedicated Ed25519 CI key, registers it on the server,
**proves it authenticates over SSH against the server's pinned host keys** (read over your already-trusted
connection, never from `ssh-keyscan`), and only then stores it as the `SHIPLORD_SSH_PRIVATE_KEY` secret (through
stdin) and sets the `SHIPLORD_*` repository variables. It then writes `.github/workflows/shiplord-release.yml`:
the template's quality checks, an image build, and `shiplord deploy` by digest from a runner joined to your tailnet.
It is resumable, keeps the private key only until GitHub has it, and never uploads your personal key.

Two things stay with you because they need authority Shiplord does not have: the **Tailscale trust credential** and
**DNS**. For Tailscale, `github setup` guides you instead: it reads the OIDC subject GitHub will actually present (repositories
with immutable subjects use `repo:Owner@<id>/name@<id>`, not the plain names), prints the policy line and the
credential settings with every value filled in, asks for the client ID and audience (not secrets), checks they belong
together, and saves them so the next application's setup does not ask again. Use the owner-wide subject it suggests,
`repo:Owner@<id>/*:ref:refs/heads/main`, to do this once for every repository. `--tailscale-client-id` and
`--tailscale-audience` skip the prompt. `github check-ci` then proves the whole path with a check-only run. The generated workflow pins every action to a commit, never cancels a running
production deploy, and gives `id-token: write` only to the jobs that need it. `rotate-key` adds and proves a new key,
updates GitHub, and only then revokes the old one. It finds the key to replace in the server's authorized-keys list,
so it works from a machine that never ran `github setup`; local setup state is kept per server and application.

CI runs `shiplord` in pinned mode: `SHIPLORD_SSH_TARGET`, `SHIPLORD_SSH_IDENTITY`, `SHIPLORD_SSH_KNOWN_HOSTS_FILE`
(and optionally `SHIPLORD_SSH_PORT`) replace the server alias, and SSH ignores `~/.ssh/config` entirely.

### The Shiplord deploy action

The deploy job's steps — join the tailnet, install the CI SSH identity, deploy by digest, write the job summary —
are also published as a composite action, so a pipeline you already have can add one step instead of replacing
its workflow:

```yaml
      - uses: QueryPlanner/shiplord/deploy@<commit sha> # v1
        with:
          check-only: false
          ssh-private-key: ${{ secrets.SHIPLORD_SSH_PRIVATE_KEY }}
          ssh-known-hosts: ${{ vars.SHIPLORD_SSH_KNOWN_HOSTS }}
          deploy-user: ${{ vars.SHIPLORD_DEPLOY_USER }}
          server-host: ${{ vars.SHIPLORD_SERVER_HOST }}
          ssh-port: ${{ vars.SHIPLORD_SSH_PORT }}
          ts-client-id: ${{ vars.SHIPLORD_TS_CLIENT_ID }}
          ts-audience: ${{ vars.SHIPLORD_TS_AUDIENCE }}
          ts-tags: ${{ vars.SHIPLORD_TS_TAGS || 'tag:ci' }}
          image: ghcr.io/owner/orders
          digest: ${{ steps.build.outputs.digest }}
          commit: ${{ github.sha }}
          request-id: gh-${{ github.run_id }}-${{ github.run_attempt }}
```

**`v1` is not tagged or published yet.** Until it is, pin the action to a full commit SHA of a commit that
contains `deploy/action.yml`; `shiplord github setup` pins the generated workflow to the same commit it installs.
The action installs nothing itself, so the job must already have `shiplord` on `PATH` (the generated workflow
installs the pinned commit) and must be a Linux job with `id-token: write` for the tailnet join.

| Input | Required | Purpose |
| --- | --- | --- |
| `check-only` | no (`false`) | Check Tailscale, SSH and the helper with `shiplord status`; deploy nothing |
| `ssh-private-key` | yes | The CI key; the `SHIPLORD_SSH_PRIVATE_KEY` secret |
| `ssh-known-hosts` | yes | Pinned host keys; the `SHIPLORD_SSH_KNOWN_HOSTS` variable |
| `deploy-user` | yes | The account to deploy as; the `SHIPLORD_DEPLOY_USER` variable |
| `server-host` | yes | The server's tailnet host name; the `SHIPLORD_SERVER_HOST` variable |
| `ssh-port` | no (empty) | The SSH port when it is not 22 |
| `ts-client-id` | yes | The Tailscale OIDC client ID; the `SHIPLORD_TS_CLIENT_ID` variable |
| `ts-audience` | yes | The Tailscale OIDC audience; the `SHIPLORD_TS_AUDIENCE` variable |
| `ts-tags` | no (`tag:ci`) | The tailnet tags the runner joins with |
| `image` | yes | The image repository, without a digest (empty for a check-only run) |
| `digest` | yes | The image digest to deploy (empty for a check-only run) |
| `commit` | yes | The full commit SHA the release records |
| `request-id` | yes | An idempotency key, so a retried run never deploys twice |

The key is written to a file under `RUNNER_TEMP` with `umask 077`, passed to `shiplord` through the environment
(never as an argument), and removed in a final `always()` step. The action declares no outputs, so no secret can
reach the run page or the calling job.

## Commands

| Command | Purpose |
| --- | --- |
| `server add <alias> --ssh user@host` | Register a trusted SSH login. Never accepts or updates host keys. |
| `server list` | Show the registered aliases and their SSH targets. Local only; contacts nothing. |
| `server remove <alias>` | Delete an alias. Refuses while the current directory's `ship.toml` names it. |
| `server rename <old> <new>` | Rename an alias. Refuses when the new alias already exists. |
| `server bootstrap <alias>` | Install or update the helper on the server. Idempotent. |
| `init`, `configure` | Create a project from the pinned template; create `ship.toml` (asks for missing values in a terminal; `--json` never prompts). |
| `provision [--update]` | Create the application's account, state, port, and key access on the server. `--update` applies a changed hostname or memory ceiling without removing state or keys. |
| `env set --from-file F` / `env diff --from-file F` / `env keys` / `env clear` | Manage runtime environment. `set` refuses an empty file; `diff` previews the key names it would add or remove without reading values; `keys` lists names only. |
| `plan [--image I \| --binary B] --commit C` | Local preview, or with an artifact, what the server would do. Changes nothing. |
| `deploy --image I \| --binary B --commit C` | Deploy a built artifact. `--request-id` makes retries safe. |
| `status`, `logs --tail N` | Release state, running state, unfinished work; bounded logs. `status --job-id <id>` reads one job by ID, including its phase or terminal outcome. |
| `rollback`, `recover [APP --server ALIAS]` | Restore the previous release; reconcile an interrupted deployment, from an app directory or by named target. |
| `restart` | Restart the deployed release so it reads the current environment. Refused when nothing is deployed. |
| `registry login --username U --token-file F \| --token-stdin` / `logout` / `status` | A per-application read-only pull login for private images. There is no `--token` option. |
| `github setup --repo R` / `check-ci` / `status [--check-server]` / `rotate-key` | CI key, secret and variables, guided Tailscale setup, release workflow; a check-only CI run. |
| `doctor --local \| --server <alias> [--deep] \| --all-servers \| (in an app directory)` | Prerequisites; helper probe (`--deep`: OS baseline, Caddy, sshd, Docker, disk, each application); every registered server read-only; access, helper, DNS. |

Every command that reports a result accepts `--json`. No output contains private keys, secret values, or complete
environments.

Shiplord keeps no registry of manifests, so `server remove` can only check the `ship.toml` in the directory where you
run it. A manifest elsewhere that names the alias is not detected; update or remove that manifest first. Renaming does
not edit any manifest, so a manifest that names the old alias must be updated to the new one.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Succeeded. A deployment is verified ready. |
| 1 | The operation failed; read the result. Also: stale release, recovery required. |
| 2 | Invalid arguments, configuration, or request. |
| 3 | A prerequisite is missing: SSH trust, credentials, permissions, sudo. |
| 4 | Incompatible CLI and helper protocol versions. |
| 5 | The deployment failed and the previous release was restored. Still a failure. |
| 6 | Recovery failed or manual intervention is required; an interrupted job is also reported as 6. |

## What a deployment does

1. The helper checks the request against the application's registration (hostname, runtime, image repository)
   and refuses anything that would change them.
2. **Prepare, without touching the running app:** pull the image by digest (platform and revision label
   verified) or verify and install the uploaded binary. A failure here changes nothing.
3. **Journal** the transaction, replace the runtime (a brief restart window), poll the readiness path.
4. Apply the Caddy route only if it changed, verify the active configuration routes the hostname, then commit.
5. On any failure, restore the last committed release, verify it, and only then clear the journal. The command
   still exits nonzero. A first release that fails leaves no runtime and no route.
6. A candidate that failed leaves nothing behind; the image or binary of a release that leaves the
   two-release window is removed.

Releases are ordered by the commit's committer timestamp (`--sequence` overrides). An honest client's queued older
release cannot replace a newer one (the server refuses it); the check relies on the sequence the caller supplies, and
`--sequence`/`allow_older` exist, so it protects against mistakes, not against a malicious account. Use `rollback` to go
back deliberately. Database and persistent data are never
rolled back.

If a worker is killed mid-deployment the journal remains, further deployments are refused with
`recovery_required`, and `shiplord recover` restores the last committed release. `shiplord status` prints the
named form of that command, so recovery works from any directory without a checkout:
`shiplord recover <app> --server <alias>`.

### Applying an environment change

`shiplord env set` writes the application's environment file; the running process keeps the values it was created
with. `shiplord restart` applies the current file to the deployed release without a new commit: it runs the same
journaled transaction (replace the runtime, re-apply the route, verify readiness), leaves the recorded release and
its rollback target unchanged, and needs no artifact. It is refused when nothing is deployed (exit 2) and while an
unfinished transaction exists (`recovery_required`). A redeploy of the same release is also applied rather than
reported `unchanged` when the file no longer matches the running process.

A restart that does not come back reports `intervention_required` (exit 6) and keeps the journal, because the
runtime may already have been replaced. It is never reported as a rollback: the "previous" release record would run
the same environment file that just failed. Fix the value and run `shiplord recover`.

## Security model

- Per-application deployment accounts; authorized keys are root-owned and carry `command=...,restrict`.
- The dispatcher never evaluates the client's command line; sudo permits one fixed helper with no arguments.
- Application ports bind to loopback only; Caddy is the only public entry point.
- Runtime environment files are root-only; secrets never appear in argv, logs, results, or generated files.
- Host keys are pinned (`StrictHostKeyChecking=yes`); a changed key fails closed.
- Containers run with a memory limit, bounded logs, `no-new-privileges` and all capabilities dropped; native
  services run as a separate unprivileged account with `ProtectSystem=strict` and no capabilities.

### Limits of the confinement

- A deployment account may request at most the application's registered memory ceiling (default 4096 MiB;
  `provision --max-memory-mb`), and only the current and previous releases are kept (`retain_successful_releases` must be 2).
- Native services may bind only their own port (`SocketBindAllow`), but **can still connect to other loopback services**
  on the host, including Caddy's admin API on `127.0.0.1:2019`. Docker applications share Docker's default bridge.
  Do not run an untrusted application on a host that holds anything else you care about; for stronger isolation move
  Caddy's admin endpoint to a unix socket.
- A deployment account cannot be used to run arbitrary code as root, but anyone holding its key can deploy whatever
  that application's registration allows.

## Changing an application

Edit `ship.toml`, then apply the change without removing the application:

```sh
shiplord provision --update
```

`--update` applies a changed `http.api_domain` and a changed memory ceiling (`--max-memory-mb`). It keeps the
application's account, state, environment, keys, releases, and data. Changing the runtime or the image repository is
refused before anything is touched; deprovision and provision the application again for those changes. If a release is
deployed and the hostname changed, the public route is moved immediately; point the new name's DNS record at the
server.

## Removing an application

`shiplord deprovision --yes` removes the account, keys, registration and state of an application that has no release
deployed. `--retire` first removes the route, stops the runtime and discards its releases. Application data (the data
directory, runtime account and Docker volume) is **never** deleted; remove it by hand when you are sure.

From outside the application's checkout, name it and its server together:
`shiplord deprovision orders --server hetzner-vm --yes`. Both selectors are required, they are used even when the
current directory has a `ship.toml` for another application, and no manifest is read.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v          # unit tests; real-host suites are skipped
tests/linux/run.sh all                            # real sshd, sudo, Caddy, Docker, systemd in containers
```

`tests/linux/run.sh <unit|caddy|host|docker|e2e|systemd>` runs one suite; see the script header. These suites
change `/etc` and system accounts, so they refuse to run outside a disposable container or CI runner.

The server-side helper code is stdlib-only and is installed from the same source as the CLI. See
[protocol.md](protocol.md) for the wire protocol and [operations.md](operations.md) for the
host layout and troubleshooting.
