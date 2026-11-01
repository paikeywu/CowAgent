"""R8 · the two criteria that were WIRED IN this round.

Two of them, and both of them answer questions that no existing
criterion could answer:

``M6f`` — the certification ledger has no ORPHAN entries.  Every other
ledger criterion JOINS the ledger to ``chunks``, so an entry that
names a chunk which is no longer in the index is INVISIBLE to all of
them.  That is the shape of a leak: the detectors that do not cover a
thing cannot report that thing, and "the fleet is green" stays green
while the ledger quietly fills up with witnesses to rows that stopped
existing.

``XF`` — the fleet's CROSS-END FINGERPRINT is uniform.  The seven
dimensions (column set / column types + constraints / trigger
definition text / guard tables / indexes / chunker version / value
domains) are what two machines must agree on before their libraries
can be called the same product.  The fingerprint is deliberately built
from **behaviour** (the trigger's normalised definition text), not
from a trigger's NAME: two machines can name their triggers
differently and still behave identically — and the earlier R7
measurement found exactly that (``8ea66d79`` vs ``da751942``: the same
data, different behaviour).

Both read SEMANTIC ANCHORS, not implementation details:

  * M6f calls the implementation's own ``judge_m6f_ledger_has_no_orphans``
    — the question ("does the ledger name a row that is gone?") is
    asked ONCE, in the implementation, and this criterion only reports
    the verdict per library.  If the question is later re-implemented
    elsewhere, the criterion follows the call instead of going stale.
  * XF calls ``fleet_check.judge_fleet_uniformity`` — the same
    discipline.

**The mutation case.**  Each of them has a REVERSE case: a copy with
an orphan ledger entry must go RED, and a copy whose trigger was
renamed (same behaviour, different name) must stay GREEN.  Those are
the cases that prove the criterion measures the thing it names.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, List

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro


#: The default ceiling on an ORPHAN's age (裁决 C3).
#: An orphan older than this is not "the mechanism produced one just
#: now" — it is "the sweeper has not kept up".  The two need different
#: repairs, so the criterion reports them apart.  Configurable through
#: ``COW_M6F_MAX_ORPHAN_AGE_SECONDS``.
M6F_DEFAULT_MAX_ORPHAN_AGE_SECONDS = 3600


def _m6f_max_orphan_age_seconds() -> int:
    """The orphan-age ceiling, in seconds (configurable)."""
    raw = os.environ.get("COW_M6F_MAX_ORPHAN_AGE_SECONDS")
    if raw is None or str(raw).strip() == "":
        return M6F_DEFAULT_MAX_ORPHAN_AGE_SECONDS
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return M6F_DEFAULT_MAX_ORPHAN_AGE_SECONDS


def _orphan_ages(conn: sqlite3.Connection, now: int) -> List[int]:
    """The ages of the orphan ledger entries, in seconds.

    The age is measured from the ledger row's OWN ``certified_at`` —
    that is when the certification was made, and the orphan is what
    remains of it.  Reading the ages here (read-only, on the same
    connection) does not re-implement the VERDICT: the verdict is the
    implementation's, and this only adds the age distribution the
    acceptance layer is required to report (裁决 C3).

    An entry with no ``certified_at`` cannot be aged; it is reported as
    the OLDEST possible age (it is unexplainable, so it is treated as
    stale rather than as fresh).
    """
    try:
        rows = conn.execute(
            "SELECT l.certified_at FROM cert_ledger l WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.id = l.id)").fetchall()
    except sqlite3.Error:
        return []
    out: List[int] = []
    for r in rows:
        created = r[0]
        if created is None:
            out.append(now)
            continue
        try:
            out.append(max(0, now - int(created)))
        except (TypeError, ValueError):
            out.append(now)
    return out


# ---------------------------------------------------------------------------
# M6f — the ledger has no orphan entries
# ---------------------------------------------------------------------------

def judge_m6f_ledger_has_no_orphans(report, workspace: Path):
    """M6f (REAL): every certification record names a chunk that exists.

    The question: does the ledger hold entries for rows that are no
    longer in the index?

    **NOTRUN when the ledger does not exist.**  A library that has
    never been certified has no ledger — and "no ledger" means
    "nothing was ever certified", which is the strongest possible form
    of the answer, not an inability to measure.  A PASS there would be
    reporting on a table that was never read.

    The implementation's own judge is the anchor; this criterion adds
    the fleet-level reading (every library, one verdict) and the
    per-library evidence chain.

    ====================================================================
    裁决 C3 (2026-11-01) · 账本孤儿 = 「撤销认证」，不是「丢数据」
    ====================================================================

    **旧语义.**  孤儿数必须为 0，否则 FAIL。  判据只报一个数字。

    **新语义.**  三件事一起报，缺一不可：

      * **孤儿数（存量）** —— 账本里指向已消失 chunk 的条目数；
      * **本轮增量（还在产生的）** —— 年龄在阈值内的孤儿数。  这
        是「机制还在产出孤儿」的信号；存量只是「还没清」；
      * **最老一条的年龄** —— 从 ``certified_at`` 起算。  刚产生的
        孤儿是**机制**的问题（重切时没剪），老孤儿是**存量清扫**的
        问题；两者的修法不同，判据必须把它们分开。

    **阈值口径.**  孤儿数 > 0 仍然 FAIL（裁决 C3 的立场是「撤销认证」
    不是「丢数据」，但撤销也要有人签字）。  阈值只管**年龄**：超过
    ``COW_M6F_MAX_ORPHAN_AGE_SECONDS``（默认 3600 秒）的孤儿单独报
    为「清扫器没跟上」。

    **关于「本轮增量」的口径说明（诚实版）.**  真正的「本轮 vs 上一轮」
    增量需要一份**持久化的上一次快照**，而系统里目前没有这个东西
    （这是 G12「终局清扫脚本」要补的）。  所以这里报的是**可从现有
    数据算出来的那个量**：年龄在阈值内的孤儿数 = 阈值之后新产生的
    孤儿数（一个孤儿要么在阈值内、要么在外面，所以这两个数相加就是
    总数）。  这是增量的**上界**，不是逐轮 diff —— 报告里写明了这一点。

    **为什么不是 NOTRUN**：这里明明测到了孤儿数，NOTRUN 的语义是
    「没法测量」。
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M6f(REAL)",
            "M6f 账本无孤儿：每条认证记录都指向一个还在索引里的块",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.ledger_orphans import (
            judge_m6f_ledger_has_no_orphans as _impl,
        )
    except Exception as e:
        return report.judge_notrun(
            "M6f(REAL)",
            "M6f 账本无孤儿：每条认证记录都指向一个还在索引里的块",
            "实现侧不可用（%s: %s）—— 本判据读语义锚点，不自己重写这个问题"
            % (type(e).__name__, e))

    ok = True
    red: List[str] = []
    notrun: List[str] = []
    grand_orphans = 0
    grand_ledger = 0
    grand_stale = 0
    grand_fresh = 0
    now = int(time.time())
    max_age = _m6f_max_orphan_age_seconds()

    detail.append("孤儿年龄阈值: %ds（COW_M6F_MAX_ORPHAN_AGE_SECONDS）"
                  % max_age)
    detail.append("判定口径: 孤儿数 > 0 ⇒ FAIL（撤销认证也要有人签字）；"
                  "年龄 > %ds 的孤儿单独报为『清扫器没跟上』；"
                  "年龄 ≤ %ds 的是阈值之后新产生的（机制还在产出）。"
                  % (max_age, max_age))
    detail.append("")

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            rep = _impl(conn, library=lib.name)
            # The ages of the orphans — the implementation's judge
            # reports the ids and the count; the age comes from the
            # ledger's own ``certified_at``.
            ages = _orphan_ages(conn, now)
        except Exception as e:
            detail.append("  %-22s ⇒ NOTRUN: 判据执行失败（%s: %s）"
                          % (lib.name, type(e).__name__, e))
            notrun.append(lib.name)
            continue
        finally:
            conn.close()

        verdict = rep.get("verdict")
        orphans = rep.get("orphans", 0)
        ledger_rows = rep.get("ledger_rows", 0)
        grand_orphans += orphans
        grand_ledger += ledger_rows
        stale = [a for a in ages if a > max_age]
        fresh = [a for a in ages if a <= max_age]
        grand_stale += len(stale)
        grand_fresh += len(fresh)
        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        detail.append("      %s 账本条目=%d 孤儿=%d" %
                      (verdict, ledger_rows, orphans))
        detail.append("      %s" % (rep.get("detail") or ""))
        if ages:
            detail.append("      孤儿年龄: 最老=%ds(%.1fh) 中位=%ds 最新=%ds(%.1fh)"
                          % (max(ages), max(ages) / 3600.0,
                             sorted(ages)[len(ages) // 2],
                             min(ages), min(ages) / 3600.0))
            detail.append("      新产生（≤ %ds）: %d   清扫器没跟上（> %ds）: %d"
                          % (max_age, len(fresh), max_age, len(stale)))
        if verdict == "FAIL":
            ok = False
            red.append(lib.name)
            for oid in rep.get("orphan_ids", [])[:10]:
                detail.append("        ! 孤儿条目: id=%s" % oid)
            detail.append("        归因: 账本行是『认证的证人』；它指向的 chunk "
                          "已经不在索引里了 ⇒ 这条认证被撤销（不是丢数据）")
        elif verdict == "NOTRUN":
            notrun.append(lib.name)

    detail.append("")
    detail.append("全活库合计: 账本条目=%d 孤儿=%d（必须为 0）"
                  % (grand_ledger, grand_orphans))
    detail.append("其中 新产生（≤ %ds）=%d   清扫器没跟上（> %ds）=%d"
                  % (max_age, grand_fresh, max_age, grand_stale))
    detail.append("（『本轮增量』的口径：系统里还没有持久化的上次快照，"
                  "所以这里报的是『阈值之后新产生的孤儿数』—— 增量的上界，"
                  "不是逐轮 diff。真正的逐轮 diff 需要 G12 的清扫脚本落一份快照。")
    detail.append("为什么它要自己一条: M6a 数『没有证人的行』、M6b 比『证人与行是否一致』、"
                  "M6c 数『没有证人的 mirror 行』—— 三条都 JOIN 账本与 chunks，"
                  "所以『账本指向一个已经不在的行』对它们全都不可见。"
                  "覆盖不到的东西就报不出来，全绿也就继续绿。")
    detail.append("（口径 C3：孤儿是『撤销认证』不是『丢数据』；"
                  "清扫必须『先备份账本表 → 出可读撤销清单 → 可回滚』。）")

    if notrun and not red:
        # Every library was NOTRUN (no ledger anywhere) — that is an
        # honest "cannot measure", and it is NOT a pass.
        return report.judge_notrun(
            "M6f(REAL)",
            "M6f 账本无孤儿：每条认证记录都指向一个还在索引里的块",
            "\n".join(detail))
    if red:
        return report.judge(
            "M6f(REAL)",
            "M6f 账本无孤儿：每条认证记录都指向一个还在索引里的块",
            False, "\n".join(detail))
    return report.judge(
        "M6f(REAL)",
        "M6f 账本无孤儿：每条认证记录都指向一个还在索引里的块",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# XF — the fleet's cross-end fingerprint is uniform
# ---------------------------------------------------------------------------

def judge_xf_cross_end_fingerprint_is_uniform(report, workspace: Path):
    """XF (REAL): every live library carries the same cross-end fingerprint.

    The seven dimensions and what each one is for:

      * ``columns``       — the column set of ``chunks`` and ``files``
      * ``constraints``   — the types, NOT NULL flags and defaults
      * ``triggers``      — the trigger definition text, NORMALISED
      * ``guard_tables``  — the guard's own tables and their columns
      * ``indexes``       — the index set
      * ``chunker``       — the chunker version the library was built with
      * ``domains``       — the legal values of the state columns

    **Why the trigger's TEXT and not its NAME.**  MEASURED (R7): 14
    libraries carried the trigger fingerprint ``8ea66d79`` and two
    carried ``da751942`` — the same data, but the two groups consulted
    DIFFERENT triggers (``body_keep`` vs ``cert_ledger``).  A name
    would have hidden that; the definition text shows it.

    **The verdict is fleet-level**: one library with a different shape
    makes the whole fleet non-uniform, because the fleet is the thing
    that must be uniform.  The report says WHICH dimension differs.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "XF(REAL)",
            "XF 跨端指纹：全活库的 7 维指纹一致（列/约束/触发器文本/守卫表/索引/chunker/取值域）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.fleet_check import judge_fleet_uniformity
    except Exception as e:
        return report.judge_notrun(
            "XF(REAL)",
            "XF 跨端指纹：全活库的 7 维指纹一致（列/约束/触发器文本/守卫表/索引/chunker/取值域）",
            "实现侧不可用（%s: %s）—— 本判据读语义锚点，不自己重写这个问题"
            % (type(e).__name__, e))

    fleet = judge_fleet_uniformity(workspace)

    detail.append("工作区: %s" % fleet.get("workspace"))
    detail.append("活库数: %s" % fleet.get("library_count"))
    detail.append("判定: %s" % fleet.get("verdict"))
    detail.append("")
    detail.append("每库指纹（7 维各维的实际值）:")
    for lib in fleet.get("per_library", fleet.get("libraries", [])):
        dims = lib.get("dimensions") or {}
        detail.append("  %-22s %s" % (lib.get("library"),
                                      lib.get("fingerprint")))
        for k in sorted(dims):
            detail.append("      %-14s %s" % (k, dims[k]))

    groups = fleet.get("groups") or fleet.get("tokens") or {}
    if groups:
        detail.append("")
        detail.append("分组:")
        if isinstance(groups, dict):
            for fp, names in groups.items():
                detail.append("  %s → %s" % (fp, ", ".join(sorted(names))))
        else:
            for g in groups:
                detail.append("  %s" % g)
    differing = fleet.get("differing_dimensions") or []
    if differing:
        detail.append("")
        detail.append("不同的维度: %s" % ", ".join(differing))
        detail.append("（这些维度的取值在库之间不一致 —— 这是可行动的诊断："
                      "差异不只是一个指纹不同，而是具体哪一面不同。）")

    ok = fleet.get("verdict") == "PASS"
    return report.judge(
        "XF(REAL)",
        "XF 跨端指纹：全活库的 7 维指纹一致（列/约束/触发器文本/守卫表/索引/chunker/取值域）",
        ok, "\n".join(detail))


#: The criteria this module contributes, in the order the runner uses.
CRITERIA = [
    ("M6f", "judge_m6f_ledger_has_no_orphans"),
    ("XF", "judge_xf_cross_end_fingerprint_is_uniform"),
]
