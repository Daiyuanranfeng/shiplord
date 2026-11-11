# Design

## Context

See `proposal.md` - Why. Both runtimes answer "is this release already running?" from host state, and
`Deployer._reconcile` uses that answer to decide whether to start the target after a failed candidate or a recovery:

- `DockerRuntime.is_running` reads one `docker inspect` of the release's container name and compares the running
  state, image digest, shutdown labels, template label, and mounts.
- `NativeRuntime.is_running` reads the release marker from the installed unit and asks systemd for the active state.
- `release_id` is derived from commit, artifact identity, and sequence only, so a configuration-only redeploy keeps
  the container name and the unit path.

The values a start consumes are already fixed in one place for Docker (`_start`'s `docker run` argv) and one place
for native (`render_unit` and `render_environment`). The gap is only that the running check reads less than the start
writes. `Deployer` itself needs no change.

## Goals / Non-Goals

**Goals:**

- A restore or recovery always leaves the committed configuration actually running.
- One definition of the managed environment, used by both the start and the check.
- A deterministic answer for values an older helper may not have recorded.
- No environment value leaves the process.

**Non-Goals:**

- Making `release_id` include configuration. That would change container names, unit paths, retention, and every
  existing release record; a restart of the same name is the smaller and safer fix.
- Comparing values a start does not control, such as image-declared `ENV`, `PATH`, or anonymous mounts.
- Changing outcomes, exit codes, journal phases, or the `rolled_back` / `intervention_required` meanings.
- A retry or fallback when the check cannot read host state.

## Decisions

### Docker: compare the run's values, not its argv

`is_running` keeps one `docker inspect` call and extends its template with the fields Docker records for the run:

| Start input | Inspected value |
| --- | --- |
| image | `.Config.Image` (digest only, as today) |
| shutdown policy | the two `shiplord.shutdown_*` labels (as today) |
| template | `shiplord.template` label (as today) |
| storage mount | `.Mounts` (as today) |
| `--memory`, `--memory-swap` | `.HostConfig.Memory`, `.HostConfig.MemorySwap` |
| `--pids-limit` | `.HostConfig.PidsLimit` |
| `--restart` | `.HostConfig.RestartPolicy.Name` |
| `--security-opt`, `--cap-drop` | `.HostConfig.SecurityOpt`, `.HostConfig.CapDrop` |
| `--log-driver`, `--log-opt` | `.HostConfig.LogConfig.Type`, `.HostConfig.LogConfig.Config` |
| `--publish` | `.HostConfig.PortBindings` |
| `--env-file` + `--env` | `.Config.Env` |

The environment is the JSON-encoded last field of the template, and the output is split with a bounded `maxsplit`, so
a value containing `|` cannot shift the other fields. It is parsed into a dict and compared as a subset: every key
the start sets (managed values plus every key in the custom environment file) must have its exact value. Extra keys
are tolerated because `.Config.Env` also contains the image's own `ENV` and the base image's `PATH`, which the start
does not control and cannot distinguish from a removed custom key. This still detects every managed change (port,
origins, host, agent values) and every added or changed custom value.

The fixed run values (`PIDS_LIMIT`, `RESTART_POLICY`, `LOG_DRIVER`, `LOG_OPTIONS`, `SECURITY_OPTIONS`, `CAP_DROP`)
move to module constants used by `_start` and the check. The managed environment moves to one
`_managed_environment(registration, configuration)` helper used by both. `_start` therefore cannot drift from what
the check expects.

Alternative considered: a `docker inspect` JSON parse of the whole object. Rejected: it transfers image history and
other large fields on every readiness poll, and the existing call already uses a narrow template.

Alternative considered: exact equality of `.Config.Env`. Rejected: the image's own `ENV` is part of it, so every
container would look stopped for a reason unrelated to configuration.

### Native: compare the installed files with the rendered files

`is_running` reads the unit and the installed `native.env` as bytes and compares them with `render_unit(layout,
registration, release)` and `render_environment(registration, release, custom)`, where `custom` comes from the
existing `load_environment_file` for `app.env`. This covers memory (`MemoryMax`), the socket bind
(`SocketBindAllow`), origins (`ALLOW_ORIGINS`), every other managed value, and any custom value change. The full
comparison subsumes the release marker, so the marker no longer needs a separate check.

The environment file is compared to the rendering rather than parsed and diffed: the renderer is the single source
of what the start writes, and byte equality also catches a missing file, a stale managed value, and a custom value
that changed.

### Absent host state means "not running"; an unreadable environment file does not

Any value that is missing, unparseable, or absent (a `null` security-option list, a missing label, a `docker inspect`
that fails) makes the check return `False`. A restart is always safe: it writes the committed configuration. This is
deliberately stricter than treating absence as a match, because the only case absence has today is a container or unit
this helper did not write, and those must be replaced to be trusted.

The two existing tolerances are kept because they are documented compatibility rules, not configuration:
a container with neither shutdown label uses the default policy, and a container with no template label is the
`google-adk` contract. Those containers already have every other inspected value because `_start` has always set
them.

The application's `app.env` is different in kind: it is operator input, and `_start` reads it *before* it stops
anything, so a corrupt file fails the start without touching the running release. Treating an unreadable file as
"not running" therefore made a restore attempt a start that could only fail on the same file, turning an existing
`rolled_back` into `intervention_required` while the committed release was still serving. So a corrupt or unreadable
`app.env` (`EnvironmentError_`) skips the custom part of the comparison instead: every record-derived value and every
managed environment value is still compared, so a candidate that replaced them is caught, and an unchanged release is
reported as running. For native releases the same rule applies to the `native.env` comparison, which cannot be
rendered without `app.env`; the unit file is still compared.

### Environment values stay in memory

The comparison is a boolean. No key or value is put into a `RuntimeFailure`, a `detail`, a job result, or a test
fixture. Tests use obvious placeholder values such as `CUSTOM_TOKEN=placeholder`, never a credential.

## Risks / Trade-offs

- [Every `is_running` now reads the application's environment file] -> One small local read per call; readiness
  polling already makes a loopback HTTP request per attempt. Accepted.
- [A changed custom environment value triggers a restart during a restore] -> That is the correct outcome: the
  committed release must run the committed environment.
- [A removed custom key is not detected] -> It cannot be distinguished from an image-declared key without knowing the
  image's `ENV`; the managed values and every present custom key are still compared. Recorded as a limitation.
- [An image's own `ENV` changes with the same digest] -> Impossible: a digest names immutable content.
- [An old container created by a helper that omitted a run flag is now restarted] -> A restart is safe and produces
  the committed configuration; it is the intended recovery, not a spurious stop.

## Migration Plan

No migration. The next restore, recovery, or deploy of an existing application compares the new fields; a container
that lacks them is restarted once into the committed configuration. The real-host Docker and systemd suites must be
run, because this is runtime behaviour against a real daemon and real systemd.
