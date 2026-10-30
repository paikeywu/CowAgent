"""The cross-end fingerprint — SEVEN dimensions, one comparable token.

**Why this exists (P1-阶段三 第八轮 · DR-1).**

The previous fingerprint derived one token from the COLUMN SET of the
modelled tables.  It answered one question — "can these two indexes run
the same queries?" — and it answered it correctly.  But it was used to
answer a DIFFERENT question, and that is where the false green came
from:

    "are the 16 live libraries in the same shape?"

Two libraries can have identical column sets and still behave
differently, because a **trigger** is not a column.  Measured on this
machine: all 16 libraries reported the SAME fingerprint
(``beb5eae5…``) while their trigger BODIES had already forked — 14 of
them still restored ``text_state`` from the guard's keep table, and 2
restored it from the certification ledger.  Same schema, different
BEHAVIOUR, one green verdict.

The generalisation this module encodes:

    **A fingerprint that only covers what it names cannot see what it
    does not name.**  So the coverage must be stated, and the coverage
    must include everything that changes behaviour.

THE SEVEN DIMENSIONS
---------------------
Each dimension answers a question that the OTHER six cannot:

1. ``columns``      — which columns exist (the old fingerprint's whole
                      world; still necessary, still not sufficient).
2. ``constraints``   — column type / NOT NULL / DEFAULT.  Two indexes
                      can carry the same column names and disagree about
                      whether ``body_path`` may be NULL — which changes
                      what the writer may do.
3. ``triggers``      — the NORMALISED DEFINITION TEXT of every trigger.
                      NOT the name set: the 16 libraries' trigger NAMES
                      were identical, so a name-set fingerprint could
                      not tell them apart at all.  The TEXT can, because
                      the text says which table the restore reads from.
4. ``guard_tables`` — the guard tables' existence AND columns.  A
                      SEPARATE dimension on purpose: delete the ledger
                      and the triggers are still there, so a
                      trigger-only fingerprint would NOT move — but every
                      write would fail with "no such table".
5. ``indexes``      — the index set (the query planner's map).
6. ``chunker``      — the chunker version the index was cut with.
7. ``domains``      — the legal values of ``text_state`` /
                      ``body_origin``.  These are the three-state
                      machine; a value the code never reads is a value
                      the machine cannot reach.

THE NORMALISATION (and its one hard limit)
------------------------------------------
The trigger text is whitespace-collapsed before it is hashed.  That is
what makes the dimension useful:

  * a RE-WRAPPED trigger (the same statement, different line breaks)
    hashes the SAME — so a re-format does not report a false drift;
  * a RE-WORDED trigger (the same intent, different table name) hashes
    DIFFERENTLY — because "restore the state from ``body_keep``" and
    "restore the state from ``cert_ledger``" are DIFFERENT BEHAVIOURS.

That is the whole point of hashing the text rather than a semantic
summary of it: the dimension must be sensitive to behaviour and
insensitive to formatting.  The limit is stated here rather than
discovered later: the normalisation collapses whitespace ONLY.  It does
NOT canonicalise SQL, so two *semantically* identical triggers written
with different keyword casing would still report as different.  That is
a false positive we accept — a false positive is a look, a false
negative is a hole.

WHY NOT ONE BIG HASH
--------------------
:func:`cross_end_fingerprint` returns ONE token (so it can be compared),
but :func:`cross_end_dimensions` returns the per-dimension values, and
:func:`describe_cross_end_fingerprint` prints them.  A single hash that
cannot be decomposed cannot be debugged: the reader is told "these two
libraries differ" and not WHERE.  The dimensions are the diagnosis, the
token is the verdict.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

#: The tables the fingerprint covers.  Stated, printed, and auditable.
CROSS_END_TABLES: Tuple[str, ...] = ("chunks", "files")

#: The guard tables the fingerprint covers (dimension 4).
GUARD_TABLES: Tuple[str, ...] = ("body_keep", "files_keep", "cert_ledger")

#: The seven dimensions, in the order they are printed.
CROSS_END_DIMENSIONS: Tuple[str, ...] = (
    "columns",
    "constraints",
    "triggers",
    "guard_tables",
    "indexes",
    "chunker",
    "domains",
)

#: The ``_meta`` key that carries the CHUNKER version the index was cut
#: with.  ``last_migration_chunker_version`` is the CURRENT name (S3-L's
#: rename); ``chunker_version`` is the LEGACY name, still READ (a
#: historical library must not crash because a key was renamed).
META_CHUNKER_VERSION = "last_migration_chunker_version"
META_CHUNKER_VERSION_LEGACY = "chunker_version"


def normalize_sql(sql: Optional[str]) -> str:
    """Whitespace-collapsed SQL — the trigger text that gets hashed.

    **Why whitespace and nothing more.**  The dimension must be
    sensitive to a change of BEHAVIOUR and insensitive to a change of
    FORMATTING.  Collapsing whitespace is exactly that trade:

      * ``CREATE TRIGGER x\\nBEGIN ...`` and ``CREATE TRIGGER x BEGIN ...``
        collapse to the same string — a re-wrap is not a behaviour change;
      * ``... FROM body_keep ...`` and ``... FROM cert_ledger ...`` stay
        different — that IS a behaviour change.

    The normalisation is deliberately NOT a SQL canonicaliser: it does not
    upper-case keywords, does not reorder clauses, does not drop
    comments.  A trigger written in a different style would report as
    different — a false positive we accept, because a false positive
    costs a look and a false negative costs a hole.
    """
    if not sql:
        return ""
    return re.sub(r"\s+", " ", sql).strip()


def _tables_of(conn: sqlite3.Connection) -> List[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name").fetchall()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows]


def _dim_columns(conn: sqlite3.Connection) -> str:
    """Dimension 1 — the column SET of every modelled table.

    The column NAME is part of the hashed material, and the table NAME
    too: two indexes whose ``chunks`` tables match but whose ``files``
    tables do not must not collide.
    """
    parts: List[str] = []
    for table in CROSS_END_TABLES:
        try:
            rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        except sqlite3.Error:
            rows = []
        if not rows:
            # An absent table is NOT an empty column set: the schema is
            # INCOMPLETE, and saying so must change the token.
            parts.append("%s=<absent>" % table)
            continue
        parts.append("%s=%s" % (table, "|".join(sorted(str(r[1]) for r in rows))))
    return ";".join(parts)


def _dim_constraints(conn: sqlite3.Connection) -> str:
    """Dimension 2 — column type / NOT NULL / DEFAULT / PK.

    The old fingerprint deliberately described the column SET and NOT the
    constraints, because a fresh database and a migrated one declare
    them differently (F-1: a fresh ``CREATE`` gives ``path TEXT NOT
    NULL``; an ``ALTER``-migrated one keeps ``path TEXT``).  That
    reasoning was sound for the OLD question ("can these queries run?")
    and unsound for THIS one ("do these two libraries BEHAVE the same?").

    So the constraint dimension is reported SEPARATELY from the column
    dimension — and, crucially, it is **not** part of the token's
    equality across FRESH vs MIGRATED.  See
    :func:`cross_end_fingerprint`'s ``strict`` parameter: the default
    token is the one the cross-end gate compares, and it excludes the
    dimension that provably differs between a fresh and a migrated
    library (measured: ``end_line``/``path``/``scope``/``source``/
    ``text``/``hash``/``start_line``/``created_at``/``updated_at``).
    """
    parts: List[str] = []
    for table in CROSS_END_TABLES:
        try:
            rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        except sqlite3.Error:
            rows = []
        if not rows:
            parts.append("%s=<absent>" % table)
            continue
        items = []
        for r in rows:
            # (cid, name, type, notnull, dflt_value, pk)
            items.append("%s:%s:notnull=%d:dflt=%s:pk=%d"
                         % (r[1], r[2], r[3], r[4], r[5]))
        parts.append("%s=%s" % (table, "|".join(sorted(items))))
    return ";".join(parts)


def _dim_triggers(conn: sqlite3.Connection) -> str:
    """Dimension 3 — the NORMALISED DEFINITION TEXT of every trigger.

    **The text, not the names.**  Measured on this machine: the 16 live
    libraries had IDENTICAL trigger name sets and two DIFFERENT bodies,
    so a name-set fingerprint reported "consistent" for a fleet whose
    behaviour had already forked.
    """
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "ORDER BY name").fetchall()
    except sqlite3.Error:
        return "<unreadable>"
    if not rows:
        return "<none>"
    return ";".join("%s=%s" % (r[0], normalize_sql(r[1])) for r in rows)


def _dim_guard_tables(conn: sqlite3.Connection) -> str:
    """Dimension 4 — the guard tables' existence AND their columns.

    **Its own dimension on purpose.**  Delete the certification ledger
    and every trigger that reads it is still present and still looks
    correct — so a trigger-only fingerprint would NOT move.  But every
    write that fires the trigger would fail with "no such table", and
    the fleet would be broken in a way no other dimension can see.
    """
    present = set(_tables_of(conn))
    parts: List[str] = []
    for table in GUARD_TABLES:
        if table not in present:
            parts.append("%s=<absent>" % table)
            continue
        try:
            rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        except sqlite3.Error:
            rows = []
        parts.append("%s=%s" % (table, "|".join(sorted(str(r[1]) for r in rows))))
    return ";".join(parts)


def _dim_indexes(conn: sqlite3.Connection) -> str:
    """Dimension 5 — the index set over the modelled tables.

    Scoped to ``chunks``/``files``: the ROOT library also carries the
    conversation tables' indexes (``sessions``, ``runs``, ``messages``),
    which belong to ``conversation_store`` and are not this fingerprint's
    business.  Including them would make the ROOT library's fingerprint
    differ from every agent's for a reason that has nothing to do with
    the memory index.
    """
    try:
        rows = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
            "ORDER BY name").fetchall()
    except sqlite3.Error:
        return "<unreadable>"
    scoped = [(n, t) for n, t in rows if t in CROSS_END_TABLES]
    if not scoped:
        return "<none>"
    return ";".join("%s@%s" % (n, t) for n, t in scoped)


def _dim_chunker(conn: sqlite3.Connection) -> str:
    """Dimension 6 — the chunker version the index was cut with.

    **Both key spellings are read.**  The key was renamed from
    ``chunker_version`` to ``last_migration_chunker_version`` (S3-L's
    ruling), but a library that was stamped before the rename still
    carries the OLD key — and a reader that only knows the new name
    would report "no version" for a library that HAS one.  The new name
    wins when both are present (it is the newer fact), and the old name
    is read as the fallback.

    The dimension reports the value it FOUND, and a value that is
    present in one library and absent in another is a difference — which
    is the point: a library that was never stamped is not the same as
    one that was stamped with the current version.
    """
    if "_meta" not in set(_tables_of(conn)):
        return "_meta=<absent>"
    for key in (META_CHUNKER_VERSION, META_CHUNKER_VERSION_LEGACY):
        try:
            row = conn.execute(
                "SELECT value FROM _meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0] is not None:
            return "_meta=%s=%s" % (key, row[0])
    return "_meta=(none)"


def _dim_domains(conn: sqlite3.Connection) -> str:
    """Dimension 7 — the legal values of the two state columns.

    These are the three-state machine's alphabet, and they are derived
    from the CODE (the one place the legal values are defined), so the
    dimension cannot drift away from what the code accepts.
    """
    from agent.memory.body_guard import DECIDED_VALUES, UNDECIDED_VALUES
    from agent.memory.body_pointer import BODY_ORIGINS

    states = sorted(set(DECIDED_VALUES.get("text_state", ()))
                    | set(UNDECIDED_VALUES.get("text_state", ())))
    origins = sorted(BODY_ORIGINS)
    return "text_state=%s;body_origin=%s" % ("|".join(states),
                                              "|".join(origins))


def cross_end_dimensions(db_path: Union[str, Path]) -> Optional[Dict[str, str]]:
    """Every dimension's value, as a dict.  None when there is no index."""
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        return None
    try:
        return {
            "columns": _dim_columns(conn),
            "constraints": _dim_constraints(conn),
            "triggers": _dim_triggers(conn),
            "guard_tables": _dim_guard_tables(conn),
            "indexes": _dim_indexes(conn),
            "chunker": _dim_chunker(conn),
            "domains": _dim_domains(conn),
        }
    except Exception:
        return None
    finally:
        conn.close()


