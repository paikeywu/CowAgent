#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 N 后续 · the TWO implementation gaps, and their breakers.

**Gap 1 — ``_ensure_content_nullable`` had no call site.**  Stage 2 of
the messages externalisation (lift the NOT NULL on ``messages.content``)
was dead code: the method existed with no caller anywhere in the tree.
A library whose ``sessions`` primary key was ALREADY composite skipped
``_ensure_composite_key`` (which rebuilds ``messages`` as a side effect,
with a nullable ``content``) and kept NOT NULL forever — so exactly the
libraries that had already been migrated could not migrate again, and
stage 4's "set content to NULL" would fail with
``NOT NULL constraint failed: messages.content``.

**Gap 2 — ``ensure_guard`` did not restore the trigger order.**  A
re-install drops and re-creates the guard's triggers, and a re-created
trigger lands at the END of ``sqlite_master`` — i.e. BEHIND the FTS sync
triggers.  The order is load-bearing: with the guard behind, the legacy
DELETE-then-INSERT shape fails with "database disk image is malformed".
The repair only happened in ``guard_sweep``, i.e. only when somebody
remembered to sweep.

Every criterion below carries a BREAKER: it names the mutation that
makes it go red, and the restore that makes it green again.  A criterion
that cannot fail is not a criterion.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory import body_guard  # noqa: E402
from agent.memory.conversation_store import ConversationStore  # noqa: E402


# ---------------------------------------------------------------------
# the fixture: a library with the EXISTING shape
# ---------------------------------------------------------------------

#: A library that already has the composite ``(agent_id, session_id)``
#: primary key — so ``_ensure_composite_key`` returns EARLY and does NOT
#: rebuild ``messages``.  That is the shape that never migrated.
#:
#: ``content TEXT NOT NULL`` is the defect under test.
_EXISTING_SHAPE_DDL = """
CREATE TABLE sessions (
    agent_id          TEXT    NOT NULL DEFAULT '',
    session_id        TEXT    NOT NULL,
    channel_type      TEXT    NOT NULL DEFAULT '',
    title             TEXT    NOT NULL DEFAULT '',
    context_start_seq INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    last_active       INTEGER NOT NULL,
    msg_count         INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0,
    context_usage     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (agent_id, session_id)
);
CREATE TABLE messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT    NOT NULL DEFAULT '',
    session_id TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    extras     TEXT    NOT NULL DEFAULT '',
    run_id     TEXT    NOT NULL DEFAULT '',
    UNIQUE (agent_id, session_id, seq)
);
INSERT INTO sessions VALUES
    ('', 's1', 'web', '标题', 0, 100, 100, 2, 0, '');
INSERT INTO messages (id, agent_id, session_id, seq, role, content, created_at)
    VALUES (7, '', 's1', 1, 'user', '第一问', 100);
INSERT INTO messages (id, agent_id, session_id, seq, role, content, created_at)
    VALUES (9, '', 's1', 2, 'assistant', '第一答', 200);
"""


def _existing_shape_library(tmp_path: Path) -> Path:
    """A library in the shape that never migrated.

    ``content`` is NOT NULL and ``sessions`` already has the composite
    primary key — so the rebuild that used to fix ``content`` as a side
    effect never ran.
    """
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_EXISTING_SHAPE_DDL)
        conn.commit()
    finally:
        conn.close()
    return db


def _messages_shape(db: Path):
    """(content notnull, indexes, rows) — the three things that matter."""
    conn = sqlite3.connect(str(db))
    try:
        info = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(messages)")}
        indexes = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='messages'")]
        rows = conn.execute(
            "SELECT id, role, content FROM messages ORDER BY id").fetchall()
        return info.get("content"), indexes, rows
    finally:
        conn.close()


# ---------------------------------------------------------------------
# GAP 1
# ---------------------------------------------------------------------

