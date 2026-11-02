#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-AA · subagent 预算与超时产出丢失（L2 判据）。

L2 交办书 · 小种子（SEED）· 2026-11-02 · 业务集成测试工程师
=====================================================================

**Why this module exists.**  主 Agent 用 ``subagent`` 工具交办任务时连续 9 次
撞 300 秒预算超时。根因在 ``agent/subagent/runner.py``：一次 spawn 内**所有
task 共用同一个 deadline**（spawn 被调用的那一刻起算），于是一个 task 跑超
了预算，其余 task 一起遭殃：

* 排在慢 task 后面、**还没开始跑**的 task 被直接 ``cancel()`` —— 工作没做，
  结果也没有；
* 已经跑完、结果还没被收集的 task，它的产出可能被 timeout 标记覆盖。

**The four criteria**

=========================================  ==========================
criterion                                  guards
=========================================  ==========================
J1 完成的产出不丢（排队臂 + 竞态臂）       共享 deadline / 排队被取消
J2 完成的产出不被超时态污染（工具路径）     笼统的 "spawn failed"
J3 配置生效可观测（预算真的被读到了）       配置被静默改写
J4 判据自身不失真（对抗样本阴性对照）       判据恒真 / 恒假
=========================================  ==========================

**Why the assertions are BEHAVIOURAL, never about the implementation.**

判据不能断言「代码里有没有 per-task deadline 变量」—— 那只对一种实现成立。
因此这里断言的全是**外部可观察行为**：返回给上层的每一条 task 结果，它的
``status`` / ``summary`` / ``error``，以及工具自己渲染出来的正文。

两种合法修法都能通过：

* (a) 每个 task 各自计时；
* (b) 共享 deadline，但**不谎报**「哪个 task 跑过、哪个没跑过」，也**不丢**
  已完成的产出。

⇒ 为此 J1 的排队臂只断言**标签与事实一致**：一个**从未被启动**的 task 不得
被标成 ``timeout``（预算耗尽）。「预算耗尽」会让上层去「加大预算重试」，
而真正的原因是它**没排上** —— 这是两种修法都必须守的不变量。

**Why the race arm widens the window through a PUBLIC callback.**

「已经跑完的 task 被 timeout 标记覆盖」这个缺陷，窗口只有几微秒 —— 测试撞不
上。判据因此**把窗口撑开**：``run_tasks`` 的 ``on_state`` 是**公开参数**
（工具自己就传了 ``view.on_state``），判据在收到 timeout 标记时**睡一会**，
于是「收集结果的扫描」与「写 timeout 标记」之间被拉开了一个可测的间隔。

* **PRE-FIX** —— 那个迟到的 task 的产出被标记覆盖 ⇒ **红**；
* **CURRENT** —— 先检查 future 是否已完成 ⇒ **绿**。

撑窗口靠的是**公开回调**，不是改 runner 的私有函数 —— 判据测的是产品的行为，
不是产品的内部结构。

**Why the criteria drive the PUBLIC entry points, never a private function.**
（交办 J6 · 知识页 ``the-entrypoint-of-a-criterion-is-the-harness``）

* J1 / J4 的排队臂走 ``agent.subagent.runner.run_tasks`` —— runner 的**公开
  spawn 入口**；
* J2 / J3 走 ``agent.tools.subagent.subagent.SubagentTool.execute_tool`` ——
  **工具调用路径**（Agent 循环真正调用的那个）。

**Why the child is stubbed.**  子 Agent 的「工作」在这里是**被脚本化的**：
它快、它慢、它空、它从没启动 —— 全部由判据自己安排。被测的机制是**预算与
结果的聚合**，不是「LLM 能不能跑完任务」。真跑 LLM 既不可复现也不可计时。
仓库里已有的 ``tests/test_subagent_parent_cancel.py`` 用的就是同一手法。

**Why J3 asserts a BAND and not just "远小于 300 秒".**  「配置读到了」这句
话有一个陷阱：旧实现把 ``timeout_seconds`` 的**下限**卡在 10 秒，于是显式
配置 5 秒会被**静默**换成 10 秒 —— 配置读到了，却被改写了。⇒ 判据断言的是
**请求值真的被用上了**：实际耗时必须落在 ``[0.6×请求, 1.6×请求]`` 内。

**Why J4 exists.**  一条判据如果对什么都绿，它和「产品是好的」长得一模一样。
J4 拿**产品真实跑出来的**三种结果去反问判据自己的判定函数：

