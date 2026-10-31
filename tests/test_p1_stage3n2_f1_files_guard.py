"""F-1 · P1 阶段三 N 后续二 —— the body-column guard's files side.

**The defect this file locks down (F-1).**

``MemoryStorage._init_db`` installed the body-column guard BEFORE the
``files`` table and its statistics columns existed (``storage.py``: the
install was 130 lines above the ``CREATE TABLE files``).  The files
side of the guard reads ``files.chunk_count`` / ``files.chunker_version``,
so at install time it could not be installed: ``ensure_guard`` reported
"the files side cannot be protected", skipped the four files-side
triggers — and ``is_installed`` (which demanded ALL NINE names) then
reported ``False`` FOREVER.

The consequence was not cosmetic:

* every open took the RE-INSTALL path, and a re-install lands the
  guard at the END of ``sqlite_master`` — which flips the load-bearing
  trigger order (see ``body_guard.reorder_fts_triggers_behind_guard``);
* on a FRESH library the first open installed 5/9 triggers and the
  second installed 9/9, with the rowids moving.

Two independent causes, two independent fixes:

1. **the install point** (the承重 one) — the guard must be installed
   where its own dependencies exist;
2. **the judgment** — "this side cannot be protected" is not "the guard
   needs re-installing", so ``is_installed`` must answer PER SIDE.

Every criterion below has a BREAKER: a mutation that makes it RED.
"""

import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from agent.memory import body_guard  # noqa: E402
from agent.memory.storage import MemoryStorage  # noqa: E402

FTS_SYNC = ("chunks_ai", "chunks_ad", "chunks_au",
            "chunks_trigram_ai", "chunks_trigram_ad", "chunks_trigram_au")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _guard_rowids(conn):
    names = set(body_guard.guard_trigger_names())
    rows = conn.execute(
        "SELECT name, rowid FROM sqlite_master WHERE type='trigger' "
        "ORDER BY rowid").fetchall()
    return {r[0]: r[1] for r in rows if r[0] in names}


def _fts_rowids(conn):
    rows = conn.execute(
        "SELECT name, rowid FROM sqlite_master WHERE type='trigger' "
        "ORDER BY rowid").fetchall()
    return {r[0]: r[1] for r in rows if r[0] in FTS_SYNC}


def _order(conn):
    """(guard_max, fts_min) — the guard must be AHEAD (smaller rowid)."""
    g = _guard_rowids(conn)
    f = _fts_rowids(conn)
    if not g or not f:
        return None, None
    return max(g.values()), min(f.values())


def _open(db):
    st = MemoryStorage(db_path=str(db))
    return st


def _files_without_stats(db):
    """A library whose ``files`` table has NO statistics columns.

    That is a legitimate state: the files side of the guard has nothing
    to protect there.  It must not make the guard "stale".
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript("""
        DROP TABLE IF EXISTS files;
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'memory',
            hash TEXT NOT NULL,
            mtime INTEGER NOT NULL,
            size INTEGER NOT NULL,
            updated_at INTEGER DEFAULT (strftime('%s','now')));
        """)
        conn.commit()
    finally:
        conn.close()


def _make_raw_library(db):
    """A library with the chunks body columns and a ``files`` table that
    has NO statistics columns — built WITHOUT ``MemoryStorage``.

    Why raw: ``MemoryStorage._migrate_files_columns()`` ADDS
    ``chunk_count`` / ``chunker_version`` on every open, so a library
    opened through the storage path can never stay unprotectable.  The
    unprotectable state belongs to a library an older build wrote, and
    it is the state that made the old ``is_installed`` answer "never".
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript("""
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            text TEXT NOT NULL,
            body_path TEXT, body_anchor TEXT, body_hash TEXT,
            text_state TEXT NOT NULL DEFAULT 'projection',
            body_origin TEXT NOT NULL DEFAULT 'unresolved');
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'memory',
            hash TEXT NOT NULL,
            mtime INTEGER NOT NULL,
            size INTEGER NOT NULL,
            updated_at INTEGER DEFAULT (strftime('%s','now')));
        """)
        conn.commit()
    finally:
        conn.close()


