# -*- coding: utf-8 -*-
"""P2-5 · 冲突裁决层。

这个包是 P2「多视角共享」的**裁决层**：当两个视角对同一件事持有不同的
值时，谁说了算、依据哪条规则、裁决记录长什么样。

模块
----
``ladder``       裁决序（载体权威表 + 层级表 + 规则阶梯）
``adjudicator``  执行裁决序，产出可追溯的裁决记录
``cli``          命令行入口（``python -m agent.memory.conflict``）

**边界**：这里**不做**跨角色借阅台（``peer_read``），**不做**知识页三段
拆分（``MVP-003``）。本包只回答一个问题：**冲突了，谁说了算。**
"""

from __future__ import annotations

from .adjudicator import (
    Ruling,
    adjudicate,
    adjudicate_json,
    ladder_digest,
    load_conflicts,
    record_ruling,
    rulings_path,
)
from .ladder import (
    CARRIER_TABLE,
    CLASS_PRECEDENCE,
    LAYER_PRECEDENCE,
    RULES,
    RULE_IDS,
    VERDICTS,
    Candidate,
    Conflict,
    Rule,
    RuleDecision,
    carrier_info,
)

__all__ = [
    "Ruling",
    "adjudicate",
    "adjudicate_json",
    "ladder_digest",
    "load_conflicts",
    "record_ruling",
    "rulings_path",
    "CARRIER_TABLE",
    "CLASS_PRECEDENCE",
    "LAYER_PRECEDENCE",
    "RULES",
    "RULE_IDS",
    "VERDICTS",
    "Candidate",
    "Conflict",
    "Rule",
    "RuleDecision",
    "carrier_info",
]
