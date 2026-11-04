# -*- coding: utf-8 -*-
"""P2-5 · 冲突裁决的「裁决序」—— 载体权威表、层级表、规则阶梯。

这个模块是**被测对象**（裁决器）的**一半**；另一半在 ``adjudicator.py``。

为什么裁决序是「数据」而不是「散文」
------------------------------------
裁决序本身就是裁决结果的一部分 —— 两条规则谁先谁后，会改变**谁说了算**。
如果它写成一段散文，那么「把裁决序颠倒」就不是一次编辑，而是一个**没有
定义的愿望**（mutant 重放会无从下手，判据也就无从证明自己抓得住）。

所以它在这里是**一张有序的表**：每条规则有 ``id`` / ``tier`` / ``name`` /
``basis`` / ``decide()``。翻转这张表是一次可以逐字节还原的编辑。

阶梯的形状：**逐步收窄 + 唯一裁决**
----------------------------------
每条规则面对的是**当前还活着的候选集合**（``alive``），而不是全部候选：

* 能把集合收窄到**只剩一个** ⇒ 该规则**裁决**（RESOLVED），它就是
  「依据哪条规则」；
* 能收窄但**还剩多个** ⇒ 该规则**只收窄**，交给下一条（NARROWED）；
* 不适用 ⇒ 跳过（NARROWED 的特例：集合没变）。

**为什么必须这样**：如果每条规则都只看全量候选并直接选一个「最优」，
那么两个同类的真源候选会在第一步就「并列最优」，而选谁是任意的 ——
那不叫「唯一确定的裁决结果」。**收窄式阶梯保证了每一个 RESOLVED 都
指向唯一一个候选。**

规则的来源（取证时间 2026-11-05，读的是仓库内的权威设计）
-----------------------------------------------------------
* ``05-端一致性守门人-一致性与共享机制.md:109``
  —— 「冲突裁决时，**以真源为准，投影可丢弃**」⇒ **R2**
* ``06-创意大师-记忆书架与主体视图.md:699``
  —— 「以 ``markdown/`` 事件文件为准（真源），知识库条目是它的『结论 +
  证据指针』，每日记忆是它的『时间切片』」⇒ **R3**（载体权威）
* ``13-端一致性守门人-查询与知识层一致性.md:346``
  —— 「以时序裁决：取 ``released_at`` 较晚的那一端的状态为准；但若两端的
  ``released_at`` 相同而状态不同 ⇒ 数据损坏 ⇒ **报红并阻断写入**（不自动
  选边 —— 自动选边 = 越权，选择权在人类伙伴）」⇒ **R5 / R6**
* ``AGENT.md``「好一致性机制的判据 · 降级可见」⇒ **R7** 的降级动作
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 载体权威表：载体 -> (类别, 权威分, 所在层, 人话名)
# ---------------------------------------------------------------------------
#
# **类别**回答「它是哪一种载体」：真源 / 摘要层 / 投影。
# **权威分**回答「同一类别里谁更权威」—— 只在同类别内比较才有意义。
# **所在层**回答「它属于哪一层」：L2 人格公共层 / L1 共享知识层 / 视角私有层。
#
# 三张表**刻意不是同一件事**：类别、权威分、层可以互相不一致，这正是
# R2 / R3 / R4 三条规则各有存在理由的原因。若三者完全同序，R3 与 R4 就是
# 死代码，阶梯也会退化成一条规则。
CARRIER_TABLE: Dict[str, Dict[str, Any]] = {
    "persona_layer": {
        "class": "truth", "authority": 70, "layer": "L2_persona",
        "name": "shared_root/persona/ 人格公共层",
    },
    "markdown_event": {
        "class": "truth", "authority": 60, "layer": "view_private",
        "name": "markdown/ 事件文件",
    },
    "knowledge_page": {
        "class": "truth", "authority": 40, "layer": "L1_knowledge",
        "name": "knowledge/ 共享知识页",
    },
    "facts_jsonl": {
        # 与知识页**同级**（40）：事实层是「原话」，知识页是「结论」。二者
        # 冲突时由**层**裁决（共享的、经过去人格化与评审的结论 > 视角私有的
        # 原始流水）。注意这不等于「原话被折叠」—— 事实层仍然完整保留、
        # 可被引用为**证据**，裁决只决定系统**采纳哪个值**。
        "class": "truth", "authority": 40, "layer": "view_private",
        "name": "facts/ JSONL 事实层",
    },
    "daily_memory": {
        "class": "summary", "authority": 30, "layer": "view_private",
        "name": "memory/YYYY-MM-DD.md 每日记忆（时间切片）",
    },
    "persona_copy": {
        "class": "projection", "authority": 20, "layer": "view_private",
        "name": "角色侧抄的人格公共层副本",
    },
    "shelf_self": {
        "class": "projection", "authority": 10, "layer": "view_private",
        "name": "shelf/self.md 自我认知页",
    },
    "chunk_projection": {
        "class": "projection", "authority": 10, "layer": "view_private",
        "name": "chunks.text / FTS / embedding 投影",
    },
}

#: 类别优先：真源 > 摘要层 > 投影。**投影可丢弃**（05 号 :109）。
CLASS_PRECEDENCE: Dict[str, int] = {"truth": 2, "summary": 1, "projection": 0}

#: 层优先：L2 人格公共层 > L1 共享知识层 > 视角私有层。
LAYER_PRECEDENCE: Dict[str, int] = {
    "L2_persona": 30, "L1_knowledge": 20, "view_private": 10,
}


def carrier_info(carrier_id: str) -> Dict[str, Any]:
    """The table row for a carrier — or a loud failure if it is unknown."""
    try:
        return CARRIER_TABLE[carrier_id]
    except KeyError as e:
        raise KeyError(
            "未知载体 %r；已知载体：%s"
            % (carrier_id, ", ".join(sorted(CARRIER_TABLE)))
        ) from e


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One side of a conflict — a value somebody holds about one subject."""

    candidate_id: str
    value: Any
    carrier: str
    occurred_at: str = ""
    author_kind: str = "agent"       # "agent" | "human"
    ruling_by_human: bool = False    # 人类伙伴对这个候选做了**显式裁决**
    path: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "value": self.value,
            "carrier": self.carrier,
            "occurred_at": self.occurred_at,
            "author_kind": self.author_kind,
            "ruling_by_human": self.ruling_by_human,
            "path": self.path,
        }


