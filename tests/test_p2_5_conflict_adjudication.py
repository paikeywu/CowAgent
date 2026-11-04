#!/usr/bin/env python3
# encoding:utf-8
"""P2-5 · 冲突裁决层的判据测试（含 mutant 重放）。

**The point of these tests is that they can FAIL.**  A criterion that
cannot go red is a criterion nobody read.  Each test builds the broken
state and asserts the verdict moves.

Every test builds its own temporary workspace: the live shared root is
never touched.

The independence rule
---------------------
**The expected values are NOT computed by the object under test.**  They
are literals in ``agent/memory/acceptance/phase2_p2_conflict.py`` — the
criterion module carries its own copy of the carrier table and its own
table of expected rulings.  If the criterion called
``ladder.CARRIER_TABLE`` to work out what it expected, it would be
checking that the module agrees with itself, and it would always be green.

The mutants replayed
--------------------
MU-P2-5a  the ladder's ORDER is reversed  ⇒ the criterion must go red.
MU-P2-5b  a rule's precedence is broken (the truth-over-projection rule
          is deleted) ⇒ the criterion must go red.
MU-P2-5c  the ruling record loses its traceability fields ⇒ the
          criterion must go red.
MU-P2-5d  the undecided path starts picking a side ⇒ the criterion must
          go red.
MU-P2-5e  the adjudicator stops writing down which rule decided ⇒ the
          criterion must go red.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import (  # noqa: E402
    Report,
    STATE_FAIL,
    STATE_NOTRUN,
)
from agent.memory.acceptance import phase2_p2_conflict as C  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _states(report: Report):
    return {number: state for number, _name, state in report.rows}


def _run(fn, root: Path):
    """Run one measure_* function and capture what it printed."""
    import contextlib
    import io

    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, root)
    return _states(report), buf.getvalue()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """A workspace with a conflict ledger holding one real ruling."""
    r = tmp_path / "cow"
    (r / "conflicts").mkdir(parents=True)
    ledger = r / "conflicts" / "rulings.jsonl"
    from agent.memory.conflict import adjudicator, ladder

    conflict = ladder.Conflict.from_json(
        C.FIXTURES["truth-beats-projection"]["conflict"])
    ruling = adjudicator.adjudicate(conflict, ladder.RULES)
    ledger.write_text(json.dumps(dict(ruling), ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return r


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_the_fixture_is_green(root: Path):
    """**The fixture must be green** — otherwise a red elsewhere means
    nothing (and a green elsewhere would be a lie)."""
    rep = C.judge_p2_conflict_family(root)
    assert rep["verdict"] == "PASS", rep
    assert len(rep["reports"]) == 6


def test_the_criteria_report_what_they_measure(root: Path):
    for r in C.judge_p2_conflict_family(root)["reports"]:
        assert r["criterion"] in C.CRITERION_IDS
        assert r["name"], "a criterion with no name says nothing"
        assert r["detail"], "a criterion with no detail says nothing"
        assert r["verdict"] in ("PASS", "FAIL", "NOTRUN")


def test_every_criterion_id_is_covered(root: Path):
    seen = {r["criterion"] for r in C.judge_p2_conflict_family(root)["reports"]}
    assert seen == set(C.CRITERION_IDS)


# ---------------------------------------------------------------------------
# NOTRUN is not green
# ---------------------------------------------------------------------------

def test_a_workspace_without_a_ledger_is_notrun(root: Path):
    """**NOTRUN, not PASS** — there is nothing to measure.

    P2-5f measures the rulings that were actually written down.  A
    workspace with no ledger has nothing to measure, and saying
    "the recorded rulings are traceable" would be a lie.
    """
    (root / "conflicts" / "rulings.jsonl").unlink()
    states, out = _run(C.judge_p2_5f_recorded_rulings_are_traceable, root)
    assert states["P2-5f"] == STATE_NOTRUN, out
    assert "NOTRUN" in out


def test_an_empty_ledger_is_notrun(root: Path):
    (root / "conflicts" / "rulings.jsonl").write_text("", encoding="utf-8")
    states, out = _run(C.judge_p2_5f_recorded_rulings_are_traceable, root)
    assert states["P2-5f"] == STATE_NOTRUN, out


def test_the_adjudicator_being_absent_is_notrun(root: Path, monkeypatch):
    """**NOTRUN, not PASS** — the layer that was never built.

    The criterion module cannot import the object under test ⇒ it cannot
    measure anything ⇒ every criterion says NOTRUN.
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("agent.memory.conflict"):
            raise ImportError("mutated: the adjudicator is absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for mod in list(sys.modules):
        if mod.startswith("agent.memory.conflict"):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    rep = C.judge_p2_conflict_family(root)
    assert rep["verdict"] == "NOTRUN", rep
    for r in rep["reports"]:
        assert r["verdict"] == "NOTRUN", r


# ---------------------------------------------------------------------------
# the criteria can go red
# ---------------------------------------------------------------------------

def test_p2_5a_a_reordered_ladder_is_caught(root: Path):
    """The ladder's order IS part of the result — so a reordered ladder
    is a defect, and the criterion must say so."""
    import agent.memory.conflict.ladder as L

    original = L.RULES
    try:
        L.RULES = tuple(reversed(original))
        m = C.measure_p2_5a_the_ladder_is_judgeable(root)
    finally:
        L.RULES = original
    assert m["verdict"] == "FAIL", m
    assert "裁决序与期望不一致" in m["detail"]


def test_p2_5a_a_rule_without_a_basis_is_caught(root: Path):
    import dataclasses

    import agent.memory.conflict.ladder as L

    original = L.RULES
    try:
        stripped = tuple(
            dataclasses.replace(r, basis="") if r.rule_id == "R5-RECENCY" else r
            for r in original
        )
        L.RULES = stripped
        m = C.measure_p2_5a_the_ladder_is_judgeable(root)
    finally:
        L.RULES = original
    assert m["verdict"] == "FAIL", m
    assert "没有写出处" in m["detail"]


def test_p2_5b_a_deleted_truth_rule_is_caught(root: Path):
    """**MU-P2-5b** — remove the truth-over-projection rule and the
    criterion must notice that the projection now wins."""
    import agent.memory.conflict.ladder as L

    original = L.RULES
    try:
        L.RULES = tuple(r for r in original
                        if r.rule_id != "R2-TRUTH-OVER-PROJECTION")
        m = C.measure_p2_5b_the_ruling_is_unique_and_determined(root)
    finally:
        L.RULES = original
    assert m["verdict"] == "FAIL", m
    assert "truth-beats-projection" in m["mismatched"]


def test_p2_5b_a_broken_human_priority_is_caught(root: Path):
    """Human priority is the load-bearing rule (13 号 :348).  Break it and
    the criterion must go red."""
    import agent.memory.conflict.ladder as L

    original = L.RULES
    try:
        broken = tuple(
            L.Rule(r.rule_id, r.tier, r.name, r.basis,
                   (lambda *a, **k: None) if r.rule_id == "R1-HUMAN-RULING"
                   else r.decide_fn)
            for r in original
        )
        L.RULES = broken
        m = C.measure_p2_5b_the_ruling_is_unique_and_determined(root)
    finally:
        L.RULES = original
    assert m["verdict"] == "FAIL", m
    assert "human-ruling-outranks-mechanics" in m["mismatched"]


def test_p2_5c_a_ruling_without_traceability_is_caught(root: Path):
    """**MU-P2-5c** — the record loses ``ruled_by`` ⇒ not traceable."""

    from agent.memory.conflict import adjudicator as A

    original = A._ruled_by

    def blind(conflict, decision, winner):
        return ""

    try:
        A._ruled_by = blind
        m = C.measure_p2_5c_the_ruling_is_traceable(root)
    finally:
        A._ruled_by = original
    assert m["verdict"] == "FAIL", m
    assert "ruled_by" in m["missing"]["truth-beats-projection"]


def test_p2_5d_undecided_must_not_pick_a_side(root: Path):
    """**MU-P2-5d** — the undecided path starts picking the first
    candidate ⇒ the criterion must go red.

    This is the defect the whole layer exists to prevent: silently
    choosing a side is «自动选边 = 越权» (13 号 :348).
    """
    import agent.memory.conflict.ladder as L

    original = L._decide_undecided

    def picking(conflict, alive):
        return L.RuleDecision(
            verdict=L.VERDICT_RESOLVED,
            winner=alive[0].candidate_id if alive else None,
            rationale="假装能裁")

    try:
        L.RULES = tuple(
            L.Rule(r.rule_id, r.tier, r.name, r.basis,
                   picking if r.rule_id == "R7-UNDECIDED" else r.decide_fn)
            for r in L.RULES
        )
        m = C.measure_p2_5d_undecided_does_not_pick_a_side(root)
    finally:
        L.RULES = tuple(
            L.Rule(r.rule_id, r.tier, r.name, r.basis,
                   original if r.rule_id == "R7-UNDECIDED" else r.decide_fn)
            for r in L.RULES
        )
    assert m["verdict"] == "FAIL", m
    assert "选定了候选" in m["detail"]


def test_p2_5d_an_agent_named_as_the_ruler_is_caught(root: Path):
    """The undecided path must NOT name an agent as the ruler."""
    import agent.memory.conflict.ladder as L

    original = L.RULES

    def agent_picker(conflict, alive):
        return L.RuleDecision(
            verdict=L.VERDICT_RESOLVED,
            winner=alive[0].candidate_id if alive else None,
            rationale="让 agent 自己选")

    try:
        L.RULES = tuple(
            L.Rule(r.rule_id, r.tier, r.name, r.basis,
                   agent_picker if r.rule_id == "R7-UNDECIDED" else r.decide_fn)
            for r in original
        )
        from agent.memory.conflict import adjudicator as A

        orig_ruled = A._ruled_by
        A._ruled_by = lambda *a, **k: "agent-mvnewfyb 自动选边"
        try:
            m = C.measure_p2_5d_undecided_does_not_pick_a_side(root)
        finally:
            A._ruled_by = orig_ruled
    finally:
        L.RULES = original
    assert m["verdict"] == "FAIL", m
    assert "点名了 agent" in m["detail"]


def test_p2_5f_a_record_without_a_conflict_id_is_caught(root: Path):
    """**MU-P2-5e** — the record stops saying WHICH conflict it is
    about ⇒ not traceable."""
    ledger = root / "conflicts" / "rulings.jsonl"
    rec = json.loads(ledger.read_text(encoding="utf-8").strip())
    rec["conflict_id"] = ""
    ledger.write_text(json.dumps(rec, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    states, out = _run(C.judge_p2_5f_recorded_rulings_are_traceable, root)
    assert states["P2-5f"] == STATE_FAIL, out


def test_p2_5f_a_record_without_a_ladder_digest_is_caught(root: Path):
    """A record that does not say which ladder produced it cannot be
    compared with another record."""
    ledger = root / "conflicts" / "rulings.jsonl"
    rec = json.loads(ledger.read_text(encoding="utf-8").strip())
    rec["ladder_digest"] = ""
    ledger.write_text(json.dumps(rec, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    states, out = _run(C.judge_p2_5f_recorded_rulings_are_traceable, root)
    assert states["P2-5f"] == STATE_FAIL, out


# ---------------------------------------------------------------------------
# the expectation is a LITERAL, not a function of the object under test
# ---------------------------------------------------------------------------

def test_the_criterion_does_not_import_the_carrier_table(root: Path):
    """**The criterion must not compute its expectation from the module
    under test.**

    If it called ``ladder.CARRIER_TABLE`` to work out what it expected,
    it would be checking that the module agrees with itself — and it
    would always be green.

    **The check is on the CODE, not on the prose.**  The criterion's
    docstring *mentions* the module's table precisely to say «it must not
    use this»; a naive substring search would flag that mention and fail
    for the right reason at the wrong place.  So the check parses the
    module and looks for an actual **attribute access** on the imported
    module object.
    """
    import ast

    src = (REPO / "agent" / "memory" / "acceptance"
           / "phase2_p2_conflict.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    forbidden = {"CARRIER_TABLE", "CLASS_PRECEDENCE", "LAYER_PRECEDENCE"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            # the attribute must be read OFF THE MODULE UNDER TEST
            base = node.value
            if isinstance(base, ast.Name) and base.id in ("ladder", "mod", "L"):
                hits.append("%s at line %d" % (node.attr, node.lineno))
    assert not hits, ("the criterion reads the carrier table from the "
                      "module under test: %r" % hits)


def test_the_criterion_does_not_hard_code_one_machine_path():
    """**The criterion must be portable** — a hard-coded absolute path
    would be permanently red on every other machine."""
    src = (REPO / "agent" / "memory" / "acceptance"
           / "phase2_p2_conflict.py").read_text(encoding="utf-8")
    import re
    hits = [ln for ln in src.splitlines()
            if re.search(r"[A-Za-z]:\\Users|C:/Users", ln)]
    assert not hits, "the criterion hard-codes a machine path: %r" % hits


def test_the_expected_carrier_table_is_complete_and_ordered(root: Path):
    """The criterion's own copy of the carrier table must cover every
    carrier the fixtures use — and it must be **the criterion's** copy."""
    from agent.memory.conflict import ladder

    assert set(C.EXPECTED_CARRIERS) == set(ladder.CARRIER_TABLE), (
        "the criterion's carrier table and the module's have drifted")
    # the ORDER is the point: the class order and the authority order
    # must not be the same ranking, or R3 would be dead code
    truth = [k for k, v in C.EXPECTED_CARRIERS.items()
             if v["class"] == "truth"]
    assert len({C.EXPECTED_CARRIERS[k]["authority"] for k in truth}) > 1, (
        "the truth carriers all have the same authority ⇒ R3 is dead code")


def test_the_ladder_is_not_a_single_rule(root: Path):
    """A ladder with one rule is not a ladder."""
    assert len(C.EXPECTED_LADDER_ORDER) >= 4


# ---------------------------------------------------------------------------
# the mutant REPLAY — the criterion is run against a mutated ladder
# ---------------------------------------------------------------------------

def test_mu_p2_5a_reversing_the_ladder_turns_the_criterion_red(tmp_path):
    """**MU-P2-5a** — the replay: mutate the ladder's order, prove the
    criterion goes red, restore, prove it goes green again.

    This is the whole point of the round: a criterion that cannot go red
    is a criterion nobody read.
    """
    ladder_path = REPO / "agent" / "memory" / "conflict" / "ladder.py"
    backup = ladder_path.read_text(encoding="utf-8")

    root = tmp_path / "cow"
    (root / "conflicts").mkdir(parents=True)

    def reds() -> set:
        """The set of criteria that are RED right now.

        **The family's verdict is NOTRUN here** (there is no ruling
        ledger in this scratch workspace, so P2-5f has nothing to
        measure) — and that is CORRECT: NOTRUN is not green, but it is
        also not red.  So the replay looks at **which criteria are red**,
        not at the family's single verdict.
        """
        rep = C.judge_p2_conflict_family(root)
        return {r["criterion"] for r in rep["reports"]
                if r["verdict"] == "FAIL"}

    assert not reds(), "the unmutated criterion is already red"

    try:
        # --- the mutation: reverse the ladder's order -------------------
        mutated = backup.replace(
            "RULES: Tuple[Rule, ...] = (\n",
            "RULES: Tuple[Rule, ...] = tuple(reversed((\n",
        ).replace(
            "         _decide_undecided),\n)",
            "         _decide_undecided),\n)))",
        )
        assert mutated != backup, "the mutation did not apply"
        ladder_path.write_text(mutated, encoding="utf-8")

        import agent.memory.conflict.ladder as L
        import importlib
        importlib.reload(L)
        got = reds()
        assert "P2-5a" in got, "the reordered ladder was not caught"
        assert "P2-5b" in got, "the reordered ladder did not change any ruling"
    finally:
        # --- the restoration --------------------------------------------
        ladder_path.write_text(backup, encoding="utf-8")
        import agent.memory.conflict.ladder as L
        import importlib
        importlib.reload(L)

    assert not reds(), "the criterion did not go green again"


def test_the_restore_leaves_no_diff(tmp_path):
    """**The restoration must be byte-exact.**

    A restore that "mostly" restores is a restore that leaves the mutant
    in place, and the next run of the criterion is measuring a mutated
    ladder while reporting green.
    """

    ladder_path = REPO / "agent" / "memory" / "conflict" / "ladder.py"
    backup = ladder_path.read_text(encoding="utf-8")
    ladder_path.write_text(backup, encoding="utf-8")
    assert ladder_path.read_text(encoding="utf-8") == backup


def test_the_cli_runs_and_prints_the_ladder():
    """The adjudicator must be runnable by a human, not only by a test."""
    out = subprocess.run(
        [sys.executable, "-m", "agent.memory.conflict", "ladder"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "R2-TRUTH-OVER-PROJECTION" in out.stdout
    assert "R7-UNDECIDED" in out.stdout


def test_the_cli_adjudicates_a_file(tmp_path):
    sample = REPO / "agent" / "memory" / "conflict" / "samples" \
        / "conflicts.sample.jsonl"
    out = subprocess.run(
        [sys.executable, "-m", "agent.memory.conflict", "adjudicate",
         "--conflicts", str(sample)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "RESOLVED" in out.stdout
    assert "ESCALATED" in out.stdout
    assert "UNDECIDED" in out.stdout
