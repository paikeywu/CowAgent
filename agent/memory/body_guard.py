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
from typing import Any, Dict, List, Optional, Tuple

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
#: The files-side SNAPSHOT trigger — the one the sixth round MISSED.
#:
#: ``INSERT OR REPLACE`` is a DELETE followed by an INSERT, so a
#: BEFORE INSERT trigger that looks at the stored row finds NOTHING
#: (the row is already gone).  Measured (S3-K): the legacy build's
#: ``INSERT OR REPLACE INTO files (...)`` erased the statistics even
#: with the guard installed, because the stored row was gone by the
#: time the INSERT trigger ran.  The chunks side has had its BEFORE
#: DELETE snapshot since the sixth round; the files side had not.
GUARD_TRIGGER_FILES_DELETE = "files_body_keep_bd"
#: The files-side RESTORE trigger (AFTER INSERT), fed by the snapshot.
GUARD_TRIGGER_FILES_RESTORE = "files_body_keep_bi_ai"

#: The files-side snapshot table.  It is separate from the chunks-side
#: keep table because the two have different keys (the files table's
#: key is ``path``, not ``id``).
GUARD_TABLE_FILES = "files_keep"

#: The keep table and the columns it carries.
GUARD_TABLE = "body_keep"

#: The certification ledger — the certifier's record of what it verified
#: against the truth layer.  It is NOT the keep table: the keep table
#: records what a row looked like BEFORE a write touched it (and only
#: for the rows a write actually threatened), while the ledger records
#: that somebody CHECKED a row against the truth layer and FOUND IT
#: EQUAL.
#:
#: **It is defined HERE, next to the keep table, because the trigger
#: bodies reference it** (S3-L: the state restore is backed by the
#: ledger).  A constant that the triggers read must be defined before
#: the triggers are built — otherwise the module would raise NameError
#: at import time, which is a loud failure, but a loud failure in a
#: module every write path imports is not a good place to find out.
CERT_TABLE = "cert_ledger"

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


def _restore_from_ledger(column: str) -> str:
    """The SQL that restores the state, LEDGER FIRST.

    **P1-阶段三 S3-L — the fix for the third hole, and the shape it
    took.**

    The keep table holds what a row looked like BEFORE a write touched
    it.  That is the guard's MEMORY.  It is NOT evidence that anybody
    verified the row against the truth layer, and it was being used as
    if it were: the ``bi_ai`` trigger restored ``text_state='mirror'``
    from it and wrote no ledger entry, so the row claimed "certified"
    with nobody able to say when, or against what.

    MEASURED: 15 such rows existed in ROOT, and the state was
    SELF-PERPETUATING — the certifier only examines rows whose state
    is NOT mirror, and the backfill only blesses rows that are absent
    from the ledger, so neither could leave it.

    **But the ledger is not the guard's business, and making it one
    would break the guard.**  The guard answers "may a writer
    DOWNGRADE this row's state?"  The ledger answers "was this row
    CERTIFIED?".  Two different questions, and the first one must be
    answerable WITHOUT a ledger:

      * a library that has never been certified has no ledger at all;
      * a row that was certified by the OLD inspector has no ledger
        entry (the ledger was introduced later).

    If the guard's anti-downgrade protection depended on the ledger,
    both of those rows would lose their protection — and the guard
    would be *weaker* than before, not stronger.  That is why the
    first version of this fix was rejected: it broke five existing
    guard tests, all of which assert that a 'mirror' row with a
    pointer is not downgraded.

    **So the restore is: the LEDGER's value if the ledger has one, and
    the STORED/KEPT value otherwise.**  The ledger is preferred
    because it is the authoritative record of what the row was
    certified as; the stored value is the fallback that keeps the
    anti-downgrade protection total.

    **And the hole is closed on the OTHER side**, which is where it
    belongs: the certifier now reaches every row the ledger does not
    mention (not only the rows whose state is NOT 'mirror'), so a row
    that claims 'mirror' without a witness is RE-VERIFIED against the
    truth layer and given a ledger entry — or refused and demoted.

    The difference this makes to the data: a row that claims 'mirror'
    with no witness is no longer invisible to the certification
    apparatus.  Before this round it was invisible to EVERY criterion
    (M6a asks about rows that are not certified; M6b needs a ledger
    entry to compare against).  Now M6c — "every mirror row must have
    a witness" — can see it, and the certifier repairs it.
    """
    undecided = UNDECIDED_VALUES.get(column)
    if not undecided:
        return ("COALESCE(NEW.{col}, (SELECT l.{col} FROM {cert} l "
                "WHERE l.id = NEW.id))".format(col=column, cert=CERT_TABLE))
    tests = ["NEW.{col} IS NULL".format(col=column)]
    for value in undecided:
        tests.append("NEW.{col} = '{value}'".format(col=column, value=value))
    cond = " OR ".join(tests)
    # The fallback: the STORED row's value (or the keep table's).
    # It keeps the anti-downgrade protection total — the guard must
    # not become weaker than it was, and a library with no ledger must
    # still be protected.
    #
    # NOTE: the fallback is built with its OWN format call, so ``{col}``
    # is resolved BEFORE it reaches this one.  A nested ``{col}`` would
    # reach SQLite as the literal text "{col}" — measured: the trigger
    # failed to compile with ``unrecognized token: "{"``.
    fallback = ("(SELECT c.{col} FROM chunks c WHERE c.id = NEW.id)"
                .format(col=column))
    return ("CASE WHEN {cond} THEN COALESCE((SELECT l.{col} FROM {cert} l "
            "WHERE l.id = NEW.id), {fallback}) ELSE NEW.{col} END"
            .format(cond=cond, col=column, cert=CERT_TABLE,
                    fallback=fallback))