def _break_order(conn):
    """Break the trigger ORDER without changing any body.

    A re-created trigger lands at the END of ``sqlite_master``, so
    re-creating the guard's triggers puts them BEHIND the FTS ones —
    while the bodies stay byte-identical, so ``is_installed`` still
    says "present".  This is the shape a library gets into when
    something ELSE re-creates the guard.
    """
    stored = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
        "AND sql IS NOT NULL").fetchall()}
    guard_names = [n for n in stored
                   if n in set(body_guard.guard_trigger_names())]
    for n in guard_names:
        conn.execute("DROP TRIGGER IF EXISTS %s" % n)
    for n in guard_names:
        conn.execute(stored[n])
    conn.commit()


# --------------------------------------------------------------------------
# 1. the FIRST open installs the WHOLE guard (the承重 criterion)
# --------------------------------------------------------------------------

def test_f1_the_first_open_installs_the_files_side_too(tmp_path):
    """The FIRST open of a FRESH library must install all NINE triggers.

    Before the fix the first open installed 5/9 — the files side was
    skipped because the ``files`` table did not exist yet at the
    install point.  The second open then installed 9/9.

    **BREAKER**: move the guard install back ABOVE the ``CREATE TABLE
    files`` (i.e. back to where it was) → the first open installs 5/9
    → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        conn = st.conn
        names = set(body_guard.guard_trigger_names())
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        files_side = {n for n in names if n.startswith("files_")}
        chunks_side = {n for n in names if n.startswith("chunks_")}
        assert chunks_side <= present, (
            "the chunks side must be installed: missing %s"
            % sorted(chunks_side - present))
        assert files_side <= present, (
            "the FIRST open must install the files side too: missing %s "
            "(the install happened before the files table existed)"
            % sorted(files_side - present))
        assert body_guard.is_installed(conn)
    finally:
        st.close()


def test_f1_opening_twice_does_not_reinstall_the_guard(tmp_path):
    """The trigger rowids must NOT move between two opens.

    A re-install drops and re-creates the triggers, so the rowids move.
    The guard must be installed ONCE and left alone.

    **BREAKER**: make ``is_installed`` demand the files side
    unconditionally → the second open takes the re-install path → the
    rowids move → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        first = _guard_rowids(st.conn)
    finally:
        st.close()

    st = _open(db)
    try:
        second = _guard_rowids(st.conn)
    finally:
        st.close()

    assert first == second, (
        "the guard was RE-INSTALLED on the second open (rowids moved): "
        "%s -> %s" % (first, second))


def test_f1_the_order_is_stable_across_opens(tmp_path):
    """The load-bearing order must survive repeated opens.

    **BREAKER**: remove the reorder from the present path → the second
    open flips the order → RED.
    """
    db = tmp_path / "index.db"
    for i in range(3):
        st = _open(db)
        try:
            gmax, fmin = _order(st.conn)
            assert gmax is not None, "no FTS triggers in the fixture"
            assert gmax < fmin, (
                "open #%d: the guard is BEHIND the FTS triggers "
                "(guard_max=%s, fts_min=%s)" % (i + 1, gmax, fmin))
        finally:
            st.close()


# --------------------------------------------------------------------------
# 2. the judgment: per SIDE, not one verdict for nine triggers
# --------------------------------------------------------------------------

def test_f1_an_unprotectable_files_side_is_not_stale(tmp_path):
    """"This side cannot be protected" must NOT mean "needs re-install".

    A library whose ``files`` table has no statistics columns has
    nothing to protect on that side.  ``is_installed`` must say True
    for the chunks side alone — otherwise every open re-installs.

    **BREAKER**: make ``is_installed`` require all nine names
    unconditionally → False → RED.

    NOTE: the fixture is a RAW connection, not ``MemoryStorage``.
    ``MemoryStorage._migrate_files_columns()`` ADDS the statistics
    columns on every open, so a library opened through the storage
    path can never stay unprotectable — which means the unprotectable
    state is only reachable for a library an older build wrote and a
    tool (the sweep, the inspector) looks at.  MEASURED: the first
    version of this criterion used ``MemoryStorage`` and the mutant
    slipped through, because the reopen re-added the columns.
    """
    db = tmp_path / "index.db"
    _make_raw_library(db)
    conn = sqlite3.connect(str(db))
    try:
        assert not body_guard.files_side_protectable(conn), (
            "the fixture must make the files side unprotectable")
        body_guard.ensure_guard(conn)
        assert body_guard.is_installed(conn), (
            "a library whose files side cannot be protected is NOT a "
            "library with a stale guard")
    finally:
        conn.close()


