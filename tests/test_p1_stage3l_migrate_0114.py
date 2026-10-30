#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-L · the 0.114 migration tool's criteria.

**Why this file exists.**  The tool was unusable on 0.114 — not
because of anything about 0.114, but because of a number baked into
the source: ``ENUM.GATE`` demanded exactly 16 live libraries, and
0.114 has exactly 1 (the 15 ``agents/`` directories are empty
shells).  The tool returned exit 2 before doing anything, so not one
row could be migrated.

These tests pin the seven hard constraints of this round, each with
its REVERSE CASE — the shape that must FAIL, so the test cannot pass
with the mechanism absent:

  B-1  the library count is NOT hardcoded (1 library must not fail)
  B-1' zero libraries IS a real gate (a run that migrates nothing
       must not report success)
  enum ``enumerate_live`` accepts a ``str`` (it used to raise)
  A-3  the rollback restores EXACTLY, through the backup API
  A-4  the baseline is a CONTENT DIGEST, not a row count
  A-6  the five "am I running the right code?" checks print their
       ACTUAL VALUES
  idem the second pass fills ZERO rows and leaves the digest alone

Every test here runs against a THROWAWAY copy.  Nothing here touches
a live library.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory import migrate  # noqa: E402
from agent.memory.body_pointer import four_columns_for_chunk  # noqa: E402
from agent.memory.chunker import chunk_for_file  # noqa: E402
from agent.memory.rebuilder import _chunk_id  # noqa: E402
from agent.memory.storage import MemoryStorage  # noqa: E402


# ---------------------------------------------------------------------------
# the fixture: a workspace with EXACTLY ONE live library (the 0.114 shape)
# ---------------------------------------------------------------------------

def _truth_files() -> dict:
    return {
        "AGENT.md": "# AGENT\n\n" + ("the agent role file. " * 40) + "\n",
        "MEMORY.md": "# MEMORY\n\n" + ("memory entry. " * 40) + "\n",
        "RULE.md": "# RULE\n\n" + ("the rule. " * 40) + "\n",
        "USER.md": "# USER\n\n" + ("the user. " * 40) + "\n",
        "knowledge/entities/dify.md": "# dify\n\n" + ("dify. " * 60) + "\n",
    }


def _one_library_workspace(tmp_path: Path) -> tuple:
    """A workspace with ONE live library and 15 empty agent shells.

    **The 0.114 shape, reproduced exactly** (前置准备 §4.2): the live
    library is ROOT, and the 15 ``agents/<id>/`` directories exist but
    have no ``memory/long-term/`` — they are empty shells, not
    libraries with an empty index.
    """
    ws = tmp_path / "cow-workspace"
    (ws / "memory" / "long-term").mkdir(parents=True)
    for i in range(15):
        shell = ws / "agents" / ("agent-shell-%02d" % i)
        shell.mkdir(parents=True)
        (shell / "AGENT.md").write_text("# shell\n", encoding="utf-8")

    truths = _truth_files()
    for rel, body in truths.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    db = ws / "memory" / "long-term" / "index.db"
    storage = MemoryStorage(db_path=str(db))
    conn = storage.conn
    try:
        for rel, body in truths.items():
            for pc in chunk_for_file(body, rel):
                cid = _chunk_id(rel, pc.start_line, pc.end_line, pc.part)
                conn.execute(
                    "INSERT INTO chunks (id, user_id, scope, source, path, "
                    "start_line, end_line, text, embedding, hash, metadata, "
                    "element_released, file_sealed, sealed_owner, body_path, "
                    "body_anchor, body_hash, text_state) "
                    "VALUES (?, NULL, 'shared', 'memory', ?, ?, ?, ?, NULL, ?, "
                    "NULL, 0, 0, NULL, NULL, NULL, NULL, 'projection')",
                    (cid, rel, pc.start_line, pc.end_line, pc.text,
                     "sha256:" + ("%064x" % (abs(hash(cid)) % (1 << 256)))))
            conn.execute(
                "INSERT OR REPLACE INTO files (path, source, hash, mtime, "
                "size, updated_at, chunk_count, chunker_version) VALUES "
                "(?, 'memory', ?, 1, 1, strftime('%s','now'), NULL, NULL)",
                (rel, "sha256:" + ("%064x" % (abs(hash(rel)) % (1 << 256)))))
        conn.commit()
    finally:
        storage.close()
    return ws, db


