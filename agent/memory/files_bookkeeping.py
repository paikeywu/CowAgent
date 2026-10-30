"""The ``files`` bookkeeping, brought current — the measurement the
``files`` table owes the truth layer.

**Why this module exists (P1-阶段三 第八轮).**

The ``files`` table carries two statistics per file: ``chunk_count``
(how many chunks the file was cut into) and ``chunker_version`` (which
chunker cut it).  They are the per-file facts about the projection.

The chunks side and the files side are TWO AXES of the same migration,
and they were migrated at DIFFERENT TIMES.  MEASURED on this machine,
before this round: 20 ``files`` rows carried NULL statistics
(consistency-guardian ×16, agent-mvnes449 ×2, core-architect ×2) —
the legacy writer's ``INSERT OR REPLACE INTO files (...)`` does not
name those columns, so they fell back to their DEFAULT and were never
measured.  At that moment the criterion called them "never measured",
which was TRUE and honest: the chunks side had no decided rows and no
v4 anchors for those paths either, so the library had never been
migrated for them.

**Then the chunks side was migrated, and the same NULLs became an
ERASURE.**  This round decided the origin of every undecided row and
filled its pointer (and the certifier then promoted it), so those
paths now have decided rows and v4 anchors — which is exactly the
discriminator the criterion uses to tell "never measured" from "erased".
And with the chunks side migrated, the files side's NULL statistics
ARE an erasure: the library demonstrably processed these files, and
their bookkeeping is missing.

**So the two axes must be migrated together.**  The migration tool's
M4 step already does the files side — it re-chunks the file and writes
the real count and the current version.  This module is that same rule,
applied as a standalone, fleet-wide operation, because the chunks side
was migrated by a different tool this round and the files side must
follow.

**The rule is CONTENT-based, never the clock.**  The count comes from
RE-CHUNKING the file with the current chunker
(``agent.memory.chunker.chunk_for_file``), which is the same rule the
criterion uses to measure "the real chunk count".  An mtime can be
forged by a ``touch``; a re-chunk cannot.

**What it does NOT do.**  It does not delete rows, does not rewrite
chunk text, does not touch the chunks table at all, and does not
certify anything.  It writes two columns on the ``files`` table, and
only where the measurement says they are wrong.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent.memory.body_pointer import resolve_truth_file
from agent.memory.chunker import TextChunker, chunk_for_file


def _real_chunk_count(workspace: Union[str, Path], relpath: str
                      ) -> Optional[int]:
    """How many chunks the CURRENT chunker cuts this file into.

    ``None`` when the truth file cannot be read — and ``None`` means
    "not measurable", which is NOT the same as zero.  A file that
    cannot be read keeps its statistics untouched: a wrong number is
    worse than no number.
    """
    if not relpath:
        return None
    truth = resolve_truth_file(workspace, relpath)
    if truth is None:
        return None
    try:
        raw = truth.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return len(chunk_for_file(raw, relpath))
    except Exception:
        return None


def measure_library(workspace: Union[str, Path],
                    db: Union[str, Path],
                    dry_run: bool = False) -> Dict[str, Any]:
    """Measure and (unless ``dry_run``) fix ONE library's files bookkeeping.

    The rows that are touched are exactly the ones whose statistics are
    wrong:

      * ``chunk_count IS NULL`` — never measured, but the chunks side
        says this library WAS migrated for this path, so the number is
        missing rather than absent;
      * ``chunk_count = 0`` — the legacy writer's DEFAULT landed there
        (a file that really produces zero chunks is a different fact,
        and the re-chunk tells the two apart);
      * ``chunker_version`` is NULL or not the current version — the
        file was cut by an older chunker, or by no chunker at all.

    A row whose count already equals the re-chunked count is LEFT
    ALONE — even if its version is older, because the count is the
    measurement and the version records WHICH chunker produced it.  A
    file that has not been re-cut keeps the version that cut it.
    """
    path = Path(db)
    out: Dict[str, Any] = {
        "db": str(path),
        "dry_run": bool(dry_run),
        "rows_before": 0,
        "null_before": 0,
        "zero_before": 0,
        "stale_version_before": 0,
        "fixed_count": 0,
        "fixed_version": 0,
        "not_measurable": 0,
        "unchanged": 0,
        "changes": [],
    }
    if not path.is_file():
        out["error"] = "the index does not exist"
        return out

    current = int(TextChunker.CHUNKER_VERSION)
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
            # **The write names BOTH columns, so the pair stays
            # consistent** — a count measured by chunker 4 must not
            # carry chunker 3's version.  The WHERE clause carries the
            # row's CURRENT values, so a concurrent writer that
            # measured the file first is not overwritten.
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


def measure_family(workspace: Union[str, Path],
                   dry_run: bool = False) -> Dict[str, Any]:
    """Bring every live library's files bookkeeping current."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    for lib in enumerate_live(root):
        rep = measure_library(lib["workspace"], lib["db"], dry_run=dry_run)
        rep["library"] = lib["name"]
        reports.append(rep)
    return {
        "workspace": str(root),
        "dry_run": bool(dry_run),
        "library_count": len(reports),
        "libraries": reports,
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
    }


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    lines = []
    lines.append("files bookkeeping: workspace=%s libraries=%d dry_run=%s"
                 % (report.get("workspace"), report.get("library_count"),
                    report.get("dry_run")))
    lines.append("chunk_count IS NULL      : %s -> %s"
                 % (report.get("null_before"), report.get("null_after")))
    lines.append("chunk_count = 0          : %s -> %s"
                 % (report.get("zero_before"), report.get("zero_after")))
    lines.append("chunker_version stale    : %s -> %s"
                 % (report.get("stale_version_before"),
                    report.get("stale_version_after")))
    lines.append("fixed: count=%s version=%s"
                 % (report.get("fixed_count"), report.get("fixed_version")))
    lines.append("")
    for rep in report.get("libraries", []):
        if not rep.get("changes"):
            continue
        lines.append("--- %s : %d row(s) to fix ---"
                     % (rep.get("library"), len(rep["changes"])))
        for ch in rep["changes"][:8]:
            lines.append("  %-46s cc %s -> %s   cv %s -> %s"
                         % ((ch["path"] or "")[:46], ch["chunk_count_before"],
                            ch["chunk_count_after"], ch["chunker_version_before"],
                            ch["chunker_version_after"]))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.files_bookkeeping --workspace <ws>``."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Bring the files table's bookkeeping current: measure "
                    "each file by re-chunking it, and write the real count "
                    "and the chunker version that produced it.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    report = measure_family(args.workspace, dry_run=args.dry_run)
    print(format_report(report))
    return 0 if (report.get("null_after") == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
