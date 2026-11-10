"""The runtime knobs people ask for first: CPU limit, ``--init``, readiness, and storage ownership.

Each knob is checked twice: the manifest accepts (or refuses) it before any side effect, and the accepted
value reaches the command line or the readiness loop that uses it.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import configuration, record  # noqa: E402
from shiplord import commands  # noqa: E402
from shiplord.commands import CommandError  # noqa: E402
from shiplord.manifest import ManifestError, validate_manifest  # noqa: E402
from shiplord.probes import Probes  # noqa: E402
from shiplord.release_state import StateError, validate_release  # noqa: E402
from shiplord.runtime import cpu_limit_arg  # noqa: E402
from shiplord.storage import StorageError, validate_storage  # noqa: E402
from shiplord.system import CommandResult  # noqa: E402
from test_client import ClientTestCase  # noqa: E402
from test_commands import COMMIT, IMAGE  # noqa: E402
from test_probes import FakeClock, handler, serve, stop  # noqa: E402
from test_runtime import REGISTRATION, RuntimeTestCase, release  # noqa: E402


def with_resources(**resources):
    raw = configuration()
    raw["resources"].update(resources)
    return raw


def with_deploy(**deploy):
    raw = configuration()
    raw["deploy"].update(deploy)
    return raw


def release_with(**deploy):
    """A release record whose stored configuration asks for a readiness knob."""
    value = release()
    value["configuration"]["deploy"].update(deploy)
    return value


class ManifestTests(unittest.TestCase):
    def test_cpu_limit_and_init_round_trip(self):
        raw = with_resources(cpu_limit=0.5)
        raw["deploy"]["init"] = True
        result = validate_manifest(raw)
        self.assertEqual(result["resources"], {"memory_mb": 512, "cpu_limit": 0.5})
        self.assertIs(result["deploy"]["init"], True)

    def test_a_whole_cpu_count_is_accepted(self):
        self.assertEqual(validate_manifest(with_resources(cpu_limit=2))["resources"]["cpu_limit"], 2.0)

    def test_the_cpu_bounds_are_inclusive(self):
        for value in (0.01, 64, 64.0):
            with self.subTest(value=value):
                self.assertEqual(validate_manifest(with_resources(cpu_limit=value))["resources"]["cpu_limit"], float(value))

    def test_invalid_cpu_limits_are_refused(self):
        for value in (0, 0.001, 64.01, -1, True, "1", None, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ManifestError):
                validate_manifest(with_resources(cpu_limit=value))

    def test_invalid_init_values_are_refused(self):
        for value in (1, 0, "true", None, [True]):
            with self.subTest(value=value), self.assertRaises(ManifestError):
                validate_manifest(with_deploy(init=value))

    def test_cpu_limit_and_init_are_refused_for_native_applications(self):
        for raw, message in ((with_resources(cpu_limit=1), "cpu_limit applies only to Docker"),
                             (with_deploy(init=True), "init applies only to Docker")):
            raw["runtime"] = "native"
            with self.assertRaisesRegex(ManifestError, message):
                validate_manifest(raw)

    def test_unknown_knobs_are_refused_rather_than_ignored(self):
        for raw, message in ((with_resources(cpu_shares=512), "resources contains unknown fields"),
                             (with_deploy(readiness_interval=5), "deploy contains unknown fields")):
            with self.subTest(message), self.assertRaisesRegex(ManifestError, message):
                validate_manifest(raw)

    def test_readiness_settings_are_optional_and_absent_from_legacy_records(self):
        result = validate_manifest(configuration())
        self.assertNotIn("readiness_status", result["deploy"])
        self.assertNotIn("readiness_interval_seconds", result["deploy"])
        self.assertNotIn("cpu_limit", result["resources"])

    def test_readiness_settings_round_trip_for_both_runtimes(self):
        for runtime in ("docker", "native"):
            with self.subTest(runtime):
                raw = with_deploy(readiness_status=204, readiness_interval_seconds=7)
                raw["runtime"] = runtime
                result = validate_manifest(raw)
                self.assertEqual((result["deploy"]["readiness_status"],
                                  result["deploy"]["readiness_interval_seconds"]), (204, 7))

    def test_invalid_readiness_values_are_refused(self):
        cases = [("readiness_status", value) for value in (99, 600, True, "200", 200.0, None)] + \
                [("readiness_interval_seconds", value) for value in (0, 61, True, 1.5, "1", None)]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ManifestError):
                validate_manifest(with_deploy(**{key: value}))


class OwnershipTests(unittest.TestCase):
    def test_a_manifest_that_asks_for_ownership_is_refused_when_it_is_loaded(self):
        raw = configuration()
        raw["storage"] = {"container_path": "/data", "owner_uid": 10001, "owner_gid": 10001}
        with self.assertRaisesRegex(ManifestError, "owner_uid and storage.owner_gid are not supported"):
            validate_manifest(raw)
        with self.assertRaisesRegex(StorageError, "not supported"):
            validate_storage(raw["storage"])

    def test_a_release_record_with_ownership_is_refused_before_deploy(self):
        # No helper could ever have committed one, so no stored release loses meaning here.
        raw = record()
        raw["configuration"]["storage"] = {"container_path": "/data", "owner_uid": 10001, "owner_gid": 10001}
        with self.assertRaises(StateError):
            validate_release(raw, "orders")

    def test_path_only_storage_is_unaffected(self):
        raw = configuration()
        raw["storage"] = {"container_path": "/data"}
        self.assertEqual(validate_manifest(raw)["storage"], {"container_path": "/data"})


class CommandLineTests(RuntimeTestCase):
    def start(self, **kwargs):
        self.docker.results["ps"] = CommandResult(0, "")
        self.runtime.start(REGISTRATION, release(**kwargs))
        return self.docker.sub("run")[0]

    def test_no_knob_is_added_by_default(self):
        argv = self.start()
        self.assertNotIn("--cpus", argv)
        self.assertNotIn("--init", argv)

    def test_the_cpu_limit_reaches_docker_as_cpus(self):
        for configured, expected in ((0.5, "0.5"), (2, "2"), (1.75, "1.75")):
            with self.subTest(configured=configured):
                self.docker.calls.clear()
                argv = self.start(cpu=configured)
                self.assertEqual(argv[argv.index("--cpus") + 1], expected)
                self.assertEqual(argv.count("--cpus"), 1)

    def test_init_reaches_docker_only_when_it_is_enabled(self):
        argv = self.start(init=True)
        self.assertEqual(argv.count("--init"), 1)
        self.assertLess(argv.index("--init"), len(argv) - 1)  # a flag before the image, never after it

    def test_both_knobs_reach_the_same_command_line(self):
        argv = self.start(cpu=1.5, init=True)
        self.assertEqual(argv[argv.index("--cpus") + 1], "1.5")
        self.assertIn("--init", argv)

    def test_a_whole_number_has_no_trailing_decimal_point(self):
        self.assertEqual([cpu_limit_arg(value) for value in (2.0, 0.5, 1.75, 0.01)], ["2", "0.5", "1.75", "0.01"])


class ReadinessTests(unittest.TestCase):
    def probes(self, server):
        clock = FakeClock()
        return Probes(clock=clock, sleep=clock.sleep), dict(REGISTRATION, host_port=server.server_address[1]), clock

    def test_a_configured_status_is_what_counts_as_ready(self):
        server = serve(handler([(204, b"")]))
        self.addCleanup(stop, server)
        probes, registration, _ = self.probes(server)
        self.assertFalse(probes.ready_once(registration, release()))  # the default stays 200
        self.assertTrue(probes.ready_once(registration, release_with(readiness_status=204)))

    def test_a_configured_interval_is_the_sleep_between_polls(self):
        server = serve(handler([(503, b""), (200, b"ok")]))
        self.addCleanup(stop, server)
        probes, registration, clock = self.probes(server)
        result = probes.wait_ready(registration, release_with(readiness_interval_seconds=7), alive=lambda: True)
        self.assertEqual(result, {"attempts": 2, "ready": True})
        self.assertEqual(clock.sleeps, [7])

    def test_the_default_interval_is_unchanged(self):
        server = serve(handler([(503, b""), (200, b"ok")]))
        self.addCleanup(stop, server)
        probes, registration, clock = self.probes(server)
        probes.wait_ready(registration, release(), alive=lambda: True)
        self.assertEqual(clock.sleeps, [1.0])


class NegotiationTests(ClientTestCase):
    def manifest(self, **deploy):
        return validate_manifest(with_deploy(**deploy))

    def test_the_current_helper_advertises_the_feature(self):
        self.assertIn("runtime_knobs_v1", self.client.call("capabilities")["actions"])

    def test_a_helper_without_the_feature_is_refused_before_any_plan_or_job(self):
        old = {"status": "ok", "request_id": "x", "version": 1, "helper_version": "0.1.0",
               "actions": ["capabilities", "plan", "deploy", "status", "logs", "rollback", "recover"]}
        with patch.object(self.client, "call", return_value=old) as call:
            with self.assertRaisesRegex(CommandError, "readiness settings"):
                commands.deploy_application(self.manifest(readiness_status=204), self.client, image=IMAGE,
                                            commit=COMMIT, sequence=1)
            with self.assertRaisesRegex(CommandError, "readiness settings"):
                commands.plan_application(self.manifest(readiness_status=204), self.client, image=IMAGE,
                                          commit=COMMIT, sequence=1)
            self.assertEqual([c.args[0] for c in call.call_args_list], ["capabilities", "capabilities"])

    def test_legacy_manifests_never_probe_capabilities(self):
        with patch.object(self.client, "call", side_effect=AssertionError("unexpected call")):
            commands.require_feature_support(validate_manifest(configuration()), self.client)

    def test_a_plan_through_the_real_helper_accepts_the_knobs(self):
        code, plan = commands.plan_application(self.manifest(readiness_status=204, readiness_interval_seconds=5,
                                                             init=True), self.client, image=IMAGE, commit=COMMIT,
                                               sequence=1000)
        self.assertEqual((code, plan["action"]), (0, "deploy"))  # a refused configuration would plan nothing


class PublicCheckForwardingTests(unittest.TestCase):
    class Client:
        def call(self, action, **kwargs):
            return {"actions": ["runtime_knobs_v1"]}

    def test_the_configured_readiness_status_reaches_the_public_check(self):
        seen = {}

        def public(domain, path, **kwargs):
            seen.update(domain=domain, path=path, **kwargs)
            return {"ok": True, "status": 204, "error": None}

        manifest = validate_manifest(with_deploy(readiness_status=204))
        succeeded = {"exit_code": 0, "status": "succeeded", "detail": {}}
        with patch.object(commands, "submit_and_wait", return_value=succeeded):
            code, result = commands.deploy_application(manifest, self.Client(), image=IMAGE, commit=COMMIT,
                                                       sequence=1, public_check=public)
        self.assertEqual((code, result["public"]["status"]), (0, 204))
        self.assertEqual((seen["domain"], seen["path"], seen["ready_status"]), ("orders.example.com", "/ready", 204))

    def test_a_manifest_without_the_setting_keeps_200(self):
        seen = {}

        def public(domain, path, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "status": 200, "error": None}

        succeeded = {"exit_code": 0, "status": "succeeded", "detail": {}}
        with patch.object(commands, "submit_and_wait", return_value=succeeded):
            commands.deploy_application(validate_manifest(configuration()), self.Client(), image=IMAGE,
                                        commit=COMMIT, sequence=1, public_check=public)
        self.assertEqual(seen["ready_status"], 200)


if __name__ == "__main__":
    unittest.main()
