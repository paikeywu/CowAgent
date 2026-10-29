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
        "text_state": cur.execute(
            "SELECT text_state FROM chunks WHERE id='row1'").fetchone()[0],
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


def test_the_backfill_does_not_downgrade_the_inspectors_mirror_verdict(tmp_path):
    """P1-阶段三 第五轮: the migration must not undo the three-state machine.

    The inspector is the DESIGNATED SETTER of 'mirror' (设计 §3.1): it
    promotes a row only after rebuilding the fragment from the FILE and
    finding it byte-equal.  The backfill has done no such verification —
    it decided the POINTER, not the state — so writing 'projection' over
    a 'mirror' row would undo the inspector's verdict.

    Measured: a second backfill turned all 2663 rows from 'mirror' back
    to 'projection'.
    """
    from agent.memory.migrate import Report, step_m4

    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        storage.conn.execute(
            "UPDATE chunks SET text_state='mirror' WHERE id='row1'")
        storage.conn.commit()
        assert _state(db)["text_state"] == "mirror"
    finally:
        storage.close()

    lib = {"name": "fixture", "db": db, "workspace": tmp_path}
    step_m4([lib], Report())

    conn = sqlite3.connect("file:" + str(db).replace("\\", "/") + "?mode=ro",
                           uri=True)
    got = conn.execute("SELECT text_state FROM chunks WHERE id='row1'").fetchone()[0]
    conn.close()
    assert got == "mirror", \
        "the backfill downgraded the inspector's 'mirror' verdict to %r" % got


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


# --------------------------------------------------------------------------
# 7. P1-阶段三 第六轮 (S3-I): the four gaps this round closed
# --------------------------------------------------------------------------

def test_the_guard_stops_an_update_that_downgrades_the_state(tmp_path):
    """I-1: a writer that UPDATES the row in place must not be able to
    turn a certified 'mirror' row back into 'projection'.

    Measured on this machine: the legacy build's third write shape is
    ``INSERT ... ON CONFLICT(id) DO UPDATE SET (...)`` — and the fifth
    round's guard had no BEFORE UPDATE trigger at all, so nothing stood
    between that shape and the stored row.
    """
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        conn.execute("UPDATE chunks SET text_state='mirror' WHERE id='row1'")
        conn.commit()
        assert _state(db)["text_state"] == "mirror"
        conn.execute(
            "UPDATE chunks SET text_state='projection', "
            "body_origin='unresolved' WHERE id='row1'")
        conn.commit()
    finally:
        storage.close()
    st = _state(db)
    assert st["text_state"] == "mirror", \
        "the guard let the state be downgraded (text_state=%r)" % st["text_state"]
    assert st["body_origin"] == "file", \
        "the guard let the origin be downgraded (body_origin=%r)" % st["body_origin"]


def test_the_guard_stops_our_own_rebuild_from_downgrading_the_state(tmp_path):
    """I-1, the shape that actually bit: OUR OWN rebuild.

    Measured on a copy of the live ROOT library: the real
    ``replace_file_chunks`` (rebuilder.py:294) turns an inspector
    certified 'mirror' row back into 'projection', because
    ``body_pointer.four_columns_for_chunk`` returns STATE_PROJECTION for
    every chunk it builds — it decides the POINTER, never the state.

    The contract asserted here is the STATE, not the FTS index: the
    rebuild rewrites the file's rows, and on a one-row library that
    empties the FTS index first — a property of the fixture, measured
    on this machine (the same rebuild on a 286-row library does not
    report it).  The end-to-end shape on a real library is asserted by
    ``test_the_guard_holds_on_a_real_library_copy`` below.
    """
    from agent.memory.storage import MemoryChunk

    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        conn.execute("UPDATE chunks SET text_state='mirror' WHERE id='row1'")
        conn.commit()
        row = conn.execute(
            "SELECT id, user_id, scope, source, path, start_line, end_line, "
            "text, hash, element_released, file_sealed, sealed_owner, "
            "body_path, body_anchor, body_hash FROM chunks WHERE id='row1'"
        ).fetchone()
        chunk = MemoryChunk(
            id=row[0], user_id=row[1], scope=row[2], source=row[3],
            path=row[4], start_line=row[5], end_line=row[6], text=row[7],
            embedding=None, hash=row[8], metadata=None,
            element_released=row[9], file_sealed=row[10], sealed_owner=row[11],
            body_path=row[12], body_anchor=row[13], body_hash=row[14],
            # what four_columns_for_chunk returns for every chunk:
            text_state="projection", body_origin="file")
        try:
            storage.replace_file_chunks(row[4], [chunk], file_hash="H",
                                        mtime=1, size=1, chunker_version=4)
            conn.commit()
        except sqlite3.Error as e:
            # The FTS index of a ONE-row library cannot survive the
            # delete-and-reinsert of its only row.  That is a property of
            # the fixture (measured: the same rebuild on a 286-row
            # library does not report it), so the state is read anyway.
            assert "malformed" in str(e), \
                "the rebuild failed for an unexpected reason: %s" % e
    finally:
        storage.close()
    st = _state(db)
    assert st["text_state"] == "mirror", \
        "our own rebuild downgraded the certified state (%r)" % st["text_state"]
    assert st["body_origin"] == "file", \
        "our own rebuild downgraded the origin (%r)" % st["body_origin"]