* 对抗样本：task **跑完了但正文是空的**（机制缺失）⇒ 判定函数必须**拒绝**；
* 输入不存在：task **根本没跑**（父级取消）⇒ 判定函数必须**拒绝**；
* 且两者的**诊断码必须不同** —— 否则「机制缺失」与「输入不存在」不可区分，
  阴性对照就只是在测自己的探针；
* 阳性对照：正常产出 ⇒ 判定函数必须**接受**（防止它变成恒假）。

三臂全部来自**产品的真实运行**（没有手写的假结果）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.memory.acceptance import Report

#: The marker the fast task puts in its summary.  It must be recognisable in
#: the returned payload, so the criterion can tell "the output survived" from
#: "there is an output-shaped hole where the output should be".
FEATURE = "S3AA-FEATURE-9F2C"

#: The goals.  The SLOW one is always first: a slow task in front is what
#: makes the second task queue behind it.
SLOW_GOAL = "slow task: never finishes inside the budget"
FAST_GOAL = "fast task: report the marker %s and stop" % FEATURE
EMPTY_GOAL = "empty task: report nothing at all"

#: The goal of the task in the race arm.  It finishes just AFTER the budget
#: ran out — it really produces an output; the product just has to notice
#: that it did and keep it.
LATE_GOAL = "late task: report the marker %s, but only after the deadline" % FEATURE

#: The budget the criteria ask for.  Two seconds is enough for the fast task
#: to finish and small enough that the whole module stays well under a minute.
BUDGET_SMALL = 2.0

#: The budget J3 asks for.  Five seconds: deliberately BELOW the OLD 10s
#: floor, because the old floor silently rewrote a 5s request into 10s.
#: That silent rewrite is exactly what J3 exists to catch.
BUDGET_CONFIG_TEST = 5.0

#: The band the configured budget must land in, as a multiple of the request.
#: Too wide and a silently clamped value sneaks through; too narrow and the
#: measurement noise on a loaded machine fails the criterion.
BAND_LOW = 0.6
BAND_HIGH = 1.6

#: How long the tool call may take before J3 calls it a hang.
WATCHDOG_SECONDS = 12.0

#: How long a scripted child may run before it gives up on its own.  It bounds
#: the mutant arm that ignores the configured budget: without this cap the
#: process would not be able to exit for 300 seconds.
CHILD_MAX_SECONDS = 20.0

#: How long after the budget the late task produces its output.  It must land
#: safely INSIDE the widened window and safely AFTER the last possible
#: results-collect sweep (the runner polls every 100ms, so the deadline is
#: noticed somewhere in [2.00s, 2.10s] after the spawn).  0.5s past the
#: deadline clears both.
LATE_GAP_SECONDS = 0.5

#: How long the ``on_state`` callback sleeps when it sees a timeout marker.
#: This is what widens the race window from microseconds to a measurable
#: interval.  It is the ONLY thing the criterion changes about the
#: product's timing, and it goes through a PUBLIC parameter.
RACE_WINDOW_SECONDS = 1.2

#: The diagnosis codes.  J4's whole point is that these are DISTINGUISHABLE.
DIAG_OK = "OK"                        # 产出在
DIAG_NO_OUTPUT = "NO_OUTPUT"          # 机制缺失：跑完了，正文是空的
DIAG_NOT_STARTED = "NOT_STARTED"      # 输入不存在：根本没跑
DIAG_NOT_COMPLETED = "NOT_COMPLETED"  # 跑了，但不是 completed
DIAG_MISSING = "MISSING"              # 连这条 task 的结果记录都没有
DIAG_POLLUTED = "POLLUTED"            # completed 却带着超时/失败信息


# ---------------------------------------------------------------------------
# helpers · the scripted child and the scripted parent
# ---------------------------------------------------------------------------

class _ScriptedChild:
    """A child whose WORK is scripted; the BUDGET is the product's.

    The child's only job is to be fast, to be slow, to be empty, or to be
    absent — so the criterion can put the product under a known pressure and
    read what the product says about it.
    """

    def __init__(self, kind: str, state: Dict[str, Any], goal: str):
        self._kind = kind
        self._state = state
        self._goal = goal

    def run_stream(self, user_message, clear_history=False, cancel_event=None,
                   on_event=None, steer_inbox=None):
        self._state["started"].append(self._goal)
        if self._kind == "fast":
            time.sleep(0.05)
            return "结论：%s 已完成" % FEATURE
        if self._kind == "empty":
            time.sleep(0.05)
            return ""
        if self._kind == "late":
            # Finishes just AFTER the budget ran out — it really produces an
            # output, the product just has to notice that it did.
            while time.time() < self._state["late_at"]:
                time.sleep(0.01)
            summary = "结论：%s 已完成（迟到了）" % FEATURE
            self._state["produced"][self._goal] = summary
            return summary
        # "slow": runs until the product tells it to stop.
        deadline = time.time() + CHILD_MAX_SECONDS
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return "stopped"
            time.sleep(0.02)
        return "gave up on its own"


