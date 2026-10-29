#!/usr/bin/env python3
# encoding:utf-8
"""The body-column guard — the write-boundary rule that no writer can use
to ERASE a certified pointer or a certified state.

P1-阶段三 第五轮 (F-4) created this module; P1-阶段三 第六轮 (S3-I)
**replaced its rules**, because the fifth round's rules were measured to
be too narrow.  This docstring records the measured facts, because the
rules below are not a design — they are what the measurements forced.

------------------------------------------------------------------
WHAT THE MEASUREMENTS SAID (S3-I, on this machine)
------------------------------------------------------------------

**Fact 1 — the legacy build has THREE write shapes, and the fifth
round's guard only covered two of them.**  From
``git show 254868ef:agent/memory/storage.py`` (the 2026-10-13 build that
is still running as PID 5488)::

    INSERT OR REPLACE INTO chunks (18 columns, no body columns)
    INSERT INTO chunks (12 columns) ON CONFLICT(id) DO UPDATE SET
        (11 columns, none of them a body column)
    DELETE FROM chunks WHERE path = ?   +   INSERT OR REPLACE

The fifth round's ``BEFORE INSERT`` trigger keyed on
``NEW.body_path IS NULL``.  Measured on a copy of the live ROOT library:
the ``ON CONFLICT`` shape does **not** reach that trigger's WHEN clause
(the row already has a pointer), and the fifth round had no
``BEFORE UPDATE`` trigger, so **nothing** stood between that shape and
the stored row.

**Fact 2 — the ``text_state`` downgrade is NOT the legacy build's doing.
It is OURS.**  Measured on a copy of the live ROOT library, using the
REAL ``MemoryStorage`` methods (not hand-written SQL — a hand-written
statement produced a false root cause first, and it was thrown away):

    real ``save_chunks_batch`` (manager.py:889)   -> 'mirror' KEPT
        (the anti-downgrade CASE in ``BODY_COLUMN_UPSERT_SQL`` works)
    real ``replace_file_chunks`` (rebuilder.py:294) -> 'mirror' LOST

The reason is structural: ``replace_file_chunks`` is a **DELETE +
INSERT**.  The DELETE destroys the row; the INSERT brings a row whose
``text_state`` is ``'projection'`` — because
``body_pointer.four_columns_for_chunk`` returns ``STATE_PROJECTION``
for every chunk it builds (it decides the POINTER, never the state).
The inspector's ``'mirror'`` verdict is therefore overwritten by our
own rebuild, on every rebuild.

And the fifth round's guard could not see it: its ``BEFORE INSERT``
trigger fired only when ``NEW.body_path IS NULL``, and our writer
brings a **non-NULL** ``body_path`` — so the trigger never fired, and
the row landed with ``text_state='projection'``.

**Fact 3 — the SQLite semantics that decide each shape's mechanism.**
Every one of these was measured on this machine:

    BEFORE INSERT: the stored row is STILL VISIBLE inside the trigger
        ("BEFORE INSERT sees: ORIGINAL")
    BEFORE INSERT + INSERT OR REPLACE: by the time the trigger body's
        SET list is evaluated, the row has been REPLACED — a subquery
        there sees the NEW row, not the old one
    BEFORE INSERT: an inner UPDATE that does not NAME a column leaves
        that column's stored value alone
    BEFORE UPDATE: an inner UPDATE is OVERWRITTEN by the outer
        statement (so it must be followed by RAISE(IGNORE))
    BEFORE UPDATE: a nested statement may not reference OLD or NEW
        ("no such column: OLD.body_path")
    AFTER INSERT: an inner UPDATE lands normally
    ON CONFLICT(id) DO UPDATE: DOES fire BEFORE INSERT

So each shape needs its OWN mechanism:

  * ``INSERT OR REPLACE`` — the stored row is visible, so the trigger
    snapshots it into the keep table and restores from the snapshot.
  * ``ON CONFLICT DO UPDATE`` — the row is updated in place, so a
    ``BEFORE UPDATE`` trigger restores the stored state from its own
    snapshot.
  * ``DELETE`` + re-``INSERT`` — the row is GONE before the INSERT
    arrives, so the ``BEFORE DELETE`` trigger snapshots it, and the
    ``AFTER INSERT`` trigger restores it.

**Fact 4 — the ``files`` table was never guarded at all.**  The
fifth round's keep table carried only the ``chunks`` columns.  The
legacy build's ``INSERT OR REPLACE INTO files`` names no statistics
column, so ``chunk_count``/``chunker_version`` fall back to their
DEFAULT — measured on a live library: ``(1, 4) -> (0, 0)`` on a
``NOT NULL DEFAULT 0`` schema, and ``NULL`` on a schema where the
columns were added by ``ALTER TABLE ADD COLUMN``.

------------------------------------------------------------------
THE RULES (each one answers a measured gap)
------------------------------------------------------------------

**Rule A — a certified STATE is never overwritten by an undecided one.**
The fifth round already knew that ``'projection'`` and ``'unresolved'``
are the "the writer did not decide" values
(:data:`UNDECIDED_VALUES`).  The fifth round applied that rule only to
the *restore* path; it did not apply it to the *incoming* row.  Now the
``BEFORE INSERT``/``BEFORE UPDATE`` triggers recognise an incoming
``text_state='projection'`` / ``body_origin='unresolved'`` against a
stored, DECIDED value, and keep the stored one.

**Rule B — the restore source is the STORED ROW, not a side table.**
For the INSERT and UPDATE shapes the stored row still holds its
previous state when the trigger runs (measured), so the trigger
snapshots it into the keep table and restores from the snapshot.  The
keep table is needed only for the DELETE-then-INSERT shape, where the
row is gone.

**Rule C — the ``files`` statistics are guarded too.**  The real key of
``files`` is ``path`` (PRIMARY KEY; measured with ``PRAGMA
table_info``) — not ``id``.  A ``files`` row whose statistics were
measured must not lose them to a writer that does not name the
columns.

**Rule D — a legitimate NULL stays NULL.**  ``chunk_count IS NULL``
means "not measured" and is a value the CURRENT writer produces on
purpose (``storage.update_file_metadata``: "``None`` means 'not
known'"), asserted by ``acceptance/phase2.py``.  A guard that restored
``OLD`` on every ``NEW IS NULL`` would destroy that distinction.  So
the ``files`` guard restores only when the incoming row is **the
legacy shape** — i.e. the writer did not name the columns at all, and
therefore left the DEFAULT behind.

**The guard is a TEMPORARY measure**, for as long as a legacy writer
is running.  It is a safety net under a known-bad writer, not a
permanent part of the design: once the desktop build is rebuilt from
the current source, the new UPSERT (which preserves the columns in the
application layer) is the real defence and this trigger becomes
redundant.  It is harmless when redundant — every trigger fires only
when the incoming row is UNDECIDED while the stored row is DECIDED,
which a current writer never produces.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Tuple

#: The chunk-guard trigger names.  They are NOT one of the FTS triggers
#: the old build drops, so a legacy ``sync()`` cannot remove them by
#: accident.
GUARD_TRIGGER = "chunks_body_keep_bi"
GUARD_TRIGGER_DELETE = "chunks_body_keep_bd"
GUARD_TRIGGER_RESTORE = "chunks_body_keep_bi_ai"
#: The STATE restore trigger — the one that stops OUR OWN rebuild from
#: downgrading a certified 'mirror' row back to 'projection'.  It is a
#: separate trigger on purpose (measured: widening the fifth round's
#: ``bi_ai`` breaks the legacy DELETE-then-INSERT shape).
GUARD_TRIGGER_STATE = "chunks_body_keep_st_ai"
GUARD_TRIGGER_UPDATE = "chunks_body_keep_bu"

#: The files-side guard trigger names.
GUARD_TRIGGER_FILES_UPDATE = "files_body_keep_bu"
GUARD_TRIGGER_FILES_INSERT = "files_body_keep_bi"

#: The keep table and the columns it carries.
GUARD_TABLE = "body_keep"

#: The chunk columns the guard protects.  One list, used by the trigger
#: bodies and by the status check — so "the guard protects what it
#: claims" is a fact about the code, not a claim about a comment.
PROTECTED_COLUMNS = ("body_path", "body_anchor", "body_hash",
                     "text_state", "body_origin")

#: The files columns the guard protects.  Measured: the real key of
#: ``files`` is ``path``; the statistics columns are nullable in a
#: schema that added them by ``ALTER TABLE`` and ``NOT NULL DEFAULT 0``
#: in a schema that declared them.
FILES_PROTECTED_COLUMNS = ("chunk_count", "chunker_version")
FILES_KEY = "path"

#: The values that mean "the writer did not decide" — so a restore may
#: overwrite them.
#:
#: ``body_origin`` is ``NOT NULL DEFAULT 'unresolved'`` and
#: ``text_state`` is ``NOT NULL DEFAULT 'projection'``, so a legacy
#: ``INSERT OR REPLACE`` does not write NULL into them — it writes the
#: DEFAULT.  A restore that only checks for NULL would therefore never
#: fire for those two columns.
UNDECIDED_VALUES = {
    "body_origin": ("unresolved",),
    "text_state": ("projection",),
}

#: The values that mean "somebody decided this" — a decided value is
#: never replaced by an undecided one.
DECIDED_VALUES = {
    "text_state": ("mirror", "externalized", "stale", "missing"),
    "body_origin": ("file", "blob", "orphan"),
}

#: The chunk columns a legacy writer does not name at all.  When a
#: writer brings a row whose these are UNDECIDED, it has not brought a
#: pointer at all.
_POINTER_COLUMNS = ("body_path", "body_anchor", "body_hash")

#: The columns the writer brings — copied onto the stored row, so the
#: CONTENT still lands (the guard protects the POINTER and the STATE,
#: never the content).
_CONTENT_COLUMNS = ("user_id", "scope", "source", "path", "start_line",
                    "end_line", "text", "embedding", "hash", "metadata",
                    "created_at", "updated_at", "hit_count", "last_hit_at",
                    "element_released", "file_sealed", "sealed_owner")


def _normalize_sql(sql: str | None) -> str:
    """Whitespace-normalised SQL, for comparing a trigger body.

    SQLite stores the body verbatim (minus ``IF NOT EXISTS``), so the
    comparison is exact once the whitespace is collapsed.  Comparing the
    TEXT is what makes "the guard is installed but it is the OLD guard"
    detectable — ``CREATE TRIGGER IF NOT EXISTS`` never raises, so a
    stale trigger would otherwise be accepted forever.
    """
    if not sql:
        return ""
    return re.sub(r"\s+", " ", sql).strip()


def _undecided_test(column: str) -> str:
    """The condition "the incoming value is undecided"."""
    tests = ["NEW.{col} IS NULL".format(col=column)]
    for value in UNDECIDED_VALUES.get(column, ()):
        tests.append("NEW.{col} = '{value}'".format(col=column, value=value))
    return " OR ".join(tests)


def _decided_test(column: str, alias: str = "c") -> str:
    """The condition "the stored value is decided"."""
    decided = DECIDED_VALUES.get(column)
    if not decided:
        return "{a}.{col} IS NOT NULL".format(a=alias, col=column)
    return "{a}.{col} IN ({vals})".format(
        a=alias, col=column,
        vals=", ".join("'%s'" % v for v in decided))


def _restore_from_keep(column: str) -> str:
    """The SQL that keeps the KEPT value when the incoming one is
    undecided.

    The keep table is the restore source for the INSERT shapes: the
    trigger snapshots the stored row into it first (measured: at BEFORE
    INSERT the stored row is still visible), and the DELETE-then-INSERT
    shape fills it from the ``BEFORE DELETE`` trigger.
    """
    undecided = UNDECIDED_VALUES.get(column)
    if not undecided:
        return ("COALESCE(NEW.{col}, (SELECT {col} FROM {tbl} WHERE id = NEW.id))"
                .format(col=column, tbl=GUARD_TABLE))
    tests = ["NEW.{col} IS NULL".format(col=column)]
    for value in undecided:
        tests.append("NEW.{col} = '{value}'".format(col=column, value=value))
    cond = " OR ".join(tests)
    return ("CASE WHEN {cond} THEN (SELECT {col} FROM {tbl} WHERE id = NEW.id) "
            "ELSE NEW.{col} END".format(cond=cond, col=column, tbl=GUARD_TABLE))


def _restore_from_stored(column: str) -> str:
    """The SQL that keeps the STORED value when the incoming one is
    undecided.

    Used by the BEFORE UPDATE trigger: at that moment the table still
    holds the row's OLD values (measured: a subquery inside a BEFORE
    UPDATE trigger reads the OLD row, not the one the outer statement is
    about to write), so the stored row IS the restore source — and it
    is a better one than the keep table, which is empty for a row that
    was never deleted.
    """
    undecided = UNDECIDED_VALUES.get(column)
    if not undecided:
        return ("COALESCE(NEW.{col}, (SELECT c.{col} FROM chunks c "
                "WHERE c.id = NEW.id))".format(col=column))
    tests = ["NEW.{col} IS NULL".format(col=column)]
    for value in undecided:
        tests.append("NEW.{col} = '{value}'".format(col=column, value=value))
    cond = " OR ".join(tests)
    return ("CASE WHEN {cond} THEN (SELECT c.{col} FROM chunks c "
            "WHERE c.id = NEW.id) ELSE NEW.{col} END"
            .format(cond=cond, col=column))


def _stored_decided_exists() -> str:
    """A subquery that is TRUE when the STORED row's state is decided."""
    parts = []
    for col in ("text_state", "body_origin"):
        parts.append(_decided_test(col))
    return " OR ".join(parts)


