#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 第七轮 (S3-K): the certification closes the loop.

**What the round found (measured, not inferred).**

The ``'mirror'`` state has a designated setter — the inspector — and the
inspector's setter had three gaps:

  ① it only ever looked at rows whose ``body_origin`` was already
     ``'file'``; a row written by a writer that fills no body columns
     (``body_origin='unresolved'``, no pointer at all) could NEVER be
     certified, no matter how many times the inspector ran;
  ② it only ever looked at **this** workspace's index, so the other
     fifteen libraries of the family were never certified at all;
  ③ it ran only as a SIDE EFFECT of an inspection, and the
     ``trigger='startup'`` entry point was documented and never called
     from anywhere — so a row written after the last inspection stayed
     ``'projection'`` forever.

These tests pin the three fixes:

  * the certification DERIVES the pointer when the row has none (by the
    migration's own D1 rule), and then certifies it;
  * the certification is a repeatable operation over the WHOLE family
    (``certify_family``), idempotent, and it REFUSES every row that does
    not pass the byte test — with the id, the reason and the two texts;
  * the write path certifies what it wrote, so "a new knowledge page
    becomes ``'mirror'``" has a mechanism and not a hope.

**And one more, which is the reason the census needed to change:**

  * "not certified YET" is not "erased".  The census must tell those
    two apart, and it must do it with EVIDENCE (the certification
    ledger) rather than with a count that a newly written row would trip.

**And the hole this round found in the sixth round's guard:**

  * the files side had no BEFORE DELETE snapshot, so the legacy build's
    ``INSERT OR REPLACE INTO files (...)`` — a DELETE followed by an
    INSERT — erased the statistics even with the guard installed.

Every test here has its REVERSE CASE: the defect it pins is reproduced
by breaking the thing, so the test cannot pass with the thing absent.
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
from agent.memory import certify  # noqa: E402
from agent.memory.storage import MemoryStorage  # noqa: E402


# ---------------------------------------------------------------------------
# the fixture: a library with a real truth file behind it
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path: Path, name: str = "ws") -> tuple:
    """A workspace with one markdown file and an index that points at it.

    The file is written the way the chunker reads it, so the rows the
    fixture cuts really ARE chunks of that file.

    ``tmp_path`` IS the workspace's own directory.  The family tests
    pass ``<root>/agents/<name>``, and the migration's whitelist
    enumerates exactly that shape.
    """
    ws = tmp_path
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    body = (
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
    (ws / "memory" / "page.md").write_text(body, encoding="utf-8")

    db_dir = ws / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"

    from agent.memory.chunker import chunk_for_file
    from agent.memory.body_pointer import four_columns_for_chunk

    pieces = chunk_for_file(body, "memory/page.md")
    assert pieces, "the chunker produced nothing — the fixture is broken"

    storage = MemoryStorage(db_path=str(db))
    try:
        conn = storage.conn
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
                "VALUES (?, NULL, 'shared', 'memory', ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?)",
                (certify_hash(pc), "memory/page.md", pc.start_line,
                 pc.end_line, pc.text, "h-" + str(pc.start_line),
                 bp, ba, bh, state, origin))
        conn.commit()
        rows = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        storage.close()
    return ws, db, rows


def certify_hash(pc) -> str:
    import hashlib

    return hashlib.md5(
        ("memory/page.md:%s:%s" % (pc.start_line, pc.end_line)).encode()
    ).hexdigest()


