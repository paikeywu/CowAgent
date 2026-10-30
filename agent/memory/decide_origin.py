"""T2 — the ``unresolved`` rows get a DECIDED origin and a POINTER.

**What this is.**  ``chunks.body_origin`` answers "where does this row's
body come from?" and its DEFAULT is ``'unresolved'`` — "nobody has
decided yet".  A row that keeps that value forever is a TODO nobody
owns, and a TODO nobody owns is a hole: the certification apparatus
cannot certify it, the guard cannot restore it, and it reads back as
"the writer did not decide", which is not the same as "the writer
decided: this row has no truth source".

**The judgement is CONTENT-based, and the clock is not allowed near it.**
This is SEED's ruling (Q-A3) and it is a correctness requirement, not a
style preference: an mtime can be forged by a ``touch``, and a file
copied onto another machine carries a meaningless timestamp.

**This module REUSES the migration's own rule.**  That is the whole
design: ``agent.memory.migrate`` already answers "where does this row's
body come from, and what pointer does it carry?" — it is the M4
backfill, and it is the ONE implementation of that question (the
certifier imports it for exactly this reason: "one implementation, two
callers", because "a second spelling of how do I decide a row's origin
is how the two sides end up disagreeing").

So this module does not re-derive the rule.  It calls:

  * ``migrate._segments_for``  — **D1**: is ``chunks.text`` still one
    of the CHUNKER's blocks for that file?  (``None`` ⇒ D1 fails ⇒ the
    row's text is not a current chunker output ⇒ factual drift.)
  * ``body_pointer.four_columns_for_chunk`` — the POINTER
    (``body_path`` / ``body_anchor`` / ``body_hash``), computed with the
    segment sequence the chunker produced.
  * ``migrate._decide_origin`` — the ORIGIN, from D1.

**Why the pointer is filled here too.**  The delegation asks for two
counters to reach zero: ``unresolved`` AND ``body_path IS NULL``.  On
this machine they are the SAME 72 rows (measured: every library's
``pathNULL`` count equals its ``unresolved`` count).  A decision that
only set the origin would leave the pointer NULL — and a row with no
pointer cannot be certified, cannot be re-checked against the file, and
reads back as "this row points nowhere".  So the two are written
together, in ONE statement, and the state is left alone.

**Why this module does NOT touch ``text_state``.**  The state is the
certification's to set (设计 §3.1: the state is set by the party that
actually verified the row).  And a row may legitimately be ``'mirror'``
AND ``'orphan'``: it was certified while its truth source still
matched, and the file then drifted under it.  The state means "verified
at certification time"; the origin means "where the body came from".
They answer DIFFERENT questions, so writing one must not disturb the
other.

**The three terminal states** (``agent.memory.body_pointer``):

  * ``'file'``   — there IS a truth-layer file and this row is still
                   one of ITS chunks;
  * ``'orphan'`` — there WAS supposed to be a truth source and it does
                   not match (the file drifted, or the row is not a
                   chunk of it any more).  The text is KEPT — an orphan
                   is a fact, not a deletion;
  * ``'blob'``   — there is honestly NO truth source: the text lives
                   only in this row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent.memory.body_pointer import (
    ORIGIN_BLOB,
    ORIGIN_FILE,
    ORIGIN_ORPHAN,
    ORIGIN_UNRESOLVED,
    body_hash,
    read_fragment,
    resolve_truth_file,
)

#: The reason codes the report carries — so a reader never has to infer
#: why a row was decided the way it was.
REASON_FILE = "file"                    # D1 holds
REASON_ORPHAN_NOT_A_CHUNK = "orphan-not-a-chunk"   # D1 fails
REASON_ORPHAN_NO_TRUTH = "orphan-no-truth-file"     # no truth file at all
REASON_ORPHAN_UNSAFE_PATH = "orphan-unsafe-path"    # the path cannot be trusted
REASON_BLOB = "blob"                    # no truth source is expected at all
REASON_NO_PATH = "no-path"              # the row names no file at all


def _recover_part(rel: str, start_line: int, end_line: int,
                  chunk_id: str) -> int:
    """The part segment, recovered from the chunk id (the migration's
    rule, reused — never re-derived).  -1 when the id matches no part."""
    from agent.memory.migrate import _recover_part as _impl

    return _impl(rel, start_line, end_line, chunk_id)


def decide_row(workspace: Union[str, Path],
               row: Dict[str, Any]) -> Dict[str, Any]:
    """The terminal origin (and pointer) for ONE row, with the evidence.

    Returns ``{"origin", "reason", "d1", "d2", "pointer", "truth",
    "detail"}`` — and the pointer is ``(body_path, body_anchor,
    body_hash)`` or ``None`` when there is honestly no pointer to write.

    **The order of the questions is the rule**, and it is the MIGRATION's
    order (``migrate.step_m4``), not a new one:

    1. **Is the path safe?**  A row must never carry a pointer that
       resolves somewhere it must not, so an unsafe path (a backslash,
       an absolute path, a ``..`` segment) is refused outright.
    2. **Is there a truth file?**  No → ``'orphan'``: there WAS supposed
       to be one (the row's path names it).
    3. **D1 — is the row still a chunk of that file?**  No →
       ``'orphan'``: the text is not a current chunker output, so the
       content has drifted away from the file.
    4. **D2 — does the stored ``body_hash`` equal the hash of the
       fragment the anchor points at?**  This is only evaluable when the
       row already HAS a pointer; when it does not, D2 is reported as
       "not evaluable" rather than as a failure.
    """
    from agent.memory.body_pointer import posix_body_path
    from agent.memory.migrate import _decide_origin, _segments_for

    rel = row.get("path") or ""
    text = row.get("text") or ""
    out: Dict[str, Any] = {
        "origin": ORIGIN_UNRESOLVED,
        "reason": "",
        "d1": False,
        "d2": None,
        "pointer": None,
        "truth": None,
        "detail": "",
    }

    if not rel:
        out["origin"] = ORIGIN_BLOB
        out["reason"] = REASON_BLOB
        out["detail"] = ("the row names no file, so there is no truth "
                         "source to mirror — the text lives only in this row")
        return out

    safe_path = posix_body_path(rel)
    if safe_path is None:
        out["origin"] = ORIGIN_ORPHAN
        out["reason"] = REASON_ORPHAN_UNSAFE_PATH
        out["detail"] = ("the path is not safe (backslash / absolute / '..'), "
                         "so it must never carry a pointer")
        return out

    truth = resolve_truth_file(workspace, safe_path)
    if truth is None:
        out["origin"] = ORIGIN_ORPHAN
        out["reason"] = REASON_ORPHAN_NO_TRUTH
        out["detail"] = ("the truth file behind this path does not exist — "
                         "the row was supposed to have one")
        return out
    out["truth"] = str(truth)

    # --- D1: is the row still a chunk of that file? --------------------
    start_line = int(row.get("start_line") or 0)
    end_line = int(row.get("end_line") or 0)
    part = _recover_part(safe_path, start_line, end_line, row.get("id") or "")
    if part < 0:
        out["origin"] = ORIGIN_ORPHAN
        out["reason"] = REASON_ORPHAN_NOT_A_CHUNK
        out["detail"] = ("the chunk id matches no part segment for this "
                         "path/span, so the row cannot be located in the file")
        return out

    segments, gaps = _segments_for(truth, safe_path, start_line, end_line,
                                   part, text)
    out["d1"] = segments is not None

    # --- the pointer (the migration's own computation) -----------------
    from agent.memory.body_pointer import four_columns_for_chunk

    bp, anchor, bh, _state, _origin_from_pointer = four_columns_for_chunk(
        workspace_root=workspace,
        relpath=safe_path,
        start_line=start_line,
        end_line=end_line,
        part=part,
        chunk_text=text,
        segments=segments,
        gaps=gaps)
    if bp is not None:
        out["pointer"] = (bp, anchor, bh)

    # --- the origin (the migration's own decision) --------------------
    origin = _decide_origin(truth, segments, text, row.get("body_hash"))
    out["origin"] = origin

    # --- D2: has the file changed under the row? -----------------------
    stored_hash = row.get("body_hash")
    stored_anchor = row.get("body_anchor")
    if stored_hash and stored_anchor:
        fragment = read_fragment(truth, stored_anchor)
        out["d2"] = (fragment is not None
                     and body_hash(fragment) == stored_hash)
    else:
        # The pointer was never derived, so D2 cannot be evaluated.  It
        # is reported as "not evaluable" (None), NOT as a failure: a
        # row with no pointer has nothing to compare.
        out["d2"] = None

    if origin == ORIGIN_FILE:
        out["reason"] = REASON_FILE
        out["detail"] = ("D1 holds: the row's text is still one of the "
                         "chunker's blocks for that file")
    elif origin == ORIGIN_ORPHAN:
        if not out["d1"]:
            out["reason"] = REASON_ORPHAN_NOT_A_CHUNK
            out["detail"] = ("D1 fails: the row's text is not one of the "
                             "chunker's blocks for that file any more "
                             "(factual drift — the text is kept, never "
                             "deleted)")
        else:
            out["reason"] = REASON_ORPHAN_NO_TRUTH
            out["detail"] = ("the truth source is there but the row does "
                             "not rebuild from it")
    else:
        out["reason"] = REASON_BLOB
        out["detail"] = "the row has no truth source"
    return out


def unresolved_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The rows whose origin is still undecided."""
    try:
        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text, hash, body_path, "
            "body_anchor, body_hash, text_state, body_origin "
            "FROM chunks WHERE body_origin=? ORDER BY path, start_line",
            (ORIGIN_UNRESOLVED,)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def decide_unresolved(workspace: Union[str, Path],
                      db: Union[str, Path],
                      dry_run: bool = False) -> Dict[str, Any]:
    """Decide the origin of every undecided row in ONE library.

    Returns a report with the counts per terminal state and the DETAIL
    of every row (so the decision is auditable row by row, not just in
    aggregate).
    """
    path = Path(db)
    out: Dict[str, Any] = {
        "library": path.parent.parent.parent.name or str(path),
        "db": str(path),
        "dry_run": bool(dry_run),
        "unresolved_before": 0,
        "path_null_before": 0,
        "decided": 0,
        "pointer_filled": 0,
        "by_origin": {},
        "by_reason": {},
        "rows": [],
    }
    if not path.is_file():
        out["error"] = "the index does not exist"
        return out

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = unresolved_rows(conn)
        out["unresolved_before"] = len(rows)
        out["path_null_before"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL").fetchone()[0]
        if not rows:
            out["unresolved_after"] = 0
            out["path_null_after"] = out["path_null_before"]
            return out
        for row in rows:
            verdict = decide_row(workspace, row)
            origin = verdict["origin"]
            out["by_origin"][origin] = out["by_origin"].get(origin, 0) + 1
            out["by_reason"][verdict["reason"]] = (
                out["by_reason"].get(verdict["reason"], 0) + 1)
            pointer = verdict["pointer"]
            entry = {
                "id": row["id"],
                "path": row["path"],
                "span": "%s-%s" % (row["start_line"], row["end_line"]),
                "text_state": row["text_state"],
                "origin": origin,
                "reason": verdict["reason"],
                "d1": verdict["d1"],
                "d2": verdict["d2"],
                "pointer": bool(pointer),
                "detail": verdict["detail"],
            }
            out["rows"].append(entry)
            if dry_run:
                continue
            # **The write is CONDITIONAL and touches only the pointer
            # and the origin.**  It never writes ``text_state`` — the
            # certification's to set.
            #
            # The WHERE clause carries the row's CURRENT origin, so a
            # concurrent writer that decided the row first is not
            # overwritten — the same optimistic-lock shape the
            # migration's backfill uses.
            if pointer:
                bp, anchor, bh = pointer
                cur = conn.execute(
                    "UPDATE chunks SET body_path=?, body_anchor=?, "
                    "body_hash=?, body_origin=? "
                    "WHERE id=? AND body_origin=?",
                    (bp, anchor, bh, origin, row["id"], ORIGIN_UNRESOLVED))
                if cur.rowcount:
                    out["decided"] += 1
                    out["pointer_filled"] += 1
            else:
                cur = conn.execute(
                    "UPDATE chunks SET body_origin=? "
                    "WHERE id=? AND body_origin=?",
                    (origin, row["id"], ORIGIN_UNRESOLVED))
                if cur.rowcount:
                    out["decided"] += 1
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

    # The AFTER counts, read back from the file: the report must state
    # the terminal state, not the intended one.
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"),
                               uri=True)
        try:
            out["unresolved_after"] = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE body_origin=?",
                (ORIGIN_UNRESOLVED,)).fetchone()[0]
            out["path_null_after"] = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        out["unresolved_after"] = None
        out["path_null_after"] = None
    return out


