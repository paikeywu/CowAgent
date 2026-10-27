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
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# The classes a guarded path can have. They mirror the visibility classes
# in ``agent.memory.visibility`` plus one for the excluded layers.
CLASS_SEALED = "sealed"      # 暗页：只对本视角与主人格开放
CLASS_RELEASED = "released"  # 已释放：对所有身份不可见
CLASS_EXCLUDED = "excluded"  # 排除层（shelf/facts/墓碑/审计）：不进索引

GUARD_TABLE = "guard_paths"

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


def _build_trigger_sql() -> List[str]:
    """The two trigger statements (BEFORE INSERT and BEFORE UPDATE).

    Kept in one place so the two can never drift: the UPDATE variant
    exists because the old code's ``save_chunk`` upserts, and an upsert of
    an existing row fires UPDATE, not INSERT.
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

    cols = ("id, user_id, scope, source, path, start_line, end_line, text, "
            "embedding, hash, metadata, updated_at, file_sealed, "
            "element_released, sealed_owner")

    insert_trigger = f"""
CREATE TRIGGER IF NOT EXISTS chunks_guard_bi BEFORE INSERT ON chunks
WHEN {when}
 AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id = NEW.id)
BEGIN
    {register}
    {register_released}
    {register_excluded}
    INSERT INTO chunks ({cols})
    VALUES ({corrective_values});
    SELECT RAISE(IGNORE);
END"""

    update_trigger = f"""
CREATE TRIGGER IF NOT EXISTS chunks_guard_bu BEFORE UPDATE ON chunks
WHEN {when}
BEGIN
    {register}
    {register_released}
    {register_excluded}
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


def install_guard(conn: sqlite3.Connection) -> bool:
    """Install the guard table and its two triggers. Idempotent.

    Returns True when the guard is in place. It never raises on a
    database that lacks the projection columns: the trigger is then
    installed without them, so a half-migrated index is still guarded on
    the axis that matters (scope) instead of crashing the writer.
    """
    conn.execute(TABLE_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if not columns:
        return False
    has_flags = {"file_sealed", "element_released"} <= columns
    has_owner = "sealed_owner" in columns
    if not (has_flags and has_owner):
        # The corrective INSERT names the projection columns. On a database
        # that predates them the guard would fail to install, and a guard
        # that fails to install is a guard that does not exist — so the
        # flags are added here, the same way the migration adds them.
        for name, decl in (("file_sealed", "INTEGER DEFAULT 0"),
                           ("element_released", "INTEGER DEFAULT 0"),
                           ("sealed_owner", "TEXT")):
            if name not in columns:
                conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {decl}")
    for statement in _build_trigger_sql():
        conn.execute(statement)
    conn.commit()
    return True


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
    return {"chunks_guard_bi", "chunks_guard_bu"} <= names


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
