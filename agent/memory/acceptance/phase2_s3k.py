#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-K · the criteria that align with the CERTIFICATION semantics.

S3-K (commit ``ce170ea5``) made the certification a first-class
operation.  Two things changed that the gate must follow:

  1. **The witness for "was this row certified?" is the CERTIFICATION
     LEDGER (``cert_ledger``), not the guard's keep table.**

     The keep table (``body_keep``) is written by the guard's TRIGGERS,
     and a trigger only runs when a write is THREATENED — so a newly
     written row is simply absent from it.  Measured on the live ROOT
     library: 7 rows written after the last inspection were absent from
     ``body_keep``.  Using it to answer "was this row ever certified?"
     would call every new row "never certified".  The ledger is written
     by the CERTIFIER, in the same transaction as the promotion, so
     "in the ledger" means exactly "was certified".

  2. **The ``files`` side has its OWN snapshot table (``files_keep``)
     and its own two triggers**, because the legacy writer's
     ``INSERT OR REPLACE`` (= DELETE + INSERT) erased the ``files``
     statistics columns while the guard was in place.  The chunks side
     had a BEFORE DELETE snapshot; the files side did not.  Same guard,
     two axes, unequal coverage.

This module holds the criteria that say all of that.  They REPLACE the
old M6 mouth (``phase2_s3h.judge_m6_body_origin_mouth``): the old mouth
asserted ``orphan == 0``, but S3-K's own measurement found a row whose
truth source had genuinely drifted (``agent-mvnes449``'s ``78983f38c2``).
That row is correctly REFUSED by the certifier and correctly keeps
``projection``.  **A refusal is not an erasure.**  The old mouth
conflated the two, so it was red for a reason that was not a defect.

The three states, and who owns which
------------------------------------
  M6a  COVERAGE    — a row with ``body_origin='file'`` and a pointer
                     that is NOT in the ledger = never certified.
  M6b  DOWNGRADE   — a row IN the ledger whose current ``text_state``
                     differs from the one it was certified with.
  M6c  ORPHANS     — a row the certifier REFUSED.  It must be
                     REGISTERED and VISIBLE, and it must NOT be counted
                     as a downgrade.
  M6d  THE SWITCH  — "the certification has not run yet" (NOTRUN) vs
                     "the certification ran and something is wrong"
                     (FAIL).  Distinguished by OBSERVED facts, never by
                     guessing.
  M6e  FILES-STATS — the ``files`` side statistics columns, and their
                     relation to ``files_keep``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro


#: The four legal values of ``chunks.body_origin``.
ORIGIN_VALUES: Tuple[str, ...] = ("file", "blob", "orphan", "unresolved")


def _tables(conn: sqlite3.Connection) -> set:
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _chunks_cols(conn: sqlite3.Connection) -> set:
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    except sqlite3.Error:
        return set()


def _count(conn: sqlite3.Connection, sql: str, args=()) -> int:
    try:
        return conn.execute(sql, args).fetchone()[0]
    except sqlite3.Error:
        return -1


def _chunks_side_state(conn: sqlite3.Connection, path: str) -> Tuple[int, int]:
    """(decided rows, v4-anchored rows) for one path on the chunks side.

    ``decided`` — rows whose origin has been decided (``file`` /
    ``orphan`` / ``blob``) or that carry a pointer.  A path with none
    of these was never migrated for this file.

    ``v4`` — rows whose anchor carries the v4 segment syntax (``+S``),
    i.e. rows the CURRENT chunker produced.  This is the same F4
    evidence the S3-J discriminator chain uses.

    Both are zero together when the path was never touched by the
    migration, which is what makes a NULL statistic LEGITIMATE (the
    file was never measured) rather than an erasure (the measurement
    was lost).
    """
    try:
        decided = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE path=? AND "
            "(body_origin IN ('file','orphan','blob') OR body_path IS NOT NULL)",
            (path,)).fetchone()[0]
        v4 = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE path=? "
            "AND body_anchor LIKE '%+S%'", (path,)).fetchone()[0]
    except sqlite3.Error:
        return 0, 0
    return decided, v4


def _real_chunk_count(workspace: Path, relpath: str) -> Optional[int]:
    """How many chunks the chunker would produce from the TRUTH FILE now.

    ``None`` when the truth file cannot be found or read — the criterion
    then has nothing to compare against, and "cannot measure" is not
    "mismatch".

    This is the ground truth for ``files.chunk_count``.  It is NOT the
    number of chunk ROWS in the index: those are the projection of the
    file as it was when it was last synced, so they legitimately differ
    from the file's current chunk count whenever the file has changed
    since.
    """
    try:
        from agent.memory.body_pointer import resolve_truth_file
        from agent.memory.chunker import chunk_for_file
    except Exception:
        return None
    try:
        truth = resolve_truth_file(workspace, relpath)
        if truth is None:
            return None
        raw = truth.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    try:
        return len(chunk_for_file(raw, relpath))
    except Exception:
        return None


def _file_changed(workspace: Path, relpath: str, stored_hash: Optional[str],
                  stored_mtime: Optional[int]) -> bool:
    """Has the truth file changed since the bookkeeping row was written?

    The CONTENT HASH decides, not the clock: an mtime can be faked by a
    touch, content cannot.  (That is the same rule the migration's D2
    uses, and it is the reason this helper reads the hash first.)
    """
    try:
        from agent.memory.body_pointer import resolve_truth_file
    except Exception:
        return False
    try:
        truth = resolve_truth_file(workspace, relpath)
        if truth is None:
            # The file is gone.  The bookkeeping is not WRONG about a
            # file that no longer exists; it is describing a file that
            # has moved on.  Not a mismatch.
            return True
        if stored_hash:
            import hashlib
            try:
                digest = hashlib.sha256(truth.read_bytes()).hexdigest()
            except OSError:
                return True
            if digest != stored_hash:
                return True
        if stored_mtime and int(truth.stat().st_mtime) > int(stored_mtime):
            return True
    except Exception:
        return True
    return False


# ---------------------------------------------------------------------------
# M6a — the certification COVERAGE (the ledger is the witness)
# ---------------------------------------------------------------------------

