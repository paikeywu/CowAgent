"""External-edit provenance — make an out-of-band edit ENTER the change log.

T2c (2026-11-01) · the hole this module closes
==============================================

**The measured root cause** (evidence in the T2c delivery doc):

1.  The change log is NOT a SQLite table.  It is an APPEND-ONLY JSONL
    file at ``<workspace>/memory/.audit/file_changes.jsonl``, written by
    :func:`agent.memory.changes.record_change`.

2.  Only FOUR call sites ever write to it, and every one of them is a
    **write path**:

    ==========================  =======================  ==============
    source                       call site                who triggers it
    ==========================  =======================  ==============
    ``facade-write``             ``facade.write``         our own API
    ``facade-element``           ``facade.write_element`` our own API
    ``import-human-edit``        ``facade.import_human_edit`` our own API
    ``rebuilder``                ``rebuilder.rebuild_file`` our own API
    ==========================  =======================  ==============

3.  ``agent/memory/manager.py`` — the module that ACTUALLY READS THE
    DISK and decides what is stale — **never imports ``changes`` at
    all** (measured: zero occurrences of ``changes.`` in the file).
    Its scan loop compares the file's content hash against
    ``files.hash`` and, when they differ, re-cuts the file and updates
    the ``files`` row.  **It never records a change event.**

⇒ **The consequence (this is the hole):** a file edited OUTSIDE our
write paths (a human in an editor, the ``write`` tool, a persona
rewrite script, another process) is *indexed* but *never logged*.
The change log therefore says "the last truth-layer change was
2026-10-30T05:32" while the persona was rewritten on 2026-11-01
04:59.  DR-2 correctly reports "no source" — the persona moved and
memory cannot say why.

**Why "only an API counts" IS the hole, not a feature:** the design
note says "the change log is the single signal source", and the
single signal source has exactly the shape of *our own writes*.  A
signal source that cannot see the writes it is supposed to witness is
not a signal source.

What this module does
=====================

It adds a **reconciliation pass**: a scan that compares what is ON
DISK against what the change log has witnessed, and records the
difference.

The rules that keep this from becoming a lie
--------------------------------------------

* **Content-based, never clock-based.**  The decision "did this file
  change?" is made by comparing the file's CURRENT canonical content
  hash against the hash the change log witnessed — NOT by comparing
  mtimes.  A ``touch`` must not fabricate a change; a change that
  preserves the mtime must not be lost.  (This is the same "content,
  not clock" rule the stats repairer uses.)

* **Never refreshes on read.**  Recording is idempotent: if the
  content hash already witnessed for this path is the current one,
  nothing is written.  So a scan that runs every minute does not
  grow the log and does not make ``latest_change_mtime`` advance —
  which would break the shelf-lag invariant (``text_inv``) by making
  every page look perpetually stale.

* **Never rewrites history.**  The log stays APPEND-ONLY: a new
  event is appended, an old event is never edited or removed.  That
  is the log's contract and this module does not weaken it.

* **Never raises into the caller's path.**  A reconciliation is a
  best-effort observation.  A permission error reading a file must
  not take down a sync.

Where it hooks in
=================

``MemoryManager.sync`` calls :func:`reconcile_external_edits` at the
END of its scan, right after it has decided which files changed.  At
that point the manager already knows the truth layer's state; this
pass adds the missing WITNESS.

The witness hash is stored where?  The change log's event schema
already carries an ``extra`` dict, and ``record_change`` merges it
into the event with ``event.update(extra)`` — **flat, not nested**.
The witness therefore lives under the top-level key
``content_sha256``.  A reader that looked for
``event["extra"]["content_sha256"]`` would find nothing and would
treat every event as unwitnessed ⇒ it would re-record every file on
every scan ⇒ the log would grow without bound and
``latest_change_mtime`` would advance on every read.  That is exactly
the "every read refreshes the stamp" failure this module is required
to avoid, so the reader matches the writer.

Existing semantics that this must NOT break
===========================================

* ``changes.latest_change_mtime`` — the newest ``mtime``/``at`` over
  all events.  This module appends events with the file's REAL
  mtime, so the newest change is still the newest change.  It does
  NOT re-stamp old events, and it does NOT append an event when
  nothing changed ⇒ ``latest_change_mtime`` does not advance on a
  no-op scan.

* ``text_inv.check_shelf_lag`` — compares the newest logged change
  against the shelf pages' ``generated_at``.  A reconciled event
  carries the file's real mtime, so a shelf page older than the
  edit is correctly reported stale.  (That is the DESIRED outcome:
  the shelf IS stale after an external edit, and the log finally
  says so.)

* ``SOURCES`` — a closed set that ``record_change`` validates.  This
  module adds ONE member, ``external-edit``, and nothing else.  The
  four existing members keep their meaning.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.memory import changes
from agent.memory.naming import canonical_text
from agent.memory.paths import normalize_relpath

#: The change-log source name for a change we did not make through
#: our own write path.
#:
#: It is a NEW member of :data:`agent.memory.changes.SOURCES`.  The
#: existing four mean "our write path did this"; this one means "the
#: disk moved and we reconciled it afterwards".  Keeping it separate is
#: what lets a reader tell a witnessed-by-construction change from a
#: witnessed-after-the-fact one.
SOURCE_EXTERNAL = "external-edit"

#: The key under which the witnessed content hash travels inside the
#: event.  FLAT (top-level), because ``record_change`` merges its
#: ``extra`` dict with ``event.update(extra)``.
WITNESS_KEY = "content_sha256"

#: Files larger than this are not hashed (their content hash is not
#: needed to answer "did it change?" — and hashing a 200 MB file on
#: every scan is not a thing a memory manager should do silently).
MAX_WITNESS_BYTES = 8 * 1024 * 1024


def _sha256_file(path: Path) -> Optional[str]:
    """The file's CANONICAL content hash, or None when it cannot be read.

    Returns None — NOT a digest of an empty file — so that an
    unreadable file is never mistaken for "unchanged".

    **Canonical, not raw.**  The witness goes through
    ``naming.canonical_text`` (LF endings, outer whitespace stripped,
    blank-line runs folded) — the ONE canonicalizer the rest of the
    memory system hashes with.  So a CRLF/LF conversion or a trailing
    newline does NOT read as "the persona changed", while a real
    content change does.  A file that is not valid UTF-8 falls back
    to its raw bytes (it still has to be witnessed somehow).
    """
    try:
        if not path.is_file():
            return None
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > MAX_WITNESS_BYTES:
            return None
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return "raw:" + hashlib.sha256(raw_bytes).hexdigest()
        return hashlib.sha256(
            canonical_text(text).encode("utf-8")).hexdigest()
    except OSError:
        return None


def _witnessed_hashes(workspace_root: Path | str) -> Dict[str, str]:
    """The newest witnessed content hash PER PATH, from the change log.

    The log is APPEND-ONLY, so the LAST event for a path carries the
    newest witness.  Events that predate this module carry no witness
    hash ⇒ the path is treated as unwitnessed (fail-closed: an
    unwitnessed file is a file we cannot vouch for).
    """
    out: Dict[str, str] = {}
    for e in changes.read_changes(workspace_root):
        path = e.get("path")
        if not path:
            continue
        digest = e.get(WITNESS_KEY)
        if not digest:
            # Older event: no witness recorded.  We must NOT treat the
            # absence as "the file is unchanged" — that is exactly the
            # hole.  Mark it explicitly so the caller sees
            # "unwitnessed", not "same".
            out[normalize_relpath(path)] = ""
            continue
        out[normalize_relpath(path)] = str(digest)
    return out


def unwitnessed(paths: Iterable[str],
                witnessed: Dict[str, str]) -> List[str]:
    """The paths with NO usable witness (absent, or an event with no hash).

    Kept separate from the comparison so the caller can report the two
    situations differently: "this path was never logged" and "this
    path was logged but the log entry carries no content witness" are
    different findings.
    """
    out = []
    for p in paths:
        norm = normalize_relpath(p)
        if norm not in witnessed or not witnessed[norm]:
            out.append(norm)
    return out


def reconcile_external_edits(
    workspace_root: Path | str,
    candidates: Optional[Iterable[Tuple[str, Path]]] = None,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Witness the external edits that the change log does not know about.

    ``candidates`` is an iterable of ``(relpath, real_path)`` pairs —
    the truth-layer files this workspace indexes.  When it is None the
    function reports what it can from the log alone (a no-op); the
    manager passes its scan set.

    The decision is CONTENT-BASED: a file is recorded only when its
    current content hash differs from the hash the log last witnessed
    for that path.  Re-running this against an unchanged tree records
    NOTHING ⇒ the log does not grow and ``latest_change_mtime`` does
    not advance.

    Never raises: a reconciliation is an observation, and an
    observation must not take down the caller.
    """
    root = Path(workspace_root)
    report: Dict[str, Any] = {
        "workspace": str(root),
        "dry_run": bool(dry_run),
        "recorded": [],
        "unwitnessed": [],
        "skipped": [],
        "errors": [],
    }
    try:
        witnessed = _witnessed_hashes(root)
    except Exception as e:  # pragma: no cover - defensive
        report["errors"].append("read_changes failed: %s" % e)
        return report

    if candidates is None:
        return report

    for relpath, real in candidates:
        try:
            norm = normalize_relpath(relpath)
            digest = _sha256_file(Path(real))
            if digest is None:
                report["skipped"].append({
                    "path": norm, "reason": "unreadable-or-missing"})
                continue
            prior = witnessed.get(norm)
            if prior == digest:
                # Already witnessed with THIS content.  Record nothing —
                # this is what keeps the log from growing on every scan
                # and keeps latest_change_mtime from advancing.
                continue
            if prior is None:
                reason = "no-witness"
            elif prior == "":
                reason = "logged-but-no-content-witness"
            else:
                reason = "content-differs-from-witness"
            event = {
                "path": norm,
                "reason": reason,
                "prior_sha256": (prior or None),
            }
            if not dry_run:
                changes.record_change(
                    root, norm, SOURCE_EXTERNAL,
                    actor="reconciler",
                    extra={WITNESS_KEY: digest, "reason": reason},
                )
            report["recorded"].append(event)
        except Exception as e:
            report["errors"].append("%s: %s" % (relpath, e))
    return report


