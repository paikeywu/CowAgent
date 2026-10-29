#!/usr/bin/env python3
# encoding:utf-8
"""The body-column guard — the write-boundary rule that a LEGACY writer
cannot erase.

P1-阶段三 第五轮 (F-4).  This module exists because of a measured fact:
the desktop backend that serves this machine is a PyInstaller build from
2026-10-13, and its ``storage.py`` UPSERT is::

    INSERT OR REPLACE INTO chunks (id, user_id, scope, source, path,
        start_line, end_line, text, embedding, hash, metadata,
        created_at, updated_at, hit_count, last_hit_at,
        element_released, file_sealed, sealed_owner)
    VALUES (...)

— **the four body columns are not in the list**.  ``INSERT OR REPLACE``
is internally DELETE + INSERT, so the row is REPLACED wholesale: the
columns the writer does not name fall back to their DEFAULT, i.e.
``body_path`` -> NULL, ``body_anchor`` -> NULL, ``body_hash`` -> NULL,
``text_state`` -> 'projection', ``body_origin`` -> 'unresolved'.

Measured on this machine (the forensic replay in the delivery document):
running that exact statement over a MIGRATED library reproduced the live
library's state EXACTLY — ``body_path IS NULL`` on every row, every
``+S`` anchor gone, ``body_origin`` back to 'unresolved', and
``files.chunker_version`` back to NULL (the old writer's
``INSERT OR REPLACE INTO files`` does not name that column either).

So the erasure is not a bug that a newer build fixes: **the old build is
still running and still writing.**  The anti-downgrade UPSERT in
``storage.py`` (P1-阶段三 第四轮) protects the NEW writer only.

**Why a database trigger.**  An invariant can only be enforced where
every writer must pass.  The application layer is not that place: the old
build does not go through it.  The scan rules are not that place: the old
build has its own.  The DATABASE WRITE BOUNDARY is that place — every
INSERT and every UPDATE crosses it, whoever wrote the statement.

**Why this shape (measured, not assumed).**  The candidate shapes were
each tried against a copy of a migrated library, with the old writer's
statement replayed over every row:

===========================  ==========  ==========  ==========
guard shape                  body cols   FTS rows   FTS MATCH
===========================  ==========  ==========  ==========
(none)                       erased      257         6 -> 12
AFTER INSERT + UPDATE        kept        257         6 -> 12  (dup)
BEFORE INSERT + re-INSERT    kept        257         6 -> 12  (dup)
**BEFORE INSERT + UPDATE**   **kept**    **257**     **6 -> 6**
===========================  ==========  ==========  ==========

The winning shape is a ``BEFORE INSERT`` trigger that UPDATES the
existing row in place and then ``RAISE(IGNORE)``s the offending INSERT.
Two SQLite facts make it work:

* ``INSERT OR REPLACE`` does **not** fire the ``BEFORE DELETE`` trigger
  (measured) — so a "snapshot the old row on delete" guard never sees
  anything.  This is the shape that silently does nothing, and it is the
  shape one would write by analogy with ``INSERT OR REPLACE`` = DELETE +
  INSERT.
* but the old row is **still visible** inside a ``BEFORE INSERT``
  trigger (measured) — so the trigger can copy the columns it must
  protect straight out of the row that is about to be replaced.

The ``UPDATE`` inside the trigger does not re-fire the FTS triggers (the
row's text did not change), so the FTS index stays exactly in sync —
which the two rejected shapes did NOT achieve (their FTS MATCH count
doubled: 6 -> 12, i.e. every row indexed twice).

**The guard is a TEMPORARY measure**, for as long as a legacy writer is
running.  It is a safety net under a known-bad writer, not a permanent
part of the design: once the desktop build is rebuilt from the current
source, the new UPSERT (which preserves the columns in the application
layer) is the real defence and this trigger becomes redundant.  It is
harmless when redundant — it only fires when the incoming row has no
``body_path`` AND the existing row HAS one, which a current writer never
produces.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List

#: The trigger's name.  It is NOT one of the FTS triggers the old build
#: drops, so a legacy ``sync()`` cannot remove it by accident.
GUARD_TRIGGER = "chunks_body_keep_bi"

#: The companion trigger, and the table they share.  The legacy build's
#: ``replace_file_chunks`` DELETEs the file's rows before it re-INSERTs
#: them, so the pointer has to survive a DELETE as well as an INSERT.
GUARD_TRIGGER_DELETE = "chunks_body_keep_bd"
GUARD_TABLE = "body_keep"

#: The columns the guard protects.  One list, used by the trigger body
#: and by the status check — so "the guard protects what it claims" is a
#: fact about the code, not a claim about a comment.
PROTECTED_COLUMNS = ("body_path", "body_anchor", "body_hash",
                     "text_state", "body_origin")

#: The values that mean "the writer did not decide" — so a restore may
#: overwrite them.
#:
#: ``body_origin`` is ``NOT NULL DEFAULT 'unresolved'`` and
#: ``text_state`` is ``NOT NULL DEFAULT 'projection'``, so a legacy
#: ``INSERT OR REPLACE`` does not write NULL into them — it writes the
#: DEFAULT.  A restore that only checks for NULL would therefore never
#: fire for those two columns (measured: ``body_origin`` stayed
#: 'unresolved' while the other four were restored).  The undecided
#: values are named here, and a restore replaces a column only when the
#: incoming value is one of them.
UNDECIDED_VALUES = {
    "body_origin": ("unresolved",),
    "text_state": ("projection",),
}


def _normalize_sql(sql: str | None) -> str:
    """Whitespace-normalised SQL, for comparing a trigger body.

    SQLite stores the body verbatim (minus ``IF NOT EXISTS``), so the
    comparison is exact once the whitespace is collapsed.  Comparing the
    TEXT is what makes "the guard is installed but it is the OLD guard"
    detectable — ``CREATE TRIGGER IF NOT EXISTS`` never raises, so a
    stale trigger would otherwise be accepted forever.
    """
    if not sql:
        return ""
    return re.sub(r"\s+", " ", sql).strip()


def _restore_expr(column: str) -> str:
    """The SQL that restores one protected column.

    The incoming value is replaced by the kept one when the incoming
    value is UNDECIDED — NULL, or (for the two columns with a NOT NULL
    DEFAULT) the DEFAULT value itself.  A legacy writer's
    ``INSERT OR REPLACE`` writes the DEFAULT, not NULL, so a restore
    that only tested for NULL would never fire for those two columns.
    """
    undecided = UNDECIDED_VALUES.get(column)
    if not undecided:
        return ("COALESCE(NEW.{col}, (SELECT {col} FROM {tbl} "
                "WHERE id = NEW.id))".format(col=column, tbl=GUARD_TABLE))
    tests = ["NEW.{col} IS NULL".format(col=column)]
    for value in undecided:
        tests.append("NEW.{col} = '{value}'".format(col=column, value=value))
    cond = " OR ".join(tests)
    return ("CASE WHEN {cond} THEN (SELECT {col} FROM {tbl} WHERE id = NEW.id) "
            "ELSE NEW.{col} END".format(cond=cond, col=column, tbl=GUARD_TABLE))


def trigger_sqls() -> List[str]:
    """The ONE definition of the guard's trigger bodies.

    Two triggers, because the legacy build writes in TWO shapes and
    each needs a different defence:

    * ``INSERT OR REPLACE INTO chunks (...)`` — the row is replaced
      wholesale.  The ``BEFORE INSERT`` trigger sees the OLD row still
      in the table (measured: SQLite does NOT fire ``BEFORE DELETE``
      for a REPLACE), copies the writer's new content onto it and
      raises IGNORE — so the content lands and the pointer survives.

    * ``DELETE FROM chunks WHERE path = ?`` followed by re-INSERTs
      (the legacy ``replace_file_chunks``) — the row is GONE before the
      INSERT arrives.  The ``BEFORE DELETE`` trigger snapshots the
      pointer into ``body_keep``, and the ``AFTER INSERT`` trigger
      restores it.

    Both are needed: measured on this machine, the INSERT-only guard
    survived the first shape and LOST the pointers under the second.
    """
    assignments = ", ".join(
        "%s=NEW.%s" % (col, col) for col in (
            "user_id", "scope", "source", "path", "start_line", "end_line",
            "text", "embedding", "hash", "metadata", "created_at",
            "updated_at", "hit_count", "last_hit_at", "element_released",
            "file_sealed", "sealed_owner"))
    restores = ", ".join("%s = %s" % (col, _restore_expr(col))
                         for col in PROTECTED_COLUMNS)

    insert_trigger = """
