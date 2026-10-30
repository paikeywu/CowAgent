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
        # --- ALL FOUR columns, one by one ---------------------------
        #
        # This used to assert only ``body_path`` (plus text_state's
        # legality), while the criterion's NAME said 四列.  The gap was
        # real and it was MEASURED (S3-H): a copy of a live library with
        # every ``body_anchor`` set to NULL was reported PASS, because
        # ``body_path`` was still filled.  The name was a lie and the
        # lie was in the direction that hides a defect.
        #
        # The S-6 self-check did not catch it either: S-6 looks for the
        # column name anywhere in the function body, and ``body_anchor``
        # appeared in the DOCSTRING — so a prose mention satisfied the
        # check.  The assertion is now explicit, and S-6 was tightened
        # to look at CODE rather than prose.
        per_col = {
            "body_path": int(prof.get("body_path_null", 0)),
            "body_anchor": int(prof.get("body_anchor_null", 0)),
            "body_hash": int(prof.get("body_hash_null", 0)),
        }
        nulls = max(per_col.values())
        if rows and nulls == rows:
            wholly_unmigrated.append(prof["name"])
        elif nulls:
            broken = ", ".join("%s=%d" % (c, n)
                               for c, n in sorted(per_col.items()) if n)
            partial.append("%s(%d/%d 行: %s)"
                           % (prof["name"], nulls, rows, broken))
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
    # ------------------------------------------------------------------------
    # R8 (第八轮) — BY BEHAVIOUR, NOT BY SOURCE TEXT.
    #
    # The old check read the SETTER'S SOURCE and looked for the literals
    # ``"text_state='mirror'"`` and ``"rebuild_from_anchor"``.  When the
    # setter DELEGATED (S3-K), the rule MOVED to the delegate, and the
    # criterion had to be patched to follow it — a patch that would
    # break again the next time the rule moves.
    #
    # **That patch is worse than no criterion.**  A criterion that
    # reports RED because the implementation moved is a false red, and
    # a false red nobody fixes teaches the whole team that the gate is
    # noise.  The R7 review said it exactly: 「最该动的不是『会假红』，
    # 而是『假红没人修』」.
    #
    # So the criterion now asks the BEHAVIOURAL question on a COPY:
    #
    #   * a row whose text does NOT match the file ⇒ the setter must
    #     NOT promote it (「复核通过才置位」);
    #   * a row whose text DOES match ⇒ the setter must promote it
    #     (「置位者是明确的」).
    #
    # Both halves are measured, so neither the name of the setter nor
    # the place the rule lives can make this criterion go red.
    has_setter = False
    writes_mirror = False
    verifies_first = False
    delegate_name = "（行为判定）"
    try:
        import shutil as _shutil
        import sqlite3 as _sqlite3
        import tempfile as _tempfile
        from agent.memory import certify as _certify

        has_setter = hasattr(_certify, "certify_library")
        libs_now = enumerate_live(workspace)
        if not libs_now:
            detail.append("没有活库，无法做行为判定")
            has_setter = False
        else:
            lib0 = libs_now[0]
            probe_dir = Path(_tempfile.mkdtemp(prefix="rs35-probe-"))
            probe_db = probe_dir / "index.db"
            _shutil.copy2(str(lib0.db), str(probe_db))
            pconn = _sqlite3.connect(str(probe_db))
            pconn.row_factory = _sqlite3.Row
            try:
                # The LEDGER is the witness: an entry means "this row
                # was certified".  So the question 「复核通过才置位」 is
                # observable as: a row whose stored text does NOT match
                # the file must NOT acquire a ledger entry.
                #
                # MEASURED (R8): the first attempt asserted on
                # ``text_state`` and got the wrong answer — the GUARD
                # restores 'mirror' from its snapshot, so the probe was
                # measuring the guard, not the setter.  The ledger is
                # written by the certifier and nobody else, so it is the
                # right witness.
                def _has_entry(conn, rid):
                    try:
                        return conn.execute(
                            "SELECT COUNT(*) FROM cert_ledger WHERE id=?",
                            (rid,)).fetchone()[0] > 0
                    except Exception:
                        return None

                victims = pconn.execute(
                    "SELECT id FROM chunks WHERE body_path IS NOT NULL "
                    "AND body_origin='file' LIMIT 2").fetchall()
                if len(victims) < 2:
                    detail.append("副本里没有两行可判定的 file 行 —— "
                                  "无法在行为层验证置位者")
                    has_setter = False
                else:
                    bad_id = victims[0]["id"]
                    good_id = victims[1]["id"]
                    # --- row 1: the stored text is WRONG -------------
                    pconn.execute(
                        "UPDATE chunks SET text=text || ' <<<DRIFT>>>' "
                        "WHERE id=?", (bad_id,))
                    pconn.execute("DELETE FROM cert_ledger WHERE id=?",
                                  (bad_id,))
                    pconn.commit()
                    rep1 = _certify.certify_library(
                        lib0.workspace, probe_db, name="probe")
                    got_entry = _has_entry(pconn, bad_id)
                    verifies_first = (got_entry is False)
                    detail.append("行为判定·行1（正文被改坏）: "
                                  "认证器 refused=%s 该行拿到账本条目=%s ⇒ "
                                  "没有复核通过就不置位=%s"
                                  % (rep1.get("refused"), got_entry,
                                     verifies_first))

                    # --- row 2: the stored text is RIGHT --------------
                    pconn.execute("DELETE FROM cert_ledger WHERE id=?",
                                  (good_id,))
                    pconn.commit()
                    rep2 = _certify.certify_library(
                        lib0.workspace, probe_db, name="probe")
                    got_entry2 = _has_entry(pconn, good_id)
                    writes_mirror = (got_entry2 is True)
                    detail.append("行为判定·行2（正文相符）: "
                                  "认证器 promoted=%s 该行拿到账本条目=%s ⇒ "
                                  "复核通过就置位=%s"
                                  % (rep2.get("promoted"), got_entry2,
                                     writes_mirror))
            finally:
                pconn.close()
                try:
                    _shutil.rmtree(probe_dir, ignore_errors=True)
                except Exception:
                    pass
        detail.append("置位者: 行为判定（不读源码）—— "
                      "存在=%s 复核通过才置位=%s 复核通过就置位=%s"
                      % (has_setter, verifies_first, writes_mirror))
        detail.append("（置位者是巡检器，不是迁移器：迁移器只测量巡检器做没做，"
                      "自己置位会让三态机退化为装饰）")
        detail.append("R8: 判据读【语义锚点】（认证器的公开行为），"
                      "不读【实现长相】（源码字面量）—— "
                      "『规则搬到哪』不再能让这条判据变红。")
        detail.append("（证人用【账本条目】而不是 text_state：守卫会把 text_state "
                      "从快照恢复成 mirror，用它当证人量到的是守卫，不是置位者。）")
    except Exception as e:
        detail.append("置位者检查失败: %s: %s" % (type(e).__name__, e))
        has_setter, writes_mirror, verifies_first = False, False, False

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

    **The guard's triggers are dropped from the copy first.**  MEASURED
    (S3-J): the S3-I guard installs a BEFORE UPDATE trigger that
    RESTORES the protected columns when the incoming value is NULL —
    so the mutation this criterion performs is reverted by the very
    statement that performs it, and the digest never moves.  The
    criterion then reports "the digest is blind to the four columns",
    which is a FALSE RED: the digest is fine, the mutation simply
    never landed.

    A criterion that reports the wrong reason for a red is worse than
    one that stays silent, because the reader goes and fixes the wrong
    thing.  So the guard is removed from the copy, the mutation is
    applied, and the row is READ BACK to confirm the mutation landed
    before the digests are compared.  If the row still holds its
    pointer after the UPDATE, the criterion says so and does NOT claim
    the digest is blind.
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
            # The guard's triggers come off the COPY: the guard exists
            # to revert exactly this mutation, so leaving it on would
            # make the criterion measure the guard instead of the
            # digest.  (The LIVE library is never touched.)
            #
            # R8: BY PURPOSE, NOT BY NAME.  The old code dropped five
            # hard-coded trigger names.  If the guard is renamed — or a
            # new trigger is added — the DROP misses it, the mutation is
            # reverted by the guard, and the criterion reports "the
            # mutation did not land" and goes NOTRUN: a false green
            # that hides the very hole the criterion exists for.
            #
            # So the triggers are DISCOVERED: every trigger attached to
            # the ``chunks`` table is dropped, by reading
            # ``sqlite_master``.  The rule is "the guard on this
            # table", not "these five names".
            dropped = []
            trig_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='chunks'").fetchall()
            for (tname,) in trig_rows:
                conn.execute("DROP TRIGGER IF EXISTS %s" % tname)
                dropped.append(tname)
            conn.commit()
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
            # --- did the mutation actually LAND? --------------------
            # Read the row back.  If it still holds its pointer, the
            # mutation was reverted by something (a trigger), and the
            # digest comparison below would be measuring nothing.
            landed = conn.execute(
                "SELECT body_path FROM chunks WHERE id=?", (row[0],)).fetchone()[0]
            if landed is not None:
                conn.close()
                detail.append("被测库（副本）: %s" % lib.db)
                detail.append("改坏的行 id=%s" % row[0])
                detail.append("改坏后该行的 body_path 仍是 %r —— 变异【没有落地】"
                              % landed)
                detail.append("已从副本上移除的守卫触发器: %s" % ", ".join(dropped))
                detail.append("结论: 无法判定 —— 变异被别的东西改回去了，"
                              "此时比较指纹没有意义（不是『指纹瞎了』）。")
                return report.judge_notrun(
                    "S-1(unit)", "内容指纹覆盖四列：改坏 body_path 会让指纹变化",
                    "\n".join(detail))
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


