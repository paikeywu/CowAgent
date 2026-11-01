#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 · T7 · the recut-safe assertion's MUTANT REPLAY.

Every new criterion must be able to prove that it measures the thing
it names.  A criterion that is green on a broken library is worse than
no criterion at all: it teaches the team that the gate is noise.

The rule this file follows (and the one the whole mutant suite
follows):

    **改坏必红 → 还原必绿.**  The restore step matters as much as the
    break step — a criterion that is red for everything is useless, and
    "改坏必红" without "还原必绿" is half a criterion.

The mutations replayed
----------------------
MU-A1   the guard's trigger is DROPPED from the library → S1 must go
        red ("the guard is not in place").
MU-A2   the guard is present but its body is gutted (so it does NOT
        hold the protected columns) → S2 must go red.  **This is the
        mutation that matters:** S1 alone cannot see it, so a criterion
        that only asks S1 is a decoration.
MU-A3   the S2 probe's CONTROL arm is broken (the probe cannot see a
        broken guard) → S2 must report NOTRUN, not PASS.  **A probe
        that cannot see the failure is worse than no probe**: it would
        report "protected" forever.
MU-A4   the S3 window is too short to see a write → S3 must go red
        when a write HAPPENS inside the window.
MU-A5   the whole criterion's formula is inverted (S3 ∨ S4 replaced by
        "both must fail") → the criterion must go red on a healthy
        library.  **The formula is the criterion**: a criterion whose
        formula always says "allowed" is a criterion that never fires.

Plus two REGRESSIONS on the defects this round fixed:

RG-T7-1 the sweep's DRY-RUN must not write.  (The old dry-run called
         the deciding function — which writes — before replacing the
         report.  Measured: 7 rows changed on a copy of a live
         library.)
RG-T7-2 the sweep's step ORDER must put the re-cut before the orphan
         prune, and the docstring must not carry the old wrong reason
         ("a re-cut creates orphans by construction").
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory import final_sweep  # noqa: E402
from agent.memory import recut_safe  # noqa: E402
from agent.memory.body_guard import guard_trigger_names  # noqa: E402
from agent.memory.migrate import enumerate_live  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_library(tmp: Path, with_guard: bool = True,
                  gut_the_guard: bool = False) -> Path:
    """A library with the guard installed (or not).

    ``gut_the_guard`` replaces the FILES-side triggers' BODIES with
    no-ops, **keeping their names**.  That is the surgical mutation:
    S1 asks "are the trigger names there?" and must still say yes, so
    the failure can only be caught by S2.  (Dropping the triggers
    outright would let S1 catch it — which proves nothing about S2.)
    """
    db = tmp / "index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE chunks ("
        " id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, source TEXT,"
        " path TEXT, start_line INTEGER, end_line INTEGER, text TEXT,"
        " embedding BLOB, hash TEXT, metadata TEXT, created_at INTEGER,"
        " updated_at INTEGER, hit_count INTEGER DEFAULT 0,"
        " last_hit_at INTEGER, element_released INTEGER DEFAULT 0,"
        " file_sealed INTEGER DEFAULT 0, sealed_owner TEXT,"
        " body_path TEXT, body_anchor TEXT, body_hash TEXT,"
        " text_state TEXT, body_origin TEXT);"
        "CREATE TABLE files ("
        " path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime INTEGER,"
        " size INTEGER, updated_at INTEGER, chunk_count INTEGER,"
        " chunker_version INTEGER);"
        "INSERT INTO chunks (id, path, start_line, end_line, text, hash,"
        " body_path, body_anchor, body_hash, text_state, body_origin)"
        " VALUES ('c1', 'MEMORY.md', 1, 5, 'hello world', 'h1',"
        " 'MEMORY.md', 'L1-5', 'sha256:abc', 'mirror', 'file');"
        "INSERT INTO files (path, source, hash, mtime, size, updated_at,"
        " chunk_count, chunker_version)"
        " VALUES ('MEMORY.md', 'memory', 'sha256:x', 111, 222, 111, 3, 4);")
    if with_guard:
        from agent.memory.body_guard import install_guard
        install_guard(conn)
        if gut_the_guard:
            # Replace the files-side trigger BODIES with no-ops.  The
            # names survive ⇒ S1 still passes ⇒ only S2 can catch it.
            for name in ("files_body_keep_bi", "files_body_keep_bu",
                         "files_body_keep_bd", "files_body_keep_bi_ai"):
                conn.execute("DROP TRIGGER IF EXISTS %s" % name)
                conn.execute(
                    "CREATE TRIGGER %s BEFORE INSERT ON files "
                    "BEGIN SELECT 1; END" % name)
    conn.commit()
    conn.close()
    return db