def _differs_from_stored(columns: Tuple[str, ...]) -> str:
    """The condition "the incoming value really DIFFERS from the stored one".

    This is what keeps the guard from re-triggering itself.  The guard's
    OWN restore is an UPDATE that writes the STORED values back, so on
    that update the incoming value equals the stored one — and a
    ``BEFORE UPDATE`` trigger that fired on it would start a SECOND
    inner UPDATE, which fires the FTS AFTER UPDATE trigger a second
    time.  Measured: that double FTS update is what makes SQLite report
    "database disk image is malformed" on the legacy DELETE-then-INSERT
    shape.

    ``IS NOT`` is the NULL-safe comparison in SQLite, so a NULL stored
    value compares equal to a NULL incoming value — which is what the
    rule needs.
    """
    parts = []
    for col in columns:
        parts.append(
            "NEW.{col} IS NOT (SELECT c.{col} FROM chunks c "
            "WHERE c.id = NEW.id)".format(col=col))
    return " OR ".join(parts)


def trigger_sqls() -> List[str]:
    """The ONE definition of the guard's trigger bodies.

    Four chunk triggers, because the legacy build writes in THREE
    shapes and each needs its own mechanism (measured — see the module
    docstring), plus two ``files`` triggers for the statistics columns.

    * ``INSERT OR REPLACE INTO chunks (...)`` — the row is replaced
      wholesale.  The ``BEFORE INSERT`` trigger sees the OLD row still
      in the table (measured: "BEFORE INSERT sees: ORIGINAL"), so it
      snapshots the row into ``body_keep``, copies the writer's new
      content onto it and raises IGNORE — so the content lands and the
      pointer survives.

    * ``INSERT INTO chunks (...) ON CONFLICT(id) DO UPDATE SET (...)``
      — the stored row is UPDATED in place.  The ``BEFORE UPDATE``
      trigger snapshots the stored row and restores the protected
      columns from the snapshot, then RAISEs IGNORE (measured: an
      inner UPDATE alone is overwritten by the outer statement).
      **This shape was NOT covered before S3-I.**

    * ``DELETE FROM chunks WHERE path = ?`` followed by re-INSERTs
      (the legacy ``replace_file_chunks``) — the row is GONE before the
      INSERT arrives.  The ``BEFORE DELETE`` trigger snapshots the
      pointer into ``body_keep``, and the ``AFTER INSERT`` trigger
      restores it.

    * ``INSERT OR REPLACE INTO files (...)`` — the ``files`` row is
      replaced wholesale and the statistics columns fall back to their
      DEFAULT.  The ``files`` triggers keep the measured values.
    """
    content_assign = ", ".join(
        "%s=NEW.%s" % (col, col) for col in _CONTENT_COLUMNS)
    restores = ", ".join("%s = %s" % (col, _restore_from_keep(col))
                         for col in PROTECTED_COLUMNS)

    # The snapshot: copy the STORED row's protected columns into the
    # keep table.  It is the FIRST statement of the trigger body, and
    # it must be — by the time the body's UPDATE runs, the row has
    # already been replaced (measured), so the snapshot is the only
    # place the old values still exist.
    snapshot = """
    INSERT OR REPLACE INTO {tbl} (id, body_path, body_anchor, body_hash,
                                   text_state, body_origin)
    SELECT c.id, c.body_path, c.body_anchor, c.body_hash,
           c.text_state, c.body_origin
    FROM chunks c WHERE c.id = NEW.id;""".format(tbl=GUARD_TABLE)

    # --- the INSERT shape ---------------------------------------------
    # The fifth round's trigger, UNCHANGED.  It is the shape that keeps
    # the pointer through the legacy ``INSERT OR REPLACE``:
    #
    #   * the WHEN fires only when the writer brought NO pointer and the
    #     STORED row has one — the legacy shape;
    #   * the body's UPDATE names only the CONTENT columns, so the
    #     protected columns keep their stored values (SQLite: a column
    #     that a SET list does not name keeps its value).
    #
    # S3-I tried to widen this trigger to also catch a state-only
    # downgrade, and that WIDENING is what made the legacy
    # DELETE-then-INSERT shape fail with "database disk image is
    # malformed" (the widened body restores the protected columns from
    # the keep table, and the keep table's snapshot of a row that was
    # just deleted no longer matches the row the writer inserted).  The
    # state downgrade is therefore handled by the NEW triggers below,
    # and this one is left exactly as the fifth round had it — a change
    # that is measured to break a passing criterion is not a change.
    pointer_missing = " AND ".join("NEW.{c} IS NULL".format(c=c)
                                   for c in _POINTER_COLUMNS)
    state_undecided = " OR ".join(
        "({t})".format(t=_undecided_test(c)) for c in ("text_state",
                                                        "body_origin"))

    insert_trigger = """
CREATE TRIGGER {name} BEFORE INSERT ON chunks
WHEN NEW.body_path IS NULL
 AND EXISTS (SELECT 1 FROM chunks c WHERE c.id = NEW.id AND c.body_path IS NOT NULL)
BEGIN
    UPDATE chunks SET
        {content}
    WHERE id = NEW.id;
    SELECT RAISE(IGNORE);
END""".format(name=GUARD_TRIGGER, content=content_assign)

    # --- the UPDATE shape (the one that was NOT covered) -------------
    # A writer that UPDATES the row in place — the legacy build's
    # ``INSERT ... ON CONFLICT(id) DO UPDATE``, and the anti-downgrade
    # UPSERT of the current build — is stopped here.
    #
    # The WHEN demands three things, and each of them is a measured
    # requirement:
    #   * the incoming value is UNDECIDED (the writer did not decide);
    #   * the STORED value is DECIDED (there is something to protect);
    #   * the incoming value really DIFFERS from the stored one — so
    #     the guard's OWN restore (which writes the stored values back)
    #     does not re-trigger it, which would fire the FTS AFTER UPDATE
    #     trigger twice and make SQLite report "database disk image is
    #     malformed" (measured).
    update_when = (
        "(({pm}) OR ({su})) "
        "AND EXISTS (SELECT 1 FROM chunks c WHERE c.id = NEW.id "
        "AND c.body_path IS NOT NULL AND ({sd})) "
        "AND ({differs})"
    ).format(pm=pointer_missing, su=state_undecided,
             sd=_stored_decided_exists(),
             differs=_differs_from_stored(PROTECTED_COLUMNS))

    update_restores = ", ".join(
        "%s = %s" % (col, _restore_from_stored(col))
        for col in PROTECTED_COLUMNS)

    update_trigger = """
CREATE TRIGGER {name} BEFORE UPDATE ON chunks
WHEN {when}
BEGIN
    UPDATE chunks SET
        {content},
        {restores}
    WHERE id = NEW.id;
    SELECT RAISE(IGNORE);
END""".format(name=GUARD_TRIGGER_UPDATE, when=update_when,
               content=content_assign, restores=update_restores)

    # --- the DELETE shape ---------------------------------------------
    # The snapshot — the fifth round's trigger, unchanged.  The
    # "a DECIDED snapshot is never overwritten by an UNDECIDED one" rule
    # lives on the RESTORE side below, not here: measured, rewriting
    # this statement as ``INSERT ... SELECT ... WHERE`` changes its
    # behaviour under the legacy DELETE-then-INSERT shape and makes
    # SQLite report "database disk image is malformed".
    delete_trigger = """
CREATE TRIGGER {name} BEFORE DELETE ON chunks
WHEN OLD.body_path IS NOT NULL
BEGIN
    INSERT OR REPLACE INTO {tbl} (id, body_path, body_anchor, body_hash,
                                   text_state, body_origin)
    VALUES (OLD.id, OLD.body_path, OLD.body_anchor, OLD.body_hash,
            OLD.text_state, OLD.body_origin);
END""".format(name=GUARD_TRIGGER_DELETE, tbl=GUARD_TABLE)

    # The DELETE-then-INSERT shape: the row is gone, so the restore
    # happens AFTER the insert — and it restores the POINTER and the
    # STATE from the keep table the BEFORE DELETE trigger filled.
    #
    # The restore touches only the PROTECTED columns: the content
    # columns are left alone, so the FTS index (whose 'delete' command
    # reads the content table) stays consistent (measured).
    restore_trigger = """
CREATE TRIGGER {name} AFTER INSERT ON chunks
WHEN NEW.body_path IS NULL
 AND EXISTS (SELECT 1 FROM {tbl} k WHERE k.id = NEW.id)
BEGIN
    UPDATE chunks SET
        {restores}
    WHERE id = NEW.id;
END""".format(
        name=GUARD_TRIGGER_RESTORE,
        restores=restores, tbl=GUARD_TABLE)

    # --- the STATE downgrade shape (ours) -----------------------------
    # Our own rebuild brings a row that HAS a pointer but an UNDECIDED
    # state: ``body_pointer.four_columns_for_chunk`` returns
    # ``STATE_PROJECTION`` for every chunk it builds — it decides the
    # POINTER, never the state.  Measured on a copy of the live ROOT
    # library: the real ``replace_file_chunks`` (rebuilder.py:294)
    # turns an inspector-certified 'mirror' row back into 'projection'.
    #
    # This trigger is SEPARATE from the fifth round's ``bi_ai`` on
    # purpose.  Widening ``bi_ai``'s WHEN clause was measured to make
    # the legacy DELETE-then-INSERT shape fail with "database disk
    # image is malformed" (the widened body restores from the keep
    # table, whose snapshot no longer matches the inserted row).  A
    # separate trigger keeps the fifth round's behaviour untouched.
    #
    # It restores ONLY the state columns, and only when the keep table
    # holds a DECIDED snapshot for this id — so it can only ever
    # restore a certified verdict, never invent one.
    state_restore_trigger = """
CREATE TRIGGER {name} AFTER INSERT ON chunks
WHEN ({su})
 AND EXISTS (SELECT 1 FROM {tbl} k WHERE k.id = NEW.id AND ({kdec}))
 AND EXISTS (SELECT 1 FROM chunks c WHERE c.id = NEW.id
             AND c.body_path IS NOT NULL)
BEGIN
    UPDATE chunks SET
        {state_restores}
    WHERE id = NEW.id;
END""".format(
        name=GUARD_TRIGGER_STATE,
        su=state_undecided,
        kdec=" OR ".join(
            "(k.{c} IN ({vals}))".format(
                c=c, vals=", ".join("'%s'" % v
                                    for v in DECIDED_VALUES.get(c, ())))
            for c in ("text_state", "body_origin")),
        state_restores=", ".join(
            "{c} = CASE WHEN {u} THEN (SELECT k.{c} FROM {tbl} k "
            "WHERE k.id = NEW.id) ELSE NEW.{c} END".format(
                c=c, u=_undecided_test(c), tbl=GUARD_TABLE)
            for c in ("text_state", "body_origin")),
        tbl=GUARD_TABLE)

    # --- the files side ------------------------------------------------
    # The real key is ``path`` (measured).  The restore fires only when
    # the incoming statistics are UNDECIDED and the stored ones were
    # MEASURED — so a legitimately "not measured" row (NULL) stays NULL
    # (Rule D).
    #
    # "Undecided" has TWO shapes, because the two live schemas differ
    # (measured with PRAGMA table_info):
    #   * a nullable schema (``ALTER TABLE ADD COLUMN``) — the legacy
    #     writer leaves NULL;
    #   * a ``NOT NULL DEFAULT 0`` schema — the legacy writer leaves
    #     the DEFAULT, i.e. **0**.
    # So the restore must accept both, and it must NOT touch a stored
    # 0 (a file that really produced no chunks).
    files_undecided = " OR ".join(
        "NEW.{c} IS NULL OR NEW.{c} = 0".format(c=c)
        for c in FILES_PROTECTED_COLUMNS)
    files_stored_measured = " AND ".join(
        "(SELECT f.{c} FROM files f WHERE f.{key} = NEW.{key}) IS NOT NULL "
        "AND (SELECT f.{c} FROM files f WHERE f.{key} = NEW.{key}) <> 0"
        .format(c=c, key=FILES_KEY) for c in FILES_PROTECTED_COLUMNS)

    files_when = "({undecided}) AND ({stored_measured})".format(
        undecided=files_undecided, stored_measured=files_stored_measured)

    files_update_trigger = """
CREATE TRIGGER {name} BEFORE UPDATE ON files
WHEN {when}
BEGIN
    UPDATE files SET
        {restores}
    WHERE {key} = NEW.{key};
    SELECT RAISE(IGNORE);
END""".format(
        name=GUARD_TRIGGER_FILES_UPDATE, when=files_when,
        restores=", ".join(
            "{c} = CASE WHEN NEW.{c} IS NULL OR NEW.{c} = 0 "
            "THEN (SELECT f.{c} FROM files f WHERE f.{key} = NEW.{key}) "
            "ELSE NEW.{c} END".format(c=c, key=FILES_KEY)
            for c in FILES_PROTECTED_COLUMNS),
        key=FILES_KEY)

    files_insert_trigger = """
CREATE TRIGGER {name} BEFORE INSERT ON files
WHEN EXISTS (SELECT 1 FROM files f WHERE f.{key} = NEW.{key}
             AND f.chunk_count IS NOT NULL AND f.chunker_version IS NOT NULL)
BEGIN
    UPDATE files SET
        {restores}
    WHERE {key} = NEW.{key};
    SELECT RAISE(IGNORE);
END""".format(
        name=GUARD_TRIGGER_FILES_INSERT,
        restores=", ".join(
            "{c} = CASE WHEN NEW.{c} IS NULL OR NEW.{c} = 0 "
            "THEN (SELECT f.{c} FROM files f WHERE f.{key} = NEW.{key}) "
            "ELSE NEW.{c} END".format(c=c, key=FILES_KEY)
            for c in FILES_PROTECTED_COLUMNS),
        key=FILES_KEY)

    return [insert_trigger, update_trigger, delete_trigger, restore_trigger,
            state_restore_trigger, files_update_trigger, files_insert_trigger]


