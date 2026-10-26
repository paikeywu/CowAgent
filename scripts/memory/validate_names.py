#!/usr/bin/env python3
"""Validate every filename under memory/ against the live-memory naming rules.

Usage:
  python scripts/memory/validate_names.py --root <workspace>     # one agent
  python scripts/memory/validate_names.py --all [--cow-root ~/cow]
  python scripts/memory/validate_names.py --selfcheck            # proves itself

Layer rules (design 06 §1.3.3 / delegation P0-2), by path relative to the
workspace root:

  memory/markdown/**   TRUTH layer: <slug>--<hash8>.md (naming.NAME_RE),
                       slug <=40 UTF-8 bytes, dated containers (events/,
                       letters/) must sit under YYYY-MM-DD/, entity
                       containers must not. Exempt: markdown/README.md.
  memory/facts/**      EVIDENCE layer: sess_<8hex>.jsonl.
  memory/shelf/**      PROJECTION: exempt from the pattern (human-readable
                       names are the point), byte budget still applies.
  everything else      LEGACY (daily files, dreams/, evolution/, users/,
                       long-term/, .evolution_backups/): exempt from the
                       pattern, byte budget still applies.

Every file, whatever the layer, must satisfy: whole path <=100 UTF-8 bytes
and every component <=255 bytes.

Exit code 0 = clean, 1 = violations found, 2 = usage/setup error.

--selfcheck builds a throwaway tree with planted violations and asserts the
validator catches every one of them (and clears the clean controls): a
checker that has never been seen to fail proves nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.memory.naming import (  # noqa: E402
    CONTAINERS_DATED,
    CONTAINERS_UNDATED,
    check_name,
    check_relpath,
)

FACTS_NAME_RE = re.compile(r"^sess_[0-9a-f]{8}\.jsonl$")
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Truth-layer files that are allowed to break the slug--hash8 pattern.
PATTERN_ALLOWLIST = {"memory/markdown/README.md"}


def validate_file(relpath: str) -> List[str]:
    """One workspace-relative POSIX path -> list of violations (empty = clean)."""
    problems: List[str] = []
    budget = check_relpath(relpath)
    if budget:
        problems.append(f"budget: {budget}")
    name = relpath.rsplit("/", 1)[-1]

    if relpath.startswith("memory/markdown/"):
        if relpath in PATTERN_ALLOWLIST:
            return problems
        bad_name = check_name(name)
        if bad_name:
            problems.append(f"name: {bad_name}")
        # Layer structure: dated containers need a YYYY-MM-DD parent; entity
        # containers must not have one; anything else is not a container.
        parts = relpath.split("/")
        if len(parts) <= 3:
            problems.append("structure: file sits directly under markdown/ "
                            "instead of a container")
        else:
            top = parts[2]
            parent = parts[-2]
            if top in CONTAINERS_DATED:
                if not DATE_DIR_RE.match(parent):
                    problems.append(
                        f"structure: {top}/ files must sit under a YYYY-MM-DD "
                        f"directory (parent is {parent!r})"
                    )
            elif top == "entities":
                if DATE_DIR_RE.match(parent):
                    problems.append(
                        "structure: entity files span time and must not sit "
                        "under a date directory"
                    )
            else:
                problems.append(
                    f"structure: unknown markdown container {top!r} "
                    f"(allowed: {CONTAINERS_DATED + CONTAINERS_UNDATED})"
                )
    elif relpath.startswith("memory/facts/"):
        if not FACTS_NAME_RE.match(name):
            problems.append("name: facts files must be sess_<8hex>.jsonl")
    # shelf/ and legacy areas: byte budget only (checked above).
    return problems


def iter_memory_files(root: Path):
    mem = root / "memory"
    if not mem.is_dir():
        return
    for p in sorted(mem.rglob("*")):
        if p.is_file():
            yield p


def validate_root(root: Path) -> List[Tuple[str, List[str]]]:
    """All violations under one workspace: [(relpath, [problems])]."""
    out: List[Tuple[str, List[str]]] = []
    for p in iter_memory_files(root):
        rel = p.relative_to(root).as_posix()
        problems = validate_file(rel)
        if problems:
            out.append((rel, problems))
    return out


def default_roots(cow_root: Path) -> List[Path]:
    """All agent workspaces plus the shared/default root."""
    roots: List[Path] = []
    team = cow_root / "agents" / "team.json"
    if team.is_file():
        data = json.loads(team.read_text(encoding="utf-8"))
        for entry in data.get("agents", []):
            roots.append(cow_root / "agents" / entry["id"])
    else:
        agents_dir = cow_root / "agents"
        if agents_dir.is_dir():
            roots.extend(d for d in sorted(agents_dir.iterdir()) if d.is_dir())
    roots.append(cow_root)
    return roots


def selfcheck() -> int:
    """Plant violations in a temp tree; every one must be caught."""
    planted = {
        # 1. over-long slug (41 ASCII bytes -> over the 40-byte slug budget)
        "memory/markdown/events/2026-10-27/"
        + "a" * 41 + "--a1b2c3d4.md": "slug",
        # 2. CJK over-budget path (CJK costs 3 bytes/char)
        "memory/markdown/events/2026-10-27/"
        + "记" * 30 + "--a1b2c3d4.md": "budget",
        # 3. missing hash
        "memory/markdown/events/2026-10-27/plain-name.md": "name",
        # 4. bad hash (not 8 lowercase hex)
        "memory/markdown/events/2026-10-27/ok--A1B2C3D4.md": "name",
        # 5. facts file with wrong name
        "memory/facts/2026-10-27/not-a-session.jsonl": "sess_",
        # 6. events file outside a date directory
        "memory/markdown/events/undated--a1b2c3d4.md": "structure",
        # 7. unknown container
        "memory/markdown/feels/2026-10-27/x--a1b2c3d4.md": "structure",
    }
    clean_controls = [
        "memory/markdown/events/2026-10-27/遗忘伦理确立--b2c3d4e5.md",
        "memory/markdown/entities/people/peiqi--e5f6a7b8.md",
        "memory/markdown/entities/relations/peiqi--agent-mvnerelm--f6a7b8c9.md",
        "memory/markdown/letters/2026-10-27/letter--d4e5f6a7-2.md",
        "memory/markdown/README.md",
        "memory/facts/2026-10-27/sess_7f3a91c2.jsonl",
        "memory/shelf/index.md",
        "memory/shelf/by-tag/记忆主权.md",
        "memory/2026-10-27.md",                    # legacy daily: exempt
        "memory/long-term/users.json",             # legacy: exempt
    ]
    with tempfile.TemporaryDirectory(prefix="namecheck-") as td:
        root = Path(td)
        for rel in list(planted) + clean_controls:
            fp = root / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text("x", encoding="utf-8")

        failures: List[str] = []
        for rel, expect in planted.items():
            problems = validate_file(rel)
            if not problems:
                failures.append(f"NOT CAUGHT: {rel} (expected {expect})")
            elif not any(expect in p for p in problems):
                failures.append(
                    f"WRONG RULE: {rel} expected {expect!r}, got {problems}"
                )
        for rel in clean_controls:
            problems = validate_file(rel)
            if problems:
                failures.append(f"FALSE POSITIVE: {rel} -> {problems}")

        if failures:
            print("SELFCHECK FAILED:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print(
            f"SELFCHECK PASS: {len(planted)} planted violations all caught "
            f"with the expected rule; {len(clean_controls)} clean controls "
            f"all passed."
        )
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, help="one workspace root")
    ap.add_argument("--all", action="store_true",
                    help="all agents from team.json plus the shared root")
    ap.add_argument("--cow-root", type=Path,
                    default=Path.home() / "cow",
                    help="cow root for --all (default: ~/cow)")
    ap.add_argument("--selfcheck", "--baseline", dest="selfcheck",
                    action="store_true",
                    help="plant violations in a temp tree and prove they are "
                         "caught (checker validates itself)")
    args = ap.parse_args(argv)

    if args.selfcheck:
        return selfcheck()

    if args.all:
        roots = default_roots(args.cow_root.expanduser())
    elif args.root:
        roots = [args.root.expanduser()]
    else:
        ap.error("give --root <dir>, --all, or --selfcheck")

    total_files = 0
    total_bad = 0
    for root in roots:
        problems = validate_root(root)
        files = sum(1 for _ in iter_memory_files(root))
        total_files += files
        total_bad += len(problems)
        status = "CLEAN" if not problems else f"{len(problems)} VIOLATIONS"
        print(f"[{status}] {root}  ({files} files scanned)")
        for rel, probs in problems:
            for p in probs:
                print(f"    {rel}: {p}")
    print(f"== {total_files} files, {total_bad} violating ==")
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
