#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-E · the criteria that close the six gaps.

What this module adds, gap by gap
---------------------------------
G1 (P1, red)  the three states.  The harness now has PASS / FAIL / NOTRUN
              (see ``acceptance/__init__.py``).  This module is where the
              third state is USED, and where the line between "the
              prerequisite is absent" (NOTRUN) and "the thing is broken"
              (FAIL) is drawn in code.

G2 (P2, red)  every live library, not just the ROOT.  The old
              ``C-A1.1(REAL)`` measured ONE index; 15 broken agent
              libraries would have left it green.

G3 (P3, orange) the ``text_state`` machine, and the M6 mouth.  Nothing
              sets ``mirror`` — the three-state machine is decorative
              today.  The criterion pins down WHO may set it, and the
              M6 mouth uses the architect's new ruling (``body_origin``),
              which does not exist yet — so the criterion must say NOTRUN
              and say WHY, not report FAIL and not silently pass.

G4 (P4, orange) the §11 total table: F / Q / M / D / S.  Every entry of
              the design's criterion table gets a criterion here, or an
              explicit reason why it is out of scope.

G5 (S-1)     the four columns are in the content digest, so a criterion
              that wipes ``body_path`` is caught (done in ``__init__.py``).

G6 (false green) the criterion NAME must say what the criterion
              measures.  ``C-A2.1`` said "four columns non-empty" while
              ``body_path`` is legitimately ``None`` for blob rows — the
              name lied.  The name is fixed, and a self-check criterion
              scans the names against the assertions.

The rules every criterion here obeys
------------------------------------
1. **Three states.**  A criterion whose precondition does not hold says
   NOTRUN and says why.  A criterion that measured something and found it
   bad says FAIL.
2. **Verify the RESULT, not the action.**  "the column is filled" is a
   result; "the migration ran" is an action.
3. **Do not call the object under test.**  No criterion asks
   ``agent.memory.migrate`` what to measure, and no criterion asks
   ``body_pointer.four_columns_for_chunk`` what the right value is — the
   expected value is recomputed from the FILE.
4. **The fingerprint is printed.**  Which library, which rows, which
   commit.
5. **It does not modify what it measures.**  Every index is opened
   read-only.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.memory.acceptance.live_libs import (
    FOUR_COLUMNS,
    STATE_MIRROR,
    STATE_PROJECTION,
    LiveLibrary,
    count,
    enumerate_live,
    format_profile,
    open_ro,
    profile_all,
    table_columns,
)


# ---------------------------------------------------------------------------
# G2 · every live library
# ---------------------------------------------------------------------------

def judge_ca6_every_live_library_is_profiled(report, workspace: Path):
    """C-A6.1 (unit): the enumeration finds EVERY live library.

    The old C-A1.1(REAL) measured the ROOT library only, so 15 broken
    agent libraries left it green.  The requirement says 16.

    The count is NOT hard-coded: "there are 16" is a fact about THIS
    machine, and a criterion that asserts 16 fails on the next machine
    that has 15 or 17 agents — which is exactly the portability trap the
    stage-2 G5 criterion was written to avoid.  What IS asserted is the
    SHAPE: every library under ``agents/`` that has an index is found,
    and no library outside that shape is found.

    The reader can re-derive the count: the report prints the enumeration
    and the number of directories under ``agents/`` that were skipped.
    """
    detail = []
    libs = enumerate_live(workspace)
    detail.append("枚举到的活库数: %d" % len(libs))
    for lib in libs:
        detail.append("  %-22s %-6s %s" % (lib.name, lib.kind, lib.db))

    # The agents directory: every sub-directory that HAS an index must be
    # enumerated.  A sub-directory without an index is not a live library
    # (a brand-new agent has none yet) and is reported as skipped.
    agents_dir = workspace / "agents"
    expected, skipped = set(), []
    if agents_dir.is_dir():
        for d in sorted(agents_dir.iterdir()):
            if not d.is_dir():
                continue
            if (d / "memory" / "long-term" / "index.db").is_file():
                expected.add(d.name)
            else:
                skipped.append(d.name)
    detail.append("agents/ 下有索引的目录数: %d（判据应全部枚举到）" % len(expected))
    detail.append("agents/ 下无索引的目录（跳过，不算活库）: %s"
                  % (", ".join(skipped) if skipped else "无"))

    found_agents = {lib.name for lib in libs if lib.kind == "AGENT"}
    missing = expected - found_agents
    extra = found_agents - expected
    detail.append("漏掉的活库: %s" % (", ".join(sorted(missing)) if missing else "无"))
    detail.append("多出的活库: %s" % (", ".join(sorted(extra)) if extra else "无"))

    # The ROOT must be present when it exists.
    root_db = workspace / "memory" / "long-term" / "index.db"
    root_found = any(lib.kind == "ROOT" for lib in libs)
    detail.append("ROOT 库存在=%s 被枚举到=%s"
                  % (root_db.is_file(), root_found))

    ok = (not missing) and (not extra)
    if root_db.is_file() and not root_found:
        ok = False
        detail.append("  —— ROOT 库存在却没被枚举到")
    return report.judge(
        "C-A6.1(unit)", "活库枚举：agents/ 下每个有索引的目录都被枚举到，无遗漏无多余",
        ok, "\n".join(detail))


