from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import shiplord
from shiplord.installer import (CLIENT_MODULES, DEPLOY_GROUP, PYTHON_PROBE, REQUIRED_PYTHON, SERVER_PYTHON,
                                SSHD_DROPIN, BootstrapError, bootstrap, helper_modules, python_supported)
from shiplord.layout import Layout, deployment_user
from shiplord.routing import RoutingError
from shiplord.system import TRUNCATION_MARKER, CommandResult

SOURCE = Path(shiplord.__file__).parent
EXPECTED_DROPIN = """\
# Managed by Shiplord. Confines deployment accounts to the forced-command dispatcher.
Match Group shiplord-deploy
    AuthorizedKeysFile /etc/shiplord/authorized_keys/%u
    AuthorizedKeysCommand none
    TrustedUserCAKeys none
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
"""


class FakeSystem:
    """Records commands and models the account database that group membership needs."""

    def __init__(self):
        self.calls = []
        self.results = {}
        self.units = {"ssh.service"}
        self.active = set()
        self.accounts = {}  # name -> passwd comment
        self.groups = {}  # name -> members
        self.truncate_passwd = False

    def run(self, argv, *, input=None, timeout=60, max_output=None):
        argv = list(argv)
        self.calls.append(argv)
        tool = argv[0].rsplit("/", 1)[-1]
        if argv[0].endswith("systemctl"):
            command = argv[1]
            if command == "list-unit-files":
                listed = [unit for unit in argv[2:] if unit in self.units]
                return CommandResult(0 if listed else 1, "".join(f"{unit} enabled enabled\n" for unit in listed))
            if command == "is-active":
                return CommandResult(0 if argv[-1] in self.active else 3, "")
            return self.results.get(argv[-1], CommandResult(0, ""))
        if tool == "getent" and argv[1] == "group":
            members = self.groups.get(argv[2])
            return CommandResult(2, "") if members is None else CommandResult(
                0, f"{argv[2]}:x:990:{','.join(sorted(members))}\n")
        if tool == "getent" and argv[1] == "passwd":
            listing = "".join(f"{name}:x:1000:1000:{comment}:/:/bin/sh\n"
                              for name, comment in sorted(self.accounts.items()))
            return CommandResult(0, TRUNCATION_MARKER + listing if self.truncate_passwd else listing)
        if tool == "groupadd":
            if tool in self.results:
                return self.results[tool]
            self.groups.setdefault(argv[-1], set())
            return CommandResult(0, "")
        if tool == "usermod":
            if tool in self.results:
                return self.results[tool]
            self.groups.setdefault(argv[argv.index("--groups") + 1], set()).add(argv[-1])
            return CommandResult(0, "")
        return self.results.get(tool, CommandResult(0, ""))


class FakeCaddy:
    def __init__(self):
        self.validated = 0
        self.reloaded = 0
        self.fail_reload = False
        self.unavailable_at: int | None = None

    def validate(self, config):
        self.validated += 1
        if self.validated == self.unavailable_at:
            raise RoutingError("could not start /usr/bin/caddy", "unavailable")

    def reload(self):
        self.reloaded += 1
        if self.fail_reload:
            raise RoutingError("failed", "reload")


class InstallerTestCase(unittest.TestCase):
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
        self.sshd_config = sshd / "sshd_config"
        self.sshd_config.write_text("Include sshd_config.d/*.conf\n")
        self.os_release = self.prefix / "etc" / "os-release"
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        self.system = FakeSystem()
        self.caddy = FakeCaddy()

    def run_bootstrap(self, source=SOURCE):
        return bootstrap(self.layout, source, self.system, self.caddy)


class ModuleSetTests(unittest.TestCase):
    def test_every_installed_module_uses_only_the_standard_library_and_the_package(self):
        allowed = set(sys.stdlib_module_names)
        for name in helper_modules(SOURCE):
            tree = ast.parse((SOURCE / name).read_text())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [(node.module or "").split(".")[0]]
                for module in modules:
                    with self.subTest(module_file=name, imported=module):
                        self.assertIn(module, allowed)

    def test_client_modules_are_excluded_and_exist(self):
        names = set(helper_modules(SOURCE))
        self.assertTrue(CLIENT_MODULES.isdisjoint(names))
        for client in CLIENT_MODULES:
            self.assertTrue((SOURCE / client).exists(), client)
        self.assertTrue({"privileged_host.py", "dispatch.py", "registry.py", "routing.py"} <= names)


