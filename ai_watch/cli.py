"""Shell-independent watch loop and one-shot reports."""

import argparse
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import __version__, demo
from .common import UsageError
from .config import load_profiles, select_profiles
from .render import render


def positive_seconds(value):
    try:
        value = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive number of seconds") from None
    if not math.isfinite(value) or value < 1:
        raise argparse.ArgumentTypeError("must be at least 1 second and finite")
    return value


def parser(watch):
    result = argparse.ArgumentParser(
        prog="ai-watch" if watch else "ai-usage",
        description="Live Codex and Claude Code subscription quotas across your accounts.",
    )
    if watch:
        result.add_argument("interval", nargs="?", type=positive_seconds, default=60,
                            help="seconds between refreshes (default: 60)")
    result.add_argument("--once", action="store_true", help="print one snapshot and exit")
    result.add_argument("--codex", action="store_true", help="include Codex accounts")
    result.add_argument("--claude", action="store_true", help="include Claude accounts")
    result.add_argument("--config", metavar="PATH", help="profile JSON file (otherwise use XDG config or defaults)")
    result.add_argument("--profile", action="append", default=[], metavar="NAME",
                        help="select a name or provider:name; repeat for multiple profiles")
    result.add_argument("--timeout", type=positive_seconds, default=20, metavar="SECONDS",
                        help="Codex probe deadline / Claude network timeout (default: 20)")
    result.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    result.add_argument("--hide-email", action="store_true", help="omit email addresses from text and JSON")
    result.add_argument("--ascii", action="store_true", help="use ASCII output and progress bars")
    colors = result.add_mutually_exclusive_group()
    colors.add_argument("--color", action="store_true", help="force ANSI colors")
    colors.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    result.add_argument("--demo", action="store_true", help="use synthetic accounts, without files or network")
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return result


def collect(profiles, timeout):
    # POSIX-specific provider imports stay out of --help and --demo.
    from . import claude, codex

    if not profiles:
        return []
    cancel = threading.Event()
    results = {}
    with ThreadPoolExecutor(max_workers=min(len(profiles), 16)) as pool:
        jobs = {pool.submit(codex.fetch if p.provider == "codex" else claude.fetch,
                            p, timeout, cancel): p for p in profiles}
        try:
            for job in as_completed(jobs):
                profile = jobs[job]
                try:
                    results[profile] = job.result()
                except Exception:
                    # Third-party data and OS exceptions can contain secrets; never echo them.
                    results[profile] = {"error": "usage probe failed; check login and provider CLI version"}
        except BaseException:
            cancel.set()
            for job in jobs:
                job.cancel()
            raise
    return [dict(results[p], provider=p.provider, name=p.name) for p in profiles]


def run(argv=None, *, watch=True):
    args = parser(watch).parse_args(argv)
    providers = [p for p in ("codex", "claude") if getattr(args, p)]
    if not args.demo and os.name != "posix":
        raise UsageError("live monitoring requires Linux, macOS, or WSL")
    if args.demo:
        profiles = []
        for name in args.profile:
            if not any(name in (a["name"], f"{a['provider']}:{a['name']}")
                       and (not providers or a["provider"] in providers) for a in demo.accounts(0)):
                raise UsageError(f"no selected demo profile matches {name!r}")
    else:
        profiles = select_profiles(load_profiles(args.config), providers, args.profile)
    once = not watch or args.once or args.json or not sys.stdout.isatty() or os.environ.get("TERM") == "dumb"
    color = not args.no_color and (args.color or (
        sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"))
    alternate = not once
    try:
        if alternate:
            print("\033[?1049h\033[?25lLoading usage…", end="", flush=True)
        while True:
            started = time.monotonic()
            if args.demo:
                accounts = [a for a in demo.accounts(time.time()) if not providers or a["provider"] in providers]
                if args.profile:
                    accounts = [a for a in accounts if any(
                        name in (a["name"], f"{a['provider']}:{a['name']}") for name in args.profile)]
            else:
                accounts = collect(profiles, args.timeout)
            if args.hide_email:
                for account in accounts:
                    account.pop("email", None)
            report = {"schema_version": 1, "generated_at": time.time(),
                      "demo": args.demo, "accounts": accounts}
            if args.json:
                print(json.dumps(report, indent=2, allow_nan=False))
            else:
                if alternate:
                    print("\033[H\033[2J", end="")
                print(render(report, color=color, ascii_only=args.ascii), flush=True)
            if once or not accounts:
                return 1 if not accounts or any(a.get("error") for a in accounts) else 0
            time.sleep(max(1, args.interval - (time.monotonic() - started)))
    finally:
        if alternate:
            print("\033[0m\033[?25h\033[?1049l", end="", flush=True)


def entry(argv=None, *, watch):
    try:
        return run(argv, watch=watch)
    except UsageError as exc:
        print(f"ai-watch: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Avoid another BrokenPipeError while Python flushes stdout at shutdown.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0


def main(argv=None):
    return entry(argv, watch=True)


def usage_main(argv=None):
    return entry(argv, watch=False)