@dataclass(frozen=True)
class Conflict:
    """A detected conflict: one subject, several incompatible values."""

    conflict_id: str
    subject: str
    candidates: Tuple[Candidate, ...] = ()
    kind: str = "value"              # "value" | "status" | "existence"
    detected_at: str = ""
    detected_by: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "subject": self.subject,
            "kind": self.kind,
            "detected_at": self.detected_at,
            "detected_by": self.detected_by,
            "candidates": [c.to_json() for c in self.candidates],
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Conflict":
        cands = tuple(
            Candidate(
                candidate_id=str(c.get("candidate_id", "")),
                value=c.get("value"),
                carrier=str(c.get("carrier", "")),
                occurred_at=str(c.get("occurred_at", "") or ""),
                author_kind=str(c.get("author_kind", "agent") or "agent"),
                ruling_by_human=bool(c.get("ruling_by_human", False)),
                path=str(c.get("path", "") or ""),
            )
            for c in (data.get("candidates") or [])
        )
        return cls(
            conflict_id=str(data.get("conflict_id", "")),
            subject=str(data.get("subject", "")),
            candidates=cands,
            kind=str(data.get("kind", "value") or "value"),
            detected_at=str(data.get("detected_at", "") or ""),
            detected_by=str(data.get("detected_by", "") or ""),
        )


# ---------------------------------------------------------------------------
# 规则阶梯
# ---------------------------------------------------------------------------

#: 一个裁决决定的四种状态。
VERDICT_RESOLVED = "RESOLVED"      # 裁出了唯一裁决方
VERDICT_CONVERGED = "CONVERGED"    # 候选值全同 —— 本来就没有冲突
VERDICT_NARROWED = "NARROWED"      # 收窄了候选集，但还没到唯一
VERDICT_ESCALATED = "ESCALATED"    # 判为损坏/多方裁决 —— 报红，不选边
VERDICT_UNDECIDED = "UNDECIDED"    # 无规则可裁 —— 未决，降级可见

