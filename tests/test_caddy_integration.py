"""Route transactions against a real Caddy process (set SHIPLORD_TEST_CADDY=1)."""

from __future__ import annotations

import http.client
import http.server
import os
import ssl
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from shiplord.registry import PORT_RANGE, port_is_free
from shiplord.release_state import HostLock
from shiplord.routing import RouteManager, RoutingError, SubprocessCaddy, probe_caddy, service_environment
from shiplord.system import CommandResult, HostSystem, run_command

ENABLED = os.environ.get("SHIPLORD_TEST_CADDY") == "1" and shutil.which("caddy")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def managed_port(skip: set[int]) -> int:
    return next(p for p in PORT_RANGE if p not in skip and port_is_free(p))


class AdminCaddy(SubprocessCaddy):
    """Real validate; reload through the admin API of the process this test started."""

    def __init__(self, caddyfile: Path, admin: str):
        super().__init__(caddy=shutil.which("caddy"))
        self.caddyfile, self.admin = caddyfile, admin

    def reload(self) -> None:
        self._run([self.caddy, "reload", "--config", str(self.caddyfile), "--adapter", "caddyfile",
                   "--address", self.admin], "reload")


class NoSystemd(HostSystem):
    """Real commands, except systemctl, which a container without systemd cannot run."""

    def run(self, argv, **kwargs):
        if argv[0].endswith("systemctl"):
            return CommandResult(1, "")
        return super().run(argv, **kwargs)