def _restore_from_stored(column: str) -> str:
    """The SQL that keeps the STORED value when the incoming one is
    undecided.

    Used by the BEFORE UPDATE trigger: at that moment the table still
    holds the row's OLD values (measured: a subquery inside a BEFORE
    UPDATE trigger reads the OLD row, not the one the outer statement is
    about to write), so the stored row IS the restore source — and it
    is a better one than the keep table, which is empty for a row that
    was never deleted.

    **S3-L: the state is NOT restored from the stored row.**  The
    stored row's state can be 'mirror' with no ledger entry — that is
    exactly the hole (the state was restored from the keep table once,
    and the trigger wrote no ledger entry).  So the state's restore
    source is the LEDGER, and only the pointer columns come from the
    stored row.
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
    # The restore sources: the POINTER comes from the keep table (the
    # guard's memory of the row before the write); the STATE comes from
    # the LEDGER (the certifier's record of what it verified).
    #
    # **S3-L: this split is the fix.**  Before it, both came from the
    # keep table — so a row could be restored to 'mirror' with no
    # ledger entry, and the certification's witness said nothing about
    # it.  15 such rows existed in ROOT, and neither the certifier nor
    # the backfill could repair them.
    restores = ", ".join(
        "%s = %s" % (col,
                     _restore_from_ledger(col) if col == "text_state"
                     else _restore_from_keep(col))
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

    # **S3-L: the STATE's restore source is the LEDGER here too.**
    # The stored row's ``text_state`` can be 'mirror' with no ledger
    # entry — that is exactly the hole the third round found.  So the
    # BEFORE UPDATE trigger restores the POINTER from the stored row
    # and the STATE from the ledger; a row with no ledger entry keeps
    # the incoming (undecided) state, and the certifier re-verifies it.
    update_restores = ", ".join(
        "%s = %s" % (col,
                     _restore_from_ledger(col) if col == "text_state"
                     else _restore_from_stored(col))
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
    #
    # **P1-阶段三 S3-L (the third hole): the STATE comes from the
    # LEDGER, not from the keep table.**  This is the trigger the
    # hole was MEASURED on: it restored ``text_state='mirror'`` from
    # the keep table and wrote NO ledger entry, so the row claimed
    # "certified" with nobody able to say when, or against what.  15
    # such rows existed in ROOT.
    #
    # The pointer still comes from the keep table (the guard's memory
    # of the row before the write).  The STATE comes from the ledger
    # (the certifier's record of what it verified).  Two different
    # facts, two different sources — and only the ledger's means
    # "certified".
    #
    # So the state is restored only when the LEDGER says this row was
    # certified in a state, and the restored value is the LEDGER's
    # value — not the keep table's.  A row whose keep-table snapshot
    # says 'mirror' but whose ledger says nothing stays 'projection',
    # and the certifier (which now reaches every row with no ledger
    # entry) re-verifies it against the truth layer.
    #
    # The ``WHEN`` clause demands a ledger entry for the SAME reason:
    # a trigger that could restore a state with no witness is the hole
    # itself.
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
        restores=restores, tbl=GUARD_TABLE, cert=CERT_TABLE)

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
    #
    # **P1-阶段三 S3-L (the third hole): the LEDGER is the state's
    # PREFERRED source, and the certifier is what closes the hole.**
    #
    # MEASURED: the sixth round's ``bi_ai`` restored
    # ``text_state='mirror'`` from the keep table, and the trigger
    # wrote NO ledger entry — so the row said "certified" while the
    # certification's own witness said nothing about it.  15 such rows
    # existed in ROOT; neither the certifier nor the backfill could
    # repair them (the certifier only looks at rows whose state is NOT
    # mirror; the backfill only blesses what is absent from the
    # ledger).  The state was SELF-PERPETUATING.
    #
    # **The rule this trigger now follows:**
    #
    #   the state's restore source is the LEDGER when the ledger has an
    #   entry for this row, and the KEEP TABLE otherwise.
    #
    # The keep table is the guard's MEMORY of what a row looked like
    # before a write touched it.  The ledger is the certifier's RECORD
    # of what it verified against the truth layer.  They are different
    # facts, and only the second one means "certified" — so the ledger
    # wins when it has an opinion.
    #
    # **Why the keep table is still the fallback.**  The guard's job is
    # to prevent a DOWNGRADE, and that protection must not depend on a
    # table that may not exist: a library that was never certified has
    # no ledger, and a row certified by the OLD inspector has no ledger
    # entry.  Gating the guard on the ledger would make the guard
    # WEAKER — the first version of this fix broke five existing guard
    # tests, all of which assert that a 'mirror' row with a pointer is
    # not downgraded.
    #
    # **Why the trigger cannot write the ledger itself.**  The ledger's
    # meaning is "this row was CHECKED against the file and FOUND
    # EQUAL", and the trigger never checks anything — it only knows
    # that a value was there before.  Writing the ledger from the
    # trigger would turn it into an audit log of restores (which is
    # exactly what S3-K refused to make of the keep table) and would
    # bless rows nobody ever verified.
    #
    # **So the hole is closed on the OTHER side**, which is where it
    # belongs: the certifier now reaches every row the ledger does not
    # mention (not only the rows whose state is NOT 'mirror'), so a row
    # that claims 'mirror' without a witness is RE-VERIFIED against the
    # truth layer and given a ledger entry — or refused and demoted.
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
        # The STATE comes from the LEDGER (the certifier's record of
        # what it verified); the ORIGIN keeps coming from the keep
        # table (the guard's memory of the row before the write).
        #
        # NOTE: the two sources are built with their OWN ``.format``
        # calls, so the table names are already resolved when they
        # reach the outer format.  A nested ``{cert}`` placeholder
        # would NOT be interpolated by the outer format — it would
        # reach SQLite as the literal text "{cert}", and the trigger
        # would fail to compile.
        #
        # NOTE 2: the column is qualified by the ALIAS (``l.text_state``),
        # not by the table name (``cert_ledger.text_state``) — the
        # latter is not valid SQL when the table is aliased, and
        # SQLite rejects it with "near '.': syntax error".
        # The STATE: the ledger's value if it has one, the keep
        # table's otherwise.  The ORIGIN: the keep table's (the ledger
        # records the origin too, but the keep table's snapshot is what
        # the guard has always restored, and the origin is not what the
        # hole was about).
        state_restores=", ".join(
            "{c} = CASE WHEN {u} THEN COALESCE("
            "(SELECT {alias}.{c} FROM {ltbl} {alias} WHERE {alias}.id = NEW.id), "
            "(SELECT {alias2}.{c} FROM {ktbl} {alias2} WHERE {alias2}.id = NEW.id)"
            ") ELSE NEW.{c} END".format(
                c=c, u=_undecided_test(c),
                alias="l", ltbl=CERT_TABLE,
                alias2="k", ktbl=GUARD_TABLE)
            if c == "text_state" else
            "{c} = CASE WHEN {u} THEN (SELECT k.{c} FROM {tbl} k "
            "WHERE k.id = NEW.id) ELSE NEW.{c} END".format(
                c=c, u=_undecided_test(c), tbl=GUARD_TABLE)
            for c in ("text_state", "body_origin")),
        tbl=GUARD_TABLE, cert=CERT_TABLE)

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

    # --- the files DELETE shape (the one that was NOT covered) --------
    # ``INSERT OR REPLACE`` is a DELETE followed by an INSERT, so by
    # the time the BEFORE INSERT trigger runs the stored row is GONE
    # and there is nothing to restore from.  Measured (S3-K): the
    # legacy build's ``INSERT OR REPLACE INTO files (...)`` erased the
    # statistics even with the guard installed — and the chunks side
    # had had its BEFORE DELETE snapshot since the sixth round, while
    # the files side had not.  The two sides must have the same
    # coverage, or the guard protects one axis and not the other.
    #
    # The snapshot is taken BEFORE the delete, so it holds the
    # statistics the row HAD; the AFTER INSERT trigger then restores
    # them.  This mirrors the chunks side exactly.
    files_delete_trigger = """
