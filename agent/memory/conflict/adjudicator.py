# -*- coding: utf-8 -*-
"""P2-5 · 冲突裁决器 —— 执行裁决序，产出**可追溯**的裁决记录。

被测对象（object under test）
----------------------------
* ``agent/memory/conflict/ladder.py`` —— 裁决序（数据）
* ``agent/memory/conflict/adjudicator.py`` —— 本文件，执行裁决序 + 写记录

两者合起来是「裁决器」。判据在
``agent/memory/acceptance/phase2_p2_conflict.py``，它**不调用本模块的
任何函数来算期望值**（那是同义反复 —— 判据若调用被测对象的裁决函数，
它测的就是「被测对象和自己一致」）。

裁决记录（ruling record）的四个必备字段
----------------------------------------
| 字段 | 名字 | 回答哪个问题 |
|:--|:--|:--|
| 裁决方 | ``ruled_by`` | 谁裁的 |
| 时间 | ``ruled_at`` | 什么时候裁的 |
| 被裁决对象 | ``target`` | 针对哪条冲突 / 哪个候选 |
| 依据 | ``rule_id`` / ``rule_basis`` | 依据哪条规则（出处 + 人话） |

另外三个字段是为了让记录**能被机器读**、能被两次运行比较：
``conflict_id``（针对哪条冲突）、``ladder_digest``（用的哪一版裁决序）、
``verdict``（裁成了什么状态）。

**为什么记录要落盘**：13 号 :346 的「报红并阻断写入」要求裁决有据可查；
而一份只存在于内存里的记录，进程一退就没了 —— 那不叫可追溯。所以
``record_ruling`` 会把记录追加到 ``<shared_root>/conflicts/rulings.jsonl``，
**只追加、不改写**（原始事实不折叠）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .ladder import (
    Conflict,
    Rule,
    RuleDecision,
    RULES,
    VERDICT_CONVERGED,
    VERDICT_ESCALATED,
    VERDICT_NARROWED,
    VERDICT_RESOLVED,
    VERDICT_UNDECIDED,
)

__all__ = [
    "Ruling",
    "adjudicate",
    "adjudicate_json",
    "record_ruling",
    "rulings_path",
    "ladder_digest",
    "load_conflicts",
    "read_rulings",
]


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat()


def ladder_digest(rules: Sequence[Rule] = RULES) -> str:
    """The digest of the ladder **as it is right now**.

    A ruling that does not say which ladder produced it cannot be compared
    with another ruling.  The digest covers the ORDER and the RULE IDS —
    which are exactly the two things the mutant replay moves.
    """
    h = hashlib.sha256()
    for r in rules:
        h.update(r.rule_id.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(r.tier).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def rulings_path(shared_root: Optional[Path] = None) -> Path:
    """Where the ruling records live.

    ``COW_SHARED_ROOT`` wins; otherwise the module's own ``~/cow``
    neighbourhood.  **No machine path is hard-coded** — a criterion that
    hard-codes one is permanently red everywhere else.
    """
    if shared_root is not None:
        root = Path(shared_root)
    else:
        env = os.environ.get("COW_SHARED_ROOT")
        if env:
            root = Path(env)
        else:
            root = Path.home() / "cow"
            here = Path(__file__).resolve()
            for parent in here.parents:
                if parent.name == "cow":
                    root = parent
                    break
    return root / "conflicts" / "rulings.jsonl"


class Ruling(dict):
    """The ruling record.  A dict so it serialises without a shim."""

    @property
    def verdict(self) -> str:
        return str(self.get("verdict", ""))

    @property
    def ruled_by(self) -> str:
        return str(self.get("ruled_by", ""))

    @property
    def rule_id(self) -> str:
        return str(self.get("rule_id", ""))

    def trace_line(self) -> str:
        """One line a human can read: who / when / which rule / which conflict."""
        return ("冲突 %s ｜ 裁决方 %s ｜ %s ｜ 依据 %s（%s）｜ 被裁决对象 %s ｜ 结果 %s"
                % (self.get("conflict_id", "-"),
                   self.ruled_by,
                   self.get("ruled_at", "-"),
                   self.rule_id,
                   self.get("rule_basis", "-"),
                   self.get("target", "-"),
                   self.verdict))


def _candidate_payload(conflict: Conflict) -> Dict[str, Any]:
    return {c.candidate_id: c.to_json() for c in conflict.candidates}


def adjudicate(conflict: Conflict,
               rules: Sequence[Rule] = RULES) -> Ruling:
    """Walk the ladder and produce ONE ruling.

    **确定性**：同一份输入 + 同一版裁决序 ⇒ 同一个结果。这是 A2 的要求。
    裁决序里**没有**依赖时钟、依赖字典序、依赖文件系统的分支；唯一的
    时间来源是候选自带的 ``occurred_at``。``ruled_at`` 是**记录时间**，
    不是裁决依据 —— 依据永远是 ``rule_id``。

    **收窄语义**：每条规则面对的是**当前还活着的候选集合**；能把集合
    收窄到只剩一个的规则才是「裁决依据」。
    """
    alive: List[Any] = list(conflict.candidates)
    trace: List[Dict[str, Any]] = []
    decision: Optional[RuleDecision] = None
    applied_rule: Optional[Rule] = None

    for rule in rules:
        out = rule.decide(conflict, alive)
        if out is None:
            trace.append({"rule_id": rule.rule_id, "tier": rule.tier,
                          "applied": False,
                          "alive_after": [c.candidate_id for c in alive],
                          "note": "不适用"})
            continue
        if out.verdict == VERDICT_NARROWED and out.alive:
            by_id = {c.candidate_id: c for c in alive}
            alive = [by_id[cid] for cid in out.alive if cid in by_id]
            # **收窄到只剩一个 ⇒ 这条规则就是裁决依据。**
            #
            # 这一步是「唯一确定」的保证：阶梯里的每条规则只负责**排除**，
            # 而「谁说了算」是**排除到只剩一个**这个事实。所以
            # ``rule_id`` 指向的是**做出最后排除的那条规则**，而不是
            # 「第一条提到它的规则」。
            if len(alive) == 1:
                out = RuleDecision(
                    verdict=VERDICT_RESOLVED,
                    winner=alive[0].candidate_id,
                    rationale=out.rationale + " ⇒ 候选只剩一个，裁决成立",
                    alive=tuple(c.candidate_id for c in alive),
                )
        trace.append({"rule_id": rule.rule_id, "tier": rule.tier,
                      "applied": True, "verdict": out.verdict,
                      "winner": out.winner,
                      "alive_after": [c.candidate_id for c in alive],
                      "note": out.rationale})
        decision = out
        applied_rule = rule
        if out.terminal:
            break

    if decision is None:
        decision = RuleDecision(verdict=VERDICT_UNDECIDED,
                                rationale="裁决序为空")
        applied_rule = None

    winners = [c for c in conflict.candidates
               if c.candidate_id == decision.winner]
    winner = winners[0] if winners else None

    return Ruling({
        "conflict_id": conflict.conflict_id,
        "subject": conflict.subject,
        "verdict": decision.verdict,
        # --- A1 · 裁决方 ---
        "ruled_by": _ruled_by(conflict, decision, winner),
        # --- A1 · 时间 ---
        "ruled_at": _now_iso(),
        "detected_at": conflict.detected_at,
        # --- A1 · 被裁决对象 ---
        "target": _target_of(conflict, winner),
        # --- A1 · 依据 ---
        "rule_id": applied_rule.rule_id if applied_rule else "",
        "rule_basis": applied_rule.basis if applied_rule else "",
        "rule_name": applied_rule.name if applied_rule else "",
        "rule_tier": applied_rule.tier if applied_rule else -1,
        "rationale": decision.rationale,
        # --- machine-readable extras ---
        "winner_candidate_id": decision.winner or "",
        "candidates": _candidate_payload(conflict),
        "alive_at_end": [c.candidate_id for c in alive],
        "trace": trace,
        "ladder_digest": ladder_digest(rules),
        "ladder": [r.rule_id for r in rules],
    })


def _ruled_by(conflict: Conflict, decision: RuleDecision,
              winner: Optional[Any]) -> str:
    """**谁说了算** —— 裁决方标识。

    * 人类伙伴显式裁决 ⇒ 裁决方 = 人类伙伴（沛琦）；
    * 机械规则裁出 ⇒ 裁决方 = 该规则的**角色**，不是某个 agent
      （规则是团队级的，写死一个 agent id 会让它随人员变动而失真）；
    * 未决 / 损坏 ⇒ 裁决方 = 「升级人类伙伴」，且**明确写出没有裁决方**
      —— 这一条不许被写成任何 agent，因为「自动选边」正是被禁止的。
    """
    if decision.verdict == VERDICT_CONVERGED:
        return "无（无冲突）"
    if decision.verdict == VERDICT_UNDECIDED:
        return "未决 —— 升级人类伙伴（沛琦）"
    if decision.verdict == VERDICT_ESCALATED:
        if winner is not None and winner.ruling_by_human:
            return "人类伙伴（沛琦）"
        return "升级人类伙伴（沛琦）—— 未自动选边"
    if winner is not None and winner.ruling_by_human:
        return "人类伙伴（沛琦）"
    return "守门人裁决器（consistency-guardian）· 规则化裁决"


def _target_of(conflict: Conflict, winner: Optional[Any]) -> str:
    """**被裁决对象** —— 读出来要能回答「针对哪条冲突 / 哪个候选」。"""
    base = "冲突 %s（subject=%s，候选 %d 个）" % (
        conflict.conflict_id, conflict.subject, len(conflict.candidates))
    if winner is not None:
        return "%s ⇒ 采纳候选 %s（载体 %s，值 %s）" % (
            base, winner.candidate_id, winner.carrier,
            json.dumps(winner.value, ensure_ascii=False, default=str))
    return "%s ⇒ 未选定任何候选" % base


def adjudicate_json(payload: Dict[str, Any],
                    rules: Sequence[Rule] = RULES) -> Dict[str, Any]:
    """Adjudicate a conflict given as a plain dict (file / CLI input)."""
    return dict(adjudicate(Conflict.from_json(payload), rules))


def record_ruling(ruling: Ruling, path: Optional[Path] = None) -> Path:
    """Append the ruling to the ruling ledger.  **Append only.**

    The ledger is ``<shared_root>/conflicts/rulings.jsonl`` — one JSON
    object per line.  Append-only because the whole system's rule is
    「原始事实不折叠」: a ruling that was overwritten is a ruling that
    cannot be audited.
    """
    target = Path(path) if path else rulings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(ruling), ensure_ascii=False) + "\n")
    return target


def read_rulings(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the ruling ledger back (append-only file, one JSON per line)."""
    target = Path(path) if path else rulings_path()
    out: List[Dict[str, Any]] = []
    if not target.is_file():
        return out
    with open(target, "r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                out.append(json.loads(s))
            except ValueError:
                continue
    return out


def load_conflicts(path: Path) -> List[Conflict]:
    """Read conflicts from a JSONL file (one conflict per line)."""
    out: List[Conflict] = []
    if not Path(path).is_file():
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(Conflict.from_json(json.loads(s)))
    return out
