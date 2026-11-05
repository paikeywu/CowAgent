#!/usr/bin/env python3
# encoding:utf-8
"""S3-WB · the ``messages`` WRITE-BOUNDARY criterion (WB-1).

**Why a new group and not another member of S3-M.**  The messages
group ``msg_inv`` is a CONTRACT: the acceptance layer compares its
check names against a fixed set of SEVEN (``MSG_INV_NAMES`` in
``phase2_s3m.py``) and reports "缺项" / "多出的判据名" when the set
does not match.  Its VERDICT is the roll-up of its members.  So an
eighth member would (a) break the name-set contract and (b) flip the
group's verdict to NOTRUN on every library whose recorder is not
installed yet — which would turn M-1 and M-2 red for a reason that has
nothing to do with them.

⇒ The eighth criterion is a group of its own, and it reads the
implementation's own published verdict (``msg_inv``'s
``write_boundary`` key) — the same "one mouth for one question" rule
the rest of the acceptance layer follows.

**What it judges.**  The old desktop build writes ``messages`` rows
with a seven-column ``INSERT`` that does not name the externalisation
columns, so the row stays ``content_state='inline'`` with three empty
pointer columns — **forever**.  The new build writes the same shape and
externalises it within the request.  On the FINAL row the two writers
are **indistinguishable** (measured 2026-11-05: all eleven columns
identical) ⇒ a gate that looks at the ROW cannot tell them apart, and
a gate that REFUSES the row breaks the product.

⇒ So the boundary is a **RECORDER** (an AFTER INSERT trigger that
writes down what it saw, and never refuses), and the question is
"is the row still inline?".  The new writer's row is external by now;
the old writer's row is not.  That is a fact about the row's HISTORY,
not its shape.

**Two proofs, because either alone is insufficient.**

  * **static** — the recorder's trigger is really installed by the
    write path (parsed with ``ast``, so a mention in a comment does
    not count) and it never RAISEs (a refusing trigger would take the
    conversation down);
  * **behavioural** — on a THROWAWAY fixture: a row written in the
    OLD shape and a row written in the NEW shape are both recorded,
    and the patrol names ONLY the old one.  The live corpus is never
    written to by a criterion.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory.acceptance import Report

#: The age ceiling: a recorded row that is still inline after this many
#: seconds was NOT an in-flight externalisation — the new writer
#: externalises within the request.  Configurable through the
#: environment so a deployment can choose, and the fingerprint says
#: which value was used.
WB1_MAX_AGE_SECONDS = 300


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def wb1_max_age_seconds() -> int:
    return _env_int("COW_WB1_MAX_AGE_SECONDS", WB1_MAX_AGE_SECONDS)


def _module_source(mod) -> Optional[str]:
    path = getattr(mod, "__file__", None)
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _calls_named(source: str, attr: str) -> bool:
    """Does the source really CALL ``attr``?  Parsed with ``ast`` — a
    mention in a comment or a docstring does not count."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == attr:
            return True
        if isinstance(func, ast.Name) and func.id == attr:
            return True
    return False


def _msg_inv_module():
    from agent.memory import msg_inv
    return msg_inv


