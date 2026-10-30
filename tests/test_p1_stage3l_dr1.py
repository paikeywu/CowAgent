# encoding: utf-8
"""P1-阶段三 第八轮 (L) — the DR-1 mechanism and the cross-end fingerprint.

The tests here are the REVERSE cases the delegation asks for: each one
must FAIL on the broken state and PASS on the fixed one.  A test that
passes on both proves nothing.

The four subjects:

  * **DR-1** — the guard sweep must reach EVERY live library, repair a
    stale one, and prove it by the trigger TEXT fingerprint.
  * **T2** — the ``unresolved`` rows get a decided origin, by content.
  * **T3** — the certification ledger holds no orphan entries (M6f).
  * **T4** — the ``_meta`` chunker-version rename is complete (M6g).
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.memory import body_guard, cross_end, guard_sweep  # noqa: E402
from agent.memory.body_pointer import ORIGIN_FILE, ORIGIN_ORPHAN  # noqa: E402
from agent.memory.storage import MemoryStorage  # noqa: E402


# ---------------------------------------------------------------------------
# the fixtures
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path):
    """A workspace with THREE live libraries (the root + two agents)."""
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    (ws / "agents" / "agent-a" / "memory" / "long-term").mkdir(parents=True)
    (ws / "agents" / "agent-b" / "memory" / "long-term").mkdir(parents=True)
    for ws_root in (ws, ws / "agents" / "agent-a", ws / "agents" / "agent-b"):
        MemoryStorage(db_path=ws_root / "memory" / "long-term" / "index.db").close()
    return ws


def _lib(ws, name):
    if name == "ROOT":
        return ws / "memory" / "long-term" / "index.db"
    return ws / "agents" / name / "memory" / "long-term" / "index.db"


def _install_stale_guard(db):
    """Replace ONE of the body guard's triggers with the OLD (pre-S3-L)
    body — and ONLY that one.

    **This is the reverse case's setup, and the precision matters.**
    The old body restored ``text_state`` from the guard's keep table;
    the current one restores it from the certification ledger.  Both
    are valid SQL and both compile — the difference is BEHAVIOUR.

    So the stale guard is written by hand, and ONLY the differing
    trigger is replaced: every other trigger keeps its current body.
    That is what makes the reverse case sharp — the two libraries then
    have **identical trigger NAME sets** and differ in exactly ONE
    body, which is precisely the situation a name-set fingerprint
    cannot see and a text fingerprint can.
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TRIGGER IF EXISTS chunks_body_keep_bu")
        conn.execute("""
CREATE TRIGGER chunks_body_keep_bu BEFORE UPDATE ON chunks
WHEN ((NEW.body_path IS NULL AND NEW.body_anchor IS NULL AND NEW.body_hash IS NULL)
   OR (NEW.text_state IS NULL OR NEW.text_state = 'projection'
       OR NEW.body_origin IS NULL OR NEW.body_origin = 'unresolved'))
 AND EXISTS (SELECT 1 FROM chunks c WHERE c.id = NEW.id
             AND c.body_path IS NOT NULL AND ((c.text_state IN ('mirror', 'externalized', 'stale', 'missing'))
                                          OR (c.body_origin IN ('file', 'blob', 'orphan'))))
 AND (NEW.body_path IS NOT (SELECT c.body_path FROM chunks c WHERE c.id = NEW.id)
   OR NEW.body_anchor IS NOT (SELECT c.body_anchor FROM chunks c WHERE c.id = NEW.id)
   OR NEW.body_hash IS NOT (SELECT c.body_hash FROM chunks c WHERE c.id = NEW.id)
   OR NEW.text_state IS NOT (SELECT c.text_state FROM chunks c WHERE c.id = NEW.id)
   OR NEW.body_origin IS NOT (SELECT c.body_origin FROM chunks c WHERE c.id = NEW.id))
BEGIN
    UPDATE chunks SET
        user_id=NEW.user_id, scope=NEW.scope, source=NEW.source, path=NEW.path,
        start_line=NEW.start_line, end_line=NEW.end_line, text=NEW.text,
        embedding=NEW.embedding, hash=NEW.hash, metadata=NEW.metadata,
        created_at=NEW.created_at, updated_at=NEW.updated_at,
        hit_count=NEW.hit_count, last_hit_at=NEW.last_hit_at,
        element_released=NEW.element_released, file_sealed=NEW.file_sealed,
        sealed_owner=NEW.sealed_owner,
        body_path = COALESCE(NEW.body_path, (SELECT c.body_path FROM chunks c WHERE c.id = NEW.id)),
        body_anchor = COALESCE(NEW.body_anchor, (SELECT c.body_anchor FROM chunks c WHERE c.id = NEW.id)),
        body_hash = COALESCE(NEW.body_hash, (SELECT c.body_hash FROM chunks c WHERE c.id = NEW.id)),
        text_state = CASE WHEN NEW.text_state IS NULL OR NEW.text_state = 'projection'
            THEN (SELECT c.text_state FROM chunks c WHERE c.id = NEW.id) ELSE NEW.text_state END,
        body_origin = CASE WHEN NEW.body_origin IS NULL OR NEW.body_origin = 'unresolved'
            THEN (SELECT c.body_origin FROM chunks c WHERE c.id = NEW.id) ELSE NEW.body_origin END
    WHERE id = NEW.id;
    SELECT RAISE(IGNORE);
END""")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DR-1 · the sweep reaches every live library
# ---------------------------------------------------------------------------

