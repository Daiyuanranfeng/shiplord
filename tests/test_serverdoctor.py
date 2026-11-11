from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import shiplord  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.admin import parse_request, run  # noqa: E402
from shiplord.client import AdminClient  # noqa: E402
from shiplord.installer import bootstrap  # noqa: E402
from shiplord.protocol import ProtocolError  # noqa: E402
from shiplord.registry import Registry  # noqa: E402
from shiplord.release_state import HostLock, ReleaseState  # noqa: E402
from shiplord.routing import RouteManager  # noqa: E402
from shiplord.serverdoctor import server_doctor  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402
from test_commands import FakeSsh, ok  # noqa: E402
from test_installer import FakeCaddy, InstallerTestCase  # noqa: E402
from test_provision import FakeSystem as AccountSystem, ed25519, envelope, release  # noqa: E402

SOURCE = Path(shiplord.__file__).parent


class HostSystem(AccountSystem):
    """Accounts plus the commands the doctor runs; each can be made to fail."""

    def __init__(self):
        super().__init__()
        self.failing: set[str] = set()
        self.units = {"ssh.service"}
        self.inactive: set[str] = set()
        self.listeners = {"0.0.0.0:80", "0.0.0.0:443"}
        self.sshd_confines = True
        self.docker_inspect = ""
        self.max_outputs = []
        self.layout = None

    def sshd_policy(self, user):
        confined = self.sshd_confines and user.startswith("ship-")
        values = {"port": "22",
                  "authorizedkeysfile": (f"{self.layout.authorized_keys}/%u" if confined
                                         else ".ssh/authorized_keys .ssh/authorized_keys2"),
                  "authenticationmethods": "publickey" if confined else "any",
                  "authorizedkeyscommand": "none",
                  "trustedusercakeys": "none",
                  "passwordauthentication": "no" if confined else "yes",
                  "kbdinteractiveauthentication": "no" if confined else "yes",
                  "permittty": "no" if confined else "yes",
                  "allowtcpforwarding": "no" if confined else "yes",
                  "allowstreamlocalforwarding": "no" if confined else "yes",
                  "allowagentforwarding": "no" if confined else "yes",
                  "x11forwarding": "no" if confined else "yes",
                  "permittunnel": "no" if confined else "yes"}
        return "".join(f"{keyword} {value}\n" for keyword, value in values.items())

    def run(self, argv, *, input=None, timeout=60, max_output=4096):
        tool = argv[0].rsplit("/", 1)[-1]
        if tool == "systemctl":
            self.calls.append(list(argv))
            if argv[1] == "list-unit-files":
                listed = [unit for unit in argv[2:] if unit in self.units]
                return CommandResult(0 if listed else 1, "".join(f"{unit} enabled enabled\n" for unit in listed))
            if argv[1] == "is-active":
                return CommandResult(3 if argv[-1] in self.inactive or tool in self.failing else 0, "")
            return CommandResult(1 if tool in self.failing else 0, "")
        if tool == "ss":
            self.calls.append(list(argv))
            self.max_outputs.append(("ss", max_output))
            if tool in self.failing:
                return CommandResult(1, "")
            return CommandResult(0, "".join(f"LISTEN 0 4096 {address} 0.0.0.0:*\n" for address in sorted(self.listeners)))
        if tool == "sshd":
            self.calls.append(list(argv))
            self.max_outputs.append(("sshd", max_output))
            if tool in self.failing:
                return CommandResult(1, "")
            if argv[1] == "-T":
                user = next(part.split("=", 1)[1] for part in argv[3].split(",") if part.startswith("user="))
                return CommandResult(0, self.sshd_policy(user))
            return CommandResult(0, "")
        if tool == "docker":
            self.calls.append(list(argv))
            if tool in self.failing:
                return CommandResult(1, "")
            return CommandResult(0, "27.3.1" if argv[1] == "info" else self.docker_inspect)
        return super().run(argv, input=input, timeout=timeout)


