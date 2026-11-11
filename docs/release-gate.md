# The 1.0 release gate

This is the decision record for issue #76: the seven criteria that must pass unattended before Shiplord is 1.0.
It is written against `main` at `97047f1` (version 0.2.0, 2026-11-11). It does not replace
[`docs/evidence.md`](evidence.md); it names the exact run or test behind each criterion and says plainly which
ones are not proven today.

A status means:

- **Verified** — the criterion as written was run end to end, and the result is recorded.
- **Partly verified** — a constituent behavior is proven by a named test or run, but the criterion as written is not.
- **Not verified** — no run or test proves the criterion as written.

Nothing below is inferred. Where a run was not performed, the status says so.

<!-- gate-index
suites: all caddy docker e2e host systemd unit
ci_jobs: caddy-integration docker-integration host-integration linux-e2e linux-systemd ssh-integration sudo-integration
-->

## The seven criteria

| # | Criterion | Status | Evidence | What is missing |
| --- | --- | --- | --- | --- |
| 1 | `tests/linux/run.sh all` is green in CI on every pull request, not only locally. | Partly verified | `.github/workflows/test.yml` triggers on every pull request and runs the real-host suites: `linux-e2e` and `linux-systemd` invoke `tests/linux/run.sh e2e` and `tests/linux/run.sh systemd`; `ssh-integration`, `sudo-integration`, `caddy-integration`, `host-integration`, and `docker-integration` run the same test modules directly. The latest completed `main` run (42913399156, `97047f1`) had all 12 jobs green. | No CI job runs `tests/linux/run.sh all`, and `run.sh unit` is not a CI step. The exact command is also not green as shipped: on 2026-11-11 `tests/linux/run.sh unit` ran the root integration 8/8 OK, then the unprivileged suite reported 1126 tests with 3 failures and 1 error (115 skipped): `test_routing.ConflictTests.test_followed_imports_are_inspected` for three arguments and `test_keys.StoreTests.test_a_symlinked_key_directory_is_refused`. The container's `tester` account has umask 0002, so tests that create a directory with default permissions produce a group-writable directory that the production code correctly refuses. The same suite passed 1126 tests (115 skipped) with `umask 0022`. `run.sh all` was not run for this document. |
| 2 | A scripted run on a fresh VM goes from `server add` to a public HTTPS 200 for three apps — the ADK template, an existing Dockerfile app, and a native binary — and each is then released by GitHub Actions. | Not verified | `docs/evidence.md` "Real server run (Hetzner, 2026-10-28/29)" records one native Go app (`hello`) from `server bootstrap` to public HTTPS, plus one GitHub Actions deploy. `tests/linux/run.sh e2e` provisions an ADK app and a generic app beside it (`tests/test_e2e.py::test_23_a_generic_application_is_isolated_from_the_adk_application`) with real sshd, sudo, Caddy, and Docker in a container. | No scripted fresh-VM run starts at `server add` and ends at a public HTTPS 200 for all three app types. No GitHub Actions release exists for each of the three. On `main`, `github setup` refuses to generate a workflow for a native application (`tests/test_github.py::test_native_applications_get_credentials_but_no_generated_workflow`); open PR #97 adds that generation. |
| 3 | For each of those apps: a failing release rolls back and its crash output is in the result; a reboot mid-deploy recovers by itself; `env set` followed by `restart` changes the running value. | Partly verified | Rollback and crash output are proven for Docker: `tests/test_deploy_integration.py::test_lifecycle` (real Docker, `docker-integration`) asserts `rolled_back`, exit 5, and the crash text in `candidate_logs.output` for an unhealthy and a crashing candidate; `tests/test_deploy.py::FailedCandidateLogTests::test_a_candidate_that_fails_readiness_reports_its_last_log_lines` asserts the same result shape. Rollback is also proven on real systemd (`tests/test_e2e_systemd.py::test_06_failing_releases_roll_back_with_the_real_cause`) and on real Docker plus Caddy (`tests/test_e2e.py::test_09_an_unhealthy_release_exits_5_and_leaves_the_previous_one_serving`). `shiplord restart` exists on `main` (PR #145), and `tests/test_deploy_integration.py::RealDockerDeployTests::test_restart_applies_a_changed_environment_without_a_new_release` proves on real Docker that an environment change reaches the running process through an authorized restart without a new release. | Two parts are missing. Native crash output: `tests/test_deploy.py::FailedCandidateLogTests::test_a_native_candidate_never_reports_container_logs` asserts `candidate_logs` is absent for a native candidate, so the native app fails this sub-criterion. Reboot: `docs/evidence.md` says a real reboot was not tested, and `main` has no boot-recovery unit; open PR #140 adds one, but its test uses `docker restart`, not a power cut. The env-change restart is proven on real Docker, but not for the three apps on a fresh VM. None of the three apps was exercised on a fresh VM. |
| 4 | A second machine can run `status`, `logs`, `rollback` and `github rotate-key` for an app set up elsewhere. | Partly verified | `tests/test_cli.py::test_status_and_logs_accept_a_named_app_and_keep_status_json_shape` proves `status APP --server` and `logs APP --server`. `tests/test_e2e.py::test_17_the_ci_credentials_alone_can_operate_the_application_over_the_pinned_host_keys` proves the pinned CI credential set can run `status` with no `~/.ssh/config`. `tests/test_github.py::test_rotation_works_from_a_machine_that_never_ran_setup` proves `github rotate-key` without local setup state. | No second machine ran all four commands against an app set up elsewhere. `rollback` accepts no application name and reads `ship.toml` from the current directory (`src/shiplord/cli.py`), so its second-machine path is unproven. `logs` has the named-app form but was not run from a second machine either. |
| 5 | Every command has readable output and a `--json` form, and the exit-code table in `AGENTS.md` is true. | Partly verified | Most result-reporting commands accept `--json` and have a readable form; `tests/test_cli.py` asserts exit codes 0 through 6 in both forms, and `tests/test_client.py` asserts the protocol-to-exit mappings (including 3 and 4). | `init`, `configure`, and `server add` have no `--json` form (`src/shiplord/cli.py`). `provision`, `server bootstrap`, `rollback`, `recover`, `env`, `registry`, and `github` print a `key: value` dictionary dump rather than purpose-built readable output. No single test asserts the `AGENTS.md` table row by row. Open PR #108 adds the missing `--json` forms, renderers, and a golden output table; it is unmerged. |
| 6 | `README.md` covers install from PyPI, the three application types, and the supported server operating systems. | Partly verified | `README.md` covers all three application types: the `google-adk` template with `init`, an existing Docker app with `configure` and the `generic` profile, and a native binary with `--runtime native` and `deploy --binary`. PyPI has `shiplord` 0.2.0. | `README.md` installs with `python -m pip install git+https://...`, not from PyPI. It does not list supported server operating systems; `docs/guide.md` explains the Python 3.11 requirement and that Ubuntu 22.04 and Debian 11 are too old, but the README only says "a Linux server". Open PR #141 adds the baseline (Ubuntu 24.04 or later, Debian 12 or later) to the README. |
| 7 | `docs/evidence.md` maps each criterion above to the run or test that proves it. | Not verified | `docs/evidence.md` currently maps `ship-cli-plan.md` §13, not the seven criteria in issue #76. | This pull request adds the mapping to `docs/evidence.md` and this document. On `main` as it stands the criterion is not met. Once this PR lands the map exists, but it records that criteria 1 through 6 are not met, so it does not make 1.0 ready. |

## Verdict

**1.0 is not ready.** None of the seven criteria is fully verified. Five are partly verified and two are not
verified.

What must happen first:

1. **Criterion 1:** add a CI job that runs `tests/linux/run.sh all` on every pull request, and make the harness
   independent of the caller's umask (set `umask 0022` in `tests/linux/run.sh` or fix the two tests that create
   default-mode directories). Observe it green on a pull request. If the project prefers the existing per-suite
   jobs, change the criterion in issue #76 explicitly rather than calling it met.
