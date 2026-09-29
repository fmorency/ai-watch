"""Keep bundled libraries out of the system Keychain helper."""

import os
import sys


def external_command_env():
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        # PyInstaller changes this for its own Python extensions. An installed
        # helper must use the library path from before the bundle was started.
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env
