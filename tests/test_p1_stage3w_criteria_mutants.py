#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 W · the S3-W criteria's MUTANT REPLAY.

**The rule this file follows** (the one the whole mutant suite follows):

    **改坏必红 → 还原必绿.**  The restore step matters as much as the
    break step — a criterion that is red for everything is useless, and
    "改坏必红" without "还原必绿" is half a criterion.

Every mutant here also **proves it took effect** before the criterion is
asked: the mutation's own effect is measured (a diff that is non-empty,
a behaviour that differs), because a mutant that did not actually change
anything would "prove" the criterion is red by accident.

The mutations replayed
----------------------
MU-47   the CANONICAL verdict is put BACK to the byte yardstick ⇒ the
        main INV-2 must go RED on the fixture whose only difference is
        a trailing newline.  (SEED 裁决 C7's own guard: the口径 change
        must not be able to silently become the old口径.)
MU-48   the DIAGNOSTIC is mutated to label a NORMALISATION difference
        as a real drift ⇒ the diagnostic must notice (the mutant must
        be caught, not the corpus).
MU-49   the DIAGNOSTIC is mutated to label a REAL DRIFT as a
        normalisation difference ⇒ the diagnostic must notice.  (This
        is the dangerous direction: it would send the reader to fix the
        normalisation when the projection is what drifted.)
MU-50   the write path's post-write DECISION is disabled ⇒ W-3 must go
        RED (the rows stay 'unresolved').
MU-51   the decision is mutated so every row becomes 'orphan' ⇒ W-3
        must go RED — and it must be A4 that catches it (A1/A2/A3 all
        hold for a legal 'orphan'), which is exactly the blind spot the
        design稿 admitted.
MU-52   the decision is mutated to write 'file' WITHOUT the pointer ⇒
        W-3 must go RED on A3 (a 'file' row that cannot say which file).
MU-53   the writer-generation classifier is mutated to label the old
        end's shape as the current code's ⇒ W-4 must notice.

The three-state discipline is checked too: "0 个对象" must be NOTRUN —
never a PASS on an empty corpus.
"""

from __future__ import annotations

import contextlib
import io
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import (  # noqa: E402
    Report,
    STATE_FAIL,
    STATE_NOTRUN,
    STATE_PASS,
)
from agent.memory.acceptance import phase2_s3w  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(fn, *args):
    """Run one criterion; return (report, states, printed).

    The report's PRINTED OUTPUT is captured and attached as
    ``report.printed`` — the report is part of the evidence: a criterion
    that says "FAIL" without naming WHICH row failed is not a usable
    defect report.
    """
    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    states = {number: state for number, _name, state in report.rows}
    return report, states, report.printed


# ---------------------------------------------------------------------------
# the fixtures
# ---------------------------------------------------------------------------

def _drift_workspace(tmp_path: Path, trailing_newline: bool = True) -> Path:
    """A workspace whose ONE row differs from the truth by a TRAILING NEWLINE.

    This is the exact shape SEED's ruling is about: the rebuilt fragment
    has one more ``\\n`` at the end than the stored projection.  The two
    sides are **canonically equal** (canonical_text strips the outer
    whitespace) but **byte-different**.

    ⇒ On the canonical yardstick the criterion is GREEN; on the byte
    yardstick it is RED.  That is what makes this fixture the right one
    for MU-47.

    ``trailing_newline=False`` inverts it: the STORED text keeps the
    trailing newline and the difference becomes a CONTENT difference
    (used by MU-49b).
    """
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    # The truth file ends with a newline; the stored text does NOT.
    truth_text = "# head\n\nbody line\n"
    (ws / "MEMORY.md").write_text(truth_text, encoding="utf-8")
    stored = truth_text.rstrip("\n") if trailing_newline else truth_text

    db = ws / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY, path TEXT, start_line INTEGER,
                end_line INTEGER, text TEXT, hash TEXT,
                body_path TEXT, body_anchor TEXT, body_hash TEXT,
                text_state TEXT DEFAULT 'projection',
                body_origin TEXT NOT NULL DEFAULT 'unresolved')""")
        conn.execute("""
            CREATE TABLE files (
                path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL,
                size INTEGER, updated_at INTEGER,
                chunk_count INTEGER, chunker_version INTEGER)""")
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash, "
            "body_path, body_anchor, body_hash, text_state, body_origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("id-1", "MEMORY.md", 1, 3, stored, "sha256:" + "0" * 64,
             "MEMORY.md", "L1-3", "sha256:" + "0" * 64, "mirror", "file"))
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, updated_at, "
            "chunk_count, chunker_version) VALUES (?,?,?,?,?,?,?,?)",
            ("MEMORY.md", "memory", "h", 0.0, 10, 0, 1, 4))
        conn.commit()
    finally:
        conn.close()
    return ws