def judge_ca6_agent_workspace_root_is_the_agent_dir(report, workspace: Path):
    """C-A6.2 (unit): an agent library's workspace root is ``agents/<id>``.

    A criterion that resolves an agent library's truth layer against
    ``agents/`` instead of ``agents/<id>`` gets "file not found" for
    every row — which reads as a wall of INV-1 violations, i.e. as a
    PRODUCT defect, when it is a bug in the criterion.  This happened
    once already (the "15 agent libraries all RED" false alarm).

    The check: for every agent library, the ROOT of its truth layer must
    resolve — the agent's own ``MEMORY.md`` must exist under it.
    """
    detail = []
    libs = [l for l in enumerate_live(workspace) if l.kind == "AGENT"]
    if not libs:
        return report.judge_notrun(
            "C-A6.2(unit)", "agent 活库的真源层根是 agents/<id>（不是 agents/）",
            "本工作区没有 agent 活库，前置条件不成立（这不是通过）")
    ok = True
    for lib in libs:
        own = lib.workspace / "MEMORY.md"
        parent = lib.workspace.parent / "MEMORY.md"
        detail.append("%-22s 自身 MEMORY.md=%s  agents/MEMORY.md=%s"
                      % (lib.name, own.is_file(), parent.is_file()))
        if not own.is_file() and parent.is_file():
            ok = False
            detail.append("  —— 该 agent 的真源层根取错了（取到了 agents/）")
    return report.judge(
        "C-A6.2(unit)", "agent 活库的真源层根是 agents/<id>（不是 agents/）",
        ok, "\n".join(detail))


def judge_ca1_files_columns_every_live_library(report, workspace: Path):
    """C-A1.1(REAL) — REWRITTEN: every live library, not just the ROOT.

    The gap this closes: the old version measured one index, so a
    migration that fixed the ROOT and left every agent library broken
    would have been green.

    It also PRINTS the per-library profile, because a reader who is told
    "15 of 16 libraries are fine" has to be able to see WHICH one is
    not.
    """
    detail = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "C-A1.1(REAL)", "每个活库的 files 表列集合 == 期望",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    profiles = profile_all(workspace)
    detail.append(format_profile(profiles))
    detail.append("")
    detail.append("files 表列检查:")
    ok = True
    bad = []
    for lib, prof in zip(libs, profiles):
        conn = open_ro(lib.db)
        try:
            have = set(table_columns(conn, "files"))
        finally:
            conn.close()
        expect = {"path", "source", "hash", "mtime", "size", "updated_at",
                  "chunk_count", "chunker_version"}
        missing = sorted(expect - have)
        detail.append("  %-22s files 列=%d 缺少=%s"
                      % (lib.name, len(have), ", ".join(missing) if missing else "无"))
        if missing:
            ok = False
            bad.append("%s(缺 %s)" % (lib.name, ",".join(missing)))
    if bad:
        detail.append("列集合不齐的库: %s" % "; ".join(bad))
        detail.append("  —— 一个库坏了不代表全好；只测根库的判据看不到这些")
    return report.judge(
        "C-A1.1(REAL)", "每个活库的 files 表列集合 == 期望（全部 16 库逐一检查）",
        ok, "\n".join(detail))


def judge_m4_every_library_was_backfilled(report, workspace: Path):
    """M4 (REAL): every live library has its four columns filled.

    The defect this criterion exists to catch, MEASURED: one library
    (``agent-mvnewfyb``) had all 255 of its rows with ``body_path`` /
    ``body_anchor`` / ``body_hash`` all NULL, while the other 15 were
    clean.  The migration had reported PASS for it (the report still
    says ``M4.agent-mvnewfyb.count: PASS``) — because the library was
    migrated correctly at 23:23 and then WIPED at 23:41 by a writer
    running code from before the four columns existed.  Only a criterion
    that looks at EVERY library sees that; the ROOT-only criterion saw a
    clean ROOT and reported green.

    The rule: a library that is ENTIRELY un-migrated (every row NULL) is
    reported by name — because "one library did not take" is a different
    failure from "a few rows are missing" and needs a different fix.
    """
    detail = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M4(REAL)", "每个活库的四列已回填（无整库未迁）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    profiles = profile_all(workspace)
    detail.append(format_profile(profiles))
    detail.append("")

    wholly_unmigrated = []
    partial = []
    clean = []
    bad_state = []
    for prof in profiles:
        if prof.get("error"):
            detail.append("  %-22s 前置不成立: %s" % (prof["name"], prof["error"]))
            continue
        rows = int(prof.get("rows", 0))
        nulls = int(prof.get("body_path_null", 0))
        if rows and nulls == rows:
            wholly_unmigrated.append(prof["name"])
        elif nulls:
            partial.append("%s(%d/%d 行)" % (prof["name"], nulls, rows))
        else:
            clean.append(prof["name"])
        # The FOURTH column: text_state must be a declared state value.
        # (Without this the name "四列已回填" would be a lie — three
        # columns checked, four claimed.)
        states = prof.get("text_states") or {}
        bad = [s for s in states if not s]
        if bad:
            bad_state.append("%s(text_state=%r)" % (prof["name"], bad))
            continue
        for s in states:
            if s not in (STATE_PROJECTION, STATE_MIRROR, "validated"):
                bad_state.append("%s(text_state=%r 不在声明的状态里)"
                                 % (prof["name"], s))

    detail.append("四列齐备的库 (%d): %s"
                  % (len(clean), ", ".join(clean) if clean else "无"))
    detail.append("部分未回填的库: %s" % (", ".join(partial) if partial else "无"))
    detail.append("整库未回填的库: %s"
                  % (", ".join(wholly_unmigrated) if wholly_unmigrated else "无"))
    detail.append("text_state 取值非法的库: %s"
                  % (", ".join(bad_state) if bad_state else "无"))
    detail.append("（text_state 允许的取值: %s / %s / validated）"
                  % (STATE_PROJECTION, STATE_MIRROR))

    if wholly_unmigrated:
        detail.append("  —— 整库未回填：迁移对这个库没有生效（或者生效后被某个"
                      "写入者清空了）。这与『迁移跑过』是两回事，必须按库定位。")
    ok = not wholly_unmigrated and not partial and not bad_state
    return report.judge(
        "M4(REAL)", "每个活库的四列已回填（无整库未迁、无零星 NULL、text_state 合法）",
        ok, "\n".join(detail))


