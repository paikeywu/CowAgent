#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-R · the reverse cases for the two criteria that were
wired in this round, and for the two mouths that were unified.

Each case builds a COPY of a real live library, puts it into the shape
the case is about, and asserts the criterion's verdict.

  * ``M6f`` — a ledger entry that names a chunk which is NOT in the
    index ⇒ M6f must FAIL and NAME the orphan.
  * ``M6f`` — a clean ledger ⇒ M6f must PASS.
  * ``XF``  — two libraries with DIFFERENT shapes ⇒ XF must FAIL and
    name the dimension that differs.
  * ``XF``  — one library ⇒ XF must PASS (a fleet of one is uniform).
  * ``J-3`` — a library whose ``files.chunker_version`` is NULL on a
    path the chunks side never decided ⇒ J-3 must PASS ("never
    measured", not "version wrong").  This is the case that used to
    be a false red.
  * ``J-3`` — a library whose ``files.chunker_version`` is NULL on a
    path the chunks side DID decide ⇒ J-3 must FAIL ("erased").
  * ``J-3`` × ``M6e`` — the two must AGREE on every row.  This is the
    property the criterion set was missing (17/17 disagreement in R7).

**Why the guard's triggers are dropped.**  The guard BLOCKS a state
downgrade, so without dropping it the mutation never lands and the
case would vacuously PASS.  The case reproduces the state a
PRE-GUARD build would have left behind.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(r"C:\Workspace\AI-Agent\CowAgent")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest

from agent.memory.acceptance import phase2_s3j
from agent.memory.acceptance import phase2_s3r
from agent.memory.acceptance.live_libs import enumerate_live, LiveLibrary

WS = Path(r"C:\Users\User\cow")

GUARD_TRIGGERS = (
    "chunks_body_keep_bu", "chunks_body_keep_bi", "chunks_body_keep_bd",
    "chunks_body_keep_bi_ai", "chunks_body_keep_st_ai",
    "files_body_keep_bu", "files_body_keep_bi",
)


class FakeReport:
    def __init__(self):
        self.rows = []

    def judge(self, group, name, ok, detail=""):
        self.rows.append((group, name, "PASS" if ok else "FAIL", detail))
        return self.rows[-1]

    def judge_notrun(self, group, name, detail=""):
        self.rows.append((group, name, "NOTRUN", detail))
        return self.rows[-1]

    def verdict(self, group):
        for (g, _n, v, _d) in self.rows:
            if g == group:
                return v
        raise AssertionError("no verdict for %s" % group)

    def detail(self, group):
        for (g, _n, _v, d) in self.rows:
            if g == group:
                return d
        raise AssertionError("no detail for %s" % group)


def _source_library():
    libs = [l for l in enumerate_live(WS) if l.kind == "AGENT"]
    if not libs:
        pytest.skip("本机没有活库，前置条件不成立")
    return libs[0]


def _copy(lib, dst_db):
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(lib.db) + suffix)
        if s.is_file():
            shutil.copy2(s, Path(str(dst_db) + suffix))
    return dst_db


def _drop_guard_triggers(conn):
    for trig in GUARD_TRIGGERS:
        conn.execute("DROP TRIGGER IF EXISTS %s" % trig)