def _fake_parent(workspace: Path) -> SimpleNamespace:
    """The parent the spawn runs under.

    Only ``workspace_dir`` is really read (the tool resolves the template
    directory from it); the rest exists so the object is shaped like the
    Agent the product expects.
    """
    return SimpleNamespace(
        workspace_dir=str(workspace),
        project_dir=str(workspace),
        model=None,
        tools=[],
        max_steps=10,
        max_context_tokens=4096,
        skill_manager=None,
        enable_skills=False,
        runtime_info=None,
        permission_mode="default",
    )


@contextmanager
def _scripted_children(script: Dict[str, str], state: Dict[str, Any]):
    """Put the scripted child in place of the real one, then take it out."""
    from agent.subagent import runner

    original = runner._build_child
    runner._build_child = lambda parent, template, task: _ScriptedChild(
        script.get(task.goal, "fast"), state, task.goal)
    try:
        yield
    finally:
        runner._build_child = original


@contextmanager
def _installed_config(subagent: Dict[str, Any]):
    """Install a ``subagent`` config block in memory, then take it out.

    The product reads it through ``conf()`` — the same object the on-disk
    ``config.json`` feeds — so this is the real read path, with the file left
    alone.  (交办: 不要改 ``C:\\Users\\User\\.cow\\config.json``.)
    """
    from config import config as config_obj

    had = "subagent" in config_obj
    saved = dict.get(config_obj, "subagent")
    config_obj["subagent"] = dict(subagent)
    try:
        yield
    finally:
        if had:
            config_obj["subagent"] = saved
        else:
            config_obj.pop("subagent", None)


def _spawn(goals: Sequence[str], settings, script: Dict[str, str],
           cancel_event=None, on_state=None,
           late_at: Optional[float] = None) -> Tuple[List[Dict[str, Any]],
                                                     Dict[str, Any]]:
    """Run a spawn through the runner's PUBLIC entry point.

    ``agent.subagent.run_tasks`` is the function the tool calls; nothing
    private is touched.  The returned state says WHICH children actually
    started — that is what tells "never ran" apart from "ran but said
    nothing".
    """
    from agent.subagent import SubagentTask, run_tasks
    from agent.subagent.templates import GENERAL_PURPOSE

    state: Dict[str, Any] = {"started": [], "produced": {}, "late_at": late_at}
    templates = {GENERAL_PURPOSE.name: GENERAL_PURPOSE}
    tasks = [SubagentTask(goal=goal) for goal in goals]
    with _scripted_children(script, state):
        results = run_tasks(_fake_parent(Path(tempfile.gettempdir())),
                            tasks, templates, settings,
                            on_state=on_state, cancel_event=cancel_event)
    return list(results), state


def _spawn_via_tool(goals: Sequence[str], subagent_config: Dict[str, Any],
                    script: Dict[str, str],
                    bound: Optional[float] = None,
                    ) -> Tuple[Optional[Any], float, str, Dict[str, Any]]:
    """Run a spawn through the TOOL CALL path.

    Returns ``(tool_result, elapsed_seconds, problem, state)``.  ``problem``
    is empty on success; ``"timeout"`` means the call did not come back
    inside ``bound`` seconds.

    ``bound`` is the watchdog: a call that never returns must not make the
    criterion hang, and must be REPORTED as such rather than as a pass.
    """
    from agent.tools.subagent.subagent import SubagentTool

    state: Dict[str, Any] = {"started": [], "produced": {}}
    tmp = Path(tempfile.mkdtemp(prefix="s3aa-tool-"))
    (tmp / "subagents").mkdir(parents=True, exist_ok=True)

    tool = SubagentTool({"cwd": str(tmp)})
    tool.context = _fake_parent(tmp)
    tool.cancel_event = threading.Event()

    box: Dict[str, Any] = {}

    def _call():
        with _scripted_children(script, state):
            with _installed_config(subagent_config):
                box["result"] = tool.execute_tool({
                    "tasks": [{"goal": goal} for goal in goals],
                })

    if bound is None:
        started = time.time()
        _call()
        return box.get("result"), time.time() - started, "", state

    thread = threading.Thread(target=_call, daemon=True)
    started = time.time()
    thread.start()
    thread.join(bound)
    elapsed = time.time() - started
    if thread.is_alive():
        return None, elapsed, "timeout", state
    return box.get("result"), elapsed, "", state


