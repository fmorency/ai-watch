"""Read Claude subscription quotas; keep a private, token-free quota cache."""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from . import __version__, cache
from .common import UsageError, optional_text, read_object, window
from .http import MAX_RESPONSE, NoRedirect, retry_time
from .runtime import external_command_env

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_TTL = 120


def credentials(profile, timeout):
    path = profile.home / ".credentials.json"
    try:
        data = read_object(path)
    except FileNotFoundError:
        if sys.platform != "darwin":
            raise UsageError("no Claude credentials; sign in with Claude Code for this profile") from None
        service = profile.keychain_service
        if service is None and profile.home == (Path.home() / ".claude").resolve():
            service = "Claude Code-credentials"
        if not service:
            raise UsageError("set keychain_service for this custom macOS profile in your config")
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(timeout, 5), check=True, env=external_command_env(),
            )
            data = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise UsageError("cannot read Claude's Keychain login; unlock it or sign in again") from None
    except (OSError, ValueError):
        raise UsageError("could not read Claude credentials") from None
    auth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(auth, dict) or not isinstance(auth.get("accessToken"), str) or not auth["accessToken"]:
        raise UsageError("no Claude subscription OAuth login; sign in with Claude Code for this profile")
    return auth


def identity(profile):
    paths = [profile.home / ".claude.json"]
    # Only the default profile can use the legacy global identity file.
    if profile.home == (Path.home() / ".claude").resolve():
        paths.append(Path.home() / ".claude.json")
    for path in paths:
        try:
            account = read_object(path).get("oauthAccount")
            if isinstance(account, dict):
                email = optional_text(account.get("emailAddress"))
                if email:
                    return email
        except (OSError, ValueError):
            pass
    return None


def parse_window(name, percent, resets_at):
    if resets_at is not None:
        try:
            parsed = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("missing timezone")
            resets_at = parsed.timestamp()
        except (AttributeError, ValueError, TypeError, OverflowError):
            raise UsageError("Claude returned an invalid reset time") from None
    return window(name, percent, resets_at)


def parse_limits(data):
    if not isinstance(data, dict) or "error" in data:
        raise UsageError("Claude returned invalid quota data")
    windows = []
    entries = data.get("limits")
    if entries is not None and not isinstance(entries, list):
        raise UsageError("Claude returned invalid quota data")
    for entry in entries or []:
        if not isinstance(entry, dict):
            raise UsageError("Claude returned invalid quota data")
        kind = entry.get("kind")
        if kind == "session":
            name = "session"
        elif kind == "weekly_all":
            name = "week (all models)"
        elif kind == "weekly_scoped":
            scope = entry.get("scope") or {}
            if not isinstance(scope, dict) or not isinstance(scope.get("model") or {}, dict):
                raise UsageError("Claude returned invalid quota data")
            model = scope.get("model") or {}
            label = optional_text(model.get("display_name")) or optional_text(model.get("id")) or "other models"
            name = f"week ({label})"
        else:
            continue
        windows.append(parse_window(name, entry.get("percent"), entry.get("resets_at")))
    if not windows:
        for key, quota in data.items():
            if quota is None:
                continue
            if key == "five_hour":
                name = "session"
            elif key == "seven_day":
                name = "week (all models)"
            elif key.startswith("seven_day_"):
                name = f"week ({key[len('seven_day_'):].replace('_', ' ')})"
            else:
                continue
            if not isinstance(quota, dict):
                raise UsageError("Claude returned invalid quota data")
            windows.append(parse_window(name, quota.get("utilization"), quota.get("resets_at")))
    if not windows:
        raise UsageError("Claude returned no quota data for this account")
    return {"windows": windows}


def fetch_usage(auth, timeout):
    request = Request(USAGE_URL, headers={
        "Authorization": f"Bearer {auth['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json",
        "User-Agent": f"ai-watch/{__version__}",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise UsageError("Claude returned an oversized response")
        return parse_limits(json.loads(body))
    except HTTPError as exc:
        status = exc.code
        retry = exc.headers.get("Retry-After") if exc.headers else None
        exc.close()
        if status == 429:
            return {"error": "usage check rate limited (HTTP 429)",
                    "retry_at": retry_time(retry, time.time())}
        if status == 401:
            return {"error": "Claude login expired or rejected; open Claude Code for this profile to renew it"}
        if status == 403:
            return {"error": "Claude denied usage access (HTTP 403); check this profile's login"}
        return {"error": f"Claude usage API returned HTTP {status}"}
    except UsageError as exc:
        return {"error": str(exc)}
    except (ValueError, UnicodeError):
        return {"error": "Claude usage API returned invalid JSON"}
    except (TimeoutError, URLError, OSError):
        return {"error": "could not reach Claude usage API (network error or timeout)"}


def cache_key(profile, auth):
    return cache.key(profile, auth["accessToken"])


def cached_usage(profile, auth, timeout, cache_dir=None, cancel=None):
    return cache.read_or_fetch(cache_key(profile, auth), lambda: fetch_usage(auth, timeout),
                               timeout, CACHE_TTL, cache_dir, cancel)


def fetch(profile, timeout=20, cancel=None):
    details = {"email": identity(profile)}
    try:
        auth = credentials(profile, timeout)
        details["plan"] = optional_text(auth.get("subscriptionType"))
        return dict(details, **cached_usage(profile, auth, timeout, cancel=cancel))
    except UsageError as exc:
        return dict(details, error=str(exc))
    except OSError:
        return dict(details, error="could not access the usage cache; check XDG_CACHE_HOME permissions")