def format_report(report: Dict[str, Any], verbose: bool = False) -> str:
    """A human-readable rendering of a reconciliation report."""
    lines = []
    lines.append("变更记录对账 · workspace=%s%s"
                 % (report.get("workspace"),
                    "  [DRY-RUN]" if report.get("dry_run") else ""))
    rec = report.get("recorded") or []
    lines.append("待记录/已记录: %d" % len(rec))
    for e in rec:
        lines.append("  + %-58s %s" % (e.get("path"), e.get("reason")))
    skipped = report.get("skipped") or []
    if skipped:
        lines.append("跳过: %d" % len(skipped))
        for e in skipped:
            lines.append("  ~ %-58s %s" % (e.get("path"), e.get("reason")))
    errs = report.get("errors") or []
    if errs:
        lines.append("错误: %d" % len(errs))
        for e in errs:
            lines.append("  ! %s" % e)
    if not rec and not skipped and not errs:
        lines.append("  （没有需要记录的外部编辑）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The truth-layer file set — derived by SHAPE, never hard-coded.
# ---------------------------------------------------------------------------

def truth_files(workspace: Path | str) -> List[Tuple[str, Path]]:
    """The truth-layer files this workspace indexes, as ``(relpath, file)``.

    The set is derived by SHAPE (the same shape the scan uses), not
    hard-coded to this machine:
      * the four root identity files;
      * every agent's identity files under ``agents/<id>/``;
      * the persona layer's declared files;
      * the markdown truth layer under ``memory/``.

    T2f (2026-11-01): **the persona layer is IN this set — and it was
    in the set all along.**  The hole was elsewhere: the *manager's*
    scan set (the ``files_to_scan`` list) never contained ``persona/``,
    so the sync-time reconciliation — which walks the manager's scan
    set — could never see the persona files, no matter how many times
    it ran.  Measured on the live workspace: ``persona/identity.md``,
    ``persona/contract.md`` and ``persona/continuity.md`` had **ZERO
    events in the change log, ever** (1328 events in the root log, not
    one of them pointing at ``persona/``) ⇒ the entire L2 persona
    layer sat outside the mechanism.

    The fix belongs in the manager (the scan set), NOT here: this
    function is the single definition of "what is the truth layer", and
    the criterion borrows it deliberately (the SET of files that must
    be witnessed is a product decision, not a criterion's opinion).
    Changing the set here would silently change what the criterion
    measures.
    """
    from agent.memory.layers import excluded_segments

    workspace = Path(workspace)
    out: List[Tuple[str, Path]] = []
    seen = set()

    def add(rel: str, real: Path) -> None:
        key = str(real)
        if key in seen:
            return
        seen.add(key)
        out.append((rel, real))

    for name in ("MEMORY.md", "USER.md", "AGENT.md", "RULE.md"):
        p = workspace / name
        if p.is_file():
            add(name, p)

    agents = workspace / "agents"
    if agents.is_dir():
        for ad in sorted(agents.iterdir()):
            if not ad.is_dir():
                continue
            for name in ("MEMORY.md", "USER.md", "AGENT.md", "RULE.md"):
                p = ad / name
                if p.is_file():
                    add("agents/%s/%s" % (ad.name, name), p)

    persona = workspace / "persona"
    if persona.is_dir():
        names = None
        meta = persona / "PERSONA.meta.json"
        if meta.is_file():
            try:
                names = list(json.loads(meta.read_text(encoding="utf-8"))
                             .get("files", {}).keys())
            except Exception:
                names = None
        names = names or sorted(p.name for p in persona.glob("*.md"))
        for name in names:
            p = persona / name
            if p.is_file():
                add("persona/%s" % name, p)

    base = workspace / "memory"
    if base.is_dir():
        for p in base.rglob("*.md"):
            rel_parts = p.relative_to(workspace).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if "dreams" in rel_parts:
                continue
            if excluded_segments("/".join(rel_parts)):
                continue
            add(p.relative_to(workspace).as_posix(), p)

    return out


def workspace_truth_files(workspace: Path | str) -> List[Tuple[str, Path]]:
    """The truth-layer files **owned by** this workspace alone.

    T2f (2026-11-01).  :func:`truth_files` returns the ROOT's set,
    which INCLUDES ``agents/<id>/...`` - because the root index owns
    those files.  But each agent workspace has its OWN change log
    (``<agent>/memory/.audit/file_changes.jsonl``), and the root's log
    is a DIFFERENT file.

    Consequence: reconciling the root's set against the root's log puts
    the agents' paths into the ROOT's log and leaves the agents' own
    logs empty.  Measured on a two-workspace fixture: the root log
    ended up holding ``agents/agent-x/MEMORY.md`` while ``agent-x``'s
    own log had **zero** events => the agent's history was invisible to
    every reader of the agent's log (which is where the criterion looks
    for an agent workspace's provenance).

    **Who owns the ``agents/...`` paths.**  The ROOT index owns them
    (the root's ``files`` table carries the rows for every agent's
    identity files - measured: the root's database holds 136 file rows
    while an agent's own holds 5).  But the ROOT's change log is not
    the place to record them: the criterion reads an agent workspace's
    provenance from **that agent's own log**, and the root's log is
    consulted only for the root's own files.

    => The rule is by LOG, not by index owner: a path belongs to the
    workspace whose log records it.  The root records its own files
    (the four identity files, ``persona/``, ``memory/``, ``knowledge/``);
    an agent workspace records its own (``MEMORY.md``, ``AGENT.md``,
    ``memory/``, ``knowledge/`` under ``agents/<id>/``).

    The ROOT keeps the ``agents/...`` paths in :func:`truth_files` (that
    is the set the criterion borrows, and the root index really does
    carry those rows) - but this function, which drives the
    RECONCILIATION, splits them to the agent that owns the file.
    """
    workspace = Path(workspace)
    out: List[Tuple[str, Path]] = []
    for rel, real in truth_files(workspace):
        rel_posix = str(rel).replace("\\", "/")
        parts = rel_posix.split("/")
        if len(parts) >= 3 and parts[0] == "agents":
            # agents/<id>/<file>  ->  owned by the AGENT workspace.
            # The root's reconciliation must not claim it, and the
            # agent's reconciliation must record it under its OWN name
            # (the agent's log is keyed by paths relative to the
            # agent's workspace, not to the root).
            continue
        out.append((rel, real))
    return out


def agent_truth_files(workspace: Path | str) -> List[Tuple[str, Path]]:
    """The truth-layer files of an AGENT workspace, keyed for ITS log.

    T2f (2026-11-01).  An agent workspace's change log records paths
    RELATIVE TO THE AGENT (``MEMORY.md``, ``memory/2026-11-01.md``),
    not relative to the root (``agents/<id>/MEMORY.md``).  The
    criterion reads the agent's log with those relative keys - so the
    reconciliation must write the same keys.
    """
    workspace = Path(workspace)
    out: List[Tuple[str, Path]] = []
    for rel, real in truth_files(workspace):
        rel_posix = str(rel).replace("\\", "/")
        parts = rel_posix.split("/")
        if len(parts) >= 3 and parts[0] == "agents":
            # Strip the "agents/<id>/" prefix: the agent's own key.
            out.append(("/".join(parts[2:]), real))
            continue
        out.append((rel_posix, real))
    return out


def _workspaces_with_candidates(workspace: Path | str):
    """Yield ``(workspace, candidates)`` for EVERY workspace under ``workspace``.

    T2f (2026-11-01).  A workspace tree is the ROOT plus every
    ``agents/<id>/`` directory, and **each has its own change log**.
    The reconciliation must run against each workspace's OWN log with
    that workspace's OWN files — otherwise the root's log swallows the
    agents' paths and the agents' logs stay empty.

    A workspace is included when it HAS a change log (a workspace
    without one is not a workspace this mechanism can serve) or when it
    has files worth witnessing but no log yet — in that case the log is
    created by the write.

    The candidates are the workspace's own truth files: the root
    (:func:`workspace_truth_files` - its own files, WITHOUT the
    ``agents/...`` paths, which belong to the agents' own logs) and each
    agent workspace (:func:`agent_truth_files` - its own files, keyed
    RELATIVE TO THE AGENT, which is how its own log is keyed).
    """
    workspace = Path(workspace)
    out = []
    seen = set()

    def push(ws: Path, is_agent: bool):
        key = str(ws.resolve()) if ws.exists() else str(ws)
        if key in seen:
            return
        seen.add(key)
        cands = (agent_truth_files(ws) if is_agent
                 else workspace_truth_files(ws))
        out.append((ws, cands))

    push(workspace, False)
    agents = workspace / "agents"
    if agents.is_dir():
        for ad in sorted(agents.iterdir()):
            if ad.is_dir():
                push(ad, True)
    return out


def diagnose(workspace: Path | str) -> Dict[str, Any]:
    """Which paths are NEWER than the log's last record for them.

    This is the visibility the brief asks for when automatic witnessing
    is not enough: it does not depend on the witness hashes at all — it
    compares the file's own mtime against the log's last record for the
    same path.  A path with no record at all is reported separately
    ("never logged"), because "the log is missing this path" and "the
    log has an older record for this path" are different findings.

    READ-ONLY: it never writes to the log.
    """
    workspace = Path(workspace)
    last: Dict[str, str] = {}
    for e in changes.read_changes(workspace):
        p = e.get("path")
        if not p:
            continue
        stamp = str(e.get("mtime") or e.get("at") or "")
        norm = str(p).replace("\\", "/")
        if stamp >= last.get(norm, ""):
            last[norm] = stamp

    newer: List[Dict[str, Any]] = []
    never: List[str] = []
    for rel, real in truth_files(workspace):
        try:
            mt = real.stat().st_mtime
        except OSError:
            continue
        stamp = last.get(rel)
        if stamp is None:
            never.append(rel)
            continue
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(stamp[:19]).timestamp()
        except Exception:
            continue
        if mt > ts + 1:
            newer.append({"path": rel, "file_mtime": mt,
                          "log_last": stamp, "lag_seconds": int(mt - ts)})
    return {"workspace": str(workspace), "paths_mtime_newer": newer,
            "never_logged": never, "log_paths": len(last)}


# ---------------------------------------------------------------------------
# CLI — ``python -m agent.memory.external_edit``
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m agent.memory.external_edit",
        description="Witness external edits into the change log (T2c).")
    ap.add_argument("--workspace", default=".",
                    help="the workspace root (default: the cwd)")
    ap.add_argument("--diagnose", action="store_true",
                    help="report the paths the log is missing (read-only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicitly do NOT record (this is the DEFAULT)")
    ap.add_argument("--apply", action="store_true",
                    help="actually record (the default is a dry run)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace).resolve()

    if args.diagnose:
        rep = diagnose(workspace)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print("变更记录缺口诊断 · workspace=%s" % workspace)
            print("变更记录里的 path 数: %d" % rep["log_paths"])
            print("mtime 晚于变更记录的文件: %d" % len(rep["paths_mtime_newer"]))
            for e in rep["paths_mtime_newer"][:200]:
                print("  ! %-58s 落后 %d 秒" % (e["path"], e["lag_seconds"]))
            print("变更记录里完全没有的 path: %d" % len(rep["never_logged"]))
            for p in rep["never_logged"][:200]:
                print("  ? %s" % p)
        return 0

    # T2f (2026-11-01): reconcile EVERY workspace against its OWN log.
    #
    # The bug this fixes (measured on a two-workspace fixture): the CLI
    # used to reconcile the ROOT's truth set against the ROOT's log —
    # which put ``agents/agent-x/MEMORY.md`` into the ROOT's log and
    # left ``agent-x``'s own log EMPTY.  Every reader of an agent
    # workspace's provenance (the criterion included) looks at that
    # agent's own log ⇒ the agent's history was invisible.
    #
    # The root keeps its own files; each agent workspace is reconciled
    # against its own log with its own files.
    reports = []
    for ws, ws_cands in _workspaces_with_candidates(workspace):
        rep = reconcile_external_edits(ws, ws_cands, dry_run=not args.apply)
        rep["workspace"] = str(ws)
        reports.append(rep)
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print(format_report(rep, verbose=True))
            print("")
        total = sum(len(r.get("recorded") or []) for r in reports)
        if total and not args.apply:
            print("（这是 dry-run。加 --apply 才会真的记录。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