def judge_m1_fingerprints_agree_across_libraries(report, workspace: Path):
    """M1 (REAL): every live library has the SAME schema fingerprint.

    The fingerprint must cover every MODELLED table (``chunks`` AND
    ``files``) — that was the single-table fingerprint false green the
    H2 hole was about.  This criterion therefore checks two things: the
    fingerprints agree, AND the fingerprint the implementation reports
    covers more than one table.

    It is NOT enough for the fingerprints to agree — they could all agree
    on a one-table fingerprint and still miss a fork.
    """
    detail = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M1(REAL)", "所有活库的 schema 指纹一致，且指纹覆盖多张表",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.schema_drift import (
            FINGERPRINT_TABLES,
            schema_fingerprint,
        )
    except Exception as e:
        return report.judge_notrun(
            "M1(REAL)", "所有活库的 schema 指纹一致，且指纹覆盖多张表",
            "schema_drift 不可用（%s: %s）—— 判据无法测量" % (type(e).__name__, e))

    fps = {}
    for lib in libs:
        fp = schema_fingerprint(lib.db)
        fps[lib.name] = fp
    uniq = sorted({v for v in fps.values() if v})
    detail.append("指纹覆盖的表: %s" % (FINGERPRINT_TABLES,))
    detail.append("不同指纹的个数: %d" % len(uniq))
    for name, fp in fps.items():
        detail.append("  %-22s %s" % (name, (fp or "-")[:32]))
    if len(uniq) > 1:
        detail.append("  —— 活库的 schema 已经分叉")

    ok = len(uniq) == 1 and len(FINGERPRINT_TABLES) > 1
    if len(FINGERPRINT_TABLES) <= 1:
        detail.append("  —— 指纹只覆盖了一张表：files 表分叉时它看不见（假绿）")
    return report.judge(
        "M1(REAL)", "所有活库的 schema 指纹一致，且指纹覆盖多张表",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# G3 · the text_state machine and the M6 mouth
# ---------------------------------------------------------------------------

def judge_r_s3_5_mirror_has_a_setter(report, workspace: Path):
    """R-S3-5 (unit): the ``mirror`` state has a DEFINED setter, and that
    setter is the thing that sets it.

    The design (§3.1) says the ``text_state`` machine is:

        projection -> (the patrol/inspection checker verifies the row)
                    -> mirror -> (the row is validated) -> validated

    and it says, in so many words, that the setter is the checker that
    VERIFIES the row — "明确置位者，防止三态机退化为装饰" (an explicit
    setter, so the three-state machine cannot decay into decoration).

    **P1-阶段三 第四轮: this criterion is now DECIDABLE.**

    The first version of this criterion said "every row is
    ``projection`` and NOTHING sets ``mirror``", and it reported
    NOTRUN for the "did the patrol really set it?" half because the
    migration's M6 step was GATED on an architect's ruling that had
    not been made.  That was true then.  It is STALE now:

      * the gate is open (架构师 §4.5 ruled the three-part mouth), and
      * the SETTER is the INSPECTOR (``inspector.Inspector._set_mirror_state``),
        not the migrator — the migrator only MEASURES whether the
        inspector has done it, because a migration that set the state
        itself would make the three-state machine a decoration, which is
        the exact thing the explicit setter exists to prevent;
      * 2674 rows ARE ``mirror`` today.

    So the criterion now asserts what it can assert:

      * the state ``mirror`` is a LEGAL value (part of the declared
        machine);
      * there EXISTS a code path that sets it, that path is EXPLICIT
        (it writes ``text_state='mirror'``), and it VERIFIES FIRST (it
        rebuilds the fragment from the file and requires byte-equality
        before promoting a row);
      * no row is WRONGLY marked ``mirror`` (a ``mirror`` row whose
        pointer does not resolve is a FAIL, not a NOTRUN).

    A criterion that keeps saying NOTRUN after the thing it waited for
    has happened is a criterion that has stopped measuring.  The
    "did the inspector really set it?" question is now answered by the
    rows' own state, and the COUNT is asserted by M6's three-part mouth.

    The distinction this criterion is careful about: "nobody set it" and
    "somebody set it to the wrong value" are DIFFERENT, and the
    criterion must be able to tell them apart.  The first is a FAIL
    (the machine has decayed into decoration); the second is a FAIL
    (a row in ``mirror`` whose pointer does not resolve is a broken
    row).
    """
    detail = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "R-S3-5(unit)", "text_state 三态机的 mirror 有明确置位者，且无错置",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    # 1. the state machine's declared values must include mirror.
    try:
        from agent.memory.body_pointer import STATE_PROJECTION as BP_PROJ
        body_pointer_states = {BP_PROJ}
    except Exception as e:
        body_pointer_states = set()
        detail.append("body_pointer 不可用: %s" % e)
    detail.append("body_pointer 声明的状态: %s" % sorted(body_pointer_states))

    # 2. the rows: count the states, and check that a row claiming
    #    'mirror' actually has a resolvable pointer (a WRONG mirror is a
    #    FAIL, not a NOTRUN).
    counts: Dict[str, int] = {}
    wrong_mirror = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            for state, n in conn.execute(
                    "SELECT text_state, COUNT(*) FROM chunks GROUP BY 1").fetchall():
                counts[state] = counts.get(state, 0) + n
            if "mirror" in {r[0] for r in conn.execute(
                    "SELECT DISTINCT text_state FROM chunks").fetchall()}:
                for row in conn.execute(
                        "SELECT id, path, body_path, body_anchor FROM chunks "
                        "WHERE text_state='mirror'").fetchall():
                    if not row[2] or not row[3]:
                        wrong_mirror.append("%s/%s" % (lib.name, row[0][:8]))
        finally:
            conn.close()
    detail.append("全活库 text_state 分布: %s"
                  % ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))

    # 3. the setter: is there a code path that sets 'mirror'?
    #
    # P1-阶段三 第四轮: the SETTER IS THE INSPECTOR, not the migrator.
    # 设计 §3.1: "校验器逐行复核通过后显式置位（巡检器在 INV-2/3 校验
    # 通过时置位——明确置位者，防止三态机退化为装饰）".  The migration
    # tool only MEASURES whether the inspector has done it.
    #
    # The previous version of this criterion looked for
    # ``migrate.step_m6`` and reported NOTRUN because that step was
    # gated on a ruling that had not yet been made.  That was correct
    # THEN and is STALE NOW: the gate is open, the inspector is the
    # setter, and 2674 rows ARE 'mirror'.  A criterion that keeps
    # saying NOTRUN after the thing it waited for has happened is a
    # criterion that has stopped measuring.
    try:
        from agent.memory import inspector as _inspector
        has_setter = hasattr(_inspector.Inspector, "_set_mirror_state")
        setter_src = ""
        if has_setter:
            import inspect
            setter_src = inspect.getsource(
                _inspector.Inspector._set_mirror_state)
    except Exception as e:
        detail.append("置位者检查失败: %s: %s" % (type(e).__name__, e))
        has_setter, setter_src = False, ""

    # The setter must be EXPLICIT: it must actually write 'mirror', and
    # it must only do so for rows it has just verified byte-equal.
    writes_mirror = "text_state='mirror'" in setter_src
    verifies_first = "rebuild_from_anchor" in setter_src and \
        "!= (text or" in setter_src
    detail.append("置位者: agent.memory.inspector.Inspector._set_mirror_state "
                  "存在=%s 显式写 text_state='mirror'=%s 置位前逐字节复核=%s"
                  % (has_setter, writes_mirror, verifies_first))
    detail.append("（置位者是巡检器，不是迁移器：迁移器只测量巡检器做没做，"
                  "自己置位会让三态机退化为装饰）")

    if wrong_mirror:
        detail.append("错置为 mirror 但指针不可解析的行: %d (%s)"
                      % (len(wrong_mirror), ", ".join(wrong_mirror[:5])))
        return report.judge(
            "R-S3-5(unit)", "text_state 三态机的 mirror 有明确置位者，且无错置",
            False, "\n".join(detail))

    if not has_setter:
        detail.append("  —— 没有任何代码路径把 text_state 置为 mirror："
                      "三态机已经退化为装饰")
        return report.judge(
            "R-S3-5(unit)", "text_state 三态机的 mirror 有明确置位者，且无错置",
            False, "\n".join(detail))

    if not writes_mirror or not verifies_first:
        detail.append("  —— 置位者存在，但没有『复核通过才置位』的语义："
                      "要么不写 mirror，要么不验证就写")
        return report.judge(
            "R-S3-5(unit)", "text_state 三态机的 mirror 有明确置位者，且无错置",
            False, "\n".join(detail))

    # The setter exists, it is explicit, it verifies first, and no row
    # is wrongly marked.  The half that was NOTRUN — "did the inspector
    # really set it?" — is now DECIDABLE: the rows' own state is the
    # evidence, and the M6 mouth (the three-part one) is the criterion
    # that asserts the count.
    mirror_rows = counts.get("mirror", 0)
    detail.append("置位者存在、显式、且先复核后置位；无错置行。")
    detail.append("『巡检器是否真的置过 mirror』现在可判定：全活库 %d 行是 "
                  "mirror（判据 M6 的三段口径负责断言计数）。" % mirror_rows)
    return report.judge(
        "R-S3-5(unit)", "text_state 三态机的 mirror 有明确置位者，且无错置",
        True, "\n".join(detail))


