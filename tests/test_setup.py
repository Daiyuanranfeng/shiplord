from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord import cli, setup
from shiplord.client import ClientError
from shiplord.commands import CommandError
from shiplord.environment import parse_environment
from shiplord.manifest import load_manifest

TOKEN = "throwaway-fixture-token"  # not a credential: the fake host never contacts a registry
ENVIRONMENT = "EXAMPLE_SETTING=placeholder\n"
TOOLING_REF = "cd" * 20


class FakeHost:
    """A fake server and GitHub behind the operations the real setup steps call.

    It records what each step did, and answers the read-only observations setup uses to decide whether a step is
    already complete. Nothing here opens a connection or runs a command.
    """

    def __init__(self, project: Path):
        self.project = project
        self.calls: list[str] = []
        self.provisioned: set[str] = set()
        self.environment: dict[str, str] = {}
        self.registries: list[dict[str, str]] = []
        self.github_done = False
        self.ci_ok = True
        self.fail: dict[str, Exception] = {}

    def _maybe_fail(self, step: str) -> None:
        if step in self.fail:
            raise self.fail[step]

    def server_applications(self, admin):
        self.calls.append("provision (check)")
        self._maybe_fail("inventory")
        return 0, {"ok": True, "applications": [{"application": name} for name in sorted(self.provisioned)]}

    def provision_application(self, manifest, admin, **options):
        self._maybe_fail("provision")
        self.calls.append("provision")
        self.provision_options = options
        self.provisioned.add(manifest["name"])
        return 0, {"ok": True, "deployment_user": f"ship-{manifest['name']}", "key_label": "local-fixture"}

    def set_environment(self, manifest, admin, operation, text=None):
        if operation == "keys":
            self.calls.append("environment (check)")
            return 0, {"ok": True, "keys": sorted(self.environment)}
        self._maybe_fail("environment")
        self.calls.append("environment")
        self.environment = parse_environment(text or "")
        return 0, {"ok": True, "keys": sorted(self.environment)}

    def registry_other(self, manifest, admin, operation, registry="ghcr.io"):
        self.calls.append("registry (check)")
        return 0, {"ok": True, "registries": list(self.registries)}

    def registry_login(self, manifest, admin, *, registry, username, token):
        self._maybe_fail("registry")
        self.calls.append("registry")
        self.registries = [{"registry": registry, "username": username}]
        return 0, {"ok": True, "registries": list(self.registries)}

    def github_setup(self, manifest, repository, **options):
        self._maybe_fail("github")
        self.calls.append("github")
        self.github_done = True
        text = (self.project / "ship.toml").read_text(encoding="utf-8")
        if "[github]" not in text:
            with (self.project / "ship.toml").open("a", encoding="utf-8") as file:
                file.write(f'\n[github]\nrepository = "{repository}"\ndeployment_branch = "main"\n')
        return {"ok": True, "repository": repository, "steps": {}}

    def setup_complete(self, manifest, *, project, repository, home=None):
        return self.github_done and manifest.get("github", {}).get("repository") == repository

    def check_ci(self, manifest, **options):
        self._maybe_fail("check-ci")
        assert "github" in manifest, "the manifest was not reloaded after github setup"
        self.calls.append("check-ci")
        if self.ci_ok:
            return 0, {"ok": True, "run": "https://github.com/Example/orders/actions/runs/1"}
        return 1, {"ok": False, "run": "https://github.com/Example/orders/actions/runs/2",
                   "error": "the CI key was refused", "hint": "run `shiplord github status --check-server`"}

    def patches(self):
        return [patch("shiplord.commands.server_applications", side_effect=self.server_applications),
                patch("shiplord.commands.provision_application", side_effect=self.provision_application),
                patch("shiplord.commands.set_environment", side_effect=self.set_environment),
                patch("shiplord.commands.registry_other", side_effect=self.registry_other),
                patch("shiplord.commands.registry_login", side_effect=self.registry_login),
                patch("shiplord.github.github_setup", side_effect=self.github_setup),
                patch("shiplord.github.setup_complete", side_effect=self.setup_complete),
                patch("shiplord.github.check_ci", side_effect=self.check_ci)]