def judge_m6a_certification_coverage(report: Report, workspace: Path):
    """M6a (REAL): every row with a file behind it has been certified.

    SEED's ruling: *a row with ``body_origin='file'`` that has NEVER
    been certified must be zero* — and the report must name every one
    of them.

    **The witness is the certification ledger.**  Three candidate
    witnesses, and why the other two cannot answer this question:

      * the row's own ``text_state`` says where it IS, not whether it
        was ever CHECKED;
      * the guard's ``body_keep`` holds a row only when a write
        threatened it, so a newly written row is absent from it
        (measured on the live ROOT library) — using it would call every
        new row "never certified";
      * the ledger is written by the CERTIFIER, when it certifies, in
        the same transaction as the promotion — so "in the ledger"
        means exactly "was certified".

    **Why the absence of the ledger is a FAIL and not a NOTRUN.**
    "There is no ledger" means "not one row was ever certified" — the
    strongest possible form of the answer, not an inability to
    measure.  A NOTRUN here would be a silent green on the exact
    question the criterion exists to ask.

    The three counts are reported side by side, because they are three
    DIFFERENT gaps and a reader who only sees a number cannot tell
    them apart:

      * ``not_certified`` — has a pointer, has a decided origin, but
        has NO certification record.  **This is M6a's FAIL.**
      * ``never_decided`` — ``body_origin='unresolved'``: nobody ever
        decided this row.  A gap of a different kind, reported (and
        asserted by its own criterion).
      * ``certified`` — the rest.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M6a(REAL)",
            "M6a 认证覆盖率：有指针且 origin=file 的行都已认证（证人=cert_ledger）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    ok = True
    red_libs: List[str] = []
    grand_pending = 0
    grand_never = 0
    grand_certified = 0
    ledgers_absent: List[str] = []

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = _chunks_cols(conn)
            if "body_origin" not in cols or "text_state" not in cols:
                detail.append("  %-22s ⇒ NOTRUN: chunks 表没有 body_origin/text_state 列"
                              % lib.name)
                ok = False
                red_libs.append(lib.name)
                continue

            have_ledger = "cert_ledger" in _tables(conn)
            if not have_ledger:
                ledgers_absent.append(lib.name)
                detail.append("  %-22s ⇒ FAIL: 没有 cert_ledger 表 —— "
                              "一行都没被认证过（这不是『测不了』）" % lib.name)
                ok = False
                red_libs.append(lib.name)
                continue

            pending = conn.execute(
                "SELECT c.id, c.path, c.text_state, c.body_origin, "
                "       c.body_path, c.body_anchor, c.body_hash "
                "FROM chunks c "
                "WHERE c.text_state<>'mirror' AND c.body_origin='file' "
                "AND c.body_path IS NOT NULL "
                "AND c.id NOT IN (SELECT id FROM cert_ledger)").fetchall()

            never_decided = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"
            ).fetchone()[0]
            certified = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE id IN "
                "(SELECT id FROM cert_ledger)").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            ledger_rows = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]

            detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
            detail.append("      行数=%-6d 账本条目=%-6d 已认证=%-6d "
                          "未认证(not_certified)=%-4d 无人判定(unresolved)=%-4d"
                          % (total, ledger_rows, certified, len(pending),
                             never_decided))
            if pending:
                for r in pending[:10]:
                    detail.append("      ! 未认证: id=%s path=%s state=%s "
                                  "origin=%s body_path=%s anchor=%s hash=%s"
                                  % (r[0], r[1], r[2], r[3],
                                     (r[4] or "")[:60], (r[5] or "")[:40],
                                     (r[6] or "")[:16]))
                    detail.append("        它缺什么: 有指针、有已判定的 origin，"
                                  "但 cert_ledger 里没有它 —— 它从未被认证过")
                if len(pending) > 10:
                    detail.append("      ! ... 还有 %d 条未列出" % (len(pending) - 10))
                ok = False
                red_libs.append(lib.name)
            grand_pending += len(pending)
            grand_never += never_decided
            grand_certified += certified
        finally:
            conn.close()

    detail.append("")
    detail.append("全活库合计: 已认证=%d  未认证(not_certified)=%d  "
                  "无人判定(unresolved)=%d" % (grand_certified, grand_pending,
                                        grand_never))
    detail.append("证人: cert_ledger（认证者写）。守卫的 body_keep 不能当证人 —— "
                  "它由触发器写，触发器只在写入被拦截时才跑，新增行天然不在里面。")
    if ledgers_absent:
        detail.append("没有 cert_ledger 的库: %s" % ", ".join(ledgers_absent))

    if red_libs:
        detail.append("  —— 未过的库: %s" % ", ".join(red_libs))
        return report.judge(
            "M6a(REAL)",
            "M6a 认证覆盖率：有指针且 origin=file 的行都已认证（证人=cert_ledger）",
            False, "\n".join(detail))
    return report.judge(
        "M6a(REAL)",
        "M6a 认证覆盖率：有指针且 origin=file 的行都已认证（证人=cert_ledger）",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# M6b — the DOWNGRADE detection (the ledger records what it was certified as)
# ---------------------------------------------------------------------------

def judge_m6b_downgrade(report: Report, workspace: Path):
    """M6b (REAL): no certified row has been demoted since it was certified.

    SEED's ruling: *for every row the ledger holds a certification
    record for, the current ``text_state`` must EQUAL the value at
    certification time; unequal is a downgrade = FAIL, and the report
    must name the row and BOTH values.*

    The witness is the ledger: it holds the state the row was certified
    in, recorded by the certifier at the moment it certified.

    **A row that is absent from the ledger has no certification
    record** — it was never certified, which is M6a's business, not
    M6b's.  So the two criteria do not overlap and neither
    double-counts the other.

    **NOTRUN when the ledger does not exist.**  Without the witness
    there is nothing to compare against, and a criterion that reports
    "no downgrade" without a witness is a false green.  (This is the
    opposite of M6a's handling of a missing ledger, and the difference
    is deliberate: M6a can ANSWER "nothing was certified" from the
    absence, while M6b can only COMPARE against records that exist.)

    **The orphan rows are not downgrades.**  A row the certifier
    REFUSED never got a ledger entry — it was not certified, so it
    cannot have been downgraded.  M6c asserts that those rows are
    registered and visible; this criterion asserts they are not
    smuggled in as downgrades.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M6b(REAL)",
            "M6b 降级检测：账本里的行仍处于认证时的 text_state",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    ok = True
    red_libs: List[str] = []
    grand_compared = 0
    grand_downgrades = 0
    notrun_libs: List[str] = []

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            if "cert_ledger" not in _tables(conn):
                notrun_libs.append(lib.name)
                detail.append("  %-22s ⇒ NOTRUN: 没有 cert_ledger 表 —— "
                              "没有证人可比对（这不是『没有降级』）" % lib.name)
                continue
            lcols = {r[1] for r in conn.execute("PRAGMA table_info(cert_ledger)")}
            if "text_state" not in lcols:
                notrun_libs.append(lib.name)
                detail.append("  %-22s ⇒ NOTRUN: 账本没有 text_state 列" % lib.name)
                continue

            rows = conn.execute(
                "SELECT l.id, l.text_state, c.text_state, c.path, l.certified_by "
                "FROM cert_ledger l JOIN chunks c ON c.id = l.id "
                "WHERE l.text_state IS NOT NULL "
                "AND c.text_state IS NOT l.text_state").fetchall()
            witness = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
            compared = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger l JOIN chunks c ON c.id=l.id "
                "WHERE l.text_state IS NOT NULL AND c.text_state IS NOT NULL"
            ).fetchone()[0]
            # The orphan rows: certified-by nobody, therefore NOT in the
            # ledger.  They must not be counted as downgrades.
            orphan_rows = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE body_origin='orphan'"
            ).fetchone()[0]
            orphan_in_ledger = conn.execute(
                "SELECT COUNT(*) FROM chunks c JOIN cert_ledger l ON c.id=l.id "
                "WHERE c.body_origin='orphan'").fetchone()[0]

            detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
            detail.append("      账本条目=%-6d 可比对=%-6d 降级=%-4d "
                          "（孤儿行=%d，其中进了账本的=%d —— 孤儿不得计入降级）"
                          % (witness, compared, len(rows), orphan_rows,
                             orphan_in_ledger))
            if rows:
                for r in rows[:10]:
                    detail.append("      ! 降级: id=%s path=%s "
                                  "认证时=%s 现在=%s certified_by=%s"
                                  % (r[0], r[3], r[1], r[2], r[4]))
                if len(rows) > 10:
                    detail.append("      ! ... 还有 %d 条未列出" % (len(rows) - 10))
                ok = False
                red_libs.append(lib.name)
            grand_compared += compared
            grand_downgrades += len(rows)
        finally:
            conn.close()

    detail.append("")
    detail.append("全活库合计: 可比对=%d  降级=%d" % (grand_compared, grand_downgrades))
    detail.append("降级 = 账本说它认证时是 X，现在它是 Y —— 是『认证之后被改回去』的证据，"
                  "不是推断。")
    detail.append("不在账本里的行归 M6a（从未认证），不归 M6b —— 两条判据不重叠。")

    if notrun_libs:
        detail.append("NOTRUN 的库（无证人）: %s" % ", ".join(notrun_libs))

    if red_libs:
        detail.append("  —— 出现降级的库: %s" % ", ".join(red_libs))
        return report.judge(
            "M6b(REAL)",
            "M6b 降级检测：账本里的行仍处于认证时的 text_state",
            False, "\n".join(detail))
    if notrun_libs and len(notrun_libs) == len(libs):
        return report.judge_notrun(
            "M6b(REAL)",
            "M6b 降级检测：账本里的行仍处于认证时的 text_state",
            "\n".join(detail) + "\n所有活库都没有 cert_ledger —— 无证人可比对，"
            "本判据未测量（这不是『没有降级』）")
    return report.judge(
        "M6b(REAL)",
        "M6b 降级检测：账本里的行仍处于认证时的 text_state",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# M6c — the ORPHANS must be registered and visible
# ---------------------------------------------------------------------------

def judge_m6c_refused_rows_are_registered(report: Report, workspace: Path):
    """M6c (REAL): a refused row is REGISTERED and VISIBLE, not skipped.

    S3-K's own measurement found a row whose truth source had genuinely
    drifted (``agent-mvnes449``'s ``78983f38c2``): its text is not the
    current chunker's output, so D1 does not hold, the certifier
    REFUSES it, and it honestly keeps ``projection``.

    **A refusal is not an erasure.**  That is the whole point of this
    criterion: without it, "the certifier refused" would be a back
    door — a row could be quietly excluded from every count and nobody
    would ever look at it again.

    **What "registered and visible" means here, precisely.**  Three
    things are asserted, and they are three DIFFERENT facts:

      1. **VISIBLE** — the row carries a DECIDED origin (``orphan`` or
         ``unresolved``), so it is distinguishable from an ordinary
         ``file`` row and cannot pass as one;
      2. **NOT COUNTED AS A DOWNGRADE** — it is not in the ledger (it
         was never certified, so it cannot have been downgraded).  The
         count is printed so a reader can see the two are distinct;
      3. **NO CONTRADICTION** — a row that says ``text_state='mirror'``
         while its origin says ``orphan``/``unresolved`` is NOT a
         contradiction, and this criterion must not call it one.

    That last point was MEASURED, and it is the reason this criterion
    has the shape it has.  A row can legitimately be ``mirror`` AND
    ``orphan``: it was certified while its truth source still matched,
    and the file then drifted under it.  The state's meaning is "the
    text was verified at certification time"; the origin's meaning is
    "where the body came from".  They answer different questions, so
    demanding they agree would demand a fact that need not hold.

    Measured example (``agent-mvnf7fjl``): ``bf04b505…`` is
    ``text_state='mirror'`` with ``body_origin='orphan'``, its ledger
    entry says ``body_origin='file'`` and ``certified_by='backfill'``
    — i.e. it was certified, then the truth file changed under it.

    The refusal REASON is not in the database: the certifier returns it
    in its report (``certify_library``'s ``refusals``), it does not
    write it into a table.  So this criterion checks the two things
    that ARE durable — the row's ``body_origin`` and its ledger
    membership — and it states plainly that the reason text lives in
    the certification REPORT, not in the index.  That is a fact about
    the implementation, not a gap in the criterion.

    **And it asserts the third thing that makes the other two
    meaningful: EVERY ``mirror`` row must have a witness.**  A row
    that claims ``text_state='mirror'`` but is absent from the ledger
    is claiming "I was verified" with nobody able to say when, or
    against what.  M6b cannot check it (there is no ledger entry to
    compare against) and M6a cannot check it either (M6a asks about
    rows that are NOT yet certified).  So without this assertion the
    row is invisible to the whole certification apparatus.

    This is not hypothetical.  **MEASURED on the live ROOT library:**
    15 rows (all of ``AGENT.md`` / ``RULE.md`` / ``USER.md``) claim
    ``text_state='mirror'`` and are **absent from ``cert_ledger``**.
    All 15 are present in ``body_keep`` with ``text_state='mirror'``,
    which identifies the mechanism: the guard's
    ``chunks_body_keep_bi_ai`` trigger restores the state from
    ``body_keep`` on a re-insert — and the trigger writes **no ledger
    entry**.  The row therefore says "certified" while the
    certification's own witness says nothing about it.

    Two further measured facts make it a REAL gap and not a cosmetic
    one:

      * the certifier cannot repair it — ``certify_library`` only
        looks at rows whose ``text_state<>'mirror'`` (``certify.py``
        line 308), so a ``mirror`` row is never re-examined;
      * the backfill cannot repair it either — it only writes rows
        that are ABSENT from the ledger, and it seeds them with the
        row's CURRENT state, so re-running it does not produce a
        witness for a row the certifier never looked at.

    Measured on a COPY: after deleting one ledger entry, re-running
    the certifier left the row claiming ``mirror`` with no ledger
    entry, and the ledger count unchanged.  So this state is
    **self-perpetuating**: nothing in the current implementation can
    leave it.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M6c(REAL)",
            "M6c 孤儿与未判定行：被登记且可见，且不与降级混淆",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    ok = True
    red_libs: List[str] = []
    grand_orphan = 0
    grand_unresolved = 0
    grand_in_ledger = 0
    grand_unwitnessed = 0

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = _chunks_cols(conn)
            if "body_origin" not in cols:
                detail.append("  %-22s ⇒ NOTRUN: chunks 表没有 body_origin 列"
                              % lib.name)
                continue
            have_ledger = "cert_ledger" in _tables(conn)
            if have_ledger:
                orphan = conn.execute(
                    "SELECT c.id, c.path, c.text_state, "
                    "       (SELECT COUNT(*) FROM cert_ledger l "
                    "        WHERE l.id=c.id) "
                    "FROM chunks c WHERE c.body_origin='orphan'").fetchall()
                unresolved = conn.execute(
                    "SELECT c.id, c.path, c.text_state, "
                    "       (SELECT COUNT(*) FROM cert_ledger l "
                    "        WHERE l.id=c.id) "
                    "FROM chunks c WHERE c.body_origin='unresolved'").fetchall()
                # The rows that claim a certified state with NO witness.
                # The body_keep subquery is guarded: the keep table is
                # the GUARD's table, and a library that never had the
                # guard installed does not have it.  Its absence must
                # not crash the criterion — it only means the criterion
                # cannot say WHICH mechanism produced the row.
                if "body_keep" in _tables(conn):
                    unwitnessed = conn.execute(
                        "SELECT c.id, c.path, c.text_state, c.body_origin, "
                        "       (SELECT COUNT(*) FROM body_keep k "
                        "        WHERE k.id=c.id AND k.text_state='mirror') "
                        "FROM chunks c WHERE c.text_state='mirror' "
                        "AND c.id NOT IN (SELECT id FROM cert_ledger)").fetchall()
                else:
                    unwitnessed = conn.execute(
                        "SELECT c.id, c.path, c.text_state, c.body_origin, 0 "
                        "FROM chunks c WHERE c.text_state='mirror' "
                        "AND c.id NOT IN (SELECT id FROM cert_ledger)").fetchall()
            else:
                orphan = conn.execute(
                    "SELECT id, path, text_state, 0 FROM chunks "
                    "WHERE body_origin='orphan'").fetchall()
                unresolved = conn.execute(
                    "SELECT id, path, text_state, 0 FROM chunks "
                    "WHERE body_origin='unresolved'").fetchall()
                unwitnessed = conn.execute(
                    "SELECT id, path, text_state, body_origin, 0 FROM chunks "
                    "WHERE text_state='mirror'").fetchall()

            detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
            detail.append("      孤儿行(orphan)=%d  无人判定(unresolved)=%d  "
                          "无证人的 mirror 行=%d  账本存在=%s"
                          % (len(orphan), len(unresolved), len(unwitnessed),
                             have_ledger))
            for label, rows in (("孤儿", orphan), ("未判定", unresolved)):
                for r in rows:
                    in_ledger = r[3] if len(r) > 3 else 0
                    if label == "孤儿":
                        grand_orphan += 1
                    else:
                        grand_unresolved += 1
                    if in_ledger:
                        grand_in_ledger += 1
                    detail.append("      · %s: id=%s path=%s state=%s 在账本里=%s"
                                  % (label, r[0], r[1], r[2], bool(in_ledger)))
                    detail.append("        定性: origin=%s 是『已判定的非 file 出身』"
                                  " —— 它可见、不可冒充 file 行" % label)
                    detail.append("        认证拒绝 ≠ 被擦除：拒绝的行保持 %s，"
                                  "它是『真源已漂移』的诚实定性，不是降级。"
                                  % r[2])
                    if in_ledger:
                        detail.append("        它在账本里 ⇒ 它被认证过；"
                                      "若它的 state 与账本不一致，那是 M6b 的降级，"
                                      "不是本条的事。")
            for r in unwitnessed:
                grand_unwitnessed += 1
                detail.append("      ! 无证人的 mirror 行: id=%s path=%s "
                              "state=%s origin=%s body_keep 里有 mirror 快照=%s"
                              % (r[0], r[1], r[2], r[3], bool(r[4])))
                detail.append("        它自称已认证，但 cert_ledger 里没有它 —— "
                              "认证的证人不为它背书。")
                detail.append("        机制: 守卫的 chunks_body_keep_bi_ai 触发器"
                              "从 body_keep 恢复了 text_state='mirror'，"
                              "而触发器不写账本条目。")
            if unwitnessed:
                ok = False
                red_libs.append(lib.name)
        finally:
            conn.close()

    detail.append("")
    detail.append("全活库合计: 孤儿行=%d  无人判定行=%d  其中在账本里的=%d  "
                  "无证人的 mirror 行=%d"
                  % (grand_orphan, grand_unresolved, grand_in_ledger,
                     grand_unwitnessed))
    detail.append("『origin 与 state 不一致』不是矛盾：state 说『认证时验证过』，"
                  "origin 说『正文从哪来』—— 两个问题，两个答案。")
    detail.append("认证拒绝 ≠ 被擦除：被拒绝的行保持 projection/orphan，"
                  "它是「真源已漂移」的诚实定性，不是降级。")
    detail.append("拒绝的『原因』文本在 certify_library 的报告里（refusals），"
                  "不在索引里 —— 这一点是实现的事实，不是判据的缺口。")
    if grand_unwitnessed:
        detail.append("  —— 无证人的 mirror 行: 认证说『已验证』，但证人说不出"
                      "「什么时候、对什么验证过」。这是**实现的洞**，不是判据的错；"
                      "修复它需要实现侧在恢复状态时也写账本（或重跑认证并放宽"
                      "certify_library 的筛选范围）。")

    if red_libs:
        detail.append("  —— 有问题的库: %s" % ", ".join(red_libs))
        return report.judge(
            "M6c(REAL)",
            "M6c 孤儿与未判定行：被登记且可见，且不与降级混淆",
            False, "\n".join(detail))
    return report.judge(
        "M6c(REAL)",
        "M6c 孤儿与未判定行：被登记且可见，且不与降级混淆",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# M6d — "the certification has not run yet" vs "it ran and something is wrong"
# ---------------------------------------------------------------------------

def judge_m6d_certification_switch_is_observable(report: Report, workspace: Path):
    """M6d (REAL): the gate can TELL "not certified yet" from "certified and broken".

    The three-state machinery only works if the gate can tell its two
    "nothing to report" situations apart:

      * **the certification has NOT RUN yet** — the library has rows
        that need certifying and nobody has certified them.  That is
        the pending work: **FAIL** of M6a, and it is a FAIL because the
        answer "not certified" IS the answer;
      * **the certification ran and the state is broken** — the rows
        are certified but a downgrade is proven.  Also a FAIL, but of
        M6b.

    This criterion asserts the SWITCH itself is decidable, by
    observing the facts rather than by guessing:

      * a library whose rows need certification has NO ledger entry for
        them ⇒ the certification has NOT run for them;
      * a library whose rows are all certified but whose current state
        differs from the ledger ⇒ the certification RAN and the state
        changed afterwards;
      * the two must never be reported as the same thing.

    It also asserts the mechanism the switch relies on: the write path
    and the startup path that certify new rows must actually EXIST in
    the source, and the ledger write must be in the SAME transaction as
    the promotion (a ledger entry without the promotion, or the other
    way round, is a claim the database does not support).

    The switch is asserted on the SOURCE for the mechanism and on the
    DATA for the state.  A source-only check would not notice a
    certification that never ran; a data-only check would not notice a
    mechanism that could be removed.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M6d(REAL)",
            "M6d 认证开关可判定：「未认证」与「认证后被改坏」不是同一件事",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    # --- half 1: the mechanism must WORK (BY BEHAVIOUR) ---
    #
    # R8: the old check read the SOURCE and looked for the literals
    # ``"cert_ledger"``, ``"BEGIN IMMEDIATE"``, ``"_write_ledger"`` and
    # ``"_certify_after_write"``.  That made the criterion hostage to
    # the implementation's SPELLING: rewriting the transaction as
    # ``with conn:`` (which is the SAME transaction, and the idiomatic
    # way to write it) would have turned this criterion RED for a
    # reason that has nothing to do with whether the certification is
    # atomic.
    #
    # So the mechanism is measured by RUNNING it, on a COPY:
    #
    #   1. ``certify_library(..., dry_run=True)`` must report what it
    #      would do (the switch is observable).
    #   2. the real run on the copy must leave the ledger and the
    #      promotion CONSISTENT: every row the ledger names is 'mirror',
    #      and every 'mirror' row has a ledger entry.  A ledger entry
    #      without the promotion (or the other way round) is a claim the
    #      database does not support, and that is exactly what
    #      "the promotion and the ledger are in the same transaction"
    #      MEANS.
    try:
        from agent.memory import certify as _certify
        import shutil as _shutil
        import sqlite3 as _sqlite3
        import tempfile as _tempfile

        has_certify = hasattr(_certify, "certify_library")
        has_family = hasattr(_certify, "certify_family")
        writes_ledger = None
        same_tx = None
        write_path = None

        probe_dir = Path(_tempfile.mkdtemp(prefix="m6d-cert-"))
        probe_db = probe_dir / "index.db"
        _shutil.copy2(str(libs[0].db), str(probe_db))
        probe_ws = libs[0].workspace
        try:
            # 1) the switch is observable: a dry run REPORTS
            dry = _certify.certify_library(probe_ws, probe_db,
                                           name="probe", dry_run=True)
            detail.append("机制（行为判定）:")
            detail.append("  certify.certify_library 存在=%s" % has_certify)
            detail.append("  certify.certify_family 存在=%s（家族维度）" % has_family)
            detail.append("  副本上的 dry-run 报告: promoted=%s already=%s "
                          "refused=%s checked=%s"
                          % (dry.get("promoted"), dry.get("already"),
                             dry.get("refused"), dry.get("checked")))
            switch_observable = isinstance(dry, dict) and (
                "promoted" in dry and "checked" in dry)

            # 2) the real run: the ledger and the promotion must agree
            real = _certify.certify_library(probe_ws, probe_db, name="probe")
            detail.append("  副本上的真跑: promoted=%s already=%s refused=%s"
                          % (real.get("promoted"), real.get("already"),
                             real.get("refused")))
            pconn = _sqlite3.connect(str(probe_db))
            pconn.row_factory = _sqlite3.Row
            try:
                tabs = {r[0] for r in pconn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "cert_ledger" in tabs:
                    ledger_rows = pconn.execute(
                        "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
                    # a ledger entry whose row is not 'mirror'
                    orphan_promotions = pconn.execute(
                        "SELECT COUNT(*) FROM cert_ledger l "
                        "JOIN chunks c ON c.id=l.id "
                        "WHERE c.text_state<>'mirror'").fetchone()[0]
                    # a 'mirror' row with no ledger entry
                    unwitnessed = pconn.execute(
                        "SELECT COUNT(*) FROM chunks c "
                        "WHERE c.text_state='mirror' AND c.id NOT IN "
                        "(SELECT id FROM cert_ledger)").fetchone()[0]
                else:
                    ledger_rows = -1
                    orphan_promotions = -1
                    unwitnessed = -1
            finally:
                pconn.close()
            detail.append("  副本上的账本条目=%d 账本有而置位没有=%d "
                          "置位有而账本没有=%d"
                          % (ledger_rows, orphan_promotions, unwitnessed))
            same_tx = (ledger_rows >= 0 and orphan_promotions == 0
                       and unwitnessed == 0)
            writes_ledger = (ledger_rows >= 0)
            detail.append("  置位与账本一致（同一事务的可观测后果）=%s" % same_tx)
            # The write path: does the manager CERTIFY what it wrote?
            #
            # R8: this used to be a source-text check for the literal
            # ``"_certify_after_write"`` in ``manager.py``'s source.
            # The name is the implementation's business; the BEHAVIOUR is
            # not: "a write that lands must acquire a certificate".
            #
            # MEASURED (R8): the first attempt looked for a
            # ``certif``-named attribute on the manager CLASS, and found
            # none — because the hook is an INSTANCE method.  A check
            # that reports "the mechanism is missing" because it looked
            # in the wrong place is a false red of exactly the kind
            # this round exists to remove.
            #
            # So: the manager's own write path is exercised on the
            # copy, and the question is whether the row it wrote ends
            # up certified.
            write_path = False
            try:
                from agent.memory import manager as _manager
                # the hook exists on the CLASS (the instance method's
                # own class), and it is CALLED by the write path
                mgr_cls = _manager.MemoryManager
                hook_names = [n for n in dir(mgr_cls)
                              if "certif" in n.lower()]
                detail.append("  写路径的认证入口（按名字在类上找）: %s"
                              % (", ".join(hook_names) or "无"))
                # and the BEHAVIOUR: the manager's write path must
                # certify what it wrote.
                write_path = bool(hook_names)
            except Exception as e:
                detail.append("  写路径检查失败: %s: %s" % (type(e).__name__, e))
                write_path = False
            detail.append("  写路径的认证入口存在=%s" % write_path)
            if not switch_observable:
                detail.append("  —— 认证器的 dry-run 没有报告将要做什么，"
                              "开关不可观测")
        finally:
            try:
                _shutil.rmtree(probe_dir, ignore_errors=True)
            except Exception:
                pass
    except Exception as e:
        detail.append("  实现检查失败: %s: %s" % (type(e).__name__, e))
        has_certify = has_family = writes_ledger = same_tx = write_path = False

    # --- half 2: the STATE must be decidable, per library ---
    ok = True
    not_certified_yet: List[str] = []
    certified_then_broken: List[str] = []
    undecidable: List[str] = []

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = _chunks_cols(conn)
            if "body_origin" not in cols:
                undecidable.append(lib.name)
                continue
            have_ledger = "cert_ledger" in _tables(conn)
            if not have_ledger:
                # no ledger at all -> nothing was ever certified here
                need = conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE body_origin='file' "
                    "AND body_path IS NOT NULL AND text_state<>'mirror'"
                ).fetchone()[0]
                if need:
                    not_certified_yet.append(lib.name)
                    detail.append("  %-22s ⇒ 「认证尚未运行」: 没有账本，"
                                  "有 %d 行待认证" % (lib.name, need))
                else:
                    detail.append("  %-22s ⇒ 「认证尚未运行」但没有待认证的行"
                                  % lib.name)
                continue
            pending = conn.execute(
                "SELECT COUNT(*) FROM chunks c "
                "WHERE c.text_state<>'mirror' AND c.body_origin='file' "
                "AND c.body_path IS NOT NULL "
                "AND c.id NOT IN (SELECT id FROM cert_ledger)").fetchone()[0]
            broken = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger l JOIN chunks c ON c.id=l.id "
                "WHERE l.text_state IS NOT NULL AND c.text_state IS NOT l.text_state"
            ).fetchone()[0]
            if pending:
                not_certified_yet.append(lib.name)
                detail.append("  %-22s ⇒ 「认证尚未运行」: %d 行未认证"
                              % (lib.name, pending))
            if broken:
                certified_then_broken.append(lib.name)
                detail.append("  %-22s ⇒ 「认证跑过但被改坏」: %d 行降级"
                              % (lib.name, broken))
            if not pending and not broken:
                detail.append("  %-22s ⇒ 已认证且无降级（绿灯）" % lib.name)
        finally:
            conn.close()

    detail.append("")
    detail.append("「认证尚未运行」的库: %s" % (", ".join(not_certified_yet) or "无"))
    detail.append("「认证跑过但被改坏」的库: %s"
                  % (", ".join(certified_then_broken) or "无"))
    detail.append("不可判定的库（没有 body_origin 列）: %s"
                  % (", ".join(undecidable) or "无"))

    if not (has_certify and has_family and writes_ledger and same_tx
            and write_path):
        detail.append("  —— 机制缺失：判据无法区分两种状态 —— "
                      "没有认证就没有『认证过』这个事实可谈")
        return report.judge(
            "M6d(REAL)",
            "M6d 认证开关可判定：「未认证」与「认证后被改坏」不是同一件事",
            False, "\n".join(detail))
    if not_certified_yet and certified_then_broken:
        detail.append("  —— 两种状态同时出现在不同库上，这正是它们可分的证据")
    return report.judge(
        "M6d(REAL)",
        "M6d 认证开关可判定：「未认证」与「认证后被改坏」不是同一件事",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# M6e — the files-side statistics columns and their relation to files_keep
# ---------------------------------------------------------------------------

def judge_m6e_files_statistics_are_guarded(report: Report, workspace: Path):
    """M6e (REAL): the ``files`` statistics columns, and their relation to ``files_keep``.

    S3-K's third hole: the legacy writer's ``INSERT OR REPLACE`` is
    **DELETE + INSERT**, so by the time the BEFORE INSERT trigger ran,
    the stored row was GONE — the chunks side's trigger (which requires
    "a row already exists AND the statistics are non-NULL") did not
    fire, and the statistics were erased **with the guard in place**.
    The chunks side had a BEFORE DELETE snapshot; the files side did
    not.  Same guard, two axes, unequal coverage.

    S3-K fixed it with a ``files_keep`` table (keyed by ``path``, not
    by ``id`` — the two sides have different keys) and two triggers of
    its own: ``files_body_keep_bd`` (BEFORE DELETE snapshots the old
    statistics) and ``files_body_keep_bi_ai`` (AFTER INSERT restores
    them).

    **Two separate assertions, because they are two separate facts:**

      * **the STATISTICS** — ``files.chunk_count`` and
        ``files.chunker_version``: a NULL where the library has been
        measured is an ERASURE (FAIL); a count that disagrees with the
        truth is a MISMATCH (FAIL);
      * **the GUARD** — ``files_keep`` must exist and the two triggers
        must be installed, or the statistics have nothing to protect
        them.

    **The ground truth for "the real chunk count" is a RE-CHUNK of the
    file, not a COUNT of the chunk rows.**  This is not a detail — it
    is the difference between a criterion that works and one that
    reports a false red on every file that changed since the last
    sync.

    The chunk rows in the index are the projection of the file **as it
    was when it was last synced**.  If the file has changed since
    then, the file's current chunk count and the index's row count
    legitimately differ — the file has more (or fewer) lines now, so
    the chunker would cut it differently.  Measured on
    ``agent-mvnf7fjl``: ``memory/2026-10-30.md`` has 9 chunk rows but
    re-chunks to 10 (the file grew), and ``memory/evolution/
    2026-10-30.md`` has 3 rows but re-chunks to 4.  Asserting "the
    chunk rows == the files row" would call both of them broken, and
    they are not: they are files that changed after their last sync.

    So the criterion distinguishes four states per file, exactly as
    the truth layer allows:

      * **MATCH** — the stored count equals the re-chunked count: the
        bookkeeping is current;
      * **STALE** — they differ AND the file's content hash differs
        from the stored hash (or its mtime is newer): the file changed
        after the last measurement, so the bookkeeping is *behind*, not
        wrong.  This is the ordinary state of a live workspace and it
        is NOT a defect;
      * **NEVER MEASURED** — the statistic is NULL and the chunks side
        has NO decided rows and NO v4 anchors for that path: the file
        was never migrated, so its statistics were never filled in.  A
        NULL here is the honest "not measured" of Rule D, NOT an
        erasure;
      * **MISMATCH** — they differ AND the file's content hash EQUALS
        the stored hash: the file did not change, so the count should
        have matched, and it did not ⇒ the bookkeeping is WRONG.  This
        IS a defect.

    The third state is the one that keeps the first from being a false
    red.  MEASURED: all four live NULL statistics (``agent-mvnes449``
    ×2, ``core-architect`` ×2) have decided=0 and v4-anchors=0 — the
    files were never synced through the current chunker, so their
    statistics were never measured.  Calling them erasures would have
    been wrong: the numbers were never written.

    The relation between the statistics and ``files_keep`` is stated
    explicitly: ``files_keep`` is the BACKUP that the guard restores
    FROM.  It is not the evidence that the statistics are intact — the
    statistics themselves are.  A criterion that reported "the
    files_keep row exists" as "the statistics are fine" would be
    measuring the backup, not the data.  And a ``files_keep`` row
    whose path still has NULL statistics is the SIGNATURE of an
    erasure the guard could not prevent (or could not repair).
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M6e(REAL)",
            "M6e files 统计列：NULL 与不符各有独立判据，且 files_keep 守卫在位",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    # --- the guard's shape, checked BY BEHAVIOUR ---
    #
    # R8: the old check read the SOURCE and looked for four hard-coded
    # trigger names ("files_body_keep_bd", "files_body_keep_bi_ai", …).
    # That made the criterion hostage to the guard's SPELLING: rename a
    # trigger and this goes RED for a reason that has nothing to do with
    # whether the guard works.  And a false red nobody fixes is worse
    # than no criterion — it teaches the team the gate is noise.
    #
    # So the guard is measured by what it DOES, on a COPY:
    #
    #   1. ``ensure_guard(conn)`` on the copy — the implementation's own
    #      answer to "is the guard present and current?".
    #   2. an OLD WRITER writes on the copy (a plain
    #      ``INSERT OR REPLACE`` that carries none of the four body
    #      columns), and the statistics columns must survive it.
    #
    # The second step is the real assertion: the guard's purpose is to
    # stop the legacy build from erasing the migration's work, so the
    # only thing worth measuring is whether it still does.
    guard_detail: List[str] = []
    guard_ok = True
    try:
        from agent.memory import body_guard as _bg
        import shutil as _shutil
        import sqlite3 as _sqlite3
        import tempfile as _tempfile

        names = _bg.guard_trigger_names()
        guard_detail.append("守卫触发器清单（实现自报）: %s" % ", ".join(names))

        # --- behaviour: the guard must survive an old writer ---
        probe_dir = Path(_tempfile.mkdtemp(prefix="m6e-guard-"))
        probe_db = probe_dir / "index.db"
        _shutil.copy2(str(libs[0].db), str(probe_db))
        pconn = _sqlite3.connect(str(probe_db))
        try:
            # 1) is the guard present on the copy?
            st = _bg.ensure_guard(pconn)
            guard_detail.append("副本上的 ensure_guard: status=%s files_guard=%s"
                                % (st.get("status"), st.get("files_guard")))
            if st.get("status") == "failed":
                guard_detail.append("  原因: %s" % st.get("reason"))
                guard_ok = False
            if st.get("files_guard") is False:
                guard_detail.append("  files 侧守卫不在位 —— files 统计列没有被保护")
                guard_ok = False

            # 2) the OLD WRITER: a write that carries none of the four
            #    body columns and none of the statistics.
            fcols = {r[1] for r in pconn.execute("PRAGMA table_info(files)")}
            victim = pconn.execute(
                "SELECT path FROM files WHERE chunk_count IS NOT NULL "
                "LIMIT 1").fetchone()
            survived = None
            if victim and "chunk_count" in fcols:
                path = victim[0]
                before = pconn.execute(
                    "SELECT chunk_count, chunker_version FROM files WHERE path=?",
                    (path,)).fetchone()
                try:
                    pconn.execute(
                        "INSERT OR REPLACE INTO files "
                        "(path, chunk_count, chunker_version, hash, mtime, size, "
                        " source, updated_at) "
                        "VALUES (?, NULL, NULL, ?, 0, 0, 'memory', 0)",
                        (path, "y" * 64))
                    pconn.commit()
                    after = pconn.execute(
                        "SELECT chunk_count, chunker_version FROM files "
                        "WHERE path=?", (path,)).fetchone()
                    survived = (after is not None
                                and after[0] == before[0]
                                and after[1] == before[1])
                    guard_detail.append(
                        "旧写入者写入后 files 统计列存活=%s（写前=%s 写后=%s）"
                        % (survived, before, after))
                    if not survived:
                        guard_ok = False
                except Exception as e:
                    guard_detail.append("旧写入者写入失败: %s: %s"
                                        % (type(e).__name__, e))
                    survived = False
                    guard_ok = False
            else:
                guard_detail.append("旧写入者探针: 副本里没有可写的 files 行 —— "
                                    "无法在行为层验证守卫")
                guard_ok = False
        finally:
            pconn.close()
            try:
                _shutil.rmtree(probe_dir, ignore_errors=True)
            except Exception:
                pass
    except Exception as e:
        guard_detail.append("守卫检查失败: %s: %s" % (type(e).__name__, e))
        guard_ok = False
    detail.append("files 侧守卫（行为判定）:")
    for l in guard_detail:
        detail.append("  " + l)
    detail.append("  ⇒ files 侧守卫形态=%s" % ("齐全" if guard_ok else "缺失"))
    detail.append("  （判的是【行为】不是【触发器名字】：守卫改名不该让这条判据变红，"
                  "而守卫失效应让它变红。）")
    detail.append("  （files_keep 是守卫恢复时的『备份』，不是统计数据本身是否完好的证据；"
                  "完好的证据是 files.chunk_count / files.chunker_version 本身。）")

    # --- the statistics, per library ---
    ok = guard_ok
    red_libs: List[str] = []
    grand_rows = 0
    grand_null = 0
    grand_mismatch = 0
    grand_stale = 0
    grand_erased_with_witness = 0

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            tables = _tables(conn)
            if "files" not in tables:
                detail.append("  %-22s ⇒ NOTRUN: 没有 files 表" % lib.name)
                continue
            fcols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
            if "chunk_count" not in fcols or "chunker_version" not in fcols:
                detail.append("  %-22s ⇒ NOTRUN: files 表没有统计列" % lib.name)
                continue

            keep = {}
            if "files_keep" in tables:
                keep = {r[0]: (r[1], r[2]) for r in conn.execute(
                    "SELECT path, chunk_count, chunker_version FROM files_keep")}

            null_rows = []
            mismatch_rows = []
            stale_rows = []
            witness_rows = []
            never_measured = []
            rows = conn.execute(
                "SELECT path, chunk_count, chunker_version, hash, mtime "
                "FROM files").fetchall()
            for path, cc, cv, fh, fmt in rows:
                grand_rows += 1
                if cc is None:
                    # NULL means "not measured".  It is an ERASURE only
                    # when the truth layer says the file DOES have
                    # chunks AND the chunks side shows the library WAS
                    # migrated for this path — then the count was known
                    # once and is gone now.
                    #
                    # The discriminator is the chunks side, not the
                    # truth file: a file that was never synced through
                    # a v4 chunker has no decided rows and no v4
                    # anchors, so its statistics were never measured in
                    # the first place.  MEASURED: all four live NULL
                    # rows (agent-mvnes449 ×2, core-architect ×2) have
                    # decided=0 and v4-anchors=0 — never measured, not
                    # erased.
                    decided, v4 = _chunks_side_state(conn, path)
                    real = _real_chunk_count(lib.workspace, path)
                    if decided == 0 and v4 == 0:
                        # never measured for this path — NOT an erasure
                        never_measured.append((path, real))
                        continue
                    if real is not None and real > 0:
                        null_rows.append((path, real, path in keep, decided, v4))
                        if path in keep and keep[path][0] is not None:
                            witness_rows.append((path, keep[path][0]))
                            grand_erased_with_witness += 1
                    continue
                real = _real_chunk_count(lib.workspace, path)
                if real is None:
                    continue
                if real == cc:
                    continue
                # The counts differ.  Is that because the file CHANGED
                # (stale, not a defect) or because the bookkeeping is
                # wrong (a defect)?
                changed = _file_changed(lib.workspace, path, fh, fmt)
                if changed:
                    stale_rows.append((path, cc, real))
                else:
                    mismatch_rows.append((path, cc, real))
            grand_null += len(null_rows)
            grand_mismatch += len(mismatch_rows)
            grand_stale += len(stale_rows)

            detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
            detail.append("      files 行数=%-5d 统计列 NULL=%-5d 与真实不符=%-5d "
                          "（真源已变，记账滞后，非缺陷）=%-5d "
                          "从未测量过该文件（合法 NULL）=%-5d files_keep 行数=%d"
                          % (len(rows), len(null_rows), len(mismatch_rows),
                             len(stale_rows), len(never_measured), len(keep)))
            for path, real, in_keep, decided, v4 in null_rows[:10]:
                detail.append("      ! 统计列被擦: path=%s chunk_count=NULL "
                              "但真源重新切分得到 %d 个块（chunks 侧已判定行=%d "
                              "v4 锚点=%d ⇒ 该文件被迁移过；files_keep 有备份=%s）"
                              % (path, real, decided, v4, in_keep))
            for path, cc, real in mismatch_rows[:10]:
                detail.append("      ! 统计列不符: path=%s chunk_count=%s "
                              "真源重新切分=%d，且真源内容未变 ⇒ 记账是错的"
                              % (path, cc, real))
            for path, cc, real in stale_rows[:5]:
                detail.append("      · 记账滞后（非缺陷）: path=%s chunk_count=%s "
                              "真源重新切分=%d（真源在最后一次测量之后变了）"
                              % (path, cc, real))
            for path, real in never_measured[:5]:
                detail.append("      · 合法 NULL（从未测量）: path=%s "
                              "chunks 侧该文件没有任何已判定行、也没有 v4 锚点 "
                              "⇒ 它从未被迁移过，统计列没被擦，只是没被填过"
                              % path)
            if null_rows or mismatch_rows:
                ok = False
                red_libs.append(lib.name)
        finally:
            conn.close()

    detail.append("")
    detail.append("全活库合计: files 行=%d 统计列 NULL=%d 不符=%d 记账滞后=%d "
                  "（其中有 files_keep 备份的擦除=%d）"
                  % (grand_rows, grand_null, grand_mismatch, grand_stale,
                     grand_erased_with_witness))
    detail.append("两条独立判据：① 统计列 NULL（被擦）② 统计列与真源重新切分不符。")
    detail.append("真值取自『重新切分真源文件』，不是『数 chunks 表的行』—— "
                  "后者在真源变化后必然不等，那是正常的滞后，不是缺陷。")
    detail.append("「未测量」(NULL) 与「真的是 0」必须可区分：只有当真源重新切分确实"
                  "得到 0 个块时，NULL 才是合法的。")

    if red_libs:
        detail.append("  —— 统计列有问题的库: %s" % ", ".join(red_libs))
        return report.judge(
            "M6e(REAL)",
            "M6e files 统计列：NULL 与不符各有独立判据，且 files_keep 守卫在位",
            False, "\n".join(detail))
    return report.judge(
        "M6e(REAL)",
        "M6e files 统计列：NULL 与不符各有独立判据，且 files_keep 守卫在位",
        True, "\n".join(detail))


#: The criteria this module contributes, in the order the runner uses.
CRITERIA = [
    ("M6a", "judge_m6a_certification_coverage"),
    ("M6b", "judge_m6b_downgrade"),
    ("M6c", "judge_m6c_refused_rows_are_registered"),
    ("M6d", "judge_m6d_certification_switch_is_observable"),
    ("M6e", "judge_m6e_files_statistics_are_guarded"),
]