def decide_unresolved_family(workspace: Union[str, Path],
                             dry_run: bool = False) -> Dict[str, Any]:
    """Decide the undecided rows of EVERY live library of a workspace."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    for lib in enumerate_live(root):
        rep = decide_unresolved(lib["workspace"], lib["db"], dry_run=dry_run)
        rep["library"] = lib["name"]
        reports.append(rep)
    by_origin: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    for rep in reports:
        for k, v in (rep.get("by_origin") or {}).items():
            by_origin[k] = by_origin.get(k, 0) + v
        for k, v in (rep.get("by_reason") or {}).items():
            by_reason[k] = by_reason.get(k, 0) + v
    return {
        "workspace": str(root),
        "dry_run": bool(dry_run),
        "library_count": len(reports),
        "libraries": reports,
        "by_origin": by_origin,
        "by_reason": by_reason,
        "unresolved_before": sum(r.get("unresolved_before", 0)
                                  for r in reports),
        "unresolved_after": sum(r.get("unresolved_after", 0) or 0
                                for r in reports),
        "path_null_before": sum(r.get("path_null_before", 0) for r in reports),
        "path_null_after": sum(r.get("path_null_after", 0) or 0
                               for r in reports),
        "decided": sum(r.get("decided", 0) for r in reports),
        "pointer_filled": sum(r.get("pointer_filled", 0) for r in reports),
    }


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    """The report, as text."""
    lines = []
    lines.append("unresolved decision: workspace=%s libraries=%d dry_run=%s"
                 % (report.get("workspace"), report.get("library_count"),
                    report.get("dry_run")))
    lines.append("unresolved: %s -> %s   body_path IS NULL: %s -> %s   "
                 "decided=%s (pointer filled=%s)"
                 % (report.get("unresolved_before"),
                    report.get("unresolved_after"),
                    report.get("path_null_before"),
                    report.get("path_null_after"),
                    report.get("decided"), report.get("pointer_filled")))
    lines.append("by origin: %s" % (report.get("by_origin") or {}))
    lines.append("by reason: %s" % (report.get("by_reason") or {}))
    lines.append("")
    for rep in report.get("libraries", []):
        if not rep.get("unresolved_before"):
            continue
        lines.append("--- %s : %d undecided row(s) ---"
                     % (rep.get("library"), rep.get("unresolved_before")))
        for row in rep.get("rows", []):
            lines.append("  %-10s %-46s %-9s %-8s %-22s D1=%-5s D2=%-5s ptr=%s"
                         % (row["id"][:10], (row["path"] or "")[:46],
                            row["span"], row["text_state"], row["origin"],
                            row["d1"], row["d2"], row["pointer"]))
            if verbose:
                lines.append("             %s" % row["detail"])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.decide_origin --workspace <ws>``."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Decide the body_origin of every row that still says "
                    "'unresolved', by CONTENT (D1/D2) and never by the "
                    "clock, and fill the pointer the decision implies.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    report = decide_unresolved_family(args.workspace, dry_run=args.dry_run)
    print(format_report(report, verbose=args.verbose))
    return 0 if (report.get("unresolved_after") == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
