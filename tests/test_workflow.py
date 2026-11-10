from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from shiplord.manifest import validate_manifest
from shiplord.workflow import (ACTIONS, DEFAULT_QUALITY_WORKFLOW, DEPLOY_WORKFLOW_PATH, PINS, WORKFLOW_PATH, PinChange,
                               WorkflowError, check_action_pins, is_generated_workflow, parse_action_pin,
                               render_deploy_workflow, render_workflow, upgrade_action_pins)

REF = "ab" * 20
QUALITY = "./" + DEFAULT_QUALITY_WORKFLOW
DEPLOY = "./" + DEPLOY_WORKFLOW_PATH


def manifest(**changes):
    value = validate_manifest({"schema_version": 1, "name": "orders", "server": "production", "template": "google-adk",
                               "runtime": "docker", "http": {"api_domain": "orders.example.com", "frontend_origins": [],
                               "container_port": 8080, "readiness_path": "/ready"}, "resources": {"memory_mb": 512},
                               "deploy": {"readiness_timeout_seconds": 90, "retain_successful_releases": 2},
                               "github": {"repository": "Example/orders", "deployment_branch": "main"}})
    value.update(changes)
    return value


def parse(text: str) -> dict:
    result = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load(STDIN.read))"], input=text,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("ruby"), "ruby is used as an independent YAML parser")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = render_workflow(manifest(), tooling_ref=REF, quality_workflow=DEFAULT_QUALITY_WORKFLOW)
        self.data = parse(self.text)
        self.jobs = self.data["jobs"]
        self.deploy_text = render_deploy_workflow(manifest(), tooling_ref=REF)
        self.deploy = parse(self.deploy_text)
        self.deploy_jobs = self.deploy["jobs"]

    def steps(self, job, document=None):
        return (document or self.jobs)[job]["steps"]

    def test_the_pipeline_is_a_trigger_that_calls_a_reusable_deploy_workflow(self):
        self.assertEqual(list(self.jobs), ["quality", "deploy"])
        self.assertEqual(self.jobs["quality"]["uses"], QUALITY)
        self.assertEqual((self.jobs["deploy"]["uses"], self.jobs["deploy"]["needs"]), (DEPLOY, "quality"))
        self.assertEqual(self.jobs["deploy"]["with"], {"check_only": "${{ inputs.check_only }}"})
        self.assertEqual(self.jobs["deploy"]["secrets"],
                         {"SHIPLORD_SSH_PRIVATE_KEY": "${{ secrets.SHIPLORD_SSH_PRIVATE_KEY }}"})
        trigger = self.deploy.get("on") or self.deploy["true"]
        self.assertEqual(list(trigger), ["workflow_call"])
        self.assertEqual(trigger["workflow_call"]["secrets"], {"SHIPLORD_SSH_PRIVATE_KEY": {"required": True}})
        self.assertEqual(list(self.deploy_jobs), ["build", "deploy"])
        self.assertEqual(self.deploy_jobs["deploy"]["needs"], "build")
        for document in (self.data, self.deploy):
            trigger = document.get("on") or document["true"]
            self.assertNotIn("pull_request_target", trigger)
            self.assertNotIn("pull_request", trigger)  # pull requests run the template's own checks, never a deploy

    def test_the_trigger_still_runs_on_the_deployment_branch_and_on_dispatch(self):
        trigger = self.data.get("on") or self.data["true"]
        self.assertEqual(trigger["push"]["branches"], ["main"])
        self.assertEqual(trigger["workflow_dispatch"]["inputs"]["check_only"]["default"], False)

    def test_a_pipeline_without_a_quality_workflow_has_no_quality_job_and_no_broken_reference(self):
        text = render_workflow(manifest(), tooling_ref=REF)
        data = parse(text)
        self.assertEqual(list(data["jobs"]), ["deploy"])
        self.assertNotIn("quality", text)
        self.assertNotIn("code-quality", text)
        self.assertEqual(data["jobs"]["deploy"]["uses"], DEPLOY)
        self.assertNotIn("needs", data["jobs"]["deploy"])
        self.assertEqual(data["jobs"]["deploy"]["if"], "${{ !cancelled() }}")

    def test_every_local_uses_names_a_generated_file_or_the_named_quality_workflow(self):
        self.assertEqual([u for u in re.findall(r"uses: (\S+)", self.text) if u.startswith("./")], [QUALITY, DEPLOY])
        self.assertEqual([u for u in re.findall(r"uses: (\S+)", self.deploy_text) if u.startswith("./")], [])

    def test_every_external_action_is_pinned_to_a_full_commit(self):
        uses = re.findall(r"uses: (\S+)", self.deploy_text)
        external = [u for u in uses if not u.startswith("./")]
        self.assertTrue(external)
        for reference in external:
            self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$", reference)
        for line in ACTIONS.values():
            self.assertRegex(line.split(" # ")[0], r"@[0-9a-f]{40}$")

    def test_permissions_are_the_least_each_job_needs(self):
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["quality"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.jobs["deploy"]["permissions"],
                         {"contents": "read", "id-token": "write", "packages": "write"})  # caps the called jobs
        self.assertEqual(self.deploy["permissions"], {"contents": "read"})
        self.assertEqual(self.deploy_jobs["build"]["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual(self.deploy_jobs["deploy"]["permissions"], {"contents": "read", "id-token": "write"})
        self.assertEqual(self.text.count("id-token: write"), 2)
        self.assertEqual(self.deploy_text.count("id-token: write"), 1)  # only where OIDC is used

    def test_a_production_deployment_is_serialized_and_never_cancelled(self):
        self.assertEqual(self.data["concurrency"], {"group": "shiplord-release-orders", "cancel-in-progress": False})
        for name in ("build", "deploy"):
            self.assertIn("timeout-minutes", self.deploy_jobs[name])
        self.assertEqual(self.deploy_jobs["deploy"]["if"],
                         "${{ !cancelled() && github.ref == 'refs/heads/main' && "
                         "(inputs.check_only || needs.build.result == 'success') }}")

    def test_a_check_only_dispatch_skips_quality_and_build_and_runs_status_instead_of_deploy(self):
        self.assertEqual(self.jobs["quality"]["if"], "${{ !inputs.check_only }}")
        self.assertEqual(self.jobs["deploy"]["if"],
                         "${{ !cancelled() && (inputs.check_only || needs.quality.result == 'success') }}")
        self.assertEqual(self.deploy_jobs["build"]["if"], "${{ !inputs.check_only }}")
        steps = {s.get("name"): s for s in self.steps("deploy", self.deploy_jobs)}
        self.assertEqual((steps["Check access only"]["if"], steps["Check access only"]["run"]),
                         ("${{ inputs.check_only }}", "shiplord status --json"))
        self.assertEqual(steps["Deploy"]["if"], "${{ !inputs.check_only }}")

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is required to validate Actions expressions")
    def test_generated_workflows_pass_actionlint(self):
        for branch in ("main", "release/prod"):
            value = manifest(github={"repository": "Example/orders", "deployment_branch": branch})
            for quality in (None, DEFAULT_QUALITY_WORKFLOW):
                for text in (render_workflow(value, tooling_ref=REF, quality_workflow=quality),
                             render_deploy_workflow(value, tooling_ref=REF)):
                    with self.subTest(branch=branch, quality=quality, text=text.splitlines()[0]):
                        result = subprocess.run(["actionlint", "-shellcheck=", "-pyflakes=", "-"],
                                                input=text, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is required to validate Actions expressions")
    def test_an_upgraded_workflow_is_still_valid_yaml_and_passes_actionlint(self):
        for branch in ("main", "release/prod"):
            value = manifest(github={"repository": "Example/orders", "deployment_branch": branch})
            for quality in (None, DEFAULT_QUALITY_WORKFLOW):
                for text in (render_workflow(value, tooling_ref=REF, quality_workflow=quality),
                             render_deploy_workflow(value, tooling_ref=REF)):
                    stale = text.replace(PINS["checkout"].commit, "1" * 40)
                    upgraded, _changes = upgrade_action_pins(stale)
                    with self.subTest(branch=branch, quality=quality, text=text.splitlines()[0]):
                        self.assertEqual(upgrade_action_pins(upgraded)[1], [])  # nothing left to refresh
                        self.assertNotIn("1" * 40, upgraded)  # the stale commit is gone
                        parse(upgraded)  # ruby's YAML parser accepts the refreshed file
                        result = subprocess.run(["actionlint", "-shellcheck=", "-pyflakes=", "-"],
                                                input=upgraded, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_deployment_and_access_conditions_for_build_and_dispatch(self):
        # Model the generated boolean expressions for these specific GitHub contexts.
        def evaluate(condition, *, check_only, cancelled=False, ref="refs/heads/main", build="success",
                     quality="success"):
            expression = condition.removeprefix("${{").removesuffix("}}").strip()
            values = {"inputs.check_only": repr(check_only), "cancelled()": repr(cancelled),
                      "github.ref": repr(ref), "needs.build.result": repr(build), "needs.quality.result": repr(quality)}
            for name, value in values.items():
                expression = expression.replace(name, value)
            expression = expression.replace("&&", " and ").replace("||", " or ")
            expression = re.sub(r"!(?!=)", " not ", expression).strip()
            tree = ast.parse(expression, mode="eval")
            allowed = (ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare, ast.Constant,
                       ast.And, ast.Or, ast.Not, ast.Eq, ast.Load)
            self.assertTrue(all(isinstance(node, allowed) for node in ast.walk(tree)))
            return eval(compile(tree, "<workflow condition>", "eval"), {"__builtins__": {}})

        quality_if = self.jobs["quality"]["if"]
        caller_if = self.jobs["deploy"]["if"]
        build_if = self.deploy_jobs["build"]["if"]
        deploy_if = self.deploy_jobs["deploy"]["if"]
        steps = {step.get("name"): step for step in self.steps("deploy", self.deploy_jobs)}
        for context, expected in (
            ({}, (True, True, True, True, False, True)),
            ({"build": "failure"}, (True, True, True, False, False, False)),
            ({"quality": "failure"}, (True, False, False, False, False, False)),
            ({"ref": "refs/heads/other"}, (True, True, True, False, False, False)),
            ({"cancelled": True}, (True, False, False, False, False, False)),
            ({"check_only": True, "build": "skipped", "quality": "skipped"}, (False, True, False, True, True, False)),
        ):
            with self.subTest(context=context):
                context.setdefault("check_only", False)
                runs_quality = evaluate(quality_if, **context)
                runs_caller = evaluate(caller_if, **context)
                runs_build = runs_caller and evaluate(build_if, **context)
                runs_deploy = runs_caller and evaluate(deploy_if, **context)
                runs_check = runs_deploy and evaluate(steps["Check access only"]["if"], **context)
                runs_release = runs_deploy and evaluate(steps["Deploy"]["if"], **context)
                self.assertEqual((runs_quality, runs_caller, runs_build, runs_deploy, runs_check, runs_release),
                                 expected)

    def test_no_secret_variable_or_event_value_is_interpolated_into_a_shell_script(self):
        for document in (self.data, self.deploy):
            for job in document["jobs"].values():
                for step in job.get("steps", []):
                    if "run" in step:
                        self.assertNotIn("${{", step["run"], step.get("name"))
        self.assertEqual(self.deploy_text.count("secrets.SHIPLORD_SSH_PRIVATE_KEY"), 1)
        key_step = next(s for s in self.steps("deploy", self.deploy_jobs) if "SSH_PRIVATE_KEY" in json.dumps(s.get("env", {})))
        self.assertEqual(key_step["env"]["SSH_PRIVATE_KEY"], "${{ secrets.SHIPLORD_SSH_PRIVATE_KEY }}")
        self.assertIn("umask 077", key_step["run"])
        self.assertEqual(self.text.count("secrets.SHIPLORD_SSH_PRIVATE_KEY"), 1)  # only the explicit mapping

    def test_the_deploy_step_uses_the_digest_the_exact_commit_a_retry_safe_id_and_requires_public_verification(self):
        deploy = next(s for s in self.steps("deploy", self.deploy_jobs) if s.get("name") == "Deploy")
        self.assertEqual(deploy["env"]["DIGEST"], "${{ needs.build.outputs.digest }}")
        for fragment in ('--image "${IMAGE}@${DIGEST}"', '--commit "$GITHUB_SHA"', '--request-id "gh-${RUN_ID}-${RUN_ATTEMPT}"',
                         "--require-public", "--json", "set -euo pipefail", "| tee"):
            self.assertIn(fragment, deploy["run"])

    def test_the_checkout_is_verified_and_credentials_are_not_persisted(self):
        checkouts = [s for job in self.deploy_jobs.values() for s in job.get("steps", [])
                     if "actions/checkout" in s.get("uses", "")]
        self.assertTrue(checkouts)
        for step in checkouts:
            self.assertIs(step["with"]["persist-credentials"], False)
        self.assertTrue(any("git rev-parse HEAD" in s.get("run", "") for s in self.steps("deploy", self.deploy_jobs)))

    def test_the_tooling_is_installed_from_the_pinned_commit(self):
        install = next(s for s in self.steps("deploy", self.deploy_jobs) if s.get("name") == "Install the pinned Shiplord")
        self.assertIn(f"git+https://github.com/QueryPlanner/shiplord.git@{REF}", install["run"])

    def test_the_ssh_identity_is_removed_even_when_the_deploy_fails(self):
        cleanup = self.steps("deploy", self.deploy_jobs)[-1]
        self.assertEqual(cleanup["if"], "${{ always() }}")
        self.assertIn("shiplord_key", cleanup["run"])

    def test_the_tailscale_step_uses_oidc_inputs_and_a_default_tag(self):
        step = next(s for s in self.steps("deploy", self.deploy_jobs) if "tailscale/github-action" in s.get("uses", ""))
        self.assertEqual(step["with"]["oauth-client-id"], "${{ vars.SHIPLORD_TS_CLIENT_ID }}")
        self.assertEqual(step["with"]["audience"], "${{ vars.SHIPLORD_TS_AUDIENCE }}")
        self.assertEqual(step["with"]["ping"], "${{ vars.SHIPLORD_SERVER_HOST }}")  # waits until the server is reachable
        self.assertIn("tag:ci", step["with"]["tags"])
        self.assertNotIn("oauth-secret", self.text + self.deploy_text)  # federation: no long-lived Tailscale secret

    def test_the_image_build_targets_the_requested_platforms_and_records_the_commit(self):
        build = next(s for s in self.steps("build", self.deploy_jobs) if s.get("id") == "build-push")
        self.assertEqual(build["with"]["platforms"], "linux/amd64")
        self.assertIn("org.opencontainers.image.revision=${{ github.sha }}", build["with"]["labels"])
        self.assertFalse(any("qemu" in s.get("uses", "") for s in self.steps("build", self.deploy_jobs)))
        both = parse(render_deploy_workflow(manifest(), tooling_ref=REF, platforms="linux/amd64,linux/arm64"))
        self.assertTrue(any("qemu" in s.get("uses", "") for s in both["jobs"]["build"]["steps"]))

    def test_output_is_deterministic_and_follows_the_deployment_branch(self):
        self.assertEqual(self.text, render_workflow(manifest(), tooling_ref=REF, quality_workflow=DEFAULT_QUALITY_WORKFLOW))
        self.assertEqual(self.deploy_text, render_deploy_workflow(manifest(), tooling_ref=REF))
        other = manifest(github={"repository": "Example/orders", "deployment_branch": "release/prod"})
        data = parse(render_workflow(other, tooling_ref=REF))
        self.assertEqual((data.get("on") or data["true"])["push"]["branches"], ["release/prod"])
        deploy = parse(render_deploy_workflow(other, tooling_ref=REF))
        self.assertIn("github.ref == 'refs/heads/release/prod'", deploy["jobs"]["deploy"]["if"])

    def test_invalid_inputs_are_refused(self):
        for render in (render_workflow, render_deploy_workflow):
            for kwargs in ({"tooling_ref": "main"}, {"tooling_ref": REF[:-1]},
                           {"tooling_ref": REF, "platforms": "linux/386"},
                           {"tooling_ref": REF, "platforms": "windows/amd64"},
                           {"tooling_ref": REF, "tooling_repository": "evil repo; rm"}):
                with self.subTest(render=render.__name__, kwargs=kwargs), self.assertRaises(WorkflowError):
                    render(manifest(), **kwargs)
            with self.assertRaisesRegex(WorkflowError, "native"):
                render(manifest(runtime="native"), tooling_ref=REF)

    def test_the_quality_workflow_must_be_a_reusable_workflow_and_never_a_generated_file(self):
        for bad in ("code-quality.yml", "/.github/workflows/x.yml", ".github/workflows/../x.yml",
                    ".github/workflows/x.txt", ".github/workflows/x.yml@main", WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH):
            with self.subTest(bad=bad), self.assertRaisesRegex(WorkflowError, "quality workflow"):
                render_workflow(manifest(), tooling_ref=REF, quality_workflow=bad)
        text = render_workflow(manifest(), tooling_ref=REF, quality_workflow=".github/workflows/checks.yml")
        self.assertEqual(parse(text)["jobs"]["quality"]["uses"], "./.github/workflows/checks.yml")


STALE = "0" * 40


class ActionPinTests(unittest.TestCase):
    """Refreshing the pins in a workflow Shiplord wrote, without touching anything else."""

    def deploy_text(self):
        return render_deploy_workflow(manifest(), tooling_ref=REF)

    def stale_text(self):
        text = self.deploy_text()
        self.assertIn(PINS["checkout"].commit, text)
        return text.replace(PINS["checkout"].commit, STALE)

    def test_every_pin_names_a_repository_a_full_commit_and_the_release_it_belongs_to(self):
        for name, pin in PINS.items():
            with self.subTest(pin=name):
                self.assertEqual(ACTIONS[name], str(pin))
                self.assertRegex(pin.commit, r"^[0-9a-f]{40}$")
                self.assertEqual(pin.repository.count("/"), 1)
                self.assertTrue(pin.version)
                self.assertEqual(parse_action_pin(str(pin)), pin)
        for bad in ("actions/checkout@v4", "actions/checkout@" + STALE, "actions/checkout@" + STALE + " # v4 # x",
                    "no-slash/repo@x", ACTIONS["checkout"].replace(" # ", " ")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_action_pin(bad)

    def test_a_stale_pin_is_refreshed_and_every_other_line_is_preserved(self):
        text = self.stale_text() + "# a local note\n"
        upgraded, changes = upgrade_action_pins(text)
        self.assertEqual(upgraded, text.replace(STALE, PINS["checkout"].commit))
        self.assertEqual(changes, [PinChange("actions/checkout", STALE, PINS["checkout"].commit, "v4")])
        self.assertTrue(is_generated_workflow(upgraded))

    def test_upgrading_twice_changes_nothing_the_second_time(self):
        upgraded, _ = upgrade_action_pins(self.stale_text())
        self.assertEqual(upgrade_action_pins(upgraded), (upgraded, []))

    def test_a_floating_tag_for_an_action_shiplord_pins_is_replaced_by_the_commit(self):
        text = self.deploy_text().replace(f"actions/checkout@{PINS['checkout'].commit}", "actions/checkout@v4")
        upgraded, changes = upgrade_action_pins(text)
        self.assertIn(str(PINS["checkout"]), upgraded)
        self.assertEqual([change.previous for change in changes], ["v4"])

    def test_a_workflow_shiplord_did_not_write_is_refused(self):
        hand_written = ("name: Mine\non: push\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    steps:\n"
                        f"      - uses: actions/checkout@{STALE}\n")
        for text in (hand_written, "# copied header\n" + self.deploy_text(), "\n" + self.deploy_text()):
            with self.subTest(text=text[:40]), self.assertRaisesRegex(WorkflowError, "generated by Shiplord"):
                upgrade_action_pins(text)

    def test_actions_and_local_workflows_shiplord_does_not_pin_are_left_alone(self):
        text = (self.stale_text() + "      - uses: example/other-action@" + STALE + " # v1\n"
                "      - uses: ./my/local-action\n")
        upgraded, changes = upgrade_action_pins(text)
        self.assertIn(f"example/other-action@{STALE} # v1", upgraded)
        self.assertIn("./my/local-action", upgraded)
        self.assertEqual([change.repository for change in changes], ["actions/checkout"])

    def test_the_trigger_workflow_is_accepted_and_needs_no_refresh(self):
        text = render_workflow(manifest(github={"repository": "Example/orders", "deployment_branch": "release/prod"}),
                               tooling_ref=REF, quality_workflow=DEFAULT_QUALITY_WORKFLOW)
        self.assertEqual(upgrade_action_pins(text), (text, []))
        self.assertIn('branches: ["release/prod"]', text)


class PinCheckTests(unittest.TestCase):
    """The resolver, driven by a fake HTTP client: the unit suite never uses the network."""

    def setUp(self):
        self.calls = []

    def fetch(self, answers):
        def fetch(url, token):
            self.calls.append((url, token))
            answer = answers.get(url, answers.get(None, (200, "{}")))
            if isinstance(answer, Exception):
                raise answer
            return answer
        return fetch

    def url(self, name):
        pin = PINS[name]
        return f"https://api.github.com/repos/{pin.repository}/commits/{pin.commit}"

    def test_every_pin_is_asked_about_in_its_own_repository(self):
        code, result = check_action_pins(fetch=self.fetch({}))
        self.assertEqual((code, result["ok"], result["checked"]), (0, True, len(PINS)))
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["unverified"], [])
        self.assertNotIn("error", result)
        self.assertEqual(sorted(url for url, _ in self.calls), sorted(self.url(name) for name in PINS))

    def test_a_pin_whose_commit_is_gone_fails_the_check_and_points_at_the_refresh(self):
        code, result = check_action_pins(fetch=self.fetch({self.url("build"): (404, "{}")}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual(result["missing"], [f"{PINS['build'].repository}@{PINS['build'].commit} (v5)"])
        self.assertEqual(result["unverified"], [])
        self.assertIn("--upgrade-workflow", result["error"])
        states = {pin["action"]: pin["state"] for pin in result["pins"]}
        self.assertEqual((states["docker/build-push-action"], states["actions/checkout"]), ("missing", "resolves"))

    def test_a_rejected_reference_is_reported_as_missing_too(self):
        code, result = check_action_pins(fetch=self.fetch({self.url("python"): (422, "{}")}))
        self.assertEqual(code, 1)
        self.assertEqual(len(result["missing"]), 1)

    def test_without_the_api_nothing_is_reported_as_resolving(self):
        code, result = check_action_pins(fetch=self.fetch({None: OSError("no route to host")}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["unverified"]), len(PINS))
        self.assertIn("nothing here says they resolve", result["error"])
        self.assertNotIn("resolves", {pin["state"] for pin in result["pins"]})

    def test_a_token_is_sent_but_never_echoed_in_the_result(self):
        token = "ghs_exampleTOKEN1234567890"
        code, result = check_action_pins(fetch=self.fetch({}), token=token)
        self.assertEqual(code, 0)
        self.assertEqual({sent for _, sent in self.calls}, {token})
        self.assertNotIn(token, json.dumps(result))

    def test_a_rate_limit_is_unverified_and_names_the_answer(self):
        code, result = check_action_pins(fetch=self.fetch({None: (403, "API rate limit exceeded")}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual({pin["detail"] for pin in result["pins"]}, {"the GitHub API answered 403"})
        self.assertNotIn("rate limit exceeded", json.dumps(result))


REPOSITORY = Path(__file__).resolve().parent.parent


class RepositoryCiTests(unittest.TestCase):
    """This repository's own CI runs the pin check, and pins its own actions the same way."""

    def setUp(self):
        self.text = (REPOSITORY / ".github" / "workflows" / "test.yml").read_text()

    def test_ci_runs_the_pin_check_with_the_repository_token(self):
        self.assertIn("shiplord github check-pins --json", self.text)
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", self.text)
        self.assertIn("  action-pins:", self.text)

    def test_every_action_this_repository_uses_is_pinned_to_a_full_commit(self):
        references = re.findall(r"uses: (\S+)", self.text)
        self.assertTrue(references)
        for reference in references:
            with self.subTest(reference=reference):
                self.assertRegex(reference, r"^[\w./-]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