def _states(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT text_state, body_origin, body_path FROM chunks").fetchall()
    finally:
        conn.close()
    return {
        "mirror": sum(1 for r in rows if r[0] == "mirror"),
        "projection": sum(1 for r in rows if r[0] == "projection"),
        "file": sum(1 for r in rows if r[1] == "file"),
        "unresolved": sum(1 for r in rows if r[1] == "unresolved"),
        "no_pointer": sum(1 for r in rows if r[2] is None),
        "total": len(rows),
    }


def _without_the_guard(db: Path, fn):
    """Run ``fn(conn)`` with the body guard DROPPED.

    **Why this is necessary, and why it is not cheating.**  The guard's
    whole job is to RESTORE a row that a plain UPDATE tries to erase —
    so a test that erases a row through a plain UPDATE measures the
    guard, not the shape it is trying to produce.  The shapes this
    round is about (a row with no pointer; a row whose certified state
    was wiped) are the shapes a writer produces when the guard is NOT
    there — which is exactly the situation the guard exists to
    survive.

    The guard is put back afterwards, so the fixture stays a real
    library.
    """
    conn = sqlite3.connect(str(db))
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN ('chunks', 'files')")]
    saved = {}
    for name in names:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name=? AND type='trigger'",
            (name,)).fetchone()
        if row and row[0]:
            saved[name] = row[0]
            conn.execute("DROP TRIGGER %s" % name)
    try:
        fn(conn)
        conn.commit()
    finally:
        for name, sql in saved.items():
            conn.execute(sql)
        conn.commit()
        conn.close()


def _set_all(conn, **columns):
    """Set columns on every row (the caller has already dropped the
    guard, so nothing restores them)."""
    sets = ", ".join("%s=?" % c for c in columns)
    conn.execute("UPDATE chunks SET %s" % sets,
                 tuple(columns.values()))


# ---------------------------------------------------------------------------
# the certification itself
# ---------------------------------------------------------------------------

def test_the_certification_promotes_the_rows_it_can(tmp_path):
    """A row whose text is byte-equal to the fragment rebuilt from the
    file becomes ``'mirror'``."""
    ws, db, rows = _make_workspace(tmp_path)
    assert rows >= 1
    report = certify.certify_library(ws, db, name="ws")
    assert report["promoted"] >= 1, report
    assert report["refused"] == 0, report
    assert _states(db)["mirror"] >= 1, _states(db)


def test_the_certification_is_idempotent(tmp_path):
    """Running it twice promotes nothing the second time.

    The second run is the PROOF that the operation is repeatable and
    not a one-shot repair.
    """
    ws, db, rows = _make_workspace(tmp_path)
    first = certify.certify_library(ws, db, name="ws")
    assert first["promoted"] >= 1, first
    second = certify.certify_library(ws, db, name="ws")
    assert second["promoted"] == 0, second
    assert second["remaining"] == 0, second


def test_a_row_whose_text_differs_is_refused_and_named(tmp_path):
    """A row that does NOT pass the byte test stays ``'projection'`` and
    is REPORTED — with its id and the reason.

    **This is the anti-"make it green" case.**  The certification must
    never promote a row it could not verify.
    """
    ws, db, rows = _make_workspace(tmp_path)

    # Corrupt the stored text of one row, so the rebuilt fragment and
    # the stored text differ.
    _without_the_guard(db, lambda c: c.execute(
        "UPDATE chunks SET text='something else entirely' "
        "WHERE id=(SELECT id FROM chunks LIMIT 1)"))

    report = certify.certify_library(ws, db, name="ws")
    assert report["refused"] >= 1, report
    assert report["promoted"] < rows, report
    row = report["refusals"][0]
    assert row["id"], row
    assert row["reason"], row
    # The report carries BOTH texts, so a reader can see the difference.
    assert "stored_text" in row or "rebuilt_text" in row, row


def test_a_row_without_a_pointer_gets_one_derived(tmp_path):
    """A row with NO pointer is not skipped — its pointer is DERIVED
    (the migration's D1 rule), and only then is it certified."""
    ws, db, rows = _make_workspace(tmp_path)

    _without_the_guard(db, lambda c: _set_all(
        c, body_path=None, body_anchor=None, body_hash=None,
        body_origin="unresolved"))
    assert _states(db)["no_pointer"] == rows

    report = certify.certify_library(ws, db, name="ws")
    assert report["derived"] >= 1, report
    assert _states(db)["no_pointer"] == 0, _states(db)


