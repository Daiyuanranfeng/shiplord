"""The deploy engine against a real Docker daemon and a local registry (set SHIPLORD_TEST_DOCKER=1).

Only resources named for a unique test application are created, and all are removed afterwards.
Fixture images imitate the template: /ready, managed environment variables, an artifacts volume,
an unhealthy release, and a crashing release.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeCaddy  # noqa: E402
from shiplord import registries  # noqa: E402
from shiplord.backup import BackupManager, read_manifest  # noqa: E402
from shiplord.deploy import Deployer, identity  # noqa: E402
from shiplord.framing import read_frame, write_frame  # noqa: E402
from shiplord.layout import Layout  # noqa: E402
from shiplord.probes import Probes  # noqa: E402
from shiplord.protocol import PROTOCOL_VERSION  # noqa: E402
from shiplord.privileged_host import serve  # noqa: E402
from shiplord.registry import PORT_RANGE, Registry, port_is_free, validate_registration  # noqa: E402
from shiplord.release_state import HostLock, ReleaseState  # noqa: E402
from shiplord.routing import RouteManager  # noqa: E402
from shiplord.routing import RoutingError  # noqa: E402
from shiplord.retire import make_retirer  # noqa: E402
from shiplord.runtime import DockerRuntime, release_id  # noqa: E402
from shiplord.services import HostServices  # noqa: E402
from shiplord.system import HostSystem  # noqa: E402
from shiplord.worker import run_job  # noqa: E402

ENABLED = os.environ.get("SHIPLORD_TEST_DOCKER") == "1" and shutil.which("docker")
REGISTRY_PORT = 5000
REPOSITORY = f"localhost:{REGISTRY_PORT}/shiplord/fixture"
SERVER = '''
import http.server, json, os, signal, sys, time

if os.environ.get("MODE") == "crash":
    print("fixture crashing on purpose", flush=True)
    sys.exit(1)
def drain(*_):  # a well-behaved PID 1: finish the task in flight, flush a row and a file, then exit
    time.sleep(1)
    import sqlite3
    database = sqlite3.connect(os.path.join(DATA, "app.db"))
    database.execute("CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, version TEXT)")
    database.execute("INSERT INTO rows (version) VALUES ('drained')")
    database.commit()
    with open(os.path.join(DATA, "drained"), "w") as file:
        file.write("done")
    os._exit(0)

if os.environ.get("MODE") == "graceful":
    signal.signal(signal.SIGTERM, drain)
elif os.environ.get("MODE") == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif os.environ.get("MODE") in ("healthy", "unhealthy"):
    # PID 1 must explicitly handle TERM; only the ignore fixture should refuse shutdown.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
DATA = os.environ.get("DATA_DIR", "/app/src/.adk")  # the legacy path unless a test points the app elsewhere
ARTIFACTS = DATA

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = 200, b"ok"
        if self.path == "/ready":
            status = 503 if os.environ.get("MODE") == "unhealthy" else 200
        elif self.path == "/version":
            body = os.environ["VERSION"].encode()
        elif self.path == "/whoami":
            body = str(os.getuid()).encode()
        elif self.path == "/env":
            keys = ("ALLOW_ORIGINS", "SERVE_WEB_INTERFACE", "RELOAD_AGENTS", "AGENT_NAME", "HOST", "PORT", "AGENT_DIR", "CUSTOM_TOKEN", "DATA_DIR")
            body = json.dumps({k: os.environ.get(k) for k in keys}).encode()
        elif self.path == "/drained":
            path = os.path.join(DATA, "drained")
            body = open(path).read().encode() if os.path.exists(path) else b"missing"
        elif self.path == "/sqlite":
            import sqlite3
            database = sqlite3.connect(os.path.join(DATA, "app.db"))
            database.execute("CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, version TEXT)")
            database.execute("INSERT INTO rows (version) VALUES (?)", (os.environ["VERSION"],))
            database.commit()
            body = json.dumps([r[0] for r in database.execute("SELECT version FROM rows ORDER BY id")]).encode()
            database.close()
        elif self.path == "/counter":
            path = os.path.join(ARTIFACTS, "counter")
            count = int(open(path).read()) + 1 if os.path.exists(path) else 1
            open(path, "w").write(str(count))
            body = str(count).encode()
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

print("fixture started version=" + os.environ["VERSION"], flush=True)
http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), Handler).serve_forever()
'''
DOCKERFILE = '''FROM python:3.12-slim
RUN useradd -u 1000 -m app && mkdir -p /app/src/.adk /data && chown -R app:app /app /data
USER app
ARG VERSION
ARG MODE=healthy
ARG REVISION
ENV VERSION=$VERSION MODE=$MODE AGENT_NAME=image-default AGENT_DIR=/image/agents
LABEL org.opencontainers.image.revision=$REVISION
COPY server.py /app/server.py
CMD ["python", "/app/server.py"]
'''
IMAGE_PATTERN = re.compile(rf"localhost:{REGISTRY_PORT}/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+@sha256:[0-9a-f]{{64}}\Z")
REPOSITORY_PATTERN = re.compile(rf"localhost:{REGISTRY_PORT}/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+\Z")


def docker(*argv, check=True, timeout=300):
    result = subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(argv)} failed: {result.stdout} {result.stderr}")
    return result


@unittest.skipUnless(ENABLED, "set SHIPLORD_TEST_DOCKER=1 with a reachable Docker daemon")
class RealDockerDeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docker("info")
        cls.app = "fixture-" + secrets.token_hex(3)
        # The fixture registry is a localhost port, which the shipped policy does not accept. Only the host
        # rule is replaced; the digest rule and every other check still run.
        original_repository, original_pinned = registries.validate_repository, registries.validate_pinned

        def local_repository(value):
            if isinstance(value, str) and REPOSITORY_PATTERN.fullmatch(value):
                return value
            return original_repository(value)

        def local_pinned(value):
            if isinstance(value, str) and IMAGE_PATTERN.fullmatch(value):
                return value
            return original_pinned(value)

        for name, replacement in (("validate_repository", local_repository), ("validate_pinned", local_pinned)):
            patcher = patch.object(registries, name, replacement)
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base = Path(cls.temp.name).resolve()
        cls.start_registry()
        cls.images = cls.build_images()
        cls.addClassCleanup(cls.remove_app_resources)

    @classmethod
    def start_registry(cls):
        name = "shiplord-test-registry-" + secrets.token_hex(3)
        docker("run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{REGISTRY_PORT}:5000", "registry:2")
        cls.addClassCleanup(lambda: docker("rm", "-f", name, check=False))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{REGISTRY_PORT}/v2/", timeout=2)
                return
            except OSError:
                time.sleep(0.3)
        raise AssertionError("the local registry did not start")

    @classmethod
    def build_images(cls) -> dict[str, dict]:
        context = cls.base / "fixture"
        context.mkdir()
        (context / "server.py").write_text(SERVER)
        (context / "Dockerfile").write_text(DOCKERFILE)
        images = {}
        for version, mode, commit in (("v1", "healthy", "a"), ("v2", "healthy", "b"), ("v3", "unhealthy", "c"),
                                      ("v4", "crash", "d"), ("v5", "healthy", "e"),
                                      ("v6", "graceful", "f"), ("v7", "ignore", "7"), ("v8", "healthy", "8"),
                                      ("v6-volume", "healthy", "9")):
            tag = f"{REPOSITORY}:{version}"
            dockerfile = context / "Dockerfile"
            if version == "v6-volume":
                dockerfile = context / "Dockerfile.volume"
                dockerfile.write_text(DOCKERFILE.replace("USER app", "VOLUME /scratch\nUSER app"))
            docker("build", "-q", "--build-arg", f"VERSION={version}", "--build-arg", f"MODE={mode}", "--build-arg",
                   f"REVISION={commit * 40}", "-f", str(dockerfile), "-t", tag, str(context))
            pushed = docker("push", tag).stdout
            digest = re.search(r"digest: (sha256:[0-9a-f]{64})", pushed).group(1)
            docker("rmi", tag, check=False)
            docker("rmi", f"{REPOSITORY}@{digest}", check=False)  # force every deploy to pull
            images[version] = {"image": f"{REPOSITORY}@{digest}", "commit": commit * 40}
        return images

    @classmethod
    def remove_app_resources(cls):
        ids = docker("ps", "-aq", "--filter", f"label=shiplord.app={cls.app}", check=False).stdout.split()
        image_volumes = set()
        if ids:
            for container in json.loads(docker("inspect", *ids, check=False).stdout or "[]"):
                image_volumes.update(mount["Name"] for mount in container.get("Mounts", [])
                                     if mount.get("Destination") == "/scratch" and mount.get("Type") == "volume"
                                     and mount.get("Name"))
            docker("rm", "-f", *ids, check=False)
        docker("volume", "rm", "-f", f"shiplord-{cls.app}-artifacts", check=False)
        for name in image_volumes:
            docker("volume", "rm", name, check=False)
        for image in cls.images.values():
            docker("rmi", image["image"], check=False)

    def setUp(self):
        self.layout = Layout(self.base / "host")
        for directory, mode in ((self.layout.var, 0o755), (self.layout.environments / self.app, 0o700),
                                (self.layout.app_state / self.app, 0o700), (self.layout.caddy_root / "ship", 0o755)):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(mode)
        self.layout.caddy_root.chmod(0o755)
        main = self.layout.caddy_root / "Caddyfile"
        main.write_text(f"import {self.layout.caddy_root}/ship/*.caddy\n")
        main.chmod(0o644)
        self.var_fd = os.open(self.layout.var, os.O_RDONLY | os.O_DIRECTORY)
        self.state_fd = os.open(self.layout.app_state / self.app, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.var_fd)
        self.addCleanup(os.close, self.state_fd)
        self.lock = HostLock(self.var_fd)
        self.state = ReleaseState(self.app, self.state_fd, self.lock)
        with self.lock.acquire():
            if not (self.layout.app_state / self.app / "state.json").exists():
                self.state.initialize()
        self.port = getattr(self.__class__, "port", None) or next(p for p in PORT_RANGE if port_is_free(p))
        self.__class__.port = self.port
        self.registration = validate_registration({
            "schema_version": 1, "application_id": self.app, "deployment_uid": 1001, "deployment_user": "ship-fixture",
            "runtime": "docker", "api_domain": "fixture.example.test", "host_port": self.port,
            "image_repository": REPOSITORY, "max_memory_mb": 4096})
        self.runtime = DockerRuntime(HostSystem(), self.layout)
        self.deployer = Deployer(registration=self.registration, state=self.state,
                                 routes=RouteManager(self.layout.caddy_root, self.lock, FakeCaddy()),
                                 runtime=self.runtime, probes=Probes(admin_port=1))

    # helpers ---------------------------------------------------------------
    def configuration(self, **resources):
        return {"schema_version": 1, "name": self.app, "server": "production", "template": "google-adk",
                "runtime": "docker", "http": {"api_domain": "fixture.example.test",
                                              "frontend_origins": ["https://app.example.test"], "container_port": 8080,
                                              "readiness_path": "/ready"},
                "resources": {"memory_mb": 512}, "deploy": {"readiness_timeout_seconds": 6, "retain_successful_releases": 2}}

    def request(self, request_id, version, sequence, **overrides):
        image = self.images[version]
        value = {"request_id": request_id, "commit": image["commit"], "sequence": sequence,
                 "artifact": {"kind": "docker", "image": image["image"]}, "configuration": self.configuration()}
        value.update(overrides)
        return value

    def deploy(self, *args, **kwargs):
        with self.lock.acquire():
            return self.deployer.deploy(self.request(*args, **kwargs))

    def get(self, path):
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as response:
                    return response.read().decode()
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def containers(self):
        return docker("ps", "-aq", "--filter", f"label=shiplord.app={self.app}").stdout.split()

    def inspect(self):
        (container,) = self.containers()
        return json.loads(docker("inspect", container).stdout)[0]

    def assertOutcome(self, outcome, status):
        self.assertEqual(outcome.status, status, json.dumps(outcome.detail, indent=1)[:1500])

    # the lifecycle, in order: each step builds on the previous one -------------
    def test_lifecycle(self):
        first = self.deploy("req-1", "v1", 1000)
        self.assertOutcome(first, "succeeded")
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(first.detail["probes"]["local_ready"]["ready"], True)
        self.assertEqual(first.detail["image"]["revision_label"], "a" * 40)

        details = self.inspect()
        host = details["HostConfig"]
        self.assertEqual((host["Memory"], host["MemorySwap"]), (512 * 1024 * 1024, 512 * 1024 * 1024))
        self.assertEqual(host["RestartPolicy"]["Name"], "unless-stopped")
        self.assertEqual(host["PortBindings"]["8080/tcp"], [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}])
        self.assertEqual(host["LogConfig"]["Config"], {"max-file": "3", "max-size": "10m"})
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertIn("no-new-privileges", " ".join(host["SecurityOpt"] or []))
        self.assertEqual(details["Config"]["Labels"]["shiplord.app"], self.app)
        self.assertEqual([m["Name"] for m in details["Mounts"]], [f"shiplord-{self.app}-artifacts"])
        self.assertEqual(self.get("/whoami"), "1000")
        environment = json.loads(self.get("/env"))
        self.assertEqual(environment["ALLOW_ORIGINS"], '["https://app.example.test"]')
        self.assertEqual((environment["SERVE_WEB_INTERFACE"], environment["RELOAD_AGENTS"], environment["HOST"],
                          environment["AGENT_NAME"]), ("false", "false", "0.0.0.0", self.app))
        self.assertEqual(self.get("/counter"), "1")

        second = self.deploy("req-2", "v2", 1001)
        self.assertOutcome(second, "succeeded")
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.get("/counter"), "2")  # the artifacts volume survived the replacement
        self.assertEqual(len(self.containers()), 1)

        unhealthy = self.deploy("req-3", "v3", 1002)
        self.assertOutcome(unhealthy, "rolled_back")
        self.assertEqual(unhealthy.exit_code, 5)
        self.assertEqual(unhealthy.detail["kind"], "timeout")
        self.assertIn("fixture started version=v3", unhealthy.detail["candidate_logs"]["output"])
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.get("/counter"), "3")

        crashing = self.deploy("req-4", "v4", 1003)
        self.assertOutcome(crashing, "rolled_back")
        self.assertEqual(crashing.detail["kind"], "exited")
        self.assertIn("fixture crashing on purpose", crashing.detail["candidate_logs"]["output"])
        self.assertEqual(crashing.detail["candidate_logs"]["tail"], 100)
        self.assertEqual(self.get("/version"), "v2")
        self.assertIn("fixture started version=v2",
                      self.runtime.logs(self.registration, self.state.read_state()["current"], 50))  # restored release
        self.assertEqual(self.state.read_state()["current"]["commit"], "b" * 40)
        self.assertIsNone(self.state.read_journal())

        for failed in ("v3", "v4"):  # a candidate that failed leaves no image behind
            self.assertNotEqual(docker("image", "inspect", self.images[failed]["image"], check=False).returncode, 0, failed)

        rolled_back = self.runtime_rollback("req-5")
        self.assertOutcome(rolled_back, "succeeded")
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(self.state.read_state()["previous"]["commit"], "b" * 40)

        # a worker killed mid-transaction: the journal says so, recovery restores the committed release
        with self.lock.acquire():
            self.state.begin("req-6", self.state.read_state()["previous"] | {"sequence": 2000})
            self.state.advance("runtime-changing")
            docker("rm", "-f", *self.containers())
        self.assertEqual(self.deploy("req-7", "v5", 2001).detail["error"], "recovery_required")
        with self.lock.acquire():
            recovered = self.deployer.recover()
        self.assertOutcome(recovered, "recovered")
        self.assertEqual(self.get("/version"), "v1")
        self.assertIsNone(self.state.read_journal())

        logs = self.runtime.logs(self.registration, self.state.read_state()["current"], 50, timestamps=True)
        self.assertIn("fixture started version=v1", logs)
        self.assertRegex(logs, r"(?m)^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")  # timestamps are real, not invented

        # A stopped container of another release is never read, even when docker lists it first.
        stale = docker("run", "-d", "--label", f"shiplord.app={self.app}", "--label", "shiplord.release=000000000000",
                       self.images["v1"]["image"], "sh", "-c", "echo stale-release").stdout.strip()
        try:
            self.assertNotIn("stale-release", self.runtime.logs(self.registration, self.state.read_state()["current"], 50))
        finally:
            docker("rm", "-f", stale, check=False)

        # Following is bounded by its own deadline, and it starts from --since.
        chunks = []
        self.assertEqual(self.runtime.follow_logs(self.registration, self.state.read_state()["current"], tail=10,
                                                  since="1h", timestamps=True, duration=2, emit=chunks.append),
                         "timeout")
        self.assertIn("fixture started version=v1", "".join(chunks))

        # preparation failures leave the running container untouched
        before = self.containers()
        wrong_label = self.deploy("req-8", "v5", 2002, commit="f" * 40)
        self.assertOutcome(wrong_label, "failed")
        self.assertEqual(wrong_label.detail["error"], "prepare_failed")
        self.assertIn("different commit", wrong_label.detail["message"])
        missing = self.deploy("req-9", "v5", 2003, artifact={"kind": "docker", "image": f"{REPOSITORY}@sha256:" + "e" * 64})
        self.assertOutcome(missing, "failed")
        self.assertEqual(missing.detail["error"], "prepare_failed")
        self.assertEqual(self.containers(), before)
        self.assertEqual(self.get("/version"), "v1")

        # a custom environment file is passed through, but may never override managed values
        env_file = self.layout.environments / self.app / "app.env"
        env_file.write_text("CUSTOM_TOKEN=s3cret\n")
        env_file.chmod(0o600)
        with_env = self.deploy("req-10", "v5", 2004)
        self.assertOutcome(with_env, "succeeded")
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "s3cret")
        self.assertEqual(self.get("/version"), "v5")
        env_file.write_text("PORT=1\n")
        broken = self.deploy("req-11", "v1", 2005)  # v1 is still retained, so nothing is re-pulled that retention removed
        self.assertOutcome(broken, "rolled_back")
        self.assertIn("environment file is unusable", json.dumps(broken.detail))
        env_file.write_text("CUSTOM_TOKEN=s3cret\n")
        self.assertEqual(self.get("/version"), "v5")

        # retention: v5 deployed over v1 while v2 was previous, so v2's image left the window and was removed
        state = self.state.read_state()
        self.assertEqual((state["current"]["commit"], state["previous"]["commit"]), ("e" * 40, "a" * 40))
        self.assertNotEqual(docker("image", "inspect", self.images["v2"]["image"], check=False).returncode, 0)
        self.assertEqual(docker("image", "inspect", self.images["v1"]["image"], check=False).returncode, 0)
        self.assertEqual(docker("image", "inspect", self.images["v5"]["image"], check=False).returncode, 0)
        self.assertEqual(len(self.containers()), 1)

        status = self.deployer.status()
        self.assertEqual((status["running"], status["current"], status["unfinished_transaction"]),
                         (True, identity(state["current"]), None))
        self.assertEqual(release_id(state["current"]), self.inspect()["Config"]["Labels"]["shiplord.release"])

    def stored_login(self):
        """A stored docker login for this application whose credential helper records that docker used it.

        The fixture registry is anonymous, so the fetch cannot fail without the login; a helper in the application's
        own config directory is what proves the fetch was given that directory. It returns no credentials, because a
        throwaway password would prove nothing more and would put a value in the test.
        """
        login = self.layout.registry_logins / self.app
        login.mkdir(parents=True, exist_ok=True)
        login.chmod(0o700)
        record = self.base / "login-record"
        name = f"shiplord-fixture-{self.app}"
        helper = Path("/usr/local/bin") / f"docker-credential-{name}"
        helper.write_text('#!/bin/sh\nprintf "%s %s\\n" "$1" "$(cat)" >> ' + str(record) + '\n'
                          'printf \'{"ServerURL":"localhost:' + str(REGISTRY_PORT) + '","Username":"","Secret":""}\'\n')
        helper.chmod(0o755)
        self.addCleanup(helper.unlink, missing_ok=True)
        self.addCleanup(shutil.rmtree, login, ignore_errors=True)
        (login / "config.json").write_text(json.dumps({"credHelpers": {f"localhost:{REGISTRY_PORT}": name}}))
        (login / "config.json").chmod(0o600)
        return record

    def test_pruned_image_is_fetched_with_the_stored_login_when_the_release_is_started(self):
        """`docker image prune -a` is routine host maintenance: the committed release must still come back."""
        self.assertOutcome(self.deploy("req-i1", "v1", 2500), "succeeded")
        self.assertOutcome(self.deploy("req-i2", "v2", 2501), "succeeded")
        self.assertEqual(self.get("/version"), "v2")

        docker("rm", "-f", *self.containers())  # the application is down, and host maintenance pruned its image
        docker("rmi", self.images["v2"]["image"])
        self.assertNotEqual(docker("image", "inspect", self.images["v2"]["image"], check=False).returncode, 0)
        record = self.stored_login()  # only from here on: the fetches above must not be credited to it

        # A worker interrupted mid-transaction leaves a journal; recovery starts the committed release again.
        with self.lock.acquire():
            self.state.begin("req-i3", self.state.read_state()["current"] | {"sequence": 2502})
            self.state.advance("runtime-changing")
        with self.lock.acquire():
            recovered = self.deployer.recover()
        self.assertOutcome(recovered, "recovered")
        self.assertEqual(recovered.detail["target"]["commit"], "b" * 40)
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(docker("image", "inspect", self.images["v2"]["image"], check=False).returncode, 0,
                         "the start did not fetch the pruned image")
        self.assertEqual(record.read_text().split(), ["get", f"localhost:{REGISTRY_PORT}"],
                         "the fetch did not use this application's stored login")
        self.assertIsNone(self.state.read_journal())

    def test_redeploying_a_release_that_is_down_keeps_the_real_rollback_target(self):
        """A CI re-run deploys the same release again with a new request ID; the rollback target must survive."""
        self.assertOutcome(self.deploy("req-r1", "v1", 3000), "succeeded")
        self.assertOutcome(self.deploy("req-r2", "v2", 3001), "succeeded")
        self.assertEqual(self.get("/version"), "v2")
        self.assertEqual(self.state.read_state()["previous"]["commit"], "a" * 40)

        docker("stop", *self.containers())  # the release is down, so the unchanged shortcut cannot fire
        again = self.deploy("req-r3", "v2", 3001)
        self.assertOutcome(again, "succeeded")
        self.assertEqual(self.get("/version"), "v2")
        state = self.state.read_state()
        self.assertEqual((state["current"]["commit"], state["previous"]["commit"]), ("b" * 40, "a" * 40))
        self.assertEqual(again.detail["previous"]["commit"], "a" * 40)
        self.assertEqual(docker("image", "inspect", self.images["v1"]["image"], check=False).returncode, 0,
                         "the redeploy removed the previous release's image")

        rolled_back = self.runtime_rollback("req-r4")
        self.assertOutcome(rolled_back, "succeeded")
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(self.state.read_state()["current"]["commit"], "a" * 40)

    def storage_configuration(self, path):
        value = self.configuration()
        if path is not None:
            value["storage"] = {"container_path": path}
        return value

    def set_data_dir(self, path):
        env_file = self.layout.environments / self.app / "app.env"
        env_file.write_text(f"DATA_DIR={path}\n")
        env_file.chmod(0o600)

    def test_storage_path_changes_keep_one_volume_and_its_files(self):
        """The same volume is mounted at a new path: SQLite rows and files survive, rollback restores the old path."""
        volume = f"shiplord-{self.app}-artifacts"
        self.set_data_dir("/app/src/.adk")
        self.assertOutcome(self.deploy("req-s1", "v1", 5000), "succeeded")
        self.assertEqual(json.loads(self.get("/sqlite")), ["v1"])
        counter = int(self.get("/counter"))

        self.set_data_dir("/data")
        moved = self.deploy("req-s2", "v1", 5000, configuration=self.storage_configuration("/data"))  # same image, commit, sequence
        self.assertOutcome(moved, "succeeded")
        mounts = [(m["Name"], m["Destination"]) for m in self.inspect()["Mounts"]]
        self.assertEqual(mounts, [(volume, "/data")])
        self.assertEqual(self.get("/counter"), str(counter + 1))  # relative file names are unchanged; nothing moved
        self.assertEqual(json.loads(self.get("/sqlite")), ["v1", "v1"])
        self.assertEqual(docker("volume", "ls", "-q", "--filter", f"name=shiplord-{self.app}").stdout.split(), [volume])

        # a failing candidate with another path rolls back to a container that really has the previous path
        failed = self.deploy("req-s3", "v3", 5001, configuration=self.storage_configuration("/var/lib/other"))
        self.assertOutcome(failed, "rolled_back")
        self.assertEqual([(m["Name"], m["Destination"]) for m in self.inspect()["Mounts"]], [(volume, "/data")])
        self.assertEqual(self.get("/counter"), str(counter + 2))
        self.assertTrue(self.runtime.is_running(self.registration, self.state.read_state()["current"]))

        # back to the legacy path: still the same data, and the retained release records are not rewritten
        self.set_data_dir("/app/src/.adk")
        self.assertOutcome(self.deploy("req-s4", "v1", 5000, configuration=self.configuration()), "succeeded")
        self.assertEqual([(m["Name"], m["Destination"]) for m in self.inspect()["Mounts"]], [(volume, "/app/src/.adk")])
        self.assertEqual(json.loads(self.get("/sqlite")), ["v1", "v1", "v1"])
        self.assertNotIn("storage", self.state.read_state()["current"]["configuration"])

    def policy_configuration(self, timeout, action):
        value = self.configuration()
        value["deploy"].update(shutdown_timeout_seconds=timeout, shutdown_timeout_action=action)
        return value

    def fresh_container_slate(self):
        """Shutdown tests deliberately leave stubborn containers behind; each starts with none (the outgoing policy governs)."""
        if self.containers():
            docker("rm", "-f", *self.containers())

    def test_zz_graceful_replacement_with_a_signal_aware_process(self):
        self.fresh_container_slate()
        self.set_data_dir("/app/src/.adk")
        policy = self.policy_configuration(8, "fail")
        self.assertOutcome(self.deploy("req-g1", "v6", 8200, configuration=policy), "succeeded")
        self.assertEqual(self.get("/version"), "v6")
        replaced = self.deploy("req-g2", "v8", 8201, configuration=policy)
        self.assertOutcome(replaced, "succeeded")
        (report,) = replaced.detail["shutdown"]
        self.assertFalse(report["forced"])
        self.assertEqual(report["stages"][:2], ["stop_requested", "signalled"])
        self.assertEqual(self.get("/drained"), "done")  # flushed before the candidate started
        self.assertIn("drained", json.loads(self.get("/sqlite")))
        self.assertEqual(len(self.containers()), 1)
        labels = self.inspect()["Config"]["Labels"]
        self.assertEqual((labels["shiplord.shutdown_timeout"], labels["shiplord.shutdown_action"]), ("8", "fail"))

    def test_zz_force_kills_a_process_that_ignores_the_signal(self):
        self.fresh_container_slate()
        policy = self.policy_configuration(2, "force")
        self.assertOutcome(self.deploy("req-f1", "v7", 8100, configuration=policy), "succeeded")
        started = time.monotonic()
        replaced = self.deploy("req-f2", "v8", 8101, configuration=policy)
        self.assertOutcome(replaced, "succeeded")
        self.assertTrue(replaced.detail["shutdown"][0]["forced"])
        self.assertGreaterEqual(time.monotonic() - started, 2)
        self.assertEqual(self.get("/version"), "v8")

    def test_zz_fail_force_stop_unblocks_deploy_and_retirement(self):
        self.fresh_container_slate()
        self.set_data_dir("/app/src/.adk")
        policy = self.policy_configuration(2, "fail")
        self.assertOutcome(self.deploy("req-x1", "v7", 8000, configuration=policy), "succeeded")
        (original,) = self.containers()
        blocked = self.deploy("req-x2", "v8", 8001, configuration=policy)
        self.assertOutcome(blocked, "intervention_required")
        self.assertEqual((blocked.exit_code, blocked.detail["kind"]), (6, "shutdown_timeout"))
        self.assertIn("--force-stop", blocked.detail["recovery"])
        self.assertEqual(self.containers(), [original])  # not killed, not removed, no candidate
        self.assertEqual(docker("inspect", "--format", "{{.State.Running}} {{.HostConfig.RestartPolicy.Name}}", original).stdout.strip(),
                         "true no")
        self.assertIsNotNone(self.state.read_journal())
        with self.lock.acquire():
            self.assertOutcome(self.deployer.recover(), "intervention_required")  # still draining: nothing is cleared
        self.assertEqual(docker("inspect", "--format", "{{.State.Running}}", original).stdout.strip(), "true")

        with self.lock.acquire():
            forced = self.deployer.recover(force_stop=True)
        self.assertOutcome(forced, "recovered")
        self.assertFalse(forced.detail["running"])
        self.assertTrue(forced.detail["forced_stop"]["forced"])
        self.assertEqual(forced.detail["forced_stop"]["signal"], "SIGKILL")
        self.assertEqual(forced.detail["forced_stop"]["policy"], {"timeout_seconds": 2, "action": "fail"})
        self.assertIn("forced", forced.detail["forced_stop"]["stages"])
        self.assertEqual(self.containers(), [])
        self.assertIsNone(self.state.read_journal())
        self.assertFalse((self.layout.caddy_root / "ship" / f"{self.app}.caddy").exists())

        following = self.deploy("req-x3", "v8", 8002, configuration=policy)
        self.assertOutcome(following, "succeeded")
        self.assertEqual(self.get("/version"), "v8")

        retire = make_retirer(self.layout, HostSystem(), self.lock, FakeCaddy(),
                              runtime_for=lambda registration: self.runtime)
        with self.lock.acquire():
            removed = retire(self.registration, self.state.read_state())
        self.assertIn("public route", removed["removed"])
        self.assertIn("running release", removed["removed"])
        self.assertEqual(self.containers(), [])

    def test_zz_fail_plain_recovery_after_the_process_exits(self):
        # The release state is shared by this class, so the sequences continue after the forced-stop case.
        self.fresh_container_slate()
        policy = self.policy_configuration(2, "fail")
        self.assertOutcome(self.deploy("req-p1", "v7", 8003, configuration=policy), "succeeded")
        (original,) = self.containers()
        blocked = self.deploy("req-p2", "v8", 8004, configuration=policy)
        self.assertOutcome(blocked, "intervention_required")
        self.assertEqual((blocked.exit_code, blocked.detail["kind"]), (6, "shutdown_timeout"))
        self.assertEqual(self.containers(), [original])  # not killed, not removed, no candidate
        self.assertEqual(docker("inspect", "--format", "{{.State.Running}} {{.HostConfig.RestartPolicy.Name}}", original).stdout.strip(),
                         "true no")
        self.assertIsNotNone(self.state.read_journal())
        with self.lock.acquire():
            self.assertOutcome(self.deployer.recover(), "intervention_required")  # still draining: nothing is cleared
        self.assertEqual(self.containers(), [original])
        self.assertEqual(docker("inspect", "--format", "{{.State.Running}}", original).stdout.strip(), "true")
        docker("kill", "--signal", "KILL", original)  # the process finally exits
        with self.lock.acquire():
            self.assertOutcome(self.deployer.recover(), "recovered")
        self.assertIsNone(self.state.read_journal())
        self.assertEqual(self.get("/version"), "v7")
        self.assertEqual(len(self.containers()), 1)

    def test_z_image_declared_volume_does_not_break_readiness_or_release_identity(self):
        deployed = self.deploy("req-v6", "v6-volume", 5002)
        self.assertOutcome(deployed, "succeeded")
        inspected = self.inspect()
        mounts = {(mount["Name"], mount["Destination"]) for mount in inspected["Mounts"]}
        self.assertIn((f"shiplord-{self.app}-artifacts", "/app/src/.adk"), mounts)
        self.assertTrue(any(destination == "/scratch" for _, destination in mounts))
        self.assertTrue(self.runtime.is_running(self.registration, self.state.read_state()["current"]))

    def test_template_change_rollback_and_journal_recovery_restore_the_correct_environment(self):
        self.set_data_dir("/app/src/.adk")
        adk = self.policy_configuration(8, "fail")
        generic = adk | {"template": "generic"}
        self.assertOutcome(self.deploy("req-profile1", "v1", 5001, configuration=adk), "succeeded")
        counter = int(self.get("/counter"))
        first = self.state.read_state()["current"]
        self.assertEqual(json.loads(self.get("/env"))["AGENT_NAME"], self.app)

        # Same image, commit, sequence, path, and port; only the application profile differs.
        self.assertOutcome(self.deploy("req-profile2", "v1", 5001, configuration=generic), "succeeded")
        current = self.state.read_state()["current"]
        self.assertEqual(release_id(first), release_id(current))
        environment = json.loads(self.get("/env"))
        self.assertEqual((environment["AGENT_NAME"], environment["AGENT_DIR"]), ("image-default", "/image/agents"))
        for key in ("ALLOW_ORIGINS", "SERVE_WEB_INTERFACE", "RELOAD_AGENTS"):
            self.assertIsNone(environment[key], key)
        self.assertEqual((environment["HOST"], environment["PORT"]), ("0.0.0.0", "8080"))
        self.assertEqual(self.get("/counter"), str(counter + 1))
        self.assertFalse(self.runtime.is_running(self.registration, first))

        with patch.object(self.deployer.routes, "apply", side_effect=RoutingError("fixture routing failure", "reload")):
            failed = self.deploy("req-profile3", "v1", 5001, configuration=adk)
        self.assertOutcome(failed, "rolled_back")
        self.assertEqual(json.loads(self.get("/env"))["AGENT_NAME"], "image-default")
        self.assertTrue(self.runtime.is_running(self.registration, current))
        self.assertIsNone(self.state.read_journal())

        # Simulate interruption after the candidate starts but before its state is committed.
        candidate = current | {"configuration": adk}
        with self.lock.acquire():
            self.state.begin("req-profile4", candidate)
            self.state.advance("runtime-changing")
            self.runtime.start(self.registration, candidate)
        self.assertEqual(json.loads(self.get("/env"))["AGENT_NAME"], self.app)
        with self.lock.acquire():
            recovered = self.deployer.recover()
        self.assertOutcome(recovered, "recovered")
        self.assertEqual(json.loads(self.get("/env"))["AGENT_NAME"], "image-default")
        self.assertEqual(self.get("/counter"), str(counter + 2))
        self.assertIsNone(self.state.read_journal())

        self.assertOutcome(self.runtime_rollback("req-profile5"), "succeeded")
        self.assertEqual(json.loads(self.get("/env"))["AGENT_NAME"], self.app)

    def test_zzz_failed_routing_restores_the_previous_configuration(self):
        """A same-release redeploy that changes configuration must restart the old one, not adopt the candidate."""
        self.fresh_container_slate()
        self.set_data_dir("/app/src/.adk")
        original = self.configuration()
        original["http"].update(container_port=8080, frontend_origins=["https://a.example.test"])
        original["resources"] = {"memory_mb": 512}
        self.assertOutcome(self.deploy("req-c1", "v1", 9000, configuration=original), "succeeded")
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(json.loads(self.get("/env"))["ALLOW_ORIGINS"], '["https://a.example.test"]')

        # Same image, commit, sequence, and therefore the same container: only the configuration differs.
        changed = self.configuration()
        changed["http"].update(container_port=8081, frontend_origins=["https://b.example.test"])
        changed["resources"] = {"memory_mb": 768}
        with patch.object(self.deployer.routes, "apply", side_effect=RoutingError("fixture routing failure", "reload")):
            failed = self.deploy("req-c2", "v1", 9000, configuration=changed)
        self.assertOutcome(failed, "rolled_back")
        self.assertEqual(failed.exit_code, 5)

        host = self.inspect()["HostConfig"]
        self.assertEqual((host["Memory"], host["MemorySwap"]), (512 * 1024 * 1024, 512 * 1024 * 1024))
        self.assertEqual(host["PortBindings"], {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}]})
        self.assertEqual(json.loads(self.get("/env"))["ALLOW_ORIGINS"], '["https://a.example.test"]')
        self.assertEqual(self.get("/version"), "v1")
        self.assertTrue(self.runtime.is_running(self.registration, self.state.read_state()["current"]))

    def runtime_rollback(self, request_id):
        with self.lock.acquire():
            return self.deployer.rollback(request_id)

    def manager(self):
        return BackupManager(registration=self.registration, state=self.state, runtime=self.runtime,
                             layout=self.layout)

    def wipe_data(self):
        """Delete the volume's contents as a lost disk would; the container keeps running with nothing to read."""
        data_root = self.runtime.data_root(self.registration)
        for entry in data_root.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        self.assertEqual(list(data_root.iterdir()), [])

    def test_zzz_backup_wipe_and_restore_round_trip(self):
        """Back up, destroy the data, restore it, and refuse a corrupt archive without touching live data."""
        self.assertOutcome(self.deploy("req-b1", "v1", 9000), "succeeded")
        before_rows = json.loads(self.get("/sqlite"))
        before_counter = int(self.get("/counter"))
        with self.lock.acquire():
            backed_up = self.manager().backup()
        self.assertOutcome(backed_up, "succeeded")
        self.assertTrue(backed_up.detail["verified"])
        name = backed_up.detail["archive"]
        archive = self.layout.backups / self.app / name
        self.assertEqual(oct(archive.stat().st_mode & 0o777), "0o600")
        self.assertEqual(read_manifest(archive.with_name(name + ".json")).archive, name)

        self.wipe_data()
        with self.lock.acquire():
            restored = self.manager().restore({"archive": name})
        self.assertOutcome(restored, "succeeded")
        self.assertTrue(restored.detail["verified"])
        self.assertEqual(self.get("/version"), "v1")
        self.assertEqual(json.loads(self.get("/sqlite")), before_rows + ["v1"])
        self.assertEqual(int(self.get("/counter")), before_counter + 1)
        self.assertTrue(self.runtime.is_running(self.registration, self.state.read_state()["current"]))

        with self.lock.acquire():
            second = self.manager().backup()
        self.assertOutcome(second, "succeeded")
        corrupt = self.layout.backups / self.app / second.detail["archive"]
        with corrupt.open("r+b") as file:
            file.seek(20)
            file.write(b"\x00")
        live_counter = int(self.get("/counter"))
        with self.lock.acquire():
            refused = self.manager().restore({"archive": second.detail["archive"]})
        self.assertOutcome(refused, "failed")
        self.assertEqual(refused.detail["error"], "checksum_mismatch")
        self.assertEqual(int(self.get("/counter")), live_counter + 1)  # the live data was never replaced
        self.assertEqual(self.get("/version"), "v1")
    # the authorized helper path, for a test that must go through the dispatcher and the worker ----
    def register_application(self):
        """Write the registration the helper reads; it must match the record every deploy in this class used."""
        self.layout.registrations.mkdir(parents=True, exist_ok=True)
        self.layout.registrations.chmod(0o700)
        descriptor = os.open(self.layout.registrations, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        with self.lock.acquire():
            registration = Registry(descriptor, self.lock).register(
                self.app, deployment_uid=1001, deployment_user="ship-fixture", runtime="docker",
                api_domain="fixture.example.test", image_repository=REPOSITORY,
                probe=lambda port: port == self.port)
        self.assertEqual(registration["host_port"], self.port)
        return registration

    def helper_services(self, registration):
        """The worker's services: the real runtime and lock, a launcher that only records, no Caddy admin."""
        launched = []

        class Launcher:
            def launch(self, application_id, request_id):
                launched.append((application_id, request_id))

            def is_active(self, application_id, request_id):
                return True

        services = HostServices(self.layout, HostSystem(), Launcher(), FakeCaddy(), probes=Probes(admin_port=1),
                                runtime_factory=lambda services, value: self.runtime)
        self.authorize = patch("shiplord.privileged_host.authorize_application", lambda application_id: registration)
        self.authorize.start()
        self.addCleanup(self.authorize.stop)
        return services, launched

    def helper_call(self, services, action, request_id, **arguments):
        message = {"version": PROTOCOL_VERSION, "request_id": request_id, "action": action,
                   "application_id": self.app, "commit": None, "arguments": arguments}
        encoded = io.BytesIO()
        write_frame(encoded, message)
        output = io.BytesIO()
        code = serve(io.BytesIO(encoded.getvalue()), output, lambda: services)
        return read_frame(io.BytesIO(output.getvalue())), code

    def test_restart_applies_a_changed_environment_without_a_new_release(self):
        """An env change reaches the process through an authorized restart; a redeploy is not `unchanged`."""
        env_file = self.layout.environments / self.app / "app.env"
        env_file.write_text("CUSTOM_TOKEN=before\n")
        env_file.chmod(0o600)
        self.assertOutcome(self.deploy("req-e1", "v5", 4000), "succeeded")
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "before")
        self.assertEqual(self.state.read_state()["current"]["commit"], "e" * 40)

        # Writing the file changes nothing by itself: the running container was created with `before`.
        env_file.write_text("CUSTOM_TOKEN=after\n")
        env_file.chmod(0o600)
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "before")

        # Criterion 2: the same release record with a new request ID is applied, not reported `unchanged`.
        again = self.deploy("req-e2", "v5", 4000)
        self.assertOutcome(again, "succeeded")
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "after")
        self.assertEqual(self.get("/version"), "v5")
        state = self.state.read_state()
        self.assertEqual((state["current"]["commit"], state["previous"]["commit"]), ("e" * 40, "a" * 40))

        # Criterion 1: an authorized `restart` job applies a later change to the process already deployed.
        env_file.write_text("CUSTOM_TOKEN=final\n")
        env_file.chmod(0o600)
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "after")
        services, launched = self.helper_services(self.register_application())
        before = self.containers()
        response, code = self.helper_call(services, "restart", "req-e3")
        self.assertEqual((code, response["status"], response["job"]["action"]), (0, "accepted", "restart"))
        self.assertEqual(launched, [(self.app, "req-e3")])
        self.assertEqual(run_job(services, self.app, "req-e3"), 0)
        job = self.helper_call(services, "status", "req-e4", job_id="req-e3")[0]["result"]["job"]
        self.assertEqual((job["state"], job["outcome"]["status"], job["outcome"]["exit_code"]),
                         ("finished", "succeeded", 0))
        self.assertEqual(job["outcome"]["detail"]["release"]["commit"], "e" * 40)
        self.assertEqual(json.loads(self.get("/env"))["CUSTOM_TOKEN"], "final")
        self.assertEqual(self.get("/version"), "v5")  # the recorded release never moved
        self.assertNotEqual(self.containers(), before)  # the process was replaced, not just told to re-read
        self.assertEqual(len(self.containers()), 1)
        self.assertIsNone(self.state.read_journal())
        state = self.state.read_state()
        self.assertEqual((state["current"]["commit"], state["previous"]["commit"]), ("e" * 40, "a" * 40))
        self.assertEqual(state["transaction_id"], "req-e3")


if __name__ == "__main__":
    unittest.main()