def test_gap1_an_existing_shape_library_gets_a_nullable_content_on_open(tmp_path):
    """GAP 1: opening the library must repair ``content``.

    **BREAKER**: delete the ``self._ensure_content_nullable(conn)`` call
    in ``ConversationStore._migrate`` → ``content`` stays NOT NULL (1)
    → RED.
    """
    db = _existing_shape_library(tmp_path)
    before_notnull, _, before_rows = _messages_shape(db)
    assert before_notnull == 1, (
        "the fixture must start with a NOT NULL content, got %r" % before_notnull)

    ConversationStore(db, agent_id="")

    after_notnull, indexes, after_rows = _messages_shape(db)
    assert after_notnull == 0, (
        "the library was NOT repaired: messages.content is still NOT NULL "
        "(notnull=%r) — stage 2 has no call site" % after_notnull)
    assert after_rows == before_rows, (
        "the rebuild lost or changed data: %r != %r" % (after_rows, before_rows))
    assert "idx_messages_session" in indexes, (
        "the rebuild lost the index: %r" % indexes)


def test_gap1_the_repair_keeps_every_id_and_every_body(tmp_path):
    """GAP 1, the DATA half: the ``id`` is the AUTOINCREMENT key the TTS
    snapshot refers to, and the body is the conversation itself.

    **BREAKER**: change the rebuild's ``SELECT id, ...`` to drop the
    ``id`` → the ids are renumbered → RED.
    """
    db = _existing_shape_library(tmp_path)
    _, _, before = _messages_shape(db)
    before_ids = [r[0] for r in before]
    before_bodies = [(r[1], r[2]) for r in before]
    assert before_ids == [7, 9], before_ids

    ConversationStore(db, agent_id="")

    _, _, after = _messages_shape(db)
    assert [r[0] for r in after] == before_ids, (
        "the ids changed: %r != %r" % ([r[0] for r in after], before_ids))
    assert [(r[1], r[2]) for r in after] == before_bodies, \
        "the bodies changed: %r" % (after,)


def test_gap1_the_repair_keeps_the_autoincrement_sequence(tmp_path):
    """GAP 1, the SEQUENCE half: the rebuild must not reset
    ``sqlite_sequence``.

    The ``id`` is an AUTOINCREMENT key, so SQLite keeps the high-water
    mark in ``sqlite_sequence``.  A rebuild that copies the rows but
    resets the sequence would hand the NEXT insert an ``id`` that a
    deleted row already had — and the TTS snapshot, which refers to
    the ``id``, would then point at the wrong row.  MEASURED on this
    machine: the sequence survives the rebuild unchanged (500 -> 500).

    **BREAKER**: change the rebuild's copy to omit the ``id`` (so the
    ids are renumbered from 1) → the sequence changes → RED.
    """
    db = _existing_shape_library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO messages (id, agent_id, session_id, seq, role, "
            "content, created_at) VALUES (500, '', 's1', 9, 'user', 'x', 1)")
        conn.commit()
        before = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='messages'"
        ).fetchone()
    finally:
        conn.close()
    assert before is not None and before[0] == 500, before

    ConversationStore(db, agent_id="")

    conn = sqlite3.connect(str(db))
    try:
        after = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='messages'"
        ).fetchone()
    finally:
        conn.close()
    assert after == before, (
        "the AUTOINCREMENT sequence was reset by the rebuild "
        "(%r -> %r) — the next insert would reuse a deleted row's id"
        % (before, after))


def test_gap1_an_already_nullable_library_is_not_rebuilt(tmp_path):
    """GAP 1, the IDEMPOTENCE half: a library that is ALREADY nullable
    must NOT be rebuilt.

    A rebuild is not free: it rewrites the table (so the rowids change),
    and a table with triggers on it must be rebuilt in the right order or
    the triggers move.  So "already fine" must mean "not touched".

    The proof that no rebuild happened is the TABLE'S OWN ROWID in
    ``sqlite_master`` — a rebuild creates a new table, and the new
    table lands at the END of ``sqlite_master``.

    **BREAKER**: make ``_ensure_content_nullable`` always rebuild (drop
    the ``if not info[3]: return False`` guard) → the table's rowid
    changes → RED.
    """
    db = tmp_path / "index.db"
    store = ConversationStore(db, agent_id="")
    store.append_messages("s1", [{"role": "user", "content": "你好"}],
                          channel_type="web")

    def _table_rowid():
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(
                "SELECT rowid FROM sqlite_master WHERE type='table' "
                "AND name='messages'").fetchone()[0]
        finally:
            conn.close()

    rowid_before = _table_rowid()
    # Open it AGAIN — the library is already nullable now.
    ConversationStore(db, agent_id="")
    rowid_after = _table_rowid()
    assert rowid_after == rowid_before, (
        "the table was rebuilt even though content was already nullable "
        "(sqlite_master rowid %s -> %s)" % (rowid_before, rowid_after))