def _run_tool(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent.memory.migrate",
         "--workspace", str(workspace)] + list(args),
        capture_output=True, text=True, cwd=str(REPO))


# ---------------------------------------------------------------------------
# B-1: the library count is not hardcoded
# ---------------------------------------------------------------------------

def test_b1_one_library_does_not_fail_the_gate(tmp_path):
    """B-1: a workspace with ONE live library must NOT return 2.

    **The reverse case is the bug itself**: before this round the gate
    demanded 16 and the 1-library machine could not run the tool at
    all.  The gate must accept the count it actually finds.
    """
    ws, db = _one_library_workspace(tmp_path)

    live = migrate.enumerate_live(ws)
    assert len(live) == 1, [l["name"] for l in live]

    proc = _run_tool(ws, "--step", "M7", "--dry-run",
                     "--backup-dir", str(tmp_path / "bk"))
    assert proc.returncode != 2, (
        "the tool returned 2 on a 1-library workspace — that is the "
        "hardcoded-16 bug, and it blocks every migration:\n%s"
        % proc.stdout)
    assert "live libraries (WHITELIST enumeration): 1" in proc.stdout
    assert "ENUM.GATE" in proc.stdout
    assert "[FAIL] ENUM.GATE" not in proc.stdout, proc.stdout


def test_b1_zero_libraries_is_a_real_gate(tmp_path):
    """**The one count that IS a gate: zero.**

    "Enumerated 0 libraries" means the tool found nothing to migrate
    — the path is wrong, or the truth layer lives somewhere else.  A
    run that migrates nothing must NOT report success, because the
    operator reads the green and moves on.
    """
    ws = tmp_path / "empty"
    ws.mkdir()
    proc = _run_tool(ws, "--step", "M7", "--dry-run")
    assert proc.returncode == 2, (
        "a run that found no library must fail, not report success:\n%s"
        % proc.stdout)
    assert "[FAIL] ENUM.GATE" in proc.stdout, proc.stdout
    assert "enumerated 0 libraries" in proc.stdout, proc.stdout


def test_b1_the_expected_count_is_a_parameter_not_a_constant(tmp_path):
    """``--expect-libraries`` compares; without it the count is REPORTED.

    Both numbers are printed — a gate that prints only "FAIL" cannot be
    acted on by the operator standing in front of the machine.
    """
    ws, db = _one_library_workspace(tmp_path)

    # The expectation that MATCHES -> PASS, with both numbers.
    proc = _run_tool(ws, "--step", "M7", "--dry-run",
                     "--expect-libraries", "1",
                     "--backup-dir", str(tmp_path / "bk1"))
    assert "[FAIL] ENUM.GATE" not in proc.stdout, proc.stdout
    assert "expected 1, enumerated 1" in proc.stdout, proc.stdout

    # The expectation that does NOT match -> FAIL, with both numbers.
    proc2 = _run_tool(ws, "--step", "M7", "--dry-run",
                      "--expect-libraries", "16",
                      "--backup-dir", str(tmp_path / "bk2"))
    assert proc2.returncode == 2, proc2.stdout
    assert "[FAIL] ENUM.GATE" in proc2.stdout, proc2.stdout
    assert "expected 16 libraries, enumerated 1" in proc2.stdout, proc2.stdout


# ---------------------------------------------------------------------------
# the enumeration takes a str
# ---------------------------------------------------------------------------

def test_the_enumeration_accepts_a_string(tmp_path):
    """MEASURED on 0.114 (前置准备 §7.2): ``enumerate_live('/home/...')``
    raised ``TypeError: unsupported operand type(s) for /: 'str' and
    'str'`` — the body does ``workspace / "memory"``.

    Every caller on 0.114 passes the workspace as it came off the
    command line, so the tool was unusable there for a reason that has
    nothing to do with the machine.
    """
    ws, db = _one_library_workspace(tmp_path)
    as_str = migrate.enumerate_live(str(ws))
    as_path = migrate.enumerate_live(ws)
    assert len(as_str) == len(as_path) == 1, (as_str, as_path)
    assert as_str[0]["db"] == as_path[0]["db"]


# ---------------------------------------------------------------------------
# A-3: the rollback restores EXACTLY
# ---------------------------------------------------------------------------