#: The dimensions that make up the COMPARABLE token.
#
#: ``constraints`` is EXCLUDED, and the reason is measured, not
#: preferred: a FRESH library and a MIGRATED library provably differ in
#: it (``end_line``/``path``/``scope``/``source``/``text``/``hash``/
#: ``start_line``/``created_at``/``updated_at`` carry NOT NULL and
#: DEFAULTs in one and not the other).  Including it would make the
#: cross-end gate report a drift between a brand-new workspace and a
#: migrated one — a false red on a healthy system, which is how a gate
#: gets ignored.
#:
#: The dimension is still MEASURED and still PRINTED — it is a real
#: difference between two libraries, and a reader who wants to know
#: about it can.  What it must not be is part of the token the
#: cross-end gate compares.
TOKEN_DIMENSIONS: Tuple[str, ...] = (
    "columns",
    "triggers",
    "guard_tables",
    "indexes",
    "chunker",
    "domains",
)


def cross_end_fingerprint(db_path: Union[str, Path]) -> Optional[str]:
    """The cross-end token: the sha256 of the comparable dimensions.

    One token, so two libraries can be compared with ``==``.  The
    per-dimension values are available separately
    (:func:`cross_end_dimensions`) so a difference can be DIAGNOSED and
    not merely detected.
    """
    dims = cross_end_dimensions(db_path)
    if dims is None:
        return None
    parts = []
    for name in TOKEN_DIMENSIONS:
        parts.append("%s=%s" % (name, dims.get(name, "")))
    return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()


