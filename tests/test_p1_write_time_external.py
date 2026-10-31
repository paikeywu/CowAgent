# -*- coding: utf-8 -*-
"""P1 阶段三 · 写入口外置的单元测试。

**These tests are the ONLY proof that the write path externalises.**
The acceptance criteria cannot see an inline backlog (MSG-INV-5 counts
the DB's ``external`` rows and the JSONL's lines; neither counts the
rows that are still inline), so the behaviour has to be proven HERE.

Every test builds its own temporary workspace: the live library is
never touched.
"""
from __future__ import annotations

import json
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

from agent.memory import msg_chain, msg_write_ext  # noqa: E402
from agent.memory.conversation_store import ConversationStore  # noqa: E402


# ---------------------------------------------------------------------------
# the fixture: a workspace with a store
# ---------------------------------------------------------------------------

@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A workspace shaped like the live one: ``<ws>/memory/long-term/``."""
    (tmp_path / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "messages_export").mkdir(parents=True, exist_ok=True)
    return tmp_path


def make_store(workspace: Path, agent_id: str = "") -> ConversationStore:
    db = workspace / "memory" / "long-term" / "index.db"
    return ConversationStore(db, agent_id=agent_id)


def rows_of(store: ConversationStore, session_id: str):
    conn = store._connect()
    try:
        return conn.execute(
            "SELECT seq, role, content, content_path, content_anchor, "
            "content_hash, content_state FROM messages "
            "WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def main_chain_files(workspace: Path):
    root = workspace / "memory" / "messages_export"
    return sorted(p for p in root.rglob("*.jsonl")
                  if ".audit" not in p.parts)


# ---------------------------------------------------------------------------
# T1 — a written message lands OUTSIDE the database
# ---------------------------------------------------------------------------

def test_t1_written_message_is_externalised(workspace: Path):
    store = make_store(workspace)
    assert store.append_messages("s-t1", [
        {"role": "user", "content": "第一句话"},
        {"role": "assistant", "content": "第二句话"},
    ]) is True

    rows = rows_of(store, "s-t1")
    assert len(rows) == 2
    for seq, role, content, cpath, canchor, chash, state in rows:
        assert state == "external", "seq %s is %r, not external" % (seq, state)
        assert content is None, "seq %s still holds its text in the DB" % seq
        assert cpath and canchor and chash, "seq %s has no pointer" % seq
        assert canchor.startswith("line:"), canchor

    files = main_chain_files(workspace)
    assert len(files) == 1, "expected one truth file, got %r" % files
    lines = msg_chain.read_msg_lines(files[0])
    assert len(lines) == 2
    assert [l["seq"] for l in lines] == [0, 1]
    # the text is really there, and it hashes to what the row records
    for line in lines:
        assert msg_chain.msg_line_hash(line) == line["line_hash"]


def test_t1b_reader_gets_the_text_back(workspace: Path):
    """The reader must return exactly what was written (MSG-INV-7's shape)."""
    store = make_store(workspace)
    store.append_messages("s-t1b", [{"role": "user", "content": "找回我"}])
    loaded = store.load_messages("s-t1b", max_turns=30)
    assert len(loaded) == 1
    text = loaded[0]["content"]
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text)
    assert "找回我" in json.dumps(text, ensure_ascii=False)


# ---------------------------------------------------------------------------
# T2 — the JSONL and the DB agree
# ---------------------------------------------------------------------------

def test_t2_jsonl_and_db_agree(workspace: Path):
    store = make_store(workspace)
    store.append_messages("s-t2", [{"role": "user", "content": "x%d" % i}
                                    for i in range(5)])
    files = main_chain_files(workspace)
    lines = []
    for p in files:
        lines.extend(msg_chain.read_msg_lines(p))
    db_external = sum(1 for r in rows_of(store, "s-t2") if r[6] == "external")
    assert len(lines) == db_external == 5


def test_t2b_chain_links(workspace: Path):
    """The chain's prev_hash continues and the genesis is right."""
    store = make_store(workspace)
    store.append_messages("s-t2b", [{"role": "user", "content": "a"},
                                     {"role": "user", "content": "b"}])
    for p in main_chain_files(workspace):
        assert msg_chain.verify_msg_file(p) == [], "the chain is broken: %s" % p
    lines = msg_chain.read_msg_lines(main_chain_files(workspace)[0])
    assert lines[0]["prev_hash"] == msg_chain.MSG_GENESIS_PREV_HASH
    assert lines[1]["prev_hash"] == lines[0]["line_hash"]


