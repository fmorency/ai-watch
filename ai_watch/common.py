"""Small shared types and validation; raw provider errors never reach output."""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class UsageError(Exception):
    """A deliberately credential-free, user-facing error."""


@dataclass(frozen=True)
class Profile:
    provider: str
    name: str
    home: Path
    keychain_service: str | None = None


def read_object(path):
    with Path(path).open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def window(name, percent, resets_at=None):
    if not number(percent):
        raise UsageError("provider returned an invalid percentage")
    if resets_at is not None:
        if not number(resets_at):
            raise UsageError("provider returned an invalid reset time")
        try:
            datetime.fromtimestamp(resets_at).astimezone()
        except (ValueError, OverflowError, OSError):
            raise UsageError("provider returned an invalid reset time") from None
    return {"name": str(name), "used_percent": percent, "resets_at": resets_at}


def optional_text(value):
    return value if isinstance(value, str) else None