def test_the_sweep_reaches_every_live_library(tmp_path):
    """DR-1: the sweep must find the ROOT and EVERY agent's library.

    The reverse case: a sweep that only knew the roster would miss
    ``agents/default`` — and a roster-only sweep is what the old code
    did for the INDEX guard.
    """
    ws = _make_workspace(tmp_path)
    found = {lib["name"] for lib in guard_sweep.discover_live_libraries(ws)}
    assert found == {"ROOT", "agent-a", "agent-b"}, found


def test_the_sweep_discovers_by_path_shape_not_by_glob(tmp_path):
    """DR-1: a stray ``*.db`` must NOT be picked up.

    A glob would also match the migration tool's ``*.p1-bak.db``
    backups and the scratch copies in ``tmp/`` — and repairing a
    backup would corrupt the rollback point.
    """
    ws = _make_workspace(tmp_path)
    stray = ws / "memory" / "long-term" / "index.db.p1-bak.db"
    stray.write_bytes(b"not a database")
    scratch = ws / "tmp" / "scratch.db"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(b"not a database either")
    found = {lib["name"] for lib in guard_sweep.discover_live_libraries(ws)}
    assert found == {"ROOT", "agent-a", "agent-b"}, found


def test_the_sweep_upgrades_a_stale_guard_and_the_fingerprint_moves(tmp_path):
    """DR-1 REVERSE CASE: a library holding the OLD guard must be
    upgraded, and the trigger fingerprint must MOVE.

    This is the heart of the delegation's requirement: the mechanism
    must (a) find the stale library, (b) repair it, and (c) make the
    change MEASURABLE — "the fingerprint changed" is the proof, not
    the sweep's own report.
    """
    ws = _make_workspace(tmp_path)
    stale = _lib(ws, "agent-b")
    _install_stale_guard(stale)

    before = cross_end.cross_end_fingerprint(stale)
    assert before is not None

    report = guard_sweep.sweep_body_guard(ws)

    after = cross_end.cross_end_fingerprint(stale)
    assert after != before, (
        "the fingerprint did not move — the repair changed nothing "
        "measurable")
    assert report["uniform"] is True, report["trigger_fingerprint_groups"]
    statuses = {r["library"]: r["status"] for r in report["libraries"]}
    assert statuses["agent-b"] == "repaired", statuses


def test_the_sweep_is_idempotent(tmp_path):
    """DR-1: running the sweep twice must change nothing the second time.

    A sweep that re-repairs on every run is a sweep that cannot be run
    from a startup path.
    """
    ws = _make_workspace(tmp_path)
    first = guard_sweep.sweep_body_guard(ws)
    fp_first = {r["library"]: r.get("trigger_fp_after")
                for r in first["libraries"]}
    second = guard_sweep.sweep_body_guard(ws)
    fp_second = {r["library"]: r.get("trigger_fp_after")
                 for r in second["libraries"]}
    assert fp_first == fp_second, "the second run changed the definitions"
    assert all(r["status"] == "present" for r in second["libraries"]), \
        {r["library"]: r["status"] for r in second["libraries"]}


