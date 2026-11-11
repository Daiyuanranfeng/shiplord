from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shiplord.osbaseline import (MINIMUM_VERSIONS, SUPPORTED, baseline, check_os, parse_os_release,
                                 read_os_release)


def release(identity: str | None, version: str | None) -> str:
    fields = ["NAME=Example"]
    if identity is not None:
        fields.append(f"ID={identity}")
    if version is not None:
        fields.append(f'VERSION_ID="{version}"')
    return "\n".join(fields) + "\n"


class ParseTests(unittest.TestCase):
    def test_quotes_comments_blank_lines_and_unknown_lines_are_handled(self):
        text = ('# a comment\n\nNAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n'
                'VERSION="24.04.1 LTS (Noble Numbat)"\nmalformed line\n')
        fields = parse_os_release(text)
        self.assertEqual((fields["NAME"], fields["ID"], fields["VERSION_ID"]), ("Ubuntu", "ubuntu", "24.04"))
        self.assertEqual(fields["VERSION"], "24.04.1 LTS (Noble Numbat)")
        self.assertNotIn("malformed line", fields)

    def test_an_unquoted_value_is_read_as_is(self):
        self.assertEqual(parse_os_release("ID=debian\nVERSION_ID=12\n"), {"ID": "debian", "VERSION_ID": "12"})


class SupportedTests(unittest.TestCase):
    def test_ubuntu_24_04_and_later_are_supported(self):
        for version in ("24.04", "24.10", "25.04", "26.04"):
            with self.subTest(version):
                result = baseline(release("ubuntu", version))
                self.assertTrue(result["ok"], result)
                self.assertEqual((result["id"], result["version_id"]), ("ubuntu", version))
                self.assertNotIn("message", result)

    def test_debian_12_and_later_are_supported(self):
        for version in ("12", "12.5", "13"):
            with self.subTest(version):
                self.assertTrue(baseline(release("debian", version))["ok"])

    def test_an_uppercase_identifier_is_accepted(self):
        self.assertTrue(baseline(release("Ubuntu", "24.04"))["ok"])

    def test_the_minimum_versions_are_the_documented_ones(self):
        self.assertEqual(MINIMUM_VERSIONS, {"ubuntu": (24, 4), "debian": (12, 0)})
        self.assertEqual(SUPPORTED, "Ubuntu 24.04 or later and Debian 12 or later")


class RefusedTests(unittest.TestCase):
    def refused(self, text):
        result = baseline(text)
        self.assertFalse(result["ok"], result)
        self.assertIn(SUPPORTED, result["message"])
        self.assertNotIn("/", result["message"])  # no host path reaches a refusal
        return result

    def test_ubuntu_22_04_is_refused(self):
        result = self.refused(release("ubuntu", "22.04"))
        self.assertEqual((result["id"], result["version_id"]), ("ubuntu", "22.04"))
        self.assertIn("ubuntu 22.04 is older than the supported minimum", result["message"])

    def test_debian_11_is_refused(self):
        self.assertIn("debian 11 is older", self.refused(release("debian", "11"))["message"])

    def test_an_unknown_distribution_is_refused_and_named(self):
        result = self.refused(release("alpine", "3.20.3"))
        self.assertIn("alpine 3.20.3 is not a supported distribution", result["message"])

    def test_a_distribution_without_a_version_is_refused(self):
        self.assertIn("did not report a usable version", self.refused(release("ubuntu", None))["message"])

    def test_a_non_numeric_version_is_refused(self):
        for version in ("bookworm/sid", "24.x", "24.04-beta", "."):
            with self.subTest(version):
                self.assertIn("did not report a usable version",
                              self.refused(release("debian", version))["message"])

    def test_a_missing_distribution_identity_is_refused(self):
        for text in ("", "NAME=Ubuntu\n", "# only a comment\n"):
            with self.subTest(text):
                self.assertIn("did not identify its distribution", self.refused(text)["message"])

    def test_an_unreadable_release_file_is_refused_without_naming_a_path(self):
        result = self.refused(None)
        self.assertEqual((result["id"], result["version_id"]), (None, None))
        self.assertIn("could not be read", result["message"])

    def test_a_field_that_could_carry_a_path_or_control_characters_is_not_echoed(self):
        for identity in ("../../etc", "/etc/os-release", "ubuntu\x00", "a" * 64):
            with self.subTest(identity):
                result = self.refused(release(identity, "22.04"))
                self.assertIsNone(result["id"])
                self.assertIn("did not identify its distribution", result["message"])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "os-release"

    def test_a_supported_file_is_read_from_disk(self):
        self.path.write_text(release("ubuntu", "24.04"))
        self.assertTrue(check_os(self.path)["ok"])

    def test_a_missing_file_is_refused(self):
        self.assertIsNone(read_os_release(self.path))
        self.assertFalse(check_os(self.path)["ok"])

    def test_an_undecodable_file_does_not_crash_the_check(self):
        self.path.write_bytes(b"ID=ubuntu\nVERSION_ID=24.04\n\xff\xfe\n")
        self.assertTrue(check_os(self.path)["ok"])


if __name__ == "__main__":
    unittest.main()
