"""Terminal rendering independent of collection and credentials."""

from datetime import datetime


def safe_text(value):
    # Account and model names are untrusted terminal input, including OSC and C1.
    return "".join(c for c in str(value) if c.isprintable())


def when(timestamp, now):
    if timestamp is None:
        return "unknown"
    try:
        date = datetime.fromtimestamp(timestamp).astimezone()
    except (ValueError, OverflowError, OSError):
        return "unknown"
    delta = timestamp - now
    if delta <= 0:
        return "now"
    days, remainder = divmod(int(delta), 86400)
    hours, seconds = divmod(remainder, 3600)
    relative = f"{days}d {hours}h" if days else (f"{hours}h {seconds // 60}m" if hours else f"{seconds // 60}m")
    return f"{date:%a %b %d %H:%M} ({relative})"


def render(report, *, color=False, ascii_only=False):
    reset, bold, dim = ("\033[0m", "\033[1m", "\033[2m") if color else ("", "", "")
    now = report["generated_at"]
    all_accounts = report["accounts"]
    name_width = max([10] + [len(safe_text(a["name"])) for a in all_accounts])
    plan_width = max([5] + [len(safe_text(a.get("plan") or "?")) for a in all_accounts])
    show_email = any(a.get("email") for a in all_accounts)
    email_width = max([32] + [len(safe_text(a.get("email") or "")) for a in all_accounts])
    separator = " | " if ascii_only else " · "

    def account_prefix(account):
        fields = [f"{safe_text(account['name']):<{name_width}}"]
        if show_email:
            fields.append(f"{safe_text(account.get('email') or ''):<{email_width}}")
        fields.append(f"{safe_text(account.get('plan') or '?'):<{plan_width}}")
        return "  " + " ".join(fields) + " "

    lines = [f"{dim}ai-watch DEMO - synthetic data{reset}"] if report.get("demo") else []
    captions = {
        "codex": "live quota via usage API (cached up to 1m)",
        "claude": "live quota via usage API (cached up to 2m)",
    }
    for provider in ("codex", "claude"):
        accounts = [a for a in all_accounts if a["provider"] == provider]
        if not accounts:
            continue
        if lines:
            lines.append("")
        lines.append(f"{bold}{provider.upper()}{reset}  {dim}{captions[provider]}{reset}")
        for account in accounts:
            head = account_prefix(account)
            pad = " " * len(head)
            details = []
            for quota in account.get("windows", []):
                percent = quota["used_percent"]
                filled = round(min(percent, 100) * 14 / 100)
                on, off = ("#", "-") if ascii_only else ("█", "░")
                bar = on * filled + off * (14 - filled)
                tint = ("\033[31m" if percent >= 90 else "\033[33m" if percent >= 70 else "\033[32m") if color else ""
                label = safe_text(quota["name"])
                line = f"{tint}{bar}{reset} {tint}{percent:5.1f}%{reset}  {label}"
                if quota.get("resets_at") is not None:
                    line += f"{dim}{separator}resets {when(quota['resets_at'], now)}{reset}"
                details.append(line)
            if account.get("notes"):
                notes = separator.join(safe_text(note) for note in account["notes"])
                details.append(f"{dim}{notes}{reset}")
            error = account.get("error")
            retry = f"{separator}retry {when(account['retry_at'], now)}" if account.get("retry_at") else ""
            if error:
                if account.get("windows") or account.get("notes"):
                    age = max(0, int((now - account["fetched_at"]) / 60))
                    prefix = f"STALE ({age}m old)"
                else:
                    prefix = "unavailable"
                details.append(f"{dim}{prefix}: {safe_text(error)}{retry}{reset}")
            elif retry:
                details.append(f"{dim}{retry[len(separator):]}{reset}")
            for index, detail in enumerate(details or ["unavailable: no quota data"]):
                lines.append((head if index == 0 else pad) + detail)
    if not all_accounts:
        lines.extend(["No profiles found. Sign in to Codex or Claude Code, or use --config.",
                      "See examples/config.json for multiple accounts."])
    text = "\n".join(lines)
    return text.encode("ascii", errors="replace").decode() if ascii_only else text