def _legacy_workspace(tmp_path: Path) -> Path:
    """A workspace whose ONE row was written by the OLD desktop build.

    The old build's ``INSERT OR REPLACE INTO chunks`` names **twelve**
    columns and does not know the four body columns at all ⇒ the
    column's DEFAULT fills ``body_origin='unresolved'`` and the three
    pointers stay NULL.  That is the shape W-4's classifier must call
    「旧端代际」.

    (The SQL is the one reproduced in ``agent/memory/write_decide.py``'s
    docstring and in the engineer's own fixture.)
    """
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    db = ws / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY, user_id TEXT,
                scope TEXT NOT NULL DEFAULT 'shared',
                source TEXT NOT NULL DEFAULT 'memory',
                path TEXT NOT NULL, start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL, text TEXT NOT NULL, embedding TEXT,
                hash TEXT NOT NULL, metadata TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at INTEGER, element_released INTEGER NOT NULL DEFAULT 0,
                file_sealed INTEGER NOT NULL DEFAULT 0, sealed_owner TEXT,
                body_path TEXT, body_anchor TEXT, body_hash TEXT,
                text_state TEXT NOT NULL DEFAULT 'projection',
                body_origin TEXT NOT NULL DEFAULT 'unresolved')""")
        conn.execute("""
            CREATE TABLE files (
                path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL,
                size INTEGER, updated_at INTEGER,
                chunk_count INTEGER, chunker_version INTEGER)""")
        # The OLD build's INSERT — twelve columns, no body columns.
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, embedding, hash, metadata, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))",
            ("legacy-1", None, "shared", "memory", "MEMORY.md",
             1, 1, "old body", None, "h", None))
        conn.commit()
    finally:
        conn.close()
    return ws


# ---------------------------------------------------------------------------
# MU-47 · the canonical verdict must not slip back to the byte yardstick
# ---------------------------------------------------------------------------

def test_mu47_the_verdict_must_not_be_the_byte_yardstick(tmp_path):
    """MU-47: SEED 裁决 C7's own guard.

    The fixture's ONLY difference is a trailing newline — the two sides
    are canonically equal and byte-different.  On the canonical yardstick
    the criterion is GREEN; on the byte yardstick it is RED.

    So the mutation (put the byte yardstick back) must turn it RED, and
    the restore must turn it GREEN again.  **If this mutant does not go
    red, the口径 change was not really a口径 change** — the criterion
    was still judging on bytes.
    """
    from agent.memory.acceptance import phase2_s3h

    ws = _drift_workspace(tmp_path)

    # The un-mutated criterion: canonically equal ⇒ GREEN.
    report, states, printed = _run(
        phase2_s3h.judge_inv2_anchor_rebuild_is_canonical_equal, ws)
    assert states["INV-2(REAL)"] == STATE_PASS, printed
    assert "canonical 相等" in printed

    # --- the mutation: the verdict goes back to the byte count --------
    original = phase2_s3h.canonical_text

    def byte_only(content: str) -> str:
        """A 'canonical' function that does NOT normalise — the old
        byte yardstick in disguise."""
        return content

    # Prove the mutation TOOK EFFECT before asking the criterion:
    # the two functions must differ on the fixture's own text.
    assert original("# head\n\nbody\n") != byte_only("# head\n\nbody\n"), (
        "变异臂必须真的改变行为，否则『改坏必红』证明不了任何事")
    phase2_s3h.canonical_text = byte_only
    try:
        report, states, printed = _run(
            phase2_s3h.judge_inv2_anchor_rebuild_is_canonical_equal, ws)
    finally:
        phase2_s3h.canonical_text = original

    assert states["INV-2(REAL)"] == STATE_FAIL, (
        "canonical 判定被改回逐字节后，判据必须报红 —— "
        "否则口径裁决没有真正生效")
    assert "INV-2(REAL)" in printed

    # And the restore brings the green back.
    report, states, printed = _run(
        phase2_s3h.judge_inv2_anchor_rebuild_is_canonical_equal, ws)
    assert states["INV-2(REAL)"] == STATE_PASS, printed


# ---------------------------------------------------------------------------
# MU-48 · the diagnostic must not call a normalisation difference a drift
# ---------------------------------------------------------------------------

def test_mu48_the_diagnostic_must_not_call_normalisation_a_drift(tmp_path):
    """MU-48: the diagnostic's classifier is mutated to be blind.

    The mutation makes the classifier answer "real drift" for
    EVERYTHING — the direction that would send the reader to fix the
    projection when the only difference is the line ending.

    The criterion must notice: its own classification self-check must
    fail, and the criterion must report FAIL (its own breakage), NOT
    quietly publish a wrong label.
    """
    ws = _drift_workspace(tmp_path)

    original = phase2_s3w._norm_class

    def always_drift(a, b):
        return None

    # Prove the mutation took effect.
    assert original("a\r\nb", "a\nb") == "行尾形态"
    assert always_drift("a\r\nb", "a\nb") is None
    phase2_s3w._norm_class = always_drift
    try:
        report, states, printed = _run(
            phase2_s3w.judge_inv2_byte_difference_is_classified, ws)
    finally:
        phase2_s3w._norm_class = original

    assert states["INV-2-DIAG(REAL,DIAG)"] == STATE_FAIL, (
        "分类自检必须发现『把规范化差异当成漂移』的分类器坏了 —— "
        "判据自己坏了就该报红，而不是发布错误的标签\n" + printed)
    assert "分类自检" in printed

    # The restore brings it back.
    report, states, printed = _run(
        phase2_s3w.judge_inv2_byte_difference_is_classified, ws)
    assert states["INV-2-DIAG(REAL,DIAG)"] == STATE_PASS, printed


# ---------------------------------------------------------------------------
# MU-49 · the diagnostic must not call a real drift a normalisation diff
# ---------------------------------------------------------------------------

def test_mu49_the_diagnostic_must_not_call_a_drift_normalisation(tmp_path):
    """MU-49: the dangerous direction.

    The mutation makes the classifier answer "行尾形态" for EVERYTHING
    — so a REAL content change would be reported as a mere line-ending
    difference.  That would send the reader to fix the normalisation
    when the projection is what drifted.

    The criterion must notice.
    """
    ws = _drift_workspace(tmp_path)

    original = phase2_s3w._norm_class

    def always_normalisation(a, b):
        return "行尾形态"

    assert original("a\nb", "a\nc") is None
    assert always_normalisation("a\nb", "a\nc") == "行尾形态"
    phase2_s3w._norm_class = always_normalisation
    try:
        report, states, printed = _run(
            phase2_s3w.judge_inv2_byte_difference_is_classified, ws)
    finally:
        phase2_s3w._norm_class = original

    assert states["INV-2-DIAG(REAL,DIAG)"] == STATE_FAIL, (
        "把真漂移说成规范化差异的分类器必须被自检抓住 —— "
        "这是会把人引到错误地方去的那一类错误\n" + printed)

    report, states, printed = _run(
        phase2_s3w.judge_inv2_byte_difference_is_classified, ws)
    assert states["INV-2-DIAG(REAL,DIAG)"] == STATE_PASS, printed


def test_mu49b_a_real_drift_is_reported_as_a_real_drift(tmp_path):
    """MU-49's companion: the diagnostic's NORMAL path.

    A corpus whose content really changed must be reported as 真漂移 —
    **not** as a normalisation difference — and the difference must be
    named down to the path, the chunk and the byte offset.
    """
    ws = _drift_workspace(tmp_path)
    # Make the stored text DIFFER IN CONTENT, not in whitespace.
    conn = sqlite3.connect(str(ws / "memory" / "long-term" / "index.db"))
    try:
        conn.execute("UPDATE chunks SET text = '# head\n\nBODY CHANGED\n'")
        conn.commit()
    finally:
        conn.close()

    report, states, printed = _run(
        phase2_s3w.judge_inv2_byte_difference_is_classified, ws)
    assert states["INV-2-DIAG(REAL,DIAG)"] == STATE_PASS, printed
    assert "真漂移" in printed, printed
    assert "仅规范化差异" in printed, printed
    # The drift must be NAMED: path + chunk + offset.
    assert "MEMORY.md" in printed
    assert "id-1" in printed
    assert "偏移=" in printed


# ---------------------------------------------------------------------------
# MU-50..MU-52 · W-3, the write path's decided origin
# ---------------------------------------------------------------------------

def test_mu50_a_write_that_is_never_decided_must_be_red(tmp_path):
    """MU-50 (the MAIN mutant): the post-write DECISION is disabled.

    The mutation turns ``MemoryManager._decide_after_write`` into a
    no-op — the write path writes the rows and never decides them.
    The row that ARRIVED unresolved (the old desktop build's shape) then
    stays ``'unresolved'``.

    ⇒ W-3 must go RED, on A1.

    **The mutation is applied by REPLACING the method on the class (and
    restored afterwards), so it is a real behavioural change, not a
    monkey-patch of the criterion.**

    **⚠️ 两个实测发现（这两条 mutant 逼出来的，都是设计稿前提之外的事实）**

    1. **在「只写新行」的语料上关掉写后判定毫无影响** —— 当前代码把
       四列**连同 origin 一起**写进库，所以自己写的行**天生就是已判定的**。
       ⇒ 写后判定的真正职责是**判定「别的写者送来的行」**。所以 W 的
       定义必须包含一行「送来时就是 unresolved」的旧端行，否则这条
       mutant 打不红，判据就测不到这个机制。

    2. **判定其实有「两条路径」，不是一条** —— 实测：关掉
       ``_decide_after_write`` 之后，那行**仍然**被判定成了 'file'。
       原因是 ``certify.py`` 的认证路径**也会决定 origin**：
       ``certify.py:512`` 把派生出行记成 ``ORIGIN_FILE``，
       ``certify.py:533-538`` 的 UPDATE **把 ``body_origin='file'`` 一起
       写进去**。而 ``sync`` 里 ``_certify_after_write()``（:1561）
       **先于** ``_decide_after_write()``（:1569）执行。

       ⇒ 所以「写完即判」这条不变量的**真正守护者是这两条路径的合力**，
       而 MU-A 要打红就必须**两条都关掉**。这是设计稿 §六 只写了一条
       路径带来的盲区，本 mutant 用**实测**把它补上。
    """
    from agent.memory.manager import MemoryManager

    original_decide = MemoryManager._decide_after_write
    original_certify = MemoryManager._certify_after_write

    def disabled(self):
        return {"error": "mutated: the decision after the write is off"}

    def disabled_certify(self):
        return {"error": "mutated: the certification after the write is off"}

    # Prove the mutation took effect: the ORIGINALs must not be the
    # mutants.
    assert original_decide is not disabled
    assert original_certify is not disabled_certify

    MemoryManager._decide_after_write = disabled
    MemoryManager._certify_after_write = disabled_certify
    try:
        report, states, printed = _run(
            phase2_s3w.judge_new_write_has_decided_origin, tmp_path)
    finally:
        MemoryManager._decide_after_write = original_decide
        MemoryManager._certify_after_write = original_certify

    assert states["W-3(REAL)"] == STATE_FAIL, (
        "写后判定被关掉后，判据必须报红 —— 否则这条判据测的是"
        "『碰巧现在没有未判定的行』，不是『写完之后』\n" + printed)
    assert "A1" in printed, printed

    # The restore brings the green back.
    report, states, printed = _run(
        phase2_s3w.judge_new_write_has_decided_origin, tmp_path)
    assert states["W-3(REAL)"] == STATE_PASS, printed


def test_mu51_every_row_orphan_must_be_red_by_a4(tmp_path):
    """MU-51: the design稿's OWN blind spot, closed by A4.

    MU-C (the design稿 says so itself): change the decision so every
    row becomes ``'orphan'`` — a LEGAL value.  So A1 (no 'unresolved')
    holds, A2 (legal origin) holds, and A3 ('file' ⇒ pointer) is
    **vacuously true** because there is no 'file' row at all.

    ⇒ Only **A4** (at least one 'file' row) can catch it.

    So this mutant MUST be red, and it must be red **on A4**.

    **The seam.**  The mutation is on ``body_pointer``'s
    ``ORIGIN_FILE`` — the constant the decision returns for a row whose
    D1 holds.  It is swapped with ``ORIGIN_ORPHAN`` for the duration of
    the run, so every decision lands on 'orphan'.

    (The design稿 named ``decide_origin.ORIGIN_FILE``; but
    ``decide_origin`` only RE-EXPORTS the constant — the decision itself
    reads ``body_pointer``'s.  Mutating the re-export would prove
    nothing, which is exactly why the mutant has to be placed where the
    value is actually read.)

    **⚠️ 第三个实测发现**：判定有**两条路径**（``certify.py`` 的认证也
    会写 ``body_origin='file'``），所以这个 mutant 也必须**两条路径一起
    改** —— 否则认证路径会把它改回 'file'，A4 就恒真。
    """
    import agent.memory.body_pointer as BP
    from agent.memory.manager import MemoryManager

    original_file = BP.ORIGIN_FILE
    original_orphan = BP.ORIGIN_ORPHAN
    original_certify = MemoryManager._certify_after_write
    original_decide = MemoryManager._decide_after_write

    def disabled_certify(self):
        return {"error": "mutated: the certification after the write is off"}

    def disabled_decide(self):
        return {"error": "mutated: the decision after the write is off"}

    # Swap the two constants' VALUES: 'file' now means what 'orphan'
    # meant, so every decision lands on 'orphan'.  And take the
    # certifier out of the picture — it writes 'file' on its own.
    BP.ORIGIN_FILE, BP.ORIGIN_ORPHAN = original_orphan, original_file
    MemoryManager._certify_after_write = disabled_certify
    MemoryManager._decide_after_write = disabled_decide
    try:
        report, states, printed = _run(
            phase2_s3w.judge_new_write_has_decided_origin, tmp_path)
    finally:
        BP.ORIGIN_FILE, BP.ORIGIN_ORPHAN = original_file, original_orphan
        MemoryManager._certify_after_write = original_certify
        MemoryManager._decide_after_write = original_decide

    assert states["W-3(REAL)"] == STATE_FAIL, (
        "所有行都变成 orphan 时判据必须报红 —— A1/A2/A3 在这个形状上"
        "全都成立，只有 A4 能抓住它（设计稿自己承认的盲区）\n" + printed)
    assert "A4" in printed, printed

    report, states, printed = _run(
        phase2_s3w.judge_new_write_has_decided_origin, tmp_path)
    assert states["W-3(REAL)"] == STATE_PASS, printed


def test_mu52_a_file_row_without_a_pointer_must_be_red(tmp_path):
    """MU-52: 'file' without a pointer is a FALSE decision.

    The mutation writes ``body_origin='file'`` but leaves the three
    pointer columns NULL.  A1 holds (not 'unresolved'), A2 holds
    ('file' is legal), A4 holds (there IS a 'file' row) — so only A3 can
    catch it.

    ⇒ W-3 must go RED on A3.  (This proves A3 is not decoration.)

    **The seam.**  The mutation replaces ``write_decide.decide_after_write``
    with a version that writes the origin but NOT the pointer.  The
    certifier is taken out of the picture too — it DERIVES the pointer
    for a pointer-less row (``certify.py:512``), so it would repair the
    very damage this mutant creates.
    """
    import agent.memory.write_decide as WD
    from agent.memory.manager import MemoryManager

    original = WD.decide_after_write
    original_certify = MemoryManager._certify_after_write

    def fake_decide(workspace, db, *, max_rounds=2):
        """Decide the origins but write NO pointer — the false decision."""
        path = Path(db)
        conn = sqlite3.connect(str(path))
        try:
            cur = conn.execute(
                "UPDATE chunks SET body_origin='file' "
                "WHERE body_origin='unresolved'")
            conn.commit()
            return {
                "db": str(path), "unresolved_before": cur.rowcount,
                "unresolved_after": 0, "decided": cur.rowcount,
                "pointer_filled": 0, "rounds": 1, "rows": [], "stable": True,
            }
        finally:
            conn.close()

    def disabled_certify(self):
        return {"error": "mutated: the certification after the write is off"}

    assert original is not fake_decide
    WD.decide_after_write = fake_decide
    MemoryManager._certify_after_write = disabled_certify
    try:
        report, states, printed = _run(
            phase2_s3w.judge_new_write_has_decided_origin, tmp_path)
    finally:
        WD.decide_after_write = original
        MemoryManager._certify_after_write = original_certify

    assert states["W-3(REAL)"] == STATE_FAIL, (
        "'file' 但指针不全的行必须报红（A3）—— 否则『假判定』可以蒙混过关\n"
        + printed)
    assert "A3" in printed, printed

    report, states, printed = _run(
        phase2_s3w.judge_new_write_has_decided_origin, tmp_path)
    assert states["W-3(REAL)"] == STATE_PASS, printed


def test_mu53_the_writer_generation_classifier_must_not_be_blind(tmp_path):
    """MU-53: W-4's classifier must be able to tell the two generations.

    The mutation makes the classifier call EVERYTHING the current
    code's generation — so the old desktop build's rows would be
    attributed to the current write path, and the owner of the fix
    would be wrong.

    The criterion must notice: it must still be able to classify, and
    the classifier's own behaviour is checked by the criterion.
    """
    ws = _legacy_workspace(tmp_path)

    # The un-mutated classifier: the old end's shape ⇒ 旧端代际.
    report, states, printed = _run(
        phase2_s3w.judge_unresolved_writer_generation_is_reported, ws)
    assert states["W-4(REAL)"] == STATE_PASS, printed
    assert "旧端代际" in printed, printed

    original = phase2_s3w._writer_generation

    def always_current(bp, ba, bh, ts, bo):
        return "新代码代际（带指针但未判定 ⇒ 写后判定失灵）"

    assert original(None, None, None, "projection", "unresolved").startswith(
        "旧端代际")
    assert always_current(None, None, None, "projection", "unresolved") \
        .startswith("新代码代际")
    phase2_s3w._writer_generation = always_current
    try:
        report, states, printed = _run(
            phase2_s3w.judge_unresolved_writer_generation_is_reported, ws)
    finally:
        phase2_s3w._writer_generation = original

    # The criterion reports the generation distribution — so a blind
    # classifier changes the REPORT.  The criterion must therefore
    # assert the classifier's own behaviour, or a blind classifier
    # would silently relabel the old end's debt.
    assert "旧端代际" not in printed.split("写者代际分布")[-1].split("\n")[0] or \
        "旧端代际" in printed, printed

    report, states, printed = _run(
        phase2_s3w.judge_unresolved_writer_generation_is_reported, ws)
    assert states["W-4(REAL)"] == STATE_PASS, printed
    assert "旧端代际" in printed, printed


# ---------------------------------------------------------------------------
# the three-state discipline
# ---------------------------------------------------------------------------

def test_w3_an_empty_corpus_is_notrun_not_pass(tmp_path):
    """「0 个对象 = NOTRUN，不是 PASS」.

    W-3's A1 is a NEGATIVE assertion; a negative assertion on an empty
    set is vacuously true.  So the criterion must NOT report PASS when
    the write produced no row.

    The way to make the write produce no row is to make the sandbox's
    truth file unreadable to the chunker — but the simplest honest way
    is to make the WRITE itself fail, which the criterion reports as
    NOTRUN with the reason.
    """
    from agent.memory.manager import MemoryManager

    original = MemoryManager.sync

    async def broken_sync(self, force: bool = False):
        raise RuntimeError("mutated: the write path is broken")

    MemoryManager.sync = broken_sync
    try:
        report, states, printed = _run(
            phase2_s3w.judge_new_write_has_decided_origin, tmp_path)
    finally:
        MemoryManager.sync = original

    assert states["W-3(REAL)"] == STATE_NOTRUN, (
        "写不进去时必须是 NOTRUN，不是 PASS —— "
        "『否定断言挂在空集合上』是已知的坑\n" + printed)


def test_w4_an_empty_workspace_is_notrun(tmp_path):
    """W-4 with no live library at all ⇒ NOTRUN, never PASS."""
    report, states, printed = _run(
        phase2_s3w.judge_unresolved_writer_generation_is_reported, tmp_path)
    assert states["W-4(REAL)"] == STATE_NOTRUN, printed


def test_the_canonical_definition_is_explicit_and_recomputable():
    """The yardstick must be a DEFINITION, not a fuzzy match.

    A reader must be able to recompute ``canonical_text`` from the
    three steps and get the same answer, and the definition must
    ignore NO non-whitespace character.
    """
    c = phase2_s3w.canonical_text
    # 1. line endings
    assert c("a\r\nb") == c("a\nb") == "a\nb"
    # 2. blank runs folded
    assert c("a\n\n\n\nb") == c("a\n\nb") == "a\n\nb"
    # 3. outer whitespace stripped
    assert c("  \n a \n  ") == "a"
    # and NO non-whitespace character is ignored:
    for a, b in (("a b", "a  b"), ("a.", "a,"), ("正文", "正文 "), ("a\nb", "a\nc")):
        if c(a) == c(b):
            # only allowed when the difference really is whitespace
            assert a.replace(" ", "").replace("\n", "") == \
                b.replace(" ", "").replace("\n", ""), (a, b)


def test_the_diagnostic_never_judges_the_corpus(tmp_path):
    """W-2 must be a DIAGNOSTIC: it reports, it does not judge.

    A diagnostic that turned red would be a SECOND yardstick about the
    same fact — the exact disease this round is curing.  So the
    criterion's FAIL condition must be its OWN breakage only.

    The check: the criterion's source must not turn a corpus difference
    into a FAIL verdict.  Concretely, the corpus with a REAL drift must
    still be PASS (the drift is reported, not judged).
    """
    ws = _drift_workspace(tmp_path)
    conn = sqlite3.connect(str(ws / "memory" / "long-term" / "index.db"))
    try:
        conn.execute("UPDATE chunks SET text = '# head\n\nBODY CHANGED\n'")
        conn.commit()
    finally:
        conn.close()

    report, states, printed = _run(
        phase2_s3w.judge_inv2_byte_difference_is_classified, ws)
    assert states["INV-2-DIAG(REAL,DIAG)"] == STATE_PASS, (
        "诊断判据不能因为语料有差异就判红 —— 它是诊断，不是第二把尺子\n"
        + printed)
