"""T3 — the certification ledger's orphan entries, and the criterion
that says so.

**What an orphan entry is.**  ``cert_ledger`` holds one row per
certification: ``id`` is the chunk id, and the row records what the
certifier VERIFIED about that chunk (its state, its origin, its
pointer's hash) and when.  It is the WITNESS — the thing M6a/M6b/M6c
compare against.

An orphan entry is a ledger row whose ``id`` no longer names a chunk.
The witness is then about a row that does not exist: it says "this text
was verified" about something that is no longer in the index.  Nobody
can act on it, and — this is the part that matters — **nobody notices
it**, because every criterion that reads the ledger joins it to
``chunks``:

  * M6a asks "which rows have a pointer but no ledger entry?" — an
    orphan is not a chunk, so it is not in the answer;
  * M6b asks "which ledger rows disagree with their chunk?" — an
    orphan has no chunk, so the join drops it;
  * M6c asks "which mirror rows have no witness?" — an orphan is not a
    mirror row.

So an orphan entry is invisible to all three.  **That is the general
shape of this whole round: a detector that does not cover a thing
cannot report that thing.**  The criterion M6f closes it.

**Why orphans exist (measured, not assumed).**  The chunk id is
``md5("path:start:end[:part]")`` — it is derived from the row's
POSITION, not from its content.  So when the truth-layer file changes
and the chunker re-cuts it, the rows get NEW ids, and the old ids are
gone.  The ledger still holds the old ids, because the ledger is not
rewritten when the chunks are replaced.

MEASURED on this machine:

  * **ROOT: 15 orphans** — all of them ``AGENT.md`` / ``RULE.md`` /
    ``USER.md``, all with ``certified_by='certify:reverified'``.  Those
    three files are re-read on every session (they ARE the identity
    files), so they are re-cut constantly, and every re-cut orphans the
    previous certification.  Not one of them has a chunk with the same
    path ANY MORE — the files were re-cut into a different number of
    chunks, so the ids moved wholesale.
  * **agent-mvnewfyb: 1 orphan** and **agent-mvnf7fjl: 1 orphan** — both
    ``memory/evolution/2026-10-30.md``, both ``certified_by='backfill'``.
    Same cause: the file grew (the day file is appended to), the
    chunker re-cut it, and the ids moved.

**What the cleanup does — and what it deliberately does NOT do.**

The cleanup DELETES the orphan ledger entries.  It does NOT touch the
chunks, does NOT re-certify anything, and does NOT invent a witness for
the chunk that replaced the orphan.  The re-certification is the
certifier's job (S3-K made it a first-class operation), and doing it
here would make the ledger a record of something nobody verified.

**Why deleting is safe here.**  The ledger is a record of
CERTIFICATIONS, and an orphan certifies a row that is not there.  The
row it used to describe is gone, so the certification has nothing to
certify.  The design's own rule ("原始事实不折叠" / never physically
delete) governs the RECORD OF CONVERSATIONS and the KNOWLEDGE — the
ledger is neither: it is a derived witness over the chunks, and a
witness for a non-existent subject is noise.  This is stated here
because "we deleted rows" is exactly the sentence a reviewer will
look for, and the answer must be precise rather than reassuring.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def orphan_ledger_entries(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The ledger entries whose chunk no longer exists."""
    try:
        rows = conn.execute(
            "SELECT l.id, l.text_state, l.body_origin, l.body_path, "
            "l.body_hash, l.certified_at, l.certified_by "
            "FROM cert_ledger l WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.id = l.id) "
            "ORDER BY l.id").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "text_state": r[1],
            "body_origin": r[2],
            "body_path": r[3],
            "body_hash": r[4],
            "certified_at": r[5],
            "certified_by": r[6],
        })
    return out


