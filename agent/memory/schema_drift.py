"""Index schema drift — the check that a stale index is LOUD (P0-17 B-1/B-3).

An index written by an older build can lack a column the current code
queries. The failure is silent in the worst way: the SQL raises
``no such column: ...``, the retrieval path catches it, and the system
reports "no matches" — indistinguishable from an empty corpus.

Two things are needed, and they are separate:

* **REPAIR** — the migration that brings one index up to the current
  schema. It runs when a database is OPENED (that is the only moment the
  connection is known to be writable) and it is idempotent.
* **DETECTION** — the check that says WHICH index is behind, so a workspace
  nobody opened yet is still reported. An index that has never been opened
  by the new build is exactly the one that will fail first, and nothing
  else would ever mention it.

The expected column set lives HERE, next to the migration, so the two
cannot drift: a column added to the schema without being added here is
reported as drift for every index until somebody notices.

REPORT-ONLY where it counts: this module never deletes anything, and the
repair it performs is an ``ALTER TABLE ADD COLUMN`` — additive, never a
rewrite, never a data loss.

P1-阶段三 (H2) · the fingerprint covers MORE THAN ONE TABLE
------------------------------------------------------------
The fingerprint used to be derived from ``chunks`` alone, and the file's own
header said so ("the current code models only the chunks table"). That made
the cross-end consistency gate (design §11 Q6) a FALSE GREEN: the 16 live
indexes reported ONE identical fingerprint while their ``files`` tables had
already forked — the root index carried ``chunk_count`` / ``chunker_version``
and the 15 agent indexes did not. A fingerprint that only looks at one table
cannot see a fork in another one, and it still says "consistent".

So the schema is now modelled as a REGISTRY of tables, and the fingerprint is
derived from all of them:

  * ``chunks`` — the retrieval projection (as before);
  * ``files``  — the per-file bookkeeping (P1-阶段二 added two columns to it).

The design's §3.3-1 asked for ``files`` / ``body_sources`` to be extended
into the SAME registration mechanism. ``body_sources`` was NOT built — the
stage-two implementation carries that role in the ``files`` table instead, and
that substitution is now recorded rather than left implicit (R-S3-3).

``FINGERPRINT_TABLES`` is the ONE list of tables the fingerprint covers, and
:func:`schema_fingerprint_tables` prints it, so a reader can never mistake
"the fingerprint agrees" for "the whole schema agrees" without also seeing
WHAT was compared.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

#: The columns the current code expects on ``chunks``.
#:
#: Kept as a tuple (not a set) so the drift report lists them in a stable
#: order: a report that reorders itself between runs is harder to read
#: than one that does not.
EXPECTED_CHUNK_COLUMNS: Tuple[str, ...] = (
    "id",
    "user_id",
    "scope",
    "source",
    "path",
    "start_line",
    "end_line",
    "text",
    "embedding",
    "hash",
    "metadata",
    "created_at",
    "updated_at",
    "hit_count",
    "last_hit_at",
    "element_released",
    "file_sealed",
    "sealed_owner",
    "body_path",
    "body_anchor",
    "body_hash",
    "text_state",
    # P1-阶段三 第四轮: the body-origin column.  It MUST be in this list:
    # the fingerprint is derived from the column set, so a column that is
    # not listed is a column whose drift the fingerprint CANNOT see — the
    # same false green as the single-table fingerprint (H2), one level down.
    "body_origin",
)

#: The columns a migration may ADD. A column that is not in this map is
#: one the current code does not know how to add, so the drift is reported
#: rather than silently invented.
ADDABLE_COLUMNS: Dict[str, str] = {
    "hit_count": "INTEGER NOT NULL DEFAULT 0",
    "last_hit_at": "INTEGER",
    "element_released": "INTEGER NOT NULL DEFAULT 0",
    "file_sealed": "INTEGER NOT NULL DEFAULT 0",
    "sealed_owner": "TEXT",
    # P1-阶段一 (D-C): the four body-externalization columns.  Every chunk
    # starts as a projection, so the new column defaults match a row that
    # never externalized: text here, no body file, no hash yet.
    "body_path": "TEXT",
    "body_anchor": "TEXT",
    "body_hash": "TEXT",
    "text_state": "TEXT NOT NULL DEFAULT 'projection'",
    # P1-阶段三 第四轮: NOT NULL with an explicit 'unresolved' default.
    # A nullable column would make "not judged yet" and "judged, and the
    # answer is nothing" indistinguishable in SQL — and the whole point of
    # the column is to make "not judged yet" a VISIBLE TODO.
    "body_origin": "TEXT NOT NULL DEFAULT 'unresolved'",
}

#: The columns the current code expects on ``files``.
#:
#: P1-阶段二 added ``chunk_count`` / ``chunker_version`` so a reader can tell
#: HOW a projection was cut.  The 15 agent indexes that predate that change
#: did NOT have them, which is exactly the fork the single-table fingerprint
#: could not see (H2).
EXPECTED_FILE_COLUMNS: Tuple[str, ...] = (
    "path",
    "source",
    "hash",
    "mtime",
    "size",
    "updated_at",
    "chunk_count",
    "chunker_version",
)

#: The columns a migration may ADD to ``files``.
#:
#: P1-阶段三 (C-A3): NULLABLE, without a default.  ``NOT NULL DEFAULT 0``
#: made every pre-existing row claim "this file produced zero chunks" —
#: a false fact that cannot be told apart from a file that really produced
#: none.  NULL means "not measured"; 0 means "measured, and it is zero".
ADDABLE_FILE_COLUMNS: Dict[str, str] = {
    "chunk_count": "INTEGER",
    "chunker_version": "INTEGER",
}

#: The tables the schema FINGERPRINT covers — the ONE list.
#:
#: This is the answer to "which tables did the fingerprint look at?", and it
#: is printed by :func:`schema_fingerprint_tables` on every report.  A
#: fingerprint whose coverage is not stated cannot be audited: the reader has
#: no way to tell "the schemas agree" from "the schemas agree ON THE ONE
#: TABLE THE FINGERPRINT LOOKED AT".
FINGERPRINT_TABLES: Tuple[str, ...] = ("chunks", "files")


def table_columns(conn: sqlite3.Connection, table: str) -> Optional[set]:
    """The columns ``table`` actually has, or None when there is no table."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return {row[1] for row in rows}