# ---------------------------------------------------------------------------
# helpers · the diagnosis (what the criterion says about ONE task)
# ---------------------------------------------------------------------------

def _diagnose(result: Optional[Dict[str, Any]], goal: str,
              started: Sequence[str]) -> Tuple[str, str]:
    """What happened to one task, in words a reader can act on.

    The codes are the point of J4: a task that RAN and said nothing
    (``NO_OUTPUT``) is a different defect from a task that never RAN
    (``NOT_STARTED``), and a criterion that cannot tell them apart is
    reporting its own probe instead of the product.
    """
    if result is None:
        return DIAG_MISSING, "没有这条 task 的结果记录"
    status = str(result.get("status") or "")
    summary = str(result.get("summary") or "")
    error = str(result.get("error") or "")
    ran = goal in started
    if status != "completed":
        if not ran:
            return (DIAG_NOT_STARTED,
                    "这个 task 根本没跑过（没有子 Agent 启动过它），状态却是 %r"
                    % (status or "(空)",))
        return (DIAG_NOT_COMPLETED,
                "这个 task 跑过了，状态却是 %r，不是 completed" % (status or "(空)",))
    if not summary.strip():
        return DIAG_NO_OUTPUT, "这个 task 跑完了并报 completed，但正文是空的"
    if error.strip():
        return (DIAG_POLLUTED,
                "这个 task 报 completed，却带着错误信息：%s" % error[:120])
    return DIAG_OK, "产出在，正文 %d 字符，含特征串" % len(summary)


def _result_for(results: Sequence[Dict[str, Any]], index: int) -> Optional[Dict[str, Any]]:
    for item in results:
        if item.get("task_index") == index:
            return item
    return None


def _is_timeout_labelled(result: Optional[Dict[str, Any]]) -> bool:
    """True when the result says, in so many words, that the budget ran out."""
    if not result:
        return False
    status = str(result.get("status") or "")
    error = str(result.get("error") or "")
    if status in ("timeout", "failed"):
        return True
    return bool(error.strip())


def _payload(result: Any) -> List[Dict[str, Any]]:
    """The tool's JSON payload, parsed.  Never a private copy of it."""
    if result is None:
        return []
    raw = getattr(result, "result", None)
    if isinstance(raw, (list, dict)):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return []
    items = data.get("results") if isinstance(data, dict) else data
    return list(items or [])


def _display(result: Any) -> str:
    return str(getattr(result, "display", "") or "")


def _code_fingerprint() -> str:
    """Which implementation this run measured.

    A criterion that cannot say what it measured is a claim.  The commit and
    the digest of the file that owns the budget are that sentence.
    """
    repo = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        commit = "(unknown)"
    runner = repo / "agent" / "subagent" / "runner.py"
    digest = ""
    try:
        digest = hashlib.sha256(runner.read_bytes()).hexdigest()[:12]
    except OSError:
        digest = "(unreadable)"
    return "commit=%s runner.py sha256=%s" % (commit or "(unknown)", digest)


# ---------------------------------------------------------------------------
# the criteria
# ---------------------------------------------------------------------------

