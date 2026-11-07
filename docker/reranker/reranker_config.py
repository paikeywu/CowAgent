"""Read the reranker configuration file -- the single source of truth.

Why a config file instead of environment variables scattered around:

The model directory used to be written down in three places (the batch
launcher, the Windows service XML, the compose file). A change to one of
them silently left the others pointing at the old path, and the failure
mode is nasty: the service starts, reports "healthy", and then every
single request fails with a model load error. One file, read by everyone,
removes that class of bug by construction.

The file is INI (``configparser``), read with interpolation disabled so a
``%`` in a path is not mistaken for a format placeholder.

This module is imported by ``serve.py`` and also runnable as a script.
The script mode exists for ``start-reranker.bat``: batch needs the port
and the model directory to do its health check, and parsing INI inside
cmd is where quoting bugs live -- so batch asks Python instead.
"""

from __future__ import annotations

import configparser
import os
import sys
from typing import Dict

DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "reranker.conf"
)

SECTION = "reranker"

# Keys this module knows about, and the environment variable each one
# maps to. server.py reads those env vars at import time, so the mapping
# has to happen BEFORE server.py is imported.
_ENV_MAP = {
    "model_dir": "MODEL_DIR",
    "threads": "TORCH_THREADS",
    "max_length": "MAX_LENGTH",
    "max_batch": "MAX_BATCH",
}


class ConfigError(RuntimeError):
    """Raised when the config file is missing, malformed or incomplete."""


def load_config(path: str = DEFAULT_CONFIG) -> Dict[str, str]:
    """Return the ``[reranker]`` section as a plain dict of strings."""
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            parser.read_file(fh)
    except configparser.Error as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc

    if not parser.has_section(SECTION):
        raise ConfigError(
            f"{path} has no [{SECTION}] section (found: "
            f"{parser.sections() or 'none'})"
        )

    values = {k: v.strip() for k, v in parser.items(SECTION)}
    missing = [k for k in ("model_dir", "host", "port") if not values.get(k)]
    if missing:
        raise ConfigError(f"{path} is missing required key(s): {missing}")

    return values


def apply_to_environ(values: Dict[str, str], environ=None) -> Dict[str, str]:
    """Push the config into the environment server.py will read.

    Existing environment variables win: an operator who exports
    ``MODEL_DIR`` to point at a different checkpoint means it. The config
    file is the default, the environment is the override.
    """
    env = os.environ if environ is None else environ
    applied = {}
    for key, env_name in _ENV_MAP.items():
        value = values.get(key)
        if value and env_name not in env:
            env[env_name] = value
            applied[env_name] = value
    return applied


def main(argv=None) -> int:
    """Print the effective config as ``KEY=VALUE`` lines (for batch)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    path = DEFAULT_CONFIG
    if argv and argv[0] == "--config":
        path = argv[1]

    try:
        values = load_config(path)
    except ConfigError as exc:
        print(f"[reranker-config] {exc}", file=sys.stderr)
        return 1

    for key in ("model_dir", "host", "port", "threads", "max_length", "log"):
        if key in values:
            print(f"{key}={values[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
