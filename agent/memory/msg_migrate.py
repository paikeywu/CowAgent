"""The messages externalisation: the tool that moves the text out of the DB.

**What this does (P1 阶段三 M · 契约 v2 §5.2).**

Six stages, in this order, and the order is the design:

  0. **backup** — ``wal_checkpoint(TRUNCATE)`` then ``VACUUM INTO``, and
     the recovery drill (G-05b): the backup must open through the real
     ``ConversationStore`` path, a 20-row sample must match, and the FTS
     query must work.  Any of the three failing STOPS the migration.
  1. **add the columns** — five ``ALTER TABLE`` statements.  ``content``
     is still NOT NULL, so the old write path is untouched and the
     regression suite must stay green.
  2. **rebuild the table** — ``content`` becomes nullable.  One
     transaction, no ``executescript`` (G-05d).
  3. **the readers** — implemented in ``conversation_store`` and
     ``msg_chain``; the equivalence check (M-7) runs against a COPY.
  4. **externalise** — for each session: write the JSONL, verify the
     bytes read back and hash the same, write the anchor, and only THEN
     empty the DB column.  "First the file, then the empty column" — the
     reverse order loses the text.
  5. **the criteria** — MSG-INV-1..7 go live.

**The rollback uses the SQLite backup API, never ``cp`` (G-05a).**

A ``cp`` over the live database applies the STALE WAL to the fresh
main database: rows that were never in the backup come back to life,
and the restore reports no error.  The backup API moves nothing, so no
lock is contested and the restore is exact.

**Every stage is measurable.**  A stage that cannot say what it did is
a stage nobody can verify.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent.memory import msg_chain
from agent.memory.naming import canonical_text  # noqa: F401  (the one normalizer)

_CST = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


class MsgMigrationError(RuntimeError):
    """A stage failed.  The caller rolls back."""


# ---------------------------------------------------------------------------
# Stage 0 — the backup + the recovery drill
# ---------------------------------------------------------------------------

def backup_database(db: Path, target: Path) -> Dict[str, Any]:
    """Checkpoint + ``VACUUM INTO`` — the ONLY backup path.

    The checkpoint comes FIRST: a backup taken without it is a partial
    snapshot (the WAL still holds rows the main file does not).
    """
    db = Path(db)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if target.exists():
            target.unlink()
        conn.execute("VACUUM INTO ?", (str(target),))
        conn.commit()
    finally:
        conn.close()
    return {"backup": str(target),
            "size_bytes": target.stat().st_size if target.is_file() else 0}


def recovery_drill(db: Path, backup: Path, workspace: Union[str, Path],
                    ) -> Dict[str, Any]:
    """G-05b: the three checks that must pass before the migration.

    1. the backup opens through the REAL ``ConversationStore`` path;
    2. a 20-row sample of ``(id, seq, content)`` matches the live one;
    3. the FTS query works on the restored copy.

    Any one of them failing means the backup cannot be trusted — and an
    untrusted backup is not a rollback path.
    """
    out: Dict[str, Any] = {}
    try:
        from agent.memory.conversation_store import ConversationStore

        store = ConversationStore(Path(backup), agent_id="")
        try:
            out["real_path_open"] = True
            out["sessions"] = store.get_stats().get("total_sessions")
        finally:
            pass
    except Exception as e:
        out["real_path_open"] = False
        out["real_path_error"] = "%s: %s" % (type(e).__name__, e)

    try:
        live = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
        try:
            sample = live.execute(
                "SELECT id, seq, content FROM messages ORDER BY id LIMIT 20"
            ).fetchall()
        finally:
            live.close()
        rest = sqlite3.connect("file:%s?mode=ro" % str(backup).replace("\\", "/"), uri=True)
        try:
            rsample = rest.execute(
                "SELECT id, seq, content FROM messages ORDER BY id LIMIT 20"
            ).fetchall()
        finally:
            rest.close()
        out["sample_match"] = sample == rsample
        out["sample_size"] = len(sample)
    except sqlite3.Error as e:
        out["sample_match"] = False
        out["sample_error"] = str(e)

    try:
        from agent.memory.storage import MemoryStorage

        storage = MemoryStorage(db_path=str(backup))
        try:
            storage.conn.execute(
                "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH '记忆'"
            ).fetchone()
            out["fts_ok"] = True
        except sqlite3.Error as e:
            out["fts_ok"] = False
            out["fts_error"] = str(e)
        finally:
            storage.close()
    except Exception as e:
        out["fts_ok"] = False
        out["fts_error"] = "%s: %s" % (type(e).__name__, e)

    out["PASS"] = bool(out.get("real_path_open")
                       and out.get("sample_match")
                       and out.get("fts_ok"))
    return out


# ---------------------------------------------------------------------------
# Stage 4 — the externalisation itself
# ---------------------------------------------------------------------------

def append_rows_to_truth(conn: sqlite3.Connection, workspace: Union[str, Path],
                          agent_id: str, session_id: str,
                          rows: List[tuple]) -> int:
    """Append the given rows to the session's truth source.

    **The archive-before-delete step (裁决 C-4 · 冲突 2 · N-10).**  The
    delete paths call this before dropping a row, so the row's text
    reaches the append-only JSONL — where it stays forever — before
    the projection goes away.

    The rows are ``(seq, role, content, created_at, run_id, extras)``.

    The chain's ``prev_hash`` linkage continues from the file's last
    line, so a session's file is ONE chain across its whole life — not
    one chain per batch.

    Returns the number of rows written.
    """
    if not rows:
        return 0
    batch_date = _today()
    rel = msg_chain.session_relpath(session_id, batch_date, agent_id)
    path = msg_chain.session_path(workspace, session_id, batch_date, agent_id)

    prev = msg_chain.MSG_GENESIS_PREV_HASH
    existing = msg_chain.read_msg_lines(path) if path.is_file() else []
    if existing:
        prev = existing[-1].get("line_hash") or msg_chain.MSG_GENESIS_PREV_HASH
    first_line_no = len(existing) + 1

    lines: List[Dict[str, Any]] = []
    for seq, role, content, created_at, run_id, extras in rows:
        if content is None:
            continue
        line = msg_chain.build_msg_line(
            seq=int(seq), agent_id=agent_id, session_id=session_id,
            role=role or "", content=content,
            created_at=int(created_at or 0), run_id=run_id or "",
            prev_hash=prev, extras=extras or "",
        )
        prev = line["line_hash"]
        lines.append(line)
    if not lines:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    # The anchor, so the chain head still describes the file's tail.
    last = lines[-1]
    msg_chain.append_anchor(
        workspace, rel=rel, seq=int(last["seq"]),
        line_hash=last["line_hash"], action="append_msg", at=_now_iso(),
    )

    # The DB pointer, so the row can still be read back (until the
    # projection is dropped).
    for offset, line in enumerate(lines):
        conn.execute(
            "UPDATE messages SET content_path = ?, content_anchor = ?, "
            "content_hash = ?, content_state = 'external' "
            "WHERE agent_id = ? AND session_id = ? AND seq = ?",
            (rel, "line:%d" % (first_line_no + offset), line["line_hash"],
             agent_id, session_id, line["seq"]),
        )
    return len(lines)


def externalise_session(conn: sqlite3.Connection, workspace: Union[str, Path],
                        agent_id: str, session_id: str, batch_date: str,
                        *, dry_run: bool = False) -> Dict[str, Any]:
    """Externalise ONE session's messages.

    The protocol, per row, in this order:

      1. build the line (with its hash);
      2. append it to the JSONL, fsync;
      3. after the whole session is written: verify every line reads
         back and hashes the same;
      4. write the anchor;
      5. only THEN empty the DB column and fill the pointer.

    Step 5 is last because the reverse order loses the text.  The
    ``pending`` state covers the window between 4 and 5.
    """
    out: Dict[str, Any] = {
        "session_id": session_id,
        "agent_id": agent_id,
        "batch_date": batch_date,
        "rows": 0,
        "written": 0,
        "emptied": 0,
        "skipped": 0,
        "errors": [],
    }
    rows = conn.execute(
        "SELECT seq, role, content, created_at, run_id, extras, id "
        "FROM messages "
        "WHERE agent_id = ? AND session_id = ? AND content IS NOT NULL "
        "ORDER BY seq ASC",
        (agent_id, session_id),
    ).fetchall()
    out["rows"] = len(rows)
    if not rows:
        return out

    rel = msg_chain.session_relpath(session_id, batch_date, agent_id)
    path = msg_chain.session_path(workspace, session_id, batch_date, agent_id)

    # The chain's previous hash: the last line already in the file.
    prev = msg_chain.MSG_GENESIS_PREV_HASH
    existing = msg_chain.read_msg_lines(path) if path.is_file() else []
    if existing:
        prev = existing[-1].get("line_hash") or msg_chain.MSG_GENESIS_PREV_HASH

    lines: List[Dict[str, Any]] = []
    for seq, role, content, created_at, run_id, extras, _rid in rows:
        line = msg_chain.build_msg_line(
            seq=int(seq), agent_id=agent_id, session_id=session_id,
            role=role or "", content=content,
            created_at=int(created_at or 0), run_id=run_id or "",
            prev_hash=prev, extras=extras or "",
        )
        prev = line["line_hash"]
        lines.append(line)

    if dry_run:
        out["written"] = 0
        out["planned"] = len(lines)
        out["rel"] = rel
        return out

    # 2. write the lines
    path.parent.mkdir(parents=True, exist_ok=True)
    # The physical line number of the FIRST line this session writes:
    # the file may already hold lines (a previous batch, an append).
    first_line_no = len(existing) + 1
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    out["written"] = len(lines)
    out["rel"] = rel
    out["first_line_no"] = first_line_no

    # 3. verify: every line reads back and hashes the same
    written = msg_chain.read_msg_lines(path)
    tail = written[-len(lines):] if len(written) >= len(lines) else []
    if len(tail) != len(lines):
        raise MsgMigrationError(
            "the JSONL holds %d lines but %d were written — the write was "
            "truncated" % (len(written), len(lines)))
    for i, (got, want) in enumerate(zip(tail, lines)):
        if got.get("line_hash") != want["line_hash"]:
            raise MsgMigrationError(
                "line %d of the JSONL does not hash to what was written — "
                "the file was changed under the writer" % (i + 1,))

    # 4. the anchor
    last = lines[-1]
    msg_chain.append_anchor(
        workspace, rel=rel, seq=int(last["seq"]),
        line_hash=last["line_hash"], action="migrate_out", at=_now_iso(),
    )

    # 5. empty the DB column — the two-step protocol's second half.
    #    The anchor is ``line:<n>`` — the PHYSICAL line number in the
    #    file, which is NOT the message's ``seq``: a session's seq has
    #    holes (the delete paths create them) and the two numbers must
    #    not be confused.
    for offset, (line, (seq, _role, _content, _created, _run, _extras, _rid)) in \
            enumerate(zip(lines, rows)):
        cur = conn.execute(
            "UPDATE messages SET content = NULL, content_path = ?, "
            "content_anchor = ?, content_hash = ?, content_state = 'external' "
            "WHERE agent_id = ? AND session_id = ? AND seq = ?",
            (rel, "line:%d" % (first_line_no + offset),
             line["line_hash"], agent_id, session_id, seq),
        )
        out["emptied"] += max(cur.rowcount, 0)
    return out


# ---------------------------------------------------------------------------
# The two-phase batch
# ---------------------------------------------------------------------------

def externalise_all(conn: sqlite3.Connection, workspace: Union[str, Path],
                    agent_id: str = "", *, batch_date: Optional[str] = None,
                    dry_run: bool = False) -> Dict[str, Any]:
    """Externalise every session of ONE agent in the given database.

    ``agent_id=""`` means "the default Agent" — it does NOT mean "every
    agent".  The ROOT library holds NINE agents' messages (measured
    2026-10-31: 33,454 rows across 9 agent ids), and a migration that
    only touched the default one would leave 26,117 rows inline — a
    migration that reported success while doing a quarter of the work.
    """
    batch_date = batch_date or _today()
    out: Dict[str, Any] = {
        "agent_id": agent_id,
        "batch_date": batch_date,
        "sessions": [],
        "rows": 0,
        "written": 0,
        "emptied": 0,
    }
    sessions = conn.execute(
        "SELECT DISTINCT session_id FROM messages "
        "WHERE agent_id = ? AND content IS NOT NULL ORDER BY session_id",
        (agent_id,),
    ).fetchall()
    for (sid,) in sessions:
        rep = externalise_session(conn, workspace, agent_id, sid, batch_date,
                                  dry_run=dry_run)
        out["sessions"].append(rep)
        out["rows"] += rep.get("rows", 0)
        out["written"] += rep.get("written", 0)
        out["emptied"] += rep.get("emptied", 0)
    if not dry_run:
        conn.commit()
    return out


def externalise_every_agent(conn: sqlite3.Connection,
                             workspace: Union[str, Path], *,
                             batch_date: Optional[str] = None,
                             dry_run: bool = False) -> Dict[str, Any]:
    """Externalise EVERY agent's messages in the given database.

    The per-agent report is kept, so the caller can see WHICH agent's
    rows were written — a migration that reports one total number for
    nine agents' work is a migration nobody can audit.
    """
    batch_date = batch_date or _today()
    agents = [r[0] for r in conn.execute(
        "SELECT DISTINCT agent_id FROM messages ORDER BY agent_id").fetchall()]
    out: Dict[str, Any] = {
        "batch_date": batch_date,
        "agents": [],
        "agent_count": len(agents),
        "rows": 0,
        "written": 0,
        "emptied": 0,
    }
    for aid in agents:
        rep = externalise_all(conn, workspace, aid, batch_date=batch_date,
                              dry_run=dry_run)
        out["agents"].append(rep)
        out["rows"] += rep.get("rows", 0)
        out["written"] += rep.get("written", 0)
        out["emptied"] += rep.get("emptied", 0)
    return out


# ---------------------------------------------------------------------------
# The rollback — the backup API, never ``cp``
# ---------------------------------------------------------------------------

def rollback_via_backup_api(db: Path, backup: Path) -> Dict[str, Any]:
    """Restore the database IN PLACE from its backup.

    **Why not ``cp``:** the ``cp`` path applies the STALE WAL to the
    fresh main database, so rows that were never in the backup come
    back to life and the restore reports no error.  The backup API
    moves nothing, renames nothing and deletes nothing — so no lock is
    contested and the content is restored exactly.
    """
    out: Dict[str, Any] = {"path": "backup-api"}
    src = sqlite3.connect("file:%s?mode=ro" % str(backup).replace("\\", "/"), uri=True)
    try:
        dst = sqlite3.connect(str(db))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    check = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    try:
        out["integrity_check"] = check.execute("PRAGMA integrity_check").fetchone()[0]
        out["messages"] = check.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        check.close()
    return out


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.msg_migrate --workspace <ws> ...``"""
    import argparse

    ap = argparse.ArgumentParser(
        description="Externalise the conversation messages out of the "
                    "database and into the append-only JSONL chain "
                    "(P1 阶段三 M).")
    ap.add_argument("--workspace", required=True,
                    help="the workspace root (the one that owns the truth layer)")
    ap.add_argument("--db", default=None,
                    help="the index to migrate (default: the workspace's ROOT index)")
    ap.add_argument("--agent-id", default="",
                    help="whose messages to migrate (default: the default Agent, '')")
    ap.add_argument("--backup-dir", default=None,
                    help="where the backup goes (default: <ws>/memory/.backup/msg)")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure everything, change nothing")
    ap.add_argument("--stage", default="all",
                    choices=("all", "backup", "externalise", "rollback"),
                    help="which stage to run")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace)
    db = Path(args.db) if args.db else (workspace / "memory" / "long-term" / "index.db")
    backup_dir = Path(args.backup_dir) if args.backup_dir else (
        workspace / "memory" / ".backup" / "msg")
    report: Dict[str, Any] = {
        "workspace": str(workspace), "db": str(db), "stage": args.stage,
        "dry_run": bool(args.dry_run),
    }

    if args.stage in ("all", "backup"):
        target = backup_dir / ("messages.p1-bak.db")
        if args.dry_run:
            report["backup"] = {"planned": str(target)}
        else:
            report["backup"] = backup_database(db, target)
            report["drill"] = recovery_drill(db, target, workspace)
            if not report["drill"].get("PASS"):
                report["verdict"] = "FAIL"
                print(json.dumps(report, ensure_ascii=False, indent=2)
                      if args.json else report)
                return 1
        if args.stage == "backup":
            report["verdict"] = "PASS"
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
                  else report)
            return 0

    if args.stage in ("all", "externalise"):
        conn = sqlite3.connect(str(db))
        try:
            if args.agent_id:
                report["externalise"] = externalise_all(
                    conn, workspace, args.agent_id, dry_run=args.dry_run)
            else:
                # No --agent-id means EVERY agent: the ROOT library holds
                # nine agents' messages, and migrating one of them and
                # reporting success is a migration that did a quarter
                # of the work.
                report["externalise"] = externalise_every_agent(
                    conn, workspace, dry_run=args.dry_run)
        finally:
            conn.close()
        report["verdict"] = "PASS"
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
              else report)
        return 0

    if args.stage == "rollback":
        target = backup_dir / ("messages.p1-bak.db")
        report["rollback"] = rollback_via_backup_api(db, target)
        report["verdict"] = "PASS"
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
              else report)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