def judge_completed_output_survives_timeout(report: Report,
                                             workspace: Path = None) -> bool:
    """J1 · a task that finished keeps its output when a sibling times out.

    Three arms, because the defect has three faces:

    * **排队臂** — the fast task waits behind the slow one.  Its own budget
      starts when IT starts, so the slow task's overrun must not decide its
      fate.  And the label must be honest: a task that never ran must not be
      reported as "budget exhausted".
    * **竞态臂** — the fast task finishes just AFTER the budget ran out.  It
      really produced an output; the product has to notice that it did and
      keep it.  The window is widened through the public ``on_state``
      callback, so the arm is repeatable rather than a lottery.
    * **并行臂** — both tasks really run at once: the fast one's summary must
      be in the result with its marker, and the slow one must be **labelled**.

    The failure message names WHICH face broke: 「完成的产出丢了」vs
    「超时没被标注」.
    """
    from agent.subagent import SubagentSettings

    lines = ["被测实现: %s" % _code_fingerprint(), ""]
    ok = True
    problems: List[str] = []

    for arm, max_concurrent in (("并行臂（两个 task 真的同时跑）", 2),
                                ("排队臂（第二个 task 排在慢 task 后面）", 1)):
        settings = SubagentSettings(timeout_seconds=BUDGET_SMALL,
                                    max_concurrent=max_concurrent)
        results, state = _spawn(
            [SLOW_GOAL, FAST_GOAL],
            settings,
            {SLOW_GOAL: "slow", FAST_GOAL: "fast"},
        )
        fast = _result_for(results, 1)
        slow = _result_for(results, 0)
        started = state["started"]
        diag, why = _diagnose(fast, FAST_GOAL, started)

        lines.append("%s（max_concurrent=%d，预算 %.1fs）" % (arm, max_concurrent,
                                                             BUDGET_SMALL))
        lines.append("  启动过的子 Agent: %s" % (", ".join(started) or "(无)"))
        lines.append("  快的那个 task: status=%r  判定=%s  —— %s"
                     % ((fast or {}).get("status"), diag, why))
        lines.append("  慢的那个 task: status=%r  有错误信息=%s"
                     % ((slow or {}).get("status"),
                        bool(str((slow or {}).get("error") or "").strip())))

        arm_ok = True
        # --- the completed task must keep its output ---
        if diag == DIAG_OK:
            summary = str((fast or {}).get("summary") or "")
            if FEATURE not in summary:
                arm_ok = False
                problems.append(
                    "%s：完成的产出丢了 —— 快的 task 报 completed，但正文里没有"
                    "特征串 %s" % (arm, FEATURE))
                lines.append("    <-- 完成的产出丢了：正文里没有特征串 %s" % FEATURE)
        elif diag == DIAG_NOT_STARTED and str((fast or {}).get("status")) == "timeout":
            # The task never ran, yet the product says the budget ran out.
            # That sends the caller to raise a budget for a task that was
            # never scheduled.
            arm_ok = False
            problems.append(
                "%s：完成的产出丢了 —— 快的 task 根本没跑，却被标成 timeout"
                "（『预算耗尽』）" % arm)
            lines.append("    <-- 完成的产出丢了：根本没跑，却被标成 timeout")
        else:
            arm_ok = False
            problems.append("%s：完成的产出丢了 —— %s" % (arm, why))
            lines.append("    <-- 完成的产出丢了：%s" % why)

        # --- the timed-out task must be labelled ---
        if not _is_timeout_labelled(slow):
            arm_ok = False
            problems.append(
                "%s：超时没被标注 —— 慢的 task 没有 timeout/failed 状态，"
                "也没有错误信息" % arm)
            lines.append("    <-- 超时没被标注")
        else:
            lines.append("    超时标注: %s" % str((slow or {}).get("error") or "")[:80])

        ok = ok and arm_ok
        lines.append("")

    # --- the race arm: a task that finished just after the deadline ---
    lines.append("竞态臂（迟到的 task 真的跑完了，但预算先到）")
    race = _race_arm()
    lines.extend(race["lines"])
    if not race["ok"]:
        ok = False
        problems.append("竞态臂：完成的产出丢了 —— %s" % race["why"])
    lines.append("")

    if problems:
        lines.append("问题:")
        for p in problems:
            lines.append("  * %s" % p)
    return report.judge("J1", "完成的产出不丢（排队臂 + 竞态臂 + 并行臂）", ok,
                        "\n".join(lines))


def _race_arm() -> Dict[str, Any]:
    """A task that finishes just after the deadline must keep its output.

    The window between "the results were collected" and "the timeout marker
    was written" is microseconds wide in the product, so the arm widens it
    through the PUBLIC ``on_state`` callback: when the callback sees a
    timeout marker it sleeps, and the late task's output lands inside that
    sleep.

    The arm is not a lottery — the same widening makes the PRE-FIX
    implementation lose the output every time.
    """
    from agent.subagent import SubagentSettings

    def on_state(index, st):
        if str(st.get("status")) in ("timeout", "cancelled"):
            time.sleep(RACE_WINDOW_SECONDS)

    settings = SubagentSettings(timeout_seconds=BUDGET_SMALL, max_concurrent=2)

    # ``late_at`` is stamped BEFORE the spawn, so the late child knows when
    # to hand in its output — it finishes after the budget ran out but
    # inside the widened window.
    late_at = time.time() + BUDGET_SMALL + LATE_GAP_SECONDS

    results, state = _spawn(
        [SLOW_GOAL, LATE_GOAL],
        settings,
        {SLOW_GOAL: "slow", LATE_GOAL: "late"},
        on_state=on_state,
        late_at=late_at,
    )
    late = _result_for(results, 1)
    slow = _result_for(results, 0)
    produced = str(state.get("produced", {}).get(LATE_GOAL) or "")
    summary = str((late or {}).get("summary") or "")
    kept = FEATURE in summary
    diag, why = _diagnose(late, LATE_GOAL, state["started"])

    lines = [
        "  预算 %.1fs，迟到的 task 在 %.2fs 后才交出产出；on_state 收到 timeout "
        "标记时睡 %.1fs（撑开竞态窗口）"
        % (BUDGET_SMALL, BUDGET_SMALL + LATE_GAP_SECONDS, RACE_WINDOW_SECONDS),
        "  子 Agent 确实交出了产出: %s" % ("是" if produced else "否"),
        "  迟到的 task: status=%r  判定=%s  —— %s"
        % ((late or {}).get("status"), diag, why),
        "  慢的那个 task: status=%r  有错误信息=%s"
        % ((slow or {}).get("status"),
           bool(str((slow or {}).get("error") or "").strip())),
    ]

    ok = True
    why_text = ""
    if not produced:
        # The child never got to finish — the product cancelled it before it
        # could report.  That is a DIFFERENT defect and must say so.
        ok = False
        why_text = "迟到的 task 根本没机会交出产出（它被提前取消了）"
    elif not kept:
        ok = False
        why_text = ("迟到的 task 交出了产出（正文 %d 字符，含特征串），"
                    "但返回体里它的正文被 timeout 标记覆盖了"
                    % len(produced))
    if not _is_timeout_labelled(slow):
        ok = False
        why_text = why_text or "慢的那个 task 没有 timeout/failed 状态，也没有错误信息"

    if not ok:
        lines.append("    <-- %s" % why_text)
    return {"ok": ok, "why": why_text, "lines": lines}


