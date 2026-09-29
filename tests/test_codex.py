import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_watch import codex
from ai_watch.common import Profile, UsageError


class CodexParsingTests(unittest.TestCase):
    def test_primary_secondary_and_model_limits(self):
        main = {"limitId": "codex", "primary": {"usedPercent": 0, "windowDurationMins": 300},
                "secondary": {"usedPercent": 91, "windowDurationMins": 10080},
                "credits": {"hasCredits": False, "unlimited": False}}
        data = {"rateLimits": main, "rateLimitsByLimitId": {
            "codex": main, "special": {"primary": {"usedPercent": 5, "windowDurationMins": 15}}}}
        result = codex.parse_limits(data)
        self.assertEqual([w["name"] for w in result["windows"]],
                         ["5h window", "7d window", "special: 15m window"])
        self.assertEqual(result["windows"][0]["used_percent"], 0)
        self.assertIn("credits: none", result["notes"])

    def test_missing_data_is_not_zero_usage(self):
        for value in ({}, {"rateLimits": {"primary": {}}}, {"rateLimits": []},
                      {"rateLimits": {"primary": {"usedPercent": True}}}):
            with self.subTest(value=value), self.assertRaises(UsageError):
                codex.parse_limits(value)

    def test_limit_reached_without_windows_and_unknown_credits(self):
        result = codex.parse_limits({"rateLimits": {"rateLimitReachedType": "rate_limit_reached",
                                                   "spendControlReached": True}})
        self.assertEqual(result["windows"], [])
        self.assertEqual(len(result["notes"]), 2)
        self.assertFalse(any("credits" in n for n in result["notes"]))


class CodexProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = Profile("codex", "test", self.root)

    def fake_cli(self, body):
        path = self.root / "codex"
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o755)
        env = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)

    def test_handshake_notification_handling_and_account_isolation(self):
        self.fake_cli('''import json, os, sys
assert os.environ['CODEX_HOME'] == os.getcwd()
assert 'OPENAI_API_KEY' not in os.environ
assert 'CODEX_API_KEY' not in os.environ
assert 'OPENAI_FEDERATION_RULE_ID' not in os.environ
initialized = False
for line in sys.stdin:
    m = json.loads(line)
    method = m.get('method')
    if method == 'initialized':
        initialized = True
        continue
    if method == 'initialize':
        result = {'userAgent': 'test'}
    elif method == 'account/read':
        assert initialized
        result = {'account': {'type': 'chatgpt', 'email': 'test@example.com', 'planType': 'pro'}}
    elif method == 'account/rateLimits/read':
        print(json.dumps({'method': 'account/rateLimits/updated', 'params': {}}), flush=True)
        result = {'rateLimits': {'primary': {'usedPercent': 25, 'windowDurationMins': 300}}}
    print(json.dumps({'id': m['id'], 'result': result}), flush=True)
''')
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-parent-secret", "CODEX_API_KEY": "fake",
                                     "OPENAI_FEDERATION_RULE_ID": "fake-parent-rule"}):
            result = codex.fetch(self.profile, timeout=3)
        self.assertNotIn("error", result)
        self.assertEqual(result["email"], "test@example.com")
        self.assertEqual(result["windows"][0]["used_percent"], 25)

    def test_silent_process_times_out_and_is_reaped(self):
        self.fake_cli('''import os, time
from pathlib import Path
Path('test.pid').write_text(str(os.getpid()))
time.sleep(60)
''')
        started = time.monotonic()
        result = codex.fetch(self.profile, timeout=0.3)
        self.assertIn("timed out", result["error"])
        self.assertLess(time.monotonic() - started, 3)
        pid = int((self.root / "test.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_partial_line_does_not_defeat_timeout(self):
        self.fake_cli("import sys, time\nsys.stdout.write('{'); sys.stdout.flush(); time.sleep(60)\n")
        result = codex.fetch(self.profile, timeout=0.3)
        self.assertIn("timed out", result["error"])

    def test_raw_server_errors_are_not_exposed(self):
        self.fake_cli('''import json, sys
m=json.loads(sys.stdin.readline())
print(json.dumps({'id':m['id'], 'error':{'message':'fake-super-secret'}}), flush=True)
''')
        result = codex.fetch(self.profile, timeout=3)
        self.assertIn("error", result)
        self.assertNotIn("fake-super-secret", json.dumps(result))

    def test_missing_binary_is_actionable(self):
        with patch("ai_watch.codex.subprocess.Popen", side_effect=FileNotFoundError):
            self.assertIn("install the Codex CLI", codex.fetch(self.profile)["error"])


if __name__ == "__main__":
    unittest.main()
