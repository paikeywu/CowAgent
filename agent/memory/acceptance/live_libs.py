#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-E · the live-library enumeration + the three-state helpers.

This module is the part of ``phase2.py`` that the S3-E round added.  It is
a SEPARATE file for one reason: the enumeration of the live libraries is
the thing most likely to be subtly wrong (it picked the wrong ROOT once
already), so it gets its own file, its own criteria, and its own
mutant — instead of being a helper buried inside a 900-line file where
nobody looks at it again.

The rule this module implements
------------------------------
The live libraries are found by PATH SHAPE, never by a ``*.db`` glob:

  * ``<ws>/memory/long-term/index.db``            — the shared ROOT library
  * ``<ws>/agents/<id>/memory/long-term/index.db`` — one per agent

and nothing else.  A glob would also pick up the migration's own backups
(``tmp/p1s3c-backup-*/…p1-bak.db``) and the scratch copies, and then the
criteria would measure a BACKUP and report it as a live library.

The agent library's workspace root is the AGENT'S OWN DIRECTORY
(``agents/<id>``), NOT ``agents/``.  Getting that wrong makes every
agent library's truth-layer resolution fail, which shows up as a wall of
INV-1 violations that look like a product defect and are in fact a bug
in the criterion.  (This was measured: taking ``parents[3]`` instead of
``parents[2]`` produced "15 agent libraries all INV-1 RED" — which was
the CRITERION's fault, not the product's.)

And it is NOT the migration tool's own enumeration: the migration tool is
the object under test for the M-criteria, so a criterion that asks it
which libraries to measure is asking the thing it is judging.  The rule
is spelled out HERE, independently, and a criterion compares the two.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

#: The four body-externalization columns (the "four columns").
FOUR_COLUMNS = ("body_path", "body_anchor", "body_hash", "text_state")

#: The state machine's states.
STATE_PROJECTION = "projection"
STATE_MIRROR = "mirror"
STATE_VALIDATED = "validated"


class LiveLibrary:
    """One live index, and the workspace root its truth layer lives under."""

    __slots__ = ("name", "db", "workspace", "kind")

    def __init__(self, name: str, db: Path, workspace: Path, kind: str):
        self.name = name
        self.db = db
        self.workspace = workspace
        self.kind = kind          # "ROOT" or "AGENT"

    def __repr__(self) -> str:
        return "<LiveLibrary %s %s>" % (self.name, self.db)


def enumerate_live(workspace: Path) -> List[LiveLibrary]:
    """The live libraries under ``workspace``, by PATH SHAPE.

    Deliberately NOT ``agent.memory.migrate.enumerate_live``: the
    migration tool is the object under test for the M-criteria, and a
    criterion that asks the object under test which objects to measure is
    a tautology.  The two are compared by ``C-A6.1`` instead.

    The agent library's workspace root is ``agents/<id>`` — the agent's own
    directory.  See the module docstring for why that detail matters.
    """
    workspace = Path(workspace)
    out: List[LiveLibrary] = []

    root_db = workspace / "memory" / "long-term" / "index.db"
    if root_db.is_file():
        out.append(LiveLibrary("ROOT", root_db, workspace, "ROOT"))

    agents_dir = workspace / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            db = agent_dir / "memory" / "long-term" / "index.db"
            if db.is_file():
                # THE DETAIL: the workspace root is the AGENT'S OWN
                # DIRECTORY, not ``agents/``.
                out.append(LiveLibrary(agent_dir.name, db, agent_dir, "AGENT"))
    return out


def open_ro(db: Path) -> sqlite3.Connection:
    """Open an index READ-ONLY.

    Read-only is not a style choice: a criterion that opens a database it
    is only supposed to MEASURE has already changed what it measures.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """The columns of ``table`` in ``conn`` — empty when the table is absent."""
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def has_columns(conn: sqlite3.Connection, table: str, columns) -> List[str]:
    """Which of ``columns`` are MISSING from ``table``."""
    have = set(table_columns(conn, table))
    return [c for c in columns if c not in have]


def count(conn: sqlite3.Connection, sql: str, args=()) -> int:
    """A COUNT, or 0 when the query cannot run (a missing table/column)."""
    try:
        row = conn.execute(sql, args).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0


def profile_library(lib: LiveLibrary) -> Dict[str, object]:
    """The four-column profile of one library — the shape of its migration.

    Every number here is a COUNT that can be re-derived by the reader from
    the same SQL, and the SQL is printed with it.
    """
    conn = open_ro(lib.db)
    try:
        cols = set(table_columns(conn, "chunks"))
        out: Dict[str, object] = {
            "name": lib.name,
            "kind": lib.kind,
            "db": str(lib.db),
            "workspace": str(lib.workspace),
            "exists": True,
            "rows": count(conn, "SELECT COUNT(*) FROM chunks"),
        }
        if not cols:
            out["error"] = "chunks 表不存在"
            return out
        missing = [c for c in FOUR_COLUMNS if c not in cols]
        out["missing_columns"] = missing
        if missing:
            out["error"] = "缺少列: %s" % ", ".join(missing)
            return out
        out["body_path_null"] = count(
            conn, "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL")
        out["body_anchor_null"] = count(
            conn, "SELECT COUNT(*) FROM chunks "
                 "WHERE body_anchor IS NULL OR body_anchor=''")
        out["body_hash_null"] = count(
            conn, "SELECT COUNT(*) FROM chunks "
                 "WHERE body_hash IS NULL OR body_hash=''")
        out["text_states"] = dict(conn.execute(
            "SELECT text_state, COUNT(*) FROM chunks GROUP BY 1").fetchall())
        out["files_rows"] = count(conn, "SELECT COUNT(*) FROM files")
        out["files_chunk_count_null"] = count(
            conn, "SELECT COUNT(*) FROM files WHERE chunk_count IS NULL")
        out["files_chunk_count_zero"] = count(
            conn, "SELECT COUNT(*) FROM files WHERE chunk_count = 0")
        return out
    finally:
        conn.close()


def profile_all(workspace: Path) -> List[Dict[str, object]]:
    """The profile of every live library."""
    return [profile_library(lib) for lib in enumerate_live(workspace)]


def format_profile(profiles: List[Dict[str, object]]) -> str:
    """The profile as a table — the reader can count the rows themselves."""
    head = ("%-22s %-6s %6s %9s %9s %9s  %s"
            % ("库", "类型", "行数", "path=NUL", "anchor=N", "hash=N", "text_state"))
    lines = [head, "-" * len(head)]
    for p in profiles:
        states = p.get("text_states")
        if isinstance(states, dict):
            state_txt = ", ".join("%s=%d" % (k, v)
                                  for k, v in sorted(states.items()))
        else:
            state_txt = str(p.get("error", "-"))
        lines.append("%-22s %-6s %6s %9s %9s %9s  %s" % (
            p.get("name", "?"), p.get("kind", "?"), p.get("rows", "?"),
            p.get("body_path_null", "-"), p.get("body_anchor_null", "-"),
            p.get("body_hash_null", "-"), state_txt))
    return "\n".join(lines)