def judge_completed_output_is_not_polluted(report: Report,
                                           workspace: Path = None) -> bool:
    """J2 · the completed task's row must not be described as a casualty.

    Driven through the TOOL CALL path — the path the Agent loop really uses
    — because that is where a blanket failure would be produced: a
    ``spawn failed`` covering every task, including the one that worked.

    Three things must hold:

    * the tool call itself succeeds (one task worked ⇒ the spawn worked);
    * the completed task's row says ``completed``, carries its summary, and
      carries **no** error;
    * the product's OWN rendering of that task does not label it
      timeout / failed / cancelled.
    """
    subagent_config = {
        "enabled": True,
        "max_depth": 1,
        "max_concurrent": 2,
        "timeout_seconds": BUDGET_SMALL,
    }
    result, elapsed, problem, state = _spawn_via_tool(
        [SLOW_GOAL, FAST_GOAL], subagent_config,
        {SLOW_GOAL: "slow", FAST_GOAL: "fast"},
    )
    lines = ["被测实现: %s" % _code_fingerprint(),
             "入口: agent.tools.subagent.subagent.SubagentTool.execute_tool",
             "耗时: %.2fs" % elapsed]
    if problem:
        return report.judge(
            "J2", "完成的产出不被超时态污染（工具调用路径）", False,
            "\n".join(lines + ["  工具调用在预算内没有返回 —— 判据无法判定"]))

    status = getattr(result, "status", None)
    items = _payload(result)
    display = _display(result)

    fast = None
    slow = None
    for item in items:
        if item.get("task_index") == 1:
            fast = item
        elif item.get("task_index") == 0:
            slow = item
    diag, why = _diagnose(fast, FAST_GOAL, state["started"])

    lines.append("工具状态: %r（一个 task 完成了 ⇒ 这一步成功）" % status)
    lines.append("快的那个 task: status=%r  判定=%s  —— %s"
                 % ((fast or {}).get("status"), diag, why))
    lines.append("慢的那个 task: status=%r  有错误信息=%s"
                 % ((slow or {}).get("status"),
                    bool(str((slow or {}).get("error") or "").strip())))
    lines.append("渲染出来的正文:")
    for block in display.split("\n\n---\n\n"):
        if block.strip():
            lines.append("  | %s" % block.strip().replace("\n", " / ")[:160])

    problems: List[str] = []
    ok = True

    if status != "success":
        ok = False
        problems.append("工具把整个 spawn 报成 %r —— 一个 task 完成了，"
                        "这一步就该是成功的（笼统的失败把全部 task 一起盖掉了）"
                        % status)
    if fast is None:
        ok = False
        problems.append("返回体里找不到快的那个 task 的结果")
    else:
        if str(fast.get("status") or "") != "completed":
            ok = False
            problems.append("快的那个 task 在返回体里被写成 %r —— 完成的产出"
                            "被超时态污染了" % (fast.get("status") or "(空)",))
        summary = str(fast.get("summary") or "")
        if FEATURE not in summary:
            ok = False
            problems.append("快的那个 task 的正文里没有特征串 %s —— 产出丢了"
                            % FEATURE)
        if str(fast.get("error") or "").strip():
            ok = False
            problems.append("快的那个 task 报 completed，却带着错误信息：%s"
                            % str(fast.get("error"))[:120])
    if not _is_timeout_labelled(slow):
        ok = False
        problems.append("慢的那个 task 没有 timeout/failed 状态，也没有错误信息")

    # The product's own rendering must not call the completed task a
    # casualty.  ``format_results`` writes ``**<status>** — <body>`` for a
    # task that did not complete, so the marker is unambiguous.
    polluted_block = ""
    for block in display.split("\n\n---\n\n"):
        if FEATURE in block:
            polluted_block = block
            break
    if polluted_block:
        for marker in ("**timeout**", "**failed**", "**cancelled**", "**error**"):
            if marker in polluted_block:
                ok = False
                problems.append("产品自己渲染出来的正文把完成的 task 标成了 %s"
                                % marker.strip("*"))
                break
    else:
        ok = False
        problems.append("产品渲染出来的正文里根本没有特征串 %s" % FEATURE)

    if problems:
        lines.append("")
        lines.append("问题:")
        for p in problems:
            lines.append("  * %s" % p)
    return report.judge("J2", "完成的产出不被超时态污染（工具调用路径）", ok,
                        "\n".join(lines))


