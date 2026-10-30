#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-L · the certification ledger's third hole.

**The hole.**  MEASURED by the test engineer on the live ROOT library:
15 rows carried ``text_state='mirror'`` with NO entry in
``cert_ledger``.  The guard's ``chunks_body_keep_bi_ai`` trigger
restored the state from ``body_keep`` and wrote no ledger entry — so
the row said "certified" while the certification's own witness said
nothing about it.

**And it was SELF-PERPETUATING**: the certifier only examined rows
whose state was NOT 'mirror', and the backfill only blessed rows that
were absent from the ledger (seeding them with the current state), so
neither path could leave it.  Deleting a ledger entry and re-running
the certifier changed nothing.

**The principle this round implements:**

    A row's state can be RESTORED by the guard, so the state cannot be
    the evidence that the row was certified.  THE LEDGER IS.

**What is pinned here, each with its reverse case:**

  1. the certifier REACHES a 'mirror' row with no ledger entry — the
     hole's shape — and re-verifies it against the truth layer;
  2. a row that PASSES the byte test gets a ledger entry
     (``certify:reverified``), and one that FAILS is refused;
  3. the ledger is the ONLY witness: deleting an entry makes the row
     uncertified again, and the certifier re-derives it;
  4. the guard's anti-downgrade protection is NOT weakened by the
     ledger — a library with no ledger is still protected (this is the
     case the first version of the fix broke);
  5. the trigger bodies name the ledger, and the ledger table exists
     before they run.

Nothing here touches a live library: every test builds its own.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory import body_guard  # noqa: E402
from agent.memory import certify  # noqa: E402
from agent.memory.chunker import chunk_for_file  # noqa: E402
from agent.memory.body_pointer import four_columns_for_chunk  # noqa: E402
from agent.memory.rebuilder import _chunk_id  # noqa: E402
from agent.memory.storage import MemoryStorage  # noqa: E402


# ---------------------------------------------------------------------------
# the fixture: a library with ONE real chunk behind a real file
# ---------------------------------------------------------------------------

BODY = (
    "# Title\n"
    "\n"
    "First paragraph of the page.\n"
    "\n"
    "Second paragraph of the page.\n"
    "\n"
    "## Section\n"
    "\n"
    "Third paragraph, under a heading.\n"
)


def _library(tmp_path: Path) -> tuple:
    """A workspace with one file, one indexed chunk, and a real pointer."""
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "page.md").write_text(BODY, encoding="utf-8")

    db_dir = ws / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"

    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
        pieces = chunk_for_file(BODY, "memory/page.md")
        assert pieces, "the fixture is broken — the chunker produced nothing"
        for pc in pieces:
            bp, ba, bh, state, origin = four_columns_for_chunk(
                workspace_root=ws,
                relpath="memory/page.md",
                start_line=pc.start_line,
                end_line=pc.end_line,
                part=pc.part,
                chunk_text=pc.text,
                segments=list(pc.segments or []),
                gaps=list(pc.gaps or []),
            )
            conn.execute(
                "INSERT INTO chunks (id, user_id, scope, source, path, "
                "start_line, end_line, text, hash, body_path, body_anchor, "
                "body_hash, text_state, body_origin) "
                "VALUES (?, NULL, 'shared', 'memory', ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?)",
                (_chunk_id("memory/page.md", pc.start_line, pc.end_line,
                          pc.part),
                 "memory/page.md", pc.start_line, pc.end_line, pc.text,
                 "h%d" % pc.start_line, bp, ba, bh, state, origin))
        conn.commit()
        rows = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        storage.close()
    return ws, db, rows


def _state(db: Path, row_id: str) -> tuple:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT text_state, body_origin FROM chunks WHERE id=?",
            (row_id,)).fetchone()
    finally:
        conn.close()
    return row


def _ledger_rows(db: Path) -> list:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT id, text_state, certified_by FROM cert_ledger"
        ).fetchall()
    finally:
        conn.close()


