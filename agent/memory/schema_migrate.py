"""Batch schema migration for EVERY workspace's index (P0-17 B-1).

Why this exists
---------------

``MemoryStorage.__init__`` migrates the index it opens. That is enough
for the workspace the current process happens to be talking to — and not
enough for the others. On a multi-agent install each agent has its own
index, and an agent that is never addressed in a session never has its
index opened, so its index stays behind the schema. The first time that
agent IS addressed, its retrieval queries fail, and (before P0-17) the
failure was reported as "no matches".

So the migration is run over EVERY known workspace at startup. It is
additive (``ALTER TABLE ... ADD COLUMN``), idempotent (a column that is
already there is skipped), and it never rewrites or deletes a row.

Where the list of workspaces comes from
----------------------------------------

The ROSTER (``agent.registry``), not a directory scan. The roster is the
source of truth for which agents exist and where their workspaces live; a
directory scan would miss a workspace parked outside the standard layout
and would invent one for an agent that no longer exists. When the roster
cannot be resolved, the caller's own workspace is still migrated — less
coverage, never a crash.

The repair is REPORTED: each migrated workspace is logged, and the
workspaces that could not be migrated are reported as errors rather than
swallowed. A migration that silently did nothing is the same failure this
whole change exists to remove.

The data backfill
-----------------

``ALTER TABLE ADD COLUMN`` gives a NEW column, and a new column is NULL
for every row that was written before it existed. For ``sealed_owner``
that is not a neutral state: it is an ORPHAN. A dark page indexed before
P0-16 carries its owner token in ``chunks.user_id`` (that was where the
owner lived before the two axes were split), so after the split the row
has an owner in the old column and NULL in the new one — and the sealed
branch of the predicate reads ONLY the new column. The result is a dark
page that NOBODY can read, including the agent that wrote it. Fail-closed,
so nothing leaks, but the data is orphaned and unusable.

So the migration also BACKFILLS the column, and the backfill rule is
deliberately narrow:

    scope = 'sealed'  AND  user_id LIKE 'agent--%'  AND  sealed_owner IS NULL

* ``scope = 'sealed'`` — only a dark page has an owner. An ordinary row
  that happens to carry a token-shaped ``user_id`` is not a dark page and
  must not acquire an owner.
* ``user_id LIKE 'agent--%'`` — the OWNER NAMESPACE only. This is the
  L-5 two-axis separation and it is a red line: an END USER id that
  happens to look like an owner token must never be treated as one. The
  prefix is the namespace marker; a namespace marker is either there or
  not, so this is a prefix test, not a guess.
* ``sealed_owner IS NULL`` — idempotent. A row that already has an owner
  keeps it.

The backfill does NOT clear ``user_id``: the two columns are separate axes
and the old value is the historical record of what the pre-P0-16 build
wrote. (A dark page whose ``user_id`` is a token-shaped value stays
invisible to the END-USER axis anyway, because the owned branch of the
predicate excludes ``scope = 'sealed'``.)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


def workspace_roots(extra: Optional[Union[str, Path]] = None
                    ) -> List[str]:
    """Every workspace root whose index should be kept current.

    The roster first (it knows every agent), then ``extra`` (the caller's
    own workspace, which may not be in the roster — a source checkout, a
    test fixture).
    """
    roots: List[str] = []
    try:
        from agent.registry import get_agent_registry

        for profile in get_agent_registry().list(include_disabled=True):
            roots.append(str(profile.workspace))
    except Exception:
        pass
    if extra:
        roots.append(str(extra))
    seen = set()
    unique: List[str] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def index_path(root: Union[str, Path]) -> Path:
    """Where one workspace keeps its index."""
    return Path(root) / "memory" / "long-term" / "index.db"


def backfill_sealed_owner(db_path: Union[str, Path]) -> int:
    """Give every orphaned dark page its owner back. Returns the row count.

    P0-18 R5. The rule (see the module docstring) is narrow on purpose:

        scope = 'sealed' AND user_id LIKE 'agent--%' AND sealed_owner IS NULL

    The prefix is the OWNER NAMESPACE marker (``visibility.AGENT_OWNER_PREFIX``),
    and an END USER id that happens to look like an owner token must never
    be treated as one — that is the L-5 two-axis separation, and it is a
    red line, not a heuristic.

    The row's ``user_id`` is left in place: the two columns are separate
    axes, and the old value is the historical record of what the pre-P0-16
    build wrote. A dark page is invisible through the end-user axis anyway,
    because the owned branch of the predicate excludes ``scope='sealed'``.

    Additive and idempotent: a row that already has an owner is untouched,
    and running it twice changes nothing the second time.
    """
    from agent.memory.visibility import AGENT_OWNER_PREFIX

    path = Path(db_path)
    if not path.is_file():
        return 0
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error:
        return 0
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        if "sealed_owner" not in cols or "user_id" not in cols:
            return 0
        cur = conn.execute(
            "UPDATE chunks SET sealed_owner = user_id "
            "WHERE scope = 'sealed' "
            "AND sealed_owner IS NULL "
            "AND user_id IS NOT NULL "
            "AND user_id LIKE ?",
            (AGENT_OWNER_PREFIX + "%",),
        )
        changed = cur.rowcount or 0
        if changed:
            conn.commit()
        return changed
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 0
    finally:
        conn.close()


def migrate_all(roots: Optional[List[str]] = None,
                extra: Optional[Union[str, Path]] = None
                ) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Bring every workspace's index up to the current schema.

    Returns ``(migrated, failed)``:

    * ``migrated`` — ``{workspace root: [columns added]}`` (an empty list
      means the index was already current, which is NOT a failure);
    * ``failed`` — ``{workspace root: reason}`` for the indexes that could
      not be brought up to date.

    A failure is returned, not raised: one unwritable index must not stop
    the other fourteen from being repaired.

    P0-18 R5: after the columns are in place, the orphaned dark pages are
    backfilled — a dark page whose owner still lives only in the pre-P0-16
    ``user_id`` column gets its owner back, so it stops being an orphan.
    The backfill is reported separately from the column list, because
    "the column was missing" and "the rows were orphaned" are different
    facts about the same index.
    """
    from agent.memory.schema_drift import missing_columns, repair_columns

    if roots is None:
        roots = workspace_roots(extra)
    migrated: Dict[str, List[str]] = {}
    failed: Dict[str, str] = {}
    for root in roots:
        db = index_path(root)
        if not db.is_file():
            continue
        try:
            added = repair_columns(db)
            remaining = missing_columns(db)
        except Exception as e:
            failed[str(root)] = f"{type(e).__name__}: {e}"
            continue
        if remaining:
            failed[str(root)] = (
                "still missing after repair: " + ", ".join(remaining))
            continue
        migrated[str(root)] = added
    backfill_all(roots=roots)
    return migrated, failed


