#!/usr/bin/env python3
# encoding:utf-8
"""MUTANT REPLAY · S3-AA · subagent 预算与超时产出丢失.

本文件重放 S3-AA 的四条判据的变异，并**证明每个变异真的生效**
（知识页 ``mutant-arm-must-prove-it-took-effect``：sha256 变了 ≠ 变异生效）。

**How a mutation is applied and proved.**  每个变异：

1. **改源码** —— 精确字符串替换（``before`` → ``after``），并断言 ``before``
   在原文里**只出现一次**（出现两次说明改错了地方，改错地方的变异是空操作）。
2. **写文件** —— 落盘。
3. **自检** —— mutant 的标记串必须出现在变异后的文件里。
4. **独立子进程** —— 每条臂跑 ``python -m agent.memory.acceptance.phase2_s3aa``，
   绕开 ``sys.modules`` 缓存（同一进程里改文件再跑，跑的还是内存里的旧代码）。
5. **还原** —— 把文件恢复原样，并核对 sha256 回到原值。

⇒ **「变异生效」的证据是「臂的输出变了」，不是「臂的文件变了」。**

**The mutants**

=====================================  ==========================  ==========
ID                                    变异                        期望
=====================================  ==========================  ==========
MU-AA1  runner.py                      恢复共享 deadline           J1 红
                                      （所有 task 共用一个）
MU-AA2  runner.py                      写 timeout 标记时           J1 红
                                      不检查 future 是否已完成
MU-AA3  subagent.py                    工具把整个 spawn           J2 红
                                      报成失败
MU-AA4  runner.py                      预算的下限卡回 10s          J3 红
                                      （5s 的请求被静默改写）
MU-AA5  phase2_s3aa.py                 判据的判定函数被阉割        J4 红
                                      （永远说「产出在」）
=====================================  ==========================  ==========

**Why MU-AA1 is not "the old file".**  旧实现不是「另一份代码」，而是**一种
形状**：一次 spawn 只取一个 deadline，所有 task 共用它。MU-AA1 把这个形状
放回去（判据照跑），而 per-task budget 的机制原封不动 ⇒ 因此 MU-AA1 证明的
是**判据不依赖实现形状** —— 共享 deadline 的实现，红；逐 task 计时的实现，
绿。

**Why MU-AA4 rewrites the DEFAULT too.**  历史形状是「下限 10s ⇒ 5s 的请求
被驳回 ⇒ 落回默认 300s」。300s 的一次重放要跑满 5 分钟，而 J3 的看门狗是
12s —— 判据本来就会判红，但进程会在解释器退出时等线程池，拖到 300s。
MU-AA4 把**默认值**也改成 10s：形状完全相同（请求被下限驳回，落回默认），
重放只花 10s。
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import (  # noqa: E402
    STATE_FAIL,
    STATE_NOTRUN,
    STATE_PASS,
)

RUNNER = REPO / "agent" / "subagent" / "runner.py"
TOOL = REPO / "agent" / "tools" / "subagent" / "subagent.py"
CRITERION = REPO / "agent" / "memory" / "acceptance" / "phase2_s3aa.py"

_STATE_LINE = re.compile(r"^\[(PASS|FAIL|NOTRUN)\]\s+([A-Za-z0-9\-]+):")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_criteria(timeout: int = 240) -> dict:
    """Run the criteria in a FRESH process and return the states.

    A fresh process is not a detail — it is the whole point: the same
    process that edits the file keeps the OLD code in ``sys.modules``, so a
    mutation applied in-process proves nothing.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "agent.memory.acceptance.phase2_s3aa"],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    states = {}
    for raw in proc.stdout.splitlines():
        match = _STATE_LINE.match(raw.strip())
        if match:
            states[match.group(2)] = match.group(1)
    return {"states": states, "stdout": proc.stdout, "stderr": proc.stderr,
            "returncode": proc.returncode}