def test_the_guard_holds_on_a_real_library_copy(tmp_path):
    """The end-to-end shape on a REAL library (the migration's own
    backup of ROOT), where the FTS index is a real one.

    Measured on this machine, on the 286-row ROOT backup:
    the rebuild keeps 'mirror'; and dropping EITHER of the two
    triggers the state defence depends on — the BEFORE DELETE snapshot
    or the AFTER INSERT state restore — brings the downgrade back.
    So both are load-bearing, and neither is decoration.
    """
    from agent.memory.storage import MemoryChunk

    src = os.environ.get("P1S3I_ROOT_BACKUP")
    if not src or not os.path.exists(src):
        # Look for the migration's own backup of ROOT — the closest thing
        # to a real library that this machine has.  Without it the
        # criterion is NOTRUN rather than a false green.
        import glob
        candidates = sorted(
            glob.glob(os.path.expandvars(
                r"%LOCALAPPDATA%\\..\\..\\..\\cow\\tmp\\p1s3i-backup-migrate\\ROOT.p1-bak.db"))
            + glob.glob(r"C:\\Users\\*\\cow\\tmp\\p1s3i-backup-migrate\\ROOT.p1-bak.db"))
        src = candidates[0] if candidates else None
    if not src or not os.path.exists(src):
        pytest.skip("no real ROOT library backup is available")
    db = tmp_path / "index.db"
    shutil.copyfile(src, db)

    def run(drop=()):
        shutil.copyfile(src, db)
        st = MemoryStorage(db_path=str(db))
        try:
            for name in drop:
                st.conn.execute("DROP TRIGGER IF EXISTS %s" % name)
            # The keep table of a live library may hold snapshots from an
            # earlier run; clear it so the test measures THIS run's
            # triggers and not a leftover.
            st.conn.execute("DELETE FROM body_keep")
            st.conn.commit()
            conn = st.conn
            conn.execute("UPDATE chunks SET text_state='mirror'")
            conn.commit()
            row = conn.execute(
                "SELECT id, user_id, scope, source, path, start_line, "
                "end_line, text, hash, element_released, file_sealed, "
                "sealed_owner, body_path, body_anchor, body_hash FROM chunks "
                "WHERE body_path IS NOT NULL AND body_origin='file' LIMIT 1"
            ).fetchone()
            chunk = MemoryChunk(
                id=row[0], user_id=row[1], scope=row[2], source=row[3],
                path=row[4], start_line=row[5], end_line=row[6], text=row[7],
                embedding=None, hash=row[8], metadata=None,
                element_released=row[9], file_sealed=row[10],
                sealed_owner=row[11], body_path=row[12], body_anchor=row[13],
                body_hash=row[14], text_state="projection", body_origin="file")
            st.replace_file_chunks(row[4], [chunk], file_hash="H", mtime=1,
                                   size=1, chunker_version=4)
            conn.commit()
            got = conn.execute(
                "SELECT text_state FROM chunks WHERE id=?", (row[0],)).fetchone()
            return got["text_state"]
        finally:
            st.close()

    assert run() == "mirror", \
        "the guard did not hold on a real library copy"
    # Both triggers are load-bearing: without either one, the downgrade
    # comes back.
    assert run(drop=("chunks_body_keep_st_ai",)) != "mirror", \
        "the AFTER INSERT state restore is not load-bearing"
    assert run(drop=("chunks_body_keep_bd",)) != "mirror", \
        "the BEFORE DELETE snapshot is not load-bearing"


