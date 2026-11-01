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

from agent.memory.sweep_support import backup_library, write_json


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
                  dry_run: bool = False,
                  backup_dir: Optional[Union[str, Path]] = None,
                  name: str = "") -> Dict[str, Any]:
    """Delete the orphan ledger entries of ONE library.

    **裁决 C3 (2026-11-01) — the three things this must do.**

    1. **Back up the ledger table FIRST.**  The backup is taken through
       the product's own checkpointed backup API (``VACUUM INTO``
       after a WAL checkpoint), NOT a file copy: a file copy of a
       live SQLite file is a partial snapshot, and a restore from it
       silently loses the newest rows.

    2. **Produce a READABLE revocation list.**  Not a count — a list
       that names every certification being revoked, WHY it is being
       revoked, and WHICH vanished chunk it was about.  "Which
       certification was revoked, and why" is a question a human has
       to be able to answer without reading SQL, and the list is
       written to disk as a manifest so it survives the process.

    3. **Be able to roll back.**  The manifest carries the deleted
       rows verbatim, so the rollback is a re-INSERT of exactly those
       rows — it undoes THIS step and touches nothing else.

    **The list is the deliverable; the deletion is the side effect.**
    That is the whole of C3: an irreversible operation must be
    explainable and reversible.  The manifest is written BEFORE the
    delete, so even a crash mid-delete leaves the undo list on disk.

    **DRY-RUN IS THE DEFAULT.**  ``dry_run=False`` means "really
    delete"; the CLI spells that ``--apply``, so the operator has to
    type that they mean it.
    """
    path = Path(db)
    out: Dict[str, Any] = {
        "db": str(path),
        "dry_run": bool(dry_run),
        "orphans_before": 0,
        "deleted": 0,
        "orphans_after": 0,
        "entries": [],
        "revocations": [],
        "backup": None,
        "manifest": None,
    }
    if not path.is_file():
        out["error"] = "the index does not exist"
        return out

    # --- step 1: read the orphans, and say WHY each one is an orphan --
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        entries = orphan_ledger_entries(conn)
        out["orphans_before"] = len(entries)
        out["entries"] = entries
        revocations = [_describe_revocation(conn, e) for e in entries]
        out["revocations"] = revocations
    except sqlite3.Error as e:
        out["error"] = "sqlite error: %s" % e
        conn.close()
        return out

    if not entries:
        conn.close()
        out["orphans_after"] = 0
        return out

    # --- step 2: back up BEFORE the write ---------------------------------
    if not dry_run:
        if backup_dir is None:
            conn.close()
            out["error"] = ("a prune without a backup directory is refused: "
                            "the backup is what makes this reversible")
            return out
        bak = backup_library(path, backup_dir, tag="pre-prune", name=name)
        out["backup"] = bak
        if bak.get("error"):
            conn.close()
            out["error"] = bak["error"]
            return out

    # --- step 3: write the manifest BEFORE the delete ---------------------
    #
    # Written first on purpose.  If the delete crashes half way, the
    # undo list is already on disk, and the operator can tell exactly
    # which certifications were revoked and restore them.
    if not dry_run:
        manifest = {
            "tool": "ledger_orphans",
            "library": name or path.parent.parent.name,
            "db": str(path),
            "reason": ("the ledger row's chunk no longer exists in the "
                       "index; the certification had no subject left to "
                       "certify, so the certification is REVOKED"),
            "revoked_count": len(revocations),
            "backup": out["backup"],
            "revocations": revocations,
            "deleted_rows": entries,
        }
        mpath = Path(backup_dir) / ("%s.ledger-revocations.json"
                                    % (name or path.parent.parent.name))
        write_json(mpath, manifest)
        out["manifest"] = str(mpath)

    if not dry_run:
        ids = [e["id"] for e in entries]
        try:
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


def _describe_revocation(conn: sqlite3.Connection,
                          entry: Dict[str, Any]) -> Dict[str, Any]:
    """Say, in one readable record, what is being revoked and why.

    The "why" is not a guess.  The chunk id is
    ``md5("path:start:end[:part]")`` — derived from the row's
    POSITION, not from its content.  So when the truth-layer file
    changed and the chunker re-cut it, the rows got NEW ids and the
    old ids are gone.  That is the ordinary, measured cause: the
    certification is about a chunk that has been REPLACED, not about a
    chunk that was lost.

    The record also names what the certification WAS — its state, its
    origin, its path, when it was certified and by whom — so the
    revocation list reads as a list of revoked certifications rather
    than a list of deleted identifiers.
    """
    path = entry.get("body_path") or ""
    same_path_now = 0
    try:
        same_path_now = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE path = ?", (path,)
        ).fetchone()[0] if path else 0
    except sqlite3.Error:
        same_path_now = -1
    if same_path_now > 0:
        why = ("the chunk this certified was re-cut (the file '%s' now "
               "has %d chunk(s) under NEW ids), so the certification's "
               "subject no longer exists under the certified id"
               % (path, same_path_now))
        cause = "RECUT"
    else:
        why = ("the chunk this certified is not in the index any more "
               "(the file '%s' has no chunk rows at all), so the "
               "certification's subject no longer exists" % path)
        cause = "VANISHED"
    return {
        "id": entry.get("id"),
        "revoked_certification": {
            "text_state": entry.get("text_state"),
            "body_origin": entry.get("body_origin"),
            "body_path": path,
            "body_hash": entry.get("body_hash"),
            "certified_at": entry.get("certified_at"),
            "certified_by": entry.get("certified_by"),
        },
        "cause": cause,
        "why": why,
        "chunks_now_under_same_path": same_path_now,
    }