def test_the_sweep_restores_the_trigger_order(tmp_path):
    """DR-1: after the sweep, the body guard must run BEFORE the FTS
    sync triggers.

    SQLite fires a table's triggers in ``sqlite_master`` rowid order,
    and a re-created trigger lands at the END.  So a repair moves the
    guard behind the FTS triggers — and the legacy DELETE-then-INSERT
    shape then fails with "database disk image is malformed".  The
    sweep must restore the order, for EVERY library (not only the ones
    it repaired).
    """
    ws = _make_workspace(tmp_path)
    guard_sweep.sweep_body_guard(ws)
    for name in ("ROOT", "agent-a", "agent-b"):
        db = _lib(ws, name)
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "ORDER BY rowid").fetchall()
        finally:
            conn.close()
        order = [r[0] for r in rows]
        guard_pos = [i for i, n in enumerate(order)
                     if n in body_guard.guard_trigger_names()]
        fts_pos = [i for i, n in enumerate(order)
                   if n in ("chunks_ai", "chunks_ad", "chunks_au",
                            "chunks_trigram_ai", "chunks_trigram_ad",
                            "chunks_trigram_au")]
        assert guard_pos and fts_pos, (name, order)
        assert max(guard_pos) < min(fts_pos), (
            "%s: the body guard is BEHIND the FTS triggers (%s)" % (name, order))


# ---------------------------------------------------------------------------
# the cross-end fingerprint · seven dimensions
# ---------------------------------------------------------------------------

def test_the_fingerprint_covers_seven_dimensions():
    """The fingerprint must STATE what it covers."""
    assert len(cross_end.CROSS_END_DIMENSIONS) == 7
    assert set(cross_end.CROSS_END_DIMENSIONS) == {
        "columns", "constraints", "triggers", "guard_tables",
        "indexes", "chunker", "domains"}
    # Six of the seven are part of the comparable token.  The seventh
    # (``constraints``) is MEASURED and PRINTED but excluded — and the
    # exclusion is deliberate and measured: a fresh library and a
    # migrated one provably differ in it (see
    # ``test_a_fresh_and_a_migrated_library_share_the_fingerprint``).
    assert set(cross_end.CROSS_END_DIMENSIONS) - set(
        cross_end.TOKEN_DIMENSIONS) == {"constraints"}
    assert len(cross_end.TOKEN_DIMENSIONS) == 6


def test_the_trigger_dimension_sees_a_behaviour_change_the_names_cannot(tmp_path):
    """THE REVERSE CASE that matters: the trigger NAMES are identical
    in both libraries, but the BODIES differ — so a name-set
    fingerprint reports "consistent" and the text fingerprint does not.
    """
    ws = _make_workspace(tmp_path)
    good = _lib(ws, "agent-a")
    bad = _lib(ws, "agent-b")
    _install_stale_guard(bad)

    def _names(db):
        conn = sqlite3.connect("file:%s?mode=ro" % str(db), uri=True)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
        finally:
            conn.close()

    # The name sets agree (both carry the guard's CHUNKS-side names)...
    #
    # NOTE: a FRESH library does not carry the guard's ``files_body_keep_*``
    # triggers — the files-side guard needs the ``files`` table's statistics
    # columns, and a library that has never had a file indexed has neither.
    # That is a legitimate state (``ensure_guard`` skips the files side and
    # says so), not a drift — and it is exactly why the guard tables are a
    # SEPARATE fingerprint dimension: the trigger set and the guard-table
    # set answer different questions.
    chunk_side = {"chunks_body_keep_bi", "chunks_body_keep_bu",
                  "chunks_body_keep_bd", "chunks_body_keep_bi_ai",
                  "chunks_body_keep_st_ai"}
    assert chunk_side <= _names(good)
    assert chunk_side <= _names(bad)
    assert _names(good) == _names(bad), (
        "the two libraries must carry IDENTICAL trigger name sets — that is "
        "the whole point: the names cannot tell them apart")

    # ...but the fingerprints do not.
    assert cross_end.cross_end_fingerprint(good) != \
        cross_end.cross_end_fingerprint(bad)


