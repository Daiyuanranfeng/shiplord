from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord import build, cli

COMMIT = "a" * 40
DIGEST = "sha256:" + "c" * 64
REPOSITORY = "ghcr.io/queryplanner/orders"


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode, self.stdout = returncode, stdout


class FakeDocker:
    """A fake docker process that writes the metadata file buildx would write."""

    def __init__(self, digest: str | None = DIGEST, returncode: int = 0, metadata: str | None = None):
        self.digest, self.returncode, self.metadata = digest, returncode, metadata
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=None, stdout=None):
        self.calls.append(list(argv))
        if self.metadata is not None:
            Path(argv[argv.index("--metadata-file") + 1]).write_text(self.metadata)
        elif self.returncode == 0 and self.digest is not None:
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.digest": self.digest}))
        return Completed(self.returncode)


class FakeGit:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode, self.stdout = returncode, stdout
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv, directory):
        self.calls.append((list(argv), directory))
        return Completed(self.returncode, self.stdout)


class BuilderTests(unittest.TestCase):
    def builder(self, docker=None, git=None, directory=None):
        return build.Builder(runner=docker or FakeDocker(), git_runner=git or FakeGit(),
                             directory=directory or Path(tempfile.gettempdir()))

    def test_a_dirty_tree_is_refused_before_docker_runs(self):
        docker, git = FakeDocker(), FakeGit(stdout=" M ship.toml\n?? scratch\n")
        builder = self.builder(docker, git)
        with self.assertRaises(build.BuildError) as caught:
            builder.require_clean_tree()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("uncommitted changes", str(caught.exception))
        self.assertNotIn("ship.toml", str(caught.exception))
        self.assertEqual(docker.calls, [])
        self.assertEqual(git.calls[0][0], ["git", "status", "--porcelain", "--untracked-files=normal"])

    def test_a_clean_tree_passes(self):
        git = FakeGit(stdout="")
        self.builder(git=git).require_clean_tree()
        self.assertEqual(len(git.calls), 1)

    def test_git_failures_are_prerequisite_errors(self):
        with self.assertRaises(build.BuildError) as caught:
            self.builder(git=FakeGit(returncode=128)).require_clean_tree()
        self.assertEqual(caught.exception.code, 3)

        def missing(argv, directory):
            raise OSError("not found")

        with self.assertRaises(build.BuildError) as caught:
            self.builder(git=missing).require_clean_tree()
        self.assertEqual(caught.exception.code, 3)

        def timed_out(argv, directory):
            raise subprocess.TimeoutExpired(argv, 30)

        with self.assertRaises(build.BuildError) as caught:
            self.builder(git=timed_out).require_clean_tree()
        self.assertEqual(caught.exception.code, 3)

    def test_the_build_pushes_one_platform_with_the_revision_label(self):
        docker = FakeDocker()
        with tempfile.TemporaryDirectory() as directory:
            builder = self.builder(docker, directory=Path(directory))
            prepared = builder.plan(repository=REPOSITORY, tag="v1", platform="linux/arm64", commit=COMMIT)
            artifact = builder.build_and_push(prepared)
        self.assertEqual(artifact, {"repository": REPOSITORY, "tag": "v1", "platform": "linux/arm64",
                                    "revision": COMMIT, "digest": DIGEST, "image": f"{REPOSITORY}@{DIGEST}"})
        argv = docker.calls[0]
        self.assertEqual(argv[:3], ["docker", "buildx", "build"])
        self.assertIn("--push", argv)
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/arm64")
        self.assertEqual(argv[argv.index("--tag") + 1], REPOSITORY + ":v1")
        self.assertEqual(argv[argv.index("--label") + 1], "org.opencontainers.image.revision=" + COMMIT)
        metadata = Path(argv[argv.index("--metadata-file") + 1])
        self.assertFalse(metadata.exists())  # the metadata directory is cleaned up after the digest is read
        self.assertEqual(argv[-1], directory)

    def test_the_default_platform_is_the_common_server_platform(self):
        self.assertEqual(build.DEFAULT_PLATFORM, "linux/amd64")
        prepared = self.builder().plan(repository=REPOSITORY, tag=COMMIT, platform=build.DEFAULT_PLATFORM,
                                       commit=COMMIT)
        self.assertEqual(prepared["platform"], "linux/amd64")

    def test_invalid_tags_and_platforms_are_rejected_before_docker_runs(self):
        docker = FakeDocker()
        cases = ({"tag": "bad tag"}, {"tag": "x" * 129}, {"platform": "linux/386"},
                 {"platform": "windows/amd64"})
        for kwargs in cases:
            options = {"repository": REPOSITORY, "tag": COMMIT, "platform": build.DEFAULT_PLATFORM, "commit": COMMIT}
            options.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(build.BuildError) as caught:
                self.builder(docker).plan(**options)
            self.assertEqual(caught.exception.code, 2)
        self.assertEqual(docker.calls, [])

    def test_a_failed_build_reports_the_exit_code_and_no_image(self):
        docker = FakeDocker(returncode=7)
        prepared = self.builder(docker).plan(repository=REPOSITORY, tag="v1", platform="linux/amd64", commit=COMMIT)
        with self.assertRaises(build.BuildError) as caught:
            self.builder(docker).build_and_push(prepared)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("exit 7", str(caught.exception))

    def test_missing_or_malformed_metadata_is_refused(self):
        for metadata, expected in (("{}", "did not report"), ("not json", "not valid JSON"),
                                   ('{"containerimage.digest": "sha256:abc"}', "did not report")):
            with self.subTest(metadata=metadata):
                docker = FakeDocker(metadata=metadata)
                prepared = self.builder(docker).plan(repository=REPOSITORY, tag="v1", platform="linux/amd64",
                                                     commit=COMMIT)
                with self.assertRaises(build.BuildError) as caught:
                    self.builder(docker).build_and_push(prepared)
                self.assertEqual(caught.exception.code, 1)
                self.assertIn(expected, str(caught.exception))

    def test_a_missing_docker_binary_is_a_prerequisite_error(self):
        def missing(argv, *, timeout=None, stdout=None):
            raise OSError("not found")
        prepared = self.builder().plan(repository=REPOSITORY, tag="v1", platform="linux/amd64", commit=COMMIT)
        with self.assertRaises(build.BuildError) as caught:
            self.builder(missing).build_and_push(prepared)
        self.assertEqual(caught.exception.code, 3)

    def test_a_timed_out_build_is_a_failure(self):
        def timed_out(argv, *, timeout=None, stdout=None):
            raise subprocess.TimeoutExpired(argv, timeout)
        prepared = self.builder().plan(repository=REPOSITORY, tag="v1", platform="linux/amd64", commit=COMMIT)
        with self.assertRaises(build.BuildError) as caught:
            self.builder(timed_out).build_and_push(prepared)
        self.assertEqual(caught.exception.code, 1)


