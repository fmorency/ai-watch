"""Read Claude subscription quotas; keep a private, token-free quota cache."""

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .common import UsageError, number, optional_text, read_object, window
from .config import cache_path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CACHE_TTL = 120
STALE_TTL = 3600
MAX_RESPONSE = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an OAuth bearer token to a redirect target.
        return None


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
                timeout=min(timeout, 5), check=True,
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


def retry_time(value, now):
    minimum = now + 60
    try:
        seconds = float(value)
        if number(seconds):
            return max(minimum, now + seconds)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        stamp = parsedate_to_datetime(value).timestamp()
        return max(minimum, stamp) if number(stamp) else minimum
    except (TypeError, ValueError, OverflowError, IndexError):
        return minimum


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
    # Token rotation may miss the cache once; it can never reuse another login's data.
    key = f"{profile.home.resolve()}\0{auth['accessToken']}".encode()
    return "claude-" + hashlib.sha256(key).hexdigest() + ".json"


def read_state(stream):
    try:
        state = json.load(stream)
        if not isinstance(state, dict) or not number(state.get("next_fetch_at")):
            return {}
        result = state.get("result")
        if not isinstance(result, dict):
            return {}
        if set(result) - {"windows", "fetched_at", "error", "retry_at", "cached"}:
            return {}
        if not result.get("windows") and not result.get("error"):
            return {}
        if result.get("windows"):
            if not isinstance(result["windows"], list) or not number(result.get("fetched_at")):
                return {}
            for item in result["windows"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    return {}
                window(item["name"], item.get("used_percent"), item.get("resets_at"))
        if "error" in result and (not isinstance(result["error"], str) or not result["error"]):
            return {}
        if "retry_at" in result and not number(result["retry_at"]):
            return {}
        return state
    except (ValueError, UsageError):
        return {}


def cached_usage(profile, auth, timeout, cache_dir=None, cancel=None):
    directory = Path(cache_dir) if cache_dir is not None else cache_path()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory / cache_key(profile, auth), flags, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise UsageError("usage cache must be a regular file owned by you")
        os.fchmod(stream.fileno(), 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                    raise UsageError("another usage check holds this profile's cache lock; try again")
                time.sleep(0.05)
        state = read_state(stream)
        now = time.time()
        previous = state.get("result", {})
        if now - previous.get("fetched_at", 0) > STALE_TTL:
            previous.pop("windows", None)
        if now < state.get("next_fetch_at", 0):
            return dict(previous, cached=True)
        result = fetch_usage(auth, timeout)
        now = time.time()
        if result.get("windows"):
            result["fetched_at"] = now
            next_fetch = now + CACHE_TTL
        else:
            next_fetch = max(now + 60, result.get("retry_at", 0))
            result["retry_at"] = next_fetch
            if previous.get("windows") and now - previous.get("fetched_at", 0) <= STALE_TTL:
                result.update(windows=previous["windows"], fetched_at=previous["fetched_at"], cached=True)
        stream.seek(0)
        json.dump({"result": result, "next_fetch_at": next_fetch}, stream, allow_nan=False)
        stream.truncate()
        return result


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
