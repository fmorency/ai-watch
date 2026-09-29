"""Entry point for the single executable and its ai-usage symlink."""

import os
import ssl
import sys
from pathlib import Path

import certifi

from ai_watch.cli import main, usage_main


if __name__ == "__main__":
    # A bundled Python can reference its build machine's certificate location.
    # Prefer user overrides and system roots; use bundled roots as a fallback.
    trust = ssl.get_default_verify_paths()
    if (not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR")
            and not trust.cafile and not trust.capath):
        os.environ["SSL_CERT_FILE"] = certifi.where()
    command = usage_main if Path(sys.argv[0]).name == "ai-usage" else main
    raise SystemExit(command())
