from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import REGISTRATION, record  # noqa: E402
from shiplord.provision import ProvisionError  # noqa: E402
from shiplord.retire import make_retirer  # noqa: E402
from shiplord.routing import RoutingError  # noqa: E402
from shiplord.runtime import RuntimeFailure  # noqa: E402


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.order = []
        self.runtime = MagicMock()
        self.runtime.stop.side_effect = lambda registration: self.order.append("stop")
        self.runtime.discard.side_effect = lambda registration, release: self.order.append(("discard", release["commit"][0]))
        patcher = patch("shiplord.retire.RouteManager")
        self.routes = patcher.start().return_value
        self.routes.remove.side_effect = lambda app: self.order.append(("unroute", app))
        self.addCleanup(patcher.stop)
        self.retire = make_retirer(MagicMock(caddy_root=Path("/x")), MagicMock(), MagicMock(), MagicMock(),
                                   runtime_for=lambda registration: self.runtime)
        self.state = {"current": record(commit="a"), "previous": record(commit="b")}

    def test_the_route_goes_first_then_the_runtime_then_both_releases_are_discarded(self):
        self.retire(REGISTRATION, self.state)
        self.assertEqual(self.order, [("unroute", "orders"), "stop", ("discard", "a"), ("discard", "b")])

    def test_a_missing_previous_release_is_skipped(self):
        self.retire(REGISTRATION, dict(self.state, previous=None))
        self.assertEqual(self.order, [("unroute", "orders"), "stop", ("discard", "a")])

    def test_failures_become_provision_errors_and_nothing_is_discarded(self):
        for failing, error in ((self.routes.remove, RoutingError("bad", "validation")),
                               (self.runtime.stop, RuntimeFailure("stuck", "stop"))):
            with self.subTest(error=type(error).__name__):
                self.order.clear()
                failing.side_effect = error
                with self.assertRaisesRegex(ProvisionError, "could not retire"):
                    self.retire(REGISTRATION, self.state)
                self.assertNotIn(("discard", "a"), self.order)

    def test_a_missing_release_state_retires_without_a_traceback_and_reports_what_is_kept(self):
        report = self.retire(REGISTRATION, None)
        self.assertEqual(self.order, [("unroute", "orders"), "stop"])
        self.assertEqual(report["removed"], ["public route", "running release"])
        self.assertIn("application data (the data directory and its volume)", report["retained"])
        self.assertIn("release artifacts (no release state was recorded, so none were discarded)", report["retained"])

    def test_an_empty_release_state_retires_without_a_traceback(self):
        report = self.retire(REGISTRATION, {})
        self.assertEqual(self.order, [("unroute", "orders"), "stop"])
        self.assertEqual(report["removed"], ["public route", "running release"])

    def test_an_unusable_release_state_is_refused_before_anything_changes(self):
        for state in ("corrupt", ["current"], {"current": "corrupt"}, {"previous": 1}):
            with self.subTest(state=state):
                self.order.clear()
                with self.assertRaisesRegex(ProvisionError, "release state is unusable, so nothing was changed") as caught:
                    self.retire(REGISTRATION, state)
                self.assertEqual(self.order, [])
                self.assertNotIn("/", str(caught.exception))

    def test_the_report_lists_each_discarded_release_only_when_it_was_verified(self):
        report = self.retire(REGISTRATION, self.state)
        self.assertEqual(report["removed"], ["public route", "running release", "the current release's artifacts",
                                             "the previous release's artifacts"])
        self.assertEqual(report["retained"], ["application data (the data directory and its volume)"])

    def test_a_discard_warning_is_reported_as_kept_instead_of_removed(self):
        warning = "an old image could not be removed (it may still be in use)"
        self.runtime.discard.side_effect = lambda registration, release: (
            [warning] if release["commit"][0] == "a" else [])
        report = self.retire(REGISTRATION, self.state)
        self.assertEqual(report["removed"], ["public route", "running release", "the previous release's artifacts"])
        self.assertIn(warning, report["retained"])


if __name__ == "__main__":
    unittest.main()