VERDICTS = (VERDICT_RESOLVED, VERDICT_CONVERGED, VERDICT_NARROWED,
            VERDICT_ESCALATED, VERDICT_UNDECIDED)


@dataclass(frozen=True)
class RuleDecision:
    """What one rule concluded about the **alive** candidates."""

    verdict: str
    winner: Optional[str] = None                 # candidate id
    rationale: str = ""
    alive: Tuple[str, ...] = ()                  # 收窄后活着的 candidate ids

    @property
    def terminal(self) -> bool:
        """Does this decision END the walk?

        **UNDECIDED is terminal** — it is the ladder's **last** rung
        («没有任何一条规则能分出胜负»).  If it were not terminal, a
        ladder whose order was flipped would keep walking after the
        terminal rung and could still reach a verdict, which would make
        the order partly unobservable.  Terminal-ness is what makes
        «颠倒裁决序必然改变结果» a provable claim rather than a hope.
        """
        return self.verdict in (VERDICT_RESOLVED, VERDICT_CONVERGED,
                                VERDICT_ESCALATED, VERDICT_UNDECIDED)


@dataclass(frozen=True)
class Rule:
    """One rung of the ladder."""

    rule_id: str
    tier: int
    name: str
    basis: str                       # 这条规则出自哪份设计文档的哪一行
    decide_fn: Any = field(repr=False, default=None)

    def decide(self, conflict: Conflict,
               alive: Sequence[Candidate]) -> Optional[RuleDecision]:
        """Ask this rule about the alive candidates."""
        if self.decide_fn is None:
            return None
        return self.decide_fn(conflict, alive)


def _class_of(c: Candidate) -> str:
    return str(carrier_info(c.carrier).get("class", "projection"))


def _authority_of(c: Candidate) -> int:
    return int(carrier_info(c.carrier).get("authority", 0))


def _layer_of(c: Candidate) -> str:
    return str(carrier_info(c.carrier).get("layer", "view_private"))


def _canon_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          default=str)
    except (TypeError, ValueError):
        return repr(value)


