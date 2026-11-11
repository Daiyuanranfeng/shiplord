from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from shiplord.cli import app
from shiplord.configure import (ConfigurationError, collect_answers, create_manifest, repository_from_git_remote,
                                repository_from_url, validate_repository)
from shiplord.manifest import ManifestError, load_manifest

NOTHING_GIVEN = {"server": None, "api_domain": None, "name": None, "runtime": None, "template": None,
                 "origins": None, "container_port": None, "readiness_path": None, "memory_mb": None,
                 "image_repository": None}
SERVERS = [{"alias": "production", "ssh": "admin@203.0.113.7"}, {"alias": "staging", "ssh": "deploy@staging.example.com"}]


class Script:
    """Answers prompts in order; an empty answer takes the default, as pressing Enter does."""

    def __init__(self, *answers):
        self.answers, self.prompts, self.said = list(answers), [], []

    def ask(self, prompt, default):
        self.prompts.append((prompt, default))
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        answer = self.answers.pop(0)
        return default if answer == "" and default is not None else answer

    def say(self, text):
        self.said.append(text)


class CollectAnswersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Handwriting_Studio"
        self.root.mkdir()

    def collect(self, script, given=None, servers=SERVERS, remote=lambda root: None):
        return collect_answers(dict(NOTHING_GIVEN, **(given or {})), root=self.root, servers=servers,
                               ask=script.ask, say=script.say, remote=remote)

    def test_existing_docker_repository_accepts_detected_defaults(self):
        (self.root / "Dockerfile").write_text("FROM node:22\nEXPOSE 3000\n")
        script = Script("2", "", "", "owner/orders", "api.example.com", "", "", "")
        answers = self.collect(script)
        self.assertEqual(answers, {"server": "staging", "name": "handwriting-studio", "template": "generic",
                                   "runtime": "docker", "api_domain": "api.example.com", "origins": [],
                                   "container_port": 3000, "readiness_path": "/health", "memory_mb": 512,
                                   "image_repository": "owner/orders"})
        self.assertIn("Detected port 3000 from EXPOSE in Dockerfile.", script.said)
        self.assertNotIn("Frontend origins", " ".join(prompt for prompt, _ in script.prompts))  # generic: unused

    def test_only_missing_values_are_asked(self):
        script = Script("/health")
        answers = self.collect(script, {"server": "production", "api_domain": "api.example.com", "name": "orders",
                                        "template": "generic", "origins": [], "container_port": 8000,
                                        "memory_mb": 256, "image_repository": "owner/orders"})
        self.assertEqual([prompt for prompt, _ in script.prompts], ["Health check path"])
        self.assertEqual(answers["readiness_path"], "/health")

    def test_adk_asks_for_origins_and_defaults_to_ready(self):
        script = Script("production", "orders", "google-adk", "owner/orders", "api.example.com",
                        "https://example.com, https://www.example.com", "", "", "")
        answers = self.collect(script)
        self.assertEqual(answers["origins"], ["https://example.com", "https://www.example.com"])
        self.assertEqual((answers["container_port"], answers["readiness_path"]), (8080, "/ready"))

    def test_invalid_answers_are_explained_and_asked_again(self):
        script = Script("9", "production", "Orders", "orders", "rails", "generic", "owner/orders", "Api.Example.com",
                        "203.0.113.7", "api.example.com", "99999", "8000", "health", "/health", "8192", "1024")
        answers = self.collect(script)
        self.assertEqual((answers["server"], answers["name"], answers["template"], answers["api_domain"],
                          answers["container_port"], answers["readiness_path"], answers["memory_mb"]),
                         ("production", "orders", "generic", "api.example.com", 8000, "/health", 1024))
        self.assertTrue(any("from 16 to 4096" in line for line in script.said))

    def test_repeated_invalid_answers_stop_without_a_result(self):
        with self.assertRaisesRegex(ConfigurationError, "too many invalid answers"):
            self.collect(Script("nope", "still-nope", "0"))

    def test_without_registered_servers_the_user_is_sent_to_server_add(self):
        with self.assertRaisesRegex(ConfigurationError, "shiplord server add"):
            self.collect(Script(), servers=[])

    def test_a_single_server_is_the_default(self):
        script = Script("", "orders", "generic", "owner/orders", "api.example.com", "", "", "")
        self.assertEqual(self.collect(script, servers=SERVERS[:1])["server"], "production")
        self.assertEqual(script.prompts[0], ("Server", "1"))

    def test_native_runtime_is_not_asked_for_a_container_port(self):
        script = Script("orders", "google-adk", "api.example.com", "", "", "")
        answers = self.collect(script, {"server": "production", "runtime": "native"})
        self.assertNotIn("Container port", [prompt for prompt, _ in script.prompts])
        self.assertEqual(answers["container_port"], 8080)

    def test_a_native_application_is_not_asked_for_an_image_repository(self):
        script = Script("orders", "google-adk", "api.example.com", "", "", "")
        answers = self.collect(script, {"server": "production", "runtime": "native"})
        self.assertNotIn("Image repository", [prompt for prompt, _ in script.prompts])
        self.assertIsNone(answers["image_repository"])

    def test_an_unusable_directory_name_is_not_offered_as_the_default(self):
        root = Path(self.temp.name) / "2024.site"
        root.mkdir()
        script = Script("orders", "generic", "owner/orders", "api.example.com", "", "", "")
        collect_answers(dict(NOTHING_GIVEN, server="production"), root=root, servers=SERVERS, ask=script.ask,
                        say=script.say, remote=lambda root: None)
        self.assertEqual(script.prompts[0], ("Application name", None))

    def test_the_image_repository_is_inferred_from_the_git_remote(self):
        script = Script("production", "orders", "generic", "api.example.com", "", "", "")
        answers = self.collect(script, remote=lambda root: "owner/orders")
        self.assertEqual(answers["image_repository"], "owner/orders")
        self.assertNotIn("Image repository", [prompt for prompt, _ in script.prompts])
        self.assertIn("Detected the image repository owner/orders from the git remote origin.", script.said)

    def test_an_explicit_image_repository_is_not_asked_for(self):
        script = Script("production", "orders", "generic", "api.example.com", "", "", "")
        answers = self.collect(script, {"image_repository": "other/orders"}, remote=lambda root: "owner/orders")
        self.assertEqual(answers["image_repository"], "other/orders")
        self.assertNotIn("Image repository", [prompt for prompt, _ in script.prompts])

    def test_an_invalid_image_repository_is_explained_and_asked_again(self):
        script = Script("production", "orders", "generic", "orders", "owner/orders", "api.example.com", "", "", "")
        self.assertEqual(self.collect(script)["image_repository"], "owner/orders")
        self.assertTrue(any("owner/repository" in line for line in script.said))

    def test_a_remote_credential_never_reaches_the_output(self):
        url = "https://user:token@github.com/owner/orders.git"
        script = Script("production", "orders", "generic", "api.example.com", "", "", "")
        answers = self.collect(script, remote=lambda root: repository_from_url(url))
        self.assertEqual(answers["image_repository"], "owner/orders")
        self.assertNotIn("token", " ".join(script.said))