CREATE TRIGGER {name} BEFORE INSERT ON chunks
WHEN NEW.body_path IS NULL
 AND EXISTS (SELECT 1 FROM chunks c WHERE c.id = NEW.id AND c.body_path IS NOT NULL)
BEGIN
    UPDATE chunks SET
        {assignments}
    WHERE id = NEW.id;
    SELECT RAISE(IGNORE);
END""".format(name=GUARD_TRIGGER, assignments=assignments)

    delete_trigger = """
CREATE TRIGGER {name} BEFORE DELETE ON chunks
WHEN OLD.body_path IS NOT NULL
BEGIN
    INSERT OR REPLACE INTO {tbl} (id, body_path, body_anchor, body_hash,
                                   text_state, body_origin)
    VALUES (OLD.id, OLD.body_path, OLD.body_anchor, OLD.body_hash,
            OLD.text_state, OLD.body_origin);
END""".format(name=GUARD_TRIGGER_DELETE, tbl=GUARD_TABLE)

    restore_trigger = """
CREATE TRIGGER {name} AFTER INSERT ON chunks
WHEN NEW.body_path IS NULL
 AND EXISTS (SELECT 1 FROM {tbl} k WHERE k.id = NEW.id)
BEGIN
    UPDATE chunks SET
        {restores}
    WHERE id = NEW.id;