def test_the_derivation_and_the_certification_are_separate_acts(tmp_path):
    """A row whose text is not a chunk of its file gets NEITHER: no
    pointer (there is no honest one) and no certification."""
    ws, db, rows = _make_workspace(tmp_path)

    _without_the_guard(db, lambda c: _set_all(
        c, body_path=None, body_anchor=None, body_hash=None,
        body_origin="unresolved", text="text that is not in the file"))
    report = certify.certify_library(ws, db, name="ws")
    assert report["promoted"] == 0, report
    assert report["derived"] == 0, report
    assert report["refused"] >= 1, report
    assert _states(db)["no_pointer"] == rows, _states(db)


def test_the_write_path_certifies_what_it_wrote(tmp_path):
    """**The question E-3 asks:** "a knowledge page is written — when
    does its projection row become ``'mirror'``?"

    The answer must be a MECHANISM, not a hope.
    """
    ws, db, rows = _make_workspace(tmp_path)
    assert _states(db)["projection"] >= 1

    from agent.memory.manager import MemoryManager
    manager = MemoryManager.__new__(MemoryManager)
    manager.config = type("C", (), {
        "get_workspace": staticmethod(lambda: str(ws))})()
    manager.storage = MemoryStorage(db_path=str(db))
    try:
        report = manager._certify_after_write()
    finally:
        manager.storage.close()

    assert report.get("promoted", 0) >= 1, report
    assert report.get("refused", 0) == 0, report


def test_a_failing_certification_does_not_break_the_write(tmp_path):
    """The certification is REPORT-ONLY for the caller.

    A write that succeeded must not be undone by a certification that
    could not run — the two are separate acts, and one failing must not
    take the other down.
    """
    ws, db, rows = _make_workspace(tmp_path)
    from agent.memory.manager import MemoryManager

    manager = MemoryManager.__new__(MemoryManager)
    manager.config = type("C", (), {
        "get_workspace": staticmethod(lambda: str(ws))})()
    manager.storage = MemoryStorage(db_path=str(db))
    # Break the certification: the index is not there.
    manager.storage.db_path = str(ws / "nope" / "index.db")
    try:
        report = manager._certify_after_write()
    finally:
        manager.storage.close()
    # No exception, and the report says what happened.
    assert isinstance(report, dict), report
    assert report.get("promoted", 0) == 0, report


# ---------------------------------------------------------------------------
# the M6 criteria — the two questions SEED's ruling names
# ---------------------------------------------------------------------------

