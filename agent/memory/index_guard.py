"""The write-side guard — a row that must not be public cannot become public.

**Why this module exists (P0-21).** The desktop build of 2026-10-13 is still
running on this machine and it shares the very index database this code
writes. Its ``MemoryManager.sync()`` knows nothing about ``visibility:
sealed``: it walks ``memory/**/*.md`` and writes every file it finds as
``scope='shared'`` with ``user_id=NULL`` — which is exactly the shape an
ANONYMOUS caller reads. Measured with the old code on a throwaway
workspace: its own anonymous search returned the dark page's front-matter
and body.

That old binary cannot be patched — it is a signed, shipped build. So the
invariant has to be enforced where it can still be enforced: **at the
database's write boundary**, by a trigger that every writer goes through,
including the old one.

**What the guard enforces.** A chunk row may never be written in a shape
that makes it public unless the truth layer says it is public:

* a dark page (``visibility: sealed``) is written under ``scope='sealed'``
  with its owner token, so only its owner can read it;
* a released element (``status=released`` / ``type=release``) is written
  with ``element_released=1``, which takes it out of EVERY result set;
* a path under an excluded layer (``shelf/``, ``facts/``, ``_tombstones/``,
  ``.audit/``, ``.snapshots/``) is never written as public.

**Why a BEFORE trigger and not an AFTER trigger.** An AFTER trigger that
updated the row would fire the FTS5 sync trigger on a row the FTS index
does not have yet, and the resulting ``'delete'`` of a missing FTS rowid
corrupts the index — measured: ``database disk image is malformed``. The
FTS5 sync triggers fire in reverse creation order, so an AFTER guard is
only safe when it happens to be the older trigger. A BEFORE trigger that
inserts the corrected row and then raises ``IGNORE`` does not depend on
trigger order at all: the corrected INSERT fires the FTS trigger once,
with the corrected scope already in place.

**Two ways a row is recognised as non-public.** The guard table
(``guard_paths``) is the truth layer's own record — the new code writes it
during sync, so it is authoritative and carries the owner token. The
chunk text's front-matter is the fallback for the window in which the old
code indexes a dark page before the new code has ever synced it: the
trigger recognises the front-matter, seals the row, and *records the path
in the guard table* so the remaining chunks of the same file (which do not
carry the front-matter) are sealed too.

The guard only ever makes a row LESS visible. It never widens access, and
it never touches the truth layer: the bytes on disk are the truth, and
this is the projection being held to it.

**A discard must be VISIBLE (P0-22).** ``RAISE(IGNORE)`` is a silent
operation: the writer's statement is dropped, the writer sees no error, and
the row it tried to publish is simply not there. That is the whole point of
the guard — but it is also the failure mode 风控 named: "把旧的『静默失败』
换了个地方继续存在". A safety mechanism that corrects the data AFTER the
write and says nothing leaves the two sides with two different beliefs
about the same row, and neither side knows.

So the trigger RECORDS every discard before it raises: one row in
``guard_discards`` (which path, which chunk, which class, when) and two
counters in ``guard_state`` (``discard_count`` and ``last_discard_at``).
The counters advance on every discard, so "was anything rewritten lately"
is a question with an answer — and the application layer (not the trigger)
is what turns that answer into an ERROR log, an operator-facing report
and an inspector finding. The trigger only records; the application layer
speaks.

**The guard must be a MECHANISM, not a snapshot (P0-22).** A roster that
misses one workspace means one index runs unguarded, and nobody notices
because "the guard is installed" was a thing somebody did once. So:

* ``ensure_guard()`` reports whether the guard is ``present`` / ``installed``
  / ``repaired`` / ``failed`` — "already installed" and "could not be
  installed" are different answers and must never be the same one;
* the trigger body is compared against the current definition, so a stale
  guard (or a deleted one) is detected and repaired instead of being
  silently accepted;
* ``discover_index_roots()`` finds every index that ACTUALLY exists on
  disk, not only the ones the roster happens to name — the roster missed
  ``agents/default`` once already.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# The classes a guarded path can have. They mirror the visibility classes
# in ``agent.memory.visibility`` plus one for the excluded layers.
CLASS_SEALED = "sealed"      # 暗页：只对本视角与主人格开放
CLASS_RELEASED = "released"  # 已释放：对所有身份不可见
CLASS_EXCLUDED = "excluded"  # 排除层（shelf/facts/墓碑/审计）：不进索引

GUARD_TABLE = "guard_paths"

#: P0-22: the audit trail of the discards. One row per dropped write.
GUARD_DISCARD_TABLE = "guard_discards"

#: P0-22: the guard's own bookkeeping — the counters that advance on every
#: discard. A SEPARATE table from ``guard_paths`` on purpose: that table is
#: the truth layer's record of WHICH paths are guarded, and a counter that
#: moves on every discard would make it a moving target.
GUARD_STATE_TABLE = "guard_state"

#: The two triggers. Their names are the guard's identity: the inspector
#: and the self-heal both look for exactly these.
GUARD_TRIGGERS = ("chunks_guard_bi", "chunks_guard_bu")

#: The keys in ``guard_state``.
STATE_DISCARD_COUNT = "discard_count"
STATE_LAST_DISCARD_AT = "last_discard_at"

# The layers the index must never carry. Reuses the ONE definition the
# scan, the inspector and the evolution watcher already share
# (``agent.memory.layers``) — a second copy here is how P0-11 happened.
def _excluded_segments() -> Tuple[str, ...]:
    from agent.memory.layers import EXCLUDED_INDEX_SEGMENTS

    return tuple(EXCLUDED_INDEX_SEGMENTS)


_NL = "char(10)"


def _fm_expr(col: str) -> str:
    """SQL expression: the front-matter block of ``col``, or '' when absent.

    A markdown element opens its front-matter with ``---`` on its own line.
    The block ends at the next ``---`` line. The expression refuses to
    guess when the closing marker is missing, because a half-read
    front-matter is a visibility we cannot know.
    """
    return (
        f"CASE WHEN {col} LIKE '---' || {_NL} || '%' "
        f"AND instr({col}, {_NL} || '---') > 0 "
        f"THEN substr({col}, 1, instr({col}, {_NL} || '---') + 4) "
        f"ELSE '' END"
    )


def _path_has_segment(path_col: str, segment: str) -> str:
    """SQL expression: does ``path_col`` sit under a ``segment`` DIRECTORY?

    Per component, never per substring: a truth-layer file called
    ``含shelf的事件--a1b2c3d4.md`` is a legal element name and must not be
    mistaken for the shelf layer.
    """
    padded = f"('/' || {path_col} || '/')"
    return f"({padded} LIKE '%/{segment}/%')"


def _guard_says(path_col: str, klass: str) -> str:
    return (f"EXISTS (SELECT 1 FROM {GUARD_TABLE} g "
            f"WHERE g.path = {path_col} AND g.class = '{klass}')")


def _klass_expr(released_expr: str, sealed_expr: str) -> str:
    """SQL expression: which class the discarded row belongs to.

    The order matters: a release wins over a dark page (the release hides
    the element from EVERYONE, the dark page hides it from everyone but its
    owner), and anything else that reached the guard is an excluded-layer
    path.
    """
    return (f"CASE WHEN {released_expr} THEN '{CLASS_RELEASED}' "
            f"WHEN {sealed_expr} THEN '{CLASS_SEALED}' "
            f"ELSE '{CLASS_EXCLUDED}' END")


def _discard_recording(op: str, klass_expr: str) -> str:
    """The statements that make a discard visible (P0-22).

    Three statements, in this order:

    1. one row in ``guard_discards`` — WHO was rewritten: the path, the
       chunk id, the class, the scope it tried to have;
    2. ``guard_state.discard_count`` advances — the counter is what the
       application layer reads to answer "did this sync drop anything";
    3. ``guard_state.last_discard_at`` moves — the timestamp is what makes
       "how many rows were rewritten in the last N minutes" answerable.

    The counter is incremented with an explicit CAST because SQLite stores
    the value as TEXT (the table is a key/value store) and `'9' + 1` is
    '91', not 10.
    """
    return (
        f"INSERT INTO {GUARD_DISCARD_TABLE} "
        f"(at, op, chunk_id, path, klass, scope, user_id) "
        f"VALUES (datetime('now'), '{op}', NEW.id, NEW.path, "
        f"{klass_expr}, NEW.scope, NEW.user_id);"
        f"INSERT INTO {GUARD_STATE_TABLE} (key, value) "
        f"VALUES ('{STATE_DISCARD_COUNT}', '1') "
        f"ON CONFLICT(key) DO UPDATE SET "
        f"value = CAST(CAST(value AS INTEGER) + 1 AS TEXT);"
        f"INSERT INTO {GUARD_STATE_TABLE} (key, value) "
        f"VALUES ('{STATE_LAST_DISCARD_AT}', datetime('now')) "
        f"ON CONFLICT(key) DO UPDATE SET value = excluded.value;"
    )


def _build_trigger_sql() -> List[str]:
    """The two trigger statements (BEFORE INSERT and BEFORE UPDATE).

    Kept in one place so the two can never drift: the UPDATE variant
    exists because the old code's ``save_chunk`` upserts, and an upsert of
    an existing row fires UPDATE, not INSERT.

    The statements are written WITHOUT ``IF NOT EXISTS`` on purpose: the
    self-heal compares the trigger body SQLite stored against the body
    built here, and SQLite strips ``IF NOT EXISTS`` when it stores it —
    so writing it out keeps the comparison exact.
    """
    segs = _excluded_segments()
    excluded_by_path = " OR ".join(
        _path_has_segment("NEW.path", s) for s in segs)

    fm = _fm_expr("NEW.text")
    sealed_by_text = f"({fm} LIKE '%visibility: sealed%')"
    released_by_text = (f"({fm} LIKE '%status: released%' "
                        f"OR {fm} LIKE '%type: release%')")

    sealed_by_guard = _guard_says("NEW.path", CLASS_SEALED)
    released_by_guard = _guard_says("NEW.path", CLASS_RELEASED)
    excluded_by_guard = _guard_says("NEW.path", CLASS_EXCLUDED)

    # The trigger only fires on a row that is trying to be PUBLIC. A row
    # that already carries a non-public scope is none of its business.
    when = (
        "NEW.scope = 'shared' AND ("
        f"{sealed_by_text} OR {released_by_text} OR "
        f"({excluded_by_path}) OR "
        f"{sealed_by_guard} OR {released_by_guard} OR {excluded_by_guard})"
    )

    # The corrective row: scope 'sealed' for every guarded class, because
    # 'sealed' with no owner token is invisible to EVERYONE (visibility
    # rule 1 + rule 8) — the safe reading of "we cannot tell who may see
    # this". A released element additionally carries element_released=1,
    # which takes it out of every result set even if a future scope value
    # is introduced.
    corrective_values = (
        "NEW.id, NULL, 'sealed', NEW.source, NEW.path, NEW.start_line, "
        "NEW.end_line, NEW.text, NEW.embedding, NEW.hash, NEW.metadata, "
        "NEW.updated_at, 1, "
        f"CASE WHEN {released_by_text} OR {released_by_guard} THEN 1 ELSE 0 END, "
        # The owner token is taken from the guard table when the truth
        # layer has recorded it. The front-matter fallback does NOT guess
        # an owner: a dark page whose owner is unknown is invisible to
        # everybody, and the next sync by this code fills the owner in.
        f"CASE WHEN {sealed_by_guard} "
        f"THEN (SELECT g.owner_token FROM {GUARD_TABLE} g "
        f"      WHERE g.path = NEW.path AND g.class = '{CLASS_SEALED}') "
        f"ELSE NULL END"
    )

    # Self-registration: a dark page whose chunks are indexed BEFORE this
    # code ever synced it must still have every one of its chunks sealed,
    # and only the first chunk carries the front-matter. Recording the
    # path here is what lets the trigger recognise the rest.
    register = (
        f"INSERT OR IGNORE INTO {GUARD_TABLE} (path, class, owner_token, updated_at) "
        f"SELECT NEW.path, '{CLASS_SEALED}', NULL, datetime('now') "
        f"WHERE {sealed_by_text};"
    )
    register_released = (
        f"INSERT OR IGNORE INTO {GUARD_TABLE} (path, class, owner_token, updated_at) "
        f"SELECT NEW.path, '{CLASS_RELEASED}', NULL, datetime('now') "
        f"WHERE {released_by_text};"
    )
    register_excluded = (
        f"INSERT OR IGNORE INTO {GUARD_TABLE} (path, class, owner_token, updated_at) "
        f"SELECT NEW.path, '{CLASS_EXCLUDED}', NULL, datetime('now') "
        f"WHERE {excluded_by_path};"
    )

    klass = _klass_expr(
        f"({released_by_text} OR {released_by_guard})",
        f"({sealed_by_text} OR {sealed_by_guard})",
    )

    cols = ("id, user_id, scope, source, path, start_line, end_line, text, "
            "embedding, hash, metadata, updated_at, file_sealed, "
            "element_released, sealed_owner")

    insert_trigger = f"""
