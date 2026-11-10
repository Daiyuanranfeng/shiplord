from __future__ import annotations

import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from shiplord import routing
from shiplord.release_state import HostLock
from shiplord.routing import RouteManager, RoutingError, SubprocessCaddy, probe_caddy, render_route
from shiplord.system import TRUNCATION_MARKER, CommandError, CommandResult


def registration(app="orders", domain="orders.example.com", port=20000, hostnames=()):
    value = {"schema_version": 1, "application_id": app, "deployment_uid": 1001, "deployment_user": "ship-orders",
             "runtime": "docker", "api_domain": domain, "host_port": port,
             "image_repository": f"ghcr.io/example/{app}", "max_memory_mb": 4096}
    if hostnames:
        value["hostnames"] = list(hostnames)
    return value


class FakeCaddy:
    """Records calls; failures are scripted by 1-based call number."""

    def __init__(self, ship: Path):
        self.ship = ship
        self.validate_calls: list[dict] = []
        self.reload_calls = 0
        self.fail_validate: set[int] = set()
        self.unavailable_validate: set[int] = set()
        self.fail_reload: set[int] = set()
        self.on_reload = None

    def validate(self, config: Path) -> None:
        number = len(self.validate_calls) + 1
        self.validate_calls.append({"config": config, "text": config.read_text(),
                                    "mode": stat.S_IMODE(config.stat().st_mode),
                                    "live": {p.name: p.read_bytes() for p in self.ship.glob("*.caddy")}})
        if number in self.unavailable_validate:
            raise RoutingError("could not start /usr/bin/caddy", "unavailable")
        if number in self.fail_validate:
            raise RoutingError("Caddy command failed", "validation", detail="bad config")

    def reload(self) -> None:
        self.reload_calls += 1
        if self.on_reload:
            self.on_reload(self.reload_calls)
        if self.reload_calls in self.fail_reload:
            raise RoutingError("Caddy command failed", "reload", detail="reload refused")


class RoutingTestCase(unittest.TestCase):
    MAIN = "{\n\temail admin@example.com\n}\nexample.org {\n\trespond \"ok\"\n}\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "caddy"
        self.ship = self.root / "ship"
        self.ship.mkdir(parents=True)
        self.root.chmod(0o755)
        self.ship.chmod(0o755)
        self.write_main(self.MAIN + f"import {self.root}/ship/*.caddy\n")
        lock_temp = tempfile.TemporaryDirectory()
        self.addCleanup(lock_temp.cleanup)
        lock_fd = os.open(lock_temp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, lock_fd)
        self.lock = HostLock(lock_fd)
        self.caddy = FakeCaddy(self.ship)
        self.manager = RouteManager(self.root, self.lock, self.caddy)

    def write_main(self, text):
        path = self.root / "Caddyfile"
        path.write_text(text)
        path.chmod(0o644)

    def write_route(self, name, text, mode=0o644):
        path = self.ship / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def snapshot(self):
        """Every file under the Caddy root, for byte-for-byte comparison."""
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def assertNoTransactionLeftovers(self):
        leftovers = [p.name for p in self.root.rglob(".*") if p.is_file()]
        self.assertEqual(leftovers, [])


