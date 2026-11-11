from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from shiplord.registry import PORT_RANGE, Registry, RegistryError, port_is_free, validate_registration
from shiplord.release_state import HostLock, StateError


def record(**changes):
    value = {"schema_version": 1, "application_id": "orders", "deployment_uid": 1001,
             "deployment_user": "ship-orders", "runtime": "docker", "api_domain": "orders.example.com",
             "host_port": 20000, "image_repository": "ghcr.io/example/orders", "max_memory_mb": 4096}
    value.update(changes)
    return value


def request(app="orders", **changes):
    value = {"deployment_uid": 1001, "deployment_user": "ship-orders", "runtime": "docker",
             "api_domain": f"{app}.example.com", "image_repository": f"ghcr.io/example/{app}"}
    value.update(changes)
    return value


class ValidationTests(unittest.TestCase):
    def test_valid_docker_and_native_records_round_trip(self):
        self.assertEqual(validate_registration(record()), record())
        native = record(runtime="native", image_repository=None)
        self.assertEqual(validate_registration(native, "orders"), native)

    def test_docker_hub_repositories_are_stored_in_their_one_spelling(self):
        for given, stored in (("docker.io/example/orders", "docker.io/example/orders"),
                              ("docker.io/library/nginx", "docker.io/library/nginx"),
                              ("nginx", "docker.io/library/nginx")):
            with self.subTest(given=given):
                self.assertEqual(validate_registration(record(image_repository=given))["image_repository"], stored)

    def test_invalid_records_are_rejected(self):
        cases = {
            "extra field": record(extra=1),
            "missing field": {k: v for k, v in record().items() if k != "host_port"},
            "bool version": record(schema_version=True),
            "future version": record(schema_version=2),
            "bad app": record(application_id="Orders"),
            "bad domain": record(api_domain="Orders.Example.com"),
            "ip domain": record(api_domain="127.0.0.1"),
            "bool uid": record(deployment_uid=True),
            "root uid": record(deployment_uid=0),
            "bad user": record(deployment_user="ship orders"),
            "newline user": record(deployment_user="ship\nroot"),
            "bad runtime": record(runtime="vm"),
            "docker without repository": record(image_repository=None),
            "docker with tag": record(image_repository="ghcr.io/example/orders:latest"),
            "docker other registry": record(image_repository="quay.io/example/orders"),
            "docker ambiguous pair": record(image_repository="example/orders"),
            "docker hub without a namespace": record(image_repository="docker.io/nginx"),
            "native with repository": record(runtime="native"),
            "port below range": record(host_port=PORT_RANGE.start - 1),
            "port above range": record(host_port=PORT_RANGE.stop),
            "caddy admin port": record(host_port=2019),
            "bool port": record(host_port=True),
            "not an object": [],
        }
        for label, value in cases.items():
            with self.subTest(label):
                with self.assertRaises(RegistryError):
                    validate_registration(value)

    def test_application_mismatch_is_rejected(self):
        with self.assertRaises(RegistryError):
            validate_registration(record(), "billing")

    def test_additional_hostnames_round_trip_and_are_validated(self):
        aliases = record(hostnames=["legacy.example.com", "www.orders.example.com"])
        self.assertEqual(validate_registration(aliases), aliases)
        self.assertNotIn("hostnames", validate_registration(record()))  # a record without aliases is unchanged
        for bad in (["orders.example.com"], ["www.other.example.com"], ["legacy.example.com"] * 2,
                    ["Legacy.example.com"], ["127.0.0.1"], "legacy.example.com"):
            with self.subTest(hostnames=bad), self.assertRaises(RegistryError):
                validate_registration(record(hostnames=bad))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.lock_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.lock_temp.cleanup)
        lock_fd = os.open(self.lock_temp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, lock_fd)
        self.lock = HostLock(lock_fd)
        self.registry = Registry(self.fd, self.lock)
        self.busy: set[int] = set()

    def register(self, app="orders", **changes):
        return self.registry.register(app, probe=lambda port: port not in self.busy, **request(app, **changes))

    def test_registration_requires_the_host_lock(self):
        with self.assertRaisesRegex(RegistryError, "lock"):
            self.register()
        self.assertEqual(os.listdir(self.directory), [])

    def test_first_registration_gets_the_lowest_port_and_is_private(self):
        with self.lock.acquire():
            created = self.register()
        self.assertEqual(created["host_port"], PORT_RANGE.start)
        path = self.directory / "orders.json"
        self.assertEqual(json.loads(path.read_text()), created)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.registry.read("orders"), created)

    def test_registering_twice_is_idempotent_and_keeps_the_port(self):
        with self.lock.acquire():
            first = self.register()
            self.busy.add(first["host_port"])  # the app itself now listens there
            self.assertEqual(self.register(), first)
        self.assertEqual(os.listdir(self.directory), ["orders.json"])

    def test_second_app_gets_a_distinct_port_skipping_listeners(self):
        with self.lock.acquire():
            first = self.register("orders")
            self.busy.add(first["host_port"] + 1)
            second = self.register("billing", deployment_uid=1002, deployment_user="ship-billing")
        self.assertEqual(second["host_port"], first["host_port"] + 2)
        self.assertEqual({r["application_id"] for r in self.registry.list()}, {"orders", "billing"})

    def test_changed_inputs_for_an_existing_app_are_refused(self):
        with self.lock.acquire():
            first = self.register()
            for change in ({"api_domain": "other.example.com"}, {"runtime": "native", "image_repository": None},
                           {"image_repository": "ghcr.io/example/other"}, {"deployment_uid": 2000},
                           {"deployment_user": "someone"}):
                with self.subTest(change=change):
                    with self.assertRaisesRegex(RegistryError, "differs"):
                        self.register(**change)
        self.assertEqual(self.registry.read("orders"), first)

    def test_update_changes_only_the_hostname_and_memory_ceiling(self):
        with self.lock.acquire():
            first = self.register()
            updated, changed = self.registry.update(
                "orders", deployment_uid=first["deployment_uid"], deployment_user=first["deployment_user"],
                runtime="docker", api_domain="new.example.com", image_repository="ghcr.io/example/orders",
                max_memory_mb=8192)
        self.assertTrue(changed)
        self.assertEqual((updated["api_domain"], updated["max_memory_mb"]), ("new.example.com", 8192))
        self.assertEqual((updated["host_port"], updated["deployment_uid"], updated["deployment_user"],
                          updated["runtime"], updated["image_repository"]),
                         (first["host_port"], first["deployment_uid"], first["deployment_user"],
                          first["runtime"], first["image_repository"]))
        self.assertEqual(self.registry.read("orders"), updated)
        self.assertEqual(json.loads((self.directory / "orders.json").read_text()), updated)

    def test_update_is_idempotent_and_a_dry_run_writes_nothing(self):
        with self.lock.acquire():
            first = self.register()
            before = (self.directory / "orders.json").read_bytes()
            planned, changed = self.registry.update(
                "orders", deployment_uid=first["deployment_uid"], deployment_user=first["deployment_user"],
                runtime="docker", api_domain="new.example.com", image_repository="ghcr.io/example/orders",
                max_memory_mb=8192, dry_run=True)
            self.assertTrue(changed)
            self.assertEqual(planned["api_domain"], "new.example.com")
            self.assertEqual((self.directory / "orders.json").read_bytes(), before)
            self.assertEqual(self.registry.update(
                "orders", deployment_uid=first["deployment_uid"], deployment_user=first["deployment_user"],
                runtime="docker", api_domain="orders.example.com", image_repository="ghcr.io/example/orders"),
                (first, False))

    def test_update_refuses_a_changed_runtime_repository_or_account(self):
        with self.lock.acquire():
            first = self.register()
            for change in ({"runtime": "native", "image_repository": None},
                           {"image_repository": "ghcr.io/example/other"},
                           {"deployment_uid": 2000}, {"deployment_user": "someone"}):
                with self.subTest(change=change), self.assertRaisesRegex(RegistryError, "cannot change it"):
                    self.registry.update("orders", **request("orders", **change))
            self.assertEqual(self.registry.read("orders"), first)
        self.assertEqual(json.loads((self.directory / "orders.json").read_text()), first)

    def test_update_refuses_an_unknown_application_a_conflict_or_invalid_input_without_writing(self):
        with self.lock.acquire():
            self.register("orders")
            self.register("billing", deployment_uid=1002, deployment_user="ship-billing")
            before = {name: (self.directory / name).read_bytes() for name in os.listdir(self.directory)}
            with self.assertRaisesRegex(RegistryError, "not registered"):
                self.registry.update("catalog", **request("catalog"))
            with self.assertRaisesRegex(RegistryError, "hostname"):
                self.registry.update("orders", **request("orders", api_domain="billing.example.com"))
            for bad in ({"api_domain": "bad domain"}, {"max_memory_mb": 0}, {"runtime": "vm"}):
                with self.subTest(bad=bad), self.assertRaises(RegistryError):
                    self.registry.update("orders", **request("orders", **bad))
        self.assertEqual({name: (self.directory / name).read_bytes() for name in os.listdir(self.directory)}, before)

    def test_update_requires_the_host_lock(self):
        with self.assertRaisesRegex(RegistryError, "lock"):
            self.registry.update("orders", **request("orders"))
        self.assertEqual(os.listdir(self.directory), [])

    def test_hostname_and_account_conflicts_are_refused_without_writing(self):
        with self.lock.acquire():
            self.register("orders")
            with self.assertRaisesRegex(RegistryError, "hostname"):
                self.register("billing", api_domain="orders.example.com",
                              deployment_uid=1002, deployment_user="ship-billing")
            with self.assertRaisesRegex(RegistryError, "account"):
                self.register("billing")
            with self.assertRaisesRegex(RegistryError, "account"):
                self.register("billing", deployment_uid=1002)
        self.assertEqual(os.listdir(self.directory), ["orders.json"])

    def test_an_alias_conflicts_with_another_applications_canonical_hostname(self):
        with self.lock.acquire():
            self.register("orders")
            for alias in (["orders.example.com"], ["legacy.example.com", "orders.example.com"]):
                with self.subTest(alias=alias), self.assertRaisesRegex(RegistryError, "hostname"):
                    self.register("billing", hostnames=alias, deployment_uid=1002,
                                  deployment_user="ship-billing")
                with self.subTest(alias=alias, check=True), self.assertRaisesRegex(RegistryError, "hostname"):
                    self.registry.check("billing", **request("billing", hostnames=alias, deployment_uid=1002,
                                                             deployment_user="ship-billing"))
        self.assertEqual(os.listdir(self.directory), ["orders.json"])

    def test_a_canonical_hostname_conflicts_with_another_applications_alias(self):
        with self.lock.acquire():
            self.register("orders", hostnames=["legacy.example.com", "www.orders.example.com"])
            for change in ({"api_domain": "legacy.example.com"}, {"api_domain": "www.orders.example.com"}):
                with self.subTest(change=change), self.assertRaisesRegex(RegistryError, "hostname"):
                    self.register("billing", deployment_uid=1002, deployment_user="ship-billing", **change)
        self.assertEqual(sorted(os.listdir(self.directory)), ["orders.json"])

    def test_aliases_are_stored_and_an_identical_registration_is_returned(self):
        with self.lock.acquire():
            created = self.register(hostnames=["legacy.example.com", "www.orders.example.com"])
            self.assertEqual(created["hostnames"], ["legacy.example.com", "www.orders.example.com"])
            self.assertEqual(json.loads((self.directory / "orders.json").read_text()), created)
            self.assertEqual(self.registry.check("orders", **request(
                hostnames=["legacy.example.com", "www.orders.example.com"])), created)
            self.assertEqual(self.register(hostnames=["legacy.example.com", "www.orders.example.com"]), created)

    def test_changing_the_aliases_of_a_registered_application_is_refused(self):
        with self.lock.acquire():
            first = self.register(hostnames=["legacy.example.com"])
            for hostnames in ([], ["other.example.com"], ["legacy.example.com", "www.orders.example.com"]):
                with self.subTest(hostnames=hostnames), self.assertRaisesRegex(RegistryError, "differs"):
                    self.register(hostnames=hostnames)
                with self.subTest(hostnames=hostnames, check=True), self.assertRaisesRegex(RegistryError, "differs"):
                    self.registry.check("orders", **request(hostnames=hostnames))
            self.assertEqual(self.registry.read("orders"), first)

    def test_invalid_input_is_rejected_before_any_write(self):
        with self.lock.acquire():
            for bad in ({"api_domain": "bad domain"}, {"runtime": "vm"}, {"deployment_uid": 0}):
                with self.subTest(bad=bad):
                    with self.assertRaises(RegistryError):
                        self.register(**bad)
            with self.assertRaises(ValueError):
                self.register("../escape")
        self.assertEqual(os.listdir(self.directory), [])

    def test_a_bare_docker_hub_name_matches_the_stored_registration(self):
        """The policy's stored spelling is what a comparison uses, so a re-provision is not a conflict."""
        with self.lock.acquire():
            created = self.register(image_repository="docker.io/library/nginx")
            self.assertEqual(self.registry.check("orders", **request(image_repository="nginx")), created)
            self.assertEqual(self.register(image_repository="nginx"), created)

    def test_check_reports_the_identical_registration_and_writes_nothing(self):
        with self.lock.acquire():
            self.assertIsNone(self.registry.check("orders", **request()))
            created = self.register()
            self.assertEqual(self.registry.check("orders", **request()), created)
        self.assertEqual(os.listdir(self.directory), ["orders.json"])

    def test_check_refuses_every_conflict_the_write_refuses(self):
        with self.lock.acquire():
            created = self.register()
            for change in ({"api_domain": "other.example.com"}, {"runtime": "native", "image_repository": None},
                           {"image_repository": "ghcr.io/example/other"}, {"deployment_uid": 2000},
                           {"deployment_user": "someone"}):
                with self.subTest(change=change), self.assertRaisesRegex(RegistryError, "differs"):
                    self.registry.check("orders", **request(**change))
            with self.assertRaisesRegex(RegistryError, "hostname"):
                self.registry.check("billing", **request("billing", api_domain="orders.example.com",
                                                         deployment_uid=1002, deployment_user="ship-billing"))
            with self.assertRaisesRegex(RegistryError, "account"):
                self.registry.check("billing", **request("billing"))
            with self.assertRaisesRegex(RegistryError, "account"):
                self.registry.check("billing", **request("billing", deployment_uid=1002))
        self.assertEqual(os.listdir(self.directory), ["orders.json"])
        self.assertEqual(self.registry.read("orders"), created)

    def test_check_requires_the_host_lock(self):
        with self.assertRaisesRegex(RegistryError, "lock"):
            self.registry.check("orders", **request())
        self.assertEqual(os.listdir(self.directory), [])

    def test_check_accepts_an_unknown_uid_only_for_an_unregistered_application(self):
        """A first provision has no account yet, so its UID is unknown; a registered one is never identical."""
        with self.lock.acquire():
            self.assertIsNone(self.registry.check("orders", **request(deployment_uid=None)))
            self.register()
            with self.assertRaisesRegex(RegistryError, "differs"):
                self.registry.check("orders", **request(deployment_uid=None))
        self.assertEqual(os.listdir(self.directory), ["orders.json"])

    def test_exhausted_port_range_fails_clearly(self):
        with self.lock.acquire():
            with self.assertRaisesRegex(RegistryError, "no loopback port"):
                self.registry.register("orders", probe=lambda port: False, **request())
        self.assertEqual(os.listdir(self.directory), [])

    def test_corrupt_or_unsafe_records_fail_closed(self):
        with self.lock.acquire():
            self.register()
            path = self.directory / "orders.json"
            unrelated = self.directory / "Not An App.json"
            cases = [
                ("corrupt json", lambda: path.write_text("{"), lambda: None),
                ("wrong app inside", lambda: path.write_text(json.dumps(record(application_id="billing"))),
                 lambda: None),
                ("name that cannot be an application", lambda: unrelated.write_text(json.dumps(record())),
                 unrelated.unlink),
                ("group writable", lambda: (path.write_text(json.dumps(record())), path.chmod(0o660)), lambda: None),
            ]
            for label, damage, cleanup in cases:
                with self.subTest(label):
                    damage()
                    try:
                        with self.assertRaises(StateError):
                            self.registry.list()
                        with self.assertRaises(StateError):
                            self.register("billing", deployment_uid=1002, deployment_user="ship-billing")
                    finally:
                        cleanup()

    def test_stray_files_are_skipped_and_named(self):
        with self.lock.acquire():
            self.register()
            for name in ("app.json~", ".app.json.swp", "stray.txt"):
                (self.directory / name).write_text("x")
            self.assertEqual([entry["application_id"] for entry in self.registry.list()], ["orders"])
            self.assertEqual(self.registry.stray, [".app.json.swp", "app.json~", "stray.txt"])
            # A stray file must not block a new registration either.
            self.register("billing", deployment_uid=1002, deployment_user="ship-billing")
        self.assertEqual(sorted(os.listdir(self.directory)),
                         [".app.json.swp", "app.json~", "billing.json", "orders.json", "stray.txt"])

    def test_symlinked_registration_is_not_followed(self):
        os.symlink("/etc/hosts", self.directory / "orders.json")
        with self.assertRaises(StateError):
            self.registry.read("orders")

    def test_untrusted_directory_is_refused(self):
        self.directory.chmod(0o777)
        self.addCleanup(self.directory.chmod, 0o700)
        with self.assertRaises(StateError):
            Registry(self.fd, self.lock)


class PortProbeTests(unittest.TestCase):
    def test_probe_sees_a_real_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(port_is_free(port))
        self.assertTrue(port_is_free(port))


if __name__ == "__main__":
    unittest.main()
