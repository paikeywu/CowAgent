"""
Conversation history persistence using SQLite.

Design:
- sessions table: per-session metadata (channel_type, last_active, msg_count)
- messages table: individual messages stored as JSON, append-only
- Pruning: age-based only (sessions not updated within N days are deleted)
- Thread-safe via a single in-process lock

Storage path: <agent workspace>/memory/long-term/index.db (shared with the
memory index)
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger

from agent.memory import msg_chain


# Runs this process opened and has not finished yet. A run stored as
# "running" but missing here was cut off by a stop or a crash.
_live_runs: set = set()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Core conversation schema. Sessions and messages are the irreplaceable part
# of this file, so their creation must always succeed; nothing optional belongs
# in this script.
#
# ``agent_id`` scopes every row to the Agent that owns the conversation. It is
# the mechanism that lets one global file hold every Agent's transcripts: the
# primary key / unique constraint are ``(agent_id, session_id[, seq])`` because
# two Agents legitimately share a ``session_id`` (the same IM user talking to
# both). Empty string means "the default Agent" -- the value backfilled onto
# every pre-global row, so a single-Agent install keeps working unchanged.
_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    agent_id          TEXT    NOT NULL DEFAULT '',
    session_id        TEXT    NOT NULL,
    channel_type      TEXT    NOT NULL DEFAULT '',
    title             TEXT    NOT NULL DEFAULT '',
    context_start_seq INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    last_active       INTEGER NOT NULL,
    msg_count         INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0,
    context_usage     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (agent_id, session_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT    NOT NULL DEFAULT '',
    session_id   TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT,
    content_path TEXT    NOT NULL DEFAULT '',
    content_anchor TEXT  NOT NULL DEFAULT '',
    content_hash TEXT    NOT NULL DEFAULT '',
    content_state TEXT   NOT NULL DEFAULT 'inline',
    extras_compensated INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    extras       TEXT    NOT NULL DEFAULT '',
    run_id       TEXT    NOT NULL DEFAULT '',
    UNIQUE (agent_id, session_id, seq)
);
"""

# Indexes live apart from table creation because on a pre-global database the
# tables already exist without ``agent_id``; the column is added by ``_migrate``
# first, and only then can these indexes reference it. Creating them here would
# abort ``_DDL`` on the very upgrade path we must support.
_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages (agent_id, session_id, seq);

CREATE INDEX IF NOT EXISTS idx_sessions_last_active
    ON sessions (agent_id, last_active);
"""

# Runs are an auxiliary table in the same file. Kept out of the core script so
# that any problem here -- a legacy table of the same name, a partial upgrade --
# degrades run tracking rather than taking conversation history down with it.
#
# A run is one addressable, persisted unit of an agent's work: the thing a
# delegated task, a subagent spawn or a scheduled job can be looked up by,
# resumed from and reported on after the call that started it has returned.
# Distinct from a session (who the conversation is with) and an agent (who is
# doing the work).
_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT    PRIMARY KEY,
    agent_id     TEXT    NOT NULL DEFAULT '',
    -- Reserved for the tenancy dimension; empty until per-user isolation lands,
    -- so filtering by owner is an additive query change rather than a schema one.
    user_id      TEXT    NOT NULL DEFAULT '',
    session_id   TEXT    NOT NULL DEFAULT '',
    -- The run that spawned this one (a delegation or subagent). Empty for a
    -- top-level run. Lets a whole delegation tree be walked from any node.
    parent_run_id TEXT   NOT NULL DEFAULT '',
    -- Free-form external work handle and where it came from. task_source is
    -- empty for a native CowAgent run; a non-empty value names the external
    -- system and task_id then addresses a work item within it. TEXT on purpose:
    -- it must hold external ids, never a foreign key into a table we own.
    task_id      TEXT    NOT NULL DEFAULT '',
    task_source  TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'running',
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    error        TEXT    NOT NULL DEFAULT '',
    extras       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_session
    ON runs (session_id, started_at);

CREATE INDEX IF NOT EXISTS idx_runs_parent
    ON runs (parent_run_id);

CREATE INDEX IF NOT EXISTS idx_runs_task
    ON runs (task_source, task_id);
"""

# Artifacts are auxiliary too: an index of the user-facing files conversations
# produced, so they can be browsed in one place instead of session by session.
# One logical row per (agent_id, path), kept unique by record_artifacts inside
# its write transaction rather than by a constraint. Columns carry defaults
# but no NOT NULL / UNIQUE either: SQLite can only lift those by rebuilding
# the table, while adding a column or an index is cheap. Readers coalesce.
_ARTIFACTS_DDL = """
CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT    DEFAULT '',
    session_id  TEXT    DEFAULT '',
    -- seq of the visible user message that opened the turn which last
    -- produced the file: what the history view scrolls to.
    turn_seq    INTEGER,
    path        TEXT    DEFAULT '',
    kind        TEXT    DEFAULT 'file',
    size        INTEGER DEFAULT 0,
    -- What produced it (write / edit / bash / send / subagent / embed / manual).
    source      TEXT    DEFAULT '',
    created_at  INTEGER DEFAULT 0,
    updated_at  INTEGER DEFAULT 0,
    -- JSON object for metadata that doesn't warrant a column of its own.
    extras      TEXT    DEFAULT '',
    -- When the user pinned it to the top of the timeline; 0 = not pinned.
    pinned_at   INTEGER DEFAULT 0,
    -- Name the user gave it in the view; '' = the file name. The file on disk
    -- keeps its name, since conversations and other files refer to the path.
    title       TEXT    DEFAULT ''
);
"""

# Columns added after the table first shipped. Append new columns here too:
# CREATE TABLE IF NOT EXISTS never adds them to an existing table.
_ARTIFACTS_ADDED_COLUMNS = (
    ("extras", "TEXT DEFAULT ''"),
    ("pinned_at", "INTEGER DEFAULT 0"),
    ("title", "TEXT DEFAULT ''"),
)