def test_the_guard_table_dimension_sees_a_deleted_ledger(tmp_path):
    """D4: deleting the ledger must MOVE the fingerprint.

    The triggers are still there and still look correct — but every
    write that fires them would fail with "no such table".  A
    trigger-only fingerprint would NOT move, so the guard tables need
    their own dimension.
    """
    ws = _make_workspace(tmp_path)
    db = _lib(ws, "agent-a")
    before = cross_end.cross_end_fingerprint(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TABLE cert_ledger")
        conn.commit()
    finally:
        conn.close()
    after = cross_end.cross_end_fingerprint(db)
    assert after != before, "deleting the ledger did not move the fingerprint"


def test_a_fresh_and_a_migrated_library_share_the_fingerprint(tmp_path):
    """F-1: the fingerprint must not report a drift between a brand-new
    library and a migrated one.

    MEASURED: a fresh ``CREATE`` gives ``path TEXT NOT NULL`` and an
    ``ALTER``-migrated one keeps ``path TEXT`` — the CONSTRAINTS
    dimension differs, so it must not be part of the comparable token.
    """
    fresh = tmp_path / "fresh.db"
    MemoryStorage(db_path=fresh).close()
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, "
        "source TEXT, path TEXT, start_line INTEGER, end_line INTEGER, "
        "text TEXT, embedding TEXT, hash TEXT, metadata TEXT, "
        "created_at INTEGER, updated_at INTEGER, "
        "hit_count INTEGER NOT NULL DEFAULT 0, last_hit_at INTEGER, "
        "element_released INTEGER NOT NULL DEFAULT 0, "
        "file_sealed INTEGER NOT NULL DEFAULT 0, sealed_owner TEXT)")
    conn.commit()
    conn.close()
    MemoryStorage(db_path=legacy).close()

    fresh_dims = cross_end.cross_end_dimensions(fresh)
    legacy_dims = cross_end.cross_end_dimensions(legacy)
    # The constraints dimension DOES differ (that is why it is excluded).
    assert fresh_dims["constraints"] != legacy_dims["constraints"]
    # But the token must agree.
    assert cross_end.cross_end_fingerprint(fresh) == \
        cross_end.cross_end_fingerprint(legacy)


def test_the_normalisation_ignores_a_rewrap_but_not_a_rewording():
    """The normalisation must be sensitive to BEHAVIOUR and insensitive
    to FORMATTING."""
    a = "CREATE TRIGGER t BEFORE INSERT ON chunks\nBEGIN\n  UPDATE x;\nEND"
    b = "CREATE TRIGGER t BEFORE INSERT ON chunks BEGIN UPDATE x; END"
    c = "CREATE TRIGGER t BEFORE INSERT ON chunks BEGIN UPDATE y; END"
    assert cross_end.normalize_sql(a) == cross_end.normalize_sql(b)
    assert cross_end.normalize_sql(a) != cross_end.normalize_sql(c)


def test_the_fingerprint_of_a_missing_index_is_none(tmp_path):
    """'Unknown' and 'known but wrong' must be distinguishable."""
    assert cross_end.cross_end_fingerprint(tmp_path / "nope.db") is None
    assert cross_end.cross_end_dimensions(tmp_path / "nope.db") is None


# ---------------------------------------------------------------------------
# T2 · the unresolved rows
# ---------------------------------------------------------------------------

def test_the_unresolved_rows_get_a_decided_origin(tmp_path):
    """T2: a row whose origin is undecided must reach a TERMINAL state.

    The judgement is by CONTENT (D1: is the row still a chunk of that
    file?), never by the clock.

    The fixture row's text is taken from the CHUNKER's own output, so
    D1 genuinely holds for it — which is the case the delegation cares
    about ("the pointer must be filled, so the row can be certified
    later").
    """
    from agent.memory.chunker import chunk_for_file
    from agent.memory.decide_origin import decide_unresolved
    from agent.memory.rebuilder import _chunk_id

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    truth = ws / "memory" / "day.md"
    body = "# T\n\n" + ("hello world. " * 200) + "\n"
    truth.write_text(body, encoding="utf-8")
    pieces = chunk_for_file(body, "memory/day.md")
    assert pieces, "the fixture must produce at least one chunk"
    piece = pieces[0]
    # The chunk id MUST follow the id rule (``md5("path:start:end[:part]")``)
    # — the part segment is RECOVERED from the id, so a made-up id makes
    # the row unlocatable, and an unlocatable row is honestly an orphan.
    cid = _chunk_id("memory/day.md", piece.start_line, piece.end_line, 0)

    db = ws / "memory" / "long-term" / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, hash, body_origin, text_state) "
            "VALUES (?, NULL, 'shared', 'memory', 'memory/day.md', "
            "?, ?, ?, 'h1', 'unresolved', 'projection')",
            (cid, piece.start_line, piece.end_line, piece.text))
        conn.commit()
    finally:
        conn.close()

    report = decide_unresolved(ws, db)
    assert report["unresolved_after"] == 0, report
    assert report["decided"] == 1, report

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT body_origin, body_path, body_anchor, body_hash "
            "FROM chunks WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    assert row[0] in (ORIGIN_FILE, ORIGIN_ORPHAN), row[0]
    # The pointer must be filled too — a decided origin with a NULL
    # pointer is a row that still points nowhere.
    assert row[1] is not None, row
    assert row[2] is not None, row


