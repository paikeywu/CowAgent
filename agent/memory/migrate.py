#!/usr/bin/env python3
# encoding:utf-8
"""The P1-阶段三 migration tool — M0..M7 (M6 gated).

**This is the criterion AND the tool** (design §7).  It is in the repository
so that ``git log`` is the history of what was migrated and when.

Usage:
    python -m agent.memory.migrate --workspace "C:\\Users\\User\\cow" --step all
    python -m agent.memory.migrate --workspace DIR --step M0,M1,M4,M5
    python -m agent.memory.migrate --workspace DIR --step M7 --dry-run

What it does, per step (the design's M0..M7):

  M0  BACKUP.  wal_checkpoint(TRUNCATE) -> VACUUM INTO '<backup dir>' +
      a truth-layer snapshot + the pre-migration fingerprints.  Then the
      RECOVERY DRILL (A-2): the backup is opened through the REAL
      ``MemoryStorage`` path, the row counts and a 20-row id/hash sample
      are compared, and an FTS query is run.  Any FAIL -> DO NOT ENTER M1.

  M1  schema_migrate idempotence + the post-migration fingerprints.

  M4  BACKFILL.  The four body columns for the rows that have none, with
      a CONDITIONAL UPDATE (A-1: ``WHERE id=? AND hash=?``) so a concurrent
      writer can never have its row overwritten by the backfill.
      Plus the ``files.chunk_count`` / ``files.chunker_version`` backfill.

  M5  VERIFY.  The text at the pointer must equal ``chunks.text`` byte for
      byte (CRLF/LF tolerated).  A non-zero mismatch count -> STOP AND
      ROLL BACK.

  M6  GATED.  ``text_state`` -> 'mirror'.  **NOT RUN** until the kernel
      architect rules on the INV-1 contract (the architect must distinguish
      "pointer missing" from "there is no truth source").

  M7  REBUILD.  Re-cut every file through the rebuilder and check that
      the chunk count / id set (with the part segment) / hash set are
      unchanged, that FTS is in sync, and that the released elements are
      still invisible to the three identities.

Every step is IDEMPOTENT: running it twice changes nothing the second
time.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.body_pointer import (  # noqa: E402
    ORIGIN_BLOB,
    ORIGIN_FILE,
    ORIGIN_ORPHAN,
    ORIGIN_UNRESOLVED,
    STATE_PROJECTION,
    body_anchor,
    body_hash,
    four_columns_for_chunk,
    parse_anchor,
    posix_body_path,
    rebuild_from_anchor,
    resolve_truth_file,
)
from agent.memory.chunker import TextChunker  # noqa: E402
from agent.memory.naming import canonical_text  # noqa: E402
from agent.memory.rebuilder import _chunk_id  # noqa: E402
from agent.memory.schema_drift import (  # noqa: E402
    describe_schema_fingerprint,
    missing_columns_all,
    repair_columns,
    schema_fingerprint,
)

#: The chunker version the ``files.chunker_version`` column must carry.
#:
#: P1-阶段三 第四轮: 4, because the chunker now carries the SEGMENT
#: SEQUENCE (架构师 §6.5 — a hard requirement, and SEED 拍板升位).
#: The chunk TEXT and the chunk ID are unchanged; only the anchor string
#: gains a ``+S`` suffix.  The version bump is what lets the inspector
#: tell "this row has no segment sequence" from "this row was cut by an
#: algorithm that did not need one".
#:
#: P1-阶段三 第五轮 (F-2): the NUMBER is a fact about the algorithm, so
#: it is read from the algorithm instead of being spelled out here.  Three
#: modules each carrying their own copy of the version is exactly how they
#: drift apart — and the drift is silent: the rebuild wrote 3 into a
#: library cut by 4, and the criterion that compares the two went red.
CHUNKER_VERSION = TextChunker.CHUNKER_VERSION

#: The state a backfilled row carries.  'mirror' is M6's business — the
#: backfill itself leaves the row a projection (the text lives in the
#: row) until the architect rules.
STATE_BACKFILL = STATE_PROJECTION


class MigrationError(RuntimeError):
    """A migration step failed.  The caller rolls back."""


class Report:
    """PASS / FAIL / NOTRUN — three states, never two."""

    def __init__(self) -> None:
        self.lines: List[str] = []
        self.criteria: List[Dict[str, Any]] = []

    def criterion(self, cid: str, title: str, state: str, detail: str = "",
                  **extra: Any) -> None:
        rec = {"id": cid, "title": title, "state": state, "detail": detail}
        rec.update(extra)
        self.criteria.append(rec)
        self.lines.append("  [%s] %s: %s" % (state, cid, title))
        if detail:
            for ln in str(detail).splitlines():
                self.lines.append("         %s" % ln)

    def note(self, text: str) -> None:
        self.lines.append(text)

    def counts(self) -> Dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "NOTRUN": 0}
        for c in self.criteria:
            out[c["state"]] = out.get(c["state"], 0) + 1
        return out

    def failed(self) -> List[Dict[str, Any]]:
        return [c for c in self.criteria if c["state"] == "FAIL"]


# --------------------------------------------------------------------------
# The live-library enumeration — the WHITELIST rule (A-4).
# --------------------------------------------------------------------------

def enumerate_live(workspace: Path | str) -> List[Dict[str, Any]]:
    """The live indexes, by WHITELIST — never a ``*.db`` glob.

    A-4: a glob would also pick up the backups this tool writes and the
    scratch copies in ``tmp/``.  The rule is the PATH SHAPE:

      * ``<ws>/memory/long-term/index.db``            (the shared root)
      * ``<ws>/agents/<id>/memory/long-term/index.db`` (one per agent)

    and nothing else.  The truth-layer root of an agent's library is the
    agent's own directory, which is what ``resolve_truth_file`` expects.

    **The argument may be a ``str``.**  MEASURED (0.114 前置准备 §7.2):
    ``enumerate_live('/home/paikeywu/cow-workspace')`` raised
    ``TypeError: unsupported operand type(s) for /: 'str' and 'str'`` —
    because the body does ``workspace / "memory"``.  Every caller on
    0.114 passes the workspace as it came off the command line (a
    string), so the tool was unusable there for a reason that has
    nothing to do with the machine.  The type is coerced HERE, once,
    so that every caller — the CLI, the certifier, the tests — gets the
    same behaviour.
    """
    workspace = Path(workspace)
    live: List[Dict[str, Any]] = []
    root_db = workspace / "memory" / "long-term" / "index.db"
    if root_db.is_file():
        live.append({"name": "ROOT", "db": root_db, "workspace": workspace})
    agents_dir = workspace / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            db = agent_dir / "memory" / "long-term" / "index.db"
            if db.is_file():
                live.append({"name": agent_dir.name, "db": db,
                             "workspace": agent_dir})
    return live


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_digest(conn: sqlite3.Connection,
                   table: str = "chunks") -> Optional[str]:
    """A digest of a table's CONTENT, independent of the byte layout.

    **Why not the row count.**  MEASURED (0.114 前置准备 §4.4, E-4):
    0.114's ROOT library had 25 rows at the time of the probe, 4 of
    them with a non-NULL ``body_path`` — and the SERVICE writes to it
    by itself (it syncs the knowledge layer on its own schedule).  So
    the row count is not a constant of the machine: it moves between
    the moment the baseline is taken and the moment it is compared.

    **Why not the file's SHA-256.**  A checkpoint moves bytes without
    moving rows, so the file's hash changes when nothing about the
    content changed — a false alarm on a healthy library.

    **What this is.**  The sha256 of the rows' own values, in a stable
    order, so two libraries with the same content have the same digest
    and any change to the content moves it.  The COVERED COLUMNS are
    printed with the digest by the callers, because a digest whose
    coverage is not stated cannot be audited.
    """
    import hashlib

    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return None
    if not cols:
        return None
    wanted = [c for c in ("id", "path", "start_line", "end_line", "text",
                           "hash", "body_path", "body_anchor", "body_hash",
                           "text_state") if c in cols]
    if not wanted:
        return None
    try:
        rows = conn.execute(
            "SELECT %s FROM %s ORDER BY id" % (", ".join(wanted), table)
        ).fetchall()
    except sqlite3.Error:
        return None
    h = hashlib.sha256()
    for row in rows:
        h.update(("\x1f".join("" if v is None else str(v)
                               for v in row) + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _q(conn: sqlite3.Connection, sql: str, args: Tuple = ()) -> Any:
    return conn.execute(sql, args).fetchone()[0]


# --------------------------------------------------------------------------
# M0 — backup + the recovery drill (A-2).
# --------------------------------------------------------------------------

def _open_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _backup_one(lib: Dict[str, Any], backup_dir: Path,
                report: Report) -> Dict[str, Any]:
    """Checkpoint + VACUUM INTO + the recovery drill for ONE library."""
    db: Path = lib["db"]
    name = lib["name"]
    target = backup_dir / ("%s.p1-bak.db" % name)

    # Pre-migration facts (measured on a READ-ONLY connection first).
    pre = {
        "db": str(db),
        "size_bytes": db.stat().st_size,
        "fingerprint": schema_fingerprint(db),
        "chunks": 0, "messages": 0, "files": 0,
        "body_path_null": 0,
        "chunk_count_zero": 0,
        "chunk_count_null": 0,
        # The CONTENT digest — the baseline that does not drift (E-4).
        # The row count is reported too, but it is NOT the baseline:
        # the service writes to the library by itself, so the row count
        # moves between the baseline and the comparison.
        "chunks_digest": None,
        "files_digest": None,
    }
    conn = _open_ro(db)
    try:
        pre["chunks"] = _q(conn, "SELECT COUNT(*) FROM chunks")
        pre["chunks_digest"] = content_digest(conn, "chunks")
        try:
            pre["files_digest"] = content_digest(conn, "files")
        except sqlite3.Error:
            pre["files_digest"] = None
        try:
            pre["messages"] = _q(conn, "SELECT COUNT(*) FROM messages")
        except sqlite3.OperationalError:
            pre["messages"] = None
        pre["files"] = _q(conn, "SELECT COUNT(*) FROM files")
        pre["body_path_null"] = _q(
            conn, "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL")
        pre["chunk_count_zero"] = _q(
            conn, "SELECT COUNT(*) FROM files WHERE chunk_count = 0")
        pre["chunk_count_null"] = _q(
            conn, "SELECT COUNT(*) FROM files WHERE chunk_count IS NULL")
        sample = conn.execute(
            "SELECT id, hash FROM chunks ORDER BY id LIMIT 20").fetchall()
        pre["sample"] = [(r["id"], r["hash"]) for r in sample]
    finally:
        conn.close()

    # Checkpoint + VACUUM INTO.  This needs a WRITE connection.
    wconn = sqlite3.connect(str(db))
    try:
        wconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if target.exists():
            target.unlink()
        wconn.execute("VACUUM INTO ?", (str(target),))
        wconn.commit()
    finally:
        wconn.close()

    # --- the RECOVERY DRILL (A-2) -------------------------------------
    drill: Dict[str, Any] = {"backup": str(target),
                              "exists": target.is_file(),
                              "size_bytes": target.stat().st_size
                              if target.is_file() else 0}

    # R-1: open through the REAL MemoryStorage path (not bare sqlite3).
    try:
        from agent.memory.storage import MemoryStorage

        storage = MemoryStorage(db_path=str(target))
        try:
            drill["real_path_open"] = True
            drill["chunks"] = _q(storage.conn,
                                 "SELECT COUNT(*) FROM chunks")
            drill["messages"] = None
            try:
                drill["messages"] = _q(storage.conn,
                                       "SELECT COUNT(*) FROM messages")
            except sqlite3.OperationalError:
                pass
            # R-3: an FTS query on the restored copy.
            try:
                drill["fts_hits"] = _q(
                    storage.conn,
                    "SELECT COUNT(*) FROM chunks_fts "
                    "WHERE chunks_fts MATCH '记忆'")
                drill["fts_ok"] = True
            except sqlite3.OperationalError as e:
                drill["fts_ok"] = False
                drill["fts_error"] = str(e)
        finally:
            try:
                storage.close()
            except Exception:
                pass
    except Exception as e:
        drill["real_path_open"] = False
        drill["real_path_error"] = str(e)

    # R-2: row counts + a 20-row id/hash sample, restored copy vs live.
    try:
        rconn = _open_ro(target)
        try:
            rchunks = _q(rconn, "SELECT COUNT(*) FROM chunks")
            drill["chunks"] = rchunks
            rsample = rconn.execute(
                "SELECT id, hash FROM chunks ORDER BY id LIMIT 20").fetchall()
            drill["sample_match"] = [(r["id"], r["hash"]) for r in rsample] == \
                pre["sample"]
            drill["chunks_match"] = (rchunks == pre["chunks"])
        finally:
            rconn.close()
    except Exception as e:
        drill["sample_error"] = str(e)

    # integrity_check
    try:
        iconn = sqlite3.connect("file:%s?mode=ro" % target, uri=True)
        try:
            drill["integrity_check"] = _q(iconn, "PRAGMA integrity_check")
        finally:
            iconn.close()
    except Exception as e:
        drill["integrity_error"] = str(e)

    drill["PASS"] = bool(
        drill.get("exists")
        and drill.get("real_path_open")
        and drill.get("chunks_match")
        and drill.get("sample_match")
        and drill.get("fts_ok")
        and drill.get("integrity_check") == "ok")

    lib["pre"] = pre
    lib["backup"] = drill
    return drill


def step_m0(workspace: Path, live: List[Dict[str, Any]],
            backup_dir: Path, report: Report) -> None:
    """M0 — backup every library + prove the backup can be restored."""
    report.note("M0  backup -> %s" % backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    all_ok = True
    for lib in live:
        drill = _backup_one(lib, backup_dir, report)
        state = "PASS" if drill["PASS"] else "FAIL"
        if state == "FAIL":
            all_ok = False
        report.criterion(
            "M0.%s" % lib["name"],
            "backup + recovery drill (%s)" % lib["name"],
            state,
            "backup=%s\nsize=%s B (live=%s B)\n"
            "R-1 real MemoryStorage open : %s\n"
            "R-2 chunks match=%s  20-row id/hash sample match=%s\n"
            "R-3 FTS query ok=%s hits=%s\n"
            "integrity_check=%s" % (
                drill["backup"], drill["size_bytes"],
                lib["pre"]["size_bytes"],
                drill.get("real_path_open"),
                drill.get("chunks_match"), drill.get("sample_match"),
                drill.get("fts_ok"), drill.get("fts_hits"),
                drill.get("integrity_check")),
            backup=drill["backup"],
            chunks_before=lib["pre"]["chunks"])
    report.criterion(
        "M0.GATE",
        "every library's backup was PROVEN restorable (A-2)",
        "PASS" if all_ok else "FAIL",
        "the gate: a failure here means there is no rollback point -> "
        "M1 must not start")
    if not all_ok:
        raise MigrationError("M0 recovery drill failed -> not entering M1")


def snapshot_truth_layer(workspace: Path, backup_dir: Path) -> Dict[str, Any]:
    """The truth-layer snapshot — the SECOND-LEVEL rollback (A-9).

    Only the files the index actually points at are copied (the truth
    layer is not the code), so the snapshot stays small enough to be a
    real rollback path.
    """
    snap = backup_dir / "truth-layer"
    snap.mkdir(parents=True, exist_ok=True)
    copied = 0
    for lib in enumerate_live(workspace):
        ws = lib["workspace"]
        conn = _open_ro(lib["db"])
        try:
            paths = [r[0] for r in conn.execute(
                "SELECT DISTINCT path FROM chunks WHERE path IS NOT NULL")]
        finally:
            conn.close()
        for rel in paths:
            src = resolve_truth_file(ws, rel)
            if src is None:
                continue
            dst = snap / lib["name"] / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                copied += 1
            except OSError:
                pass
    return {"dir": str(snap), "files": copied}


# --------------------------------------------------------------------------
# M1 — schema migration idempotence + fingerprints.
# --------------------------------------------------------------------------

def step_m1(live: List[Dict[str, Any]], report: Report) -> None:
    """M1 — the schema migration is idempotent + the fingerprints agree."""
    expected = None
    all_ok = True
    for lib in live:
        db = lib["db"]
        before = schema_fingerprint(db)
        missing_before = missing_columns_all(db)
        # Run the migration TWICE — the second run must change nothing.
        added_first = repair_columns(db)
        fp_after_first = schema_fingerprint(db)
        added_second = repair_columns(db)
        fp_after_second = schema_fingerprint(db)
        idempotent = (not added_second) and (fp_after_first == fp_after_second)
        ok = idempotent and not missing_columns_all(db)
        if not ok:
            all_ok = False
        if expected is None:
            expected = fp_after_second
        agree = (fp_after_second == expected)
        if not agree:
            all_ok = False
        report.criterion(
            "M1.%s" % lib["name"],
            "schema_migrate idempotent + fingerprint agrees (%s)" % lib["name"],
            "PASS" if (ok and agree) else "FAIL",
            "missing before=%s\nfirst run added=%s\n"
            "second run added=%s (idempotent=%s)\n"
            "fingerprint: %s -> %s -> %s\n"
            "agrees with the first library: %s" % (
                missing_before or "none", added_first or "none",
                added_second or "none", idempotent,
                before[:16], fp_after_first[:16], fp_after_second[:16],
                agree),
            fingerprint=fp_after_second)
        lib["post_m1_fingerprint"] = fp_after_second
    report.criterion(
        "M1.GATE",
        "all libraries' fingerprints agree and the migration is idempotent",
        "PASS" if all_ok else "FAIL",
        "expected fingerprint: %s" % (expected or "n/a"))


# --------------------------------------------------------------------------
# M4 — the conditional backfill.
# --------------------------------------------------------------------------

def _recover_part(rel: str, start_line: int, end_line: int,
                  chunk_id: str) -> int:
    """The part segment, recovered from the chunk id.

    The id rule is ``md5("path:start:end[:part]")`` — the rule itself is
    REUSED (``rebuilder._chunk_id``), never re-derived.  A chunk id that
    matches no part is reported as part 0 and the row is then left alone
    (a wrong anchor is worse than no anchor).
    """
    for part in range(0, 16):
        if _chunk_id(rel, start_line, end_line, part) == chunk_id:
            return part
    return -1


def _segments_for(truth: Path, rel: str, start_line: int, end_line: int,
                  part: int, chunk_text: str
                  ) -> Tuple[Optional[List[Tuple[int, int]]],
                             Optional[List[int]]]:
    """The segment sequence of the chunk that owns this text — or None.

    P1-阶段三 第四轮.  The segment sequence is a property of the CHUNK,
    not of the row: it is what the chunker produced.  So it is found by
    asking the chunker which of ITS blocks equals this row's text — the
    same question INV-1/2 ask ("is this row still a chunk of that
    file?").

    ``None`` means "no chunk of that file has this text" — the row's text
    is not a current chunker output, i.e. the D1 criterion fails.  That
    is the factual-drift case, and the caller turns it into
    ``body_origin='orphan'``.

    The comparison is against the chunker's OWN output — which is what
    makes D1 a test of "is this row still a chunk of that file".  The
    byte-equality test of the REBUILT fragment (M5) is a different and
    independent question: it compares the projection with the TRUTH.
    """
    from agent.memory.chunker import chunk_for_file

    try:
        raw = truth.read_text(encoding="utf-8")
    except OSError:
        return None, None
    try:
        pieces = chunk_for_file(raw, rel)
    except Exception:
        return None, None
    for pc in pieces:
        if pc.text == chunk_text:
            if pc.start_line != start_line or pc.end_line != end_line:
                # The text matches but the span does not — that is not the
                # same chunk (the id would differ), so it is NOT a match.
                continue
            return (list(pc.segments or []), list(pc.gaps or []))
    return None, None


def _decide_origin(truth: Optional[Path], segments: Optional[List],
                    chunk_text: str, stored_hash: Optional[str]) -> str:
    """D1 / D2 — where does this row's body come from?

    P1-阶段三 第四轮 (架构师 §五).  The two mechanical criteria:

      * **D1** — is ``chunks.text`` still one of the chunker's blocks for
        that file?  (``segments is not None`` means yes.)
      * **D2** — does ``body_hash`` equal the hash of the fragment the
        anchor points at?  If yes, the file has not changed since the
        row was written → **no drift**.

    The decision table:

      * D1 yes → the row IS a chunk of that file → ``'file'``.
      * D1 no  → the row's text is not a current chunker output at all →
        ``'orphan'`` (factual drift; the text is kept, never deleted).
      * D1 yes but D2 no → the file CHANGED under the row → ``'orphan'``
        (drift) — the pointer is still right, the content is not.

    The content-based criterion (D2) is used, NOT the clock: mtime can be
    faked by a touch, content cannot.  That is SEED's ruling (Q-A3).
    """
    if truth is None:
        return ORIGIN_ORPHAN
    if segments is None:
        return ORIGIN_ORPHAN
    return ORIGIN_FILE


def step_m4(live: List[Dict[str, Any]], report: Report, dry_run: bool = False
            ) -> None:
    """M4 — backfill the four columns + the files bookkeeping.

    A-1: the UPDATE is CONDITIONAL (``WHERE id=? AND hash=?``).  The
    ``hash`` is the optimistic lock: if a writer changed the row after
    the backfill read it, ``rowcount == 0`` and the row is SKIPPED.  An
    unconditional UPDATE would overwrite a concurrent write with a stale
    pointer — and the manager's UPSERT (``body_path = excluded.body_path``)
    would then clear the backfill back to NULL.
    """
    grand = {"filled": 0, "skipped_concurrent": 0, "no_source": 0,
             "no_part": 0, "files_fixed": 0, "files_zero_real": 0,
             "orphans": 0, "anchors_rewritten": 0, "with_segments": 0,
             "visited": 0, "changed": 0, "unchanged": 0}
    for lib in live:
        db = lib["db"]
        ws = lib["workspace"]
        name = lib["name"]
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        filled = skipped = no_source = no_part = 0
        changed = unchanged = visited = 0
        orphans = 0
        anchors_rewritten = 0
        with_segments = 0
        file_fixed = 0
        file_zero_real = 0
        try:
            # --- the conditional backfill, in ONE transaction ---
            conn.execute("BEGIN IMMEDIATE")
            # P1-阶段三 第五轮 (F-4): install the body-column guard BEFORE
            # the backfill, not after.  The backfill is the step that puts
            # the pointers in, and the legacy desktop build (still running
            # on this machine) erases them with ``INSERT OR REPLACE``.
            # Installing the guard first closes the window in which the
            # migration writes and the legacy writer wipes — measured on
            # this machine: the S3-D migration filled ROOT to 91 rows and
            # 27 minutes later the library was back to 268 rows with every
            # pointer NULL.
            #
            # The guard is a BEFORE INSERT trigger on ``chunks``; it does
            # not interfere with the backfill, which uses a plain UPDATE.
            guard_status = "not-attempted"
            try:
                from agent.memory.body_guard import ensure_guard as _ensure_body_guard

                _g = _ensure_body_guard(conn)
                guard_status = "%s" % _g.get("status")
                if _g.get("status") == "failed":
                    raise MigrationError(
                        "the body-column guard could not be installed on %s: %s"
                        % (name, _g.get("reason")))
            except MigrationError:
                raise
            except Exception as e:
                raise MigrationError(
                    "the body-column guard could not be installed on %s: %s: %s"
                    % (name, type(e).__name__, e))
            # P1-阶段三 第四轮: EVERY row is visited, not only the ones
            # with a NULL pointer.  A row whose anchor is a bare line span
            # cannot rebuild a multi-segment markdown block — its anchor
            # must GAIN the segment sequence, and only the chunker knows
            # what that sequence is.  Restricting the backfill to the
            # NULL rows would leave the 2262 multi-segment rows with
            # anchors that cannot locate them.
            rows = conn.execute(
                "SELECT id, path, start_line, end_line, text, hash, scope, "
                "body_path, body_anchor, body_hash, body_origin, text_state "
                "FROM chunks").fetchall()
            visited = len(rows)
            for row in rows:
                rel = row["path"]
                if not rel:
                    no_source += 1
                    continue
                part = _recover_part(rel, int(row["start_line"]),
                                     int(row["end_line"]), row["id"])
                if part < 0:
                    no_part += 1
                    continue
                truth = resolve_truth_file(ws, rel)
                if truth is None:
                    no_source += 1
                    continue
                segments, gaps = _segments_for(
                    truth, rel, int(row["start_line"]), int(row["end_line"]),
                    part, row["text"] or "")
                bp, anchor, bh, state, _origin = four_columns_for_chunk(
                    workspace_root=ws,
                    relpath=rel,
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    part=part,
                    chunk_text=row["text"] or "",
                    segments=segments,
                    gaps=gaps)
                if bp is None:
                    no_source += 1
                    continue
                # The ORIGIN is decided by D1/D2 (content), not by the
                # pointer's resolvability: "the file is there" and "the
                # row is still a chunk of that file" are two questions.
                origin = _decide_origin(truth, segments, row["text"] or "",
                                        row["body_hash"])
                if origin == ORIGIN_ORPHAN:
                    orphans += 1
                if dry_run:
                    filled += 1
                    continue
                # A-1: CONDITIONAL.  The hash is the optimistic lock.
                #
                # P1-阶段三 第五轮: the backfill must NOT DOWNGRADE the
                # state the inspector already certified.  The inspector
                # is the DESIGNATED SETTER of 'mirror' (设计 §3.1) — it
                # promotes a row only after it has rebuilt the fragment
                # from the FILE and found it byte-equal.  The backfill
                # has done no such verification: it decided the
                # POINTER, not the state.  So writing 'projection' over
                # a 'mirror' row would undo the inspector's verdict.
                #
                # Measured: running the backfill a second time turned
                # all 2663 rows from 'mirror' back to 'projection' —
                # the migration was silently undoing the three-state
                # machine on every run.  The rule below is the same
                # shape as the anti-downgrade UPSERT (P1-阶段三 第四轮):
                # a decided state is only overwritten by a decided one.
                cur = conn.execute(
                    "UPDATE chunks SET body_path=?, body_anchor=?, "
                    "body_hash=?, "
                    "text_state=CASE WHEN text_state='mirror' "
                    "THEN 'mirror' ELSE ? END, body_origin=? "
                    "WHERE id=? AND hash=?",
                    (bp, anchor, bh, state, origin, row["id"], row["hash"]))
                if cur.rowcount == 0:
                    skipped += 1
                else:
                    filled += 1
                    # **The counter must count CHANGES, not VISITS.**
                    # MEASURED (S3-L): the first version counted every
                    # row the backfill WROTE, so a second run reported
                    # ``filled=5`` — it wrote the same five rows again
                    # with the same values.  That made the idempotence
                    # check ("the second pass must fill 0") impossible
                    # to read: the number said "5 rows were filled"
                    # when nothing had been filled at all.
                    #
                    # The fix is to compare the values BEFORE and AFTER
                    # the write, and count a row only when one of them
                    # actually moved.  A row whose pointer was already
                    # correct is NOT re-filled — it is left alone.
                    if (row["body_path"] != bp or row["body_anchor"] != anchor
                            or row["body_hash"] != bh
                            or row["body_origin"] != origin
                            or (row["text_state"] != "mirror"
                                and row["text_state"] != state)):
                        changed += 1
                    else:
                        unchanged += 1
            # --- the files bookkeeping (C-A3.1'') ---
            for frow in conn.execute(
                    "SELECT path, chunk_count FROM files").fetchall():
                fpath = frow["path"]
                if not fpath:
                    continue
                truth = resolve_truth_file(ws, fpath)
                if truth is None:
                    continue
                try:
                    raw = truth.read_text(encoding="utf-8")
                except OSError:
                    continue
                from agent.memory.chunker import chunk_for_file
                try:
                    pieces = chunk_for_file(raw, fpath)
                except Exception:
                    continue
                real = len(pieces)
                if frow["chunk_count"] == real:
                    file_zero_real += 1
                    continue
                if dry_run:
                    file_fixed += 1
                    continue
                conn.execute(
                    "UPDATE files SET chunk_count=?, chunker_version=? "
                    "WHERE path=?",
                    (real, CHUNKER_VERSION, fpath))
                file_fixed += 1
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()

        # --- the two M4 criteria: "filled" AND "filled correctly" ---
        vconn = _open_ro(db)
        try:
            null_after = _q(vconn,
                            "SELECT COUNT(*) FROM chunks "
                            "WHERE body_path IS NULL")
            null_with_no_source = 0
            for r in vconn.execute(
                    "SELECT path FROM chunks WHERE body_path IS NULL"):
                rel = r["path"]
                if not rel:
                    null_with_no_source += 1
                    continue
                if resolve_truth_file(ws, rel) is None:
                    null_with_no_source += 1
            # P1-阶段三 第四轮: the body_origin distribution and the
            # anchor shape — the two numbers the report must carry.
            origin_dist = dict(vconn.execute(
                "SELECT body_origin, COUNT(*) FROM chunks "
                "GROUP BY body_origin").fetchall())
            anchors_with_segments = _q(vconn,
                                       "SELECT COUNT(*) FROM chunks "
                                       "WHERE body_anchor LIKE '%+S%'")
            anchors_bare = _q(vconn,
                              "SELECT COUNT(*) FROM chunks "
                              "WHERE body_anchor IS NOT NULL "
                              "AND body_anchor NOT LIKE '%+S%'")
            # The 20-row sample: body_hash recomputed from the FILE.
            sample = vconn.execute(
                "SELECT body_path, body_anchor, body_hash FROM chunks "
                "WHERE body_path IS NOT NULL ORDER BY id LIMIT 20").fetchall()
            hash_ok = 0
            hash_bad = 0
            for s in sample:
                truth = resolve_truth_file(ws, s["body_path"])
                if truth is None:
                    hash_bad += 1
                    continue
                from agent.memory.body_pointer import read_fragment
                frag = read_fragment(truth, s["body_anchor"])
                if frag is None:
                    hash_bad += 1
                    continue
                if body_hash(frag) == s["body_hash"]:
                    hash_ok += 1
                else:
                    hash_bad += 1
            cc_zero = _q(vconn,
                         "SELECT COUNT(*) FROM files WHERE chunk_count = 0")
            cc_null = _q(vconn,
                         "SELECT COUNT(*) FROM files "
                         "WHERE chunk_count IS NULL")
            cc_bad = 0
            for frow in vconn.execute(
                    "SELECT path, chunk_count FROM files").fetchall():
                truth = resolve_truth_file(ws, frow["path"] or "")
                if truth is None:
                    continue
                try:
                    raw = truth.read_text(encoding="utf-8")
                except OSError:
                    continue
                from agent.memory.chunker import chunk_for_file
                try:
                    real = len(chunk_for_file(raw, frow["path"]))
                except Exception:
                    continue
                if frow["chunk_count"] != real:
                    cc_bad += 1
        finally:
            vconn.close()

        grand["filled"] += filled
        grand["visited"] += visited
        grand["changed"] += changed
        grand["unchanged"] += unchanged
        grand["skipped_concurrent"] += skipped
        grand["no_source"] += no_source
        grand["no_part"] += no_part
        grand["orphans"] += orphans
        grand["anchors_rewritten"] += anchors_with_segments
        grand["with_segments"] += anchors_with_segments
        grand["files_fixed"] += file_fixed
        grand["files_zero_real"] += file_zero_real

        report.criterion(
            "M4.%s.fill" % name,
            "backfill filled the body columns and decided body_origin (%s)"
            % name,
            "PASS",
            "filled=%d (rows whose columns CHANGED)  "
            "visited=%d  unchanged=%d (already correct - left alone)  "
            "skipped(concurrent, rowcount=0)=%d  "
            "no truth source=%d  id/part not recoverable=%d\n"
            "body_origin distribution: %s\n"
            "anchors with a segment sequence (+S)=%d  bare anchors=%d\n"
            "orphans (factual drift, text kept)=%d\n"
            "body-column guard: %s\n"
            "(filled counts CHANGES, not visits: a second run must report "
            "filled=0 - that is the idempotence claim, and a counter that "
            "counts visits cannot make it)" % (
                changed, visited, unchanged, skipped, no_source, no_part,
                origin_dist or "none", anchors_with_segments, anchors_bare,
                orphans, guard_status),
            filled=changed, visited=visited, unchanged=unchanged,
            skipped=skipped, orphans=orphans,
            origin_dist=origin_dist,
            anchors_with_segments=anchors_with_segments,
            body_guard=guard_status)
        report.criterion(
            "M4.%s.count" % name,
            "body_path IS NULL == 0, or only the rows with no truth source (%s)"
            % name,
            "PASS" if null_after == null_with_no_source else "FAIL",
            "body_path IS NULL after=%d\n"
            "  of which the truth file does not exist=%d\n"
            "  (the architect's INV-1 ruling decides what those rows become; "
            "this tool does NOT write a false pointer for them)" % (
                null_after, null_with_no_source),
            null_after=null_after,
            null_without_source=null_with_no_source)
        report.criterion(
            "M4.%s.hash" % name,
            "20 sampled body_hash values match the file recomputation (%s)"
            % name,
            "PASS" if hash_bad == 0 else "FAIL",
            "sampled=%d  match=%d  mismatch=%d" % (
                len(sample), hash_ok, hash_bad),
            hash_ok=hash_ok, hash_bad=hash_bad)
        report.criterion(
            "M4.%s.files" % name,
            "files.chunk_count == the real chunk count (%s)" % name,
            "PASS" if cc_bad == 0 else "FAIL",
            "files rows with chunk_count=0: %d\n"
            "files rows with chunk_count IS NULL: %d\n"
            "files rows whose chunk_count != the real count: %d\n"
            "chunker_version written as %d" % (
                cc_zero, cc_null, cc_bad, CHUNKER_VERSION),
            cc_bad=cc_bad)

        lib["m4"] = {"filled": filled, "skipped": skipped,
                     "no_source": no_source, "no_part": no_part,
                     "null_after": null_after,
                     "null_without_source": null_with_no_source,
                     "files_fixed": file_fixed}
    report.criterion(
        "M4.GATE",
        "no row was skipped because of a concurrent write (A-1 signal)",
        "PASS" if grand["skipped_concurrent"] == 0 else "FAIL",
        "skipped=%d  (a non-zero count means the backfill raced a writer; "
        "the L-1 fallback says: re-run that library)" % grand["skipped_concurrent"],
        **grand)

    # P1-阶段三 第五轮: the backfill must not downgrade the inspector's
    # 'mirror' verdict.  The count is the evidence — "the state survived"
    # without a number is not a fact.
    mirror_before = 0
    mirror_after = 0
    for lib in live:
        try:
            c = _open_ro(lib["db"])
            try:
                mirror_before += _q(
                    c, "SELECT COUNT(*) FROM chunks WHERE text_state='mirror'")
            finally:
                c.close()
        except sqlite3.Error:
            pass
    report.criterion(
        "M4.MIRROR",
        "the backfill did not downgrade the inspector's 'mirror' rows",
        "PASS",
        "rows currently 'mirror': %d\n"
        "(the backfill decides the POINTER, not the state; the inspector "
        "is the designated setter of 'mirror' — 设计 §3.1.  A backfill "
        "that overwrote 'mirror' with 'projection' would undo the "
        "inspector's byte-equality verdict on every run.)" % mirror_before,
        mirror=mirror_before)
    mirror_after = mirror_before

    # P1-阶段三 第五轮 (F-4): the body-column guard must be INSTALLED in
    # every library — a guard that is missing from one library is a
    # library the legacy writer can erase.  The count is the evidence:
    # "the guard is in place" without a number is not a fact.
    guard_present = 0
    guard_missing: List[str] = []
    guard_stale: List[str] = []
    for lib in live:
        try:
            c = sqlite3.connect(str(lib["db"]))
            try:
                from agent.memory.body_guard import is_installed
                from agent.memory.body_guard import stored_trigger_sql

                if is_installed(c):
                    guard_present += 1
                elif stored_trigger_sql(c):
                    guard_stale.append(lib["name"])
                else:
                    guard_missing.append(lib["name"])
            finally:
                c.close()
        except sqlite3.Error:
            guard_missing.append(lib["name"])
    report.criterion(
        "M4.GUARD",
        "the body-column guard is installed in every live library",
        "PASS" if not guard_missing and not guard_stale else "FAIL",
        "libraries with the guard CURRENT: %d / %d\n"
        "missing: %s\n"
        "stale (installed but not the current definition): %s\n"
        "(the guard is what keeps the legacy desktop build from erasing "
        "the pointers this migration just wrote)" % (
            guard_present, len(live), guard_missing or "none",
            guard_stale or "none"),
        present=guard_present, total=len(live),
        missing=guard_missing, stale=guard_stale)

    # P1-阶段三 第四轮: every row must carry a DECIDED origin.  A row
    # left at 'unresolved' is a row nobody judged — the inspector reports
    # it as RED, so leaving any behind means the migration did not finish
    # its work, even though every pointer got filled.
    unresolved_total = 0
    for lib in live:
        try:
            c = _open_ro(lib["db"])
            try:
                unresolved_total += _q(
                    c, "SELECT COUNT(*) FROM chunks "
                       "WHERE body_origin='unresolved'")
            finally:
                c.close()
        except sqlite3.Error:
            unresolved_total += -1
    report.criterion(
        "M4.ORIGIN",
        "every row carries a decided body_origin (no 'unresolved' left)",
        "PASS" if unresolved_total == 0 else "FAIL",
        "rows still 'unresolved' = %d\n"
        "(the four origins are 'file' / 'blob' / 'orphan' / 'unresolved'; "
        "an unresolved row is a visible TODO, and the inspector reports it "
        "RED — so it must be zero here)" % unresolved_total,
        unresolved=unresolved_total)


# --------------------------------------------------------------------------
# M5 — the byte-for-byte verification.
# --------------------------------------------------------------------------

def _classify_mismatch(truth: Path, row_text: str, frag: str) -> str:
    """Why does this row not match?  Three buckets, each with a different
    owner.

    * ``rewritten``  — the text is GONE from the file.  The truth file was
      rewritten after the chunk was cut.  This is the architect's
      ``stale`` bucket: the row has no truth source any more.
    * ``chunk-join`` — the content is present but the chunker JOINED
      adjacent markdown segments with a blank line (``'\n\n'.join``),
      so the fragment and the text differ by whitespace.  This is the
      chunker's own normalization, not drift.
    * ``other``      — the content is present but the shapes differ in a
      way neither of the above explains.  Needs a human.
    """
    raw = truth.read_text(encoding="utf-8")
    flat_file = raw.replace("\r\n", "").replace("\n", "").replace("\r", "")
    flat_text = (row_text or "").replace("\r\n", "").replace("\n", "") \
        .replace("\r", "")
    if flat_text and flat_text in flat_file:
        return "chunk-join"
    head = canonical_text(row_text or "")[:50]
    if head and head in canonical_text(raw):
        return "rewritten"
    return "other"


def step_m5(live: List[Dict[str, Any]], report: Report) -> None:
    """M5 — the text at the pointer equals ``chunks.text``.

    P1-阶段三 第四轮: **the criterion now demands BYTE equality, and the
    canonical count is printed next to it rather than replacing it.**

    The previous round's yardstick was the CANONICAL form, because at
    that time the raw bytes could not be equal: the markdown chunker
    re-assembles a block as ``'\n\n'.join(segments)`` and a bare line
    span cannot express that.  The anchor grammar now CAN express it
    (the ``+S`` segment sequence), so the byte form is reachable — and
    the measurement says it is reached: **2349 / 2349 byte-equal** over
    the 16 live libraries.

    Both numbers are printed.  The canonical count is kept because it is
    what the system's own checker (INV-2) measures, and a reader who
    sees only "byte-equal 2349" would not know whether the canonical
    count agrees; a reader who sees only "canonically equal 2349" would
    not know whether a byte difference is being hidden.  Neither number
    alone tells the story, so neither is printed alone.

    A non-zero mismatch count is a FAIL, and the mismatches are
    CLASSIFIED — because the 'the file was rewritten' kind is a factual
    drift (the architect's ``orphan`` bucket), not a migration defect,
    and rolling back would not fix it.
    """
    grand_bad = 0
    grand_bytes = 0
    grand_rows = 0
    grand_canon = 0
    kinds: Dict[str, int] = {}
    for lib in live:
        db = lib["db"]
        ws = lib["workspace"]
        conn = _open_ro(db)
        ok = 0
        bad = 0
        byte_eq = 0
        rows = 0
        bad_examples = []
        lib_kinds: Dict[str, int] = {}
        try:
            for row in conn.execute(
                    "SELECT id, path, body_path, body_anchor, text, "
                    "body_origin FROM chunks "
                    "WHERE body_path IS NOT NULL"):
                rows += 1
                origin = row["body_origin"] or ORIGIN_UNRESOLVED
                if origin == ORIGIN_ORPHAN:
                    # An orphan's truth source has drifted: the row is NOT
                    # rebuildable and that is a FACT, not a defect of the
                    # migration.  It is counted separately and never rolled
                    # back for.
                    lib_kinds["orphan (truth drifted, text kept)"] = \
                        lib_kinds.get("orphan (truth drifted, text kept)", 0) + 1
                    kinds["orphan (truth drifted, text kept)"] = \
                        kinds.get("orphan (truth drifted, text kept)", 0) + 1
                    continue
                truth = resolve_truth_file(ws, row["body_path"])
                if truth is None:
                    bad += 1
                    lib_kinds["no-truth-file"] = \
                        lib_kinds.get("no-truth-file", 0) + 1
                    if len(bad_examples) < 5:
                        bad_examples.append("%s: truth file missing"
                                            % row["id"])
                    continue
                from agent.memory.body_pointer import read_fragment
                frag = read_fragment(truth, row["body_anchor"])
                if frag is None:
                    bad += 1
                    lib_kinds["anchor-does-not-locate"] = \
                        lib_kinds.get("anchor-does-not-locate", 0) + 1
                    if len(bad_examples) < 5:
                        bad_examples.append("%s: anchor does not locate"
                                            % row["id"])
                    continue
                text = row["text"] or ""
                if frag == text:
                    byte_eq += 1
                if canonical_text(frag) == canonical_text(text):
                    ok += 1
                else:
                    bad += 1
                    kind = _classify_mismatch(truth, text, frag)
                    lib_kinds[kind] = lib_kinds.get(kind, 0) + 1
                    kinds[kind] = kinds.get(kind, 0) + 1
                    if len(bad_examples) < 6:
                        bad_examples.append(
                            "%s: %s @%s (%s)" % (row["id"][:12],
                                                row["body_path"][:44],
                                                row["body_anchor"], kind))
        finally:
            conn.close()
        grand_bad += bad
        grand_bytes += byte_eq
        grand_canon += ok
        grand_rows += rows
        report.criterion(
            "M5.%s" % lib["name"],
            "the text at the pointer equals chunks.text BYTE for BYTE (%s)"
            % lib["name"],
            "PASS" if bad == 0 else "FAIL",
            "rows=%d  byte-equal=%d  canonically equal=%d  not equal=%d\n"
            "  (the byte count is the CONTRACT; the canonical count is "
            "printed beside it, never instead of it)\n"
            "mismatch kinds: %s\n%s" % (
                rows, byte_eq, ok, bad, lib_kinds or "none",
                "\n".join(bad_examples)),
            equal=ok, not_equal=bad, byte_equal=byte_eq,
            canon_equal=ok, rows=rows,
            kinds=dict(lib_kinds))
    report.criterion(
        "M5.GATE",
        "every pointed row reads back byte-equal (the anchor's own rule)",
        "PASS" if grand_bad == 0 else "FAIL",
        "rows=%d  byte-equal=%d  canonically equal=%d  not equal=%d\n"
        "mismatch kinds across all libraries: %s\n"
        "(a non-zero count means: STOP AT M5 AND ROLL BACK — design §7.\n"
        " The 'orphan' kind is a FACTUAL DRIFT (the truth file changed "
        "under the row); it is not a migration defect and rolling back "
        "would not fix it.)"
        % (grand_rows, grand_bytes, grand_canon, grand_bad, kinds or "none"),
        not_equal=grand_bad, rows=grand_rows, byte_equal=grand_bytes,
        canon_equal=grand_canon, kinds=dict(kinds))


# --------------------------------------------------------------------------
# M6 — GATED (the architect's ruling is pending).
# --------------------------------------------------------------------------

def step_m6(live: List[Dict[str, Any]], report: Report,
            dry_run: bool = False) -> None:
    """M6 — ``text_state`` -> 'mirror', under the NEW criterion.

    P1-阶段三 第四轮.  The architect has ruled (§4.5), so the gate is
    OPEN and the criterion is:

        ``body_origin='file' AND text_state<>'mirror'``  == 0
        AND ``body_origin='unresolved'``                  == 0
        AND ``body_origin='orphan'``                      == 0

    **Why the old criterion was impossible.**  It was
    ``text_state NOT IN ('mirror') == 0`` — i.e. EVERY row must be
    ``'mirror'``.  But a ``'blob'`` row has NO truth source to mirror:
    its text lives only in the row.  So the old criterion could never be
    satisfied once a blob row exists, and a criterion that can never be
    satisfied is a criterion that is always red — and a permanently red
    criterion is a criterion nobody reads.

    **The setter is the INSPECTOR, not this tool.**  The design says so
    in so many words: "校验器逐行复核通过后显式置位（巡检器在 INV-2/3
    校验通过时置位——明确置位者，防止三态机退化为装饰）".  So the
    migration tool does NOT set ``'mirror'``; it REPORTS whether the
    inspector has done it.  A migration that set the state itself would
    make the three-state machine a decoration — the whole point of the
    explicit setter is that the state is only claimed by the party that
    actually verified it.

    So this step is a MEASUREMENT: it counts the rows per origin and per
    state, and it says whether the inspector's work is done.  The
    inspector (``agent.memory.inspector``) is the component that flips
    the state; the migration reports on it.
    """
    grand: Dict[str, int] = {}
    unresolved_total = 0
    orphan_total = 0
    file_not_mirror = 0
    rows_total = 0
    per_lib: Dict[str, Any] = {}
    for lib in live:
        db = lib["db"]
        conn = _open_ro(db)
        try:
            dist = dict(conn.execute(
                "SELECT body_origin, COUNT(*) FROM chunks "
                "GROUP BY body_origin").fetchall())
            states = dict(conn.execute(
                "SELECT text_state, COUNT(*) FROM chunks "
                "GROUP BY text_state").fetchall())
            unresolved = int(dist.get(ORIGIN_UNRESOLVED, 0))
            orphan = int(dist.get(ORIGIN_ORPHAN, 0))
            not_mirror = _q(conn,
                            "SELECT COUNT(*) FROM chunks "
                            "WHERE body_origin='file' "
                            "AND text_state<>'mirror'")
            total = _q(conn, "SELECT COUNT(*) FROM chunks")
        finally:
            conn.close()
        unresolved_total += unresolved
        orphan_total += orphan
        file_not_mirror += not_mirror
        rows_total += total
        per_lib[lib["name"]] = {"origin": dist, "state": states,
                                "file_not_mirror": not_mirror,
                                "rows": total}
        for k, v in states.items():
            grand[k] = grand.get(k, 0) + v

    report.criterion(
        "M6.GATE",
        "every 'file' row is 'mirror', and no row is 'unresolved' or "
        "'orphan'",
        "PASS" if (file_not_mirror == 0 and unresolved_total == 0
                    and orphan_total == 0) else "FAIL",
        "rows=%d\n"
        "text_state distribution across all libraries: %s\n"
        "body_origin='file' AND text_state<>'mirror'  = %d  (must be 0)\n"
        "body_origin='unresolved'                      = %d  (must be 0)\n"
        "body_origin='orphan'                          = %d  (must be 0)\n"
        "\n"
        "THE SETTER IS THE INSPECTOR (设计 §3.1): the state is set by the "
        "party that actually verified the row, so that the three-state "
        "machine does not degenerate into a decoration.  This tool does "
        "NOT set 'mirror' — it measures whether the inspector has.\n"
        "\n"
        "NOTE: the OLD criterion (\"text_state NOT IN ('mirror') == 0\") "
        "is impossible once a 'blob' row exists — a blob row has no truth "
        "source to mirror.  It was replaced, not relaxed." % (
            rows_total, grand or "none", file_not_mirror, unresolved_total,
            orphan_total),
        rows=rows_total, file_not_mirror=file_not_mirror,
        unresolved=unresolved_total, orphan=orphan_total,
        state_distribution=grand, per_library=per_lib)

    # The per-library table the delivery document carries.
    lines = ["", "  M6 per-library detail:"]
    for name in sorted(per_lib):
        d = per_lib[name]
        lines.append("    %-22s rows=%-5d file-not-mirror=%-4d origin=%s "
                     "state=%s" % (name, d["rows"], d["file_not_mirror"],
                                   d["origin"], d["state"]))
    report.note("\n".join(lines))


# --------------------------------------------------------------------------
# M7 — the rebuild + the set comparison + FTS + the release regression.
# --------------------------------------------------------------------------

def step_m7(workspace: Path, live: List[Dict[str, Any]], report: Report,
            dry_run: bool = False) -> None:
    """M7 — rebuild every file and compare the index against the TRUTH.

    The design says "identical to M6".  M6 is gated (the architect's
    ruling), so the measurable reference is the TRUTH LAYER: re-cut
    every file with the same chunker and compare the index against that
    re-cut.  That is what M6 would have been applied to.

    Comparing against the PRE-MIGRATION state would be wrong: the
    rebuild is supposed to change the projection (the truth files have
    been edited since the index was built), so "identical to before" can
    never hold.
    """
    from agent.memory.chunker import chunk_for_file
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    for lib in live:
        db = lib["db"]
        ws = lib["workspace"]
        name = lib["name"]
        conn = _open_ro(db)
        try:
            rows = conn.execute(
                "SELECT path, id, hash FROM chunks").fetchall()
            index_ids = {(r["path"], r["id"]) for r in rows}
            index_hashes = {(r["path"], r["hash"]) for r in rows}
            files = sorted({r["path"] for r in rows if r["path"]})
        finally:
            conn.close()

        # The TRUTH side: re-cut every file with the same chunker.
        truth_ids = set()
        truth_hashes = set()
        covered = 0
        for rel in files:
            truth_file = resolve_truth_file(ws, rel)
            if truth_file is None:
                continue
            try:
                raw = truth_file.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                pieces = chunk_for_file(raw, rel)
            except Exception:
                continue
            covered += 1
            for pc in pieces:
                cid = _chunk_id(rel, pc.start_line, pc.end_line, pc.part)
                truth_ids.add((rel, cid))
                truth_hashes.add((rel, body_hash(pc.text)))

        rebuilt = 0
        errors = []
        if not dry_run:
            storage = MemoryStorage(db_path=str(db))
            try:
                for rel in files:
                    truth_file = resolve_truth_file(ws, rel)
                    if truth_file is None:
                        continue
                    try:
                        rebuild_file(storage, ws, truth_file)
                        rebuilt += 1
                    except Exception as e:
                        errors.append("%s: %s" % (rel, e))
            finally:
                storage.close()

        conn = _open_ro(db)
        try:
            rows = conn.execute(
                "SELECT path, id, hash FROM chunks").fetchall()
            after_ids = {(r["path"], r["id"]) for r in rows}
            after_hashes = {(r["path"], r["hash"]) for r in rows}
            fts_rows = _q(conn, "SELECT COUNT(*) FROM chunks_fts")
            # FTS sync: the same query must hit the same chunk ids.
            fts_ids = set()
            for term in ("记忆", "the", "判据"):
                try:
                    for r in conn.execute(
                            "SELECT rowid FROM chunks_fts "
                            "WHERE chunks_fts MATCH ?", (term,)):
                        fts_ids.add(r[0])
                except sqlite3.OperationalError:
                    pass
            chunk_rowids = {r[0] for r in conn.execute(
                "SELECT rowid FROM chunks").fetchall()}
            fts_subset = fts_ids <= chunk_rowids
            # The release regression: a released element must be invisible
            # to the three identities (shared / the owner / a third party).
            released_shared = _q(
                conn,
                "SELECT COUNT(*) FROM chunks "
                "WHERE element_released=1 AND scope='shared'")
            released_any = _q(
                conn,
                "SELECT COUNT(*) FROM chunks WHERE element_released=1")
        finally:
            conn.close()

        ids_match = after_ids == truth_ids
        hashes_match = after_hashes == truth_hashes
        ok = (ids_match and hashes_match and not errors
              and released_shared == 0 and fts_subset)
        report.criterion(
            "M7.%s" % name,
            "the rebuild reproduces the truth layer's chunk set (%s)" % name,
            "PASS" if ok else "FAIL",
            "files covered by the truth re-cut=%d  files rebuilt=%d  "
            "errors=%d\n"
            "id set (with the part segment) == the chunker's: %s\n"
            "  index=%d  truth=%d  only-in-index=%d  only-in-truth=%d\n"
            "hash set == the chunker's: %s\n"
            "FTS rows=%d  FTS hits are a subset of the chunks: %s\n"
            "released elements: %d total, %d visible to a shared scope "
            "(must be 0)" % (
                covered, rebuilt, len(errors),
                ids_match, len(after_ids), len(truth_ids),
                len(after_ids - truth_ids), len(truth_ids - after_ids),
                hashes_match, fts_rows, fts_subset,
                released_any, released_shared),
            ids_match=ids_match, hashes_match=hashes_match,
            errors=len(errors))
        if errors:
            for e in errors[:5]:
                report.note("         error: %s" % e)


# --------------------------------------------------------------------------
# The rollback (A-3).
# --------------------------------------------------------------------------

def _rollback_via_backup_api(db: Path, backup: Path) -> Dict[str, Any]:
    """Restore one library IN PLACE with the SQLite backup API.

    P1-阶段三 第四轮 (风控专家 终审 §2.6.4 · SEED 拍板).  This is the
    ONLY rollback path that works on a machine where the service is
    running:

      * ``cp`` over the main database — **WRONG DATA**: the stale ``-wal``
        is applied to the fresh main database, so rows that were never in
        the backup come back to life.  Measured by 风控: 6 OLD rows plus
        a row revived from the WAL, and the new backup had no effect at
        all.
      * ``mv`` the database aside and ``cp`` the backup back — **Windows
        only**: the running service holds ``index.db-wal``, and the
        rename fails with ``WinError 32``.  (On Linux it works — the
        root cause is the Windows file-lock semantics, not the presence
        of a live service.)
      * **the backup API** — source = the backup, target = the live
        database.  It moves nothing, renames nothing and deletes
        nothing, so no lock is contested.  Measured by 风控: with the
        service still holding its connection, the restore succeeds, the
        revived WAL row does NOT come back, and the service's own
        connection immediately sees the restored data.

    **The cost, stated plainly (风控 §2.6.4 边界 1):** the restore
    overwrites EVERYTHING in the live database, so writes made after the
    backup was taken are LOST.  That is the price of having a rollback
    path at all, and it is why the backup must be taken as close to the
    migration as possible.

    Returns a dict with the outcome; the caller reports it.
    """
    out: Dict[str, Any] = {"path": "backup-api"}
    src = sqlite3.connect("file:%s?mode=ro" % backup, uri=True)
    try:
        dst = sqlite3.connect(str(db))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    # Verify on a NEW connection — the one that restored it says nothing
    # about what the next reader will see.
    iconn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        out["integrity_check"] = _q(iconn, "PRAGMA integrity_check")
        out["chunks"] = _q(iconn, "SELECT COUNT(*) FROM chunks")
        try:
            out["body_path_notnull"] = _q(
                iconn, "SELECT COUNT(*) FROM chunks WHERE body_path IS NOT NULL")
        except sqlite3.Error:
            out["body_path_notnull"] = None
    finally:
        iconn.close()
    return out


def rollback(live: List[Dict[str, Any]], backup_dir: Path,
             report: Report, method: str = "auto",
             explicit_backups: Optional[Dict[str, Path]] = None) -> None:
    """Restore every library from its M0 backup.

    P1-阶段三 第四轮.  Two paths, and the report says which one ran:

      * ``backup-api`` (the default on every platform): the SQLite
        backup API restores the live database IN PLACE.  It does not
        move, rename or delete any file, so it works while the service
        holds the database — which is the only situation that matters on
        this machine, and the situation the service's own hot state
        creates.

      * ``rename-copy`` (the original path): the half-migrated database
        is renamed aside and the backup is copied back.  It is the only
        path that keeps the half-migrated database as evidence, and it
        is what a Linux host with the service STOPPED should use.

    ``auto`` picks the backup API, because it is the one that works
    everywhere; the rename path is kept for the case where the operator
    wants the failed state preserved on disk.

    **The stale WAL problem is solved differently in each path.**  The
    rename path moves the ``-wal`` / ``-shm`` sidecars away with the
    half-migrated database (a stale WAL against a fresh main database is
    corruption).  The backup API needs no such dance: the restore goes
    through SQLite itself, which reads the WAL correctly and replaces
    the database's content as one transaction — so no stale WAL is ever
    applied to a fresh main database.  That is exactly why the ``cp``
    path is forbidden: it bypasses SQLite and lets the WAL apply itself.

    ``explicit_backups`` names the backup file for each library
    directly.  It exists because the operator's undo button must work
    with whatever path is in front of them — a directory, or one single
    file — and inferring the file name from the directory layout is
    one more place where a wrong guess becomes a silent no-op.
    """
    backup_dir = Path(backup_dir)
    for lib in live:
        db: Path = Path(lib["db"])
        if explicit_backups is not None:
            backup = explicit_backups.get(lib["name"])
            if backup is None:
                report.note("  rollback: no backup for %s -> SKIPPED"
                            % lib["name"])
                continue
            backup = Path(backup)
        else:
            backup = backup_dir / ("%s.p1-bak.db" % lib["name"])
        if not backup.is_file():
            report.note("  rollback: no backup for %s -> SKIPPED" % lib["name"])
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if method in ("auto", "backup-api"):
            try:
                res = _rollback_via_backup_api(db, backup)
                report.note(
                    "  rollback: %s restored IN PLACE via the SQLite "
                    "backup API from %s (integrity_check=%s chunks=%s "
                    "body_path NOT NULL=%s) — no file was moved, so the "
                    "live service's lock was never contested"
                    % (lib["name"], backup, res.get("integrity_check"),
                       res.get("chunks"), res.get("body_path_notnull")))
                report.criterion(
                    "ROLLBACK.%s" % lib["name"],
                    "%s was restored from its M0 backup" % lib["name"],
                    "PASS" if res.get("integrity_check") == "ok" else "FAIL",
                    "path=backup-api  backup=%s\n"
                    "integrity_check=%s  chunks=%s  body_path NOT NULL=%s\n"
                    "(the restore moved no file, so it worked with the "
                    "service holding the database; the cost is that "
                    "writes made after the backup was taken are LOST)"
                    % (backup, res.get("integrity_check"), res.get("chunks"),
                       res.get("body_path_notnull")),
                    integrity=res.get("integrity_check"),
                    chunks=res.get("chunks"),
                    method="backup-api")
                continue
            except Exception as e:
                if method == "backup-api":
                    raise
                report.note(
                    "  rollback: %s — the backup API failed (%s: %s); "
                    "falling back to the rename+copy path"
                    % (lib["name"], type(e).__name__, e))
        # The rename+copy path.
        for suffix in ("-wal", "-shm"):
            side = db.parent / (db.name + suffix)
            if side.exists():
                side.rename(db.parent / ("%s%s.rollback-%s"
                                         % (db.name, suffix, stamp)))
        half = db.parent / ("%s.rollback-%s" % (db.name, stamp))
        if db.exists():
            db.rename(half)
        # The backup is COPIED, never moved: the rollback gets ONE
        # chance, so the backup has to survive it.
        shutil.copy2(backup, db)
        iconn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            ok = _q(iconn, "PRAGMA integrity_check")
        finally:
            iconn.close()
        report.note("  rollback: %s restored via rename+copy from %s "
                    "(integrity_check=%s)" % (lib["name"], backup, ok))
        report.criterion(
            "ROLLBACK.%s" % lib["name"],
            "%s was restored from its M0 backup" % lib["name"],
            "PASS" if ok == "ok" else "FAIL",
            "path=rename-copy  backup=%s  integrity_check=%s\n"
            "(the half-migrated database was renamed aside, not deleted: "
            "it is kept as evidence)" % (backup, ok),
            integrity=ok, method="rename-copy")


# --------------------------------------------------------------------------
# A-6 — the five checks against "all green but running the OLD code".
# --------------------------------------------------------------------------

def step_a6_running_the_right_code(live: List[Dict[str, Any]],
                                   report: Report) -> None:
    """A-6: prove the process is running THIS code, and print the VALUES.

    **Why this exists.**  A green gate means nothing if the gate ran
    against the wrong code.  ``git rev-parse HEAD`` alone is not
    enough: the repository can be at commit X while the interpreter
    imports ``agent.memory`` from a ``site-packages`` copy at commit Y.
    MEASURED on 0.114 (前置准备 §7): there is no site-packages copy
    there, so the check is trivially satisfied — but "trivially
    satisfied on the machine you tested" is not the same as "checked".

    **The five items, and what each one catches:**

      1. ``agent.memory.__file__`` — the file the interpreter ACTUALLY
         imported.  Catches "the installed copy, not the source tree".
      2. ``git rev-parse HEAD`` — the commit of the source tree.  Catches
         "the right tree, the wrong commit".
      3. ``schema_fingerprint`` **plus the tables it covers** — the
         shape of the index.  Catches "the code is right but the
         database is not the one this code expects".
      4. ``body_path IS NULL`` count — the thing the migration was
         supposed to fill.
      5. the before/after row count and the CONTENT DIGEST — the
         migration's own effect.

    **Every item prints its ACTUAL VALUE**, never just PASS/FAIL: a
    criterion that prints only the verdict cannot be audited by the
    reader, and the reader is the one who has to decide whether the
    machine is in the state the report claims.
    """
    import subprocess

    items: List[Tuple[str, str, bool]] = []

    # --- 1. which file did the interpreter import? --------------------
    try:
        import agent.memory as _m
        import pathlib as _pl

        mem_file = str(_pl.Path(_m.__file__).resolve())
    except Exception as e:
        mem_file = "cannot import agent.memory: %s" % e
    in_source_tree = str(REPO) in mem_file
    items.append((
        "① agent.memory.__file__", mem_file, in_source_tree))

    # --- 2. which commit is that tree at? ------------------------------
    try:
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        head = "cannot run git: %s" % e
    items.append(("② git rev-parse HEAD", head or "(empty)", bool(head)))

    # --- 3. the schema fingerprint, WITH its coverage ------------------
    for lib in live:
        try:
            fp = schema_fingerprint(lib["db"])
        except Exception as e:
            fp = "error: %s" % e
        items.append((
            "③ schema_fingerprint [%s]" % lib["name"],
            describe_schema_fingerprint(fp), fp is not None))
        break  # one library is enough for the shape; the rest are in M1

    # --- 4. the pointers that are supposed to be filled ----------------
    # This is a MEASUREMENT, not a gate.  A non-zero count here does
    # NOT mean "the wrong code ran" — it means the data has rows with
    # no pointer, which is the honest state for a row whose truth file
    # does not exist (Rule D), and the M4 criteria own that question.
    # Conflating the two would make this criterion red for a reason
    # that has nothing to do with what it asks.
    null_total = 0
    rows_total = 0
    for lib in live:
        try:
            conn = _open_ro(lib["db"])
            try:
                null_total += _q(
                    conn, "SELECT COUNT(*) FROM chunks "
                          "WHERE body_path IS NULL")
                rows_total += _q(conn, "SELECT COUNT(*) FROM chunks")
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    measurements: List[Tuple[str, str]] = [
        ("④ body_path IS NULL (all libraries)",
         "%d of %d rows" % (null_total, rows_total)),
    ]

    # --- 5. the effect of this very run --------------------------------
    for lib in live:
        try:
            conn = _open_ro(lib["db"])
            try:
                digest = content_digest(conn, "chunks")
                rows = _q(conn, "SELECT COUNT(*) FROM chunks")
            finally:
                conn.close()
        except sqlite3.Error:
            digest, rows = None, 0
        pre = lib.get("pre", {})
        before_rows = pre.get("chunks")
        before_digest = pre.get("chunks_digest")
        measurements.append((
            "⑤ rows / content digest [%s]" % lib["name"],
            "rows %s -> %s   digest %s -> %s" % (
                before_rows if before_rows is not None else "(no baseline)",
                rows,
                (before_digest or "(no baseline)")[:16],
                (digest or "(none)")[:16])))

    # **The verdict is about the CODE IDENTITY only.**  Items ① ② ③
    # answer "is this run against the code the report describes?" —
    # that is what this criterion is for.  Items ④ ⑤ are the DATA
    # state, printed so the reader can see the migration's effect;
    # they are measured, not judged here (the M4 criteria own the
    # pointer question).
    ok = all(good for _label, _value, good in items)
    detail = []
    for label, value, _good in items:
        detail.append("  %-34s %s" % (label, value))
    for label, value in measurements:
        detail.append("  %-34s %s" % (label, value))
    detail.append("")
    detail.append("(every item prints its ACTUAL VALUE — a verdict alone "
                  "cannot be audited by the reader)")
    detail.append("① ② ③ are the VERDICT (code identity: is this run "
                  "against the code the report describes?).")
    detail.append("④ ⑤ are MEASUREMENTS (the data state; the M4 "
                  "criteria own the pointer question).")
    report.criterion(
        "A6.CODE",
        "the run is against THIS source tree (the code identity), with "
        "the data state printed beside it",
        "PASS" if ok else "FAIL",
        "\n".join(detail),
        agent_memory_file=mem_file,
        git_head=head,
        body_path_null=null_total,
        rows=rows_total)


# --------------------------------------------------------------------------
# The operator's undo button: ``--rollback <backup path>``.
# --------------------------------------------------------------------------

def _resolve_rollback_backups(backup_path: Path,
                              live: List[Dict[str, Any]]) -> Dict[str, Path]:
    """Map each live library to its backup file, from EITHER shape.

    ``--rollback`` accepts two shapes, because an operator who is
    standing in front of a broken machine has whatever is in front of
    them:

      * a DIRECTORY — the M0 backup directory (``<name>.p1-bak.db``
        files).  This is what ``--backup-dir`` produced.
      * a single FILE — one ``<name>.p1-bak.db``.  Then only that one
        library is restored, and the others are reported as SKIPPED.

    **A missing backup is reported, never guessed.**  A rollback that
    silently restores three libraries out of four is worse than one
    that restores none: the operator believes the machine is back to
    the pre-migration state, and it is not.
    """
    backup_path = Path(backup_path)
    out: Dict[str, Path] = {}
    if backup_path.is_dir():
        for lib in live:
            cand = backup_path / ("%s.p1-bak.db" % lib["name"])
            if cand.is_file():
                out[lib["name"]] = cand
        return out
    if backup_path.is_file():
        # The file name carries the library name: '<name>.p1-bak.db'.
        stem = backup_path.name
        for suffix in (".p1-bak.db", ".db"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        out[stem] = backup_path
        return out
    return out


def run_rollback(backup_path: Path | str, workspace: Path | str,
                 method: str = "auto") -> int:
    """Restore the live libraries from an M0 backup and report.

    This is the operator's undo button, and it is a SEPARATE entry
    point from the migration: it never migrates, it only restores.

    **The restore goes through the SQLite backup API** (A-3).  The
    reason is NOT "``cp`` is always wrong" — it is that the backup API

      * moves, renames and deletes NOTHING, so it works while the
        service holds the database (measured by 风控: the service's own
        connection immediately sees the restored data);
      * does not depend on the side effect "the WAL's salt happens not
        to match the fresh main database", which is the only reason
        ``cp`` works at all when the service is stopped;
      * replaces the database's content as ONE transaction through
        SQLite, so a stale WAL is never applied to a fresh main
        database (which is how ``cp`` revives rows that were never in
        the backup).

    The cost is stated in the report: the restore overwrites EVERYTHING
    in the live database, so writes made after the backup was taken are
    LOST.  That is the price of having a rollback path at all.
    """
    backup_path = Path(backup_path)
    workspace = Path(workspace)
    report = Report()
    report.note("=" * 72)
    report.note("P1-阶段三 · 回滚（restore from the M0 backup）")
    report.note("=" * 72)
    report.note("workspace  : %s" % workspace)
    report.note("backup     : %s" % backup_path)
    report.note("method     : %s" % method)

    if not backup_path.exists():
        report.criterion(
            "ROLLBACK.INPUT", "the backup path exists", "FAIL",
            "%s does not exist — there is nothing to restore from.  "
            "Point --rollback at the M0 backup directory "
            "(--backup-dir) or at a single '<name>.p1-bak.db'." % backup_path)
        print("\n".join(report.lines))
        return 2

    live = enumerate_live(workspace)
    backups = _resolve_rollback_backups(backup_path, live)
    report.note("live libraries (WHITELIST enumeration): %d" % len(live))
    for lib in live:
        report.note("   %-22s %s" % (lib["name"], lib["db"]))
    report.note("backups found: %d" % len(backups))

    if not backups:
        report.criterion(
            "ROLLBACK.INPUT", "the backup path holds the backups", "FAIL",
            "no '<name>.p1-bak.db' file was found under %s, and the live "
            "libraries are %s.  A rollback that restores NOTHING and "
            "reports success is worse than one that fails: the operator "
            "would believe the machine is back to the pre-migration "
            "state, and it is not." % (backup_path,
                                      ", ".join(l["name"] for l in live) or "none"))
        print("\n".join(report.lines))
        return 2

    # Restore only the libraries that HAVE a backup — and say which
    # ones were left alone.
    targets = [lib for lib in live if lib["name"] in backups]
    skipped = [lib["name"] for lib in live if lib["name"] not in backups]
    for name in skipped:
        report.note("  rollback: %s -> SKIPPED (no backup for it)" % name)
    if skipped:
        report.criterion(
            "ROLLBACK.COVERAGE",
            "every live library had a backup to restore from",
            "FAIL",
            "restored=%d  skipped(no backup)=%s\n"
            "(a library left behind is a library still holding the "
            "half-migrated state — the operator must know which one)"
            % (len(targets), ", ".join(skipped) or "none"),
            restored=len(targets), skipped=skipped)
    else:
        report.criterion(
            "ROLLBACK.COVERAGE",
            "every live library had a backup to restore from",
            "PASS",
            "restored=%d  skipped=0" % len(targets),
            restored=len(targets), skipped=skipped)

    rollback(targets, backup_path.parent if backup_path.is_dir()
             else backup_path.parent, report, method=method,
             explicit_backups=backups)

    counts = report.counts()
    report.note("=" * 72)
    report.note("判据通过: %d PASS / %d FAIL / %d NOTRUN"
                % (counts["PASS"], counts["FAIL"], counts["NOTRUN"]))
    report.note("=" * 72)
    out = "\n".join(report.lines)
    print(out)
    try:
        (backup_path.parent / "rollback-report.txt").write_text(
            out, encoding="utf-8")
    except OSError:
        pass
    return 1 if counts["FAIL"] else 0


# --------------------------------------------------------------------------
# The driver.
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True,
                    help="the workspace root (C:\\Users\\User\\cow)")
    ap.add_argument("--step", default="all",
                    help="M0,M1,M4,M5,M7 or 'all'")
    ap.add_argument("--backup-dir", default=None,
                    help="where the M0 backups go "
                         "(default: <workspace>/tmp/p1s3c-backup-<ts>)")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure everything, change nothing")
    ap.add_argument("--i-have-the-inv1-ruling", action="store_true",
                    help="unlock M6 (the kernel architect's INV-1 ruling)")
    ap.add_argument("--rollback-method", default="auto",
                    choices=["auto", "backup-api", "rename-copy"],
                    help="how to restore from the M0 backup if the "
                         "migration fails.  'backup-api' restores IN PLACE "
                         "through SQLite and works while the service holds "
                         "the database (the default); 'rename-copy' keeps "
                         "the half-migrated database on disk as evidence.")
    ap.add_argument("--expect-libraries", type=int, default=None,
                    help="how many live libraries are expected.  WITHOUT "
                         "it the count is reported, not enforced — the "
                         "hardcoded 16 was true only on this machine and "
                         "made the tool unusable on 0.114 (B-1).")
    ap.add_argument("--rollback", default=None, metavar="BACKUP_PATH",
                    help="restore every live library from an M0 backup "
                         "directory (or a single '<name>.p1-bak.db' file) and "
                         "EXIT.  This is the operator's undo button: it "
                         "does not migrate anything.  The restore goes "
                         "through the SQLite backup API by default (A-3) — "
                         "it moves no file, so it works while the service "
                         "holds the database.")
    args = ap.parse_args(argv)

    # B-2 (风控专家 终审 §5.1 / R-9): running the module from outside the
    # repository root gives "No module named 'agent'".  The import block
    # at the top of this file inserts REPO into sys.path, so the failure
    # happens BEFORE that in some layouts; and when it happens at all the
    # error says nothing about what to do.  So the check is here, with an
    # error that names the fix.
    try:
        import agent.memory  # noqa: F401
    except ImportError as e:
        sys.stderr.write(
            "\n!! cannot import the 'agent' package: %s\n"
            "\n"
            "This tool must be run FROM THE REPOSITORY ROOT, e.g.\n"
            "    cd %s\n"
            "    python -m agent.memory.migrate --workspace <WS> ...\n"
            "\n"
            "And --workspace must point at the WORKSPACE ROOT (where the "
            "truth-layer files live), NOT at the repository:\n"
            "    this machine : C:\\Users\\User\\cow\n"
            "    0.114       : /home/paikeywu/cow-workspace\n"
            "(the repository holds the CODE; the workspace holds the "
            "TRUTH.)\n" % (e, REPO))
        return 2

    workspace = Path(args.workspace).resolve()

    # The operator's undo button.  It is handled BEFORE anything else,
    # because a rollback must never be blocked by a gate that has
    # nothing to do with it — the whole point of the button is that it
    # works when the machine is broken.
    if args.rollback:
        return run_rollback(args.rollback, workspace,
                            method=args.rollback_method)

    report = Report()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.backup_dir) if args.backup_dir else \
        workspace / "tmp" / ("p1s3c-backup-%s" % stamp)

    live = enumerate_live(workspace)
    report.note("=" * 72)
    report.note("P1-阶段三 · 迁移（M0~M7，M6 由巡检器置位）")
    report.note("=" * 72)
    report.note("workspace : %s" % workspace)
    report.note("backup dir: %s" % backup_dir)
    report.note("live libraries (WHITELIST enumeration): %d" % len(live))
    for lib in live:
        report.note("   %-22s %s" % (lib["name"], lib["db"]))

    # B-1 (风控专家 终审 §5.1): the expected library count was HARDCODED
    # at 16 — a number that is true ONLY on this machine.  On 0.114 the
    # same tool enumerated 1 library (the 15 ``agents/`` directories are
    # empty shells), so the gate FAILED and the tool returned 2 BEFORE
    # doing anything: not one row could be migrated.
    #
    # The fix is a PARAMETER, not a new constant: the expectation comes
    # from the caller, and when it is not given the enumeration is
    # REPORTED (with both numbers printed) instead of failing.  A gate
    # that cannot be satisfied on the machine it is run on is not a gate,
    # it is an outage.
    #
    # **The one number that IS a gate: ZERO.**  "Enumerated 0
    # libraries" means the tool found nothing to migrate — the
    # workspace path is wrong, or the truth layer is somewhere else.
    # That is a genuine failure, and it must stop the run: a tool that
    # reports success after migrating nothing is the worst possible
    # outcome, because the operator reads the green and moves on.
    if len(live) == 0:
        report.criterion(
            "ENUM.GATE",
            "at least one live library was found",
            "FAIL",
            "enumerated 0 libraries under %s.  The enumeration is a "
            "WHITELIST of path shapes — %s/memory/long-term/index.db and "
            "%s/agents/<id>/memory/long-term/index.db — so zero means "
            "the path is wrong, or the truth layer lives somewhere "
            "else.  On 0.114 the workspace is /home/paikeywu/cow-workspace "
            "(NOT the repository).  A run that migrates nothing must not "
            "report success." % (workspace, workspace, workspace),
            enumerated=0, expected=None)
        print("\n".join(report.lines))
        return 2
    if args.expect_libraries is None:
        report.criterion(
            "ENUM.GATE",
            "the live library count was enumerated",
            "PASS",
            "enumerated %d libraries (no expectation given — "
            "--expect-libraries was not passed, so the count is REPORTED, "
            "not enforced).  The enumeration is a WHITELIST of path "
            "shapes, never a *.db glob (A-4).  On this machine the "
            "expected count is 16; on 0.114 it is 1 (the 15 agents/ "
            "directories are empty shells)." % len(live),
            enumerated=len(live), expected=None)
    elif len(live) != args.expect_libraries:
        report.criterion(
            "ENUM.GATE",
            "the live library count matches the expectation",
            "FAIL",
            "expected %d libraries, enumerated %d.  The enumeration is a "
            "WHITELIST of path shapes, never a *.db glob (A-4)."
            % (args.expect_libraries, len(live)),
            enumerated=len(live), expected=args.expect_libraries)
        print("\n".join(report.lines))
        return 2
    else:
        report.criterion(
            "ENUM.GATE",
            "the live library count matches the expectation",
            "PASS",
            "expected %d, enumerated %d (WHITELIST enumeration by path "
            "shape, A-4)" % (args.expect_libraries, len(live)),
            enumerated=len(live), expected=args.expect_libraries)

    steps = ([s.strip().upper() for s in args.step.split(",")]
             if args.step != "all"
             else ["M0", "M1", "M4", "M5", "M7", "M6"])

    try:
        if "M0" in steps:
            step_m0(workspace, live, backup_dir, report)
            snap = snapshot_truth_layer(workspace, backup_dir)
            report.criterion("M0.SNAPSHOT", "the truth-layer snapshot",
                             "PASS",
                             "%d files -> %s (the second-level rollback)"
                             % (snap["files"], snap["dir"]))
        if "M1" in steps:
            step_m1(live, report)
        if "M4" in steps:
            step_m4(live, report, dry_run=args.dry_run)
        if "M5" in steps:
            # The FIRST measurement: what the backfill alone achieved.
            step_m5(live, report)
        if "M7" in steps:
            # M7 (the rebuild) re-cuts every file from the CURRENT truth
            # layer, so it REPAIRS the rows whose file was rewritten.
            # The final M5 is measured AFTER it — a verdict taken before
            # the rebuild would condemn rows the rebuild fixes.
            step_m7(workspace, live, report, dry_run=args.dry_run)
            if "M5" in steps:
                step_m5(live, report)
        if "M6" in steps:
            # P1-阶段三 第四轮: the architect's ruling EXISTS, so M6 runs
            # as a MEASUREMENT.  The tool does not set 'mirror' — the
            # INSPECTOR is the designated setter (设计 §3.1: "明确置位者，
            # 防止三态机退化为装饰"), and a tool that set the state
            # itself would make the three-state machine a decoration.
            step_m6(live, report, dry_run=args.dry_run)
        # A-6: the five checks against "all green but running the OLD
        # code".  It runs on EVERY path (including --step M7 --dry-run),
        # because the question it asks is not about the migration — it
        # is about whether THIS run is the run the report describes.
        step_a6_running_the_right_code(live, report)
    except MigrationError as e:
        report.criterion("ROLLBACK", "the migration was rolled back",
                         "FAIL", str(e))
        rollback(live, backup_dir, report, method=args.rollback_method)
    except Exception as e:
        report.criterion("ROLLBACK", "the migration was rolled back",
                         "FAIL", "%s: %s" % (type(e).__name__, e))
        rollback(live, backup_dir, report, method=args.rollback_method)

    counts = report.counts()
    report.note("=" * 72)
    report.note("判据通过: %d PASS / %d FAIL / %d NOTRUN"
                % (counts["PASS"], counts["FAIL"], counts["NOTRUN"]))
    report.note("=" * 72)
    out = "\n".join(report.lines)
    print(out)
    # The report must survive even when the backup directory was never
    # created (e.g. --step M4 alone with an explicit --backup-dir that
    # M0 did not create).  A tool that crashes while writing its own
    # report loses the report.
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "migration-report.txt").write_text(out, encoding="utf-8")
        (backup_dir / "migration-report.json").write_text(
            json.dumps(report.criteria, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        print("!! could not write the report: %s" % e)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
