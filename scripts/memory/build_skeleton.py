#!/usr/bin/env python3
"""Build the live-memory directory skeleton for every agent workspace.

Creates, per workspace root (15 agents from team.json + the shared root):

  memory/facts/                          evidence room (append-only JSONL)
  memory/markdown/events/                truth layer: dated events
  memory/markdown/entities/people/       truth layer: person files
  memory/markdown/entities/relations/    truth layer: relation files
  memory/markdown/letters/               truth layer: dated letters
  memory/markdown/README.md              what this layer is (indexed by design)
  memory/shelf/index.md                  shelf card: 我记得的/我没记住的/你释放的

Idempotent: existing directories and files are left untouched (no overwrite
without --force). Stdlib only.

Usage:
  python scripts/memory/build_skeleton.py [--cow-root ~/cow] [--root DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Tuple

DIRS = (
    "memory/facts",
    "memory/markdown/events",
    "memory/markdown/entities/people",
    "memory/markdown/entities/relations",
    "memory/markdown/letters",
    "memory/shelf",
)

MARKDOWN_README = """# memory/markdown/ — 书架（真源层）

这一层是**记忆本体**，一件事/一个人/一段关系/一封信 = 一个文件。

## 容器（固定枚举，不自造）

| 目录 | 装什么 | 命名 |
|:--|:--|:--|
| `events/YYYY-MM-DD/` | 有始有终的事 | `<slug>--<hash8>.md` |
| `entities/people/` | 一个人的可变档案 | `<slug>--<hash8>.md` |
| `entities/relations/` | 一段关系的演化史 | `<slug>--<hash8>.md` |
| `letters/YYYY-MM-DD/` | 写给未来的信 | `<slug>--<hash8>.md` |

## 三条规矩

1. **永不改写，只追加更正** —— 错了就写更正段，不覆盖原文。
2. **文件名不是身份** —— 真正的 id 在 front-matter 的 `element_id`；
   文件名只负责可读与唯一（slug 先截断，hash8 永不截断）。
3. **命名可被校验** —— `python scripts/memory/validate_names.py --root .`
   必须零例外。

目录卡（给人翻的索引）在旁边的 `memory/shelf/`，是投影，可随时重建。
"""

SHELF_INDEX_TEMPLATE = """---
kind: shelf-index
agent: {agent_id}
generated_at: {now}
generator: build_skeleton/1.0
---

# 记忆书架 · {agent_id}

> 目录卡（投影），真源在 `memory/markdown/` 与 `memory/facts/`。
> 本页由生成器维护，分区内容将在 P0-5（主体自读入口）落地。

## 我记得的

（暂无）

## 我没记住的

（暂无）

## 你释放的

（暂无）
"""


def default_roots(cow_root: Path) -> List[Tuple[str, Path]]:
    """(agent_id, workspace) for every agent in team.json, plus the shared
    root (the default agent's public shelf lives there)."""
    roots: List[Tuple[str, Path]] = []
    team = cow_root / "agents" / "team.json"
    if team.is_file():
        data = json.loads(team.read_text(encoding="utf-8"))
        for entry in data.get("agents", []):
            roots.append((entry["id"], cow_root / "agents" / entry["id"]))
    else:
        agents_dir = cow_root / "agents"
        for d in sorted(agents_dir.iterdir()):
            if d.is_dir():
                roots.append((d.name, d))
    roots.append(("default-root", cow_root))
    return roots


def build(root: Path, agent_id: str, *, force: bool = False) -> List[str]:
    """Create the skeleton under one workspace. Returns human-readable actions."""
    actions: List[str] = []
    for rel in DIRS:
        d = root / rel
        if d.is_dir():
            actions.append(f"exists  {rel}/")
        else:
            d.mkdir(parents=True)
            actions.append(f"created {rel}/")

    readme = root / "memory" / "markdown" / "README.md"
    readme_existed = readme.exists()
    if readme_existed and not force:
        actions.append("exists  memory/markdown/README.md")
    else:
        readme.write_text(MARKDOWN_README, encoding="utf-8", newline="\n")
        actions.append(("forced " if readme_existed else "wrote ")
                       + "memory/markdown/README.md")

    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    index = root / "memory" / "shelf" / "index.md"
    index_existed = index.exists()
    if index_existed and not force:
        actions.append("exists  memory/shelf/index.md")
    else:
        index.write_text(
            SHELF_INDEX_TEMPLATE.format(agent_id=agent_id, now=now),
            encoding="utf-8",
            newline="\n",
        )
        actions.append(("forced " if index_existed else "wrote ")
                       + "memory/shelf/index.md")
    return actions


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cow-root", type=Path, default=Path.home() / "cow")
    ap.add_argument("--root", type=Path,
                    help="build one workspace only (agent id = dir name)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite README.md / shelf/index.md even if present")
    args = ap.parse_args(argv)

    if args.root:
        roots = [(args.root.expanduser().name, args.root.expanduser())]
    else:
        roots = default_roots(args.cow_root.expanduser())

    for agent_id, root in roots:
        print(f"== {agent_id}  ({root})")
        for action in build(root, agent_id, force=args.force):
            print(f"   {action}")
    print(f"== done: {len(roots)} workspaces ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
