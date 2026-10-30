"""
Embedding-related index utilities.

We don't keep a sidecar state file — the SQLite index is the source of truth
and config.json is the source of intent. The two functions below are the
only things needing on-disk awareness:

  detect_index_dim         : read the dim of stored vectors (display-only)
  detect_chunker_version   : read the chunker version that produced the index
  cleanup_legacy_state_file: remove old embedding_state.json from earlier
                             versions; safe no-op when absent.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, os.PathLike]


def detect_index_dim(storage) -> Optional[int]:
    """Return the dim of the first stored embedding, or None if the index
    has no embeddings. Used by /memory status."""
    try:
        row = storage.conn.execute(
            "SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    if not row or not row["embedding"]:
        return None
    try:
        raw = row["embedding"]
        if isinstance(raw, (bytes, bytearray)):
            # New BLOB format: 4 bytes per float32
            return len(raw) // 4
        emb = json.loads(raw)
        return len(emb) if isinstance(emb, list) else None
    except (json.JSONDecodeError, TypeError, Exception):
        return None


def detect_chunker_version(storage) -> Optional[int]:
    """Return the chunker version recorded for the index, or None when the
    index carries no record at all.

    `None` means the index predates chunker-version tracking (or was built by
    an unknown strategy), i.e. its chunk boundaries may not match the current
    algorithm — /memory status uses this to suggest a rebuild. Chunked files
    are only re-split when their content hash changes, so a chunker change
    alone would otherwise never refresh existing boundaries.

    **P1-阶段三 第八轮 (T4): BOTH key spellings are read.**

    The `_meta` key was renamed from `chunker_version` to
    `last_migration_chunker_version` — the new name says what the value
    actually IS (the version at the last migration), where the old name
    claimed to describe the whole index.  The rename does NOT delete the
    old key: a library stamped before the rename carries only the old
    spelling, and a reader that knows one spelling would report "no
    version" for a library that HAS one.

    The current name wins when both are present (it is the newer fact);
    the legacy name is the fallback.  The order is: current, then legacy.

    **Why this matters here and not only in the database.**  `/memory
    status` uses this answer to tell the user whether the index should
    be rebuilt.  A reader that returned None for a half-renamed library
    would tell the user "built by an older strategy" — which is a
    guess dressed as a fact.  The answer must be the fact that is
    actually recorded, under whichever name it was recorded.
    """
    for key in ("last_migration_chunker_version", "chunker_version"):
        try:
            raw = storage.get_meta(key)
        except Exception:
            raw = None
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            # A malformed value is not a version — keep looking rather
            # than reporting a guess.
            continue
    return None


def cleanup_legacy_state_file(db_path: PathLike) -> None:
    """Remove old embedding_state.json files from earlier versions.
    Safe to call repeatedly; no-op if the file is absent."""
    legacy = Path(db_path).parent / "embedding_state.json"
    try:
        legacy.unlink(missing_ok=True)
    except Exception:
        pass