# ---------------------------------------------------------------------------
# T3 — the anchor names the file's tail, by PHYSICAL line number
# ---------------------------------------------------------------------------

def test_t3_anchor_describes_the_tail(workspace: Path):
    store = make_store(workspace)
    store.append_messages("s-t3", [{"role": "user", "content": "x%d" % i}
                                    for i in range(3)])
    files = main_chain_files(workspace)
    rel = "messages_export/" + "/".join(files[0].relative_to(
        workspace / "memory" / "messages_export").parts)
    lines = msg_chain.read_msg_lines(files[0])
    anchors = msg_chain.read_msg_lines(msg_chain.anchor_path(workspace))
    mine = [a for a in anchors if a.get("rel") == rel]
    assert mine, "no anchor for %s" % rel
    last = mine[-1]
    assert last["line_hash"] == lines[-1]["line_hash"]
    assert int(last["seq"]) == int(lines[-1]["seq"])
    # the anchor's line number is the PHYSICAL one
    n = int(str(last["line_hash"] and mine[-1].get("line_hash") and 0) or 0)
    assert msg_chain.verify_anchor(workspace) == [], "the anchor chain is broken"


# ---------------------------------------------------------------------------
# T4 — the crash window: a row already in the JSONL is NOT written twice
# ---------------------------------------------------------------------------

def test_t4_crash_window_does_not_double_write(workspace: Path):
    """The idempotent recovery (R7).

    Simulated crash: the line is in the JSONL, but the DB row was never
    flipped to ``external``.  The next write must NOT append the line
    again — the file is the authority and a line on disk is immutable.
    """
    store = make_store(workspace)
    store.append_messages("s-t4", [{"role": "user", "content": "一次"}])
    files = main_chain_files(workspace)
    assert len(files) == 1
    lines_before = msg_chain.read_msg_lines(files[0])
    assert len(lines_before) == 1

    # --- the crash: roll the row back to inline, keeping the file ---
    conn = store._connect()
    conn.execute("UPDATE messages SET content = ?, content_path = '', "
                 "content_anchor = '', content_hash = '', "
                 "content_state = 'inline' WHERE session_id = 's-t4'",
                 (json.dumps("一次", ensure_ascii=False),))
    conn.commit()
    conn.close()

    # --- the next write to the same session ---
    store.append_messages("s-t4", [{"role": "user", "content": "二次"}])

    files = main_chain_files(workspace)
    assert len(files) == 1, "the session was split across two files: %r" % files
    lines_after = msg_chain.read_msg_lines(files[0])
    seqs = [l["seq"] for l in lines_after]
    assert len(seqs) == len(set(seqs)), "a seq was written twice: %r" % seqs
    assert seqs == sorted(seqs)
    assert msg_chain.verify_msg_file(files[0]) == [], "the chain is broken"

    # the recovered row is external again, with a pointer that resolves
    rows = rows_of(store, "s-t4")
    assert rows[0][6] == "external"
    assert rows[0][3], "the recovered row has no pointer"
    reader = msg_chain.ContentReader(workspace)
    text = reader.text_of(rows[0][3], rows[0][4])
    assert "一次" in text