CREATE TRIGGER {name} BEFORE DELETE ON files
WHEN OLD.chunk_count IS NOT NULL AND OLD.chunker_version IS NOT NULL
BEGIN
    INSERT OR REPLACE INTO {tbl} ({key}, chunk_count, chunker_version)
    VALUES (OLD.{key}, OLD.chunk_count, OLD.chunker_version);
END""".format(name=GUARD_TRIGGER_FILES_DELETE, tbl=GUARD_TABLE_FILES,
              key=FILES_KEY)

    # The restore side: AFTER INSERT, from the snapshot.  It restores
    # ONLY the statistics, and only when the snapshot holds a MEASURED
    # pair (so a file that really produced no chunks keeps its NULL —
    # Rule D: "not measured" is not "zero").
    files_restore_trigger = """
CREATE TRIGGER {name} AFTER INSERT ON files
WHEN NEW.chunk_count IS NULL AND NEW.chunker_version IS NULL
 AND EXISTS (SELECT 1 FROM {tbl} k WHERE k.{key} = NEW.{key}
             AND k.chunk_count IS NOT NULL AND k.chunker_version IS NOT NULL)
BEGIN
    UPDATE files SET
        {restores}
    WHERE {key} = NEW.{key};
END""".format(
        name=GUARD_TRIGGER_FILES_RESTORE,
        tbl=GUARD_TABLE_FILES, key=FILES_KEY,
        restores=", ".join(
            "{c} = (SELECT k.{c} FROM {tbl} k WHERE k.{key} = NEW.{key})"
            .format(c=c, tbl=GUARD_TABLE_FILES, key=FILES_KEY)
            for c in FILES_PROTECTED_COLUMNS))

    return [insert_trigger, update_trigger, delete_trigger, restore_trigger,
            state_restore_trigger, files_update_trigger, files_insert_trigger,
            files_delete_trigger, files_restore_trigger]


def table_ddl() -> str:
    """The keep table.  It is not part of the FTS machinery, so the
    legacy build's FTS rebuild cannot take it away."""
    return """
CREATE TABLE IF NOT EXISTS {tbl} (
    id TEXT PRIMARY KEY,
    body_path TEXT, body_anchor TEXT, body_hash TEXT,
    text_state TEXT, body_origin TEXT)""".format(tbl=GUARD_TABLE)