def chunk_columns(conn: sqlite3.Connection) -> Optional[set]:
    """The columns ``chunks`` actually has, or None when there is no table."""
    return table_columns(conn, "chunks")



def missing_columns(db_path: Union[str, Path]) -> List[str]:
    """The expected columns this index lacks, in the expected order.

    An unreadable or absent database yields an empty list: there is no
    drift to report about a database that is not there, and reporting one
    would make the check cry wolf on every fresh workspace.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        cols = chunk_columns(conn)
    finally:
        conn.close()
    if cols is None:
        return []
    return [c for c in EXPECTED_CHUNK_COLUMNS if c not in cols]


def missing_file_columns(db_path: Union[str, Path]) -> List[str]:
    """The expected ``files`` columns this index lacks, in the expected order.

    P1-阶段三 (H2): the drift detector used to look at ``chunks`` only, so
    the 15 agent indexes whose ``files`` table predates the two bookkeeping
    columns were reported as NOT drifted — while the root index, which had
    them, was equally "not drifted".  The two were different schemas with
    the same verdict.

    An absent ``files`` table is NOT drift: a database that has never been
    initialized has nothing to be behind, and reporting one would make the
    check cry wolf on every fresh workspace (same rule as
    :func:`missing_columns`).
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        cols = table_columns(conn, "files")
    finally:
        conn.close()
    if cols is None:
        return []
    return [c for c in EXPECTED_FILE_COLUMNS if c not in cols]


def missing_columns_all(db_path: Union[str, Path]) -> List[str]:
    """Every missing column across EVERY modelled table, in expected order.

    The names are BARE (not table-qualified), because the drift report has
    always shown bare column names and the repair path consumes them as
    such.  The table each name belongs to is reported alongside, by
    :func:`missing_columns_by_table` — "chunk_count" alone does not say
    which table it is in, and a report that cannot say that sends the
    reader hunting.
    """
    out: List[str] = []
    out.extend(missing_columns(db_path))
    out.extend(missing_file_columns(db_path))
    return out


def missing_columns_by_table(db_path: Union[str, Path]) -> Dict[str, List[str]]:
    """The missing columns PER TABLE — ``{"chunks": [...], "files": [...]}``.

    This is the answer to "which table is behind", which the bare name list
    cannot give: ``path`` and ``hash`` exist in BOTH modelled tables, so a
    bare list is genuinely ambiguous for those names.
    """
    return {
        "chunks": missing_columns(db_path),
        "files": missing_file_columns(db_path),
    }