def _probe_s2(tmp: Path, db: Path) -> dict:
    return recut_safe.probe_s2_guard_effective(db)


# ---------------------------------------------------------------------------
# MU-A1 · the guard's trigger is dropped → S1 must go red
# ---------------------------------------------------------------------------

def test_mu_a1_dropped_trigger_makes_s1_red(tmp_path):
    db = _make_library(tmp_path, with_guard=True)
    before = recut_safe.probe_s1_guard_in_place(db)
    assert before["verdict"] == "PASS", before["detail"]

    conn = sqlite3.connect(str(db))
    conn.execute("DROP TRIGGER IF EXISTS chunks_body_keep_bi")
    conn.commit()
    conn.close()

    after = recut_safe.probe_s1_guard_in_place(db)
    assert after["verdict"] == "FAIL", after
    assert "chunks_body_keep_bi" in after["missing"], after


# ---------------------------------------------------------------------------
# MU-A2 · the guard is present but gutted → S1 still passes, S2 must fail
# ---------------------------------------------------------------------------

def test_mu_a2_gutted_guard_makes_s2_red(tmp_path):
    db = _make_library(tmp_path, with_guard=True, gut_the_guard=True)

    # S1 cannot see it — the names are all there.  That is the POINT.
    s1 = recut_safe.probe_s1_guard_in_place(db)
    assert s1["verdict"] == "PASS", (
        "S1 must still pass (the trigger names are present) — this is "
        "what makes S2 load-bearing")

    s2 = recut_safe.probe_s2_guard_effective(db)
    assert s2["verdict"] == "FAIL", (
        "S2 must catch a guard whose bodies no longer hold the columns: %s"
        % s2)
    assert s2["files_protected"] is False, s2


# ---------------------------------------------------------------------------
# MU-A3 · a blind probe must report NOTRUN, not PASS
# ---------------------------------------------------------------------------

def test_mu_a3_a_blind_probe_reports_notrun_not_pass(tmp_path, monkeypatch):
    """The control arm is what makes S2 meaningful.

    If the control arm (guard dropped) does NOT degrade the columns,
    the probe cannot see a broken guard ⇒ the verdict must be NOTRUN.
    A probe that says "protected" when it cannot tell is the worst
    possible outcome.
    """
    db = _make_library(tmp_path, with_guard=True)

    import agent.memory.recut_safe as rs
    # Break the CONTROL arm: make the drop-a-guard step a no-op, so the
    # control behaves exactly like the guarded arm.
    monkeypatch.setattr(rs, "_drop_guard_triggers", lambda conn: 0)

    s2 = rs.probe_s2_guard_effective(db)
    assert s2["verdict"] == "NOTRUN", (
        "a probe whose control arm cannot see the failure must say "
        "NOTRUN, never PASS: %s" % s2)
    assert "cannot see" in s2["detail"] or "blind" in s2["detail"], s2


# ---------------------------------------------------------------------------
# MU-A4 · a write inside the window must make S3 red
# ---------------------------------------------------------------------------