def upstream(body: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


@unittest.skipUnless(ENABLED, "set SHIPLORD_TEST_CADDY=1 with caddy installed")
class RealCaddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "caddy"
        (self.root / "ship").mkdir(parents=True)
        self.root.chmod(0o755)
        (self.root / "ship").chmod(0o755)
        self.https_port, self.http_port, self.admin_port = free_port(), free_port(), free_port()
        self.data_home = Path(self.temp.name).resolve() / "data"
        self.admin = f"127.0.0.1:{self.admin_port}"
        caddyfile = self.root / "Caddyfile"
        caddyfile.write_text(f"{{\n\tlocal_certs\n\tskip_install_trust\n\tauto_https disable_redirects\n"
                             f"\thttps_port {self.https_port}\n\thttp_port {self.http_port}\n\tadmin {self.admin}\n}}\n"
                             f"import {self.root}/ship/*.caddy\n")
        caddyfile.chmod(0o644)
        self.caddyfile = caddyfile
        self.process = subprocess.Popen([shutil.which("caddy"), "run", "--config", str(caddyfile), "--adapter",
                                         "caddyfile"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env=dict(os.environ, XDG_DATA_HOME=str(self.data_home)))
        self.addCleanup(self.stop_caddy)
        self.wait_for(lambda: socket.create_connection(("127.0.0.1", self.admin_port), 0.2).close())
        lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(lock_dir.cleanup)
        fd = os.open(lock_dir.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.lock = HostLock(fd)
        self.manager = RouteManager(self.root, self.lock, AdminCaddy(caddyfile, self.admin))
        self.servers = []

    def stop_caddy(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for server in getattr(self, "servers", []):
            server.shutdown()
            server.server_close()

    @staticmethod
    def wait_for(check, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return check()
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def assert_route_gone(self):
        """With its only site removed, Caddy stops serving; any upstream body would mean the route lingers."""
        try:
            body = self.fetch("orders.example.com")
        except OSError:
            return
        raise AssertionError(f"route still served {body!r}")

    def start_upstream(self, body: bytes, skip=()) -> int:
        port = managed_port(set(skip))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), upstream(body))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return port

    def fetch(self, host: str) -> bytes:
        """HTTPS request to the local Caddy, verifying its certificate against Caddy's own local CA."""
        root = self.data_home / "caddy" / "pki" / "authorities" / "local" / "root.crt"
        context = ssl.create_default_context(cafile=str(root))  # raises until Caddy has issued its CA

        class Connection(http.client.HTTPSConnection):
            def connect(inner):
                sock = socket.create_connection(("127.0.0.1", self.https_port), timeout=5)
                inner.sock = context.wrap_socket(sock, server_hostname=host)

        connection = Connection(host, context=context, timeout=5)
        try:
            connection.request("GET", "/")
            return connection.getresponse().read()
        finally:
            connection.close()

    @staticmethod
    def registration(port, domain="orders.example.com"):
        return {"schema_version": 1, "application_id": "orders", "deployment_uid": 1001,
                "deployment_user": "ship-orders", "runtime": "docker", "api_domain": domain,
                "host_port": port, "image_repository": "ghcr.io/example/orders", "max_memory_mb": 4096}

    def test_the_caddy_probe_validates_a_minimal_configuration(self):
        probe_caddy(SubprocessCaddy(caddy=shutil.which("caddy")))

    def test_a_missing_caddy_binary_is_reported_as_unavailable(self):
        absent = str(self.root / "absent")
        with self.assertRaises(RoutingError) as caught:
            probe_caddy(SubprocessCaddy(caddy=absent))
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertIn(absent, str(caught.exception))

    def test_apply_update_and_remove_change_real_traffic(self):
        first = self.start_upstream(b"first")
        second = self.start_upstream(b"second", skip={first})
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(self.registration(first)).changed)
            self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"first")
            self.assertTrue(self.manager.apply(self.registration(second)).changed)
            self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"second")
            self.assertFalse(self.manager.apply(self.registration(second)).changed)
            self.assertEqual(self.manager.reconcile("orders", self.registration(second)).changed, False)
            self.assertTrue(self.manager.remove("orders").changed)
            self.wait_for(self.assert_route_gone)

    def test_invalid_existing_route_is_reported_as_preexisting_and_nothing_changes(self):
        (self.root / "ship" / "broken.caddy").write_text("broken.example.com {\n\tnot_a_directive\n}\n")
        (self.root / "ship" / "broken.caddy").chmod(0o644)  # independent of the umask; group-writable files are refused
        before = {p.name: p.read_bytes() for p in (self.root / "ship").iterdir()}
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "already invalid"):
            self.manager.apply(self.registration(managed_port(set())))
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "ship").iterdir()}, before)

    def test_tls_style_or_scheme_duplicates_that_caddy_misses_are_still_refused(self):
        with open(self.caddyfile, "a") as file:
            file.write("http://orders.example.com:9999 {\n\trespond ok\n}\n")
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.apply(self.registration(managed_port(set())))
        self.assertEqual(caught.exception.kind, "conflict")
        # Caddy itself accepts this configuration, so only the scan prevents the takeover.
        self.assertEqual(run_command([shutil.which("caddy"), "validate", "--config", str(self.caddyfile),
                                      "--adapter", "caddyfile"], env=dict(os.environ, HOME=str(self.data_home))).code, 0)

    def test_a_hostname_in_an_upstream_or_header_value_is_not_a_conflict(self):
        # Caddy accepts this configuration, so the hostname really is only a directive value.
        with open(self.caddyfile, "a") as file:
            file.write("legacy.example.net {\n\treverse_proxy https://orders.example.com\n"
                       "\theader Link https://orders.example.com/x\n}\n")
        self.assertEqual(run_command([shutil.which("caddy"), "validate", "--config", str(self.caddyfile),
                                      "--adapter", "caddyfile"],
                                     env=dict(os.environ, HOME=str(self.data_home))).code, 0)
        port = self.start_upstream(b"proxied")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(self.registration(port)).changed)
        self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"proxied")

    def test_reload_failure_with_caddy_stopped_restores_the_file_and_flags_intervention(self):
        port = managed_port(set())
        self.process.terminate()
        self.process.wait(5)
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.apply(self.registration(port))
        self.assertEqual(caught.exception.kind, "reload")
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(os.listdir(self.root / "ship"), [])

    def test_a_real_transaction_leaves_no_temporary_files(self):
        with self.lock.acquire():
            self.manager.apply(self.registration(managed_port(set())))
        self.assertEqual([p.name for p in self.root.rglob(".*")], [])

    def test_a_hardened_caddyfile_stays_hardened_and_usable_after_a_rewrite(self):
        # The import rewrite is the only transaction that replaces the main Caddyfile, and that file can hold a token.
        import_line = f"import {self.root}/ship/*.caddy\n"
        text = self.caddyfile.read_text()
        self.assertTrue(text.endswith(import_line))
        self.caddyfile.write_text(text[:-len(import_line)])
        self.caddyfile.chmod(0o640)
        port = self.start_upstream(b"hardened")
        with self.lock.acquire():
            self.assertTrue(self.manager.ensure_import().changed)
            self.assertEqual(stat.S_IMODE(self.caddyfile.stat().st_mode), 0o640)
            self.assertTrue(self.manager.apply(self.registration(port)).changed)
        self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"hardened")
        self.assertEqual(stat.S_IMODE(self.caddyfile.stat().st_mode), 0o640)

    def test_a_crash_between_the_link_and_the_publish_is_reconciled_and_serves(self):
        import_line = f"import {self.root}/ship/*.caddy\n"
        text = self.caddyfile.read_text()
        self.caddyfile.write_text(text[:-len(import_line)])
        os.link(self.caddyfile, self.root / ".prior-Caddyfile")
        port = self.start_upstream(b"recovered")
        with self.lock.acquire():
            self.assertTrue(self.manager.ensure_import().changed)
            self.assertTrue(self.manager.apply(self.registration(port)).changed)
        self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"recovered")
        self.assertEqual([p.name for p in self.root.rglob(".*")], [])

    def test_a_crash_after_the_publish_is_reconciled_and_serves(self):
        import_line = f"import {self.root}/ship/*.caddy\n"
        text = self.caddyfile.read_text()
        prior = self.root / ".prior-Caddyfile"
        prior.write_text(text[:-len(import_line)])
        prior.chmod(0o644)
        port = self.start_upstream(b"still served")
        with self.lock.acquire():
            self.assertFalse(self.manager.ensure_import().changed)
            self.assertTrue(self.manager.apply(self.registration(port)).changed)
        self.assertEqual(self.wait_for(lambda: self.fetch("orders.example.com")), b"still served")
        self.assertEqual([p.name for p in self.root.rglob(".*")], [])

    def test_a_caddyfile_that_reads_a_service_variable_validates(self):
        # A container without systemd cannot report the unit, so this exercises the packaged-defaults path: the
        # variable the Caddyfile reads has to reach `caddy validate`, or the file is judged broken for no reason.
        defaults = Path(self.temp.name).resolve() / "default-caddy"
        defaults.write_text(f'SHIPLORD_TEST_SITE="localhost:{self.http_port}"\n'
                            'SHIPLORD_TEST_UPSTREAM="127.0.0.1:9999"\n')
        caddyfile = Path(self.temp.name).resolve() / "variable.Caddyfile"
        caddyfile.write_text("{\n\tlocal_certs\n\tskip_install_trust\n}\n"
                             '{$SHIPLORD_TEST_SITE} {\n\treverse_proxy {$SHIPLORD_TEST_UPSTREAM}\n}\n')
        caddyfile.chmod(0o644)
        with_variable = SubprocessCaddy(shutil.which("caddy"),
                                        service_environment=lambda: service_environment(NoSystemd(), (defaults,)))
        with_variable.validate(caddyfile)  # must not raise: the service has both variables
        with self.assertRaises(RoutingError) as caught:
            SubprocessCaddy(shutil.which("caddy"), service_environment=lambda: {}).validate(caddyfile)
        self.assertEqual(caught.exception.kind, "validation")


if __name__ == "__main__":
    unittest.main()
