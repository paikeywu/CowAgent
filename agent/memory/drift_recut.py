"""Re-cut the files whose projection has drifted from the truth layer.

**What this is.**  A knowledge page or an identity file is edited
AFTER it was cut into chunks.  The truth layer moved; the projection
(the ``chunks.text`` the index holds) did not.  Every reader that
rebuilds the fragment from the anchor then gets something that is not
the file — that is the INV-2 / INV-3 red, and the ``~N`` anchor
mismatch.  The architect's F1 ruling: **F1 is data drift, not an
implementation drift** — the fix is to RE-CUT the drifted files, not to
relax the criterion.

**The re-cut uses the product's own cutter.**  ``agent.memory.
rebuilder.rebuild_file`` is the ONE implementation of "cut a truth file
into a projection" (it calls ``chunker.chunk_for_file``, the same
call the write path makes, and it carries the release-state cache
optimisation from commit ``c126853b``).  This module does NOT
re-implement the cut — a second spelling of the cutter is how the two
sides end up disagreeing about where a chunk ends.

**Why the re-cut is a SEPARATE, guarded step.**  Two facts, both
measured:

  1. A re-cut **rewrites the ``files`` statistics columns** (it is a
     ``replace_file_chunks``), so the statistics repair must run AFTER
     it, not before.  The sweep's order reflects this.
  2. The old desktop build's ``INSERT OR REPLACE INTO files`` names
     only six columns, so a DELETE->INSERT against a library whose
     ``files`` DDL is ``NOT NULL DEFAULT 0`` lands the row on
     ``(0, 0)``.  The risk officer's B1/B2: while the old build can
     still write, a re-cut of a path the old build deleted is
     FORBIDDEN, and the statistics columns cannot be trusted.

⇒ This step is **off by default** and refuses to run without an
explicit assertion that the old runtime has been retired
(``--require-old-runtime-retired``).  The assertion is checked against
the LIVE process list and the old build's directory, and the refusal
says exactly which condition failed.

**The drift measurement is CONTENT-based.**  A file's mtime can be
forged by a ``touch``; the chunk ids are position-derived, so a
re-cut moves them wholesale.  The drift is therefore measured by
REBUILDING each chunk's fragment from the truth file and comparing it
to the stored text — the same reading the criterion uses.  The
"how far behind" number is reported for the human, but the DECISION to
re-cut is made on the content.

**What a re-cut does to the ledger.**  The chunk ids are
position-derived, so a re-cut replaces the ids.  ``replace_file_chunks``
prunes the ledger entries of the ids that did NOT come back, inside the
same transaction — so the ledger never keeps a witness for a row that
does not exist.  That is why the re-cut must run BEFORE the orphan
prune is judged: a re-cut creates orphans by construction, and the
prune is what removes them.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

#: The old desktop build.  The re-cut is forbidden while it can write.
OLD_BUILD_DIR = Path(r"C:\AI\CowAgent")
OLD_BUILD_EXECUTABLES = ("CowAgent.exe", "cowagent-backend.exe")


# ---------------------------------------------------------------------------
# the environment assertion
# ---------------------------------------------------------------------------

def old_runtime_state() -> Dict[str, Any]:
    """Is the old desktop build still able to write the live libraries?

    Read-only.  Three signals, because none of them alone is enough:
    the directory exists, an executable is alive, and the port the old
    backend listens on has an owner.
    """
    out: Dict[str, Any] = {
        "build_dir": str(OLD_BUILD_DIR),
        "build_present": OLD_BUILD_DIR.is_dir(),
        "processes": [],
        "port_owner": None,
        "running": False,
    }
    if out["build_present"]:
        for exe in OLD_BUILD_EXECUTABLES:
            found = list(OLD_BUILD_DIR.rglob(exe))
            if found:
                out.setdefault("executables", []).append(
                    {"name": exe, "paths": [str(p) for p in found[:5]]})
    if os.name == "nt":
        try:
            res = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=30)
            for line in res.stdout.splitlines():
                for exe in OLD_BUILD_EXECUTABLES:
                    if exe.lower() in line.lower():
                        out["processes"].append(line.strip()[:160])
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            res = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, timeout=30)
            for line in res.stdout.splitlines():
                if ":9876" in line and "LISTENING" in line.upper():
                    out["port_owner"] = line.strip()[:160]
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    out["running"] = bool(out["processes"]) or bool(out["port_owner"])
    return out


def assert_old_runtime_retired() -> Dict[str, Any]:
    """The gate.  Returns ``{"ok": bool, "reason": str, "state": {...}}``.

    A refusal is a REFUSAL, not a warning: the re-cut of a path the
    old build deleted is exactly the operation that turns the ledger
    into orphans, and the statistics columns into ``(0, 0)``.
    """
    state = old_runtime_state()
    reasons = []
    if state.get("processes"):
        reasons.append("the old build's processes are alive: %s"
                       % "; ".join(state["processes"][:4]))
    if state.get("port_owner"):
        reasons.append("something still listens on 9876: %s"
                       % state["port_owner"])
    if reasons:
        return {
            "ok": False,
            "reason": ("the old runtime has NOT been retired, so a re-cut "
                       "is forbidden: " + " | ".join(reasons) +
                       ".  Stop the old build, install the service build, "
                       "then re-run with --require-old-runtime-retired."),
            "state": state,
        }
    return {
        "ok": True,
        "reason": ("no old-build process is alive and nothing listens on "
                   "9876; the re-cut is allowed.  (The build directory "
                   "still exists at %s, but a build that cannot write "
                   "cannot re-degrade the statistics columns.)"
                   % OLD_BUILD_DIR),
        "state": state,
    }


# ---------------------------------------------------------------------------
# the drift measurement
# ---------------------------------------------------------------------------

def drifted_paths(conn: sqlite3.Connection,
                  workspace: Union[str, Path],
                  ) -> List[Dict[str, Any]]:
    """The paths whose projection no longer matches the truth layer.

    For every chunk row that carries a pointer, the fragment is
    REBUILT from the truth file at the row's anchor and compared with
    the stored text.  A path is drifted when at least one of its rows
    rebuilds to something else.

    The comparison is CANONICAL (the system's own ``canonical_text``),
    because the invariant the criterion guards is "the projection
    rebuilds to the truth", not "the bytes are identical" — a line-ending
    difference is not drift, a content difference is.
    """
    from agent.memory.body_pointer import read_fragment
    from agent.memory.naming import canonical_text

    root = Path(workspace)
    try:
        rows = conn.execute(
            "SELECT id, path, body_path, body_anchor, text FROM chunks "
            "WHERE body_path IS NOT NULL AND body_anchor IS NOT NULL "
            "ORDER BY path, start_line, id").fetchall()
    except sqlite3.Error as e:
        return [{"error": "cannot read chunks: %s" % e}]

    # group by path so the truth file is read once per path
    by_path: Dict[str, List[Tuple[Any, ...]]] = {}
    for r in rows:
        by_path.setdefault(r[1], []).append(tuple(r))

    out: List[Dict[str, Any]] = []
    for path, group in sorted(by_path.items()):
        if not path:
            continue
        truth = _resolve_truth(root, path)
        if truth is None:
            # No truth file: the row cannot be checked here.  It is NOT
            # reported as drift (that is the orphan's business), and it
            # is reported as "unmeasurable" so the reader knows the
            # difference between "checked and fine" and "not checked".
            out.append({"path": path, "status": "unmeasurable",
                        "reason": "the truth file does not exist",
                        "rows": len(group), "drifted_rows": 0})
            continue
        drifted = 0
        checked = 0
        sample = None
        for (cid, _p, bp, anchor, text) in group:
            frag = read_fragment(truth, anchor)
            if frag is None:
                continue
            checked += 1
            if canonical_text(frag) != canonical_text(text or ""):
                drifted += 1
                if sample is None:
                    sample = {"id": cid, "anchor": anchor,
                              "stored_len": len(text or ""),
                              "rebuilt_len": len(frag)}
        if drifted:
            stat = truth.stat()
            out.append({
                "path": path,
                "status": "drifted",
                "rows": len(group),
                "checked_rows": checked,
                "drifted_rows": drifted,
                "truth_file": str(truth),
                "truth_mtime": int(stat.st_mtime),
                "oldest_chunk_created_at": _oldest_created(conn, path),
                "lag_seconds": _lag_seconds(conn, path, int(stat.st_mtime)),
                "sample": sample,
            })
    return out


def _resolve_truth(workspace: Path, relpath: str) -> Optional[Path]:
    """The truth file behind an index key (the ONE resolution rule)."""
    from agent.memory.body_pointer import resolve_truth_file

    try:
        return resolve_truth_file(workspace, relpath)
    except Exception:
        return None


def _oldest_created(conn: sqlite3.Connection, path: str) -> Optional[int]:
    try:
        row = conn.execute(
            "SELECT MIN(created_at) FROM chunks WHERE path=?", (path,)
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _lag_seconds(conn: sqlite3.Connection, path: str,
                 truth_mtime: int) -> Optional[int]:
    """How far behind the projection is, in seconds.

    Reported for the human.  The DECISION to re-cut is made on the
    content, never on this number — an mtime can be forged.
    """
    created = _oldest_created(conn, path)
    if not created:
        return None
    return int(truth_mtime) - int(created)


# ---------------------------------------------------------------------------
# the re-cut
# ---------------------------------------------------------------------------

def recut_library(workspace: Union[str, Path],
                  db: Union[str, Path],
                  *,
                  dry_run: bool = True,
                  name: str = "",
                  require_old_runtime_retired: bool = False,
                  backup_dir: Optional[Union[str, Path]] = None,
                  ) -> Dict[str, Any]:
    """Re-cut the drifted files of ONE library.

    The dry-run is the default.  ``dry_run=False`` writes, and it
    refuses without the environment assertion.
    """
    root = Path(workspace)
    dbp = Path(db)
    out: Dict[str, Any] = {
        "library": name or dbp.parent.parent.parent.name or str(dbp),
        "db": str(dbp),
        "dry_run": bool(dry_run),
        "drifted_paths": [],
        "recut_paths": [],
        "recut": [],
        "failed": 0,
        "skipped": [],
        "environment": None,
    }
    if not dbp.is_file():
        out["error"] = "the index does not exist"
        return out

    # --- the environment gate -----------------------------------------
    gate = assert_old_runtime_retired()
    out["environment"] = gate
    if not dry_run:
        if not require_old_runtime_retired:
            out["error"] = (
                "REFUSED: the re-cut is forbidden without an explicit "
                "assertion that the old runtime has been retired.  Pass "
                "--require-old-runtime-retired once the old build is "
                "stopped and the service build is installed.")
            return out
        if not gate["ok"]:
            out["error"] = "REFUSED: " + gate["reason"]
            return out

    conn = sqlite3.connect("file:%s?mode=ro" % str(dbp).replace("\\", "/"),
                           uri=True)
    try:
        drifted = drifted_paths(conn, root)
    finally:
        conn.close()
    out["drifted_paths"] = drifted
    real = [d for d in drifted if d.get("status") == "drifted"]
    out["drifted_count"] = len(real)

    if dry_run:
        # What WOULD happen, spelled out per path.  Nothing is written.
        out["plan"] = [{
            "path": d["path"],
            "truth_file": d.get("truth_file"),
            "drifted_rows": d.get("drifted_rows"),
            "rows": d.get("rows"),
            "lag_seconds": d.get("lag_seconds"),
            "would": ("re-cut this path from the truth file; the chunk "
                      "ids are position-derived, so they move, and the "
                      "ledger entries of the ids that did not come back "
                      "are pruned in the same transaction"),
        } for d in real]
        out["unmeasurable"] = [d["path"] for d in drifted
                                if d.get("status") == "unmeasurable"]
        return out

    # --- the real thing ------------------------------------------------
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.release_state import ReleaseStateCache
    from agent.memory.storage import MemoryStorage

    try:
        storage = MemoryStorage(str(dbp))
    except Exception as e:
        out["error"] = "cannot open the index for writing: %s" % e
        return out
    cache = ReleaseStateCache(root)
    try:
        for d in real:
            truth = Path(d["truth_file"])
            try:
                res = rebuild_file(storage, root, truth,
                                   release_state=cache)
                out["recut"].append({"path": d["path"],
                                     "chunks": res.get("chunk_count"),
                                     "result": "recut"})
                out["recut_paths"].append(d["path"])
            except Exception as e:
                out["failed"] += 1
                out["skipped"].append({"path": d["path"],
                                        "error": "%s: %s" % (type(e).__name__, e)})
    finally:
        try:
            storage.close()
        except Exception:
            pass

    # Read the AFTER state back from the file, so the report states the
    # terminal state rather than the intended one.
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(dbp).replace("\\", "/"),
                               uri=True)
        try:
            after = drifted_paths(conn, root)
            out["drifted_after"] = len([d for d in after
                                         if d.get("status") == "drifted"])
            out["drifted_before"] = len(real)
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    out["recut_count"] = len(out["recut_paths"])
    return out


def recut_family(workspace: Union[str, Path],
                 *,
                 dry_run: bool = True,
                 require_old_runtime_retired: bool = False,
                 backup_dir: Optional[Union[str, Path]] = None,
                 ) -> Dict[str, Any]:
    """Re-cut the drifted files of EVERY live library."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    for lib in enumerate_live(root):
        rep = recut_library(lib["workspace"], lib["db"], dry_run=dry_run,
                            name=lib["name"],
                            require_old_runtime_retired=require_old_runtime_retired,
                            backup_dir=backup_dir)
        reports.append(rep)
    return {
        "workspace": str(root),
        "dry_run": bool(dry_run),
        "library_count": len(reports),
        "libraries": reports,
        "drifted_before": sum(r.get("drifted_count", 0) for r in reports),
        "drifted_after": sum(r.get("drifted_after", 0) or 0 for r in reports),
        "recut_count": sum(r.get("recut_count", 0) for r in reports),
        "failed": sum(r.get("failed", 0) for r in reports),
    }


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    lines = []
    lines.append("drift re-cut: workspace=%s libraries=%s dry_run=%s"
                 % (report.get("workspace"), report.get("library_count"),
                    report.get("dry_run")))
    lines.append("drifted paths: %s -> %s   re-cut: %s   failed: %s"
                 % (report.get("drifted_before"), report.get("drifted_after"),
                    report.get("recut_count"), report.get("failed")))
    for rep in report.get("libraries", []):
        if not rep.get("drifted_count"):
            continue
        lines.append("--- %s : %d drifted path(s) ---"
                     % (rep.get("library"), rep.get("drifted_count")))
        for d in rep.get("drifted_paths", []):
            if d.get("status") != "drifted":
                lines.append("    [unmeasurable] %s (%s)"
                             % (d.get("path"), d.get("reason")))
                continue
            lines.append("    %-58s rows=%-4s drifted=%-4s lag=%ss"
                         % ((d.get("path") or "")[:58], d.get("rows"),
                            d.get("drifted_rows"), d.get("lag_seconds")))
            if verbose:
                lines.append("        truth=%s" % d.get("truth_file"))
                lines.append("        sample=%s" % d.get("sample"))
        if rep.get("error"):
            lines.append("    ERROR: %s" % rep["error"])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.drift_recut --workspace <ws> [--apply]``."""
    ap = argparse.ArgumentParser(
        description="Re-cut the files whose projection drifted from the "
                    "truth layer.  DRY-RUN IS THE DEFAULT: --apply is "
                    "required to write, and it refuses unless the old "
                    "runtime has been retired.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually re-cut (without it, nothing is written)")
    ap.add_argument("--require-old-runtime-retired", action="store_true",
                    help="assert that the old desktop build can no longer "
                         "write the live libraries (required by --apply)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    report = recut_family(args.workspace, dry_run=not args.apply,
                          require_old_runtime_retired=(
                              args.require_old_runtime_retired))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report, verbose=args.verbose))
    return 0 if not report.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