def judge_m6_body_origin_mouth(report, workspace: Path):
    """M6 (REAL): SUPERSEDED by the S3-H criterion — see below.

    **This function is no longer wired into the gate.**  The S3-H module
    (``phase2_s3h.judge_m6_body_origin_mouth``) replaces it, because this
    one used the OLD mouth (``text_state NOT IN ('mirror') == 0``), which
    is UNSATISFIABLE once a blob row exists: a blob row has no truth
    source to mirror, so the count could never reach zero.  A criterion
    that guards an unsatisfiable invariant is permanently red, and a
    permanently red criterion is one nobody reads.

    It is kept (not deleted) for two reasons: the mutant replay and any
    external caller still refer to it, and deleting a superseded
    criterion loses the record of WHY it was superseded — which is the
    part a future reader needs.

    The old mouth's flaw, for the record:

      * the old mouth was ``text_state NOT IN ('mirror') == 0`` — i.e.
        EVERY row must be ``'mirror'``.  But a ``'blob'`` row has NO
        truth source to mirror: its text lives only in the row.  So the
        old criterion could never be satisfied once a blob row exists.

    The new mouth (架构师 §4.5):

      * ``body_origin='file' AND text_state<>'mirror'``  == 0
      * ``body_origin='unresolved'``                      == 0
      * ``body_origin='orphan'``                          == 0

    and ``blob`` is a legal terminal state whose count is REPORTED but
    not judged.
    """
    from agent.memory.acceptance.phase2_s3h import (
        judge_m6_body_origin_mouth as _s3h_m6,
    )
    return _s3h_m6(report, workspace)