_ARTIFACTS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_artifacts_recent
    ON artifacts (agent_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_artifacts_updated
    ON artifacts (updated_at);

CREATE INDEX IF NOT EXISTS idx_artifacts_path
    ON artifacts (agent_id, path);
"""

# Migration: add channel_type column to existing databases that predate it.
_MIGRATION_ADD_CHANNEL_TYPE = """
ALTER TABLE sessions ADD COLUMN channel_type TEXT NOT NULL DEFAULT '';
"""

_MIGRATION_ADD_TITLE = """
ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT '';
"""

_MIGRATION_ADD_CONTEXT_START_SEQ = """
ALTER TABLE sessions ADD COLUMN context_start_seq INTEGER NOT NULL DEFAULT 0;
"""

# User-pinned conversations, kept at the top of the session list.
_MIGRATION_ADD_PINNED = """
ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
"""

# Context-usage snapshot from the session's last run, shown when no live Agent exists.
_MIGRATION_ADD_CONTEXT_USAGE = """
ALTER TABLE sessions ADD COLUMN context_usage TEXT NOT NULL DEFAULT '';
"""

# Generic JSON sidecar for per-message attachments (TTS audio URL, future use).
# Always optional — readers must tolerate missing column / empty / invalid JSON.
_MIGRATION_ADD_MSG_EXTRAS = """
ALTER TABLE messages ADD COLUMN extras TEXT NOT NULL DEFAULT '';
"""

# Attribute each message to the run that produced it, so a run's trace can be
# reconstructed and a delegated/subagent turn is distinguishable from its
# parent's. Empty for messages written before runs were tracked.
_MIGRATION_ADD_MSG_RUN_ID = """
ALTER TABLE messages ADD COLUMN run_id TEXT NOT NULL DEFAULT '';
"""

# First-level global migration: scope existing rows to their Agent. Adding the
# column is an O(1) metadata change; the backfill is a single-column UPDATE.
# Empty string is left in place and resolved to the default Agent at read time,
# so a legacy single-Agent database needs no backfill at all.
_MIGRATION_ADD_SESSION_AGENT_ID = """
ALTER TABLE sessions ADD COLUMN agent_id TEXT NOT NULL DEFAULT '';
"""

_MIGRATION_ADD_MSG_AGENT_ID = """
ALTER TABLE messages ADD COLUMN agent_id TEXT NOT NULL DEFAULT '';
"""

# ---------------------------------------------------------------------------
# P1 阶段三 M · messages 外置 (契约 v2 C-1..C-12)
#
# The message text moves OUT of the database and into an append-only
# JSONL chain (``memory/messages_export/<date>/<session>.jsonl``).  The
# database keeps a PROJECTION: the pointer to the text, a drift token,
# and the state.
#
# Five columns, added in TWO stages:
#
#   * stage 1 (below) — the four pointer columns + ``extras_compensated``.
#     ``content`` is still NOT NULL, so the old write path is untouched
#     and the regression suite must stay green.
#   * stage 2 — the TABLE REBUILD that makes ``content`` nullable.
#     SQLite cannot lift a NOT NULL with ALTER TABLE, so the table is
#     rebuilt (create-new / copy / drop / rename) — the same shape
#     ``_ensure_composite_key`` already uses.
#
# The two stages are separate so each is independently revertible.
# ---------------------------------------------------------------------------

_MIGRATION_ADD_MSG_CONTENT_PATH = """
ALTER TABLE messages ADD COLUMN content_path TEXT NOT NULL DEFAULT '';
"""

_MIGRATION_ADD_MSG_CONTENT_ANCHOR = """
ALTER TABLE messages ADD COLUMN content_anchor TEXT NOT NULL DEFAULT '';
"""

_MIGRATION_ADD_MSG_CONTENT_HASH = """
ALTER TABLE messages ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
"""

_MIGRATION_ADD_MSG_CONTENT_STATE = """
ALTER TABLE messages ADD COLUMN content_state TEXT NOT NULL DEFAULT 'inline';
"""

_MIGRATION_ADD_MSG_EXTRAS_COMPENSATED = """
ALTER TABLE messages ADD COLUMN extras_compensated INTEGER NOT NULL DEFAULT 0;
"""

#: The five new columns, in the order stage 1 adds them.
_MSG_EXTERNAL_COLUMNS = (
    ("content_path", "TEXT NOT NULL DEFAULT ''"),
    ("content_anchor", "TEXT NOT NULL DEFAULT ''"),
    ("content_hash", "TEXT NOT NULL DEFAULT ''"),
    ("content_state", "TEXT NOT NULL DEFAULT 'inline'"),
    ("extras_compensated", "INTEGER NOT NULL DEFAULT 0"),
)

# Bookkeeping for the one-time, idempotent global migration. Lives in the
# global file itself so "have we already absorbed source X?" survives restarts
# without a side-car file. Mirrors the scheduler's ``_migrate_legacy_task_stores``
# marker discipline.
_META_DDL = """
CREATE TABLE IF NOT EXISTS _migration_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL DEFAULT '',
    done_at INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_MAX_AGE_DAYS: int = 30


def _msg_copy_exprs(conn: sqlite3.Connection) -> str:
    """The five externalisation columns' copy expressions.

    A database that predates the columns must still be rebuildable: the
    copy fills the missing ones with their DEFAULTS (an empty pointer
    and the state ``inline``), which is the truth — a row that was
    never externalised IS inline.

    The expressions are read from the SOURCE table's own column set, so
    one rebuild handles both shapes.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    defaults = {
        "content_path": "''",
        "content_anchor": "''",
        "content_hash": "''",
        "content_state": "'inline'",
        "extras_compensated": "0",
    }
    return ", ".join(
        (col if col in cols else defaults[col])
        for col in ("content_path", "content_anchor", "content_hash",
                    "content_state", "extras_compensated")
    )


def _is_visible_user_message(content: Any) -> bool:
    """
    Return True when a user-role message represents actual user input
    (not an internal tool_result injected by the agent loop).
    """
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text"
            for b in content
        )
    return False


def _extract_display_text(content: Any) -> str:
    """
    Extract the human-readable text portion from a message content value.
    Returns an empty string for tool_use / tool_result blocks.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _first_line_preview(text: str, max_chars: int = 60) -> str:
    """Compact single-line preview of a user message, for the nav timeline.

    Collapses whitespace to keep the tooltip on one line and truncates to
    ``max_chars`` with an ellipsis. Trailing ``[label: path]`` attachment
    markers are stripped so the preview shows the actual question, not the
    file references appended to it.
    """
    if not text:
        return ""
    # Drop trailing attachment marker lines (e.g. "[Image: /path]").
    lines = text.split("\n")
    while lines:
        stripped = lines[-1].strip()
        if stripped and re.match(r"^\[[^\]:]+:\s*.+\]$", stripped):
            lines.pop()
            continue
        break
    body = "\n".join(lines)
    collapsed = " ".join(body.split())
    if len(collapsed) > max_chars:
        return collapsed[:max_chars].rstrip() + "…"
    return collapsed


# Internal markers written into the session for the agent's own bookkeeping
# (scheduler injection / self-evolution undo). They must stay in the stored
# content (the LLM reads them, e.g. to find a backup_id for undo) but should
# never be shown verbatim to the user in the chat history UI.
_SCHEDULED_DISPLAY_MARKERS = ("[SCHEDULED]", "Scheduled task")
_EVOLUTION_DISPLAY_MARKER = "[EVOLUTION]"


def _is_internal_user_marker(text: str) -> bool:
    """True if a user-turn text is an internal injection marker (hide from UI)."""
    t = (text or "").lstrip()
    return any(t.startswith(m) for m in _SCHEDULED_DISPLAY_MARKERS)


def _is_evolution_text(text: str) -> bool:
    """True if assistant text is a self-evolution summary (before cleaning)."""
    return (text or "").lstrip().startswith(_EVOLUTION_DISPLAY_MARKER)


def _message_plain_text(raw_content: Any) -> str:
    """Human-readable text of a stored message, whose content is a JSON string.

    Decodes the DB-stored JSON (a string or a list of content blocks) and reuses
    :func:`_extract_display_text`. Returns '' for tool-only messages.
    """
    try:
        content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
    except Exception:
        content = raw_content
    return _extract_display_text(content)


def _message_is_scheduled_marker(raw_content: Any) -> bool:
    """True when a stored user message is the ``[SCHEDULED]`` injection marker
    that precedes a scheduled task's delivered assistant reply."""
    return _is_internal_user_marker(_message_plain_text(raw_content))


def _clean_display_text(text: str) -> str:
    """Strip internal markers from assistant text for user-facing display.

    Removes a leading ``[EVOLUTION]`` tag and a trailing ``(backup_id: ...)``
    undo hint. The raw stored message is untouched, so undo + LLM context still
    work; only the rendered chat bubble is cleaned.
    """
    if not text:
        return text
    cleaned = text
    stripped = cleaned.lstrip()
    if stripped.startswith(_EVOLUTION_DISPLAY_MARKER):
        cleaned = stripped[len(_EVOLUTION_DISPLAY_MARKER):].lstrip()
    # Drop a trailing backup_id undo hint line, e.g.
    #   "(backup_id: 20260607-...; to undo, restore this backup)"
    cleaned = re.sub(
        r"\n*\(backup_id:[^\)]*\)\s*$",
        "",
        cleaned,
    ).rstrip()
    return cleaned


def _extract_tool_calls(content: Any) -> List[Dict[str, Any]]:
    """
    Extract tool_use blocks from an assistant message content.
    Returns a list of {name, arguments} dicts (result filled in later).
    """
    if not isinstance(content, list):
        return []
    return [
        {"id": b.get("id", ""), "name": b.get("name", ""), "arguments": b.get("input", {})}
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def _extract_tool_results(content: Any) -> Dict[str, dict]:
    """
    Extract tool_result blocks from a user message, keyed by tool_use_id.
    Values are {"result": str, "is_error": bool}.
    """
    if not isinstance(content, list):
        return {}
    results = {}
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        tool_id = b.get("tool_use_id", "")
        result_content = b.get("content", "")
        if isinstance(result_content, list):
            result_content = "\n".join(
                rb.get("text", "") for rb in result_content
                if isinstance(rb, dict) and rb.get("type") == "text"
            )
        results[tool_id] = {"result": str(result_content), "is_error": bool(b.get("is_error", False))}
    return results


def _ends_with_answer(rest: List[tuple]) -> bool:
    """True when a turn's stored reply closes with a final assistant message."""
    if not rest:
        return False
    role, content = rest[-1][0], rest[-1][1]
    if role != "assistant":
        return False
    return not (isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    ))


def _usage_from_extras(raw_extras: Any) -> Optional[Dict[str, Any]]:
    """Read a normalized per-message usage snapshot from stored extras."""
    if not raw_extras:
        return None
    try:
        extras = json.loads(raw_extras) if isinstance(raw_extras, str) else raw_extras
    except Exception:
        return None
    if not isinstance(extras, dict):
        return None
    usage = extras.get("usage")
    return usage if isinstance(usage, dict) and usage else None


def _group_into_display_turns(
    rows: List[tuple],
    include_thinking: bool = True,
    unfinished_runs: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Convert raw DB rows into display turns. Rows loaded for the web history
    include ``seq`` as their first field and may carry ``run_id`` as their
    last; older callers may still pass the legacy
    ``(role, content_json, created_at, extras)`` shape.

    ``unfinished_runs`` maps a run id to ``"running"`` or ``"interrupted"``.
    A turn of such a run whose reply stops short of a final answer is tagged
    with that ``run_state``, and still gets an assistant turn when nothing of
    the reply was stored yet.

    One display turn = one visible user message  +  one merged assistant reply.
    All intermediate assistant messages (those carrying tool_use) and the final
    assistant text reply produced for the same user query are collapsed into a
    single assistant turn, exactly matching the live SSE rendering where tools
    and the final answer appear inside the same bubble.

    Grouping rules:
    - A visible user message starts a new group.
    - tool_result user messages are internal; their content is attached to the
      matching tool_use entry via tool_use_id and they never become own turns.
    - All assistant messages within a group are merged:
        * tool_use blocks → tool_calls list (result filled from tool_results)
        * text blocks → last non-empty text becomes the display content
    """
    # ------------------------------------------------------------------ #
    # Pass 1: split rows into groups, each starting with a visible user msg
    # ------------------------------------------------------------------ #
    # group = (user_row | None, [subsequent_rows])
    # user_row: (content, created_at)
    groups: List[tuple] = []
    cur_user: Optional[tuple] = None
    cur_rest: List[tuple] = []
    started = False

    for row in rows:
        run_id = ""
        if len(row) == 6:
            seq, role, raw_content, created_at, raw_extras, run_id = row
        elif len(row) == 5:
            seq, role, raw_content, created_at, raw_extras = row
        else:
            seq = None
            role, raw_content, created_at, raw_extras = row
        try:
            content = json.loads(raw_content)
        except Exception:
            content = raw_content
        try:
            extras = json.loads(raw_extras) if raw_extras else {}
            if not isinstance(extras, dict):
                extras = {}
        except Exception:
            extras = {}

        if role == "user" and _is_visible_user_message(content):
            if started:
                groups.append((cur_user, cur_rest))
            cur_user = (content, created_at, extras, seq, run_id or "")
            cur_rest = []
            started = True
        else:
            cur_rest.append((role, content, created_at, extras, seq, run_id or ""))

    if started:
        groups.append((cur_user, cur_rest))

    # ------------------------------------------------------------------ #
    # Pass 2: build display turns from each group
    # ------------------------------------------------------------------ #
    turns: List[Dict[str, Any]] = []

    for user_row, rest in groups:
        # User turn
        if user_row:
            content, created_at, u_extras, user_seq, _u_run = user_row
            text = _extract_display_text(content)
            # Hide internal injection markers (scheduler / self-evolution) so the
            # user never sees a synthetic "[SCHEDULED] self-evolution" bubble;
            # the assistant reply that follows is still rendered.
            if text and not _is_internal_user_marker(text):
                turn = {"role": "user", "content": text, "created_at": created_at}
                display = u_extras.get("display")
                if isinstance(display, str) and display:
                    turn["display"] = display
                if user_seq is not None:
                    turn["_seq"] = user_seq
                turns.append(turn)

        # Build an ordered list of steps preserving the original sequence:
        #   thinking → content → tool_call → content → ...
        steps: List[Dict[str, Any]] = []
        tool_results: Dict[str, str] = {}
        final_text = ""
        final_ts: Optional[int] = None
        final_seq: Optional[int] = None
        merged_extras: Dict[str, Any] = {}

        for role, content, created_at, extras, seq, _run in rest:
            if role == "assistant" and isinstance(extras, dict):
                merged_extras.update(extras)
            if role == "user":
                tool_results.update(_extract_tool_results(content))
            elif role == "assistant":
                # Walk content blocks in order to preserve interleaving
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "thinking":
                            if not include_thinking:
                                continue
                            txt = block.get("thinking", "").strip()
                            if txt:
                                steps.append({"type": "thinking", "content": txt})
                        elif btype == "text":
                            txt = block.get("text", "").strip()
                            if txt:
                                steps.append({"type": "content", "content": txt})
                                final_text = txt
                        elif btype == "tool_use":
                            steps.append({
                                "type": "tool",
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": block.get("input", {}),
                            })
                elif isinstance(content, str) and content.strip():
                    steps.append({"type": "content", "content": content.strip()})
                    final_text = content.strip()
                final_ts = created_at
                if seq is not None:
                    final_seq = seq

        # Attach tool results to tool steps
        for step in steps:
            if step["type"] == "tool":
                tr = tool_results.get(step.get("id", ""), {})
                if not isinstance(tr, dict):
                    tr = {"result": tr}
                step["result"] = tr.get("result", "")
                step["is_error"] = tr.get("is_error", False)

        # Detect a self-evolution bubble BEFORE cleaning the marker away, so the
        # UI can flag it even though the visible text stays clean.
        is_evolution = _is_evolution_text(final_text)

        # Clean internal markers from the user-facing assistant text. Applies to
        # both the final content and the mirrored content step so the rendered
        # bubble shows clean text while the stored message keeps the markers.
        final_text = _clean_display_text(final_text)
        for step in steps:
            if step.get("type") == "content":
                step["content"] = _clean_display_text(step.get("content", ""))

        run_state = None
        if unfinished_runs and not _ends_with_answer(rest):
            run_id = (user_row[4] if user_row else "") or next(
                (r[5] for r in reversed(rest) if r[5]), ""
            )
            run_state = unfinished_runs.get(run_id)

        if steps or final_text or run_state:
            turn = {
                "role": "assistant",
                "content": final_text,
                "steps": steps,
                "created_at": final_ts or (user_row[1] if user_row else 0),
            }
            if run_state:
                turn["run_state"] = run_state
            if is_evolution:
                turn["kind"] = "evolution"
            if merged_extras:
                turn["extras"] = merged_extras
                usage = merged_extras.get("usage")
                if isinstance(usage, dict) and usage:
                    turn["usage"] = usage
            if final_seq is not None:
                turn["_seq"] = final_seq
            turns.append(turn)

    return turns


# Ceiling for a page requested from outside (HTTP or a remote caller).
MAX_PAGE_SIZE = 200


def page_window(page: Any, page_size: Any, default_page_size: int,
                 cap: Optional[int] = MAX_PAGE_SIZE) -> tuple:
    """Clamp an untrusted (page, page_size) pair into a usable window.

    A negative page_size would reach SQLite as LIMIT -1 ("no limit") and 0
    would divide by zero; ``cap=None`` keeps only those lower bounds.
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = default_page_size
    page_size = max(1, page_size)
    return max(1, page), min(page_size, cap) if cap else page_size


class ConversationStore:
    """
    SQLite-backed store for per-session conversation history.

    Usage:
        store = ConversationStore(db_path)
        store.append_messages("user_123", new_messages, channel_type="feishu")
        msgs = store.load_messages("user_123", max_turns=30)
    """

    def __init__(self, db_path: Path, agent_id: str = ""):
        self._db_path = db_path
        # The Agent every row read/written through this handle belongs to. The
        # default Agent keeps ``""`` so a single-Agent install never backfills a
        # value and its historical rows stay valid; every other Agent scopes to
        # its own id. All queries are scoped by this so one global file can hold
        # every Agent's transcripts without one Agent seeing another's.
        self._agent_id = agent_id or ""
        self._lock = threading.RLock()  # Use RLock to allow reentrant locking
        self._schema_identity: tuple = ()
        # True once the runs table is confirmed present. When it is not, run
        # bookkeeping degrades to a no-op so it can never break a turn or a
        # history query -- runs are auxiliary to conversation storage.
        self._runs_ready = False
        self._artifacts_ready = False
        self._init_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_messages(
        self,
        session_id: str,
        max_turns: int = 30,
        with_authors: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Load the most recent messages for a session, for injection into the LLM.

        ALL message types (user text, assistant tool_use, tool_result) are returned
        in their original JSON form so the LLM can reconstruct the full context.

        max_turns is a *visible-turn* count: we count only user messages whose
        content is actual user text (not tool_result blocks).  This prevents
        tool-heavy sessions from exhausting the turn budget prematurely.

        **P1 阶段三 M (契约 C-11/C-12): the content is back-filled BEFORE
        the turn budget is computed.**  This is the one place where the
        order of operations is load-bearing, and the reason is measured:
        ``_rows_within_turns`` counts VISIBLE user messages, and
        ``_is_visible_user_message(None)`` is False.  So a session whose
        content had been externalised without a back-fill would count
        ZERO visible turns, the budget would never be reached, and
        EVERY row would be returned — measured on the ROOT library:
        ``max_turns=1`` returned 12,136 rows instead of 378, with no
        error anywhere.  The back-fill therefore happens on the rows,
        and the budget is computed on the back-filled text.

        Args:
            session_id: Unique session identifier.
            max_turns: Maximum number of visible user-assistant turns to keep.
            with_authors: Also report which Agent wrote each message, as an
                ``agent_id`` key. Off by default because the answer is only
                ever "the one Agent in this conversation"; a shared transcript
                is where it matters, and where an Agent reading back its own
                history would otherwise take a colleague's work for its own.

        Returns:
            Chronologically ordered list of message dicts (role, content).
        """
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                # Respect context_start_seq: only load messages at or after the boundary
                ctx_row = conn.execute(
                    "SELECT context_start_seq FROM sessions WHERE agent_id = ? AND session_id = ?",
                    (aid, session_id),
                ).fetchone()
                ctx_start = ctx_row[0] if ctx_row else 0

                columns = ("seq, role, content, content_path, content_anchor, "
                           "content_hash, content_state")
                columns += ", extras" if with_authors else ""
                cursor = conn.execute(
                    f"""
                    SELECT {columns}
                    FROM messages
                    WHERE agent_id = ? AND session_id = ? AND seq >= ?
                    ORDER BY seq DESC
                    """,
                    (aid, session_id, ctx_start),
                )
                # ONE reader for the whole call: the truth source is
                # opened once per file, not once per row (契约 C-12).
                reader = msg_chain.ContentReader(self._workspace_root())
                rows = self._rows_within_turns(cursor, max(1, max_turns),
                                               reader=reader)
            finally:
                conn.close()

        if not rows:
            return []

        authors = {row[0]: self._author_of(row[7]) for row in rows} if with_authors else {}

        # **The back-fill (契约 C-12).**  It happens HERE, before the
        # result is assembled — and the budget above was already
        # computed on BACK-FILLED content, because
        # ``_rows_within_turns`` back-fills before its visibility test.
        # A reader that returned ``None`` for an externalised row would
        # hand the model a screenful of empty messages; a reader that
        # returned ``""`` would make the message invisible.  Both are
        # silent failures, so the back-fill either produces the text or
        # raises.
        result = []
        for row in reversed(rows):
            seq, role = row[0], row[1]
            raw_content = row[2]
            rest = row[3:]
            if with_authors:
                extras = rest[3] if len(rest) > 3 else ""
            else:
                extras = None
            content_text = self._backfill_content(raw_content, rest, reader)
            try:
                content = json.loads(content_text)
            except Exception:
                content = content_text
            # Strip thinking blocks — they are stored for UI display only
            if role == "assistant" and isinstance(content, list):
                content = [b for b in content if b.get("type") != "thinking"]
            message = {"role": role, "content": content}
            if authors.get(seq):
                message["agent_id"] = authors[seq]
            result.append(message)
        return result

    def _workspace_root(self) -> Path:
        """The workspace this store's truth layer lives under.

        The messages truth source is ``<workspace>/memory/messages_export``.
        The store's DB path is ``<workspace>/memory/long-term/index.db``, so
        the workspace is the DB path's grandparent — the same shape
        ``resolve_truth_file`` uses for the chunks side.
        """
        try:
            return self._db_path.resolve().parents[2]
        except Exception:
            return Path(self._db_path).parent.parent.parent

    def _backfill_content(self, raw_content: Optional[str], rest: tuple,
                          reader) -> str:
        """The row's text — from the DB, or back-filled from the truth source.

        ``rest`` is the tail of the SELECTed row after ``content``: the
        pointer columns, then (optionally) ``extras``.

        The dispatch lives in ``msg_chain.load_content`` so that EVERY
        reader goes through ONE implementation (契约 C-12 forbids each
        read path implementing its own back-fill).
        """
        row = {
            "content": raw_content,
            "content_path": rest[0] if len(rest) > 0 else "",
            "content_anchor": rest[1] if len(rest) > 1 else "",
            "content_hash": rest[2] if len(rest) > 2 else "",
            "content_state": rest[3] if len(rest) > 3 else "inline",
        }
        return msg_chain.load_content(row, self._workspace_root(), reader=reader)

    def _backfill_rows(self, rows: List[tuple]) -> List[tuple]:
        """Every row's ``content``, back-filled — one reader for all rows.

        The rows come in as the ten-column shape
        ``(seq, role, content, created_at, extras, run_id,
        content_path, content_anchor, content_hash, content_state)``
        and go out as the SIX-column shape
        ``(seq, role, content, created_at, extras, run_id)`` that
        ``_group_into_display_turns`` reads.  The pointer columns are
        dropped on purpose: by the time the rows reach the grouper the
        text is already in hand, and the grouper must not have to know
        about the externalisation at all (裁决 R-05: the grouper's code
        does not change — only the content it is handed does).

        A row whose content cannot be recovered RAISES (契约 C-12).
        """
        reader = msg_chain.ContentReader(self._workspace_root())
        out: List[tuple] = []
        for row in rows:
            raw = row[2]
            tail = row[6:] if len(row) > 6 else ()
            text = self._backfill_content(raw, tail, reader)
            if len(row) >= 6:
                out.append((row[0], row[1], text, row[3], row[4], row[5]))
            else:
                out.append((row[0], row[1], text, row[3]))
        return out

    @staticmethod
    def _rows_within_turns(cursor, max_turns: int, reader=None) -> List[tuple]:
        """Newest-first rows back to the ``max_turns``-th visible user message.

        Reading stops at the first visible user message beyond the budget, so a
        long session costs its recent tail instead of its whole history. When
        the session has no more than ``max_turns`` visible turns, every row is
        returned, including any that precede the oldest visible message.

        **P1 阶段三 M (契约 C-12): the visibility test runs on the
        BACK-FILLED content.**  ``_is_visible_user_message(None)`` is
        False — so an externalised row whose text had not been
        back-filled would count as "not a visible user message", the
        visible count would stay at ZERO, the budget would never be
        reached, and EVERY row of the session would be returned.
        Measured on the ROOT library: ``max_turns=1`` returned 12,136
        rows instead of 378, silently.

        So the content is back-filled HERE, before the visibility test,
        and the back-filled text replaces the row's ``content`` in the
        returned row.  A row whose content cannot be recovered RAISES
        (契约 C-12) — the budget must never be computed on a guess.
        """
        kept: List[tuple] = []
        older: List[tuple] = []
        visible = 0
        for row in cursor:
            is_visible = False
            if row[1] == "user":
                content = ConversationStore._visible_content_of(row, reader)
                is_visible = _is_visible_user_message(content)
            if visible >= max_turns:
                if is_visible:
                    return kept
                older.append(row)
                continue
            kept.append(row)
            if is_visible:
                visible += 1
        return kept + older

    @staticmethod
    def _visible_content_of(row: tuple, reader) -> Any:
        """The row's text, back-filled, for the visibility test."""
        raw = row[2]
        if raw is not None:
            # An inline row: the text is here.  A row whose state says
            # 'external' or 'pending' is NOT inline — the dispatch below
            # handles it, and the two are different states.
            state = row[6] if len(row) > 6 else "inline"
            if state == "inline":
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
        # The externalised / pending / illegal states go through the ONE
        # implementation (契约 C-12).
        text = msg_chain.load_content(
            {
                "content": raw,
                "content_path": row[3] if len(row) > 3 else "",
                "content_anchor": row[4] if len(row) > 4 else "",
                "content_hash": row[5] if len(row) > 5 else "",
                "content_state": row[6] if len(row) > 6 else "inline",
            },
            reader._root if reader is not None else Path("."),
            reader=reader,
        )
        try:
            return json.loads(text)
        except Exception:
            return text

    def _adjust_msg_count(self, conn: sqlite3.Connection, session_id: str, delta: int) -> None:
        if not delta:
            return
        conn.execute(
            "UPDATE sessions SET msg_count = MAX(msg_count + ?, 0) WHERE agent_id = ? AND session_id = ?",
            (delta, self._agent_id, session_id),
        )

    @staticmethod
    def _author_of(raw_extras: Any) -> str:
        """The Agent stamped on a stored message, if any.

        Absent on every message written by a conversation's own Agent, which is
        all of them until somebody else is invited in.
        """
        if not raw_extras:
            return ""
        try:
            extras = json.loads(raw_extras) if isinstance(raw_extras, str) else raw_extras
        except ValueError:
            return ""
        if not isinstance(extras, dict):
            return ""
        agent_id = extras.get("agent_id")
        return agent_id if isinstance(agent_id, str) else ""

    def append_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        channel_type: str = "",
        create_if_missing: bool = True,
        run_id: Optional[str] = None,
    ) -> bool:
        """
        Append new messages to a session's history.

        Seq numbers continue from the session's current maximum, so
        concurrent callers on distinct sessions never collide.

        Args:
            session_id: Unique session identifier.
            messages: List of message dicts to append.
            channel_type: Source channel (e.g. "feishu", "web", "wechat").
                          Only written on session creation; ignored on update.
            create_if_missing: When False, do nothing if the session row is
                          gone. Callers that already stored the user turn use
                          this so a session deleted mid-run is not recreated
                          from the reply alone.
            run_id: The run these messages belong to. Falls back to the ambient
                          RuntimeIdentity's run id, so callers inside a run do
                          not have to thread it through. A per-message ``run_id``
                          key overrides it for that one message.

        Returns:
            True when the messages were written, False when the session was
            missing and ``create_if_missing`` is False.
        """
        if not messages:
            return False

        if run_id is None:
            from common.utils import current_agent_run_id
            run_id = current_agent_run_id() or ""

        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    if not create_if_missing:
                        exists = conn.execute(
                            "SELECT 1 FROM sessions WHERE agent_id = ? AND session_id = ?",
                            (aid, session_id),
                        ).fetchone()
                        if not exists:
                            return False
                    # INSERT OR IGNORE creates the row on first visit;
                    # the UPDATE always refreshes last_active.
                    # Avoids ON CONFLICT...DO UPDATE (requires SQLite >= 3.24).
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO sessions
                            (agent_id, session_id, channel_type, created_at, last_active, msg_count)
                        VALUES (?, ?, ?, ?, ?, 0)
                        """,
                        (aid, session_id, channel_type, now, now),
                    )
                    conn.execute(
                        "UPDATE sessions SET last_active = ? WHERE agent_id = ? AND session_id = ?",
                        (now, aid, session_id),
                    )

                    # Determine starting seq for the new batch.
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    ).fetchone()
                    next_seq = row[0] + 1
                    # Deleting old turns can lower MAX(seq) below a cleared
                    # context boundary. New messages must remain visible even
                    # when those deleted sequence numbers would be reusable.
                    boundary = conn.execute(
                        "SELECT context_start_seq FROM sessions "
                        "WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    ).fetchone()
                    if boundary:
                        next_seq = max(next_seq, boundary[0])

                    inserted = 0
                    for msg in messages:
                        role = msg.get("role", "")
                        content = json.dumps(
                            msg.get("content", ""), ensure_ascii=False
                        )
                        extras_obj = msg.get("extras") or {}
                        extras = json.dumps(extras_obj, ensure_ascii=False) if extras_obj else ""
                        msg_run_id = str(msg.get("run_id") or run_id or "")
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO messages
                                (agent_id, session_id, seq, role, content, created_at, extras, run_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (aid, session_id, next_seq, role, content, now, extras, msg_run_id),
                        )
                        inserted += max(cur.rowcount, 0)
                        next_seq += 1

                    # Incremental on purpose: on databases upgraded in place the
                    # session index lacks agent_id, so a COUNT(*) here reads
                    # every row of the session and, on a long transcript, holds
                    # the write lock for minutes.
                    self._adjust_msg_count(conn, session_id, inserted)

                    # Auto-generate title from the first visible user message
                    cur_title = conn.execute(
                        "SELECT title FROM sessions WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    ).fetchone()
                    if cur_title and not cur_title[0]:
                        for msg in messages:
                            if msg.get("role") == "user":
                                content = msg.get("content", "")
                                text = (msg.get("extras") or {}).get("display") or _extract_display_text(content)
                                if text:
                                    title = text[:50].split("\n")[0]
                                    conn.execute(
                                        "UPDATE sessions SET title = ? WHERE agent_id = ? AND session_id = ?",
                                        (title, aid, session_id),
                                    )
                                    break
                    return True
            finally:
                conn.close()

    def clear_context(self, session_id: str) -> int:
        """
        Set the context boundary to after the current last message.
        Messages before this boundary are still stored but excluded from LLM context.

        Returns the new context_start_seq value.
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    ).fetchone()
                    new_start = row[0] + 1
                    conn.execute(
                        "UPDATE sessions SET context_start_seq = ?, context_usage = '' "
                        "WHERE agent_id = ? AND session_id = ?",
                        (new_start, aid, session_id),
                    )
                    return new_start
            finally:
                conn.close()

    def get_context_start_seq(self, session_id: str) -> int:
        """Return the context_start_seq for a session (0 if not set)."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT context_start_seq FROM sessions WHERE agent_id = ? AND session_id = ?",
                    (self._agent_id, session_id),
                ).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()

    def get_latest_pair_seqs(self, session_id: str) -> Dict[str, Any]:
        """Return the latest visible user/assistant seqs and assistant usage.

        A "visible" user message is one whose content is real user text
        (not just a tool_result block), so tool-execution turns do not
        shadow the actual user query.

        Returns:
            Dict with keys ``user_seq``, ``bot_seq`` and ``usage``; seqs may be
            None when no matching message exists, and usage is None when the
            latest assistant message has no provider snapshot.
        """
        result: Dict[str, Any] = {"user_seq": None, "bot_seq": None, "usage": None}
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                # Latest assistant message (cheap: single row by seq DESC).
                row = conn.execute(
                    "SELECT seq, extras FROM messages "
                    "WHERE agent_id = ? AND session_id = ? AND role = 'assistant' "
                    "ORDER BY seq DESC LIMIT 1",
                    (aid, session_id),
                ).fetchone()
                if row:
                    result["bot_seq"] = int(row[0])
                    result["usage"] = _usage_from_extras(row[1])

                # Latest visible user message: scan recent user rows and
                # skip pure tool_result entries.
                rows = conn.execute(
                    "SELECT seq, content FROM messages "
                    "WHERE agent_id = ? AND session_id = ? AND role = 'user' "
                    "ORDER BY seq DESC",
                    (aid, session_id),
                )
                for seq, content_raw in rows:
                    try:
                        content = json.loads(content_raw)
                    except Exception:
                        result["user_seq"] = int(seq)
                        break
                    if isinstance(content, list):
                        has_text = any(
                            isinstance(b, dict) and b.get("type") == "text"
                            for b in content
                        )
                        has_tool_result = any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content
                        )
                        if has_text and not has_tool_result:
                            result["user_seq"] = int(seq)
                            break
                    else:
                        result["user_seq"] = int(seq)
                        break
            finally:
                conn.close()
        return result

    def _archive_before_delete(self, conn: sqlite3.Connection, session_id: str,
                               seq_from: Optional[int] = None,
                               seq_to: Optional[int] = None) -> int:
        """Archive the rows' text into the truth source BEFORE deleting them.

        **Why this step exists (裁决 C-4 · 冲突 2 · N-10).**  The
        contract's model is: *the database is a projection, the JSONL is
        the truth, and a delete may only remove the projection.*  The
        invariant behind the delete guard is therefore not "refuse to
        delete" — it is **"never let a fact exist nowhere else"**.

        An ``inline`` row's text exists ONLY in the database.  So the
        delete path ARCHIVES it first: the text goes into the
        append-only JSONL (the truth source keeps it forever — 裁决
        N-10: "真源只追加 … JSONL 里的旧行永久保留"), the row is marked
        ``external``, and only then is the projection dropped.

        **The deviation, stated plainly.**  The contract's literal
        wording is "``inline`` / ``pending`` → 拒绝删除并报红".  Taken
        literally that BREAKS the product: ``inline`` is the normal
        state of every message written in the last seven days, so the
        web console's "delete session", the "edit message" flow and the
        scheduler's pruning would all start throwing — and those are
        DAILY user actions (风控 R-05 measured the trigger as
        "用户日常操作，概率高").  Nine existing product tests assert
            that those paths work.

        The archive-first reading keeps the invariant STRICTLY
        STRONGER (the text is preserved rather than the delete being
        refused) and keeps the product working.  The guard below still
        refuses — but now it refuses the case that is actually
        dangerous: a row whose text could NOT be archived (the archive
        failed, or the row is in the crash-window ``pending`` state).

        Returns the number of rows archived.
        """
        sql = ("SELECT seq, role, content, created_at, run_id, extras "
               "FROM messages WHERE agent_id = ? AND session_id = ? "
               "AND content IS NOT NULL")
        args: list = [self._agent_id, session_id]
        if seq_from is not None:
            sql += " AND seq >= ?"
            args.append(seq_from)
        if seq_to is not None:
            sql += " AND seq < ?"
            args.append(seq_to)
        sql += " ORDER BY seq ASC"
        rows = conn.execute(sql, tuple(args)).fetchall()
        if not rows:
            return 0
        try:
            from agent.memory import msg_migrate

            archived = msg_migrate.append_rows_to_truth(
                conn, self._workspace_root(), self._agent_id, session_id,
                [tuple(r) for r in rows],
            )
        except Exception as e:
            raise msg_chain.MsgChainError(
                "the messages could not be archived into the truth source "
                "before the delete: %s: %s — the delete is refused, because "
                "deleting them now would fold a fact"
                % (type(e).__name__, e))
        return archived

    def _guard_delete(self, conn: sqlite3.Connection, session_id: str,
                      seq_from: Optional[int] = None,
                      seq_to: Optional[int] = None) -> None:
        """REFUSE to delete rows whose text has not reached the truth source.

        **P1 阶段三 M (契约 C-4 · 风控 G-04a): the guard lives in the
        DELETE path itself, not in the caller.**  A caller-side guard is
        a guard that the next caller forgets — and the four delete paths
        (``clear_session`` / ``delete_message_pair`` /
        ``prune_scheduled_messages`` / ``cleanup_old_sessions``) are
        called from a dozen places, one of which
        (``cleanup_old_sessions``) is currently dead code but is wired
        to a 30-day timer the day somebody remembers it.

        The rule: a row whose text is NOT in the truth source holds text
        that exists nowhere else — and deleting it is folding a fact.
        The delete paths archive first (see ``_archive_before_delete``),
        so by the time this guard runs the rows are ``external``.  What
        is left to refuse is the genuinely dangerous case: the archive
        failed, or the row is in the crash-window ``pending`` state.

        Raises ``MsgChainError`` — loudly — and deletes nothing.
        """
        sql = ("SELECT seq, content_state FROM messages "
               "WHERE agent_id = ? AND session_id = ?")
        args: list = [self._agent_id, session_id]
        if seq_from is not None:
            sql += " AND seq >= ?"
            args.append(seq_from)
        if seq_to is not None:
            sql += " AND seq < ?"
            args.append(seq_to)
        sql += " AND content_state != 'external'"
        try:
            rows = conn.execute(sql, tuple(args)).fetchall()
        except sqlite3.OperationalError:
            # A database that predates the column has no externalised
            # rows at all — nothing to guard.
            return
        if not rows:
            return
        states = {}
        for r in rows:
            state = r[1] or "inline"
            states.setdefault(state, []).append(r[0])
        raise msg_chain.MsgChainError(
            "refusing to delete %d message row(s) whose text has not "
            "reached the truth source (content_state: %s) — deleting them "
            "would fold a fact." % (
                len(rows),
                ", ".join("%s=%s" % (k, len(v)) for k, v in sorted(states.items())),
            ))

    def clear_session(self, session_id: str) -> None:
        """Delete all messages and the session record for a given session_id.

        **P1 阶段三 M:** the messages are ARCHIVED into the truth
        source first, and the delete is refused if that cannot be done.
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    self._archive_before_delete(conn, session_id)
                    self._guard_delete(conn, session_id)
                    conn.execute(
                        "DELETE FROM messages WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    )
                    conn.execute(
                        "DELETE FROM sessions WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    )
            finally:
                conn.close()

    def delete_message_pair(self, session_id: str, user_seq: int, delete_user: bool = True, cascade: bool = False) -> int:
        """Delete a user message and/or its corresponding assistant reply.

        The assistant reply is identified as all messages between user_seq
        and the next visible user message (or end of session).

        Args:
            session_id: Session identifier.
            user_seq: The seq number of the user message.
            delete_user: If True (default), delete the user message too.
                        If False, only delete assistant reply (for regenerate scenarios).
            cascade: If True, also delete all subsequent turns after this one.
                    Used by edit-message which removes this turn and everything after.

        Returns:
            Number of message rows deleted.
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    # Verify this is a user message
                    row = conn.execute(
                        "SELECT role FROM messages WHERE agent_id = ? AND session_id = ? AND seq = ?",
                        (aid, session_id, user_seq),
                    ).fetchone()
                    if not row or row[0] != "user":
                        return 0

                    if cascade:
                        # Delete from this message to end of session
                        start_seq = user_seq if delete_user else user_seq + 1
                        end_seq_row = conn.execute(
                            "SELECT MAX(seq) FROM messages WHERE agent_id = ? AND session_id = ?",
                            (aid, session_id),
                        ).fetchone()
                        end_seq = (end_seq_row[0] or user_seq) + 1
                    else:
                        # Find the next visible user message seq (exclude tool_result)
                        # Use batched query to avoid loading too many rows at once
                        next_user_seq = None
                        batch_size = 100
                        offset = 0
                        while True:
                            batch = conn.execute(
                                """
                                SELECT seq, content FROM messages
                                WHERE agent_id = ? AND session_id = ? AND seq > ? AND role = 'user'
                                ORDER BY seq ASC
                                LIMIT ? OFFSET ?
                                """,
                                (aid, session_id, user_seq, batch_size, offset),
                            ).fetchall()
                            if not batch:
                                break
                            for seq, content in batch:
                                try:
                                    content_obj = json.loads(content)
                                except Exception:
                                    content_obj = content
                                if _is_visible_user_message(content_obj):
                                    next_user_seq = seq
                                    break
                            if next_user_seq is not None:
                                break
                            offset += batch_size

                        # Determine the end boundary for deletion
                        if next_user_seq is not None:
                            end_seq = next_user_seq
                        else:
                            end_seq_row = conn.execute(
                                "SELECT MAX(seq) FROM messages WHERE agent_id = ? AND session_id = ?",
                                (aid, session_id),
                            ).fetchone()
                            end_seq = (end_seq_row[0] or user_seq) + 1

                        # Determine the start boundary for deletion
                        start_seq = user_seq if delete_user else user_seq + 1

                    # Delete messages from start_seq to end_seq (exclusive)
                    #
                    # P1 阶段三 M: the text is archived into the truth
                    # source first, and the guard is in the DELETE path.
                    self._archive_before_delete(conn, session_id, start_seq,
                                                end_seq)
                    self._guard_delete(conn, session_id, start_seq, end_seq)
                    cur = conn.execute(
                        "DELETE FROM messages WHERE agent_id = ? AND session_id = ? AND seq >= ? AND seq < ?",
                        (aid, session_id, start_seq, end_seq),
                    )
                    deleted = cur.rowcount
                    self._adjust_msg_count(conn, session_id, -deleted)

                    return deleted
            finally:
                conn.close()

    def prune_scheduled_messages(
        self,
        session_id: str,
        keep_last_n: int,
        markers: Optional[List[str]] = None,
    ) -> int:
        """
        Keep at most ``keep_last_n`` scheduler-injected user/assistant pairs in
        the session, deleting the older ones.

        A scheduler-injected pair is identified by a user message whose first
        text block starts with one of ``markers``; the immediately following
        assistant message (next seq) is treated as its paired output.

        Only scheduler-tagged messages are touched; regular user turns are
        never deleted. Safe to call repeatedly; no-op if nothing to prune.

        Args:
            session_id: Session to prune.
            keep_last_n: Maximum scheduler pairs to retain (must be >= 0).
            markers: Text prefixes that identify scheduler user messages.
                Defaults to ``["[SCHEDULED]", "Scheduled task"]`` so that
                pairs written by older versions are also recognised.

        Returns:
            Number of message rows deleted.
        """
        if keep_last_n < 0:
            keep_last_n = 0
        if markers is None:
            markers = ["[SCHEDULED]", "Scheduled task"]

        def _matches_marker(raw_content: str) -> bool:
            try:
                parsed = json.loads(raw_content)
            except Exception:
                parsed = raw_content
            text = _extract_display_text(parsed) if not isinstance(parsed, str) else parsed
            if not text:
                return False
            return any(text.startswith(m) for m in markers)

        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                # Only user content can carry a marker. Skipping the rest keeps
                # SQLite from pulling large assistant/tool payloads off disk.
                rows = conn.execute(
                    """
                    SELECT seq, role, CASE WHEN role = 'user' THEN content ELSE '' END
                    FROM messages
                    WHERE agent_id = ? AND session_id = ?
                    ORDER BY seq ASC
                    """,
                    (aid, session_id),
                ).fetchall()

                # Find scheduler pairs: each is (user_seq, assistant_seq?)
                pairs: List[tuple] = []  # list of (user_seq, assistant_seq_or_None)
                for idx, (seq, role, raw_content) in enumerate(rows):
                    if role != "user" or not _matches_marker(raw_content):
                        continue
                    assistant_seq = None
                    # Pair with the very next message if it's an assistant turn.
                    if idx + 1 < len(rows):
                        next_seq, next_role, _ = rows[idx + 1]
                        if next_role == "assistant":
                            assistant_seq = next_seq
                    pairs.append((seq, assistant_seq))

                if len(pairs) <= keep_last_n:
                    return 0

                to_delete_pairs = pairs[: len(pairs) - keep_last_n]
                seqs_to_delete: List[int] = []
                for user_seq, assistant_seq in to_delete_pairs:
                    seqs_to_delete.append(user_seq)
                    if assistant_seq is not None:
                        seqs_to_delete.append(assistant_seq)

                if not seqs_to_delete:
                    return 0

                placeholders = ",".join("?" * len(seqs_to_delete))
                with conn:
                    # P1 阶段三 M: the text is archived into the truth
                    # source first, and the guard is in the DELETE path.
                    self._archive_before_delete(conn, session_id)
                    self._guard_delete(conn, session_id)
                    cur = conn.execute(
                        f"DELETE FROM messages WHERE agent_id = ? AND session_id = ? AND seq IN ({placeholders})",
                        (aid, session_id, *seqs_to_delete),
                    )
                    self._adjust_msg_count(conn, session_id, -cur.rowcount)
                return len(seqs_to_delete)
            finally:
                conn.close()

    def cleanup_old_sessions(self, max_age_days: Optional[int] = None) -> int:
        """
        Delete sessions that have not been active within max_age_days.
        Web channel sessions are excluded — they are meant to be permanent.

        **P1 阶段三 M (契约 C-4 · 风控 G-04a): the 30-day delete is
        GUARDED.**  The database is a PROJECTION — a row whose text
        lives in the truth source may be deleted, because the truth
        source survives (裁决 N-10: the truth source is never deleted
        automatically).  A row whose text is still INLINE, or is in the
        crash-window ``pending`` state, holds text that exists nowhere
        else — and deleting it is folding a fact.

        This function is currently DEAD CODE (measured: the whole
        repository has one hit — the definition itself).  The guard is
        here anyway: the cost is one status query before the delete,
        and the cost of NOT having it is that the day somebody wires it
        to a timer, thirty days of conversations disappear with nobody
        remembering that the guard was supposed to be there.
        """
        try:
            from config import conf
            max_age = max_age_days or conf().get(
                "conversation_max_age_days", DEFAULT_MAX_AGE_DAYS
            )
        except Exception:
            max_age = max_age_days or DEFAULT_MAX_AGE_DAYS

        cutoff = int(time.time()) - max_age * 86400
        deleted = 0

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    stale = conn.execute(
                        "SELECT session_id FROM sessions "
                        "WHERE agent_id = ? AND last_active < ? AND channel_type != 'web'",
                        (aid, cutoff),
                    ).fetchall()
                    for (sid,) in stale:
                        # P1 阶段三 M: the text is archived into the truth
                        # source first, and the guard is in the DELETE
                        # path.
                        self._archive_before_delete(conn, sid)
                        self._guard_delete(conn, sid)
                        conn.execute(
                            "DELETE FROM messages WHERE agent_id = ? AND session_id = ?",
                            (aid, sid),
                        )
                        conn.execute(
                            "DELETE FROM sessions WHERE agent_id = ? AND session_id = ?",
                            (aid, sid),
                        )
                        deleted += 1
            finally:
                conn.close()

        if deleted:
            logger.info(f"[ConversationStore] Pruned {deleted} expired sessions")
        return deleted

    def attach_extras_to_last_assistant(
        self,
        session_id: str,
        extras: Dict[str, Any],
    ) -> Optional[int]:
        """
        Merge ``extras`` into the latest assistant message of a session.

        Used by post-processing (e.g. TTS) that needs to annotate an already
        persisted bot reply with attachments such as audio URLs.

        Returns the message seq that was updated, or ``None`` if no assistant
        message exists or the update could not be applied.
        """
        return self._attach_assistant_extras(session_id, extras)

    def get_assistant_message_snapshot(
        self, session_id: str, message_seq: int
    ) -> Optional[tuple]:
        """Capture an assistant row's immutable id and raw content before work.

        **P1 阶段三 M (契约 C-12 · R-13): the snapshot carries the
        DRIFT TOKEN, not the text.**  The old shape was
        ``(id, content)`` and the identity filter was
        ``AND id = ? AND content = ?`` — after the content is
        externalised, ``content`` is NULL in the DB, so the filter
        matched NOTHING and the extras update silently did nothing (it
        only logged a warning).  The identity that still holds after
        the externalisation is the row's ``id`` plus its
        ``content_hash``, so the snapshot is
        ``(id, content_hash)`` and the filter compares the HASH.

        The ``content`` is still returned to the caller for the
        "has the text changed?" question — the caller (the TTS path)
        compares it.  But the UPDATE's identity filter uses the hash.
        """
        with self._lock:
            conn = self._connect()
            try:
                cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(messages)").fetchall()}
                has_hash = "content_hash" in cols
                if has_hash:
                    row = conn.execute(
                        "SELECT id, content, content_hash FROM messages "
                        "WHERE agent_id = ? AND session_id = ? AND seq = ? "
                        "AND role = 'assistant'",
                        (self._agent_id, session_id, message_seq),
                    ).fetchone()
                    if row:
                        return (row[0], row[1], row[2])
                    return None
                row = conn.execute(
                    "SELECT id, content FROM messages WHERE agent_id = ? "
                    "AND session_id = ? AND seq = ? AND role = 'assistant'",
                    (self._agent_id, session_id, message_seq),
                ).fetchone()
                return row
            finally:
                conn.close()

    def attach_extras_to_assistant(
        self, session_id: str, message_seq: int, extras: Dict[str, Any], snapshot: tuple
    ) -> Optional[int]:
        """Merge extras only if the captured assistant row still has its content."""
        if (isinstance(message_seq, bool) or not isinstance(message_seq, int)
                or message_seq < 0 or snapshot is None):
            return None
        return self._attach_assistant_extras(session_id, extras, message_seq, snapshot)

    def _attach_assistant_extras(
        self, session_id: str, extras: Dict[str, Any], message_seq: Optional[int] = None,
        snapshot: Optional[tuple] = None
    ) -> Optional[int]:
        if not extras:
            return None
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                # **P1 阶段三 M (R-13): the identity filter must survive
                # the externalisation.**  The old filter was
                # ``AND id = ? AND content = ?`` — and after the content
                # moved to the JSONL, ``content`` is NULL in the DB, so
                # the UPDATE matched no row, ``rowcount`` was 0, and the
                # extras were DROPPED with only a warning in the log.
                # The identity that still holds is the row's ``id`` plus
                # its ``content_hash`` (the drift token).  A snapshot
                # that carries no hash (an old caller) falls back to the
                # content comparison, which is the old behaviour.
                identity_filter = ""
                identity_values: tuple = ()
                if snapshot is not None:
                    if len(snapshot) >= 3 and snapshot[2]:
                        identity_filter = " AND id = ? AND content_hash = ?"
                        identity_values = (snapshot[0], snapshot[2])
                    else:
                        # An inline row (no drift token yet) — the old
                        # comparison, unchanged.
                        identity_filter = " AND id = ? AND content = ?"
                        identity_values = (snapshot[0], snapshot[1])
                seq_filter = (" AND seq = ?" if message_seq is not None else "") + identity_filter
                params = (aid, session_id, message_seq) if message_seq is not None else (aid, session_id)
                params += identity_values
                row = conn.execute(
                    f"""
                    SELECT seq, extras FROM messages
                    WHERE agent_id = ? AND session_id = ? AND role = 'assistant'{seq_filter}
                    ORDER BY seq DESC LIMIT 1
                    """,
                    params,
                ).fetchone()
                if not row:
                    return None
                seq, raw = row
                try:
                    cur = json.loads(raw) if raw else {}
                    if not isinstance(cur, dict):
                        cur = {}
                except Exception:
                    cur = {}
                cur.update(extras)
                updated = conn.execute(
                    "UPDATE messages SET extras = ? WHERE agent_id = ? AND session_id = ? AND seq = ?"
                    + identity_filter,
                    (json.dumps(cur, ensure_ascii=False), aid, session_id, seq, *identity_values),
                )
                conn.commit()
                return seq if updated.rowcount else None
            except Exception as e:
                logger.warning(f"[ConversationStore] attach_extras failed: {e}")
                return None
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Runs: addressable, persisted units of work
    # ------------------------------------------------------------------

    @staticmethod
    def _run_row_to_dict(row: tuple) -> Dict[str, Any]:
        (
            run_id, agent_id, user_id, session_id, parent_run_id,
            task_id, task_source, status, started_at, ended_at, error, raw_extras,
        ) = row
        try:
            extras = json.loads(raw_extras) if raw_extras else {}
            if not isinstance(extras, dict):
                extras = {}
        except Exception:
            extras = {}
        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "session_id": session_id,
            "parent_run_id": parent_run_id,
            "task_id": task_id,
            "task_source": task_source,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "error": error,
            "extras": extras,
        }

    _RUN_COLUMNS = (
        "run_id, agent_id, user_id, session_id, parent_run_id, "
        "task_id, task_source, status, started_at, ended_at, error, extras"
    )

    def create_run(
        self,
        run_id: str,
        *,
        agent_id: str = "",
        user_id: str = "",
        session_id: str = "",
        parent_run_id: str = "",
        task_id: str = "",
        task_source: str = "",
        status: str = "running",
        extras: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record the start of a run. Idempotent: a second call with the same
        run_id is ignored so a retried entry point does not duplicate the row.

        Returns True when a new row was written, False when it already existed.
        """
        if not run_id:
            raise ValueError("run_id is required")
        if not self._runs_ready:
            return False
        if status == "running":
            _live_runs.add(run_id)
        now = int(time.time())
        extras_json = (
            json.dumps(extras, ensure_ascii=False) if extras else ""
        )
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO runs
                            (run_id, agent_id, user_id, session_id, parent_run_id,
                             task_id, task_source, status, started_at, ended_at,
                             error, extras)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?)
                        """,
                        (
                            run_id, agent_id, user_id, session_id, parent_run_id,
                            task_id, task_source, status, now, extras_json,
                        ),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def finish_run(
        self,
        run_id: str,
        status: str = "done",
        error: str = "",
        extras: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark a run finished (or failed). Sets ended_at and, when given,
        merges ``extras`` into the stored sidecar. Returns True if the run
        existed.
        """
        _live_runs.discard(run_id)
        if not run_id or not self._runs_ready:
            return False
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    if extras:
                        row = conn.execute(
                            "SELECT extras FROM runs WHERE run_id = ?", (run_id,)
                        ).fetchone()
                        if row is None:
                            return False
                        try:
                            cur_extras = json.loads(row[0]) if row[0] else {}
                            if not isinstance(cur_extras, dict):
                                cur_extras = {}
                        except Exception:
                            cur_extras = {}
                        cur_extras.update(extras)
                        extras_json = json.dumps(cur_extras, ensure_ascii=False)
                        cur = conn.execute(
                            """
                            UPDATE runs
                            SET status = ?, error = ?, ended_at = ?, extras = ?
                            WHERE run_id = ?
                            """,
                            (status, error, now, extras_json, run_id),
                        )
                    else:
                        cur = conn.execute(
                            """
                            UPDATE runs
                            SET status = ?, error = ?, ended_at = ?
                            WHERE run_id = ?
                            """,
                            (status, error, now, run_id),
                        )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def update_run_extras(self, run_id: str, extras: Dict[str, Any]) -> bool:
        """Merge keys into a run's sidecar without touching its lifecycle.

        Lets an observer attach a payload to a run it did not execute, so the
        status stays owned by whoever actually ran the work. Returns True if
        the run existed.
        """
        if not run_id or not extras or not self._runs_ready:
            return False
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    row = conn.execute(
                        "SELECT extras FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    if row is None:
                        return False
                    try:
                        merged = json.loads(row[0]) if row[0] else {}
                        if not isinstance(merged, dict):
                            merged = {}
                    except Exception:
                        merged = {}
                    merged.update(extras)
                    conn.execute(
                        "UPDATE runs SET extras = ? WHERE run_id = ?",
                        (json.dumps(merged, ensure_ascii=False), run_id),
                    )
                    return True
            finally:
                conn.close()

    def _unfinished_runs(self, conn: sqlite3.Connection, session_id: str) -> Dict[str, str]:
        """Runs of a session that have not completed, as run_id -> run_state.

        A run still marked running is ``"running"`` only while this process
        runs it; otherwise it was cut off and reads as ``"interrupted"``, the
        same as a failed one.
        """
        if not self._runs_ready:
            return {}
        try:
            rows = conn.execute(
                "SELECT run_id, status FROM runs "
                "WHERE session_id = ? AND status IN ('running', 'failed')",
                (session_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {
            run_id: "running" if status == "running" and run_id in _live_runs else "interrupted"
            for run_id, status in rows
        }

    def latest_seq(self, session_id: str) -> Optional[int]:
        """Seq of the newest stored message in a session, or None if empty."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT MAX(seq) FROM messages WHERE agent_id = ? AND session_id = ?",
                    (self._agent_id, session_id),
                ).fetchone()
            finally:
                conn.close()
        return int(row[0]) if row and row[0] is not None else None

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return a single run by id, or None."""
        if not run_id or not self._runs_ready:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {self._RUN_COLUMNS} FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            finally:
                conn.close()
        return self._run_row_to_dict(row) if row else None

    def list_runs(
        self,
        session_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        task_source: Optional[str] = None,
        task_id: Optional[str] = None,
        status: Optional[str] = None,
        agent_id: Optional[str] = None,
        since: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List runs, newest first, filtered by any combination of the given
        dimensions. ``parent_run_id=''`` selects top-level runs only.

        ``since`` keeps only runs that *started strictly after* the given epoch
        seconds; it powers the client's cross-session scheduler poll, which asks
        "any new executions since I last checked?" without re-fetching history.

        ``offset`` skips the first N rows for paging the history list ("load
        more"). Combined with the stable ``started_at DESC, run_id DESC`` order
        it gives deterministic pages.

        Unlike the conversation methods this is intentionally *not* scoped to the
        store's bound ``self._agent_id``: the runs table is one global ledger and
        the console shows the whole team's activity by default. Pass an explicit
        ``agent_id`` to scope to a single Agent (``''`` selects the default
        Agent's own rows).
        """
        if not self._runs_ready:
            return []
        clauses: List[str] = []
        params: List[Any] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if parent_run_id is not None:
            clauses.append("parent_run_id = ?")
            params.append(parent_run_id)
        if task_source is not None:
            clauses.append("task_source = ?")
            params.append(task_source)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("started_at > ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, limit))
        params.append(max(0, offset))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT {self._RUN_COLUMNS} FROM runs {where} "
                    "ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?",
                    tuple(params),
                ).fetchall()
            finally:
                conn.close()
        return [self._run_row_to_dict(r) for r in rows]

    def delete_run(self, run_id: str, agent_id: Optional[str] = None) -> bool:
        """Delete a single run row. Returns True if a row was removed.

        Only touches the ``runs`` ledger entry (the history-list item); the
        delivered message persisted in the session history is left intact. When
        ``agent_id`` is given the delete is scoped to that Agent so one Agent
        cannot remove another's run by id alone.
        """
        if not self._runs_ready or not run_id:
            return False
        clauses = ["run_id = ?"]
        params: List[Any] = [run_id]
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        where = " AND ".join(clauses)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(f"DELETE FROM runs WHERE {where}", tuple(params))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        """A run plus the full text it delivered, for a history detail view.

        The runs row only keeps a short ``output_preview`` (a list-view index).
        The complete delivered message lives in the *receiver's session*, which
        the scheduler threads the push into (``_remember_delivered_output``), so
        this joins back to that session to recover the full body.

        Correlation is by ``(session_id, agent_id)`` from the run, then the
        scheduler-injected assistant message whose ``created_at`` is closest at
        or after the run's ``started_at`` (the pair this run wrote). It is
        best-effort: the session copy is itself capped (~2000 chars) and old
        scheduler pairs are pruned, so ``full_output`` may be ``None`` — the
        caller falls back to the preview. Messages are scoped to the *run's*
        ``agent_id``, not this store's bound id, since a run may belong to any
        Agent in the global ledger.
        """
        run = self.get_run(run_id)
        if run is None:
            return None

        detail = dict(run)
        extras = detail.pop("extras", {}) or {}
        detail["task_name"] = extras.get("task_name", "")
        detail["action_type"] = extras.get("action_type", "")
        detail["channel_type"] = extras.get("channel_type", "")
        detail["instance_id"] = extras.get("instance_id", "")
        detail["trigger"] = extras.get("trigger", "")
        detail["output_preview"] = extras.get("output_preview", "")
        detail["full_output"] = None

        session_id = run.get("session_id") or ""
        if not session_id:
            return detail

        run_agent_id = run.get("agent_id") or ""
        started_at = run.get("started_at") or 0
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT seq, role, content, created_at
                    FROM messages
                    WHERE agent_id = ? AND session_id = ?
                    ORDER BY seq ASC
                    """,
                    (run_agent_id, session_id),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                conn.close()

        # Assistant messages that answer a [SCHEDULED] user turn are the delivered
        # bodies. Pick the one written closest at/after this run started; fall
        # back to the newest scheduler body if none lands after the timestamp
        # (clock skew, or the pair persisted a hair before the run row).
        candidates: List[tuple] = []  # (created_at, text)
        for i, (seq, role, raw, created_at) in enumerate(rows):
            if role != "user":
                continue
            if not _message_is_scheduled_marker(raw):
                continue
            if i + 1 < len(rows) and rows[i + 1][1] == "assistant":
                text = _message_plain_text(rows[i + 1][2])
                if text:
                    candidates.append((rows[i + 1][3] or 0, text))

        if candidates:
            after = [c for c in candidates if c[0] >= started_at]
            chosen = min(after, key=lambda c: c[0]) if after else max(
                candidates, key=lambda c: c[0]
            )
            detail["full_output"] = chosen[1]
        return detail

    def load_history_page(
        self,
        session_id: str,
        page: int = 1,
        page_size: int = 20,
        until_seq: Optional[int] = None,
        max_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Load a page of conversation history for UI display, grouped into turns.

        With ``until_seq`` the response runs from ``page`` back through the
        page holding the turn with that seq, in one go, and ``page`` in the
        result is the last page covered. This lets a client jump to an old
        message without walking the history one page per request.

        ``max_seq`` leaves out messages stored after it, so a reply still in
        flight can be shown as of a known point and followed live from there.

        An assistant turn whose run has not reached a final answer carries
        ``run_state``: ``"running"`` while this process still runs it,
        ``"interrupted"`` once it failed or was cut off by a stop or crash.

        Each "turn" maps to one of:
          - A user message (role="user", content=str)
          - An assistant message (role="assistant", content=str,
            tool_calls=[{name, arguments, result}] when tools were used)

        Internal tool_result user messages are merged into the preceding
        assistant entry's tool_calls list and never appear as standalone items.

        Pages are numbered from 1 (most recent).  Messages within a page are
        returned in chronological order.

        ``page`` and ``page_size`` are clamped to ``[1, MAX_PAGE_SIZE]``.

        **P1 阶段三 M (契约 C-12): the content is back-filled before the
        turns are grouped.**  Measured on the ROOT library: with the
        content externalised and NOT back-filled, this method returned
        ``total = 0`` — the whole web history page went blank, with no
        error.  The back-fill happens here, through the ONE
        implementation, before ``_group_into_display_turns`` sees the
        rows.

        Returns:
            {
                "messages": [
                    {
                        "role": "user" | "assistant",
                        "content": str,
                        "tool_calls": [...],   # assistant only, may be []
                        "usage": {...},        # assistant only, provider-reported totals
                        "created_at": int,
                    },
                    ...
                ],
                "total": <visible turn count>,
                "page": <current page>,
                "page_size": <page_size>,
                "has_more": bool,
            }
        """
        page, page_size = page_window(page, page_size, 20)
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                ctx_row = conn.execute(
                    "SELECT context_start_seq FROM sessions WHERE agent_id = ? AND session_id = ?",
                    (aid, session_id),
                ).fetchone()
                ctx_start = ctx_row[0] if ctx_row else 0

                # extras / run_id columns are added by migration; tolerate
                # older DBs that might miss them by falling back to empty.
                #
                # P1 阶段三 M: the pointer columns are selected too, so the
                # back-fill knows WHERE the text is.  A database that
                # predates them still reads (the fallback branch), and
                # its rows are all 'inline' anyway.
                try:
                    rows = conn.execute(
                        """
                        SELECT seq, role, content, created_at, extras, run_id,
                               content_path, content_anchor, content_hash,
                               content_state
                        FROM messages
                        WHERE agent_id = ? AND session_id = ?
                        ORDER BY seq ASC
                        """,
                        (aid, session_id),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = [
                        (seq, role, content, created_at, "", "",
                         "", "", "", "inline")
                        for (seq, role, content, created_at) in conn.execute(
                            """
                            SELECT seq, role, content, created_at
                            FROM messages
                            WHERE agent_id = ? AND session_id = ?
                            ORDER BY seq ASC
                            """,
                            (aid, session_id),
                        ).fetchall()
                    ]
                unfinished_runs = self._unfinished_runs(conn, session_id)
            finally:
                conn.close()

        if max_seq is not None:
            rows = [row for row in rows if row[0] <= max_seq]

        # **The back-fill (契约 C-12).**  The rows' ``content`` is
        # replaced by the text from the truth source BEFORE the turns
        # are grouped — a turn whose user message has no text is not a
        # turn, and that is how a whole history page goes blank.
        rows = self._backfill_rows(rows)

        # Honour the current enable_thinking switch when building display turns
        # so that toggling it off hides previously-saved thinking blocks too.
        try:
            from config import conf
            include_thinking = bool(conf().get("enable_thinking", False))
        except Exception:
            include_thinking = False

        visible = _group_into_display_turns(
            rows,
            include_thinking=include_thinking,
            unfinished_runs=unfinished_runs,
        )

        total = len(visible)
        newest_first = list(reversed(visible))
        offset = (page - 1) * page_size
        last_page = page
        if until_seq is not None:
            for idx, turn in enumerate(newest_first):
                seq = turn.get("_seq")
                if seq is not None and seq <= until_seq:
                    last_page = max(page, idx // page_size + 1)
                    break
        end = last_page * page_size
        page_items = list(reversed(newest_first[offset:end]))

        return {
            "messages": page_items,
            "context_start_seq": ctx_start,
            "total": total,
            "page": last_page,
            "page_size": page_size,
            "has_more": end < total,
        }

    def list_user_messages(
        self,
        session_id: str,
        preview_chars: int = 60,
    ) -> Dict[str, Any]:
        """
        Return a lightweight index of every visible user message in a session,
        for building a navigation timeline in the UI.

        Unlike ``load_history_page`` this skips turn grouping and assistant
        content entirely: it only walks user rows, applies the same visibility
        rules (hiding tool_result and internal marker messages), and returns a
        compact ``{seq, preview, created_at}`` per entry. The payload stays
        small even for very long conversations, so the whole index can be
        fetched at once without pagination.

        **P1 阶段三 M (契约 C-12): the content is back-filled before the
        visibility test.**  Measured on the ROOT library: with the content
        externalised and NOT back-filled, this method returned
        ``total = 0`` — the navigation timeline disappeared entirely,
        with no error.

        Returns:
            {
                "messages": [{"seq": int, "preview": str, "created_at": int}, ...],
                "total": int,
            }
        """
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                rows = conn.execute(
                    """
                    SELECT seq, content, created_at, extras,
                           content_path, content_anchor, content_hash,
                           content_state
                    FROM messages
                    WHERE agent_id = ? AND session_id = ? AND role = 'user'
                    ORDER BY seq ASC
                    """,
                    (aid, session_id),
                ).fetchall()
            finally:
                conn.close()

        # **The back-fill (契约 C-12).**  One reader for the whole call.
        reader = msg_chain.ContentReader(self._workspace_root())
        items: List[Dict[str, Any]] = []
        for row in rows:
            seq, raw_content, created_at, raw_extras = row[0], row[1], row[2], row[3]
            text = self._backfill_content(raw_content, row[4:], reader)
            try:
                content = json.loads(text)
            except Exception:
                content = text
            # Only real user turns: skip tool_result carriers and the internal
            # scheduler / self-evolution injection markers.
            if not _is_visible_user_message(content):
                continue
            text = _extract_display_text(content)
            if not text or _is_internal_user_marker(text):
                continue
            try:
                extras = json.loads(raw_extras) if raw_extras else {}
            except Exception:
                extras = {}
            display = extras.get("display") if isinstance(extras, dict) else None
            preview = _first_line_preview(display if isinstance(display, str) and display else text, preview_chars)
            if not preview:
                continue
            items.append({
                "seq": seq,
                "preview": preview,
                "created_at": created_at,
            })

        return {"messages": items, "total": len(items)}

    def list_sessions(
        self,
        channel_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """
        List sessions with pinned ones first, then last_active DESC, with an
        optional channel_type filter.

        Pinned sessions sort ahead of everything else rather than only ahead of
        the rows on the same page, so a pin still reaches the top of the list
        when the conversation is old enough to sit several pages down.

        ``page`` and ``page_size`` are only bounded below: the cross-Agent
        listing asks each store for ``page * page_size`` rows at once.

        Returns:
            {
                "sessions": [{session_id, title, created_at, last_active,
                              msg_count, pinned}, ...],
                "total": int,
                "page": int,
                "page_size": int,
                "has_more": bool,
            }
        """
        page, page_size = page_window(page, page_size, 50, cap=None)
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                if channel_type:
                    total = conn.execute(
                        "SELECT COUNT(*) FROM sessions WHERE agent_id = ? AND channel_type = ?",
                        (aid, channel_type),
                    ).fetchone()[0]
                    rows = conn.execute(
                        """
                        SELECT session_id, title, created_at, last_active, msg_count, pinned
                        FROM sessions
                        WHERE agent_id = ? AND channel_type = ?
                        ORDER BY pinned DESC, last_active DESC
                        LIMIT ? OFFSET ?
                        """,
                        (aid, channel_type, page_size, (page - 1) * page_size),
                    ).fetchall()
                else:
                    total = conn.execute(
                        "SELECT COUNT(*) FROM sessions WHERE agent_id = ?",
                        (aid,),
                    ).fetchone()[0]
                    rows = conn.execute(
                        """
                        SELECT session_id, title, created_at, last_active, msg_count, pinned
                        FROM sessions
                        WHERE agent_id = ?
                        ORDER BY pinned DESC, last_active DESC
                        LIMIT ? OFFSET ?
                        """,
                        (aid, page_size, (page - 1) * page_size),
                    ).fetchall()
            finally:
                conn.close()

        sessions = [
            {
                "session_id": r[0],
                "title": r[1],
                "created_at": r[2],
                "last_active": r[3],
                "msg_count": r[4],
                "pinned": bool(r[5]),
            }
            for r in rows
        ]
        return {
            "sessions": sessions,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": (page - 1) * page_size + page_size < total,
        }

    def rename_session(self, session_id: str, title: str) -> bool:
        """Update the title of a session. Returns True if the session existed."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE sessions SET title = ? WHERE agent_id = ? AND session_id = ?",
                        (title, self._agent_id, session_id),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def upsert_title(self, session_id: str, title: str, channel_type: str = "") -> bool:
        """Set a session's title, creating its record first when missing.

        A client may title a brand-new chat before the agent has persisted its
        first turn; a plain rename would hit no row and lose the title. The
        record created here is the same one ``append_messages`` would create,
        so the later write just fills it in. Returns True if the row existed.
        """
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    inserted = conn.execute(
                        """
                        INSERT OR IGNORE INTO sessions
                            (agent_id, session_id, channel_type, title, created_at, last_active, msg_count)
                        VALUES (?, ?, ?, ?, ?, ?, 0)
                        """,
                        (aid, session_id, channel_type, title, now, now),
                    ).rowcount
                    if inserted:
                        return False
                    conn.execute(
                        "UPDATE sessions SET title = ? WHERE agent_id = ? AND session_id = ?",
                        (title, aid, session_id),
                    )
                    return True
            finally:
                conn.close()

    def set_pinned(self, session_id: str, pinned: bool) -> bool:
        """Pin or unpin a session. Returns True if the session existed."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE sessions SET pinned = ? WHERE agent_id = ? AND session_id = ?",
                        (1 if pinned else 0, self._agent_id, session_id),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def set_context_usage(self, session_id: str, usage: Optional[dict]) -> bool:
        """Store a session's context-usage snapshot (None clears it). Best-effort;
        returns True if the session existed."""
        try:
            payload = "" if usage is None else json.dumps(usage, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.debug(f"[ConversationStore] Context usage not serialisable: {e}")
            return False
        try:
            with self._lock:
                conn = self._connect()
                try:
                    with conn:
                        cur = conn.execute(
                            "UPDATE sessions SET context_usage = ? WHERE agent_id = ? AND session_id = ?",
                            (payload, self._agent_id, session_id),
                        )
                        return cur.rowcount > 0
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"[ConversationStore] Context usage write skipped: {e}")
            return False

    def get_context_usage(self, session_id: str) -> Optional[dict]:
        """The snapshot written by :meth:`set_context_usage`; None if missing or unreadable."""
        try:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT context_usage FROM sessions WHERE agent_id = ? AND session_id = ?",
                        (self._agent_id, session_id),
                    ).fetchone()
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"[ConversationStore] Context usage read skipped: {e}")
            return None
        if not row or not row[0]:
            return None
        try:
            data = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def list_session_ids(self, channel_type: Optional[str] = None) -> List[str]:
        """Every session id, optionally filtered by channel.

        One cheap single-column scan, used to work out how many distinct project
        spaces are actually in play without paging through full session rows.
        """
        with self._lock:
            conn = self._connect()
            try:
                if channel_type:
                    rows = conn.execute(
                        "SELECT session_id FROM sessions WHERE agent_id = ? AND channel_type = ?",
                        (self._agent_id, channel_type),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT session_id FROM sessions WHERE agent_id = ?",
                        (self._agent_id,),
                    ).fetchall()
            finally:
                conn.close()
        return [r[0] for r in rows]

    def get_stats(self) -> Dict[str, Any]:
        """Return basic stats keyed by channel_type, for monitoring."""
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                total_sessions = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE agent_id = ?", (aid,)
                ).fetchone()[0]
                total_messages = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE agent_id = ?", (aid,)
                ).fetchone()[0]
                by_channel = conn.execute(
                    """
                    SELECT channel_type, COUNT(*) as cnt
                    FROM sessions
                    WHERE agent_id = ?
                    GROUP BY channel_type
                    ORDER BY cnt DESC
                    """,
                    (aid,),
                ).fetchall()
                return {
                    "total_sessions": total_sessions,
                    "total_messages": total_messages,
                    "by_channel": {row[0] or "unknown": row[1] for row in by_channel},
                }
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    def record_artifacts(
        self,
        session_id: str,
        items: List[Dict[str, Any]],
        turn_seq: Optional[int] = None,
    ) -> int:
        """Index the files one stretch of a turn produced.

        Each item needs ``path`` (absolute) and may carry ``kind``, ``size`` and
        ``source``. A path already indexed for this Agent is moved to the top
        and re-pointed at the turn, so the index always leads to the turn that
        last produced the file. ``turn_seq`` defaults to the session's latest
        turn. Returns the number of items written.
        """
        if not items or not session_id or not self._artifacts_ready:
            return 0
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    if turn_seq is None:
                        turn_seq = self._latest_visible_user_seq(conn, session_id)
                    written = 0
                    for item in items:
                        path = str(item.get("path") or "")
                        if not path:
                            continue
                        kind = str(item.get("kind") or "file")
                        size = int(item.get("size") or 0)
                        source = str(item.get("source") or "")
                        row = conn.execute(
                            "SELECT id FROM artifacts WHERE agent_id = ? AND path = ? "
                            "ORDER BY id DESC LIMIT 1",
                            (aid, path),
                        ).fetchone()
                        if row:
                            conn.execute(
                                """
                                UPDATE artifacts
                                SET session_id = ?, turn_seq = ?, kind = ?, size = ?,
                                    source = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (session_id, turn_seq, kind, size, source, now, row[0]),
                            )
                        else:
                            conn.execute(
                                """
                                INSERT INTO artifacts
                                    (agent_id, session_id, turn_seq, path, kind, size,
                                     source, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (aid, session_id, turn_seq, path, kind, size, source, now, now),
                            )
                        written += 1
                    return written
            finally:
                conn.close()

    def list_artifacts(
        self,
        agent_ids: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
        query: str = "",
        path: str = "",
        offset: int = 0,
        limit: int = 60,
    ) -> Dict[str, Any]:
        """Newest-first page of indexed artifacts, with their session's title.

        Pinned rows come first, most recently pinned on top. ``agent_ids``
        widens the scope from this handle's Agent to the stored ids given
        (``""`` is the default Agent): every Agent shares the file, so one
        handle can read them all. ``path`` narrows to one exact file, and
        ``query`` matches the path or the given title.
        """
        if not self._artifacts_ready:
            return {"items": [], "has_more": False}
        scope = list(agent_ids) if agent_ids is not None else [self._agent_id]
        if not scope:
            return {"items": [], "has_more": False}
        where: List[str] = ["a.agent_id IN (%s)" % ",".join("?" * len(scope))]
        args: List[Any] = list(scope)
        if kinds:
            where.append("a.kind IN (%s)" % ",".join("?" * len(kinds)))
            args.extend(kinds)
        if path:
            where.append("a.path = ?")
            args.append(path)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(a.path LIKE ? ESCAPE '\\' OR a.title LIKE ? ESCAPE '\\')")
            args.extend([f"%{escaped}%"] * 2)
        clause = "WHERE " + " AND ".join(where)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"""
                    SELECT a.id, COALESCE(a.agent_id, ''), COALESCE(a.session_id, ''),
                           a.turn_seq, a.path, COALESCE(a.kind, 'file'),
                           COALESCE(a.size, 0), COALESCE(a.source, ''),
                           COALESCE(a.created_at, 0), COALESCE(a.updated_at, 0),
                           s.title, s.channel_type, COALESCE(a.pinned_at, 0) AS pin,
                           COALESCE(a.title, '')
                    FROM artifacts a
                    LEFT JOIN sessions s
                        ON s.agent_id = a.agent_id AND s.session_id = a.session_id
                    {clause}
                    ORDER BY pin DESC, a.updated_at DESC, a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*args, limit + 1, offset),
                ).fetchall()
            finally:
                conn.close()
        items = [
            {
                "id": row[0],
                "agent_id": row[1],
                "session_id": row[2],
                "turn_seq": row[3],
                "path": row[4],
                "kind": row[5],
                "size": row[6],
                "source": row[7],
                "created_at": row[8],
                "updated_at": row[9],
                "session_title": row[10],
                "session_channel": row[11],
                "session_exists": row[11] is not None,
                "pinned_at": row[12],
                "title": row[13],
            }
            for row in rows[:limit]
        ]
        return {"items": items, "has_more": len(rows) > limit}

    def set_artifact_title(self, artifact_id: int, title: str) -> bool:
        """Name one row for the view; an empty title falls back to the file name."""
        if not self._artifacts_ready:
            return False
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE artifacts SET title = ? WHERE agent_id = ? AND id = ?",
                        ((title or "").strip(), self._agent_id, int(artifact_id)),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def set_artifact_pinned(self, artifact_id: int, pinned: bool) -> Optional[int]:
        """Pin one row to the top of the timeline, or unpin it.

        Returns the stored ``pinned_at`` (0 when unpinned), or None when the row
        is not this Agent's.
        """
        if not self._artifacts_ready:
            return None
        pinned_at = int(time.time()) if pinned else 0
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE artifacts SET pinned_at = ? WHERE agent_id = ? AND id = ?",
                        (pinned_at, self._agent_id, int(artifact_id)),
                    )
                    return pinned_at if cur.rowcount > 0 else None
            finally:
                conn.close()

    def delete_artifact(self, artifact_id: int) -> bool:
        """Drop one row from the index. The file itself is left alone."""
        if not self._artifacts_ready:
            return False
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "DELETE FROM artifacts WHERE agent_id = ? AND id = ?",
                        (self._agent_id, int(artifact_id)),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def _latest_visible_user_seq(self, conn: sqlite3.Connection, session_id: str) -> Optional[int]:
        """seq of the newest real user message in a session, if any."""
        rows = conn.execute(
            "SELECT seq, content FROM messages "
            "WHERE agent_id = ? AND session_id = ? AND role = 'user' "
            "ORDER BY seq DESC",
            (self._agent_id, session_id),
        )
        for seq, raw in rows:
            try:
                content = json.loads(raw)
            except Exception:
                content = raw
            if _is_visible_user_message(content):
                return int(seq)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._raw_connect()
        try:
            # Core tables first and unguarded: if these cannot be created the
            # store is genuinely unusable and the error should surface.
            conn.executescript(_DDL)
            conn.commit()
            self._migrate(conn)
            # Runs are auxiliary. Their setup is isolated so a legacy table, a
            # half-applied upgrade or any other surprise degrades run tracking
            # instead of taking conversation history offline.
            self._init_runs(conn)
            self._init_artifacts(conn)
        finally:
            conn.close()
        self._schema_identity = self._db_identity()

    def _init_artifacts(self, conn: sqlite3.Connection) -> None:
        """Create the artifacts index without ever risking the core schema."""
        try:
            conn.executescript(_ARTIFACTS_DDL)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(artifacts)")}
            for name, decl in _ARTIFACTS_ADDED_COLUMNS:
                if name not in cols:
                    conn.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {decl}")
            conn.executescript(_ARTIFACTS_INDEX_DDL)
            conn.commit()
            self._artifacts_ready = True
        except Exception as e:
            self._artifacts_ready = False
            logger.warning(
                f"[ConversationStore] Artifact index unavailable ({e}); "
                "conversation history is unaffected"
            )
            try:
                conn.rollback()
            except Exception:
                pass

    def _init_runs(self, conn: sqlite3.Connection) -> None:
        """Create the runs table without ever risking the core schema."""
        try:
            self._retire_incompatible_runs_table(conn)
            conn.executescript(_RUNS_DDL)
            conn.commit()
            self._runs_ready = True
        except Exception as e:
            self._runs_ready = False
            logger.warning(
                f"[ConversationStore] Run tracking unavailable ({e}); "
                "conversation history is unaffected"
            )
            try:
                conn.rollback()
            except Exception:
                pass

    def _retire_incompatible_runs_table(self, conn: sqlite3.Connection) -> None:
        """Move aside a pre-existing ``runs`` table that predates this schema.

        An earlier, since-removed feature shipped a differently shaped ``runs``
        table in the same file. Its columns do not match the one the current
        code owns, so ``CREATE TABLE IF NOT EXISTS`` leaves the old table in
        place and a later ``CREATE INDEX`` on a column it lacks aborts the whole
        init script -- which takes every conversation query down with it. The
        old table is renamed rather than dropped so its rows stay recoverable,
        and the marker column check makes this a no-op on both the current
        schema and a database that never had the legacy table.
        """
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            if not exists:
                return
            cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            if "task_source" in cols:
                return
            backup = "runs_legacy_backup"
            # Never clobber an earlier backup; keep the current file untouched
            # if one is already parked there.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (backup,),
            ).fetchone():
                backup = f"runs_legacy_backup_{int(time.time())}"
            # Indexes on the old table would otherwise collide with the new
            # ones the DDL is about to create.
            for (idx_name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='runs' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                conn.execute(f'DROP INDEX IF EXISTS "{idx_name}"')
            conn.execute(f"ALTER TABLE runs RENAME TO {backup}")
            conn.commit()
            logger.warning(
                "[ConversationStore] Renamed a legacy runs table to "
                f"{backup}; its rows are preserved there"
            )
        except Exception as e:
            logger.warning(
                f"[ConversationStore] Could not retire legacy runs table: {e}"
            )

    def _db_identity(self) -> tuple:
        """Identify the physical file behind _db_path, or () when it is missing."""
        try:
            st = self._db_path.stat()
        except OSError:
            return ()
        return (st.st_dev, st.st_ino)

    def _ensure_schema(self) -> None:
        """Recreate the conversation tables when the shared DB file was swapped.

        The long-term memory index lives in the same file and may quarantine and
        replace it on corruption. Without this check, every later query would
        keep failing with "no such table: sessions" for the whole process
        lifetime, so new messages would silently stop being persisted.

        The stat identity alone is not sufficient: filesystems that hand freed
        inodes straight back (ext4/tmpfs, e.g. CI /tmp) can recreate the file
        under the exact same (dev, ino) pair, so the replacement is also caught
        by verifying the core table is still present.
        """
        if self._db_identity() == self._schema_identity and self._schema_present():
            return
        logger.warning(
            "[ConversationStore] Shared DB file was replaced; recreating conversation schema"
        )
        self._init_db()

    def _schema_present(self) -> bool:
        try:
            conn = self._raw_connect()
        except sqlite3.Error:
            return False
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
                ).fetchone()
                is not None
            )
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply incremental schema migrations on existing databases."""
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "channel_type" not in cols:
            try:
                conn.execute(_MIGRATION_ADD_CHANNEL_TYPE)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added channel_type column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration failed: {e}")
        if "title" not in cols:
            try:
                conn.execute(_MIGRATION_ADD_TITLE)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added title column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (title) failed: {e}")
        if "context_start_seq" not in cols:
            try:
                conn.execute(_MIGRATION_ADD_CONTEXT_START_SEQ)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added context_start_seq column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (context_start_seq) failed: {e}")
        if "pinned" not in cols:
            try:
                conn.execute(_MIGRATION_ADD_PINNED)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added pinned column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (pinned) failed: {e}")
        if "context_usage" not in cols:
            try:
                conn.execute(_MIGRATION_ADD_CONTEXT_USAGE)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added context_usage column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (context_usage) failed: {e}")

        msg_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "extras" not in msg_cols:
            try:
                conn.execute(_MIGRATION_ADD_MSG_EXTRAS)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added messages.extras column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (extras) failed: {e}")
        if "run_id" not in msg_cols:
            try:
                conn.execute(_MIGRATION_ADD_MSG_RUN_ID)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added messages.run_id column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (run_id) failed: {e}")

        # P1 阶段三 M · stage 1: the five externalisation columns.
        # ``content`` is still NOT NULL here — the table rebuild that
        # lifts the constraint is a SEPARATE step (stage 2), so this
        # stage leaves the old write path untouched.
        for col, decl in _MSG_EXTERNAL_COLUMNS:
            if col in msg_cols:
                continue
            try:
                conn.execute("ALTER TABLE messages ADD COLUMN %s %s" % (col, decl))
                conn.commit()
                logger.info("[ConversationStore] Migrated: added messages.%s column" % col)
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration ({col}) failed: {e}")

        # First-level global migration: give both tables an agent_id column.
        # Cheap, idempotent, and safe for every user -- a single-Agent install
        # simply gains an all-empty column that reads back as the default Agent.
        if "agent_id" not in cols:
            try:
                conn.execute(_MIGRATION_ADD_SESSION_AGENT_ID)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added sessions.agent_id column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (sessions.agent_id) failed: {e}")
        if "agent_id" not in msg_cols:
            try:
                conn.execute(_MIGRATION_ADD_MSG_AGENT_ID)
                conn.commit()
                logger.info("[ConversationStore] Migrated: added messages.agent_id column")
            except Exception as e:
                logger.warning(f"[ConversationStore] Migration (messages.agent_id) failed: {e}")

        # Now that agent_id is guaranteed present on both tables, (re)create the
        # agent-scoped indexes. Safe on every path: a fresh install created the
        # tables via _DDL a moment ago, an upgrade just added the column.
        try:
            conn.executescript(_INDEX_DDL)
            conn.commit()
        except Exception as e:
            logger.warning(f"[ConversationStore] Index creation failed: {e}")

    def _ensure_composite_key(self, conn: sqlite3.Connection) -> None:
        """Rebuild sessions/messages under composite ``(agent_id, session_id)`` keys.

        A pre-global database keyed ``sessions`` by ``session_id`` alone and
        ``messages`` by ``(session_id, seq)``. Those keys cannot hold two Agents
        that share a ``session_id`` (the same IM user in both), so before any
        second Agent's rows can join this file the tables must be rebuilt under
        the composite keys the current ``_DDL`` declares.

        Detected by inspecting the primary key of ``sessions``: a single-column
        PK means the old shape. The rebuild is the classic SQLite
        create-new / copy / drop / rename, wrapped in one transaction so an
        interruption rolls back to the old shape rather than a half-migrated one.
        Restricted by the caller to installs that actually need it (more than
        one Agent), so the vast single-Agent majority never rebuilds.
        """
        pk_cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            if row[5]  # pk position, 0 when not part of the primary key
        ]
        if pk_cols == ["agent_id", "session_id"]:
            return  # already composite
        logger.info(
            "[ConversationStore] Rebuilding sessions/messages under composite "
            "(agent_id, session_id) keys for multi-Agent merge"
        )
        with conn:
            conn.execute("DROP INDEX IF EXISTS idx_messages_session")
            conn.execute("DROP INDEX IF EXISTS idx_sessions_last_active")
            # **P1 阶段三 M: ``executescript`` IMPLICITLY COMMITS.**
            # The rebuild below must be ONE transaction — a half-migrated
            # ``messages_new`` left behind is exactly the state 裁决
            # G-05d forbids.  So the statements are executed one by one
            # through ``conn.execute`` inside the ``with conn`` block,
            # and the block's COMMIT is the only commit.
            conn.execute(
                """
                CREATE TABLE sessions_new (
                    agent_id          TEXT    NOT NULL DEFAULT '',
                    session_id        TEXT    NOT NULL,
                    channel_type      TEXT    NOT NULL DEFAULT '',
                    title             TEXT    NOT NULL DEFAULT '',
                    context_start_seq INTEGER NOT NULL DEFAULT 0,
                    created_at        INTEGER NOT NULL,
                    last_active       INTEGER NOT NULL,
                    msg_count         INTEGER NOT NULL DEFAULT 0,
                    pinned            INTEGER NOT NULL DEFAULT 0,
                    context_usage     TEXT    NOT NULL DEFAULT '',
                    PRIMARY KEY (agent_id, session_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sessions_new
                    (agent_id, session_id, channel_type, title, context_start_seq,
                     created_at, last_active, msg_count, pinned, context_usage)
                SELECT agent_id, session_id, channel_type, title, context_start_seq,
                       created_at, last_active, msg_count, pinned, context_usage
                FROM sessions
                """
            )
            conn.execute("DROP TABLE sessions")
            conn.execute("ALTER TABLE sessions_new RENAME TO sessions")

            conn.execute(
                """
                CREATE TABLE messages_new (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id     TEXT    NOT NULL DEFAULT '',
                    session_id   TEXT    NOT NULL,
                    seq          INTEGER NOT NULL,
                    role         TEXT    NOT NULL,
                    content      TEXT,
                    content_path TEXT    NOT NULL DEFAULT '',
                    content_anchor TEXT  NOT NULL DEFAULT '',
                    content_hash TEXT    NOT NULL DEFAULT '',
                    content_state TEXT   NOT NULL DEFAULT 'inline',
                    extras_compensated INTEGER NOT NULL DEFAULT 0,
                    created_at   INTEGER NOT NULL,
                    extras       TEXT    NOT NULL DEFAULT '',
                    run_id       TEXT    NOT NULL DEFAULT '',
                    UNIQUE (agent_id, session_id, seq)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO messages_new
                    (id, agent_id, session_id, seq, role, content, content_path,
                     content_anchor, content_hash, content_state,
                     extras_compensated, created_at, extras, run_id)
                SELECT id, agent_id, session_id, seq, role, content,
                       {cols},
                       created_at, extras, run_id
                FROM messages
                """.format(cols=_msg_copy_exprs(conn))
            )
            conn.execute("DROP TABLE messages")
            conn.execute("ALTER TABLE messages_new RENAME TO messages")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session "
                "ON messages (agent_id, session_id, seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_last_active "
                "ON sessions (agent_id, last_active)"
            )

    def _ensure_content_nullable(self, conn: sqlite3.Connection) -> bool:
        """P1 阶段三 M · stage 2: make ``messages.content`` NULLABLE.

        SQLite cannot lift a NOT NULL with ``ALTER TABLE`` — the only way
        is to rebuild the table.  The rebuild is:

          * ONE transaction (裁决 G-05d: ``executescript`` implicitly
            commits, so it must not be used here);
          * the copy preserves every row, including the ``id`` values
            (the ``id`` is the AUTOINCREMENT key and the TTS path's
            snapshot refers to it);
          * the indexes are recreated EXPLICITLY — a renamed table
            loses its indexes, and an index that is silently gone is an
            index that nobody notices until the table is slow.

        **The trigger order matters (the S3-L lesson).**  This table has
        no FTS triggers of its own — the FTS lives on ``chunks`` — so
        there is no ordering hazard HERE.  It is the ``chunks`` side's
        guard that must stay ahead of the FTS triggers, and this method
        does not touch that table.

        Returns True when the table was rebuilt.
        """
        cols = {r[1]: r for r in conn.execute(
            "PRAGMA table_info(messages)").fetchall()}
        if not cols:
            return False
        info = cols.get("content")
        if info is None:
            return False
        # ``notnull`` is 1 when the column carries NOT NULL.
        if not info[3]:
            return False  # already nullable

        logger.info(
            "[ConversationStore] Rebuilding messages with a nullable content "
            "column (P1 阶段三 M · messages 外置)"
        )
        with conn:
            conn.execute("DROP INDEX IF EXISTS idx_messages_session")
            conn.execute(
                """
                CREATE TABLE messages_new (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id     TEXT    NOT NULL DEFAULT '',
                    session_id   TEXT    NOT NULL,
                    seq          INTEGER NOT NULL,
                    role         TEXT    NOT NULL,
                    content      TEXT,
                    content_path TEXT    NOT NULL DEFAULT '',
                    content_anchor TEXT  NOT NULL DEFAULT '',
                    content_hash TEXT    NOT NULL DEFAULT '',
                    content_state TEXT   NOT NULL DEFAULT 'inline',
                    extras_compensated INTEGER NOT NULL DEFAULT 0,
                    created_at   INTEGER NOT NULL,
                    extras       TEXT    NOT NULL DEFAULT '',
                    run_id       TEXT    NOT NULL DEFAULT '',
                    UNIQUE (agent_id, session_id, seq)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO messages_new
                    (id, agent_id, session_id, seq, role, content, content_path,
                     content_anchor, content_hash, content_state,
                     extras_compensated, created_at, extras, run_id)
                SELECT id, agent_id, session_id, seq, role, content,
                       {cols},
                       created_at, extras, run_id
                FROM messages
                """.format(cols=_msg_copy_exprs(conn))
            )
            conn.execute("DROP TABLE messages")
            conn.execute("ALTER TABLE messages_new RENAME TO messages")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session "
                "ON messages (agent_id, session_id, seq)"
            )
        return True

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            self._ensure_schema()
        return self._raw_connect()

    def _raw_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_store_instance: Optional[ConversationStore] = None
_store_instances: Dict[str, ConversationStore] = {}
_store_lock = threading.RLock()


def _default_db_path() -> Path:
    """The one global conversation file: the DEFAULT Agent's ``index.db``.

    Resolved explicitly from the registry's default Agent workspace, never via
    the routing-aware ``get_default_memory_config()`` — that one follows the
    ambient identity, so under ``identity_scope(agent_id=X)`` it would hand back
    Agent X's own file and split X's writes off into its (soon-archived) source
    DB instead of the shared global file. This must be identity-independent.
    """
    from common.utils import expand_path

    try:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
        default_ws = registry.get(require_enabled=False).workspace
        from agent.memory.config import MemoryConfig
        return MemoryConfig(workspace_root=str(default_ws)).get_db_path().resolve()
    except Exception:
        return (
            Path(expand_path("~/cow")) / "memory" / "long-term" / "index.db"
        ).resolve()


def _resolve_store_path(workspace_root=None) -> Path:
    """Legacy per-workspace file path (kept for the registry-less fallback)."""
    if workspace_root is None:
        return _default_db_path()
    from agent.memory.config import MemoryConfig
    from common.utils import expand_path
    workspace = Path(expand_path(str(workspace_root))).resolve()
    return MemoryConfig(workspace_root=str(workspace)).get_db_path().resolve()


def _resolve_global_binding(workspace_root) -> tuple:
    """Map a caller's ``workspace_root`` to ``(db_path, agent_id)`` on the
    global model.

    Every Agent's conversations live in one file — the default Agent's
    ``index.db`` — and are told apart by an ``agent_id`` column. The default
    Agent binds to ``""`` so its historical, un-tagged rows read back
    unchanged (zero migration for the single-Agent majority); every other
    Agent binds to its own id.

    Falls back to the legacy per-workspace file (and ``""`` id) when the
    registry is unavailable — early startup or a test that never built one —
    so nothing depends on registry readiness just to open a store.
    """
    try:
        from agent.registry import get_agent_registry
        from common.utils import expand_path

        registry = get_agent_registry()
        default_id = registry.default_agent_id
        global_path = _default_db_path()

        # No explicit workspace: follow the ambient RuntimeIdentity so the
        # no-arg call is routing-aware (a scheduler tick or channel turn scoped
        # to Agent X sees X's conversations), exactly as it did when the file
        # itself was routed. Absent an identity this resolves to the default.
        if workspace_root is None:
            try:
                from common.runtime_identity import current_identity
                current_agent_id = current_identity().agent_id or ""
            except Exception:
                current_agent_id = ""
            agent_id = "" if (not current_agent_id or current_agent_id == default_id) else current_agent_id
            return global_path, agent_id

        want = str(Path(expand_path(str(workspace_root))).resolve())
        agents = registry.list(include_disabled=True)
        for profile in agents:
            if str(Path(expand_path(str(profile.workspace))).resolve()) == want:
                agent_id = "" if profile.id == default_id else profile.id
                return global_path, agent_id

        # A workspace that no registered Agent claims. On a multi-Agent install
        # we must NOT fall back to that workspace's own file: once the global
        # migration renames its conversation tables aside, a handle still bound
        # to that file would silently recreate empty tables and split new writes
        # off into the archived source (the pm-agent split bug). Bind to the
        # global file instead, deriving the id from the ``agents/<id>/`` layout
        # so the rows still land under the right Agent; unknown -> default ("").
        if len(agents) > 1:
            derived_id = ""
            try:
                parts = Path(want).parts
                if "agents" in parts:
                    idx = parts.index("agents")
                    if idx + 1 < len(parts):
                        candidate = parts[idx + 1]
                        derived_id = "" if candidate == default_id else candidate
            except Exception:
                derived_id = ""
            return global_path, derived_id

        # Genuinely single-Agent and unmatched (e.g. a bespoke test workspace):
        # its own file is safe, nothing will ever archive it.
        return _resolve_store_path(workspace_root), ""
    except Exception:
        return _resolve_store_path(workspace_root), ""


def get_conversation_store(workspace_root=None) -> ConversationStore:
    """
    Return the ConversationStore for one Agent, backed by the one global file.

    All Agents share a single SQLite file (the default Agent's
    ``memory/long-term/index.db``); each returned handle is scoped to its
    Agent by an ``agent_id`` column, so callers keep passing a workspace and
    still see only that Agent's conversations. The default Agent scopes to the
    empty string, so a single-Agent install behaves exactly as before.

    The conversation tables (sessions / messages) share the file with the
    memory tables (memory_chunks / file_metadata) as before.
    """
    global _store_instance
    db_path, agent_id = _resolve_global_binding(workspace_root)
    # Key by (file, agent_id): one handle per Agent even though several share
    # the same physical file.
    key = f"{db_path}::{agent_id}"
    store = _store_instances.get(key)
    if store is not None:
        if workspace_root is None:
            _store_instance = store
        return store

    with _store_lock:
        store = _store_instances.get(key)
        if store is None:
            store = ConversationStore(db_path, agent_id=agent_id)
            _store_instances[key] = store
            logger.debug(
                f"[ConversationStore] Using global DB at: {db_path} (agent_id={agent_id or 'default'})"
            )
        if workspace_root is None:
            _store_instance = store
        return store


def clear_conversation_store_cache() -> None:
    """Forget cached store objects. Intended for config reloads and tests."""
    global _store_instance
    with _store_lock:
        _store_instances.clear()
        _store_instance = None


# ---------------------------------------------------------------------------
# Global migration: fold every Agent's conversations into the one global file
# ---------------------------------------------------------------------------

_CONV_TABLES = ("sessions", "messages", "runs")


def _mark_migration_done(conn: sqlite3.Connection, key: str, value: str = "") -> None:
    conn.execute(_META_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO _migration_meta (key, value, done_at) VALUES (?, ?, ?)",
        (key, value, int(time.time())),
    )


def _migration_done(conn: sqlite3.Connection, key: str) -> bool:
    try:
        conn.execute(_META_DDL)
        row = conn.execute(
            "SELECT 1 FROM _migration_meta WHERE key = ?", (key,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def migrate_conversations_to_global(kickoff_async: bool = True) -> None:
    """Bring every Agent's conversations into the one global file.

    Two levels, matching what each user actually needs:

    * **Level 1 (all users, synchronous, here):** ensure the global file's
      ``sessions``/``messages`` carry an ``agent_id`` column (done by
      ``_init_db``'s ``_migrate``) and — only when more than one Agent exists —
      that both tables use the composite ``(agent_id, session_id)`` key. These
      touch the file the default Agent is about to read/write, so they must be
      finished before traffic flows. They are cheap (metadata add + at most one
      rebuild) and idempotent.

    * **Level 2 (multi-Agent only, asynchronous):** copy each *non-default*
      Agent's conversation rows into the global file, tagged with that Agent's
      id, then rename the source tables aside (never drop). This reads other
      Agents' files and only affects when their history becomes visible in the
      aggregate view, so it runs on a background thread and resumes on the next
      start if interrupted.

    Safe to call any number of times; both levels are guarded by their own
    idempotency checks.
    """
    try:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
    except Exception as e:  # pragma: no cover - registry always present in prod
        logger.debug(f"[ConvMigrate] registry unavailable, skipping: {e}")
        return

    default_store = get_conversation_store()  # binds default file + agent_id=""

    agents = registry.list(include_disabled=True)
    multi_agent = len(agents) > 1

    # Level 1: composite key, only where a second Agent could ever collide.
    if multi_agent:
        with default_store._lock:
            conn = default_store._raw_connect()
            try:
                default_store._ensure_composite_key(conn)
            except Exception as e:
                logger.warning(f"[ConvMigrate] composite-key upgrade failed: {e}")
            finally:
                conn.close()
        # The rebuild swapped the physical tables; drop the cached schema
        # identity so the next query re-validates against the new shape.
        default_store._schema_identity = ()

    if not multi_agent:
        return

    # Drop every cached handle before the merge renames any source tables.
    # A handle that a warmup/first-request opened against a secondary Agent's
    # own file (e.g. a workspace the registry hadn't matched yet) would, once
    # its tables are archived, silently recreate empty tables and split new
    # writes off into the source — the pm-agent split we just diagnosed.
    # Clearing forces every subsequent open to re-resolve to the global file.
    clear_conversation_store_cache()

    if kickoff_async:
        threading.Thread(
            target=_merge_secondary_agents,
            args=(),
            name="conv-merge",
            daemon=True,
        ).start()
    else:
        _merge_secondary_agents()


def _merge_secondary_agents() -> None:
    """Copy each non-default Agent's conversation tables into the global file.

    Per Agent: one transaction, ``INSERT OR IGNORE`` for idempotency, a
    ``_migration_meta`` marker so a restart never repeats it, and a rename of
    the source tables to ``*_migrated_<ts>`` so the rows stay recoverable and a
    lost marker still cannot double-import (the source table is simply gone).
    A failure is logged and isolated to that Agent; the source file keeps
    serving until the next attempt.
    """
    try:
        from agent.registry import get_agent_registry
        from common.utils import expand_path
        registry = get_agent_registry()
    except Exception:
        return

    default_id = registry.default_agent_id
    global_store = get_conversation_store()
    global_path = str(global_store._db_path)

    for profile in registry.list(include_disabled=True):
        if profile.id == default_id:
            continue
        agent_id = profile.id
        try:
            src_path = str(
                Path(expand_path(str(profile.workspace))).resolve()
                / "memory" / "long-term" / "index.db"
            )
        except Exception:
            continue
        if src_path == global_path or not Path(src_path).exists():
            continue

        marker = f"merged_agent:{agent_id}"
        with global_store._lock:
            conn = global_store._raw_connect()
            try:
                if _migration_done(conn, marker):
                    continue
                _merge_one_agent(conn, src_path, agent_id)
                _mark_migration_done(conn, marker, src_path)
                conn.commit()
                logger.info(
                    f"[ConvMigrate] merged conversations for agent '{agent_id}' "
                    f"from {src_path}"
                )
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.warning(
                    f"[ConvMigrate] merge for agent '{agent_id}' failed "
                    f"({e}); source left intact, will retry next start"
                )
            finally:
                conn.close()


def _merge_one_agent(conn: sqlite3.Connection, src_path: str, agent_id: str) -> None:
    """Attach a source Agent DB and copy its conversation rows in, tagged."""
    conn.execute("ATTACH DATABASE ? AS src", (src_path,))
    try:
        src_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM src.sqlite_master WHERE type='table'"
            ).fetchall()
        }
        with conn:
            if "sessions" in src_tables:
                session_cols = {
                    row[1] for row in conn.execute("PRAGMA src.table_info(sessions)")
                }
                # Secondary workspaces have not passed through _migrate():
                # opening a store already resolves to the global file. Supply
                # the same defaults for metadata added by skipped releases.
                optional_session_cols = ", ".join(
                    name if name in session_cols else default
                    for name, default in (
                        ("channel_type", "''"), ("title", "''"),
                        ("context_start_seq", "0"),
                    )
                )
                pinned = "pinned" if "pinned" in session_cols else "0"
                context_usage = (
                    "context_usage" if "context_usage" in session_cols else "''"
                )
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO sessions
                        (agent_id, session_id, channel_type, title, context_start_seq,
                         created_at, last_active, msg_count, pinned, context_usage)
                    SELECT ?, session_id, {optional_session_cols},
                           created_at, last_active, msg_count, {pinned}, {context_usage}
                    FROM src.sessions
                    """,
                    (agent_id,),
                )
            if "messages" in src_tables:
                message_cols = {
                    row[1] for row in conn.execute("PRAGMA src.table_info(messages)")
                }
                extras = "COALESCE(extras, '')" if "extras" in message_cols else "''"
                run_id = "COALESCE(run_id, '')" if "run_id" in message_cols else "''"
                # id -> NULL so the global file re-issues AUTOINCREMENT ids and
                # cross-file ids never collide; dedupe is on (agent_id, session_id, seq).
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO messages
                        (agent_id, session_id, seq, role, content, created_at, extras, run_id)
                    SELECT ?, session_id, seq, role, content, created_at,
                           {extras}, {run_id}
                    FROM src.messages
                    """,
                    (agent_id,),
                )
            if "runs" in src_tables:
                # runs already carry agent_id (uuid run_id never collides);
                # backfill only the empties to this source's owner.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runs
                        (run_id, agent_id, user_id, session_id, parent_run_id,
                         task_id, task_source, status, started_at, ended_at, error, extras)
                    SELECT run_id,
                           CASE WHEN COALESCE(agent_id, '') = '' THEN ? ELSE agent_id END,
                           user_id, session_id, parent_run_id,
                           task_id, task_source, status, started_at, ended_at,
                           COALESCE(error, ''), COALESCE(extras, '')
                    FROM src.runs
                    """,
                    (agent_id,),
                )
    finally:
        conn.execute("DETACH DATABASE src")

    # Rename the source tables aside so the rows survive but can never be
    # re-imported. Best-effort and outside the copy transaction.
    ts = int(time.time())
    src_conn = sqlite3.connect(src_path, timeout=10)
    try:
        existing = {
            row[0]
            for row in src_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        with src_conn:
            for table in _CONV_TABLES:
                if table in existing:
                    src_conn.execute(
                        f"ALTER TABLE {table} RENAME TO {table}_migrated_{ts}"
                    )
    except Exception as e:
        logger.warning(
            f"[ConvMigrate] could not archive source tables in {src_path}: {e}"
        )
    finally:
        src_conn.close()
