"""The write path's own decision: a fresh row must not stay undecided.

**What this is.**  ``chunks.body_origin`` has four legal values and
one of them — ``'unresolved'`` — means "nobody has decided where this
row's body comes from".  It is the DEFAULT of the column, so a writer
that does not carry the column writes it by omission.  A row that
keeps it forever is a TODO nobody owns: the certifier will not
certify it (it only reaches rows that carry a pointer), the guard has
nothing to restore, and it reads back as "the writer did not decide".

**The hole this closes.**  The current code CANNOT write
``'unresolved'`` — ``body_pointer.four_columns_for_chunk`` returns
``'file'`` / ``'orphan'`` / ``'blob'`` and nothing else, and both
writers (``manager.sync`` / ``manager.add_memory``) go through it.
But a row can still arrive with ``'unresolved'`` from a writer that
does not know the column: **the old desktop build** (built
2026-10-13), whose ``INSERT OR REPLACE INTO chunks`` names twelve
columns and whose ``INSERT OR REPLACE INTO files`` names six.
Measured on a copy of the live ROOT library: the old build's SQL
produces EXACTLY the live shape — ``body_path``/``body_anchor``/
``body_hash`` all NULL, ``text_state='projection'``,
``body_origin='unresolved'``, and a ``files`` row whose
``chunk_count``/``chunker_version`` are both NULL.

⇒ **The write-path fix is a post-write decision, not a
pre-write guard.**  Whatever wrote the row, the writer's own path
decides the row's origin immediately afterwards, by CONTENT (D1/D2),
and fills the pointer the decision implies.  That is what
``agent.memory.decide_origin`` already does — this module is the
hook that makes the write path CALL it, so the decision happens on
every write instead of only when somebody remembers to run the
pre-decide.

**Why the decision rides on the write.**  The decision reads the TRUTH
LAYER, so it must run AFTER the truth file is complete — and the
truth file is written by the caller (the knowledge skill writes the
page, then the index is updated).  Deciding inside the write would
race the file.  So the decision goes right after the write, next to
the certification that already rides there (``_certify_after_write``).

**What it does NOT do.**  It never touches ``text_state``: the state
is the certification's to set, and a row may legitimately be
``'mirror'`` AND ``'orphan'``.  It never deletes a row.  And it never
raises into the write path: a failure to decide is a LOGGED fact, not
a failed write — the write already succeeded, and refusing to report
it would hide the debt rather than record it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent.memory.decide_origin import decide_row, unresolved_rows


def decide_after_write(workspace: Union[str, Path],
                       db: Union[str, Path],
                       *,
                       max_rounds: int = 2,
                       ) -> Dict[str, Any]:
    """Decide every undecided row of ONE library, right after a write.

    Returns the report: ``unresolved_before``, ``unresolved_after``,
    ``decided``, ``pointer_filled``, and the per-row detail — so the
    debt is recorded row by row, not just in aggregate.

    The loop is bounded, and a bound that is exceeded is REPORTED, not
    hidden: a concurrent writer can add a row between the read and the
    write, and the second pass catches it.  If the count still does
    not settle, the remaining rows are named.
    """
    path = Path(db)
    out: Dict[str, Any] = {
        "db": str(path),
        "unresolved_before": 0,
        "unresolved_after": None,
        "decided": 0,
        "pointer_filled": 0,
        "rounds": 0,
        "rows": [],
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
            out["rounds"] = round_no
            if round_no == 1:
                out["unresolved_before"] = len(rows)
            if not rows:
                break
            for row in rows:
                verdict = decide_row(workspace, row)
                origin = verdict["origin"]
                pointer = verdict["pointer"]
                if pointer:
                    bp, anchor, bh = pointer
                    cur = conn.execute(
                        "UPDATE chunks SET body_path=?, body_anchor=?, "
                        "body_hash=?, body_origin=? "
                        "WHERE id=? AND body_origin='unresolved'",
                        (bp, anchor, bh, origin, row["id"]))
                else:
                    cur = conn.execute(
                        "UPDATE chunks SET body_origin=? "
                        "WHERE id=? AND body_origin='unresolved'",
                        (origin, row["id"]))
                if cur.rowcount:
                    out["decided"] += 1
                    if pointer:
                        out["pointer_filled"] += 1
                out["rows"].append({
                    "id": row["id"],
                    "path": row["path"],
                    "origin": origin,
                    "reason": verdict["reason"],
                    "pointer": bool(pointer),
                })
            conn.commit()
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


def decide_after_write_family(workspace: Union[str, Path],
                              *,
                              max_rounds: int = 2) -> Dict[str, Any]:
    """Decide the undecided rows of EVERY live library of a workspace."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports: List[Dict[str, Any]] = []
    for lib in enumerate_live(root):
        rep = decide_after_write(lib["workspace"], lib["db"],
                                  max_rounds=max_rounds)
        rep["library"] = lib["name"]
        reports.append(rep)
    return {
        "workspace": str(root),
        "library_count": len(reports),
        "libraries": reports,
        "unresolved_before": sum(r.get("unresolved_before", 0) for r in reports),
        "unresolved_after": sum(r.get("unresolved_after", 0) or 0
                                for r in reports),
        "decided": sum(r.get("decided", 0) for r in reports),
        "pointer_filled": sum(r.get("pointer_filled", 0) for r in reports),
        "stable": all(r.get("stable") for r in reports),
    }


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    lines = []
    lines.append("write-path decision: workspace=%s libraries=%s stable=%s"
                 % (report.get("workspace"), report.get("library_count"),
                    report.get("stable")))
    lines.append("unresolved: %s -> %s   decided=%s (pointer filled=%s)"
                 % (report.get("unresolved_before"),
                    report.get("unresolved_after"),
                    report.get("decided"), report.get("pointer_filled")))
    for rep in report.get("libraries", []):
        if not rep.get("unresolved_before"):
            continue
        lines.append("--- %s : %d undecided row(s) ---"
                     % (rep.get("library"), rep.get("unresolved_before")))
        for row in rep.get("rows", []):
            lines.append("    %-12s %-52s -> %-8s %-22s ptr=%s"
                         % (row["id"][:12], (row["path"] or "")[:52],
                            row["origin"], row["reason"], row["pointer"]))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.write_decide --workspace <ws>``."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Decide the origin of every row that still says "
                    "'unresolved' — the same decision the write path "
                    "makes, run on demand.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    report = decide_after_write_family(args.workspace)
    print(format_report(report, verbose=args.verbose))
    return 0 if report.get("stable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
