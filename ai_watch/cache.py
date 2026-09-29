"""Private, locked quota caches shared by the HTTP providers."""

import fcntl
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .common import UsageError, number, window
from .config import cache_path

STALE_TTL = 3600
RESULT_FIELDS = {"windows", "notes", "plan", "fetched_at", "error", "retry_at", "cached"}


def key(profile, token, account_id=None):
    # Preserve the existing Claude keys. Codex also binds the organization ID.
    value = f"{profile.home.resolve()}\0{token}"
    if account_id is not None:
        value += "\0" + account_id
    return profile.provider + "-" + hashlib.sha256(value.encode()).hexdigest() + ".json"


def read_state(stream):
    try:
        state = json.load(stream)
        if not isinstance(state, dict) or not number(state.get("next_fetch_at")):
            return {}
        result = state.get("result")
        if not isinstance(result, dict) or set(result) - RESULT_FIELDS:
            return {}
        if not result.get("windows") and not result.get("notes") and not result.get("error"):
            return {}
        if "windows" in result:
            if not isinstance(result["windows"], list):
                return {}
            for item in result["windows"]:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                        or set(item) - {"name", "used_percent", "resets_at"}):
                    return {}
                window(item["name"], item.get("used_percent"), item.get("resets_at"))
        if "notes" in result and (not isinstance(result["notes"], list)
                                   or any(not isinstance(n, str) for n in result["notes"])):
            return {}
        if (result.get("windows") or result.get("notes")) and not number(result.get("fetched_at")):
            return {}
        if "fetched_at" in result and not number(result["fetched_at"]):
            return {}
        if "plan" in result and not isinstance(result["plan"], str):
            return {}
        if "error" in result and (not isinstance(result["error"], str) or not result["error"]):
            return {}
        if "retry_at" in result and not number(result["retry_at"]):
            return {}
        return state
    except (ValueError, UsageError):
        return {}


def read_or_fetch(filename, fetcher, timeout, ttl, cache_dir=None, cancel=None):
    directory = Path(cache_dir) if cache_dir is not None else cache_path()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory / filename, flags, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise UsageError("usage cache must be a regular file owned by you")
        os.fchmod(stream.fileno(), 0o600)
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                raise UsageError("usage check cancelled")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise UsageError("another usage check holds this profile's cache lock; try again")
                time.sleep(0.05)
        state = read_state(stream)
        now = time.time()
        previous = state.get("result", {})
        if now - previous.get("fetched_at", 0) > STALE_TTL:
            for field in ("windows", "notes", "plan"):
                previous.pop(field, None)
        if now < state.get("next_fetch_at", 0):
            return dict(previous, cached=True)
        result = {k: v for k, v in fetcher().items() if k in RESULT_FIELDS}
        now = time.time()
        if not result.get("error") and (result.get("windows") or result.get("notes")):
            result["fetched_at"] = now
            next_fetch = now + ttl
        else:
            next_fetch = max(now + 60, result.get("retry_at", 0))
            result["retry_at"] = next_fetch
            if ((previous.get("windows") or previous.get("notes"))
                    and now - previous.get("fetched_at", 0) <= STALE_TTL):
                result.update({k: previous[k] for k in ("windows", "notes", "plan", "fetched_at")
                               if k in previous})
                result["cached"] = True
        stream.seek(0)
        json.dump({"result": result, "next_fetch_at": next_fetch}, stream, allow_nan=False)
        stream.truncate()
        return result