CREATE TRIGGER {GUARD_TRIGGERS[0]} BEFORE INSERT ON chunks
WHEN {when}
 AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id = NEW.id)
BEGIN
    {register}
    {register_released}
    {register_excluded}
    {_discard_recording('insert', klass)}
    INSERT INTO chunks ({cols})
    VALUES ({corrective_values});
    SELECT RAISE(IGNORE);
END"""

    update_trigger = f"""
CREATE TRIGGER {GUARD_TRIGGERS[1]} BEFORE UPDATE ON chunks
WHEN {when}
BEGIN
    {register}
    {register_released}
    {register_excluded}
    {_discard_recording('update', klass)}
    UPDATE chunks SET
        user_id = NULL,
        scope = 'sealed',
        file_sealed = 1,
        element_released = CASE WHEN {released_by_text} OR {released_by_guard}
                                THEN 1 ELSE 0 END,
        sealed_owner = CASE WHEN {sealed_by_guard}
                            THEN (SELECT g.owner_token FROM {GUARD_TABLE} g
                                  WHERE g.path = NEW.path AND g.class = '{CLASS_SEALED}')
                            ELSE NULL END
    WHERE id = NEW.id;
    SELECT RAISE(IGNORE);
END"""
    return [insert_trigger, update_trigger]


TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {GUARD_TABLE} (
    path TEXT PRIMARY KEY,
    class TEXT NOT NULL,
    owner_token TEXT,
    updated_at TEXT
)"""

