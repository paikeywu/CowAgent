#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""regression_baseline.py — 回归基线快照机制（交办 C · PRC-004 / VER-006）

**这个脚本做什么**

把「同一个人、同一时刻、同一命令」的回归基线固定下来：

*   **快照（snapshot）**：跑一次全量回归，把**失败集合**（不是失败个数）
    落成一份可读、可 diff 的文本。
*   **比对（compare）**：拿一份快照与另一份快照比，输出 **NEW**（新增失败）
    与 **GONE**（已消失的失败）**两个集合**，以及它们的对称差。

**这个脚本绝不做什么**（C1：基线只由人手改）

*   **它没有任何写基线的代码路径。** 没有 ``--update`` / ``--write`` /
    ``--refresh`` 之类的开关；``grep -n "write\|open(.*w"`` 找不到一处对基线
    文件的写。
*   它只**读**基线、**写**自己的快照（快照是**证据**，不是基线）。
*   基线文件（本机制里是 ``tests/baseline/p1_regression_baseline.txt``
    与 ``tests/baseline/global_failure_baseline.txt``）**永远由人手改**。

**为什么「比集合，不比个数」**

同一台机器、同一份代码，全量回归的**失败个数**也会变（见知识页
``full-regression-numbers-lie-compare-sets.md``）：``state_dir.tmp_dir()``
依赖进程级全局注册表 ⇒ 顺序效应 ⇒ 同一份代码在不同的执行顺序下失败数
可以差 23 条。**个数会骗人，集合不会**。

**用法**

::

    # 1) 拍一份快照（默认写到 snapshots/<时间戳>.txt）
    python regression_baseline.py snapshot --out snapshots/snap-1105.txt

    # 2) 拍两份快照，比它们的失败集合（对称差必须为空才能拿去钉基线）
    python regression_baseline.py compare --a snapshots/a.txt --b snapshots/b.txt

    # 3) 拿一份快照与**人手维护的基线**比
    python regression_baseline.py compare --a snapshots/snap-1105.txt \\
                                        --baseline tests/baseline/p1_regression_baseline.txt

**退出码**

===  ==========================================================
0    OK（两个集合相同 / 无 NEW）
1    REGRESSION（有 NEW 失败）
4    NOT_RUN（快照缺失、为空、或没有可比对的失败集合）
===  ==========================================================

作者：业务集成测试工程师（agent-mvnf7fjl）· 2026-11-05
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Set, Tuple

# ---------------------------------------------------------------------------
# The one and only way this script touches a file: it WRITES snapshots and
# READS baselines.  There is deliberately no code path that writes a
# baseline (C1).  Keep this list short and keep it honest.
# ---------------------------------------------------------------------------
WRITES_ONLY_SNAPSHOTS = True

#: pytest's ``-q`` failure lines look like ``FAILED tests/x.py::test_y``.
_FAIL_LINE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
#: pytest's short summary also uses ``path::test`` without the FAILED prefix.
_ERROR_LINE = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)
#: ``N failed, M passed`` / ``N passed`` summaries.
_SUMMARY = re.compile(r"^(\d+) failed", re.MULTILINE)
_SUMMARY_PASS = re.compile(r"^(\d+) passed", re.MULTILINE)


def _eprint(*a) -> None:
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------------------
# 1 · snapshot
# ---------------------------------------------------------------------------
def take_snapshot(repo: Path, out: Path, extra_args: List[str], tests: str = "tests/") -> int:
    """Run the full regression once and write its FAILURE SET to ``out``.

    The command is fixed here so that every snapshot is "the same person,
    the same moment, the same command".  Override the test path with
    ``--tests`` only if you understand that you are then producing a
    DIFFERENT baseline.
    """
    cmd = [sys.executable, "-m", "pytest", tests, "-q",
           "--tb=no", "-p", "no:cacheprovider"]
    cmd.extend(extra_args)
    _eprint("[snapshot] command : %s" % " ".join(cmd))
    _eprint("[snapshot] cwd     : %s" % repo)
    started = datetime.now().astimezone()
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
    finished = datetime.now().astimezone()

    raw = (proc.stdout or "") + (proc.stderr or "")
    failures = sorted(set(_FAIL_LINE.findall(raw)) | set(_ERROR_LINE.findall(raw)))
    n_failed = _SUMMARY.search(raw)
    n_passed = _SUMMARY_PASS.search(raw)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write("# 回归基线快照（regression_baseline.py · 自动生成 · 勿手改）\n")
        fh.write("# 采集时间(起) : %s\n" % started.isoformat())
        fh.write("# 采集时间(止) : %s\n" % finished.isoformat())
        fh.write("# 仓库         : %s\n" % repo)
        fh.write("# 命令         : %s\n" % " ".join(cmd))
        fh.write("# 退出码       : %s\n" % proc.returncode)
        fh.write("# 摘要         : failed=%s passed=%s\n"
                 % (n_failed.group(1) if n_failed else "?",
                    n_passed.group(1) if n_passed else "?"))
        fh.write("#\n# ---- 失败集合（集合，不是个数）----\n")
        for f in failures:
            fh.write(f + "\n")
        fh.write("# ---- 失败集合结束（%d 条）----\n" % len(failures))
    _eprint("[snapshot] wrote   : %s (%d failures)" % (out, len(failures)))
    return 0