class FakeHostSequenceTests(unittest.TestCase):
    """Drive the whole `shiplord setup` sequence against the fake host, through the real CLI wiring."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "orders"
        self.project.mkdir()
        config = root / "config"
        servers = config / "shiplord" / "servers"
        servers.mkdir(parents=True, mode=0o700)
        for path in (config, servers.parent, servers):
            path.chmod(0o700)
        (servers / "production.json").write_text(json.dumps({"schema_version": 1, "alias": "production",
                                                             "ssh": "admin@example.com"}))
        (servers / "production.json").chmod(0o600)
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(config))
        (self.project / ".env.production").write_text(ENVIRONMENT)
        (self.project / ".env.bad").write_text("NOT A VARIABLE\n")
        (self.project / ".env.empty").write_text("# nothing here\n")
        (self.project / "token").write_text(TOKEN + "\n")
        self.host = FakeHost(self.project)
        current = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, current)

    def run_setup(self, *extra: str, environment: bool = True, registry: bool = True):
        arguments = ["setup", "--server", "production", "--api-domain", "api.example.com", "--no-frontend",
                     "--name", "orders"]
        if environment:
            arguments += ["--env-file", ".env.production"]
        if registry:
            arguments += ["--registry-username", "bot", "--token-file", "token"]
        arguments += ["--repo", "Example/orders", "--tooling-ref", TOOLING_REF]
        with ExitStack() as stack:
            for patcher in self.host.patches():
                stack.enter_context(patcher)
            return CliRunner().invoke(cli.app, [*arguments, *extra], env=self.env)

    def statuses(self, result) -> list[str]:
        return [item["status"] for item in json.loads(result.stdout)["steps"]]

    def ran(self) -> list[str]:
        """The steps that changed the host, without the read-only observations that decided to run them."""
        return [call for call in self.host.calls if not call.endswith("(check)")]

    def test_the_whole_sequence_runs_in_order_against_the_fake_host(self):
        result = self.run_setup("--json")
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.stdout)
        self.assertEqual([item["step"] for item in data["steps"]], list(setup.STEP_ORDER))
        self.assertEqual(self.statuses(result), ["ran"] * len(setup.STEP_ORDER))
        self.assertEqual(self.ran(), ["provision", "environment", "registry", "github", "check-ci"])
        self.assertEqual(data["resumed_at"], "configure")
        self.assertEqual(data["application"], "orders")
        self.assertEqual(self.host.provision_options["image_repository"], "ghcr.io/example/orders")
        self.assertEqual(load_manifest(self.project / "ship.toml")["github"]["repository"], "Example/orders")
        self.assertEqual(self.host.environment, {"EXAMPLE_SETTING": "placeholder"})
        self.assertEqual(self.host.registries, [{"registry": "ghcr.io", "username": "bot"}])
        self.assertNotIn(TOKEN, result.stdout)

    def test_a_second_run_resumes_at_the_first_incomplete_step(self):
        self.assertEqual(self.run_setup("--json").exit_code, 0)
        self.host.calls.clear()
        second = self.run_setup("--json")
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertEqual(self.statuses(second), ["already complete"] * (len(setup.STEP_ORDER) - 1) + ["ran"])
        self.assertEqual(self.ran(), ["check-ci"])
        self.assertEqual(json.loads(second.stdout)["resumed_at"], "check-ci")

    def test_a_failure_stops_the_sequence_and_the_next_run_continues_there(self):
        self.host.fail["provision"] = ClientError("the server refused the account", 3)
        failed = self.run_setup("--json")
        self.assertEqual(failed.exit_code, 3, failed.output)
        data = json.loads(failed.stdout)
        self.assertEqual(data["failed_step"], "provision")
        self.assertEqual(self.statuses(failed), ["ran", "failed"])
        self.assertNotIn("github", self.host.calls)
        self.host.fail.clear()
        self.host.calls.clear()
        resumed = self.run_setup("--json")
        self.assertEqual(resumed.exit_code, 0, resumed.output)
        self.assertEqual(self.statuses(resumed), ["already complete", "ran", "ran", "ran", "ran", "ran"])
        self.assertEqual(json.loads(resumed.stdout)["resumed_at"], "provision")
        self.assertEqual(self.ran()[0], "provision")

    def test_a_failed_final_check_exits_non_zero(self):
        self.host.ci_ok = False
        result = self.run_setup("--json")
        self.assertEqual(result.exit_code, 1, result.output)
        data = json.loads(result.stdout)
        self.assertEqual(data["failed_step"], "check-ci")
        self.assertEqual(self.statuses(result)[-1], "failed")
        self.assertIn("the CI key was refused", data["error"])
        self.assertEqual(data["steps"][-1]["result"]["hint"], "run `shiplord github status --check-server`")

    def test_env_and_registry_are_reported_as_not_requested_without_their_inputs(self):
        result = self.run_setup("--json", environment=False, registry=False)
        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.stdout)
        by_name = {item["step"]: item for item in data["steps"]}
        self.assertEqual(by_name["environment"]["status"], "not requested")
        self.assertEqual(by_name["registry"]["status"], "not requested")
        self.assertNotIn("environment", self.host.calls)
        self.assertNotIn("registry", self.host.calls)
        self.assertEqual(self.host.environment, {})
        self.assertEqual(self.host.registries, [])

    def test_skip_ci_stops_after_the_local_steps(self):
        result = self.run_setup("--json", "--skip-ci")
        self.assertEqual(result.exit_code, 0, result.output)
        by_name = {item["step"]: item for item in json.loads(result.stdout)["steps"]}
        self.assertEqual((by_name["github"]["status"], by_name["check-ci"]["status"]),
                         ("not requested", "not requested"))
        self.assertNotIn("github", self.host.calls)
        self.assertNotIn("check-ci", self.host.calls)

    def test_an_image_repository_can_replace_the_one_derived_from_the_repository(self):
        result = self.run_setup("--json", "--image-repository", "ghcr.io/example/custom")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.host.provision_options["image_repository"], "ghcr.io/example/custom")

    def test_a_generic_application_runs_the_local_steps_with_skip_ci(self):
        result = self.run_setup("--json", "--skip-ci", "--template", "generic")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(load_manifest(self.project / "ship.toml")["template"], "generic")
        self.assertNotIn("github", self.host.calls)

    def test_rerun_repeats_the_repeatable_steps(self):
        self.assertEqual(self.run_setup("--json").exit_code, 0)
        self.host.calls.clear()
        again = self.run_setup("--json", "--rerun")
        self.assertEqual(again.exit_code, 0, again.output)
        self.assertEqual(self.statuses(again), ["already complete"] + ["ran"] * (len(setup.STEP_ORDER) - 1))
        self.assertEqual(self.ran(), ["provision", "environment", "registry", "github", "check-ci"])

    def test_bad_input_is_rejected_before_anything_is_written(self):
        base = ["setup", "--server", "production", "--api-domain", "api.example.com", "--no-frontend", "--name",
                "orders", "--tooling-ref", TOOLING_REF]
        cases = [
            ([*base, "--repo", "Example/orders", "--token-file", "token", "--json"], 2),
            ([*base, "--json"], 2),
            ([*base, "--repo", "Example/orders", "--env-file", "missing.env", "--json"], 2),
            ([*base, "--repo", "Example/orders", "--env-file", ".env.bad", "--json"], 2),
            ([*base, "--repo", "Example/orders", "--env-file", ".env.empty", "--json"], 2),
            ([*base, "--repo", "Example/orders", "--template", "generic", "--json"], 2),
            ([*base, "--repo", "Example/orders", "--runtime", "native", "--json"], 2),
            ([*base, "--repo", "Example/orders", "--public-key", "missing.pub", "--json"], 3),
            ([*base, "--skip-ci", "--json"], 2),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                with ExitStack() as stack:
                    for patcher in self.host.patches():
                        stack.enter_context(patcher)
                    result = CliRunner().invoke(cli.app, arguments, env=self.env)
                self.assertEqual(result.exit_code, expected, result.output)
                self.assertFalse((self.project / "ship.toml").exists())
                self.assertEqual(self.host.calls, [])


class RunSetupTests(unittest.TestCase):
    """The sequencing itself, with injected steps: order, resume, skips, and failure."""

    def step(self, name, *, done=False, result=(0, {"ok": True}), error=None, skip_reason=None):
        calls = []

        def run():
            calls.append(name)
            if error is not None:
                raise error
            return result

        return setup.Step(name, run=run, done=lambda: done, skip_reason=skip_reason), calls

    def test_steps_run_in_order_and_completed_ones_are_skipped(self):
        first, first_calls = self.step("first", done=True)
        second, second_calls = self.step("second")
        third, third_calls = self.step("third")
        code, result = setup.run_setup([first, second, third])
        self.assertEqual(code, 0)
        self.assertEqual((first_calls, second_calls, third_calls), ([], ["second"], ["third"]))
        self.assertEqual([item["status"] for item in result["steps"]],
                         ["already complete", "ran", "ran"])
        self.assertEqual(result["resumed_at"], "second")

    def test_a_not_requested_step_does_not_count_as_resumed(self):
        skipped, skipped_calls = self.step("skipped", skip_reason="not asked for")
        ran, ran_calls = self.step("ran")
        code, result = setup.run_setup([skipped, ran])
        self.assertEqual(code, 0)
        self.assertEqual(skipped_calls, [])
        self.assertEqual(ran_calls, ["ran"])
        self.assertEqual(result["resumed_at"], "ran")
        self.assertEqual(result["steps"][0]["detail"], "not asked for")

    def test_an_unreadable_observation_runs_the_step_again(self):
        def done():
            raise ClientError("the server did not answer", 3)

        calls = []
        step = setup.Step("provision", run=lambda: (calls.append("run"), (0, {}))[1], done=done)
        code, _result = setup.run_setup([step])
        self.assertEqual((code, calls), (0, ["run"]))

    def test_a_failed_step_stops_the_sequence_with_its_exit_code(self):
        first, _ = self.step("first")
        failing, _ = self.step("second", error=CommandError("bad input", 2))
        last, last_calls = self.step("last")
        code, result = setup.run_setup([first, failing, last])
        self.assertEqual(code, 2)
        self.assertEqual(result["failed_step"], "second")
        self.assertEqual(last_calls, [])
        self.assertEqual([item["status"] for item in result["steps"]], ["ran", "failed"])

    def test_a_step_that_reports_failure_without_raising_keeps_its_result(self):
        code, result = setup.run_setup([setup.Step("check-ci", run=lambda: (1, {"ok": False, "error": "no route"}))])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "no route")
        self.assertEqual(result["steps"][0]["result"], {"ok": False, "error": "no route"})

    def test_rerun_ignores_what_looks_complete(self):
        step, calls = self.step("first", done=True)
        code, result = setup.run_setup([step], rerun=True)
        self.assertEqual((code, calls), (0, ["first"]))
        self.assertEqual(result["steps"][0]["status"], "ran")

    def test_the_report_names_each_step_as_it_starts(self):
        seen = []
        steps = [self.step("one")[0], self.step("two", done=True)[0]]
        setup.run_setup(steps, report=lambda index, total, name, status: seen.append((index, total, name, status)))
        self.assertEqual(seen, [(1, 2, "one", "running"), (2, 2, "two", "already complete")])


class PreflightTests(unittest.TestCase):
    def test_help_lists_the_steps_and_the_escape_hatches(self):
        result = CliRunner().invoke(cli.app, ["setup", "--help"], color=True)
        self.assertEqual(result.exit_code, 0, result.output)
        output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        for value in ("configure, provision, env, registry login, github setup", "check-ci", "--env-file",
                      "--registry-username", "--repo", "--skip-ci", "--rerun", "--json"):
            self.assertIn(value, output.replace("\n", " ").replace("  ", " "))

    def test_a_conflicting_frontend_choice_is_refused(self):
        result = CliRunner().invoke(cli.app, ["setup", "--frontend", "https://example.com", "--no-frontend",
                                              "--skip-ci", "--json"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--frontend", json.loads(result.stdout)["error"])

    def test_a_manifest_without_a_repository_needs_repo_unless_ci_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ship.toml").write_text(
                'schema_version = 1\nname = "orders"\nserver = "production"\ntemplate = "google-adk"\n'
                'runtime = "docker"\n[http]\napi_domain = "api.example.com"\nfrontend_origins = []\n'
                'container_port = 8080\nreadiness_path = "/ready"\n[resources]\nmemory_mb = 512\n'
                '[deploy]\nreadiness_timeout_seconds = 90\nretain_successful_releases = 2\n')
            current = os.getcwd()
            try:
                os.chdir(root)
                refused = CliRunner().invoke(cli.app, ["setup", "--json"])
            finally:
                os.chdir(current)
        self.assertEqual(refused.exit_code, 2)
        self.assertIn("--repo", json.loads(refused.stdout)["error"])