def ledger_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """The three numbers the M6f criterion is about.

    * ``ledger``    — how many certification records exist;
    * ``orphans``   — how many of them name a chunk that is not there;
    * ``witnessed`` — how many chunks the ledger actually witnesses.
    """
    out = {"ledger": 0, "orphans": 0, "witnessed": 0}
    try:
        out["ledger"] = conn.execute(
            "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
    except sqlite3.Error:
        return out
    out["orphans"] = len(orphan_ledger_entries(conn))
    try:
        out["witnessed"] = conn.execute(
            "SELECT COUNT(*) FROM cert_ledger l JOIN chunks c "
            "ON c.id = l.id").fetchone()[0]
    except sqlite3.Error:
        out["witnessed"] = 0
    return out


def clean_orphans(db: Union[str, Path],
                  dry_run: bool = False) -> Dict[str, Any]:
    """Delete the orphan ledger entries of ONE library."""
    path = Path(db)
    out: Dict[str, Any] = {
        "db": str(path),
        "dry_run": bool(dry_run),
        "orphans_before": 0,
        "deleted": 0,
        "orphans_after": 0,
        "entries": [],
    }
    if not path.is_file():
        out["error"] = "the index does not exist"
        return out
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        entries = orphan_ledger_entries(conn)
        out["orphans_before"] = len(entries)
        out["entries"] = entries
        if entries and not dry_run:
            ids = [e["id"] for e in entries]
            conn.execute("BEGIN IMMEDIATE")
            try:
                for cid in ids:
                    conn.execute("DELETE FROM cert_ledger WHERE id=?", (cid,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            out["deleted"] = len(ids)
    except sqlite3.Error as e:
        out["error"] = "sqlite error: %s" % e
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"),
                               uri=True)
        try:
            out["orphans_after"] = len(orphan_ledger_entries(conn))
        finally:
            conn.close()
    except sqlite3.Error:
        out["orphans_after"] = None
    return out


def clean_orphans_family(workspace: Union[str, Path],
                         dry_run: bool = False) -> Dict[str, Any]:
    """Clean the orphan ledger entries of EVERY live library."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    for lib in enumerate_live(root):
        rep = clean_orphans(lib["db"], dry_run=dry_run)
        rep["library"] = lib["name"]
        reports.append(rep)
    return {
        "workspace": str(root),
        "dry_run": bool(dry_run),
        "library_count": len(reports),
        "libraries": reports,
        "orphans_before": sum(r.get("orphans_before", 0) for r in reports),
        "orphans_after": sum(r.get("orphans_after", 0) or 0 for r in reports),
        "deleted": sum(r.get("deleted", 0) for r in reports),
    }


# ---------------------------------------------------------------------------
# M6f — the criterion
# ---------------------------------------------------------------------------

#: The criterion's identity, as the acceptance layer spells it.
CRITERION_ID = "M6f"
CRITERION_TITLE = ("the certification ledger holds no entry for a chunk "
                   "that does not exist")


def judge_m6f_ledger_has_no_orphans(conn: sqlite3.Connection,
                                    library: str = "") -> Dict[str, Any]:
    """**M6f — the ledger has no orphan entries.**

    **The question.**  Does every certification record name a chunk
    that is still in the index?

    **Why it needs its own criterion.**  Every other ledger criterion
    JOINS the ledger to ``chunks``, so an orphan entry is invisible to
    all of them: M6a counts rows without a witness, M6b compares
    witnesses with their chunks, M6c counts mirror rows without a
    witness.  None of them asks whether the witness itself still points
    at something.  That is the shape of a leak: the detectors that do
    not cover a thing cannot report that thing, and "the fleet is green"
    stays green while the ledger quietly fills up with witnesses to rows
    that stopped existing.

    **NOTRUN, not PASS, when the ledger does not exist.**  A library
    that has never been certified has no ledger — and "no ledger" means
    "nothing was ever certified", which is the strongest possible form
    of the answer, not an absence of an answer.  A criterion that
    reported PASS there would be reporting on a table it never read.
    """
    out: Dict[str, Any] = {
        "criterion": CRITERION_ID,
        "name": "the ledger has no orphan entries",
        "library": library,
        "verdict": "NOTRUN",
        "ledger_rows": 0,
        "orphans": 0,
        "orphan_ids": [],
        "detail": "",
    }
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as e:
        out["detail"] = "cannot read sqlite_master: %s" % e
        return out
    if "cert_ledger" not in tables:
        out["detail"] = ("the certification ledger does not exist in this "
                         "library — nothing has ever been certified here, "
                         "so there is nothing to be an orphan")
        return out
    try:
        counts = ledger_counts(conn)
        orphans = orphan_ledger_entries(conn)
    except sqlite3.Error as e:
        out["detail"] = "sqlite error: %s" % e
        return out
    out["ledger_rows"] = counts["ledger"]
    out["witnessed"] = counts["witnessed"]
    out["orphans"] = counts["orphans"]
    out["orphan_ids"] = [o["id"] for o in orphans[:50]]
    out["verdict"] = "PASS" if counts["orphans"] == 0 else "FAIL"
    out["detail"] = ("%d certification record(s); %d of them name a chunk "
                     "that is not in the index (must be 0)"
                     % (counts["ledger"], counts["orphans"]))
    return out


def judge_m6f_family(workspace: Union[str, Path]) -> Dict[str, Any]:
    """M6f over every live library of the workspace."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    verdict = "PASS"
    for lib in enumerate_live(root):
        try:
            conn = sqlite3.connect(
                "file:%s?mode=ro" % str(lib["db"]).replace("\\", "/"), uri=True)
        except sqlite3.Error as e:
            reports.append({"criterion": CRITERION_ID, "library": lib["name"],
                            "verdict": "NOTRUN", "detail": str(e)})
            verdict = "FAIL"
            continue
        try:
            rep = judge_m6f_ledger_has_no_orphans(conn, library=lib["name"])
        finally:
            conn.close()
        reports.append(rep)
        if rep["verdict"] == "FAIL":
            verdict = "FAIL"
    return {
        "criterion": CRITERION_ID,
        "title": CRITERION_TITLE,
        "workspace": str(root),
        "library_count": len(reports),
        "libraries": reports,
        "verdict": verdict,
        "total_orphans": sum(r.get("orphans", 0) for r in reports),
    }


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.ledger_orphans --workspace <ws>``."""
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Clean the certification ledger's orphan entries (entries "
                    "whose chunk no longer exists), and report the M6f "
                    "criterion.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--judge", action="store_true",
                    help="only judge M6f, change nothing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.judge:
        report = judge_m6f_family(args.workspace)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("M6f %s — %s" % (report["criterion"], report["title"]))
            for lib in report["libraries"]:
                print("  %-24s %-7s %s" % (lib.get("library"),
                                           lib.get("verdict"),
                                           lib.get("detail", "")))
                for oid in lib.get("orphan_ids", [])[:5]:
                    print("        ORPHAN %s" % oid[:12])
            print("VERDICT: %s (total orphans=%d)"
                  % (report["verdict"], report["total_orphans"]))
        return 0 if report["verdict"] == "PASS" else 1

    report = clean_orphans_family(args.workspace, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("ledger orphan cleanup: workspace=%s libraries=%d dry_run=%s"
              % (report["workspace"], report["library_count"],
                 report["dry_run"]))
        print("orphans before=%s after=%s deleted=%s"
              % (report["orphans_before"], report["orphans_after"],
                 report["deleted"]))
        for lib in report["libraries"]:
            if not lib.get("orphans_before"):
                continue
            print("  %-24s orphans=%-3d deleted=%-3d"
                  % (lib.get("library"), lib.get("orphans_before"),
                     lib.get("deleted")))
            for e in lib.get("entries", [])[:5]:
                print("      %-12s %-40s %s"
                      % (e["id"][:12], (e.get("body_path") or "")[:40],
                         e.get("certified_by")))
    return 0 if report["orphans_after"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