def _parse_ts(ts: str) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 timestamp; ``None`` when it is absent/unparseable."""
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _comparable_ts(cands: Sequence[Candidate]) -> Optional[List[_dt.datetime]]:
    """The timestamps, if — and only if — they can be COMPARED.

    Two ways they cannot be compared:

    * one of them is missing or unparseable  ⇒ we do not know;
    * one is timezone-aware and another is naive ⇒ comparing them would
      raise, and guessing an offset would be inventing a fact.

    Both cases mean "cannot decide by time", not "the times are equal".
    """
    parsed: List[_dt.datetime] = []
    for c in cands:
        dt = _parse_ts(c.occurred_at)
        if dt is None:
            return None
        parsed.append(dt)
    if not parsed:
        return None
    aware = [d.tzinfo is not None for d in parsed]
    if any(aware) and not all(aware):
        return None
    return parsed


def _ids(cands: Sequence[Candidate]) -> Tuple[str, ...]:
    return tuple(c.candidate_id for c in cands)


def _narrow(conflict: Conflict, alive: Sequence[Candidate],
            keep: Sequence[Candidate], why: str) -> RuleDecision:
    """Build a NARROWED decision that keeps ``keep``."""
    return RuleDecision(verdict=VERDICT_NARROWED, rationale=why,
                        alive=_ids(keep))


# --- R0 · 收敛预检 -------------------------------------------------------

def _decide_convergence(conflict: Conflict,
                         alive: Sequence[Candidate]) -> Optional[RuleDecision]:
    """All candidates hold the SAME value ⇒ nothing to adjudicate.

    This is a **pre-check**, not a priority rule: a conflict detector that
    over-reports must not produce a "ruling" about a non-conflict.
    """
    if len(alive) < 2:
        return None
    values = [_canon_value(c.value) for c in alive]
    if len(set(values)) == 1:
        return RuleDecision(
            verdict=VERDICT_CONVERGED,
            rationale="全部 %d 个候选的值相同 —— 没有冲突要裁" % len(values),
        )
    return None


# --- R1 · 人类伙伴显式裁决 ------------------------------------------------

def _decide_human(conflict: Conflict,
                  alive: Sequence[Candidate]) -> Optional[RuleDecision]:
    """The human partner's explicit ruling outranks every mechanical rule.

    **为什么它在最上面**：下面每一条都只是**默认**；人类伙伴随时可以推翻
    默认。13 号文档 :348 写得很清楚 —— 「自动选边 = 越权，选择权在人类
    伙伴」。若机械规则排在人类之上，那条规则就变成「不可推翻的默认」，
    与该原则直接冲突。
    """
    ruled = [c for c in alive if c.ruling_by_human]
    if not ruled:
        return None
    if len(ruled) == 1:
        return RuleDecision(
            verdict=VERDICT_RESOLVED,
            winner=ruled[0].candidate_id,
            rationale="人类伙伴对该冲突做了显式裁决（候选 %s）"
                      % ruled[0].candidate_id,
        )
    # 两条以上的人类裁决：能按时序分出胜负就分，分不出 ⇒ 不自动选边。
    ts = _comparable_ts(ruled)
    if ts:
        latest = max(ts)
        winners = [c for c, t in zip(ruled, ts) if t == latest]
        if len(winners) == 1:
            return RuleDecision(
                verdict=VERDICT_RESOLVED,
                winner=winners[0].candidate_id,
                rationale="多条人类裁决中时序最晚的一条（%s）"
                          % winners[0].candidate_id,
            )
    return RuleDecision(
        verdict=VERDICT_ESCALATED,
        rationale="同一冲突上有多条人类裁决且无法分出先后 ⇒ 不自动选边，"
                  "升级人类伙伴",
    )


# --- R2 · 真源优先于投影 --------------------------------------------------

def _decide_truth_over_projection(conflict: Conflict,
                                   alive: Sequence[Candidate]
                                   ) -> Optional[RuleDecision]:
    """The truth source wins; the projection may be discarded.

    05 号 :109 —— 「冲突裁决时，以真源为准，投影可丢弃」。
    """
    if len(alive) < 2:
        return None
    classes = [_class_of(c) for c in alive]
    if len(set(classes)) == 1:
        return None
    best = max(CLASS_PRECEDENCE.get(k, 0) for k in classes)
    keep = [c for c, k in zip(alive, classes)
            if CLASS_PRECEDENCE.get(k, 0) == best]
    return _narrow(conflict, alive, keep,
                   "载体类别分出高低（%s 的类别 %s 最高）⇒ 投影可丢弃"
                   % (keep[0].carrier, _class_of(keep[0])))


# --- R3 · 载体权威（同一类别内） -------------------------------------------

def _decide_carrier_authority(conflict: Conflict,
                               alive: Sequence[Candidate]
                               ) -> Optional[RuleDecision]:
    """Within one class, the more authoritative carrier wins.

    06 号 :699 —— 「以 ``markdown/`` 事件文件为准（真源），知识库条目是
    它的『结论 + 证据指针』，每日记忆是它的『时间切片』」。
    """
    if len(alive) < 2:
        return None
    classes = {_class_of(c) for c in alive}
    if len(classes) != 1:
        return None
    auths = [_authority_of(c) for c in alive]
    if len(set(auths)) == 1:
        return None
    best = max(auths)
    keep = [c for c, a in zip(alive, auths) if a == best]
    return _narrow(conflict, alive, keep,
                   "同类别（%s）内权威分 %d 最高 ⇒ 取权威载体 %s"
                   % (classes.pop(), best, keep[0].carrier))


# --- R4 · 层优先 ----------------------------------------------------------

def _decide_layer_priority(conflict: Conflict,
                            alive: Sequence[Candidate]
                            ) -> Optional[RuleDecision]:
    """Same class, same authority — the higher layer decides.

    L2 人格公共层 > L1 共享知识层 > 视角私有层。
    """
    if len(alive) < 2:
        return None
    classes = {_class_of(c) for c in alive}
    if len(classes) != 1:
        return None
    auths = [_authority_of(c) for c in alive]
    if len(set(auths)) != 1:
        return None
    layers = [_layer_of(c) for c in alive]
    if len(set(layers)) == 1:
        return None
    best = max(LAYER_PRECEDENCE.get(k, 0) for k in layers)
    keep = [c for c, k in zip(alive, layers)
            if LAYER_PRECEDENCE.get(k, 0) == best]
    return _narrow(conflict, alive, keep,
                   "同类别同权威分 ⇒ 层优先（%s 在 %s 层）⇒ 取高层候选"
                   % (keep[0].carrier, _layer_of(keep[0])))


# --- R5 · 时序裁决 --------------------------------------------------------

def _decide_recency(conflict: Conflict,
                    alive: Sequence[Candidate]) -> Optional[RuleDecision]:
    """Same class, same authority, same layer ⇒ the later one wins.

    13 号 :346 —— 「以时序裁决：取 ``released_at`` 较晚的那一端的状态
    为准」。
    """
    if len(alive) < 2:
        return None
    classes = {_class_of(c) for c in alive}
    if len(classes) != 1:
        return None
    auths = [_authority_of(c) for c in alive]
    if len(set(auths)) != 1:
        return None
    layers = {_layer_of(c) for c in alive}
    if len(layers) != 1:
        return None
    ts = _comparable_ts(alive)
    if ts is None:
        return None                      # 不能比较 ⇒ 交给 R7（未决）
    latest = max(ts)
    keep = [c for c, t in zip(alive, ts) if t == latest]
    if len(keep) == 1:
        return RuleDecision(
            verdict=VERDICT_RESOLVED,
            winner=keep[0].candidate_id,
            rationale="同类别同权威同层 ⇒ 时序裁决，较晚者胜（%s，%s）"
                      % (keep[0].candidate_id, keep[0].occurred_at),
        )
    return _narrow(conflict, alive, keep,
                   "同类别同权威同层 ⇒ 时序裁决，%d 个候选时间戳并列最晚"
                   % len(keep))


# --- R6 · 平局 ⇒ 数据损坏 ⇒ 报红并阻断 ------------------------------------

def _decide_corrupt_tie(conflict: Conflict,
                         alive: Sequence[Candidate]) -> Optional[RuleDecision]:
    """Equal timestamps, different values ⇒ corruption ⇒ block, do not pick.

    13 号 :346 —— 「若两端的 ``released_at`` 相同而状态不同 ⇒ 数据损坏 ⇒
    报红并阻断写入（不自动选边）」。

    **前置条件与 R5 相同**（同类别、同权威分、同层）：R6 是「同一桶里的
    并列」，不是「全世界的并列」。若不加上这个前置，两个**不同类别**的
    候选只要时间戳相同就会被误判成「数据损坏」—— 而那其实是类别不同，
    R2 会处理它。
    """
    if len(alive) < 2:
        return None
    classes = {_class_of(c) for c in alive}
    if len(classes) != 1:
        return None
    auths = [_authority_of(c) for c in alive]
    if len(set(auths)) != 1:
        return None
    layers = {_layer_of(c) for c in alive}
    if len(layers) != 1:
        return None
    ts = _comparable_ts(alive)
    if ts is None:
        return None                      # 时间不可比 ⇒ 不是「相同」，是「不知」
    if len(set(ts)) != 1:
        return None                      # 时序能分出高低 ⇒ R5 早该裁掉了
    return RuleDecision(
        verdict=VERDICT_ESCALATED,
        rationale="同类别同权威同层，时间戳相同（%s）而取值不同 ⇒ 判为数据"
                  "损坏：报红并阻断写入，不自动选边" % alive[0].occurred_at,
    )


# --- R7 · 未决 ⇒ 降级可见 --------------------------------------------------

def _decide_undecided(conflict: Conflict,
                      alive: Sequence[Candidate]) -> Optional[RuleDecision]:
    """The terminal rung: nothing could decide it ⇒ 未决 ⇒ 降级.

    **它是兜底，所以必须待在最后一级 —— 而「最后一级」是由裁决序的
    位置决定的，不是在函数里写死的。** 这条区分很重要：

    * 若把「是不是最后一级」写进函数（``is_last`` 参数），那么**颠倒
      裁决序不会改变任何结果** —— 未决规则会跟着位置跑，整个裁决序
      就成了一张**顺序无关**的表，mutant 重放也就无从谈起；
    * 若把「最后一级」交给**位置**（本实现），那么**把 R7 提到最前面，
      每一条冲突都会变成未决** —— 结果变了，而且变得很显眼。

    ⇒ 「裁决序是结果的一部分」这句话，在这里是**可证的**，不是修辞。

    ⚠️ **代价（诚实地写出来）**：R2/R3/R4/R5 这四条规则的**前置条件是
    层层嵌套**的（同类 → 同权威分 → 同层），所以它们构成一条**精炼链**，
    对这四条而言顺序是**无关**的。这不是缺陷，是决策树天然的性质。
    真正让顺序有区别的，是**互相竞争**的规则对：R0（收敛）vs R1（人类
    裁决）vs R7（兜底）。P2-5e 的构造冲突正是针对这些竞争规则对设计的。

    **不是 ESCALATED**：ESCALATED 说的是「我们知道这是损坏」；未决说的是
    「我们不知道」。两者的降级动作相同（阻断写入 + 读侧标记 + 升级人类），
    但归因不同 —— 混为一谈会让台账把「信息缺失」记成「数据损坏」。
    """
    if not alive:
        return RuleDecision(
            verdict=VERDICT_UNDECIDED,
            rationale="冲突没有候选 ⇒ 无可裁决对象",
        )
    return RuleDecision(
        verdict=VERDICT_UNDECIDED,
        rationale="没有任何一条裁决序规则能分出胜负（时间戳缺失或不可比）"
                  "⇒ 未决：阻断写入 + 读侧标记『当前可能不是最新』，"
                  "升级人类伙伴",
    )


#: **裁决序**。顺序即裁决结果 —— 这张表被翻转，裁决结果就会变。
#:
#: ``tier`` 从 0 开始连续编号，且与表中的位置一一对应；
#: ``phase2_p2_conflict`` 的判据 P2-5a 会检查这一点（阶梯必须可判定）。
RULES: Tuple[Rule, ...] = (
    Rule("R0-CONVERGENCE", 0, "收敛预检：候选值全同则无冲突可裁",
         "本层自定（冲突检测器可能过报）", _decide_convergence),
    Rule("R1-HUMAN-RULING", 1, "人类伙伴显式裁决优先",
         "13 号 :348「自动选边 = 越权，选择权在人类伙伴」", _decide_human),
    Rule("R2-TRUTH-OVER-PROJECTION", 2, "真源优先于投影，投影可丢弃",
         "05 号 :109「以真源为准，投影可丢弃」", _decide_truth_over_projection),
    Rule("R3-CARRIER-AUTHORITY", 3, "同类别内按载体权威分裁决",
         "06 号 :699「以 markdown/ 事件文件为准」", _decide_carrier_authority),
    Rule("R4-LAYER-PRIORITY", 4, "同类别同权威分时按层裁决（L2>L1>私有）",
         "P2 多视角共享：共享层高于视角私有层", _decide_layer_priority),
    Rule("R5-RECENCY", 5, "同类别同权威同层时按时序裁决（较晚者胜）",
         "13 号 :346「以时序裁决：取 released_at 较晚的那一端」",
         _decide_recency),
    Rule("R6-TIE-ESCALATE", 6, "时间戳相同而取值不同 ⇒ 数据损坏 ⇒ 报红阻断",
         "13 号 :346「released_at 相同而状态不同 ⇒ 数据损坏 ⇒ 报红并阻断写入」",
         _decide_corrupt_tie),
    Rule("R7-UNDECIDED", 7, "无可适用规则 ⇒ 未决 ⇒ 降级可见并升级人类",
         "AGENT.md「降级可见」+ 13 号 :348「不自动选边」",
         _decide_undecided),
)

RULE_IDS: Tuple[str, ...] = tuple(r.rule_id for r in RULES)