@contextlib.contextmanager
def _mutated(*edits, path: Path = RUNNER):
    """Apply one or more edits, prove they landed, then put the file back.

    ``edits`` are ``(before, after, marker)`` triples.  The mechanical
    guarantee is: every ``before`` matched **exactly once**, the mutation's
    marker is present afterwards, and the file's digest comes back to its
    original value.  The *behavioural* proof — that the mutation actually
    changed what the product does — is the caller's: the criterion's verdict
    must move.
    """
    original = path.read_text(encoding="utf-8")
    original_sha = _sha256(path)
    mutated = original
    for before, after, marker in edits:
        count = mutated.count(before)
        assert count == 1, (
            "变异定位失败：%r 在 %s 里出现 %d 次（必须恰好 1 次 —— 0 次是改错了"
            "地方，>1 次说明这个片段不唯一，变异会变成空操作）"
            % (before[:70], path.name, count))
        mutated = mutated.replace(before, after)
        assert marker in mutated, "变异自检失败：标记 %r 不在变异后的文件里" % marker
    assert mutated != original, "变异是空操作（替换前后一样）"
    path.write_text(mutated, encoding="utf-8")
    try:
        yield
    finally:
        path.write_text(original, encoding="utf-8")
        assert _sha256(path) == original_sha, "还原后 sha256 不一致 —— 文件没还原干净"


# ---------------------------------------------------------------------------
# the mutants
# ---------------------------------------------------------------------------

#: MU-AA1 · ONE deadline for the whole spawn.
#:
#: The pre-fix runner took ``deadline = time.time() + timeout_seconds`` when
#: the spawn was called, and every task was judged against THAT.  The two
#: edits put that shape back: one deadline, taken before the loop, and every
#: pending task judged against it.
MU_AA1_EDIT_1 = (
    """        pending = dict(enumerate(futures))
        while True:""",
    """        pending = dict(enumerate(futures))
        _mu_aa1_shared_deadline = time.time() + settings.timeout_seconds
        while True:""",
    "_mu_aa1_shared_deadline = time.time()",
)

MU_AA1_EDIT_2 = (
    """            remaining_by_index = {i: budgets[i].remaining() for i in pending}
            expired = [i for i, left in remaining_by_index.items()
                       if left is not None and left <= 0]""",
    """            remaining_by_index = {i: _mu_aa1_shared_deadline - time.time()
                                  for i in pending}
            expired = [i for i, left in remaining_by_index.items()
                       if left <= 0]""",
    "_mu_aa1_shared_deadline - time.time()",
)

#: MU-AA2 · the clobber: the timeout marker is written over a result that
#: has already been produced.  The runner's own comment says why this is
#: the defect — "Writing a timeout marker over it would throw away work
#: that was actually done".  The mutant removes the guard.
MU_AA2_EDIT = (
    """                    if pending[index].done():
                        results[index] = pending.pop(index).result()
                        continue""",
    """                    if False:  # MU-AA2: write the marker over a real result
                        results[index] = pending.pop(index).result()
                        continue""",
    "MU-AA2: write the marker over a real result",
)

#: MU-AA3 · the blanket failure: the tool reports the whole spawn as failed
#: even though one task worked.
MU_AA3_EDIT = (
    """        if all(r.get("status") not in ("completed", "cancelled") for r in results):""",
    """        if True:  # MU-AA3: one blanket failure over every task""",
    "MU-AA3: one blanket failure over every task",
)

#: MU-AA4 · the silently rewritten budget: the request falls below the floor,
#: so the default is used instead — and the caller never hears about it.
MU_AA4_EDIT = (
    """            timeout_seconds=_bounded("timeout_seconds", 300.0, 1.0, 3600.0, float),""",
    """            timeout_seconds=_bounded("timeout_seconds", 10.0, 10.0, 3600.0, float),""",
    '_bounded("timeout_seconds", 10.0, 10.0, 3600.0, float)',
)

#: MU-AA5 · the criterion's OWN diagnosis is neutered: it always says the
#: output is there.  This is the mutant that proves J4 has teeth — without
#: it, "J4 is green" could just mean "the diagnosis is a rubber stamp".
MU_AA5_EDIT = (
    """    if result is None:
        return DIAG_MISSING, "没有这条 task 的结果记录"
    status = str(result.get("status") or "")""",
    """    if True:  # MU-AA5: the diagnosis is a rubber stamp
        return DIAG_OK, "永远说产出在"
    if result is None:
        return DIAG_MISSING, "没有这条 task 的结果记录"
    status = str(result.get("status") or "")""",
    "MU-AA5: the diagnosis is a rubber stamp",
)


