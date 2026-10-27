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
from typing import Any, Dict, List, Optional

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

# P0-21: a page the truth layer says is a dark page (or a release), whose
# index row still has a PUBLIC shape. The row is the leak: the truth
# layer is right, the projection is wrong, and every anonymous caller
# can read it.
REASON_INDEX_PUBLIC = "gap-rule:index-public-row"

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
        self._check_schema_drift(report)
        self._check_public_rows(report)
        if persist_snapshot:
            self._write_snapshot(snapshot)
        self._write_audit(report)
        return report

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

    # -- check 8: index schema drift (P0-17 B-3) ----------------------------

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
            missing_columns,
            scan_workspaces,
        )

        roots = self._drift_roots()
        # This workspace's own index first (the one being inspected), then
        # every other workspace the roster knows about.
        drift = scan_workspaces(roots)
        own_db = self._index_db_path()
        if own_db.is_file():
            own_missing = missing_columns(own_db)
            if own_missing:
                drift.setdefault(str(self.workspace_root), own_missing)

        for root in sorted(drift):
            missing = drift[root]
            rel = self._relative_to_self(root)
            self._violation(
                report, REASON_SCHEMA_DRIFT, rel,
                f"the index is behind the current schema (missing "
                f"{', '.join(missing)}); retrieval queries against it fail, "
                f"and a failed query is reported as 'no matches' rather "
                f"than as an error",
                _SEV_HIGH, code=CODE_SCHEMA_DRIFT,
                missing_columns=missing,
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
    args = ap.parse_args(argv)

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
    return 1 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
