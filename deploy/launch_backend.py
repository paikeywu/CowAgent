#!/usr/bin/env python
# encoding:utf-8
r"""Launch the CowAgent backend with optional LAN settings.

WHY A PYTHON LAUNCHER INSTEAD OF A CMD `CALL`
    cmd's CALL takes a command or a batch LABEL, not a file path. Even with
    a .cmd extension, `cmd /c 'call "C:\path\settings.cmd"'` loses the
    nested quotes and cmd reports:
        '"C:\path\settings.cmd"' is not recognized as an internal or
        external command
    So the settings never reached python and the bind address never
    changed. Python has no such quoting layer: it reads the file and sets
    os.environ directly.

WHAT IT DOES
    1. reads  <user profile>/.cow/lan-access.cmd   (KEY=VALUE lines)
    2. copies each KEY into os.environ (a KEY already in the environment
       wins, so the launcher can be overridden from the command line)
    3. runs app.py in this same interpreter via runpy

If the settings file is absent, the backend starts exactly as before -
loopback only, no password. That is the default and the safe state.

The settings file lives in the USER's home directory, never in the repo:
it holds a password, and the repo is a git working tree.
"""
import io
import os
import runpy
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SETTINGS = os.path.join(os.path.expanduser("~"), ".cow", "lan-access.cmd")


def load_settings(path):
    """Read KEY=VALUE lines into a dict. Malformed lines are skipped.

    Accepts both plain 'KEY=VALUE' lines and the cmd-flavoured
    'set "KEY=VALUE"' lines. The cmd form is what a person would type by
    hand, so it is accepted rather than rejected - but the 'set' keyword
    and the quotes must be stripped, or the KEY comes out as
    'set "WEB_HOST' and the setting silently does nothing.
    """
    values = {}
    if not os.path.exists(path):
        return values
    with io.open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("REM") or line.startswith("#"):
                continue
            # strip a leading cmd 'set' keyword
            low = line.lower()
            if low.startswith("set "):
                line = line[4:].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().strip('"').strip()
            value = value.strip().strip('"').strip()
            if key:
                values[key] = value
    return values


def main():
    settings = load_settings(SETTINGS)
    if not settings:
        print("[launcher] no LAN settings file - starting loopback-only",
              flush=True)
    else:
        for key, value in settings.items():
            # An existing environment variable wins: an explicit override
            # on the command line should beat the file.
            if key in os.environ and os.environ[key] != value:
                print(f"[launcher] {key}: keeping the environment value "
                      f"(file says something else)", flush=True)
                continue
            os.environ[key] = value
            shown = "<hidden>" if "PASSWORD" in key.upper() else value
            print(f"[launcher] applied {key}={shown}", flush=True)

    os.chdir(REPO)
    sys.path.insert(0, REPO)
    print(f"[launcher] starting app.py from {REPO}", flush=True)
    runpy.run_path(os.path.join(REPO, "app.py"), run_name="__main__")


if __name__ == "__main__":
    main()