def test_mu_aa1_shared_deadline_makes_j1_red():
    """MU-AA1 · the shared-deadline shape ⇒ J1 must go RED."""
    with _mutated(MU_AA1_EDIT_1, MU_AA1_EDIT_2):
        run = _run_criteria()
    states = run["states"]
    assert states.get("J1") == STATE_FAIL, (
        "MU-AA1 没被杀死：J1 判据在共享 deadline 的实现下仍然是 %r（应为 FAIL）"
        "\n%s" % (states.get("J1"), run["stdout"]))
    assert states.get("J4") == STATE_PASS, (
        "MU-AA1 误伤了 J4（阴性对照不该被实现形状影响）：%r" % states.get("J4"))


def test_mu_aa2_clobber_makes_j1_red():
    """MU-AA2 · the clobber ⇒ J1 must go RED (the race arm)."""
    with _mutated(MU_AA2_EDIT):
        run = _run_criteria()
    states = run["states"]
    assert states.get("J1") == STATE_FAIL, (
        "MU-AA2 没被杀死：J1 判据在「timeout 标记覆盖已完成产出」的实现下"
        "仍然是 %r（应为 FAIL）\n%s" % (states.get("J1"), run["stdout"]))


def test_mu_aa3_blanket_failure_makes_j2_red():
    """MU-AA3 · the blanket failure ⇒ J2 must go RED."""
    with _mutated(MU_AA3_EDIT, path=TOOL):
        run = _run_criteria()
    states = run["states"]
    assert states.get("J2") == STATE_FAIL, (
        "MU-AA3 没被杀死：J2 判据在「整个 spawn 报失败」的实现下仍然是 %r"
        "（应为 FAIL）\n%s" % (states.get("J2"), run["stdout"]))
    assert states.get("J4") == STATE_PASS, (
        "MU-AA3 误伤了 J4：%r" % states.get("J4"))


def test_mu_aa4_silently_rewritten_budget_makes_j3_red():
    """MU-AA4 · the silently rewritten budget ⇒ J3 must go RED."""
    with _mutated(MU_AA4_EDIT):
        run = _run_criteria()
    states = run["states"]
    assert states.get("J3") == STATE_FAIL, (
        "MU-AA4 没被杀死：J3 判据在「5 秒的请求被静默改写」的实现下仍然是 %r"
        "（应为 FAIL）\n%s" % (states.get("J3"), run["stdout"]))


def test_mu_aa5_rubber_stamp_diagnosis_makes_j4_red():
    """MU-AA5 · the criterion's own diagnosis is neutered ⇒ J4 must go RED."""
    with _mutated(MU_AA5_EDIT, path=CRITERION):
        run = _run_criteria()
    states = run["states"]
    assert states.get("J4") == STATE_FAIL, (
        "MU-AA5 没被杀死：J4 判据在「判定函数永远说产出在」的实现下仍然是 %r"
        "（应为 FAIL）\n%s" % (states.get("J4"), run["stdout"]))


def test_the_control_arm_is_green_on_the_unmutated_tree():
    """The CONTROL · the tree as it is ⇒ every criterion must be GREEN.

    Without it, "every mutant is killed" could also mean "everything is red
    all the time" — a criterion that fails on the healthy product has no
    discriminating power at all.
    """
    run = _run_criteria()
    states = run["states"]
    for number in ("J1", "J2", "J3", "J4"):
        assert states.get(number) == STATE_PASS, (
            "对照组失效：%s 在未变异的实现上不是绿（%r）\n%s"
            % (number, states.get(number), run["stdout"]))


def test_the_tree_is_clean_after_every_replay():
    """The files the replays touched must be byte-identical afterwards.

    A replay that leaves a mutated file behind has not proved anything —
    it has just changed the product and left it changed.

    The check is on the **content**, not on ``git status``: the criterion
    module and this replay file are NEW (untracked) by design, so ``git
    status`` would report them forever and say nothing about whether a
    mutation was rolled back.
    """
    for path in (RUNNER, TOOL, CRITERION):
        proc = subprocess.run(["git", "-C", str(REPO), "diff", "--", "--",
                               str(path.relative_to(REPO)).replace("\\", "/")],
                              capture_output=True, text=True)
        assert proc.stdout.strip() == "", (
            "%s 在重放之后不是干净的（git diff 非空）：\n%s" % (path.name, proc.stdout))
