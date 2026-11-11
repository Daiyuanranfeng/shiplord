from __future__ import annotations

import base64
import json
import re
import stat
import tempfile
import unittest
from pathlib import Path

from shiplord import tailscale
from shiplord.tailscale import (API_KEYS_URL, API_TOKEN_ENV, TailscaleError, api_token, create_federated_identity,
                                guidance, load_defaults, resolve, subjects, validate)

CLIENT = "TJYVecfQJf11CNTRL-kEhAeizRMt11CNTRL"
AUDIENCE = f"api.tailscale.com/{CLIENT}"
TOKEN = "tskey-api-throwaway-fixture-value"


def matches(pattern: str, value: str) -> bool:
    """Tailscale's documented claim matching: a subject value may contain ``*`` to match any character."""
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), value) is not None


class FakeTransport:
    """A stand-in for the HTTPS request; records what would have been sent and never opens a socket."""

    def __init__(self, status: int = 200, body: dict | bytes | None = None, error: Exception | None = None):
        self.status, self.error = status, error
        self.body = body if body is not None else {"id": CLIENT, "audience": AUDIENCE}
        self.requests: list[tuple[str, dict[str, str], bytes]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        self.requests.append((url, headers, body))
        if self.error is not None:
            raise self.error
        return self.status, self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


class SubjectTests(unittest.TestCase):
    def test_immutable_and_classic_prefixes_produce_repository_and_owner_wide_subjects(self):
        self.assertEqual(subjects("repo:QueryPlanner@86554769/hello@1437016276", "main"),
                         {"repository": "repo:QueryPlanner@86554769/hello@1437016276:ref:refs/heads/main",
                          "all_repositories": "repo:QueryPlanner@86554769/*:ref:refs/heads/main"})
        self.assertEqual(subjects("repo:octo/app", "release/prod")["all_repositories"],
                         "repo:octo/*:ref:refs/heads/release/prod")

    def test_an_unexpected_prefix_is_refused_rather_than_guessed(self):
        for prefix in ("", "octo/app", "repo:octo", "repo:a/b/c", "repo:a/b:extra"):
            with self.subTest(prefix=prefix), self.assertRaises(TailscaleError):
                subjects(prefix, "main")

    def test_guidance_fills_in_every_value(self):
        text = " ".join(guidance("repo:octo/app", "main")["steps"])
        for expected in ("tag:ci", "autogroup:admin", tailscale.TRUST_CREDENTIALS_URL, tailscale.GITHUB_ISSUER,
                         "repo:octo/*:ref:refs/heads/main", "repo:octo/app:ref:refs/heads/main", "Auth Keys"):
            self.assertIn(expected, text)
        self.assertNotIn("warning", guidance("repo:octo/app", "main"))


class ValidationTests(unittest.TestCase):
    def test_values_from_the_console_are_accepted_and_trimmed(self):
        settings, warnings = validate(f"  {CLIENT} ", f"{AUDIENCE}\n")
        self.assertEqual(settings, {"SHIPLORD_TS_CLIENT_ID": CLIENT, "SHIPLORD_TS_AUDIENCE": AUDIENCE})
        self.assertEqual(warnings, [])

    def test_an_audience_from_a_different_credential_is_warned_about(self):
        self.assertTrue(validate(CLIENT, "api.tailscale.com/other-credential")[1])

    def test_malformed_values_are_refused(self):
        for client, audience in (("", AUDIENCE), ("has space", AUDIENCE), ("x", AUDIENCE), (CLIENT, ""),
                                 (CLIENT, "two words"), (CLIENT, "a" * 513)):
            with self.subTest(client=client, audience=audience[:20]), self.assertRaises(TailscaleError):
                validate(client, audience)


class WildcardSubjectTests(unittest.TestCase):
    """Records what the owner-wide subject is expected to match. This is not a real-Tailscale verification.

    Issue #48 asks whether ``repo:Owner@id/*:ref:refs/heads/main`` covers a push from a second repository. The
    matcher below encodes Tailscale's documented rule ("values can contain an `*` to match against any character"),
    so a failure here would show the expectation, not the service. Only a ``check-ci`` run from a second repository
    can confirm the real behaviour.
    """

    def test_the_owner_wide_subject_covers_a_second_repository_of_the_same_owner(self):
        immutable = subjects("repo:QueryPlanner@86554769/hello@1437016276", "main")["all_repositories"]
        classic = subjects("repo:QueryPlanner/hello", "main")["all_repositories"]
        self.assertTrue(matches(immutable, "repo:QueryPlanner@86554769/other@999:ref:refs/heads/main"))
        self.assertTrue(matches(classic, "repo:QueryPlanner/other:ref:refs/heads/main"))
        # A repository with immutable subjects presents a different owner form, so the classic pattern misses it.
        self.assertFalse(matches(immutable, "repo:QueryPlanner/other:ref:refs/heads/main"))

    def test_the_owner_wide_subject_does_not_cover_another_owner_or_branch(self):
        owner_wide = subjects("repo:octo/app", "main")["all_repositories"]
        self.assertFalse(matches(owner_wide, "repo:someone-else/app:ref:refs/heads/main"))
        self.assertFalse(matches(owner_wide, "repo:octo/app:ref:refs/heads/release"))

    def test_the_repository_only_subject_does_not_cover_a_second_repository(self):
        repository_only = subjects("repo:octo/app", "main")["repository"]
        self.assertFalse(matches(repository_only, "repo:octo/other:ref:refs/heads/main"))

    def test_the_wildcard_stays_in_the_middle_until_real_tailscale_evidence_exists(self):
        self.assertEqual(subjects("repo:QueryPlanner@86554769/hello@1437016276", "main")["all_repositories"],
                         "repo:QueryPlanner@86554769/*:ref:refs/heads/main")


class ApiTokenTests(unittest.TestCase):
    def test_the_token_is_read_from_the_documented_variable_and_blank_values_are_absent(self):
        self.assertEqual(API_TOKEN_ENV, "TAILSCALE_API_KEY")
        self.assertEqual(api_token({API_TOKEN_ENV: f"  {TOKEN}\n"}), TOKEN)
        self.assertIsNone(api_token({API_TOKEN_ENV: "   "}))
        self.assertIsNone(api_token({}))


class CreateFederatedIdentityTests(unittest.TestCase):
    def test_the_request_carries_the_issuer_subject_scopes_and_tag_for_this_single_repository(self):
        transport = FakeTransport()
        settings, warnings = create_federated_identity(
            TOKEN, subject="repo:QueryPlanner@86554769/*:ref:refs/heads/main", tags=["tag:ci"],
            description="shiplord: QueryPlanner/orders", transport=transport)
        self.assertEqual(settings, {"SHIPLORD_TS_CLIENT_ID": CLIENT, "SHIPLORD_TS_AUDIENCE": AUDIENCE})
        self.assertEqual(warnings, [])
        (url, headers, body), = transport.requests
        self.assertEqual(url, API_KEYS_URL)
        self.assertEqual(json.loads(body), {
            "keyType": "federated", "description": "shiplord: QueryPlanner/orders", "scopes": ["auth_keys"],
            "tags": ["tag:ci"], "issuer": tailscale.GITHUB_ISSUER,
            "subject": "repo:QueryPlanner@86554769/*:ref:refs/heads/main"})
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(f"{TOKEN}:".encode()).decode())
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_a_generated_audience_for_another_client_is_reported_as_a_warning(self):
        settings, warnings = create_federated_identity(
            TOKEN, subject="repo:octo/app:ref:refs/heads/main", tags=["tag:ci"], description="d",
            transport=FakeTransport(body={"id": CLIENT, "audience": "api.tailscale.com/other"}))
        self.assertEqual(settings["SHIPLORD_TS_CLIENT_ID"], CLIENT)
        self.assertTrue(warnings)

    def test_every_failure_is_described_without_the_token_or_the_response_body(self):
        transports = [FakeTransport(401), FakeTransport(500),
                      FakeTransport(200, body={"message": f"bad {TOKEN}"}),
                      FakeTransport(200, body={"id": "short", "audience": AUDIENCE}),
                      FakeTransport(200, body={"id": CLIENT}),
                      FakeTransport(200, body=b"not json")]
        for transport in transports:
            with self.subTest(status=transport.status, body=transport.body), \
                    self.assertRaises(TailscaleError) as caught:
                create_federated_identity(TOKEN, subject="repo:octo/app:ref:refs/heads/main", tags=["tag:ci"],
                                          description="d", transport=transport)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_an_unreachable_api_is_reported_without_the_token(self):
        with self.assertRaises(TailscaleError) as caught:
            create_federated_identity(TOKEN, subject="repo:octo/app:ref:refs/heads/main", tags=["tag:ci"],
                                      description="d", transport=FakeTransport(error=OSError("network down")))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_unusable_values_are_refused_before_any_request_is_made(self):
        cases = [("", "repo:octo/app:ref:refs/heads/main", ["tag:ci"]),
                 ("has space", "repo:octo/app:ref:refs/heads/main", ["tag:ci"]),
                 (TOKEN, "", ["tag:ci"]),
                 (TOKEN, "repo:octo/app ref:refs/heads/main", ["tag:ci"]),
                 (TOKEN, "repo:octo/app:ref:refs/heads/main", []),
                 (TOKEN, "repo:octo/app:ref:refs/heads/main", ["ci"])]
        for token, subject, tags in cases:
            transport = FakeTransport()
            with self.subTest(token=token, subject=subject, tags=tags), self.assertRaises(TailscaleError):
                create_federated_identity(token, subject=subject, tags=tags, description="d", transport=transport)
            self.assertEqual(transport.requests, [])


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_options_win_and_are_saved_privately_for_the_next_application(self):
        settings, origin = resolve({"SHIPLORD_TS_CLIENT_ID": CLIENT, "SHIPLORD_TS_AUDIENCE": AUDIENCE}, home=self.home)
        self.assertEqual((settings["SHIPLORD_TS_CLIENT_ID"], origin["source"]), (CLIENT, "options"))
        path = self.home / "shiplord" / "tailscale.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {"client_id": CLIENT, "audience": AUDIENCE})
        again, origin = resolve({}, home=self.home, ask=lambda g: self.fail("must not ask"), guide=dict)
        self.assertEqual((again["SHIPLORD_TS_AUDIENCE"], origin["source"]), (AUDIENCE, "saved from an earlier setup"))

    def test_only_one_of_the_two_options_is_an_error(self):
        with self.assertRaisesRegex(TailscaleError, "both"):
            resolve({"SHIPLORD_TS_CLIENT_ID": CLIENT}, home=self.home)

    def test_the_user_is_shown_the_guide_then_asked_and_may_skip(self):
        shown = []
        settings, origin = resolve({}, home=self.home, guide=lambda: {"steps": ["x"]},
                                   ask=lambda g: shown.append(g) or None)
        self.assertEqual((settings, origin["source"], shown), ({}, "skipped", [{"steps": ["x"]}]))
        self.assertIsNone(load_defaults(self.home))
        settings, origin = resolve({"SHIPLORD_TS_TAGS": "tag:deploy"}, home=self.home, guide=dict,
                                   ask=lambda g: (CLIENT, AUDIENCE))
        self.assertEqual((settings["SHIPLORD_TS_TAGS"], origin["source"]), ("tag:deploy", "entered"))
        self.assertIsNotNone(load_defaults(self.home))

    def test_without_a_terminal_nothing_is_asked_and_nothing_is_configured(self):
        self.assertEqual(resolve({}, home=self.home), ({}, {"source": "not configured", "warnings": []}))

    def test_the_api_creates_the_credential_before_the_user_is_asked_and_the_result_is_saved(self):
        asked = []
        created = ({"SHIPLORD_TS_CLIENT_ID": CLIENT, "SHIPLORD_TS_AUDIENCE": AUDIENCE}, [])
        settings, origin = resolve({}, home=self.home, ask=lambda guide: asked.append(guide), guide=dict,
                                   provision=lambda: created)
        self.assertEqual((settings["SHIPLORD_TS_CLIENT_ID"], origin["source"]),
                         (CLIENT, "created through the Tailscale API"))
        self.assertEqual(asked, [])
        self.assertEqual(load_defaults(self.home), created[0])

    def test_saved_defaults_win_over_creating_a_second_credential(self):
        resolve({"SHIPLORD_TS_CLIENT_ID": CLIENT, "SHIPLORD_TS_AUDIENCE": AUDIENCE}, home=self.home)
        settings, origin = resolve({}, home=self.home,
                                   provision=lambda: self.fail("must not create another credential"))
        self.assertEqual((settings["SHIPLORD_TS_CLIENT_ID"], origin["source"]), (CLIENT, "saved from an earlier setup"))

    def test_without_a_token_the_guide_is_shown_and_the_user_may_still_enter_values(self):
        settings, origin = resolve({}, home=self.home, provision=lambda: None, guide=dict,
                                   ask=lambda guide: (CLIENT, AUDIENCE))
        self.assertEqual((settings["SHIPLORD_TS_CLIENT_ID"], origin["source"]), (CLIENT, "entered"))

    def test_a_damaged_saved_file_is_ignored(self):
        (self.home / "shiplord").mkdir()
        (self.home / "shiplord" / "tailscale.json").write_text("{not json")
        self.assertIsNone(load_defaults(self.home))


if __name__ == "__main__":
    unittest.main()
