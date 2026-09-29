"""Read account identity and limits from a short-lived Codex app-server."""

import json
import os
import selectors
import subprocess
import time

from . import __version__
from .common import UsageError, number, optional_text, window


class AppServer:
    def __init__(self, home, timeout, cancel=None):
        self.deadline = time.monotonic() + timeout
        self.cancel = cancel
        self.buffer = b""
        self.counter = 0
        env = dict(os.environ, CODEX_HOME=str(home))
        # A parent agent's credentials must not override the selected profile.
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_IDENTITY_TOKEN_FILE",
                    "OPENAI_FEDERATION_RULE_ID"):
            env.pop(key, None)
        try:
            self.proc = subprocess.Popen(
                ["codex", "app-server"], env=env, cwd=home,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
        except OSError:
            raise UsageError("cannot start codex; install the Codex CLI and check PATH") from None
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)

    def send(self, message):
        try:
            self.proc.stdin.write((json.dumps(message) + "\n").encode())
            self.proc.stdin.flush()
        except OSError:
            raise UsageError("Codex app-server closed its input") from None

    def receive(self):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or (self.cancel is not None and self.cancel.is_set()):
                raise UsageError("Codex app-server timed out; check this profile's login and network")
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(message, dict):
                    return message
                continue
            if not self.selector.select(min(remaining, 0.1)):
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise UsageError("Codex app-server exited before returning usage")
            self.buffer += chunk
            if len(self.buffer) > 4 * 1024 * 1024:
                raise UsageError("Codex app-server returned an oversized response")

    def request(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if "method" in message:
                if "id" in message:
                    self.send({"id": message["id"], "error": {
                        "code": -32601, "message": "ai-watch does not handle server requests"}})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise UsageError(f"Codex rejected {method}; check login and update the Codex CLI")
            result = message.get("result")
            if not isinstance(result, dict):
                raise UsageError("Codex app-server returned an invalid response")
            return result

    def close(self):
        self.selector.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


def duration(minutes, fallback):
    if not number(minutes) or not minutes:
        return fallback
    if minutes % 1440 == 0:
        return f"{minutes / 1440:g}d"
    if minutes % 60 == 0:
        return f"{minutes / 60:g}h"
    return f"{minutes:g}m"


def parse_limits(data):
    if not isinstance(data, dict):
        raise UsageError("Codex returned invalid quota data")
    main = data.get("rateLimits") or {}
    buckets = data.get("rateLimitsByLimitId") or {}
    if not isinstance(main, dict) or not isinstance(buckets, dict):
        raise UsageError("Codex returned invalid quota data")
    entries = [(None, main)] if main else []
    for key, limit in buckets.items():
        if key != main.get("limitId") and limit != main:
            entries.append((key, limit))
    windows, notes = [], []
    for key, limit in entries:
        if not isinstance(limit, dict):
            raise UsageError("Codex returned invalid quota data")
        label = optional_text(limit.get("limitName")) or key
        prefix = f"{label}: " if label else ""
        for slot in ("primary", "secondary"):
            quota = limit.get(slot)
            if quota is None:
                continue
            if not isinstance(quota, dict):
                raise UsageError("Codex returned invalid quota data")
            name = prefix + duration(quota.get("windowDurationMins"), slot) + " window"
            windows.append(window(name, quota.get("usedPercent"), quota.get("resetsAt")))
        reached = optional_text(limit.get("rateLimitReachedType"))
        if reached:
            notes.append(prefix + "LIMIT REACHED: " + reached)
        if limit.get("spendControlReached") is True:
            notes.append(prefix + "spend control reached")
        credits = limit.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited") is True:
                notes.append(prefix + "credits: unlimited")
            elif credits.get("hasCredits") is True:
                balance = optional_text(credits.get("balance")) or "available"
                notes.append(prefix + "credits: " + balance)
            elif credits.get("hasCredits") is False:
                notes.append(prefix + "credits: none")
    if data.get("ordinaryUsageAllowed") is False:
        notes.append("ordinary included usage is currently unavailable")
    if not windows and not notes:
        raise UsageError("Codex returned no quota data for this account")
    return {"windows": windows, "notes": list(dict.fromkeys(notes))}


def fetch(profile, timeout=20, cancel=None):
    if not profile.home.is_dir():
        return {"error": "profile directory does not exist"}
    server = None
    identity = {}
    try:
        server = AppServer(profile.home, timeout, cancel)
        server.request("initialize", {"clientInfo": {
            "name": "ai-watch", "title": "ai-watch", "version": __version__}})
        server.send({"method": "initialized", "params": {}})
        account = server.request("account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict):
            raise UsageError("not logged in; run codex login for this profile")
        if account.get("type") != "chatgpt":
            raise UsageError("subscription quota requires a ChatGPT login, not API billing")
        identity = {"email": optional_text(account.get("email")),
                    "plan": optional_text(account.get("planType"))}
        result = parse_limits(server.request("account/rateLimits/read", {}))
        return dict(identity, **result, fetched_at=time.time())
    except UsageError as exc:
        return dict(identity, error=str(exc))
    except OSError:
        return dict(identity, error="could not communicate with Codex app-server")
    finally:
        if server is not None:
            server.close()