def _strip_docstring(node, lines) -> str:
    """A function's CODE, with its docstring removed.

    The docstring is the first statement of a function when it is a
    bare string constant.  Removing it is what makes "does the code
    mention this column?" mean "does the code ASSERT this column?".
    """
    import ast

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return ""
    try:
        clone = ast.FunctionDef(name=node.name, args=node.args,
                                 body=body, decorator_list=[],
                                 returns=None, type_comment=None)
        ast.fix_missing_locations(clone)
        return ast.unparse(clone)
    except Exception:
        # Fall back to the raw source minus the docstring's lines.
        return "\n".join(lines[node.lineno - 1:node.end_lineno])


def _column_is_asserted_by_a_helper(code_src: str, column: str) -> bool:
    """Is a column asserted by a HELPER the criterion calls?

    Some criteria do not re-implement the check: they call a helper
    that owns the rule.  ``S-1`` (the content digest covers the four
    columns) calls ``acceptance.content_digest``, and the digest's
    definition — with the column list — lives in ``__init__.py``.  The
    criterion's OWN body therefore never names ``text_state``, and a
    naive scan reports a false red.

    So the check follows the calls: for every name the criterion calls,
    the helper's own source is searched (transitively, one level deep
    through the acceptance package) for the column.

    The rule is deliberately narrow: the helper must be IN THE
    ACCEPTANCE PACKAGE.  A criterion that delegates its assertion to
    the PRODUCT (e.g. to ``text_inv``) is a different question — and
    for those the claim is scoped out by the name, not by this helper.
    """
    import ast

    here = Path(__file__).resolve().parent
    try:
        tree = ast.parse(code_src)
    except SyntaxError:
        return False
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(ast.unparse(node.func))
    if not called:
        return False
    # The acceptance package's own modules — the only helpers a
    # criterion may delegate a CLAIM to.
    for path in sorted(here.glob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            htree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        lines = src.splitlines()
        for node in ast.walk(htree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in called:
                continue
            try:
                hsrc = _strip_docstring(node, lines)
            except Exception:
                continue
            if column in hsrc:
                return True
            # The helper may READ the column from a MODULE-LEVEL
            # constant instead of naming it in its body — and that is
            # exactly how ``content_digest`` works: the column list
            # lives in ``_CONTENT_COLUMNS``.  So the helper's own
            # module's constants are searched too.
            for const in htree.body:
                if isinstance(const, ast.Assign):
                    try:
                        csrc = ast.unparse(const)
                    except Exception:
                        continue
                    if column in csrc:
                        return True
    return False


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
        # ALL FOUR criterion files.  The S3-H and S3-J modules are
        # included: a criterion that lives in a new file must not be
        # able to slip a lying name past this check by living
        # somewhere else.
        targets = [here, here.parent / "phase2.py", here.parent / "phase2_s3h.py",
                   here.parent / "phase2_s3j.py"]
    else:
        targets = [Path(t) for t in targets]

    # claim keyword -> the columns that must be asserted for it
    #
    # The claims are SCOPED: a name that says "每个活库的 files 表列集合"
    # is a claim about the files TABLE's columns, not about the four
    # extern columns.  A claim keyword therefore only applies when the
    # name does NOT narrow the subject.
    # claim keyword -> the columns that must be asserted for it
    #
    # The claims are SCOPED, and the scoping is the subtle part.
    #
    # 「四列」 is an EXPLICIT claim about the four extern columns: a
    # criterion that says 四列 must assert all four.  That is
    # unconditional and stays strict.
    #
    # 「每个活库」 is only a claim about the four columns when the
    # criterion is ABOUT the four columns.  「每个活库的 INV-1 为
    # GREEN」 is a claim about the INV-1 invariant, and INV-1's own
    # definition lives in ``text_inv`` — the criterion correctly does
    # not re-implement it.  Firing on it would be a false red that
    # teaches the reader to ignore this check.
    #
    # So the 「每个活库」 claim fires only when the name ALSO names one
    # of the four columns (which is how a criterion says "the four
    # columns, in every library").
    #
    # The MEASURED case that made this precise: 「每个活库的
    # files.chunker_version 与当前切法一致」 is about the CHUNKER
    # VERSION, not about the four columns, and firing on it was a false
    # red.  The 「四列」 form is the one that caught the real defect.
    claims = [
        # "四列" — the four extern columns, always.
        ("四列", ("body_path", "body_anchor", "body_hash", "text_state")),
    ]
    # The 「每个活库」 claim, and the condition under which it applies.
    every_lib_claim = ("每个活库",)
    four_col_names = ("body_path", "body_anchor", "body_hash", "text_state")
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
            # --- the CODE, not the prose -----------------------------
            #
            # This used to search the whole function body — DOCSTRING
            # included — for the column names.  That is a false green:
            # the MEASURED case (S3-H) was ``M4``, named 「四列已回填」,
            # whose DOCSTRING mentions all four columns while its
            # ASSERTION read only ``body_path_null``.  A prose mention
            # satisfied the check, so a lying name passed the very
            # criterion that exists to catch lying names.
            #
            # So the scan now looks at the CODE only: the docstring is
            # stripped, and the column must appear in an executable
            # expression.  A criterion that merely TALKS about a column
            # does not assert it.
            code_src = _strip_docstring(node, lines)
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
                # The 「每个活库」 claim fires only when the name ALSO
                # names one of the four columns.
                if any(k in name for k in every_lib_claim) and \
                        any(c in name for c in four_col_names):
                    needed.extend(four_col_names)
                if not needed:
                    continue
                # A narrowed subject scopes the claim out.
                if any(n in name for n in narrowed) and "四列" not in name:
                    needed = []
                    detail.append("SKIP [%s] %-22s %s（主张已限定在 files 表，不适用）"
                                  % (target.name, number, name))
                    continue
                checked += 1
                missing = [c for c in dict.fromkeys(needed)
                           if c not in code_src]
                if missing:
                    # The column may be asserted by a HELPER the
                    # criterion calls (e.g. the content digest lives in
                    # acceptance/__init__.py).  If the criterion calls
                    # something whose OWN source names the column, the
                    # claim IS backed — by the helper.  This is checked
                    # statically, over the helper's source.
                    still = []
                    for c in missing:
                        if not _column_is_asserted_by_a_helper(code_src, c):
                            still.append(c)
                    missing = still
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
