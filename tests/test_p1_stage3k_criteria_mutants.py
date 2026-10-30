#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-K · the MUTANT REPLAY for the certification criteria.

Every test here has the same shape as the S3-E/S3-H/S3-J replays:

    1. build a disposable copy of the object (never the live index);
    2. break it in ONE specific way;
    3. run the criterion against the broken copy;
    4. assert it went RED (or NOTRUN — the third state, where the
       mutation removes a PRECONDITION rather than breaking a value);
    5. restore the object and assert it went GREEN again.

**The mutations are made on the DATA, not on the criterion**, wherever
the data can express the mutation.  A data mutation is the stronger
proof: it says "the database is in this state, and the criterion says
this about it", not "the code was patched and then it said that".  A
criterion that is monkeypatched into agreeing with the mutant proves
nothing about the criterion.

The mutations replayed
----------------------
MU-21  the keep table becomes the ledger.  The keep table is written
       by the guard's TRIGGERS, and a trigger only runs when a write
       is THREATENED — so a newly written row is absent from it, and
       a row in it means "a write threatened this", NOT "this was
       certified".  A criterion that asks "was this row certified?"
       against the keep table answers the wrong question.

MU-22  the ledger table is MISSING.  "There is no ledger" means "not
       one row was ever certified" — the strongest possible form of the
       answer, not an inability to measure.  A NOTRUN there would be a
       silent green on the exact question the criterion exists to ask.

MU-23  the certified row's state is changed under it.  The ledger
       says "mirror", the row now says "projection": that is a
       PROVEN downgrade.  The complement — a REFUSED (orphan) row that
       was never certified — must NOT be counted as one.

MU-24  the ledger entry is deleted while the row keeps claiming
       "mirror".  The row then says "certified" with nobody able to
       say when, or against what.  MEASURED on the live ROOT library:
       15 such rows exist, and neither the certifier nor the backfill
       can repair them.

MU-25  the truth file grows after the bookkeeping was written.  The
       chunk rows are the projection of the file as it was when it was
       last synced, so the stored count legitimately falls behind.  A
       criterion that compares against the CHUNK ROW COUNT calls every
       grown file broken.

MU-26  the chunks side says the file WAS migrated (decided origin +
       v4 anchor) while the statistics are NULL.  That is an erasure.
       The complement — the chunks side says the file was NEVER
       migrated — makes the NULL the honest "not measured" of Rule D.

The independence rule
----------------------
The criteria's expected values are NOT taken from the object under
test.  MU-21's expectation (the ledger is the witness) is checked by
the criterion reading ``cert_ledger`` itself; MU-22's expectation (a
missing ledger is a FAIL, not a NOTRUN) is checked by the verdict the
criterion returns; MU-23's expectation (a refused row is not a
downgrade, a real downgrade is) is checked by the DOWNGRADE count the
criterion prints; MU-24's expectation (every mirror row has a
witness) is checked by the count of unwitnessed rows; MU-25's
expectation (the ground truth is a re-chunk) is checked by the
criterion's own re-chunking; MU-26's expectation (never-measured is
not erased) is checked by the chunks-side state the criterion reads.
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
from agent.memory.acceptance import phase2_s3k  # noqa: E402
from agent.memory.acceptance.live_libs import LiveLibrary  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _states(report: Report):
    return {number: state for number, _name, state in report.rows}


def _run(fn, *args):
    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    return report, _states(report)


def _db_of(ws: Path) -> Path:
    return ws / "memory" / "long-term" / "index.db"