def test_the_decision_is_not_made_by_the_clock(tmp_path):
    """T2 REVERSE CASE: the file's mtime must not change the verdict.

    A ``touch`` on the truth file must not flip a row's origin.
    """
    import time

    from agent.memory.decide_origin import decide_row

    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    truth = ws / "memory" / "day.md"
    truth.write_text("# T\n\n" + ("hello world. " * 200) + "\n", encoding="utf-8")

    row = {
        "id": "r1", "path": "memory/day.md", "start_line": 1, "end_line": 1,
        "text": "hello world. ", "body_anchor": None, "body_hash": None,
    }
    before = decide_row(ws, row)
    # Push the mtime far into the future.
    os.utime(str(truth), (time.time() + 100000, time.time() + 100000))
    after = decide_row(ws, row)
    assert before["origin"] == after["origin"], (before, after)
    assert before["reason"] == after["reason"]


def test_a_row_that_is_not_a_chunk_of_the_file_is_an_orphan(tmp_path):
    """T2: D1 failing must give 'orphan' — and the text is KEPT.

    An orphan is a FACT, not a deletion: the row's content has drifted
    away from the file, and the honest answer is "this row's body came
    from a file it no longer matches".
    """
    from agent.memory.decide_origin import decide_row

    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    truth = ws / "memory" / "day.md"
    truth.write_text("# T\n\ncompletely different content\n", encoding="utf-8")

    verdict = decide_row(ws, {
        "id": "r1", "path": "memory/day.md", "start_line": 1, "end_line": 1,
        "text": "text that is NOT a chunk of that file",
        "body_anchor": None, "body_hash": None,
    })
    assert verdict["origin"] == ORIGIN_ORPHAN, verdict
    assert verdict["d1"] is False, verdict


# ---------------------------------------------------------------------------
# T3 · the ledger's orphan entries (M6f)
# ---------------------------------------------------------------------------

def test_m6f_is_red_when_the_ledger_holds_an_orphan(tmp_path):
    """M6f REVERSE CASE: an orphan entry must turn the criterion RED.

    Every other ledger criterion JOINS the ledger to ``chunks``, so an
    orphan is invisible to all of them.  M6f is the one that asks.
    """
    from agent.memory.ledger_orphans import judge_m6f_ledger_has_no_orphans

    db = tmp_path / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS cert_ledger ("
                     "id TEXT PRIMARY KEY, text_state TEXT, "
                     "body_origin TEXT, body_path TEXT, body_hash TEXT, "
                     "certified_at INTEGER, certified_by TEXT)")
        conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin) "
            "VALUES ('ghost', 'mirror', 'file')")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(db))
    try:
        report = judge_m6f_ledger_has_no_orphans(conn)
    finally:
        conn.close()
    assert report["verdict"] == "FAIL", report
    assert report["orphans"] == 1, report
    assert "ghost" in report["orphan_ids"], report


def test_m6f_is_green_when_the_ledger_has_no_orphan(tmp_path):
    """M6f: the positive case."""
    from agent.memory.ledger_orphans import judge_m6f_ledger_has_no_orphans

    db = tmp_path / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS cert_ledger ("
                     "id TEXT PRIMARY KEY, text_state TEXT, "
                     "body_origin TEXT, body_path TEXT, body_hash TEXT, "
                     "certified_at INTEGER, certified_by TEXT)")
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash)"
            " VALUES ('real', 'p.md', 1, 1, 'x', 'h')")
        conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin) "
            "VALUES ('real', 'mirror', 'file')")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(db))
    try:
        report = judge_m6f_ledger_has_no_orphans(conn)
    finally:
        conn.close()
    assert report["verdict"] == "PASS", report