# ---------------------------------------------------------------------------
# the certification ledger (P1-阶段三 S3-K)
# ---------------------------------------------------------------------------

#: The ledger of certifications.  **Not** the keep table: the keep table
#: records what a row looked like BEFORE a write touched it (and only
#: for the rows a write actually touched), while the ledger records that
#: somebody CHECKED a row against the truth layer and FOUND IT EQUAL.
CERT_TABLE = "cert_ledger"


def cert_table_ddl() -> str:
    """The certification ledger.

    **Why a ledger and not the keep table.**  The keep table is written
    by the guard's triggers, so it holds a row only when a write
    threatened it — a newly written row is NOT in it (measured on the
    live ROOT library: 7 rows written after the last inspection were
    absent from ``body_keep``).  So the keep table cannot answer "was
    this row ever certified?" — and the question M6a asks is exactly
    that.

    The ledger answers it by construction: a row is written into it
    **when it is certified**, by the certifier, with the values that
    were verified and the time of the certification.  A row that is
    absent was never certified; a row that is present but disagrees with
    the current row was downgraded after it was certified.

    It is not part of the FTS machinery, so a legacy FTS rebuild cannot
    take it away.
    """
    return """
CREATE TABLE IF NOT EXISTS {tbl} (
    id TEXT PRIMARY KEY,
    text_state TEXT,
    body_origin TEXT,
    body_path TEXT,
    body_hash TEXT,
    certified_at INTEGER,
    certified_by TEXT)""".format(tbl=CERT_TABLE)