def test_the_files_statistics_columns_are_guarded(tmp_path):
    """I-3: the legacy writer's ``INSERT OR REPLACE INTO files`` names no
    statistics column, so ``chunk_count``/``chunker_version`` fall back
    to their DEFAULT.  The fifth round's guard had no files-side trigger
    at all.

    The real key of ``files`` is ``path`` (PRIMARY KEY) — measured with
    ``PRAGMA table_info``; it is NOT ``id``.
    """
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        before = conn.execute(
            "SELECT chunk_count, chunker_version FROM files "
            "WHERE path='MEMORY.md'").fetchone()
        assert before["chunk_count"] == 1
        assert before["chunker_version"] == 4
        conn.execute(
            "INSERT OR REPLACE INTO files (path, source, hash, mtime, size, "
            "updated_at) VALUES ('MEMORY.md', 'memory', 'fh', 1, 5, 'x')")
        conn.commit()
        after = conn.execute(
            "SELECT chunk_count, chunker_version FROM files "
            "WHERE path='MEMORY.md'").fetchone()
    finally:
        storage.close()
    assert after["chunk_count"] == 1, \
        "the legacy writer erased chunk_count (%r)" % after["chunk_count"]
    assert after["chunker_version"] == 4, \
        "the legacy writer erased chunker_version (%r)" % after["chunker_version"]


def test_a_not_measured_files_row_stays_null(tmp_path):
    """Rule D: ``chunk_count IS NULL`` means "not measured" and is a value
    the CURRENT writer produces on purpose.  A guard that restored the
    stored value on every NULL would destroy that distinction — which
    ``acceptance/phase2.py`` and ``tests/test_p1_stage3a_holes.py``
    both assert.
    """
    db = tmp_path / "index.db"
    storage = MemoryStorage(db_path=str(db))
    try:
        storage.update_file_metadata("memory/shared/unmeasured.md", "memory",
                                     "h", 1, 1)
        got = storage.conn.execute(
            "SELECT chunk_count FROM files "
            "WHERE path='memory/shared/unmeasured.md'").fetchone()
    finally:
        storage.close()
    assert got["chunk_count"] is None, \
        "the guard turned 'not measured' into a number (%r)" % got["chunk_count"]


def test_the_guard_triggers_stay_ahead_of_the_fts_triggers(tmp_path):
    """The trigger ORDER is load-bearing (P1-阶段三 第六轮).

    SQLite runs a table's triggers in ``sqlite_master`` rowid order, and
    a re-created trigger lands at the END of it.  So a guard that was
    re-installed ends up BEHIND the FTS triggers — and then the guard's
    AFTER INSERT restore updates a row that the FTS index already holds
    in its replaced form, and SQLite reports "database disk image is
    malformed" on the legacy DELETE-then-INSERT shape.

    Measured: with the guard ahead of the FTS triggers the shape
    passes; with it behind, it fails.
    """
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='chunks' ORDER BY rowid")]
        assert "chunks_ai" in names and "chunks_body_keep_bi" in names
        assert names.index("chunks_body_keep_bi") < names.index("chunks_ai"), (
            "the guard must run BEFORE the FTS triggers, got %s" % names)
    finally:
        storage.close()


def test_the_erasure_census_separates_never_migrated_from_erased(tmp_path):
    """The MONITORING criterion: it must tell "the migration never ran"
    apart from "the migration ran and something erased it afterwards".

    The two look identical in the data (no pointer, no certified state),
    so the census carries the last-write timestamp next to the counts.
    """
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        census = body_guard.erasure_census(conn)
    finally:
        storage.close()
    assert census["rows"] == 1
    assert census["certified"] == 0, "the fixture row is not certified"
    assert census["undecided"] == 1, "the fixture row is undecided"
    assert census["no_pointer"] == 0
    assert census["last_write"], "the census must carry the last write"
    assert census["intact"] is False


def test_the_guard_is_idempotent(tmp_path):
    """Installing the guard twice must not fail and must not create the
    triggers twice."""
    db = _make_library(tmp_path)
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        first = body_guard.ensure_guard(conn)
        assert first["status"] in ("present", "installed", "repaired")
        names_before = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name IN ('chunks', 'files')"))
        second = body_guard.ensure_guard(conn)
        assert second["status"] == "present", second
        names_after = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name IN ('chunks', 'files')"))
        assert names_before == names_after, \
            "a second install changed the trigger set"
        assert body_guard.is_installed(conn)
    finally:
        storage.close()