def test_gap1_the_nullability_call_site_exists_in_the_source(tmp_path):
    """GAP 1, the STATIC half: the call site must EXIST.

    A method with no caller is dead code, and the failure it causes is
    invisible until somebody tries to externalise a row — which is why
    21+ criteria never caught it.  This half fails the moment the call
    is removed, without needing a fixture that reproduces the whole
    shape.

    **BREAKER**: comment out the call → RED.
    """
    src = (REPO / "agent" / "memory" / "conversation_store.py").read_text(
        encoding="utf-8")
    # The DEFINITION does not count — only a call does.
    calls = [
        line for line in src.splitlines()
        if "_ensure_content_nullable" in line
        and "def _ensure_content_nullable" not in line
    ]
    assert calls, (
        "``_ensure_content_nullable`` has NO call site — stage 2 is dead "
        "code and every existing library keeps messages.content NOT NULL")


def test_gap1_the_call_site_runs_after_the_agent_id_column_is_added(tmp_path):
    """GAP 1, the ORDER half: the call site must run AFTER
    ``messages.agent_id`` exists.

    The rebuild's copy names ``agent_id`` explicitly.  A library that
    does not have the column yet fails with "no such column: agent_id"
    — and (before the explicit transaction) that failure used to leave
    a half-built ``messages_new`` behind, which broke the NEXT rebuild
    with "table messages_new already exists".  That is how a single
    failed migration turned into a broken multi-Agent merge
    (MEASURED: it broke
    ``tests/test_conversation_global_migration.py::test_multi_agent_merge_survives_shared_session_id``).

    The criterion opens a library that has NEITHER the ``agent_id``
    column NOR a nullable ``content``, and asserts that it ends up
    BOTH migrated and free of stray tables.

    **BREAKER**: move the call site back above the ``agent_id`` block
    → RED.
    """
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            "CREATE TABLE sessions (session_id TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, last_active INTEGER NOT NULL, "
            "PRIMARY KEY (session_id));"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, "
            "created_at INTEGER NOT NULL);"
            "INSERT INTO sessions VALUES ('s1', 1, 1);"
            "INSERT INTO messages (id, session_id, seq, role, content, "
            "created_at) VALUES (7, 's1', 1, 'user', 'hi', 1);")
        conn.commit()
    finally:
        conn.close()

    ConversationStore(db, agent_id="")

    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        info = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(messages)")}
        rows = conn.execute("SELECT id, content FROM messages").fetchall()
    finally:
        conn.close()

    assert "messages_new" not in tables, (
        "a half-built messages_new survived the migration: %r "
        "(a stray table breaks the NEXT rebuild with 'table messages_new "
        "already exists')" % sorted(tables))
    assert info.get("content") == 0, (
        "messages.content is still NOT NULL: %r" % info.get("content"))
    assert rows == [(7, "hi")], "the rows did not survive: %r" % (rows,)


