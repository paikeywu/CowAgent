"""The pre-decide step: the gate's race against the live writer, closed.

**The problem (交办书 · 另外两件小事 · 第 2 条).**

Every round that writes memory produces NEW rows, and a new row's
``body_origin`` starts as ``'unresolved'`` — nobody has judged it yet.
So the assertion "``unresolved == 0``" races the live writer: the
criterion runs, the writer writes, the criterion's own reading is now
out of date.  The gate and the assertion become flaky, and a flaky
gate is a gate nobody trusts.

**Why the flakiness is not the writer's fault.**  A row's origin is
decided by CONTENT (D1/D2), never by the clock — so the decision for
a row that already exists is a pure function of the truth layer.  The
race is therefore not a semantic conflict; it is a SEQUENCE problem:
the criterion must read the world at a moment when no new row has
appeared since it decided.

**The mechanism.**  ``pre_decide`` is the ONE entry point that closes
the race, and it does it in the only order that works:

  1. **decide** every unresolved row (content-based, idempotent);
  2. **re-read** the unresolved count;
  3. **if it is not zero, decide AGAIN** and re-read.

The second pass exists because a row can be written BETWEEN step 1 and
step 2.  The loop terminates because each pass decides the rows that
existed at its start, and the writer's rate is far below the
decision's rate — but more importantly, the loop is bounded, and a
bound that is exceeded is REPORTED, not hidden.

**What it does NOT do.**  It does not lock the writer, and it does not
make the count stable forever — the writer keeps writing.  What it
does is make the count TRUE at the moment the criterion reads it, and
it makes the criterion's reading the SAME reading the gate reports.

**The honest limit.**  The count can still move between the
pre-decide and the criterion's own read.  The pre-decide therefore
REPORTS the count it achieved and WHEN, so a reader can see whether
the number moved.  A gate that says "0 at 12:00:01" and a criterion
that says "0 at 12:00:03" are consistent; a gate that says "0" and a
criterion that says "3" is a race that was reported as a fact.

The second half of the mechanism is on the criterion's side: the
criterion must read the count THROUGH the same function, so there is
one number, not two.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent.memory.decide_origin import (
    decide_row,
    unresolved_rows,
)


#: How many times the pre-decide decides before it gives up.
MAX_ROUNDS = 3


def pre_decide(workspace: Union[str, Path], db: Union[str, Path],
               *, max_rounds: int = MAX_ROUNDS) -> Dict[str, Any]:
    """Decide every unresolved row of ONE library, until the count is 0.

    Returns the report: ``rounds``, ``decided``, ``unresolved_before``,
    ``unresolved_after``, and the per-round readings — so a reader can
    see whether the writer was writing DURING the pre-decide (which is
    the thing the race is about).
    """
    path = Path(db)
    out: Dict[str, Any] = {
        "library": path.parent.parent.parent.name or str(path),
        "db": str(path),
        "rounds": [],
        "decided": 0,
        "unresolved_before": 0,
        "unresolved_after": None,
        "stable": False,
    }
    if not path.is_file():
        out["error"] = "the index does not exist"
        return out

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        for round_no in range(1, max_rounds + 1):
            rows = unresolved_rows(conn)
            round_rec = {"round": round_no, "found": len(rows),
                         "at": time.time(), "decided": 0, "after": None}
            out["rounds"].append(round_rec)
            if round_no == 1:
                out["unresolved_before"] = len(rows)
            if not rows:
                # Nothing to decide — but the READ-BACK still has to
                # happen, so the round reports the count it observed.
                # (Without this the round would carry no "after", and a
                # reader could not tell "nothing to decide" from "the
                # count was never read".)
                round_rec["after"] = conn.execute(
                    "SELECT COUNT(*) FROM chunks "
                    "WHERE body_origin='unresolved'").fetchone()[0]
                break
            decided = 0
            for row in rows:
                verdict = decide_row(workspace, row)
                origin = verdict["origin"]
                pointer = verdict["pointer"]
                if pointer:
                    bp, anchor, bh = pointer
                    cur = conn.execute(
                        "UPDATE chunks SET body_path=?, body_anchor=?, "
                        "body_hash=?, body_origin=? "
                        "WHERE id=? AND body_origin=?",
                        (bp, anchor, bh, origin, row["id"], "unresolved"))
                else:
                    cur = conn.execute(
                        "UPDATE chunks SET body_origin=? "
                        "WHERE id=? AND body_origin=?",
                        (origin, row["id"], "unresolved"))
                if cur.rowcount:
                    decided += 1
            out["decided"] += decided
            round_rec["decided"] = decided
            conn.commit()
            # The READ-BACK: the number the criterion will read.
            left = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"
            ).fetchone()[0]
            round_rec["after"] = left
            if left == 0:
                break
        out["unresolved_after"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"
        ).fetchone()[0]
        out["stable"] = (out["unresolved_after"] == 0)
    except sqlite3.Error as e:
        out["error"] = "sqlite error: %s" % e
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return out


def pre_decide_family(workspace: Union[str, Path],
                       *, max_rounds: int = MAX_ROUNDS) -> Dict[str, Any]:
    """Pre-decide EVERY live library of a workspace.

    This is the entry point the gate calls BEFORE it runs the criteria.
    It is the "流程化入口" the delegation asked for: one command, one
    report, and the criteria that follow read a world in which nobody
    has an undecided row.
    """
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports: List[Dict[str, Any]] = []
    for lib in enumerate_live(root):
        rep = pre_decide(lib["workspace"], lib["db"], max_rounds=max_rounds)
        rep["library"] = lib["name"]
        reports.append(rep)
    before = sum(r.get("unresolved_before", 0) for r in reports)
    after = sum(r.get("unresolved_after", 0) or 0 for r in reports)
    return {
        "workspace": str(root),
        "library_count": len(reports),
        "libraries": reports,
        "unresolved_before": before,
        "unresolved_after": after,
        "decided": sum(r.get("decided", 0) for r in reports),
        "stable": (after == 0),
    }


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    """The report, as text."""
    lines = []
    lines.append("pre-decide: workspace=%s libraries=%s stable=%s"
                 % (report.get("workspace"), report.get("library_count"),
                    report.get("stable")))
    lines.append("unresolved: %s -> %s   decided=%s"
                 % (report.get("unresolved_before"),
                    report.get("unresolved_after"),
                    report.get("decided")))
    for rep in report.get("libraries", []):
        if not rep.get("rounds"):
            continue
        lines.append("  %-22s %s" % (rep.get("library"),
                                     " ".join(
                                         "r%d:%d->%s" % (
                                             r.get("round"), r.get("found"),
                                             r.get("after"))
                                         for r in rep["rounds"])))
        if verbose:
            for r in rep["rounds"]:
                lines.append("      round %s found=%s decided=%s after=%s"
                             % (r.get("round"), r.get("found"),
                                r.get("decided"), r.get("after")))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.msg_pre_decide --workspace <ws>``"""
    import argparse

    ap = argparse.ArgumentParser(
        description="Decide every undecided row before the criteria run, "
                    "so the 'unresolved == 0' assertion does not race the "
                    "live writer.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--max-rounds", type=int, default=MAX_ROUNDS,
                    help="how many times to decide before reporting that "
                         "the count did not settle")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    report = pre_decide_family(args.workspace, max_rounds=args.max_rounds)
    print(format_report(report, verbose=args.verbose))
    return 0 if report.get("stable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
