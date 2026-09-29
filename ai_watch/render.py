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
    stamp = datetime.fromtimestamp(now).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    suffix = "  DEMO — synthetic data" if report.get("demo") else ""
    lines = [f"{bold}ai-watch{reset}  {dim}{stamp}{reset}{suffix}"]
    for provider in ("codex", "claude"):
        accounts = [a for a in report["accounts"] if a["provider"] == provider]
        if not accounts:
            continue
        lines.extend(["", f"{bold}{provider.upper()}{reset}"])
        for account in accounts:
            email = f"  {safe_text(account['email'])}" if account.get("email") else ""
            plan = f"  ({safe_text(account['plan'])})" if account.get("plan") else ""
            lines.append(f"  {bold}{safe_text(account['name'])}{reset}{email}{dim}{plan}{reset}")
            for quota in account.get("windows", []):
                percent = quota["used_percent"]
                filled = round(min(percent, 100) * 14 / 100)
                on, off = ("#", "-") if ascii_only else ("█", "░")
                bar = on * filled + off * (14 - filled)
                tint = ("\033[31m" if percent >= 90 else "\033[33m" if percent >= 70 else "\033[32m") if color else ""
                label = safe_text(quota["name"])
                line = f"    {tint}{bar} {percent:5.1f}%{reset}  {label}"
                if quota.get("resets_at") is not None:
                    line += f"  {dim}resets {when(quota['resets_at'], now)}{reset}"
                lines.append(line)
            for note in account.get("notes", []):
                lines.append(f"    {dim}{safe_text(note)}{reset}")
            error = account.get("error")
            if error:
                if account.get("windows"):
                    age = max(0, int((now - account["fetched_at"]) / 60))
                    prefix = f"STALE ({age}m old)"
                else:
                    prefix = "unavailable"
                lines.append(f"    {prefix}: {safe_text(error)}")
            elif account.get("cached") and account.get("windows"):
                age = max(0, int(now - account["fetched_at"]))
                lines.append(f"    {dim}cached {age}s ago{reset}")
            if account.get("retry_at"):
                lines.append(f"    {dim}retry {when(account['retry_at'], now)}{reset}")
    if not report["accounts"]:
        lines.extend(["", "No profiles found. Sign in to Codex or Claude Code, or use --config.",
                      "See examples/config.json for multiple accounts."])
    text = "\n".join(lines)
    return text.encode("ascii", errors="replace").decode() if ascii_only else text
