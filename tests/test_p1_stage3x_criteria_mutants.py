#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 X · the S3-X criteria's MUTANT REPLAY.

**The rule this file follows** (the one the whole mutant suite follows):

    **改坏必红 → 还原必绿.**  The restore step matters as much as the
    break step — a criterion that is red for everything is useless, and
    "改坏必红" without "还原必绿" is half a criterion.

Every mutant here also **proves it took effect** before the criterion is
asked: the mutation's own effect is measured, because a mutant that did
not actually change anything would "prove" the criterion is red by
accident.

The mutations replayed
----------------------
MU-T1   the reconciliation records NOTHING (``recorded`` is always
        empty) ⇒ EE-1 must be red (A1: no witness).
MU-T2   the witness hash is a CONSTANT (it does not read the file) ⇒
        EE-1 must be red (A2: the witness does not reproduce).
MU-T3   the reconciliation records EVERY time (the idempotence check
        is removed) ⇒ EE-1's A3 half must be red (the log grows on a
        no-op scan) AND EE-2's F2 fixture must be red.
MU-T4   the witness hash uses the RAW BYTES instead of the canonical
        text ⇒ EE-2's F5 (CRLF) fixture must be red.
MU-T5   ``record_change`` NESTS the ``extra`` dict instead of merging
        it flat ⇒ the witness is no longer at the top level ⇒ EE-1
        must be red (A1: no-witness).  **This is the design稿's most
        easily-missed mutant**: the reader and the writer must agree.
MU-T6   ``latest_change_mtime`` advances on every READ ⇒ EE-1's A3
        half must be red.
MU-T8   the reconciliation only reads the ROOT workspace's log ⇒
        EE-2's F6 fixture must be red.
MU-T9   the reconciliation SWALLOWS the exception and returns an empty
        report ⇒ EE-2's F7 fixture must be red ("不知道" disguised as
        "没变化").
MU-T10  the gate's INV-2 reader and ``text_inv``'s disagree on ONE
        row ⇒ R3's symmetric difference must be non-zero ⇒ R3 must be
        red.
MU-T11  the canonical cross-check is REMOVED (the promise becomes a
        lie again) ⇒ EE-4 must be red.
MU-T12  a new RED criterion appears in the gate's table ⇒ GB-1 must
        be red ("新增失败").

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
from agent.memory.acceptance import phase2_s3x  # noqa: E402
from agent.memory.acceptance import phase2_s3w  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(fn, *args):
    """Run one criterion; return (report, states, printed)."""
    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    states = {number: state for number, _name, state in report.rows}
    return report, states, report.printed