#: P0-22: the discard audit trail. Append-only, never rewritten — the
#: point of a trail is that it cannot be laundered.
DISCARD_DDL = f"""
CREATE TABLE IF NOT EXISTS {GUARD_DISCARD_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    op TEXT NOT NULL,
    chunk_id TEXT,
    path TEXT,
    klass TEXT,
    scope TEXT,
    user_id TEXT
)"""

#: P0-22: the guard's counters.
STATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {GUARD_STATE_TABLE} (
    key TEXT PRIMARY KEY,
    value TEXT
)"""


def _normalize_sql(sql: Optional[str]) -> str:
    """Whitespace-normalised SQL, for comparing two trigger bodies.

    SQLite stores a trigger's body verbatim (minus ``IF NOT EXISTS``), so
    the comparison is exact after the whitespace is collapsed. Comparing
    the TEXT is what makes "the guard is installed but it is the OLD guard"
    detectable — ``CREATE TRIGGER IF NOT EXISTS`` never raises, so a stale
    trigger is otherwise accepted forever.
    """
    if not sql:
        return ""
    return re.sub(r"\s+", " ", sql).strip()


def trigger_sqls(conn: sqlite3.Connection) -> Dict[str, str]:
    """The stored SQL of the two guard triggers (missing → '')."""
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN (?, ?)", GUARD_TRIGGERS).fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: (row[1] or "") for row in rows}


def is_current(conn: sqlite3.Connection) -> bool:
    """Whether both guard triggers are present AND match the definition.

    "Installed" and "up to date" are two different questions: a trigger
    left over from an older build is installed but does not record the
    discards (P0-22), so the patrol must be able to tell them apart.
    """
    stored = trigger_sqls(conn)
    if not all(name in stored for name in GUARD_TRIGGERS):
        return False
    expected = _build_trigger_sql()
    for name, want in zip(GUARD_TRIGGERS, expected):
        if _normalize_sql(stored.get(name)) != _normalize_sql(want):
            return False
    return True


def ensure_guard(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Make sure the guard is present, current, and say which it is.

    Returns a dict with:

    * ``status`` — ``present`` (already there and current), ``installed``
      (there now, it was not), ``repaired`` (there but stale — the body
      did not match the current definition), ``failed`` (could not be
      installed, with ``reason``);
    * ``reason`` — why it failed, when it failed.

    "Already installed" and "could not be installed" must never be the same
    answer: the first is a fact about the index, the second is a fact about
    the system's safety, and the patrol needs to be able to tell them
    apart.
    """
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    except sqlite3.Error as e:
        return {"status": "failed", "reason": f"cannot read chunks: {e}"}
    if not columns:
        return {"status": "failed",
                "reason": "the database has no chunks table yet"}

    has_flags = {"file_sealed", "element_released"} <= columns
    has_owner = "sealed_owner" in columns
    if not (has_flags and has_owner):
        # The corrective INSERT names the projection columns. On a database
        # that predates them the guard would fail to install, and a guard
        # that fails to install is a guard that does not exist — so the
        # flags are added here, the same way the migration adds them.
        try:
            for name, decl in (("file_sealed", "INTEGER DEFAULT 0"),
                               ("element_released", "INTEGER DEFAULT 0"),
                               ("sealed_owner", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {decl}")
        except sqlite3.Error as e:
            return {"status": "failed",
                    "reason": f"cannot add the projection columns: {e}"}

    if is_current(conn):
        return {"status": "present"}

    stored = trigger_sqls(conn)
    existed = all(name in stored for name in GUARD_TRIGGERS)
    statements = _build_trigger_sql()
    try:
        # The drop-and-create must be ATOMIC: a guard that is absent for
        # even a moment is a window in which the old build can write a
        # public row. ``BEGIN IMMEDIATE`` takes the write lock up front,
        # so the swap is invisible to every other writer.
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(TABLE_DDL)
        conn.execute(DISCARD_DDL)
        conn.execute(STATE_DDL)
        for name in GUARD_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"status": "failed", "reason": f"{type(e).__name__}: {e}"}
    return {"status": "repaired" if existed else "installed"}


def install_guard(conn: sqlite3.Connection) -> bool:
    """Install the guard table and its two triggers. Idempotent.

    Returns True when the guard is in place. It never raises on a
    database that lacks the projection columns: the trigger is then
    installed without them, so a half-migrated index is still guarded on
    the axis that matters (scope) instead of crashing the writer.
    """
    return ensure_guard(conn).get("status") != "failed"


def record_paths(conn: sqlite3.Connection,
                 entries: Iterable[Tuple[str, str, Optional[str]]]) -> int:
    """Record guarded paths (path, class, owner_token) in the guard table.

    The truth layer is the authority for what is sealed and who owns it.
    Recording it here means the guard does not have to re-derive it from
    a chunk's text — and it carries the OWNER, which the text fallback
    deliberately does not guess.
    """
    rows = [(p, c, o) for (p, c, o) in entries if p and c]
    if not rows:
        return 0
    conn.executemany(
        f"INSERT INTO {GUARD_TABLE} (path, class, owner_token, updated_at) "
        f"VALUES (?, ?, ?, datetime('now')) "
        f"ON CONFLICT(path) DO UPDATE SET "
        f"class = excluded.class, owner_token = excluded.owner_token, "
        f"updated_at = excluded.updated_at",
        rows)
    conn.commit()
    return len(rows)


def guarded_paths(conn: sqlite3.Connection) -> Dict[str, Tuple[str, Optional[str]]]:
    """Every guarded path -> (class, owner_token)."""
    try:
        rows = conn.execute(
            f"SELECT path, class, owner_token FROM {GUARD_TABLE}").fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    for row in rows:
        out[row[0]] = (row[1], row[2])
    return out


def is_installed(conn: sqlite3.Connection) -> bool:
    """Whether both guard triggers are present."""
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
    except sqlite3.Error:
        return False
    return set(GUARD_TRIGGERS) <= names


def public_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Rows that claim to be public but are guarded — the leak, spelled out.

    Used by the inspector's check: a guarded path whose row is still a
    public shape is the exact failure the guard exists to prevent.
    """
    sql = f"""
    SELECT c.path, c.scope, c.user_id, c.file_sealed, c.element_released,
           c.sealed_owner, g.class AS guard_class, g.owner_token AS guard_owner
    FROM chunks c
    JOIN {GUARD_TABLE} g ON g.path = c.path
    WHERE c.scope = 'shared' AND c.user_id IS NULL
    """
    try:
        return list(conn.execute(sql))
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# P0-22: the discard trail — the guard's own answers to "what did you do?"
# ---------------------------------------------------------------------------

def _state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute(
            f"SELECT value FROM {GUARD_STATE_TABLE} WHERE key = ?",
            (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def discard_count(conn: sqlite3.Connection) -> int:
    """How many writes the guard has dropped since the index was created."""
    raw = _state(conn, STATE_DISCARD_COUNT)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def last_discard_at(conn: sqlite3.Connection) -> Optional[str]:
    """When the guard last dropped a write."""
    return _state(conn, STATE_LAST_DISCARD_AT)


def recent_discards(conn: sqlite3.Connection,
                    minutes: int = 60,
                    limit: int = 200) -> List[Dict[str, Any]]:
    """The discards of the last ``minutes`` minutes, newest first.

    The rows come from the trail the trigger writes; the window is applied
    in SQL so the answer does not depend on how many rows the trail holds.
    """
    try:
        rows = conn.execute(
            f"SELECT at, op, chunk_id, path, klass, scope, user_id "
            f"FROM {GUARD_DISCARD_TABLE} "
            f"WHERE at >= datetime('now', ?) "
            f"ORDER BY id DESC LIMIT ?",
            (f"-{int(minutes)} minutes", int(limit))).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"at": r[0], "op": r[1], "chunk_id": r[2], "path": r[3],
         "class": r[4], "scope": r[5], "user_id": r[6]}
        for r in rows
    ]


def guard_report(conn: sqlite3.Connection,
                 minutes: int = 60) -> Dict[str, Any]:
    """The operator-facing answer to "what did the guard drop?".

    * ``blocked_count`` — the total number of dropped writes;
    * ``blocked_recent`` — the ones of the last ``minutes`` minutes;
    * ``last_at`` — when the last one happened;
    * ``paths`` — WHICH paths were rewritten (the distinct paths, with how
      many times each).

    This is the interface the 交办单 asks for: "这次 sync 有多少行被守卫挡
    下了？挡了哪些？" — answered from the index itself, so the answer does
    not depend on anybody having been watching the log.
    """
    recent = recent_discards(conn, minutes=minutes)
    by_path: Dict[str, int] = {}
    for item in recent:
        by_path[item["path"] or "?"] = by_path.get(item["path"] or "?", 0) + 1
    return {
        "blocked_count": discard_count(conn),
        "blocked_recent": len(recent),
        "window_minutes": int(minutes),
        "last_at": last_discard_at(conn),
        "paths": [{"path": p, "count": n} for p, n in
                  sorted(by_path.items(), key=lambda kv: (-kv[1], kv[0]))],
        "events": recent,
    }


def guard_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The guard's health, as the patrol sees it.

    ``installed`` / ``current`` / ``reason`` — the three answers the
    inspector needs, without the caller having to know how the guard is
    built.
    """
    stored = trigger_sqls(conn)
    missing = [n for n in GUARD_TRIGGERS if n not in stored]
    if missing:
        return {"installed": False, "current": False,
                "missing": missing, "reason": "missing trigger(s)"}
    if not is_current(conn):
        return {"installed": True, "current": False, "missing": [],
                "reason": "the installed trigger is not the current definition"}
    return {"installed": True, "current": True, "missing": [], "reason": ""}


# ---------------------------------------------------------------------------
# P0-22: the mechanism — every index that ACTUALLY exists
# ---------------------------------------------------------------------------

def index_path_for(root: Path | str) -> Path:
    """Where one workspace keeps its index (the layout the state dir uses)."""
    return Path(root) / "memory" / "long-term" / "index.db"


def discover_index_roots(extra: Path | str | None = None) -> List[str]:
    """Every workspace root whose index EXISTS on disk.

    Two sources, and the second one is the point:

    1. the ROSTER (``agent.registry``) — it knows every agent and where its
       workspace lives;
    2. the DIRECTORY LAYOUT — every ``<shared root>/agents/*/`` that has a
       ``memory/long-term/index.db``, plus the shared root's own index.

    The roster alone is not enough: ``agents/default`` is not in it, so a
    roster-only sweep leaves that index unguarded, and "the roster missed
    one" is indistinguishable from "everything is guarded" unless the
    sweep looks at the disk. The layout is the ground truth for "which
    indexes EXIST"; the roster only says which workspaces are supposed to
    exist.

    The result is deduplicated and keeps its order.
    """
    roots: List[str] = []

    def _add(value: Path | str) -> None:
        text = str(value)
        if text and text not in roots:
            roots.append(text)

    try:
        from agent.registry import get_agent_registry

        for profile in get_agent_registry().list(include_disabled=True):
            _add(profile.workspace)
    except Exception:
        pass

    try:
        from common import state_dir

        shared = Path(state_dir.shared_root())
        _add(shared)
        agents_dir = shared / "agents"
        if agents_dir.is_dir():
            for child in sorted(agents_dir.iterdir()):
                if child.is_dir():
                    _add(child)
    except Exception:
        pass

    if extra is not None:
        _add(extra)

    # Only the roots that really have an index.
    return [r for r in roots if index_path_for(r).is_file()]