# ---------------------------------------------------------------------------
# 2 · reading a failure set out of a snapshot or a hand-written baseline
# ---------------------------------------------------------------------------
def read_failure_set(path: Path) -> Tuple[Set[str], List[str]]:
    """The failure set inside ``path``.

    Accepts BOTH shapes:

    *   a snapshot written by :func:`take_snapshot` (``FAILED x`` lines), and
    *   a hand-written baseline (``path::test`` lines, or ``FAILED path``).
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    text = path.read_text(encoding="utf-8", errors="replace")
    items: Set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("FAILED "):
            s = s[len("FAILED "):].strip()
        elif s.startswith("ERROR "):
            s = s[len("ERROR "):].strip()
        if "::" in s:
            items.add(s.split(" ", 1)[0].strip())
    return items, text.splitlines()


# ---------------------------------------------------------------------------
# 3 · compare  (SETS, never counts)
# ---------------------------------------------------------------------------
def compare(a_path: Path, b_path: Path, baseline_path: Path = None) -> int:
    try:
        a, _ = read_failure_set(a_path)
    except FileNotFoundError:
        _eprint("NOT_RUN: 找不到快照 %s" % a_path)
        return 4
    try:
        b, _ = read_failure_set(b_path)
    except FileNotFoundError:
        _eprint("NOT_RUN: 找不到快照 %s" % b_path)
        return 4
    if not a and not b:
        _eprint("NOT_RUN: 两份快照的失败集合都是空的 —— 没有可比对的东西")
        return 4

    new = sorted(a - b)
    gone = sorted(b - a)
    sym = sorted(a ^ b)

    print("")
    print("A 快照 : %s  （%d 条失败）" % (a_path, len(a)))
    print("B 快照 : %s  （%d 条失败）" % (b_path, len(b)))
    print("")
    print("NEW 失败（A 有 B 无 —— 本次新引入）: %d 条" % len(new))
    for f in new:
        print("    + %s" % f)
    print("GONE 失败（B 有 A 无 —— 本次已消失）: %d 条" % len(gone))
    for f in gone:
        print("    - %s" % f)
    print("对称差 |A XOR B|: %d 条" % len(sym))
    print("")
    if baseline_path is not None:
        try:
            base, _ = read_failure_set(baseline_path)
        except FileNotFoundError:
            _eprint("NOT_RUN: 找不到基线 %s" % baseline_path)
            return 4
        vs_base_new = sorted(a - base)
        vs_base_gone = sorted(base - a)
        print("对比基线 : %s （%d 条）" % (baseline_path, len(base)))
        print("  相对基线 NEW : %d 条" % len(vs_base_new))
        for f in vs_base_new:
            print("      + %s" % f)
        print("  相对基线 GONE: %d 条" % len(vs_base_gone))
        for f in vs_base_gone:
            print("      - %s" % f)
        print("")
        if vs_base_new:
            print("结论     : REGRESSION —— 相对基线有 %d 条新失败。" % len(vs_base_new))
            return 1
        print("结论     : OK —— 相对基线无新失败。")
        return 0

    if sym:
        print("结论     : 两份快照的失败集合**不同**（对称差 %d 条）——"
              " 此刻**不得**据此钉基线（活库在被持续改动，单次快照不可信）。" % len(sym))
        return 1
    print("结论     : 两份快照的失败集合**完全相同**（对称差 = 0）⇒ 可用于钉基线。")
    return 0


# ---------------------------------------------------------------------------
# 4 · CLI
# ---------------------------------------------------------------------------
def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(
        description="回归基线快照机制（基线只由人手改；本脚本只写快照）")
    ap.add_argument("--repo", default=r"C:\Workspace\AI-Agent\CowAgent",
                    help="仓库路径")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("snapshot", help="跑一次全量回归并落一份失败集合快照")
    sp.add_argument("--out", required=True, help="快照输出路径")
    sp.add_argument("--tests", default="tests/", help="被测目录（默认 tests/）")

    cp = sub.add_parser("compare", help="比两份快照的失败集合（NEW / GONE）")
    cp.add_argument("--a", required=True)
    cp.add_argument("--b", required=True)
    cp.add_argument("--baseline", default=None,
                    help="可选：再与人手维护的基线比一次")

    args = ap.parse_args(argv)
    repo = Path(args.repo)

    if args.cmd == "snapshot":
        out = Path(args.out)
        if not out.is_absolute():
            out = Path.cwd() / out
        take_snapshot(repo, out, [], tests=args.tests)
        return 0
    if args.cmd == "compare":
        return compare(Path(args.a), Path(args.b),
                       Path(args.baseline) if args.baseline else None)
    ap.print_help()
    return 4


if __name__ == "__main__":
    sys.exit(main())