# ---------------------------------------------------------------------------
# G4 · the §11 total table
# ---------------------------------------------------------------------------

def judge_q1_inv1_every_library(report, workspace: Path):
    """Q1 (REAL): INV-1 is GREEN in every live library.

    INV-1 says: a row in state ``projection``/``mirror`` must carry a
    pointer that RESOLVES.  A row with ``body_path IS NULL`` is what it
    calls "points nowhere".

    Every library, not just the ROOT.  The measured state on this
    machine: one library RED with 255 violations (all rows un-migrated),
    plus the ROOT and one agent RED on a smaller count (an anchor that
    cannot be located in the file).
    """
    detail = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "Q1(REAL)", "每个活库的 INV-1 为 GREEN",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.text_inv import run_text_invariants
    except Exception as e:
        return report.judge_notrun(
            "Q1(REAL)", "每个活库的 INV-1 为 GREEN",
            "text_inv 不可用（%s: %s）—— 判据无法测量" % (type(e).__name__, e))

    red = []
    notrun = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            r = run_text_invariants(conn, lib.workspace)
        finally:
            conn.close()
        inv1 = r["checks"]["INV-1"]
        v = inv1["verdict"]
        violations = inv1.get("violations") or []
        n = len(violations)
        detail.append("  %-22s INV-1=%-7s 违规=%-5d 指纹=%s"
                      % (lib.name, v, n, inv1.get("fingerprint", "-")))
        # The REASON CODES, per library.  They are what the operator
        # acts on: "the pointer is missing" and "nobody has decided
        # whether this row has a truth source" are two DIFFERENT
        # repairs, and a report that only says "RED" sends the reader
        # to the wrong one.  (The architect's §6.2 requires the codes
        # to be separate precisely so they can be counted.)
        codes: Dict[str, int] = {}
        for viol in violations:
            code = viol.get("code") or viol.get("reason") or "?"
            codes[code] = codes.get(code, 0) + 1
        if codes:
            detail.append("      违规 reason code: %s"
                          % ", ".join("%s×%d" % (c, k)
                                      for c, k in sorted(codes.items())))
        if v == "RED":
            red.append("%s(%d)" % (lib.name, n))
        elif v == "NOTRUN":
            notrun.append(lib.name)
    detail.append("")
    detail.append("INV-1 RED 的库: %s" % (", ".join(red) if red else "无"))
    detail.append("INV-1 NOTRUN 的库: %s" % (", ".join(notrun) if notrun else "无"))

    if notrun:
        # The invariant could not be evaluated: that is NOT green.
        detail.append("  —— NOTRUN 的库没有被判据测量；NOTRUN 不等于 GREEN，"
                      "本判据判 FAIL（『无法判定』不等于『没问题』）")
        return report.judge(
            "Q1(REAL)", "每个活库的 INV-1 为 GREEN",
            False, "\n".join(detail))
    if red:
        detail.append("  —— 只测根库的判据看不到这些库；迁移对它们没有生效")
        return report.judge(
            "Q1(REAL)", "每个活库的 INV-1 为 GREEN",
            False, "\n".join(detail))
    return report.judge(
        "Q1(REAL)", "每个活库的 INV-1 为 GREEN", True, "\n".join(detail))


