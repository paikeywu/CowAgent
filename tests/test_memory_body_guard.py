#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 第五轮 (F-4): the body-column guard.

The desktop build from 2026-10-13 is still running on this machine and
writes with ``INSERT OR REPLACE INTO chunks (...)`` — a column list that
does not name the four body columns.  Measured on a migrated library,
that statement replaces the row wholesale and the columns fall back to
their DEFAULT (``body_path`` -> NULL, ``body_origin`` -> 'unresolved',
``files.chunker_version`` -> NULL).

These tests pin the guard down:

* it is installed by opening the library through the REAL
  ``MemoryStorage`` path (not by a side effect of the test);
* a legacy write does NOT erase the body columns;
* a legacy write does NOT desynchronise the FTS index;
* the guard does NOT fire for a current writer (a row that arrives with
  a body_path is written normally);
* the guard survives the legacy build's FTS rebuild (it drops the six
  FTS triggers; it must not drop this one);
* the guard's own state is reportable: present / installed / repaired /
  failed must be distinguishable (a guard that is "installed" but stale
  is not the current guard).

The reverse case is the point: with the guard DROPPED, the legacy write
erases the columns and the test goes red.  Without that, the test would
pass with the guard absent — it would prove nothing.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory import body_guard  # noqa: E402
from agent.memory.storage import MemoryStorage  # noqa: E402


#: The 2026-10-13 writer's column list, verbatim.  This is the statement
#: that erases the body columns.
LEGACY_CHUNK_COLUMNS = (
    "id, user_id, scope, source, path, start_line, end_line, text, "
    "embedding, hash, metadata, created_at, updated_at, hit_count, "
    "last_hit_at, element_released, file_sealed, sealed_owner")

LEGACY_CHUNK_UPSERT = (
    "INSERT OR REPLACE INTO chunks (%s) VALUES (%s)"
    % (LEGACY_CHUNK_COLUMNS, ",".join("?" * len(LEGACY_CHUNK_COLUMNS.split(",")))))

LEGACY_FILES_UPSERT = (
    "INSERT OR REPLACE INTO files (path, source, hash, mtime, size, updated_at) "
    "VALUES (?,?,?,?,?,?)")


def _make_library(tmp_path: Path, with_pointer: bool = True) -> Path:
    """A small library with the body columns, one row with a pointer.

    The library is built through the REAL ``MemoryStorage`` path, so the
    FTS table, its sync triggers and the guard are all present and
    CONSISTENT.  A hand-written ``CREATE TABLE`` would produce a library
    whose FTS index does not contain the row — and then the guard's
    in-place UPDATE would delete a non-existent FTS row and SQLite would
    report "database disk image is malformed".  That is a property of
    the FIXTURE, not of the guard, so the fixture has to be real.
    """
    db = tmp_path / "index.db"
    body = (("MEMORY.md", "L1-2", "sha256:abc", "file") if with_pointer
            else (None, None, None, "unresolved"))
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, start_line, "
            "end_line, text, hash, created_at, updated_at, body_path, "
            "body_anchor, body_hash, text_state, body_origin) "
            "VALUES ('row1', NULL, 'shared', 'memory', 'MEMORY.md', 1, 2, "
            "'hello', 'h1', '2026-01-01', '2026-01-01', ?, ?, ?, "
            "'projection', ?)", body)
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, updated_at, "
            "chunk_count, chunker_version) "
            "VALUES ('MEMORY.md', 'memory', 'fh', 1, 5, '2026-01-01', 1, 4)")
        conn.commit()
    finally:
        storage.close()
    return db


def _legacy_replace_file(conn: sqlite3.Connection, path: str) -> None:
    """The legacy build's ``replace_file_chunks`` for one file.

    It DELETEs the file's rows and re-INSERTs them.  Measured on the
    2026-10-13 build (``storage.py``: ``DELETE FROM chunks WHERE path = ?``)
    — and it is the shape that defeats an INSERT-only guard, because the
    row is GONE before the INSERT arrives.
    """
    cols = LEGACY_CHUNK_COLUMNS
    rows = conn.execute("SELECT %s FROM chunks WHERE path = ?" % cols,
                        (path,)).fetchall()
    payload = [tuple(r[k] for k in r.keys()) for r in rows]
    conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
    for tup in payload:
        conn.execute(LEGACY_CHUNK_UPSERT, tup)