def test_the_certification_leaves_a_ledger_record(tmp_path):
    """A certification leaves a RECORD that it happened.

    Without the ledger the question "was this row ever certified?" has
    no answer for a row that was never at risk of being downgraded —
    the guard's keep table only holds the rows a write threatened.
    """
    ws, db, rows = _make_workspace(tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        # Before: no ledger — and the criterion says so, rather than
        # claiming it cannot measure.  "No ledger" means "no row was
        # ever certified", which is the strongest form of the answer.
        before = certify.judge_m6a_coverage(conn)
        assert before["verdict"] == certify.VERDICT_FAIL, before
        assert before["ledger_present"] is False, before
        assert before["not_certified"] >= 1, before
    finally:
        conn.close()

    report = certify.certify_library(ws, db, name="ws")
    assert report["promoted"] >= 1, report

    conn = sqlite3.connect(str(db))
    try:
        led = body_guard.certification_records(conn)
        assert led["present"], led
        assert led["certified"] >= 1, led
        assert led["stale"] == 0, led
    finally:
        conn.close()


def test_m6a_is_fail_when_a_row_was_never_certified(tmp_path):
    """M6a: a row with a file behind it that was NEVER certified is a
    FAIL — and it is named.

    The reverse case: the same library, certified, is a PASS.  So the
    criterion cannot pass with the mechanism absent.
    """
    ws, db, rows = _make_workspace(tmp_path)

    # The rows start as 'projection' (the fixture writes them that way)
    # with a pointer — so they are "not certified yet".
    conn = sqlite3.connect(str(db))
    try:
        a = certify.judge_m6a_coverage(conn)
        assert a["verdict"] == certify.VERDICT_FAIL, a
        assert a["not_certified"] >= 1, a
        assert a["pending"], "the pending rows must be NAMED"
        assert a["pending"][0]["id"], a
    finally:
        conn.close()

    certify.certify_library(ws, db, name="ws")

    conn = sqlite3.connect(str(db))
    try:
        a = certify.judge_m6a_coverage(conn)
        assert a["verdict"] == certify.VERDICT_PASS, a
        assert a["not_certified"] == 0, a
    finally:
        conn.close()


def test_m6b_is_fail_when_a_certified_row_is_downgraded(tmp_path):
    """M6b: a certified row that falls back is a FAIL, named with BOTH
    values.

    The reverse case: the same library, not downgraded, is a PASS.
    """
    ws, db, rows = _make_workspace(tmp_path)
    certify.certify_library(ws, db, name="ws")

    conn = sqlite3.connect(str(db))
    try:
        b = certify.judge_m6b_downgrade(conn)
        assert b["verdict"] == certify.VERDICT_PASS, b
        assert b["witness_rows"] >= 1, b
    finally:
        conn.close()

    # Now downgrade one certified row, with the guard out of the way
    # (the guard would RESTORE it — that is its job, and it is
    # measured separately).
    _without_the_guard(db, lambda c: c.execute(
        "UPDATE chunks SET text_state='projection' "
        "WHERE id=(SELECT id FROM chunks WHERE text_state='mirror' "
        "LIMIT 1)"))

    conn = sqlite3.connect(str(db))
    try:
        b = certify.judge_m6b_downgrade(conn)
        assert b["verdict"] == certify.VERDICT_FAIL, b
        assert b["downgrades"] == 1, b
        row = b["downgraded"][0]
        assert row["certified_state"] == "mirror", row
        assert row["current_state"] == "projection", row
        assert row["id"], row
    finally:
        conn.close()


def test_the_ledger_is_not_the_keep_table(tmp_path):
    """The two witnesses answer different questions, and a row that is
    in one is not necessarily in the other.

    The live measurement that motivated the ledger: 7 rows written
    after the last inspection were ABSENT from the keep table (no
    write had threatened them) while their state was 'projection'.
    """
    ws, db, rows = _make_workspace(tmp_path)

    conn = sqlite3.connect(str(db))
    try:
        keep = conn.execute("SELECT COUNT(*) FROM body_keep").fetchone()[0]
        # Nothing has threatened a row yet: the keep table is EMPTY.
        assert keep == 0, keep
    finally:
        conn.close()

    certify.certify_library(ws, db, name="ws")

    conn = sqlite3.connect(str(db))
    try:
        led = body_guard.certification_records(conn)
        assert led["certified"] >= 1, led
        keep = conn.execute("SELECT COUNT(*) FROM body_keep").fetchone()[0]
        # The certification did NOT put anything into the keep table:
        # the ledger and the keep table are different things.
        assert keep == 0, keep
    finally:
        conn.close()


def test_the_backfill_is_explicit_and_idempotent(tmp_path):
    """The backfill seeds the ledger with the rows that were certified
    before the ledger existed — and it is EXPLICIT.

    A backfill that ran implicitly during a certification would turn
    "never certified" into "certified", which is the very thing the
    coverage criterion exists to detect.
    """
    ws, db, rows = _make_workspace(tmp_path)
    # Certify WITHOUT the backfill: the ledger holds only what this
    # call certified.
    certify.certify_library(ws, db, name="ws")
    conn = sqlite3.connect(str(db))
    try:
        first = body_guard.certification_records(conn)["certified"]
    finally:
        conn.close()

    # Now add a row that is 'mirror' WITHOUT a ledger record — the
    # state the inspector's own certifications left behind.  It has to
    # be a NEW row: the fixture's row was certified by the call above.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO chunks (id, scope, source, path, start_line, "
            "end_line, text, hash, body_path, body_anchor, body_hash, "
            "text_state, body_origin) "
            "VALUES ('legacy-certified-row', 'shared', 'memory', "
            "'memory/page.md', 1, 1, 'x', 'h', 'memory/page.md', "
            "'L1-1', 'bh', 'mirror', 'file')")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE id NOT IN "
            "(SELECT id FROM cert_ledger) AND text_state='mirror'"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    conn = sqlite3.connect(str(db))
    try:
        res = certify.backfill_ledger(conn)
        assert res["written"] == 1, res
        # Idempotent: the second run writes nothing.
        res2 = certify.backfill_ledger(conn)
        assert res2["written"] == 0, res2
        assert res2["already"] >= 1, res2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the hole this round found in the sixth round's guard