END""".format(name=GUARD_TRIGGER + "_ai", restores=restores, tbl=GUARD_TABLE)

    return [insert_trigger, delete_trigger, restore_trigger]


def table_ddl() -> str:
    """The keep table.  It is not part of the FTS machinery, so the
    legacy build's FTS rebuild cannot take it away."""
    return """
CREATE TABLE IF NOT EXISTS {tbl} (
    id TEXT PRIMARY KEY,
    body_path TEXT, body_anchor TEXT, body_hash TEXT,
    text_state TEXT, body_origin TEXT)""".format(tbl=GUARD_TABLE)


def stored_trigger_sql(conn: sqlite3.Connection) -> str:
    """The guard triggers' stored SQL, joined ('' when not installed)."""
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN (?, ?, ?)",
            (GUARD_TRIGGER, GUARD_TRIGGER_DELETE,
             GUARD_TRIGGER + "_ai")).fetchall()
    except sqlite3.Error:
        return ""
    return " ;; ".join((r[1] or "") for r in rows)


def is_installed(conn: sqlite3.Connection) -> bool:
    """True when the guard is installed AND its bodies are the current ones."""
    stored = stored_trigger_sql(conn)
    if not stored:
        return False
    expected = " ;; ".join(trigger_sqls())
    return _normalize_sql(stored) == _normalize_sql(expected)


def ensure_guard(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Make sure the guard is present and current, and say which it is.

    Returns ``{"status": present|installed|repaired|failed, ...}``.

    "Already installed" and "could not be installed" must never be the
    same answer: the first is a fact about the index, the second is a
    fact about the system's safety, and the patrol must tell them apart.
    """
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    except sqlite3.Error as e:
        return {"status": "failed", "reason": "cannot read chunks: %s" % e}
    if not columns:
        return {"status": "failed",
                "reason": "the database has no chunks table yet"}
    missing = [c for c in PROTECTED_COLUMNS if c not in columns]
    if missing:
        # A library that predates the columns has nothing to protect, and
        # the trigger would not compile against it.
        return {"status": "failed",
                "reason": "the chunks table has no %s column(s)"
                           % ", ".join(missing)}

    if is_installed(conn):
        return {"status": "present"}

    existed = bool(stored_trigger_sql(conn))
    try:
        if conn.in_transaction:
            conn.commit()
        # The drop-and-create must be ATOMIC: a guard that is absent for
        # even a moment is a window in which the legacy build can erase
        # the pointers.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(table_ddl())
        for name in (GUARD_TRIGGER, GUARD_TRIGGER_DELETE,
                     GUARD_TRIGGER + "_ai"):
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        for statement in trigger_sqls():
            conn.execute(statement)
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"status": "failed", "reason": "%s: %s" % (type(e).__name__, e)}
    return {"status": "repaired" if existed else "installed"}


def install_guard(conn: sqlite3.Connection) -> bool:
    """Install the guard.  Idempotent.  True when it is in place."""
    return ensure_guard(conn).get("status") != "failed"


def status(conn: sqlite3.Connection) -> Dict[str, Any]:
    """A reportable description of the guard's state."""
    stored = stored_trigger_sql(conn)
    out: Dict[str, Any] = {
        "trigger": GUARD_TRIGGER,
        "triggers": [GUARD_TRIGGER, GUARD_TRIGGER_DELETE,
                     GUARD_TRIGGER + "_ai"],
        "installed": bool(stored),
        "current": is_installed(conn),
        "protects": list(PROTECTED_COLUMNS),
    }
    if stored and not out["current"]:
        out["reason"] = ("the guard is installed but its body is not the "
                          "current definition (it will be repaired on the "
                          "next ensure)")
    return out


def protected_rows(conn: sqlite3.Connection) -> List[str]:
    """The ids of the rows the guard would protect right now.

    A guard that protects nothing is a guard that is installed and does
    nothing — and the two must be distinguishable, so the count is
    reported rather than assumed.
    """
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_path IS NOT NULL").fetchone()
    except sqlite3.Error:
        return []
    return [str(rows[0])] if rows else []
