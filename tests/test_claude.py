import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from ai_watch import claude
from ai_watch.common import Profile, UsageError


def quota(percent=25):
    return {"windows": [{"name": "session", "used_percent": percent, "resets_at": None}]}


class ClaudeParsingTests(unittest.TestCase):
    def test_legacy_zero_and_model_windows(self):
        result = claude.parse_limits({
            "five_hour": {"utilization": 0, "resets_at": "2026-10-01T00:00:00Z"},
            "seven_day": {"utilization": 72, "resets_at": None},
            "seven_day_sonnet": {"utilization": 10, "resets_at": None},
            "seven_day_opus": None,
        })
        self.assertEqual([w["used_percent"] for w in result["windows"]], [0, 72, 10])
        self.assertIsInstance(result["windows"][0]["resets_at"], float)

    def test_structured_windows_take_precedence(self):
        result = claude.parse_limits({"limits": [
            {"kind": "weekly_scoped", "percent": 42, "scope": {"model": {"display_name": "Sonnet"}}},
            {"kind": "future_kind", "percent": 1},
        ], "five_hour": {"utilization": 99}})
        self.assertEqual([w["name"] for w in result["windows"]], ["week (Sonnet)"])

    def test_invalid_responses_do_not_become_zero(self):
        values = [None, [], {}, {"error": "secret-body"}, {"limits": {}},
                  {"five_hour": "bad"}]
        values += [{"five_hour": {"utilization": v}} for v in (None, True, -1, float("nan"), float("inf"), "3")]
        values += [{"five_hour": {"utilization": 5, "resets_at": v}}
                   for v in ("bad-date", "2026-10-01T00:00:00", 123, [])]
        for value in values:
            with self.subTest(value=value), self.assertRaises(UsageError):
                claude.parse_limits(value)

    def test_retry_after_seconds_date_and_invalid(self):
        self.assertEqual(claude.retry_time("300", 0), 300)
        self.assertEqual(claude.retry_time("Thu, 01 Jan 1970 00:10:00 GMT", 0), 600)
        for value in (None, "invalid", "nan", "inf", "-1"):
            with self.subTest(value=value):
                self.assertEqual(claude.retry_time(value, 100), 160)

    def test_http_errors_hide_response_bodies_and_honor_retry_after(self):
        for status in (401, 403, 429, 500, 302):
            error = HTTPError(claude.USAGE_URL, status, "secret-message", {"Retry-After": "300"}, io.BytesIO(b"secret-body"))
            opener = Mock()
            opener.open.side_effect = error
            with patch("ai_watch.claude.build_opener", return_value=opener), patch("time.time", return_value=100):
                result = claude.fetch_usage({"accessToken": "fake-token"}, 1)
            self.assertIn("error", result)
            self.assertNotIn("secret", json.dumps(result))
            if status == 429:
                self.assertEqual(result["retry_at"], 400)

    def test_request_headers_and_normalized_output(self):
        response = io.BytesIO(json.dumps({"five_hour": {"utilization": 8}}).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch("ai_watch.claude.build_opener", return_value=opener):
            result = claude.fetch_usage({"accessToken": "fake-token"}, 7)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, claude.USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-token")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        self.assertEqual(result, quota(8))

    def test_bearer_token_requests_never_follow_redirects(self):
        self.assertIsNone(claude.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))


class ClaudeCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = Profile("claude", "work", self.root / "work")
        self.profile.home.mkdir()

    def test_custom_profile_cannot_borrow_global_identity(self):
        (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "private@example.com"}}))
        with patch("pathlib.Path.home", return_value=self.root):
            self.assertIsNone(claude.identity(self.profile))
            default = Profile("claude", "default", self.root / ".claude")
            self.assertEqual(claude.identity(default), "private@example.com")

    def test_missing_identity_does_not_prevent_usage(self):
        with patch("ai_watch.claude.credentials", return_value={"accessToken": "fake"}), \
             patch("ai_watch.claude.cached_usage", return_value=quota()):
            result = claude.fetch(self.profile)
        self.assertIsNone(result["email"])
        self.assertEqual(result["windows"], quota()["windows"])

    def test_linux_credentials_never_invoke_claude_or_keychain(self):
        (self.profile.home / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "fake-token", "subscriptionType": "max"}}))
        with patch("ai_watch.claude.subprocess.run") as run:
            self.assertEqual(claude.credentials(self.profile, 1)["accessToken"], "fake-token")
            run.assert_not_called()

    def test_custom_macos_profile_requires_exact_keychain_service(self):
        with patch("sys.platform", "darwin"), patch("ai_watch.claude.subprocess.run") as run:
            with self.assertRaisesRegex(UsageError, "keychain_service"):
                claude.credentials(self.profile, 1)
            run.assert_not_called()
            profile = Profile("claude", "work", self.profile.home, "Claude Code-credentials-example")
            run.return_value = subprocess.CompletedProcess([], 0, b'{"claudeAiOauth":{"accessToken":"fake"}}')
            self.assertEqual(claude.credentials(profile, 1)["accessToken"], "fake")
            self.assertEqual(run.call_args.args[0][3], "Claude Code-credentials-example")


class ClaudeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = Profile("claude", "test", self.root / "profile")
        self.auth = {"accessToken": "fake-test-token-never-real"}
        self.cache = self.root / "cache"

    def read(self, auth=None, timeout=2):
        return claude.cached_usage(self.profile, auth or self.auth, timeout, self.cache)

    def test_cache_reused_without_credentials_or_identity_on_disk(self):
        with patch("ai_watch.claude.fetch_usage", side_effect=lambda *_: quota()) as fetch:
            first = self.read()
            second = self.read()
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(second["cached"])
        self.assertEqual(first["windows"], second["windows"])
        path = next(self.cache.glob("*.json"))
        self.assertNotIn(self.auth["accessToken"], path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_account_switch_cannot_reuse_previous_quota(self):
        with patch("ai_watch.claude.fetch_usage", side_effect=[quota(20), quota(80)]) as fetch:
            self.read()
            result = self.read({"accessToken": "different-fake-login"})
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["windows"][0]["used_percent"], 80)

    def test_429_keeps_recent_data_then_expires_it_and_respects_backoff(self):
        with patch("time.time", return_value=1000), patch("ai_watch.claude.fetch_usage", return_value=quota()):
            self.read()
        limited = {"error": "rate limited", "retry_at": 8000}
        with patch("time.time", return_value=1201), patch("ai_watch.claude.fetch_usage", return_value=limited):
            stale = self.read()
        self.assertEqual(stale["windows"], quota()["windows"])
        self.assertEqual(stale["fetched_at"], 1000)
        with patch("time.time", return_value=4700), patch("ai_watch.claude.fetch_usage") as fetch:
            expired = self.read()
            fetch.assert_not_called()
        self.assertNotIn("windows", expired)
        self.assertEqual(expired["retry_at"], 8000)

    def test_corrupt_cache_is_recovered(self):
        self.cache.mkdir()
        path = self.cache / claude.cache_key(self.profile, self.auth)
        for value in ('not json', '[]', '{"next_fetch_at":"never","result":{}}',
                      '{"next_fetch_at":9999999999,"result":{}}',
                      '{"next_fetch_at":9999999999,"result":{"error":""}}',
                      '{"next_fetch_at":9999999999,"result":{"error":"cached","accessToken":"fake"}}',
                      '{"next_fetch_at":9999999999,"result":{"windows":[{}]}}'):
            path.write_text(value)
            with patch("ai_watch.claude.fetch_usage", return_value=quota()):
                self.assertEqual(self.read()["windows"], quota()["windows"])

    def test_concurrent_monitors_share_one_fetch(self):
        def fetch(*_):
            time.sleep(0.1)
            return quota()
        with patch("ai_watch.claude.fetch_usage", side_effect=fetch) as mocked:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.read(), range(2)))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(results[0]["windows"], results[1]["windows"])

    def test_lock_wait_is_bounded(self):
        self.cache.mkdir()
        path = self.cache / claude.cache_key(self.profile, self.auth)
        with path.open("w") as lock:
            claude.fcntl.flock(lock, claude.fcntl.LOCK_EX)
            started = time.monotonic()
            with self.assertRaisesRegex(UsageError, "cache lock"):
                self.read(timeout=0.1)
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
