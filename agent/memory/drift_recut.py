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
explicit assertion that the re-cut is SAFE
(``--require-recut-safe``).  The assertion is the BEHAVIOURAL one
(:mod:`agent.memory.recut_safe`): ``S1 ∧ S2 ∧ (S3 ∨ S4)`` — the guard
is in place, the guard is effective, and the old build either writes
nothing or cannot reach the live libraries.

**Why the assertion changed its OBJECT (2026-11-01, 架构师 T7 裁决).**
The old assertion was "the old runtime has been retired" — a statement
about a PROCESS.  The thing it was protecting is an INVARIANT: a
re-cut's product must not be degraded by a legacy writer.  Judging the
proxy (a process is alive) instead of the invariant (the write boundary
cannot be degraded) is what made the assertion a false red: the old
build was alive, but the guard held on all 16 libraries, so a re-cut
could not have been harmed.

**Changing the assertion did NOT remove the protection.**  The old
assertion is still here as ``--require-old-runtime-retired`` (an alias
that keeps the STRICT form: it demands the old build be GONE), and it
is now the *stronger* of the two.  The new form is what the sweep uses.

**What the re-cut does to the ledger.**  The chunk ids are
position-derived, so a re-cut replaces the ids.  ``replace_file_chunks``
prunes the ledger entries of the ids that did NOT come back, inside the
same transaction — so the ledger never keeps a witness for a row that
does not exist, and the orphan count does NOT grow.

**The drift measurement is CONTENT-based.**  A file's mtime can be
forged by a ``touch``; the chunk ids are position-derived, so a
re-cut moves them wholesale.  The drift is therefore measured by
REBUILDING each chunk's fragment from the truth file and comparing it
to the stored text — the same reading the criterion uses.  The
"how far behind" number is reported for the human, but the DECISION to
re-cut is made on the content.

**The drift measurement has TWO questions, not one (2026-11-05,
交办书 D-1).**  The selector used to ask ONE question — "does the
rebuilt fragment differ from ``chunks.text``?" — and that question has
a blind spot, measured on the live corpus:

  * the rebuilt fragment differs from the stored text ⇒ **body drift**
    (the projection is behind the truth layer); and
  * the rebuilt fragment EQUALS the stored text, but the stored
    ``body_hash`` is not the hash of that fragment ⇒ **stale token**.

The second shape is invisible to the first question, so the re-cut
never selects those paths — **the leak survives every sweep**, and the
INV-3 criterion (``inv3-fragment-hash-mismatch``) stays red forever.
MEASURED on this machine (2026-11-05): 10 rows across 5 libraries were
"body consistent but token stale"; the selector selected **none** of
their paths.  ⇒ Q2 was structurally unfixable by the re-cut.

⇒ The selector now asks **BOTH** questions, and the report keeps them
apart (``body_drift_rows`` vs ``stale_token_rows``) so a reader can see
WHICH kind of drift a path had.  **Both are repaired by the same
operation** (a re-cut re-derives the pointer AND the token from the
truth file), so both must SELECT the path.

**Why the token is compared the way it is.**  The drift token is
``sha256(canonical_text(fragment))`` — the system's own definition
(:func:`agent.memory.body_pointer.body_hash`), never a re-spelling of
it.  A second copy of the hashing rule is exactly how the two sides
end up disagreeing about what "the same text" means.

**What a re-cut does to the ledger.**  The chunk ids are
position-derived, so a re-cut replaces the ids.  ``replace_file_chunks``
prunes the ledger entries of the ids that did NOT come back, inside the
same transaction — so the ledger never keeps a witness for a row that
does not exist.

**WHY the re-cut runs BEFORE the orphan prune (2026-11-01, 架构师 T7
裁决 D-1).**  The reason used to be written down as "a re-cut creates
orphans by construction, and the prune is what removes them".  **That
reason is WRONG, and it was corrected by measurement:** a re-cut on a
really-drifted file moves the ids AND prunes the ledger rows of the ids
that did not come back **in the same transaction**
(``storage.replace_file_chunks``: ``gone_ids = set(old_stats) - {c.id for
c in chunks}`` → ``_prune_ledger_entries(gone_ids)``).  Measured on a
copy of a live library: orphans 0 → 0, content drift 10 → 0, nine old
ids gone and exactly nine ledger rows pruned.  ⇒ **a re-cut does NOT
create orphans; it REMOVES ledger entries.**