def _legacy_write(db: Path) -> None:
    """Replay the legacy build's write over every row and every file."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cols = LEGACY_CHUNK_COLUMNS
    rows = conn.execute("SELECT %s FROM chunks" % cols).fetchall()
    for r in rows:
        conn.execute(LEGACY_CHUNK_UPSERT, tuple(r[k] for k in r.keys()))
    frows = conn.execute(
        "SELECT path, source, hash, mtime, size, updated_at FROM files").fetchall()
    for r in frows:
        conn.execute(LEGACY_FILES_UPSERT, tuple(r[k] for k in r.keys()))
    conn.commit()
    conn.close()


def _legacy_replace_all(db: Path) -> None:
    """Replay the legacy build's DELETE-then-INSERT shape for every file."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    paths = [r[0] for r in conn.execute(
        "SELECT DISTINCT path FROM chunks").fetchall()]
    for p in paths:
        _legacy_replace_file(conn, p)
    conn.commit()
    conn.close()


def _state(db: Path) -> dict:
    conn = sqlite3.connect("file:" + str(db).replace("\\", "/") + "?mode=ro",
                           uri=True)
    cur = conn.cursor()
    out = {
        "body_path": cur.execute(
            "SELECT body_path FROM chunks WHERE id='row1'").fetchone()[0],
        "body_anchor": cur.execute(
            "SELECT body_anchor FROM chunks WHERE id='row1'").fetchone()[0],
        "body_hash": cur.execute(
            "SELECT body_hash FROM chunks WHERE id='row1'").fetchone()[0],
        "body_origin": cur.execute(
            "SELECT body_origin FROM chunks WHERE id='row1'").fetchone()[0],
    }
    try:
        out["fts_rows"] = cur.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    except sqlite3.Error:
        out["fts_rows"] = None
    conn.close()
    return out


# --------------------------------------------------------------------------
# 1. the guard is installed by the REAL storage path
# --------------------------------------------------------------------------

def test_guard_is_installed_by_the_real_storage_path(tmp_path):
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        assert body_guard.is_installed(conn), \
            "opening the library through MemoryStorage must install the guard"
    finally:
        storage.close()


def test_guard_status_distinguishes_present_from_failed(tmp_path):
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        st = body_guard.status(storage.conn)
        assert st["installed"] is True
        assert st["current"] is True
        assert st["protects"] == list(body_guard.PROTECTED_COLUMNS)
    finally:
        storage.close()


# --------------------------------------------------------------------------
# 2. the legacy write does NOT erase the body columns  (the point)
# --------------------------------------------------------------------------

def test_legacy_write_does_not_erase_the_body_columns(tmp_path):
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        _legacy_write(db)
    finally:
        storage.close()
    st = _state(db)
    assert st["body_path"] == "MEMORY.md", \
        "the legacy writer erased body_path (guard did not fire)"
    assert st["body_anchor"] == "L1-2"
    assert st["body_hash"] == "sha256:abc"
    assert st["body_origin"] == "file"


def test_legacy_delete_then_insert_does_not_erase_the_body_columns(tmp_path):
    """The legacy build's OTHER write shape: DELETE, then re-INSERT.

    Measured on the 2026-10-13 build: ``storage.py`` does
    ``DELETE FROM chunks WHERE path = ?`` and then re-inserts the file's
    chunks.  The row is GONE before the INSERT arrives, so an
    INSERT-only guard cannot protect it — the pointer has to survive the
    DELETE too.
    """
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        _legacy_replace_all(db)
    finally:
        storage.close()
    st = _state(db)
    assert st["body_path"] == "MEMORY.md", \
        "the legacy DELETE-then-INSERT erased body_path"
    assert st["body_anchor"] == "L1-2"
    assert st["body_hash"] == "sha256:abc"
    assert st["body_origin"] == "file"


def test_legacy_write_does_not_desync_fts(tmp_path):
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        before = _state(db)["fts_rows"]
        _legacy_write(db)
        after = _state(db)["fts_rows"]
    finally:
        storage.close()
    assert before == after, \
        "the guard's in-place UPDATE changed the FTS row count"


# --------------------------------------------------------------------------
# 3. REVERSE CASE: with the guard dropped, the legacy write erases
#    (without this, the test above would pass with the guard absent)
# --------------------------------------------------------------------------