def judge_configured_budget_is_honoured(report: Report,
                                         workspace: Path = None) -> bool:
    """J3 · the configured budget is the budget that applies.

    ``subagent.timeout_seconds`` is set to 5 seconds and a task that cannot
    finish is run.  Three things must hold:

    * the call comes back inside the watchdog (it is a timeout, not a hang);
    * the elapsed time lands in ``[0.6×5, 1.6×5]`` — **the value that was
      asked for**, not a value the product quietly rewrote;
    * the result says the budget ran out.

    The band is what makes 「配置读到了」 mean something: an old
    implementation clamped a 5-second request to 10 seconds, silently.  A
    request that comes back in 10 seconds is a request that was NOT honoured.
    """
    subagent_config = {
        "enabled": True,
        "max_depth": 1,
        "max_concurrent": 2,
        "timeout_seconds": BUDGET_CONFIG_TEST,
    }
    result, elapsed, problem, state = _spawn_via_tool(
        [SLOW_GOAL], subagent_config, {SLOW_GOAL: "slow"},
        bound=WATCHDOG_SECONDS,
    )
    low = BAND_LOW * BUDGET_CONFIG_TEST
    high = BAND_HIGH * BUDGET_CONFIG_TEST

    lines = ["被测实现: %s" % _code_fingerprint(),
             "入口: agent.tools.subagent.subagent.SubagentTool.execute_tool",
             "请求的预算: subagent.timeout_seconds = %.1fs" % BUDGET_CONFIG_TEST,
             "实测耗时: %.2fs（允许区间 %.1fs ~ %.1fs）" % (elapsed, low, high),
             "看门狗: %.0fs" % WATCHDOG_SECONDS]
    problems: List[str] = []
    ok = True

    if problem == "timeout":
        ok = False
        problems.append("配置没有被读到 —— 调用在 %.0fs 内没有返回"
                        % WATCHDOG_SECONDS)
        return report.judge("J3", "配置生效可观测（预算真的被用上了）", False,
                            "\n".join(lines + ["  * " + problems[-1]]))

    items = _payload(result)
    item = items[0] if items else None
    lines.append("返回结果: status=%r  error=%r"
                 % ((item or {}).get("status"),
                    str((item or {}).get("error") or "")[:100]))

    if not (low <= elapsed <= high):
        ok = False
        problems.append(
            "请求了 %.1fs 的预算，实际用了 %.2fs —— 配置被静默改写了"
            % (BUDGET_CONFIG_TEST, elapsed))
    if not _is_timeout_labelled(item):
        ok = False
        problems.append("必然超预算的 task 没有被标成 timeout/failed")

    if problems:
        lines.append("")
        for p in problems:
            lines.append("  * %s" % p)
    return report.judge("J3", "配置生效可观测（预算真的被用上了）", ok,
                        "\n".join(lines))