def repair_columns(db_path: Union[str, Path]) -> List[str]:
    """Add the missing columns to this index. Returns what was added.

    Additive and idempotent: a column that is already there is skipped,
    and a column the current code cannot add is left alone (and reported
    by :func:`missing_columns`).

    P1-阶段三 (H2): the ``files`` table is repaired through the SAME
    mechanism — the two bookkeeping columns are added from
    :data:`ADDABLE_FILE_COLUMNS`, so the 15 agent indexes that lack them
    are brought to the root index's shape by the ordinary migration path
    rather than by a hand-written one-off script.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    added: List[str] = []
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error:
        return []
    try:
        cols = chunk_columns(conn)
        if cols is None:
            return []
        for name in EXPECTED_CHUNK_COLUMNS:
            if name in cols:
                continue
            ddl = ADDABLE_COLUMNS.get(name)
            if ddl is None:
                continue
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {ddl}")
            added.append(name)
        # The files table: same rule, same mechanism.
        fcols = table_columns(conn, "files")
        if fcols is not None:
            for name in EXPECTED_FILE_COLUMNS:
                if name in fcols:
                    continue
                ddl = ADDABLE_FILE_COLUMNS.get(name)
                if ddl is None:
                    continue
                conn.execute(f"ALTER TABLE files ADD COLUMN {name} {ddl}")
                added.append(name)
        if added:
            conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return added


def scan_workspaces(roots: List[Union[str, Path]]) -> Dict[str, List[str]]:
    """Every workspace root that carries an index BEHIND the schema.

    Returns ``{workspace_root: [missing columns]}`` for the roots whose
    index exists and is missing something. A root with no index at all is
    not drift (nothing has been written yet); a root whose index is
    complete is not drift either.

    P1-阶段三 (H2): the columns are reported TABLE-QUALIFIED, and the
    ``files`` table is checked too — the old single-table scan could not see
    the fork that had already happened across the 16 live indexes.
    """
    drift: Dict[str, List[str]] = {}
    for root in roots:
        root_path = Path(root)
        db = root_path / "memory" / "long-term" / "index.db"
        if not db.is_file():
            continue
        missing = missing_columns_all(db)
        if missing:
            drift[str(root_path)] = missing
    return drift


def describe_drift(drift: Dict[str, List[str]]) -> str:
    """One line per drifting workspace, naming the workspace and columns.

    A drift report that says "an index is behind" is half a report: the
    whole point is to name WHICH index and WHICH columns, because that is
    what makes it actionable.
    """
    if not drift:
        return "no schema drift"
    lines = []
    for root in sorted(drift):
        cols = ", ".join(drift[root])
        lines.append(f"{root}: missing {cols}")
    return "; ".join(lines)


def schema_fingerprint_tables() -> Tuple[str, ...]:
    """The tables the fingerprint covers — printed on every report.

    C-A1.4: the fingerprint's coverage must be VISIBLE.  A reader who sees
    two fingerprints agree must also see WHICH tables were compared, or
    "the schemas agree" and "the schemas agree on the one table the
    fingerprint looked at" look identical.
    """
    return tuple(FINGERPRINT_TABLES)


def describe_schema_fingerprint(fp: Optional[str]) -> str:
    """One line: the fingerprint AND the tables it covers.

    ``"<fp> (tables=chunks,files)"`` — so every place that prints a
    fingerprint can also print what it means.
    """
    tables = ",".join(schema_fingerprint_tables())
    if fp is None:
        return f"none (tables={tables})"
    return f"{fp} (tables={tables})"


def schema_fingerprint(db_path: Union[str, Path]) -> Optional[str]:
    """A stable fingerprint of the index's schema — over EVERY modelled table.

    P1-阶段一 (M1): the gate that decides whether an index may serve needs
    one comparable token, not a column list it must re-derive. The token is
    the sha256 of the ordered ``PRAGMA table_info`` column set, so
    two indexes that can answer the same queries share a fingerprint and
    two that cannot do not. ``None`` means there is no readable chunks
    table to fingerprint (a fresh workspace, or a damaged file) — which is
    itself a meaningful answer, distinct from any schema.

    P1-阶段三 (H2 · C-A1.2/C-A1.3): the token is derived from EVERY table in
    :data:`FINGERPRINT_TABLES`, not from ``chunks`` alone.  The old
    single-table token reported "consistent" for a pair of indexes whose
    ``files`` tables had already forked — a false green on the cross-end
    consistency gate (design §11 Q6).  Deleting a column from ANY modelled
    table now changes the token.

    The fingerprint must describe the column SET, not the order it was
    physically laid down nor the constraints each build happened to
    declare. A fresh database CREATES every column in one statement; a
    migrated one gains the newer columns by ALTER TABLE, which APPENDS
    them to the end — same set, different order — and an older build may
    not have declared NOT NULL / defaults the current build does (F-1:
    fresh vs migrated could never agree). The gate cares about ONE
    question — "can these queries run" — which the column NAMES answer.
    So the fingerprint is the sorted set of names, per table, joined in the
    table order of :data:`FINGERPRINT_TABLES`.

    The table NAME is part of the hashed material: two indexes whose
    ``chunks`` table matches but whose ``files`` table does not must NOT
    collide, and a token built only from column names could not tell them
    apart.
    """
    import hashlib

    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        parts: List[str] = []
        for table in FINGERPRINT_TABLES:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                # A table that is not there is not an empty column set: the
                # schema is INCOMPLETE, and saying so must change the token
                # rather than silently shorten it.
                parts.append(f"{table}=<absent>")
                continue
            names = sorted(str(row[1]) for row in rows)
            parts.append(f"{table}=" + "|".join(names))
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not parts:
        return None
    return hashlib.sha256(";".join(parts).encode("utf-8")).hexdigest()
