"""T4 — the ``_meta`` chunker-version key, renamed without breaking a reader.

**The conflict.**  Every live library carries TWO records of "which
chunker cut this index":

  * ``_meta['chunker_version'] = '3'``  — the stamp the manager writes
    when it rebuilds an index from empty;
  * ``files.chunker_version``            — the version that cut EACH
    FILE, written by ``update_file_metadata``.

and they DISAGREE.  MEASURED on this machine: ``_meta`` says 3 in all
16 libraries, while ``files.chunker_version`` says 4 (the current
chunker) for 94 of ROOT's 98 files, 70 of 87 in agent-mvnes449, and so
on — with a handful of rows at NULL ("not measured").

The disagreement is not cosmetic, and the direction matters: ``_meta``
carries the OLD version (3) and ``files`` carries the CURRENT one (4).
The ``_meta`` stamp is written by a code path that only fires on a
rebuild-from-empty (``manager.py``: "Stamp the chunker version only
when we rebuilt from an empty index"), so an index that was built by
the OLD chunker and later partially re-cut by the new one keeps its
old stamp forever.  A reader that trusts ``_meta`` therefore learns
"this index was cut by chunker 3" — which is FALSE for most of its
files.

**The ruling.**  ``files.chunker_version`` is the authority, because it
is the per-file fact: it says which chunker cut THIS file, and it is
written on every write.  ``_meta``'s value is a whole-index claim that
is only ever true for a library rebuilt from empty.

**The rename, and the compatibility rule.**  The ``_meta`` key becomes
``last_migration_chunker_version`` — the name says what it actually
is: the version at the last MIGRATION, not "the version of this
index".  A key that lies about what it holds is a key that will be
read as a fact.

**The old key is NOT deleted.**  Two reasons, and the second is the
one that matters:

  1. a library that was stamped before the rename carries only the OLD
     key, and a reader that knows only the new name would report "no
     version" for a library that HAS one — the very failure this whole
     round is about (a fact that is present but invisible);
  2. the rule the team follows — "原始事实不折叠", never physically
     delete — applies to the historical stamp: it is a record of what
     the index was cut with, and deleting it destroys the only evidence
     of that.

So the migration COPIES the old value to the new key and LEAVES the old
key in place, marked as a legacy spelling.  Every reader accepts both.

**Who reads this.**  ``agent.memory.embedding.state.detect_chunker_version``
is the reader — it feeds ``/memory status``, which tells the user
whether the index should be rebuilt.  It is updated to read BOTH
spellings, newest first.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

#: The CURRENT name — what the value actually IS.
META_KEY = "last_migration_chunker_version"

#: The LEGACY name — kept, read, and never deleted.
META_KEY_LEGACY = "chunker_version"


def read_chunker_version(conn: sqlite3.Connection) -> Optional[str]:
    """The chunker version the index was cut with — BOTH spellings read.

    The current name wins when both are present (it is the newer fact);
    the legacy name is the fallback.  Returns None when neither is
    there — "no record", which is a different answer from "recorded as
    3".
    """
    for key in (META_KEY, META_KEY_LEGACY):
        try:
            row = conn.execute(
                "SELECT value FROM _meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        if row and row[0] is not None:
            return str(row[0])
    return None


def migrate_meta_key(db: Union[str, Path],
                     dry_run: bool = False) -> Dict[str, Any]:
    """Copy the legacy ``_meta`` stamp to the new key.  Idempotent.

    The legacy key is LEFT IN PLACE — see the module docstring.
    """
    path = Path(db)
    out: Dict[str, Any] = {
        "db": str(path),
        "dry_run": bool(dry_run),
        "legacy_value": None,
        "new_value_before": None,
        "new_value_after": None,
        "copied": False,
        "legacy_kept": False,
    }
    if not path.is_file():
        out["error"] = "the index does not exist"
        return out
    conn = sqlite3.connect(str(path))
    try:
        def _get(key):
            row = conn.execute(
                "SELECT value FROM _meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

        out["legacy_value"] = _get(META_KEY_LEGACY)
        out["new_value_before"] = _get(META_KEY)
        if out["legacy_value"] is None:
            out["detail"] = ("the legacy key is absent — nothing to copy "
                             "(a library that was never stamped has no "
                             "version to migrate)")
            return out
        if out["new_value_before"] == out["legacy_value"]:
            out["detail"] = "the new key already holds the legacy value"
            out["new_value_after"] = out["new_value_before"]
            out["legacy_kept"] = True
            return out
        if dry_run:
            out["detail"] = ("would copy %s=%s into %s"
                             % (META_KEY_LEGACY, out["legacy_value"],
                                META_KEY))
            out["new_value_after"] = out["legacy_value"]
            return out
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, ?)",
            (META_KEY, str(out["legacy_value"])))
        conn.commit()
        out["copied"] = True
        out["new_value_after"] = out["legacy_value"]
        out["legacy_kept"] = _get(META_KEY_LEGACY) is not None
        out["detail"] = ("copied %s=%s into %s; the legacy key is KEPT "
                         "(a reader that knows only one spelling must not "
                         "find the record missing)"
                         % (META_KEY_LEGACY, out["legacy_value"], META_KEY))
    except sqlite3.Error as e:
        out["error"] = "sqlite error: %s" % e
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return out


def migrate_meta_key_family(workspace: Union[str, Path],
                            dry_run: bool = False) -> Dict[str, Any]:
    """Migrate the key in EVERY live library."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    for lib in enumerate_live(root):
        rep = migrate_meta_key(lib["db"], dry_run=dry_run)
        rep["library"] = lib["name"]
        reports.append(rep)
    return {
        "workspace": str(root),
        "dry_run": bool(dry_run),
        "library_count": len(reports),
        "libraries": reports,
        "copied": sum(1 for r in reports if r.get("copied")),
    }