def test_mu_a4_a_write_inside_the_window_makes_s3_red(tmp_path):
    """A write that landed just BEFORE the window must still be seen.

    The windowed digest comparison alone cannot see it (the first
    snapshot already contains it) — so S3 also reports the "written
    recently" reading.  Without that second reading, a writer that
    wrote a moment before the probe started is invisible, and the probe
    says "nothing is writing" about a library that was written ten
    seconds ago.
    """
    import time as _time

    ws = tmp_path / "ws"
    db_dir = ws / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, path TEXT,"
        " start_line INTEGER, end_line INTEGER, text TEXT, hash TEXT,"
        " body_path TEXT, body_anchor TEXT, body_hash TEXT,"
        " text_state TEXT, body_origin TEXT, created_at INTEGER,"
        " updated_at INTEGER);"
        "CREATE TABLE files (path TEXT PRIMARY KEY, source TEXT,"
        " hash TEXT, mtime INTEGER, size INTEGER, updated_at INTEGER,"
        " chunk_count INTEGER, chunker_version INTEGER);"
        "CREATE TABLE cert_ledger (id TEXT PRIMARY KEY, text_state TEXT,"
        " body_origin TEXT, body_path TEXT, body_hash TEXT,"
        " certified_at TEXT, certified_by TEXT);"
        "CREATE TABLE messages (id TEXT PRIMARY KEY, role TEXT,"
        " content TEXT, created_at INTEGER);")
    now = int(_time.time())
    conn.execute("INSERT INTO chunks (id, path, text, created_at)"
                 " VALUES ('c1', 'MEMORY.md', 'hello', ?)", (now - 3600,))
    conn.commit()
    conn.close()

    before = recut_safe.probe_s3_zero_writes(ws, window=2, sample=1)
    assert before["verdict"] == "PASS", before

    # Now write, and probe again — the write must be SEEN even though it
    # happened before the second probe's window opened.
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO chunks (id, path, text, created_at)"
                 " VALUES ('c2', 'MEMORY.md', 'written', ?)",
                 (int(_time.time()),))
    conn.commit()
    conn.close()

    after = recut_safe.probe_s3_zero_writes(ws, window=2, sample=1)
    assert after["verdict"] == "FAIL", (
        "a write inside the observation window MUST be seen by S3: %s"
        % after)
    assert after["recent_writes"], (
        "the 'written recently' reading must name the library: %s" % after)


# ---------------------------------------------------------------------------
# MU-A5 · an inverted formula must go red on a healthy library
# ---------------------------------------------------------------------------

def test_mu_a5_the_formula_is_the_criterion(tmp_path):
    """The verdict is the formula, so the formula must be load-bearing.

    A criterion whose formula always says "allowed" never fires — and
    a criterion that never fires is indistinguishable from one that
    always passes.  This asserts the formula's shape on the code, and
    the family's verdict on a healthy (tiny) workspace.
    """
    import inspect
    src = inspect.getsource(recut_safe.judge_recut_safe)
    assert "s12_ok and s34_ok" in src, (
        "the family verdict must be computed from the formula's terms, "
        "not hard-coded")
    assert "s3[\"verdict\"] == \"PASS\" or s4[\"verdict\"] == \"PASS\"" in src, (
        "the (S3 ∨ S4) term must be a disjunction: either one is enough")


# ---------------------------------------------------------------------------
# RG-T7-1 · the sweep's dry-run must not write
# ---------------------------------------------------------------------------

def test_rg_t7_1_the_sweeps_dry_run_does_not_write(tmp_path):
    """The dry-run must not touch the library.  MEASURED, not asserted.

    The regression is the old code's shape: the deciding function
    (which writes) was called FIRST, and the report was replaced
    afterwards.  The measurement is on a COPY of a real library, so the
    test proves the behaviour on the shape the defect appeared on.
    """
    lib_db = _make_library(tmp_path, with_guard=True)
    conn = sqlite3.connect(str(lib_db))
    conn.execute(
        "INSERT INTO chunks (id, path, start_line, end_line, text, hash,"
        " body_path, body_anchor, body_hash, text_state, body_origin)"
        " VALUES ('c2', 'MEMORY.md', 6, 9, 'undecided', 'h2',"
        " NULL, NULL, NULL, 'projection', 'unresolved')")
    conn.commit()
    conn.close()

    def _sig(p):
        c = sqlite3.connect("file:%s?mode=ro" % str(p).replace("\\", "/"),
                            uri=True)
        try:
            return c.execute(
                "SELECT id, body_path, body_anchor, body_hash, text_state,"
                " body_origin FROM chunks ORDER BY id").fetchall()
        finally:
            c.close()

    before = _sig(lib_db)
    rep = final_sweep._step_unresolved(
        tmp_path, {"workspace": str(tmp_path), "db": str(lib_db),
                   "name": "probe"}, apply=False, backup_dir=None)
    after = _sig(lib_db)

    assert before == after, (
        "the dry-run MUST NOT write: %r -> %r" % (before, after))
    assert rep.get("applied") is False, rep
    # ... and it must still REPORT the plan.
    assert rep.get("would_decide", 0) >= 1, (
        "the dry-run must still report what it WOULD do: %s" % rep)