class DoctorTestCase(InstallerTestCase):
    def setUp(self):
        super().setUp()
        self.system = HostSystem()
        self.system.layout = self.layout
        self.caddy = FakeCaddy()
        bootstrap(self.layout, SOURCE, self.system, self.caddy)
        for directory in (self.layout.sudoers_dir, self.layout.registrations, self.layout.environments):
            directory.mkdir(parents=True, exist_ok=True)
        self.var_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.var_fd)
        self.lock = HostLock(self.var_fd)

    def provision(self, app="orders", runtime="docker", image_repository=None):
        from shiplord.provision import provision
        register = Registry.register

        def register_with_free_fixture_ports(registry, *args, **kwargs):
            return register(registry, *args, probe=lambda _port: True, **kwargs)

        if image_repository is None and runtime == "docker":
            image_repository = f"ghcr.io/example/{app}"
        with self.lock.acquire(), patch.object(Registry, "register", register_with_free_fixture_ports):
            return provision(self.layout, self.system, self.lock, application_id=app, runtime=runtime,
                             api_domain=f"{app}.example.com", image_repository=image_repository,
                             public_key=ed25519(1), key_label="laptop")

    def registration(self, app="orders"):
        descriptor = os.open(self.layout.registrations, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return Registry(descriptor, self.lock).read(app)
        finally:
            os.close(descriptor)

    def commit_release(self, app="orders", record=None):
        record = release(app) if record is None else record
        state_fd = os.open(self.layout.app_state / app, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.lock.acquire():
                store = ReleaseState(app, state_fd, self.lock)
                store.begin(f"req-{app}", record)
                for phase in ("runtime-changing", "runtime-ready", "routing-changing", "routing-ready"):
                    store.advance(phase)
                store.record_commit()
                store.clear_after_reconciliation()
        finally:
            os.close(state_fd)
        return record

    def apply_route(self, app="orders"):
        with self.lock.acquire():
            RouteManager(self.layout.caddy_root, self.lock, self.caddy).apply(self.registration(app))

    def report(self):
        return server_doctor(self.layout, self.system, self.lock)

    def consistency(self, app="orders"):
        entries = self.report()["checks"]["consistency"]["applications"]
        return next(entry for entry in entries if entry["application"] == app)

    def failing(self, report):
        return sorted(name for name, check in report["checks"].items() if not check["ok"])


class ServerDoctorTests(DoctorTestCase):
    def test_a_healthy_bootstrapped_server_with_an_application_reports_ok(self):
        self.provision()
        report = self.report()
        self.assertEqual((report["ok"], self.failing(report)), (True, []))
        self.assertEqual(report["checks"]["docker"]["version"], "27.3.1")
        self.assertRegex(report["checks"]["helper"]["version"], r"^\d+\.\d+\.\d+-[0-9a-f]{12}$")
        application = report["checks"]["applications"]["applications"][0]
        self.assertEqual((application["application"], application["deployed"], application["unfinished_transaction"]),
                         ("orders", False, None))

    def test_admin_apps_action_lists_release_state_and_keeps_the_port_in_json(self):
        from shiplord.serverdoctor import application_inventory
        provisioned = self.provision()
        report = application_inventory(self.layout, self.lock)
        self.assertTrue(report["ok"])
        application = report["applications"][0]
        self.assertEqual((application["application"], application["domain"], application["runtime"],
                          application["release_recorded"], application["unfinished_transaction"]),
                         ("orders", "orders.example.com", "docker", False, None))
        self.assertEqual(application["host_port"], provisioned["host_port"])
        request = {"version": 1, "request_id": "apps-1", "action": "apps", "application_id": None,
                   "commit": None, "arguments": {}}
        response, code = run(request, self.layout, self.system, {})
        self.assertEqual(code, 0)
        self.assertEqual(response["result"]["applications"][0]["host_port"], application["host_port"])
        state_fd = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.lock.acquire():
                ReleaseState("orders", state_fd, self.lock).begin("req-apps", release())
        finally:
            os.close(state_fd)
        pending = application_inventory(self.layout, self.lock)["applications"][0]
        self.assertEqual(pending["unfinished_transaction"], {"request_id": "req-apps", "phase": "prepared"})
        response, code = run(request, self.layout, self.system, {})
        self.assertEqual(code, 0)
        self.assertEqual(response["result"]["applications"][0]["unfinished_transaction"]["phase"], "prepared")

    def test_stray_registry_file_is_reported_without_breaking_the_inventory(self):
        from shiplord.serverdoctor import application_inventory
        self.provision()
        (self.layout.registrations / "orders.json~").write_text("x")
        report = application_inventory(self.layout, self.lock)
        self.assertTrue(report["ok"])
        self.assertEqual([entry["application"] for entry in report["applications"]], ["orders"])
        self.assertEqual(report["ignored_files"], ["orders.json~"])
        request = {"version": 1, "request_id": "apps-stray", "action": "apps", "application_id": None,
                   "commit": None, "arguments": {}}
        response, code = run(request, self.layout, self.system, {})
        self.assertEqual((code, response["result"]["ignored_files"]), (0, ["orders.json~"]))

    def test_doctor_reports_stray_registry_files_without_failing(self):
        self.provision()
        (self.layout.registrations / "orders.json~").write_text("x")
        report = self.report()
        self.assertEqual(report["checks"]["applications"]["ignored_files"], ["orders.json~"])
        self.assertEqual([entry["application"] for entry in report["checks"]["applications"]["applications"]],
                         ["orders"])
        self.assertEqual((report["ok"], report["checks"]["applications"]["ok"]), (True, True))
        request = {"version": 1, "request_id": "doctor-stray", "action": "doctor", "application_id": None,
                   "commit": None, "arguments": {}}
        response, code = run(request, self.layout, self.system, {})
        self.assertEqual((code, response["result"]["checks"]["applications"]["ignored_files"]),
                         (0, ["orders.json~"]))
        healthy = FakeSsh((ok(report), b"", 0))
        self.assertEqual(commands.server_doctor(AdminClient("a@h", runner=healthy))[0], 0)

    def test_the_os_baseline_is_reported(self):
        os_check = self.report()["checks"]["os"]
        self.assertEqual((os_check["ok"], os_check["id"], os_check["version_id"]), (True, "ubuntu", "24.04"))

    def test_an_unsupported_os_is_reported_by_the_os_check(self):
        (self.layout.prefix / "etc" / "os-release").write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
        report = self.report()
        self.assertEqual((report["ok"], report["checks"]["os"]["ok"]), (False, False))
        self.assertEqual(self.failing(report), ["os"])
        self.assertIn("Ubuntu 24.04 or later and Debian 12 or later", report["checks"]["os"]["message"])
        self.assertIn("ubuntu 22.04", report["checks"]["os"]["message"])
        request = {"version": 1, "request_id": "doctor-os", "action": "doctor", "application_id": None,
                   "commit": None, "arguments": {}}
        response, code = run(request, self.layout, self.system, {})
        self.assertEqual((code, response["result"]["checks"]["os"]["ok"]), (0, False))

    def test_a_clean_registry_reports_no_ignored_files(self):
        self.provision()
        report = self.report()
        self.assertEqual(report["checks"]["applications"]["ignored_files"], [])

    def test_a_corrupt_registration_still_fails_the_applications_check(self):
        self.provision()
        (self.layout.registrations / "orders.json").write_text("{not json")
        report = self.report()
        self.assertEqual((report["checks"]["applications"]["ok"], report["ok"]), (False, False))
        request = {"version": 1, "request_id": "doctor-corrupt", "action": "doctor", "application_id": None,
                   "commit": None, "arguments": {}}
        response, code = run(request, self.layout, self.system, {})
        self.assertEqual((code, response["result"]["checks"]["applications"]["ok"]), (0, False))
        broken = FakeSsh((ok(report), b"", 0))
        self.assertEqual(commands.server_doctor(AdminClient("a@h", runner=broken))[0], 3)

    def test_admin_apps_action_rejects_application_identity_and_arguments(self):
        request = {"version": 1, "request_id": "apps-1", "action": "apps", "application_id": None,
                   "commit": None, "arguments": {}}
        self.assertEqual(parse_request(request)["action"], "apps")
        with self.assertRaises(ProtocolError):
            parse_request(dict(request, application_id="orders"))
        self.provision()
        response, code = run(dict(request, arguments={"unexpected": True}), self.layout, self.system, {})
        self.assertEqual((code, response["error"]), (2, "invalid_request"))

    def test_each_problem_is_reported_by_the_check_that_owns_it(self):
        cases = {
            "helper": lambda: (self.layout.lib / "current").unlink(),
            "caddy": lambda: (self.layout.caddy_root / "Caddyfile").write_text("example.org {\n}\n"),
            "sshd": lambda: self.layout.sshd_dropin.write_text("# edited by hand\n"),
        }
        for check, damage in cases.items():
            with self.subTest(check):
                bootstrap(self.layout, SOURCE, self.system, self.caddy) if check != "helper" else None
                damage()
                self.assertIn(check, self.failing(self.report()))
                self.setUp()

    def test_a_stopped_service_or_an_invalid_sshd_or_a_dead_docker_is_reported(self):
        self.system.failing = {"systemctl"}
        self.assertEqual(self.report()["checks"]["caddy"]["service_active"], False)

    def test_the_sshd_check_reports_the_discovered_unit_and_include(self):
        self.system.units = {"sshd.service"}
        sshd = self.report()["checks"]["sshd"]
        self.assertEqual((sshd["ok"], sshd["unit"], sshd["dropin_included"]), (True, "sshd.service", True))

    def test_the_sshd_check_fails_when_the_dropin_is_not_included(self):
        self.sshd_config.write_text("Port 22\n")
        sshd = self.report()["checks"]["sshd"]
        self.assertEqual((sshd["ok"], sshd["dropin_current"], sshd["dropin_included"]), (False, True, False))
        self.assertIn("sshd", self.failing(self.report()))

    def test_the_sshd_check_fails_when_no_unit_is_installed(self):
        self.system.units = set()
        sshd = self.report()["checks"]["sshd"]
        self.assertEqual((sshd["ok"], sshd["unit"]), (False, None))

    def test_caddy_admin_api_reachability_is_reported_without_failing_the_check(self):
        with patch("shiplord.serverdoctor.http_get", side_effect=OSError):
            caddy = self.report()["checks"]["caddy"]
        self.assertEqual(caddy["admin_api"], False)
        with patch("shiplord.serverdoctor.http_get", return_value=(200, b"")):
            self.assertEqual(self.report()["checks"]["caddy"]["admin_api"], True)
        self.system.failing = {"sshd"}
        self.assertEqual(self.report()["checks"]["sshd"]["config_valid"], False)
        self.provision()  # a Docker application makes the Docker check required
        self.system.failing = {"docker"}
        report = self.report()
        self.assertEqual((report["checks"]["docker"]["ok"], report["ok"]), (False, False))

    def test_low_disk_is_reported_with_the_measured_free_space(self):
        with patch("shiplord.serverdoctor.shutil.disk_usage", return_value=type("U", (), {"free": 50 * 2**20})()):
            disk = self.report()["checks"]["disk"]
        self.assertFalse(disk["ok"])
        self.assertEqual({entry["free_mib"] for entry in disk["paths"]}, {50})

    def test_an_unfinished_transaction_or_unreadable_state_marks_that_application(self):
        self.provision()
        state_fd = os.open(self.layout.app_state / "orders", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, state_fd)
        with self.lock.acquire():
            ReleaseState("orders", state_fd, self.lock).begin("req-1", release())
        application = self.report()["checks"]["applications"]["applications"][0]
        self.assertEqual((application["ok"], application["unfinished_transaction"]), (False, "prepared"))
        (self.layout.app_state / "orders" / "state.json").write_text("{}")
        application = self.report()["checks"]["applications"]["applications"][0]
        self.assertEqual((application["ok"], application["error"]), (False, "state is unreadable"))

    def test_the_report_names_no_secret_or_key_material(self):
        self.provision()
        text = str(self.report())
        for forbidden in ("AAAA", "ssh-ed25519", "PRIVATE KEY"):
            self.assertNotIn(forbidden, text)

    def test_the_ports_check_requires_a_listener_beyond_loopback(self):
        self.provision()
        self.commit_release()
        self.assertEqual([(port["port"], port["listening"], port["external"])
                          for port in self.report()["checks"]["ports"]["ports"]],
                         [(80, True, True), (443, True, True)])
        self.system.listeners = {"127.0.0.1:80", "[::1]:443"}
        ports = self.report()["checks"]["ports"]
        self.assertEqual((ports["ok"], [port["listening"] for port in ports["ports"]],
                          [port["external"] for port in ports["ports"]]), (False, [True, True], [False, False]))
        self.assertIn("next_step", ports)
        self.assertIn("ports", self.failing(self.report()))
        self.system.listeners = {"0.0.0.0:80", "[::]:443"}
        self.assertTrue(self.report()["checks"]["ports"]["ok"])
        self.system.listeners = {"0.0.0.0:80"}
        self.assertEqual([port["listening"] for port in self.report()["checks"]["ports"]["ports"]], [True, False])

    def test_ports_are_optional_until_an_application_is_deployed(self):
        self.system.listeners = {"0.0.0.0:80"}
        ports = self.report()["checks"]["ports"]
        self.assertEqual((ports["ok"], ports["required"], [port["listening"] for port in ports["ports"]]),
                         (True, False, [True, False]))

    def test_a_loopback_only_listener_fails_even_before_a_deploy(self):
        self.system.listeners = {"127.0.0.1:80"}
        ports = self.report()["checks"]["ports"]
        self.assertEqual((ports["ok"], ports["required"]), (False, False))
        self.assertIn("loopback", ports["next_step"])

    def test_the_ports_check_reports_an_unreadable_socket_table(self):
        self.system.failing = {"ss"}
        ports = self.report()["checks"]["ports"]
        self.assertEqual((ports["ok"], ports["error"]), (False, "could not inspect listening sockets"))
        self.assertIn("next_step", ports)
        self.assertIn("ports", self.failing(self.report()))

    def test_the_port_and_sshd_probes_ask_for_more_than_the_default_output_cap(self):
        from shiplord.system import MAX_OUTPUT_BYTES
        self.provision()
        self.report()
        limits = dict(self.system.max_outputs)
        self.assertGreater(limits["ss"], MAX_OUTPUT_BYTES)
        self.assertGreater(limits["sshd"], MAX_OUTPUT_BYTES)

    def test_the_sshd_check_proves_the_policy_applies_to_the_registered_account(self):
        self.provision()
        sshd = self.report()["checks"]["sshd"]
        self.assertEqual((sshd["ok"], sshd["policy_in_effect"], sshd["confined_to_deployment_accounts"],
                          sshd["service_active"]), (True, True, True, True))
        probes = [call for call in self.system.calls if call[1:2] == ["-T"]]
        self.assertTrue(any("user=ship-orders," in call[3] for call in probes), probes)

    def test_the_sshd_check_fails_when_the_policy_leaks_to_other_accounts(self):
        self.provision()
        self.system.sshd_confines = False
        sshd = self.report()["checks"]["sshd"]
        self.assertEqual((sshd["ok"], sshd["policy_in_effect"], sshd["confined_to_deployment_accounts"]),
                         (False, False, True))
        self.assertIn("sshd", self.failing(self.report()))

    def test_the_sshd_check_reports_no_effective_policy_without_a_deployment_account(self):
        sshd = self.report()["checks"]["sshd"]
        self.assertEqual((sshd["ok"], sshd["policy_in_effect"]), (True, None))

    def test_the_sshd_check_accepts_socket_activation_without_a_fixed_unit_name(self):
        self.system.units = {"sshd.service", "sshd.socket"}
        self.system.inactive = {"sshd.service"}
        sshd = self.report()["checks"]["sshd"]
        self.assertEqual((sshd["ok"], sshd["unit"], sshd["service_active"]), (True, "sshd.service", True))

    def test_a_native_only_host_passes_without_docker(self):
        self.provision(runtime="native", image_repository=None)
        self.system.failing = {"docker"}
        report = self.report()
        self.assertEqual(self.failing(report), [])
        self.assertEqual(report["checks"]["docker"],
                         {"ok": True, "required": False, "available": False, "version": None})
        self.assertEqual(self.consistency()["ok"], True)

    def test_a_docker_application_still_requires_docker(self):
        self.provision()
        self.system.failing = {"docker"}
        report = self.report()
        self.assertEqual((report["checks"]["docker"]["ok"], report["checks"]["docker"]["required"]), (False, True))
        self.assertIn("next_step", report["checks"]["docker"])

    def inspect_line(self, record, app="orders"):
        import json as jsonlib

        from shiplord import runtime
        from shiplord.storage import container_path

        registration = self.registration(app)
        configuration = record["configuration"]
        memory = configuration["resources"]["memory_mb"] * 1024 * 1024
        environment = jsonlib.dumps([f"{key}={value}" for key, value in
                                     runtime._managed_environment(registration, configuration).items()])
        port = configuration["http"]["container_port"]
        return (f"true false {record['artifact']['image']}|<no value>|<no value>|{configuration['template']}|"
                f" volume:shiplord-{app}-artifacts:{container_path(configuration)}"
                f"|{memory}|{memory}|{runtime.PIDS_LIMIT}|{runtime.RESTART_POLICY}"
                f"|{jsonlib.dumps(list(runtime.SECURITY_OPTIONS))}|{jsonlib.dumps(list(runtime.CAP_DROP))}"
                f"|{runtime.LOG_DRIVER}|{jsonlib.dumps(runtime.LOG_OPTIONS)}"
                f"|{jsonlib.dumps({f'{port}/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(registration['host_port'])}]})}"
                f"|{environment}")

    def test_consistency_passes_when_registration_release_runtime_and_route_agree(self):
        self.provision()
        record = self.commit_release()
        self.apply_route()
        self.system.docker_inspect = self.inspect_line(record)
        entry = self.consistency()
        self.assertEqual((entry["ok"], entry["commit"], entry["route"], entry["running"], entry["environment"],
                          entry["mismatches"]), (True, record["commit"], "identical", True, "absent", []))
        self.assertNotIn("next_step", entry)
        self.assertTrue(self.report()["checks"]["consistency"]["ok"])

    def test_consistency_names_a_route_runtime_or_account_mismatch_with_a_next_step(self):
        self.provision()
        record = self.commit_release()
        self.apply_route()
        self.system.docker_inspect = self.inspect_line(record)
        (self.layout.caddy_root / "ship" / "orders.caddy").unlink()
        entry = self.consistency()
        self.assertEqual((entry["ok"], entry["route"], entry["mismatches"]), (False, "absent", ["route"]))
        self.assertIn("redeploy", entry["next_step"])
        self.apply_route()
        self.system.docker_inspect = ""
        entry = self.consistency()
        self.assertEqual((entry["ok"], entry["running"], entry["mismatches"]), (False, False, ["runtime"]))
        self.system.docker_inspect = self.inspect_line(record)
        self.system.users["ship-orders"]["uid"] += 1
        entry = self.consistency()
        self.assertEqual(entry["mismatches"], ["account"])
        self.assertIn("re-provision", entry["next_step"])
        self.assertIn("consistency", self.failing(self.report()))

    def test_consistency_flags_a_committed_release_for_another_domain(self):
        self.provision()
        record = release("orders")
        record["configuration"]["http"]["api_domain"] = "old.example.com"
        record = self.commit_release(record=record)
        self.apply_route()
        self.system.docker_inspect = self.inspect_line(record)
        entry = self.consistency()
        self.assertEqual(entry["mismatches"], ["release"])
        self.assertIn("redeploy", entry["next_step"])

    def test_consistency_flags_an_unusable_environment_file(self):
        self.provision()
        path = self.layout.environments / "orders" / "app.env"
        path.write_text("not a KEY=VALUE line\n")
        path.chmod(0o600)
        entry = self.consistency()
        self.assertEqual((entry["ok"], entry["environment"], entry["mismatches"]), (False, "invalid", ["environment"]))
        self.assertIn("shiplord env set", entry["next_step"])

    def test_consistency_flags_a_route_without_a_committed_release(self):
        self.provision()
        self.apply_route()
        entry = self.consistency()
        self.assertEqual((entry["ok"], entry["route"], entry["mismatches"]), (False, "identical", ["route"]))

    def test_consistency_requires_a_native_environment_file_once_deployed(self):
        self.provision(runtime="native", image_repository=None)
        record = release("orders")
        record["configuration"]["runtime"] = "native"
        record["artifact"] = {"kind": "native", "sha256": "c" * 64, "size": 1024, "architecture": "linux-amd64"}
        self.commit_release(record=record)
        self.assertIn("environment", self.consistency()["mismatches"])
        (self.layout.environments / "orders" / "native.env").write_text('PORT="20000"\n')
        self.assertNotIn("environment", self.consistency()["mismatches"])

    def test_consistency_reports_unreadable_state_as_a_named_mismatch(self):
        self.provision()
        (self.layout.app_state / "orders" / "state.json").write_text("{}")
        entry = self.consistency()
        self.assertEqual((entry["ok"], entry["mismatches"], entry["running"]), (False, ["state"], None))
        self.assertIn("next_step", entry)

    def test_the_consistency_report_contains_no_host_paths_or_secrets(self):
        self.provision()
        self.commit_release()
        self.apply_route()
        text = str(self.report()["checks"]["consistency"])
        self.assertNotIn(str(self.prefix), text)
        for forbidden in ("ssh-ed25519", "PRIVATE KEY"):
            self.assertNotIn(forbidden, text)

    def test_every_failed_check_names_a_remedy_in_text_output(self):
        from shiplord.output import render_doctor
        self.provision()
        self.commit_release()
        self.system.listeners = {"127.0.0.1:80"}
        self.system.sshd_confines = False
        report = self.report()
        self.assertFalse(report["ok"])
        for name, check in report["checks"].items():
            if not check["ok"]:
                self.assertTrue(check.get("next_step"), name)
        lines = []
        render_doctor(report, "server test", say=lines.append)
        text = "\n".join(lines)
        self.assertIn("ports: failed", text)
        self.assertIn("sshd: failed", text)
        self.assertIn("next:", text)
        self.assertIn("port 80: listening; loopback only", text)
        self.assertIn("port 443: not listening; nothing bound", text)


class AdminLockTests(DoctorTestCase):
    """The host-wide lock is taken for an action that changes host state, and never for a read-only one."""

    def short_wait(self):
        """A lock that gives up quickly, so a busy-lock assertion does not wait for the production timeout."""
        class ShortWait(HostLock):
            def acquire(lock, timeout=0.05):
                return super(ShortWait, lock).acquire(timeout)
        return ShortWait

    def call(self, action):
        hostwide = action in ("doctor", "apps")
        return run(envelope(action, application_id=None if hostwide else "orders", arguments={}),
                   self.layout, self.system, {})

    def hold_lock(self):
        descriptor = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        return HostLock(descriptor).acquire()

    def test_read_only_actions_answer_while_another_operation_holds_the_lock(self):
        self.provision()
        with self.hold_lock():
            for action in ("doctor", "apps"):
                with self.subTest(action):
                    response, code = self.call(action)
                    self.assertEqual((code, response["status"]), (0, "ok"), response)

    def test_mutating_actions_take_the_lock_and_report_it_busy(self):
        self.provision()
        with patch("shiplord.admin.HostLock", self.short_wait()), self.hold_lock():
            for action in ("provision", "keys", "environment", "registry", "deprovision"):
                with self.subTest(action):
                    response, code = self.call(action)
                    self.assertEqual((code, response["message"]), (1, "host lock is busy"))

    def test_the_read_only_set_is_enumerated_and_covers_every_admin_action(self):
        from shiplord.admin import READ_ONLY_ACTIONS
        from shiplord.protocol import ADMIN_ACTIONS
        self.assertEqual(READ_ONLY_ACTIONS, frozenset({"doctor", "apps"}))
        self.assertEqual(READ_ONLY_ACTIONS | frozenset({"provision", "keys", "environment", "registry", "deprovision"}),
                         ADMIN_ACTIONS)

    def test_a_registered_deployment_account_is_denied_a_read_only_action_too(self):
        self.provision()
        uid = self.system.users["ship-orders"]["uid"]
        for action in ("doctor", "apps"):
            with self.subTest(action):
                response, code = run(envelope(action, application_id=None, arguments={}), self.layout, self.system,
                                     {"SUDO_UID": str(uid)})
                self.assertEqual((code, response["error"]), (3, "denied"))


class DoctorProtocolTests(DoctorTestCase):
    def test_only_the_doctor_may_omit_the_application(self):
        self.assertEqual(parse_request(envelope("doctor", application_id=None, arguments={}))["action"], "doctor")
        for message in (envelope("doctor", arguments={}), envelope("provision", application_id=None),
                        envelope("keys", application_id=None, arguments={"operation": "list"})):
            with self.subTest(action=message["action"]), self.assertRaises(ProtocolError):
                parse_request(message)

    def test_through_the_admin_entry_point_and_the_command_layer(self):
        response, code = run(envelope("doctor", application_id=None, arguments={}), self.layout, self.system, {})
        self.assertEqual((code, response["result"]["ok"]), (0, True))
        bad, code = run(envelope("doctor", application_id=None, arguments={"x": 1}), self.layout, self.system, {})
        self.assertEqual(code, 2)
        healthy = FakeSsh((ok({"ok": True, "checks": {"disk": {"ok": True}}}), b"", 0))
        self.assertEqual(commands.server_doctor(AdminClient("a@h", runner=healthy))[0], 0)
        broken = FakeSsh((ok({"ok": False, "checks": {"disk": {"ok": False}}}), b"", 0))
        code, result = commands.server_doctor(AdminClient("a@h", runner=broken))
        self.assertEqual((code, result["ok"]), (3, False))


if __name__ == "__main__":
    unittest.main()