def _make_library(root: Path, name: str = "TEST", *,
                  text_state: str = "mirror",
                  body_origin: str = "file",
                  with_pointer: bool = True,
                  with_files: bool = True,
                  with_ledger: bool = True,
                  with_body_keep: bool = False,
                  chunk_count=1, chunker_version=4,
                  ledger_state: str = "mirror",
                  ledger_rows=1):
    """A live library with one pointed row, its files bookkeeping, and
    (optionally) the certification ledger and the guard's keep table."""
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
                ("MEMORY.md", "test", "h", 1.0, 3, 1000, chunk_count,
                 chunker_version))
        if with_body_keep:
            conn.execute("""
                CREATE TABLE body_keep (
                    id TEXT PRIMARY KEY,
                    body_path TEXT, body_anchor TEXT, body_hash TEXT,
                    text_state TEXT, body_origin TEXT)""")
            conn.execute(
                "INSERT INTO body_keep (id, body_path, body_anchor, "
                "body_hash, text_state, body_origin) VALUES (?,?,?,?,?,?)",
                ("id-" + name, "MEMORY.md", "L1-3", "sha256:" + "0" * 64,
                 "mirror", "file"))
        if with_ledger:
            conn.execute("""
                CREATE TABLE cert_ledger (
                    id TEXT PRIMARY KEY,
                    text_state TEXT, body_origin TEXT,
                    body_path TEXT, body_hash TEXT,
                    certified_at INTEGER, certified_by TEXT)""")
            if ledger_rows:
                conn.execute(
                    "INSERT INTO cert_ledger (id, text_state, body_origin, "
                    "body_path, body_hash, certified_at, certified_by) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("id-" + name, ledger_state, "file", "MEMORY.md",
                     "sha256:" + "0" * 64, 1000, "certify:promoted"))
        conn.commit()
    finally:
        conn.close()
    return LiveLibrary(name, db, root, "ROOT")


def _add_refused_row(ws: Path, row_id: str = "id-refused"):
    """Add a REFUSED row: orphan origin, projection state, no ledger."""
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash, "
            "body_path, body_anchor, body_hash, text_state, body_origin, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, "MEMORY.md", 4, 6, "refused text",
             "sha256:" + "1" * 64, "MEMORY.md", "L4-6",
             "sha256:" + "1" * 64, "projection", "orphan", 1000, 1000))
        conn.commit()
    finally:
        conn.close()


def _md(n_sections: int) -> str:
    """A markdown body whose chunk count grows with the section count.

    MEASURED: ``chunk_for_file`` on a ``.md`` path uses the
    structure-aware splitter, and the chunk count grows with the
    sections (1 section -> 1 chunk, 8 -> 2, 12 -> 3, 20 -> 5).  That
    is what the MU-25 fixture needs: a file whose chunk count really
    changes when the file grows.
    """
    return "\n\n".join("# Section %d\n\n%s" % (i, "body text " * 30)
                       for i in range(1, n_sections + 1))


# ---------------------------------------------------------------------------
# MU-21: the keep table is not the witness
# ---------------------------------------------------------------------------

def test_mu21_the_keep_table_is_not_the_witness(tmp_path):
    """MU-21: the keep table cannot answer "was this row certified?".

    The keep table is written by the guard's triggers, and a trigger
    only runs when a write is THREATENED — so a newly written row is
    absent from it, and a row IN it means "a write threatened this",
    not "this was certified".

    The mutation is made on the DATA: the keep table is RENAMED to
    ``cert_ledger``.  The criterion then reads it as the ledger and
    finds the row in it — so it concludes "this row was certified" when
    nobody ever certified it.  That is the hole, expressed as a fact
    about the database rather than about the code.
    """
    ws = tmp_path / "keep-witness"
    _make_library(ws, "TEST", text_state="projection", body_origin="file",
                  with_ledger=False, with_body_keep=True)

    # --- BEFORE the mutation: the ledger is absent ⇒ the row is pending
    report, states = _run(phase2_s3k.judge_m6a_certification_coverage, ws)
    assert states.get("M6a(REAL)") == STATE_FAIL, report.printed
    assert "没有 cert_ledger" in report.printed

    # --- the mutation: the keep table BECOMES the ledger
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute("ALTER TABLE body_keep RENAME TO cert_ledger")
        conn.commit()
    finally:
        conn.close()

    report2, states2 = _run(phase2_s3k.judge_m6a_certification_coverage, ws)
    assert states2.get("M6a(REAL)") == STATE_PASS, (
        "变异体应当把『从未认证』的行判成已认证 —— "
        "这正是 MU-21 证明的洞：keep 表记的是『被威胁过』，不是『被认证过』")
    assert "未认证(not_certified)=0" in report2.printed

    # --- AFTER the restore: the keep table is a keep table again
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute("ALTER TABLE cert_ledger RENAME TO body_keep")
        conn.commit()
    finally:
        conn.close()
    report3, states3 = _run(phase2_s3k.judge_m6a_certification_coverage, ws)
    assert states3.get("M6a(REAL)") == STATE_FAIL
    assert "没有 cert_ledger" in report3.printed