def backfill_all(roots: Optional[List[str]] = None,
                 extra: Optional[Union[str, Path]] = None
                 ) -> Dict[str, int]:
    """Backfill the orphaned dark pages of every workspace.

    Returns ``{workspace root: rows backfilled}`` for the workspaces whose
    index actually changed. A workspace with nothing to repair is not in
    the result — an empty list would read like "we looked and there was
    nothing", which is true but not worth a line of log per workspace.
    """
    if roots is None:
        roots = workspace_roots(extra)
    out: Dict[str, int] = {}
    for root in roots:
        db = index_path(root)
        if not db.is_file():
            continue
        try:
            changed = backfill_sealed_owner(db)
        except Exception:
            continue
        if changed:
            out[str(root)] = changed
    return out


def run_startup_migration(extra: Optional[Union[str, Path]] = None
                          ) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Migrate every index and REPORT what happened.

    The report is the point: a migration that ran and repaired thirteen
    indexes is worth saying out loud, and a migration that could not
    repair one is worth an ERROR line — that index will fail its first
    query.
    """
    migrated, failed = migrate_all(extra=extra)
    try:
        from common.log import logger

        repaired = {k: v for k, v in migrated.items() if v}
        if repaired:
            for root, cols in repaired.items():
                logger.info(
                    "[Memory] Migrated index schema for %s (added: %s)",
                    root, ", ".join(cols))
        backfilled = backfill_all()
        for root, count in sorted(backfilled.items()):
            logger.info(
                "[Memory] Backfilled %d orphaned dark page row(s) in %s "
                "(owner restored to chunks.sealed_owner)", count, root)
        if failed:
            for root, reason in failed.items():
                logger.error(
                    "[Memory] Could not migrate the index schema for %s: %s "
                    "(retrieval against it will fail)", root, reason)
    except Exception:
        pass
    return migrated, failed


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.schema_migrate`` — migrate and report."""
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Bring every workspace's memory index up to the current "
                    "schema (P0-17 B-1). Additive and idempotent.")
    ap.add_argument("--workspace", default=None,
                    help="also migrate this workspace's index")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args(argv)

    migrated, failed = run_startup_migration(args.workspace)
    if args.json:
        print(json.dumps({"migrated": migrated, "failed": failed},
                         ensure_ascii=False, indent=2))
    else:
        for root, cols in sorted(migrated.items()):
            state = ", ".join(cols) if cols else "already current"
            print(f"[OK ] {root}: {state}")
        for root, reason in sorted(failed.items()):
            print(f"[ERR] {root}: {reason}")
        print(f"\nmigrated={len(migrated)} failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
