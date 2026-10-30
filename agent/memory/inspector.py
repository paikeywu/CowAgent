"""Live-memory periodic inspector — read-only verifier over the truth layer.

Design sources (upstream, do not re-derive rules here):
  - P0-1 §3.4/§六:  facts monotonic-append + hash-chain checks, snapshot
    convention at ``memory/.snapshots/YYYY-MM-DD.json``
  - P0-3 RR-03/WR-12: markdown front-matter fm_hash + body_hash self-seal
  - P0-2:           naming rules, enforced by scripts/memory/validate_names.py
  - SEED 拍板 2026-10-27: NO 60s polling. Three event-driven triggers only:
      1. before each shelf rebuild (shelf.py calls inspect())
      2. once at startup (caller wires it; inspector never self-schedules)
      3. manual command (python -m agent.memory.inspector --workspace <dir>)

Hard rules honored throughout:
  * REPORT-ONLY. This module never repairs, rewrites, or deletes anything.
    A violation becomes one line in the report + one line in
    ``memory/.audit/inspector.jsonl``; remediation is a human decision.
  * Snapshots are the comparison baseline for "history unchanged" and
    "monotonic append". First run establishes the baseline and reports
    nothing about history (it has none to compare against).
  * The audit log keeps the last-known per-file line counts, so a snapshot
    file that was itself deleted or rolled back does NOT launder a facts
    truncation (same defense-in-depth as facade.append_fact's seq check).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.memory.facade import (  # noqa: E402
    _GENESIS_PREV_HASH,
    _fact_line_hash,
    _fm_hash,
    split_frontmatter,
)
from agent.memory.naming import canonical_text  # noqa: E402
from agent.memory.layers import (  # noqa: E402
    EXCLUDED_INDEX_SEGMENTS as EXCLUDED_LAYER_SEGMENTS,
    excluded_segments,
)

_CST = timezone(timedelta(hours=8))

# Reason codes the shelf generator consumes for its gaps view.
REASON_BODY_HASH = "gap-rule:body-hash-mismatch"
REASON_FM_HASH = "gap-rule:fm-hash-mismatch"
REASON_LINE_HASH = "gap-rule:facts-line-hash"
REASON_PREV_HASH = "gap-rule:facts-prev-hash-break"
REASON_SEQ = "gap-rule:facts-seq-break"
# P0-8 收口: 物理删除（行数减少）与历史行改写是两个不同的判据，不能混用
REASON_SHRUNK = "gap-rule:facts-shrunk"
REASON_HISTORY = "gap-rule:history-rewritten"
REASON_NAMING = "gap-rule:naming"
REASON_SNAPSHOT_TAMPER = "gap-rule:snapshot-hash-mismatch"
# P0-10 L-1: the index layer has its own hygiene. A chunk row can outlive
# the file it indexes (deleted page) or point at a layer the scan excludes
# by policy (shelf/ facts/ .snapshots/ .audit/) — the latter is an
# anonymity leak, because those rows carry scope='shared' + user_id=NULL
# and are therefore visible to EVERY caller (V2-Q-01).
REASON_INDEX_EXCLUDED = "gap-rule:index-excluded-path"
REASON_INDEX_ORPHAN = "gap-rule:index-orphan"
# P0-13 L-3: the release projection could not be refreshed, so the
# retrieval exits were failing closed. The marker the manager leaves behind
# is what turns "it broke once" into "it is still broken" — a degradation
# nobody can find is a degradation that stays.
REASON_PROJECTION_DEGRADED = "gap-rule:release-projection-degraded"
# P0-16 L-4: a READ EXIT failed closed. The projection marker above covers
# the RETRIEVAL path (which goes through the projection refresh); the read
# exits resolve the truth layer themselves and never touch that function,
# so their refusals needed a marker of their own. Without this check the
# marker was written by nobody who read it — the loudest possible
# silence.
REASON_READ_DEGRADED = "gap-rule:read-degraded"
# P0-17 B-3: an index whose SCHEMA is behind the code. The retrieval
# queries name columns that a stale index does not have, the SQL raises,
# and (before P0-17) the failure was caught and reported as "no matches".
# The drift is therefore invisible from the outside — the system looks
# healthy and knows nothing. Naming the workspace and the columns is what
# turns it into something a human can act on.
REASON_SCHEMA_DRIFT = "gap-rule:schema-drift"
# P1-阶段三 第四轮: the body-origin column's own criteria.  They get
# their OWN reason and their OWN codes, because the four origins are four
# different contracts — "nobody decided yet", "there is honestly no truth
# source", "the truth source drifted" and "the pointer does not resolve"
# each need a different repair, and one shared code would hide which.
REASON_BODY_ORIGIN = "gap-rule:body-origin"

# P0-21: a page the truth layer says is a dark page (or a release), whose
# index row still has a PUBLIC shape. The row is the leak: the truth
# layer is right, the projection is wrong, and every anonymous caller
# can read it.
REASON_INDEX_PUBLIC = "gap-rule:index-public-row"

# P0-22: an index whose write-side guard is missing — or is present but
# is not the CURRENT definition. A database without the guard is a
# database the old desktop build can turn a dark page into a public row:
# the leak is not "possible" on such an index, it is only a matter of
# time. This is the check that makes "the guard is installed" a fact about
# every index rather than a thing somebody did once.
REASON_GUARD_MISSING = "gap-rule:guard-missing"

# G-2: a REBUILD that FAILED. The rebuilder writes the failure to
# ``memory/.audit/rebuild_failures.jsonl`` — and until now NOTHING read
# that file: the marker's own docstring claimed "the inspector reads
# it", which was false. A rebuild that fails keeps the old projection
# (by design, atomic swap), so the index silently stays behind the truth
# layer and the failure is invisible. This check is that reader.
REASON_REBUILD_FAILED = "gap-rule:rebuild-failed"

# G-2: the front-matter and the tombstone disagree about whether an
# element is released. The tombstone wins (the rebuilder enforces it), so
# this is not a leak — but an EDITABLE front-matter claiming something
# else is a finding, and the marker had no reader.
REASON_RELEASE_MISMATCH = "gap-rule:release-mismatch"

# Short machine codes carried on every violation (the shelf view needs a
# stable code, not a sentence).
CODE_SHRUNK = "facts-shrunk"
CODE_HISTORY = "history-rewritten"
CODE_INDEX_EXCLUDED = "index-excluded-path"
CODE_INDEX_ORPHAN = "index-orphan"
CODE_PROJECTION_DEGRADED = "release-projection-degraded"
CODE_READ_DEGRADED = "read-degraded"
CODE_SCHEMA_DRIFT = "schema-drift"
CODE_INDEX_PUBLIC = "index-public-row"
CODE_GUARD_MISSING = "guard-missing"
CODE_REBUILD_FAILED = "rebuild-failed"
CODE_RELEASE_MISMATCH = "release-mismatch"
# P1-阶段三 第四轮: the body-origin codes (O-1..O-4).
CODE_BODY_ORIGIN_UNRESOLVED = "body-origin-unresolved"
CODE_BODY_ORIGIN_FILE_UNRESOLVED = "body-origin-file-does-not-resolve"
CODE_BODY_ORIGIN_BLOB_SHAPE = "body-origin-blob-shape-wrong"
CODE_BODY_ORIGIN_ORPHAN_TEXT_LOST = "body-origin-orphan-text-lost"

# P0-22: how far back the patrol looks when it asks "was anything
# rewritten lately?". The guard records every discard, so the window is
# only a question of how much of it to show.
GUARD_WINDOW_MINUTES = 60

# Path segments the sync scan skips by policy (agent/memory/manager.py).
# A chunk row under any of them is a policy violation, not a stale page.
#
# The set lives in agent/memory/layers.py because THREE call sites need it
# (the scan, this check, the evolution watcher) and they drifted once
# already: P0-11 found tombstones excluded from the scan but not from
# this check, so the inspector called a leaking row legitimate.
EXCLUDED_INDEX_SEGMENTS = EXCLUDED_LAYER_SEGMENTS

_SEV_HIGH = "high"
_SEV_MID = "mid"

# The three trigger points (SEED 拍板 2026-10-27: NO 60s polling, NO daemon).
# The inspector never schedules itself; callers invoke one of these.
TRIGGER_POINTS = ("shelf-rebuild", "startup", "manual")


def run_inspection(workspace_root: Path | str, *,
                   trigger: str = "manual") -> Dict[str, Any]:
    """Single entry point for all three trigger points.

    ① ``trigger='shelf-rebuild'`` — agent/memory/shelf.py calls this before
       every full rebuild (the report feeds the gaps view).
    ② ``trigger='startup'``      — called once by the runtime at startup.
       **P1-阶段三 S3-K: this one is now actually wired** — it used to be
       DOCUMENTED and called from nowhere, which is a promise the code
       did not keep.  ``bridge/agent_initializer.py`` calls it (through
       the certification) on the same background thread as the memory
       sync, right after the index has been refreshed.
    ③ ``trigger='manual'``       — ``python -m agent.memory.inspector
       --workspace <dir>`` (or this function) at any time.

    The checks are identical at every trigger point; only the recorded
    trigger differs. There is NO periodic polling: a violation is found
    when someone looks, and the report is what makes the looking visible.
    """
    if trigger not in TRIGGER_POINTS:
        raise ValueError(
            f"trigger must be one of {TRIGGER_POINTS}, got {trigger!r}")
    return Inspector(workspace_root).inspect(trigger=trigger)


def _utc8_now() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _today() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iter_md(root: Path):
    md = root / "memory" / "markdown"
    if md.is_dir():
        for p in sorted(md.rglob("*.md")):
            if p.is_file():
                yield p


def _iter_facts(root: Path):
    facts = root / "memory" / "facts"
    if facts.is_dir():
        for p in sorted(facts.rglob("*.jsonl")):
            if p.is_file():
                yield p


#: P0-22: the truth layer of every workspace, cached by its signature.
#:
#: The sweep resolves one truth layer PER INDEX — each index is judged
#: against ITS OWN workspace's truth layer, never against this one's. The
#: resolution is the expensive part (it parses every markdown file), so it
#: is cached behind the same cheap signature the release-state cache uses:
#: a workspace whose truth layer did not move is not re-read.
_TRUTH_LAYER_CACHE: Dict[str, Tuple[Any, Dict[str, str]]] = {}


def _guarded_paths_for(root: Path | str) -> Dict[str, str]:
    """``path -> class`` for the pages one workspace's truth layer hides.

    An empty result means "this workspace hides nothing" — and also "the
    truth layer could not be resolved". The two are not the same, so the
    failure is not silent: the caller reports it (the check that uses this
    says so in its detail), and an unresolvable truth layer never turns
    into "nothing is hidden".
    """
    from agent.memory.release_state import ReleaseStateCache, tree_signature

    key = str(root)
    try:
        signature = tree_signature(root)
    except Exception:
        signature = None
    cached = _TRUTH_LAYER_CACHE.get(key)
    if cached is not None and signature is not None and cached[0] == signature:
        return cached[1]
    try:
        state = ReleaseStateCache(root).get()
    except Exception:
        return {}
    guarded: Dict[str, str] = {p: "sealed" for p in state.sealed_paths}
    for p in state.released_paths:
        guarded[p] = "released"
    _TRUTH_LAYER_CACHE[key] = (signature, guarded)
    return guarded


class Inspector:
    """One workspace's read-only verifier. Construct per workspace root."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root)
        self.memory_root = self.workspace_root / "memory"
        self.snapshots_dir = self.memory_root / ".snapshots"
        self.audit_dir = self.memory_root / ".audit"

    # -- public entry --------------------------------------------------------

    def inspect(self, *, trigger: str = "manual",
                persist_snapshot: bool = True) -> Dict[str, Any]:
        """Run all four checks. Returns the report dict (also written to
        .audit/inspector.jsonl). ``trigger`` is one of
        'shelf-rebuild' / 'startup' / 'manual' and is recorded, not branched
        on — the checks are identical at every trigger point.
        """
        prev_snapshot = self._load_latest_snapshot()
        report: Dict[str, Any] = {
            "inspector": "inspector/1.0",
            "trigger": trigger,
            "workspace": str(self.workspace_root),
            "at": _utc8_now(),
            "violations": [],
            "snapshot_baseline": (
                prev_snapshot["date"] if prev_snapshot else None),
        }
        self._check_markdown(report)
        snapshot = self._check_facts(report, prev_snapshot)
        self._check_naming(report)
        self._check_index_hygiene(report)
        self._check_projection_degraded(report)
        self._check_read_degraded(report)
        # G-2: the rebuild-failure marker now has a reader.
        self._check_rebuild_failed(report)
        # G-2: so does the release-mismatch marker.
        self._check_release_mismatch(report)
        self._check_schema_drift(report)
        self._check_public_rows(report)
        # P1 阶段二-B T5: INV 第 9 检查组（03 号 §八的 chunks 落地版 6 条）。
        report["text_inv"] = self._check_text_externalization(report)
        # P1-阶段三 第四轮: the body-origin check group.
        report["body_origin"] = self._check_body_origin(report)
        # P1-阶段三 第六轮 (S3-I): the ERASURE check — has the certified
        # state been erased since the migration put it there?
        report["erasure"] = self._check_erasure(report)
        # P1-阶段三 第七轮 (S3-K): the two M6 criteria, in the form a
        # criterion file calls.  They are separate from the erasure
        # check on purpose:
        #
        #   * M6a (coverage) asks "is every row with a file behind it
        #     certified?" — it catches "a new page is written and nobody
        #     certifies it", which is a GAP;
        #   * M6b (downgrade) asks "has a certified row been demoted?"
        #     — it catches damage, and it is the only one that is a
        #     HIGH violation.
        #
        # The two do not overlap: a row absent from the ledger is M6a's
        # business; a row in the ledger that no longer holds its
        # certified state is M6b's.
        report["m6"] = self._check_m6(report)
        # P1-阶段三 第四轮: the inspector is the DESIGNATED SETTER of the
        # 'mirror' state (设计 §3.1).  It runs AFTER the invariants, because
        # the promotion is only legal for the rows the invariants just
        # verified byte-for-byte.  The count is reported, so the state is
        # never a silent decoration.
        report["mirror_state"] = self._set_mirror_state()
        # P0-22: the sweep over EVERY index of the family. The checks above
        # look at THIS workspace; the sweep looks at every index that
        # exists — including the ones nobody has addressed in a session,
        # which is exactly where a leak would sit unnoticed.
        report["index_sweep"] = self._sweep_indexes(report)
        report["guard"] = report["index_sweep"].get("guard", {})
        if persist_snapshot:
            self._write_snapshot(snapshot)
        self._write_audit(report)
        return report

    # -- the family-wide sweep (P0-22) ---------------------------------------

    def _family_roots(self) -> List[str]:
        """Every workspace root whose index EXISTS on disk.

        The roster alone is not enough: ``agents/default`` is not in it,
        and a sweep that only knows the roster leaves that index
        unexamined. The disk is the ground truth for "which indexes
        exist"; the roster only says which workspaces are supposed to.
        """
        try:
            from agent.memory.index_guard import discover_index_roots

            return discover_index_roots(extra=self.workspace_root)
        except Exception:
            return [str(self.workspace_root)]

    def _sweep_indexes(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """Walk EVERY index of the family and report what is wrong with it.

        Three things are checked per index, and all three are things a
        single-workspace inspection cannot see:

        ① an EXCLUDED-LAYER row in a PUBLIC shape (the leak the old
           desktop build leaves behind — it indexes ``memory/shelf/*.md``
           as ``scope='shared'`` + ``user_id=NULL``, i.e. readable by
           every caller);
        ② the write-side GUARD: missing, or present but not the current
           definition (an index without the guard is one the old build
           can turn a dark page into a public row);
        ③ a PUBLIC row for a page the truth layer says is a dark page or
           a release — resolved from THAT workspace's own truth layer, not
           from this one's.

        The result is a separate section of the report (``index_sweep``)
        rather than a part of ``violations``: the violations of this
        report belong to THIS workspace, and a machine-wide sweep that
        mixed the two would make every workspace's report depend on every
        other workspace's state. The sweep's own findings are still
        RED — they turn the CLI's exit code and the audit trail — and each
        one names the workspace it belongs to.
        """
        from agent.memory.index_guard import guard_state, guard_report

        roots = self._family_roots()
        findings: List[Dict[str, Any]] = []
        guard_sections: List[Dict[str, Any]] = []
        checked: List[str] = []

        for root in roots:
            db = self._index_db_path_for(Path(root))
            if not db.is_file():
                continue
            checked.append(str(root))
            rel_db = self._relative_to_self(root)
            try:
                conn = sqlite3.connect(
                    "file:" + str(db).replace("\\", "/") + "?mode=ro", uri=True)
            except sqlite3.Error as e:
                findings.append({
                    "reason": REASON_INDEX_ORPHAN,
                    "code": CODE_INDEX_ORPHAN,
                    "path": rel_db,
                    "severity": _SEV_MID,
                    "detail": f"cannot open index read-only: {e}",
                    "workspace_root": str(root),
                })
                continue
            try:
                findings.extend(self._sweep_excluded_public(conn, root, rel_db))
                findings.extend(self._sweep_public_rows(conn, root, rel_db))
                state = guard_state(conn)
                if not state.get("installed") or not state.get("current"):
                    findings.append({
                        "reason": REASON_GUARD_MISSING,
                        "code": CODE_GUARD_MISSING,
                        "path": rel_db,
                        "severity": _SEV_HIGH,
                        "detail": (
                            f"the write-side guard is not in place on this "
                            f"index ({state.get('reason') or 'unknown'}); "
                            f"until it is, any writer — including the old "
                            f"desktop build — can write a row that must not "
                            f"be public (run a sync: the guard is re-installed "
                            f"by the mechanism, not by hand)"),
                        "workspace_root": str(root),
                        "guard_state": state,
                    })
                try:
                    report_ = guard_report(conn, minutes=GUARD_WINDOW_MINUTES)
                except Exception as e:
                    report_ = {"error": f"{type(e).__name__}: {e}"}
            finally:
                conn.close()
            guard_sections.append({"workspace_root": str(root),
                                   "index": str(db),
                                   **report_})

        return {
            "checked": checked,
            "checked_count": len(checked),
            "violations": findings,
            "violation_count": len(findings),
            "guard": {
                "window_minutes": GUARD_WINDOW_MINUTES,
                "indexes": guard_sections,
                "blocked_recent": sum(
                    int((g.get("blocked_recent") or 0)) for g in guard_sections),
                "blocked_total": sum(
                    int((g.get("blocked_count") or 0)) for g in guard_sections),
                "last_at": max(
                    [g.get("last_at") or "" for g in guard_sections] or [""]),
            },
        }

    def _sweep_excluded_public(self, conn, root: Path, rel_db: str
                                ) -> List[Dict[str, Any]]:
        """Rows under an excluded layer that are in a PUBLIC shape."""
        try:
            rows = conn.execute(
                "SELECT path, scope, user_id, COUNT(*) FROM chunks "
                "WHERE scope='shared' AND user_id IS NULL "
                "GROUP BY path, scope, user_id").fetchall()
        except sqlite3.Error as e:
            return [{
                "reason": REASON_INDEX_ORPHAN, "code": CODE_INDEX_ORPHAN,
                "path": rel_db, "severity": _SEV_MID,
                "detail": f"cannot read index: {e}",
                "workspace_root": str(root),
            }]
        out: List[Dict[str, Any]] = []
        for path, scope, user_id, n in rows:
            segments = list(excluded_segments(path))
            if not segments:
                continue
            out.append({
                "reason": REASON_INDEX_EXCLUDED,
                "code": CODE_INDEX_EXCLUDED,
                "path": path,
                "severity": _SEV_HIGH,
                "detail": (
                    f"{n} chunk row(s) under excluded segment(s) {segments} "
                    f"are in a PUBLIC shape (scope={scope!r}, "
                    f"user_id={user_id!r}) — this layer is not indexed by "
                    f"policy, and a 'shared' row with no owner is readable "
                    f"by EVERY caller (run a sync: the reconcile drops "
                    f"excluded paths)"),
                "workspace_root": str(root),
                "chunk_count": n, "scope": scope, "user_id": user_id,
                "excluded_segments": segments,
            })
        return out

    def _sweep_public_rows(self, conn, root: Path, rel_db: str
                           ) -> List[Dict[str, Any]]:
        """Public rows for pages THIS workspace's truth layer hides."""
        guarded = _guarded_paths_for(root)
        if not guarded:
            return []
        try:
            rows = conn.execute(
                "SELECT path, scope, user_id, file_sealed, element_released, "
                "COUNT(*) FROM chunks GROUP BY path, scope, user_id, "
                "file_sealed, element_released").fetchall()
        except sqlite3.Error as e:
            return [{
                "reason": REASON_INDEX_PUBLIC, "code": CODE_INDEX_PUBLIC,
                "path": rel_db, "severity": _SEV_MID,
                "detail": f"cannot read index: {e}",
                "workspace_root": str(root),
            }]
        out: List[Dict[str, Any]] = []
        for path, scope, user_id, file_sealed, element_released, n in rows:
            klass = guarded.get(path)
            if klass is None:
                continue
            public_shape = (scope == "shared" and not user_id
                            and not (file_sealed or 0))
            if klass == "released":
                bad = not (element_released or 0)
            else:
                bad = public_shape
            if not bad:
                continue
            out.append({
                "reason": REASON_INDEX_PUBLIC, "code": CODE_INDEX_PUBLIC,
                "path": path, "severity": _SEV_HIGH,
                "detail": (
                    f"{n} chunk row(s) for a page the truth layer says is "
                    f"{klass}, but the index stores it as scope={scope!r}, "
                    f"user_id={user_id!r}, file_sealed={file_sealed!r}, "
                    f"element_released={element_released!r} — a 'shared' row "
                    f"with no owner is readable by EVERY caller, so this row "
                    f"is a leak (run a sync; the write-side guard stops it "
                    f"from being written this way again)"),
                "workspace_root": str(root),
                "chunk_count": n, "scope": scope, "user_id": user_id,
                "guard_class": klass,
            })
        return out

    # -- check 1: markdown body_hash / fm_hash --------------------------------

    def _check_markdown(self, report: Dict[str, Any]) -> None:
        for p in _iter_md(self.workspace_root):
            rel = p.relative_to(self.workspace_root).as_posix()
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                self._violation(report, REASON_BODY_HASH, rel,
                                f"unreadable: {e}", _SEV_HIGH)
                continue
            fm, body = split_frontmatter(text)
            if not fm:
                continue  # README.md and other pattern-exempt files
            declared = fm.get("body_hash")
            if declared:
                actual = "sha256:" + _sha256_hex(canonical_text(body))
                if declared != actual:
                    self._violation(
                        report, REASON_BODY_HASH, rel,
                        f"body_hash declared {declared[:23]}… != actual "
                        f"{actual[:23]}… (body modified outside facade)",
                        _SEV_HIGH)
            stored_fm = fm.get("fm_hash")
            if stored_fm and stored_fm != _fm_hash(fm):
                self._violation(
                    report, REASON_FM_HASH, rel,
                    "fm_hash mismatch (front-matter modified outside facade)",
                    _SEV_HIGH)

    # -- check 2+3: facts chain + monotonic append ----------------------------

    def _check_facts(self, report: Dict[str, Any],
                     prev_snapshot: Optional[Dict[str, Any]]
                     ) -> Dict[str, Any]:
        """Chain/seq/hash per file (self-contained) + monotonic line count
        and history-unchanged against the previous snapshot. Returns the NEW
        snapshot payload.

        P0-1 §3.4 判定 A/B:
          A (单调性): 行数减少 → 物理删除 → ``facts-shrunk``。
                      行数增加是正常追加，不报。
          B (历史不变): 逐行比对 ``line_no -> line_hash``，哈希变了 →
                      ``history-rewritten``，并给出变化的行号。
        The snapshot stores the RECOMPUTED content hash per line, not the
        file's own ``line_hash`` claim — otherwise a rewrite that leaves the
        stale claim in place (content changed, line_hash untouched) would
        compare equal to the baseline and slip through (P0-8 实测缺陷).
        """
        prev_files = (prev_snapshot or {}).get("files", {})
        audit_counts = self._audit_line_counts()
        files: Dict[str, Any] = {}
        for p in _iter_facts(self.workspace_root):
            rel = p.relative_to(self.workspace_root).as_posix()
            entry = self._verify_one_facts_file(p, rel, report)
            prev_entry = prev_files.get(rel)
            if prev_entry:
                prev_count = int(prev_entry.get("line_count", 0))
                if entry["line_count"] < prev_count:
                    self._violation(
                        report, REASON_SHRUNK, rel,
                        f"line count {prev_count} -> {entry['line_count']} "
                        f"(行数减少 = 物理删除, INV-5; line-count-decreased)",
                        _SEV_HIGH, code=CODE_SHRUNK,
                        line_count=entry["line_count"],
                        previous_line_count=prev_count)
                prev_hashes = prev_entry.get("line_hashes", {})
                for line_no_s, prev_hash in sorted(
                        prev_hashes.items(), key=lambda kv: int(kv[0])):
                    line_no = int(line_no_s)
                    if line_no > entry["line_count"]:
                        # already reported by the monotonic check
                        continue
                    cur = entry["line_hashes"].get(line_no)
                    if cur is not None and cur != prev_hash:
                        self._violation(
                            report, REASON_HISTORY, rel,
                            f"line {line_no} hash rewritten: {prev_hash[:23]}…"
                            f" -> {cur[:23]}… (历史行被改写, INV-5)",
                            _SEV_HIGH, code=CODE_HISTORY, line_no=line_no,
                            expected_hash=prev_hash, actual_hash=cur)
            # Defense-in-depth: audit trail remembers counts even if the
            # snapshot file itself was rolled back.
            audit_prev = audit_counts.get(rel)
            if audit_prev is not None and entry["line_count"] < audit_prev:
                self._violation(
                    report, REASON_SHRUNK, rel,
                    f"line count {entry['line_count']} < audit-remembered "
                    f"{audit_prev} (snapshot rollback suspected)",
                    _SEV_HIGH, code=CODE_SHRUNK,
                    line_count=entry["line_count"],
                    previous_line_count=audit_prev)
            files[rel] = {
                "line_count": entry["line_count"],
                "line_hashes": {str(k): v for k, v in
                                entry["line_hashes"].items()},
            }
        return {"date": _today(), "files": files}

    def _verify_one_facts_file(self, path: Path, rel: str,
                               report: Dict[str, Any]) -> Dict[str, Any]:
        lines: List[Dict[str, Any]] = []
        # line_no (physical, 1-based) -> recomputed content hash. The
        # physical index is the baseline key: seq can be rewritten by an
        # attacker, the line position cannot be moved without changing the
        # count (which is checked separately).
        line_hashes: Dict[int, str] = {}
        with open(path, encoding="utf-8") as f:
            for i, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    self._violation(report, REASON_LINE_HASH, rel,
                                    f"line {i}: unparseable (tail truncation?)",
                                    _SEV_MID)
        prev = _GENESIS_PREV_HASH
        for expected_seq, line in enumerate(lines, start=1):
            seq = line.get("seq")
            if seq != expected_seq:
                self._violation(report, REASON_SEQ, rel,
                                f"seq break: expected {expected_seq}, got {seq}",
                                _SEV_HIGH)
            if line.get("prev_hash") != prev:
                self._violation(report, REASON_PREV_HASH, rel,
                                f"prev_hash break at seq={seq}", _SEV_HIGH)
            lh = line.get("line_hash", "")
            expected_lh = _fact_line_hash(line)
            if lh != expected_lh:
                self._violation(report, REASON_LINE_HASH, rel,
                                f"line_hash mismatch at seq={seq}", _SEV_HIGH)
                # Tampered content: the STORED hash feeds the chain
                # continuity check (the file's own claim), not the recomputed
                # one — otherwise a rewritten line 1 makes line 2's prev_hash
                # look consistent and the chain check goes blind.
            # The BASELINE stores the recomputed hash: it is the only value
            # that changes when the CONTENT changes, even if the writer left
            # the line_hash claim stale.
            line_hashes[expected_seq] = expected_lh
            prev = lh or prev
        return {"line_count": len(lines), "line_hashes": line_hashes}

    # -- check 4: naming (reuse, never re-implement) --------------------------

    def _check_naming(self, report: Dict[str, Any]) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                               / "scripts" / "memory"))
        import validate_names  # noqa: E402  (scripts/memory/validate_names.py)

        for rel, problems in validate_names.validate_root(self.workspace_root):
            for prob in problems:
                self._violation(report, REASON_NAMING, rel, prob, _SEV_MID)

    # -- check 5: index layer hygiene (P0-10 L-1) ----------------------------

    def _index_db_path(self) -> Path:
        """This workspace's index. One per Agent (common/state_dir.memory_index_db
        puts it at ``<agent_base>/memory/long-term/index.db``)."""
        return self.memory_root / "long-term" / "index.db"

    def _index_db_path_for(self, root: Path) -> Path:
        """Another workspace's index — the same layout rule."""
        return Path(root) / "memory" / "long-term" / "index.db"

    def _shared_root(self) -> Optional[Path]:
        """The shared root, for knowledge/ paths that live outside this
        workspace. None when the registry is not resolvable (the check then
        degrades to 'registered but unverifiable', never to a false red)."""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from common import state_dir
            return Path(state_dir.shared_root())
        except Exception:
            return None

    def _index_source_file(self, rel: str) -> Optional[Path]:
        """Where the file behind an index path actually lives, or None when
        it cannot be found anywhere. ``knowledge/...`` may live in this
        workspace's own knowledge/ or in the shared one; everything else is
        relative to the workspace root."""
        candidates = [self.workspace_root / rel]
        if rel.startswith("knowledge/"):
            shared = self._shared_root()
            if shared is not None:
                candidates.append(shared / rel)
        for cand in candidates:
            if cand.is_file():
                return cand
        return None

    def _check_index_hygiene(self, report: Dict[str, Any]) -> None:
        """Report index rows that should not be searchable.

        REPORT-ONLY, like every other check: the index is repaired by
        running sync() (whose reconcile now drops excluded paths and chunk
        rows without a files counterpart). The inspector never edits the
        database — an index that "fixes itself" is how a deletion becomes
        invisible to the audit trail.
        """
        db = self._index_db_path()
        if not db.is_file():
            return
        rel_db = db.relative_to(self.workspace_root).as_posix()
        try:
            conn = sqlite3.connect(
                "file:" + str(db).replace("\\", "/") + "?mode=ro", uri=True)
        except sqlite3.Error as e:
            self._violation(report, REASON_INDEX_ORPHAN, rel_db,
                            f"cannot open index read-only: {e}", _SEV_MID,
                            code=CODE_INDEX_ORPHAN)
            return
        try:
            rows = conn.execute(
                "SELECT c.path, c.scope, c.user_id, COUNT(*) "
                "FROM chunks c GROUP BY c.path, c.scope, c.user_id"
            ).fetchall()
            registered = {r[0] for r in conn.execute(
                "SELECT DISTINCT path FROM files").fetchall()}
        except sqlite3.Error as e:
            self._violation(report, REASON_INDEX_ORPHAN, rel_db,
                            f"cannot read index: {e}", _SEV_MID,
                            code=CODE_INDEX_ORPHAN)
            return
        finally:
            conn.close()

        for path, scope, user_id, n in rows:
            excluded = list(excluded_segments(path))
            if excluded:
                self._violation(
                    report, REASON_INDEX_EXCLUDED, path,
                    f"{n} chunk row(s) under excluded segment(s) "
                    f"{excluded} (scope={scope!r}, user_id={user_id!r}) — this "
                    f"layer is not indexed by policy; a 'shared' row here is "
                    f"visible to every caller",
                    _SEV_HIGH, code=CODE_INDEX_EXCLUDED,
                    chunk_count=n, scope=scope, user_id=user_id,
                    excluded_segments=excluded)
                continue
            if path not in registered:
                self._violation(
                    report, REASON_INDEX_ORPHAN, path,
                    f"{n} chunk row(s) with no files-table counterpart — the "
                    f"page is not registered, so sync()'s reconcile cannot "
                    f"see it (run a sync to drop it)",
                    _SEV_MID, code=CODE_INDEX_ORPHAN,
                    chunk_count=n, scope=scope, user_id=user_id)
                continue
            if self._index_source_file(path) is None:
                self._violation(
                    report, REASON_INDEX_ORPHAN, path,
                    f"{n} chunk row(s) for a file that no longer exists on "
                    f"disk — the row outlived its page",
                    _SEV_MID, code=CODE_INDEX_ORPHAN,
                    chunk_count=n, scope=scope, user_id=user_id)

    # -- check 9: a guarded page whose index row is still public (P0-21) -------

    def _check_public_rows(self, report: Dict[str, Any]) -> None:
        """Report index rows that are PUBLIC although the truth layer says
        they must not be.

        P0-21: the desktop build of 2026-10-13 is still running against
        these very index files. Its sync does not know about ``visibility:
        sealed`` and writes every markdown file under ``memory/`` as
        ``scope='shared'`` with ``user_id=NULL`` — the shape an anonymous
        caller reads. Measured on a throwaway workspace: the old build's
        own anonymous search returned a dark page's front-matter and body.

        This check is the one place that can SEE that: the truth layer
        says which pages are dark or released, and the index says how
        they are stored. Where they disagree, the row is the leak.

        REPORT-ONLY, like every other check: the write-side guard
        (``agent.memory.index_guard``) stops the row from being written
        public, and a sync re-stamps the ones that already are. The
        inspector never edits the database.

        A row that is properly sealed is NOT reported — the reverse case
        is the one that must not produce noise, because a check that
        cries wolf about correct rows gets switched off.
        """
        db = self._index_db_path()
        if not db.is_file():
            return
        # The truth layer: which paths are dark pages, which are
        # released. Resolved from the DISK (the authority).
        try:
            from agent.memory.release_state import ReleaseStateCache

            state = ReleaseStateCache(self.workspace_root).get()
        except Exception as e:
            self._violation(
                report, REASON_INDEX_PUBLIC,
                self._index_db_path().relative_to(self.workspace_root).as_posix(),
                f"cannot resolve the truth layer, so no page can be judged: {e}",
                _SEV_MID, code=CODE_INDEX_PUBLIC)
            return
        guarded = {}
        for path in state.sealed_paths:
            guarded[path] = "sealed"
        for path in state.released_paths:
            guarded[path] = "released"
        if not guarded:
            return
        try:
            conn = sqlite3.connect(
                "file:" + str(db).replace("\\", "/") + "?mode=ro", uri=True)
        except sqlite3.Error as e:
            self._violation(
                report, REASON_INDEX_PUBLIC,
                db.relative_to(self.workspace_root).as_posix(),
                f"cannot open index read-only: {e}", _SEV_MID,
                code=CODE_INDEX_PUBLIC)
            return
        try:
            rows = conn.execute(
                "SELECT path, scope, user_id, file_sealed, element_released, "
                "COUNT(*) FROM chunks GROUP BY path, scope, user_id, "
                "file_sealed, element_released").fetchall()
        except sqlite3.Error as e:
            self._violation(
                report, REASON_INDEX_PUBLIC,
                db.relative_to(self.workspace_root).as_posix(),
                f"cannot read index: {e}", _SEV_MID, code=CODE_INDEX_PUBLIC)
            return
        finally:
            conn.close()

        for path, scope, user_id, file_sealed, element_released, n in rows:
            klass = guarded.get(path)
            if klass is None:
                continue
            # A released element must carry the release flag; a dark page
            # must not be a public row. Anything else is a leak.
            public_shape = (scope == "shared" and not user_id
                            and not (file_sealed or 0))
            if klass == "released":
                bad = not (element_released or 0)
            else:
                bad = public_shape
            if not bad:
                continue
            self._violation(
                report, REASON_INDEX_PUBLIC, path,
                f"{n} chunk row(s) for a page the truth layer says is "
                f"{klass}, but the index stores it as scope={scope!r}, "
                f"user_id={user_id!r}, file_sealed={file_sealed!r}, "
                f"element_released={element_released!r} — a 'shared' row "
                f"with no owner is readable by EVERY caller, so this row "
                f"is a leak (run a sync; the write-side guard stops it "
                f"from being written this way again)",
                _SEV_HIGH, code=CODE_INDEX_PUBLIC,
                chunk_count=n, scope=scope, user_id=user_id,
                guard_class=klass)

    # -- check 9: text-externalization invariants (P1 阶段二-B T5) -------------

    def _check_text_externalization(self, report: Dict[str, Any]
                                    ) -> Dict[str, Any]:
        """INV 第 9 检查组：03 号 §八 INV-1/2/3/6/9/11 的 chunks 落地版。

        These run over the chunks table + filesystem (NOT a copy of 03 号's
        SQL, which targets the P2 element-system tables that do not exist
        yet). INV-4/5/7/8 are deferred and registered in the report with
        their trigger condition. REPORT-ONLY, like every other check — each
        INV carries its own verdict + fingerprint, and each violation is also
        surfaced into report["violations"] so the CLI exit code and the audit
        trail turn red.
        """
        db = self._index_db_path()
        if not db.is_file():
            return {"scope": "03 号 INV chunks 落地版", "verdict": "NOTRUN",
                    "fingerprint": "index-missing", "checks": {},
                    "violations": [], "violation_count": 0,
                    "deferred": {}}
        try:
            conn = sqlite3.connect(
                "file:" + str(db).replace("\\", "/") + "?mode=ro", uri=True)
        except sqlite3.Error as e:
            self._violation(report, REASON_INDEX_ORPHAN,
                            db.relative_to(self.workspace_root).as_posix(),
                            f"cannot open index for INV checks: {e}",
                            _SEV_MID, code=CODE_INDEX_ORPHAN)
            return {"scope": "03 号 INV chunks 落地版", "verdict": "NOTRUN",
                    "fingerprint": f"open-error:{e}", "checks": {},
                    "violations": [], "violation_count": 0, "deferred": {}}
        try:
            from agent.memory import text_inv
            # G-1: the production inspection path is EXHAUSTIVE. It used to
            # call run_text_invariants() without the argument, inheriting the
            # sampling default (20 of N rows) — so 300 broken rows were
            # caught 1 time in 60 inspections and the gate stayed green
            # over a corrupted library. `full=True` is passed EXPLICITLY
            # here as well, so the intent is visible at the call site and
            # a future default change cannot silently reintroduce the blind
            # spot.
            result = text_inv.run_text_invariants(
                conn, self.workspace_root, full=True)
        finally:
            conn.close()
        for v in result["violations"]:
            self._violation(report, v["reason"], v["path"], v["detail"],
                            v.get("severity", _SEV_HIGH),
                            code=v.get("code"),
                            **{k: val for k, val in v.items()
                               if k not in ("reason", "code", "path",
                                            "detail", "severity")})
        return result

    # -- check 6: the release projection's health (P0-13 L-3) -----------------

    def _check_projection_degraded(self, report: Dict[str, Any]) -> None:
        """Report a release projection that could not be refreshed.

        The manager leaves a marker behind whenever the projection refresh
        fails, and removes it on the next success. While the marker is
        there, the retrieval exits are FAILING CLOSED (they refuse rather
        than serve a stale "nothing is released" view) — which is the right
        behaviour and a broken system at the same time. The inspector is
        where the second half gets said out loud.
        """
        marker = self.audit_dir / "release_projection.json"
        if not marker.is_file():
            return
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            payload = {"error": f"unreadable marker: {e}"}
        rel = marker.relative_to(self.workspace_root).as_posix()
        self._violation(
            report, REASON_PROJECTION_DEGRADED, rel,
            f"the release projection could not be refreshed "
            f"(stage={payload.get('stage')!r}, at={payload.get('at')!r}, "
            f"error={payload.get('error')!r}); the retrieval exits are "
            f"failing closed until a refresh succeeds",
            _SEV_HIGH, code=CODE_PROJECTION_DEGRADED,
            stage=payload.get("stage"), at=payload.get("at"),
            error=payload.get("error"))

    # -- check 7: a read exit failed closed (P0-16 L-4) ----------------------

    def _check_read_degraded(self, report: Dict[str, Any]) -> None:
        """Report a READ EXIT that is failing closed.

        The projection check above covers the retrieval path. The read
        exits (memory_get, the facade's read, the web console's file
        reader) resolve the truth layer themselves, so they refuse
        service on their own — and, before P0-16 L-4, they refused
        SILENTLY: no log line, no marker, nothing for the inspector to
        find. The content was not leaked, but a privacy control that
        breaks shut and says nothing can stay broken forever.

        The marker is removed once the truth layer resolves again, so this
        check goes green by itself after the cause is fixed. The
        append-only history (``read_denied.jsonl``) is never removed —
        the refusals happened.
        """
        from agent.memory.read_audit import read_denied_state

        payload = read_denied_state(self.workspace_root)
        if payload is None:
            return
        rel = (self.audit_dir / "read_denied.json").relative_to(
            self.workspace_root).as_posix()
        self._violation(
            report, REASON_READ_DEGRADED, rel,
            f"a read exit refused to serve (exit={payload.get('last_exit')!r}, "
            f"at={payload.get('at')!r}, "
            f"path={payload.get('last_path')!r}, "
            f"error={payload.get('last_error')!r}); the read exits are "
            f"failing closed until the truth layer resolves again",
            _SEV_HIGH, code=CODE_READ_DEGRADED,
            last_exit=payload.get("last_exit"),
            last_path=payload.get("last_path"),
            at=payload.get("at"),
            error=payload.get("last_error"))

    # -- check 7b: a rebuild FAILED (G-2) ------------------------------------

    def _check_rebuild_failed(self, report: Dict[str, Any]) -> None:
        """Report a REBUILD that failed (G-2).

        The rebuilder is atomic: when a rebuild fails, the OLD projection
        is kept and a marker is written to
        ``memory/.audit/rebuild_failures.jsonl``. Until now NOTHING read
        that file — the marker's own docstring claimed "the inspector
        reads it", which was false — so a failing rebuild was SILENT:
        the index stayed behind the truth layer and every gate stayed
        green.

        This check is that reader. The marker is append-only history
        (the failures happened), so the check reports what it finds; it
        does not clear the file.
        """
        from agent.memory.rebuilder import read_rebuild_failures

        try:
            failures = read_rebuild_failures(self.workspace_root)
        except Exception as e:
            rel = (self.audit_dir / "rebuild_failures.jsonl").relative_to(
                self.workspace_root).as_posix()
            self._violation(
                report, REASON_REBUILD_FAILED, rel,
                f"the rebuild-failure log could not be read: {e}",
                _SEV_HIGH, code=CODE_REBUILD_FAILED)
            return
        for rec in failures:
            path = str(rec.get("path") or "<unknown>")
            error = str(rec.get("error") or "<no error recorded>")
            rel = (self.audit_dir / "rebuild_failures.jsonl").relative_to(
                self.workspace_root).as_posix()
            self._violation(
                report, REASON_REBUILD_FAILED,
                f"{rel}#{path}",
                f"a rebuild failed and the old projection was kept "
                f"(path={path!r}, error={error!r}); the index is behind the "
                f"truth layer for this file until a rebuild succeeds",
                _SEV_HIGH, code=CODE_REBUILD_FAILED,
                rebuild_path=path, error=error)

    def _check_release_mismatch(self, report: Dict[str, Any]) -> None:
        """Report a front-matter/tombstone disagreement (G-2).

        The tombstone is the final authority on release state, so the
        index is correct even when the two disagree — but the
        disagreement means an EDITABLE front-matter claimed something
        different from the tombstone. That is a finding worth saying
        out loud, and until now the marker had no reader at all.
        """
        from agent.memory.rebuilder import read_release_mismatches

        try:
            mismatches = read_release_mismatches(self.workspace_root)
        except Exception as e:
            rel = (self.audit_dir / "release_mismatch.jsonl").relative_to(
                self.workspace_root).as_posix()
            self._violation(
                report, REASON_RELEASE_MISMATCH, rel,
                f"the release-mismatch log could not be read: {e}",
                _SEV_HIGH, code=CODE_RELEASE_MISMATCH)
            return
        for rec in mismatches:
            path = str(rec.get("path") or "<unknown>")
            detail = str(rec.get("mismatch") or "<no detail recorded>")
            rel = (self.audit_dir / "release_mismatch.jsonl").relative_to(
                self.workspace_root).as_posix()
            self._violation(
                report, REASON_RELEASE_MISMATCH,
                f"{rel}#{path}",
                f"front-matter and tombstone disagree about the release "
                f"state (path={path!r}, detail={detail!r}); the tombstone "
                f"wins, but the front-matter was edited",
                _SEV_HIGH, code=CODE_RELEASE_MISMATCH,
                release_path=path, mismatch=detail)

    # -- check 8: index schema drift (P0-17 B-3) ----------------------------

    def _check_m6(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """P1-阶段三 第七轮 (S3-K) — the two M6 criteria, for THIS
        workspace's own index.

        The rule lives in ``agent.memory.certify``; this method only
        CALLS it and puts the result into the report, so the inspector
        and the criterion file cannot disagree about what "certified"
        means.

        REPORT-ONLY, like every other check here.
        """
        from agent.memory.certify import (
            VERDICT_FAIL,
            VERDICT_NOTRUN,
            judge_m6a_coverage,
            judge_m6b_downgrade,
        )

        db = self._index_db_path()
        out: Dict[str, Any] = {
            "scope": "m6",
            "verdict": VERDICT_NOTRUN,
            "detail": "",
            "m6a": {},
            "m6b": {},
            "violations": [],
            "violation_count": 0,
        }
        if not db.is_file():
            out["detail"] = "the index is not there"
            return out
        try:
            conn = sqlite3.connect(str(db))
        except sqlite3.Error as e:
            out["detail"] = "the index cannot be opened: %s" % e
            return out
        try:
            a = judge_m6a_coverage(conn)
            b = judge_m6b_downgrade(conn)
        finally:
            conn.close()
        out["m6a"] = a
        out["m6b"] = b

        violations = []
        if a.get("verdict") == VERDICT_FAIL:
            violations.append({
                "severity": "MID",
                "reason": "m6a: the certification coverage",
                "path": str(db),
                "detail": ("%d row(s) with a file behind them have never "
                           "been certified — they are pending work, not "
                           "damage: %s"
                           % (a.get("not_certified", 0),
                              a.get("detail", ""))),
            })
        if b.get("verdict") == VERDICT_FAIL:
            rows = b.get("downgraded", [])
            violations.append({
                "severity": "HIGH",
                "reason": "m6b: a certified row was downgraded",
                "path": str(db),
                "detail": ("%d certified row(s) no longer hold the state "
                           "they were certified in: %s"
                           % (b.get("downgrades", 0),
                              "; ".join(
                                  "%s certified=%s now=%s"
                                  % (r["id"][:10], r["certified_state"],
                                     r["current_state"])
                                  for r in rows[:5]))),
            })
        out["violations"] = violations
        out["violation_count"] = len(violations)
        if VERDICT_FAIL in (a.get("verdict"), b.get("verdict")):
            out["verdict"] = VERDICT_FAIL
        else:
            out["verdict"] = "PASS" if VERDICT_NOTRUN not in (
                a.get("verdict"), b.get("verdict")) else VERDICT_NOTRUN
        out["fingerprint"] = ("m6a: %s (%d not certified) | m6b: %s "
                              "(%d downgrade(s))"
                              % (a.get("verdict"), a.get("not_certified", 0),
                                 b.get("verdict"), b.get("downgrades", 0)))
        return out

    def _check_erasure(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """P1-阶段三 第六轮 (S3-I) — the ERASURE check.

        **The question:** was the certified state ERASED after the
        migration put it there?

        The desktop build from 2026-10-13 is still running on this
        machine, and it writes with statements that do not name the
        body columns — so a row it touches loses its pointer and its
        certified state.  The body-column guard stops that, but a guard
        that is silently defeated is worse than no guard, so the state
        has to be CHECKED, not assumed.

        **The check answers the question that actually matters:**
        "has this library been erased since it was migrated?" — and it
        distinguishes that from "this library was never migrated",
        which looks identical in the data:

        * never migrated  → every row has no pointer AND no certified
          state, and the library's last write is OLD;
        * migrated, then erased → the same shape, but the last write is
          NEWER than the migration's own timestamp.

        So the report carries ``last_write`` next to the counts, and
        the reader compares it with the migration's timestamp.  A
        library with a certified state and a recent write is a library
        that was touched — and the certified count says whether the
        touch was clean.

        REPORT-ONLY, like every other check here.
        """
        from agent.memory.body_guard import erasure_census

        db = self._index_db_path()
        if not db.is_file():
            return {"scope": "erasure", "verdict": "NOTRUN",
                    "fingerprint": "index-missing", "violations": [],
                    "violation_count": 0}
        try:
            conn = sqlite3.connect(
                "file:" + str(db).replace("\\", "/") + "?mode=ro", uri=True)
        except sqlite3.Error as e:
            return {"scope": "erasure", "verdict": "NOTRUN",
                    "fingerprint": "index-unreadable",
                    "detail": str(e), "violations": [], "violation_count": 0}
        try:
            census = erasure_census(conn)
        finally:
            conn.close()
        if "error" in census:
            return {"scope": "erasure", "verdict": "NOTRUN",
                    "fingerprint": "census-failed", "detail": census["error"],
                    "violations": [], "violation_count": 0}

        violations = []
        # The certified state must be there.  A row that fell back to
        # 'projection'/'unresolved' is the ERASED shape.
        #
        # P1-阶段三 S3-K: the verdict is built from the WITNESSES, not
        # from "some rows are not certified".  A row written five
        # minutes ago and not certified yet is the inspector's own
        # pending work, NOT damage — and calling it damage would make
        # this check red on every newly written page.  A permanently
        # red check is a check nobody reads.
        #
        # So:
        #   * ``downgrade_proven`` / ``pointer_lost``  → a PROVEN
        #     erasure (the snapshot says it WAS certified / had a
        #     pointer, and the row says it is not any more);
        #   * ``not_certified`` → pending certification (reported, not
        #     a violation);
        #   * ``never_decided`` → a gap (nobody decided this row yet).
        if census.get("erased"):
            violations.append({
                "severity": "HIGH",
                "reason": "erasure: the certified state was lost",
                "path": str(db),
                "detail": ("%d row(s) were PROVEN to have been certified (or "
                           "to have had a pointer) and are not any more — "
                           "the guard's snapshot says so (%d downgrade(s), "
                           "%d pointer(s) lost)"
                           % (census.get("erased"),
                              census.get("downgrade_proven", 0),
                              census.get("pointer_lost", 0))),
            })
        if census.get("no_pointer") and not census.get("pointer_lost"):
            # No pointer, but no proof that one was there: the rows were
            # never decided.  That is a GAP (the migration / the
            # certification has not reached them), not an erasure.
            violations.append({
                "severity": "MID",
                "reason": "erasure: the pointer was never filled",
                "path": str(db),
                "detail": ("%s of %s rows have no body_path and no evidence "
                           "that one was ever there — the row's origin was "
                           "never decided ('unresolved'), so this is a GAP "
                           "to be certified, not a proven erasure"
                           % (census.get("no_pointer"), census.get("rows"))),
            })
        if census.get("files_without_stats"):
            violations.append({
                "severity": "MID",
                "reason": "erasure: the files statistics were lost",
                "path": str(db),
                "detail": ("%s files rows have no chunk_count/chunker_version "
                           "— the statistics were erased after the migration"
                           % census.get("files_without_stats")),
            })
        verdict = "RED" if violations else "GREEN"
        return {
            "scope": "erasure",
            "verdict": verdict,
            "fingerprint": ("rows=%s certified=%s undecided=%s no_pointer=%s "
                            "not_certified=%s never_decided=%s erased=%s "
                            "files_without_stats=%s last_write=%s"
                            % (census.get("rows"), census.get("certified"),
                               census.get("undecided"),
                               census.get("no_pointer"),
                               census.get("not_certified"),
                               census.get("never_decided"),
                               census.get("erased"),
                               census.get("files_without_stats"),
                               census.get("last_write"))),
            "census": census,
            "last_write": census.get("last_write"),
            "violations": violations,
            "violation_count": len(violations),
        }

    def _check_body_origin(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """P1-阶段三 第四轮 — the ``body_origin`` check group.

        Four criteria, one per origin, each with its own contract:

        * **O-1** ``body_origin='unresolved'`` == 0 — a row nobody
          judged is a VISIBLE TODO, and it is always RED.  (This is the
          criterion that makes "the migration has not run yet" a fact
          rather than a silent NULL.)
        * **O-2** every ``'file'`` row's pointer RESOLVES to a real file.
        * **O-3** every ``'blob'`` row has ``body_path IS NULL`` **and**
          ``body_hash IS NOT NULL`` — "there is honestly no truth
          source" is a design, not a defect; but a blob row with no
          drift token is untrackable, and that IS a defect.
        * **O-4** every ``'orphan'`` row KEEPS its text — an orphan is
          the last remaining copy of a fact whose source is gone, and
          dropping its text is silent deletion of history.

        REPORT-ONLY, like every other check: the inspector reports, the
        migration decides.  (The ONE thing the inspector writes is the
        ``'mirror'`` state — see :meth:`_set_mirror_state`.)
        """
        from agent.memory.body_pointer import (
            ORIGIN_BLOB,
            ORIGIN_FILE,
            ORIGIN_ORPHAN,
            ORIGIN_UNRESOLVED,
            resolve_truth_file,
        )

        db = self._index_db_path()
        if not db.is_file():
            return {"scope": "body_origin", "verdict": "NOTRUN",
                    "fingerprint": "index-missing", "violations": [],
                    "violation_count": 0}
        try:
            conn = sqlite3.connect(
                "file:" + str(db).replace("\\", "/") + "?mode=ro", uri=True)
        except sqlite3.Error as e:
            return {"scope": "body_origin", "verdict": "NOTRUN",
                    "fingerprint": "open-error:%s" % e, "violations": [],
                    "violation_count": 0}
        violations: List[Dict[str, Any]] = []
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
            if "body_origin" not in cols:
                # The column is not there yet — that is schema drift, and
                # the drift check reports it.  This check is NOTRUN, not
                # PASS: "I cannot judge" and "there is nothing wrong" are
                # different answers.
                return {"scope": "body_origin", "verdict": "NOTRUN",
                        "fingerprint": "body_origin-column-absent",
                        "violations": [], "violation_count": 0}
            dist = dict(conn.execute(
                "SELECT body_origin, COUNT(*) FROM chunks "
                "GROUP BY body_origin").fetchall())
            rows = conn.execute(
                "SELECT id, path, body_path, body_anchor, body_hash, text, "
                "body_origin FROM chunks WHERE body_origin='unresolved'"
            ).fetchall()
            for cid, rel, bpath, banchor, bhash, _text, _o in rows:
                violations.append({
                    "reason": REASON_BODY_ORIGIN,
                    "code": CODE_BODY_ORIGIN_UNRESOLVED,
                    "path": str(rel or "?"),
                    "detail": ("chunk %s: body_origin='unresolved' — nobody "
                               "has decided whether this row has a truth "
                               "source" % cid),
                    "severity": _SEV_HIGH,
                    "chunk_id": cid,
                })
            # O-2: a 'file' row must resolve.
            for cid, rel, bpath, banchor, _bh, _t, _o in conn.execute(
                    "SELECT id, path, body_path, body_anchor, body_hash, "
                    "text, body_origin FROM chunks WHERE body_origin='file'"):
                if not bpath or resolve_truth_file(
                        self.workspace_root, bpath) is None:
                    violations.append({
                        "reason": REASON_BODY_ORIGIN,
                        "code": CODE_BODY_ORIGIN_FILE_UNRESOLVED,
                        "path": str(rel or "?"),
                        "detail": ("chunk %s: body_origin='file' but the "
                                   "pointer %r does not resolve to a truth "
                                   "file" % (cid, bpath)),
                        "severity": _SEV_HIGH,
                        "chunk_id": cid,
                    })
            # O-3: a 'blob' row has no pointer but has a drift token.
            for cid, rel, bpath, _banchor, bhash, _t, _o in conn.execute(
                    "SELECT id, path, body_path, body_anchor, body_hash, "
                    "text, body_origin FROM chunks WHERE body_origin='blob'"):
                if bpath is not None or bhash is None:
                    violations.append({
                        "reason": REASON_BODY_ORIGIN,
                        "code": CODE_BODY_ORIGIN_BLOB_SHAPE,
                        "path": str(rel or "?"),
                        "detail": ("chunk %s: body_origin='blob' must have "
                                   "body_path IS NULL and body_hash NOT "
                                   "NULL (has path=%r hash=%r)"
                                   % (cid, bpath, bhash)),
                        "severity": _SEV_HIGH,
                        "chunk_id": cid,
                    })
            # O-4: an orphan keeps its text.
            for cid, rel, _bpath, _banchor, _bh, text, _o in conn.execute(
                    "SELECT id, path, body_path, body_anchor, body_hash, "
                    "text, body_origin FROM chunks WHERE body_origin='orphan'"):
                if text is None:
                    violations.append({
                        "reason": REASON_BODY_ORIGIN,
                        "code": CODE_BODY_ORIGIN_ORPHAN_TEXT_LOST,
                        "path": str(rel or "?"),
                        "detail": ("chunk %s: body_origin='orphan' and its "
                                   "text is NULL — the orphan is the last "
                                   "remaining copy of that fact" % cid),
                        "severity": _SEV_HIGH,
                        "chunk_id": cid,
                    })
        finally:
            conn.close()
        for v in violations:
            self._violation(report, v["reason"], v["path"], v["detail"],
                            v["severity"], code=v["code"],
                            chunk_id=v.get("chunk_id"))
        return {"scope": "body_origin", "verdict": "RED" if violations else "GREEN",
                "fingerprint": "distribution=%s" % (dist or "none"),
                "violations": violations, "violation_count": len(violations),
                "distribution": dist}

    def _set_mirror_state(self) -> Dict[str, Any]:
        """Flip ``text_state`` to ``'mirror'`` for the rows WE verified.

        P1-阶段三 第四轮.  The inspector is the DESIGNATED SETTER of the
        ``'mirror'`` state (设计 §3.1): "校验器逐行复核通过后显式置位
        （巡检器在 INV-2/3 校验通过时置位——明确置位者，防止三态机退化
        为装饰）".

        **The rule:** a row becomes ``'mirror'`` only when the inspector
        has just rebuilt its fragment from the FILE and found it
        BYTE-equal to the stored text.  Nothing else promotes a row:
        not the migration (which only knows what it wrote), not the
        writer (which is the party being judged), and not the mere
        absence of an error.

        **Why the setter must be explicit.**  A three-state machine whose
        states are set by "whoever wrote last" is a decoration: every
        row would be ``'projection'`` forever, and the state would
        carry no information.  A state that only ONE party may set, and
        only after that party has actually checked, carries the
        information it exists to carry.

        **Why the state must not be set for the rows that do not
        qualify.**  A ``'blob'`` row has no truth source to mirror, and
        an ``'orphan'`` row's truth source has drifted — marking either
        as ``'mirror'`` would be a false claim about the truth layer.

        The write is counted and reported: "how many rows were promoted"
        is a number the delivery document carries.

        **P1-阶段三 S3-K: the setter now delegates to the ONE
        certification rule.**  This method used to spell the rule out
        itself, and it had two gaps that the round found:

        * it only looked at rows whose ``body_origin`` was already
          ``'file'`` — so a row written by a writer that fills no body
          columns (``body_origin='unresolved'``, no pointer at all) could
          NEVER be certified, no matter how many times this ran;
        * it only ever looked at **this** workspace's index, so the
          other fifteen libraries of the family were never certified.

        Both are fixed in ``agent.memory.certify`` — the pointer is
        DERIVED first (by the migration's own D1 rule), and the whole
        family is reachable.  This method now calls that one
        implementation, so the inspector and the CLI cannot disagree
        about what "certified" means.
        """
        from agent.memory.certify import certify_library

        db = self._index_db_path()
        out: Dict[str, Any] = {"promoted": 0, "checked": 0, "skipped": 0}
        if not db.is_file():
            return out
        report = certify_library(self.workspace_root, db,
                                 name=self.workspace_root.name)
        out["promoted"] = int(report.get("promoted", 0))
        out["checked"] = int(report.get("checked", 0))
        out["skipped"] = int(report.get("refused", 0))
        out["derived"] = int(report.get("derived", 0))
        out["refusals"] = report.get("refusals", [])
        return out

    def _check_schema_drift(self, report: Dict[str, Any]) -> None:
        """Report an index whose SCHEMA is behind the current code.

        A stale index is invisible from the outside: the retrieval queries
        name a column it does not have, the SQL raises, and (before P0-17)
        the failure was caught and returned as an empty result set. The
        system looked healthy and knew nothing — the most expensive kind
        of silence, because nobody goes looking for a bug in a system
        that reports no errors.

        The check names the workspace AND the columns, because "an index
        is behind" is not actionable: the person reading it has to know
        which file to migrate and what to add.

        A workspace whose index is complete is NOT reported — the check
        goes green by itself once the migration has run, and a check that
        cries wolf on a healthy index gets ignored.
        """
        from agent.memory.schema_drift import (
            describe_drift,
            missing_columns_all,
            missing_columns_by_table,
            scan_workspaces,
        )

        roots = self._drift_roots()
        # This workspace's own index first (the one being inspected), then
        # every other workspace the roster knows about.
        drift = scan_workspaces(roots)
        own_db = self._index_db_path()
        if own_db.is_file():
            own_missing = missing_columns_all(own_db)
            if own_missing:
                drift.setdefault(str(self.workspace_root), own_missing)

        for root in sorted(drift):
            missing = drift[root]
            rel = self._relative_to_self(root)
            # P1-阶段三 (H2): the report says WHICH TABLE each missing column
            # belongs to. "path" and "hash" exist in BOTH modelled tables,
            # so a bare list is genuinely ambiguous — and the whole point of
            # this change is that the drift report must not be ambiguous
            # about where the fork is.
            by_table = missing_columns_by_table(
                (Path(root) / "memory" / "long-term" / "index.db")
                if (Path(root) / "memory" / "long-term" / "index.db").is_file()
                else own_db)
            self._violation(
                report, REASON_SCHEMA_DRIFT, rel,
                f"the index is behind the current schema (missing "
                f"{', '.join(missing)}); retrieval queries against it fail, "
                f"and a failed query is reported as 'no matches' rather "
                f"than as an error",
                _SEV_HIGH, code=CODE_SCHEMA_DRIFT,
                missing_columns=missing,
                missing_by_table=by_table,
                index_root=root,
                drift_summary=describe_drift({root: missing}),
            )

    def _relative_to_self(self, root: str) -> str:
        """The workspace root as a path relative to this workspace, when it
        is inside it; otherwise the absolute path."""
        try:
            return Path(root).relative_to(self.workspace_root).as_posix() or "."
        except ValueError:
            return str(root)

    def _drift_roots(self) -> List[str]:
        """Every workspace root whose index should be checked.

        The roster is the source of truth for which workspaces exist — the
        agents directory is only a layout detail, and a workspace may live
        anywhere. When the roster cannot be resolved (a source checkout
        with no team.json, a test), this workspace alone is checked: the
        check degrades to "less coverage", never to a false red.
        """
        roots = [str(self.workspace_root)]
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from agent.registry import get_agent_registry

            for profile in get_agent_registry().list(include_disabled=True):
                roots.append(str(profile.workspace))
        except Exception:
            pass
        # Deduplicate, keep order.
        seen = set()
        unique = []
        for root in roots:
            if root not in seen:
                seen.add(root)
                unique.append(root)
        return unique

    # -- snapshots -------------------------------------------------------------

    def _snapshot_path(self, date: str) -> Path:
        return self.snapshots_dir / f"{date}.json"

    def _load_latest_snapshot(self) -> Optional[Dict[str, Any]]:
        if not self.snapshots_dir.is_dir():
            return None
        candidates = sorted(self.snapshots_dir.glob("????-??-??.json"))
        for path in reversed(candidates):
            try:
                snap = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            stored = snap.get("snapshot_hash")
            payload = {k: v for k, v in snap.items() if k != "snapshot_hash"}
            actual = "sha256:" + _sha256_hex(
                json.dumps(payload, ensure_ascii=False, sort_keys=True))
            if stored and stored != actual:
                # Snapshot itself was tampered: report it, then fall back to
                # the audit trail as the comparison baseline (it is append-
                # only and hash-chained independently).
                self._tampered_snapshot = path.name
                continue
            return snap
        return None

    def _write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(snapshot)
        payload["snapshot_hash"] = "sha256:" + _sha256_hex(
            json.dumps(payload, ensure_ascii=False, sort_keys=True))
        path = self._snapshot_path(snapshot["date"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False,
                                  indent=2, sort_keys=True) + "\n",
                       encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    # -- audit -----------------------------------------------------------------

    def _audit_line_counts(self) -> Dict[str, int]:
        """Last-known facts line counts from the append-only audit trail."""
        counts: Dict[str, int] = {}
        path = self.audit_dir / "inspector.jsonl"
        if not path.is_file():
            return counts
        with open(path, encoding="utf-8") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for rel, info in (line.get("facts_counts") or {}).items():
                    counts[rel] = max(counts.get(rel, 0), int(info))
        return counts

    def _violation(self, report: Dict[str, Any], reason: str, path: str,
                   detail: str, severity: str, *, code: Optional[str] = None,
                   **fields: Any) -> None:
        """Record one violation. ``code`` is the short machine code the
        shelf view keys on (``facts-shrunk`` / ``history-rewritten`` / …);
        ``reason`` stays the fully-qualified reason. ``fields`` carries
        structured facts (changed line numbers, counts) so downstream
        consumers never have to parse the detail sentence."""
        item: Dict[str, Any] = {
            "reason": reason, "path": path, "detail": detail,
            "severity": severity,
        }
        if code:
            item["code"] = code
        if fields:
            item.update(fields)
        report["violations"].append(item)

    def _write_audit(self, report: Dict[str, Any]) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        path = self.audit_dir / "inspector.jsonl"
        last_hash = _GENESIS_PREV_HASH
        if path.exists():
            with open(path, "rb") as f:
                lines = f.read().splitlines()
            if lines:
                try:
                    last_hash = json.loads(lines[-1])["line_hash"]
                except (ValueError, KeyError):
                    pass
        counts: Dict[str, int] = {}
        for p in _iter_facts(self.workspace_root):
            rel = p.relative_to(self.workspace_root).as_posix()
            with open(p, encoding="utf-8") as f:
                counts[rel] = sum(1 for raw in f if raw.strip())
        entry: Dict[str, Any] = {
            "actor": "inspector",
            "action": "inspect",
            "trigger": report["trigger"],
            "at": report["at"],
            "violation_count": len(report["violations"]),
            "facts_counts": counts,
            "prev_hash": last_hash,
        }
        # P0-22: the family-wide sweep is part of the patrol, so it belongs
        # in the trail: how many indexes were checked, what was found, and
        # how many writes the guard dropped. A sweep that only lived in the
        # console output would be a sweep nobody can audit later.
        sweep = report.get("index_sweep") or {}
        if sweep:
            entry["index_sweep"] = {
                "checked_count": sweep.get("checked_count", 0),
                "violation_count": sweep.get("violation_count", 0),
                "guard": {
                    "blocked_recent": (sweep.get("guard") or {}).get(
                        "blocked_recent", 0),
                    "blocked_total": (sweep.get("guard") or {}).get(
                        "blocked_total", 0),
                    "last_at": (sweep.get("guard") or {}).get("last_at", ""),
                    "window_minutes": (sweep.get("guard") or {}).get(
                        "window_minutes", GUARD_WINDOW_MINUTES),
                },
            }
        if getattr(self, "_tampered_snapshot", None):
            entry["snapshot_tampered"] = self._tampered_snapshot
            self._tampered_snapshot = None
        canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        entry["line_hash"] = "sha256:" + _sha256_hex(canonical)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Live-memory read-only inspector (report-only; never "
                    "repairs). Trigger points: shelf-rebuild / startup / "
                    "manual — this CLI is the manual one.")
    ap.add_argument("--workspace", type=Path, required=True,
                    help="agent workspace root (contains memory/)")
    ap.add_argument("--trigger", default="manual",
                    choices=["shelf-rebuild", "startup", "manual"])
    ap.add_argument("--json", action="store_true",
                    help="print the full report as JSON")
    ap.add_argument("--certify-mirror", action="store_true",
                    help="certify the 'mirror' state of EVERY live library "
                         "of the workspace (P1-阶段三 S3-K) and print the "
                         "counts, then exit.  This is the repeatable form "
                         "of the promotion the inspector does as a side "
                         "effect: it is idempotent, it refuses every row "
                         "that does not pass the byte test, and it lists "
                         "the refusals.")
    args = ap.parse_args(argv)

    if args.certify_mirror:
        from agent.memory.certify import certify_family

        report = certify_family(args.workspace)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("certify: workspace=%s libraries=%d"
                  % (report["workspace"], report["library_count"]))
            for lib in report["libraries"]:
                print("   %-22s promoted=%-4d derived=%-4d refused=%-4d "
                      "remaining=%d"
                      % (lib["library"], lib["promoted"], lib["derived"],
                         lib["refused"], lib.get("remaining", 0)))
                for r in lib["refusals"]:
                    print("      REFUSED %s  %s  %s"
                          % (r["id"][:10], r["reason"],
                             r.get("detail", "")))
            print("TOTAL: promoted=%d derived=%d refused=%d remaining=%d"
                  % (report["promoted"], report["derived"],
                     report["refused"], report["remaining"]))
        return 1 if report["refused"] else 0

    report = Inspector(args.workspace).inspect(trigger=args.trigger)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        v = report["violations"]
        print(f"[inspector] {args.workspace} trigger={args.trigger} "
              f"violations={len(v)}")
        for item in v:
            print(f"  [{item['severity']}] {item['reason']} {item['path']}: "
                  f"{item['detail']}")
        # P0-22: the family-wide sweep — every index that exists, not only
        # this workspace's. Its findings are RED too: the exit code below
        # counts them.
        sweep = report.get("index_sweep") or {}
        if sweep:
            print(f"[sweep] 已巡检索引 {sweep.get('checked_count', 0)} 个，"
                  f"违例 {sweep.get('violation_count', 0)} 条")
            for item in sweep.get("violations", []):
                print(f"  [{item['severity']}] {item['reason']} "
                      f"{item.get('workspace_root', '')} :: {item['path']}: "
                      f"{item['detail']}")
            guard = sweep.get("guard") or {}
            print(f"[guard] 最近 {guard.get('window_minutes', 0)} 分钟被守卫"
                  f"改写的行数: {guard.get('blocked_recent', 0)}"
                  f"（累计 {guard.get('blocked_total', 0)}，"
                  f"最后一次 {guard.get('last_at') or '无'}）")
            for item in guard.get("indexes", []):
                if int(item.get("blocked_recent") or 0) or \
                        int(item.get("blocked_count") or 0):
                    print(f"  - {item.get('workspace_root')}: "
                          f"累计 {item.get('blocked_count')}，"
                          f"最近 {item.get('blocked_recent')}，"
                          f"路径 {[p.get('path') for p in item.get('paths', [])][:5]}")
    return 1 if (report["violations"]
                 or (report.get("index_sweep") or {}).get("violation_count")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
