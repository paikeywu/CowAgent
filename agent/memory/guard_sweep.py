"""The fleet sweep — the mechanism that reaches EVERY live library.

**Why this module exists (P1-阶段三 第八轮 · DR-1).**

The body-column guard was installed in exactly TWO places:

  * ``MemoryStorage.__init__`` — which opens ONE library, the one the
    current process happens to be talking to; and
  * ``agent.memory.migrate.step_m4`` — the migration tool.

So a change to the guard's DEFINITION reached only the libraries that
were OPENED by the new build.  Measured on this machine: the S3-L
change (restore ``text_state`` from the certification LEDGER instead of
the guard's keep table) reached **2 of 16** libraries — the two the new
build had opened — and the other **14** kept the old body.  Their
trigger NAME sets were identical, so every existing fingerprint said
"consistent", and the fleet was quietly running two different guards.

**That is the mechanism gap, and it is the shape of the gap, not a
one-off failure.**  A sweep that is invoked from one place, over a list
that is derived from a roster, will always reach the libraries that
place happens to know about.  The fix is therefore:

  * a sweep that DISCOVERS every live library by PATH SHAPE (never a
    ``*.db`` glob — a glob would also pick up the backups this tool
    writes and the scratch copies in ``tmp/``);
  * a sweep that is IDEMPOTENT and RE-RUNNABLE (running it twice
    changes nothing the second time);
  * a sweep that PROVES what it did, per library, by printing that
    library's trigger-text fingerprint — so "which libraries carry the
    new definition" is visible at a glance rather than established by
    somebody diffing SQL by hand.

**The order matters, and it is not cosmetic.**  SQLite fires a table's
triggers in ``sqlite_master`` rowid order, and a re-created trigger
lands at the END of it.  The body guard's AFTER INSERT restore trigger
updates the protected columns of the row the writer just inserted, and
that update fires the FTS AFTER UPDATE trigger, whose ``delete``
command reads the content table.  With the guard BEHIND the FTS
triggers, the FTS index holds the new text while the content table
already holds the replaced one — the tokens disagree, and SQLite reports
``database disk image is malformed``.  MEASURED on this machine: after
a guard repair, ``chunks_body_keep_st_ai`` sat at rowid 16 — BEHIND
the FTS triggers at 8..13.  So the sweep RE-ORDERS after every repair,
by re-creating the FTS sync triggers (which puts them at the end,
behind the guard again).

This module is the ONE place that does it, so the next person does not
have to rediscover it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

#: The path shapes a live library can have, relative to a workspace root.
#:
#:   <root>/memory/long-term/index.db              — the shared root
#:   <root>/agents/<id>/memory/long-term/index.db  — one per agent
#:
#: and NOTHING else.  A ``*.db`` glob would also match the ``*.p1-bak.db``
#: backups the migration writes and the scratch copies in ``tmp/``.
LIVE_DB_RELPATH = "memory/long-term/index.db"


def discover_live_libraries(workspace: Union[str, Path]
                            ) -> List[Dict[str, Any]]:
    """Every live library of a workspace, by PATH SHAPE.

    The rule is the same one the migration tool uses
    (``agent.memory.migrate.enumerate_live``) and it is IMPORTED rather
    than re-spelled — a second spelling of "which libraries exist" is
    how two sides end up disagreeing about what they are looking at.

    Returns ``[{"name": ..., "db": Path, "workspace": Path}, ...]``.
    """
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    out: List[Dict[str, Any]] = []
    for lib in enumerate_live(root):
        out.append({
            "name": lib["name"],
            "db": Path(lib["db"]),
            "workspace": Path(lib["workspace"]),
        })
    return out


def _reorder_fts_triggers_behind_guard(conn: sqlite3.Connection) -> bool:
    """Put the body guard's triggers AHEAD of the FTS sync triggers.

    True when it acted.

    **The rule, and why it is load-bearing.**  SQLite runs a table's
    triggers in ``sqlite_master`` rowid order.  The body guard's AFTER
    INSERT restore trigger updates the protected columns of the row the
    writer just inserted; that update fires the FTS AFTER UPDATE trigger,
    whose ``delete`` command reads the content table.  When the guard
    runs AFTER the FTS triggers, the FTS index already holds the row
    while the content table already holds the REPLACED one — the tokens
    disagree, and SQLite reports ``database disk image is malformed``.

    **How the order is fixed — and the mistake this module first made.**
    A re-created trigger lands at the END of ``sqlite_master``.  So the
    obvious move ("re-create the FTS triggers so they end up behind the
    guard") does NOT work when the guard itself was just re-created:
    the guard is ALREADY at the end, so re-creating the FTS triggers
    moves THEM to the end too — and the guard stays in front of the
    OLD FTS rows... no: it moves them behind the guard, but the guard
    is behind the FTS rows it was supposed to precede.  Measured on
    this machine: after the first sweep, 14 libraries had the FTS
    triggers at rowid 0..5 and the body guard at 8..16 — the guard was
    BEHIND, which is the failure this function exists to prevent.

    **The correct move is to re-create the GUARD's triggers.**  They
    land at the end of ``sqlite_master``, and the FTS triggers (which
    were not touched) stay where they are — so the guard ends up behind
    them.  That is also wrong.

    So neither trigger group can be "moved" by re-creating it alone:
    what matters is the RELATIVE order, and re-creating one group puts
    it LAST.  The rule that actually holds the invariant is the one the
    storage layer uses: **re-create the FTS triggers LAST** — i.e.
    drop and re-create the FTS sync triggers, and re-create the guard's
    triggers BEFORE them.  Concretely, in ONE transaction:

        1. drop the FTS sync triggers and the body guard's triggers;
        2. re-create the body guard's triggers;
        3. re-create the FTS sync triggers.

    The result is: guard first, FTS second — and that is the order the
    legacy DELETE-then-INSERT shape needs.

    **The index guard (``chunks_guard_bi`` / ``chunks_guard_bu``) is
    NOT an FTS trigger** and must not be treated as one: it is a
    visibility guard with its own ordering requirements, and the first
    version of this function swept it into the "FTS" group by accident
    (it only excluded the body guard's names).  It is left exactly
    where it is.
    """
    body_guard_names = {
        "chunks_body_keep_bi", "chunks_body_keep_bu", "chunks_body_keep_bd",
        "chunks_body_keep_bi_ai", "chunks_body_keep_st_ai",
        "files_body_keep_bu", "files_body_keep_bi", "files_body_keep_bd",
        "files_body_keep_bi_ai",
    }
    # The FTS sync triggers — the ones that mirror the content table
    # into the FTS index.  The index guard is NOT one of them.
    fts_names = {
        "chunks_ai", "chunks_ad", "chunks_au",
        "chunks_trigram_ai", "chunks_trigram_ad", "chunks_trigram_au",
    }
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND sql IS NOT NULL ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return False
    if not rows:
        return False
    stored = {r[0]: r[1] for r in rows}
    order = [r[0] for r in rows]

    guard_rows = [n for n in order if n in body_guard_names]
    fts_rows = [n for n in order if n in fts_names]
    if not guard_rows or not fts_rows:
        return False
    if max(order.index(n) for n in guard_rows) < \
            min(order.index(n) for n in fts_rows):
        return False  # already in the right order

    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for name in fts_rows + guard_rows:
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        # The GUARD first, then the FTS triggers: a re-created trigger
        # lands at the end, so this order is the one that ends up with
        # the guard ahead of the FTS triggers.
        for name in guard_rows:
            conn.execute(stored[name])
        for name in fts_rows:
            conn.execute(stored[name])
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return False
    return True


def install_body_guard_on(db_path: Union[str, Path]) -> Dict[str, Any]:
    """Make ONE library's body guard the current definition.

    Returns a report with the status (``present`` / ``installed`` /
    ``repaired`` / ``failed``) AND the trigger-text fingerprint BEFORE
    and AFTER — so the change is measurable, not asserted.
    """
    from agent.memory.body_guard import ensure_guard
    from agent.memory.cross_end import (
        cross_end_dimension_fingerprint,
        cross_end_dimensions,
    )

    path = Path(db_path)
    out: Dict[str, Any] = {"db": str(path), "status": "failed",
                            "reason": ""}
    if not path.is_file():
        out["reason"] = "the index does not exist"
        return out

    before = cross_end_dimensions(path)
    out["trigger_fp_before"] = (
        cross_end_dimension_fingerprint(before, "triggers")
        if before else "<no-index>")

    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as e:
        out["reason"] = "cannot open: %s" % e
        return out
    try:
        result = ensure_guard(conn)
        out["status"] = result.get("status", "failed")
        out["reason"] = result.get("reason", "")
        # **The ORDER is enforced for EVERY library, not only the ones
        # whose definition changed.**
        #
        # MEASURED on this machine: ROOT and agent-mvnewfyb carried
        # ``chunks_body_keep_st_ai`` at rowid 16 — BEHIND the FTS
        # triggers at rowid 8..13 — because the S3-L round repaired
        # their definition and the re-create moved the trigger to the
        # end, and nobody noticed.  Their definition was already
        # current, so a sweep that only reorders after a repair would
        # have left them in the wrong order forever.
        #
        # The order is the load-bearing invariant (see
        # :func:`_reorder_fts_triggers_behind_guard`), and it is a
        # property of the LIBRARY, not of the repair.
        out["fts_reordered"] = _reorder_fts_triggers_behind_guard(conn)
        conn.commit()
    except Exception as e:
        out["status"] = "failed"
        out["reason"] = "%s: %s" % (type(e).__name__, e)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    after = cross_end_dimensions(path)
    out["trigger_fp_after"] = (
        cross_end_dimension_fingerprint(after, "triggers")
        if after else "<no-index>")
    out["trigger_fp_changed"] = out["trigger_fp_before"] != out["trigger_fp_after"]
    return out


def sweep_body_guard(workspace: Union[str, Path],
                     dry_run: bool = False) -> Dict[str, Any]:
    """Bring EVERY live library's body guard up to the current definition.

    The sweep is the MECHANISM: it discovers every live library by path
    shape, repairs what is stale, leaves what is current alone, and
    PRINTS the per-library trigger-text fingerprint so the result is
    self-evident.

    ``dry_run=True`` measures and changes nothing.
    """
    libs = discover_live_libraries(workspace)
    reports: List[Dict[str, Any]] = []
    for lib in libs:
        if dry_run:
            from agent.memory.cross_end import (
                cross_end_dimension_fingerprint,
                cross_end_dimensions,
            )
            dims = cross_end_dimensions(lib["db"])
            reports.append({
                "library": lib["name"],
                "db": str(lib["db"]),
                "status": "would-repair" if _needs_repair(lib["db"]) else "present",
                "trigger_fp": (cross_end_dimension_fingerprint(dims, "triggers")
                               if dims else "<no-index>"),
            })
            continue
        res = install_body_guard_on(lib["db"])
        res["library"] = lib["name"]
        reports.append(res)

    statuses = {}
    for r in reports:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    fps = {}
    for r in reports:
        fps.setdefault(r.get("trigger_fp_after") or r.get("trigger_fp"), []).append(
            r["library"])
    return {
        "workspace": str(Path(workspace)),
        "dry_run": bool(dry_run),
        "library_count": len(reports),
        "libraries": reports,
        "statuses": statuses,
        "trigger_fingerprint_groups": fps,
        "uniform": len(fps) == 1,
    }


def _needs_repair(db_path: Union[str, Path]) -> bool:
    """Whether this library's body guard is NOT the current definition."""
    from agent.memory.body_guard import is_installed

    path = Path(db_path)
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        return False
    try:
        return not is_installed(conn)
    except Exception:
        return True
    finally:
        conn.close()


def format_sweep_report(report: Dict[str, Any]) -> str:
    """The sweep's report, as text — one line per library.

    The per-library trigger fingerprint is the SELF-PROOF: after the
    sweep, the reader can see that every library carries the SAME
    definition without comparing a single trigger body by hand.
    """
    lines = []
    lines.append("body-guard sweep: workspace=%s libraries=%d dry_run=%s"
                 % (report.get("workspace"), report.get("library_count"),
                    report.get("dry_run")))
    lines.append("")
    lines.append("%-24s %-10s %-18s %s"
                 % ("library", "status", "trigger-fp", "note"))
    lines.append("-" * 96)
    for r in report.get("libraries", []):
        fp = r.get("trigger_fp_after") or r.get("trigger_fp") or "-"
        note = ""
        if r.get("reason"):
            note = r["reason"]
        elif r.get("trigger_fp_changed"):
            note = "definition changed"
        elif r.get("fts_reordered"):
            note = "FTS triggers re-ordered behind the guard"
        lines.append("%-24s %-10s %-18s %s"
                     % (r.get("library"), r.get("status"), fp, note))
    lines.append("")
    lines.append("statuses: %s" % (report.get("statuses") or {}))
    groups = report.get("trigger_fingerprint_groups") or {}
    lines.append("trigger fingerprint groups: %d" % len(groups))
    for fp, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines.append("  %s  (%d): %s" % (fp, len(names), ", ".join(names)))
    lines.append("UNIFORM: %s" % ("yes" if report.get("uniform") else "NO"))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.guard_sweep --workspace <ws>``."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Bring every live library's body-column guard up to "
                    "the current definition, and print each library's "
                    "trigger-text fingerprint as the proof.")
    ap.add_argument("--workspace", required=True,
                    help="the workspace root (C:\\Users\\User\\cow)")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure everything, change nothing")
    args = ap.parse_args(argv)

    report = sweep_body_guard(args.workspace, dry_run=args.dry_run)
    print(format_sweep_report(report))
    failed = [r for r in report.get("libraries", [])
              if r.get("status") == "failed"]
    if failed:
        print("\n%d library/libraries could not be repaired:" % len(failed))
        for r in failed:
            print("  %s: %s" % (r.get("library"), r.get("reason")))
        return 1
    if not report.get("uniform"):
        print("\nthe fleet is NOT uniform: %d distinct trigger definitions."
              % len(report.get("trigger_fingerprint_groups") or {}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