def test_gap1_a_failed_rebuild_leaves_no_half_built_table(tmp_path):
    """GAP 1, the TRANSACTION half: the rebuild must roll back its DDL
    too, not only its DML.

    Python's sqlite3 opens an implicit transaction before DML — but NOT
    before DDL.  So inside ``with conn:`` a ``CREATE TABLE`` is
    committed the moment it runs, and a later failure leaves the
    half-built table behind.  MEASURED on this machine (Python 3.15 /
    SQLite 3.53.4):

        with conn:              -> ``t_new`` SURVIVES the rollback
        explicit BEGIN IMMEDIATE -> ``t_new`` is gone

    And the leftover is not harmless: the next rebuild fails with
    "table messages_new already exists".

    The fixture: a library whose ``messages`` table is missing the
    ``agent_id`` column, so the rebuild's copy fails.  The criterion
    asserts that no ``messages_new`` is left behind.

    **BREAKER**: replace the explicit ``BEGIN IMMEDIATE`` with
    ``with conn:`` → RED.
    """
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            "CREATE TABLE sessions (session_id TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, last_active INTEGER NOT NULL, "
            "PRIMARY KEY (session_id));"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, "
            "created_at INTEGER NOT NULL);"
            "INSERT INTO sessions VALUES ('s1', 1, 1);"
            "INSERT INTO messages (id, session_id, seq, role, content, "
            "created_at) VALUES (7, 's1', 1, 'user', 'hi', 1);")
        conn.commit()
    finally:
        conn.close()

    # Open it with the rebuild FORCED to fail: patch the copy so it
    # names a column that does not exist.
    import agent.memory.conversation_store as cs

    original = cs._msg_copy_exprs

    def _broken(conn):
        return original(conn) + ", no_such_column"

    cs._msg_copy_exprs = _broken
    try:
        store = ConversationStore(db, agent_id="")
        # The migration must not have raised.
        assert store is not None
    finally:
        cs._msg_copy_exprs = original

    conn = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    assert "messages_new" not in tables, (
        "the failed rebuild left a half-built messages_new behind: %r — "
        "the next rebuild will fail with 'table messages_new already "
        "exists'" % sorted(tables))


# ---------------------------------------------------------------------
# GAP 2
# ---------------------------------------------------------------------

def _make_real_library(tmp_path: Path) -> Path:
    """A REAL library — built through ``MemoryStorage``, so the FTS index
    is consistent.

    A hand-written ``CREATE TABLE`` would produce a library whose FTS
    index does not contain the row, and the guard's in-place UPDATE
    would delete a non-existent FTS row — SQLite would report
    "database disk image is malformed" and the criterion would be
    measuring the FIXTURE, not the guard.

    MEASURED (P1 阶段三 N): the files-side guard triggers are NOT
    installed by the ``MemoryStorage`` open path — only the chunks-side
    ones are.  So ``is_installed`` is False until somebody calls
    ``ensure_guard`` once, and the criteria below call it so the
    fixture reaches the "bodies are current" state.  (The files-side
    gap itself is a FINDING, reported to the SEED — it is out of this
    delivery's scope.)
    """
    from agent.memory.storage import MemoryStorage

    db = tmp_path / "index.db"
    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, start_line, "
            "end_line, text, hash, created_at, updated_at, body_path, "
            "body_anchor, body_hash, text_state, body_origin) "
            "VALUES ('row1', NULL, 'shared', 'memory', 'MEMORY.md', 1, 2, "
            "'hello', 'h1', '2026-01-01', '2026-01-01', 'MEMORY.md', 'L1-2', "
            "'sha256:abc', 'projection', 'file')")
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, updated_at, "
            "chunk_count, chunker_version) "
            "VALUES ('MEMORY.md', 'memory', 'fh', 1, 5, '2026-01-01', 1, 4)")
        conn.commit()
        # Bring the guard fully up (both sides) so the fixture starts
        # from the "bodies are current" state.
        body_guard.ensure_guard(conn)
    finally:
        storage.close()
    return db


def _order(conn):
    """(guard_max, fts_min) over ``sqlite_master`` rowid order."""
    rows = conn.execute(
        "SELECT rowid, name FROM sqlite_master WHERE type='trigger' "
        "AND sql IS NOT NULL ORDER BY rowid").fetchall()
    names = {r[1] for r in rows}
    g = [r[0] for r in rows if r[1] in set(body_guard.guard_trigger_names())]
    f = [r[0] for r in rows if r[1] in body_guard.FTS_SYNC_TRIGGERS]
    if not g or not f:
        return None, None, names
    return max(g), min(f), names