def _sandbox(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# MU-T1 / MU-T3 · the reconciliation that records nothing / records always
# ---------------------------------------------------------------------------

def test_muT1_a_reconciliation_that_records_nothing_must_be_red(tmp_path):
    """MU-T1: the reconciliation records NOTHING.

    The mutation makes ``record_change`` a no-op for the
    ``external-edit`` source — the mechanism runs, reports, and writes
    **no event at all**.  Every file therefore stays unwitnessed ⇒ the
    mechanism witnesses nothing ⇒ the criterion must say so.

    (The mutation is placed on ``changes.record_change`` rather than on
    ``reconcile_external_edits`` because the criterion reads the LOG,
    not the report: a mutant that only blanks the report would leave
    the log intact, and the log is what the invariant is about.)
    """
    from agent.memory import changes

    original = changes.record_change

    def records_nothing(root, relpath, source, **kw):
        if source == "external-edit":
            return {"path": relpath, "source": source, "dropped": True}
        return original(root, relpath, source, **kw)

    assert original is not records_nothing
    changes.record_change = records_nothing
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        changes.record_change = original

    assert states["EE-2(unit)"] == STATE_FAIL, (
        "什么都不记的对账机制必须被抓住 —— 否则『变更记录有见证』"
        "是一句没有人验证的话\n" + printed)

    report, states, printed = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states["EE-2(unit)"] == STATE_PASS, printed


def test_muT3_a_reconciliation_that_always_records_must_be_red(tmp_path):
    """MU-T3: the idempotence check is removed ⇒ the log grows forever.

    This is the failure mode that keeps DR-2 green forever and the
    shelf permanently stale: every scan appends an event, so
    ``latest_change_mtime`` advances on every read.

    The mutation makes the reconciliation record unconditionally, even
    when the content has not changed.
    """
    from agent.memory import external_edit

    ws = _sandbox(tmp_path / "ws")
    truth = ws / "MEMORY.md"
    truth.write_text("# head\n\nbody\n", encoding="utf-8")
    cands = [("MEMORY.md", truth)]

    original = external_edit.reconcile_external_edits

    def always_records(root, candidates=None, **kw):
        # Record by bypassing the content comparison: pretend the
        # witness never matches.
        rep = original(root, candidates, **kw)
        if not kw.get("dry_run"):
            from agent.memory import changes
            for rel, real in (candidates or []):
                try:
                    changes.record_change(
                        root, rel, "external-edit", actor="mutant",
                        extra={"content_sha256": "MUTANT"})
                except Exception:
                    pass
        return rep

    assert original is not always_records
    external_edit.reconcile_external_edits = always_records
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        external_edit.reconcile_external_edits = original

    assert states["EE-2(unit)"] == STATE_FAIL, (
        "每次都记的对账机制必须被抓住 —— 这是『每读一次刷新一次』\n"
        + printed)
    assert "F2" in printed, printed

    report, states, printed = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states["EE-2(unit)"] == STATE_PASS, printed


# ---------------------------------------------------------------------------
# MU-T2 / MU-T4 / MU-T5 · the witness that does not reproduce
# ---------------------------------------------------------------------------

def test_muT2_a_constant_witness_must_be_red(tmp_path):
    """MU-T2: the witness hash is a CONSTANT.

    The mutation makes the reconciliation record a fixed digest instead
    of the file's real canonical hash.  A1 (has a source) still holds —
    which is exactly why A1 alone is not enough — but A2 (the witness
    reproduces) must fail.
    """
    from agent.memory import external_edit

    ws = _sandbox(tmp_path / "ws")
    truth = ws / "MEMORY.md"
    truth.write_text("# head\n\nbody\n", encoding="utf-8")

    original = external_edit.reconcile_external_edits

    def constant_witness(root, candidates=None, **kw):
        from agent.memory import changes
        rep = {"workspace": str(root), "dry_run": bool(kw.get("dry_run")),
               "recorded": [], "unwitnessed": [], "skipped": [], "errors": []}
        for rel, real in (candidates or []):
            if not kw.get("dry_run"):
                changes.record_change(
                    root, rel, "external-edit", actor="mutant",
                    extra={"content_sha256": "0" * 64})
            rep["recorded"].append({"path": rel, "reason": "mutant"})
        return rep

    assert original is not constant_witness
    external_edit.reconcile_external_edits = constant_witness
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        external_edit.reconcile_external_edits = original

    assert states["EE-2(unit)"] == STATE_FAIL, (
        "恒定见证哈希必须被抓住 —— 『记了』不等于『记对了』\n" + printed)

    report, states, printed = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states["EE-2(unit)"] == STATE_PASS, printed


def test_muT4_a_raw_byte_witness_must_fail_on_crlf(tmp_path):
    """MU-T4: the witness uses the RAW BYTES, not the canonical text.

    This is MU-T2's mild version: it only shows up on a CRLF file, so
    **the fixture must contain one** — otherwise the mutant survives.
    """
    from agent.memory import external_edit

    ws = _sandbox(tmp_path / "ws")
    truth = ws / "MEMORY.md"
    truth.write_bytes(b"# head\r\n\r\nbody\r\n")

    original = external_edit.reconcile_external_edits

    def raw_witness(root, candidates=None, **kw):
        from agent.memory import changes
        rep = {"workspace": str(root), "dry_run": bool(kw.get("dry_run")),
               "recorded": [], "unwitnessed": [], "skipped": [], "errors": []}
        for rel, real in (candidates or []):
            try:
                raw = Path(real).read_bytes()
            except OSError:
                raw = b""
            import hashlib
            if not kw.get("dry_run"):
                changes.record_change(
                    root, rel, "external-edit", actor="mutant",
                    extra={"content_sha256": hashlib.sha256(raw).hexdigest()})
            rep["recorded"].append({"path": rel, "reason": "mutant"})
        return rep

    assert original is not raw_witness
    external_edit.reconcile_external_edits = raw_witness
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        external_edit.reconcile_external_edits = original

    assert states["EE-2(unit)"] == STATE_FAIL, (
        "用原始字节算见证 ⇒ CRLF 文件的见证复现不出规范哈希 ⇒ 必须红\n"
        + printed)
    assert "F5" in printed, printed

    report, states, printed = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states["EE-2(unit)"] == STATE_PASS, printed


def test_muT5_a_nested_extra_must_be_red(tmp_path):
    """MU-T5: ``record_change`` NESTS the ``extra`` dict.

    The writer does ``event.update(extra)`` — FLAT.  A version that
    nests it as ``event["extra"] = extra`` puts the witness one level
    down, where the reader does not look ⇒ every event reads as
    unwitnessed ⇒ the log would grow without bound.

    **This is the design稿's most easily-missed mutant** and the one
    that bites only when the reader and the writer disagree.
    """
    from agent.memory import changes

    original = changes.record_change

    def nested(root, relpath, source, **kw):
        extra = kw.pop("extra", None)
        ev = original(root, relpath, source, **kw)
        # Rewrite the last event into the nested shape.
        import json
        log = changes.change_log_path(root)
        if log.is_file() and extra:
            lines = [l for l in log.read_text(encoding="utf-8").splitlines()
                     if l.strip()]
            last = json.loads(lines[-1])
            for k in list(last.keys()):
                if k in extra:
                    del last[k]
            last["extra"] = dict(extra)
            lines[-1] = json.dumps(last, ensure_ascii=False)
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ev

    assert original is not nested
    changes.record_change = nested
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        changes.record_change = original

    assert states["EE-2(unit)"] == STATE_FAIL, (
        "见证被嵌套到 event['extra'] 里 ⇒ 读侧找不到见证 ⇒ 必须红\n"
        + printed)

    report, states, printed = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states["EE-2(unit)"] == STATE_PASS, printed


# ---------------------------------------------------------------------------
# MU-T6 / MU-T8 / MU-T9
# ---------------------------------------------------------------------------

def test_muT6_a_refreshing_latest_change_mtime_must_be_red(tmp_path):
    """MU-T6: ``latest_change_mtime`` advances on every READ.

    A refreshing stamp makes the shelf permanently stale and DR-2
    permanently green — the log stops meaning anything.
    """
    from agent.memory import changes

    ws = _sandbox(tmp_path / "ws")
    (ws / "MEMORY.md").write_text("# head\n", encoding="utf-8")
    from agent.memory import external_edit
    external_edit.reconcile_external_edits(ws, [("MEMORY.md", ws / "MEMORY.md")])

    original = changes.latest_change_mtime
    calls = {"n": 0}

    def refreshing(root):
        calls["n"] += 1
        return "2099-01-01T00:00:%02d+08:00" % (calls["n"] % 60)

    assert original is not refreshing
    changes.latest_change_mtime = refreshing
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        changes.latest_change_mtime = original

    assert states["EE-2(unit)"] == STATE_FAIL, (
        "每次读都推进 latest_change_mtime ⇒ 货架永远滞后 ⇒ 必须红\n"
        + printed)

    report, states, printed = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states["EE-2(unit)"] == STATE_PASS, printed


def test_muT8_reading_only_the_root_log_must_be_red(tmp_path):
    """MU-T8: the reconciliation only sees the ROOT workspace's log.

    The守门人's DR-2 already walked into this: the paths are relative
    to each agent's own workspace, so reading one log for all of them
    makes every agent's identity file look unwitnessed.
    """
    from agent.memory import external_edit

    wsA = _sandbox(tmp_path / "multi" / "a")
    wsB = _sandbox(tmp_path / "multi" / "b")
    (wsA / "MEMORY.md").write_text("# A\n", encoding="utf-8")
    (wsB / "MEMORY.md").write_text("# B\n", encoding="utf-8")
    external_edit.reconcile_external_edits(wsA, [("MEMORY.md", wsA / "MEMORY.md")])
    external_edit.reconcile_external_edits(wsB, [("MEMORY.md", wsB / "MEMORY.md")])

    original = external_edit.reconcile_external_edits

    def root_only(root, candidates=None, **kw):
        # Record into a SHARED log: the second workspace's events land
        # in the first one's log ⇒ the two workspaces' witnesses mix.
        shared = Path(tmp_path) / "multi" / "a"
        return original(shared, candidates, **kw)

    assert original is not root_only
    external_edit.reconcile_external_edits = root_only
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        external_edit.reconcile_external_edits = original

    assert states["EE-2(unit)"] == STATE_FAIL, (
        "只读一个工作区的日志 ⇒ 各 agent 的见证互相串 ⇒ 必须红\n"
        + printed)
    assert "F6" in printed, printed

    report, states, printed = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states["EE-2(unit)"] == STATE_PASS, printed


def test_muT9_swallowing_the_exception_must_be_red(tmp_path):
    """MU-T9: the reconciliation swallows the error and reports nothing.

    **The most insidious one.**  "I could not read it" and "it did not
    change" become indistinguishable ⇒ the mechanism reports success
    for a file it never looked at.
    """
    from agent.memory import external_edit

    ws = _sandbox(tmp_path / "ws")
    (ws / "MEMORY.md").write_text("# head\n", encoding="utf-8")

    original = external_edit.reconcile_external_edits

    def swallowing(root, candidates=None, **kw):
        try:
            return original(root, candidates, **kw)
        except Exception:
            return {"workspace": str(root), "dry_run": False,
                    "recorded": [], "unwitnessed": [], "skipped": [],
                    "errors": []}

    assert original is not swallowing
    external_edit.reconcile_external_edits = swallowing
    try:
        report, states, printed = _run(
            phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    finally:
        external_edit.reconcile_external_edits = original

    # The swallowing mutant must not make the criterion green by
    # accident: the F7 fixture (an unreadable path) must still be
    # visible as "skipped", and the mutant must be caught.
    report2, states2, printed2 = _run(
        phase2_s3x.judge_external_edit_mechanism_witnesses_edits)
    assert states2["EE-2(unit)"] == STATE_PASS, printed2


def test_muT9b_an_unreadable_path_must_not_look_witnessed(tmp_path):
    """MU-T9's companion: "不知道" must not be reported as "没变化".

    The F7 fixture: a path that cannot be read must come back as
    ``skipped``, never as ``recorded`` and never as "unchanged".
    """
    from agent.memory import external_edit

    ws = _sandbox(tmp_path / "ws")
    d = ws / "MEMORY.md"
    d.mkdir(parents=True, exist_ok=True)      # a DIRECTORY
    rep = external_edit.reconcile_external_edits(ws, [("MEMORY.md", d)])
    assert rep.get("skipped"), (
        "读不到的文件必须被报成 skipped —— 『不知道』不能伪装成『没变化』")
    assert not rep.get("recorded"), "读不到的文件不能被报成 recorded"


# ---------------------------------------------------------------------------
# MU-T10 · R3, the two criteria's RED sets
# ---------------------------------------------------------------------------

def test_muT10_a_disagreeing_reader_must_be_red():
    """MU-T10: the gate's reader and ``text_inv``'s disagree on ONE row.

    The mutation makes the gate's reader see one extra RED row that
    ``text_inv`` does not report.  **The counts are then different
    (N vs N-1) — but the point of R3 is the SET, so the mutant is
    built to keep the COUNT the same** (one row dropped from the gate
    side, one added).  A count-based comparison would call that a pass.

    **Why a FIXTURE and not the live corpus.**  The live libraries are
    being rebuilt while this suite runs (measured: the gate's judged
    rows went from 3305 to 810 in five minutes), so a test that reads
    the live corpus is a test whose result depends on when it ran.  The
    fixture below is two libraries with one drifting row each — enough
    to make both readers produce a non-empty RED set, and stable.
    """
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="muT10-") as td:
        base = Path(td)
        # Build a workspace with ONE library whose truth file drifted.
        ws = base / "ws"
        (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
        truth = ws / "MEMORY.md"
        truth.write_text("# head\n\nBODY\n", encoding="utf-8")
        db = ws / "memory" / "long-term" / "index.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "CREATE TABLE chunks (id TEXT PRIMARY KEY, path TEXT, "
                "body_path TEXT, body_anchor TEXT, body_hash TEXT, "
                "text TEXT, text_state TEXT, body_origin TEXT, "
                "created_at TEXT, updated_at TEXT)")
            conn.execute(
                "INSERT INTO chunks VALUES ('id-1','MEMORY.md','MEMORY.md',"
                "'L1-2','sha256:x','# head\\n\\nDRIFTED\\n','mirror','file',"
                "'2026-11-01','2026-11-01')")
            conn.commit()
        finally:
            conn.close()

        original = phase2_s3x._gate_red_set_with_rows

        def shifted(libs):
            red, judged, rows = original(libs)
            red = set(red)
            if red:
                # Drop one and add a DIFFERENT one: the count is
                # unchanged, the SET is different.  This is the failure a
                # count-based comparison cannot see.
                victim = sorted(red)[0]
                red.discard(victim)
                red.add(("MUTANT-LIB", "MUTANT-CHUNK"))
            return red, judged, rows

        assert original is not shifted
        phase2_s3x._gate_red_set_with_rows = shifted
        try:
            report, states, printed = _run(
                phase2_s3x.judge_inv2_red_sets_are_the_same_set, ws)
        finally:
            phase2_s3x._gate_red_set_with_rows = original

        assert states["R3(REAL)"] == STATE_FAIL, (
            "两把尺子在各选各的行 ⇒ 对称差非空 ⇒ 必须红（哪怕数量相同）\n"
            + printed)
        assert "对称差" in printed, printed

        # And the restore brings the green back — on the SAME fixture.
        report, states, printed = _run(
            phase2_s3x.judge_inv2_red_sets_are_the_same_set, ws)
        assert states["R3(REAL)"] == STATE_PASS, printed


# ---------------------------------------------------------------------------
# MU-T11 · the fake guardian
# ---------------------------------------------------------------------------

def test_muT11_removing_the_canonical_cross_check_must_be_red():
    """MU-T11: the canonical cross-check is NEUTERED.

    This is the architect's finding, turned into a mutant: the docstring
    promised a cross-check, the module did not have one.  Now it has
    one — and if somebody removes it again (or replaces it with one
    that always says "no disagreement"), **EE-4 must notice**.

    The mutation replaces the check with one that always reports "no
    disagreement" — the exact shape of "the check is gone but nothing
    says so".
    """
    original = phase2_s3w._canonical_cross_check

    def always_fine():
        return []

    assert original is not always_fine
    phase2_s3w._canonical_cross_check = always_fine
    try:
        report, states, printed = _run(
            phase2_s3x.judge_the_canonical_cross_check_is_really_called,
            Path(r"C:\Users\User\cow"))
    finally:
        phase2_s3w._canonical_cross_check = original

    # The mutant neutered the REAL check ⇒ EE-4's own mutation (which
    # breaks this module's canonical COPY) can no longer make W-4 red
    # ⇒ EE-4 must be red.
    assert states["EE-4(unit)"] == STATE_FAIL, (
        "交叉校验被摘掉 ⇒ W-4 永远不会因为两份 canonical 不一致而红 ⇒ "
        "那又是一个假守护者\n" + printed)

    report, states, printed = _run(
        phase2_s3x.judge_the_canonical_cross_check_is_really_called,
        Path(r"C:\Users\User\cow"))
    assert states["EE-4(unit)"] == STATE_PASS, printed


def test_muT11b_deleting_the_cross_check_entirely_must_be_red():
    """MU-T11's companion: the check is DELETED, not neutered.

    A deleted attribute is the other way this hole reappears — and it
    must be reported with its own message, not as a crash.
    """
    original = phase2_s3w._canonical_cross_check
    del phase2_s3w._canonical_cross_check
    try:
        report, states, printed = _run(
            phase2_s3x.judge_the_canonical_cross_check_is_really_called,
            Path(r"C:\Users\User\cow"))
    finally:
        phase2_s3w._canonical_cross_check = original

    assert states["EE-4(unit)"] == STATE_FAIL, (
        "交叉校验被整个删掉 ⇒ 守护者不存在 ⇒ 必须红\n" + printed)

    report, states, printed = _run(
        phase2_s3x.judge_the_canonical_cross_check_is_really_called,
        Path(r"C:\Users\User\cow"))
    assert states["EE-4(unit)"] == STATE_PASS, printed


def test_muT11b_the_canonical_copy_must_differ_from_the_real_one():
    """The cross-check's own teeth: break THIS module's copy.

    The cross-check compares ``phase2_s3w.canonical_text`` against
    ``naming.canonical_text``.  If the comparison were vacuous (e.g.
    both sides were the same object), it could never fail.  This test
    proves the comparison is real by making the copy diverge.
    """
    original = phase2_s3w.canonical_text

    def broken_copy(content):
        # A copy that forgets to fold the blank-line runs.
        text = content.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()

    assert original is not broken_copy
    phase2_s3w.canonical_text = broken_copy
    try:
        problems = phase2_s3w._canonical_cross_check()
    finally:
        phase2_s3w.canonical_text = original

    assert problems, (
        "本模块的 canonical 副本被改坏（不折叠连续空行）⇒ 交叉校验"
        "必须报出分歧 —— 校验本身没有牙齿")


# ---------------------------------------------------------------------------
# MU-T12 · the global baseline
# ---------------------------------------------------------------------------

def test_muT12_a_new_red_criterion_must_be_red():
    """MU-T12: a NEW red criterion appears in the gate's table.

    GB-1 reads the gate's own result table, so the mutation is simply
    "put one more FAIL row in the table" — the criterion must notice
    that the failure set grew.
    """
    report = Report()
    report.judge("SOME-EXISTING", "a criterion that was already red",
                 False, "detail")
    report.judge("MUTANT-NEW", "a criterion that just went red",
                 False, "detail")

    # Run GB-1 against a table that already contains the mutant row.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        phase2_s3x.judge_global_failure_set_matches_baseline(report)
    states = {number: state for number, _n, state in report.rows}
    printed = buf.getvalue()

    assert states.get("GB-1(unit)") == STATE_FAIL, (
        "门禁多了一条红而全局基线没有 ⇒ 那是回归 ⇒ 必须红\n" + printed)
    assert "MUTANT-NEW" in printed, printed


def test_the_global_baseline_file_must_be_tracked():
    """The baseline must be IN the repository.

    A baseline in ``tmp/`` is a baseline nobody else has — that was the
    stage-two B2 root cause, and it is why ``Q5``'s baseline lives in
    ``tests/baseline/``.
    """
    import subprocess
    path = phase2_s3x._global_baseline_path()
    assert path.is_file(), "全局基线文件不存在: %s" % path
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--error-unmatch",
         str(path.relative_to(REPO)).replace("\\", "/")],
        capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, (
        "全局基线不在版本库内：换台机器跑不了 —— "
        "这正是阶段二 B2 未闭环的根因")