2. **Criterion 2:** merge #97 for native workflow generation, then run the scripted fresh-VM journey for the ADK
   template, an existing Dockerfile app, and a native binary, and record the public HTTPS 200 and the GitHub
   Actions run for each.
3. **Criterion 3:** merge #140 (boot recovery). `shiplord restart` is now on `main` and its env-change path is
   proven on real Docker, so the remaining restart work is the three-app fresh-VM run. Add native crash output to
   the deploy result, or narrow the criterion. Then run all three sub-checks for each of the three apps, including
   a real reboot mid-deploy.
4. **Criterion 4:** run `status`, `logs`, `rollback`, and `github rotate-key` from a second machine against an app
   set up elsewhere. Add an app-name form to `rollback` or document the supported path and test it.
5. **Criterion 5:** merge #108, then add one test that asserts every row of the `AGENTS.md` exit-code table.
6. **Criterion 6:** add `python -m pip install shiplord` and the supported server operating systems to
   `README.md` (merge #141 for the baseline).
7. **Criterion 7:** merge this PR, and keep this document and `docs/evidence.md` in step as the other criteria
   close.

## What this document does not claim

- It does not claim `tests/linux/run.sh all` passed. That command was not run for this change.
- It does not claim a fresh VM, a public HTTPS deployment, a real reboot, or a second machine was exercised.
- It does not claim CI has run for this branch. The CI run cited above is the latest observed `main` run,
  42913399156, read with `gh run view`; it is not a run of this branch.
- It does not claim the `run.sh unit` failure is fixed. It is recorded as the reason criterion 1 is not met.

## How this document was checked

| What | Command | Result |
| --- | --- | --- |
| Unit suite, macOS arm64, Python 3.14.3 | `PYTHONPATH="$PWD/src" python3 -m unittest discover -s tests` | 1126 tests, OK, 94 skipped |
| Real-host unit mode, Docker 29.1.3 arm64 | `tests/linux/run.sh unit` | Root integration 8/8 OK; unprivileged suite 1126 tests, 3 failures and 1 error, 115 skipped (umask 0002) |
| Same suite with a fixed umask | The container command with `umask 0022` before the unprivileged suite | 1126 tests, OK, 115 skipped |
| CI history, `main` | `gh run view 42913399156 --json jobs` | All 12 jobs succeeded |

The `caddy`, `host`, `docker`, `e2e`, `systemd`, and `all` modes were not run for this change.