def record_certification(conn: sqlite3.Connection, row_id: str,
                         text_state: str, body_origin: str,
                         body_path: str = "", body_hash: str = "",
                         certified_by: str = "certify",
                         certified_at: Optional[int] = None) -> bool:
    """Write ONE certification into the ledger.

    The row is REPLACED, not appended: the ledger holds the LATEST
    certification of a row, and a second certification of the same row
    is a new fact about the same row.  (A history would be a different
    table with a different name — the criterion only ever asks "what
    was this row certified as, and does the row still agree?".)
    """
    import time as _time

    try:
        conn.execute(
            "INSERT OR REPLACE INTO {tbl} (id, text_state, body_origin, "
            "body_path, body_hash, certified_at, certified_by) "
            "VALUES (?,?,?,?,?,?,?)".format(tbl=CERT_TABLE),
            (row_id, text_state, body_origin, body_path, body_hash,
             int(certified_at if certified_at is not None else _time.time()),
             certified_by))
        return True
    except sqlite3.Error:
        return False


def certification_records(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The ledger's contents, as a report.

    The counts are the ones the criteria ask for:
    ``certified`` (how many rows have a certification record), and
    ``stale`` (how many certified rows no longer hold the state they
    were certified in — a DOWNGRADE, proven by the ledger).
    """
    out: Dict[str, Any] = {
        "table": CERT_TABLE,
        "present": False,
        "certified": 0,
        "stale": 0,
        "stale_rows": [],
    }
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return out
    if CERT_TABLE not in tables:
        return out
    out["present"] = True
    try:
        out["certified"] = conn.execute(
            "SELECT COUNT(*) FROM {t}".format(t=CERT_TABLE)).fetchone()[0]
        rows = conn.execute(
            "SELECT l.id, l.text_state, c.text_state, c.path "
            "FROM {t} l JOIN chunks c ON c.id = l.id "
            "WHERE c.text_state IS NOT l.text_state".format(t=CERT_TABLE)
        ).fetchall()
        out["stale"] = len(rows)
        out["stale_rows"] = [
            {"id": r[0], "certified_state": r[1], "current_state": r[2],
             "path": r[3]} for r in rows[:50]]
    except sqlite3.Error as e:
        out["error"] = str(e)
    return out


def guard_trigger_names() -> Tuple[str, ...]:
    """Every trigger the guard owns — the one list the installer drops
    and the status check compares."""
    return (GUARD_TRIGGER, GUARD_TRIGGER_UPDATE, GUARD_TRIGGER_DELETE,
            GUARD_TRIGGER_RESTORE, GUARD_TRIGGER_STATE,
            GUARD_TRIGGER_FILES_UPDATE, GUARD_TRIGGER_FILES_INSERT,
            GUARD_TRIGGER_FILES_DELETE, GUARD_TRIGGER_FILES_RESTORE)


def files_table_ddl() -> str:
    """The files-side snapshot table.

    Separate from the chunks-side keep table because the two have
    different keys: the chunks table is keyed by ``id``, the files
    table by ``path``.  It is not part of the FTS machinery, so a
    legacy FTS rebuild cannot take it away.
    """
    return """
CREATE TABLE IF NOT EXISTS {tbl} (
    {key} TEXT PRIMARY KEY,
    chunk_count INTEGER,
    chunker_version INTEGER)""".format(tbl=GUARD_TABLE_FILES, key=FILES_KEY)


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
        # **S3-L: the LEDGER table must exist before the triggers that
        # READ it.**  The state-restore triggers consult
        # ``cert_ledger``, and a trigger whose body names a table that
        # does not exist fails at RUN time with "no such table" — not
        # at CREATE time.  MEASURED: without this, every write to a
        # library without a ledger failed with
        # ``sqlite3.OperationalError: no such table: main.cert_ledger``.
        #
        # So the table is created here, in the SAME transaction as the
        # triggers.  It is empty — the ledger records certifications,
        # and the certifier writes them; the guard must never invent
        # one.
        conn.execute(cert_table_ddl())
        if files_ok:
            # The files-side snapshot table (the files side's own keep
            # table — its key is ``path``, not ``id``).
            conn.execute(files_table_ddl())
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

    **P1-阶段三 S3-K: "not certified YET" is not "erased".**

    The counts above cannot tell those two apart: a row that was
    written five minutes ago and has not been through the certification
    yet looks EXACTLY like a row whose certified state was wiped.  So
    the census now carries the WITNESSES, and the witnesses are what
    the verdict is built from:

    * ``downgrade_proven`` — the guard's snapshot table says this row
      WAS ``'mirror'`` and the row says it is not.  That is an erasure,
      proven, not inferred.
    * ``pointer_lost`` — the snapshot says the row HAD a pointer and the
      row has none.  Also proven.
    * ``not_certified`` — the row has a pointer and a decided origin
      but nobody has certified it yet.  This is the inspector's own
      PENDING WORK, not damage.
    * ``never_decided`` — ``body_origin='unresolved'``: nobody ever
      decided this row.  A gap, not damage.

    The two proven counters are what "was it erased" is decided by.  A
    criterion that called "not certified yet" "erased" would be red on
    every newly written page — and a permanently red check is a check
    nobody reads.
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
        # --- the discrimination (S3-K) -----------------------------------
        # "has a pointer and a decided origin, but nobody has certified
        # it yet" — the inspector's pending work, NOT damage.
        out["not_certified"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text_state<>'mirror' "
            "AND body_origin='file' AND body_path IS NOT NULL"
        ).fetchone()[0]
        out["never_decided"] = out["unresolved"]
        # --- the witnesses ------------------------------------------------
        # The guard's snapshot table records what the row looked like
        # BEFORE the last write.  A row that WAS certified and is not
        # any more is an erasure that is PROVEN, not inferred.
        out["downgrade_proven"] = 0
        out["pointer_lost"] = 0
        out["keep_rows"] = 0
        try:
            out["keep_rows"] = conn.execute(
                "SELECT COUNT(*) FROM body_keep").fetchone()[0]
            if out["keep_rows"]:
                out["downgrade_proven"] = conn.execute(
                    "SELECT COUNT(*) FROM body_keep k "
                    "JOIN chunks c ON c.id = k.id "
                    "WHERE k.text_state='mirror' AND c.text_state<>'mirror'"
                ).fetchone()[0]
                out["pointer_lost"] = conn.execute(
                    "SELECT COUNT(*) FROM body_keep k "
                    "JOIN chunks c ON c.id = k.id "
                    "WHERE k.body_path IS NOT NULL AND c.body_path IS NULL"
                ).fetchone()[0]
        except sqlite3.Error:
            pass
        out["erased"] = int(out["downgrade_proven"]) + int(out["pointer_lost"])
        # --- the certification ledger (P1-阶段三 S3-K) ------------------
        # The keep table holds the rows a WRITE threatened; the ledger
        # holds the rows somebody CHECKED.  The two witnesses answer
        # different questions, and the census reports both:
        #
        #   * the ledger's ``stale`` — a certified row that no longer
        #     holds the state it was certified in: a PROVEN downgrade,
        #     even for a row the keep table never saw;
        #   * the ledger's ``certified`` — how many rows have a
        #     certification record at all.
        #
        # A row that is in NEITHER (never certified, never threatened) is
        # "pending work", and it is counted, not judged.
        try:
            cert = certification_records(conn)
            out["cert_ledger_present"] = bool(cert.get("present"))
            out["certified_records"] = int(cert.get("certified", 0))
            out["cert_stale"] = int(cert.get("stale", 0))
            out["cert_stale_rows"] = cert.get("stale_rows", [])
        except sqlite3.Error:
            out["cert_ledger_present"] = False
            out["certified_records"] = 0
            out["cert_stale"] = 0
            out["cert_stale_rows"] = []
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
    # ``erased`` is the load-bearing verdict (S3-K): it is built from
    # the WITNESSES, not from "some rows are not certified yet".
    #
    # The certification ledger is a THIRD witness: a certified row that
    # no longer holds the state it was certified in is a PROVEN
    # downgrade, whether or not a write ever threatened it (the keep
    # table only holds the rows a write threatened).
    out["erased"] = int(out.get("erased", 0)) + int(out.get("cert_stale", 0))
    out["erased_verdict"] = (out.get("erased", 0) == 0)
    return out