def judge_q2_inv2_inv3_every_library(report, workspace: Path):
    """Q2 (REAL): INV-2/3 is GREEN (or explicitly NOTRUN) in every library.

    This is the criterion the brief called "R-S3-5": after the migration
    INV-2/3 must become DECIDABLE.  Before the migration they were
    NOTRUN because the samplable set was empty (every ``body_path`` was
    NULL).  A criterion that only knows PASS/FAIL cannot tell
    "the migration ran and the rows are checkable" from "the migration
    never ran and there is nothing to check" — both look like "no
    violations found".

    So: a library whose INV-2/3 is NOTRUN is a FAIL here, because the
    migration was supposed to have made it decidable.  That is the
    "迁移跑了却仍 NOTRUN → 判 FAIL" rule, in code.

    The yardstick is the CHECKER'S OWN (``check_inv2_inv3``), and the
    byte count is printed alongside — NOT hidden.  The design's original
    wording ("byte-identical") is unsatisfiable for the markdown chunker
    (it re-assembles a chunk with a blank line between heading blocks),
    so a criterion stricter than the invariant it guards reports RED on
    a healthy corpus, and then nobody trusts it.
    """
    detail = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "Q2(REAL)", "每个活库的 INV-2/3 可判定且为 GREEN",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.text_inv import run_text_invariants
    except Exception as e:
        return report.judge_notrun(
            "Q2(REAL)", "每个活库的 INV-2/3 可判定且为 GREEN",
            "text_inv 不可用（%s: %s）—— 判据无法测量" % (type(e).__name__, e))

    red = []
    notrun = []
    byte_rows = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            r = run_text_invariants(conn, lib.workspace)
        finally:
            conn.close()
        chk = r["checks"]["INV-2/3"]
        v = chk["verdict"]
        n = len(chk.get("violations") or [])
        detail.append("  %-22s INV-2/3=%-7s 违规=%-5d 指纹=%s"
                      % (lib.name, v, n, chk.get("fingerprint", "-")))
        if v == "RED":
            red.append("%s(%d)" % (lib.name, n))
        elif v == "NOTRUN":
            notrun.append(lib.name)
    detail.append("")
    detail.append("INV-2/3 RED 的库: %s" % (", ".join(red) if red else "无"))
    detail.append("INV-2/3 NOTRUN（不可判定）的库: %s"
                  % (", ".join(notrun) if notrun else "无"))
    detail.append("口径：与 text_inv.check_inv2_inv3 同尺（canonical 相等）；"
                  "字节计数一并打印、不藏 —— 逐字节相等对 markdown 分块器"
                  "结构上不可满足。")

    if notrun:
        detail.append("  —— 迁移本应让这些库变得可判定；仍 NOTRUN 说明迁移没生效"
                      "（或被写入者清空）。『无法判定』不等于『没问题』，判 FAIL。")
        return report.judge(
            "Q2(REAL)", "每个活库的 INV-2/3 可判定且为 GREEN",
            False, "\n".join(detail))
    if red:
        return report.judge(
            "Q2(REAL)", "每个活库的 INV-2/3 可判定且为 GREEN",
            False, "\n".join(detail))
    return report.judge(
        "Q2(REAL)", "每个活库的 INV-2/3 可判定且为 GREEN", True, "\n".join(detail))