class ApplyTests(RoutingTestCase):
    def test_first_route_is_rendered_validated_before_publish_and_reloaded(self):
        other = self.write_route("billing.caddy", "billing.example.com {\n\treverse_proxy 127.0.0.1:20001\n}\n")
        before_other, before_main = other.read_bytes(), (self.root / "Caddyfile").read_bytes()
        with self.lock.acquire():
            result = self.manager.apply(registration())
        self.assertTrue(result.changed)
        self.assertEqual((self.ship / "orders.caddy").read_bytes(),
                         b"orders.example.com {\n\treverse_proxy 127.0.0.1:20000\n}\n")
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertEqual(len(self.caddy.validate_calls), 1)
        seen = self.caddy.validate_calls[0]
        self.assertNotIn("orders.caddy", seen["live"])  # candidate was not live during validation
        self.assertIn(f"import {self.root}/ship/billing.caddy", seen["text"])
        self.assertIn(f"import {self.root}/ship/.tmp-", seen["text"])
        self.assertNotIn("*.caddy", seen["text"])
        self.assertIn("example.org", seen["text"])
        self.assertEqual(other.read_bytes(), before_other)
        self.assertEqual((self.root / "Caddyfile").read_bytes(), before_main)
        self.assertNoTransactionLeftovers()

    def test_validation_runs_inside_the_caddy_root_so_relative_imports_resolve(self):
        with self.lock.acquire():
            self.manager.apply(registration())
        self.assertEqual(self.caddy.validate_calls[0]["config"].parent, self.root)

    def test_identical_route_skips_validation_and_reload(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            result = self.manager.apply(registration())
        self.assertFalse(result.changed)
        self.assertEqual((len(self.caddy.validate_calls), self.caddy.reload_calls), (1, 1))

    def test_changed_route_replaces_the_file_and_drops_the_backup(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            result = self.manager.apply(registration(port=20005))
        self.assertTrue(result.changed)
        self.assertIn(b"127.0.0.1:20005", (self.ship / "orders.caddy").read_bytes())
        self.assertEqual(self.caddy.reload_calls, 2)
        self.assertNoTransactionLeftovers()

    def test_requires_the_host_lock_and_writes_nothing_without_it(self):
        before = self.snapshot()
        with self.assertRaisesRegex(RoutingError, "lock"):
            self.manager.apply(registration())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.reload_calls, 0)

    def test_invalid_registration_or_injection_is_rejected_before_any_write(self):
        before = self.snapshot()
        bad = [registration(domain="a.example.com {\n\tfile_server\n}\nb.example.com"),
               registration(domain="a.example.com }"), registration(domain="A.example.com"),
               registration(port=2019), registration(port=22), dict(registration(), extra=1),
               dict(registration(), hostnames=["a.example.com {\n\tfile_server\n}\nb.example.com"])]
        with self.lock.acquire():
            for value in bad:
                with self.subTest(value=str(value)[:60]), self.assertRaises(ValueError):
                    self.manager.apply(value)
            with self.assertRaises(ValueError):
                self.manager.remove("../ship/orders")
            with self.assertRaises(RoutingError):
                self.manager.reconcile("orders", registration(app="billing", domain="b.example.com"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.validate_calls, [])

    def test_render_route_validates_its_inputs(self):
        with self.assertRaises(ValueError):
            render_route("bad domain", 20000)
        with self.assertRaises(RoutingError):
            render_route("orders.example.com", 2019)

    def test_render_route_rejects_an_invalid_alias(self):
        for hostnames in (["bad domain"], ["orders.example.com"], ["a.example.com", "a.example.com"],
                          ["Orders.example.com"], ["127.0.0.1"], "legacy.example.com"):
            with self.subTest(hostnames=hostnames), self.assertRaises(ValueError):
                render_route("orders.example.com", 20000, hostnames)

    def test_additional_hostnames_are_permanent_redirects_that_keep_the_uri(self):
        self.assertEqual(render_route("orders.example.com", 20000,
                                      ["legacy.example.com", "www.orders.example.com"]),
                         b"orders.example.com {\n\treverse_proxy 127.0.0.1:20000\n}\n"
                         b"\nlegacy.example.com {\n\tredir https://orders.example.com{uri} 308\n}\n"
                         b"\nwww.orders.example.com {\n\tredir https://orders.example.com{uri} 308\n}\n")

    def test_a_route_with_aliases_is_applied_and_reloads_once(self):
        aliases = ["legacy.example.com", "www.orders.example.com"]
        with self.lock.acquire():
            result = self.manager.apply(registration(hostnames=aliases))
        self.assertTrue(result.changed)
        self.assertIn(b"\nlegacy.example.com {\n\tredir https://orders.example.com{uri} 308\n}\n",
                      (self.ship / "orders.caddy").read_bytes())
        self.assertEqual(self.caddy.reload_calls, 1)

    def test_inspecting_a_route_reports_an_alias_change_as_different(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            self.assertEqual(self.manager.inspect_route(registration()), "identical")
            self.assertEqual(self.manager.inspect_route(registration(hostnames=["legacy.example.com"])),
                             "different")
            self.manager.apply(registration(hostnames=["legacy.example.com"]))
            self.assertEqual(self.manager.inspect_route(registration(hostnames=["legacy.example.com"])),
                             "identical")
            self.assertEqual(self.manager.inspect_route(registration()), "different")

    def test_a_route_change_leaves_a_0640_caddyfile_at_0640(self):
        (self.root / "Caddyfile").chmod(0o640)
        with self.lock.acquire():
            self.manager.apply(registration())
        self.assertEqual(self.mode(self.root / "Caddyfile"), 0o640)

    def test_the_validation_copy_of_the_caddyfile_keeps_its_mode(self):
        # The copy holds whatever token or password hash the Caddyfile holds, so it must not be wider than it.
        (self.root / "Caddyfile").chmod(0o640)
        with self.lock.acquire():
            self.manager.apply(registration())
        self.assertEqual([call["mode"] for call in self.caddy.validate_calls], [0o640])

    def test_route_files_are_created_with_an_explicit_mode_regardless_of_umask(self):
        for umask in (0o077, 0o000):
            with self.subTest(umask=oct(umask)):
                (self.ship / "orders.caddy").unlink(missing_ok=True)  # each umask must create the file itself
                previous = os.umask(umask)
                try:
                    with self.lock.acquire():
                        self.manager.apply(registration())
                finally:
                    os.umask(previous)
                self.assertEqual(self.mode(self.ship / "orders.caddy"), 0o644)


class FailureTests(RoutingTestCase):
    def test_first_release_validation_failure_leaves_no_route_and_no_reload(self):
        self.caddy.fail_validate = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.apply(registration())
        self.assertEqual(caught.exception.kind, "validation")
        self.assertEqual(caught.exception.detail, "bad config")
        self.assertNotIn("bad config", str(caught.exception))
        self.assertEqual(self.caddy.reload_calls, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertNoTransactionLeftovers()

    def test_validation_failure_keeps_the_previous_route_live(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_validate = {2}  # the candidate check; call 3 is the baseline check
            with self.assertRaises(RoutingError):
                self.manager.apply(registration(port=20007))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertNoTransactionLeftovers()

    def test_preexisting_invalid_configuration_is_not_blamed_on_the_candidate(self):
        self.caddy.fail_validate = {1, 2}
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "already invalid"):
            self.manager.apply(registration())
        self.assertEqual(self.caddy.reload_calls, 0)

    def test_an_unavailable_caddy_is_not_blamed_on_the_route_or_the_existing_configuration(self):
        self.caddy.unavailable_validate = {1, 2}  # candidate and baseline both cannot run Caddy
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.apply(registration())
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertNotIn("rejected", str(caught.exception))
        self.assertNotIn("already invalid", str(caught.exception))
        self.assertEqual((self.snapshot(), self.caddy.reload_calls), (before, 0))
        self.assertNoTransactionLeftovers()

    def test_reload_failure_restores_the_previous_route_and_reconciles(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_reload = {2}
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20007))
        self.assertEqual(caught.exception.kind, "reload")
        self.assertFalse(caught.exception.intervention_required)
        self.assertEqual(self.caddy.reload_calls, 3)  # apply, failed reload, reconciling reload
        self.assertEqual(self.snapshot(), before)
        self.assertNoTransactionLeftovers()

    def test_first_release_reload_failure_removes_the_route_and_reloads_again(self):
        self.caddy.fail_reload = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError):
            self.manager.apply(registration())
        self.assertEqual(self.caddy.reload_calls, 2)
        self.assertEqual(self.snapshot(), before)
        self.assertNoTransactionLeftovers()

    def test_second_reload_failure_requires_intervention_with_the_old_file_restored(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_reload = {2, 3}
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20007))
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(self.snapshot(), before)

    def test_failed_restore_requires_intervention(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            self.caddy.fail_reload = {2}
            self.caddy.on_reload = lambda n: (self.ship / ".prior-orders").unlink() if n == 2 else None
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20007))
        self.assertTrue(caught.exception.intervention_required)
        self.assertEqual(self.caddy.reload_calls, 2)  # never claims a reconciling reload happened

    def test_other_routes_are_untouched_by_every_outcome(self):
        other = self.write_route("billing.caddy", "billing.example.com {\n\treverse_proxy 127.0.0.1:20001\n}\n")
        expected = other.read_bytes()
        outcomes = [{"fail_validate": {1, 2}}, {"fail_validate": {3}}, {"fail_reload": {1}}, {"fail_reload": {4, 5}}]
        with self.lock.acquire():
            for outcome in outcomes:
                self.caddy.fail_validate = outcome.get("fail_validate", set())
                self.caddy.fail_reload = outcome.get("fail_reload", set())
                self.caddy.validate_calls.clear()
                self.caddy.reload_calls = 0
                try:
                    self.manager.apply(registration())
                except RoutingError:
                    pass
                self.assertEqual(other.read_bytes(), expected)


