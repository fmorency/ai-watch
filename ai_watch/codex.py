"""Read Codex subscription quotas directly, without starting the Codex CLI."""

import base64
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from . import __version__, cache
from .common import UsageError, number, optional_text, read_object, window
from .http import MAX_RESPONSE, NoRedirect, retry_time

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CACHE_TTL = 60


def credentials(profile):
    try:
        data = read_object(profile.home / "auth.json")
    except FileNotFoundError:
        raise UsageError("no Codex auth.json; sign in with file-based credential storage for this profile") from None
    except (OSError, ValueError):
        raise UsageError("could not read Codex credentials") from None
    auth = data.get("tokens")
    if (data.get("auth_mode") not in (None, "chatgpt")
            or not isinstance(auth, dict) or data.get("OPENAI_API_KEY")):
        raise UsageError("subscription quota requires a Codex ChatGPT login, not API billing")
    for key in ("access_token", "account_id"):
        value = auth.get(key)
        if (not isinstance(value, str) or not value or not value.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            raise UsageError("incomplete Codex login; run codex login for this profile")
    # Refresh tokens are neither needed nor passed to the HTTP/cache layers.
    return {key: auth.get(key) for key in ("access_token", "account_id", "id_token")}


def identity(auth):
    # Decode only for display; the server authenticates the access token. Keeping
    # identity outside the quota cache avoids writing emails to another file.
    try:
        payload = auth["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        details = claims.get("https://api.openai.com/auth") or {}
        return {"email": optional_text(claims.get("email")),
                "plan": optional_text(details.get("chatgpt_plan_type"))}
    except (KeyError, AttributeError, IndexError, TypeError, ValueError):
        return {}


def duration(seconds, fallback):
    if not number(seconds) or not seconds:
        return fallback
    if seconds % 86400 == 0:
        return f"{seconds / 86400:g}d"
    if seconds % 3600 == 0:
        return f"{seconds / 3600:g}h"
    if seconds % 60 == 0:
        return f"{seconds / 60:g}m"
    return f"{seconds:g}s"


def parse_limits(data):
    if not isinstance(data, dict) or "error" in data:
        raise UsageError("Codex returned invalid quota data")
    entries = [(None, data.get("rate_limit"))]
    additional = data.get("additional_rate_limits")
    if additional is not None and not isinstance(additional, list):
        raise UsageError("Codex returned invalid quota data")
    for entry in additional or []:
        if not isinstance(entry, dict):
            raise UsageError("Codex returned invalid quota data")
        label = optional_text(entry.get("limit_name")) or optional_text(entry.get("metered_feature"))
        entries.append((label, entry.get("rate_limit")))
    windows, notes = [], []
    for label, limit in entries:
        if limit is None:
            continue
        if not isinstance(limit, dict):
            raise UsageError("Codex returned invalid quota data")
        prefix = f"{label}: " if label else ""
        for slot in ("primary", "secondary"):
            quota = limit.get(slot + "_window")
            if quota is None:
                continue
            if not isinstance(quota, dict):
                raise UsageError("Codex returned invalid quota data")
            name = prefix + duration(quota.get("limit_window_seconds"), slot) + " window"
            windows.append(window(name, quota.get("used_percent"), quota.get("reset_at")))
        if limit.get("allowed") is False:
            notes.append(prefix + "ordinary included usage is currently unavailable")
        elif limit.get("limit_reached") is True:
            notes.append(prefix + "LIMIT REACHED")
    reached = data.get("rate_limit_reached_type")
    if isinstance(reached, dict) and optional_text(reached.get("type")):
        notes.append("LIMIT REACHED: " + reached["type"])
    spend = data.get("spend_control")
    if isinstance(spend, dict) and spend.get("reached") is True:
        notes.append("spend control reached")
    credits = data.get("credits")
    if isinstance(credits, dict):
        if credits.get("unlimited") is True:
            notes.append("credits: unlimited")
        elif credits.get("has_credits") is True:
            notes.append("credits: " + (optional_text(credits.get("balance")) or "available"))
        elif credits.get("has_credits") is False:
            notes.append("credits: none")
    if not windows and not notes:
        raise UsageError("Codex returned no quota data for this account")
    result = {"windows": windows, "notes": list(dict.fromkeys(notes))}
    if optional_text(data.get("plan_type")):
        result["plan"] = data["plan_type"]
    return result


def fetch_usage(auth, timeout):
    request = Request(USAGE_URL, headers={
        "Authorization": f"Bearer {auth['access_token']}",
        "ChatGPT-Account-Id": auth["account_id"], "Accept": "application/json",
        "User-Agent": f"ai-watch/{__version__}",
    }, method="GET")
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise UsageError("Codex returned an oversized response")
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("account_id") != auth["account_id"]:
            raise UsageError("Codex usage account does not match this profile's login; sign in again")
        return parse_limits(data)
    except HTTPError as exc:
        status = exc.code
        retry = exc.headers.get("Retry-After") if exc.headers else None
        exc.close()
        if status == 429:
            return {"error": "usage check rate limited (HTTP 429)",
                    "retry_at": retry_time(retry, time.time())}
        if status == 401:
            return {"error": "Codex login expired or rejected; run codex login for this profile to renew it"}
        if status == 403:
            return {"error": "Codex denied usage access (HTTP 403); check this profile's login"}
        return {"error": f"Codex usage API returned HTTP {status}"}
    except UsageError as exc:
        return {"error": str(exc)}
    except (ValueError, UnicodeError):
        return {"error": "Codex usage API returned invalid JSON"}
    except (TimeoutError, URLError, OSError):
        return {"error": "could not reach Codex usage API (network error or timeout)"}


def cache_key(profile, auth):
    return cache.key(profile, auth["access_token"], auth["account_id"])


def cached_usage(profile, auth, timeout, cache_dir=None, cancel=None):
    return cache.read_or_fetch(cache_key(profile, auth), lambda: fetch_usage(auth, timeout),
                               timeout, CACHE_TTL, cache_dir, cancel)


def fetch(profile, timeout=20, cancel=None):
    details = {}
    try:
        auth = credentials(profile)
        details = identity(auth)
        return dict(details, **cached_usage(profile, auth, timeout, cancel=cancel))
    except UsageError as exc:
        return dict(details, error=str(exc))
    except OSError:
        return dict(details, error="could not access the usage cache; check XDG_CACHE_HOME permissions")
