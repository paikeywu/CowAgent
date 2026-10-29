#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-E · the MUTANT REPLAY.

The spec's rule: "每条判据配反向用例：改坏必红 → 恢复必转绿".
A static self-check proves the criterion FILE is honest; it does not
prove the criterion CATCHES the defect.  This file proves that, by
making the defect for real, on a COPY, and asking the criteria whether
they went red.

Every test here has the same shape:

    1. build a disposable copy of the object (never the live index);
    2. break it in ONE specific way;
    3. run the criterion against the broken copy;
    4. assert it went RED (or NOTRUN — the third state, where the
       mutation removes a PRECONDITION rather than breaking a value);
    5. restore the object and assert it went GREEN again.

The restore step matters as much as the break step: a criterion that is
red for everything is useless, and "改坏必红" without "恢复必转绿" is
half a criterion.

The mutations replayed
----------------------
MU-1   the live-library enumeration drops the agent libraries (the
       "only measure the ROOT" false green, made real)
MU-2   the enumeration takes ``agents/`` as the agent workspace root
       (the mistake that produced the "15 libraries all RED" false alarm)
MU-3   the enumeration picks up the migration's BACKUP databases
       (the ``*.db`` glob the design forbids)
MU-4   the three-state harness is hollowed out: every NOTRUN becomes a
       FAIL
MU-5   the content digest stops covering the four extern columns
       (S-1 goes blind to a wiped ``body_path``)
MU-6   a criterion's name claims "四列" but its assertion checks one
MU-7   the coverage matrix claims an entry it does not cover
MU-8   the baseline comparison compares COUNTS instead of SETS
MU-9   a library's rows are wiped (the ``agent-mvnewfyb`` defect,
       made real on a copy)
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
sys.path.insert(0, str(REPO))

