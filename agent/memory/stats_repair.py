"""The statistics-column repairer: ``files.chunk_count`` and
``files.chunker_version``.

**What it repairs, and why those two columns are wrong.**

The ``files`` table carries two statistics per file: ``chunk_count``
(how many chunks the file was cut into) and ``chunker_version``
(which chunker cut it).  They are the FILES side of the projection;
the chunks side is the other axis.  The two axes were migrated at
DIFFERENT TIMES, and the legacy writer's ``INSERT OR REPLACE INTO
files (...)`` does not name those two columns — so they fell back to
their DEFAULT (or to NULL) and were never measured.

MEASURED on this machine on 2026-11-01 (16 live libraries):

  * **ROOT: 22 paths with ``chunker_version = 0``** (and
    ``chunk_count = 0``) while its other 107 paths carry version 4.
    This is the C-A3.4 red: a library that MIXES the current version
    with the legacy default is "REBUILT but MISMATCHED" — and no
    amount of "rebuild it again" fixes a mixed state;
  * **57 paths across 5 libraries with NULL statistics** (18 in
    ``agent-mvnes449``, 32 in ``core-architect``, 2 each in
    ``agent-mvnewfyb`` / ``agent-mvnf7fjl`` / ``consistency-guardian``)
    — the J-3 red: the statistics were erased by the old writer.

**The rule is CONTENT-based, never the clock.**  The count comes from
RE-CHUNKING the file with the current chunker
(``agent.memory.chunker.chunk_for_file``) — the same rule the M6e
criterion uses to measure "the real chunk count".  An mtime can be
forged by a ``touch``; a re-chunk cannot.  And a file that cannot be
read is **left alone**: ``None`` means "not measurable", which is NOT
zero, and a wrong number is worse than no number.

**What it does NOT do.**  It does not delete rows, does not rewrite
chunk text, does not touch the ``chunks`` table, and does not certify
anything.  It writes two columns on the ``files`` table, and only
where the measurement says they are wrong.

**The guard is on the other side of this write.**  The libraries carry
``files_body_keep_bd`` / ``files_body_keep_bi_ai`` triggers and a
``files_keep`` table: the guard's job is to RESTORE the statistics
when the legacy writer erases them.  This repairer does not go
through the guard — it writes the measurement directly, and the
``files_keep`` row stays as the backup.  (MEASURED: the guard's own
backup holds a usable value for only 6 of ROOT's 22 broken paths and
0 of the 57 NULL ones, so the guard's backup is NOT a repair source —
the re-chunk is.)

**The safety rules, which are the whole point of this module:**

  1. **DRY-RUN IS THE DEFAULT.**  Without ``--apply`` nothing is
     written.  The ``--apply`` spelling is deliberate: an irreversible
     write should require the operator to TYPE that they want it.
  2. **COPY FIRST.**  ``--work-on-copy`` copies every library into a
     scratch directory and repairs the COPIES, so the repair can be
     rehearsed end to end without touching the live workspace.
  3. **EVERY APPLY TAKES A BACKUP** through the product's own
     checkpointed backup API, and the backup's path and sha256 go
     into the report.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent.memory.chunker import TextChunker
from agent.memory.sweep_support import (backup_library, sha256_of,
                                        write_json)


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------

def _real_chunk_count(workspace: Union[str, Path], relpath: str
                      ) -> Optional[int]:
    """How many chunks the CURRENT chunker cuts this file into.

    ``None`` when the truth file cannot be read — and ``None`` means
    "not measurable", which is NOT the same as zero.
    """
    if not relpath:
        return None
    from agent.memory.body_pointer import resolve_truth_file

    truth = resolve_truth_file(workspace, relpath)
    if truth is None:
        return None
    try:
        raw = truth.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    from agent.memory.chunker import chunk_for_file
    try:
        return len(chunk_for_file(raw, relpath))
    except Exception:
        return None


def _is_broken(chunk_count: Optional[int], chunker_version: Optional[int],
               current: int) -> bool:
    """Is this row's statistics pair a defect?

    Three ways to be broken, and they are three different stories:

      * **NULL** — the statistic was never filled in (or was erased);
      * **0** — the legacy writer's DEFAULT landed there.  A file that
        really produces zero chunks is a different fact, and the
        re-chunk tells the two apart;
      * **a version that is not the current one** — the file was cut by
        an older chunker, or by no chunker at all.

    A row whose count already equals the re-chunked count is LEFT
    ALONE — even if its version is older, because the count is the
    measurement and the version records WHICH chunker produced it.
    """
    if chunk_count is None or chunk_count == 0:
        return True
    if chunker_version is None or chunker_version != current:
        return True
    return False


# ---------------------------------------------------------------------------
# one library
# ---------------------------------------------------------------------------

def repair_library(workspace: Union[str, Path],
                   db: Union[str, Path],
                   dry_run: bool = True,
                   backup_dir: Optional[Union[str, Path]] = None,
                   name: str = "",
                   ) -> Dict[str, Any]:
    """Measure and (unless ``dry_run``) repair ONE library's statistics.

    The write names BOTH columns, so the pair stays consistent — a
    count measured by chunker 4 must not carry chunker 3's version.
    The ``WHERE`` clause carries the row's CURRENT values, so a
    concurrent writer that measured the file first is not overwritten.
    """
    path = Path(db)
    current = int(TextChunker.CHUNKER_VERSION)
    out: Dict[str, Any] = {
        "db": str(path),
        "dry_run": bool(dry_run),
        "chunker_version_current": current,
        "rows_before": 0,
        "broken_before": 0,
        "null_before": 0,
        "zero_before": 0,
        "stale_version_before": 0,
        "not_measurable": 0,
        "fixed_count": 0,
        "fixed_version": 0,
        "unchanged": 0,
        "changes": [],
        "backup": None,
    }
    if not path.is_file():
        out["error"] = "the index does not exist"
        return out

    if not dry_run:
        if backup_dir is None:
            out["error"] = ("an apply without a backup directory is refused: "
                            "the backup is what makes this reversible")
            return out
        bak = backup_library(path, backup_dir, tag="pre-stats", name=name)
        out["backup"] = bak
        if bak.get("error"):
            out["error"] = bak["error"]
            return out

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        if "chunk_count" not in cols or "chunker_version" not in cols:
            out["error"] = "the files table has no statistics columns"
            return out
        rows = conn.execute(
            "SELECT path, chunk_count, chunker_version FROM files"
        ).fetchall()
        out["rows_before"] = len(rows)
        for row in rows:
            rel = row["path"] or ""
            cc = row["chunk_count"]
            cv = row["chunker_version"]
            if cc is None:
                out["null_before"] += 1
            elif cc == 0:
                out["zero_before"] += 1
            if cv is None or cv != current:
                out["stale_version_before"] += 1

            if not _is_broken(cc, cv, current):
                out["unchanged"] += 1
                continue
            out["broken_before"] += 1

            real = _real_chunk_count(workspace, rel)
            if real is None:
                # Not measurable: the truth file is missing or unreadable.
                # The statistics stay as they are — a wrong number is
                # worse than no number, and "not measured" is honest.
                out["not_measurable"] += 1
                continue

            need_count = (cc is None) or (cc != real)
            need_version = (cv is None) or (cv != current)
            if not need_count and not need_version:
                out["unchanged"] += 1
                continue
            out["changes"].append({
                "path": rel,
                "chunk_count_before": cc,
                "chunk_count_after": real,
                "chunker_version_before": cv,
                "chunker_version_after": current,
                "count_changed": need_count,
                "version_changed": need_version,
            })
            if dry_run:
                continue
            cur = conn.execute(
                "UPDATE files SET chunk_count=?, chunker_version=? "
                "WHERE path=? AND chunk_count IS ? AND chunker_version IS ?",
                (real, current, rel, cc, cv))
            if cur.rowcount:
                if need_count:
                    out["fixed_count"] += 1
                if need_version:
                    out["fixed_version"] += 1
        if not dry_run:
            conn.commit()
    except sqlite3.Error as e:
        out["error"] = "sqlite error: %s" % e
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    # The AFTER numbers, read back from the file.
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"),
                               uri=True)
        try:
            out["null_after"] = conn.execute(
                "SELECT COUNT(*) FROM files WHERE chunk_count IS NULL"
            ).fetchone()[0]
            out["zero_after"] = conn.execute(
                "SELECT COUNT(*) FROM files WHERE chunk_count = 0").fetchone()[0]
            out["stale_version_after"] = conn.execute(
                "SELECT COUNT(*) FROM files WHERE chunker_version IS NULL "
                "OR chunker_version <> ?", (current,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        out["null_after"] = out["zero_after"] = out["stale_version_after"] = None
    return out


# ---------------------------------------------------------------------------
# the family
# ---------------------------------------------------------------------------

def repair_family(workspace: Union[str, Path],
                   dry_run: bool = True,
                   backup_dir: Optional[Union[str, Path]] = None,
                   work_on_copy: bool = False,
                   copy_dir: Optional[Union[str, Path]] = None,
                   ) -> Dict[str, Any]:
    """Repair every live library's statistics columns.

    ``work_on_copy`` copies each library's index into ``copy_dir``
    first and repairs the COPY.  The copy is a byte copy of the
    database file only — the truth layer (the files) is read from the
    LIVE workspace, because that is where the truth lives; the copy is
    only a rehearsal of the WRITE.
    """
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    scratch = Path(copy_dir) if copy_dir else None
    if work_on_copy and scratch is None:
        scratch = Path(tempfile.mkdtemp(prefix="stats_repair_copy_"))
    reports: List[Dict[str, Any]] = []
    for lib in enumerate_live(root):
        db = Path(lib["db"])
        target_db = db
        if work_on_copy and scratch is not None:
            scratch.mkdir(parents=True, exist_ok=True)
            target_db = scratch / ("%s.db" % lib["name"])
            shutil.copy2(str(db), str(target_db))
        rep = repair_library(lib["workspace"], target_db, dry_run=dry_run,
                             backup_dir=backup_dir, name=lib["name"])
        rep["library"] = lib["name"]
        rep["source_db"] = str(db)
        rep["worked_on_copy"] = bool(work_on_copy)
        reports.append(rep)
    return {
        "workspace": str(root),
        "dry_run": bool(dry_run),
        "work_on_copy": bool(work_on_copy),
        "copy_dir": str(scratch) if scratch else None,
        "library_count": len(reports),
        "libraries": reports,
        "broken_before": sum(r.get("broken_before", 0) for r in reports),
        "null_before": sum(r.get("null_before", 0) for r in reports),
        "null_after": sum(r.get("null_after", 0) or 0 for r in reports),
        "zero_before": sum(r.get("zero_before", 0) for r in reports),
        "zero_after": sum(r.get("zero_after", 0) or 0 for r in reports),
        "stale_version_before": sum(r.get("stale_version_before", 0)
                                    for r in reports),
        "stale_version_after": sum(r.get("stale_version_after", 0) or 0
                                   for r in reports),
        "fixed_count": sum(r.get("fixed_count", 0) for r in reports),
        "fixed_version": sum(r.get("fixed_version", 0) for r in reports),
        "not_measurable": sum(r.get("not_measurable", 0) for r in reports),
    }


def format_report(report: Dict[str, Any]) -> str:
    lines = [
        "files statistics repair: workspace=%s libraries=%d dry_run=%s "
        "copy=%s" % (report.get("workspace"), report.get("library_count"),
                     report.get("dry_run"), report.get("work_on_copy")),
        "chunk_count IS NULL   : %s -> %s" % (report.get("null_before"),
                                              report.get("null_after")),
        "chunk_count = 0       : %s -> %s" % (report.get("zero_before"),
                                              report.get("zero_after")),
        "chunker_version stale : %s -> %s" % (report.get("stale_version_before"),
                                              report.get("stale_version_after")),
        "broken rows           : %s found, %s repaired (count=%s version=%s), "
        "not measurable=%s"
        % (report.get("broken_before"),
           report.get("fixed_count", 0) + report.get("fixed_version", 0),
           report.get("fixed_count"), report.get("fixed_version"),
           report.get("not_measurable")),
        "",
    ]
    for rep in report.get("libraries", []):
        if not rep.get("changes") and not rep.get("backup"):
            continue
        lines.append("--- %s : %d row(s) to fix ---"
                     % (rep.get("library"), len(rep.get("changes", []))))
        if rep.get("backup"):
            lines.append("    backup: %s (%s bytes, sha256 %s)"
                         % (rep["backup"].get("backup"),
                            rep["backup"].get("size_bytes"),
                            str(rep["backup"].get("sha256"))[:16]))
        for ch in rep.get("changes", [])[:8]:
            lines.append("    %-46s cc %s -> %s   cv %s -> %s"
                         % ((ch["path"] or "")[:46], ch["chunk_count_before"],
                            ch["chunk_count_after"], ch["chunker_version_before"],
                            ch["chunker_version_after"]))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.stats_repair --workspace <ws> [--apply]``."""
    ap = argparse.ArgumentParser(
        description="Repair the files table's statistics columns "
                    "(chunk_count / chunker_version) by RE-CHUNKING each "
                    "file with the current chunker.  DRY-RUN IS THE "
                    "DEFAULT: --apply is required to write.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually write (without it, nothing is written)")
    ap.add_argument("--work-on-copy", action="store_true",
                    help="rehearse on copies of the libraries, not on them")
    ap.add_argument("--copy-dir", default=None,
                    help="where the rehearsal copies go")
    ap.add_argument("--apply-on-copy", action="store_true",
                    help="rehearse the WRITE on copies (needs --work-on-copy "
                         "and --backup-dir); the live libraries are not "
                         "touched")
    ap.add_argument("--backup-dir", default=None,
                    help="where the pre-write backups go (required by --apply)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", default=None,
                    help="write the full report as JSON to this path")
    args = ap.parse_args(argv)

    if args.apply_on_copy and not args.work_on_copy:
        print("REFUSED: --apply-on-copy requires --work-on-copy.", file=sys.stderr)
        return 2
    if (args.apply or args.apply_on_copy) and not args.backup_dir:
        print("REFUSED: an apply without a backup directory is refused: "
              "the backup is what makes this reversible.", file=sys.stderr)
        return 2

    report = repair_family(
        args.workspace,
        dry_run=not (args.apply or args.apply_on_copy),
        backup_dir=args.backup_dir,
        work_on_copy=args.work_on_copy,
        copy_dir=args.copy_dir,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    if args.report:
        write_json(args.report, report)
        print("\nreport written to %s" % args.report)
    return 0 if (report.get("null_after") == 0
                 and report.get("zero_after") == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