def test_mu21_a_row_the_ledger_does_not_witness_is_pending(tmp_path):
    """The criterion's own behaviour: the keep table must not rescue a
    row the ledger does not witness."""
    ws = tmp_path / "keep-rescue"
    _make_library(ws, "TEST", text_state="projection", body_origin="file",
                  with_ledger=True, with_body_keep=True)
    # the ledger EXISTS but holds no entry for this row
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute("DELETE FROM cert_ledger")
        conn.commit()
    finally:
        conn.close()
    report, states = _run(phase2_s3k.judge_m6a_certification_coverage, ws)
    assert states.get("M6a(REAL)") == STATE_FAIL, report.printed
    assert "未认证" in report.printed
    assert "id-TEST" in report.printed


# ---------------------------------------------------------------------------
# MU-22: a missing ledger is a FAIL, not a NOTRUN
# ---------------------------------------------------------------------------

def test_mu22_a_missing_ledger_is_a_fail_not_a_notrun(tmp_path):
    """MU-22: "no ledger" means "nothing was ever certified".

    That is the STRONGEST possible form of the answer, not an inability
    to measure.  A NOTRUN here would be a silent green on the exact
    question the criterion exists to ask.

    The mutation is made on the DATA: the ledger table is DROPPED.
    """
    ws = tmp_path / "no-ledger"
    _make_library(ws, "TEST", text_state="projection", body_origin="file",
                  with_ledger=True)
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute("DROP TABLE cert_ledger")
        conn.commit()
    finally:
        conn.close()

    report, states = _run(phase2_s3k.judge_m6a_certification_coverage, ws)
    assert states.get("M6a(REAL)") == STATE_FAIL, report.printed
    assert "没有 cert_ledger" in report.printed
    assert "一行都没被认证过" in report.printed

    # --- AFTER the restore: the ledger is back and the row is certified
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute("""
            CREATE TABLE cert_ledger (
                id TEXT PRIMARY KEY, text_state TEXT, body_origin TEXT,
                body_path TEXT, body_hash TEXT, certified_at INTEGER,
                certified_by TEXT)""")
        conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin, "
            "body_path, body_hash, certified_at, certified_by) "
            "VALUES (?,?,?,?,?,?,?)",
            ("id-TEST", "mirror", "file", "MEMORY.md",
             "sha256:" + "0" * 64, 1000, "certify:promoted"))
        conn.execute("UPDATE chunks SET text_state='mirror' WHERE id='id-TEST'")
        conn.commit()
    finally:
        conn.close()
    report2, states2 = _run(phase2_s3k.judge_m6a_certification_coverage, ws)
    assert states2.get("M6a(REAL)") == STATE_PASS, report2.printed


# ---------------------------------------------------------------------------
# MU-23: a real downgrade is caught; a refused row is not one
# ---------------------------------------------------------------------------

def test_mu23_a_real_downgrade_is_caught(tmp_path):
    """MU-23: the ledger says 'mirror' and the row says 'projection' ⇒
    a PROVEN downgrade.

    The row identifier and BOTH values must be reported.
    """
    ws = tmp_path / "real-downgrade"
    _make_library(ws, "TEST", text_state="projection", body_origin="file",
                  with_ledger=True, ledger_state="mirror")

    report, states = _run(phase2_s3k.judge_m6b_downgrade, ws)
    assert states.get("M6b(REAL)") == STATE_FAIL, report.printed
    assert "id-TEST" in report.printed
    assert "认证时=mirror" in report.printed
    assert "现在=projection" in report.printed

    # --- AFTER the restore: the row agrees with the ledger again
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute("UPDATE chunks SET text_state='mirror' WHERE id='id-TEST'")
        conn.commit()
    finally:
        conn.close()
    report2, states2 = _run(phase2_s3k.judge_m6b_downgrade, ws)
    assert states2.get("M6b(REAL)") == STATE_PASS, report2.printed


def test_mu23_a_refused_row_is_not_a_downgrade(tmp_path):
    """The complement: a REFUSED (orphan) row was never certified, so it
    has no certification record and cannot have been downgraded.

    Counting it would turn "the certifier honestly refused" into "the
    product is broken".
    """
    ws = tmp_path / "refused"
    _make_library(ws, "TEST", text_state="mirror", body_origin="file",
                  with_ledger=True)
    _add_refused_row(ws)

    report, states = _run(phase2_s3k.judge_m6b_downgrade, ws)
    assert states.get("M6b(REAL)") == STATE_PASS, report.printed
    assert "降级=0" in report.printed
    # and the orphan count is reported, so a reader can see the two are
    # distinct facts
    assert "孤儿行=1" in report.printed