def test_f1_an_unprotectable_files_side_does_not_reinstall_every_open(tmp_path):
    """The re-install loop must be gone even on an unprotectable library.

    **BREAKER**: make ``is_installed`` require all nine names
    unconditionally → False → the re-install path runs every time →
    the rowids move → RED.
    """
    db = tmp_path / "index.db"
    _make_raw_library(db)
    conn = sqlite3.connect(str(db))
    try:
        body_guard.ensure_guard(conn)
        first = _guard_rowids(conn)
        second_status = body_guard.ensure_guard(conn)
        second = _guard_rowids(conn)
    finally:
        conn.close()

    assert second_status.get("status") == "present", (
        "the second ensure_guard must be a no-op, got %s"
        % second_status.get("status"))
    assert first == second, (
        "the guard was RE-INSTALLED on every open of a library whose "
        "files side cannot be protected (rowids moved): %s -> %s"
        % (first, second))


def test_f1_a_protectable_files_side_is_still_required(tmp_path):
    """The per-side judgment must not become "the files side is optional".

    When the files side CAN be protected, its four triggers must be
    required — a library missing one of them IS stale.

    **BREAKER**: drop the files side from the required list
    unconditionally → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        conn = st.conn
        assert body_guard.files_side_protectable(conn), (
            "the fixture must make the files side protectable")
        conn.execute("DROP TRIGGER IF EXISTS files_body_keep_bi")
        conn.commit()
        assert not body_guard.is_installed(conn), (
            "a library missing one files-side trigger IS stale")
    finally:
        st.close()


# --------------------------------------------------------------------------
# 3. the S3-L staleness rules must survive the per-side change
# --------------------------------------------------------------------------

def test_f1_a_renamed_trigger_is_stale(tmp_path):
    """A trigger RENAMED (behaviour unchanged) must be judged stale.

    This is the S3-L rule and it must not be weakened by the per-side
    change.

    **BREAKER**: compare the trigger set instead of the names → the
    renamed trigger is "still there" → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        conn = st.conn
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='chunks_body_keep_bi'").fetchone()[0]
        conn.execute("DROP TRIGGER chunks_body_keep_bi")
        conn.execute(sql.replace("chunks_body_keep_bi",
                                  "chunks_body_keep_bi_RENAMED"))
        conn.commit()
        assert not body_guard.is_installed(conn), (
            "a RENAMED trigger (same behaviour) must be judged stale")
    finally:
        st.close()