class BootstrapTests(InstallerTestCase):
    def test_creates_layout_installs_code_and_wires_caddy_and_sshd(self):
        result = self.run_bootstrap()
        self.assertTrue(all(result["changed"].values()), result)
        layout = self.layout
        for path, mode in ((layout.registrations, 0o700), (layout.environments, 0o700), (layout.app_state, 0o700),
                           (layout.jobs, 0o700), (layout.authorized_keys, 0o755), (layout.var, 0o755)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)
        current = layout.lib / "current"
        self.assertEqual(os.readlink(current), result["version"])
        self.assertTrue((current / "shiplord" / "privileged_host.py").exists())
        self.assertFalse((current / "shiplord" / "cli.py").exists())
        self.assertIn("from shiplord.privileged_host import main", (current / "host_launcher.py").read_text())
        for wrapper, launcher in ((layout.host, "host_launcher.py"), (layout.admin, "admin_launcher.py"),
                                  (layout.worker, "worker_launcher.py"), (layout.dispatch, "dispatch_launcher.py")):
            self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
            self.assertEqual(wrapper.read_text(),
                             f'#!/bin/sh\nexec /usr/bin/python3 -I -S -B {layout.lib}/current/{launcher} "$@"\n')
        self.assertIn(f"import {layout.caddy_root}/ship/*.caddy", (layout.caddy_root / "Caddyfile").read_text())
        self.assertTrue((layout.caddy_root / "ship" / "00-shiplord.caddy").exists())
        self.assertEqual((self.caddy.validated, self.caddy.reloaded), (2, 1))  # probe, managed import
        dropin = layout.sshd_dropin.read_text()
        self.assertIn(f"AuthorizedKeysFile {layout.authorized_keys}/%u", dropin)
        self.assertIn("PermitTTY no", dropin)
        self.assertEqual(self.system.calls[0][:2], ["/usr/bin/systemctl", "list-unit-files"])
        self.assertIn(["/usr/sbin/sshd", "-t"], self.system.calls)
        self.assertIn(["/usr/bin/systemctl", "try-reload-or-restart", "ssh.service"], self.system.calls)
        self.assertEqual(self.system.calls[-2:], [["/usr/sbin/sshd", "-t"],
                                                  ["/usr/bin/systemctl", "try-reload-or-restart", "ssh.service"]])

    def test_the_installed_wrappers_run_the_interpreter_the_probe_checks(self):
        self.run_bootstrap()
        for wrapper in (self.layout.host, self.layout.admin, self.layout.worker, self.layout.dispatch):
            self.assertIn(f"exec {SERVER_PYTHON} -I -S -B ", wrapper.read_text())

    def test_second_run_changes_nothing(self):
        first = self.run_bootstrap()
        before = {str(p): p.read_bytes() for p in self.prefix.rglob("*") if p.is_file()}
        calls = len(self.system.calls)
        second = self.run_bootstrap()
        self.assertEqual(second["version"], first["version"])
        self.assertFalse(any(second["changed"].values()), second)
        self.assertEqual({str(p): p.read_bytes() for p in self.prefix.rglob("*") if p.is_file()}, before)
        self.assertEqual([[call[0].rsplit("/", 1)[-1], *call[1:2]] for call in self.system.calls[calls:]],
                         [["systemctl", "list-unit-files"], ["getent", "group"], ["getent", "passwd"]])
        self.assertEqual(self.caddy.reloaded, 1)

    def test_changed_code_installs_a_new_version_and_keeps_the_old_one(self):
        first = self.run_bootstrap()
        source = Path(self.temp.name) / "pkg"
        source.mkdir()
        for name in helper_modules(SOURCE):
            (source / name).write_bytes((SOURCE / name).read_bytes())
        (source / "dispatch.py").write_text((SOURCE / "dispatch.py").read_text() + "\n# changed\n")
        second = self.run_bootstrap(source)
        self.assertNotEqual(second["version"], first["version"])
        self.assertTrue(second["changed"]["code"] and second["changed"]["current"])
        self.assertFalse(second["changed"]["caddy_import"])
        self.assertEqual(os.readlink(self.layout.lib / "current"), second["version"])
        self.assertTrue((self.layout.lib / first["version"] / ".complete").exists())

    def test_tampered_installed_version_is_not_trusted(self):
        result = self.run_bootstrap()
        target = self.layout.lib / result["version"] / "shiplord" / "dispatch.py"
        target.write_text("raise SystemExit(0)\n")
        with self.assertRaisesRegex(BootstrapError, "do not match"):
            self.run_bootstrap()

    def test_non_root_cannot_bootstrap_the_real_host(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaisesRegex(BootstrapError, "root"):
            bootstrap(Layout(), SOURCE, self.system, self.caddy)

    def test_refuses_symlinked_targets(self):
        self.run_bootstrap()
        self.layout.host.unlink()
        self.layout.host.symlink_to("/bin/sh")
        with self.assertRaisesRegex(BootstrapError, "symbolic link"):
            self.run_bootstrap()

    def test_missing_caddy_configuration_directory_is_a_clear_failure_before_code_install(self):
        (self.layout.caddy_root / "Caddyfile").unlink()
        self.layout.caddy_root.rmdir()
        with self.assertRaisesRegex(BootstrapError, "Caddy is not installed"):
            self.run_bootstrap()
        self.assertFalse((self.layout.lib / "current").exists())
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_a_missing_caddy_binary_names_caddy_and_how_to_install_it(self):
        self.caddy.unavailable_at = 1
        with self.assertRaisesRegex(
                BootstrapError, r"Caddy is not installed.*install the caddy package.*/usr/bin/caddy"):
            self.run_bootstrap()
        self.assertEqual(self.caddy.validated, 1)  # the probe; nothing else ran
        self.assertFalse(self.layout.lib.exists())
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_a_missing_caddy_binary_leaves_the_previous_helper_active(self):
        first = self.run_bootstrap()
        current = os.readlink(self.layout.lib / "current")
        dropin = self.layout.sshd_dropin.read_bytes()
        self.caddy.validated = 0
        self.caddy.unavailable_at = 1
        with self.assertRaisesRegex(BootstrapError, "Caddy is not installed"):
            self.run_bootstrap()
        self.assertEqual(os.readlink(self.layout.lib / "current"), current)
        self.assertEqual(self.layout.sshd_dropin.read_bytes(), dropin)
        self.assertEqual(os.readlink(self.layout.lib / "current"), first["version"])

    def test_an_unavailable_caddy_during_the_import_keeps_the_distinct_kind(self):
        self.caddy.unavailable_at = 2  # the probe passes; the managed-import validation cannot start Caddy
        with self.assertRaisesRegex(BootstrapError, r"Caddy integration failed \(unavailable\)") as caught:
            self.run_bootstrap()
        self.assertNotIn("rejected", str(caught.exception))
        self.assertFalse((self.layout.lib / "current").exists())
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_caddy_failure_stops_before_touching_sshd(self):
        self.caddy.fail_reload = True
        with self.assertRaisesRegex(BootstrapError, "Caddy integration failed"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())
        self.assertNotIn(["/usr/sbin/sshd", "-t"], self.system.calls)


class OsBaselineTests(InstallerTestCase):
    """An unsupported host is refused before the first side effect, not after a misleading failure."""

    def assert_nothing_happened(self):
        for path in (self.layout.var, self.layout.lib, self.layout.registrations, self.layout.sshd_dropin):
            self.assertFalse(path.exists(), path)
        self.assertEqual(self.system.calls, [])
        self.assertEqual((self.caddy.validated, self.caddy.reloaded), (0, 0))
        self.assertEqual((self.layout.caddy_root / "Caddyfile").read_text(), "example.org {\n}\n")

    def test_a_supported_host_bootstraps(self):
        self.os_release.write_text('ID=debian\nVERSION_ID="12"\n')
        self.assertTrue(all(self.run_bootstrap()["changed"].values()))

    def test_ubuntu_22_04_is_refused_before_any_side_effect(self):
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
        with self.assertRaises(BootstrapError) as caught:
            self.run_bootstrap()
        message = str(caught.exception)
        self.assertIn("Ubuntu 24.04 or later and Debian 12 or later", message)
        self.assertIn("ubuntu 22.04", message)
        self.assertNotIn("/", message)
        self.assertNotIn(str(self.prefix), message)
        self.assert_nothing_happened()

    def test_debian_11_is_refused_before_any_side_effect(self):
        self.os_release.write_text('ID=debian\nVERSION_ID="11"\n')
        with self.assertRaisesRegex(BootstrapError, "debian 11 is older than the supported minimum"):
            self.run_bootstrap()
        self.assert_nothing_happened()

    def test_an_unknown_distribution_is_refused(self):
        self.os_release.write_text('ID=alpine\nVERSION_ID="3.20.3"\n')
        with self.assertRaisesRegex(BootstrapError, "alpine 3.20.3 is not a supported distribution"):
            self.run_bootstrap()
        self.assert_nothing_happened()

    def test_a_missing_or_malformed_release_file_is_refused(self):
        cases = {"missing": None, "malformed": "this is not an os-release file\n"}
        for name, content in cases.items():
            with self.subTest(name):
                if content is None:
                    self.os_release.unlink()
                else:
                    self.os_release.write_text(content)
                with self.assertRaisesRegex(BootstrapError, "Ubuntu 24.04 or later and Debian 12 or later"):
                    self.run_bootstrap()
                self.assert_nothing_happened()

    def test_the_os_check_runs_before_the_caddy_probe(self):
        self.os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
        self.caddy.unavailable_at = 1
        with self.assertRaises(BootstrapError) as caught:
            self.run_bootstrap()
        self.assertIn("ubuntu 22.04", str(caught.exception))
        self.assertNotIn("Caddy", str(caught.exception))
        self.assertEqual(self.caddy.validated, 0)


class SshdTests(InstallerTestCase):
    def test_the_installed_sshd_unit_is_discovered_and_reloaded(self):
        self.system.units = {"sshd.service"}
        self.run_bootstrap()
        self.assertIn(["/usr/bin/systemctl", "try-reload-or-restart", "sshd.service"], self.system.calls)
        self.assertNotIn(["/usr/bin/systemctl", "try-reload-or-restart", "ssh.service"], self.system.calls)

    def test_an_absent_sshd_unit_is_refused_before_the_dropin_is_written(self):
        self.system.units = set()
        with self.assertRaisesRegex(BootstrapError, r"ssh\.service.*sshd\.service"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())
        self.assertFalse((self.layout.lib / "current").exists())

    def test_a_configuration_that_does_not_include_the_dropin_is_refused(self):
        self.sshd_config.write_text("Port 22\n")
        with self.assertRaises(BootstrapError) as raised:
            self.run_bootstrap()
        self.assertIn("does not include", str(raised.exception))
        self.assertIn("Include sshd_config.d/*.conf", str(raised.exception))
        self.assertNotIn(str(self.prefix), str(raised.exception))
        self.assertFalse(self.layout.sshd_dropin.exists())
        self.assertFalse((self.layout.lib / "current").exists())
        self.assertEqual(self.system.calls, [])

    def test_the_include_may_be_reached_through_another_file(self):
        (self.sshd_config.parent / "extra.conf").write_text("Include sshd_config.d/*.conf\n")
        self.sshd_config.write_text("Include extra.conf\n")
        self.run_bootstrap()
        self.assertTrue(self.layout.sshd_dropin.exists())

    def test_a_quoted_include_is_accepted(self):
        self.sshd_config.write_text('Include "sshd_config.d/*.conf"\n')
        self.run_bootstrap()
        self.assertTrue(self.layout.sshd_dropin.exists())

    def test_a_tab_separated_include_is_accepted(self):
        self.sshd_config.write_text("Include\tsshd_config.d/*.conf\n")
        self.run_bootstrap()
        self.assertTrue(self.layout.sshd_dropin.exists())

    def test_an_include_path_containing_an_equals_sign_is_read_whole(self):
        (self.sshd_config.parent / "a=b.d").mkdir()
        (self.sshd_config.parent / "a=b.d" / "route.conf").write_text("Include sshd_config.d/*.conf\n")
        self.sshd_config.write_text("Include a=b.d/route.conf\n")
        self.run_bootstrap()
        self.assertTrue(self.layout.sshd_dropin.exists())

    def test_a_commented_out_include_does_not_count(self):
        self.sshd_config.write_text("#Include sshd_config.d/*.conf\nPort 22\n")
        with self.assertRaisesRegex(BootstrapError, "does not include"):
            self.run_bootstrap()

    def test_an_include_of_another_directory_does_not_count(self):
        self.sshd_config.write_text("Include other.d/*.conf\n")
        with self.assertRaisesRegex(BootstrapError, "does not include"):
            self.run_bootstrap()

    def test_a_shallower_glob_does_not_count_as_including_the_dropin(self):
        self.sshd_config.write_text(f"Include {self.sshd_config.parent}/*\n")
        with self.assertRaisesRegex(BootstrapError, "does not include"):
            self.run_bootstrap()

    def test_a_removed_include_is_refused_on_a_repeat_bootstrap(self):
        self.run_bootstrap()
        self.sshd_config.write_text("Port 22\n")
        with self.assertRaisesRegex(BootstrapError, "does not include"):
            self.run_bootstrap()
        self.assertTrue(self.layout.sshd_dropin.exists())

    def test_a_socket_activated_inactive_service_is_not_restarted(self):
        self.system.units = {"ssh.service", "ssh.socket"}
        self.system.active = {"ssh.socket"}
        result = self.run_bootstrap()
        self.assertTrue(result["changed"]["sshd"])
        self.assertIn(["/usr/bin/systemctl", "is-active", "--quiet", "ssh.socket"], self.system.calls)
        self.assertNotIn(["/usr/bin/systemctl", "try-reload-or-restart", "ssh.service"], self.system.calls)

    def test_a_socket_activated_running_service_is_reloaded(self):
        self.system.units = {"ssh.service", "ssh.socket"}
        self.system.active = {"ssh.service", "ssh.socket"}
        self.run_bootstrap()
        self.assertIn(["/usr/bin/systemctl", "try-reload-or-restart", "ssh.service"], self.system.calls)

    def test_rejected_sshd_configuration_is_removed(self):
        self.system.results["sshd"] = CommandResult(1, "bad")
        with self.assertRaisesRegex(BootstrapError, "rejected"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_rejected_update_restores_the_previous_dropin(self):
        self.run_bootstrap()
        previous = self.layout.sshd_dropin.read_bytes()
        self.layout.sshd_dropin.write_text("# older\n")
        self.system.results["sshd"] = CommandResult(1, "bad")
        with self.assertRaises(BootstrapError):
            self.run_bootstrap()
        self.assertEqual(self.layout.sshd_dropin.read_text(), "# older\n")
        self.assertNotEqual(previous, b"# older\n")

    def test_failed_reload_restores_and_reports(self):
        self.system.results["ssh.service"] = CommandResult(1, "failed")
        with self.assertRaisesRegex(BootstrapError, "reload"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())


class SshdPolicyTests(unittest.TestCase):
    def test_the_rendered_dropin_matches_the_deployment_group_and_disables_ca_authentication(self):
        rendered = SSHD_DROPIN.format(keys="/etc/shiplord/authorized_keys", group=DEPLOY_GROUP)
        self.assertEqual(rendered, EXPECTED_DROPIN)
        self.assertIn("Match Group shiplord-deploy\n", rendered)
        self.assertNotIn("Match User", rendered)
        self.assertNotIn("ship-*", rendered)  # a human account with the prefix keeps the host's default policy
        self.assertIn("AuthorizedKeysCommand none\n", rendered)
        self.assertIn("TrustedUserCAKeys none\n", rendered)

    def test_the_existing_confinement_is_still_rendered(self):
        rendered = SSHD_DROPIN.format(keys="/etc/shiplord/authorized_keys", group=DEPLOY_GROUP)
        for line in ("AuthorizedKeysFile /etc/shiplord/authorized_keys/%u", "AuthenticationMethods publickey",
                     "PasswordAuthentication no", "KbdInteractiveAuthentication no", "PermitTTY no",
                     "AllowTcpForwarding no", "AllowStreamLocalForwarding no", "AllowAgentForwarding no",
                     "X11Forwarding no", "PermitTunnel no", "GatewayPorts no"):
            with self.subTest(line=line):
                self.assertIn(f"{line}\n", rendered)


class DeploymentGroupTests(InstallerTestCase):
    def test_bootstrap_creates_the_group_the_policy_matches(self):
        self.assertNotIn(DEPLOY_GROUP, self.system.groups)
        result = self.run_bootstrap()
        self.assertTrue(result["changed"]["sshd_group"])
        self.assertEqual(self.system.groups[DEPLOY_GROUP], set())
        self.assertIn(["/usr/sbin/groupadd", "--system", DEPLOY_GROUP], self.system.calls)

    def test_bootstrap_confines_deployment_accounts_that_already_exist(self):
        self.system.accounts = {"ship-orders": "shiplord-deploy-orders", "ship-human": "a human account",
                                "app-orders": "shiplord-runtime-orders"}
        self.run_bootstrap()
        self.assertEqual(self.system.groups[DEPLOY_GROUP], {"ship-orders"})
        self.assertIn(["/usr/sbin/usermod", "--append", "--groups", DEPLOY_GROUP, "ship-orders"], self.system.calls)

    def test_membership_is_granted_once(self):
        self.system.accounts = {"ship-orders": "shiplord-deploy-orders"}
        self.run_bootstrap()
        calls = len(self.system.calls)
        second = self.run_bootstrap()
        self.assertFalse(second["changed"]["sshd_group"])
        self.assertEqual([call[0].rsplit("/", 1)[-1] for call in self.system.calls[calls:]],
                         ["systemctl", "getent", "getent"])

    def test_a_group_that_cannot_be_created_stops_before_the_dropin(self):
        self.system.results["groupadd"] = CommandResult(1, "failed")
        with self.assertRaisesRegex(BootstrapError, "deployment group"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_membership_that_cannot_be_granted_stops_before_the_dropin(self):
        self.system.accounts = {"ship-orders": "shiplord-deploy-orders"}
        self.system.results["usermod"] = CommandResult(1, "failed")
        with self.assertRaisesRegex(BootstrapError, "deployment group"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())

    def test_a_cut_short_account_listing_is_refused_rather_than_guessed(self):
        self.system.accounts = {"ship-orders": "shiplord-deploy-orders"}
        self.system.truncate_passwd = True
        with self.assertRaisesRegex(BootstrapError, "too large"):
            self.run_bootstrap()
        self.assertFalse(self.layout.sshd_dropin.exists())


class ServerPythonTests(unittest.TestCase):
    """The helper imports tomllib, so the interpreter the wrappers run must be 3.11 or later."""

    def test_only_python_311_or_later_is_supported(self):
        for answer in ("3 11", "3 12", "4 0", "3 11\n"):
            with self.subTest(answer=answer):
                self.assertTrue(python_supported(answer))
        for answer in ("3 10", "2 7", "", "3", "3.10.12", "Python 3.12.3", "three twelve", "3 x"):
            with self.subTest(answer=answer):
                self.assertFalse(python_supported(answer))

    def test_the_probe_prints_the_major_and_minor_version_on_this_interpreter(self):
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", PYTHON_PROBE], capture_output=True,
                                check=True)
        self.assertTrue(python_supported(result.stdout.decode("utf-8", "replace")))

    def test_the_requirement_and_the_interpreter_path_are_one_definition(self):
        self.assertEqual(SERVER_PYTHON, "/usr/bin/python3")
        self.assertEqual(REQUIRED_PYTHON, (3, 11))


class DeploymentUserTests(unittest.TestCase):
    def test_names_are_short_deterministic_and_distinct(self):
        self.assertEqual(deployment_user("orders"), "ship-orders")
        long_a, long_b = "a" * 40, "a" * 39 + "b"
        names = {deployment_user(long_a), deployment_user(long_b)}
        self.assertEqual(len(names), 2)
        for name in names | {deployment_user("orders")}:
            self.assertLessEqual(len(name), 32)
            self.assertRegex(name, r"^ship-[a-z0-9-]+$")
        self.assertEqual(deployment_user(long_a), deployment_user(long_a))
        with self.assertRaises(ValueError):
            deployment_user("../x")


if __name__ == "__main__":
    unittest.main()