# ---------------------------------------------------------------------------
# MU-24: a mirror row with no witness
# ---------------------------------------------------------------------------

def test_mu24_every_mirror_row_must_have_a_witness(tmp_path):
    """MU-24: a 'mirror' row with no ledger entry claims to be certified
    with nobody able to say when, or against what.

    MEASURED on the live ROOT library: 15 such rows exist, and neither
    the certifier nor the backfill can repair them (the certifier only
    looks at rows whose state is NOT 'mirror'; the backfill only writes
    rows absent from the ledger, seeding them with the current state).

    The mutation is made on the DATA: the ledger entry is DELETED while
    the row keeps claiming "mirror".
    """
    ws = tmp_path / "unwitnessed"
    _make_library(ws, "TEST", text_state="mirror", body_origin="file",
                  with_ledger=True, with_body_keep=True)

    # --- BEFORE the mutation: the row is witnessed ⇒ M6c is green
    report, states = _run(phase2_s3k.judge_m6c_refused_rows_are_registered, ws)
    assert states.get("M6c(REAL)") == STATE_PASS, report.printed

    # --- the mutation: the witness is gone
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute("DELETE FROM cert_ledger")
        conn.commit()
    finally:
        conn.close()

    report2, states2 = _run(
        phase2_s3k.judge_m6c_refused_rows_are_registered, ws)
    assert states2.get("M6c(REAL)") == STATE_FAIL, report2.printed
    assert "无证人的 mirror 行" in report2.printed
    assert "id-TEST" in report2.printed

    # --- AFTER the restore: the witness is back
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin, "
            "body_path, body_hash, certified_at, certified_by) "
            "VALUES (?,?,?,?,?,?,?)",
            ("id-TEST", "mirror", "file", "MEMORY.md",
             "sha256:" + "0" * 64, 1000, "certify:promoted"))
        conn.commit()
    finally:
        conn.close()
    report3, states3 = _run(
        phase2_s3k.judge_m6c_refused_rows_are_registered, ws)
    assert states3.get("M6c(REAL)") == STATE_PASS, report3.printed


# ---------------------------------------------------------------------------
# MU-25: the ground truth is a re-chunk, not the chunk row count
# ---------------------------------------------------------------------------

def test_mu25_a_changed_file_is_stale_not_a_mismatch(tmp_path):
    """MU-25: comparing against the CHUNK ROW COUNT is a false red.

    The chunk rows are the projection of the file as it was when it was
    last synced.  If the file has changed since, the counts legitimately
    differ.  MEASURED on the live corpus: this made the criterion red
    on files that had merely grown.

    The mutation is made on the DATA: the truth file grows after the
    bookkeeping row was written, and its chunk count grows with it.
    """
    ws = tmp_path / "stale"
    ws.mkdir(parents=True, exist_ok=True)
    truth = ws / "MEMORY.md"
    truth.write_text(_md(1), encoding="utf-8")     # -> 1 chunk
    _make_library(ws, "TEST", chunk_count=1, chunker_version=4)

    # --- BEFORE the mutation: the file matches the bookkeeping
    report, states = _run(phase2_s3k.judge_m6e_files_statistics_are_guarded, ws)
    assert states.get("M6e(REAL)") == STATE_PASS, report.printed
    assert "不符=0" in report.printed

    # --- the mutation: the truth file grows (1 chunk -> 5 chunks)
    truth.write_text(_md(20), encoding="utf-8")

    report2, states2 = _run(
        phase2_s3k.judge_m6e_files_statistics_are_guarded, ws)
    assert states2.get("M6e(REAL)") == STATE_PASS, report2.printed
    assert "记账滞后" in report2.printed, (
        "真源变了 ⇒ 记账滞后，这不是缺陷 —— 判据必须这样分")
    assert "不符=0" in report2.printed