def test_f1_a_trigger_with_a_changed_body_is_stale(tmp_path):
    """A trigger whose BODY changed (name unchanged) must be stale.

    This is the S3-L rule and it must not be weakened by the per-side
    change.

    **BREAKER**: compare only the trigger NAMES → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        conn = st.conn
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='chunks_body_keep_bu'").fetchone()[0]
        conn.execute("DROP TRIGGER chunks_body_keep_bu")
        conn.execute(sql.replace("SELECT RAISE(IGNORE);",
                                  "SELECT RAISE(IGNORE); -- tampered"))
        conn.commit()
        assert not body_guard.is_installed(conn), (
            "a trigger whose BODY changed must be judged stale")
    finally:
        st.close()


# --------------------------------------------------------------------------
# 4. the ORDER repair: every open, not only on re-install
# --------------------------------------------------------------------------

def test_f1_the_order_is_repaired_on_the_present_path(tmp_path):
    """The order is a property of the LIBRARY, so it is repaired on
    EVERY open.

    ``is_installed`` compares the BODIES and says nothing about the
    POSITION — so a library can have current bodies and a broken
    order, and only the every-open repair can fix it.  MEASURED:
    ``is_installed`` reports True while the order is broken.

    **This criterion calls ``body_guard.ensure_guard`` DIRECTLY.**  It
    must not go through ``MemoryStorage``: the storage path has its own
    call to the same repair, so going through it would MASK a removal
    of the repair from ``ensure_guard``'s present path.  MEASURED: the
    first version of this criterion went through ``MemoryStorage`` and
    the mutant survived.

    **BREAKER**: remove the reorder from the present path of
    ``ensure_guard`` → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        conn = st.conn
        _break_order(conn)
        assert body_guard.is_installed(conn), (
            "the fixture must leave the bodies CURRENT — only the order "
            "is broken")
        gmax, fmin = _order(conn)
        assert gmax > fmin, (
            "the fixture must leave the order BROKEN (guard_max=%s, "
            "fts_min=%s)" % (gmax, fmin))
    finally:
        st.close()

    # A RAW connection — ``ensure_guard`` is the unit under test.
    conn = sqlite3.connect(str(db))
    try:
        result = body_guard.ensure_guard(conn)
        gmax_after, fmin_after = _order(conn)
    finally:
        conn.close()

    assert result.get("status") == "present", result
    assert gmax_after < fmin_after, (
        "ensure_guard did NOT fix the order on the present path "
        "(guard_max=%s, fts_min=%s) — a library whose bodies are current "
        "but whose order is broken stays writable-but-broken"
        % (gmax_after, fmin_after))


def test_f1_the_trigram_flag_may_only_be_written_when_the_index_exists(tmp_path):
    """A flag that claims an index exists may only be written when it does.

    **The defect this locks down (found while fixing F-1).**

    ``_repair_search_indexes`` runs on the BACKGROUND MAINTENANCE
    THREAD, on a SECOND connection, and it writes
    ``_TRIGRAM_DONE='1'`` from ``self.trigram_fts5_available`` — a value
    that belongs to the instance that scheduled the thread, NOT to the
    database the thread is about to touch.

    So an instance whose build CAN open the trigram index schedules the
    thread; a LATER instance whose build CANNOT opens the same file,
    DETACHES the trigram index and DELETES the flag (the flag must go
    with the triggers, or a later capable build would trust an index
    missing every write made in between) — and then the FIRST
    instance's thread wakes up, sees the flag absent, and writes it
    back, on a database whose trigram triggers are GONE.

    The result: a library that says "the trigram index is complete"
    while it has no trigram index at all.

    MEASURED: the race reproduces 1/36 on the pre-fix code and 6/36
    with the F-1 fix — so it was ALREADY there, and the F-1 work made
    it more likely to be hit.  It is the reason
    ``test_shared_db_recovery.py``'s trigram criterion went red.

    **BREAKER**: drop the ``trigram_present`` check → the flag is
    written on a database with no trigram triggers → RED.
    """
    from agent.memory import storage as st_mod

    db = tmp_path / "index.db"
    st = MemoryStorage(db_path=str(db))
    try:
        conn = st.conn
        # Detach the trigram index the way an older build does, and
        # delete the flag with it.
        for name in ("chunks_trigram_ai", "chunks_trigram_ad",
                     "chunks_trigram_au"):
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        conn.execute("DELETE FROM _meta WHERE key = 'trigram_backfill_done'")
        conn.commit()

        # The repair runs on a connection of its own; hand it this one.
        st._repair_search_indexes(conn)

        row = conn.execute(
            "SELECT 1 FROM _meta WHERE key = 'trigram_backfill_done'"
        ).fetchone()
        assert row is None, (
            "the trigram backfill flag was written on a database whose "
            "trigram index does NOT exist — the flag claims an index "
            "that is not there")
    finally:
        st.close()


