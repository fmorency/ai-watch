import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from ai_watch import codex
from ai_watch.common import Profile, UsageError


def response(percent=25):
    return {"account_id": "test-account", "plan_type": "pro", "rate_limit": {
        "primary_window": {"used_percent": percent, "limit_window_seconds": 18000},
    }, "credits": {"has_credits": False}}


class CodexParsingTests(unittest.TestCase):
    def test_primary_secondary_and_model_limits(self):
        data = response(0)
        data["rate_limit"]["secondary_window"] = {
            "used_percent": 91, "limit_window_seconds": 604800, "reset_at": 1800000000}
        data["additional_rate_limits"] = [{"metered_feature": "special", "limit_name": "Model",
            "rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": 900}}}]
        result = codex.parse_limits(data)
        self.assertEqual([w["name"] for w in result["windows"]],
                         ["5h window", "7d window", "Model: 15m window"])
        self.assertEqual(result["windows"][0]["used_percent"], 0)
        self.assertEqual(result["windows"][1]["resets_at"], 1800000000)
        self.assertIn("credits: none", result["notes"])
        self.assertEqual(result["plan"], "pro")

    def test_missing_data_is_not_zero_usage(self):
        values = [None, [], {}, {"rate_limit": []}, {"additional_rate_limits": {}},
                  {"additional_rate_limits": [None]}, {"rate_limit": {"primary_window": {}}}]
        values += [{"rate_limit": {"primary_window": {"used_percent": v}}}
                   for v in (None, True, -1, "3", float("nan"), float("inf"))]
        for value in values:
            with self.subTest(value=value), self.assertRaises(UsageError):
                codex.parse_limits(value)

    def test_limit_reached_without_windows_and_unknown_credits(self):
        result = codex.parse_limits({"rate_limit_reached_type": {"type": "rate_limit_reached"},
                                    "spend_control": {"reached": True}})
        self.assertEqual(result["windows"], [])
        self.assertEqual(len(result["notes"]), 2)
        self.assertFalse(any("credits" in n for n in result["notes"]))

    def test_unavailable_included_usage_and_unlimited_credits(self):
        result = codex.parse_limits({"rate_limit": {"allowed": False}, "credits": {"unlimited": True}})
        self.assertIn("ordinary included usage is currently unavailable", result["notes"])
        self.assertIn("credits: unlimited", result["notes"])


class CodexHTTPTests(unittest.TestCase):
    def setUp(self):
        self.auth = {"access_token": "fake-token", "account_id": "test-account"}

    def fetch(self, body):
        opener = Mock()
        opener.open.return_value = io.BytesIO(body)
        with patch("ai_watch.codex.build_opener", return_value=opener):
            result = codex.fetch_usage(self.auth, 7)
        return result, opener.open.call_args

    def test_get_headers_normalized_output_and_profile_isolation(self):
        data = response()
        data["email"] = "private@example.com"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "wrong-parent-login", "CODEX_API_KEY": "wrong"}):
            result, call = self.fetch(json.dumps(data).encode())
        request = call.args[0]
        self.assertEqual(request.full_url, codex.USAGE_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-token")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "test-account")
        self.assertEqual(call.kwargs["timeout"], 7)
        self.assertEqual(result, codex.parse_limits(response()))
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("account_id", result)

    def test_wrong_or_missing_response_account_is_rejected(self):
        for account in ("another-private-account", None):
            data = dict(response(), account_id=account)
            result, _ = self.fetch(json.dumps(data).encode())
            self.assertIn("does not match", result["error"])
            self.assertNotIn("windows", result)
            self.assertNotIn("private", json.dumps(result))

    def test_http_errors_hide_bodies_and_honor_retry_after(self):
        for status in (401, 403, 429, 500, 302):
            error = HTTPError(codex.USAGE_URL, status, "secret-message",
                              {"Retry-After": "300"}, io.BytesIO(b"secret-body"))
            opener = Mock()
            opener.open.side_effect = error
            with patch("ai_watch.codex.build_opener", return_value=opener), patch("time.time", return_value=100):
                result = codex.fetch_usage(self.auth, 1)
            self.assertIn("error", result)
            self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("fake-token", json.dumps(result))
            if status == 429:
                self.assertEqual(result["retry_at"], 400)
            if status == 401:
                self.assertIn("codex login", result["error"])

    def test_invalid_json_and_response_size_are_bounded(self):
        for body in (b"not-json-secret", b"x" * (codex.MAX_RESPONSE + 1)):
            result, _ = self.fetch(body)
            self.assertIn("error", result)
            self.assertNotIn("secret", json.dumps(result))

    def test_network_failures_are_safe_and_no_refresh_is_attempted(self):
        for error in (TimeoutError("secret"), URLError("secret"), OSError("secret")):
            opener = Mock()
            opener.open.side_effect = error
            with patch("ai_watch.codex.build_opener", return_value=opener):
                result = codex.fetch_usage(self.auth, 1)
            self.assertIn("network error or timeout", result["error"])
            self.assertEqual(opener.open.call_count, 1)

    def test_no_redirect_can_forward_the_bearer_token(self):
        self.assertIsNone(codex.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))


class CodexLoginAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = Profile("codex", "test", self.root)
        self.auth = {"access_token": "fake-token", "account_id": "test-account"}
        self.cache = self.root / "cache"

    def read(self, auth=None):
        return codex.cached_usage(self.profile, auth or self.auth, 2, self.cache)

    def test_file_credentials_are_read_only_and_identity_stays_out_of_cache(self):
        payload = base64.urlsafe_b64encode(json.dumps({"email": "test@example.com",
            "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}).encode()).decode().rstrip("=")
        tokens = dict(self.auth, id_token=f"header.{payload}.signature", refresh_token="never-use-this")
        path = self.root / "auth.json"
        path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}))
        before = path.read_bytes()
        with patch("ai_watch.codex.fetch_usage", return_value=codex.parse_limits(response())) as fetch, \
             patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache), "OPENAI_API_KEY": "unrelated"}):
            result = codex.fetch(self.profile)
        self.assertEqual(result["email"], "test@example.com")
        self.assertEqual(result["plan"], "pro")
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("refresh_token", fetch.call_args.args[0])
        saved = next((self.cache / "ai-watch").glob("*.json")).read_text()
        for private in ("test@example.com", "fake-token", "never-use-this", "test-account"):
            self.assertNotIn(private, saved)

    def test_missing_file_and_api_keys_do_not_start_processes(self):
        with patch("subprocess.Popen") as popen:
            self.assertIn("auth.json", codex.fetch(self.profile)["error"])
            (self.root / "auth.json").write_text('{"OPENAI_API_KEY":"fake-key"}')
            self.assertIn("ChatGPT login", codex.fetch(self.profile)["error"])
            popen.assert_not_called()

    def test_invalid_credentials_fail_before_network_access(self):
        for auth in ({}, dict(self.auth, account_id=""), dict(self.auth, access_token="bad\nheader")):
            (self.root / "auth.json").write_text(json.dumps({"tokens": auth}))
            with patch("ai_watch.codex.build_opener") as network:
                self.assertIn("error", codex.fetch(self.profile))
                network.assert_not_called()
        for token in (None, "broken", "a.!!!.b", "a.W10.b"):
            self.assertEqual(codex.identity({"id_token": token}), {})

    def test_cache_lasts_one_minute_and_separates_account_and_token(self):
        with patch("time.time", return_value=1000), \
             patch("ai_watch.codex.fetch_usage", return_value=codex.parse_limits(response())) as fetch:
            self.read()
            self.assertTrue(self.read()["cached"])
            self.assertEqual(fetch.call_count, 1)
            self.read(dict(self.auth, account_id="other-account"))
            self.read(dict(self.auth, access_token="other-token"))
            self.assertEqual(fetch.call_count, 3)
        with patch("time.time", return_value=1061), \
             patch("ai_watch.codex.fetch_usage", return_value=codex.parse_limits(response(50))) as fetch:
            self.assertEqual(self.read()["windows"][0]["used_percent"], 50)
            fetch.assert_called_once()

    def test_notes_only_quota_is_cached_and_marked_stale_during_backoff(self):
        with patch("time.time", return_value=1000), \
             patch("ai_watch.codex.fetch_usage", return_value={"windows": [], "notes": ["credits: none"]}):
            self.read()
        with patch("time.time", return_value=1061), \
             patch("ai_watch.codex.fetch_usage", return_value={"error": "rate limited", "retry_at": 8000}):
            result = self.read()
            self.assertEqual(result["notes"], ["credits: none"])
            self.assertEqual(result["fetched_at"], 1000)
            self.assertTrue(result["cached"])
        with patch("time.time", return_value=4700), patch("ai_watch.codex.fetch_usage") as fetch:
            result = self.read()
            self.assertNotIn("notes", result)
            self.assertEqual(result["retry_at"], 8000)
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
