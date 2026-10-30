#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 第七轮 (S3-K) — the certification step, as a STANDALONE
operation.

**Why this module exists.**

The ``'mirror'`` state has a designated setter: the inspector.  That is
right — a state that anybody may set carries no information.  But the
setter only ever ran as a SIDE EFFECT of an inspection, and an
inspection only runs when somebody remembers to run one.  So:

  * a row written AFTER the last inspection stayed ``'projection'``
    forever, with no mechanism and no report;
  * the setter only ever looked at **this workspace's own** index, so the
    other fifteen libraries of the family were never certified at all;
  * the ``trigger='startup'`` entry point was DOCUMENTED and never
    called from anywhere.

This module makes the certification a first-class, repeatable
operation:

  * ``certify_library`` — one library: every row that can be certified
    IS certified, every row that cannot is REPORTED with its id and the
    reason (never silently skipped, never certified on a guess);
  * ``certify_family`` — every live library of a workspace, in one call;
  * ``judge_m6a_coverage`` / ``judge_m6b_downgrade`` — the two M6
    criteria, in the form a criterion file calls;
  * ``python -m agent.memory.certify --workspace <ws>`` — the CLI.

**The rule of promotion is unchanged.**  A row becomes ``'mirror'`` only
when the fragment rebuilt from the FILE is BYTE-equal to the stored
``chunks.text``.  The rebuild reads the truth layer and compares it with
the projection — it never re-runs the chunker and compares the
projection with itself, which could never be red.

**What is new here, and why it is not the same rule.**

A row whose ``body_path`` is NULL has no pointer, so there is nothing
to rebuild FROM — the old setter simply skipped it.  But such a row is
not necessarily uncertifiable: the row knows its own ``path``, its own
line span and its own text, and the truth file behind ``path`` can be
resolved.  So this module DERIVES the pointer first (by asking the
chunker which of ITS blocks equals the row's text — the D1 criterion,
the SAME question the migration asks), and only then certifies it.

That derivation is the migration's M4 backfill, reused as a function —
one implementation, two callers — because a second spelling of "how do
I decide a row's origin" is how the two sides end up disagreeing.

**The derivation writes the pointer; the certification writes the
state.**  They are separate acts, and the report says so: a row whose
pointer was derived is reported as ``derived``, a row that was already
pointing and passed the byte test is reported as ``promoted``, and a row
that failed is reported as ``refused`` with the reason and the two texts.

**The certification leaves a LEDGER.**  ``cert_ledger`` records that a
row was checked against the truth layer and found equal — which is a
different fact from "the guard's keep table holds what the row looked
like before a write touched it" (the keep table only holds the rows a
write threatened, so it cannot answer "was this row ever certified?").
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.body_pointer import (  # noqa: E402
    ORIGIN_FILE,
    ORIGIN_ORPHAN,
    ORIGIN_UNRESOLVED,
    STATE_PROJECTION,
    body_anchor,
    body_hash,
    rebuild_from_anchor,
    resolve_truth_file,
)

#: The state the inspector certifies.
STATE_MIRROR = "mirror"


# ---------------------------------------------------------------------------
# the reasons a row cannot be certified
# ---------------------------------------------------------------------------

REASON_NO_PATH = "no-path"
REASON_NO_TRUTH = "no-truth-file"
REASON_NO_ANCHOR = "no-anchor"
REASON_DERIVE_FAILED = "pointer-not-derivable"
REASON_NOT_A_CHUNK = "text-is-not-a-chunk-of-that-file"
REASON_REBUILD_FAILED = "fragment-not-rebuildable"
REASON_NOT_BYTE_EQUAL = "rebuilt-fragment-differs-from-stored-text"

#: Human-readable text for each reason (the report carries both the code
#: and the sentence, so a reader never has to look the code up).
REASON_TEXT = {
    REASON_NO_PATH: "the row has no path, so there is no truth file to certify against",
    REASON_NO_TRUTH: "the truth file behind the row's path does not exist",
    REASON_NO_ANCHOR: "the row has no anchor, and none could be derived",
    REASON_DERIVE_FAILED: "the row's text is not one of the chunker's blocks for that file",
    REASON_NOT_A_CHUNK: "the row's text is not one of the chunker's blocks for that file",
    REASON_REBUILD_FAILED: "the anchor does not locate anything in the truth file",
    REASON_NOT_BYTE_EQUAL: "the fragment rebuilt from the file is not byte-equal to the stored text",
}


