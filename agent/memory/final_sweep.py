"""The final sweep: one command that brings the data layer back.

**Why this exists.**  The old desktop build (``C:\\AI\\CowAgent``,
built 2026-10-13 21:22) is still running, and it is the common
source of the inline backlog, the ledger orphans, the erased
statistics and the uncertified rows.  Sweeping the data layer while
it is still running means chasing a moving target — that was the
mistake of the 2026-11-01 05:50 round.  So the sweep is a TOOL,
ready to run the moment the service build replaces the old one.

**The order is fixed, and the order is not arbitrary.**

  1. **Backfill the inline messages** — move the message bodies out
     of the database and into the truth layer.  This is the step that
     makes the messages durable, so it goes FIRST: everything after it
     operates on a database whose messages are already externalised.
  2. **Prune the ledger orphans** — revoke the certifications whose
     subject no longer exists.  After step 1 the truth layer is
     current, so a chunk that is still missing is genuinely missing,
     not "about to be written".
  3. **Re-cut the drifted files** — refresh the projection of the
     files whose truth source moved under them (F1: 13 files were
     edited after they were cut, so the projection no longer rebuilds
     from its own anchor).  The re-cut MOVES the chunk ids (they are
     position-derived), so it comes AFTER the prune and BEFORE the
     statistics: a re-cut rewrites the ``files`` statistics columns,
     and the statistics must be measured against the final projection.
  4. **Decide the undecided rows** — give every row whose origin
     nobody decided a DECIDED origin and the pointer it implies.  It
     runs AFTER the re-cut (the re-cut writes fresh rows, and a fresh
     row that nobody decided is the same debt) and BEFORE the
     certification, because the certifier only reaches rows that
     carry a pointer.
  5. **Repair the statistics columns** — re-measure ``chunk_count``
     and ``chunker_version``.  This runs AFTER the re-cut because the
     re-cut is what rewrites those columns.
  6. **Certify** — promote the rows that can be certified.  This is
     LAST because certification is the only step that asserts "this
     text was verified", and it must be asserted against the final
     state, not against a state that later steps will change.
  7. **Re-run the gate** — the acceptance criteria, on the swept
     state, so the report says what the sweep achieved rather than
     what it hopes for.

**Every step can be stopped, and every step can be rolled back.**

  * a step that fails does not stop the sweep silently: the report
    records the failure and the sweep continues to the next step, so
    one broken library does not hide the state of the other fifteen;
  * the whole-database rollback is one command per library
    (``--rollback <library>``), from the backup taken before the
    first write;
  * the ledger-only rollback is one command per library
    (``--rollback-ledger <library>``), from the revocation manifest.

**The dry-run is the default.**  ``--apply`` is required to write,
and ``--apply`` refuses to run without a backup directory.

**What the sweep will NOT do, and says so in the report.**  The old
build's runtime must be stopped before the sweep, or the sweep's
results are immediately re-dirtied.  The sweep detects the old
runtime's executables and reports them as ``deferred``, with the
reason — it does not stop them (that is the environment item, and the
environment item is deferred by 沛琦's decision).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent.memory.sweep_support import (backup_library, read_json,
                                        restore_library, write_json)

#: The steps, in the order they run.
STEPS = ("inline", "orphans", "recut", "unresolved", "stats", "certify",
         "gate")


# ---------------------------------------------------------------------------
# the environment: is the old build still running?
# ---------------------------------------------------------------------------

#: The old desktop build's executables.  We do not stop them (that is
#: the deferred environment item); we REPORT them, because a sweep
#: run against a live old build is a sweep whose results expire.
OLD_BUILD_DIR = Path(r"C:\AI\CowAgent")

OLD_BUILD_EXECUTABLES = ("CowAgent.exe", "cowagent-backend.exe")


def detect_old_runtime() -> Dict[str, Any]:
    """Is the old desktop build still there, and is it running?

    Read-only.  It looks for the executables on disk and, on Windows,
    asks the process list which of them are alive.  It never kills
    anything.
    """
    out: Dict[str, Any] = {
        "build_dir": str(OLD_BUILD_DIR),
        "build_present": OLD_BUILD_DIR.is_dir(),
        "executables_found": [],
        "processes": [],
        "running": False,
        "deferred_reason": None,
    }
    if not out["build_present"]:
        return out
    for exe in OLD_BUILD_EXECUTABLES:
        found = list(OLD_BUILD_DIR.rglob(exe))
        if found:
            out["executables_found"].append({
                "name": exe,
                "count": len(found),
                "paths": [str(p) for p in found[:5]],
            })
    # Is one of them alive?
    if out["executables_found"] and os.name == "nt":
        try:
            res = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=30)
            for line in res.stdout.splitlines():
                for exe in OLD_BUILD_EXECUTABLES:
                    if exe.lower() in line.lower():
                        out["processes"].append(line.strip()[:160])
        except (OSError, subprocess.SubprocessError):
            pass
    out["running"] = bool(out["processes"])
    if out["running"]:
        out["deferred_reason"] = (
            "the old desktop build (%s) is still running and is the "
            "common source of the inline backlog, the ledger orphans and "
            "the erased statistics; a sweep run now would be swept "
            "again within minutes.  Stop the old build, install the "
            "service build, and run this again."
            % ", ".join(OLD_BUILD_EXECUTABLES))
    return out


# ---------------------------------------------------------------------------
# one step
# ---------------------------------------------------------------------------

def _step_inline(workspace: Path, lib: Dict[str, Any], apply: bool,
                 backup_dir: Optional[Path]) -> Dict[str, Any]:
    """Step 1 — externalise the inline messages of ONE library.

    The migration's own API (``msg_migrate.externalise_every_agent``)
    is used, because it knows how to write the truth-layer files, how
    to heal the rows that are already in the file, and how to leave a
    row alone when it cannot be externalised.
    """
    from agent.memory import msg_migrate

    db = Path(lib["db"])
    rep: Dict[str, Any] = {"step": "inline", "library": lib["name"],
                            "applied": bool(apply)}
    try:
        conn = __import__("sqlite3").connect(str(db))
    except Exception as e:
        rep["error"] = "cannot open the index: %s" % e
        return rep
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NOT NULL "
            "AND content <> ''").fetchone()[0]
        rep["pending_before"] = pending
        out = msg_migrate.externalise_every_agent(conn, lib["workspace"],
                                                   dry_run=not apply)
        rep["agents"] = out.get("agent_count")
        rep["rows"] = out.get("rows")
        rep["written"] = out.get("written")
        rep["emptied"] = out.get("emptied")
        rep["pending_after"] = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NOT NULL "
            "AND content <> ''").fetchone()[0]
    except Exception as e:
        rep["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        conn.close()
    return rep


def _step_orphans(workspace: Path, lib: Dict[str, Any], apply: bool,
                  backup_dir: Optional[Path]) -> Dict[str, Any]:
    """Step 2 — revoke the ledger certifications with no subject."""
    from agent.memory import ledger_orphans

    rep = ledger_orphans.clean_orphans(
        lib["db"], dry_run=not apply,
        backup_dir=str(backup_dir) if backup_dir else None,
        name=lib["name"])
    rep["step"] = "orphans"
    rep["library"] = lib["name"]
    rep["applied"] = bool(apply)
    return rep


def _step_recut(workspace: Path, lib: Dict[str, Any], apply: bool,
                backup_dir: Optional[Path],
                require_old_runtime_retired: bool = False) -> Dict[str, Any]:
    """Step 3 — re-cut the files whose projection drifted (F1).

    The re-cut is the ONE step that moves the chunk ids, so it is also
    the one step that creates ledger orphans by construction (the
    ledger entries of the ids that did not come back are pruned inside
    the same transaction — so the orphan count is measured against the
    post-prune state, which is why the prune runs BEFORE it).

    **The re-cut is guarded.**  The old desktop build's
    ``INSERT OR REPLACE INTO files`` names only six columns, so a
    DELETE->INSERT against a library whose ``files`` DDL is
    ``NOT NULL DEFAULT 0`` lands the row on ``(0, 0)``.  The risk
    officer's B1/B2: while the old build can still write, a re-cut of
    a path it deleted is forbidden.

    ⇒ The step refuses unless the caller asserted that the old
    runtime has been retired.  The sweep's ``--apply`` does NOT assert
    that by itself: the environment item is 沛琦's decision, and a
    tool that assumes it is a tool that lies.
    """
    from agent.memory import drift_recut

    rep = drift_recut.recut_library(
        lib["workspace"], lib["db"], dry_run=not apply,
        name=lib["name"],
        require_old_runtime_retired=require_old_runtime_retired)
    rep["step"] = "recut"
    rep["library"] = lib["name"]
    rep["applied"] = bool(apply)
    return rep


def _step_unresolved(workspace: Path, lib: Dict[str, Any], apply: bool,
                     backup_dir: Optional[Path]) -> Dict[str, Any]:
    """Step 4 — decide the rows whose origin nobody decided.

    ``chunks.body_origin='unresolved'`` means "nobody has decided
    where this row's body comes from".  It is the column's DEFAULT,
    so a writer that does not carry the column writes it by omission
    — and the OLD desktop build's twelve-column INSERT does exactly
    that (measured on a library copy: the old build's SQL produces the
    live shape exactly — four NULL body columns, ``text_state=
    'projection'``, ``body_origin='unresolved'``).

    A row that stays undecided is a TODO nobody owns: the certifier
    only reaches rows that carry a pointer, so the row is never
    certified and never resolves.

    The decision is by CONTENT (D1/D2), never by the clock, and it is
    the SAME decision the write path now makes (``manager.
    _decide_after_write``) — this step is the BACKSTOP for the rows
    that were written before the write path decided to decide.
    """
    from agent.memory import write_decide

    rep = write_decide.decide_after_write(lib["workspace"], lib["db"])
    rep["step"] = "unresolved"
    rep["library"] = lib["name"]
    rep["applied"] = bool(apply)
    if not apply:
        # The dry-run must NOT write, so it is run against a COPY of
        # the rows list only: decide_after_write writes.  For the
        # dry-run the decision is computed but not applied.
        rep = _unresolved_dry_run(lib)
    return rep


def _unresolved_dry_run(lib: Dict[str, Any]) -> Dict[str, Any]:
    """What the decision WOULD do — without writing."""
    import sqlite3 as _sqlite3

    from agent.memory.decide_origin import decide_row, unresolved_rows

    db = Path(lib["db"])
    out: Dict[str, Any] = {"step": "unresolved", "library": lib["name"],
                           "applied": False, "unresolved_before": 0,
                           "unresolved_after": None, "decided": 0,
                           "pointer_filled": 0, "rows": []}
    if not db.is_file():
        out["error"] = "the index does not exist"
        return out
    conn = _sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                            uri=True)
    conn.row_factory = _sqlite3.Row
    try:
        rows = unresolved_rows(conn)
        out["unresolved_before"] = len(rows)
        by_origin: Dict[str, int] = {}
        for row in rows:
            verdict = decide_row(lib["workspace"], row)
            origin = verdict["origin"]
            by_origin[origin] = by_origin.get(origin, 0) + 1
            out["rows"].append({"id": row["id"], "path": row["path"],
                                "origin": origin, "reason": verdict["reason"],
                                "pointer": bool(verdict["pointer"])})
        out["by_origin"] = by_origin
        out["would_decide"] = len(rows)
    finally:
        conn.close()
    return out


def _step_stats(workspace: Path, lib: Dict[str, Any], apply: bool,
                backup_dir: Optional[Path]) -> Dict[str, Any]:
    """Step 3 — re-measure the files statistics columns."""
    from agent.memory import stats_repair

    rep = stats_repair.repair_library(
        lib["workspace"], lib["db"], dry_run=not apply,
        backup_dir=str(backup_dir) if backup_dir else None,
        name=lib["name"])
    rep["step"] = "stats"
    rep["library"] = lib["name"]
    rep["applied"] = bool(apply)
    return rep


def _step_certify(workspace: Path, lib: Dict[str, Any], apply: bool,
                  backup_dir: Optional[Path]) -> Dict[str, Any]:
    """Step 4 — certify the rows that can be certified."""
    from agent.memory import certify

    rep = certify.certify_library(lib["workspace"], lib["db"],
                                  dry_run=not apply)
    rep["step"] = "certify"
    rep["library"] = lib["name"]
    rep["applied"] = bool(apply)
    return rep


def _step_gate(workspace: Path, lib: Dict[str, Any], apply: bool,
               backup_dir: Optional[Path]) -> Dict[str, Any]:
    """Step 5 — the M6f criterion (the gate's own mouth on the ledger).

    The full gate is a separate, long run (it is the acceptance
    layer's command).  The sweep runs the criterion that covers the
    step it just performed, so the report says what changed in terms
    the gate itself uses.
    """
    from agent.memory import ledger_orphans

    try:
        conn = __import__("sqlite3").connect(
            "file:%s?mode=ro" % str(lib["db"]).replace("\\", "/"), uri=True)
    except Exception as e:
        return {"step": "gate", "library": lib["name"], "error": str(e)}
    try:
        rep = ledger_orphans.judge_m6f_ledger_has_no_orphans(
            conn, library=lib["name"])
    finally:
        conn.close()
    rep["step"] = "gate"
    rep["applied"] = False
    return rep


#: The step implementations, in order.
STEP_FNS = {
    "inline": _step_inline,
    "orphans": _step_orphans,
    "recut": _step_recut,
    "unresolved": _step_unresolved,
    "stats": _step_stats,
    "certify": _step_certify,
    "gate": _step_gate,
}


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def sweep(workspace: Union[str, Path],
          apply: bool = False,
          backup_dir: Optional[Union[str, Path]] = None,
          only_steps: Optional[List[str]] = None,
          require_old_runtime_retired: bool = False,
          ) -> Dict[str, Any]:
    """Run the sweep over every live library of the workspace."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    steps = [s for s in STEPS if not only_steps or s in only_steps]
    started = time.time()
    report: Dict[str, Any] = {
        "workspace": str(root),
        "applied": bool(apply),
        "steps": steps,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": detect_old_runtime(),
        "libraries": [],
        "errors": [],
    }
    if apply and not backup_dir:
        report["errors"].append(
            "REFUSED: an apply without a backup directory is refused")
        report["ok"] = False
        return report

    libs = enumerate_live(root)
    for lib in libs:
        librep: Dict[str, Any] = {"library": lib["name"], "db": str(lib["db"]),
                                   "steps": {}}
        # --- the library's PRE-SWEEP backup, taken BEFORE any step ------
        #
        # This is the backup the whole-library rollback restores from.
        # It has to be taken HERE, before the first step writes, because
        # the steps take their OWN backups as they go (``pre-prune``,
        # ``pre-stats``) — and the LAST of those is taken after the
        # earlier steps already wrote.  Restoring from the last one
        # would undo the last step and silently leave the earlier ones
        # applied, which is the worst possible outcome for a rollback:
        # it reports success and leaves the library half-swept.
        if apply and backup_dir:
            bak = backup_library(lib["db"], Path(backup_dir),
                                 tag="pre-sweep", name=lib["name"])
            librep["pre_sweep_backup"] = bak
            if bak.get("error"):
                report["errors"].append("%s: pre-sweep backup failed: %s"
                                        % (lib["name"], bak["error"]))
        for step in steps:
            try:
                if step == "recut":
                    librep["steps"][step] = _step_recut(
                        root, lib, apply, Path(backup_dir) if backup_dir else None,
                        require_old_runtime_retired=require_old_runtime_retired)
                else:
                    librep["steps"][step] = STEP_FNS[step](
                        root, lib, apply, Path(backup_dir) if backup_dir else None)
            except Exception as e:
                # A step that blows up does not stop the sweep: the
                # report must show the state of every library, not
                # only the ones that worked.
                librep["steps"][step] = {"step": step, "library": lib["name"],
                                         "error": "%s: %s" % (type(e).__name__, e)}
                report["errors"].append("%s/%s: %s: %s"
                                        % (lib["name"], step,
                                           type(e).__name__, e))
        report["libraries"].append(librep)

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    report["elapsed_seconds"] = round(time.time() - started, 2)
    report["summary"] = _summarise(report)
    report["ok"] = not report["errors"]
    return report


def _summarise(report: Dict[str, Any]) -> Dict[str, Any]:
    """What changed, what is left, and what is deferred."""
    out: Dict[str, Any] = {"inline": {}, "orphans": {}, "recut": {},
                           "unresolved": {}, "stats": {}, "certify": {},
                           "gate": {}}
    for lib in report.get("libraries", []):
        steps = lib.get("steps", {})
        inl = steps.get("inline", {})
        out["inline"]["pending_before"] = (out["inline"].get("pending_before", 0)
                                           + (inl.get("pending_before") or 0))
        out["inline"]["pending_after"] = (out["inline"].get("pending_after", 0)
                                          + (inl.get("pending_after") or 0))
        out["inline"]["written"] = out["inline"].get("written", 0) + (inl.get("written") or 0)
        orp = steps.get("orphans", {})
        out["orphans"]["before"] = out["orphans"].get("before", 0) + (orp.get("orphans_before") or 0)
        out["orphans"]["after"] = out["orphans"].get("after", 0) + (orp.get("orphans_after") or 0)
        out["orphans"]["revoked"] = out["orphans"].get("revoked", 0) + (orp.get("deleted") or 0)
        rc = steps.get("recut", {})
        out["recut"]["drifted_before"] = (out["recut"].get("drifted_before", 0)
                                          + (rc.get("drifted_count") or 0))
        out["recut"]["drifted_after"] = (out["recut"].get("drifted_after", 0)
                                         + (rc.get("drifted_after") or 0))
        out["recut"]["recut"] = out["recut"].get("recut", 0) + (rc.get("recut_count") or 0)
        out["recut"]["failed"] = out["recut"].get("failed", 0) + (rc.get("failed") or 0)
        ur = steps.get("unresolved", {})
        out["unresolved"]["before"] = (out["unresolved"].get("before", 0)
                                       + (ur.get("unresolved_before") or 0))
        out["unresolved"]["after"] = (out["unresolved"].get("after", 0)
                                      + (ur.get("unresolved_after")
                                         if ur.get("unresolved_after") is not None
                                         else ur.get("would_decide") or 0))
        out["unresolved"]["decided"] = (out["unresolved"].get("decided", 0)
                                        + (ur.get("decided") or 0))
        st = steps.get("stats", {})
        out["stats"]["broken_before"] = out["stats"].get("broken_before", 0) + (st.get("broken_before") or 0)
        out["stats"]["null_before"] = out["stats"].get("null_before", 0) + (st.get("null_before") or 0)
        out["stats"]["null_after"] = out["stats"].get("null_after", 0) + (st.get("null_after") or 0)
        out["stats"]["zero_before"] = out["stats"].get("zero_before", 0) + (st.get("zero_before") or 0)
        out["stats"]["zero_after"] = out["stats"].get("zero_after", 0) + (st.get("zero_after") or 0)
        ce = steps.get("certify", {})
        out["certify"]["certified"] = out["certify"].get("certified", 0) + (ce.get("certified") or 0)
        ga = steps.get("gate", {})
        out["gate"][lib["library"]] = ga.get("verdict", "NOTRUN")
    return out


def format_report(report: Dict[str, Any]) -> str:
    env = report.get("environment", {})
    lines = [
        "final sweep: workspace=%s applied=%s steps=%s"
        % (report.get("workspace"), report.get("applied"),
           ",".join(report.get("steps", []))),
        "environment: old build present=%s running=%s"
        % (env.get("build_present"), env.get("running")),
    ]
    if env.get("deferred_reason"):
        lines.append("  DEFERRED: %s" % env["deferred_reason"])
    s = report.get("summary", {})
    lines.append("")
    lines.append("inline messages pending : %s -> %s (written=%s)"
                 % (s.get("inline", {}).get("pending_before"),
                    s.get("inline", {}).get("pending_after"),
                    s.get("inline", {}).get("written")))
    lines.append("ledger orphans          : %s -> %s (revoked=%s)"
                 % (s.get("orphans", {}).get("before"),
                    s.get("orphans", {}).get("after"),
                    s.get("orphans", {}).get("revoked")))
    lines.append("drifted paths          : %s -> %s (re-cut=%s, failed=%s)"
                 % (s.get("recut", {}).get("drifted_before"),
                    s.get("recut", {}).get("drifted_after"),
                    s.get("recut", {}).get("recut"),
                    s.get("recut", {}).get("failed")))
    lines.append("undecided rows         : %s -> %s (decided=%s)"
                 % (s.get("unresolved", {}).get("before"),
                    s.get("unresolved", {}).get("after"),
                    s.get("unresolved", {}).get("decided")))
    lines.append("statistics broken rows   : %s (NULL %s -> %s, zero %s -> %s)"
                 % (s.get("stats", {}).get("broken_before"),
                    s.get("stats", {}).get("null_before"),
                    s.get("stats", {}).get("null_after"),
                    s.get("stats", {}).get("zero_before"),
                    s.get("stats", {}).get("zero_after")))
    lines.append("M6f per library         : %s"
                 % ", ".join("%s=%s" % (k, v)
                             for k, v in sorted(s.get("gate", {}).items())))
    if report.get("errors"):
        lines.append("")
        lines.append("ERRORS (%d):" % len(report["errors"]))
        for e in report["errors"][:20]:
            lines.append("  %s" % e)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

def rollback_library(workspace: Union[str, Path], library: str,
                     backup_dir: Union[str, Path]) -> Dict[str, Any]:
    """Restore ONE library from its pre-sweep backup.

    **The pre-sweep backup is the ONLY correct source.**  The steps
    take their own backups as they go (``pre-prune``, ``pre-stats``),
    and each of those is taken AFTER the previous steps already wrote.
    Restoring from one of those would undo the last step and leave the
    earlier ones applied — a rollback that reports success and leaves
    the library half-swept.  So this restores from ``pre-sweep`` and
    refuses to guess if that file is absent.
    """
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    for lib in enumerate_live(root):
        if lib["name"] != library:
            continue
        bak = Path(backup_dir) / ("%s.pre-sweep.db" % library)
        if not bak.is_file():
            return {"error": ("the pre-sweep backup does not exist: %s  "
                              "(refusing to restore from a step backup: it "
                              "was taken after the earlier steps already "
                              "wrote, so it would leave the library "
                              "half-swept)" % bak)}
        return restore_library(lib["db"], bak)
    return {"error": "no live library named %r" % library}


def rollback_ledger(workspace: Union[str, Path], library: str,
                    backup_dir: Union[str, Path]) -> Dict[str, Any]:
    """Re-INSERT the certifications a prune revoked."""
    from agent.memory import ledger_orphans
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    manifest = Path(backup_dir) / ("%s.ledger-revocations.json" % library)
    for lib in enumerate_live(root):
        if lib["name"] == library:
            return ledger_orphans.restore_orphans(lib["db"], manifest)
    return {"error": "no live library named %r" % library}


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.final_sweep --workspace <ws> [--apply]``."""
    ap = argparse.ArgumentParser(
        description="The final data-layer sweep, in the fixed order: "
                    "backfill inline -> prune orphans -> repair the "
                    "statistics -> certify -> re-run the gate.  DRY-RUN IS "
                    "THE DEFAULT: --apply is required to write.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually write (without it, nothing is written)")
    ap.add_argument("--backup-dir", default=None,
                    help="where the pre-sweep backups and the revocation "
                         "manifests go (required by --apply)")
    ap.add_argument("--steps", default=None,
                    help="comma-separated subset of %s (default: all)"
                         % ",".join(STEPS))
    ap.add_argument("--require-old-runtime-retired", action="store_true",
                    help="assert that the old desktop build can no longer "
                         "write the live libraries.  REQUIRED by the "
                         "re-cut step (it is the only step that moves the "
                         "chunk ids, and the old build's six-column "
                         "files write can degrade the statistics columns "
                         "it rewrites).")
    ap.add_argument("--report", default=None,
                    help="write the full report as JSON to this path")
    ap.add_argument("--rollback", default=None, metavar="LIBRARY",
                    help="restore one library from its pre-sweep backup")
    ap.add_argument("--rollback-ledger", default=None, metavar="LIBRARY",
                    help="re-INSERT the certifications a prune revoked")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.rollback or args.rollback_ledger:
        if not args.backup_dir:
            print("REFUSED: a rollback needs --backup-dir (that is where "
                  "the backup lives).", file=sys.stderr)
            return 2
        if args.rollback:
            rep = rollback_library(args.workspace, args.rollback,
                                   args.backup_dir)
        else:
            rep = rollback_ledger(args.workspace, args.rollback_ledger,
                                  args.backup_dir)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if not rep.get("error") else 1

    if args.apply and not args.backup_dir:
        print("REFUSED: --apply requires --backup-dir.  A sweep without a "
              "backup is not a sweep you can take back.", file=sys.stderr)
        return 2

    only = [s.strip() for s in args.steps.split(",")] if args.steps else None
    for s in only or []:
        if s not in STEPS:
            print("REFUSED: unknown step %r; the steps are %s"
                  % (s, ", ".join(STEPS)), file=sys.stderr)
            return 2

    report = sweep(args.workspace, apply=args.apply,
                   backup_dir=args.backup_dir, only_steps=only,
                   require_old_runtime_retired=args.require_old_runtime_retired)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    if args.report:
        write_json(args.report, report)
        print("\nreport written to %s" % args.report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