class RemoveAndReconcileTests(RoutingTestCase):
    def test_remove_is_validated_reloaded_and_idempotent(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            self.assertTrue(self.manager.remove("orders").changed)
            self.assertFalse((self.ship / "orders.caddy").exists())
            self.assertFalse(self.manager.remove("orders").changed)
        self.assertEqual(self.caddy.reload_calls, 2)
        self.assertNotIn("orders.caddy", self.caddy.validate_calls[1]["text"])
        self.assertNoTransactionLeftovers()

    def test_remove_reload_failure_restores_the_route(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            before = self.snapshot()
            self.caddy.fail_reload = {2}
            with self.assertRaises(RoutingError):
                self.manager.remove("orders")
        self.assertEqual(self.snapshot(), before)

    def test_reconcile_always_validates_and_reloads_even_when_the_file_matches(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            result = self.manager.reconcile("orders", registration())
        self.assertFalse(result.changed)
        self.assertEqual((len(self.caddy.validate_calls), self.caddy.reload_calls), (2, 2))

    def test_reconcile_to_absent_reloads_even_when_the_file_is_already_gone(self):
        with self.lock.acquire():
            result = self.manager.reconcile("orders", None)
        self.assertFalse(result.changed)
        self.assertEqual(self.caddy.reload_calls, 1)

    def test_reconcile_failure_of_an_unchanged_route_flags_intervention(self):
        with self.lock.acquire():
            self.manager.apply(registration())
            self.caddy.fail_reload = {2}
            with self.assertRaises(RoutingError) as caught:
                self.manager.reconcile("orders", registration())
        self.assertTrue(caught.exception.intervention_required)

    def test_interrupted_transaction_blocks_apply_but_reconcile_recovers_it(self):
        # Crash after publishing the candidate: new file live, old file kept as the backup.
        old = b"orders.example.com {\n\treverse_proxy 127.0.0.1:20000\n}\n"
        self.write_route("orders.caddy", render_route("orders.example.com", 20009).decode())
        self.write_route(".prior-orders", old.decode())
        with self.lock.acquire():
            with self.assertRaises(RoutingError) as caught:
                self.manager.apply(registration(port=20009))
            self.assertEqual(caught.exception.kind, "interrupted")
            self.assertEqual(self.caddy.reload_calls, 0)
            self.manager.reconcile("orders", registration())  # desired state is the old route
        self.assertEqual((self.ship / "orders.caddy").read_bytes(), old)
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertNoTransactionLeftovers()

    def test_stale_temporary_candidates_are_discarded(self):
        self.write_route(".tmp-deadbeef", "x.example.com {\n}\n")
        with self.lock.acquire():
            self.manager.apply(registration())
        self.assertNoTransactionLeftovers()


class ConflictTests(RoutingTestCase):
    def assertConflict(self, kind="conflict", hostnames=(), **options):
        before = self.snapshot()
        manager = RouteManager(self.root, self.lock, self.caddy, **options)
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            manager.apply(registration(hostnames=hostnames))
        self.assertEqual(caught.exception.kind, kind)
        self.assertEqual(self.caddy.validate_calls, [])
        self.assertEqual(self.caddy.reload_calls, 0)
        self.assertEqual(self.snapshot(), before)

    def main_with(self, text):
        self.write_main(self.MAIN + text + f"\nimport {self.root}/ship/*.caddy\n")

    def assertApplies(self, text, hostnames=()):
        """The hostname is free: the route is installed, then removed for the next case."""
        self.main_with(text)
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration(hostnames=hostnames)).changed)
            self.assertTrue(self.manager.remove("orders").changed)

    def inspect(self, text, hostnames=()):
        return routing._Inspection(["orders.example.com", *hostnames], f"{self.root}/ship/*.caddy", False).scan(text)

    def test_hostname_forms_in_the_main_caddyfile_are_conflicts(self):
        forms = ["orders.example.com {\n}", "https://orders.example.com {\n}", "http://orders.example.com:8080 {\n}",
                 "orders.example.com:443 {\n}", "ORDERS.Example.com {\n}", "a.example.com, orders.example.com {\n}",
                 "a.example.com,orders.example.com {\n}", '"orders.example.com" {\n}',
                 "orders.example.com. {\n}", "https://ORDERS.example.com.:443 {\n}",
                 "orders.example.com { respond ok }"]
        for form in forms:
            with self.subTest(form=form):
                self.main_with(form)
                self.assertConflict()

    def test_addresses_spread_over_lines_are_all_compared(self):
        self.main_with("a.example.com,\norders.example.com {\n}")
        self.assertConflict()

    def test_hostnames_inside_directive_values_are_not_site_addresses(self):
        cases = {
            "reverse_proxy upstream": "site.example.net {\n\treverse_proxy https://orders.example.com\n}",
            "header value": 'site.example.net {\n\theader Link https://orders.example.com/x\n}',
            "host matcher": "site.example.net {\n\t@api host orders.example.com\n\trespond @api ok\n}",
            "handle body": ("site.example.net {\n\thandle /api/* {\n\t\treverse_proxy orders.example.com:8443\n\t}\n}"),
            "one-line body": "site.example.net { reverse_proxy https://orders.example.com }",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertApplies(text)

    def test_an_alias_in_the_main_caddyfile_is_a_conflict(self):
        for form in ("legacy.example.com {\n}", "https://legacy.example.com/x {\n}",
                     "a.example.com, legacy.example.com {\n}", "LEGACY.example.com:443 {\n}"):
            with self.subTest(form=form):
                self.main_with(form)
                self.assertConflict(hostnames=["legacy.example.com"])

    def test_an_alias_in_another_managed_route_is_refused(self):
        self.write_route("billing.caddy", "legacy.example.com {\n\treverse_proxy 127.0.0.1:20001\n}\n")
        self.assertConflict(hostnames=["legacy.example.com"])

    def test_a_redirect_target_is_not_treated_as_a_site_address(self):
        # Another site redirecting to the alias is not another site serving it.
        self.assertApplies("site.example.net {\n\tredir https://legacy.example.com{uri} 308\n}",
                           hostnames=["legacy.example.com"])
        self.assertEqual(self.inspect("legacy.example.net {\n\tredir https://www.orders.example.com{uri} 308\n}",
                                      ["www.orders.example.com"]), [])

    def test_a_wildcard_that_covers_an_alias_is_refused(self):
        self.main_with("*.orders.example.com {\n}")
        self.assertConflict(hostnames=["www.orders.example.com"])
        with self.lock.acquire():
            RouteManager(self.root, self.lock, self.caddy, allow_wildcard_overlap=True).apply(
                registration(hostnames=["www.orders.example.com"]))
        self.assertTrue((self.ship / "orders.caddy").exists())

    def test_unrelated_wildcard_and_similar_hostnames_do_not_conflict_with_an_alias(self):
        self.main_with("*.example.net {\n}\nlegacy.example.com.evil.test {\n}")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration(hostnames=["legacy.example.com"])).changed)

    def test_a_wildcard_inside_a_directive_value_does_not_overlap(self):
        self.assertApplies("site.example.net {\n\treverse_proxy https://*.example.com\n}")

    def test_a_hostname_in_another_route_upstream_is_not_a_conflict(self):
        other = self.write_route("billing.caddy",
                                 "billing.example.com {\n\treverse_proxy https://orders.example.com\n}\n")
        expected = other.read_bytes()
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration()).changed)
        self.assertEqual(other.read_bytes(), expected)

    def test_a_directive_import_is_not_followed_for_site_addresses(self):
        (self.root / "legacy.conf").write_text("reverse_proxy https://orders.example.com\n")
        self.assertApplies(f"site.example.net {{\n\timport {self.root}/legacy.conf\n}}")

    def test_a_brace_less_single_site_reads_only_its_address_line(self):
        self.assertEqual(self.inspect("legacy.example.net\n\nreverse_proxy https://orders.example.com\n"), [])
        self.assertEqual(self.inspect("legacy.example.net\n\nheader X {$SOME_TOKEN}\n"), [])

    def test_a_brace_less_single_site_address_still_conflicts(self):
        with self.assertRaises(RoutingError) as caught:
            self.inspect("orders.example.com\n\nreverse_proxy 127.0.0.1:20000\n")
        self.assertEqual(caught.exception.kind, "conflict")

    def test_a_quoted_placeholder_site_address_is_refused(self):
        self.main_with('"{$SITE_ADDRESS}" {\n}')
        self.assertConflict("uninspectable")

    def test_internationalized_spellings_of_a_hostname_are_the_same_site(self):
        self.assertEqual(list(routing._addresses("bücher.example.org.")), ["xn--bcher-kva.example.org"])

    def test_dot_named_caddy_files_are_inspected_because_caddy_globs_match_them(self):
        self.write_route(".stray.caddy", "orders.example.com {\n}\n")
        self.assertConflict()

    def test_conflict_in_another_managed_route_is_refused(self):
        self.write_route("billing.caddy", "orders.example.com {\n\treverse_proxy 127.0.0.1:20001\n}\n")
        self.assertConflict()

    def test_wildcard_overlap_is_refused_unless_explicitly_allowed(self):
        self.main_with("*.example.com {\n\trespond \"wild\"\n}")
        self.assertConflict()
        with self.lock.acquire():
            RouteManager(self.root, self.lock, self.caddy, allow_wildcard_overlap=True).apply(registration())
        self.assertTrue((self.ship / "orders.caddy").exists())

    def test_unrelated_wildcard_and_similar_hostnames_do_not_conflict(self):
        self.main_with("*.example.net {\n}\nxorders.example.com {\n}\norders.example.com.evil.test {\n}")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration()).changed)

    def test_comments_and_heredoc_bodies_are_not_hostnames(self):
        self.main_with("# orders.example.com used to live here\nother.example.net {\n\trespond <<EOT\n"
                       "orders.example.com\nEOT 200\n}")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration()).changed)

    def test_followed_imports_are_inspected(self):
        (self.root / "extra").mkdir()
        (self.root / "extra" / "legacy.conf").write_text("orders.example.com {\n}\n")
        for argument in (f"{self.root}/extra/*.conf", "extra/legacy.conf", "./extra/*"):
            with self.subTest(argument=argument):
                self.write_main(self.MAIN + f"import {argument}\nimport {self.root}/ship/*.caddy\n")
                self.assertConflict()

    def test_snippet_imports_are_ignored_and_own_glob_is_not_followed(self):
        self.write_main(self.MAIN + "(common) {\n\tencode gzip\n}\nimport common\n" + f"import {self.root}/ship/*.caddy\n")
        with self.lock.acquire():
            self.assertTrue(self.manager.apply(registration()).changed)

    def test_uninspectable_configuration_fails_closed(self):
        (self.root / "extra").mkdir()
        (self.root / "extra" / "dir.conf").mkdir()
        cases = {
            "directory import": f"import {self.root}/extra/dir.conf\n",
            "placeholder address": "{$SITE_ADDRESS} {\n}\n",
            "unbalanced open": "example.net {\n",
            "unbalanced close": "}\n",
            "unterminated quote": 'example.net "unterminated\n',
            "open heredoc": "example.net {\n\trespond <<EOT\nbody\n}\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write_main(self.MAIN + text + f"import {self.root}/ship/*.caddy\n")
                self.assertConflict("uninspectable")

    def test_import_nesting_and_count_are_bounded(self):
        for index in range(4):
            (self.root / f"level{index}.conf").write_text(f"import level{index + 1}.conf\n")
        (self.root / "level4.conf").write_text("example.net {\n}\n")
        self.write_main(self.MAIN + "import level0.conf\n" + f"import {self.root}/ship/*.caddy\n")
        self.assertConflict("uninspectable")
        many = self.root / "many"
        many.mkdir()
        for index in range(65):
            (many / f"{index}.conf").write_text(f"site{index}.example.net {{\n}}\n")
        self.write_main(self.MAIN + "import many/*.conf\n" + f"import {self.root}/ship/*.caddy\n")
        self.assertConflict("uninspectable")


class PreconditionTests(RoutingTestCase):
    def assertPrecondition(self, pattern):
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, pattern) as caught:
            self.manager.apply(registration())
        self.assertEqual(caught.exception.kind, "precondition")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.caddy.validate_calls, [])

    def test_missing_directory_or_caddyfile(self):
        (self.root / "Caddyfile").unlink()
        self.assertPrecondition("missing")
        self.ship.rmdir()
        self.assertPrecondition("not bootstrapped")

    def test_missing_nested_or_duplicate_import_is_refused(self):
        for text in (self.MAIN, self.MAIN + f"example.net {{\n\timport {self.root}/ship/*.caddy\n}}\n",
                     self.MAIN + f"import {self.root}/ship/*.caddy\nimport {self.root}/ship/*.caddy\n",
                     self.MAIN + f"import {self.root}/other/*.caddy\n"):
            with self.subTest(text=text[-60:]):
                self.write_main(text)
                self.assertPrecondition("exactly once")

    def test_symlinked_or_writable_directories_and_files_are_refused(self):
        self.ship.chmod(0o775)
        self.assertPrecondition("not trusted")
        self.ship.chmod(0o755)
        moved = self.root / "real-ship"
        self.ship.rename(moved)
        self.ship.symlink_to(moved)
        self.assertPrecondition("not bootstrapped")
        self.ship.unlink()
        moved.rename(self.ship)
        (self.root / "Caddyfile").chmod(0o666)
        with self.lock.acquire(), self.assertRaises(RoutingError):
            self.manager.apply(registration())
        (self.root / "Caddyfile").chmod(0o644)
        target = self.root / "elsewhere"
        target.write_text("billing.example.com {\n}\n")
        (self.ship / "billing.caddy").symlink_to(target)
        with self.lock.acquire(), self.assertRaises(RoutingError):
            self.manager.apply(registration())

    def test_relative_root_is_refused(self):
        with self.assertRaises(RoutingError):
            RouteManager(Path("relative"), self.lock, self.caddy)


class EnsureImportTests(RoutingTestCase):
    def setUp(self):
        super().setUp()
        self.write_main(self.MAIN)
        self.line = f"import {self.root}/ship/*.caddy\n"

    def test_appends_the_import_once_preserving_existing_content(self):
        with self.lock.acquire():
            self.assertTrue(self.manager.ensure_import().changed)
            self.assertEqual((self.root / "Caddyfile").read_text(), self.MAIN + self.line)
            self.assertFalse(self.manager.ensure_import().changed)
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertIn(self.line, self.caddy.validate_calls[0]["text"])
        self.assertNoTransactionLeftovers()
        self.assertEqual(stat.S_IMODE((self.root / "Caddyfile").stat().st_mode), 0o644)

    def test_adds_a_newline_when_the_file_does_not_end_with_one(self):
        self.write_main("example.org {\n}")
        with self.lock.acquire():
            self.manager.ensure_import()
        self.assertEqual((self.root / "Caddyfile").read_text(), "example.org {\n}\n" + self.line)

    def test_rewriting_the_caddyfile_preserves_its_owner_and_mode(self):
        path = self.root / "Caddyfile"
        path.chmod(0o640)
        before = path.stat()
        with self.lock.acquire():
            self.assertTrue(self.manager.ensure_import().changed)
        after = path.stat()
        self.assertEqual((stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid),
                         (0o640, before.st_uid, before.st_gid))

    def test_a_caddyfile_whose_owner_cannot_be_preserved_is_not_replaced(self):
        path = self.root / "Caddyfile"
        path.chmod(0o640)
        before = self.snapshot()
        with patch("shiplord.routing.os.fchown", side_effect=OSError(1, "operation not permitted")):
            with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
                self.manager.ensure_import()
        self.assertEqual(caught.exception.kind, "precondition")
        self.assertEqual((self.snapshot(), self.mode(path)), (before, 0o640))
        self.assertEqual(self.caddy.reload_calls, 0)
        self.assertNoTransactionLeftovers()

    def test_duplicate_imports_and_missing_files_are_refused(self):
        self.write_main(self.MAIN + self.line + self.line)
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "more than once"):
            self.manager.ensure_import()
        (self.root / "Caddyfile").unlink()
        with self.lock.acquire(), self.assertRaisesRegex(RoutingError, "missing"):
            self.manager.ensure_import()

    def test_validation_failure_leaves_the_caddyfile_untouched(self):
        self.caddy.fail_validate = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertEqual(caught.exception.kind, "validation")
        self.assertEqual((self.snapshot(), self.caddy.reload_calls), (before, 0))

    def test_an_unavailable_caddy_is_not_reported_as_a_rejected_caddyfile(self):
        self.caddy.unavailable_validate = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertNotIn("rejected", str(caught.exception))
        self.assertEqual((self.snapshot(), self.caddy.reload_calls), (before, 0))

    def test_reload_failure_restores_the_caddyfile_and_reloads_again(self):
        self.caddy.fail_reload = {1}
        before = self.snapshot()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertFalse(caught.exception.intervention_required)
        self.assertEqual((self.snapshot(), self.caddy.reload_calls), (before, 2))
        self.assertNoTransactionLeftovers()

    def write_prior(self, text):
        path = self.root / ".prior-Caddyfile"
        path.write_text(text)
        path.chmod(0o644)
        return path

    def test_an_unmatched_prior_and_a_missing_lock_are_refused(self):
        with self.assertRaisesRegex(RoutingError, "lock"):
            self.manager.ensure_import()
        self.write_prior("old")
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertTrue(caught.exception.intervention_required)

    def test_a_crash_before_the_publish_drops_the_prior_and_appends_the_import(self):
        # Crash between the link and the replace: the live Caddyfile is still the prior, hard link intact.
        os.link(self.root / "Caddyfile", self.root / ".prior-Caddyfile")
        with self.lock.acquire():
            self.assertTrue(self.manager.ensure_import().changed)
            self.assertEqual((self.root / "Caddyfile").read_text(), self.MAIN + self.line)
        self.assertEqual(self.caddy.reload_calls, 1)
        self.assertNoTransactionLeftovers()

    def test_a_crash_after_the_publish_reports_the_import_as_present(self):
        # Crash between the replace and the unlink: the live Caddyfile is the candidate, the prior the old file.
        self.write_main(self.MAIN + self.line)
        self.write_prior(self.MAIN)
        with self.lock.acquire():
            self.assertFalse(self.manager.ensure_import().changed)
            self.assertEqual((self.root / "Caddyfile").read_text(), self.MAIN + self.line)
        self.assertEqual((self.caddy.validate_calls, self.caddy.reload_calls), ([], 0))
        self.assertNoTransactionLeftovers()

    def test_a_prior_that_cannot_be_proven_redundant_is_refused_with_the_recovery_step(self):
        cases = {
            "a different Caddyfile": lambda: self.write_prior("example.net {\n}\n"),
            "a prior that imports the managed routes": lambda: self.write_prior(self.MAIN + self.line),
            "a symbolic link": lambda: (self.root / ".prior-Caddyfile").symlink_to(self.root / "Caddyfile"),
            "a directory": lambda: (self.root / ".prior-Caddyfile").mkdir(),
            "a prior another account can write": lambda: self.write_prior(self.MAIN).chmod(0o666),
        }
        for name, leave in cases.items():
            with self.subTest(name):
                leave()
                before = (self.root / "Caddyfile").read_bytes()
                with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
                    self.manager.ensure_import()
                error = caught.exception
                self.assertEqual(error.kind, "interrupted")
                self.assertTrue(error.intervention_required)
                self.assertIn(".prior-Caddyfile", str(error))
                self.assertIn("compare .prior-Caddyfile with the Caddyfile", str(error))
                self.assertIn("remove .prior-Caddyfile", str(error))
                self.assertEqual((self.root / "Caddyfile").read_bytes(), before)
                self.assertTrue(os.path.lexists(self.root / ".prior-Caddyfile"))
                self.assertEqual((self.caddy.validate_calls, self.caddy.reload_calls), ([], 0))
                prior = self.root / ".prior-Caddyfile"
                if prior.is_dir() and not prior.is_symlink():
                    prior.rmdir()
                else:
                    prior.unlink()

    def test_a_crash_after_the_publish_reconstructs_a_missing_final_newline(self):
        self.write_main("example.org {\n}\n" + self.line)
        self.write_prior("example.org {\n}")
        with self.lock.acquire():
            self.assertFalse(self.manager.ensure_import().changed)
        self.assertNoTransactionLeftovers()

    def test_an_identical_pair_that_already_imports_is_refused(self):
        # The transaction only links a Caddyfile that lacks the import, so this pair was not produced by a crash.
        self.write_main(self.MAIN + self.line)
        self.write_prior(self.MAIN + self.line)
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertTrue(caught.exception.intervention_required)
        self.assertIn(".prior-Caddyfile", str(caught.exception))

    def test_a_missing_caddyfile_beside_a_prior_is_refused(self):
        self.write_prior(self.MAIN)
        (self.root / "Caddyfile").unlink()
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertEqual(caught.exception.kind, "interrupted")
        self.assertTrue(caught.exception.intervention_required)
        self.assertIn(".prior-Caddyfile", str(caught.exception))
        self.assertFalse((self.root / "Caddyfile").exists())
        self.assertTrue((self.root / ".prior-Caddyfile").exists())

    def test_an_untrusted_caddyfile_beside_a_prior_is_refused(self):
        self.write_prior(self.MAIN)
        (self.root / "Caddyfile").chmod(0o666)
        with self.lock.acquire(), self.assertRaises(RoutingError) as caught:
            self.manager.ensure_import()
        self.assertEqual(caught.exception.kind, "interrupted")
        self.assertTrue(caught.exception.intervention_required)
        self.assertIn(".prior-Caddyfile", str(caught.exception))
        self.assertTrue((self.root / ".prior-Caddyfile").exists())


class TokenizerTests(unittest.TestCase):
    def test_the_quoted_flag_marks_only_the_quoted_tokens(self):
        first = next(routing._lines('a.com "b c" { # comment\n'))
        self.assertEqual((first[1], first[2]), (["a.com", "b c", "{"], [False, True, False]))


class SubprocessCaddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def script(self, name, body):
        path = self.directory / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    def test_validate_and_reload_run_fixed_argv_without_a_shell(self):
        log = self.directory / "log"
        caddy = self.script("caddy", f'echo "$@" > {log}\n')
        systemctl = self.script("systemctl", f'echo "$@" >> {log}\n')
        runner = SubprocessCaddy(caddy, systemctl)
        runner.validate(Path("/etc/caddy/with space; rm -rf x"))
        runner.reload()
        self.assertEqual(log.read_text().splitlines(),
                         ["validate --config /etc/caddy/with space; rm -rf x --adapter caddyfile", "reload caddy"])

    def test_environment_is_minimal_and_fixed(self):
        out = self.directory / "env"
        caddy = self.script("caddy", f'env > {out}\n')
        os.environ["SHIPLORD_SECRET_TEST"] = "do-not-leak"
        self.addCleanup(os.environ.pop, "SHIPLORD_SECRET_TEST", None)
        SubprocessCaddy(caddy).validate(Path("/x"))
        names = {line.split("=", 1)[0] for line in out.read_text().splitlines()}
        self.assertNotIn("SHIPLORD_SECRET_TEST", names)
        self.assertIn("PATH", names)

    def test_validation_writes_caddy_storage_to_a_throwaway_home_never_the_working_directory(self):
        out = self.directory / "home"
        caddy = self.script("caddy", f'echo "$HOME|$XDG_DATA_HOME" > {out}; mkdir "$HOME/caddy"\n')
        SubprocessCaddy(caddy).validate(Path("/x"))
        home, data = out.read_text().strip().split("|")
        self.assertEqual(home, data)
        self.assertIn("shiplord-caddy-validate-", home)
        self.assertFalse(Path(home).exists())  # removed afterwards, with anything Caddy wrote there

    def test_failure_exposes_capped_clean_detail_but_not_in_the_message(self):
        caddy = self.script("caddy", 'head -c 200000 /dev/zero | tr "\\0" x; printf "bad\\033[31m config\\n"; exit 3\n')
        with self.assertRaises(RoutingError) as caught:
            SubprocessCaddy(caddy).validate(Path("/x"))
        error = caught.exception
        self.assertEqual(error.kind, "validation")
        self.assertLessEqual(len(error.detail), 4096)
        self.assertTrue(error.detail.endswith("bad?[31m config\n"))
        self.assertNotIn("bad", str(error))

    def test_timeout_kills_the_process_and_reports_it(self):
        marker = self.directory / "survived"
        caddy = self.script("caddy", f"sleep 5\ntouch {marker}\n")
        started = __import__("time").monotonic()
        with self.assertRaisesRegex(RoutingError, "timed out") as caught:
            SubprocessCaddy(caddy, timeout=0.5).validate(Path("/x"))
        self.assertEqual(caught.exception.kind, "validation")  # a slow Caddy is not an absent one
        self.assertLess(__import__("time").monotonic() - started, 4)
        self.assertFalse(marker.exists())

    def test_missing_executable_is_unavailable_not_a_rejected_configuration(self):
        absent = str(self.directory / "absent")
        with self.assertRaises(RoutingError) as caught:
            SubprocessCaddy(absent).validate(Path("/x"))
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertIn(absent, str(caught.exception))
        self.assertNotIn("rejected", str(caught.exception))

    def test_reload_failure_has_reload_kind(self):
        systemctl = self.script("systemctl", "exit 1\n")
        with self.assertRaises(RoutingError) as caught:
            SubprocessCaddy("/bin/true", systemctl).reload()
        self.assertEqual(caught.exception.kind, "reload")

    def test_validation_receives_the_environment_the_caddy_service_runs_with(self):
        out = self.directory / "token"
        caddy = self.script("caddy", f'printf "%s" "$CF_API_TOKEN" > {out}\n')
        SubprocessCaddy(caddy, service_environment=lambda: {"CF_API_TOKEN": "fake-token-value"}).validate(Path("/x"))
        self.assertEqual(out.read_text(), "fake-token-value")

    def test_the_throwaway_directories_override_ones_the_service_sets(self):
        out = self.directory / "home"
        caddy = self.script("caddy", f'printf "%s|%s|%s" "$HOME" "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" > {out}\n')
        service = {"HOME": "/var/lib/caddy", "XDG_DATA_HOME": "/var/lib/caddy", "XDG_CONFIG_HOME": "/var/lib/caddy"}
        SubprocessCaddy(caddy, service_environment=lambda: service).validate(Path("/x"))
        home, data, config = out.read_text().split("|")
        self.assertEqual((home, config), (data, data))
        self.assertIn("shiplord-caddy-validate-", home)
        self.assertFalse(Path(home).exists())  # removed afterwards, with anything Caddy wrote there

    def test_a_service_variable_overrides_the_fixed_defaults(self):
        out = self.directory / "lang"
        caddy = self.script("caddy", f'printf "%s" "$LANG" > {out}\n')
        SubprocessCaddy(caddy, service_environment=lambda: {"LANG": "C.UTF-8"}).validate(Path("/x"))
        self.assertEqual(out.read_text(), "C.UTF-8")

    def test_an_unreadable_service_environment_keeps_the_fixed_environment(self):
        out = self.directory / "env"
        caddy = self.script("caddy", f'env > {out}\n')
        os.environ["SHIPLORD_SECRET_TEST"] = "do-not-leak"
        self.addCleanup(os.environ.pop, "SHIPLORD_SECRET_TEST", None)
        SubprocessCaddy(caddy, service_environment=lambda: None).validate(Path("/x"))
        names = {line.split("=", 1)[0] for line in out.read_text().splitlines()}
        self.assertNotIn("SHIPLORD_SECRET_TEST", names)
        self.assertEqual(names & {"PATH", "LANG", "HOME"}, {"PATH", "LANG", "HOME"})

    def test_service_values_are_removed_from_captured_output_but_reach_the_child(self):
        out = self.directory / "token"
        token = "fake-token-value"
        caddy = self.script("caddy", f'printf "%s" "$CF_API_TOKEN" > {out}\nprintf "failed for {token}\\n"\nexit 3\n')
        with self.assertRaises(RoutingError) as caught:
            SubprocessCaddy(caddy, service_environment=lambda: {"CF_API_TOKEN": token}).validate(Path("/x"))
        self.assertEqual(out.read_text(), token)  # the value is what validation is for
        self.assertNotIn(token, caught.exception.detail)
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("[shiplord:redacted]", caught.exception.detail)


class FakeSystem:
    """Answers the systemd probe with scripted output, and records the command it was asked to run."""

    def __init__(self, output="", code=0, error=None):
        self.output, self.code, self.error = output, code, error
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.error is not None:
            raise self.error
        return CommandResult(self.code, self.output)


class ServiceEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_the_unit_probe_asks_for_the_two_documented_properties(self):
        system = FakeSystem()
        routing.service_environment(system, ())
        self.assertEqual(system.calls, [["/usr/bin/systemctl", "show", "caddy",
                                         "--property=Environment", "--property=EnvironmentFiles"]])

    def test_environment_words_honour_quotes_and_escapes(self):
        self.assertEqual(routing.parse_environment_list(r'A=1 B="two words" C=three\ four D="line\nbreak"'),
                         {"A": "1", "B": "two words", "C": "three four", "D": "line\nbreak"})
        self.assertEqual(routing.parse_environment_list(""), {})

    def test_environment_files_drop_comments_and_malformed_lines(self):
        text = ('# comment\n; also a comment\nA=1\nB="two words"\nC=\'literal $x\'\nexport D=4\n'
                'E="unterminated\nF=has space\nG=\n')
        self.assertEqual(routing.parse_environment_file(text),
                         {"A": "1", "B": "two words", "C": "literal $x", "F": "has space", "G": ""})

    def test_the_unit_environment_and_its_environment_files_are_applied(self):
        env_file = self.directory / "caddy.env"
        env_file.write_text("FROM_FILE=yes\n")
        for property in ("EnvironmentFiles", "EnvironmentFile"):  # the singular is what older systemctl printed
            with self.subTest(property):
                system = FakeSystem(f"Environment=FROM_UNIT=1\n{property}={env_file} (ignore_errors=no)\n")
                self.assertEqual(routing.service_environment(system, ()), {"FROM_UNIT": "1", "FROM_FILE": "yes"})

    def test_a_relative_environment_file_is_ignored(self):
        system = FakeSystem("EnvironmentFiles=caddy.env (ignore_errors=no)\n")
        self.assertEqual(routing.service_environment(system, ()), {})

    def test_a_failed_or_truncated_systemd_answer_falls_back_to_the_packaged_defaults(self):
        defaults = self.directory / "default-caddy"
        defaults.write_text("PACKAGED=yes\n")
        for label, system in {
            "non-zero exit": FakeSystem("Environment=PARTIAL=1\n", code=1),
            "truncated": FakeSystem(TRUNCATION_MARKER + "Environment=PARTIAL=1\n"),
            "cannot run": FakeSystem(error=CommandError("could not start the command", "start")),
        }.items():
            with self.subTest(label):
                self.assertEqual(routing.service_environment(system, (defaults,)), {"PACKAGED": "yes"})

    def test_no_readable_source_yields_no_variables(self):
        self.assertEqual(routing.service_environment(FakeSystem("", code=1), (self.directory / "absent",)), {})

    def test_a_source_that_is_not_a_regular_file_is_ignored(self):
        self.assertEqual(routing.service_environment(FakeSystem(), (self.directory,)), {})

    def test_the_process_environment_is_never_read(self):
        os.environ["SHIPLORD_SECRET_TEST"] = "do-not-leak"
        self.addCleanup(os.environ.pop, "SHIPLORD_SECRET_TEST", None)
        self.assertEqual(routing.service_environment(FakeSystem("Environment=\nEnvironmentFiles=\n"), ()), {})


class ProbeTests(unittest.TestCase):
    def test_the_probe_validates_a_fixed_minimal_configuration(self):
        seen = {}

        class Recorder:
            def validate(self, config):
                seen["path"], seen["text"] = config, config.read_text()

            def reload(self):
                raise AssertionError("the probe must not reload")

        probe_caddy(Recorder())
        self.assertEqual(seen["text"], routing.CADDY_PROBE.decode())
        self.assertFalse(seen["path"].exists())  # the throwaway file is removed, never left on the host

    def test_the_probe_reports_an_absent_binary_as_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            absent = str(Path(directory) / "absent")
            with self.assertRaises(RoutingError) as caught:
                probe_caddy(SubprocessCaddy(absent))
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertIn(absent, str(caught.exception))

    def test_the_probe_propagates_any_other_refusal(self):
        class Broken:
            def validate(self, config):
                raise RoutingError("Caddy command failed", "validation", detail="bad config")

            def reload(self):
                raise AssertionError("the probe must not reload")

        with self.assertRaises(RoutingError) as caught:
            probe_caddy(Broken())
        self.assertEqual(caught.exception.kind, "validation")


if __name__ == "__main__":
    unittest.main()