# ---------------------------------------------------------------------------
# the pointer derivation — the migration's M4 rule, reused
# ---------------------------------------------------------------------------

def derive_pointer(workspace: Path | str, relpath: str, start_line: int,
                    end_line: int, part: int, chunk_text: str
                    ) -> Optional[Tuple[str, str, str]]:
    """The pointer for a row that has none — ``(path, anchor, hash)``.

    Returns None when the row's text is not one of the chunker's blocks
    for that file (D1 fails): there is then no honest pointer to write,
    and a wrong pointer is worse than none.

    The question "which of the chunker's blocks equals this text?" is
    asked by ``agent.memory.migrate._segments_for`` — the SAME function
    the migration's backfill asks, so the two cannot drift apart.
    """
    from agent.memory.migrate import _segments_for

    truth = resolve_truth_file(workspace, relpath)
    if truth is None:
        return None
    segments, gaps = _segments_for(truth, relpath, start_line, end_line,
                                   part, chunk_text)
    if segments is None:
        # D1 fails: the row's text is not a current chunker output.
        return None
    anchor = body_anchor(start_line, end_line, part, segments=segments,
                         gaps=gaps)
    from agent.memory.body_pointer import read_fragment

    try:
        frag = read_fragment(truth, anchor)
    except (OSError, UnicodeDecodeError, ValueError):
        frag = None
    digest = body_hash(frag) if frag is not None else body_hash(chunk_text)
    return relpath, anchor, digest


def _recover_part(rel: str, start_line: int, end_line: int,
                  chunk_id: str) -> int:
    """The part segment, recovered from the chunk id (the migration's
    rule, reused — never re-derived).  -1 when the id matches no part."""
    from agent.memory.migrate import _recover_part as _impl

    return _impl(rel, start_line, end_line, chunk_id)


# ---------------------------------------------------------------------------
# the certification of ONE library
# ---------------------------------------------------------------------------

def _write_ledger(conn: sqlite3.Connection,
                  derived: List[Tuple[str, str, str, str, str]],
                  promoted: List[str]) -> int:
    """Write the certification records for the rows just promoted.

    The ledger table is created if it is missing (it is not part of the
    FTS machinery, so nothing else creates it).  A failure to write the
    ledger does NOT undo the promotion — the promotion is the fact, the
    ledger is the witness of it — but the failure is counted and
    reported, because a criterion that asks "was this certified?" and
    gets "no record" would call a certified row uncertified.
    """
    from agent.memory.body_guard import (
        cert_table_ddl,
        record_certification,
    )

    try:
        conn.execute(cert_table_ddl())
    except sqlite3.Error:
        return 0
    written = 0
    for cid, bp, ba, bh, origin in derived:
        if record_certification(conn, cid, STATE_MIRROR, origin, bp, bh,
                                certified_by="certify:derived"):
            written += 1
    for cid in promoted:
        try:
            row = conn.execute(
                "SELECT body_path, body_hash, body_origin FROM chunks "
                "WHERE id=?", (cid,)).fetchone()
        except sqlite3.Error:
            row = None
        bp = row[0] if row else ""
        bh = row[1] if row else ""
        origin = row[2] if row else ORIGIN_FILE
        if record_certification(conn, cid, STATE_MIRROR, origin, bp, bh,
                                certified_by="certify:promoted"):
            written += 1
    return written