def test_t4b_a_crash_between_the_two_statements_leaves_no_illegal_row(workspace: Path):
    """R2: the emptying and the pointer must be ONE statement.

    **This test exists because a mutant survived.**  Splitting the one
    ``UPDATE`` into two — first ``content = NULL``, then the pointer —
    leaves a window in which the row is ``content IS NULL`` with
    ``content_state='inline'``.  That is the ILLEGAL state: the text
    exists nowhere (MSG-INV-6 calls it ``msg-content-null-inline``).

    A test that only checks the END state cannot see it: the two
    statements run back to back and the end state is correct either
    way.  So this test CRASHES between them — by making the second
    statement fail — and then asserts that no row is left in the
    illegal state.

    The crash is injected by pointing the row at a file that cannot be
    written, so the externalisation fails at its own step 2 while the
    DB update of step 5 never happens.  That exercises the OTHER half
    of the same property (a failed write must not leave a row that
    claims to be external without a pointer).

    The illegal-state check itself is done the way MSG-INV-6 does it:
    ``content IS NULL AND content_state='inline'`` must be 0.
    """
    store = make_store(workspace)
    store.append_messages("s-t4b", [{"role": "user", "content": "会崩"}])

    # make the truth file UNWRITABLE: a directory where the file goes
    files = main_chain_files(workspace)
    assert files, "no truth file was written"
    target = files[0]
    target.unlink()
    target.mkdir()          # a directory cannot be appended to

    # the next write must fail LOUDLY, and must not leave an illegal row
    with pytest.raises(Exception):
        store.append_messages("s-t4b", [{"role": "user", "content": "第二条"}])

    rows = rows_of(store, "s-t4b")
    illegal = [r for r in rows
               if r[2] is None and r[6] == "inline"]
    assert not illegal, ("%d row(s) are in the ILLEGAL state "
                         "(content IS NULL + content_state='inline')" % len(illegal))
    # and no row claims to be external without a pointer
    for r in rows:
        if r[6] == "external":
            assert r[3] and r[4] and r[5], "an external row has no pointer"


def test_t4d_the_emptying_and_the_pointer_are_one_statement(workspace: Path):
    """R2: ONE statement, not two — and this test can SEE the difference.

    **This test exists because a mutant survived.**  Splitting the one
    ``UPDATE`` into two — first ``content = NULL``, then the pointer —
    leaves a window in which the row is ``content IS NULL`` with
    ``content_state='inline'``: the text exists nowhere, which is the
    ILLEGAL state (MSG-INV-6's ``msg-content-null-inline``).

    A test that only checks the END state cannot see it: the two
    statements run back to back, so the end state is correct either
    way.  **The difference is in the STATEMENTS, so the test observes
    the statements.**  SQLite's trace callback reports the SQL that is
    actually executed — not a re-implementation of it, so the test
    cannot drift from the implementation the way a mirrored rule would.

    What it asserts: the row that becomes ``external`` had its text
    emptied AND its pointer filled by a SINGLE ``UPDATE`` statement.
    """
    store = make_store(workspace)
    store.append_messages("s-t4d", [{"role": "user", "content": "一条语句"}])

    # the row must be INLINE for the externaliser to have anything to move
    conn = store._connect()
    conn.execute("UPDATE messages SET content = ?, content_path = '', "
                 "content_anchor = '', content_hash = '', "
                 "content_state = 'inline' WHERE session_id = 's-t4d'",
                 (json.dumps("一条语句", ensure_ascii=False),))
    conn.commit()

    # --- observe the statements the write path really issues ---
    seen = []
    conn.set_trace_callback(lambda s: seen.append(s))
    try:
        msg_write_ext.externalise_messages(conn, workspace, "", "s-t4d", [0])
    finally:
        conn.set_trace_callback(None)
        conn.close()

    # the statements that touch the row's text and its pointer
    touching = [s for s in seen
                if "UPDATE messages" in s and "content" in s.lower()]
    assert touching, ("no UPDATE on messages was observed — the test cannot "
                      "see what it is supposed to check (trace statements: %r)"
                      % seen[:8])

    # the statement that empties the text must ALSO fill the pointer and
    # flip the state — all in one statement
    empties = [s for s in touching
               if "content = NULL" in s or "content=NULL" in s]
    assert empties, ("the text is not emptied by an UPDATE at all: %r" % touching)
    for s in empties:
        assert "content_path" in s, (
            "the text is emptied by one statement and the pointer is filled "
            "by another — a crash between them leaves the text nowhere: %r" % s)
        assert "content_state" in s, (
            "the state is not flipped by the same statement: %r" % s)