class RepositoryTests(unittest.TestCase):
    def test_github_remotes_are_understood_in_every_form_git_writes(self):
        for url in ("git@github.com:owner/orders.git", "git@github.com:owner/orders",
                    "https://github.com/owner/orders.git", "https://github.com/owner/orders",
                    "ssh://git@github.com/owner/orders.git", "ssh://git@github.com:22/owner/orders.git",
                    "https://GitHub.com/owner/orders.git", "  https://github.com/owner/orders.git\n"):
            with self.subTest(url=url):
                self.assertEqual(repository_from_url(url), "owner/orders")

    def test_a_remote_that_names_no_github_repository_is_not_guessed(self):
        for url in ("", "   ", "nonsense", "git@github.com:owner", "git@github.com:owner/orders/tree/main",
                    "https://gitlab.com/owner/orders.git", "https://github.com/owner", "https://github.com/",
                    "/srv/git/orders.git", "https://github.com/owner/../orders.git"):
            with self.subTest(url=url):
                self.assertIsNone(repository_from_url(url))

    def test_validate_repository_uses_the_manifest_rule(self):
        for value in ("owner/orders", "Owner/Orders.2", "owner/my-orders"):
            with self.subTest(value=value):
                self.assertEqual(validate_repository(value), value)
        for value in ("orders", "owner/", "/orders", "owner/orders/tree/main", "", "owner/..", None):
            with self.subTest(value=value):
                with self.assertRaises(ManifestError):
                    validate_repository(value)


