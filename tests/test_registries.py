from __future__ import annotations

import unittest

from shiplord import registries
from shiplord.registries import RegistryPolicyError

DIGEST = "sha256:" + "a" * 64


class RepositoryTests(unittest.TestCase):
    def test_supported_repositories_are_stored_in_their_one_spelling(self):
        for given, stored in (
            ("ghcr.io/queryplanner/orders", "ghcr.io/queryplanner/orders"),
            ("docker.io/example/orders", "docker.io/example/orders"),
            ("docker.io/library/nginx", "docker.io/library/nginx"),
            ("docker.io/example/my--image", "docker.io/example/my--image"),
            ("nginx", "docker.io/library/nginx"),
            ("hello-world", "docker.io/library/hello-world"),
        ):
            with self.subTest(given=given):
                self.assertEqual(registries.validate_repository(given), stored)

    def test_ambiguous_or_unsupported_forms_are_refused(self):
        for value in ("example/orders", "docker.io/nginx", "quay.io/example/orders", "localhost:5000/example/orders",
                      "ghcr.io/orders", "ghcr.io/Example/orders", "docker.io/example/orders:latest",
                      f"docker.io/example/orders@{DIGEST}", "https://ghcr.io/example/orders", " ghcr.io/example/orders",
                      "ghcr.io/example/orders ", "ghcr.io//orders", "ghcr.io/../orders", "", None, 5):
            with self.subTest(value=value):
                with self.assertRaises(RegistryPolicyError):
                    registries.validate_repository(value)

    def test_a_github_pair_becomes_its_lowercase_ghcr_repository(self):
        self.assertEqual(registries.github_repository("QueryPlanner/Orders"), "ghcr.io/queryplanner/orders")
        self.assertEqual(registries.validate_repository(registries.github_repository("a/b")), "ghcr.io/a/b")


class PinnedReferenceTests(unittest.TestCase):
    def test_both_registries_pin_to_a_sha256_digest(self):
        for given, stored in (
            (f"ghcr.io/example/orders@{DIGEST}", f"ghcr.io/example/orders@{DIGEST}"),
            (f"docker.io/example/orders@{DIGEST}", f"docker.io/example/orders@{DIGEST}"),
            (f"docker.io/library/nginx@{DIGEST}", f"docker.io/library/nginx@{DIGEST}"),
            (f"nginx@{DIGEST}", f"docker.io/library/nginx@{DIGEST}"),
        ):
            with self.subTest(given=given):
                self.assertEqual(registries.validate_pinned(given), stored)
                self.assertEqual(registries.repository_of(given), stored.partition("@")[0])

    def test_a_tag_or_a_non_sha256_digest_is_refused(self):
        for value in ("ghcr.io/example/orders:latest", "ghcr.io/example/orders@sha256:abc",
                      f"ghcr.io/example/orders@sha512:{'a' * 128}", f"ghcr.io/example/orders@SHA256:{'a' * 64}",
                      f"ghcr.io/example/orders@{'A' * 64}", f"ghcr.io/example/orders@{DIGEST}@{DIGEST}",
                      f"ghcr.io/example/orders", f"quay.io/example/orders@{DIGEST}", f"example/orders@{DIGEST}",
                      f"docker.io/nginx@{DIGEST}", "", None):
            with self.subTest(value=value):
                with self.assertRaises(RegistryPolicyError):
                    registries.validate_pinned(value)


class LoginPolicyTests(unittest.TestCase):
    def test_docker_hub_uses_the_key_docker_looks_up(self):
        self.assertEqual(registries.login_key("ghcr.io"), "ghcr.io")
        self.assertEqual(registries.login_key("docker.io"), "https://index.docker.io/v1/")
        self.assertEqual(registries.login_keys("docker.io"),
                         ("https://index.docker.io/v1/", "docker.io"))
        self.assertEqual(registries.login_keys("ghcr.io"), ("ghcr.io",))
        self.assertEqual(registries.host_for_key("https://index.docker.io/v1/"), "docker.io")
        self.assertEqual(registries.host_for_key("ghcr.io"), "ghcr.io")
        self.assertEqual(registries.host_for_key("registry.example"), "registry.example")

    def test_only_the_two_supported_hosts_are_accepted(self):
        for host in ("ghcr.io", "docker.io"):
            self.assertEqual(registries.validate_host(host), host)
        for host in ("quay.io", "index.docker.io", "registry-1.docker.io", "https://index.docker.io/v1/",
                     "docker.io/evil", "GHCR.IO", "", None):
            with self.subTest(host=host):
                with self.assertRaises(RegistryPolicyError):
                    registries.validate_host(host)

    def test_the_username_and_token_rules_are_shared_and_never_echoed(self):
        self.assertEqual(registries.validate_login("docker.io", "deploy-bot", "tok_en-1"), "docker.io")
        for username, token in (("a b", "s3cr3t"), ("", "s3cr3t"), (None, "s3cr3t"),
                                ("bot", "has s3cr3t"), ("bot", ""), ("bot", "s3cr3t" * 100),
                                ("bot", "s3cr3t\n"), ("bot", None)):
            with self.subTest(username=username, token=str(token)[:10]):
                with self.assertRaises(RegistryPolicyError) as caught:
                    registries.validate_login("ghcr.io", username, token)
                if isinstance(token, str) and token:
                    self.assertNotIn(token, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