def judge_wb1_write_boundary_has_no_unowned_rows(report: Report,
                                                  workspace: Path):
    """WB-1 (unit+REAL): the messages write boundary names its unowned
    rows — and never breaks the product to do it.

    Three proofs:

      ① **static** — the write path really installs the recorder (AST),
         and the recorder's trigger never RAISEs (a trigger that
         refuses would take the conversation down for a policy
         violation);
      ② **behavioural** — on a throwaway fixture, the OLD writer's
         shape and the NEW writer's shape are both recorded, and the
         patrol names ONLY the old one.  This is the proof that the
         criterion is not a tautology;
      ③ **REAL** — every live library's own published verdict
         (``msg_inv``'s ``write_boundary``), so the criterion reads the
         implementation's mouth instead of re-deriving the question.
    """
    detail: List[str] = []
    ok = True
    red: List[str] = []
    notrun: List[str] = []
    measured: List[str] = []

    max_age = wb1_max_age_seconds()
    detail.append("超龄阈值: %d 秒（环境变量 COW_WB1_MAX_AGE_SECONDS）" % max_age)

    # ---- ① the static proof -----------------------------------------
    try:
        from agent.memory import conversation_store as cs_mod
        from agent.memory import msg_write_guard as guard_mod
    except Exception as e:
        return report.judge_notrun(
            "WB-1(REAL)",
            "WB-1 messages 写入边界：记录器在位、不拦写入，且巡检只点名旧写者的行",
            "模块不可用（%s: %s）" % (type(e).__name__, e))

    cs_src = _module_source(cs_mod)
    installs = bool(cs_src) and _calls_named(cs_src, "_ensure_write_watch")
    detail.append("静态证据: %s" % getattr(cs_mod, "__file__", "?"))
    detail.append("  写入口调用了 _ensure_write_watch（AST 解析）: %s" % installs)
    if not installs:
        detail.append("  —— 写入口没有装记录器：接线不在（旧写者的行不会被看见）")
        ok = False

    # The trigger must NEVER refuse.  A trigger that raises aborts the
    # insert — that is the shape that breaks the product (measured
    # 2026-11-05: the "refuse inline" candidate aborted the new
    # writer's own insert).
    guard_src = _module_source(guard_mod) or ""
    raises = "RAISE(" in guard_src
    detail.append("  记录器里含 RAISE(（会中止写入）: %s" % raises)
    if raises:
        detail.append("  —— 记录器会中止写入：那不是记录器，是会砸掉产品的新写路径的闸门")
        ok = False

    # ---- ② the behavioural proof (throwaway fixture) ----------------
    behaviour_ok = False
    behaviour_detail: List[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="s3wb-wb1-"))
    try:
        ws = tmp / "ws"
        (ws / "memory" / "long-term").mkdir(parents=True)
        db_path = ws / "memory" / "long-term" / "index.db"
        from agent.memory.conversation_store import ConversationStore
        from agent.memory import msg_write_guard as guard_mod

        store = ConversationStore(db_path)
        try:
            # **The NEW writer, through the REAL write path.**  This is
            # what installs the recorder (the write path is where the
            # recorder is installed — 交办书 I-2), and it is what
            # externalises the row afterwards.  So one call gives us
            # BOTH writers' rows: the new writer's through the product,
            # the old writer's through a raw seven-column INSERT.
            ok_write = store.append_messages(
                "wb1-new",
                [{"role": "user", "content": "wb1 new writer message"}],
                channel_type="test")
            behaviour_detail.append("真实写入口写入返回: %s" % ok_write)

            conn = sqlite3.connect(str(db_path))
            try:
                installed = guard_mod.is_installed(conn)
                behaviour_detail.append("写完之后记录器已安装: %s" % installed)
                if not installed:
                    behaviour_detail.append("! 写完之后记录器仍未安装：接线不在")
                else:
                    # The OLD writer's shape: a seven-column INSERT that
                    # does not name the externalisation columns — so the
                    # row keeps ``content_state='inline'`` and three
                    # empty pointer columns, FOREVER.
                    conn.execute(
                        "INSERT INTO messages (agent_id, session_id, seq, "
                        "role, content, created_at, run_id) "
                        "VALUES (?,?,?,?,?,?,?)",
                        ("", "wb1-legacy", 1, "user",
                         '[{"type": "text", "text": "legacy"}]', 1700000000, ""))
                    conn.commit()

                    watched = conn.execute(
                        "SELECT COUNT(*) FROM messages_write_watch").fetchone()[0]
                    named = guard_mod.patrol(conn, older_than=0)
                    legacy_named = [r for r in named.get("rows", [])
                                    if r.get("session_id") == "wb1-legacy"]
                    new_named = [r for r in named.get("rows", [])
                                 if r.get("session_id") == "wb1-new"]
                    # the new writer's row must be external by now
                    new_state = conn.execute(
                        "SELECT content_state FROM messages "
                        "WHERE session_id='wb1-new'").fetchone()
                    behaviour_detail.append(
                        "记录器看见的行数: %d（两个写者各 1 行 ⇒ 应为 2）" % watched)
                    behaviour_detail.append(
                        "新写者的行现在 content_state=%r（外置后 ⇒ external）"
                        % (new_state[0] if new_state else None))
                    behaviour_detail.append(
                        "巡检点名的行: legacy=%d new=%d"
                        % (len(legacy_named), len(new_named)))
                    behaviour_ok = (watched >= 2 and len(legacy_named) == 1
                                   and len(new_named) == 0)
                    behaviour_detail.append(
                        "结论: 巡检只点名旧写者、新写者不留名 ⇒ %s"
                        % behaviour_ok)
            finally:
                conn.close()
        finally:
            pass
    except Exception as e:
        behaviour_detail.append("行为证据失败: %s: %s" % (type(e).__name__, e))
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    for line in behaviour_detail:
        detail.append("  " + line)
    if not behaviour_ok:
        ok = False

    # ---- ③ the REAL proof: every live library's own verdict --------
    try:
        from agent.memory.migrate import enumerate_live
        msg_inv = _msg_inv_module()
    except Exception as e:
        return report.judge_notrun(
            "WB-1(REAL)",
            "WB-1 messages 写入边界：记录器在位、不拦写入，且巡检只点名旧写者的行",
            "判据库不可用（%s: %s）" % (type(e).__name__, e))

    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "WB-1(REAL)",
            "WB-1 messages 写入边界：记录器在位、不拦写入，且巡检只点名旧写者的行",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    for lib in libs:
        # ``enumerate_live`` returns DICTS (name / db / workspace), not
        # objects — reading ``lib.name`` would die with
        # "'dict' object has no attribute 'name'".
        lib_name = lib["name"] if isinstance(lib, dict) else lib.name
        lib_db = lib["db"] if isinstance(lib, dict) else lib.db
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % str(lib_db).replace("\\", "/"),
                                   uri=True)
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "messages" not in tables:
                    detail.append("  %-22s 没有 messages 表 —— 不适用（N-7）" % lib_name)
                    continue
                res = msg_inv.check_msg_inv8(conn)
            finally:
                conn.close()
        except Exception as e:
            detail.append("  %-22s 判据执行失败: %s: %s"
                          % (lib_name, type(e).__name__, e))
            notrun.append(lib_name)
            continue

        verdict = res.get("verdict")
        detail.append("  %-22s %s   %s"
                      % (lib_name, verdict, res.get("fingerprint")))
        for v in res.get("violations", [])[:5]:
            detail.append("      ! %s" % (v.get("detail") or v.get("reason")))
        if verdict == "RED":
            ok = False
            red.append(lib_name)
        elif verdict == "NOTRUN":
            notrun.append(lib_name)
        else:
            measured.append(lib_name)

    detail.append("")
    detail.append("实测绿=%d 红=%d NOTRUN=%d（没有 messages 表的库跳过）"
                  % (len(measured), len(red), len(notrun)))
    detail.append("NOTRUN 的库 = 记录器未安装 ⇒ 那一侧『没人看着』（不是『没问题』）")
    detail.append("为什么这条必须存在：MSG-INV-1..7 与 I-1..I-4 都看不见")
    detail.append("  『这行是谁写的』—— 它们问的是链、计数、外置是否完整；")
    detail.append("  旧写者的行在**最终形状**上与新写者完全相同 ⇒ 只有")
    detail.append("  『记录器看见了它、而它至今还是 inline』这条事实能把它点出来。")

    if not measured and not red:
        # Nothing was measured anywhere: that is NOTRUN, never a pass.
        return report.judge_notrun(
            "WB-1(REAL)",
            "WB-1 messages 写入边界：记录器在位、不拦写入，且巡检只点名旧写者的行",
            "\n".join(detail))

    return report.judge(
        "WB-1(REAL)",
        "WB-1 messages 写入边界：记录器在位、不拦写入，且巡检只点名旧写者的行",
        ok, "\n".join(detail))