def test_the_rollback_restores_the_pre_migration_state_exactly(tmp_path):
    """A-3: the rollback is an undo button, and it must be exact.

    **The reverse case is built into the test**: the library is
    damaged the way a failed migration would damage it, and a row that
    was NEVER in the backup is added.  A restore that revived that row
    (which is what a ``cp`` overwrite does — the stale WAL applies
    itself to the fresh main database) fails here.
    """
    ws, db = _one_library_workspace(tmp_path)
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()

    # --- the M0 backup ------------------------------------------------
    proc = _run_tool(ws, "--step", "M0", "--backup-dir", str(backup_dir))
    assert proc.returncode == 0, proc.stdout
    assert "[PASS] M0.ROOT" in proc.stdout, proc.stdout
    # A-2: the recovery drill ran inside M0.
    assert "R-1 real MemoryStorage open : True" in proc.stdout, proc.stdout
    assert "integrity_check=ok" in proc.stdout, proc.stdout

    # --- damage it the way a failed migration would -------------------
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
                     "body_hash=NULL, text='DAMAGED'")
        conn.execute("INSERT INTO chunks (id, scope, source, path, "
                     "start_line, end_line, text, hash) VALUES "
                     "('revived-row','shared','memory','x.md',1,2,"
                     "'NEVER IN THE BACKUP','h')")
        conn.commit()
    finally:
        conn.close()

    # --- the undo button ----------------------------------------------
    proc2 = _run_tool(ws, "--rollback", str(backup_dir))
    assert proc2.returncode == 0, proc2.stdout
    assert "[PASS] ROLLBACK.ROOT" in proc2.stdout, proc2.stdout
    assert "integrity_check=ok" in proc2.stdout, proc2.stdout

    # --- and the data is EXACTLY the pre-migration state ---------------
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE id='revived-row'"
        ).fetchone()[0] == 0, (
            "the rollback revived a row that was never in the backup — "
            "the restore went through the WAL, not through SQLite")
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text='DAMAGED'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL"
        ).fetchone()[0] > 0, (
            "the rollback did not restore the pre-migration state")
        assert conn.execute(
            "PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_the_rollback_refuses_a_backup_that_is_not_there(tmp_path):
    """A rollback that restores NOTHING and reports success is worse
    than one that fails: the operator would believe the machine is
    back to the pre-migration state, and it is not."""
    ws, db = _one_library_workspace(tmp_path)
    proc = _run_tool(ws, "--rollback", str(tmp_path / "no-such-dir"))
    assert proc.returncode == 2, proc.stdout
    assert "[FAIL] ROLLBACK.INPUT" in proc.stdout, proc.stdout


def test_the_rollback_reports_a_library_it_could_not_restore(tmp_path):
    """A library left behind is a library still holding the
    half-migrated state — the operator must know WHICH one."""
    ws, db = _one_library_workspace(tmp_path)
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    # A backup for a DIFFERENT library name only.
    shutil.copy2(str(db), str(backup_dir / "SOMEONE-ELSE.p1-bak.db"))

    proc = _run_tool(ws, "--rollback", str(backup_dir))
    assert proc.returncode != 0, proc.stdout
    assert "ROLLBACK" in proc.stdout
    # The live library was NOT silently restored from a stranger's file.
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text='DAMAGED'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A-4: the baseline is a content digest, not a row count
# ---------------------------------------------------------------------------

def test_the_baseline_is_a_content_digest_not_a_row_count(tmp_path):
    """**Why not the row count.**  MEASURED (前置准备 §4.4, E-4): 0.114's
    ROOT library had 25 rows and the SERVICE writes to it by itself —
    it syncs the knowledge layer on its own schedule.  So the row
    count is not a constant of the machine.

    **Why not the file's SHA-256.**  A checkpoint moves bytes without
    moving rows, so the file's hash changes when nothing about the
    content changed.

    The digest must therefore move when the CONTENT moves, and stay
    when only the bytes move.
    """
    ws, db = _one_library_workspace(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        before = migrate.content_digest(conn, "chunks")
        assert before, "the digest must not be empty"

        # A checkpoint moves bytes without moving rows.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        assert migrate.content_digest(conn, "chunks") == before, (
            "a checkpoint changed the content digest — the digest is "
            "measuring the byte layout, not the content")
    finally:
        conn.close()

    # And a content change moves it.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET text='a different body' WHERE "
                     "id=(SELECT id FROM chunks LIMIT 1)")
        conn.commit()
        assert migrate.content_digest(conn, "chunks") != before
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A-6: the five checks print their ACTUAL VALUES
# ---------------------------------------------------------------------------