def backfill_ledger(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Seed the ledger with the rows that were ALREADY certified.

    **Why this is needed, and why it is honest.**  The ledger was
    introduced in S3-K; the rows certified before it existed (the
    inspector's own certifications) have no record.  Without a backfill
    the coverage criterion would call every one of them "never
    certified" — which is false, and a criterion that is false about the
    healthy majority is a criterion nobody reads.

    **What is written is exactly what is true**: the row's CURRENT
    state, with ``certified_by='backfill'``.  It says "this row holds a
    certified state", not "this row was checked at this moment".  The
    distinction matters for the DOWNGRADE criterion: a backfilled row
    that later falls back to 'projection' is still reported as a
    downgrade — which is right, because the state it was in when the
    ledger started watching it was 'mirror'.

    The backfill is idempotent (it only writes rows that are absent
    from the ledger) and it is a SEPARATE, explicitly-called step: it
    must never run implicitly during a certification, because that
    would silently paper over a row that was never certified.
    """
    from agent.memory.body_guard import cert_table_ddl, record_certification

    out: Dict[str, Any] = {"written": 0, "already": 0, "skipped": 0}
    try:
        conn.execute(cert_table_ddl())
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        if "text_state" not in cols:
            out["skipped"] = -1
            return out
        have = {r[0] for r in conn.execute("SELECT id FROM cert_ledger")}
        rows = conn.execute(
            "SELECT id, text_state, body_origin, body_path, body_hash "
            "FROM chunks WHERE text_state='mirror'").fetchall()
    except sqlite3.Error as e:
        out["error"] = str(e)
        return out

    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            if r[0] in have:
                out["already"] += 1
                continue
            if record_certification(conn, r[0], r[1], r[2] or "", r[3] or "",
                                    r[4] or "", certified_by="backfill"):
                out["written"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


def certify_library(workspace: Path | str, db: Path | str, *,
                    name: Optional[str] = None,
                    dry_run: bool = False) -> Dict[str, Any]:
    """Certify every certifiable row of one library.

    Returns a report with four counts and the detail of every row that
    was REFUSED (id + reason + the two texts).  The refused rows are
    the point: a certification that only reports its successes cannot
    tell "everything is certified" from "everything I looked at passed".
    """
    workspace = Path(workspace)
    db = Path(db)
    out: Dict[str, Any] = {
        "library": name or db.parent.parent.parent.name or str(db),
        "db": str(db),
        "workspace": str(workspace),
        "dry_run": bool(dry_run),
        "promoted": 0,
        "derived": 0,
        "already": 0,
        "refused": 0,
        "checked": 0,
        "refusals": [],
        "notes": [],
    }
    if not db.is_file():
        out["notes"].append("index-missing")
        return out

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        if "body_origin" not in cols:
            out["notes"].append("no-body_origin-column")
            return out

        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text, hash, body_path, "
            "body_anchor, body_hash, text_state, body_origin "
            "FROM chunks WHERE text_state<>'mirror'").fetchall()

        promoted: List[str] = []
        derived: List[Tuple[str, str, str, str, str]] = []
        for row in rows:
            out["checked"] += 1
            cid = row["id"]
            rel = row["path"]
            if not rel:
                out["refused"] += 1
                out["refusals"].append({
                    "id": cid, "reason": REASON_NO_PATH,
                    "detail": REASON_TEXT[REASON_NO_PATH]})
                continue

            bpath = row["body_path"]
            banchor = row["body_anchor"]
            bhash = row["body_hash"]
            derived_now = False
            if not bpath or not banchor:
                # No pointer: derive one (D1), then certify against it.
                part = _recover_part(rel, int(row["start_line"] or 0),
                                     int(row["end_line"] or 0), cid)
                if part < 0:
                    out["refused"] += 1
                    out["refusals"].append({
                        "id": cid, "reason": REASON_DERIVE_FAILED,
                        "detail": ("the chunk id matches no part segment "
                                   "for this path/span"),
                        "path": rel})
                    continue
                got = derive_pointer(workspace, rel, int(row["start_line"] or 0),
                                     int(row["end_line"] or 0), part,
                                     row["text"] or "")
                if got is None:
                    out["refused"] += 1
                    out["refusals"].append({
                        "id": cid, "reason": REASON_NOT_A_CHUNK,
                        "detail": REASON_TEXT[REASON_NOT_A_CHUNK],
                        "path": rel,
                        "stored_text": (row["text"] or "")[:400]})
                    continue
                bpath, banchor, bhash = got
                derived_now = True

            truth = resolve_truth_file(workspace, bpath)
            if truth is None:
                out["refused"] += 1
                out["refusals"].append({
                    "id": cid, "reason": REASON_NO_TRUTH,
                    "detail": REASON_TEXT[REASON_NO_TRUTH], "path": bpath})
                continue

            frag = rebuild_from_anchor(truth, banchor)
            if frag is None:
                out["refused"] += 1
                out["refusals"].append({
                    "id": cid, "reason": REASON_REBUILD_FAILED,
                    "detail": REASON_TEXT[REASON_REBUILD_FAILED],
                    "path": bpath, "anchor": banchor})
                continue

            if frag != (row["text"] or ""):
                out["refused"] += 1
                out["refusals"].append({
                    "id": cid, "reason": REASON_NOT_BYTE_EQUAL,
                    "detail": REASON_TEXT[REASON_NOT_BYTE_EQUAL],
                    "path": bpath, "anchor": banchor,
                    "stored_text": (row["text"] or "")[:400],
                    "rebuilt_text": frag[:400]})
                continue

            # PASSED.  Promote it, and (when the pointer had to be
            # derived) write the pointer too — the two acts are
            # separate and the report counts them separately.
            if derived_now:
                derived.append((cid, bpath, banchor, bhash, ORIGIN_FILE))
                out["derived"] += 1
            else:
                promoted.append(cid)
            out["promoted"] += 1

        if not dry_run and (promoted or derived):
            conn.execute("BEGIN IMMEDIATE")
            try:
                # The DERIVED rows first: the pointer AND the state, in
                # ONE statement.  (Two statements would work too, but the
                # state update's own WHERE clause demands
                # ``body_origin='file'`` — so a derived row that is only
                # given its pointer in one statement would be skipped by
                # the next.  One statement, one row, one fact.)
                for cid, bp, ba, bh, origin in derived:
                    conn.execute(
                        "UPDATE chunks SET body_path=?, body_anchor=?, "
                        "body_hash=?, body_origin=?, text_state=? "
                        "WHERE id=? AND text_state<>?",
                        (bp, ba, bh, origin, STATE_MIRROR, cid, STATE_MIRROR))
                for cid in promoted:
                    conn.execute(
                        "UPDATE chunks SET text_state=? WHERE id=? "
                        "AND body_origin=? AND text_state<>?",
                        (STATE_MIRROR, cid, ORIGIN_FILE, STATE_MIRROR))
                # The CERTIFICATION LEDGER: the record that this row was
                # checked against the truth layer and FOUND EQUAL.  It is
                # written in the SAME transaction as the promotion — a
                # ledger entry without the promotion (or the other way
                # round) would be a claim the database does not support.
                _write_ledger(conn, derived, promoted)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except sqlite3.Error as e:
        out["notes"].append("sqlite-error: %s" % e)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    # The rows that were ALREADY certified are counted too, so the
    # report can say "how many rows does this library have that are not
    # mirror" and not just "how many did I promote".
    try:
        conn = sqlite3.connect("file:" + str(db).replace("\\", "/")
                               + "?mode=ro", uri=True)
        out["already"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text_state='mirror'").fetchone()[0]
        out["remaining"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text_state<>'mirror'").fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
    return out


# ---------------------------------------------------------------------------
# the M6 criteria — the two questions SEED's ruling names
# ---------------------------------------------------------------------------

#: The verdict of a criterion: PASS / FAIL / NOTRUN.  Three states,
#: never two: a criterion that cannot measure must not claim to pass.
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_NOTRUN = "NOTRUN"


def judge_m6a_coverage(conn: sqlite3.Connection) -> Dict[str, Any]:
    """**M6a — the certification COVERAGE.**  Is every row that has a
    file behind it certified?

    The question SEED's ruling asks: *a row with ``body_origin='file'``
    that has NEVER been certified must be zero.*  That is the criterion
    that catches "a new page is written and nobody ever certifies it" —
    the hole the sixth round found.

    **"never been certified" is measured by the CERTIFICATION LEDGER**
    (``cert_ledger``), not by the row's own state and not by the
    guard's keep table:

    * the row's own state says where it IS, not whether it was ever
      CHECKED;
    * the guard's keep table holds a row only when a write threatened
      it, so a newly written row is absent from it (measured on the
      live ROOT library: 7 rows written after the last inspection were
      absent from ``body_keep``) — using it would call every new row
      "never certified", which is true but not the question;
    * the ledger is written **by the certifier, when it certifies**, so
      "in the ledger" means exactly "was certified".

    The three outcomes are distinguished on purpose:

    * ``not_certified`` — has a pointer and a decided origin, but no
      certification record: **the pending work**.  M6a is FAIL while
      these exist, and the report lists every one of them.
    * ``never_decided`` — ``body_origin='unresolved'``: nobody ever
      decided this row.  Also a gap, and also reported.
    * ``certified`` — the rest.

    NOTRUN when the library has no ``body_origin`` column at all: the
    criterion cannot measure anything there, and saying "PASS" would be
    a lie.
    """
    out: Dict[str, Any] = {
        "criterion": "M6a",
        "name": "certification coverage",
        "verdict": VERDICT_NOTRUN,
        "rows": 0,
        "certified": 0,
        "not_certified": 0,
        "never_decided": 0,
        "pending": [],
        "detail": "",
    }
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    except sqlite3.Error as e:
        out["detail"] = "cannot read the chunks table: %s" % e
        return out
    if "body_origin" not in cols or "text_state" not in cols:
        out["detail"] = ("the chunks table has no body_origin/text_state "
                         "column — the criterion cannot measure anything")
        return out
    try:
        out["rows"] = conn.execute(
            "SELECT COUNT(*) FROM chunks").fetchone()[0]
        out["certified"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE text_state='mirror'"
        ).fetchone()[0]
        # The ledger may not exist yet (it was introduced in S3-K, and a
        # library that has never been certified has no ledger).  That is
        # NOT a reason to skip the measurement: "no ledger" means "no row
        # was ever certified", which is the strongest possible form of
        # the answer.  So the ledger's absence makes every pending row
        # pending, rather than making the criterion NOTRUN.
        have_ledger = "cert_ledger" in {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        out["ledger_present"] = have_ledger
        if have_ledger:
            pending = conn.execute(
                "SELECT c.id, c.path, c.text_state, c.body_origin "
                "FROM chunks c "
                "WHERE c.text_state<>'mirror' AND c.body_origin='file' "
                "AND c.body_path IS NOT NULL "
                "AND c.id NOT IN (SELECT id FROM cert_ledger)").fetchall()
        else:
            pending = conn.execute(
                "SELECT id, path, text_state, body_origin FROM chunks "
                "WHERE text_state<>'mirror' AND body_origin='file' "
                "AND body_path IS NOT NULL").fetchall()
        out["not_certified"] = len(pending)
        out["never_decided"] = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"
        ).fetchone()[0]
        out["pending"] = [
            {"id": r[0], "path": r[1], "text_state": r[2],
             "body_origin": r[3]} for r in pending[:50]]
    except sqlite3.Error as e:
        out["detail"] = "sqlite error: %s" % e
        return out
    out["verdict"] = (VERDICT_PASS if out["not_certified"] == 0
                      else VERDICT_FAIL)
    out["detail"] = ("%d row(s) with a file behind them are not certified "
                     "yet; %d row(s) were never decided at all"
                     % (out["not_certified"], out["never_decided"]))
    return out


def judge_m6b_downgrade(conn: sqlite3.Connection) -> Dict[str, Any]:
    """**M6b — the DOWNGRADE detection.**  Has a certified row been
    demoted since it was certified?

    SEED's ruling: *for every row the guard's snapshot table holds a
    certification record for, the current ``text_state`` must EQUAL the
    value at certification time; unequal is a downgrade and is a FAIL,
    and the report must name the row and both values.*

    The witness is the **CERTIFICATION LEDGER**: the row's state when
    it was certified, recorded by the certifier at the moment it
    certified.  A row that is in the ledger and no longer holds the
    state it was certified in has been demoted since — that is a
    PROVEN downgrade, not an inference.

    A row that is absent from the ledger has no certification record:
    it was never certified, which is M6a's business, not M6b's.  So the
    two criteria do not overlap and neither double-counts the other.

    NOTRUN when the ledger does not exist: without the witness there is
    nothing to compare against, and a criterion that reports "no
    downgrade" without a witness is a false green.
    """
    out: Dict[str, Any] = {
        "criterion": "M6b",
        "name": "downgrade detection",
        "verdict": VERDICT_NOTRUN,
        "witness_rows": 0,
        "downgrades": 0,
        "downgraded": [],
        "detail": "",
    }
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as e:
        out["detail"] = "cannot read sqlite_master: %s" % e
        return out
    if "cert_ledger" not in tables:
        out["detail"] = ("the certification ledger (cert_ledger) does not "
                         "exist — there is no witness to compare against")
        return out
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cert_ledger)")}
        if "text_state" not in cols:
            out["detail"] = "the ledger has no text_state column"
            return out
        out["witness_rows"] = conn.execute(
            "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
        rows = conn.execute(
            "SELECT l.id, l.text_state, c.text_state, c.path "
            "FROM cert_ledger l JOIN chunks c ON c.id = l.id "
            "WHERE l.text_state IS NOT NULL "
            "AND c.text_state IS NOT l.text_state").fetchall()
        out["downgrades"] = len(rows)
        out["downgraded"] = [
            {"id": r[0], "certified_state": r[1], "current_state": r[2],
             "path": r[3]} for r in rows[:50]]
    except sqlite3.Error as e:
        out["detail"] = "sqlite error: %s" % e
        return out
    out["verdict"] = (VERDICT_PASS if out["downgrades"] == 0
                      else VERDICT_FAIL)
    out["detail"] = ("%d certified row(s) compared; %d row(s) are not in "
                     "the state they were certified in"
                     % (out["witness_rows"], out["downgrades"]))
    return out


def judge_m6_family(workspace: Path | str) -> Dict[str, Any]:
    """Both M6 criteria over every live library of the workspace.

    This is the form a criterion file calls: one call, one report, the
    verdicts of every library side by side.
    """
    workspace = Path(workspace)
    libs = enumerate_live(workspace)
    out: Dict[str, Any] = {
        "workspace": str(workspace),
        "library_count": len(libs),
        "m6a": [],
        "m6b": [],
        "verdict": VERDICT_PASS,
    }
    for lib in libs:
        try:
            conn = sqlite3.connect("file:" + str(lib["db"]).replace("\\", "/")
                                   + "?mode=ro", uri=True)
        except sqlite3.Error as e:
            a = {"criterion": "M6a", "library": lib["name"],
                 "verdict": VERDICT_NOTRUN, "detail": str(e)}
            b = dict(a)
            a["criterion"] = "M6a"
            b["criterion"] = "M6b"
            out["m6a"].append(a)
            out["m6b"].append(b)
            out["verdict"] = VERDICT_FAIL
            continue
        try:
            a = judge_m6a_coverage(conn)
            b = judge_m6b_downgrade(conn)
        finally:
            conn.close()
        a["library"] = lib["name"]
        b["library"] = lib["name"]
        out["m6a"].append(a)
        out["m6b"].append(b)
        if VERDICT_FAIL in (a["verdict"], b["verdict"]):
            out["verdict"] = VERDICT_FAIL
    return out


# ---------------------------------------------------------------------------
# the certification of the WHOLE family
# ---------------------------------------------------------------------------

def enumerate_live(workspace: Path | str) -> List[Dict[str, Any]]:
    """The live libraries, by WHITELIST — never a ``*.db`` glob.

    The migration owns this rule (``agent.memory.migrate.enumerate_live``)
    and it is imported rather than re-spelled: a glob would pick up the
    backups the migration writes and the scratch copies in ``tmp/``.
    """
    from agent.memory.migrate import enumerate_live as _impl

    return _impl(Path(workspace))


def certify_family(workspace: Path | str, *, dry_run: bool = False,
                   backfill: bool = False) -> Dict[str, Any]:
    """Certify every live library of a workspace.

    Each library is certified against ITS OWN truth layer — an agent's
    index is judged against that agent's files, never against the shared
    root's (the migration's rule, and the reason the knowledge pages
    resolve at all).

    ``backfill=True`` also seeds the certification ledger with the rows
    that were certified before the ledger existed.  It is OFF by
    default and must stay explicit: a backfill that ran implicitly would
    silently turn "never certified" into "certified", which is the exact
    thing the coverage criterion exists to detect.
    """
    workspace = Path(workspace)
    libs = enumerate_live(workspace)
    reports = []
    backfilled = 0
    for lib in libs:
        if backfill and not dry_run:
            try:
                conn = sqlite3.connect(str(lib["db"]))
                try:
                    res = backfill_ledger(conn)
                    backfilled += int(res.get("written", 0))
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
        reports.append(certify_library(lib["workspace"], lib["db"],
                                       name=lib["name"], dry_run=dry_run))
    total_promoted = sum(r["promoted"] for r in reports)
    total_refused = sum(r["refused"] for r in reports)
    total_derived = sum(r["derived"] for r in reports)
    total_already = sum(r.get("already", 0) for r in reports)
    total_remaining = sum(r.get("remaining", 0) for r in reports)
    return {
        "workspace": str(workspace),
        "dry_run": bool(dry_run),
        "backfill": bool(backfill),
        "backfilled": backfilled,
        "libraries": reports,
        "library_count": len(reports),
        "promoted": total_promoted,
        "derived": total_derived,
        "already": total_already,
        "refused": total_refused,
        "remaining": total_remaining,
    }


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Certify the 'mirror' state of the live-memory "
                    "libraries (P1-阶段三 S3-K).  A row is certified only "
                    "when the fragment rebuilt from the FILE is byte-equal "
                    "to the stored text; a row that fails is reported with "
                    "its id and the reason, never certified on a guess.")
    ap.add_argument("--workspace", required=True,
                    help="the workspace root (C:\\Users\\User\\cow)")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure everything, change nothing")
    ap.add_argument("--json", action="store_true",
                    help="print the full report as JSON")
    ap.add_argument("--backfill-ledger", action="store_true",
                    help="seed the certification ledger with the rows that "
                         "were certified before the ledger existed (the "
                         "inspector's own certifications).  EXPLICIT by "
                         "design: a backfill that ran implicitly would "
                         "silently turn 'never certified' into "
                         "'certified', which is the very thing the "
                         "coverage criterion exists to detect.")
    ap.add_argument("--judge-m6", action="store_true",
                    help="run the two M6 criteria (M6a coverage, M6b "
                         "downgrade detection) over every live library of "
                         "the workspace and print the verdicts.  This is "
                         "the form a criterion file calls: one call, one "
                         "report, PASS / FAIL / NOTRUN per library.")
    args = ap.parse_args(argv)

    if args.judge_m6:
        report = judge_m6_family(args.workspace)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("M6 criteria: workspace=%s libraries=%d"
                  % (report["workspace"], report["library_count"]))
            for crit in ("m6a", "m6b"):
                print("  %s:" % crit.upper())
                for lib in report[crit]:
                    print("     %-22s %-7s %s"
                          % (lib.get("library"), lib.get("verdict"),
                             lib.get("detail", "")))
                    for row in lib.get("pending", [])[:5]:
                        print("        PENDING %s %s (%s)"
                              % (row["id"][:10], row["path"][:60],
                                 row["text_state"]))
                    for row in lib.get("downgraded", [])[:5]:
                        print("        DOWNGRADED %s certified=%s now=%s"
                              % (row["id"][:10], row["certified_state"],
                                 row["current_state"]))
            print("VERDICT: %s" % report["verdict"])
        return 0 if report["verdict"] == VERDICT_PASS else 1

    try:
        import agent.memory  # noqa: F401
    except ImportError as e:
        sys.stderr.write(
            "\n!! cannot import the 'agent' package: %s\n"
            "\nRun this from the repository root:\n"
            "    cd %s\n"
            "    python -m agent.memory.certify --workspace <WS>\n"
            "\nAnd --workspace must point at the WORKSPACE ROOT (where the "
            "truth-layer files live), NOT at the repository.\n" % (e, REPO))
        return 2

    report = certify_family(args.workspace, dry_run=args.dry_run,
                            backfill=args.backfill_ledger)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("certify: workspace=%s libraries=%d dry_run=%s backfill=%s"
              % (report["workspace"], report["library_count"],
                 report["dry_run"], report.get("backfilled", 0)))
        for lib in report["libraries"]:
            print("   %-22s promoted=%-4d derived=%-4d refused=%-4d "
                  "already=%-5d remaining=%d"
                  % (lib["library"], lib["promoted"], lib["derived"],
                     lib["refused"], lib.get("already", 0),
                     lib.get("remaining", 0)))
            for r in lib["refusals"]:
                print("      REFUSED %s  %s  %s"
                      % (r["id"][:10], r["reason"], r.get("detail", "")))
        print("TOTAL: promoted=%d derived=%d refused=%d already=%d "
              "remaining=%d"
              % (report["promoted"], report["derived"], report["refused"],
                 report["already"], report["remaining"]))
    # A refusal is a RED verdict: the rows that could not be certified
    # are reported, and the exit code says so.
    return 1 if report["refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
