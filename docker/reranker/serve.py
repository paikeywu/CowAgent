"""Launcher for the reranker HTTP service.

Wraps ``server.py``'s FastAPI app with uvicorn and takes the model
directory / host / port from a configuration file instead of from
environment variables or duplicated command-line arguments.

Why a config file: the model directory used to be written down in three
places -- the batch launcher, the Windows service XML and this file.
Changing one left the others on the old path, and the failure mode is
ugly: the service starts, reports "healthy", and then every request
fails with a model load error. ``reranker.conf`` is now the only place
the parameters live; the batch launcher and the service both come
through here.

Usage:
    python serve.py --config C:\\path\\to\\reranker.conf
    python serve.py --config ... --host 0.0.0.0 --port 9999   # overrides
"""

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BGE reranker service.")
    parser.add_argument(
        "--config",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "reranker.conf"
        ),
        help="INI file holding model_dir / host / port / threads.",
    )
    parser.add_argument("--host", default=None,
                        help="Override the configured bind address.")
    parser.add_argument("--port", type=int, default=None,
                        help="Override the configured port.")
    parser.add_argument("--model-dir", default=None,
                        help="Override the configured model directory.")
    parser.add_argument("--threads", type=int, default=0,
                        help="torch thread count (0 = use the configured value).")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # Load the config BEFORE importing server.py: server.py reads
    # MODEL_DIR / TORCH_THREADS / MAX_LENGTH at import time, so they have
    # to be in the environment by then.
    try:
        import reranker_config
    except ImportError as exc:  # pragma: no cover - defensive
        print(f"[reranker] cannot import reranker_config: {exc}", flush=True)
        return 1

    try:
        values = reranker_config.load_config(args.config)
    except reranker_config.ConfigError as exc:
        print(f"[reranker] config error: {exc}", flush=True)
        return 1

    # Config file provides the defaults; the environment overrides them
    # (an operator exporting MODEL_DIR means it), and explicit CLI
    # arguments override both.
    if args.model_dir:
        values["model_dir"] = args.model_dir
    if args.host:
        values["host"] = args.host
    if args.port:
        values["port"] = str(args.port)
    if args.threads > 0:
        values["threads"] = str(args.threads)

    os.environ["MODEL_DIR"] = values["model_dir"]
    os.environ["TORCH_THREADS"] = values.get("threads", "4")
    os.environ["MAX_LENGTH"] = values.get("max_length", "1024")
    os.environ["MAX_BATCH"] = values.get("max_batch", "64")

    try:
        import uvicorn
        from server import app
    except ImportError as e:
        print(f"[reranker] failed to import dependencies: {e}", flush=True)
        print("[reranker] install them with: pip install -r requirements.txt", flush=True)
        return 1

    host = values["host"]
    port = int(values["port"])
    print(
        f"[reranker] serving model_dir={values['model_dir']} on {host}:{port}",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