def build_workspace(tmp_path, kind):
    """A workspace holding ONE library, in the shape ``kind`` says."""
    lib = _source_library()
    ws = tmp_path / kind
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    db = ws / "memory" / "long-term" / "index.db"
    _copy(lib, db)
    conn = sqlite3.connect(str(db))
    try:
        _drop_guard_triggers(conn)
        if kind == "orphan_ledger":
            # a certification record that names a chunk which is GONE
            victim = conn.execute(
                "SELECT id FROM chunks LIMIT 1").fetchone()[0]
            conn.execute("DELETE FROM chunks WHERE id=?", (victim,))
        elif kind in ("clean_ledger", "xf_one", "agreement"):
            pass
        elif kind == "never_measured":
            # a files row whose chunker_version is NULL, for a path the
            # chunks side never decided
            _null_files_row(conn, "memory/never-measured.md")
            # and no chunks row for that path at all
            conn.execute("DELETE FROM chunks WHERE path='memory/never-measured.md'")
        elif kind == "erased_stat":
            # a files row whose chunker_version is NULL, for a path the
            # chunks side DID decide (it has decided rows and v4 anchors)
            path = conn.execute(
                "SELECT path FROM chunks WHERE body_origin='file' "
                "AND body_anchor LIKE '%+S%' LIMIT 1").fetchone()[0]
            _null_files_row(conn, path)
        else:  # pragma: no cover
            raise AssertionError("unknown kind %r" % kind)
        conn.commit()
    finally:
        conn.close()
    return ws


