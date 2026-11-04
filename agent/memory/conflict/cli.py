# -*- coding: utf-8 -*-
"""P2-5 · 冲突裁决器的命令行入口。

用法
----
裁决一份冲突文件（JSONL，一行一条冲突），把裁决记录**追加**到裁决台账：

    python -m agent.memory.conflict adjudicate \\
        --conflicts <conflicts.jsonl> [--root <shared_root>] [--record]

只裁决不落盘（拿来看结果）：

    python -m agent.memory.conflict adjudicate --conflicts <conflicts.jsonl>

看当前裁决序（这张表就是「谁说了算」的全部）：

    python -m agent.memory.conflict ladder

看已落盘的裁决记录：

    python -m agent.memory.conflict rulings [--root <shared_root>]

**为什么要有 CLI**：一条只在测试里被调用的裁决路径，等于一条没人走过的路。
裁决器必须在**人手能跑**的地方也能跑 —— 否则「可执行」只是测试的假象。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .adjudicator import (
    adjudicate,
    load_conflicts,
    read_rulings,
    record_ruling,
    rulings_path,
)
from .ladder import RULES


def _cmd_ladder(_args) -> int:
    print("裁决序（顺序即裁决结果）:")
    for r in RULES:
        print("  tier %d  %-30s %s" % (r.tier, r.rule_id, r.name))
        print("            依据: %s" % r.basis)
    return 0


def _cmd_adjudicate(args) -> int:
    conflicts = load_conflicts(Path(args.conflicts))
    if not conflicts:
        print("没有读到任何冲突：%s" % args.conflicts, file=sys.stderr)
        return 2
    out: List[dict] = []
    for c in conflicts:
        ruling = adjudicate(c, RULES)
        out.append(dict(ruling))
        if args.record:
            record_ruling(ruling, Path(args.root) / "conflicts" / "rulings.jsonl"
                          if args.root else None)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for r in out:
            print("  [%s] %s" % (r["verdict"], r["rule_id"]))
            print("         %s" % r["trace_line"] if "trace_line" in r
                  else "         裁决方 %s ｜ 依据 %s ｜ %s"
                  % (r["ruled_by"], r["rule_basis"], r["target"]))
    return 0


def _cmd_rulings(args) -> int:
    base = Path(args.root) if args.root else None
    path = (base / "conflicts" / "rulings.jsonl") if base else rulings_path()
    records = read_rulings(path)
    if not records:
        print("没有裁决记录：%s" % path)
        return 1
    for r in records:
        print("  [%s] %s ｜ 裁决方 %s ｜ 依据 %s"
              % (r.get("verdict"), r.get("conflict_id"),
                 r.get("ruled_by"), r.get("rule_id")))
    print("共 %d 条：%s" % (len(records), path))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m agent.memory.conflict",
        description="P2-5 · 冲突裁决器")
    sub = ap.add_subparsers(dest="cmd")

    p_lad = sub.add_parser("ladder", help="打印裁决序")
    p_lad.set_defaults(func=_cmd_ladder)

    p_adj = sub.add_parser("adjudicate", help="裁决一份冲突文件")
    p_adj.add_argument("--conflicts", required=True,
                       help="冲突文件（JSONL，一行一条冲突）")
    p_adj.add_argument("--root", default=None,
                       help="共享根（默认 $COW_SHARED_ROOT 或 ~/cow）")
    p_adj.add_argument("--record", action="store_true",
                       help="把裁决记录追加到裁决台账")
    p_adj.add_argument("--json", action="store_true")
    p_adj.set_defaults(func=_cmd_adjudicate)

    p_ru = sub.add_parser("rulings", help="打印已落盘的裁决记录")
    p_ru.add_argument("--root", default=None)
    p_ru.set_defaults(func=_cmd_rulings)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