def _witnessed(db: Path) -> int:
    """The rows that claim 'mirror' but have NO ledger entry."""
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM chunks c WHERE c.text_state='mirror' "
            "AND c.id NOT IN (SELECT id FROM cert_ledger)").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. the certifier REACHES the hole's shape
# ---------------------------------------------------------------------------

def test_the_certifier_reaches_a_mirror_row_with_no_ledger_entry(tmp_path):
    """**The hole's exact shape.**

    A row that claims 'mirror' with no ledger entry must be
    RE-VERIFIED — not skipped, not blessed.  Before this round the
    certifier's query was ``WHERE text_state<>'mirror'``, so this row
    was invisible to it, and the backfill could not repair it either.
    """
    ws, db, rows = _library(tmp_path)

    # Manufacture the hole: a row that claims 'mirror' with no ledger
    # entry — exactly what the guard's restore used to leave behind.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET text_state='mirror'")
        conn.commit()
    finally:
        conn.close()
    assert _witnessed(db) == rows, "the fixture did not create the hole"

    report = certify.certify_library(ws, db, name="ws")

    # The row was CHECKED (not skipped).
    assert report["checked"] == rows, report
    # It passed the byte test, so it was promoted (re-verified).
    assert report["promoted"] == rows, report
    assert report["refused"] == 0, report
    # And it is now witnessed.
    assert _witnessed(db) == 0, (
        "the certifier ran and the row still has no ledger entry — the "
        "hole is not closed")


def test_a_reverified_row_is_recorded_as_reverified(tmp_path):
    """The ledger must say WHICH KIND of certification it was.

    A row that already claimed 'mirror' and was then re-verified is
    not the same fact as a row promoted from 'projection'.  Recording
    both as 'promoted' would lose the distinction that IS the hole.
    """
    ws, db, rows = _library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET text_state='mirror'")
        conn.commit()
    finally:
        conn.close()

    certify.certify_library(ws, db, name="ws")
    entries = _ledger_rows(db)
    assert entries, "the ledger is empty"
    assert all(e[2] == "certify:reverified" for e in entries), entries

    # And a row promoted from 'projection' is recorded differently.
    ws2, db2, rows2 = _library(tmp_path / "second")
    certify.certify_library(ws2, db2, name="ws2")
    entries2 = _ledger_rows(db2)
    assert entries2, "the ledger is empty"
    assert all(e[2] == "certify:promoted" for e in entries2), entries2


def test_a_row_that_fails_the_byte_test_is_refused_not_blessed(tmp_path):
    """**The reverse case.**  A row whose text no longer matches its
    file must NOT get a ledger entry — the ledger means "verified",
    and this row was not.

    This is what the backfill did wrong: it seeded the ledger with the
    row's CURRENT state, which "blesses" a row nobody verified.
    """
    ws, db, rows = _library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        # The hole's shape, and a text that does not match the file.
        conn.execute("UPDATE chunks SET text_state='mirror', "
                     "text='a body that is not in the file'")
        conn.commit()
    finally:
        conn.close()

    report = certify.certify_library(ws, db, name="ws")
    assert report["refused"] >= 1, report
    assert report["promoted"] == 0, report
    assert _ledger_rows(db) == [], (
        "a row that failed the byte test got a ledger entry — the "
        "ledger means 'verified', and this row was not")


# ---------------------------------------------------------------------------
# 2. the ledger is the ONLY witness
# ---------------------------------------------------------------------------

def test_deleting_a_ledger_entry_makes_the_row_uncertified_again(tmp_path):
    """**The reverse case the delegation demands.**

    Delete a ledger entry -> the row must stop being certified.  If it
    stayed certified, the ledger would be a decoration and the state
    would still be the evidence.
    """
    ws, db, rows = _library(tmp_path)
    certify.certify_library(ws, db, name="ws")
    assert _ledger_rows(db), "the certification left no ledger entry"

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM cert_ledger")
        conn.commit()
    finally:
        conn.close()

    # The row is now a 'mirror' row with no witness — the hole's shape.
    assert _witnessed(db) == rows, (
        "deleting the ledger entry did not create the unwitnessed state")
    # And the criterion M6c uses must SEE it.
    conn = sqlite3.connect(str(db))
    try:
        a = certify.judge_m6a_coverage(conn)
        b = certify.judge_m6b_downgrade(conn)
    finally:
        conn.close()
    # M6a asks about rows that are not certified; M6b needs a witness.
    # The row claims 'mirror', so neither catches it — which is exactly
    # why M6c exists.  What matters here is that the certifier, run
    # again, REPAIRS it.
    report = certify.certify_library(ws, db, name="ws")
    assert report["checked"] == rows, report
    assert _witnessed(db) == 0, (
        "the certifier did not repair the unwitnessed row — the hole is "
        "not closed")


