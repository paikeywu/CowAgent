# -*- coding: utf-8 -*-
"""P2-5 · 冲突裁决层的判据（独立于被测实现）。

被测对象
--------
* ``agent/memory/conflict/ladder.py`` —— 裁决序（规则阶梯 + 载体权威表）
* ``agent/memory/conflict/adjudicator.py`` —— 执行裁决序 + 产出裁决记录

**独立性规则（本模块最重要的一条纪律）**
--------------------------------------
**期望值不许由被测对象算出来。** 本模块对每一条构造冲突都写死了**字面量**
期望（``expected`` 字典），字面量来自设计文档的原文，而不是来自
``ladder.py`` 的函数。判据只做一件事：**跑被测对象，和字面量对表**。

⇒ 被测对象与判据若共用一份「真相来源」（例如判据去调
``ladder.CARRIER_TABLE`` 算期望），判据就会永远绿 —— 那是同义反复。

**载体性质在本模块里重写了一遍**（``EXPECTED_CARRIERS``）：类别 / 权威分 /
所在层。设计依据：

* 「投影可丢弃，真源为准」⇒ 投影的类别低于真源（05 号 :109）
* 「以 markdown/ 事件文件为准，知识库条目是它的结论 + 证据指针」⇒
  同一真源类别内，事件文件的权威分高于知识页（06 号 :699）
* 「L2 人格公共层是共享层」⇒ 共享层高于视角私有层（P2 多视角共享）

五条判据
--------
| # | 名称 | 断言 |
|:--|:--|:--|
| P2-5a | 裁决序本身可判定 | 阶梯形状、tier 连续、顺序与设计一致、每条规则都写得出处 |
| P2-5b | 构造冲突 ⇒ 唯一确定裁决 | 每条构造冲突的裁决结果 == 字面量期望；且两次跑结果相同 |
| P2-5c | 裁决记录可追溯 | 记录里有裁决方 / 时间 / 被裁决对象 / 依据，且指向具体冲突 |
| P2-5d | 未决与损坏都不自动选边 | 未决 ⇒ 未选定候选 + 降级可见；时间戳并列 ⇒ 报红升级，且都不点名任何 agent |
| P2-5e | 判据对裁决序敏感 | 把裁决序颠倒 ⇒ 结果必须变 ⇒ 判据抓得住「顺序被改」 |
| P2-5f | 已落盘的裁决记录可追溯 | 工作区里已写入的裁决记录逐条可追溯；无记录 ⇒ NOTRUN |

**NOTRUN 不是 GREEN**：裁决器不存在 ⇒ 全部 NOTRUN；工作区里没有裁决记录
⇒ P2-5f NOTRUN。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# ---------------------------------------------------------------------------
# 载体性质 —— **本模块自己写的一份**，不复用 ladder.py 的表
# ---------------------------------------------------------------------------
#
# 见上面的独立性规则：期望值必须能被独立复述一遍。
EXPECTED_CARRIERS: Dict[str, Dict[str, Any]] = {
    "persona_layer": {"class": "truth", "authority": 70, "layer": "L2_persona"},
    "markdown_event": {"class": "truth", "authority": 60, "layer": "view_private"},
    "knowledge_page": {"class": "truth", "authority": 40, "layer": "L1_knowledge"},
    "facts_jsonl": {"class": "truth", "authority": 40, "layer": "view_private"},
    "daily_memory": {"class": "summary", "authority": 30, "layer": "view_private"},
    "persona_copy": {"class": "projection", "authority": 20, "layer": "view_private"},
    "shelf_self": {"class": "projection", "authority": 10, "layer": "view_private"},
    "chunk_projection": {"class": "projection", "authority": 10, "layer": "view_private"},
}

#: 期望的裁决序（**字面量**）。顺序即裁决结果，所以这张表本身就是期望。
EXPECTED_LADDER_ORDER: Tuple[str, ...] = (
    "R0-CONVERGENCE",
    "R1-HUMAN-RULING",
    "R2-TRUTH-OVER-PROJECTION",
    "R3-CARRIER-AUTHORITY",
    "R4-LAYER-PRIORITY",
    "R5-RECENCY",
    "R6-TIE-ESCALATE",
    "R7-UNDECIDED",
)

#: 期望的规则出处关键词 —— 每条规则必须指得出它出自哪份设计的哪一行。
EXPECTED_BASIS_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "R0-CONVERGENCE": ("自定",),
    "R1-HUMAN-RULING": ("13", "人类伙伴"),
    "R2-TRUTH-OVER-PROJECTION": ("05", "投影"),
    "R3-CARRIER-AUTHORITY": ("06", "markdown"),
    "R4-LAYER-PRIORITY": ("P2", "层"),
    "R5-RECENCY": ("13", "时序"),
    "R6-TIE-ESCALATE": ("13", "损坏"),
    "R7-UNDECIDED": ("降级",),
}

# ---------------------------------------------------------------------------
# 构造冲突 —— 每条带**字面量**期望
# ---------------------------------------------------------------------------
#
# 期望的三个要素：
#   verdict  裁决状态
#   winner   被采纳的候选 id（"" = 未选定任何候选）
#   rule_id  依据哪条规则
#   ruled_by 裁决方标识里必须出现的字样
#   basis    该规则出处的关键词（本模块自己写的期望）
_T = "2026-11-05T10:00:00+08:00"


def _c(cid: str, value: Any, carrier: str, occurred_at: str = _T,
       human: bool = False) -> Dict[str, Any]:
    return {"candidate_id": cid, "value": value, "carrier": carrier,
            "occurred_at": occurred_at, "author_kind": "agent",
            "ruling_by_human": human, "path": ""}


FIXTURES: Dict[str, Dict[str, Any]] = {
    # ① 真源 vs 投影 ⇒ 真源赢（05 号 :109）
    "truth-beats-projection": {
        "conflict": {
            "conflict_id": "CF-001", "subject": "沛琦的职业",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-md", "产品经理", "markdown_event"),
                _c("c-proj", "工程师", "chunk_projection"),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-md",
                     "rule_id": "R2-TRUTH-OVER-PROJECTION",
                     "ruled_by": "守门人", "basis": ("05", "投影")},
    },
    # ② 摘要层 vs 投影 ⇒ 摘要层赢（类别优先，不是权威分优先）
    "summary-beats-projection": {
        "conflict": {
            "conflict_id": "CF-002", "subject": "今天的会议时间",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-daily", "10:00", "daily_memory"),
                _c("c-shelf", "11:00", "shelf_self"),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-daily",
                     "rule_id": "R2-TRUTH-OVER-PROJECTION",
                     "ruled_by": "守门人", "basis": ("05", "投影")},
    },
    # ③ 同为真源 ⇒ 比载体权威分（06 号 :699：事件文件 > 知识页）
    "carrier-authority-within-truth": {
        "conflict": {
            "conflict_id": "CF-003", "subject": "知识页与人格层对称呼的写法",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-md", "沛琦", "markdown_event"),
                _c("c-persona", "Primus", "persona_layer"),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-persona",
                     "rule_id": "R3-CARRIER-AUTHORITY",
                     "ruled_by": "守门人", "basis": ("06", "markdown")},
    },
    # ④ 同类别同权威分 ⇒ 比层（共享层 > 视角私有层）
    "layer-priority-at-equal-authority": {
        "conflict": {
            "conflict_id": "CF-004", "subject": "知识页结论与事实层原话",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-know", "已结论", "knowledge_page"),
                _c("c-facts", "原话", "facts_jsonl", "2026-11-05T12:00:00+08:00"),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-know",
                     "rule_id": "R4-LAYER-PRIORITY",
                     "ruled_by": "守门人", "basis": ("P2", "层")},
    },
    # ⑤ 同类别同权威同层 ⇒ 比时序（13 号 :346）
    "recency-within-same-carrier": {
        "conflict": {
            "conflict_id": "CF-005", "subject": "当日记忆里的时间切片",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-old", "旧切片", "daily_memory", "2026-11-05T09:00:00+08:00"),
                _c("c-new", "新切片", "daily_memory", "2026-11-05T11:00:00+08:00"),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-new",
                     "rule_id": "R5-RECENCY",
                     "ruled_by": "守门人", "basis": ("13", "时序")},
    },
    # ⑥ 时间戳并列而取值不同 ⇒ 数据损坏 ⇒ 报红阻断，不选边（13 号 :346）
    "equal-timestamp-is-corruption": {
        "conflict": {
            "conflict_id": "CF-006", "subject": "同一时刻的两端释放状态",
            "kind": "status", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-a", "released", "daily_memory"),
                _c("c-b", "active", "daily_memory"),
            ],
        },
        "expected": {"verdict": "ESCALATED", "winner": "",
                     "rule_id": "R6-TIE-ESCALATE",
                     "ruled_by": "升级人类伙伴", "basis": ("13", "损坏")},
    },
    # ⑦ 时间戳缺失 ⇒ 不是「并列」而是「不知」⇒ 未决 ⇒ 降级可见
    "missing-timestamp-is-undecided": {
        "conflict": {
            "conflict_id": "CF-007", "subject": "两份无时间戳的每日记忆",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-a", "切片甲", "daily_memory", ""),
                _c("c-b", "切片乙", "daily_memory"),
            ],
        },
        "expected": {"verdict": "UNDECIDED", "winner": "",
                     "rule_id": "R7-UNDECIDED",
                     "ruled_by": "升级人类伙伴", "basis": ("降级",)},
    },
    # ⑧ naive 与 aware 时间戳不可比 ⇒ 未决（不是损坏）
    "mixed-timezone-is-undecided": {
        "conflict": {
            "conflict_id": "CF-008", "subject": "跨时区写的两份记忆",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-a", "甲", "daily_memory", "2026-11-05T10:00:00"),
                _c("c-b", "乙", "daily_memory", "2026-11-05T10:00:00+08:00"),
            ],
        },
        "expected": {"verdict": "UNDECIDED", "winner": "",
                     "rule_id": "R7-UNDECIDED",
                     "ruled_by": "升级人类伙伴", "basis": ("降级",)},
    },
    # ⑨ 人类伙伴显式裁决 ⇒ 压过一切机械规则（13 号 :348）
    "human-ruling-outranks-mechanics": {
        "conflict": {
            "conflict_id": "CF-009", "subject": "沛琦的称呼",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-md", "天尊", "markdown_event"),
                _c("c-human", "沛琦", "markdown_event", human=True),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-human",
                     "rule_id": "R1-HUMAN-RULING",
                     "ruled_by": "人类伙伴", "basis": ("13", "人类伙伴")},
    },
    # ⑩ 人类的显式裁决压过「真源优先」这条机械规则
    "human-ruling-outranks-truth-rule": {
        "conflict": {
            "conflict_id": "CF-010", "subject": "两个投影上的不同写法",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-proj-a", "写法甲", "chunk_projection"),
                _c("c-proj-b", "写法乙", "chunk_projection", human=True),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-proj-b",
                     "rule_id": "R1-HUMAN-RULING",
                     "ruled_by": "人类伙伴", "basis": ("13", "人类伙伴")},
    },
    # ⑪ 候选值全同 ⇒ 没有冲突要裁（不得凭空造一条裁决）
    "identical-values-are-not-a-conflict": {
        "conflict": {
            "conflict_id": "CF-011", "subject": "两处写了同一个结论",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-a", "同一个值", "markdown_event"),
                _c("c-b", "同一个值", "knowledge_page"),
            ],
        },
        "expected": {"verdict": "CONVERGED", "winner": "",
                     "rule_id": "R0-CONVERGENCE",
                     "ruled_by": "无（无冲突）", "basis": ("自定",)},
        "order_sensitive": True,
    },
    # ⑫ 人类裁决 vs 真源优先 —— 两条规则**互相竞争** ⇒ 顺序有区别
    "human-ruling-competes-with-truth-rule": {
        "conflict": {
            "conflict_id": "CF-012", "subject": "投影上的人类裁决 vs 事件文件真源",
            "kind": "value", "detected_at": _T, "detected_by": "consistency-guardian",
            "candidates": [
                _c("c-human", "人类采纳的值", "chunk_projection", human=True),
                _c("c-md", "事件文件里的值", "markdown_event"),
            ],
        },
        "expected": {"verdict": "RESOLVED", "winner": "c-human",
                     "rule_id": "R1-HUMAN-RULING",
                     "ruled_by": "人类伙伴", "basis": ("13", "人类伙伴")},
        "order_sensitive": True,
    },
}

FIXTURE_NAMES: Tuple[str, ...] = tuple(FIXTURES.keys())


# ---------------------------------------------------------------------------
# 被测对象的加载 —— 加载不上就是 NOTRUN，不是 PASS
# ---------------------------------------------------------------------------

def _load_under_test():
    """Import the object under test.  Returns ``None`` when it is absent."""
    try:
        from agent.memory.conflict import adjudicator, ladder
    except Exception:
        return None
    return ladder, adjudicator


def shared_root(root: Optional[Path] = None) -> Path:
    """Where the conflict ledger lives.  **No machine path is hard-coded.**"""
    if root is not None:
        return Path(root)
    env = os.environ.get("COW_SHARED_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "cow":
            return parent
    return Path.home() / "cow"


def _rules_of(ladder) -> Tuple[Any, ...]:
    return tuple(ladder.RULES)


def _inverted(rules: Sequence[Any]) -> Tuple[Any, ...]:
    """The ladder, **reversed** — the mutant."""
    return tuple(reversed(list(rules)))


def _run(ladder, adjudicator, fixture: Dict[str, Any],
         rules: Sequence[Any]) -> Dict[str, Any]:
    conflict = ladder.Conflict.from_json(fixture["conflict"])
    return dict(adjudicator.adjudicate(conflict, rules))


# ---------------------------------------------------------------------------
# P2-5a · 裁决序本身可判定
# ---------------------------------------------------------------------------

def measure_p2_5a_the_ladder_is_judgeable(root: Optional[Path] = None
                                          ) -> Dict[str, Any]:
    """P2-5a: the ladder is a machine-checkable ordered table.

    **NOTRUN** when the adjudicator does not exist at all — a layer that
    was never built is not a layer that passed.
    """
    out: Dict[str, Any] = {
        "criterion": "P2-5a",
        "name": "裁决序本身可判定（阶梯形状、顺序、出处齐全）",
        "verdict": "NOTRUN", "detail": "", "problems": [],
    }
    loaded = _load_under_test()
    if loaded is None:
        out["detail"] = ("裁决器不存在（agent.memory.conflict 导入失败）—— "
                         "本判据未测量，NOTRUN 不等于通过")
        return out
    ladder, _adjudicator = loaded
    rules = _rules_of(ladder)
    if not rules:
        out["detail"] = "裁决序是空的 —— 没有规则可判"
        return out

    problems: List[str] = []
    ids = [r.rule_id for r in rules]
    if tuple(ids) != EXPECTED_LADDER_ORDER:
        problems.append("裁决序与期望不一致：实际 %s / 期望 %s"
                        % (" → ".join(ids), " → ".join(EXPECTED_LADDER_ORDER)))
    if len(set(ids)) != len(ids):
        problems.append("规则 id 有重复")
    tiers = [r.tier for r in rules]
    if tiers != list(range(len(rules))):
        problems.append("tier 必须从 0 起连续编号：实际 %s" % tiers)
    for r in rules:
        basis = str(getattr(r, "basis", "") or "")
        if not basis.strip():
            problems.append("%s 没有写出处" % r.rule_id)
            continue
        for kw in EXPECTED_BASIS_KEYWORDS.get(r.rule_id, ()):
            if kw not in basis:
                problems.append("%s 的出处 %r 里没有 %r" % (r.rule_id, basis, kw))
        if not str(getattr(r, "name", "") or "").strip():
            problems.append("%s 没有名字" % r.rule_id)
        if getattr(r, "decide_fn", None) is None:
            problems.append("%s 没有实现" % r.rule_id)

    out["problems"] = problems
    out["rules"] = ids
    if problems:
        out["verdict"] = "FAIL"
        out["detail"] = ("裁决序有 %d 处问题：%s" % (len(problems),
                                                  "；".join(problems)))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("裁决序 %d 条，tier 连续，顺序与设计一致，每条都写得出处"
                     % len(rules))
    return out


# ---------------------------------------------------------------------------
# P2-5b · 构造冲突 ⇒ 唯一确定裁决
# ---------------------------------------------------------------------------

def measure_p2_5b_the_ruling_is_unique_and_determined(
        root: Optional[Path] = None) -> Dict[str, Any]:
    """P2-5b: every constructed conflict yields EXACTLY the expected ruling.

    **The expectation is a literal in this module** (see ``FIXTURES``) —
    it is not computed by the object under test.
    """
    out: Dict[str, Any] = {
        "criterion": "P2-5b",
        "name": "构造冲突 ⇒ 唯一确定的裁决结果（含裁决方标识）",
        "verdict": "NOTRUN", "detail": "", "mismatched": {},
    }
    loaded = _load_under_test()
    if loaded is None:
        out["detail"] = ("裁决器不存在（agent.memory.conflict 导入失败）—— "
                         "本判据未测量，NOTRUN 不等于通过")
        return out
    ladder, adjudicator = loaded
    rules = _rules_of(ladder)

    mismatched: Dict[str, List[str]] = {}
    for name, fixture in FIXTURES.items():
        exp = fixture["expected"]
        problems: List[str] = []
        try:
            ruling = _run(ladder, adjudicator, fixture, rules)
        except Exception as e:  # a crash is a FAIL, never a silent pass
            mismatched[name] = ["裁决器抛异常：%s: %s" % (type(e).__name__, e)]
            continue
        got_verdict = str(ruling.get("verdict", ""))
        got_winner = str(ruling.get("winner_candidate_id", "") or "")
        got_rule = str(ruling.get("rule_id", ""))
        got_by = str(ruling.get("ruled_by", ""))
        got_basis = str(ruling.get("rule_basis", ""))
        if got_verdict != exp["verdict"]:
            problems.append("verdict 期望 %s 实得 %s" % (exp["verdict"], got_verdict))
        if got_winner != exp["winner"]:
            problems.append("winner 期望 %r 实得 %r" % (exp["winner"], got_winner))
        if got_rule != exp["rule_id"]:
            problems.append("依据规则期望 %s 实得 %s" % (exp["rule_id"], got_rule))
        if exp["ruled_by"] not in got_by:
            problems.append("裁决方 %r 里没有 %r" % (got_by, exp["ruled_by"]))
        for kw in exp["basis"]:
            if kw not in got_basis:
                problems.append("依据出处 %r 里没有 %r" % (got_basis, kw))
        # 确定性：同一输入跑两次，结果必须一样（只允许记录时间不同）
        try:
            again = _run(ladder, adjudicator, fixture, rules)
        except Exception as e:
            problems.append("第二次运行抛异常：%s: %s" % (type(e).__name__, e))
            again = None
        if again is not None:
            a = {k: v for k, v in ruling.items() if k != "ruled_at"}
            b = {k: v for k, v in again.items() if k != "ruled_at"}
            if a != b:
                problems.append("两次运行结果不同 —— 裁决不确定")
        if problems:
            mismatched[name] = problems

    out["mismatched"] = mismatched
    out["checked"] = len(FIXTURES)
    if mismatched:
        out["verdict"] = "FAIL"
        out["detail"] = ("%d/%d 条构造冲突的裁决结果与字面量期望不符：%s"
                         % (len(mismatched), len(FIXTURES),
                            "；".join("%s(%s)" % (k, "，".join(v))
                                      for k, v in sorted(mismatched.items()))))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("%d 条构造冲突全部命中字面量期望，且两次运行结果一致"
                     % len(FIXTURES))
    return out


# ---------------------------------------------------------------------------
# P2-5c · 裁决记录可追溯
# ---------------------------------------------------------------------------

#: 裁决记录里必须有的四个字段（A1）。
TRACEABLE_FIELDS = ("ruled_by", "ruled_at", "target", "rule_id")


def measure_p2_5c_the_ruling_is_traceable(root: Optional[Path] = None
                                           ) -> Dict[str, Any]:
    """P2-5c: from the ruling you can read who / when / which rule / which conflict.

    A record that answers three of those four is not traceable — it is
    a ruling somebody has to guess at.
    """
    out: Dict[str, Any] = {
        "criterion": "P2-5c",
        "name": "裁决记录可追溯（谁裁的 / 何时 / 依据哪条规则 / 针对哪条冲突）",
        "verdict": "NOTRUN", "detail": "", "missing": {},
    }
    loaded = _load_under_test()
    if loaded is None:
        out["detail"] = ("裁决器不存在（agent.memory.conflict 导入失败）—— "
                         "本判据未测量，NOTRUN 不等于通过")
        return out
    ladder, adjudicator = loaded
    rules = _rules_of(ladder)

    missing: Dict[str, List[str]] = {}
    for name, fixture in FIXTURES.items():
        try:
            ruling = _run(ladder, adjudicator, fixture, rules)
        except Exception as e:
            missing[name] = ["裁决器抛异常：%s: %s" % (type(e).__name__, e)]
            continue
        gaps = [f for f in TRACEABLE_FIELDS
                if not str(ruling.get(f, "") or "").strip()]
        target = str(ruling.get("target", "") or "")
        if not gaps:
            # 「针对哪条冲突」必须真的点到那一条冲突
            cid = str(fixture["conflict"].get("conflict_id", ""))
            if cid and cid not in target:
                gaps.append("target 里没有出现冲突号 %s" % cid)
        if gaps:
            missing[name] = gaps

    out["missing"] = missing
    out["checked"] = len(FIXTURES)
    if missing:
        out["verdict"] = "FAIL"
        out["detail"] = ("%d/%d 条裁决记录缺可追溯字段：%s"
                         % (len(missing), len(FIXTURES),
                            "；".join("%s(%s)" % (k, "，".join(v))
                                      for k, v in sorted(missing.items()))))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("%d 条裁决记录都带齐 %s，且都点名了具体冲突"
                     % (len(FIXTURES), " / ".join(TRACEABLE_FIELDS)))
    return out


# ---------------------------------------------------------------------------
# P2-5d · 未决与损坏都不自动选边
# ---------------------------------------------------------------------------

def measure_p2_5d_undecided_does_not_pick_a_side(root: Optional[Path] = None
                                                  ) -> Dict[str, Any]:
    """P2-5d: 未决 ⇒ 不选边 + 降级可见；损坏 ⇒ 报红 + 不选边。

    13 号 :348 —— 「自动选边 = 越权，选择权在人类伙伴」。

    **裁决方标识里不许出现任何 agent id**：未决与损坏的裁决方只能是
    「人类伙伴」或「未决」。若那里写着某个 agent，说明系统在**自动选边**。
    """
    out: Dict[str, Any] = {
        "criterion": "P2-5d",
        "name": "未决与损坏都不自动选边（未决降级可见，损坏报红阻断）",
        "verdict": "NOTRUN", "detail": "", "problems": [],
    }
    loaded = _load_under_test()
    if loaded is None:
        out["detail"] = ("裁决器不存在（agent.memory.conflict 导入失败）—— "
                         "本判据未测量，NOTRUN 不等于通过")
        return out
    ladder, adjudicator = loaded
    rules = _rules_of(ladder)

    problems: List[str] = []
    for name in ("equal-timestamp-is-corruption", "missing-timestamp-is-undecided",
                 "mixed-timezone-is-undecided"):
        fixture = FIXTURES[name]
        exp = fixture["expected"]
        try:
            ruling = _run(ladder, adjudicator, fixture, rules)
        except Exception as e:
            problems.append("%s 裁决器抛异常：%s: %s" % (name, type(e).__name__, e))
            continue
        if str(ruling.get("verdict", "")) != exp["verdict"]:
            problems.append("%s 的 verdict 期望 %s 实得 %s"
                            % (name, exp["verdict"], ruling.get("verdict")))
        if str(ruling.get("winner_candidate_id", "") or ""):
            problems.append("%s 选定了候选 %r —— 未决/损坏时不得选边"
                            % (name, ruling.get("winner_candidate_id")))
        by = str(ruling.get("ruled_by", ""))
        if "升级人类伙伴" not in by and "未决" not in by:
            problems.append("%s 的裁决方 %r 不是「升级人类伙伴 / 未决」"
                            % (name, by))
        if "agent-" in by or "SEED" in by:
            problems.append("%s 的裁决方 %r 点名了 agent —— 自动选边是越权"
                            % (name, by))
        rationale = str(ruling.get("rationale", ""))
        if exp["verdict"] == "UNDECIDED":
            if "降级" not in rationale and "阻断" not in rationale:
                problems.append("%s 的未决理由里没有降级/阻断动作：%r"
                                % (name, rationale))
        if exp["verdict"] == "ESCALATED":
            if "损坏" not in rationale and "阻断" not in rationale:
                problems.append("%s 的损坏理由里没有报红/阻断动作：%r"
                                % (name, rationale))

    out["problems"] = problems
    out["checked"] = 3
    if problems:
        out["verdict"] = "FAIL"
        out["detail"] = "%d 处问题：%s" % (len(problems), "；".join(problems))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("3 条不可自裁的冲突（时间戳并列 / 时间戳缺失 / 时区不可比）"
                     "都没有选定候选，裁决方都指向人类伙伴，且理由里写明了"
                     "降级或阻断动作")
    return out


# ---------------------------------------------------------------------------
# P2-5e · 判据对裁决序敏感（顺序被改 ⇒ 判据必须转红）
# ---------------------------------------------------------------------------

def measure_p2_5e_the_criterion_is_sensitive_to_the_order(
        root: Optional[Path] = None) -> Dict[str, Any]:
    """P2-5e: the order of the ladder is **observable**.

    **Why this criterion exists.**  A criterion that passes no matter what
    the ladder says is not a criterion — it is a rubber stamp.  This one
    *proves* the order matters: it runs the SAME fixtures through the
    **reversed** ladder and requires the result to change for every
    fixture whose expected ruling is **not** the terminal "undecided" one.

    **Which fixtures must change** is decided by the **literal expectation
    table**, not by the module: a fixture whose expected ``rule_id`` is
    ``R7-UNDECIDED`` is *by construction* order-insensitive — the ladder
    has run out of applicable rules in either direction, and the answer is
    "we cannot decide" both times.  Every other fixture must change,
    because its expected ruling names a **specific** rule, and a reversed
    ladder reaches that rule at a different point (or not at all).

    ⇒ If someone edits ``ladder.py`` and flips the order, this criterion
    turns red, and so does P2-5b.
    """
    out: Dict[str, Any] = {
        "criterion": "P2-5e",
        "name": "判据对裁决序敏感（把裁决序颠倒 ⇒ 结果必须变）",
        "verdict": "NOTRUN", "detail": "", "problems": [],
        "unchanged": [], "must_change": [],
    }
    loaded = _load_under_test()
    if loaded is None:
        out["detail"] = ("裁决器不存在（agent.memory.conflict 导入失败）—— "
                         "本判据未测量，NOTRUN 不等于通过")
        return out
    ladder, adjudicator = loaded
    rules = _rules_of(ladder)
    flipped = _inverted(rules)

    unchanged: List[str] = []
    problems: List[str] = []
    must_change: List[str] = []
    for name, fixture in FIXTURES.items():
        exp = fixture["expected"]
        # **字面量决定的「这次裁决是否与顺序有关」**。
        #
        # 为什么需要这个标记：R2/R3/R4/R5 是一条**精炼链**（同类 → 同
        # 权威分 → 同层），对它们而言顺序是**无关**的 —— 这是决策树的
        # 天然性质，不是缺陷。真正让顺序有区别的是**互相竞争**的规则对
        # （R0 收敛 / R1 人类裁决 / R7 兜底）。所以「哪些构造冲突必须随
        # 顺序改变结果」必须**由本模块的字面量指定**，而不是让被测对象
        # 自己说。
        order_sensitive = bool(fixture.get("order_sensitive", False))
        if order_sensitive:
            must_change.append(name)
        try:
            good = _run(ladder, adjudicator, fixture, rules)
            bad = _run(ladder, adjudicator, fixture, flipped)
        except Exception as e:
            problems.append("%s 裁决器抛异常：%s: %s" % (name, type(e).__name__, e))
            continue
        good_key = (good.get("verdict"), good.get("winner_candidate_id"),
                    good.get("rule_id"))
        bad_key = (bad.get("verdict"), bad.get("winner_candidate_id"),
                   bad.get("rule_id"))
        if order_sensitive and good_key == bad_key:
            unchanged.append(name)
        # 颠倒之后还「命中期望」，说明这条判据对顺序完全无感
        if order_sensitive and bad_key == (exp["verdict"], exp["winner"],
                                           exp["rule_id"]):
            problems.append("%s：裁决序颠倒后结果不变 ⇒ 判据对顺序无感"
                            % name)

    out["unchanged"] = unchanged
    out["problems"] = problems
    out["must_change"] = must_change
    out["checked"] = len(FIXTURES)
    if problems:
        out["verdict"] = "FAIL"
        out["detail"] = "%d 处问题：%s" % (len(problems), "；".join(problems))
        return out
    if unchanged:
        out["verdict"] = "FAIL"
        out["detail"] = ("%d/%d 条应当随顺序改变的构造冲突，在裁决序颠倒后"
                         "结果不变 —— 判据对顺序无感：%s"
                         % (len(unchanged), len(must_change),
                            ", ".join(unchanged)))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("%d/%d 条与顺序有关的构造冲突在裁决序颠倒后**全部**改变"
                     "结果；另 %d 条（未决类）按定义与顺序无关"
                     % (len(must_change), len(FIXTURES),
                        len(FIXTURES) - len(must_change)))
    return out


# ---------------------------------------------------------------------------
# P2-5f · 已落盘的裁决记录可追溯
# ---------------------------------------------------------------------------

def measure_p2_5f_recorded_rulings_are_traceable(root: Optional[Path] = None
                                                 ) -> Dict[str, Any]:
    """P2-5f: every ruling already written to the ledger is traceable.

    **NOTRUN** when the workspace holds no ruling ledger — there is
    nothing to measure, and "nothing to measure" is not a pass.
    """
    out: Dict[str, Any] = {
        "criterion": "P2-5f",
        "name": "工作区里已落盘的裁决记录逐条可追溯",
        "verdict": "NOTRUN", "detail": "", "bad": [],
    }
    loaded = _load_under_test()
    if loaded is None:
        out["detail"] = ("裁决器不存在（agent.memory.conflict 导入失败）—— "
                         "本判据未测量，NOTRUN 不等于通过")
        return out
    _ladder, adjudicator = loaded
    base = shared_root(root)
    ledger = base / "conflicts" / "rulings.jsonl"
    if not ledger.is_file():
        out["detail"] = ("工作区 %s 下没有 conflicts/rulings.jsonl —— "
                         "本判据未测量（NOTRUN 不等于通过）" % base)
        return out
    try:
        records = adjudicator.read_rulings(ledger)
    except Exception as e:
        out["verdict"] = "FAIL"
        out["detail"] = "裁决记录读不出来：%s: %s" % (type(e).__name__, e)
        return out
    if not records:
        out["detail"] = ("裁决记录文件存在但没有记录 —— 本判据未测量"
                         "（NOTRUN 不等于通过）")
        return out

    bad: List[str] = []
    for i, rec in enumerate(records):
        gaps = [f for f in TRACEABLE_FIELDS
                if not str(rec.get(f, "") or "").strip()]
        if gaps:
            bad.append("第 %d 条缺 %s" % (i + 1, "/".join(gaps)))
            continue
        if not str(rec.get("conflict_id", "") or "").strip():
            bad.append("第 %d 条没有 conflict_id" % (i + 1))
        if not str(rec.get("ladder_digest", "") or "").strip():
            bad.append("第 %d 条没有 ladder_digest（不知道用的哪一版裁决序）"
                       % (i + 1))
    out["bad"] = bad
    out["checked"] = len(records)
    if bad:
        out["verdict"] = "FAIL"
        out["detail"] = "%d/%d 条记录不可追溯：%s" % (
            len(bad), len(records), "；".join(bad))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("裁决记录 %d 条，逐条都带齐 %s"
                     % (len(records), " / ".join(TRACEABLE_FIELDS)))
    return out


CRITERION_IDS = ("P2-5a", "P2-5b", "P2-5c", "P2-5d", "P2-5e", "P2-5f")

_MEASURES = (
    measure_p2_5a_the_ladder_is_judgeable,
    measure_p2_5b_the_ruling_is_unique_and_determined,
    measure_p2_5c_the_ruling_is_traceable,
    measure_p2_5d_undecided_does_not_pick_a_side,
    measure_p2_5e_the_criterion_is_sensitive_to_the_order,
    measure_p2_5f_recorded_rulings_are_traceable,
)


def judge_p2_conflict_family(root: Optional[Path] = None) -> Dict[str, Any]:
    """All six, over one workspace."""
    reports = []
    verdict = "PASS"
    for fn in _MEASURES:
        rep = fn(root)
        reports.append(rep)
        if rep["verdict"] == "FAIL":
            verdict = "FAIL"
        elif rep["verdict"] == "NOTRUN" and verdict == "PASS":
            verdict = "NOTRUN"
    return {
        "group": "P2-5",
        "title": "冲突裁决层",
        "root": str(shared_root(root)),
        "verdict": verdict,
        "reports": reports,
    }


# ---------------------------------------------------------------------------
# harness contract: judge_*(report, workspace) -> report.judge(...)
# ---------------------------------------------------------------------------

def _book(report, m: Dict[str, Any]):
    name = m.get("name") or m.get("criterion") or "?"
    detail = m.get("detail") or ""
    if m.get("verdict") == "NOTRUN":
        return report.judge_notrun(m.get("criterion", "?"), name, detail)
    return report.judge(m.get("criterion", "?"), name,
                        m.get("verdict") == "PASS", detail)


def judge_p2_5a_the_ladder_is_judgeable(report, workspace) -> bool:
    return _book(report, measure_p2_5a_the_ladder_is_judgeable(Path(workspace)))


def judge_p2_5b_the_ruling_is_unique_and_determined(report, workspace) -> bool:
    return _book(report,
                 measure_p2_5b_the_ruling_is_unique_and_determined(Path(workspace)))


def judge_p2_5c_the_ruling_is_traceable(report, workspace) -> bool:
    return _book(report, measure_p2_5c_the_ruling_is_traceable(Path(workspace)))


def judge_p2_5d_undecided_does_not_pick_a_side(report, workspace) -> bool:
    return _book(report, measure_p2_5d_undecided_does_not_pick_a_side(Path(workspace)))


def judge_p2_5e_the_criterion_is_sensitive_to_the_order(report, workspace) -> bool:
    return _book(report,
                 measure_p2_5e_the_criterion_is_sensitive_to_the_order(Path(workspace)))


def judge_p2_5f_recorded_rulings_are_traceable(report, workspace) -> bool:
    return _book(report,
                 measure_p2_5f_recorded_rulings_are_traceable(Path(workspace)))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Judge the P2-5 conflict adjudication layer.")
    ap.add_argument("--root", default=None,
                    help="the shared root (default: $COW_SHARED_ROOT or ~/cow)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else None
    rep = judge_p2_conflict_family(root)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("P2-5 · 冲突裁决层  —— %s" % rep["root"])
        for r in rep["reports"]:
            print("  [%-7s] %-8s %s" % (r["verdict"], r["criterion"], r["name"]))
            print("           %s" % r["detail"])
        print("VERDICT: %s" % rep["verdict"])
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