def judge_meta_key_consistency(conn: sqlite3.Connection,
                               library: str = "") -> Dict[str, Any]:
    """The criterion: the ``_meta`` chunker-version rename is COMPLETE
    and CONSISTENT.

    **Three assertions, and each one answers a different question.**

    1. **COMPLETE** — if the legacy key is present, the new key is
       present too.  The rename is a migration, and a migration that
       has not run leaves the fleet half-renamed: the reader that knows
       only the new name sees "no version", and the reader that knows
       only the old one sees a name that lies about what it holds.
       MEASURED on this machine: all 16 libraries carried ONLY the
       legacy key, so the rename had not run anywhere — and a criterion
       that merely asked "do the two keys disagree?" would have called
       that PASS, because a key that is ABSENT does not disagree with
       anything.  That is the false green this criterion is built to
       avoid: **an absent fact is not an agreeing fact.**

    2. **CONSISTENT** — when both are present they hold the SAME
       value.  They describe ONE fact, so two names for it must not
       carry two answers.

    3. **THE PER-FILE FACT IS SEPARATE** — ``files.chunker_version``
       says which chunker cut THIS file, and it is the authority the
       ruling names.  A file whose value is NULL is "not measured",
       which is a legitimate state (Rule D: "not measured" is not
       "wrong") and is reported as such — NOT as a conflict.  A file
       whose value is NOT the current chunker version is reported as a
       "stale file" count, because that is a real fact about the fleet
       and it is NOT the same fact as the ``_meta`` stamp.

    **Why the ``_meta`` stamp and the per-file version are not compared
    to each other.**  Before the rename they LOOKED like a conflict
    (``_meta`` says 3, ``files`` says 4) — and that reading was wrong.
    The ``_meta`` stamp is written only on a rebuild-from-empty, so it
    is a claim about the index's ORIGIN; the per-file version is a
    claim about each file's LAST CUT.  A library that was built by
    chunker 3 and whose files were later re-cut by chunker 4 has BOTH
    facts true at once.  The rename makes the ``_meta`` key say what
    it actually holds ("the version at the last migration"), and after
    that the two are different facts — not a conflict.
    """
    out: Dict[str, Any] = {
        "criterion": "M6g",
        "name": "the _meta chunker-version rename is complete and consistent",
        "library": library,
        "verdict": "NOTRUN",
        "legacy": None,
        "current": None,
        "files_versions": {},
        "files_null": 0,
        "files_stale": 0,
        "complete": False,
        "conflict": False,
        "detail": "",
    }
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as e:
        out["detail"] = "cannot read sqlite_master: %s" % e
        return out
    if "_meta" not in tables:
        out["detail"] = "the library has no _meta table"
        return out
    try:
        def _get(key):
            row = conn.execute(
                "SELECT value FROM _meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

        out["legacy"] = _get(META_KEY_LEGACY)
        out["current"] = _get(META_KEY)
        if "files" in tables:
            fcols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
            if "chunker_version" in fcols:
                rows = conn.execute(
                    "SELECT chunker_version, COUNT(*) FROM files "
                    "GROUP BY 1").fetchall()
                out["files_versions"] = {
                    ("null" if r[0] is None else str(r[0])): r[1]
                    for r in rows}
                out["files_null"] = conn.execute(
                    "SELECT COUNT(*) FROM files WHERE chunker_version IS NULL"
                ).fetchone()[0]
                try:
                    from agent.memory.chunker import TextChunker

                    out["files_stale"] = conn.execute(
                        "SELECT COUNT(*) FROM files WHERE chunker_version IS "
                        "NOT NULL AND chunker_version <> ?",
                        (TextChunker.CHUNKER_VERSION,)).fetchone()[0]
                except Exception:
                    out["files_stale"] = -1
    except sqlite3.Error as e:
        out["detail"] = "sqlite error: %s" % e
        return out

    # --- assertion 1: COMPLETE ----------------------------------------
    if out["legacy"] is not None and out["current"] is None:
        out["verdict"] = "FAIL"
        out["detail"] = ("the rename has NOT run here: the legacy key %s=%s "
                         "is present but the new key %s is absent — a reader "
                         "that knows only the new name finds no version, "
                         "and an absent fact is not an agreeing fact"
                         % (META_KEY_LEGACY, out["legacy"], META_KEY))
        return out
    out["complete"] = True

    # --- assertion 2: CONSISTENT --------------------------------------
    if out["legacy"] is not None and out["current"] is not None:
        if str(out["legacy"]) != str(out["current"]):
            out["conflict"] = True
            out["verdict"] = "FAIL"
            out["detail"] = ("the two _meta keys DISAGREE: %s=%s but %s=%s "
                             "— they describe ONE fact"
                             % (META_KEY_LEGACY, out["legacy"],
                                META_KEY, out["current"]))
            return out

    out["verdict"] = "PASS"
    out["detail"] = ("_meta: %s=%s / %s=%s (agree); files.chunker_version "
                     "distribution: %s (NULL=%d 'not measured', stale=%d) — "
                     "the per-file fact is the authority and is NOT the "
                     "same fact as the _meta stamp"
                     % (META_KEY_LEGACY, out["legacy"], META_KEY,
                        out["current"], out["files_versions"] or "none",
                        out["files_null"], out["files_stale"]))
    return out


def judge_meta_key_family(workspace: Union[str, Path]) -> Dict[str, Any]:
    """The criterion over every live library."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    reports = []
    verdict = "PASS"
    for lib in enumerate_live(root):
        try:
            conn = sqlite3.connect(
                "file:%s?mode=ro" % str(lib["db"]).replace("\\", "/"), uri=True)
        except sqlite3.Error as e:
            reports.append({"criterion": "M6g", "library": lib["name"],
                            "verdict": "NOTRUN", "detail": str(e)})
            verdict = "FAIL"
            continue
        try:
            rep = judge_meta_key_consistency(conn, library=lib["name"])
        finally:
            conn.close()
        reports.append(rep)
        if rep["verdict"] == "FAIL":
            verdict = "FAIL"
    return {
        "criterion": "M6g",
        "title": "the _meta chunker-version keys do not conflict",
        "workspace": str(root),
        "library_count": len(reports),
        "libraries": reports,
        "verdict": verdict,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m agent.memory.meta_chunker_key --workspace <ws>``."""
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="Rename the _meta chunker-version key to the honest "
                    "name, keeping the legacy key readable (and intact).")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--judge", action="store_true",
                    help="only judge, change nothing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.judge:
        report = judge_meta_key_family(args.workspace)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("M6g %s — %s" % (report["criterion"], report["title"]))
            for lib in report["libraries"]:
                print("  %-24s %-7s %s" % (lib.get("library"),
                                           lib.get("verdict"),
                                           lib.get("detail", "")))
            print("VERDICT: %s" % report["verdict"])
        return 0 if report["verdict"] == "PASS" else 1

    report = migrate_meta_key_family(args.workspace, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("meta chunker key: workspace=%s libraries=%d copied=%d "
              "dry_run=%s"
              % (report["workspace"], report["library_count"],
                 report["copied"], report["dry_run"]))
        for lib in report["libraries"]:
            print("  %-24s legacy=%-4s new=%-4s copied=%-5s %s"
                  % (lib.get("library"), lib.get("legacy_value"),
                     lib.get("new_value_after"), lib.get("copied"),
                     lib.get("detail", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