def test_the_certification_is_idempotent_with_the_new_selection(tmp_path):
    """The second pass must certify NOTHING new — the ledger is the
    work list, and a row in it is done."""
    ws, db, rows = _library(tmp_path)
    first = certify.certify_library(ws, db, name="ws")
    assert first["promoted"] == rows, first
    second = certify.certify_library(ws, db, name="ws")
    assert second["promoted"] == 0, second
    assert second["checked"] == 0, second
    assert second["remaining"] == 0, second


# ---------------------------------------------------------------------------
# 3. the guard's protection is NOT weakened
# ---------------------------------------------------------------------------

def test_the_guard_still_prevents_a_downgrade_without_a_ledger(tmp_path):
    """**The case the first version of the fix broke.**

    A library that has never been certified has no ledger entry for
    its rows.  The guard's anti-downgrade protection must NOT depend
    on the ledger — otherwise the guard would be WEAKER than before
    for exactly the rows that need it most.
    """
    ws, db, rows = _library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET text_state='mirror'")
        conn.commit()
        # No ledger entry at all.
        conn.execute("DELETE FROM cert_ledger")
        conn.commit()
        conn.execute(
            "UPDATE chunks SET text_state='projection', "
            "body_origin='unresolved' WHERE id=(SELECT id FROM chunks "
            "LIMIT 1)")
        conn.commit()
    finally:
        conn.close()

    states = [s for s, _o in
              (sqlite3.connect(str(db)).execute(
                  "SELECT text_state, body_origin FROM chunks"))]
    assert states[0] == "mirror", (
        "the guard let the state be downgraded on a library with no "
        "ledger entry — the fix must not make the guard weaker")


def test_the_trigger_bodies_name_the_ledger(tmp_path):
    """The state's restore source must be the ledger, with the keep
    table as the fallback — so the reader can see it in the SQL."""
    sqls = "\n".join(body_guard.trigger_sqls())
    assert "cert_ledger" in sqls, (
        "no trigger reads the certification ledger — the state's "
        "restore source is still only the keep table")


def test_the_ledger_table_exists_before_the_triggers_run(tmp_path):
    """A trigger whose body names a table that does not exist fails at
    RUN time with 'no such table' — so the table must be created
    first, in the same transaction as the triggers."""
    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True)
    storage = MemoryStorage(db_path=str(db))
    conn = storage.conn
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "cert_ledger" in tables, (
            "the guard installed without the ledger table — every write "
            "would fail with 'no such table: main.cert_ledger'")
        # And a write works.
        conn.execute(
            "INSERT INTO chunks (id, scope, source, path, start_line, "
            "end_line, text, hash) VALUES ('probe','shared','memory',"
            "'x.md',1,1,'t','h')")
        conn.commit()
    finally:
        storage.close()


def test_a_brand_new_library_reports_that_nothing_was_certified(tmp_path):
    """The flag must say "the ledger holds no record", not "the ledger
    table exists".

    The guard now creates the (empty) ledger table when it installs
    itself — so "the table exists" no longer means "something was
    certified", and reporting it that way would be a lie.
    """
    ws, db, rows = _library(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        a = certify.judge_m6a_coverage(conn)
    finally:
        conn.close()
    assert a["ledger_present"] is False, a
    assert a["ledger_table"] is True, a
    assert a["ledger_rows"] == 0, a
    assert a["verdict"] == certify.VERDICT_FAIL, a
    assert a["not_certified"] >= 1, a