@unittest.skipUnless(shutil.which("git"), "git is needed to read a remote")
class GitRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_origin_remote_is_read(self):
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://github.com/owner/orders.git")
        self.assertEqual(repository_from_git_remote(self.root), "owner/orders")

    def test_a_directory_that_is_not_a_repository_has_no_remote(self):
        self.assertIsNone(repository_from_git_remote(self.root))

    def test_a_repository_without_origin_has_no_remote(self):
        self.git("init", "-q")
        self.git("remote", "add", "upstream", "https://github.com/owner/orders.git")
        self.assertIsNone(repository_from_git_remote(self.root))

    def test_an_odd_remote_is_not_a_crash(self):
        self.git("init", "-q")
        self.git("remote", "add", "origin", "https://example.com/nonsense")
        self.assertIsNone(repository_from_git_remote(self.root))


class CreateManifestTests(unittest.TestCase):
    def test_the_image_repository_is_written_as_the_github_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_manifest(Path(directory), "orders", "production", "api.example.com", [],
                                   image_repository="owner/orders")
            self.assertEqual(load_manifest(path)["github"],
                             {"repository": "owner/orders", "deployment_branch": "main"})
            self.assertIn('[github]\nrepository = "owner/orders"\ndeployment_branch = "main"\n', path.read_text())

    def test_a_manifest_without_a_repository_has_no_github_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_manifest(Path(directory), "orders", "production", "api.example.com", [])
            self.assertNotIn("github", load_manifest(path))

    def test_an_unusable_repository_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ManifestError, "owner/repository"):
                create_manifest(Path(directory), "orders", "production", "api.example.com", [],
                                image_repository="orders")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_additional_hostnames_and_the_www_alias_are_written_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = create_manifest(Path(directory), "orders", "production", "example.com", [],
                                   hostnames=["legacy.example.com"], www_redirect=True)
            self.assertEqual(load_manifest(path)["http"]["hostnames"],
                             ["legacy.example.com", "www.example.com"])
            self.assertIn('hostnames = ["legacy.example.com", "www.example.com"]\n', path.read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = create_manifest(Path(directory), "orders", "production", "example.com", [])
            self.assertNotIn("hostnames", path.read_text())  # a manifest without aliases is unchanged

    def test_an_unusable_additional_hostname_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ManifestError):
                create_manifest(Path(directory), "orders", "production", "example.com", [],
                                hostnames=["example.com"])  # the alias would redirect to itself
            self.assertEqual(list(Path(directory).iterdir()), [])


