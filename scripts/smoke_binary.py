"""Exercise an extracted standalone binary without real accounts or provider requests."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_watch import claude, codex  # noqa: E402
from ai_watch.common import Profile  # noqa: E402


def main():
    binary = Path(sys.argv[1]).absolute()
    with tempfile.TemporaryDirectory(prefix="ai-watch-smoke-") as temporary:
        root = Path(temporary).resolve()
        # An empty PATH proves the extracted executable needs no Python or CLI.
        # Uncached synthetic requests can reach only a closed loopback proxy.
        env = dict(os.environ, PATH="", AI_WATCH_CONFIG=str(root / "missing-config.json"),
                   XDG_CACHE_HOME=str(root / "cache"), HTTPS_PROXY="http://127.0.0.1:1",
                   https_proxy="http://127.0.0.1:1", NO_PROXY="", no_proxy="")

        def run(command, expected=0):
            result = subprocess.run(command, cwd=root, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            if result.returncode != expected:
                raise RuntimeError(f"{command[0]} returned {result.returncode}: {result.stderr}")
            return result.stdout

        print(run([str(binary), "--version"]).strip())
        run([str(binary), "--help"])
        report = json.loads(run([str(binary), "--demo", "--json", "--hide-email"]))
        assert report["demo"] and len(report["accounts"]) == 3
        assert all("email" not in account for account in report["accounts"])
        alias = binary.with_name("ai-usage")
        assert run([str(alias), "--version"]).startswith("ai-usage ")
        run([str(alias), "--demo", "--ascii"])

        home = root / "profile"
        home.mkdir()
        config = root / "profiles.json"
        config.write_text(json.dumps({"profiles": [
            {"provider": provider, "name": "test", "home": str(home)} for provider in ("codex", "claude")
        ]}), encoding="utf-8")
        report = json.loads(run([str(binary), "--json", "--config", str(config)], expected=1))
        assert all(account.get("error") for account in report["accounts"])

        codex_auth = {"access_token": "fake-codex-token", "account_id": "fake-account"}
        claude_auth = {"accessToken": "fake-claude-token", "subscriptionType": "max"}
        (home / "auth.json").write_text(json.dumps({"tokens": codex_auth}), encoding="utf-8")
        (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": claude_auth}), encoding="utf-8")
        cache = root / "cache/ai-watch"
        cache.mkdir(parents=True)
        for module, auth in ((codex, codex_auth), (claude, claude_auth)):
            provider = module.__name__.split(".")[-1]
            name = module.cache_key(Profile(provider, "test", home), auth)
            quota = {"windows": [{"name": "test window", "used_percent": 42, "resets_at": None}],
                     "fetched_at": time.time()}
            (cache / name).write_text(json.dumps({"next_fetch_at": time.time() + 60, "result": quota}))
        report = json.loads(run([str(binary), "--json", "--config", str(config)]))
        assert all(a.get("cached") and a["windows"][0]["used_percent"] == 42 for a in report["accounts"])
        # Exercise both frozen HTTP backends and their safe network-error path.
        for path in cache.glob("*.json"):
            path.unlink()
        report = json.loads(run([str(binary), "--json", "--config", str(config), "--timeout", "1"], expected=1))
        assert all("network error or timeout" in a.get("error", "") for a in report["accounts"]), report
    print("Standalone demo, alias, saved logins, quota caches, and HTTP loading checks passed with empty PATH")


if __name__ == "__main__":
    main()