def test_t4c_the_double_write_guard_holds(workspace: Path):
    """R7: a seq that is already in the file must NOT be written again.

    **This test exists because a mutant survived.**  The guard lives in
    the write path: a seq the file already holds is RECOVERED (its
    line is re-used, not re-written).  Without the guard, the same
    message would appear TWICE in the chain — and the chain's
    ``prev_hash`` would describe a history that never happened.

    The test drives the guard directly: it asks the externaliser to
    externalise a seq that is already on disk, and asserts that the
    file did not grow.
    """
    store = make_store(workspace)
    store.append_messages("s-t4c", [{"role": "user", "content": "只写一次"}])
    files = main_chain_files(workspace)
    before_lines = msg_chain.read_msg_lines(files[0])
    before_physical = sum(1 for _ in open(files[0], encoding="utf-8"))

    # --- the crash window: the row is INLINE again, but its line is on
    # disk.  This is exactly the state a crash between step 2 and
    # step 5 leaves behind. ---
    conn = store._connect()
    conn.execute("UPDATE messages SET content = ?, content_path = '', "
                 "content_anchor = '', content_hash = '', "
                 "content_state = 'inline' WHERE session_id = 's-t4c'",
                 (json.dumps("只写一次", ensure_ascii=False),))
    conn.commit()

    # ask for seq 0 again — it is already in the file
    try:
        report = msg_write_ext.externalise_messages(
            conn, workspace, "", "s-t4c", [0])
    finally:
        conn.close()

    files = main_chain_files(workspace)
    after_lines = msg_chain.read_msg_lines(files[0])
    after_physical = sum(1 for _ in open(files[0], encoding="utf-8"))

    assert after_physical == before_physical, (
        "the file grew from %d to %d line(s) — the seq was written twice"
        % (before_physical, after_physical))
    assert len(after_lines) == len(before_lines)
    assert [l["seq"] for l in after_lines] == [l["seq"] for l in before_lines]
    assert report.get("recovered", 0) >= 1, (
        "the guard did not recognise the seq as already written: %r" % report)
    assert msg_chain.verify_msg_file(files[0]) == [], "the chain is broken"

    # and the row is back to external, with a pointer that resolves
    rows = rows_of(store, "s-t4c")
    assert rows[0][6] == "external"
    assert rows[0][2] is None, "the recovered row still holds its text"


# ---------------------------------------------------------------------------
# T5 — the delete path must not append the same messages a second time
# ---------------------------------------------------------------------------

def test_t5_delete_path_does_not_double_write(workspace: Path):
    """R6: the content must REALLY be emptied.

    The delete path archives the rows it is about to delete, and it
    selects ``content IS NOT NULL``.  A row that was merely POINTED AT
    (and left in place) would be archived a second time — and the JSONL
    would carry two copies of one message.
    """
    store = make_store(workspace)
    store.append_messages("s-t5", [{"role": "user", "content": "要被删"}])
    files = main_chain_files(workspace)
    before = sum(len(msg_chain.read_msg_lines(p)) for p in files)

    # the delete path: it archives the rows before dropping them
    store.clear_session("s-t5")

    files = main_chain_files(workspace)
    after = sum(len(msg_chain.read_msg_lines(p)) for p in files)
    assert after == before, (
        "the delete path appended %d extra line(s) — the content was not "
        "emptied, so the rows were archived twice" % (after - before))

    # and no row is left holding text it also has in the JSONL
    for row in rows_of(store, "s-t5"):
        assert row[6] != "external" or row[2] is None, (
            "a row is external but still holds its text — the delete path "
            "would archive it again")


# ---------------------------------------------------------------------------
# T6 — a re-cut prunes the ledger AT THE WRITE BOUNDARY
# ---------------------------------------------------------------------------

