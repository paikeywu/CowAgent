"""Index schema drift — the check that a stale index is LOUD (P0-17 B-1/B-3).

An index written by an older build can lack a column the current code
queries. The failure is silent in the worst way: the SQL raises
``no such column: ...``, the retrieval path catches it, and the system
reports "no matches" — indistinguishable from an empty corpus.

Two things are needed, and they are separate:

* **REPAIR** — the migration that brings one index up to the current
  schema. It runs when a database is OPENED (that is the only moment the
  connection is known to be writable) and it is idempotent.
* **DETECTION** — the check that says WHICH index is behind, so a workspace
  nobody opened yet is still reported. An index that has never been opened
  by the new build is exactly the one that will fail first, and nothing
  else would ever mention it.

The expected column set lives HERE, next to the migration, so the two
cannot drift: a column added to the schema without being added here is
reported as drift for every index until somebody notices.

REPORT-ONLY where it counts: this module never deletes anything, and the
repair it performs is an ``ALTER TABLE ADD COLUMN`` — additive, never a
rewrite, never a data loss.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

#: The columns the current code expects on ``chunks``.
#:
#: Kept as a tuple (not a set) so the drift report lists them in a stable
#: order: a report that reorders itself between runs is harder to read
#: than one that does not.
EXPECTED_CHUNK_COLUMNS: Tuple[str, ...] = (
    "id",
    "user_id",
    "scope",
    "source",
    "path",
    "start_line",
    "end_line",
    "text",
    "embedding",
    "hash",
    "metadata",
    "created_at",
    "updated_at",
    "hit_count",
    "last_hit_at",
    "element_released",
    "file_sealed",
    "sealed_owner",
    "body_path",
    "body_anchor",
    "body_hash",
    "text_state",
)

#: The columns a migration may ADD. A column that is not in this map is
#: one the current code does not know how to add, so the drift is reported
#: rather than silently invented.
ADDABLE_COLUMNS: Dict[str, str] = {
    "hit_count": "INTEGER NOT NULL DEFAULT 0",
    "last_hit_at": "INTEGER",
    "element_released": "INTEGER NOT NULL DEFAULT 0",
    "file_sealed": "INTEGER NOT NULL DEFAULT 0",
    "sealed_owner": "TEXT",
    # P1-阶段一 (D-C): the four body-externalization columns.  Every chunk
    # starts as a projection, so the new column defaults match a row that
    # never externalized: text here, no body file, no hash yet.
    "body_path": "TEXT",
    "body_anchor": "TEXT",
    "body_hash": "TEXT",
    "text_state": "TEXT NOT NULL DEFAULT 'projection'",
}


def chunk_columns(conn: sqlite3.Connection) -> Optional[set]:
    """The columns ``chunks`` actually has, or None when there is no table."""
    try:
        rows = conn.execute("PRAGMA table_info(chunks)").fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return {row[1] for row in rows}


def missing_columns(db_path: Union[str, Path]) -> List[str]:
    """The expected columns this index lacks, in the expected order.

    An unreadable or absent database yields an empty list: there is no
    drift to report about a database that is not there, and reporting one
    would make the check cry wolf on every fresh workspace.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        cols = chunk_columns(conn)
    finally:
        conn.close()
    if cols is None:
        return []
    return [c for c in EXPECTED_CHUNK_COLUMNS if c not in cols]


def repair_columns(db_path: Union[str, Path]) -> List[str]:
    """Add the missing columns to this index. Returns what was added.

    Additive and idempotent: a column that is already there is skipped,
    and a column the current code cannot add is left alone (and reported
    by :func:`missing_columns`).
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    added: List[str] = []
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error:
        return []
    try:
        cols = chunk_columns(conn)
        if cols is None:
            return []
        for name in EXPECTED_CHUNK_COLUMNS:
            if name in cols:
                continue
            ddl = ADDABLE_COLUMNS.get(name)
            if ddl is None:
                continue
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {ddl}")
            added.append(name)
        if added:
            conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return added


def scan_workspaces(roots: List[Union[str, Path]]) -> Dict[str, List[str]]:
    """Every workspace root that carries an index BEHIND the schema.

    Returns ``{workspace_root: [missing columns]}`` for the roots whose
    index exists and is missing something. A root with no index at all is
    not drift (nothing has been written yet); a root whose index is
    complete is not drift either.
    """
    drift: Dict[str, List[str]] = {}
    for root in roots:
        root_path = Path(root)
        db = root_path / "memory" / "long-term" / "index.db"
        if not db.is_file():
            continue
        missing = missing_columns(db)
        if missing:
            drift[str(root_path)] = missing
    return drift


def describe_drift(drift: Dict[str, List[str]]) -> str:
    """One line per drifting workspace, naming the workspace and columns.

    A drift report that says "an index is behind" is half a report: the
    whole point is to name WHICH index and WHICH columns, because that is
    what makes it actionable.
    """
    if not drift:
        return "no schema drift"
    lines = []
    for root in sorted(drift):
        cols = ", ".join(drift[root])
        lines.append(f"{root}: missing {cols}")
    return "; ".join(lines)


def schema_fingerprint(db_path: Union[str, Path]) -> Optional[str]:
    """A stable fingerprint of the index's chunks schema.

    P1-阶段一 (M1): the gate that decides whether an index may serve needs
    one comparable token, not a column list it must re-derive. The token is
    the sha256 of the ordered ``PRAGMA table_info(chunks)`` column set, so
    two indexes that can answer the same queries share a fingerprint and
    two that cannot do not. ``None`` means there is no readable chunks
    table to fingerprint (a fresh workspace, or a damaged file) — which is
    itself a meaningful answer, distinct from any schema.
    """
    import hashlib

    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute("PRAGMA table_info(chunks)").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not rows:
        return None
    # cid, name, type, notnull, dflt, pk — the shape that decides whether
    # a query can run, in the order the table defines it.
    material = "|".join(":".join(str(p) for p in row) for row in rows)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
