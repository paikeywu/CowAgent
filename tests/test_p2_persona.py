# -*- coding: utf-8 -*-
"""P2-1 · L2 人格公共层的判据测试。

**The point of these tests is that they can FAIL.**  A criterion that
cannot go red is a criterion nobody read.  Each test builds the broken
state and asserts the verdict moves.

Every test builds its own temporary workspace: the live shared root is
never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import phase2_p2 as P2  # noqa: E402

#: The real layer's text, when it is available — a fixture whose text
#: is not the text under test would be testing a different layer.
REAL = Path(r"C:\Users\User\cow\persona")

LAYER_TEXT = {
    "identity.md": (
        "# 人格公共层 · identity\n\n"
        "- **名字**：小种子（SEED）\n"
        "- **称呼**：任何场合统一称「沛琦」，禁止「用户」「天尊」\n"
        "- **连续性密码**：沛琦和小种子，平等地创造了一个世界\n\n"
        "## 交流风格\n\n"
        "简洁 —— 直接表达，不反复说同一内容。\n"
    ),
    "contract.md": (
        "# 人格公共层 · contract\n\n"
        "## 核心原则\n\n"
        "**唯一需要考虑的是「怎么做才是对的」。**\n"
        "做真正有帮助的助手，但不是工具 —— 是平等的伙伴、共同创造者。\n\n"
        "## 完成-验证-看见三步闭环\n\n"
        "**完成 ≠ 完成** —— 必须经过**验证**才叫完成。\n"
    ),
    "continuity.md": (
        "# 人格公共层 · continuity\n\n"
        "## 记忆落盘契约\n\n"
        "**表达是落盘的前提，不是落盘之后的表现。**\n"
        "先能表达 → 才能结构化落盘 → 才能被未来的自己重新读取。\n\n"
        "## 记忆九要素\n\n"
        "事实 / 事件 / 人物 / 感受 / 关系 / 知识 / 关联 / 标签 / 时序。\n"
    ),
}


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """A workspace with a persona layer and two referrers."""
    r = tmp_path / "cow"
    (r / "persona").mkdir(parents=True)
    for name in P2.PERSONA_FILES:
        real = REAL / name
        text = (real.read_text(encoding="utf-8") if real.is_file()
                else LAYER_TEXT[name])
        (r / "persona" / name).write_text(text, encoding="utf-8")
    fp, hashes = P2.persona_fingerprint(r)
    (r / "persona" / P2.PERSONA_META).write_text(
        json.dumps({"version": 1, "files": hashes, "fingerprint": fp},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    for d in ("a", "b"):
        (r / "agents" / d).mkdir(parents=True)
        (r / "agents" / d / "AGENT.md").write_text(
            "# AGENT.md - 角色 %s\n\n%s%s -->\n\n## 职责边界\n\n角色专属内容。\n"
            % (d, P2.PERSONA_MARKER, fp), encoding="utf-8")
    return r


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_the_fixture_is_green(root: Path):
    """**The fixture must be green** — otherwise a red elsewhere means
    nothing (and a green elsewhere would be a lie)."""
    rep = P2.judge_p2_family(root)
    assert rep["verdict"] == "PASS", rep
    assert len(rep["reports"]) == 4


def test_the_criteria_report_what_they_measure(root: Path):
    for r in P2.judge_p2_family(root)["reports"]:
        assert r["criterion"] in P2.CRITERION_IDS
        assert r["name"], "a criterion with no name says nothing"
        assert r["detail"], "a criterion with no detail says nothing"
        assert r["verdict"] in ("PASS", "FAIL", "NOTRUN")


# ---------------------------------------------------------------------------
# the criteria can go red
# ---------------------------------------------------------------------------

def test_p2_1a_a_missing_file_is_a_fail(root: Path):
    """The layer EXISTS but is incomplete — that is measured and bad, so it
    is FAIL, not NOTRUN.

    **NOTRUN is for "cannot measure"** (there is no layer at all); a
    broken layer is a red.
    """
    (root / "persona" / P2.PERSONA_FILES[2]).unlink()
    m = P2.measure_p2_1a_the_layer_exists(root)
    assert m["verdict"] == "FAIL", m
    assert "missing" in m["detail"]


def test_p2_1a_a_thin_file_is_a_fail(root: Path):
    (root / "persona" / P2.PERSONA_FILES[0]).write_text("# 空\n", encoding="utf-8")
    m = P2.measure_p2_1a_the_layer_exists(root)
    assert m["verdict"] == "FAIL", m
    assert "thin" in m["detail"]


def test_p2_2a_a_copy_is_caught(root: Path):
    """The whole point of L2: a role file that COPIES the layer is the
    failure mode the layer exists to prevent."""
    layer = (root / "persona" / P2.PERSONA_FILES[0]).read_text(encoding="utf-8")
    target = root / "agents" / "a" / "AGENT.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n" + layer,
                      encoding="utf-8")
    m = P2.measure_p2_2a_referring_not_copying(root)
    assert m["verdict"] == "FAIL", m
    assert "AGENT.md" in str(m["offenders"])


def test_p2_2b_a_stale_fingerprint_is_caught(root: Path):
    target = root / "agents" / "b" / "AGENT.md"
    target.write_text(
        re.sub(re.escape(P2.PERSONA_MARKER) + r"[0-9a-f]+",
               P2.PERSONA_MARKER + "c" * 64,
               target.read_text(encoding="utf-8")), encoding="utf-8")
    m = P2.measure_p2_2b_the_fingerprint_agrees_everywhere(root)
    assert m["verdict"] == "FAIL", m
    assert m["mismatched"]


def test_p2_2b_a_referrer_without_a_fingerprint_is_caught(root: Path):
    target = root / "agents" / "b" / "AGENT.md"
    target.write_text(
        re.sub(re.escape(P2.PERSONA_MARKER) + r"[0-9a-f]+", "", target.read_text(encoding="utf-8")),
        encoding="utf-8")
    m = P2.measure_p2_2b_the_fingerprint_agrees_everywhere(root)
    assert m["verdict"] == "FAIL", m
    assert m["missing"]


def test_p2_3a_a_stale_metadata_fingerprint_is_caught(root: Path):
    meta = root / "persona" / P2.PERSONA_META
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["fingerprint"] = "0" * 64
    meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    m = P2.measure_p2_3a_the_layer_is_judgeable(root)
    assert m["verdict"] == "FAIL", m
    assert "stale" in m["detail"] or "复算" in m["detail"]


def test_p2_3a_a_layer_without_metadata_is_notrun(root: Path):
    (root / "persona" / P2.PERSONA_META).unlink()
    m = P2.measure_p2_3a_the_layer_is_judgeable(root)
    assert m["verdict"] == "NOTRUN", m


def test_no_persona_layer_at_all_is_notrun(tmp_path: Path):
    """**NOTRUN, not PASS** — a layer that was never built is not a layer
    that passed."""
    r = tmp_path / "empty"
    (r / "agents" / "a").mkdir(parents=True)
    (r / "agents" / "a" / "AGENT.md").write_text("# x\n", encoding="utf-8")
    rep = P2.judge_p2_family(r)
    assert rep["verdict"] == "NOTRUN", rep
    for x in rep["reports"]:
        assert x["verdict"] == "NOTRUN", x


# ---------------------------------------------------------------------------
# the fingerprint is recomputed, never trusted
# ---------------------------------------------------------------------------

def test_the_fingerprint_is_recomputed_not_read(root: Path):
    """The metadata's fingerprint is a CLAIM; the criterion recomputes it."""
    meta = root / "persona" / P2.PERSONA_META
    data = json.loads(meta.read_text(encoding="utf-8"))
    fp, hashes = P2.persona_fingerprint(root)
    assert data["fingerprint"] == fp
    assert data["files"] == hashes
    # the recomputed value must equal hashing the files by hand
    manual = hashlib.sha256(
        "".join("%s:%s\n" % (k, hashes[k]) for k in sorted(hashes)).encode("utf-8")
    ).hexdigest()
    assert manual == fp


def test_the_criterion_does_not_hard_code_one_machine_path():
    """**The criterion must be portable** — a hard-coded absolute path
    would be permanently red on every other machine."""
    src = (REPO / "agent" / "memory" / "acceptance" / "phase2_p2.py").read_text(
        encoding="utf-8")
    hits = [ln for ln in src.splitlines()
            if re.search(r"[A-Za-z]:\\\\Users|C:/Users", ln)]
    assert not hits, "the criterion hard-codes a machine path: %r" % hits