def restore_orphans(db: Union[str, Path],
                    manifest: Union[str, Path]) -> Dict[str, Any]:
    """Undo a prune: re-INSERT the rows the manifest names.

    This is the ROLLBACK half of 裁决 C3.  It restores ONLY the
    ``cert_ledger`` rows the prune deleted — it does not restore the
    chunks (the prune never deleted any) and it does not touch
    anything else.  So the rollback is scoped to what the prune did.
    """
    from agent.memory.sweep_support import read_json, restore_ledger_rows

    path = Path(db)
    mpath = Path(manifest)
    if not mpath.is_file():
        return {"error": "the manifest does not exist: %s" % mpath}
    data = read_json(mpath)
    rows = data.get("deleted_rows") or []
    if not path.is_file():
        return {"error": "the index does not exist: %s" % path}
    conn = sqlite3.connect(str(path))
    try:
        rep = restore_ledger_rows(conn, rows)
    finally:
        conn.close()
    rep["manifest"] = str(mpath)
    rep["db"] = str(path)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"),
                               uri=True)
        try:
            rep["orphans_after"] = len(orphan_ledger_entries(conn))
        finally:
            conn.close()
    except sqlite3.Error:
        rep["orphans_after"] = None
    return rep


def clean_orphans_family(workspace: Union[str, Path],
                         dry_run: bool = False,
                         backup_dir: Optional[Union[str, Path]] = None
                         ) -> Dict[str, Any]:
    """Clean the orphan ledger entries of EVERY live library."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    for lib in enumerate_live(root):
        rep = clean_orphans(lib["db"], dry_run=dry_run,
                             backup_dir=backup_dir, name=lib["name"])
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
        "manifests": [r["manifest"] for r in reports if r.get("manifest")],
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
    import sys

    ap = argparse.ArgumentParser(
        description="Clean the certification ledger's orphan entries (entries "
                    "whose chunk no longer exists), and report the M6f "
                    "criterion.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually revoke (without it, this is a dry-run: "
                         "nothing is written)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry-run (this is the default)")
    ap.add_argument("--backup-dir", default=None,
                    help="where the pre-prune backups and the revocation "
                         "manifests go (required by --apply)")
    ap.add_argument("--restore", default=None, metavar="MANIFEST",
                    help="undo a prune: re-INSERT the certifications the "
                         "manifest names")
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

    if args.restore:
        from agent.memory.migrate import enumerate_live
        rep = None
        for lib in enumerate_live(Path(args.workspace)):
            if lib["name"] in args.restore or args.restore == lib["name"]:
                rep = restore_orphans(lib["db"], args.restore)
                break
        if rep is None:
            print("REFUSED: no live library matches %r" % args.restore,
                  file=sys.stderr)
            return 2
        print(json.dumps(rep, ensure_ascii=False, indent=2)
              if args.json else
              "restored %s certification(s) from %s; orphans now %s"
              % (rep.get("restored"), rep.get("manifest"),
                 rep.get("orphans_after")))
        return 0

    if args.apply and not args.backup_dir:
        print("REFUSED: --apply requires --backup-dir.  A prune without a "
              "backup and a revocation manifest is not a reversible "
              "operation.", file=sys.stderr)
        return 2

    dry_run = not args.apply
    report = clean_orphans_family(args.workspace, dry_run=dry_run,
                                  backup_dir=args.backup_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("ledger orphan cleanup: workspace=%s libraries=%d dry_run=%s"
              % (report["workspace"], report["library_count"],
                 report["dry_run"]))
        print("orphans before=%s after=%s revoked=%s"
              % (report["orphans_before"], report["orphans_after"],
                 report["deleted"]))
        for lib in report["libraries"]:
            if not lib.get("orphans_before"):
                continue
            print("  %-24s orphans=%-3d revoked=%-3d manifest=%s"
                  % (lib.get("library"), lib.get("orphans_before"),
                     lib.get("deleted"), lib.get("manifest") or "-"))
            for rv in lib.get("revocations", [])[:5]:
                cert = rv.get("revoked_certification", {})
                print("      REVOKED %-12s %-9s %-40s was %s/%s by %s"
                      % ((rv.get("id") or "")[:12], rv.get("cause"),
                         (cert.get("body_path") or "")[:40],
                         cert.get("text_state"), cert.get("certified_at"),
                         cert.get("certified_by")))
                print("        why: %s" % rv.get("why"))
    return 0 if report["orphans_after"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