def test_f1_there_is_only_ONE_ordering_rule(tmp_path):
    """There must be ONE implementation of the ordering rule.

    **MEASURED on 0.114 (this round).**  ``storage.py`` kept a PRIVATE
    copy of the reorder algorithm, and that copy was wrong: it listed
    only six guard trigger names — missing ``chunks_body_keep_st_ai``
    (added by S3-L) — and treated everything else on the ``chunks``
    table as an FTS trigger.  So it re-created the state-restore
    trigger as if it were an FTS trigger and put it at the END of
    ``sqlite_master`` — BEHIND the FTS ones.

    The trigger dump on 0.114's live library showed exactly one
    offender (``chunks_body_keep_st_ai`` at rowid 63, behind the FTS
    triggers at 55..60) while the other eight guard triggers sat
    correctly ahead at 47..54.

    Two copies of one rule is one copy that will drift — and the copy
    that drifts is the one nobody runs.

    **BREAKER**: put a private copy of the algorithm back into
    ``storage.py`` → the source check fails → RED.
    """
    storage_src = (REPO.replace("\\", os.sep) + os.sep +
                   "agent" + os.sep + "memory" + os.sep + "storage.py")
    with open(storage_src, encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.find("def _reorder_fts_triggers_behind_guard"):]
    assert "guard_names = (" not in body, (
        "storage.py must NOT carry its own copy of the guard trigger "
        "list — that copy went stale (it missed "
        "chunks_body_keep_st_ai) and put the state-restore trigger "
        "BEHIND the FTS triggers")
    assert "reorder_fts_triggers_behind_guard" in body, (
        "storage.py must call the ONE implementation that lives in "
        "body_guard")
    assert "DROP TRIGGER" not in body, (
        "storage.py must not drop/re-create triggers itself — the rule "
        "belongs to body_guard, and a second copy is a second rule")


def test_f1_every_guard_trigger_is_ahead_of_the_fts_ones(tmp_path):
    """ALL NINE guard triggers must be ahead — not eight of nine.

    This is the criterion that the private copy defeated: the state
    restore trigger (``chunks_body_keep_st_ai``) was the one that went
    behind, and a criterion that only checked "the first guard trigger
    is ahead" would have passed.

    **BREAKER**: make the order check use the FIRST guard trigger
    instead of the MAXIMUM → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        conn = st.conn
        gmax, fmin = _order(conn)
        assert gmax is not None and fmin is not None
        assert gmax < fmin, (
            "guard_max=%s must be < fts_min=%s — EVERY guard trigger has "
            "to be ahead of every FTS trigger" % (gmax, fmin))
        # And the state-restore trigger specifically.
        rowids = _guard_rowids(conn)
        st_ai = rowids.get("chunks_body_keep_st_ai")
        assert st_ai is not None, "the state-restore trigger must exist"
        assert st_ai < fmin, (
            "chunks_body_keep_st_ai (rowid=%s) is BEHIND the FTS triggers "
            "(fts_min=%s) — the copy of the rule in storage.py put it "
            "there" % (st_ai, fmin))
    finally:
        st.close()


def test_f1_the_order_survives_a_write_after_the_repair(tmp_path):
    """The point of the order: the library must still be WRITABLE.

    A guard behind the FTS triggers makes the legacy DELETE-then-INSERT
    shape fail with "database disk image is malformed".

    **BREAKER**: break the order and skip the repair → the write
    raises → RED.
    """
    db = tmp_path / "index.db"
    st = _open(db)
    try:
        conn = st.conn
        conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, hash, body_path, body_anchor, "
            "body_hash, text_state, body_origin) VALUES "
            "('row1', NULL, 'shared', 'memory', 'MEMORY.md', 1, 2, "
            "'hello', 'h1', 'MEMORY.md', 'L1-2', 'sha256:abc', 'mirror', "
            "'file')")
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, updated_at, "
            "chunk_count, chunker_version) VALUES "
            "('MEMORY.md', 'memory', 'fh', 1, 5, '2026-01-01', 1, 4)")
        conn.commit()
    finally:
        st.close()

    st = _open(db)
    try:
        conn = st.conn
        # The legacy DELETE-then-INSERT shape.
        conn.execute("DELETE FROM chunks WHERE path = ?", ("MEMORY.md",))
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, hash) VALUES "
            "('row1', NULL, 'shared', 'memory', 'MEMORY.md', 1, 2, "
            "'hello', 'h1')")
        conn.commit()
        survived = conn.execute(
            "SELECT body_path FROM chunks WHERE id='row1'").fetchone()[0]
        assert survived is not None, (
            "the guard did not protect the pointer")
    finally:
        st.close()
