"""Boot recovery on a host with real systemd (SHIPLORD_TEST_SYSTEMD=1).

``tests/linux/run.sh systemd`` runs this module twice with the container restarted in between:

- phase ``interrupt`` bootstraps a host, deploys a release, then starts a second deploy and
  ``SIGKILL``s its worker mid-transition. The journal is left open, exactly as a power loss
  would leave it.
- phase ``verify`` runs after the reboot and asserts that ``shiplord-boot-recovery.service``
  finished that transaction by itself: the committed release serves again, the journal is
  closed, and nobody ran ``shiplord recover``.

The two phases share one container, so the fixture's state travels in a JSON file in a durable
directory. Not `/tmp`: this image's `tmpfiles.d` declares `D /tmp`, so systemd empties `/tmp` at boot,
which is exactly what this test does to the host.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path("/root/boot-recovery")
STATE = ROOT / "state.json"
UNIT = "shiplord-boot-recovery.service"


def _systemd_running() -> bool:
    try:
        result = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() in ("running", "degraded")


PHASE = os.environ.get("SHIPLORD_BOOT_PHASE")
ENABLED = (os.environ.get("SHIPLORD_TEST_SYSTEMD") == "1" and os.path.exists("/.dockerenv") and os.geteuid() == 0
           and shutil.which("gcc") and shutil.which("caddy") and _systemd_running())

FIXTURE = r'''
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#ifndef VERSION
#define VERSION "v0"
#endif
int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
#ifdef SLOW_START
    /* A candidate that is still starting when its worker is killed. */
    sleep(SLOW_START);
#endif
    const char *port = getenv("PORT"), *host = getenv("HOST");
    printf("fixture started version=%s port=%s\n", VERSION, port ? port : "?");
    int server = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(atoi(port ? port : "0"))};
    inet_pton(AF_INET, host ? host : "127.0.0.1", &address.sin_addr);
    if (bind(server, (struct sockaddr *)&address, sizeof address) != 0 || listen(server, 16) != 0) { perror("bind"); return 2; }
    for (;;) {
        int client = accept(server, NULL, NULL);
        if (client < 0) continue;
        char request[2048] = {0};
        read(client, request, sizeof request - 1);
        char out[256];
        int n = snprintf(out, sizeof out, "HTTP/1.1 200 OK\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n%s",
                         strlen(VERSION), VERSION);
        write(client, out, n);
        close(client);
    }
}
'''


def sh(*argv, check=True, input=None, timeout=300):
    result = subprocess.run(argv, capture_output=True, text=True, input=input, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"{argv} failed: {result.stdout} {result.stderr}")
    return result


class BootRecoveryCase(unittest.TestCase):
    """Helpers shared by the two phases; each phase is its own test class."""

    @classmethod
    def setUpClass(cls):
        ROOT.mkdir(exist_ok=True)

    # the CLI, exactly as a person would run it ---------------------------------
    def cli(self, *args, check=None):
        project = ROOT / "project"
        project.mkdir(exist_ok=True)
        environment = dict(os.environ, XDG_CONFIG_HOME=str(ROOT / "config"), HOME="/root")
        result = subprocess.run([shutil.which("shiplord"), *args], cwd=project, env=environment, capture_output=True,
                                text=True, timeout=900)
        if check is not None and result.returncode != check:
            self.fail(f"shiplord {' '.join(args)} exited {result.returncode}, expected {check}\n"
                      f"{result.stdout}\n{result.stderr}")
        return result

    def cli_json(self, *args, check=0):
        return json.loads(self.cli(*args, "--json", check=check).stdout)

    def get(self, port, path="/version", timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
                    return response.read().decode()
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)

    def wait_until(self, condition, timeout=90, interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(interval)
        self.fail("timed out waiting for a condition")

    def state(self) -> dict:
        return json.loads(STATE.read_text())

    def journal(self, since: str | None = None) -> str:
        argv = ["journalctl", "--unit", UNIT, "--no-pager", "--output", "cat"]
        if since is not None:
            argv.append(f"--since={since}")  # one argument: a leading dash would be read as an option
        return sh(*argv, check=False).stdout


@unittest.skipUnless(ENABLED and PHASE == "interrupt", "set SHIPLORD_BOOT_PHASE=interrupt in a systemd container")
class InterruptPhase(BootRecoveryCase):
    """Leave a real interrupted transaction behind for the next boot."""

    def test_01_a_host_is_bootstrapped_and_serving_a_release(self):
        cls = type(self)
        cls.app = "boot-" + secrets.token_hex(3)
        ROOT.mkdir(exist_ok=True)
        (ROOT / "config").mkdir(exist_ok=True)
        (ROOT / "project").mkdir(exist_ok=True)
        self.setup_ssh()
        cls.binaries = self.build_fixtures()
        self.cli("server", "add", "production", "--ssh", "appuser@127.0.0.1", check=0)
        result = self.cli_json("server", "bootstrap", "production")
        # `test_e2e_systemd` bootstrapped this same container first, so this is a repeat: it must not
        # reinstall, and the unit must already be installed and enabled.
        self.assertIn("boot_recovery", result["changed"], result)
        self.assertEqual(sh("systemctl", "is-enabled", UNIT).stdout.strip(), "enabled")
        self.cli("configure", "--name", cls.app, "--server", "production", "--api-domain",
                 f"{cls.app}.example.test", "--no-frontend", "--runtime", "native", check=0)
        manifest = ROOT / "project" / "ship.toml"
        text = manifest.read_text().replace("memory_mb = 512", "memory_mb = 128")
        manifest.write_text(text.replace("readiness_timeout_seconds = 90", "readiness_timeout_seconds = 12"))
        cls.port = self.cli_json("provision", "--public-key", str(ROOT / "dev_key.pub"))["host_port"]
        first = self.deploy("v1", 1000)
        self.assertEqual((first["status"], first["release"]["commit"]), ("succeeded", cls.binaries["v1"]["commit"]))
        self.assertEqual(self.get(cls.port), "v1")
        # The reboot has to bring the application's own unit and SSH back without the test's help.
        sh("systemctl", "enable", "ssh.socket", check=False)
        sh("systemctl", "enable", "caddy", check=False)

    def test_02_a_deploy_killed_mid_transition_leaves_an_open_journal(self):
        cls = type(self)
        binary = cls.binaries["stuck"]
        process = subprocess.Popen(
            [shutil.which("shiplord"), "deploy", "--binary", str(binary["path"]), "--commit", binary["commit"],
             "--sequence", "2000", "--no-verify-public", "--request-id", "boot-killed-1", "--json"],
            cwd=ROOT / "project", env=dict(os.environ, XDG_CONFIG_HOME=str(ROOT / "config"), HOME="/root"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        unit = f"shiplord-job-{cls.app}:boot-killed-1.service"
        self.wait_until(lambda: sh("systemctl", "is-active", unit, check=False).stdout.strip() == "active")
        self.wait_until(lambda: (self.cli_json("status")["application"]["unfinished_transaction"] or {}).get("phase")
                        == "runtime-changing")
        sh("systemctl", "kill", "--signal=SIGKILL", unit)  # the worker dies; its journal stays open
        stdout, stderr = process.communicate(timeout=180)
        self.assertEqual(process.returncode, 6, stderr)
        self.assertEqual(json.loads(stdout)["status"], "interrupted")
        status = self.cli_json("status")["application"]
        self.assertIsNotNone(status["unfinished_transaction"])
        self.assertEqual(status["current"]["commit"], cls.binaries["v1"]["commit"])
        STATE.write_text(json.dumps({"application": cls.app, "port": cls.port,
                                     "committed": cls.binaries["v1"]["commit"],
                                     "candidate": binary["commit"]}))

    # fixtures -----------------------------------------------------------------
    def setup_ssh(self):
        if sh("getent", "passwd", "appuser", check=False).returncode != 0:
            sh("useradd", "-m", "-s", "/bin/bash", "appuser")
        Path("/etc/sudoers.d/e2e-admin").write_text("appuser ALL=(ALL) NOPASSWD: ALL\n")
        os.chmod("/etc/sudoers.d/e2e-admin", 0o440)
        if not (ROOT / "dev_key").exists():
            sh("ssh-keygen", "-q", "-N", "", "-t", "ed25519", "-f", str(ROOT / "dev_key"))
        directory = Path("/home/appuser/.ssh")
        directory.mkdir(mode=0o700, exist_ok=True)
        (directory / "authorized_keys").write_text((ROOT / "dev_key.pub").read_text())
        os.chmod(directory / "authorized_keys", 0o600)
        sh("chown", "-R", "appuser:appuser", str(directory))
        sh("ssh-keygen", "-A", check=False)
        sh("systemctl", "start", "ssh.socket", check=False)
        host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()[:2]
        (ROOT / "known_hosts").write_text(f"127.0.0.1 {host_key[0]} {host_key[1]}\n")
        os.makedirs("/root/.ssh", exist_ok=True)
        Path("/root/.ssh/config").write_text(f"Host *\n  IdentityFile {ROOT / 'dev_key'}\n  IdentitiesOnly yes\n"
                                             f"  UserKnownHostsFile {ROOT / 'known_hosts'}\n")

    @staticmethod
    def build_fixtures() -> dict:
        source = ROOT / "fixture.c"
        source.write_text(FIXTURE)
        binaries = {}
        variants = {"v1": ([], "v1"), "stuck": (["-DSLOW_START=40"], "stuck")}
        for index, (name, (flags, version)) in enumerate(variants.items()):
            target = ROOT / f"app-{name}"
            sh("gcc", "-O1", f'-DVERSION="{version}"', *flags, "-o", str(target), str(source))
            binaries[name] = {"path": target, "commit": f"{index + 1:x}" * 40}
        return binaries

    def deploy(self, name, sequence, check=0):
        binary = type(self).binaries[name]
        return self.cli_json("deploy", "--binary", str(binary["path"]), "--commit", binary["commit"], "--sequence",
                             str(sequence), "--no-verify-public", check=check)


@unittest.skipUnless(ENABLED and PHASE == "verify", "set SHIPLORD_BOOT_PHASE=verify in a systemd container")
class VerifyPhase(BootRecoveryCase):
    """After the reboot: the unit recovered the transaction without a person running `recover`."""

    def test_01_the_boot_unit_ran_and_succeeded(self):
        properties = dict(line.split("=", 1) for line in sh(
            "systemctl", "show", UNIT, "--property=ActiveState,Result,ExecMainStatus").stdout.splitlines())
        self.assertEqual((properties["ActiveState"], properties["Result"], properties["ExecMainStatus"]),
                         ("inactive", "success", "0"), properties)
        self.assertEqual(sh("systemctl", "is-enabled", UNIT).stdout.strip(), "enabled")
        unit_file = Path("/etc/systemd/system") / UNIT
        self.assertTrue(unit_file.exists())
        text = unit_file.read_text()
        self.assertIn("Type=oneshot", text)
        self.assertIn("WantedBy=multi-user.target", text)  # it really is part of the boot
        self.assertIn("After=docker.service caddy.service", text)
        after = set(sh("systemctl", "show", UNIT, "--property=After").stdout.partition("=")[2].split())
        self.assertTrue({"docker.service", "caddy.service"} <= after, after)  # and systemd parsed the ordering

    def test_02_the_journal_is_closed_without_a_person_running_recover(self):
        state = self.state()
        status = self.cli_json("status")["application"]
        self.assertIsNone(status["unfinished_transaction"])
        self.assertEqual(status["current"]["commit"], state["committed"])
        self.assertNotEqual(status["current"]["commit"], state["candidate"])

    def test_03_the_committed_release_is_serving_again(self):
        self.assertEqual(self.get(self.state()["port"]), "v1")

    def test_04_the_recovery_is_visible_in_the_journal(self):
        output = self.journal()
        self.assertIn(f"shiplord: {self.state()['application']}: recovered", output)

    def test_05_running_it_again_does_nothing(self):
        sh("systemctl", "start", UNIT)  # a second boot must be a no-op, not an error
        status = self.cli_json("status")["application"]
        self.assertIsNone(status["unfinished_transaction"])
        self.assertEqual(self.get(self.state()["port"]), "v1")
        self.assertIn("shiplord: no interrupted transaction to recover", self.journal(since="-2min"))


if __name__ == "__main__":
    unittest.main()
