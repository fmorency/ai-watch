import contextlib
import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from ai_watch.cli import main, usage_main
from ai_watch.render import render


class CLITests(unittest.TestCase):
    def invoke(self, args, function=main):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = function(args)
        return status, output.getvalue()

    def test_demo_is_offline_and_ignores_missing_config(self):
        with patch("ai_watch.cli.load_profiles", side_effect=AssertionError("read profiles")), \
             patch("ai_watch.cli.collect", side_effect=AssertionError("network")):
            status, output = self.invoke(["--demo", "--json", "--config", "/nonexistent"])
        self.assertEqual(status, 0)
        report = json.loads(output)
        self.assertTrue(report["demo"])
        self.assertEqual(len(report["accounts"]), 3)

    def test_json_email_redaction_and_filters(self):
        status, output = self.invoke(["--demo", "--json", "--codex", "--hide-email", "--profile", "work"])
        self.assertEqual(status, 0)
        accounts = json.loads(output)["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertNotIn("email", accounts[0])
        self.assertNotIn("@", output)

    def test_both_provider_flags_include_both(self):
        _, output = self.invoke(["--demo", "--json", "--codex", "--claude"])
        self.assertEqual(len(json.loads(output)["accounts"]), 3)

    def test_piped_watch_exits_once_without_control_codes(self):
        status, output = self.invoke(["--demo"])
        self.assertEqual(status, 0)
        self.assertNotIn("\x1b", output)
        self.assertEqual(output.count("ai-watch"), 1)

    def test_ascii_and_ai_usage_alias(self):
        status, output = self.invoke(["--demo", "--ascii"], usage_main)
        self.assertEqual(status, 0)
        self.assertTrue(output.isascii())
        self.assertIn("###", output)

    def test_no_profiles_and_partial_failure_exit_codes(self):
        with patch("ai_watch.cli.load_profiles", return_value=[]), patch("ai_watch.cli.collect", return_value=[]):
            status, output = self.invoke(["--once"])
        self.assertEqual(status, 1)
        self.assertIn("No profiles", output)
        with patch("ai_watch.cli.load_profiles", return_value=[]), patch("ai_watch.cli.collect", return_value=[
            {"provider": "codex", "name": "test", "error": "unavailable"}]):
            status, _ = self.invoke(["--json"])
        self.assertEqual(status, 1)

    def test_invalid_intervals_rejected(self):
        for interval in ("0", "-1", "nan", "inf", "no"):
            with self.subTest(interval=interval), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.invoke([interval, "--demo"])
                self.assertEqual(error.exception.code, 2)

    def test_unavailable_and_stale_are_distinguished_and_terminal_controls_removed(self):
        report = {"generated_at": 1000, "accounts": [{
            "provider": "claude", "name": "work\x1b\x9b", "email": "fake@example.com\r\n",
            "windows": [{"name": "session", "used_percent": 15, "resets_at": None}],
            "fetched_at": 880, "error": "rate limited"},
            {"provider": "codex", "name": "personal", "error": "not logged in"}]}
        text = render(report)
        self.assertIn("STALE (2m old)", text)
        self.assertIn("unavailable: not logged in", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x9b", text)
        self.assertNotIn("\r", text)

    def test_watch_restores_terminal_on_ctrl_c(self):
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, "-m", "ai_watch", "--demo"],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=dict(os.environ, TERM="xterm"))
        os.close(slave)
        data = b""
        try:
            deadline = time.monotonic() + 5
            while b"Ctrl+C" not in data and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    data += os.read(master, 65536)
            self.assertIn(b"Ctrl+C", data)
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
            while select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    data += chunk
                except OSError:
                    break
            self.assertEqual(proc.returncode, 130)
            self.assertIn(b"\x1b[?25h\x1b[?1049l", data)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
