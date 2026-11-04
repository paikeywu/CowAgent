#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_regression_baseline_mechanism.py — 回归基线机制的判据/自测

主责：业务集成测试工程师（agent-mvnf7fjl）· 2026-11-05 · 交办 C

**测的是什么**：脚本 ``regression_baseline.py`` 本身。

判据（每一条都可判定，且都**不碰真实仓库**）：

*   **C1** 基线只由人手改 —— 脚本里**没有任何**写基线的代码路径。
    用**语义**证明：把脚本里所有「打开文件准备写」的调用点都找出来，
    断言它们写的**目标**全部是**快照**（``snapshots/`` 或 ``.snap``/``.txt``
    由调用方指定），且脚本**从不打开**仓库里的两份基线文件做写模式。
*   **C2** 比对的是**集合**不是个数 —— 两份快照失败数相同但内容不同 ⇒
    必须报出 NEW/GONE；两份内容相同 ⇒ 对称差 = 0。
*   **C3** 退出码三态可判 —— 0 / 1 / 4。
*   **C4** 0 匹配 = NOTRUN —— 快照文件不存在 ⇒ 退出码 4，不是「通过」。

**不碰真实仓库**：本文件只在 ``tmp`` 里造夹具，绝不跑全量回归
（那由交付物里的实跑输出作证）。
"""

import ast
import io
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 脚本已从「队友 workspace 的 tmp/」搬进仓库（tmp/ 会被清掉）
SCRIPT = HERE.parent / "scripts" / "memory" / "regression_baseline.py"

#: The two hand-written baselines in the repository.  The script must NEVER
#: open these in a write mode.
REPO_BASELINES = (
    Path("tests") / "baseline" / "global_failure_baseline.txt",
    Path("tests") / "baseline" / "p1_regression_baseline.txt",
)

#: The call that means "this call opens a file for writing".
_WRITE_CALL_NAMES = {
    "write_text", "write_bytes", "writelines",
}
_OPEN_WRITE_MODES = re.compile(r"""open\(\s*[^)]*mode\s*=\s*['"][wax]""")
_OPEN_BARE = re.compile(r"""open\(\s*([^)]*)\)""")


def _read_source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _all_write_call_sites(tree: ast.AST):
    """Every AST node in the script that can write to a file."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name in _WRITE_CALL_NAMES:
                yield node, name


def _open_write_mode(node: ast.Call) -> bool:
    """Does this ``open(...)`` call open for WRITING?

    **AST, not regex.**  A regex that only looks for ``mode="w"`` is blind
    to the positional form ``open(path, "w")`` — which is exactly the form
    a "clever" mutation would use.  (The first version of this judge was a
    false green; the mutant replay caught it.)
    """
    # keyword form: open(path, mode="w")
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            v = kw.value.value
            if isinstance(v, str) and v and v[0] in "wax":
                return True
    # positional form: open(path, "w")
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        v = node.args[1].value
        if isinstance(v, str) and v and v[0] in "wax":
            return True
    return False


def test_c1_script_has_no_baseline_write_path():
    """C1 · 基线只由人手改：脚本里没有写基线的代码路径。"""
    src = _read_source()
    tree = ast.parse(src)

    # (1) Every write call site must be a SNAPSHOT write.  We assert this
    #     structurally: the only ``open(...)`` in write mode must be the
    #     snapshot file, and the ``write_text`` calls must not mention a
    #     baseline file name.
    baseline_names = ("global_failure_baseline", "p1_regression_baseline",
                      "regression_baseline.txt")

    offenders = []
    for node, name in _all_write_call_sites(tree):
        seg = ast.get_source_segment(src, node) or ""
        if any(b in seg for b in baseline_names):
            offenders.append(("write_text", seg))
    assert not offenders, (
        "脚本里出现了写基线的代码路径：%r" % (offenders,))

    # (2) The ONLY ``open(...)`` with a write mode must be the snapshot.
    #     **Detect the write mode through the AST**, not a regex: a regex
    #     that only looks for ``mode="w"`` is blind to the positional form
    #     ``open(path, "w")`` — which is exactly what a "clever" mutation
    #     uses.  (The first version of this judge was a false green; the
    #     mutant replay caught it.  See mutant_replay_c1.py.)
    opens = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            opens.append(node)
    write_opens = [n for n in opens if _open_write_mode(n)]
    assert len(write_opens) <= 1, (
        "脚本里有 %d 处写模式的 open()，基线只由人手改 ⇒ 最多允许 1 处"
        "（快照）：%r"
        % (len(write_opens),
           [ast.get_source_segment(src, n) for n in write_opens]))

    # (3) The script must not contain any "refresh/update/rewrite baseline"
    #     switch in its CLI.
    ap = ast.parse(src)
    switches = []
    for node in ast.walk(ap):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_argument":
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    switches.append(a.value)
    forbidden = [s for s in switches
                 if re.search(r"update|refresh|rewrite|write.baseline|pin", s, re.I)]
    assert not forbidden, (
        "脚本的 CLI 里有「刷新/写入基线」开关：%r" % (forbidden,))


def test_c1_script_never_opens_repo_baselines_for_write():
    """C1 · 更硬的一条：脚本从不打开仓库里那两份基线。"""
    src = _read_source()
    # The script may MENTION the baseline file names in its docstring (it
    # explains what a baseline is), but it must not open them at all.
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            # The TARGET is what matters — not the whole call segment.
            target = ""
            if node.args:
                target = ast.get_source_segment(src, node.args[0]) or ""
            assert "global_failure_baseline" not in target, (
                "脚本打开了 global_failure_baseline：%r" % target)
            assert "p1_regression_baseline" not in target, (
                "脚本打开了 p1_regression_baseline：%r" % target)


def test_c3_compare_reports_sets_not_counts():
    """C3 · 比对的是集合：个数相同、内容不同 ⇒ 必须报 NEW/GONE。"""
    tmp = HERE / "_c3_fixtures"
    tmp.mkdir(exist_ok=True)
    a = tmp / "a.txt"
    b = tmp / "b.txt"
    a.write_text("FAILED tests/x.py::test_one\nFAILED tests/x.py::test_two\n",
                 encoding="utf-8")
    b.write_text("FAILED tests/x.py::test_one\nFAILED tests/x.py::test_three\n",
                 encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "compare", "--a", str(a), "--b", str(b)],
        capture_output=True, text=True)
    out = proc.stdout
    assert "NEW" in out and "GONE" in out, "输出里没有 NEW/GONE 两个集合：\n%s" % out
    assert "test_two" in out and "test_three" in out, (
        "两个集合的内容没被点名：\n%s" % out)
    # 两条失败 ⇒ 数量相同，但集合不同 ⇒ 必须判 REGRESSION（退出码 1）
    assert proc.returncode == 1, (
        "失败数相同但集合不同，退出码必须是 1（REGRESSION），实际 %d"
        % proc.returncode)


def test_c3_identical_sets_give_symmetric_difference_zero():
    """C3 · 两次快照的失败集合相同 ⇒ 对称差 = 0 ⇒ 退出码 0。"""
    tmp = HERE / "_c3_fixtures"
    tmp.mkdir(exist_ok=True)
    a = tmp / "a2.txt"
    b = tmp / "b2.txt"
    body = "FAILED tests/y.py::test_one\nFAILED tests/y.py::test_two\n"
    a.write_text(body, encoding="utf-8")
    b.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "compare", "--a", str(a), "--b", str(b)],
        capture_output=True, text=True)
    assert "对称差 |A XOR B|: 0 条" in proc.stdout, proc.stdout
    assert proc.returncode == 0, (
        "失败集合相同 ⇒ 退出码必须是 0，实际 %d" % proc.returncode)


def test_c4_missing_snapshot_is_notrun_not_pass():
    """C4 · 0 匹配 = NOTRUN，不是通过。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "compare",
         "--a", str(HERE / "_does_not_exist_1.txt"),
         "--b", str(HERE / "_does_not_exist_2.txt")],
        capture_output=True, text=True)
    assert proc.returncode == 4, (
        "快照缺失 ⇒ 退出码必须是 4（NOT_RUN），实际 %d" % proc.returncode)
    assert "NOT_RUN" in proc.stderr, (
        "stderr 必须写明 NOT_RUN，实际：\n%s" % proc.stderr)