def test_the_orphan_cleanup_removes_only_the_orphans(tmp_path):
    """T3: the cleanup must remove the orphans and NOTHING else."""
    from agent.memory.ledger_orphans import clean_orphans

    db = tmp_path / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS cert_ledger ("
                     "id TEXT PRIMARY KEY, text_state TEXT, "
                     "body_origin TEXT, body_path TEXT, body_hash TEXT, "
                     "certified_at INTEGER, certified_by TEXT)")
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash)"
            " VALUES ('real', 'p.md', 1, 1, 'x', 'h')")
        conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin) "
            "VALUES ('real', 'mirror', 'file')")
        conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin) "
            "VALUES ('ghost', 'mirror', 'file')")
        conn.commit()
    finally:
        conn.close()

    report = clean_orphans(db)
    assert report["deleted"] == 1, report
    assert report["orphans_after"] == 0, report

    conn = sqlite3.connect(str(db))
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM cert_ledger")}
    finally:
        conn.close()
    assert ids == {"real"}, ids


# ---------------------------------------------------------------------------
# T4 · the _meta chunker-version key
# ---------------------------------------------------------------------------

def test_m6g_is_red_when_the_rename_has_not_run(tmp_path):
    """M6g REVERSE CASE: the legacy key alone must turn the criterion
    RED.

    The point is subtle and it is the whole reason the criterion
    exists: a criterion that merely asked "do the two keys DISAGREE?"
    would call this PASS — an ABSENT key does not disagree with
    anything.  But the rename has not run, so the fleet is half-renamed
    and a reader that knows only the new name finds no version.
    """
    from agent.memory.meta_chunker_key import judge_meta_key_consistency

    db = tmp_path / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT OR REPLACE INTO _meta(key, value) "
                     "VALUES('chunker_version', '3')")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(db))
    try:
        report = judge_meta_key_consistency(conn)
    finally:
        conn.close()
    assert report["verdict"] == "FAIL", report
    assert report["complete"] is False, report


def test_m6g_is_red_when_the_two_keys_disagree(tmp_path):
    """M6g: two names for ONE fact must not carry two answers."""
    from agent.memory.meta_chunker_key import judge_meta_key_consistency

    db = tmp_path / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT OR REPLACE INTO _meta(key, value) "
                     "VALUES('chunker_version', '3')")
        conn.execute("INSERT OR REPLACE INTO _meta(key, value) "
                     "VALUES('last_migration_chunker_version', '4')")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(db))
    try:
        report = judge_meta_key_consistency(conn)
    finally:
        conn.close()
    assert report["verdict"] == "FAIL", report
    assert report["conflict"] is True, report


def test_the_migration_copies_the_value_and_keeps_the_legacy_key(tmp_path):
    """T4: the rename must be complete AND non-destructive.

    The legacy key is KEPT: it is the historical record of what the
    index was built with, and a reader that knows only the old spelling
    must not find the record missing.
    """
    from agent.memory.meta_chunker_key import migrate_meta_key

    db = tmp_path / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT OR REPLACE INTO _meta(key, value) "
                     "VALUES('chunker_version', '3')")
        conn.commit()
    finally:
        conn.close()

    report = migrate_meta_key(db)
    assert report["copied"] is True, report

    conn = sqlite3.connect(str(db))
    try:
        values = dict(conn.execute("SELECT key, value FROM _meta"))
    finally:
        conn.close()
    assert values.get("last_migration_chunker_version") == "3", values
    assert values.get("chunker_version") == "3", values


def test_the_reader_accepts_both_spellings(tmp_path):
    """T4: the reader must find the version under EITHER name."""
    from agent.memory.embedding.state import detect_chunker_version

    s = MemoryStorage(db_path=tmp_path / "index.db")
    try:
        assert detect_chunker_version(s) is None
        s.set_meta("chunker_version", "3")
        assert detect_chunker_version(s) == 3
        s.set_meta("last_migration_chunker_version", "4")
        # The current name wins.
        assert detect_chunker_version(s) == 4
    finally:
        s.close()


def test_the_rename_is_idempotent(tmp_path):
    """T4: running the migration twice must not change the second time."""
    from agent.memory.meta_chunker_key import migrate_meta_key

    db = tmp_path / "index.db"
    MemoryStorage(db_path=db).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT OR REPLACE INTO _meta(key, value) "
                     "VALUES('chunker_version', '3')")
        conn.commit()
    finally:
        conn.close()

    migrate_meta_key(db)
    second = migrate_meta_key(db)
    assert second["copied"] is False, second
    assert second["new_value_after"] == "3", second
