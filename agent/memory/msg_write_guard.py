# -*- coding: utf-8 -*-
"""The ``messages`` write boundary: a RECORDER, not a gate.

**What this is.**  The old desktop build writes ``messages`` rows with a
seven-column ``INSERT`` that does not name the externalisation columns
at all, so the row keeps ``content_state='inline'`` and the three
pointer columns empty — **forever**.  The new build writes the same
shape (transient ``inline``) and then externalises it within the same
request.  So on the FINAL row the two writers are **indistinguishable**
(measured 2026-11-05: all eleven columns identical).

⇒ A gate that looks at the row ("refuse inline") **breaks the product**:
it refuses the new writer's transient row.  Measured: candidate A
aborted the new writer's insert.

⇒ So the boundary is a **RECORDER + a patrol**, not a gate:

  * the trigger does not refuse anything — it **writes down what it
    saw** (a witness row), and
  * the patrol asks the only question that separates the two writers:
    **is the row still inline?**  The new writer externalises its row
    within the request; the old writer's row stays inline forever.  So
    "the recorder saw it AND it is still inline" names the old
    writer's rows — and names ONLY them.

**Why a witness table and not a column on ``messages``.**  A column on
``messages`` would change the row's shape, and the whole point of the
measurement was that the two writers' rows are identical — a column
that only the new writer fills would be a second spelling of "who
wrote this", and the old writer's twelve-column INSERT would not fill
it, so the row would finally carry its own answer… **but only for
rows written from now on, and the trigger would have to write it,
which is the same as a witness table with more moving parts.**  The
witness table keeps ``messages``'s shape untouched (so a foreign
writer's row is byte-identical to before) and keeps the evidence
**outside** the thing being measured.

**What it is NOT.**  It does not refuse, it does not repair, and it
does not raise into the write.  A trigger that raises would take the
conversation down for a policy violation; the boundary here is
*observability first*, because the old writer is still running and
refusing its writes would be a fight we have not won yet.

**The patrol's own criterion** lives in :mod:`agent.memory.msg_inv` as
``MSG-INV-8`` (the eighth message-domain criterion), so it is run by
the patrol like the other seven and its verdict is in the report.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Union

#: The witness table.  ``row_id`` is the ``messages.id`` the trigger saw.
#: ``state_at_insert`` is what the row said about itself at insert time
#: (``'inline'`` for both writers — that is the point).
_WATCH_DDL = """
CREATE TABLE IF NOT EXISTS messages_write_watch (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id         INTEGER NOT NULL,
    agent_id       TEXT    NOT NULL DEFAULT '',
    session_id     TEXT    NOT NULL DEFAULT '',
    seq            INTEGER NOT NULL,
    state_at_insert TEXT   NOT NULL DEFAULT '',
    seen_at        INTEGER NOT NULL
);
"""

#: The index the patrol needs: "the rows the recorder saw that are
#: still inline".
_WATCH_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_write_watch_row
    ON messages_write_watch (row_id);
"""

#: The trigger.  AFTER INSERT, so it can never refuse the write (a
#: BEFORE trigger that raises would break the new writer — measured).
#: It only records.
_WATCH_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS messages_write_watch_ai
AFTER INSERT ON messages
BEGIN
    INSERT INTO messages_write_watch
        (row_id, agent_id, session_id, seq, state_at_insert, seen_at)
    VALUES (NEW.id, NEW.agent_id, NEW.session_id, NEW.seq,
            NEW.content_state, strftime('%s','now'));
END;
"""


def install(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Install the recorder on ONE connection (idempotent).

    Returns a report — the installation never raises into the caller:
    a conversation store whose recorder is unavailable still serves
    conversations; it just cannot say who wrote what.
    """
    out: Dict[str, Any] = {"installed": False, "errors": []}
    try:
        conn.executescript(_WATCH_DDL)
        conn.executescript(_WATCH_INDEX_DDL)
        conn.executescript(_WATCH_TRIGGER_DDL)
        conn.commit()
        out["installed"] = True
    except sqlite3.Error as e:
        out["errors"].append("%s: %s" % (type(e).__name__, e))
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    return out


def is_installed(conn: sqlite3.Connection) -> bool:
    """Is the recorder in place on this connection?"""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name='messages_write_watch_ai'"
        ).fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False