def judge_criterion_has_teeth(report: Report,
                               workspace: Path = None) -> bool:
    """J4 · the criterion itself must be able to fail.

    Three runs of the PRODUCT, and three questions of the criterion's own
    diagnosis function:

    * **对抗样本（机制缺失）** — the fast task runs and reports ``completed``
      with an EMPTY body.  The diagnosis must reject it, and must say
      ``NO_OUTPUT``.
    * **输入不存在** — the parent is cancelled before anything starts, so no
      child ever runs.  The diagnosis must reject it too, and must say
      ``NOT_STARTED``.
    * the two diagnoses must be **different**: a criterion that cannot tell
      「跑了但没产出」 from 「根本没跑」 is reporting its own probe.
    * **阳性对照** — a normal run must be accepted, or the diagnosis is simply
      always false.

    All three arms come from REAL runs of the product; no result is
    hand-written.  A rare marker word would have proved nothing here — the
    question is not "does the word exist" but "does the mechanism hold".
    """
    from agent.subagent import SubagentSettings

    lines = ["被测实现: %s" % _code_fingerprint()]
    problems: List[str] = []
    ok = True

    # --- 对抗样本: the task ran and said nothing ---
    settings = SubagentSettings(timeout_seconds=BUDGET_SMALL, max_concurrent=2)
    results, state = _spawn(
        [SLOW_GOAL, EMPTY_GOAL],
        settings,
        {SLOW_GOAL: "slow", EMPTY_GOAL: "empty"},
    )
    empty = _result_for(results, 1)
    diag_empty, why_empty = _diagnose(empty, EMPTY_GOAL, state["started"])
    lines.append("对抗样本（跑完了但正文是空的）: status=%r  判定=%s —— %s"
                 % ((empty or {}).get("status"), diag_empty, why_empty))
    if diag_empty == DIAG_OK:
        ok = False
        problems.append("判据把「跑完了但正文是空的」当成了「产出还在」—— "
                        "判据没有牙齿")
    if diag_empty != DIAG_NO_OUTPUT:
        ok = False
        problems.append("对抗样本的判定码是 %r，应为 %r"
                        % (diag_empty, DIAG_NO_OUTPUT))

    # --- 输入不存在: nothing ever ran ---
    stop = threading.Event()
    stop.set()
    settings = SubagentSettings(timeout_seconds=BUDGET_SMALL, max_concurrent=2)
    results, state = _spawn(
        [SLOW_GOAL, FAST_GOAL],
        settings,
        {SLOW_GOAL: "slow", FAST_GOAL: "fast"},
        cancel_event=stop,
    )
    absent = _result_for(results, 1)
    diag_absent, why_absent = _diagnose(absent, FAST_GOAL, state["started"])
    lines.append("输入不存在（父级取消，没有子 Agent 启动）: status=%r  判定=%s —— %s"
                 % ((absent or {}).get("status"), diag_absent, why_absent))
    if diag_absent == DIAG_OK:
        ok = False
        problems.append("判据把「根本没跑」当成了「产出还在」")
    if diag_absent != DIAG_NOT_STARTED:
        ok = False
        problems.append("输入不存在的判定码是 %r，应为 %r"
                        % (diag_absent, DIAG_NOT_STARTED))
    if diag_empty == diag_absent:
        ok = False
        problems.append("「机制缺失」(%r) 与「输入不存在」(%r) 不可区分 —— "
                        "阴性对照只在测自己的探针"
                        % (diag_empty, diag_absent))

    # --- 阳性对照: a normal run must be accepted ---
    settings = SubagentSettings(timeout_seconds=BUDGET_SMALL, max_concurrent=2)
    results, state = _spawn(
        [SLOW_GOAL, FAST_GOAL],
        settings,
        {SLOW_GOAL: "slow", FAST_GOAL: "fast"},
    )
    normal = _result_for(results, 1)
    diag_normal, why_normal = _diagnose(normal, FAST_GOAL, state["started"])
    lines.append("阳性对照（正常产出）: status=%r  判定=%s —— %s"
                 % ((normal or {}).get("status"), diag_normal, why_normal))
    if diag_normal != DIAG_OK:
        ok = False
        problems.append("阳性对照失效：正常的产出没有被接受（%s）—— 判据成了恒假"
                        % why_normal)

    if problems:
        lines.append("")
        for p in problems:
            lines.append("  * %s" % p)
    return report.judge("J4", "判据自身不失真（对抗样本阴性对照）", ok,
                        "\n".join(lines))


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------

_CRITERIA = (
    ("J1", judge_completed_output_survives_timeout),
    ("J2", judge_completed_output_is_not_polluted),
    ("J3", judge_configured_budget_is_honoured),
    ("J4", judge_criterion_has_teeth),
)


def run(report: Report, workspace: Path = None, only: str = None) -> None:
    """Run J1..J4 against the product's sub agent machinery."""
    print("  被测实现: %s" % _code_fingerprint())
    for number, fn in _CRITERIA:
        if only and not number.startswith(only):
            continue
        fn(report, workspace)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S3-AA subagent 预算与超时产出丢失")
    ap.add_argument("--root", default=r"C:\Users\User\cow")
    ap.add_argument("--only", default=None)
    args = ap.parse_args(argv)
    report = Report()
    run(report, Path(args.root), args.only)
    return 0 if report.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