def test_without_the_guard_the_legacy_write_erases_the_columns(tmp_path):
    db = _make_library(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TRIGGER IF EXISTS %s" % body_guard.GUARD_TRIGGER)
    conn.commit()
    conn.close()
    _legacy_write(db)
    st = _state(db)
    assert st["body_path"] is None, \
        "the premise is wrong: the legacy writer should have erased it"


def test_the_guard_is_what_prevents_the_erasure(tmp_path):
    """The same scenario, guard installed -> kept; dropped -> erased."""
    kept_dir = tmp_path / "kept"
    dropped_dir = tmp_path / "dropped"
    kept_dir.mkdir()
    dropped_dir.mkdir()
    kept_db = _make_library(kept_dir)
    dropped_db = _make_library(dropped_dir)

    for db in (kept_db, dropped_db):
        storage = MemoryStorage(db_path=str(db))
        storage.close()

    conn = sqlite3.connect(str(dropped_db))
    conn.execute("DROP TRIGGER IF EXISTS %s" % body_guard.GUARD_TRIGGER)
    conn.commit()
    conn.close()

    _legacy_write(kept_db)
    _legacy_write(dropped_db)

    assert _state(kept_db)["body_path"] == "MEMORY.md"
    assert _state(dropped_db)["body_path"] is None


# --------------------------------------------------------------------------
# 4. the guard does NOT fire for a current writer
# --------------------------------------------------------------------------

def test_a_current_writer_is_not_blocked(tmp_path):
    """A row that arrives WITH a body_path must be written normally."""
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, hash, created_at, updated_at, "
            "body_path, body_anchor, body_hash, text_state, body_origin) "
            "VALUES ('row2', NULL, 'shared', 'memory', 'MEMORY.md', 3, 4, "
            "'world', 'h2', '2026-01-01', '2026-01-01', 'MEMORY.md', "
            "'L3-4', 'sha256:def', 'projection', 'file')")
        conn.commit()
        got = tuple(conn.execute(
            "SELECT body_path, body_anchor, body_origin FROM chunks "
            "WHERE id='row2'").fetchone())
    finally:
        storage.close()
    assert got == ("MEMORY.md", "L3-4", "file"), \
        "the guard blocked a legitimate current write"


def test_the_guard_still_lets_the_legacy_writer_update_the_text(tmp_path):
    """The guard protects the POINTER, not the CONTENT.

    A guard that also blocked the legacy writer's legitimate text update
    would trade a lost pointer for a lost memory — and a guard that
    silently discards writes is worse than no guard at all.  So the
    legacy writer's new text MUST land.
    """
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, hash, created_at, updated_at) "
            "VALUES ('row1', NULL, 'shared', 'memory', 'MEMORY.md', 1, 2, "
            "'NEW TEXT FROM THE LEGACY WRITER', 'h1', '2026-01-01', "
            "'2026-01-02')")
        conn.commit()
        got = conn.execute(
            "SELECT text, body_path FROM chunks WHERE id='row1'").fetchone()
    finally:
        storage.close()
    assert got["text"] == "NEW TEXT FROM THE LEGACY WRITER", \
        "the guard swallowed the legacy writer's text update"
    assert got["body_path"] == "MEMORY.md", \
        "the guard did not protect the pointer"


def test_the_guard_only_fires_when_the_stored_row_has_a_pointer(tmp_path):
    """A row that never had a pointer is not 'protected' — it stays NULL."""
    db = _make_library(tmp_path, with_pointer=False)
    storage = MemoryStorage(db_path=str(db))
    try:
        _legacy_write(db)
    finally:
        storage.close()
    st = _state(db)
    assert st["body_path"] is None
    assert st["body_origin"] == "unresolved"


# --------------------------------------------------------------------------
# 5. the guard survives the legacy build's FTS rebuild
# --------------------------------------------------------------------------

def test_the_guard_survives_the_fts_object_rebuild(tmp_path):
    """The legacy build drops the six FTS triggers — not this one."""
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        # what the legacy build's reset_fts5() does
        for name in ("chunks_ai", "chunks_ad", "chunks_au",
                     "chunks_trigram_ai", "chunks_trigram_ad",
                     "chunks_trigram_au"):
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        conn.commit()
        assert body_guard.is_installed(conn), \
            "the FTS rebuild must not take the body guard with it"
    finally:
        storage.close()


# --------------------------------------------------------------------------
# 6. a stale guard is detected (not silently accepted)
# --------------------------------------------------------------------------

def test_a_stale_guard_is_detected_and_repaired(tmp_path):
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        conn.execute("DROP TRIGGER IF EXISTS %s" % body_guard.GUARD_TRIGGER)
        conn.execute("""CREATE TRIGGER %s BEFORE INSERT ON chunks
                        BEGIN SELECT RAISE(IGNORE); END"""
                     % body_guard.GUARD_TRIGGER)
        conn.commit()
        assert not body_guard.is_installed(conn), \
            "a stale guard must not be reported as current"
        result = body_guard.ensure_guard(conn)
        assert result["status"] == "repaired"
        assert body_guard.is_installed(conn)
    finally:
        storage.close()