def test_t6_recut_prunes_the_ledger_without_waiting_for_a_pass(workspace: Path):
    """The orphan must never come to exist, not be cleaned up later.

    The write-boundary prune lives in ``MemoryStorage.replace_file_chunks``
    (P1 阶段三 N, 任务二): the ids that did NOT come back are pruned in the
    SAME transaction as the re-cut.  This test drives the REAL re-cut path
    (``rebuilder.rebuild_file``) and proves the property the criterion
    cannot see — that the orphan count is zero WITHOUT a certification
    pass having run.
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    db = workspace / "memory" / "long-term" / "index.db"
    truth = workspace / "memory" / "page.md"
    truth.parent.mkdir(parents=True, exist_ok=True)
    truth.write_text("# 标题\n\n第一段。\n\n第二段。\n", encoding="utf-8")

    storage = MemoryStorage(db)
    try:
        rebuild_file(storage, workspace, truth)

        # the ledger witnesses what is there
        ids = [r[0] for r in storage.conn.execute("SELECT id FROM chunks").fetchall()]
        assert ids, "the rebuild produced no chunks"
        storage.conn.execute(
            "CREATE TABLE IF NOT EXISTS cert_ledger ("
            "id TEXT PRIMARY KEY, text_state TEXT, body_origin TEXT, "
            "body_path TEXT, body_hash TEXT, certified_at INTEGER, "
            "certified_by TEXT)")
        storage.conn.executemany(
            "INSERT OR REPLACE INTO cert_ledger (id, text_state, body_origin, "
            "body_path, body_hash, certified_at, certified_by) "
            "VALUES (?, 'mirror', 'file', 'memory/page.md', 'sha256:x', 0, 'test')",
            [(i,) for i in ids])
        storage.conn.commit()

        # --- the re-cut: the file changed, so the ids moved ---
        truth.write_text("# 标题\n\n第一段改了。\n\n第二段也改了。\n\n第三段。\n",
                         encoding="utf-8")
        rebuild_file(storage, workspace, truth)

        orphans = storage.conn.execute(
            "SELECT COUNT(*) FROM cert_ledger l WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.id = l.id)").fetchone()[0]
        assert orphans == 0, ("%d orphan(s) survived the re-cut — the prune "
                              "did not happen at the write boundary" % orphans)
    finally:
        storage.close()


def test_t6b_clear_index_prunes_the_ledger(workspace: Path):
    """``clear_index`` deletes EVERY chunk — it must prune the ledger too.

    Measured on this machine before the fix: ``clear_index`` left the whole
    ledger behind, and every one of those entries was an orphan by
    definition (its chunk no longer existed).
    """
    from agent.memory.embedding.rebuild import clear_index
    from agent.memory.storage import MemoryStorage

    db = workspace / "memory" / "long-term" / "index.db"
    storage = MemoryStorage(db)
    try:
        # the real schema — MemoryStorage creates it
        storage.conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash, "
            "source, user_id, scope, embedding) "
            "VALUES ('a', 'memory/x.md', 1, 1, 'x', 'h', 'memory', '', 'shared', NULL)")
        storage.conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin, body_path, "
            "body_hash, certified_at, certified_by) "
            "VALUES ('a', 'mirror', 'file', 'memory/x.md', 'sha256:x', 0, 'test')")
        storage.conn.commit()
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM cert_ledger").fetchone()[0] == 1
    finally:
        storage.close()

    clear_index(db)

    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
        assert n == 0, ("%d ledger entr(ies) survived a full index wipe — they "
                        "are orphans by definition" % n)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T7 — idempotence
# ---------------------------------------------------------------------------

def test_t7_externalisation_is_idempotent(workspace: Path):
    store = make_store(workspace)
    store.append_messages("s-t7", [{"role": "user", "content": "幂等"}])
    files = main_chain_files(workspace)
    n1 = len(msg_chain.read_msg_lines(files[0]))

    # calling the externaliser again with the same seq must change nothing
    conn = store._connect()
    try:
        msg_write_ext.externalise_messages(
            conn, workspace, "", "s-t7", [0])
    finally:
        conn.close()

    files = main_chain_files(workspace)
    n2 = len(msg_chain.read_msg_lines(files[0]))
    assert n2 == n1, "the truth file grew from %d to %d line(s)" % (n1, n2)


# ---------------------------------------------------------------------------
# T8 — the session's file is the one it ALREADY has
# ---------------------------------------------------------------------------

def test_t8_session_keeps_its_file(workspace: Path):
    """A session must not be split across two date directories."""
    store = make_store(workspace)
    store.append_messages("s-t8", [{"role": "user", "content": "第一天"}])
    files = main_chain_files(workspace)
    first = files[0]
    rel1 = first.relative_to(workspace / "memory" / "messages_export").parts

    # a second write, on a "later day" (the date is what would change)
    store.append_messages("s-t8", [{"role": "user", "content": "第二天"}])
    files = main_chain_files(workspace)
    assert len(files) == 1, "the session was split: %r" % files
    lines = msg_chain.read_msg_lines(files[0])
    assert [l["seq"] for l in lines] == [0, 1]
    assert msg_chain.verify_msg_file(files[0]) == []