def test_a6_prints_the_five_actual_values(tmp_path):
    """A-6: "all green but running the OLD code".

    ``git rev-parse HEAD`` alone is not enough: the repository can be
    at commit X while the interpreter imports ``agent.memory`` from a
    ``site-packages`` copy at commit Y.  Every item prints its ACTUAL
    VALUE, so the reader can audit it.
    """
    ws, db = _one_library_workspace(tmp_path)
    proc = _run_tool(ws, "--step", "M7", "--dry-run",
                     "--backup-dir", str(tmp_path / "bk"))
    out = proc.stdout
    assert "A6.CODE" in out, out
    assert "agent.memory.__file__" in out, out
    assert "git rev-parse HEAD" in out, out
    assert "schema_fingerprint" in out, out
    assert "body_path IS NULL" in out, out
    assert "rows / content digest" in out, out
    # The fingerprint prints its COVERAGE — a digest whose coverage is
    # not stated cannot be audited.
    assert "tables=chunks,files" in out, out


# ---------------------------------------------------------------------------
# idempotence: the second pass fills ZERO rows
# ---------------------------------------------------------------------------

def test_the_second_pass_fills_zero_rows(tmp_path):
    """**The idempotence claim, made readable.**

    The counter must count CHANGES, not VISITS: a backfill that
    rewrites the same values into the same rows has filled nothing.
    The content digest must also be unchanged — that is the proof
    that the second pass really changed nothing.
    """
    ws, db = _one_library_workspace(tmp_path)
    bk1 = tmp_path / "bk1"
    bk2 = tmp_path / "bk2"

    first = _run_tool(ws, "--step", "M0,M1,M4,M5,M7",
                      "--backup-dir", str(bk1))
    assert first.returncode == 0, first.stdout
    assert "filled=5 (rows whose columns CHANGED)" in first.stdout, \
        first.stdout

    conn = sqlite3.connect(str(db))
    try:
        digest_after_first = migrate.content_digest(conn, "chunks")
    finally:
        conn.close()

    second = _run_tool(ws, "--step", "M0,M1,M4,M5,M7",
                       "--backup-dir", str(bk2))
    assert second.returncode == 0, second.stdout
    assert "filled=0 (rows whose columns CHANGED)" in second.stdout, \
        second.stdout
    assert "unchanged=5" in second.stdout, second.stdout

    conn = sqlite3.connect(str(db))
    try:
        assert migrate.content_digest(conn, "chunks") == digest_after_first
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# the whole flow, on the 0.114 shape
# ---------------------------------------------------------------------------

def test_the_whole_flow_is_green_on_the_0114_shape(tmp_path):
    """The acceptance shape: enumerate -> backup -> drill -> migrate ->
    verify -> idempotent, all green on a ONE-library workspace."""
    ws, db = _one_library_workspace(tmp_path)
    proc = _run_tool(ws, "--step", "M0,M1,M4,M5,M7",
                     "--backup-dir", str(tmp_path / "bk"))
    assert proc.returncode == 0, proc.stdout
    for cid in ("ENUM.GATE", "M0.ROOT", "M0.GATE", "M1.ROOT", "M1.GATE",
                "M4.ROOT.fill", "M4.ROOT.count", "M4.ROOT.hash",
                "M4.GATE", "M5.ROOT", "M5.GATE", "M7.ROOT", "A6.CODE"):
        assert "[PASS] %s" % cid in proc.stdout, (
            "%s did not pass:\n%s" % (cid, proc.stdout))
    assert "[FAIL]" not in proc.stdout, proc.stdout


def test_the_backup_is_written_outside_the_workspace(tmp_path):
    """A-4: the backup must not land inside the workspace, or the
    enumeration would pick it up on the next run."""
    ws, db = _one_library_workspace(tmp_path)
    backup_dir = tmp_path / "bk-outside"
    proc = _run_tool(ws, "--step", "M0", "--backup-dir", str(backup_dir))
    assert proc.returncode == 0, proc.stdout
    assert (backup_dir / "ROOT.p1-bak.db").is_file()
    assert not list(ws.glob("**/ROOT.p1-bak.db")), (
        "the backup landed inside the workspace")