class InteractiveConfigureTests(unittest.TestCase):
    """The terminal path, run in-process with scripted keyboard input."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / "orders"
        self.project.mkdir()
        (self.project / "Dockerfile").write_text("FROM python:3.12\nEXPOSE 8000\n")
        servers = root / "config/shiplord/servers"
        servers.mkdir(parents=True)
        for path in (root / "config", servers.parent, servers):
            path.chmod(0o700)
        (servers / "production.json").write_text(json.dumps({"schema_version": 1, "alias": "production",
                                                             "ssh": "admin@example.com"}))
        (servers / "production.json").chmod(0o600)
        self.env = {"XDG_CONFIG_HOME": str(root / "config")}
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    def run_configure(self, keyboard, *args):
        with patch.dict(os.environ, self.env), patch("shiplord.cli._interactive", return_value=True):
            return CliRunner().invoke(app, ["configure", *args], input=keyboard)

    def test_accepting_the_summary_writes_the_manifest(self):
        result = self.run_configure("\n\n\nowner/orders\napi.example.com\n\n\n\ny\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("container port: 8000", result.output)
        self.assertIn("image repository: owner/orders", result.output)
        self.assertIn("registered locally", result.output)
        config = load_manifest(self.project / "ship.toml")
        self.assertEqual((config["name"], config["server"], config["template"], config["http"]["container_port"]),
                         ("orders", "production", "generic", 8000))
        self.assertEqual(config["github"], {"repository": "owner/orders", "deployment_branch": "main"})

    def test_declining_or_ending_input_writes_nothing(self):
        for keyboard in ("\n\n\nowner/orders\napi.example.com\n\n\n\nn\n", "\n\n"):
            with self.subTest(keyboard=keyboard):
                result = self.run_configure(keyboard)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("no file was created", result.output)
                self.assertEqual(sorted(path.name for path in self.project.iterdir()), ["Dockerfile"])

    def test_an_existing_manifest_is_refused_before_any_question(self):
        (self.project / "ship.toml").write_text("keep")
        result = self.run_configure("")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertNotIn("Server", result.output)
        self.assertEqual((self.project / "ship.toml").read_text(), "keep")

    def test_flags_skip_their_questions(self):
        result = self.run_configure("owner/orders\napi.example.com\n\n\ny\n", "--server", "production",
                                    "--name", "orders", "--template", "generic", "--container-port", "9000")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Servers:", result.output)
        self.assertEqual(load_manifest(self.project / "ship.toml")["http"]["container_port"], 9000)

    def test_an_image_repository_flag_skips_its_question(self):
        result = self.run_configure("api.example.com\n\n\n\ny\n", "--server", "production", "--name", "orders",
                                    "--template", "generic", "--image-repository", "owner/orders")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Image repository", result.output)
        self.assertEqual(load_manifest(self.project / "ship.toml")["github"]["repository"], "owner/orders")

    def test_hostname_flags_add_aliases_without_another_question(self):
        result = self.run_configure("owner/orders\napi.example.com\n\n\n\ny\n", "--server", "production",
                                    "--name", "orders", "--template", "generic", "--hostname", "legacy.example.com",
                                    "--www")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("additional hostnames: legacy.example.com", result.output)
        self.assertIn("www redirect: www.api.example.com", result.output)
        self.assertEqual(load_manifest(self.project / "ship.toml")["http"]["hostnames"],
                         ["legacy.example.com", "www.api.example.com"])

    def test_an_unusable_hostname_flag_writes_nothing(self):
        result = self.run_configure("owner/orders\napi.example.com\n\n\n\ny\n", "--server", "production",
                                    "--name", "orders", "--template", "generic", "--hostname", "www.other.test")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(sorted(path.name for path in self.project.iterdir()), ["Dockerfile"])

    @unittest.skipUnless(shutil.which("git"), "git is needed to read a remote")
    def test_a_github_remote_skips_the_repository_question(self):
        result = subprocess.run(["git", "-C", str(self.project), "init", "-q"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin",
                                 "git@github.com:owner/orders.git"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_configure("\n\n\napi.example.com\n\n\n\ny\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Image repository", result.output)
        self.assertIn("Detected the image repository owner/orders from the git remote origin.", result.output)
        self.assertEqual(load_manifest(self.project / "ship.toml")["github"]["repository"], "owner/orders")