The ORDER stays as it is (prune, then re-cut, then prune again is not
what we do — the sweep runs ``orphans`` before ``recut``), because the
order is harmless either way; but the REASON has to change, or the next
person will use the wrong reason to "fix" the order.  The correct
reason for the order is: **the prune runs first so that the sweep's
report shows the debt that existed BEFORE the re-cut** (the re-cut's own
ledger prune would otherwise mask it, and the two numbers would be
confused with each other).

**And the architect's order correction (裁决 §4.2) is in the SWEEP, not
here:** ``recut → orphans → certify → stats → gate``.  This module's
docstring used to carry the wrong reason; the sweep's docstring carried
it too.  Both are corrected in this round.
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
    """The STRICT gate: the old build must be GONE.

    Returns ``{"ok": bool, "reason": str, "state": {...}}``.

    **This is the OLD assertion, kept.**  It is stronger than the
    behavioural one (it demands an environment fact, not an invariant),
    so it is no longer the sweep's default — but it must not be DELETED:
    a tool that can only be used in one narrow environment is a tool
    nobody can reason about, and the strict form is the one that says
    "this environment is clean".

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


def assert_recut_safe(workspace: Union[str, Path],
                       window: int = 0) -> Dict[str, Any]:
    """The BEHAVIOURAL gate: is a re-cut safe?

    Returns ``{"ok": bool, "reason": str, "report": {...}}``.

    **What it judges.**  The invariant, not the proxy: a re-cut's
    product must not be degraded by a legacy writer.  The formula is
    ``S1 ∧ S2 ∧ (S3 ∨ S4)`` — see :mod:`agent.memory.recut_safe`.

    **What it does NOT judge.**  Whether the old build is running.  A
    running old build is an ENVIRONMENT fact; it is reported, and the
    report says which term of the formula failed.

    **Why S2 is the load-bearing term.**  S1 (the triggers exist) says
    nothing about whether they WORK.  S2 is the only term that is
    about the LIBRARY, and therefore the only term that cannot go stale
    between the check and the re-cut.  S3 and S4 are about NOW.
    """
    from agent.memory.recut_safe import judge_recut_safe

    if window and window > 0:
        report = judge_recut_safe(workspace, window=window)
    else:
        # The default window would make a refusal take two minutes; a
        # refusal is cheap to decide, so a SHORT window is used for the
        # verdict and the report says so.
        report = judge_recut_safe(workspace, window=30, sample=10)
    ok = report.get("verdict") == "PASS"
    terms = report.get("formula", {})
    if ok:
        reason = ("the write boundary cannot be degraded: S1 (guard in "
                   "place) ∧ S2 (guard effective) ∧ (S3 (the old build "
                   "writes nothing) ∨ S4 (the old build cannot reach the "
                   "live libraries)) ⇒ the re-cut is allowed.  "
                   "This judges the INVARIANT, not whether the old build "
                   "is running.")
    else:
        failed = [k for k, v in terms.items() if not v]
        reason = ("the re-cut is FORBIDDEN: the formula S1 ∧ S2 ∧ (S3 ∨ "
                   "S4) does not hold (the failing term(s): %s).  "
                   "Detail:\n%s"
                   % (", ".join(failed) or "the formula itself",
                      report.get("detail", "")))
    return {"ok": ok, "reason": reason, "report": report,
            "formula": terms}


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

    **TWO questions, since 2026-11-05 (交办书 D-1).**  A path is
    selected when EITHER of these holds for at least one of its rows:

      ① **body drift** — the rebuilt fragment differs from the stored
         ``text`` (the projection is behind the truth layer); OR
      ② **stale token** — the rebuilt fragment EQUALS the stored
         ``text``, but the stored ``body_hash`` is not the hash of that
         fragment (the token is from somewhere else).

    ② used to be invisible here, so the re-cut never selected those
    paths and the INV-3 red survived every sweep.  Both are repaired by
    the same operation (a re-cut re-derives the pointer AND the token
    from the truth file), so both must select the path.

    The two counts are reported SEPARATELY (``body_drift_rows`` /
    ``stale_token_rows``): a reader must be able to see WHICH kind of
    drift a path had, because the two have different histories (a body
    drift means the file moved; a stale token means the row was copied
    from somewhere else).
    """
    from agent.memory.body_pointer import body_hash, read_fragment
    from agent.memory.naming import canonical_text

    root = Path(workspace)
    try:
        rows = conn.execute(
            "SELECT id, path, body_path, body_anchor, body_hash, text "
            "FROM chunks "
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
                        "rows": len(group), "drifted_rows": 0,
                        "body_drift_rows": 0, "stale_token_rows": 0})
            continue
        body_drifted = 0
        stale_token = 0
        checked = 0
        sample = None
        stale_sample = None
        for (cid, _p, bp, anchor, bhash, text) in group:
            frag = read_fragment(truth, anchor)
            if frag is None:
                continue
            checked += 1
            if canonical_text(frag) != canonical_text(text or ""):
                body_drifted += 1
                if sample is None:
                    sample = {"id": cid, "anchor": anchor,
                              "stored_len": len(text or ""),
                              "rebuilt_len": len(frag)}
                continue
            # ② the STALE TOKEN: the body agrees with the truth, but
            # the drift token does not.  This is the shape the old
            # selector could not see.
            actual = body_hash(frag)
            if (bhash or "") != actual:
                stale_token += 1
                if stale_sample is None:
                    stale_sample = {"id": cid, "anchor": anchor,
                                    "stored_token": (bhash or "")[:32],
                                    "actual_token": actual[:32]}
        drifted = body_drifted + stale_token
        if drifted:
            stat = truth.stat()
            out.append({
                "path": path,
                "status": "drifted",
                "rows": len(group),
                "checked_rows": checked,
                "drifted_rows": drifted,
                "body_drift_rows": body_drifted,
                "stale_token_rows": stale_token,
                "truth_file": str(truth),
                "truth_mtime": int(stat.st_mtime),
                "oldest_chunk_created_at": _oldest_created(conn, path),
                "lag_seconds": _lag_seconds(conn, path, int(stat.st_mtime)),
                "sample": sample,
                "stale_token_sample": stale_sample,
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
                  require_recut_safe: bool = False,
                  recut_safe_window: int = 0,
                  backup_dir: Optional[Union[str, Path]] = None,
                  ) -> Dict[str, Any]:
    """Re-cut the drifted files of ONE library.

    The dry-run is the default.  ``dry_run=False`` writes, and it
    refuses without an explicit safety assertion.

    **Two assertions, and which one is which.**

    * ``require_recut_safe`` — the BEHAVIOURAL one (the architect's
      T7 ruling): ``S1 ∧ S2 ∧ (S3 ∨ S4)``.  It judges the invariant.
    * ``require_old_runtime_retired`` — the STRICT one (the old
      assertion): the old build must be GONE.  It judges an environment
      fact.

    Both are honoured when both are passed: the strict one is stricter,
    so passing it implies the behavioural one passed too (the strict
    form's "the old build is gone" makes S3 and S4 trivially true).
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
        "recut_safe": None,
    }
    if not dbp.is_file():
        out["error"] = "the index does not exist"
        return out

    # --- the environment gate -----------------------------------------
    #
    # The STRICT gate is always measured (it is cheap and it is the
    # environment fact the report must carry).  The BEHAVIOURAL gate is
    # measured when it is the one being asserted, and when the strict
    # one already holds it is not needed: "the old build is gone" makes
    # S3 and S4 trivially true, and S1/S2 are the library's own
    # property — which the strict gate cannot vouch for.
    gate = assert_old_runtime_retired()
    out["environment"] = gate
    if require_recut_safe or not require_old_runtime_retired:
        safe = assert_recut_safe(root, window=recut_safe_window)
        out["recut_safe"] = safe
    if not dry_run:
        if not require_recut_safe and not require_old_runtime_retired:
            out["error"] = (
                "REFUSED: the re-cut is forbidden without an explicit "
                "safety assertion.  Pass --require-recut-safe (the "
                "behavioural one: S1∧S2∧(S3∨S4) — the write boundary "
                "cannot be degraded) or --require-old-runtime-retired "
                "(the strict one: the old build must be gone).")
            return out
        if require_recut_safe and not out["recut_safe"]["ok"]:
            out["error"] = "REFUSED: " + out["recut_safe"]["reason"]
            return out
        if require_old_runtime_retired and not gate["ok"]:
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
            "body_drift_rows": d.get("body_drift_rows"),
            "stale_token_rows": d.get("stale_token_rows"),
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
                 require_recut_safe: bool = False,
                 recut_safe_window: int = 0,
                 backup_dir: Optional[Union[str, Path]] = None,
                 ) -> Dict[str, Any]:
    """Re-cut the drifted files of EVERY live library.

    **The safety assertion is measured ONCE for the family, not once
    per library.**  S3 and S4 are about the ENVIRONMENT (one window, one
    port) and S1/S2 are about the LIBRARIES (sixteen of them).  A
    per-library window would take 16 × 120 s and would measure the same
    environment sixteen times.  The family-level verdict is therefore
    computed once and handed down — and the per-library report says
    which libraries it covered.
    """
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    family_safe: Optional[Dict[str, Any]] = None
    for lib in enumerate_live(root):
        rep = recut_library(lib["workspace"], lib["db"], dry_run=dry_run,
                            name=lib["name"],
                            require_old_runtime_retired=require_old_runtime_retired,
                            require_recut_safe=require_recut_safe,
                            recut_safe_window=recut_safe_window,
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
            lines.append("    %-58s rows=%-4s drifted=%-4s (body=%-4s "
                         "token=%-4s) lag=%ss"
                         % ((d.get("path") or "")[:58], d.get("rows"),
                            d.get("drifted_rows"),
                            d.get("body_drift_rows"),
                            d.get("stale_token_rows"),
                            d.get("lag_seconds")))
            if verbose:
                lines.append("        truth=%s" % d.get("truth_file"))
                lines.append("        sample=%s" % d.get("sample"))
                lines.append("        stale_token_sample=%s"
                             % d.get("stale_token_sample"))
        if rep.get("error"):
            lines.append("    ERROR: %s" % rep["error"])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.drift_recut --workspace <ws> [--apply]``."""
    ap = argparse.ArgumentParser(
        description="Re-cut the files whose projection drifted from the "
                    "truth layer.  DRY-RUN IS THE DEFAULT: --apply is "
                    "required to write, and it refuses without an "
                    "explicit safety assertion.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually re-cut (without it, nothing is written)")
    ap.add_argument("--require-recut-safe", action="store_true",
                    help="assert that the re-cut is SAFE: the write "
                         "boundary cannot be degraded (S1∧S2∧(S3∨S4)).  "
                         "This judges the INVARIANT, not whether the old "
                         "build is running.")
    ap.add_argument("--require-old-runtime-retired", action="store_true",
                    help="assert that the old desktop build can no longer "
                         "write the live libraries (the STRICT form; it "
                         "demands the old build be GONE, not merely unable "
                         "to do harm)")
    ap.add_argument("--recut-safe-window", type=int, default=0,
                    help="the S3 observation window in seconds (0 = the "
                         "tool's default)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    report = recut_family(args.workspace, dry_run=not args.apply,
                          require_old_runtime_retired=(
                              args.require_old_runtime_retired),
                          require_recut_safe=args.require_recut_safe,
                          recut_safe_window=args.recut_safe_window)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report, verbose=args.verbose))
    return 0 if not report.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