def table_ddl() -> str:
    """The keep table.  It is not part of the FTS machinery, so the
    legacy build's FTS rebuild cannot take it away."""
    return """
CREATE TABLE IF NOT EXISTS {tbl} (
    id TEXT PRIMARY KEY,
    body_path TEXT, body_anchor TEXT, body_hash TEXT,
    text_state TEXT, body_origin TEXT)""".format(tbl=GUARD_TABLE)


def guard_trigger_names() -> Tuple[str, ...]:
    """Every trigger the guard owns — the one list the installer drops
    and the status check compares."""
    return (GUARD_TRIGGER, GUARD_TRIGGER_UPDATE, GUARD_TRIGGER_DELETE,
            GUARD_TRIGGER_RESTORE, GUARD_TRIGGER_STATE,
            GUARD_TRIGGER_FILES_UPDATE, GUARD_TRIGGER_FILES_INSERT)


def stored_trigger_sql(conn: sqlite3.Connection) -> str:
    """The guard triggers' stored SQL, joined ('' when not installed)."""
    names = guard_trigger_names()
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN (%s)" % ",".join("?" * len(names)),
            names).fetchall()
    except sqlite3.Error:
        return ""
    return " ;; ".join((r[1] or "") for r in rows)


def _trigger_name_of(sql: str) -> str:
    """The trigger's name, parsed out of its ``CREATE TRIGGER`` statement."""
    for token in sql.strip().split():
        if token.upper() == "TRIGGER" and token is not None:
            idx = sql.strip().split().index(token)
            parts = sql.strip().split()
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return ""