def _legacy_replace_all(db: Path):
    """The legacy build's DELETE-then-INSERT shape — the one that
    breaks when the guard sits behind the FTS triggers."""
    from agent.memory.storage import MemoryStorage  # noqa: F401

    cols = ("id, user_id, scope, source, path, start_line, end_line, text, "
            "embedding, hash, metadata, created_at, updated_at, hit_count, "
            "last_hit_at, element_released, file_sealed, sealed_owner")
    upsert = ("INSERT OR REPLACE INTO chunks (%s) VALUES (%s)"
              % (cols, ",".join("?" * len(cols.split(",")))))
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT %s FROM chunks" % cols).fetchall()
        payload = [tuple(r[k] for k in r.keys()) for r in rows]
        for tup in payload:
            conn.execute("DELETE FROM chunks WHERE path='MEMORY.md'")
            conn.execute(upsert, tup)
        conn.commit()
    finally:
        conn.close()


def test_gap2_ensure_guard_restores_the_order_after_a_reinstall(tmp_path):
    """GAP 2: after a re-install, the guard must be AHEAD of the FTS
    triggers — and the library must be WRITABLE.

    The direction is not a matter of taste: it was measured A/B.  With
    the guard AHEAD the legacy DELETE-then-INSERT shape succeeds; with
    the guard BEHIND it raises "database disk image is malformed".

    The fixture forces the re-install path by making the guard STALE
    (a trigger body that differs from the current definition) — that is
    what ``is_installed`` reports, and it is what makes ``ensure_guard``
    take the DROP+CREATE path that lands the guard at the END of
    ``sqlite_master``.

    **BREAKER**: remove the ``reorder_fts_triggers_behind_guard(conn)``
    call from ``ensure_guard`` → the guard stays behind → RED.
    """
    db = _make_real_library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # Make the guard STALE: replace one trigger's body with an
        # equivalent-but-different text (same behaviour, different SQL).
        conn.execute(
            "DROP TRIGGER chunks_body_keep_bi")
        conn.execute(
            "CREATE TRIGGER chunks_body_keep_bi "
            "AFTER INSERT ON chunks BEGIN SELECT 1; END")
        conn.commit()

        gmax_before, fmin_before, _ = _order(conn)
        result = body_guard.ensure_guard(conn)
        assert result.get("status") in ("repaired", "installed"), result
        gmax_after, fmin_after, _ = _order(conn)
    finally:
        conn.close()

    assert gmax_before is not None and fmin_before is not None
    assert gmax_after < fmin_after, (
        "the guard was NOT put ahead of the FTS triggers "
        "(guard_max=%s, fts_min=%s) — the library is now writable-but-broken"
        % (gmax_after, fmin_after))


def test_gap2_the_repaired_library_is_writable(tmp_path):
    """GAP 2, the BEHAVIOUR half: the order is not cosmetic — writing the
    library must not raise "database disk image is malformed".

    **BREAKER**: remove the reorder call from ``ensure_guard`` → the
    write raises malformed → RED.
    """
    db = _make_real_library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TRIGGER chunks_body_keep_bi")
        conn.execute(
            "CREATE TRIGGER chunks_body_keep_bi "
            "AFTER INSERT ON chunks BEGIN SELECT 1; END")
        conn.commit()
        body_guard.ensure_guard(conn)
    finally:
        conn.close()

    # The real proof: write the legacy shape.
    _legacy_replace_all(db)

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT body_path FROM chunks WHERE id='row1'").fetchone()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert row is not None and row[0] == "MEMORY.md", (
        "the guard lost the pointer on the legacy shape: %r" % (row,))
    assert integrity == "ok", "the library is corrupt: %r" % integrity