def test_c4_empty_failure_set_is_notrun():
    """C4 · 两份都没有失败 ⇒ 没有可比对的东西 ⇒ NOTRUN（不是「通过」）。"""
    tmp = HERE / "_c3_fixtures"
    tmp.mkdir(exist_ok=True)
    a = tmp / "empty_a.txt"
    b = tmp / "empty_b.txt"
    a.write_text("# 空\n", encoding="utf-8")
    b.write_text("# 空\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "compare", "--a", str(a), "--b", str(b)],
        capture_output=True, text=True)
    assert proc.returncode == 4, (
        "两份快照都没有失败 ⇒ 退出码必须是 4（NOT_RUN），实际 %d"
        % proc.returncode)


def test_c3_baseline_comparison_reports_new_and_gone():
    """C3 · 与人手维护的基线比：NEW / GONE 两个集合都要给。"""
    tmp = HERE / "_c3_fixtures"
    tmp.mkdir(exist_ok=True)
    snap = tmp / "snap.txt"
    base = tmp / "base.txt"
    snap.write_text("FAILED tests/z.py::test_new\nFAILED tests/z.py::test_old\n",
                    encoding="utf-8")
    base.write_text("tests/z.py::test_old\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "compare", "--a", str(snap),
         "--b", str(snap), "--baseline", str(base)],
        capture_output=True, text=True)
    assert "相对基线 NEW : 1 条" in proc.stdout, proc.stdout
    assert "test_new" in proc.stdout, proc.stdout
    assert proc.returncode == 1, (
        "相对基线有 NEW ⇒ 退出码必须是 1，实际 %d" % proc.returncode)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