from agent.memory.acceptance import Report, STATE_FAIL, STATE_NOTRUN, STATE_PASS  # noqa: E402
from agent.memory.acceptance import live_libs  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _workspace_with_agents(tmp: Path, n_agents: int = 3) -> Path:
    """A throwaway workspace with ``n_agents`` agent libraries + a ROOT."""
    ws = tmp / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    _make_index(ws / "memory" / "long-term" / "index.db", "ROOT")
    for i in range(n_agents):
        d = ws / "agents" / ("agent-%02d" % i)
        (d / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
        (d / "MEMORY.md").write_text("# agent %d\n\nbody\n" % i, encoding="utf-8")
        _make_index(d / "memory" / "long-term" / "index.db", "agent-%02d" % i)
    (ws / "MEMORY.md").write_text("# root\n\nbody\n", encoding="utf-8")
    return ws


def _make_index(db: Path, name: str) -> None:
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
                text_state TEXT DEFAULT 'projection'
            )""")
        conn.execute("""
            CREATE TABLE files (
                path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL,
                size INTEGER, updated_at INTEGER,
                chunk_count INTEGER, chunker_version INTEGER
            )""")
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash, "
            "body_path, body_anchor, body_hash, text_state) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("id-" + name, "MEMORY.md", 1, 3, "body text",
             "sha256:" + "0" * 64, "MEMORY.md", "L1-3",
             "sha256:" + "0" * 64, "projection"))
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, updated_at, "
            "chunk_count, chunker_version) VALUES (?,?,?,?,?,?,?,?)",
            ("MEMORY.md", "memory", "h", 0.0, 10, 0, 1, 3))
        conn.commit()
    finally:
        conn.close()


def _states(report: Report):
    return {number: state for number, _name, state in report.rows}


def _run(fn, *args):
    """Run one criterion; return (report, states).

    The report's PRINTED OUTPUT is captured and attached as
    ``report.printed`` — the report is part of the evidence: a criterion
    that says "FAIL" without naming WHICH library failed is not a
    usable defect report.
    """
    import contextlib
    import io

    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    return report, _states(report)


# ---------------------------------------------------------------------------
# MU-1 · the enumeration drops the agent libraries
# ---------------------------------------------------------------------------

def test_mu1_enumeration_that_only_finds_the_root_is_caught(tmp_path):
    """MU-1: a criterion that measures only the ROOT must be caught."""
    ws = _workspace_with_agents(tmp_path, n_agents=3)
    report, states = _run(
        __import__("agent.memory.acceptance.phase2_s3e", fromlist=["x"])
        .judge_ca6_every_live_library_is_profiled, ws)
    assert states["C-A6.1(unit)"] == STATE_PASS

    # Now the mutation: the enumeration only returns the ROOT.
    original = live_libs.enumerate_live

    def root_only(workspace):
        return [lib for lib in original(workspace) if lib.kind == "ROOT"]

    import agent.memory.acceptance.phase2_s3e as s3e
    s3e.enumerate_live = root_only
    try:
        report, states = _run(
            s3e.judge_ca6_every_live_library_is_profiled, ws)
    finally:
        s3e.enumerate_live = original
    assert states["C-A6.1(unit)"] == STATE_FAIL, (
        "只枚举根库的判据必须报红 —— 15 个 agent 库全坏也会绿的那个洞")
    # and the restore turns it green again
    report, states = _run(
        s3e.judge_ca6_every_live_library_is_profiled, ws)
    assert states["C-A6.1(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-2 · the enumeration takes agents/ as the agent's workspace root
# ---------------------------------------------------------------------------

def test_mu2_wrong_workspace_root_is_caught(tmp_path):
    """MU-2: the workspace root must be ``agents/<id>``, not ``agents/``."""
    ws = _workspace_with_agents(tmp_path, n_agents=2)
    import agent.memory.acceptance.phase2_s3e as s3e

    report, states = _run(s3e.judge_ca6_agent_workspace_root_is_the_agent_dir, ws)
    assert states["C-A6.2(unit)"] == STATE_PASS

    # The mutation: the root becomes agents/.
    original = live_libs.enumerate_live

    def wrong_root(workspace):
        libs = original(workspace)
        for lib in libs:
            if lib.kind == "AGENT":
                lib.workspace = lib.workspace.parent
        return libs

    s3e.enumerate_live = wrong_root
    try:
        report, states = _run(
            s3e.judge_ca6_agent_workspace_root_is_the_agent_dir, ws)
    finally:
        s3e.enumerate_live = original
    assert states["C-A6.2(unit)"] == STATE_FAIL, (
        "真源层根取成 agents/ 必须报红 —— 那正是『15 个库全红』误报的成因")

    report, states = _run(s3e.judge_ca6_agent_workspace_root_is_the_agent_dir, ws)
    assert states["C-A6.2(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-3 · the enumeration picks up the migration's backups
# ---------------------------------------------------------------------------

def test_mu3_backup_databases_are_not_live_libraries(tmp_path):
    """MU-3: a backup ``.db`` under tmp/ must NOT be a live library."""
    ws = _workspace_with_agents(tmp_path, n_agents=1)
    bak_dir = ws / "tmp" / "p1s3c-backup"
    bak_dir.mkdir(parents=True)
    _make_index(bak_dir / "agent-00.p1-bak.db", "backup")

    libs = live_libs.enumerate_live(ws)
    names = {lib.name for lib in libs}
    assert "backup" not in names, "备份库不是活库"
    assert not any("p1-bak" in str(lib.db) for lib in libs), \
        "枚举绝不能按 *.db 通配 —— 会把备份当成活库"
    assert len(libs) == 2  # ROOT + 1 agent


# ---------------------------------------------------------------------------
# MU-4 · the three-state harness is hollowed out
# ---------------------------------------------------------------------------

def test_mu4_three_states_are_not_hollowed_out(tmp_path):
    """MU-4: a NOTRUN must stay a NOTRUN, not collapse into a FAIL."""
    import agent.memory.acceptance.phase2_guards as guards

    report, states = _run(guards.judge_mu12_three_states_are_real)
    assert states["MU-12(unit)"] == STATE_PASS

    # The mutation: the criteria never use the third state.
    from agent.memory.acceptance import phase2_s3e
    original = phase2_s3e.__file__
    # the guard scans the FILE, so mutate the file on a copy
    src = Path(original).read_text(encoding="utf-8")
    mutated = src.replace("judge_notrun(", "judge_passed(")
    mutated = mutated.replace("notrun=True", "ok=True")
    tmp_copy = Path(tmp_path) / "phase2_s3e_mutated.py"
    tmp_copy.write_text(mutated, encoding="utf-8")

    guards.CRITERION_FILES = ("phase2.py", str(tmp_copy), "live_libs.py")
    try:
        report, states = _run(guards.judge_mu12_three_states_are_real)
        assert states["MU-12(unit)"] == STATE_FAIL, (
            "判据全都不用第三态时，MU-12 必须报红 —— 机制被架空")
    finally:
        guards.CRITERION_FILES = ("phase2.py", "phase2_s3e.py", "live_libs.py")

    report, states = _run(guards.judge_mu12_three_states_are_real)
    assert states["MU-12(unit)"] == STATE_PASS


def test_report_keeps_notrun_distinct_from_fail():
    """The third state is a DISTINCT state, and it is not green."""
    from agent.memory.acceptance import Report
    r = Report()
    r.judge("A", "ok", True, "d")
    r.judge("B", "bad", False, "d")
    r.judge_notrun("C", "cannot measure", "reason")
    states = {n: s for n, _x, s in r.rows}
    assert states["A"] == STATE_PASS
    assert states["B"] == STATE_FAIL
    assert states["C"] == STATE_NOTRUN
    assert r.summary() is False, "有 NOTRUN 的报告不许返回 True"


# ---------------------------------------------------------------------------
# MU-5 · the content digest stops covering the four extern columns
# ---------------------------------------------------------------------------

def test_mu5_digest_must_cover_the_four_columns(tmp_path):
    """MU-5: wiping body_path must move the S-1 digest."""
    import agent.memory.acceptance.phase2_s3e as s3e

    ws = _workspace_with_agents(tmp_path, n_agents=1)
    report, states = _run(
        s3e.judge_s1_criteria_do_not_modify_what_they_measure, ws)
    assert states["S-1(unit)"] == STATE_PASS

    # The mutation: remove the four columns from the digest.
    import agent.memory.acceptance as acceptance
    original = acceptance._CONTENT_COLUMNS
    acceptance._CONTENT_COLUMNS = tuple(
        c for c in original
        if c not in ("body_path", "body_anchor", "body_hash", "text_state"))
    try:
        report, states = _run(
            s3e.judge_s1_criteria_do_not_modify_what_they_measure, ws)
        assert states["S-1(unit)"] == STATE_FAIL, (
            "指纹不覆盖四列时 S-1 必须报红 —— 它对被清空的 body_path 是瞎的")
    finally:
        acceptance._CONTENT_COLUMNS = original

    report, states = _run(
        s3e.judge_s1_criteria_do_not_modify_what_they_measure, ws)
    assert states["S-1(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-6 · a criterion's name claims more than it asserts
# ---------------------------------------------------------------------------

def test_mu6_a_lying_criterion_name_is_caught(tmp_path):
    """MU-6: a name that claims '四列' must assert all four."""
    import agent.memory.acceptance.phase2_s3e as s3e

    report, states = _run(s3e.judge_s6_criterion_names_say_what_they_measure)
    assert states["S-6(unit)"] == STATE_PASS

    # The mutation: a criterion that claims 四列 but only checks one.
    liar = tmp_path / "phase2_liar.py"
    liar.write_text(
        "def liar(report):\n"
        "    return report.judge('X-1(unit)', '这个库四列齐备', True, 'body_path ok')\n",
        encoding="utf-8")

    report, states = _run(
        s3e.judge_s6_criterion_names_say_what_they_measure, None, [liar])
    assert states["S-6(unit)"] == STATE_FAIL, (
        "判据名声称『四列』但只断言一列，必须报红 —— 名字骗人就是假绿")
    assert "X-1(unit)" in report.printed, "要指名是哪个判据名不副实"

    # and the restore turns it green again
    report, states = _run(s3e.judge_s6_criterion_names_say_what_they_measure)
    assert states["S-6(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-7 · the coverage matrix claims an entry it does not cover
# ---------------------------------------------------------------------------

def test_mu7_overreported_coverage_is_caught():
    """MU-7: an entry with neither a criterion nor a reason is a gap."""
    import agent.memory.acceptance.phase2_guards as guards

    report, states = _run(guards.judge_mu14_coverage_is_not_overreported)
    assert states["MU-14(unit)"] == STATE_PASS

    # The mutation that matters: an entry that EXISTS in the table but
    # has neither a criterion nor an out-of-scope reason.
    original_scope = dict(guards.OUT_OF_SCOPE)
    try:
        guards.OUT_OF_SCOPE.pop("S5", None)
        report, states = _run(guards.judge_mu14_coverage_is_not_overreported)
        assert states["MU-14(unit)"] == STATE_FAIL, (
            "有条目既无判据也无不适用理由 = 覆盖矩阵谎报，必须报红")
        assert "S5" in report.printed, "要指名漏了哪一条"
    finally:
        guards.OUT_OF_SCOPE.clear()
        guards.OUT_OF_SCOPE.update(original_scope)

    report, states = _run(guards.judge_mu14_coverage_is_not_overreported)
    assert states["MU-14(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-8 · the baseline comparison compares counts instead of sets
# ---------------------------------------------------------------------------

def test_mu8_baseline_compares_sets_not_counts(tmp_path, monkeypatch):
    """MU-8: 'same count, different content' is a regression.

    The mutation: the comparison is made on the COUNT of failures, so a
    baseline with 3 failures passes when the current run has 3 DIFFERENT
    failures.  The set comparison catches it.
    """
    import agent.memory.acceptance.phase2_s3e as s3e

    ws = _workspace_with_agents(tmp_path, n_agents=1)

    # An isolated baseline file: the real one names libraries that do not
    # exist in this synthetic workspace.
    #
    # The synthetic corpus's verdicts are: INV-1 GREEN, INV-2/3 RED,
    # INV-9 NOTRUN, SHELF-LAG NOTRUN.  So the baseline must name
    # ROOT:INV-2/3 and agent-00:INV-2/3 — and MUST NOT name the
    # NOTRUN ones (those are not regressions).
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(
        "# 合成基线\nROOT:INV-2/3\nagent-00:INV-2/3\n", encoding="utf-8")

    # Point the criterion at our baseline by patching the path it uses.
    import agent.memory.acceptance.phase2_s3e as mod
    original_src = mod.__file__

    # The criterion resolves the baseline relative to the repo.  Give it
    # a repo-shaped tree instead: tests/baseline/ under a fake root.
    fake_repo = tmp_path / "repo"
    (fake_repo / "tests" / "baseline").mkdir(parents=True)
    (fake_repo / "tests" / "baseline" / "p1_regression_baseline.txt").write_text(
        baseline.read_text(encoding="utf-8"), encoding="utf-8")

    import importlib.util
    src = Path(original_src).read_text(encoding="utf-8")
    mutated = src.replace(
        'Path(__file__).resolve().parents[3] / "tests" / "baseline" / \\\n'
        '        "p1_regression_baseline.txt"',
        'Path(r"%s") / "tests" / "baseline" / "p1_regression_baseline.txt"' % fake_repo)
    if mutated == src:
        # the source layout differs; patch by replacing the parents[3] part
        mutated = src.replace(
            'Path(__file__).resolve().parents[3]',
            'Path(r"%s")' % fake_repo)
    tmp_copy = tmp_path / "phase2_s3e_fakerepo.py"
    tmp_copy.write_text(mutated, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("phase2_s3e_fakerepo", tmp_copy)
    mod2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod2)

    # The synthetic corpus: ROOT and agent-00 are both RED on INV-2/3.
    report, states = _run(mod2.judge_q5_no_library_regressed, ws)
    assert states["Q5(REAL)"] == STATE_PASS, (
        "基线里的两条 INV-2/3 现在还是 RED，不是回归")

    # The mutation: the baseline names a DIFFERENT entry that is now
    # RED — same COUNT, different content.  The set comparison must
    # call it a regression.
    (fake_repo / "tests" / "baseline" / "p1_regression_baseline.txt").write_text(
        "agent-99:INV-2/3\n", encoding="utf-8")
    report, states = _run(mod2.judge_q5_no_library_regressed, ws)
    assert states["Q5(REAL)"] == STATE_FAIL, (
        "失败集内容换了（数量可能相同）必须算回归 —— 比集合不比数量")


# ---------------------------------------------------------------------------
# MU-9 · a library's rows are wiped (the agent-mvnewfyb defect, for real)
# ---------------------------------------------------------------------------

def test_mu9_a_wholly_unmigrated_library_is_caught(tmp_path):
    """MU-9: one library wiped out of 16 must make M4(REAL) red."""
    import agent.memory.acceptance.phase2_s3e as s3e

    ws = _workspace_with_agents(tmp_path, n_agents=3)
    report, states = _run(s3e.judge_m4_every_library_was_backfilled, ws)
    assert states["M4(REAL)"] == STATE_PASS

    # The mutation: wipe one library's four columns (what a stale writer
    # does).
    victim = ws / "agents" / "agent-01" / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(victim))
    try:
        conn.execute("UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
                     "body_hash=NULL")
        conn.commit()
    finally:
        conn.close()

    report, states = _run(s3e.judge_m4_every_library_was_backfilled, ws)
    assert states["M4(REAL)"] == STATE_FAIL, (
        "一个库被清空必须让 M4 报红 —— 只测根库的判据看不到这个")
    # the report must NAME the broken library
    assert "agent-01" in report.printed, \
        "报告要指名是哪个库（不能只说『有一个库坏了』）"

    # and the restore turns it green again
    conn = sqlite3.connect(str(victim))
    try:
        conn.execute("UPDATE chunks SET body_path='MEMORY.md', "
                     "body_anchor='L1-3', body_hash='sha256:" + "0" * 64 + "'")
        conn.commit()
    finally:
        conn.close()
    report, states = _run(s3e.judge_m4_every_library_was_backfilled, ws)
    assert states["M4(REAL)"] == STATE_PASS