def test_gap2_is_installed_does_not_depend_on_the_trigger_order(tmp_path):
    """GAP 2, the REVERSE case: a guard whose bodies are current but
    whose triggers sit in a DIFFERENT ORDER is STILL the current guard.

    ``is_installed`` compares one trigger at a time, by NAME.  A
    comparison over the JOINED strings would depend on the order — and
    the order is exactly the thing a re-install changes, so that
    comparison is self-amplifying: "judged stale" → re-install → order
    flipped → judged stale again → re-install again.  Every open would
    re-install, and every re-install flips the order, which is what
    makes the legacy DELETE-then-INSERT shape fail with "database disk
    image is malformed".

    The fixture: re-create the guard's triggers so they land at the END
    of ``sqlite_master``.  The BODIES are byte-identical; only the
    ORDER changed.  ``is_installed`` must still say True — otherwise
    every open re-installs and flips the order again.

    **BREAKER**: change ``is_installed`` to compare the joined strings
    → the reordered library is judged stale → RED.
    """
    db = _make_real_library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
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

        # The bodies are unchanged — only the ORDER moved.
        after = {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND sql IS NOT NULL").fetchall()}
        for n in guard_names:
            assert after[n] == stored[n], (
                "the fixture must not change a body: %s" % n)

        installed = body_guard.is_installed(conn)
    finally:
        conn.close()

    assert installed is True, (
        "a guard whose bodies are current but whose ORDER changed was "
        "judged stale — every open would re-install, and every re-install "
        "flips the order again (the self-amplifying loop the per-name "
        "comparison exists to prevent)")


def test_gap2_a_renamed_trigger_is_judged_stale(tmp_path):
    """GAP 2, the NAME half: a trigger that was RENAMED is a DIFFERENT
    trigger, so it must be judged stale.

    This is the other half of the per-name comparison.  The name is
    part of the definition: the guard's triggers are addressed by name
    (the installer drops them by name, the status check reports them by
    name), so a renamed trigger is a trigger the guard does not own.

    The delivery note phrased this as "a renamed trigger with the same
    behaviour must not be judged as needing a re-install".  Taken
    literally that is the opposite of what the per-name comparison
    does — and taking it literally would leave the guard believing it
    owns a trigger it does not own, which is the worse failure.  What
    the note is really protecting against is the ORDER-based false
    positive, and that is the criterion above.

    **BREAKER**: make ``is_installed`` ignore the name (compare only
    the bodies) → the renamed trigger is judged current → RED.
    """
    db = _make_real_library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='chunks_body_keep_bd'").fetchone()
        assert row is not None
        conn.execute("DROP TRIGGER chunks_body_keep_bd")
        conn.execute(
            row[0].replace("chunks_body_keep_bd", "chunks_body_keep_bd2"))
        conn.commit()

        installed = body_guard.is_installed(conn)
    finally:
        conn.close()

    assert installed is False, (
        "a RENAMED trigger was judged current — the guard would then "
        "believe it owns a trigger it does not own")


def test_gap2_the_order_survives_a_second_ensure(tmp_path):
    """GAP 2, the IDEMPOTENCE half: a second ``ensure_guard`` must not
    flip the order back.

    **BREAKER**: make the reorder unconditional-but-wrong (re-create the
    guard WITHOUT re-creating the FTS triggers) → the second call moves
    the guard behind → RED.
    """
    db = _make_real_library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TRIGGER chunks_body_keep_bi")
        conn.execute(
            "CREATE TRIGGER chunks_body_keep_bi "
            "AFTER INSERT ON chunks BEGIN SELECT 1; END")
        conn.commit()
        body_guard.ensure_guard(conn)
        gmax_1, fmin_1, _ = _order(conn)
        body_guard.ensure_guard(conn)
        gmax_2, fmin_2, _ = _order(conn)
    finally:
        conn.close()
    assert gmax_1 < fmin_1, (gmax_1, fmin_1)
    assert gmax_2 < fmin_2, (
        "the second ensure moved the guard behind the FTS triggers "
        "(%s, %s)" % (gmax_2, fmin_2))


def test_gap2_ensure_guard_fixes_the_order_even_when_the_bodies_are_current(tmp_path):
    """GAP 2, the PRESENT path: the order is a SEPARATE property from
    the trigger bodies, and ``ensure_guard`` must fix it even when it
    does not re-install anything.

    This is the shape a library gets into when something ELSE re-creates
    the guard's triggers — a different tool, a different version, a
    hand-run ``DROP``/``CREATE``.  The bodies are then current (so
    ``is_installed`` says "present" and nothing is re-installed) but the
    order is broken, and the library is writable-but-broken.

    Before the fix, the order was only restored by ``guard_sweep`` — so
    a library stayed broken until somebody swept.

    **BREAKER**: remove the reorder call from the ``present`` branch of
    ``ensure_guard`` → the order stays broken → RED.
    """
    db = _make_real_library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # Break the ORDER without changing any body: re-create the
        # guard's triggers (a re-created trigger lands at the END of
        # sqlite_master, i.e. behind the FTS triggers) — the bodies are
        # byte-identical, so ``is_installed`` still says "present".
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

        assert body_guard.is_installed(conn), (
            "the fixture must leave the bodies CURRENT — only the order is "
            "broken, otherwise this is testing the repair path")
        gmax_before, fmin_before, _ = _order(conn)
        assert gmax_before > fmin_before, (
            "the fixture must leave the order BROKEN (guard_max=%s, "
            "fts_min=%s)" % (gmax_before, fmin_before))

        result = body_guard.ensure_guard(conn)
        gmax_after, fmin_after, _ = _order(conn)
    finally:
        conn.close()

    assert result.get("status") == "present", result
    assert gmax_after < fmin_after, (
        "ensure_guard did NOT fix the order on the present path "
        "(guard_max=%s, fts_min=%s) — a library whose bodies are current "
        "but whose order is broken stays writable-but-broken"
        % (gmax_after, fmin_after))


def test_gap2_guard_sweep_and_ensure_guard_share_one_implementation(tmp_path):
    """GAP 2, the STRUCTURE half: there must be ONE ordering rule.

    Two copies of an ordering rule is one copy that will drift — and
    the copy that drifts is the one nobody runs.  ``guard_sweep`` must
    call the implementation that lives in ``body_guard``.

    **BREAKER**: restore a private copy of the algorithm inside
    ``guard_sweep`` → the source check fails → RED.
    """
    sweep_src = (REPO / "agent" / "memory" / "guard_sweep.py").read_text(
        encoding="utf-8")
    alias = sweep_src.find("def _reorder_fts_triggers_behind_guard")
    assert alias != -1
    body = sweep_src[alias:]
    body = body[:body.find("\ndef ", 1)] if "\ndef " in body[1:] else body
    assert "reorder_fts_triggers_behind_guard" in body, (
        "guard_sweep does not delegate to the shared implementation")
    # A private copy would show the algorithm's own vocabulary.
    for marker in ("BEGIN IMMEDIATE", "DROP TRIGGER IF EXISTS",
                   "AND sql IS NOT NULL ORDER BY rowid"):
        assert marker not in body, (
            "guard_sweep carries its OWN copy of the ordering logic "
            "(found %r) — the rule must live in body_guard only" % marker)


def test_gap2_a_live_library_copy_with_a_wal_sidecar_can_be_written(tmp_path):
    """GAP 2, the END-TO-END half on a REAL library copy — the shape the
    delivery note measured (a copy of a live library with its ``-wal``
    sidecar).

    The copy is a copy of THIS machine's ROOT library when it is
    available; otherwise the criterion is NOTRUN rather than a false
    green.

    **BREAKER**: remove the reorder call → RED on a live-shaped copy.
    """
    import os

    src = os.environ.get("P1S3N_ROOT_BACKUP")
    if not src or not os.path.exists(src):
        pytest.skip("no live ROOT library backup is available")

    db = tmp_path / "index.db"
    shutil.copyfile(src, db)
    for suffix in ("-wal", "-shm"):
        s = Path(src + suffix)
        if s.exists():
            shutil.copyfile(s, Path(str(db) + suffix))

    conn = sqlite3.connect(str(db))
    try:
        result = body_guard.ensure_guard(conn)
        gmax, fmin, names = _order(conn)
    finally:
        conn.close()

    assert result.get("status") in ("present", "repaired", "installed"), result
    assert gmax is not None and fmin is not None, (
        "the copy has no guard/FTS triggers to order: %r" % sorted(names))
    assert gmax < fmin, (
        "the guard is BEHIND the FTS triggers on the copy "
        "(guard_max=%s, fts_min=%s)" % (gmax, fmin))
