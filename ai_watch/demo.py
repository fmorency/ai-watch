"""Synthetic accounts only; the demo never discovers files or calls providers."""


def accounts(now):
    def quota(name, percent, seconds):
        return {"name": name, "used_percent": percent, "resets_at": now + seconds}

    return [
        {"provider": "codex", "name": "personal", "email": "alex@example.com", "plan": "pro",
         "windows": [quota("5h window", 34, 7200), quota("7d window", 62, 259200)],
         "notes": ["credits: none"], "fetched_at": now},
        {"provider": "codex", "name": "work", "email": "alex@example.org", "plan": "team",
         "windows": [quota("5h window", 78, 3600), quota("7d window", 45, 172800)],
         "fetched_at": now},
        {"provider": "claude", "name": "personal", "email": "alex@example.com", "plan": "max",
         "windows": [quota("session", 23, 10800), quota("week (all models)", 91, 86400),
                     quota("week (Sonnet)", 18, 86400)], "fetched_at": now - 45, "cached": True},
    ]