def _expected_trigger_map() -> Dict[str, str]:
    """The current definition, keyed by trigger name."""
    out: Dict[str, str] = {}
    for statement in trigger_sqls():
        name = _trigger_name_of(statement)
        if name:
            out[name] = statement
    return out


def is_installed(conn: sqlite3.Connection) -> bool:
    """True when the guard is installed AND its bodies are the current ones.

    The comparison is per trigger, by NAME — not by the order the
    triggers happen to sit in ``sqlite_master``.  The order changes
    whenever a trigger is re-created (a re-created trigger lands at the
    END of ``sqlite_master``), and that order is not part of the
    definition: a guard whose bodies are current but whose triggers
    happen to be listed in another order is STILL the current guard.
    Comparing the joined strings would report it as stale — and the
    repair would then re-install it on every open, which flips the
    trigger order again (measured: that flip is what makes the legacy
    DELETE-then-INSERT shape fail with "database disk image is
    malformed").
    """
    names = guard_trigger_names()
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND name IN (%s)" % ",".join("?" * len(names)),
            names).fetchall()
    except sqlite3.Error:
        return False
    stored = {r[0]: (r[1] or "") for r in rows}
    expected = _expected_trigger_map()
    if not stored:
        return False
    for name in names:
        if name not in expected:
            continue  # the files-side trigger may be skipped
        if name not in stored:
            return False
        if _normalize_sql(stored[name]) != _normalize_sql(expected[name]):
            return False
    return True