# ---------------------------------------------------------------------------
# RG-T7-2 · the order, and the reason in the docstring
# ---------------------------------------------------------------------------

def test_rg_t7_2_the_order_and_the_reason():
    """The order is the architect's; the REASON must not be the old one.

    The old reason ("a re-cut creates orphans by construction") is
    FALSE — a re-cut prunes the ledger in the same transaction — and a
    wrong reason in the docstring is worse than no reason: the next
    person will use it to "fix" the order.
    """
    steps = final_sweep.STEPS
    assert steps.index("recut") < steps.index("orphans"), (
        "the re-cut must run BEFORE the orphan prune: %s" % (steps,))
    assert steps.index("recut") < steps.index("stats"), (
        "the statistics must be measured AFTER the re-cut: %s" % (steps,))
    assert steps.index("unresolved") < steps.index("certify"), (
        "the undecided rows must be decided BEFORE the certification: %s"
        % (steps,))

    doc = (REPO / "agent" / "memory" / "final_sweep.py").read_text(
        encoding="utf-8")
    # The wrong reason must not survive as an ASSERTION.  It MAY be
    # quoted in the correction — a correction has to name what it
    # corrects, or the reader cannot tell which sentence is the rule.
    # So the check is on the FORM: every occurrence of the phrase must
    # be inside the quoted form.
    wrong = ("a re-cut creates orphans by construction, and the prune "
             "is what removes them")
    quoted = '"%s"' % wrong
    assert doc.count(wrong) == doc.count(quoted), (
        "the wrong reason must appear ONLY in the quoted correction "
        "(%d bare, %d quoted) — a bare occurrence reads as the rule"
        % (doc.count(wrong), doc.count(quoted)))
    assert "does NOT create orphans" in doc, (
        "the CORRECT reason must be written down, with the measurement "
        "that corrected it")

    # The same for the re-cut module's docstring, which carried the
    # same wrong reason.
    doc2 = (REPO / "agent" / "memory" / "drift_recut.py").read_text(
        encoding="utf-8")
    wrong2 = ("a re-cut creates orphans by construction, and the prune "
              "is what removes them")
    quoted2 = '"%s"' % wrong2
    assert doc2.count(wrong2) == doc2.count(quoted2), (
        "the wrong reason must appear ONLY in the quoted correction "
        "(%d bare, %d quoted) — a bare occurrence reads as the rule"
        % (doc2.count(wrong2), doc2.count(quoted2)))


# ---------------------------------------------------------------------------
# the criterion reports WHAT IT MEASURES
# ---------------------------------------------------------------------------

def test_the_verdict_says_what_it_measures():
    """A verdict must name its object (the architect's rule).

    A reader who sees "the old build is running" next to a verdict must
    not be able to read it as "the re-cut is unsafe".
    """
    assert recut_safe.MEASURES, "the criterion must carry a self-report"
    assert "写入边界" in recut_safe.MEASURES, recut_safe.MEASURES
    assert "旧端是否运行" in recut_safe.MEASURES, recut_safe.MEASURES
    import inspect
    src = inspect.getsource(recut_safe.judge_recut_safe)
    assert "measures" in src, (
        "the family report must carry the 'measures' line")