def cross_end_dimension_fingerprint(dims: Dict[str, str],
                                    dimension: str) -> str:
    """The sha256 of ONE dimension — so a drift can be located exactly."""
    value = dims.get(dimension, "")
    return hashlib.sha256(
        ("%s=%s" % (dimension, value)).encode("utf-8")).hexdigest()[:16]


def describe_cross_end_fingerprint(db_path: Union[str, Path]) -> str:
    """One line: the token AND the dimensions it covers.

    ``"<fp> (dims=columns,triggers,guard_tables,indexes,chunker,domains)"``
    — so every place that prints a cross-end token can also print what
    it means.
    """
    dims = cross_end_dimensions(db_path)
    fp = cross_end_fingerprint(db_path)
    covered = ",".join(TOKEN_DIMENSIONS)
    if fp is None:
        return "none (dims=%s)" % covered
    return "%s (dims=%s)" % (fp, covered)


def describe_cross_end_dimensions(db_path: Union[str, Path]) -> str:
    """A multi-line report: every dimension, its value and its hash.

    This is the SELF-PROOF the installer prints: "which libraries carry
    the new definition" must be visible at a glance, not established by
    somebody comparing two blocks of SQL by hand.
    """
    dims = cross_end_dimensions(db_path)
    if dims is None:
        return "no index"
    lines = []
    for name in CROSS_END_DIMENSIONS:
        value = dims.get(name, "")
        digest = cross_end_dimension_fingerprint(dims, name)
        lines.append("  %-12s %s" % (name, digest))
        if name in ("triggers", "columns", "guard_tables"):
            # Long values: show the beginning only, with its length, so
            # the reader knows it was truncated.
            shown = value if len(value) <= 160 else value[:160] + "..."
            lines.append("               %s (%d chars)" % (shown, len(value)))
    return "\n".join(lines)