def legacy_inline_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The rows the recorder saw that are **still inline**.

    These are the old writer's rows: the new writer externalises its
    row within the request, so a row the recorder saw and that is
    still inline is a row nobody took responsibility for.

    The join is on ``messages.id``, so a row deleted in the meantime
    does not appear (the evidence dies with the row).
    """
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT w.row_id, w.agent_id, w.session_id, w.seq, "
            "       w.state_at_insert, w.seen_at, m.content_state, "
            "       m.content_path, m.content_anchor, m.content_hash, "
            "       m.created_at "
            "FROM messages_write_watch w "
            "LEFT JOIN messages m ON m.id = w.row_id "
            "WHERE m.id IS NOT NULL AND m.content_state = 'inline' "
            "ORDER BY w.agent_id, w.session_id, w.seq"
        ).fetchall()
    except sqlite3.Error as e:
        return [{"error": "cannot read the watch table: %s" % e}]
    for r in rows:
        out.append({
            "row_id": r[0],
            "agent_id": r[1],
            "session_id": r[2],
            "seq": r[3],
            "state_at_insert": r[4],
            "seen_at": r[5],
            "content_state": r[6],
            "content_path": r[7],
            "content_anchor": r[8],
            "content_hash": r[9],
            "created_at": r[10],
        })
    return out


def patrol(conn: sqlite3.Connection,
           *,
           older_than: int = 0,
           now: Optional[int] = None) -> Dict[str, Any]:
    """The patrol: name the rows the old writer left behind.

    ``older_than`` (seconds) keeps the question honest: a row the
    recorder saw a **millisecond** ago and that is still inline may
    simply be a new write in flight (the new writer externalises it
    a moment later).  So the default asks for rows that have been
    inline for a while — but the threshold is a PARAMETER, not a
    constant, because "how long is too long" is a deployment question,
    not a code question.

    Returns the report the criterion reads.
    """
    import time
    if now is None:
        now = int(time.time())
    out: Dict[str, Any] = {
        "recorder_installed": is_installed(conn),
        "watch_rows": 0,
        "legacy_inline_rows": 0,
        "older_than": older_than,
        "rows": [],
        "sessions": [],
        # The rows that were ALREADY inline when the recorder was
        # installed.  The recorder is PROSPECTIVE — it sees inserts that
        # happen from now on — so the backlog that predates it is
        # invisible to it.  That backlog is REPORTED (it is a fact about
        # the library), never part of the verdict: the age rule that
        # would catch it misfires (measured 2026-11-05: it picked 1,236
        # rows across 7 sessions, all of them written by the new
        # writer's own transient inline state).
        "pre_recorder_inline_rows": 0,
    }
    if not out["recorder_installed"]:
        out["error"] = ("the recorder is not installed on this library — "
                        "there is no evidence to judge")
        return out
    try:
        out["watch_rows"] = conn.execute(
            "SELECT COUNT(*) FROM messages_write_watch").fetchone()[0]
    except sqlite3.Error as e:
        out["error"] = "cannot read the watch table: %s" % e
        return out
    try:
        out["pre_recorder_inline_rows"] = conn.execute(
            "SELECT COUNT(*) FROM messages m WHERE m.content_state='inline' "
            "AND m.content_path='' AND m.content_anchor='' "
            "AND m.content_hash='' "
            "AND NOT EXISTS (SELECT 1 FROM messages_write_watch w "
            "WHERE w.row_id = m.id)").fetchone()[0]
    except sqlite3.Error:
        out["pre_recorder_inline_rows"] = None
    rows = legacy_inline_rows(conn)
    if older_than > 0:
        cutoff = now - older_than
        rows = [r for r in rows
                if isinstance(r.get("created_at"), int)
                and r["created_at"] <= cutoff]
    out["legacy_inline_rows"] = len(rows)
    out["rows"] = rows
    sess = sorted({(r.get("agent_id") or "", r.get("session_id") or "")
                   for r in rows})
    out["sessions"] = [{"agent_id": a, "session_id": s} for a, s in sess]
    return out


def format_report(report: Dict[str, Any], limit: int = 20) -> str:
    lines = ["messages write boundary: recorder=%s watch_rows=%s "
             "legacy_inline_rows=%s (older_than=%ss)"
             % (report.get("recorder_installed"),
                report.get("watch_rows"),
                report.get("legacy_inline_rows"),
                report.get("older_than"))]
    if report.get("error"):
        lines.append("  %s" % report["error"])
    for r in report.get("rows", [])[:limit]:
        lines.append("  agent=%s session=%s seq=%s seen_at=%s "
                     "state_at_insert=%s still=%s"
                     % (r.get("agent_id"), r.get("session_id"), r.get("seq"),
                        r.get("seen_at"), r.get("state_at_insert"),
                        r.get("content_state")))
    more = len(report.get("rows", [])) - limit
    if more > 0:
        lines.append("  … and %d more" % more)
    return "\n".join(lines)
