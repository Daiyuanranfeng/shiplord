"""Checks on .github/workflows/publish.yml, the PyPI Trusted Publishing workflow."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"


class PublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def job(self, name):
        match = re.search(rf"^  {name}:\n(.*?)(?=^  \w|\Z)", self.text, re.S | re.M)
        self.assertIsNotNone(match, f"job {name} is missing")
        return match.group(1)

    def test_runs_only_when_a_release_is_published(self):
        on_block = re.search(r"^on:\n(.*?)^\S", self.text, re.S | re.M).group(1)
        self.assertIn("release:", on_block)
        self.assertIn("types: [published]", on_block)
        self.assertNotIn("push:", on_block)
        self.assertNotIn("pull_request", on_block)

    def test_only_the_publish_job_can_mint_an_oidc_token(self):
        self.assertEqual(self.text.count("id-token: write"), 1)
        self.assertIn("id-token: write", self.job("publish"))
        self.assertNotIn("id-token", self.job("build"))
        self.assertIn("contents: read", self.text.split("jobs:")[0])

    def test_publish_uses_the_pypi_environment_and_the_built_artifact(self):
        publish = self.job("publish")
        self.assertIn("environment: pypi", publish)
        self.assertIn("needs: build", publish)
        self.assertIn("download-artifact", publish)
        self.assertIn("gh-action-pypi-publish", publish)
        self.assertNotIn("password", publish)

    def test_every_external_action_is_pinned_to_a_full_commit(self):
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertGreaterEqual(len(uses), 5)
        for ref in uses:
            self.assertRegex(ref, r"@[0-9a-f]{40}$", ref)

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is required")
    def test_passes_actionlint(self):
        result = subprocess.run(
            ["actionlint", "-shellcheck=", "-pyflakes=", str(WORKFLOW)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
