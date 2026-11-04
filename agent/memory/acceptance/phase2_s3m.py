"""P1 阶段三 N · the messages-domain criteria (MSG-INV-1..7) as the
acceptance layer sees them.

**What this module is.**  The施工图 §1 阶段 5 工序 ① asks for exactly
this file: ``agent/memory/acceptance/phase2_s3m.py``, "仿 phase2_s3r.py
的 judge_* 形态".  It is the WIRING of the messages-domain criteria into
the gate — and the gate is the only place where a verdict is said out
loud.

**Why the verdict is spoken HERE and not in ``msg_inv.py``.**  施工图
§3.1 draws the boundary:

    「实现侧只提供『原语 + 抛错语义 + 自报计数』，验收侧独占
      GREEN / RED / NOTRUN 的判定权。」

``msg_inv.py`` computes the verdicts (it must — the question has one
answer wherever it is asked), but the criterion that says *"this fleet is
green"* is an ACCEPTANCE-side asset.  That is why this module exists as
a separate file rather than a flag in ``phase2.py``: the ownership of
the verdict is a design property, not an implementation detail.

**The discipline this module follows, and why each part is there.**

  * **M-1 — the report is read, not recomputed.**  The criterion calls
    ``msg_inv.run_msg_invariants`` (the ONE implementation) and REPORTS
    its verdicts.  It never re-implements the question: a second
    spelling of "is the chain self-consistent" is exactly how the
    J-3×M6e pair ended up disagreeing 17/17.  Reading the real output
    also means the criterion measures what the PATROL measures — and a
    criterion that reports "green" while the patrol reports "notrun" is
    a divergence nobody would see.

  * **M-1 also checks that nothing is MISSING.**  "7 条各有 verdict,
    不许缺项" is 施工图 阶段 5 中间态判据 ①.  A report that carries six
    of the seven and says green has not measured the seventh — so the
    criterion compares the SET of names, not the count.

  * **M-2 — an un-externalised library is NOTRUN, never GREEN.**  This
    is 施工图 §3.3 判据自身的判据 A/B, and it is the criterion that
    proves M-1 is not a hollow green.  The precondition is decided by
    the criterion library itself (``msg_inv._precondition``), so the
    criterion and the measurement cannot drift apart.

  * **M-3 — the wiring is real, and there are two proofs.**  A static
    proof (the patrol's source calls ``run_msg_invariants``) and a
    behavioural proof (the patrol's report carries a ``msg_inv`` group
    with the seven verdicts).  The static proof alone would pass on a
    call that is never reached; the behavioural proof alone would pass on
    a group assembled by hand.  Both are needed.

  * **M-4 — INV-5 is no longer deferred.**  施工图 W-3: ``INV-5`` must
    leave ``text_inv.DEFERRED_INVS`` once the messages domain landed,
    and the deferred set must be exactly ``{"INV-4","INV-7","INV-8"}``.
    The check is on the REPORT's deferred set, not on the source
    dictionary, so a criterion that keeps a stale copy of the set is
    caught.

  * **M-5 — the status page self-reports.**  施工图 W-4 / 契约 v2 G-08 /
    N-8: ``get_status`` must expose ``msg_chain_anchor_scope`` and
    ``cold_read_latency_ms``.  The second one has a trap in it:
    "不适用" must be ``None``, NOT ``0.0`` — a zero latency is a claim
    ("the read was instant"), and ``None`` is a different claim ("there
    was no cold read to measure").  The criterion accepts ``None`` and a
    float, and REJECTS ``0.0`` on a library that has nothing to read.

  * **M-6 — the ledger's orphans are pruned at the WRITE boundary, and
    the detector stays awake.**  任务二 put ``_prune_ledger_entries`` in
    ``storage.py``: a re-cut that removes chunk ids removes their ledger
    entries in the SAME transaction.  That is the fix for M6f crying
    wolf on a normal state.  But a fix at the write boundary must not
    make the DETECTOR blind: a foreign writer's raw ``DELETE`` does not
    go through the write path, and the orphan it leaves must still be
    reported.  So M-6 has two halves: the prune works, and the detector
    still catches what the prune cannot see.

**The three states.**  ``PASS`` / ``FAIL`` / ``NOTRUN``, and NOTRUN is
never green — see the harness's own docstring.  Every criterion here
names the precondition that did not hold when it reports NOTRUN.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro

#: The seven messages-domain criteria.  The SET is the contract: 判据阶段 5
#: 中间态判据 ① says "7 条各有 verdict（不许缺项）", so the names are
#: compared as a set — a report carrying six of them and calling itself
#: green has not measured the seventh.
MSG_INV_NAMES: Tuple[str, ...] = (
    "MSG-INV-1", "MSG-INV-2", "MSG-INV-3", "MSG-INV-4",
    "MSG-INV-5", "MSG-INV-6", "MSG-INV-7",
)

#: The deferred set 施工图 W-3 demands: INV-5 has landed, and the three
#: element-system ones have not.
EXPECTED_DEFERRED = frozenset({"INV-4", "INV-7", "INV-8"})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _msg_inv_module():
    """The ONE implementation of the messages-domain criteria."""
    from agent.memory import msg_inv
    return msg_inv


def _main_chain_files(workspace: Path):
    """The registered JSONL files — read through the criterion library's
    OWN discovery (never a glob of our own)."""
    msg_inv = _msg_inv_module()
    return msg_inv._main_chain_files(workspace)


def _export_root(workspace: Path) -> Path:
    msg_inv = _msg_inv_module()
    return msg_inv._export_root(workspace)


def _report_of(checks: Dict[str, Any]) -> str:
    """One line per criterion: the name, the verdict, the coverage."""
    out = []
    for name in MSG_INV_NAMES:
        chk = (checks or {}).get(name)
        if not chk:
            out.append("    %-12s <缺项 / MISSING>" % name)
            continue
        out.append("    %-12s %-7s %s" % (name, chk.get("verdict"),
                                          chk.get("fingerprint")))
        for v in chk.get("violations") or []:
            out.append("        ! %s: %s" % (v.get("code"), v.get("detail")))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# M-1 — the patrol's real output: seven verdicts, none missing
# ---------------------------------------------------------------------------

def judge_m1_msg_inv_group_is_complete(report: Report, workspace: Path):
    """M-1 (REAL): the messages-domain group exists, carries all seven
    verdicts, and is GREEN on a library that was externalised.

    **The measurement is the PATROL's, not ours.**  The criterion calls
    the inspector's own wiring (the same path the patrol takes) and
    reports what came back.  A criterion that re-implemented the
    question would be a second mouth for it.

    **The missing-item check is a SET comparison.**  阶段 5 中间态判据 ①:
    "7 条各有 verdict（不许缺项）".  A group that carries six of the seven
    and reports green has not measured the seventh — so the criterion
    fails on a missing name, and prints WHICH one is missing.

    **Why the verdict is a group-level verdict and not seven criteria.**
    The施工图 asks for ONE group with seven verdicts.  Splitting it into
    seven report rows would make the report say "MSG-INV-2 failed" while
    the underlying library is not the one being measured — the granularity
    trap that broke J-3×M6e.  So the group is one row, and the seven
    verdicts are IN the detail, where the reader can see them.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M-1(REAL)",
            "M-1 messages 域判据组：巡检报告有 msg_inv 组，7 条判据齐全且 verdict=GREEN",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.inspector import Inspector
    except Exception as e:
        return report.judge_notrun(
            "M-1(REAL)",
            "M-1 messages 域判据组：巡检报告有 msg_inv 组，7 条判据齐全且 verdict=GREEN",
            "巡检模块不可用（%s: %s）—— 本判据读巡检的真实产出，不自己重算"
            % (type(e).__name__, e))

    ok = True
    red: List[str] = []
    notrun: List[str] = []
    measured: List[str] = []

    for lib in libs:
        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        try:
            # The patrol is run against the library's OWN workspace: the
            # messages truth tree lives under the workspace root, and a
            # ROOT library's root is the workspace, an agent library's
            # root is the agent's own directory.
            report_data = Inspector(lib.workspace).inspect(
                trigger="manual", persist_snapshot=False)
        except Exception as e:
            detail.append("      巡检失败（%s: %s）" % (type(e).__name__, e))
            notrun.append(lib.name)
            continue

        group = report_data.get("msg_inv")
        if not group:
            detail.append("      ! 巡检报告里没有 msg_inv 组 —— W-1 的接线不在")
            ok = False
            red.append(lib.name)
            continue

        checks = group.get("checks") or {}
        present = set(checks)
        missing = [n for n in MSG_INV_NAMES if n not in present]
        extra = sorted(present - set(MSG_INV_NAMES))
        if missing:
            detail.append("      ! 缺 %d 条判据: %s —— 报绿但没查"
                          % (len(missing), ", ".join(missing)))
            ok = False
            red.append(lib.name)
        if extra:
            detail.append("      (多出的判据名: %s)" % ", ".join(extra))
        detail.append(_report_of(checks))

        verdict = group.get("verdict")
        detail.append("      组判定: %s" % verdict)
        if verdict == "RED":
            ok = False
            if lib.name not in red:
                red.append(lib.name)
        elif verdict == "NOTRUN":
            notrun.append(lib.name)
            detail.append("      NOTRUN：这个库没有外置过 messages —— "
                          "『没查』不是『查过没问题』")
        elif verdict == "GREEN":
            measured.append(lib.name)

    detail.append("")
    detail.append("判据组: %s" % ", ".join(MSG_INV_NAMES))
    detail.append("全活库合计: 实测绿=%d 红=%d NOTRUN=%d"
                  % (len(measured), len(red), len(notrun)))
    if measured:
        detail.append("实测为绿的库（真的查过，且查过没问题）: %s"
                      % ", ".join(measured))
    if red:
        detail.append("红的库: %s" % ", ".join(red))
    if notrun:
        detail.append("NOTRUN 的库（未外置，按契约 NOTRUN 而非 GREEN）: %s"
                      % ", ".join(notrun))
    detail.append("（NOTRUN 的库不是失败：契约 N-7 说 messages 只在 ROOT 库，"
                  "其余库没有可测的东西 —— 但它们也不许被算成绿。）")

    if red:
        return report.judge(
            "M-1(REAL)",
            "M-1 messages 域判据组：巡检报告有 msg_inv 组，7 条判据齐全且 verdict=GREEN",
            False, "\n".join(detail))
    if not measured:
        # NO library was measured.  That is an honest "cannot measure",
        # and it is NOT a pass — the same rule the harness follows for a
        # criterion whose precondition never held.  (A fleet where every
        # library is NOTRUN is a fleet where nobody looked.)
        return report.judge_notrun(
            "M-1(REAL)",
            "M-1 messages 域判据组：巡检报告有 msg_inv 组，7 条判据齐全且 verdict=GREEN",
            "\n".join(detail))
    return report.judge(
        "M-1(REAL)",
        "M-1 messages 域判据组：巡检报告有 msg_inv 组，7 条判据齐全且 verdict=GREEN",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# M-2 — an un-externalised library is NOTRUN, never GREEN
# ---------------------------------------------------------------------------

def judge_m2_un_externalised_is_notrun(report: Report, workspace: Path):
    """M-2 (REAL): the messages criteria do not answer about a library
    that was never externalised.

    **This is the criterion that proves M-1 is not a hollow green.**  施工图
    §3.3 判据自身的判据 A/B:

      A — 未外置库上 MSG-INV-1..7 必须判 NOTRUN，不得判 GREEN；
      B — ``messages_export/`` 缺失或为空、基线缺失 → NOTRUN。

    The precondition is decided by the criterion library's OWN
    ``_precondition`` — so the criterion and the measurement cannot
    disagree about what "not externalised" means.  If the measurement
    ever started answering GREEN about an empty truth tree, this
    criterion would go red, and the reason it prints would name the
    criterion that misbehaved.

    **The libraries that have no messages table at all are skipped, not
    failed.**  Most of the fleet has no ``messages`` table (the messages
    live in the ROOT library only — 契约 N-7 / 跨端清单 X-8).  Such a
    library has nothing to judge, and the criterion says so.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M-2(REAL)",
            "M-2 未外置库必须 NOTRUN：没有 messages_export/ 的库，判据不许给 GREEN",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        msg_inv = _msg_inv_module()
    except Exception as e:
        return report.judge_notrun(
            "M-2(REAL)",
            "M-2 未外置库必须 NOTRUN：没有 messages_export/ 的库，判据不许给 GREEN",
            "判据库不可用（%s: %s）" % (type(e).__name__, e))

    ok = True
    checked: List[str] = []
    skipped: List[str] = []

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "messages" not in tables:
                skipped.append(lib.name)
                detail.append("  %-22s 没有 messages 表 —— 不适用（N-7："
                              "messages 只在 ROOT 库）" % lib.name)
                continue
            try:
                res = msg_inv.run_msg_invariants(lib.workspace, lib.db, "")
            except Exception as e:
                detail.append("  %-22s 判据执行失败: %s: %s"
                              % (lib.name, type(e).__name__, e))
                ok = False
                continue
        finally:
            conn.close()

        root = _export_root(lib.workspace)
        has_tree = root.is_dir() and bool(_main_chain_files(lib.workspace))
        verdict = res.get("verdict")
        checks = res.get("checks") or {}
        verdicts = {n: (checks.get(n) or {}).get("verdict")
                    for n in MSG_INV_NAMES}

        detail.append("  %-22s truth_tree=%s 组判定=%s"
                      % (lib.name, "有" if has_tree else "无", verdict))
        for n in MSG_INV_NAMES:
            detail.append("      %-12s %s" % (n, verdicts.get(n)))

        if has_tree:
            # The tree exists -> the criteria MUST have measured.
            checked.append(lib.name)
            if verdict == "NOTRUN":
                detail.append("      ! 有外置真源树却判 NOTRUN —— 前置与事实不符")
                ok = False
            elif verdict != "GREEN" and verdict != "RED":
                detail.append("      ! 非法判定: %r" % verdict)
                ok = False
        else:
            # No tree -> the criteria MUST be NOTRUN, never GREEN.
            checked.append(lib.name)
            if verdict != "NOTRUN":
                detail.append("      ! 未外置的库被判为 %s —— 判据自身的判据 A："
                              "没有 messages_export/ 时必须 NOTRUN，绝不许 GREEN"
                              % verdict)
                ok = False
            greens = [n for n, v in verdicts.items() if v == "GREEN"]
            if greens:
                detail.append("      ! 其中 %s 判了 GREEN" % ", ".join(greens))

    detail.append("")
    detail.append("判定过的库: %d（其中有真源树的 %d，无真源树的 %d）"
                  % (len(checked), len([l for l in checked
                                         if _export_root(
                                             [x for x in libs
                                              if x.name == l][0].workspace
                                         ).is_dir()]),
                     len([l for l in checked
                          if not _export_root(
                              [x for x in libs if x.name == l][0].workspace
                          ).is_dir()])))
    detail.append("不适用的库（没有 messages 表）: %d" % len(skipped))
    detail.append("规则：真源树缺失 → 全 7 条 NOTRUN；真源树存在 → 不得 NOTRUN。")

    if not checked:
        return report.judge_notrun(
            "M-2(REAL)",
            "M-2 未外置库必须 NOTRUN：没有 messages_export/ 的库，判据不许给 GREEN",
            "\n".join(detail))
    return report.judge(
        "M-2(REAL)",
        "M-2 未外置库必须 NOTRUN：没有 messages_export/ 的库，判据不许给 GREEN",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# M-3 — the wiring is real (static + behavioural)
# ---------------------------------------------------------------------------

def _inspector_calls_run_msg_invariants(source: str) -> bool:
    """Does the inspector's source really CALL ``run_msg_invariants``?

    **Parsed with ``ast``, not matched with a string.**  A string match
    finds the text whether or not it is inside a function body, and the
    criterion would then be scanning a comment.  The structural question
    is "is there a call to ``run_msg_invariants`` inside the inspector
    module" — and the AST can answer exactly that.

    The attribute spelling is matched loosely (``.run_msg_invariants``
    on any object) because the wiring's shape is the implementation's
    business; the CALL is the contract.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and \
                func.attr == "run_msg_invariants":
            return True
        if isinstance(func, ast.Name) and func.id == "run_msg_invariants":
            return True
    return False


def judge_m3_the_wiring_is_real(report: Report, workspace: Path):
    """M-3 (unit): the MSG-INV criteria are WIRED INTO the patrol.

    Two proofs, because either alone is insufficient:

      * **static** — the inspector's source really calls
        ``run_msg_invariants`` (parsed with ``ast``, so a call in a
        comment does not count);
      * **behavioural** — the patrol's report really carries a
        ``msg_inv`` group with the seven verdicts.

    A static proof alone passes on a call that is never reached; a
    behavioural proof alone passes on a group assembled by hand.  Both
    are needed, and the criterion says which half failed.
    """
    detail: List[str] = []
    try:
        from agent.memory import inspector as inspector_mod
    except Exception as e:
        return report.judge_notrun(
            "M-3(unit)",
            "M-3 接线是真的：inspector 调用 msg_inv.run_msg_invariants，巡检报告出现 msg_inv 组",
            "inspector 模块不可用（%s: %s）" % (type(e).__name__, e))

    # --- half 1: the static proof -----------------------------------
    src_path = Path(inspector_mod.__file__)
    try:
        src = src_path.read_text(encoding="utf-8")
    except OSError as e:
        return report.judge_notrun(
            "M-3(unit)",
            "M-3 接线是真的：inspector 调用 msg_inv.run_msg_invariants，巡检报告出现 msg_inv 组",
            "读不到 inspector 源码（%s: %s）" % (type(e).__name__, e))
    static_ok = _inspector_calls_run_msg_invariants(src)
    detail.append("  静态证据: %s" % src_path)
    detail.append("    源码里存在 run_msg_invariants 的调用（AST 解析）: %s"
                  % static_ok)

    # --- half 2: the behavioural proof ------------------------------
    behavioural_ok = False
    behaviour_detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        detail.append("  行为证据: 本工作区没有活库 —— 巡检跑不了")
    else:
        lib = libs[0]
        try:
            from agent.memory.inspector import Inspector
            rep = Inspector(lib.workspace).inspect(trigger="manual",
                                                    persist_snapshot=False)
            group = rep.get("msg_inv")
            if group is None:
                behaviour_detail.append("巡检报告里没有 msg_inv 组")
            else:
                names = set((group.get("checks") or {}))
                missing = [n for n in MSG_INV_NAMES if n not in names]
                behaviour_detail.append(
                    "msg_inv 组存在，checks=%d 条，缺项=%s"
                    % (len(names), missing or "无"))
                behavioural_ok = not missing
        except Exception as e:
            behaviour_detail.append("巡检失败（%s: %s）" % (type(e).__name__, e))
        detail.append("  行为证据: %s" % lib.name)
        for line in behaviour_detail:
            detail.append("    %s" % line)

    ok = static_ok and behavioural_ok
    detail.append("")
    detail.append("静态证明: %s   行为证明: %s   两者皆真才通过"
                  % (static_ok, behavioural_ok))
    if not static_ok:
        detail.append("  —— 源码里没有 run_msg_invariants 的调用：接线不在"
                      "（W-1 未落地）")
    if not behavioural_ok:
        detail.append("  —— 巡检跑起来却没有完整的 msg_inv 组：接线不在"
                      "（W-1 未落地）")
    return report.judge(
        "M-3(unit)",
        "M-3 接线是真的：inspector 调用 msg_inv.run_msg_invariants，巡检报告出现 msg_inv 组",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# M-4 — INV-5 is no longer a deferred invariant
# ---------------------------------------------------------------------------

def judge_m4_inv5_is_no_longer_deferred(report: Report, workspace: Path):
    """M-4 (unit): the deferred set is exactly ``{"INV-4","INV-7","INV-8"}``.

    施工图 W-3: ``INV-5`` left ``text_inv.DEFERRED_INVS`` when the
    messages domain landed (its successor is MSG-INV-2/3).  The
    criterion reads the deferred set the IMPLEMENTATION reports — the
    ``run_text_invariants`` report carries it — and compares it as a SET.

    **Why the report and not the dictionary.**  The dictionary is the
    implementation's own data; the report is what the patrol publishes.
    A criterion that read the dictionary would pass while the patrol
    still published a stale set.  Reading the report is the same
    discipline M-1 uses: read what is actually published.
    """
    detail: List[str] = []
    try:
        from agent.memory import text_inv
    except Exception as e:
        return report.judge_notrun(
            "M-4(unit)",
            "M-4 INV-5 不再是暂缓项：deferred 集合 == {INV-4, INV-7, INV-8}",
            "text_inv 不可用（%s: %s）" % (type(e).__name__, e))

    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M-4(unit)",
            "M-4 INV-5 不再是暂缓项：deferred 集合 == {INV-4, INV-7, INV-8}",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    lib = libs[0]
    conn = open_ro(lib.db)
    try:
        try:
            res = text_inv.run_text_invariants(conn, lib.workspace)
        except Exception as e:
            return report.judge_notrun(
                "M-4(unit)",
                "M-4 INV-5 不再是暂缓项：deferred 集合 == {INV-4, INV-7, INV-8}",
                "run_text_invariants 执行失败（%s: %s）" % (type(e).__name__, e))
    finally:
        conn.close()

    deferred = res.get("deferred") or {}
    got = set(deferred)
    detail.append("库: %s" % lib.name)
    detail.append("报告里的 deferred 集合: %s" % (sorted(got) or "空"))
    for k in sorted(got):
        detail.append("    %-8s %s" % (k, deferred[k]))
    detail.append("期望: %s" % sorted(EXPECTED_DEFERRED))

    # The source dictionary, as a SECOND reading — the two must agree,
    # or the implementation publishes one set and computes another.
    src_deferred = set(getattr(text_inv, "DEFERRED_INVS", {}) or {})
    detail.append("源码 DEFERRED_INVS: %s" % sorted(src_deferred))
    detail.append("报告与源码一致: %s" % (src_deferred == got))

    ok = (got == set(EXPECTED_DEFERRED) and src_deferred == got)
    if "INV-5" in got:
        detail.append("  —— INV-5 还在暂缓列表里：messages 外置已经落地，"
                      "它的判据是 MSG-INV-2/3（W-3 未完成）")
    missing = sorted(set(EXPECTED_DEFERRED) - got)
    if missing:
        detail.append("  —— 少了 %s：暂缓集合与施工图 W-3 不符" % ", ".join(missing))
    if src_deferred != got:
        detail.append("  —— 报告与源码不一致：巡检发布的暂缓集合和实现算的不是一个")
    return report.judge(
        "M-4(unit)",
        "M-4 INV-5 不再是暂缓项：deferred 集合 == {INV-4, INV-7, INV-8}",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# M-5 — the status page self-reports (and "not applicable" is not 0.0)
# ---------------------------------------------------------------------------

def judge_m5_status_self_reports(report: Report, workspace: Path):
    """M-5 (unit): ``get_status`` exposes the two keys, and "not
    applicable" is ``None`` — never ``0.0``.

    施工图 W-4 / 契约 v2 G-08 / N-8:

      * ``msg_chain_anchor_scope == "same_dir"`` — the chain-head anchor
        lives in the SAME directory tree as the JSONL it anchors.  That
        is a REGISTERED exposure (风控 G-08), and the status page is
        where the registration lives, so it is a FACT ON THE PAGE rather
        than a finding in a report;
      * ``cold_read_latency_ms`` — there is no warm-up for a cold
        session, so the cold-read latency must be SELF-REPORTED.  It is
        a float when there was a cold read to measure, and ``None`` when
        there was not.

    **The trap this criterion exists for.**  A status page that reports
    ``0.0`` for "no cold read happened" says "the read was instant" —
    a claim about a measurement that was never taken.  ``None`` says
    "there was nothing to measure".  The two are different claims, and
    only one of them is honest.  So the criterion REJECTS ``0.0`` on a
    library that has no externalised message to read.

    The measurement is on a THROWAWAY workspace (a copy of the live
    library under a temporary root), so the criterion never opens the
    live database for writing and never perturbs the live corpus.
    """
    detail: List[str] = []
    try:
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager
    except Exception as e:
        return report.judge_notrun(
            "M-5(unit)",
            "M-5 status 自曝：msg_chain_anchor_scope == same_dir，cold_read_latency_ms 是 float 或 None",
            "manager 不可用（%s: %s）" % (type(e).__name__, e))

    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M-5(unit)",
            "M-5 status 自曝：msg_chain_anchor_scope == same_dir，cold_read_latency_ms 是 float 或 None",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    import shutil
    import tempfile

    lib = libs[0]
    tmp = Path(tempfile.mkdtemp(prefix="s3m-status-"))
    try:
        # A throwaway workspace with a COPY of the live library: the
        # status probe reads the messages truth tree, and the live tree
        # must not be touched by a criterion.
        ws = tmp / "ws"
        (ws / "memory" / "long-term").mkdir(parents=True)
        shutil.copy2(lib.db, ws / "memory" / "long-term" / "index.db")
        # The messages truth tree, copied too — the probe measures a
        # cold read through the REAL ContentReader, so it needs the
        # JSONL.  (It is a copy: the criterion never writes the live
        # tree.)
        src_root = _export_root(lib.workspace)
        if src_root.is_dir():
            shutil.copytree(src_root, ws / "memory" / "messages_export")

        cfg = MemoryConfig(workspace_root=str(ws))
        mgr = MemoryManager(cfg)
        try:
            status = mgr.get_status()
        finally:
            try:
                mgr.close()
            except Exception:
                pass

        detail.append("库（副本）: %s" % lib.db)
        detail.append("status 里的两个键: %s"
                      % [k for k in ("msg_chain_anchor_scope",
                                     "cold_read_latency_ms")
                         if k in status])

        ok = True
        scope = status.get("msg_chain_anchor_scope")
        detail.append("  msg_chain_anchor_scope = %r" % scope)
        if scope != "same_dir":
            detail.append("    —— 不是 'same_dir'：锚点范围没有自曝，"
                          "或者自曝了别的值（W-4 未完成）")
            ok = False

        if "cold_read_latency_ms" not in status:
            detail.append("  cold_read_latency_ms <缺项>")
            detail.append("    —— 键不在 status 里：冷读延迟没有自曝（W-4 未完成）")
            ok = False
        else:
            latency = status.get("cold_read_latency_ms")
            detail.append("  cold_read_latency_ms = %r (%s)"
                          % (latency, type(latency).__name__))
            if latency is None:
                detail.append("    —— None = 『不适用』（没有外置消息可读）"
                              "：这是合法答案")
            elif isinstance(latency, bool) or not isinstance(latency, float):
                detail.append("    —— 值既不是 float 也不是 None："
                              "『不适用』不许伪装成别的东西")
                ok = False
            elif latency < 0:
                detail.append("    —— 负的延迟不是延迟")
                ok = False
            elif latency == 0.0:
                # The trap: 0.0 claims "the read was instant".  It is
                # only honest if a cold read really happened AND took
                # no measurable time.  On a library that HAS externalised
                # rows, 0.0 is a measurement; on one that has none, it
                # is a disguised "not applicable".
                has_external = False
                try:
                    conn = sqlite3.connect(
                        "file:%s?mode=ro"
                        % str(ws / "memory" / "long-term" / "index.db")
                        .replace("\\", "/"), uri=True)
                    try:
                        row = conn.execute(
                            "SELECT COUNT(*) FROM messages WHERE "
                            "content_state='external'").fetchone()
                        has_external = bool(row and row[0])
                    except sqlite3.Error:
                        has_external = False
                    finally:
                        conn.close()
                except sqlite3.Error:
                    has_external = False
                detail.append("    该库外置行数: %s" % has_external)
                if not has_external:
                    detail.append("    —— 0.0 出现在一个没有外置消息的库上："
                                  "『不适用』必须报 None，0.0 是伪装成的『瞬时』")
                    ok = False
                else:
                    detail.append("    —— 0.0 在有外置行的库上是实测值，"
                                  "但它意味着探针没有真正读到东西（可疑）")
        return report.judge(
            "M-5(unit)",
            "M-5 status 自曝：msg_chain_anchor_scope == same_dir，cold_read_latency_ms 是 float 或 None",
            ok, "\n".join(detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# M-6 — the ledger's orphans are pruned at the WRITE boundary, and the
#      detector stays awake
# ---------------------------------------------------------------------------

def judge_m6_ledger_orphans_are_pruned_at_the_write_boundary(
        report: Report, workspace: Path):
    """M-6 (REAL): the write boundary prunes the orphans, and M6f still
    catches what the write boundary cannot see.

    任务二 put ``_prune_ledger_entries`` in ``storage.py``: a re-cut that
    removes chunk ids removes their ``cert_ledger`` entries in the SAME
    transaction.  That is the fix for M6f crying wolf on a NORMAL state
    (a re-cut moves the ids wholesale, and the old ids were kept
    forever).

    **Two halves, because the fix must not blind the detector:**

      1. **the prune works** — after a re-cut, the entries of the ids
         that disappeared are GONE, and the entries of the ids that came
         back are KEPT.  The "kept" half is the load-bearing wall: the
         S3-L restore triggers read the ledger to bring the certified
         state back, so a blanket cascade would de-certify every re-cut
         file;
      2. **the detector stays awake** — an orphan produced by a FOREIGN
         writer's raw ``DELETE`` (which does not go through the write
         path) must still be reported by the M6f detector.  A detector
        that stopped reporting because the write path now cleans up
        would be a detector that goes blind exactly where it is needed.

    The mutation happens on a COPY of a live library — the live index is
    never written to by a criterion.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M-6(REAL)",
            "M-6 账本孤儿在写边界被剪枝（消失的 id 清、存活的 id 留），且检测器不瞎",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory import ledger_orphans
        from agent.memory.storage import MemoryStorage
    except Exception as e:
        return report.judge_notrun(
            "M-6(REAL)",
            "M-6 账本孤儿在写边界被剪枝（消失的 id 清、存活的 id 留），且检测器不瞎",
            "实现侧不可用（%s: %s）" % (type(e).__name__, e))

    # The library that HAS a ledger and enough rows to re-cut.
    target = None
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "cert_ledger" not in tables:
                continue
            rows = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            ledger = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
        finally:
            conn.close()
        if rows and ledger:
            target = lib
            break
    if target is None:
        return report.judge_notrun(
            "M-6(REAL)",
            "M-6 账本孤儿在写边界被剪枝（消失的 id 清、存活的 id 留），且检测器不瞎",
            "没有既有账本又有行的活库 —— 前置条件不成立（这不是通过）")

    detail.append("被测库（副本）: %s" % target.db)

    import shutil
    import tempfile

    from agent.memory.storage import MemoryChunk

    tmp = Path(tempfile.mkdtemp(prefix="s3m-ledger-"))
    try:
        copy = tmp / "index.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(target.db) + suffix)
            if src.is_file():
                shutil.copy2(src, Path(str(copy) + suffix))

        # --- the copy's trigger order must be REPAIRED first ----------
        #
        # **Why this step exists (MEASURED, 2026-10-31).**  Opening a
        # COPY of a live library with ``MemoryStorage`` re-installs the
        # body guard, and a re-created trigger lands at the END of
        # ``sqlite_master`` — i.e. BEHIND the FTS sync triggers.  That
        # is the malformed-risk shape (``guard_sweep``'s own docstring
        # names it), and the symptom is a write that dies with
        # ``database disk image is malformed``.
        #
        # The reason it happens on a COPY and not on the live library:
        # ``body_guard.is_installed`` compares the trigger BODIES BY NAME
        # and deliberately ignores the rowid order — so a copy whose
        # guard bodies are byte-identical still reads as "not installed"
        # when the copy is made from a WAL that the ``-wal`` sidecar
        # carries, and the repair flips the order.
        #
        # So the criterion repairs the order with the PRODUCTION tool
        # (``guard_sweep``) before it writes — the same way the fleet is
        # kept in shape — and it REPORTS the order it found.  A
        # criterion that did not repair it would be measuring a
        # malformed copy, and would report the malformed copy's
        # exception instead of the ledger's state.
        from agent.memory.guard_sweep import install_body_guard_on
        sweep_rep = install_body_guard_on(copy)
        detail.append("副本的守卫修复: status=%s fts_reordered=%s"
                      % (sweep_rep.get("status"), sweep_rep.get("fts_reordered")))

        # --- half 1: the prune works ---------------------------------
        #
        # **The REAL write path is exercised.**  The criterion does not
        # call ``_prune_ledger_entries`` itself — it calls
        # ``replace_file_chunks`` (the write path that owns the prune)
        # and reads the ledger afterwards.  Calling the prune directly
        # would be the criterion testing its OWN idea of the prune
        # instead of the implementation's: a mutant that made the
        # write path stop pruning would still be green.
        storage = MemoryStorage(db_path=str(copy))
        try:
            conn = storage.conn
            conn.row_factory = sqlite3.Row

            # The rows of ONE path, and their ledger entries.
            row = conn.execute(
                "SELECT path FROM chunks WHERE path IS NOT NULL "
                "GROUP BY path HAVING COUNT(*) >= 2 LIMIT 1").fetchone()
            if row is None:
                return report.judge_notrun(
                    "M-6(REAL)",
                    "M-6 账本孤儿在写边界被剪枝（消失的 id 清、存活的 id 留），且检测器不瞎",
                    "\n".join(detail + ["没有一条至少两块的路径 —— "
                                        "无法造出『重切后 id 移动』的形状"]))
            path = row["path"]
            before = conn.execute(
                "SELECT id, start_line, end_line, text_state, body_origin, "
                "body_path, body_anchor, body_hash FROM chunks WHERE path=? "
                "ORDER BY start_line", (path,)).fetchall()
            ids = [r["id"] for r in before]
            detail.append("路径: %s （%d 块）" % (path, len(ids)))

            ledger_before = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger WHERE id IN (%s)"
                % ",".join("?" * len(ids)), ids).fetchone()[0]
            detail.append("这些 id 的账本条目（重切前）: %d" % ledger_before)

            # The re-cut: the SAME path, re-cut into DIFFERENT segments,
            # so every id moves (the ids are position-derived).  The
            # first block SURVIVES (same id), the others are GONE.
            survivor = before[0]
            survivors = [survivor["id"]]
            gone_ids = []
            new_chunks = []
            for i, r in enumerate(before):
                if i == 0:
                    # The survivor: the same id, the same segment.
                    new_chunks.append(MemoryChunk(
                        id=r["id"], user_id=None, scope="shared",
                        source="memory", path=path,
                        start_line=r["start_line"], end_line=r["end_line"],
                        text="text of %s" % r["id"], embedding=None,
                        hash="h-%s" % r["id"], metadata=None,
                        body_path=r["body_path"],
                        body_anchor=r["body_anchor"],
                        body_hash=r["body_hash"],
                        text_state=r["text_state"],
                        body_origin=r["body_origin"]))
                else:
                    gone_ids.append(r["id"])
                    new_chunks.append(MemoryChunk(
                        id="%s::moved-%d" % (r["id"][:16], i), user_id=None,
                        scope="shared", source="memory", path=path,
                        start_line=900 + i * 10, end_line=905 + i * 10,
                        text="text of %s" % r["id"], embedding=None,
                        hash="h-%s" % r["id"], metadata=None,
                        body_path=r["body_path"],
                        body_anchor="L%d-%d" % (900 + i * 10, 905 + i * 10),
                        body_hash=r["body_hash"],
                        text_state=r["text_state"],
                        body_origin=r["body_origin"]))
            detail.append("消失的 id（重切后不再存在）: %d" % len(gone_ids))
            detail.append("存活的 id（重切后回来了）: %d" % len(survivors))

            # **THE WRITE PATH.**  This is the call that owns the prune.
            storage.replace_file_chunks(path, new_chunks, file_hash="H2",
                                        mtime=2, size=1, chunker_version=2)

            ghosts_left = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger WHERE id IN (%s)"
                % ",".join("?" * len(gone_ids)), gone_ids).fetchone()[0] \
                if gone_ids else 0
            detail.append("重切后，消失 id 的账本条目: %d（必须为 0）"
                          % ghosts_left)

            kept = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger WHERE id=?",
                (survivor["id"],)).fetchone()[0]
            detail.append("存活 id 的账本条目（必须保留）: %d / 1" % kept)

            orphans_after_prune = ledger_orphans.judge_m6f_ledger_has_no_orphans(
                conn, library=target.name)
            detail.append("重切后 M6f 的判定: %s（孤儿=%d）"
                          % (orphans_after_prune.get("verdict"),
                             orphans_after_prune.get("orphans")))
        finally:
            storage.close()

        # --- half 2: the detector stays awake ------------------------
        # A foreign writer's raw DELETE does not go through the write
        # path.  The detector must still report it.
        #
        # **交办 B (2026-11-05): this half's PREMISE changed, and the
        # change is the point of that round.**  The ledger guard
        # (``chunks_cert_revoke_bd``) now revokes the witness AT THE
        # WRITE BOUNDARY, so a foreign writer's raw DELETE does NOT
        # leave an orphan behind — MEASURED on this machine, 2026-11-05:
        #
        #     with the guard:      DELETE FROM chunks WHERE id='c-bbb'
        #                          => ledger=['c-aaa']  orphans=0
        #     with the guard gone: the same DELETE
        #                          => ledger=['c-aaa','c-bbb']  orphans=1
        #
        # So the orphan this half used to provoke is now prevented by a
        # mechanism that a foreign writer CANNOT bypass (it is in the
        # database, not in the application).  The half's REAL question
        # — "is the detector still awake?" — therefore has to be asked
        # of a shape the guard does NOT cover.
        #
        # **What the guard does NOT cover: the DETECTOR's own subject.**
        # The guard revokes a witness when a chunk is DELETED.  It does
        # not remove a witness whose chunk is ALREADY gone — that is
        # the pre-existing orphan, and it is exactly what M6f exists to
        # report.  So the honest shape is: **insert a witness for a
        # chunk that does not exist** (which is what a witness written
        # by a writer that never wrote the chunk looks like), and the
        # detector must report it.
        #
        # And the mechanism behind that shape is the SAME one a guard
        # regression would break: if the guard's revoke trigger stopped
        # firing, the orphan would appear on every delete — and the
        # detector would have to catch it.  So the half still has teeth:
        # it asks "does the detector report an orphan that exists?" —
        # and the GUARD's absence is what makes the orphan exist.
        storage2 = MemoryStorage(db_path=str(copy))
        try:
            conn2 = storage2.conn
            conn2.row_factory = sqlite3.Row
            # **交办 B (2026-11-05): the second half must not MASK the
            # first half's verdict.**  If the first half's re-cut took
            # the survivor's witness with it (which is what happens when
            # the guard's restore trigger is broken), there is nothing
            # left to delete — and a NOTRUN there would report the
            # criterion as "not measured" while the first half had
            # already FAILED.  **A NOTRUN that hides a FAIL is worse
            # than a FAIL**: it says "we did not look", and the reader
            # stops looking.
            #
            # So the order is: if the first half already failed, the
            # criterion FAILS — and the second half says why it could
            # not run.
            first_half_ok = (ghosts_left == 0 and kept == len(survivors))
            if not first_half_ok:
                detail.append("")
                detail.append("第一半已经报红（消失 id 清=%s 存活 id 留=%s）—— "
                              "第二半无法构造（副本里没有『有块也有证人』的行），"
                              "但这不是 NOTRUN：第一半的红就是本判据的红。"
                              % (ghosts_left == 0, kept == len(survivors)))
                return report.judge(
                    "M-6(REAL)",
                    "M-6 账本孤儿在写边界被剪枝（消失的 id 清、存活的 id 留），且检测器不瞎",
                    False, "\n".join(detail))
            victim = conn2.execute(
                "SELECT id FROM cert_ledger LIMIT 1").fetchone()
            if victim is None:
                return report.judge_notrun(
                    "M-6(REAL)",
                    "M-6 账本孤儿在写边界被剪枝（消失的 id 清、存活的 id 留），且检测器不瞎",
                    "\n".join(detail + ["副本里没有账本条目 —— "
                                        "无法构造『外来删除』的形状"]))
            vid = victim["id"]
            # The shape the guard does NOT cover: a witness whose chunk
            # is not in the index.  It is created the way a real one
            # appears — a writer certified a row, and the row is not
            # there (a re-cut moved the ids, a foreign writer deleted
            # the row without the guard, or a migration restored a
            # ledger from an older snapshot).
            conn2.execute(
                "INSERT OR REPLACE INTO cert_ledger (id, text_state, "
                "body_origin, body_path, body_hash, certified_at, "
                "certified_by) VALUES (?, 'mirror', 'file', "
                "'memory/never-existed.md', 'bh-none', 1, 'foreign')",
                ("foreign-witness-never-existed",))
            conn2.commit()
            rep = ledger_orphans.judge_m6f_ledger_has_no_orphans(
                conn2, library=target.name)
            detail.append("外来写入的证人（块不存在）之后的 M6f 判定: %s"
                          "（孤儿=%d）"
                          % (rep.get("verdict"), rep.get("orphans")))
            detector_awake = (rep.get("verdict") == "FAIL"
                              and rep.get("orphans", 0) >= 1)
            if not detector_awake:
                detail.append("  —— 检测器没有报 FAIL：一个块根本不存在的证人"
                              "被漏掉了（检测器瞎了）")
            # And the shape the guard DOES cover: a foreign writer's raw
            # DELETE must leave NO orphan, because the guard revoked the
            # witness at the write boundary.  This is the half that
            # proves the guard is at the boundary and not in the
            # application layer — and it is the half that the
            # application-layer prune CANNOT satisfy, because a foreign
            # writer does not go through the application at all.
            orphans_before_delete = rep.get("orphans", 0)
            conn2.execute("DELETE FROM chunks WHERE id=?", (vid,))
            conn2.commit()
            rep2 = ledger_orphans.judge_m6f_ledger_has_no_orphans(
                conn2, library=target.name)
            detail.append("外来 DELETE 之后的 M6f 判定: %s（孤儿=%d）"
                          "（守卫在写边界撤销了证人 ⇒ 不留孤儿）"
                          % (rep2.get("verdict"), rep2.get("orphans")))
            # The DELTA is what matters: the one orphan above is the
            # foreign witness we inserted ON PURPOSE — it is still
            # there, which is the POINT (the detector sees it and the
            # guard does not silently eat it).  What must not change is
            # the COUNT.
            boundary_holds = (rep2.get("orphans", 0) == orphans_before_delete)
            if not boundary_holds:
                detail.append("  —— 外来 DELETE 之后孤儿数变了：%d → %d"
                              "（守卫没有在写边界撤销证人）"
                              % (orphans_before_delete, rep2.get("orphans", 0)))
        finally:
            storage2.close()

        ok = (ghosts_left == 0 and kept == len(survivors)
              and detector_awake and boundary_holds)
        detail.append("")
        detail.append("写边界剪枝: 消失 id 清=%s 存活 id 留=%s"
                      % (ghosts_left == 0, kept == len(survivors)))
        detail.append("检测器仍能报出块不存在的证人: %s" % detector_awake)
        detail.append("外来 DELETE 不留孤儿（守卫在写边界）: %s" % boundary_holds)
        detail.append("（『保留』是承重墙：S3-L 的恢复触发器读账本把认证状态找回来，"
                      "一刀切会抹掉每一次重切的认证）")
        return report.judge(
            "M-6(REAL)",
            "M-6 账本孤儿在写边界被剪枝（消失的 id 清、存活的 id 留），且检测器不瞎",
            ok, "\n".join(detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# The criteria this module contributes, in the order the runner uses.
# ---------------------------------------------------------------------------

CRITERIA = [
    ("M-1", "judge_m1_msg_inv_group_is_complete"),
    ("M-2", "judge_m2_un_externalised_is_notrun"),
    ("M-3", "judge_m3_the_wiring_is_real"),
    ("M-4", "judge_m4_inv5_is_no_longer_deferred"),
    ("M-5", "judge_m5_status_self_reports"),
    ("M-6", "judge_m6_ledger_orphans_are_pruned_at_the_write_boundary"),
]