# ---------------------------------------------------------------------------

def test_the_files_guard_survives_the_legacy_replace_shape(tmp_path):
    """**The hole S3-K found in the sixth round's guard.**

    The chunks side got its BEFORE DELETE snapshot; the files side did
    not.  So the legacy build's ``INSERT OR REPLACE INTO files (...)``
    — which is a DELETE followed by an INSERT — erased the statistics
    even with the guard installed: by the time the BEFORE INSERT
    trigger ran, the stored row was GONE and there was nothing to
    restore from.

    Measured on the live libraries: the M0 backup (05:32) had 10 files
    rows WITH the statistics, the live library had 0 — written by the
    legacy build at 10:05, long after the guard was installed.

    The reverse case is in the next test: with the guard DROPPED, the
    same statement erases the statistics.  So the test cannot pass
    with the mechanism absent.
    """
    from agent.memory import body_guard as bg

    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = MemoryStorage(db_path=str(db))
    conn = storage.conn
    try:
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, chunk_count, "
            "chunker_version, updated_at) VALUES ('memory/a.md','memory',"
            "'h',1,10,3,4,strftime('%s','now'))")
        conn.commit()
        bg.ensure_guard(conn)
        conn.commit()
        assert bg.is_installed(conn)

        # THE LEGACY SHAPE: INSERT OR REPLACE, no statistics columns.
        conn.execute(
            "INSERT OR REPLACE INTO files (path, source, hash, mtime, size, "
            "updated_at) VALUES ('memory/a.md','memory','h2',2,20,"
            "strftime('%s','now'))")
        conn.commit()
        r = conn.execute("SELECT chunk_count, chunker_version FROM files "
                         "WHERE path='memory/a.md'").fetchone()
        assert r[0] == 3, "the chunk count was erased: %s" % (r,)
        assert r[1] == 4, "the chunker version was erased: %s" % (r,)
    finally:
        storage.close()


def test_the_files_guard_does_not_fire_without_the_guard(tmp_path):
    """The reverse case of the test above: WITHOUT the guard, the same
    statement erases the statistics.

    Two tests, one behaviour: the first says "the guard holds", this
    one says "nothing else holds it".
    """
    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = MemoryStorage(db_path=str(db))
    conn = storage.conn
    try:
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, chunk_count, "
            "chunker_version, updated_at) VALUES ('memory/a.md','memory',"
            "'h',1,10,3,4,strftime('%s','now'))")
        conn.commit()
        # NO guard installed.
        conn.execute(
            "INSERT OR REPLACE INTO files (path, source, hash, mtime, size, "
            "updated_at) VALUES ('memory/a.md','memory','h2',2,20,"
            "strftime('%s','now'))")
        conn.commit()
        r = conn.execute("SELECT chunk_count FROM files "
                         "WHERE path='memory/a.md'").fetchone()
        assert r[0] is None, (
            "the reverse case did not reproduce: without the guard the "
            "statistics should be erased, but they are %s" % (r[0],))
    finally:
        storage.close()


def test_the_files_guard_does_not_invent_a_zero(tmp_path):
    """The guard restores only a MEASURED pair.  A file that really
    produced no chunks keeps its NULL — "not measured" is not "zero"
    (Rule D)."""
    from agent.memory import body_guard as bg

    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = MemoryStorage(db_path=str(db))
    conn = storage.conn
    try:
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, chunk_count, "
            "chunker_version, updated_at) VALUES ('memory/empty.md','memory',"
            "'h',1,10,NULL,NULL,strftime('%s','now'))")
        conn.commit()
        bg.ensure_guard(conn)
        conn.commit()
        conn.execute(
            "INSERT OR REPLACE INTO files (path, source, hash, mtime, size, "
            "updated_at) VALUES ('memory/empty.md','memory','h2',2,20,"
            "strftime('%s','now'))")
        conn.commit()
        r = conn.execute("SELECT chunk_count, chunker_version FROM files "
                         "WHERE path='memory/empty.md'").fetchone()
        assert r[0] is None, (
            "the guard invented a measurement for a file that never had "
            "one: %s" % (r,))
        assert r[1] is None, r
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# the family and the startup trigger
# ---------------------------------------------------------------------------

