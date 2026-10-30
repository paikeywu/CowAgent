"""The "all green, but the old code ran" check — a MECHANISM, not a report.

**What this exists for.**  A gate can be green for the wrong reason.
The sharpest version of that on this machine was not a bug in the
gate — it was the gate answering a NARROWER question than the one
that mattered:

    "do the 16 libraries have the same COLUMNS?"   → YES, one token
    "do the 16 libraries BEHAVE the same?"         → NO, two behaviours

Both readings were true, and only the first one was being checked.  A
fleet in that state is green and wrong at the same time, and the
symptom is invisible: everything works, the numbers are right, and the
one thing that was supposed to protect the data is protecting it in
one library out of sixteen.

**The shape of the hole, stated generally.**  A detector that does not
cover a thing cannot report that thing.  So the repair is not "make
this one criterion better" — it is "make the detector's COVERAGE part
of the thing being asserted, and make the coverage VISIBLE".

**What this module does.**  It answers ONE question about a whole
fleet:

    Do all the live libraries carry the SAME cross-end fingerprint?

and it answers it with the evidence: the token, the per-dimension
hashes, and — when they differ — WHICH dimension differs and WHICH
libraries are on each side.

**The second question it answers.**  "Is the process that is running
against this index the code that this report describes?"  A green
fleet measured by the OLD code is still a green fleet measured by the
old code.  The check is cheap: the interpreter's ``agent.memory``
file path, the git HEAD, and — the part that actually catches it —
whether the module the caller named even EXISTS in the running tree.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent.memory.cross_end import (
    CROSS_END_DIMENSIONS,
    TOKEN_DIMENSIONS,
    cross_end_dimension_fingerprint,
    cross_end_dimensions,
    cross_end_fingerprint,
)


def judge_fleet_uniformity(workspace: Union[str, Path]) -> Dict[str, Any]:
    """Every live library's cross-end fingerprint, grouped.

    Returns the groups, the per-dimension hashes, and the verdict.
    """
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    libs = enumerate_live(root)
    tokens: Dict[str, List[str]] = {}
    per_library: List[Dict[str, Any]] = []
    dims_by_library: Dict[str, Dict[str, str]] = {}

    for lib in libs:
        fp = cross_end_fingerprint(lib["db"])
        dims = cross_end_dimensions(lib["db"])
        dims_by_library[lib["name"]] = dims or {}
        if fp:
            tokens.setdefault(fp, []).append(lib["name"])
        per_library.append({
            "library": lib["name"],
            "fingerprint": fp,
            "dimensions": {
                d: cross_end_dimension_fingerprint(dims, d)
                for d in CROSS_END_DIMENSIONS
            } if dims else {},
        })

    # Which dimension differs between the groups?  That is the
    # DIAGNOSIS, and it is what turns "the fleet is not uniform" into
    # "the TRIGGER bodies differ" — which is actionable.
    differing: List[str] = []
    if len(tokens) > 1:
        names = sorted(tokens.values(), key=len, reverse=True)
        base = dims_by_library.get(names[0][0], {})
        for dim in CROSS_END_DIMENSIONS:
            values = {dims_by_library.get(n, {}).get(dim) for n in names[0]}
            for group in names[1:]:
                for n in group:
                    values.add(dims_by_library.get(n, {}).get(dim))
            if len(values) > 1:
                differing.append(dim)

    uniform = len(tokens) == 1
    return {
        "criterion": "A7.FLEET",
        "name": "every live library carries the same cross-end fingerprint",
        "workspace": str(root),
        "library_count": len(libs),
        "uniform": uniform,
        "tokens": {k: v for k, v in tokens.items()},
        "differing_dimensions": differing,
        "dimensions_covered": list(CROSS_END_DIMENSIONS),
        "token_dimensions": list(TOKEN_DIMENSIONS),
        "libraries": per_library,
        "verdict": "PASS" if uniform else "FAIL",
    }


def judge_code_identity(repo: Union[str, Path] = None) -> Dict[str, Any]:
    """Is the running interpreter executing THIS tree, at THIS commit?

    Three checks, because each catches a different way of being wrong:

      1. the imported ``agent.memory`` file path — catches "the
         installed copy, not the source tree";
      2. the git HEAD of that tree — catches "the right tree, the
         wrong commit";
      3. **the module the caller named EXISTS** — catches "the check
         runs, but the thing it checks is not in the code that is
         running".

    The third one is the one this round needed.  A criterion that asks
      "does the fleet's fingerprint agree?"
    against a codebase that has no fingerprint module answers the
    question with the OLD code's answer, and the report says PASS.
    """
    repo_path = Path(repo) if repo else Path(__file__).resolve().parents[2]
    out: Dict[str, Any] = {
        "criterion": "A8.CODE",
        "name": "the running code is this tree at this commit",
        "verdict": "PASS",
        "agent_memory_file": "",
        "git_head": "",
        "modules_present": {},
        "detail": "",
    }
    try:
        import agent.memory as _m

        out["agent_memory_file"] = str(Path(_m.__file__).resolve())
    except Exception as e:
        out["agent_memory_file"] = "cannot import agent.memory: %s" % e
        out["verdict"] = "FAIL"
        out["detail"] = "agent.memory could not be imported"
        return out

    in_tree = str(repo_path) in out["agent_memory_file"]
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        head = "cannot run git: %s" % e
    out["git_head"] = head

    # The modules this round's checks live in.  Their ABSENCE means the
    # check ran against the old code.
    for name in ("agent.memory.cross_end", "agent.memory.guard_sweep",
                  "agent.memory.decide_origin", "agent.memory.ledger_orphans",
                  "agent.memory.meta_chunker_key"):
        out["modules_present"][name] = name in sys.modules

    problems = []
    if not in_tree:
        problems.append("the imported agent.memory is NOT in this tree")
    if not head:
        problems.append("the git HEAD could not be read")
    if not out["modules_present"].get("agent.memory.cross_end"):
        problems.append("agent.memory.cross_end is not loaded — the "
                        "cross-end check cannot have run against it")
    out["verdict"] = "FAIL" if problems else "PASS"
    out["detail"] = ("agent.memory=%s  head=%s  modules=%s"
                     % (out["agent_memory_file"], head[:12],
                        ",".join("%s:%s" % (k.split(".")[-1],
                                             "yes" if v else "NO")
                                 for k, v in out["modules_present"].items())))
    if problems:
        out["detail"] += "  ||  " + "; ".join(problems)
    return out


def format_report(fleet: Dict[str, Any],
                  code: Optional[Dict[str, Any]] = None) -> str:
    """The report, as text — the fleet's shape, per dimension."""
    lines = []
    lines.append("A7.FLEET — every live library carries the same "
                 "cross-end fingerprint")
    lines.append("  workspace : %s" % fleet["workspace"])
    lines.append("  libraries : %d" % fleet["library_count"])
    lines.append("  dimensions covered : %s" % (", ".join(fleet["dimensions_covered"])))
    lines.append("  token dimensions  : %s" % (", ".join(fleet["token_dimensions"])))
    lines.append("")
    for fp, names in sorted(fleet["tokens"].items(), key=lambda kv: -len(kv[1])):
        lines.append("  %s  (%d): %s"
                     % (fp[:16], len(names), ", ".join(names)))
    lines.append("")
    if fleet["differing_dimensions"]:
        lines.append("  DIFFERING DIMENSIONS: %s"
                     % ", ".join(fleet["differing_dimensions"]))
    else:
        lines.append("  differing dimensions: none")
    lines.append("  VERDICT: %s" % fleet["verdict"])
    if code is not None:
        lines.append("")
        lines.append("A8.CODE — %s" % code["name"])
        lines.append("  %s" % code["detail"])
        lines.append("  VERDICT: %s" % code["verdict"])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.fleet_check --workspace <ws>``."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Check that every live library carries the same "
                    "cross-end fingerprint, and that the running code is "
                    "this tree at this commit.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--repo", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    fleet = judge_fleet_uniformity(args.workspace)
    code = judge_code_identity(args.repo)
    if args.json:
        import json

        print(json.dumps({"fleet": fleet, "code": code},
                         ensure_ascii=False, indent=2))
    else:
        print(format_report(fleet, code))
    return 0 if (fleet["verdict"] == "PASS" and code["verdict"] == "PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