def test_mu25_an_unchanged_file_with_a_wrong_count_is_a_mismatch(tmp_path):
    """The complement: the file did NOT change but the count is wrong ⇒
    a real defect."""
    ws = tmp_path / "mismatch"
    ws.mkdir(parents=True, exist_ok=True)
    truth = ws / "MEMORY.md"
    truth.write_text(_md(20), encoding="utf-8")    # -> 5 chunks
    _make_library(ws, "TEST", chunk_count=1, chunker_version=4)
    # The stored hash is the fixture's 'h', so the file looks changed.
    # Fix the bookkeeping so the file looks UNCHANGED: store the real
    # hash and the real mtime, and keep the WRONG count.
    import hashlib
    digest = hashlib.sha256(truth.read_bytes()).hexdigest()
    conn = sqlite3.connect(str(_db_of(ws)))
    try:
        conn.execute(
            "UPDATE files SET hash=?, mtime=? WHERE path='MEMORY.md'",
            (digest, int(truth.stat().st_mtime)))
        conn.commit()
    finally:
        conn.close()

    report, states = _run(phase2_s3k.judge_m6e_files_statistics_are_guarded, ws)
    assert states.get("M6e(REAL)") == STATE_FAIL, report.printed
    assert "统计列不符" in report.printed
    assert "MEMORY.md" in report.printed


# ---------------------------------------------------------------------------
# MU-26: a NULL statistic that was never measured is not an erasure
# ---------------------------------------------------------------------------

def test_mu26_a_never_measured_null_is_not_an_erasure(tmp_path):
    """MU-26: the chunks side decides whether a NULL is an erasure.

    A file that was never synced through the current chunker has no
    decided rows and no v4 anchors, so its statistics were never
    measured.  MEASURED: all four live NULL statistics are of this
    kind.
    """
    ws = tmp_path / "never-measured"
    ws.mkdir(parents=True, exist_ok=True)
    truth = ws / "MEMORY.md"
    truth.write_text("\n".join("line %d" % i for i in range(1, 40)),
                     encoding="utf-8")
    lib = _make_library(ws, "TEST", chunk_count=None, chunker_version=None)
    # the chunks side of this path has NO decided rows and NO v4 anchor
    conn = sqlite3.connect(str(lib.db))
    try:
        conn.execute("UPDATE chunks SET body_origin='unresolved', "
                     "body_path=NULL, body_anchor=NULL WHERE id='id-TEST'")
        conn.commit()
    finally:
        conn.close()

    report, states = _run(phase2_s3k.judge_m6e_files_statistics_are_guarded, ws)
    assert states.get("M6e(REAL)") == STATE_PASS, report.printed
    assert "从未测量" in report.printed


def test_mu26_a_migrated_path_with_a_null_statistic_is_an_erasure(tmp_path):
    """The complement: the file WAS migrated (the chunks side has a
    decided row and a v4 anchor) but its statistic is NULL ⇒ erased."""
    ws = tmp_path / "erased"
    ws.mkdir(parents=True, exist_ok=True)
    truth = ws / "MEMORY.md"
    truth.write_text("\n".join("line %d" % i for i in range(1, 40)),
                     encoding="utf-8")
    lib = _make_library(ws, "TEST", chunk_count=None, chunker_version=None)
    # the chunks side says this file WAS migrated: decided origin + v4
    conn = sqlite3.connect(str(lib.db))
    try:
        conn.execute(
            "UPDATE chunks SET body_origin='file', body_path='MEMORY.md', "
            "body_anchor='L1-3+S1-3' WHERE id='id-TEST'")
        conn.commit()
    finally:
        conn.close()

    report, states = _run(phase2_s3k.judge_m6e_files_statistics_are_guarded, ws)
    assert states.get("M6e(REAL)") == STATE_FAIL, report.printed
    assert "统计列被擦" in report.printed
    assert "MEMORY.md" in report.printed


# ---------------------------------------------------------------------------
# the replay registry
# ---------------------------------------------------------------------------

def test_the_s3k_mutants_are_registered():
    """The replay must be REGISTERED — a mutant nobody replays is a
    mutant nobody knows about.

    The registry lives in the guard module (it is the one the gate's
    MU-REPLAY criterion reads), so the S3-K mutants are added to it.
    """
    import agent.memory.acceptance.phase2_guards as guards
    src = Path(guards.__file__).read_text(encoding="utf-8")
    for mu in ("MU-21", "MU-22", "MU-23", "MU-24", "MU-25", "MU-26"):
        assert mu in src, (
            "变异 %s 没有登记 —— 没人重放的变异就是没人知道的不变量" % mu)
        assert mu in Path(__file__).read_text(encoding="utf-8"), (
            "变异 %s 没有在重放测试里出现" % mu)