class ReleaseCliTests(unittest.TestCase):
    def manifest(self, **changes):
        value = {"name": "orders", "server": "production", "runtime": "docker",
                 "github": {"repository": "QueryPlanner/orders"}}
        value.update(changes)
        return value

    def invoke(self, arguments, *, docker=None, git=None, deploy_result=None, manifest=None):
        docker = docker or FakeDocker()
        git = git or FakeGit()
        with tempfile.TemporaryDirectory() as directory:
            builder = build.Builder(runner=docker, git_runner=git, directory=Path(directory))
            deploy_result = deploy_result or {"ok": True, "status": "succeeded", "commit": COMMIT,
                                              "artifact": f"{REPOSITORY}@{DIGEST}"}
            with patch("shiplord.cli._manifest", return_value=manifest or self.manifest()), \
                    patch("shiplord.cli._helper"), \
                    patch("shiplord.cli._head_commit", return_value=COMMIT), \
                    patch("shiplord.cli.commands.commit_sequence", return_value=1001), \
                    patch("shiplord.commands.build.Builder", return_value=builder), \
                    patch("shiplord.cli.commands.deploy_application", return_value=(0, deploy_result)) as deploy:
                result = CliRunner().invoke(cli.app, arguments)
        return result, docker, git, deploy

    def test_a_real_dirty_checkout_is_refused_without_building(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            (repository / "ship.toml").write_text("schema_version = 1\n")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "initial"],
                           cwd=repository, check=True)
            (repository / "app.py").write_text("changed\n")
            docker = FakeDocker()
            builder = build.Builder(runner=docker, directory=repository)  # the real git runner
            with patch("shiplord.cli._manifest", return_value=self.manifest()), \
                    patch("shiplord.cli._helper"), \
                    patch("shiplord.cli._head_commit", return_value=COMMIT), \
                    patch("shiplord.cli.commands.commit_sequence", return_value=1001), \
                    patch("shiplord.commands.build.Builder", return_value=builder), \
                    patch("shiplord.cli.commands.deploy_application") as deploy:
                result = CliRunner().invoke(cli.app, ["release", "--json"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(docker.calls, [])
        deploy.assert_not_called()
        self.assertIn("uncommitted changes", json.loads(result.stdout)["error"])

    def test_release_builds_pushes_and_deploys_the_pushed_digest(self):
        result, docker, git, deploy = self.invoke(["release", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(git.calls), 1)  # the clean-tree check ran first
        self.assertEqual(len(docker.calls), 1)
        argv = docker.calls[0]
        self.assertIn("--push", argv)
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/amd64")
        self.assertEqual(argv[argv.index("--tag") + 1], f"{REPOSITORY}:{COMMIT}")
        self.assertEqual(argv[argv.index("--label") + 1], "org.opencontainers.image.revision=" + COMMIT)
        deploy.assert_called_once()
        options = deploy.call_args.kwargs
        self.assertEqual(options["image"], f"{REPOSITORY}@{DIGEST}")
        self.assertEqual(options["commit"], COMMIT)
        self.assertEqual(options["sequence"], 1001)
        output = json.loads(result.stdout)
        self.assertEqual(output["build"]["digest"], DIGEST)
        self.assertEqual(output["build"]["image"], f"{REPOSITORY}@{DIGEST}")
        self.assertEqual(output["build"]["platform"], "linux/amd64")

    def test_a_dirty_tree_stops_the_release_before_the_build_and_the_deploy(self):
        result, docker, git, deploy = self.invoke(["release", "--json"], git=FakeGit(stdout=" M app.py\n"))
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(len(git.calls), 1)
        self.assertEqual(docker.calls, [])
        deploy.assert_not_called()
        self.assertIn("uncommitted changes", json.loads(result.stdout)["error"])

    def test_a_failed_build_is_not_deployed(self):
        result, docker, _git, deploy = self.invoke(["release", "--json"], docker=FakeDocker(returncode=1))
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(len(docker.calls), 1)
        deploy.assert_not_called()
        self.assertIn("build failed", json.loads(result.stdout)["error"])

    def test_a_repository_deploy_would_refuse_is_rejected_before_the_build(self):
        result, docker, _git, deploy = self.invoke(
            ["release", "--image-repository", "ghcr.io/Example/Orders", "--json"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(docker.calls, [])
        deploy.assert_not_called()
        # The registry policy refuses the uppercase repository before Docker runs or the server is contacted.
        self.assertIn("image repository", json.loads(result.stdout)["error"])

    def test_release_reports_the_build_and_deployment_in_readable_output(self):
        result, _docker, _git, deploy = self.invoke(["release"])
        self.assertEqual(result.exit_code, 0, result.output)
        deploy.assert_called_once()
        self.assertIn(f"Image: {REPOSITORY}@{DIGEST}", result.output)
        self.assertIn("Platform: linux/amd64", result.output)
        self.assertIn("Outcome: succeeded", result.output)

    def test_a_native_application_is_refused_before_any_build(self):
        result, docker, _git, deploy = self.invoke(["release", "--json"], manifest=self.manifest(runtime="native"))
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(docker.calls, [])
        deploy.assert_not_called()
        self.assertIn("deploy --binary", json.loads(result.stdout)["error"])

    def test_release_uses_an_explicit_repository_and_tag(self):
        result, docker, _git, _deploy = self.invoke(
            ["release", "--image-repository", "ghcr.io/example/other", "--tag", "v2", "--platform", "linux/arm64",
             "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        argv = docker.calls[0]
        self.assertEqual(argv[argv.index("--tag") + 1], "ghcr.io/example/other:v2")
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/arm64")
        output = json.loads(result.stdout)
        self.assertEqual(output["build"]["repository"], "ghcr.io/example/other")
        self.assertEqual(output["build"]["tag"], "v2")
        self.assertEqual(output["build"]["platform"], "linux/arm64")


if __name__ == "__main__":
    unittest.main()
