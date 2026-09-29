import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_watch.common import UsageError
from ai_watch.config import load_profiles, select_profiles


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.home = patch("pathlib.Path.home", return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)

    def config(self, profiles):
        path = self.root / "profiles.json"
        path.write_text(json.dumps({"profiles": profiles}))
        return path

    def test_default_discovery_uses_only_existing_homes(self):
        (self.root / ".codex").mkdir()
        (self.root / ".codex-private").mkdir()
        profiles = load_profiles()
        self.assertEqual([(p.provider, p.name) for p in profiles], [("codex", "default")])

    def test_environment_overrides_default_profiles(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "work")}):
            self.assertEqual(load_profiles()[0].home, self.root / "work")

    def test_relative_homes_resolve_against_config_directory(self):
        path = self.config([{"provider": "claude", "name": "work", "home": "work"}])
        self.assertEqual(load_profiles(path)[0].home, self.root / "work")

    def test_explicit_missing_config_does_not_silently_discover(self):
        with self.assertRaises(UsageError):
            load_profiles(self.root / "missing.json")

    def test_default_config_and_environment_config(self):
        path = self.config([{"provider": "codex", "name": "example", "home": "home"}])
        with patch.dict(os.environ, {"AI_WATCH_CONFIG": str(path)}):
            self.assertEqual(load_profiles()[0].name, "example")
        target = self.root / ".config/ai-watch/config.json"
        target.parent.mkdir(parents=True)
        target.write_text(path.read_text())
        self.assertEqual(load_profiles()[0].name, "example")

    def test_invalid_profiles_and_duplicate_accounts_are_rejected(self):
        good = {"provider": "codex", "name": "personal", "home": "home"}
        bad = [dict(good, provider="typo"), dict(good, name="escape\x1b"),
               dict(good, home=None), dict(good, token="never-accepted"),
               dict(good, keychain_service="not-for-codex")]
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(UsageError):
                load_profiles(self.config([entry]))
        for duplicate in (good, dict(good, name="another"), dict(good, home="another")):
            with self.subTest(duplicate=duplicate), self.assertRaises(UsageError):
                load_profiles(self.config([good, duplicate]))

    def test_combined_provider_and_profile_filters(self):
        path = self.config([
            {"provider": "codex", "name": "work", "home": "codex"},
            {"provider": "claude", "name": "work", "home": "claude"},
        ])
        profiles = load_profiles(path)
        self.assertEqual(len(select_profiles(profiles, names=["work"])), 2)
        self.assertEqual(select_profiles(profiles, names=["claude:work"])[0].provider, "claude")
        with self.assertRaises(UsageError):
            select_profiles(profiles, ["codex"], ["claude:work"])


if __name__ == "__main__":
    unittest.main()