def ensure_guard(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Make sure the guard is present and current, and say which it is.

    Returns ``{"status": present|installed|repaired|failed, ...}``.

    "Already installed" and "could not be installed" must never be the
    same answer: the first is a fact about the index, the second is a
    fact about the system's safety, and the patrol must tell them apart.
    """
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    except sqlite3.Error as e:
        return {"status": "failed", "reason": "cannot read chunks: %s" % e}
    if not columns:
        return {"status": "failed",
                "reason": "the database has no chunks table yet"}
    missing = [c for c in PROTECTED_COLUMNS if c not in columns]
    if missing:
        # A library that predates the columns has nothing to protect, and
        # the trigger would not compile against it.
        return {"status": "failed",
                "reason": "the chunks table has no %s column(s)"
                           % ", ".join(missing)}

    # The files-side guard needs the files table and its statistics
    # columns.  A library without them is a library with nothing to
    # protect on that side — the chunks side is still worth guarding,
    # so the files triggers are skipped rather than failing the whole
    # installation.
    files_ok = True
    files_reason = ""
    try:
        fcols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    except sqlite3.Error as e:
        fcols = set()
        files_ok = False
        files_reason = "cannot read files: %s" % e
    else:
        fmissing = [c for c in FILES_PROTECTED_COLUMNS if c not in fcols]
        if fmissing:
            files_ok = False
            files_reason = ("the files table has no %s column(s)"
                            % ", ".join(fmissing))
        elif FILES_KEY not in fcols:
            files_ok = False
            files_reason = "the files table has no %s column" % FILES_KEY

    if is_installed(conn):
        return {"status": "present", "files_guard": files_ok}

    existed = bool(stored_trigger_sql(conn))
    try:
        if conn.in_transaction:
            conn.commit()
        # The drop-and-create must be ATOMIC: a guard that is absent for
        # even a moment is a window in which the legacy build can erase
        # the pointers.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(table_ddl())
        for name in guard_trigger_names():
            conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        for statement in trigger_sqls():
            if not files_ok and (" ON files" in statement):
                continue
            conn.execute(statement)
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"status": "failed", "reason": "%s: %s" % (type(e).__name__, e)}
    out: Dict[str, Any] = {"status": "repaired" if existed else "installed"}
    if not files_ok:
        out["files_guard"] = False
        out["files_reason"] = files_reason
    else:
        out["files_guard"] = True
    return out


def install_guard(conn: sqlite3.Connection) -> bool:
    """Install the guard.  Idempotent.  True when it is in place."""
    return ensure_guard(conn).get("status") != "failed"


def status(conn: sqlite3.Connection) -> Dict[str, Any]:
    """A reportable description of the guard's state."""
    stored = stored_trigger_sql(conn)
    names = list(guard_trigger_names())
    present = []
    try:
        for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name IN (%s)" % ",".join("?" * len(names)), names):
            present.append(r[0])
    except sqlite3.Error:
        pass
    out: Dict[str, Any] = {
        "trigger": GUARD_TRIGGER,
        "triggers": names,
        "installed": bool(stored),
        "present": sorted(present),
        "current": is_installed(conn),
        "protects": list(PROTECTED_COLUMNS),
        "files_protects": list(FILES_PROTECTED_COLUMNS),
        "files_key": FILES_KEY,
        "files_guard": (GUARD_TRIGGER_FILES_UPDATE in present
                        and GUARD_TRIGGER_FILES_INSERT in present),
    }
    if stored and not out["current"]:
        out["reason"] = ("the guard is installed but its body is not the "
                          "current definition (it will be repaired on the "
                          "next ensure)")
    return out


def protected_rows(conn: sqlite3.Connection) -> List[str]:
    """The ids of the rows the guard would protect right now.

    A guard that protects nothing is a guard that is installed and does
    nothing — and the two must be distinguishable, so the count is
    reported rather than assumed.
    """
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_path IS NOT NULL").fetchone()
    except sqlite3.Error:
        return []
    return [str(rows[0])] if rows else []


def erasure_census(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The MONITORING criterion: is the library's certified state intact,
    and WHEN did it last change?

    This is the check that tells "the migration never ran" apart from
    "the migration ran and something erased it afterwards":

    * ``certified``   — rows whose state the inspector certified;
    * ``undecided``   — rows that fell back to 'projection'/'unresolved'
      (the erased shape);
    * ``no_pointer``  — rows whose pointer is gone;
    * ``last_write``  — the newest ``updated_at`` in the library.

    A library that was never migrated has ``no_pointer`` on every row
    and ``undecided`` on every row.  A library that WAS migrated and was
    erased afterwards has the same shape but a ``last_write`` NEWER
    than the migration's own timestamp — and that is the fact that
    separates the two.
    """
    out: Dict[str, Any] = {}
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    except sqlite3.Error as e:
        return {"error": str(e)}
    if "text_state" not in cols:
        return {"error": "the chunks table has no text_state column"}
    try:
        out["rows"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        out["certified"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text_state='mirror' "
            "AND body_origin='file'").fetchone()[0]
        out["undecided"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text_state='projection' "
            "OR body_origin='unresolved'").fetchone()[0]
        out["no_pointer"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL").fetchone()[0]
        out["unresolved"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"
        ).fetchone()[0]
        out["projection"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text_state='projection'"
        ).fetchone()[0]
        last = conn.execute(
            "SELECT MAX(updated_at) FROM chunks").fetchone()[0]
        out["last_write"] = last
        try:
            out["files_without_stats"] = conn.execute(
                "SELECT COUNT(*) FROM files WHERE chunk_count IS NULL "
                "OR chunker_version IS NULL").fetchone()[0]
            out["files_with_version"] = conn.execute(
                "SELECT COUNT(*) FROM files WHERE chunker_version IS NOT NULL "
                "AND chunker_version <> 0").fetchone()[0]
        except sqlite3.Error:
            pass
    except sqlite3.Error as e:
        return {"error": str(e)}
    out["intact"] = (out.get("undecided", 1) == 0
                     and out.get("no_pointer", 1) == 0)
    return out
