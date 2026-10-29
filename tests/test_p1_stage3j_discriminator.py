#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-J · the J-2/J-3 discriminator's reverse cases.

Each case builds a COPY of a real live library, puts it into the shape
the case is about, and asserts the criterion's verdict:

  * ``downgraded`` — a migrated library whose rows were demoted back
    to 'projection' (the origin held).  ⇒ J-2 must FAIL, and the
    reason must point at the downgrade.
  * ``never`` — a library the migration never touched.  ⇒ J-2 must
    NOTRUN and NAME the missing column.
  * ``erased`` — a migrated library whose files statistics columns
    were erased to NULL.  ⇒ J-3 must FAIL, saying the files
    bookkeeping was erased while the chunks bookkeeping survived.

**Why the guard's triggers are dropped.**  The S3-I guard BLOCKS a
state downgrade — measured: an UPDATE that demotes 'mirror' to
'projection' is reverted by the ``chunks_body_keep_bu`` trigger, and
an UPDATE that clears the files statistics is reverted by
``files_body_keep_bu``.  So without dropping them the mutation never
lands and the case would vacuously PASS.  That is the guard working,
and it is a good sign for the product — but a criterion that cannot
be made to fail is not a criterion, so the case reproduces the state a
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
from agent.memory.acceptance.live_libs import enumerate_live

WS = Path(r"C:\Users\User\cow")

#: The guard's triggers — dropped so the mutation can land.
GUARD_TRIGGERS = (
    "chunks_body_keep_bu", "chunks_body_keep_bi", "chunks_body_keep_bd",
    "chunks_body_keep_bi_ai", "chunks_body_keep_st_ai",
    "files_body_keep_bu", "files_body_keep_bi",
)


class FakeReport:
    """The report object the criteria write into."""

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


def _as_library(db, workspace):
    """A ``LiveLibrary`` for a library that is not under ``agents/``.

    The test workspaces hold their copy at the ROOT path shape, and
    ``enumerate_live`` classifies by path shape — so the ROOT copy
    comes back as a ROOT library.  The profile does not care which
    kind it is; it reads the database.  So the library is built
    directly instead of being enumerated.
    """
    from agent.memory.acceptance.live_libs import LiveLibrary
    return LiveLibrary("TEST", db, workspace, "ROOT")


def build_workspace(tmp_path, kind):
    """A workspace holding ONE library, in the shape ``kind`` says."""
    lib = _source_library()
    ws = tmp_path / kind
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    db = ws / "memory" / "long-term" / "index.db"
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(lib.db) + suffix)
        if s.is_file():
            shutil.copy2(s, Path(str(db) + suffix))
    conn = sqlite3.connect(str(db))
    try:
        for trig in GUARD_TRIGGERS:
            conn.execute("DROP TRIGGER IF EXISTS %s" % trig)
        if kind == "downgraded":
            conn.execute("UPDATE chunks SET text_state='projection'")
        elif kind == "never":
            conn.execute(
                "UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
                "body_hash=NULL, text_state='projection', "
                "body_origin='unresolved'")
            conn.execute("UPDATE files SET chunk_count=NULL, chunker_version=NULL")
        elif kind == "erased":
            conn.execute("UPDATE files SET chunk_count=NULL, chunker_version=NULL")
        else:  # pragma: no cover
            raise AssertionError("unknown kind %r" % kind)
        conn.commit()
    finally:
        conn.close()
    return ws


# ---------------------------------------------------------------------------
# the reverse cases
# ---------------------------------------------------------------------------

def test_downgraded_library_is_reported_as_a_defect(tmp_path):
    """A migrated-then-downgraded library is FAIL, with the downgrade named."""
    ws = build_workspace(tmp_path, "downgraded")
    rep = FakeReport()
    phase2_s3j.judge_migration_state_is_discriminable(rep, ws)
    assert rep.verdict("J-2(REAL)") == "FAIL", rep.detail("J-2(REAL)")
    detail = rep.detail("J-2(REAL)")
    assert "FAIL（迁移后被擦）" in detail
    # the evidence must be a PROOF, not an inference
    assert "被证明的" in detail
    # and the basis chain must be visible
    for f in ("F1", "F2", "F3", "F4"):
        assert f in detail


def test_never_migrated_library_is_notrun_and_names_the_missing_column(tmp_path):
    """A library the migration never touched is NOTRUN, and says why."""
    ws = build_workspace(tmp_path, "never")
    rep = FakeReport()
    phase2_s3j.judge_migration_state_is_discriminable(rep, ws)
    assert rep.verdict("J-2(REAL)") == "NOTRUN", rep.detail("J-2(REAL)")
    detail = rep.detail("J-2(REAL)")
    assert "NOTRUN（未迁移）" in detail
    # it must NAME which fact failed — a NOTRUN that does not say why
    # is a NOTRUN nobody can act on
    assert "未成立" in detail
    assert "chunks.body_path" in detail
    assert "chunks.body_origin" in detail


