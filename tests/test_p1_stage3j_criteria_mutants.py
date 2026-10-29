#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-J · the MUTANT REPLAY for the discriminator.

Every test here has the same shape as the S3-E/S3-H replays:

    1. build a disposable copy of the object (never the live index);
    2. break it in ONE specific way;
    3. run the criterion against the broken copy;
    4. assert it went RED (or NOTRUN — the third state, where the
       mutation removes a PRECONDITION rather than breaking a value);
    5. restore the object and assert it went GREEN again.

The mutations replayed
----------------------
MU-18  the F-chain loses F4.  If the chain only asks "is there a
       pointer and a decided origin" then a library whose files
       bookkeeping was never written looks migrated — and the
       "never migrated" state collapses into the "migrated" state,
       which is exactly the confusion this round exists to kill.

MU-19  the version comparison becomes TEXTUAL.  ``files.chunker_version``
       is declared TEXT, so SQLite hands back ``'4'``.  Comparing
       ``'4' == 4`` is False, and the criterion then reports "never
       migrated" for every library that IS migrated.  This is not
       hypothetical: it was a REAL bug in the first version of this
       round's module, caught by the criterion's own first run.

MU-20  the verdict order is inverted: an unmet prerequisite (NOTRUN)
       outranks a measured defect (FAIL).  Then a library that was
       migrated and erased gets hidden behind a library that was merely
       not ready — the criterion reports "not measured" for a defect it
       DID measure.  A criterion that can hide a defect behind a
       missing prerequisite is worse than no criterion.

The independence rule
----------------------
The criteria's expected values are NOT taken from the object under
test.  MU-18's expectation (the chain must consult the files
bookkeeping) is checked by the criterion reading the files table
itself; MU-19's expectation (the version must compare numerically) is
checked by the criterion's own normalisation; MU-20's expectation (a
measured defect outranks an unmet prerequisite) is checked by the
verdict the criterion returns.

So the mutants are injected into the CRITERION (the object under test
for this round) and the replay proves the criterion notices.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import Report, STATE_FAIL, STATE_NOTRUN, STATE_PASS  # noqa: E402
from agent.memory.acceptance import phase2_s3j  # noqa: E402
from agent.memory.acceptance.live_libs import LiveLibrary  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _states(report: Report):
    return {number: state for number, _name, state in report.rows}


def _run(fn, *args):
    import contextlib
    import io

    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    return report, _states(report)