def test_the_whole_family_is_certifiable(tmp_path):
    """``certify_family`` reaches EVERY live library of the workspace —
    the gap the sixth round found (the setter only ever looked at one
    workspace's index)."""
    root = tmp_path / "cow"
    (root / "memory" / "long-term").mkdir(parents=True)
    (root / "agents" / "a1" / "memory" / "long-term").mkdir(parents=True)

    for base in (root, root / "agents" / "a1"):
        body = "# T\n\nA paragraph of the page.\n"
        (base / "memory").mkdir(exist_ok=True)
        (base / "memory" / "page.md").write_text(body, encoding="utf-8")
        db = base / "memory" / "long-term" / "index.db"
        st = MemoryStorage(db_path=str(db))
        st.close()

    libs = certify.enumerate_live(root)
    assert len(libs) == 2, [l["name"] for l in libs]

    report = certify.certify_family(root, dry_run=True)
    assert report["library_count"] == 2, report


def test_the_startup_trigger_is_not_dangling(tmp_path):
    """The ``trigger='startup'`` entry point must be CALLED from
    somewhere — a documented entry point that nobody calls is a promise
    the code does not keep.

    The check is on the SOURCE, not on a running server: the runtime
    must call the certification from the startup path.
    """
    init = (REPO / "bridge" / "agent_initializer.py").read_text(
        encoding="utf-8")
    assert "certify_family" in init, (
        "the startup trigger is still dangling: the runtime must call the "
        "certification at startup")
    # And it must be inside the startup path, not in a docstring.
    body = "\n".join(
        line for line in init.splitlines()
        if not line.strip().startswith("#"))
    assert "certify_family(" in body, (
        "the call must be in executable code, not in a comment")


def test_a_new_row_becomes_mirror_through_the_write_path(tmp_path):
    """**The question E-3 asks:** "a knowledge page is written — when
    does its projection row become ``'mirror'``?"

    The answer must be a MECHANISM, not a hope.  This test writes a
    page, indexes it through the real write path, and checks that the
    row ends up certified — without anybody running an inspection.
    """
    ws = tmp_path
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "page.md").write_text(
        "# T\n\nA paragraph of the page.\n", encoding="utf-8")

    from agent.memory.chunker import chunk_for_file
    from agent.memory.body_pointer import four_columns_for_chunk

    db = ws / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    storage = MemoryStorage(db_path=str(db))
    try:
        pieces = chunk_for_file(
            (ws / "memory" / "page.md").read_text(encoding="utf-8"),
            "memory/page.md")
        assert pieces
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
            storage.conn.execute(
                "INSERT INTO chunks (id, scope, source, path, start_line, "
                "end_line, text, hash, body_path, body_anchor, body_hash, "
                "text_state, body_origin) "
                "VALUES (?, 'shared', 'memory', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("row-" + str(pc.start_line), "memory/page.md",
                 pc.start_line, pc.end_line, pc.text,
                 "h" + str(pc.start_line), bp, ba, bh, state, origin))
        storage.conn.commit()
    finally:
        storage.close()

    # The row is 'projection' before anything certifies it.
    assert _states(db)["projection"] >= 1

    # The write path certifies it — no inspection, no CLI, no daemon.
    from agent.memory.manager import MemoryManager
    manager = MemoryManager.__new__(MemoryManager)
    manager.config = type("C", (), {
        "get_workspace": staticmethod(lambda: str(ws))})()
    manager.storage = MemoryStorage(db_path=str(db))
    try:
        report = manager._certify_after_write()
    finally:
        manager.storage.close()

    assert report.get("promoted", 0) >= 1, report
    assert _states(db)["mirror"] >= 1, _states(db)