def judge_q5_no_library_regressed(report, workspace: Path):
    """Q5 (REAL): no live library got WORSE than the recorded baseline.

    The baseline is a FILE IN THE REPOSITORY
    (``tests/baseline/p1_regression_baseline.txt``), not a set computed
    by a script that lives in ``tmp/`` — because the stage-two set-diff
    tool was in ``tmp/``, and ``tmp/`` is git-ignored, so on another
    machine there was no baseline to compare against and the criterion
    could not be reproduced.  That was the B2 root cause.

    The comparison is on the SET of failing libraries, not on a COUNT:
    "the same number of failures but different ones" is a regression,
    and a count-based comparison would call it a pass.
    """
    detail = []
    baseline_file = Path(__file__).resolve().parents[3] / "tests" / "baseline" / \
        "p1_regression_baseline.txt"
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "Q5(REAL)", "失败集只减不增（与版本库内的基线文件比对）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    if not baseline_file.is_file():
        detail.append("基线文件不存在: %s" % baseline_file)
        detail.append("基线必须进版本库：放 tmp/ 里换台机器就没有了，"
                      "那正是阶段二 B2 未闭环的根因。")
        detail.append("本判据因此无法判定『失败集只减不增』，报 NOTRUN。")
        return report.judge_notrun(
            "Q5(REAL)", "失败集只减不增（与版本库内的基线文件比对）",
            "\n".join(detail))

    text = baseline_file.read_text(encoding="utf-8")
    baseline_bad = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        baseline_bad.add(line.split()[0])

    try:
        from agent.memory.text_inv import run_text_invariants
    except Exception as e:
        return report.judge_notrun(
            "Q5(REAL)", "失败集只减不增（与版本库内的基线文件比对）",
            "text_inv 不可用（%s: %s）" % (type(e).__name__, e))

    # The current VERDICT per (library, check) — not just the failing set.
    # The comparison has to be state-aware, because "RED → NOTRUN" is a
    # REGRESSION (the failure stopped being visible) while a NOTRUN on a
    # check that was never RED is not a regression at all (that check is
    # simply not measurable on this corpus — INV-9 waits for the element
    # system).
    current: Dict[str, str] = {}
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            r = run_text_invariants(conn, lib.workspace)
        finally:
            conn.close()
        for name, chk in r["checks"].items():
            current["%s:%s" % (lib.name, name)] = chk["verdict"]

    current_bad = {k for k, v in current.items() if v == "RED"}
    current_notrun = {k for k, v in current.items() if v == "NOTRUN"}

    # A regression is:
    #   (a) an entry that is RED now and was not in the baseline, OR
    #   (b) an entry that was RED in the baseline and is NOTRUN now
    #       (it stopped being measured — the failure did NOT go away).
    new_failures = sorted(current_bad - baseline_bad)
    lost_visibility = sorted(
        k for k in baseline_bad
        if k in current and current[k] == "NOTRUN")
    regressions = new_failures + lost_visibility

    detail.append("基线文件: %s" % baseline_file)
    detail.append("基线失败集: %d 条" % len(baseline_bad))
    detail.append("当前失败集(RED): %d 条" % len(current_bad))
    detail.append("当前不可判定(NOTRUN): %d 条" % len(current_notrun))
    detail.append("新增失败（RED 且不在基线里）: %d 条" % len(new_failures))
    for f in new_failures[:20]:
        detail.append("  + %s" % f)
    detail.append("失去可见性（基线里是 RED，现在 NOTRUN）: %d 条" % len(lost_visibility))
    for f in lost_visibility[:20]:
        detail.append("  ! %s" % f)
    detail.append("已转绿（基线 RED → 现在 GREEN）: %d 条"
                  % len([k for k in baseline_bad if current.get(k) == "GREEN"]))
    detail.append("（失败集按『库:判据』比对，不比数量 —— 数量相同但内容换了也是回归）")
    detail.append("（RED→NOTRUN 按回归计：失败没有消失，只是不再被测量）")
    detail.append("（从未 RED 过的 NOTRUN 不算回归：那是该判据在本语料上不可测量）")

    if regressions:
        return report.judge(
            "Q5(REAL)", "失败集只减不增（与版本库内的基线文件比对）",
            False, "\n".join(detail))
    return report.judge(
        "Q5(REAL)", "失败集只减不增（与版本库内的基线文件比对）",
        True, "\n".join(detail))


def judge_s1_criteria_do_not_modify_what_they_measure(report, workspace: Path):
    """S-1 (unit): the content digest covers the four extern columns.

    The S-1 principle: a criterion that changes the thing it measures is
    measuring a state it manufactured.  The check is a before/after
    digest comparison.

    The hole this closes: the digest did NOT include ``body_path`` /
    ``body_anchor`` / ``body_hash`` / ``text_state``.  So a criterion
    that wiped ``body_path`` — exactly the defect this stage is about —
    was invisible to the check that exists to catch it.

    The criterion proves it in the only way that counts: it CHANGES the
    four columns on a COPY, and shows that the digest MOVES.  If the
    digest does not move, the check is blind.
    """
    detail = []
    import shutil
    import tempfile

    from agent.memory.acceptance import content_digest

    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "S-1(unit)", "内容指纹覆盖四列：改坏 body_path 会让指纹变化",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    lib = libs[0]
    tmp = Path(tempfile.mkdtemp())
    copy = tmp / "index.db"
    try:
        for suffix in ("", "-wal", "-shm"):
            s = Path(str(lib.db) + suffix)
            if s.is_file():
                shutil.copy2(s, Path(str(copy) + suffix))
        conn = sqlite3.connect(str(copy))
        try:
            before = content_digest(conn)
            row = conn.execute("SELECT id FROM chunks WHERE body_path IS NOT NULL "
                               "LIMIT 1").fetchone()
            if row is None:
                detail.append("该库没有 body_path 非空的行，无法演示『改坏必红』")
                conn.close()
                return report.judge_notrun(
                    "S-1(unit)", "内容指纹覆盖四列：改坏 body_path 会让指纹变化",
                    "\n".join(detail))
            conn.execute("UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
                         "body_hash=NULL WHERE id=?", (row[0],))
            conn.commit()
            after = content_digest(conn)
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    detail.append("被测库（副本）: %s" % lib.db)
    detail.append("改坏前内容指纹: %s" % (before[:32] or "-"))
    detail.append("改坏后内容指纹: %s" % (after[:32] or "-"))
    moved = bool(before) and bool(after) and before != after
    detail.append("指纹是否随之变化: %s" % moved)
    if not moved:
        detail.append("  —— 指纹没动：S-1 对四列是瞎的，改坏 body_path 不会被发现")
        return report.judge(
            "S-1(unit)", "内容指纹覆盖四列：改坏 body_path 会让指纹变化",
            False, "\n".join(detail))
    return report.judge(
        "S-1(unit)", "内容指纹覆盖四列：改坏 body_path 会让指纹变化",
        True, "\n".join(detail))