def _make_library(root: Path, name: str = "TEST", *, text_state: str = "mirror",
                  body_origin: str = "file", chunker_version=4,
                  with_pointer: bool = True, with_files: bool = True):
    """A live library with one pointed row and its files bookkeeping."""
    db_dir = root / "memory" / "long-term"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, source TEXT,
                path TEXT, start_line INTEGER, end_line INTEGER, text TEXT,
                embedding BLOB, hash TEXT, metadata TEXT,
                created_at INTEGER, updated_at INTEGER,
                hit_count INTEGER DEFAULT 0, last_hit_at INTEGER,
                element_released INTEGER DEFAULT 0, file_sealed INTEGER DEFAULT 0,
                sealed_owner TEXT,
                body_path TEXT, body_anchor TEXT, body_hash TEXT,
                text_state TEXT DEFAULT 'projection',
                body_origin TEXT NOT NULL DEFAULT 'unresolved'
            )""")
        conn.execute("""
            CREATE TABLE files (
                path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL,
                size INTEGER, updated_at INTEGER,
                chunk_count INTEGER, chunker_version INTEGER
            )""")
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash, "
            "body_path, body_anchor, body_hash, text_state, body_origin, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("id-" + name, "MEMORY.md", 1, 3, "body text",
             "sha256:" + "0" * 64,
             "MEMORY.md" if with_pointer else None,
             "L1-3" if with_pointer else None,
             "sha256:" + "0" * 64 if with_pointer else None,
             text_state, body_origin, 1000, 1000))
        if with_files:
            conn.execute(
                "INSERT INTO files (path, source, hash, mtime, size, "
                "updated_at, chunk_count, chunker_version) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("MEMORY.md", "test", "h", 1.0, 3, 1000, 1, chunker_version))
        conn.commit()
    finally:
        conn.close()
    return LiveLibrary(name, db, root, "ROOT")


# ---------------------------------------------------------------------------
# MU-18: the F-chain loses F4
# ---------------------------------------------------------------------------

def test_mu18_the_chain_must_consult_the_files_bookkeeping(tmp_path):
    """MU-18: drop F4 from the chain ⇒ the never-migrated state vanishes.

    Without F4, a library whose files bookkeeping was never written
    still satisfies F1..F3, so the chain calls it MIGRATED — and the
    "never migrated" verdict becomes unreachable.  The criterion would
    then report FAIL (or PASS) for a library whose prerequisite was
    simply not met.
    """
    lib = _make_library(tmp_path / "no-files", with_files=False)
    version, _src = phase2_s3j.current_chunker_version()

    # --- BEFORE the mutation: F4 fails ⇒ the library is NOT migrated
    prof = phase2_s3j.MigrationProfile(lib)
    chain = prof.f_chain(version)
    assert chain[3][0].startswith("F4")
    assert chain[3][1] is False, "没有 files 记账时 F4 必须不成立"
    assert prof.is_migrated(version) is False

    # --- the mutant: the chain stops consulting the files bookkeeping
    original = phase2_s3j.MigrationProfile.f_chain

    def without_f4(self, current_chunker_version):
        chain = original(self, current_chunker_version)
        return chain[:3] + [("F4 已删除（变异体）", True, "files.chunker_version")]

    phase2_s3j.MigrationProfile.f_chain = without_f4
    try:
        prof2 = phase2_s3j.MigrationProfile(lib)
        assert prof2.is_migrated(version) is True, (
            "变异体应当让『没迁移』这个状态消失 —— "
            "这正是 MU-18 证明的洞")
    finally:
        phase2_s3j.MigrationProfile.f_chain = original

    # --- AFTER the restore: the chain consults the files bookkeeping again
    prof3 = phase2_s3j.MigrationProfile(lib)
    assert prof3.is_migrated(version) is False


def test_mu18_the_criterion_reports_notrun_for_a_library_without_files(tmp_path):
    """The criterion itself: a library with no files bookkeeping ⇒ NOTRUN."""
    lib = _make_library(tmp_path / "no-files2", with_files=False)
    ws = tmp_path / "no-files2"
    report, states = _run(phase2_s3j.judge_migration_state_is_discriminable, ws)
    assert states.get("J-2(REAL)") == STATE_NOTRUN, report.printed
    assert "NOTRUN（未迁移）" in report.printed


# ---------------------------------------------------------------------------
# MU-19: the version comparison becomes textual
# ---------------------------------------------------------------------------

def test_mu19_a_text_declared_version_column_must_still_compare_numerically(tmp_path):
    """MU-19: compare the version as TEXT ⇒ every library looks unmigrated.

    ``files.chunker_version`` is declared TEXT, so SQLite returns the
    string ``'4'``.  A textual comparison against the integer 4 is
    False for every library — and the criterion then says "never
    migrated" about sixteen libraries that had all been migrated.

    This was a REAL bug in the first version of this round's module.
    The mutant here is the same shape: normalise the keys away and the
    criterion's own conclusion changes.
    """
    ws = tmp_path / "text-version"
    _make_library(ws, chunker_version="4")   # a TEXT value, as SQLite stores it
    version, _src = phase2_s3j.current_chunker_version()

    prof = phase2_s3j.MigrationProfile(
        LiveLibrary("TEST", ws / "memory" / "long-term" / "index.db", ws, "ROOT"))
    # the criterion's own normalisation makes the comparison numeric
    assert prof.is_migrated(version) is True, (
        "TEXT '4' 必须被规范化成整数 4 —— 否则 F4 永远不成立")

    # --- the mutant: the keys are compared as TEXT
    original = phase2_s3j.MigrationProfile.__dict__["_dist"]

    def textual(conn, sql):
        out = {}
        for k, v in conn.execute(sql).fetchall():
            out[str(k)] = int(v)
        return out

    phase2_s3j.MigrationProfile._dist = staticmethod(textual)
    try:
        prof2 = phase2_s3j.MigrationProfile(
            LiveLibrary("TEST", ws / "memory" / "long-term" / "index.db", ws, "ROOT"))
        assert prof2.is_migrated(version) is False, (
            "变异体（按文本比较）应当让『已迁移』这个状态消失")
    finally:
        # The restore must put back the STATICMETHOD, not the bare
        # function — restoring the bare function turns it into an
        # instance method, and the criterion then dies with
        # "_dist() takes 2 positional arguments but 3 were given".
        # That failure is NOT hypothetical: it is what the first
        # version of this test did, and it broke the two tests that
        # ran after it.
        phase2_s3j.MigrationProfile._dist = original

    # --- AFTER the restore: the comparison is numeric again
    prof3 = phase2_s3j.MigrationProfile(
        LiveLibrary("TEST", ws / "memory" / "long-term" / "index.db", ws, "ROOT"))
    assert prof3.is_migrated(version) is True


# ---------------------------------------------------------------------------
# MU-20: the verdict order is inverted
# ---------------------------------------------------------------------------

def test_mu20_a_measured_defect_outranks_an_unmet_prerequisite(tmp_path):
    """MU-20: NOTRUN outranking FAIL hides a defect behind a missing prerequisite.

    Two libraries in one workspace: one was migrated and then erased (a
    defect), one was never migrated (a missing prerequisite).  The
    verdict must be FAIL — the defect was MEASURED.  If the order is
    inverted, the criterion says "not measured" about a defect it did
    measure, and the defect disappears from the report.
    """
    ws = tmp_path / "mixed"
    ws.mkdir(parents=True, exist_ok=True)
    # The mixed workspace is built by hand: one ROOT library and one
    # AGENT library, because ``enumerate_live`` classifies by path
    # shape and both shapes must be present.
    agent_ws = ws / "agents" / "erased"
    (agent_ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    root_ws = ws
    (root_ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)

    # the ROOT library: never migrated
    _make_library(root_ws, "ROOT", text_state="projection",
                  body_origin="unresolved", with_pointer=False, with_files=False)
    # the AGENT library: migrated, then erased
    _make_library(agent_ws, "erased", text_state="projection",
                  body_origin="file", with_pointer=True, with_files=True)

    report, states = _run(phase2_s3j.judge_migration_state_is_discriminable, ws)
    assert states.get("J-2(REAL)") == STATE_FAIL, (
        "存在已测出的降级时必须报 FAIL，不能被『未迁移』盖住")
    assert "迁移后被擦" in report.printed

    # --- the mutant: NOTRUN outranks FAIL
    original = phase2_s3j.judge_migration_state_is_discriminable

    def inverted(report, workspace):
        # the mutant's behaviour: an unmet prerequisite wins
        detail = []
        libs = phase2_s3j.enumerate_live(workspace)
        version, _src = phase2_s3j.current_chunker_version()
        notrun = False
        for lib in libs:
            prof = phase2_s3j.MigrationProfile(lib)
            if not prof.is_migrated(version):
                notrun = True
            else:
                for state, n in prof.text_states.items():
                    if state != "mirror":
                        notrun = True
        if notrun:
            return report.judge_notrun(
                "J-2(REAL)", "J-2 判别（变异体：NOTRUN 优先）", "变异体")
        return original(report, workspace)

    phase2_s3j.judge_migration_state_is_discriminable = inverted
    try:
        report2, states2 = _run(phase2_s3j.judge_migration_state_is_discriminable, ws)
        assert states2.get("J-2(REAL)") == STATE_NOTRUN, (
            "变异体应当把『已测出的降级』藏成『没测』")
    finally:
        phase2_s3j.judge_migration_state_is_discriminable = original

    # --- AFTER the restore: the defect is visible again
    report3, states3 = _run(phase2_s3j.judge_migration_state_is_discriminable, ws)
    assert states3.get("J-2(REAL)") == STATE_FAIL


# ---------------------------------------------------------------------------
# the replay registry
# ---------------------------------------------------------------------------

def test_the_s3j_mutants_are_registered():
    """The replay must be REGISTERED — a mutant nobody replays is a
    mutant nobody knows about.

    The registry lives in the S3-H replay module (it is the one the
    gate's MU-REPLAY criterion reads), so the S3-J mutants are added
    to it.
    """
    import agent.memory.acceptance.phase2_guards as guards
    src = Path(guards.__file__).read_text(encoding="utf-8")
    for mu in ("MU-18", "MU-19", "MU-20"):
        assert mu in src, "%s 没有登记" % mu