def _null_files_row(conn, path):
    """A ``files`` row whose statistics columns are NULL.

    ``files.hash`` / ``files.size`` / ``files.mtime`` / ``files.source``
    are NOT NULL in the live schema, so they are filled with the
    values a real row carries — the case under test is about the
    STATISTICS columns being NULL, not about the row being malformed.

    The row may already exist (the path is a live one), in which case
    its statistics are CLEARED rather than a second row inserted.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM files WHERE path=?", (path,)).fetchone()[0]
    if row:
        conn.execute(
            "UPDATE files SET chunk_count=NULL, chunker_version=NULL "
            "WHERE path=?", (path,))
        return
    conn.execute(
        "INSERT INTO files (path, chunk_count, chunker_version, hash, mtime, "
        "                     size, source, updated_at) "
        "VALUES (?, NULL, NULL, ?, 0, 0, 'memory', 0)",
        (path, "x" * 64))


# ------------------------------------------------------------------ M6f

def test_m6f_an_orphan_ledger_entry_is_a_defect(tmp_path):
    """A ledger entry that names a chunk which is gone must be FAIL."""
    ws = build_workspace(tmp_path, "orphan_ledger")
    rep = FakeReport()
    phase2_s3r.judge_m6f_ledger_has_no_orphans(rep, ws)
    assert rep.verdict("M6f(REAL)") == "FAIL", rep.detail("M6f(REAL)")
    detail = rep.detail("M6f(REAL)")
    assert "孤儿" in detail


def test_m6f_a_clean_ledger_passes(tmp_path):
    """A ledger whose every entry names a live chunk must PASS."""
    ws = build_workspace(tmp_path, "clean_ledger")
    rep = FakeReport()
    phase2_s3r.judge_m6f_ledger_has_no_orphans(rep, ws)
    assert rep.verdict("M6f(REAL)") == "PASS", rep.detail("M6f(REAL)")


# ------------------------------------------------------------------- XF

def test_xf_one_library_is_uniform(tmp_path):
    """A fleet of one is uniform — the criterion must not fail on that."""
    ws = build_workspace(tmp_path, "xf_one")
    rep = FakeReport()
    phase2_s3r.judge_xf_cross_end_fingerprint_is_uniform(rep, ws)
    assert rep.verdict("XF(REAL)") == "PASS", rep.detail("XF(REAL)")


def test_xf_two_libraries_with_different_shapes_fail(tmp_path):
    """Two libraries that differ in one dimension must FAIL, and the
    report must NAME the dimension that differs."""
    lib = _source_library()
    ws = tmp_path / "xf_two"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    (ws / "agents" / "agent-test" / "memory" / "long-term").mkdir(
        parents=True, exist_ok=True)
    db_a = ws / "memory" / "long-term" / "index.db"
    db_b = ws / "agents" / "agent-test" / "memory" / "long-term" / "index.db"
    _copy(lib, db_a)
    _copy(lib, db_b)
    # make B differ: drop a column's NOT NULL in the files table
    conn = sqlite3.connect(str(db_b))
    try:
        _drop_guard_triggers(conn)
        conn.execute("DROP TABLE files")
        conn.execute(
            "CREATE TABLE files (path TEXT PRIMARY KEY, chunk_count INTEGER, "
            "chunker_version INTEGER, hash TEXT, mtime INTEGER, size INTEGER, "
            "source TEXT, updated_at INTEGER)")
        conn.commit()
    finally:
        conn.close()

    rep = FakeReport()
    phase2_s3r.judge_xf_cross_end_fingerprint_is_uniform(rep, ws)
    assert rep.verdict("XF(REAL)") == "FAIL", rep.detail("XF(REAL)")
    detail = rep.detail("XF(REAL)")
    assert "不同的维度" in detail


# ------------------------------------------------------------------ J-3

def test_j3_a_never_measured_null_is_not_a_defect(tmp_path):
    """A NULL statistic on a path the chunks side never decided is
    'never measured' — NOT 'version wrong'.  This is the case that
    used to be a false red (R7: 17/17 disagreement with M6e)."""
    ws = build_workspace(tmp_path, "never_measured")
    rep = FakeReport()
    phase2_s3j.judge_files_statistics_are_not_erased(rep, ws)
    assert rep.verdict("J-3(REAL)") == "PASS", rep.detail("J-3(REAL)")
    detail = rep.detail("J-3(REAL)")
    assert "从未测量" in detail
    # and it must NOT report the NULL as a version problem.  The
    # criterion's own three-state legend mentions 「版本错」, so the
    # assertion is on the VERDICT line, not on the legend.
    assert "存在非当前版本的 chunker_version" not in detail, detail


def test_j3_an_erased_null_is_a_defect(tmp_path):
    """A NULL statistic on a path the chunks side DID decide was
    erased — that IS a defect."""
    ws = build_workspace(tmp_path, "erased_stat")
    rep = FakeReport()
    phase2_s3j.judge_files_statistics_are_not_erased(rep, ws)
    assert rep.verdict("J-3(REAL)") == "FAIL", rep.detail("J-3(REAL)")
    detail = rep.detail("J-3(REAL)")
    assert "被擦" in detail


def test_j3_and_m6e_agree_on_every_row(tmp_path):
    """J-3 and M6e ask the same question with the same discriminator,
    so their conclusions MUST agree.  This is the property the
    criterion set did not have (R7 measured 17/17 disagreement)."""
    ws = build_workspace(tmp_path, "agreement")
    rep = FakeReport()
    phase2_s3j.judge_files_statistics_are_not_erased(rep, ws)
    j3 = rep.verdict("J-3(REAL)")
    j3_detail = rep.detail("J-3(REAL)")

    from agent.memory.acceptance import phase2_s3k
    rep2 = FakeReport()
    phase2_s3k.judge_m6e_files_statistics_are_guarded(rep2, ws)
    m6e = rep2.verdict("M6e(REAL)")

    # J-3 may report NOTRUN for a library with no files rows; M6e may
    # report NOTRUN for no live library.  Otherwise they must agree.
    if j3 != "NOTRUN" and m6e != "NOTRUN":
        assert j3 == m6e, (
            "J-3 and M6e disagree on the same library:\n"
            "  J-3  = %s\n%s\n  M6e  = %s\n%s"
            % (j3, j3_detail, m6e, rep2.detail("M6e(REAL)")))


def test_j3_a_null_is_never_reported_as_a_wrong_version(tmp_path):
    """The specific bug the R7 review found: NULL was classified as
    'a non-current version'.  The mutation is a NULL, and the verdict
    must not call it a version problem."""
    ws = build_workspace(tmp_path, "never_measured")
    rep = FakeReport()
    phase2_s3j.judge_files_statistics_are_not_erased(rep, ws)
    detail = rep.detail("J-3(REAL)")
    assert "存在非当前版本的 chunker_version" not in detail, detail
