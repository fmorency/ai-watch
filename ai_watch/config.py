"""Configuration contains profile locations, never credentials."""

import os
import re
from pathlib import Path

from .common import Profile, UsageError, read_object


def config_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ai-watch" / "config.json"


def cache_path():
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "ai-watch"


def expanded_path(value, base=None):
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve()


def load_profiles(path=None):
    explicit = path is not None or bool(os.environ.get("AI_WATCH_CONFIG"))
    target = expanded_path(str(path or os.environ.get("AI_WATCH_CONFIG") or config_path()))
    if not explicit and not target.exists():
        profiles = []
        for provider, variable, default in (
            ("codex", "CODEX_HOME", ".codex"),
            ("claude", "CLAUDE_CONFIG_DIR", ".claude"),
        ):
            configured = os.environ.get(variable)
            home = expanded_path(configured or str(Path.home() / default))
            if configured or home.is_dir():
                profiles.append(Profile(provider, "default", home))
        return profiles
    try:
        data = read_object(target)
    except (OSError, ValueError):
        raise UsageError(f"cannot read configuration at {target}; expected a JSON object") from None
    if set(data) != {"profiles"} or not isinstance(data["profiles"], list):
        raise UsageError("configuration must contain only a profiles array")
    profiles, names, homes = [], set(), set()
    for index, entry in enumerate(data["profiles"], 1):
        error = f"invalid profile #{index}"
        if not isinstance(entry, dict) or set(entry) - {"provider", "name", "home", "keychain_service"}:
            raise UsageError(f"{error}: use provider, name, home, and optional keychain_service")
        provider, name, home = (entry.get(key) for key in ("provider", "name", "home"))
        if provider not in ("codex", "claude"):
            raise UsageError(f"{error}: provider must be codex or claude")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", name):
            raise UsageError(f"{error}: name must be 1–48 letters, digits, dots, dashes, or underscores")
        if not isinstance(home, str) or not home.strip() or "\0" in home:
            raise UsageError(f"{error}: home must be a directory path")
        service = entry.get("keychain_service")
        if service is not None and (provider != "claude" or not isinstance(service, str)
                                    or not service.strip() or any(ord(c) < 32 for c in service)):
            raise UsageError(f"{error}: keychain_service must be a nonempty Claude Keychain service name")
        resolved = expanded_path(home, target.parent)
        if (provider, name) in names or (provider, resolved) in homes:
            raise UsageError(f"{error}: duplicate name or directory for this provider")
        names.add((provider, name))
        homes.add((provider, resolved))
        profiles.append(Profile(provider, name, resolved, service))
    return profiles


def select_profiles(profiles, providers=(), names=()):
    selected = [p for p in profiles if not providers or p.provider in providers]
    for name in names:
        if not any(name in (p.name, f"{p.provider}:{p.name}") for p in selected):
            raise UsageError(f"no selected profile matches {name!r}")
    return [p for p in selected if not names or any(
        name in (p.name, f"{p.provider}:{p.name}") for name in names)]
