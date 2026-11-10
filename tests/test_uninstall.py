"""Uninstall removes exactly the artifacts bootstrap installed, and application data survives it."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from test_installer import SOURCE, FakeCaddy, FakeSystem  # noqa: E402
from shiplord.admin import run as admin_run  # noqa: E402
from shiplord.installer import bootstrap  # noqa: E402
from shiplord.layout import Layout  # noqa: E402
from shiplord.release_state import HostLock  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402
from shiplord.uninstall import UninstallError, UninstallRefused, uninstall  # noqa: E402

REGISTRATION = {"schema_version": 1, "application_id": "orders", "deployment_uid": 1001,
                "deployment_user": "ship-orders", "runtime": "docker", "api_domain": "orders.example.com",
                "host_port": 20000, "image_repository": "ghcr.io/example/orders", "max_memory_mb": 4096}


class UninstallTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name).resolve()
        self.layout = Layout(self.prefix)
        caddy = self.layout.caddy_root
        caddy.mkdir(parents=True)
        caddy.chmod(0o755)
        (caddy / "Caddyfile").write_text("example.org {\n}\n")
        (caddy / "Caddyfile").chmod(0o644)
        sshd = self.prefix / "etc" / "ssh"
        sshd.mkdir(parents=True)
        (sshd / "sshd_config").write_text("Include sshd_config.d/*.conf\n")
        self.system = FakeSystem()
        self.caddy = FakeCaddy()
        bootstrap(self.layout, SOURCE, self.system, self.caddy)
        self.lock_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.lock_fd)
        self.lock = HostLock(self.lock_fd)

    def run_uninstall(self, **options):
        with self.lock.acquire():
            return uninstall(self.layout, self.system, self.lock, self.caddy, **options)

    def provision(self, app="orders", **overrides):
        """A registration and the state directories provisioning creates, without an account database."""
        record = {**REGISTRATION, "application_id": app, "deployment_user": f"ship-{app}",
                  "api_domain": f"{app}.example.com", **overrides}
        path = self.layout.registrations / f"{app}.json"
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        for directory in (self.layout.environments / app, self.layout.app_state / app, self.layout.jobs / app,
                          self.layout.data / app):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        (self.layout.environments / app / "app.env").write_text("SECRET=value\n")
        (self.layout.data / app / "keep").write_text("application data\n")
        return record

    def files(self):
        """Every path under the prefix, so a refused or partial run can be compared byte for byte."""
        return {str(path.relative_to(self.prefix)): (path.read_bytes() if path.is_file() else None)
                for path in sorted(self.prefix.rglob("*"))}


class RemovalTests(UninstallTestCase):
    def test_uninstall_removes_every_artifact_bootstrap_installed(self):
        result = self.run_uninstall()
        self.assertEqual(result["status"], "uninstalled")
        self.assertEqual(result["removed"]["caddy"], {"import": True, "placeholder": True})
        self.assertTrue(result["removed"]["sshd"])
        self.assertEqual(result["removed"]["wrappers"], ["shiplord-host", "shiplord-admin", "shiplord-worker",
                                                         "shiplord-dispatch"])
        self.assertEqual(result["removed"]["versions"], 1)
        for path in (self.layout.host, self.layout.admin, self.layout.worker, self.layout.dispatch, self.layout.lib,
                     self.layout.sshd_dropin, self.layout.path("/etc/shiplord"), self.layout.caddy_root / "ship",
                     self.layout.releases):
            self.assertFalse(os.path.lexists(path), path)
        self.assertEqual((self.layout.caddy_root / "Caddyfile").read_text(), "example.org {\n}\n")
        self.assertEqual(self.caddy.reloaded, 2)  # once by bootstrap, once by uninstall
        self.assertEqual(result["applications"], [])
        self.assertEqual(result["retained"], ["the state directory that holds the host lock"])

    def test_the_host_lock_directory_is_kept_so_a_repeat_can_run_under_the_lock(self):
        self.run_uninstall()
        self.assertTrue(self.layout.var.is_dir())
        self.assertTrue((self.layout.var / "host.lock").exists())

    def test_a_second_uninstall_removes_nothing_and_is_not_an_error(self):
        self.run_uninstall()
        again = self.run_uninstall()
        self.assertEqual(again["removed"], {"caddy": {"import": False, "placeholder": False}, "sshd": False,
                                            "wrappers": [], "versions": 0, "directories": []})
        self.assertEqual(again["status"], "uninstalled")

    def test_a_host_that_was_never_bootstrapped_reports_nothing_removed(self):
        empty = Path(self.temp.name).resolve() / "never"
        (empty / "var" / "lib" / "shiplord").mkdir(parents=True)
        layout = Layout(empty)
        lock_fd = os.open(layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, lock_fd)
        lock = HostLock(lock_fd)
        with lock.acquire():
            result = uninstall(layout, self.system, lock, self.caddy)
        self.assertEqual(result["removed"]["wrappers"], [])
        self.assertEqual(result["removed"]["versions"], 0)

    def test_uninstall_requires_the_host_lock(self):
        with self.assertRaisesRegex(UninstallError, "host lock"):
            uninstall(self.layout, self.system, self.lock, self.caddy)
        self.assertTrue(self.layout.host.exists())


class PreconditionTests(UninstallTestCase):
    def test_it_refuses_while_an_application_is_provisioned_and_changes_nothing(self):
        self.provision()
        with self.lock.acquire():
            before = self.files()
            with self.assertRaises(UninstallRefused) as caught:
                uninstall(self.layout, self.system, self.lock, self.caddy)
            self.assertEqual(self.files(), before)
        message = str(caught.exception)
        self.assertIn("orders", message)
        self.assertIn("--force", message)
        self.assertIn("deprovision", message)
        self.assertNotIn(str(self.prefix), message)  # no host path in the refusal
        self.assertEqual(self.caddy.reloaded, 1)

    def test_force_removes_the_host_and_keeps_every_application_artifact(self):
        self.provision()
        result = self.run_uninstall(force=True)
        self.assertEqual(result["applications"], ["orders"])
        self.assertFalse(self.layout.host.exists())
        self.assertFalse(self.layout.lib.exists())
        self.assertFalse(self.layout.sshd_dropin.exists())
        for path in (self.layout.registrations / "orders.json", self.layout.environments / "orders" / "app.env",
                     self.layout.data / "orders" / "keep", self.layout.app_state / "orders",
                     self.layout.jobs / "orders"):
            self.assertTrue(path.exists(), path)
        self.assertTrue(any("provisioned application" in note for note in result["retained"]))
        self.assertTrue(any("managed state directories" in note and "data" in note for note in result["retained"]))
        self.assertEqual((self.layout.data / "orders" / "keep").read_text(), "application data\n")

    def test_an_unreadable_registry_is_refused_before_anything_is_removed(self):
        self.provision()
        (self.layout.registrations / "orders.json").write_text("{not json")
        with self.lock.acquire():
            before = self.files()
            with self.assertRaises(UninstallRefused) as caught:
                uninstall(self.layout, self.system, self.lock, self.caddy, force=True)
            self.assertEqual(self.files(), before)
        self.assertIn("nothing was removed", str(caught.exception))

    def test_an_untrusted_registry_directory_is_refused_before_anything_is_removed(self):
        self.layout.registrations.chmod(0o777)  # another account could swap a record between the check and the read
        with self.lock.acquire():
            before = self.files()
            with self.assertRaises(UninstallError) as caught:
                uninstall(self.layout, self.system, self.lock, self.caddy)
            self.assertEqual(self.files(), before)
        self.assertEqual(str(caught.exception), "the application registry is not trusted")
        self.assertNotIn(str(self.prefix), str(caught.exception))  # no host path in the failure
        self.assertEqual(self.caddy.reloaded, 1)  # only the bootstrap reload


class OwnershipTests(UninstallTestCase):
    def test_an_untrusted_launcher_directory_stops_the_run_where_it_stands(self):
        self.layout.host.parent.chmod(0o777)
        with self.assertRaisesRegex(UninstallError, "^the launcher directory is not trusted$"):
            self.run_uninstall()
        self.assertTrue(self.layout.host.exists())  # nothing was unlinked from a directory others can rewrite
        self.assertTrue(self.layout.admin.exists())

    def test_a_managed_directory_is_removed_when_only_its_parent_is_writable_by_others(self):
        # /opt is group-writable on a GitHub runner; the empty directory Shiplord owns is still safe to remove.
        self.layout.releases.parent.chmod(0o777)
        result = self.run_uninstall()
        self.assertIn("releases", result["removed"]["directories"])
        self.assertFalse(os.path.lexists(self.layout.releases))

    def test_a_managed_directory_writable_by_others_is_refused_by_name(self):
        self.layout.releases.chmod(0o777)
        with self.assertRaisesRegex(UninstallError, "^the releases directory is not trusted$"):
            self.run_uninstall()
        self.assertTrue(self.layout.releases.is_dir())

    def test_a_symlinked_managed_directory_is_left_in_place(self):
        elsewhere = self.layout.releases.parent / "elsewhere"
        elsewhere.mkdir()
        self.layout.releases.rmdir()
        self.layout.releases.symlink_to(elsewhere)
        result = self.run_uninstall()
        self.assertNotIn("releases", result["removed"]["directories"])
        self.assertTrue(self.layout.releases.is_symlink())  # the link is never followed and never unlinked
        self.assertTrue(elsewhere.is_dir())
        self.assertTrue(any("releases" in note for note in result["retained"]))

    def test_a_launcher_shiplord_did_not_write_is_left_in_place(self):
        foreign = "#!/bin/sh\nexec /bin/true\n"
        self.layout.host.write_text(foreign)
        result = self.run_uninstall()
        self.assertEqual(self.layout.host.read_text(), foreign)
        self.assertNotIn("shiplord-host", result["removed"]["wrappers"])
        self.assertIn("shiplord-admin", result["removed"]["wrappers"])
        self.assertIn("a launcher that Shiplord did not write", result["retained"])

    def test_an_sshd_dropin_shiplord_did_not_write_is_left_in_place(self):
        self.layout.sshd_dropin.write_text("# edited by hand\n")
        result = self.run_uninstall()
        self.assertEqual(self.layout.sshd_dropin.read_text(), "# edited by hand\n")
        self.assertFalse(result["removed"]["sshd"])
        self.assertIn("an sshd drop-in that Shiplord did not write", result["retained"])

    def test_a_caddy_file_shiplord_did_not_write_is_left_in_place(self):
        route = self.layout.caddy_root / "ship" / "orders.caddy"
        route.write_text("orders.example.com {\n\treverse_proxy 127.0.0.1:20000\n}\n")
        result = self.run_uninstall()
        self.assertEqual(route.read_text(), "orders.example.com {\n\treverse_proxy 127.0.0.1:20000\n}\n")
        self.assertTrue(result["removed"]["caddy"]["import"])
        self.assertTrue((self.layout.caddy_root / "ship").is_dir())  # not empty, so it stays
        self.assertNotIn("caddy_ship", result["removed"]["directories"])

    def test_a_replaced_placeholder_is_left_in_place(self):
        placeholder = self.layout.caddy_root / "ship" / "00-shiplord.caddy"
        placeholder.write_text("# hand written\n")
        result = self.run_uninstall()
        self.assertEqual(placeholder.read_text(), "# hand written\n")
        self.assertFalse(result["removed"]["caddy"]["placeholder"])
        self.assertIn("a managed Caddy file that Shiplord did not write", result["retained"])

    def test_a_version_directory_shiplord_did_not_finish_is_left_in_place(self):
        unfinished = self.layout.lib / "0.2.0-0123456789ab"
        unfinished.mkdir()
        (unfinished / "shiplord").mkdir()
        result = self.run_uninstall()
        self.assertEqual(result["removed"]["versions"], 1)  # the complete one
        self.assertTrue(unfinished.is_dir())
        self.assertTrue(self.layout.lib.is_dir())  # not empty, so it stays
        self.assertIn("the helper directory, which still holds files Shiplord did not write", result["retained"])

    def test_a_hand_made_directory_in_the_helper_directory_is_left_in_place(self):
        scratch = self.layout.lib / "scratch"
        scratch.mkdir()
        (scratch / "notes").write_text("mine\n")
        result = self.run_uninstall()
        self.assertEqual((scratch / "notes").read_text(), "mine\n")
        self.assertEqual(result["removed"]["versions"], 1)

    def test_a_symlinked_launcher_is_not_followed(self):
        target = self.prefix / "elsewhere"
        target.write_text("#!/bin/sh\nexit 0\n")
        self.layout.host.unlink()
        self.layout.host.symlink_to(target)
        self.run_uninstall()
        self.assertEqual(target.read_text(), "#!/bin/sh\nexit 0\n")
        self.assertTrue(self.layout.host.is_symlink())


class FailureTests(UninstallTestCase):
    def test_a_caddy_failure_stops_the_uninstall_before_the_helper_is_touched(self):
        self.caddy.fail_reload = True
        with self.assertRaisesRegex(UninstallError, "Caddy"):
            self.run_uninstall()
        for path in (self.layout.host, self.layout.admin, self.layout.sshd_dropin, self.layout.lib / "current"):
            self.assertTrue(os.path.lexists(path), path)
        self.assertIn(f"import {self.layout.caddy_root}/ship/*.caddy",
                      (self.layout.caddy_root / "Caddyfile").read_text())  # restored

    def test_sshd_rejecting_the_configuration_restores_the_dropin_and_keeps_the_launchers(self):
        previous = self.layout.sshd_dropin.read_bytes()
        self.system.results["sshd"] = CommandResult(1, "")
        with self.assertRaisesRegex(UninstallError, "restored"):
            self.run_uninstall()
        self.assertEqual(self.layout.sshd_dropin.read_bytes(), previous)
        self.assertTrue(self.layout.host.exists())

    def test_an_sshd_reload_failure_restores_the_dropin(self):
        previous = self.layout.sshd_dropin.read_bytes()
        self.system.results["ssh.service"] = CommandResult(1, "")
        with self.assertRaisesRegex(UninstallError, "restored"):
            self.run_uninstall()
        self.assertEqual(self.layout.sshd_dropin.read_bytes(), previous)

    def test_a_host_whose_sshd_cannot_be_inspected_is_refused_before_anything_is_removed(self):
        from shiplord.system import CommandError

        def failing(argv, **kwargs):
            raise CommandError("could not run", "start")

        with patch.object(self.system, "run", failing):
            with self.assertRaisesRegex(UninstallError, "nothing was removed"):
                self.run_uninstall()
        self.assertTrue(self.layout.host.exists())
        self.assertTrue(self.layout.sshd_dropin.exists())


class AdminActionTests(UninstallTestCase):
    def setUp(self):
        super().setUp()
        # The admin entry point builds its own Caddy runner; point it at the fake the bootstrap used.
        patcher = patch("shiplord.admin.SubprocessCaddy", return_value=self.caddy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, action="uninstall", application_id=None, arguments=None):
        request = {"version": 1, "request_id": "admin-uninstall", "action": action,
                   "application_id": application_id, "commit": None, "arguments": arguments or {}}
        return admin_run(request, self.layout, self.system, {})

    def test_the_admin_entry_point_runs_the_uninstall_as_a_hostwide_action(self):
        response, code = self.call()
        self.assertEqual(code, 0, response)
        self.assertEqual(response["result"]["status"], "uninstalled")
        self.assertFalse(self.layout.host.exists())

    def test_a_provisioned_application_is_rejected_with_exit_two(self):
        self.provision()
        response, code = self.call()
        self.assertEqual((code, response["error"]), (2, "rejected"))
        self.assertIn("orders", response["message"])
        self.assertTrue(self.layout.host.exists())
        response, code = self.call(arguments={"force": True})
        self.assertEqual(code, 0, response)

    def test_the_action_takes_no_application_and_only_a_boolean_force(self):
        for application_id in ("orders", ""):
            with self.subTest(application_id=application_id):
                response, code = self.call(application_id=application_id)
                self.assertEqual((code, response["error"]), (2, "invalid_request"))
        for arguments in ({"force": "yes"}, {"unexpected": True}):
            with self.subTest(arguments=arguments):
                response, code = self.call(arguments=arguments)
                self.assertEqual((code, response["error"]), (2, "invalid_request"))
        self.assertTrue(self.layout.host.exists())


if __name__ == "__main__":
    unittest.main()