def test_erased_files_statistics_are_a_defect_not_a_prerequisite(tmp_path):
    """The files bookkeeping erased while the chunks survived is FAIL."""
    ws = build_workspace(tmp_path, "erased")
    rep = FakeReport()
    phase2_s3j.judge_files_statistics_are_not_erased(rep, ws)
    assert rep.verdict("J-3(REAL)") == "FAIL", rep.detail("J-3(REAL)")
    detail = rep.detail("J-3(REAL)")
    assert "被擦成 NULL" in detail
    # the reason must distinguish it from "never migrated"
    assert "chunks 侧已迁移" in detail


def test_the_two_states_give_different_verdicts(tmp_path):
    """The whole point: the two states must NOT collapse into one."""
    down = build_workspace(tmp_path / "a", "downgraded")
    never = build_workspace(tmp_path / "b", "never")

    r1, r2 = FakeReport(), FakeReport()
    phase2_s3j.judge_migration_state_is_discriminable(r1, down)
    phase2_s3j.judge_migration_state_is_discriminable(r2, never)
    assert r1.verdict("J-2(REAL)") != r2.verdict("J-2(REAL)")
    assert r1.verdict("J-2(REAL)") == "FAIL"
    assert r2.verdict("J-2(REAL)") == "NOTRUN"


def test_a_migrated_library_passes(tmp_path):
    """The unmodified copy of a real library PASSES — no false red."""
    lib = _source_library()
    ws = tmp_path / "clean"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    db = ws / "memory" / "long-term" / "index.db"
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(lib.db) + suffix)
        if s.is_file():
            shutil.copy2(s, Path(str(db) + suffix))
    rep = FakeReport()
    phase2_s3j.judge_migration_state_is_discriminable(rep, ws)
    assert rep.verdict("J-2(REAL)") == "PASS", rep.detail("J-2(REAL)")


# ---------------------------------------------------------------------------
# the F-chain's own mechanics
# ---------------------------------------------------------------------------

def test_the_f_chain_reads_the_columns_it_claims():
    """Every fact in the chain names the column it reads — and reads it."""
    lib = _source_library()
    prof = phase2_s3j.MigrationProfile(lib)
    chain = prof.f_chain(4)
    assert len(chain) == 5
    for (name, _ok, column) in chain:
        assert name and column
    # F1 must be decided by body_path, F2 by body_origin, F4 by the
    # files' chunker_version — the chain's whole value is that each
    # fact reads a DIFFERENT column that only the migration writes.
    assert "body_path" in chain[0][2]
    assert "body_origin" in chain[1][2]
    assert "chunker_version" in chain[3][2]


def test_the_version_comparison_is_numeric_not_textual():
    """A TEXT-declared version column must still compare as a number.

    This is a REGRESSION test for a real bug: ``files.chunker_version``
    is declared TEXT, so SQLite returned the distribution as
    ``{'4': 93}``.  The F4 comparison against ``{4}`` was therefore
    False for every library, and the criterion reported "never
    migrated" for sixteen libraries that had all been migrated.
    """
    lib = _source_library()
    prof = phase2_s3j.MigrationProfile(lib)
    if not prof.files_cv:
        pytest.skip("本机没有 files.chunker_version 数据")
    for key in prof.files_cv:
        assert isinstance(key, int), (
            "chunker_version 的键必须是整数，否则 F4 的比较永远不成立"
            "（实测 bug：TEXT 列返回 '4'，与 4 不相等）")


def test_the_bulk_rewrite_needs_many_paths_not_many_rows():
    """A big file may write many rows in one second; that is not a rewrite."""
    prof = phase2_s3j.MigrationProfile.__new__(phase2_s3j.MigrationProfile)
    prof.created_at_top = [(100, 500, 1)]   # one path, 500 rows
    assert prof.bulk_rewrite() is None
    prof.created_at_top = [(100, 8, 3)]     # three paths, 8 rows
    assert prof.bulk_rewrite() == (100, 8, 3)


def test_a_proven_downgrade_is_found_in_body_keep(tmp_path):
    """``body_keep`` is the witness: keep=mirror & chunks!=mirror."""
    ws = build_workspace(tmp_path, "downgraded")
    db = ws / "memory" / "long-term" / "index.db"
    prof = phase2_s3j.MigrationProfile(_as_library(db, ws))
    assert prof.downgrade_proven > 0, (
        "降级后 body_keep 应当记住 mirror —— 这是【证明】而不是推断")


def test_a_clean_library_has_no_downgrade_evidence(tmp_path):
    """The unmodified copy must NOT look like a downgrade."""
    lib = _source_library()
    ws = tmp_path / "clean2"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    db = ws / "memory" / "long-term" / "index.db"
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(lib.db) + suffix)
        if s.is_file():
            shutil.copy2(s, Path(str(db) + suffix))
    conn = sqlite3.connect(str(db))
    try:
        for trig in GUARD_TRIGGERS:
            conn.execute("DROP TRIGGER IF EXISTS %s" % trig)
        conn.commit()
    finally:
        conn.close()
    prof = phase2_s3j.MigrationProfile(_as_library(db, ws))
    assert prof.downgrade_proven == 0