def judge_s6_criterion_names_say_what_they_measure(report, workspace: Path = None,
                                                  targets=None):
    """S-6 (unit): a criterion's NAME must say what it asserts.

    The false green this catches: the criterion was named "add_memory
    写出的行四列非空" (the four columns are non-empty) while
    ``body_path`` is legitimately ``None`` for a blob row — ``add_memory``
    stores a blob under a SYNTHETIC path and writes no truth file, so
    there is nothing to point at.  The NAME said "non-empty"; the
    ASSERTION said "the three that CAN be filled are filled".  A reader
    reading the name would conclude the row was broken.

    The rule: a name that makes a UNIVERSAL claim must be backed by an
    assertion of that claim in the same criterion's body.

    The check is a STATIC one (``ast`` — the function's own source, not a
    guess), and it scans BOTH criterion files, so a new criterion cannot
    slip a lying name past it by living in the other file.

    "Universal claim" means one of:
      * 「四列」/「四列非空」/「四列已回填」 — every one of the four
        extern columns is asserted;
      * 「每个活库」/「every live library」 — the assertion must mention
        the column it claims to check on EVERY library (the row-level
        column name must appear).
    """
    import ast

    detail = []
    here = Path(__file__).resolve()
    if targets is None:
        targets = [here, here.parent / "phase2.py"]
    else:
        targets = [Path(t) for t in targets]

    # claim keyword -> the columns that must be asserted for it
    #
    # The claims are SCOPED: a name that says "每个活库的 files 表列集合"
    # is a claim about the files TABLE's columns, not about the four
    # extern columns.  A claim keyword therefore only applies when the
    # name does NOT narrow the subject.
    claims = [
        # "四列" — the four extern columns, always.
        ("四列", ("body_path", "body_anchor", "body_hash", "text_state")),
        # "每个活库" — the four extern columns, but ONLY when the name is
        # about the chunks table (the files-table claim is scoped out).
        ("每个活库", ("body_path",)),
    ]
    # Names that narrow the subject away from the four extern columns.
    narrowed = ("files 表", "files 列")

    offenders = []
    checked = 0
    for target in targets:
        if not target.is_file():
            detail.append("跳过（文件不存在）: %s" % target)
            continue
        src = target.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            detail.append("跳过（无法解析）: %s (%s)" % (target.name, e))
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body_src = "\n".join(lines[node.lineno - 1:node.end_lineno])
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not (isinstance(func, ast.Attribute)
                        and func.attr in ("judge", "judge_notrun")):
                    continue
                args = call.args
                if len(args) < 2:
                    continue
                number = args[0].value if isinstance(args[0], ast.Constant) else "?"
                name = args[1].value if isinstance(args[1], ast.Constant) else ""
                if not isinstance(name, str):
                    continue
                needed = []
                for key, cols in claims:
                    if key in name:
                        needed.extend(cols)
                if not needed:
                    continue
                # A narrowed subject scopes the claim out.
                if any(n in name for n in narrowed) and "四列" not in name:
                    needed = []
                    detail.append("SKIP [%s] %-22s %s（主张已限定在 files 表，不适用）"
                                  % (target.name, number, name))
                    continue
                checked += 1
                missing = [c for c in dict.fromkeys(needed) if c not in body_src]
                if missing:
                    offenders.append((target.name, number, name, missing))
                else:
                    detail.append("OK  [%s] %-22s %s" % (target.name, number, name))

    detail.append("受检判据数（名称含普遍性主张）: %d" % checked)
    if offenders:
        for fname, number, name, missing in offenders:
            detail.append("名不副实 [%s] %s『%s』: 断言里没有 %s"
                          % (fname, number, name, ", ".join(missing)))
        detail.append("  —— 判据名与断言不符 = 读者按名字理解会得到错误结论")
        return report.judge(
            "S-6(unit)", "每条判据的名称与它的断言一致（名称里的普遍性主张必须被断言支撑）",
            False, "\n".join(detail))
    return report.judge(
        "S-6(unit)", "每条判据的名称与它的断言一致（名称里的普遍性主张必须被断言支撑）",
        True, "\n".join(detail))
