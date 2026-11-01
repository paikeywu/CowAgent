"""The support layer the sweep tools share: backups, manifests, rollback.

**Why this module exists.**  Three tools have to answer the same
question — "if this write goes wrong, how do I get back?" — and they
must answer it the SAME way, or "one command to sweep the fleet" is
one command with three different rollback stories.  So the backup and
the rollback live here, once.

**The backup is taken through the product's own API**, not through a
file copy.  ``msg_migrate.backup_database`` checkpoints the WAL first
and then ``VACUUM INTO``s a fresh file; a plain ``shutil.copy`` of a
live SQLite file is a PARTIAL snapshot (the WAL still holds rows the
main file does not), so a restore from it silently loses the newest
rows.  The rollback goes through ``rollback_via_backup_api`` for the
same reason: it does NOT move or rename files, so the stale WAL is
never applied to the fresh database.

**Two levels of rollback, and the difference matters:**

  * **whole-library rollback** — restore the database file from the
    backup.  Undoes everything the sweep did to that library.
  * **ledger-only rollback** — re-INSERT the exact rows the sweep
    deleted from ``cert_ledger``.  Undoes ONE step, and it does so
    without touching anything else the sweep may have done since.

The second one is why the orphan sweeper writes a manifest: the
manifest IS the undo list, and it is readable by a human (the whole
point of 裁决 C3 — "不可逆操作必须可解释、可回滚").
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

#: The columns of ``cert_ledger`` that the orphan sweeper restores.
LEDGER_COLUMNS = ("id", "text_state", "body_origin", "body_path",
                  "body_hash", "certified_at", "certified_by")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def sha256_of(path: Union[str, Path]) -> str:
    """The sha256 of a file's bytes."""
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Union[str, Path], data: Any) -> str:
    """Write ``data`` as UTF-8 JSON, atomically enough for a report.

    The temporary file is in the SAME directory, so the rename is on
    one filesystem and cannot half-land.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(str(tmp), str(p))
    return str(p)


def read_json(path: Union[str, Path]) -> Any:
    with open(str(path), "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# the database backup / restore
# ---------------------------------------------------------------------------

def backup_library(db: Union[str, Path], backup_dir: Union[str, Path],
                   tag: str = "pre-sweep", name: str = "") -> Dict[str, Any]:
    """Take a checkpointed backup of ONE library.

    ``name`` is the library's identity in the backup's file name.  It
    MUST be passed: deriving it from the database's path would name
    every rehearsal copy after the scratch directory it happens to sit
    in, and every backup would overwrite the previous one — a backup
    directory that holds one backup is not a backup.

    Returns ``{"backup": path, "size_bytes": n, "sha256": hex}`` — the
    hash is taken over the backup file, so the report can prove later
    that the backup was not rewritten.
    """
    from agent.memory.msg_migrate import backup_database

    src = Path(db)
    if not src.is_file():
        return {"error": "the index does not exist: %s" % src}
    label = name or src.parent.parent.name or src.stem
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in label)
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ("%s.%s.db" % (safe, tag))
    if target.exists():
        target.unlink()
    rep = backup_database(src, target)
    rep["sha256"] = sha256_of(target)
    rep["source"] = str(src)
    rep["name"] = label
    return rep


def restore_library(db: Union[str, Path], backup: Union[str, Path]
                     ) -> Dict[str, Any]:
    """Restore ONE library IN PLACE from a backup made by
    :func:`backup_library`."""
    from agent.memory.msg_migrate import rollback_via_backup_api

    src = Path(db)
    bak = Path(backup)
    if not bak.is_file():
        return {"error": "the backup does not exist: %s" % bak}
    rep = rollback_via_backup_api(src, bak)
    rep["restored_from"] = str(bak)
    return rep


# ---------------------------------------------------------------------------
# the ledger: read it, and put it back
# ---------------------------------------------------------------------------

def ledger_rows(conn: sqlite3.Connection,
                ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """The ``cert_ledger`` rows (optionally only these ids)."""
    cols = ", ".join(LEDGER_COLUMNS)
    if ids is None:
        sql = "SELECT %s FROM cert_ledger ORDER BY id" % cols
        args: List[Any] = []
    else:
        id_list = list(ids)
        if not id_list:
            return []
        sql = "SELECT %s FROM cert_ledger WHERE id IN (%s) ORDER BY id" % (
            cols, ",".join("?" * len(id_list)))
        args = id_list
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        raise RuntimeError("cannot read cert_ledger: %s" % e)
    out = []
    for r in rows:
        out.append({c: r[i] for i, c in enumerate(LEDGER_COLUMNS)})
    return out


def restore_ledger_rows(conn: sqlite3.Connection,
                        rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Re-INSERT the given ledger rows (the undo of a prune).

    Uses ``INSERT OR REPLACE`` so a row that is already back does not
    raise.  The write is one transaction: a half-restored ledger is
    worse than an unrestored one.
    """
    if not rows:
        return {"restored": 0}
    cols = ", ".join(LEDGER_COLUMNS)
    marks = ", ".join("?" * len(LEDGER_COLUMNS))
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO cert_ledger (%s) VALUES (%s)"
                    % (cols, marks),
                    tuple(row.get(c) for c in LEDGER_COLUMNS))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    except sqlite3.Error as e:
        return {"error": "sqlite error: %s" % e, "restored": 0}
    return {"restored": len(rows)}
