"""S3-U · the «既有库迁得动» criterion (U-1).

**The gap this closes.**  Measured 2026-11-01: every one of the 21+
existing criteria runs in a FRESH FIXTURE — a library the test built
itself, empty, with the schema already in its final shape.  **No
criterion exercised the migration on a library that was actually IN
THE OLD SHAPE.**  So the claim "既有库迁得动" (an existing library can
be migrated) had no coverage at all — a collective blind spot of 21
criteria, not one missing test.

**Why the fresh fixture cannot answer it.**  A fresh fixture is
created by the CURRENT code, so it already has:

  * the five externalisation columns (``content_path`` /
    ``content_anchor`` / ``content_hash`` / ``content_state`` /
    ``content``);
  * ``content`` NULLABLE (so the externalisation can empty it);
  * a ``UNIQUE (agent_id, session_id, seq)`` key.

An old library has none of those.  The migration's job on an old
library is not "copy the rows" — it is **make the table able to hold
the externalised state at all**, and only then move the text out.
A criterion that only ever sees the new shape never exercises that
first half.

**What U-1 measures, and on what.**  The subject is a COPY of a REAL
pre-migration library — one that was captured before the migration and
is still on disk.  The criterion:

  1. copies it (the live library is never written to);
  2. runs the product's OWN migration path on the copy;
  3. asks the three questions that distinguish the two failure modes:

     * **「迁不动」** — the migration could not run at all (the schema
       could not be lifted, the write failed).  ⇒ FAIL, and the report
       names the exception.
     * **「没迁」** — the migration ran and reported success, but the
       rows are still inline.  ⇒ FAIL, and the report names how many
       rows are still holding their text.
     * **PASS** — the rows that the migration claimed to have moved
       really moved: their text left the table and landed in the truth
       tree.

**The third question is the one that matters.**  A migration that
reports "0 rows" is not a success — it is a migration that did nothing
and said so.  And a migration that reports "7,203 of 31,191 rows" is
not a success either; it is a migration that did a quarter of the work
and reported success (measured — see the report's own output).

**Why the criterion never runs on the live library.**  The migration
WRITES.  A criterion that migrates a live library has changed the thing
it measures, and the live corpus is the one corpus that matters.  So
the copy is mandatory, and the report says which file was copied.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory.acceptance import Report


#: Where a REAL pre-migration library is looked for.
#:
#: **The path is CONFIGURABLE and defaults to nothing.**  A criterion
#: that hard-codes one machine's path is not portable (团队规范 §3.6),
#: and a criterion that GUESSES a path will measure a scratch copy and
#: call it an "existing library".  So:
#:
#:   * ``COW_PREMIGRATION_LIBRARY`` — the file to use (explicit);
#:   * otherwise the search walks ``<workspace>/tmp/**/index.db`` and
#:     picks the one whose ``messages`` table has the OLD shape;
#:   * if there is none ⇒ **NOTRUN**, with the reason named.
#:
#: The "old shape" test is MECHANICAL, not a filename convention: a
#: library is pre-migration iff its ``messages`` table has no
#: ``content_state`` column (or its ``content`` column is NOT NULL).
PRE_MIGRATION_ENV = "COW_PREMIGRATION_LIBRARY"

#: How deep the search walks under ``<workspace>/tmp``.
PRE_MIGRATION_SEARCH_ROOT = "tmp"


def _is_pre_migration_shape(db: Path) -> Tuple[bool, str]:
    """Is this library's ``messages`` table in the PRE-migration shape?

    **Mechanical, not a naming convention.**  The two markers of the
    old shape are:

      * no ``content_state`` column — the state machine did not exist;
      * ``content`` declared ``NOT NULL`` — the text could not be
        emptied, so the externalisation could not run.

    A library is pre-migration iff EITHER marker is present.
    """
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                               uri=True)
    except sqlite3.Error as e:
        return False, "cannot open (%s)" % e
    try:
        tabs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "messages" not in tabs:
            return False, "no messages table"
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(messages)")}
        if "content_state" not in cols:
            return True, "no content_state column"
        content_row = cols.get("content")
        if content_row is not None and content_row[3]:
            return True, "content is NOT NULL"
        return False, "already in the new shape"
    except sqlite3.Error as e:
        return False, "sqlite error (%s)" % e
    finally:
        conn.close()


def _find_pre_migration_library(workspace: Path) -> Tuple[Optional[Path], str]:
    """The real pre-migration library to measure, and why it was chosen.

    The explicit environment variable wins.  Otherwise the search walks
    the workspace's own ``tmp/`` for an index whose ``messages`` table
    is in the old shape — and the report says WHICH file it picked and
    WHY (the shape marker), so the reader can check the choice.
    """
    explicit = os.environ.get(PRE_MIGRATION_ENV)
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            return None, "%s=%s does not exist" % (PRE_MIGRATION_ENV, explicit)
        ok, why = _is_pre_migration_shape(p)
        if not ok:
            return None, ("%s=%s is not in the pre-migration shape (%s)"
                          % (PRE_MIGRATION_ENV, explicit, why))
        return p, "%s=%s（%s）" % (PRE_MIGRATION_ENV, explicit, why)

    root = Path(workspace) / PRE_MIGRATION_SEARCH_ROOT
    if not root.is_dir():
        return None, ("%s is not a directory —— 没有可测的既有库"
                      % root)
    found: List[Tuple[Path, str, int]] = []
    for p in root.rglob("index.db"):
        try:
            if p.stat().st_size < 1024:
                continue
        except OSError:
            continue
        ok, why = _is_pre_migration_shape(p)
        if not ok:
            continue
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % str(p).replace("\\", "/"),
                                   uri=True)
            try:
                n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        found.append((p, why, n))
    if not found:
        return None, ("在 %s 下没有找到处于迁移前形态的既有库"
                      "（既有库必须带旧的 messages 表形态）" % root)
    # The BIGGEST one: the more rows it carries, the more the
    # migration has to do, and the more a partial migration shows.
    found.sort(key=lambda t: -t[2])
    p, why, n = found[0]
    return p, "%s（%s，messages %d 行）" % (p, why, n)


def judge_u1_existing_library_can_be_migrated(report: Report, workspace: Path):
    """U-1 (REAL): a REAL pre-migration library can actually be
    migrated — and the migration's own claim is checked against the
    result.

    **The subject.**  One real library in the pre-migration shape,
    copied to a temporary directory.  The live library is never
    written to.

    **The verdict口径.**

      * **PASS** — the migration ran, and the rows it CLAIMED to have
        moved really moved: their ``content_state`` is ``external``,
        their pointer is filled, their text left the table, and the
        truth tree carries the lines;
      * **FAIL** — either the migration could not run at all
        (「迁不动」: the exception is named), or it ran and reported
        success while the rows are still holding their text
        (「没迁」), or it moved only a fraction of what it should have
        (a PARTIAL migration — the failure mode that a "did it report
        success?" check misses);
      * **NOTRUN** — there is no real pre-migration library to measure
        (the search found none, or the environment variable points at
        something that is not in the old shape).  **NOT "zero rows =
        PASS"**: a library with no old-shaped rows has nothing to
        migrate, and that is NOT evidence that migration works.

    **Why the "claimed vs actual" comparison is the core.**  A
    migration tool that prints "rows=7203" is making a CLAIM.  The
    criterion checks the claim against the database.  The three ways
    the claim can be wrong are all FAIL, and each is reported
    separately, because each has a different owner.
    """
    detail: List[str] = []
    lib_path, why = _find_pre_migration_library(workspace)
    if lib_path is None:
        return report.judge_notrun(
            "U-1(REAL)",
            "U-1 既有库迁得动：真实的迁移前形态库能被迁完，且迁移自报的数与实际一致",
            "%s\n（0 个可测对象 —— 没有既有库 ⇒ 测不了，不是『迁得动』）" % why)

    detail.append("被测的既有库: %s" % lib_path)
    detail.append("选它的理由: %s" % why)
    detail.append("（活库零写入：下面跑的是临时目录里的**副本**）")
    detail.append("")

    tmp = Path(tempfile.mkdtemp(prefix="s3t-u1-"))
    try:
        ws = tmp / "ws"
        (ws / "memory" / "long-term").mkdir(parents=True)
        copy = ws / "memory" / "long-term" / "index.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(lib_path) + suffix)
            if src.is_file():
                shutil.copy2(src, Path(str(copy) + suffix))
        detail.append("副本: %s （%d 字节）" % (copy, copy.stat().st_size))

        # --- the BEFORE shape -----------------------------------------
        conn = sqlite3.connect(str(copy))
        try:
            before_total = conn.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]
            before_with_text = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE content IS NOT NULL "
                "AND content <> ''").fetchone()[0]
            before_agents = conn.execute(
                "SELECT COUNT(DISTINCT agent_id) FROM messages").fetchone()[0]
        finally:
            conn.close()
        detail.append("迁前: messages %d 行（有正文 %d 行，%d 个 agent_id）"
                      % (before_total, before_with_text, before_agents))

        # --- the migration --------------------------------------------
        #
        # **The schema has to be lifted FIRST.**  An old library has no
        # ``content_state`` column and its ``content`` is NOT NULL, so
        # the externalisation literally cannot run: the product's own
        # ``ConversationStore`` schema step is what adds the columns
        # and rebuilds the table with a nullable ``content``.  The
        # criterion runs THAT step too — because "既有库迁得动" means
        # "the whole path works on an old library", and the schema
        # step is part of that path.
        try:
            from agent.memory.conversation_store import ConversationStore
            store = ConversationStore(copy)
            try:
                closer = getattr(store, "close", None)
                if callable(closer):
                    closer()
            except Exception:
                pass
            detail.append("schema 步: 已执行（ConversationStore 的建表/迁移步）")
        except Exception as e:
            detail.append("schema 步失败: %s: %s" % (type(e).__name__, e))
            return report.judge(
                "U-1(REAL)",
                "U-1 既有库迁得动：真实的迁移前形态库能被迁完，且迁移自报的数与实际一致",
                False, "\n".join(detail + [
                    "  —— 「迁不动」：连 schema 步都跑不过去（%s: %s）"
                    % (type(e).__name__, e)]))

        from agent.memory import msg_migrate
        claimed_rows = 0
        claimed_written = 0
        claimed_emptied = 0
        try:
            conn = sqlite3.connect(str(copy))
            try:
                t0 = time.time()
                rep = msg_migrate.externalise_every_agent(
                    conn, ws, batch_date="2026-10-31")
                conn.commit()
                elapsed = time.time() - t0
            finally:
                conn.close()
            claimed_rows = _sum_field(rep, "rows")
            claimed_written = _sum_field(rep, "written")
            claimed_emptied = _sum_field(rep, "emptied")
            detail.append("迁移自报: rows=%d written=%d emptied=%d （%.1fs）"
                          % (claimed_rows, claimed_written, claimed_emptied,
                             elapsed))
        except Exception as e:
            detail.append("迁移执行失败: %s: %s" % (type(e).__name__, e))
            return report.judge(
                "U-1(REAL)",
                "U-1 既有库迁得动：真实的迁移前形态库能被迁完，且迁移自报的数与实际一致",
                False, "\n".join(detail + [
                    "  —— 「迁不动」：迁移在真实既有库上跑不起来（%s: %s）"
                    % (type(e).__name__, e)]))

        # --- the AFTER state ------------------------------------------
        conn = sqlite3.connect(str(copy))
        try:
            after_total = conn.execute(
                "SELECT COUNT(*) FROM messages").fetchone()[0]
            after_external = 0
            after_inline_text = 0
            try:
                after_external = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE content_state='external'"
                ).fetchone()[0]
                after_inline_text = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE content_state='inline' "
                    "AND content IS NOT NULL AND content <> ''").fetchone()[0]
            except sqlite3.Error:
                pass
        finally:
            conn.close()

        root = ws / "memory" / "messages_export"
        tree_files = []
        tree_lines = 0
        if root.is_dir():
            # **Only the MESSAGE lines.**  The ``.audit`` directory holds
            # the anchor chain and the compensation chain — they are not
            # message lines, and counting them would make the number
            # disagree with MSG-INV-5's (the same exclusion MSG-INV-5
            # applies: the main chain has ONE shape).
            audit = ".audit"
            for p in sorted(root.rglob("*.jsonl")):
                if audit in p.parts:
                    continue
                tree_files.append(p)
                try:
                    with open(p, encoding="utf-8") as fh:
                        tree_lines += sum(1 for _ in fh)
                except OSError:
                    pass

        detail.append("迁后: messages %d 行（external %d 行，仍 inline 有正文 %d 行）"
                      % (after_total, after_external, after_inline_text))
        detail.append("真源树: %d 个文件 / %d 行" % (len(tree_files), tree_lines))
        detail.append("")

        # --- the three verdicts ---------------------------------------
        #
        # **The claim is checked against the result, in BOTH
        # directions.**  A migration that claims MORE than it did is
        # just as wrong as one that claims less: the claim is what a
        # reader acts on ("the migration finished"), and a claim of
        # 62,382 on a 31,191-row library is a claim about a library
        # twice its size.
        ok = True
        if after_inline_text > 0:
            ok = False
            detail.append("  ! 「没迁」: 还有 %d 行把正文留在表里"
                          "（迁移自报 emptied=%d，与实际不符）"
                          % (after_inline_text, claimed_emptied))
        if claimed_emptied and after_external != claimed_emptied:
            ok = False
            if after_external < claimed_emptied:
                detail.append("  ! 部分迁移: 自报 emptied=%d，实际 external 只有 %d 行"
                              % (claimed_emptied, after_external))
            else:
                detail.append("  ! 自报比实际多: 自报 emptied=%d，实际 external %d 行"
                              "（迁移的计数与自己做的事对不上 —— 声称搬走的行数"
                              "比库里存在的还多）" % (claimed_emptied, after_external))
        if before_with_text and after_external == 0:
            ok = False
            detail.append("  ! 「没迁」: 迁移自报成功，但没有任何一行变成 external")
        if tree_lines == 0 and after_external > 0:
            ok = False
            detail.append("  ! 真源树是空的: 正文从表里搬走了，却没有落到文件里"
                          "（这是丢数据，不是迁移）")
        if ok:
            detail.append("  判定 PASS: 自报与实际一致，正文已离表并落在真源树里")
            detail.append("  （既有库 %s：%d 行 → external %d 行 / 真源 %d 行）"
                          % (lib_path.name, before_total, after_external,
                             tree_lines))
        return report.judge(
            "U-1(REAL)",
            "U-1 既有库迁得动：真实的迁移前形态库能被迁完，且迁移自报的数与实际一致",
            ok, "\n".join(detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sum_field(rep: Any, field: str) -> int:
    """The TOTAL of ``field`` in a migration report (or 0).

    **The shape of the report matters, and getting it wrong is a
    false red.**  ``externalise_every_agent`` returns a dictionary with
    BOTH a top-level total (``rows`` = the sum over all agents) AND a
    per-agent list (``agents`` = the same numbers, one per agent).
    Adding both double-counts every row — measured: a 6-row library
    reported ``emptied=12``, and the criterion then said "12 emptied,
    only 6 external" and went red on a PERFECT migration.

    So: **if the top level carries the total, it IS the total** — the
    nested list is only consulted when the top level does not have the
    field.  A missing number is 0, never an exception (a criterion
    that crashes on a report shape is a criterion that measures
    nothing).
    """
    if not isinstance(rep, dict):
        return 0
    if isinstance(rep.get(field), int):
        return int(rep[field])
    total = 0
    for key in ("agents", "sessions", "libraries"):
        rows = rep.get(key)
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and isinstance(r.get(field), int):
                    total += r[field]
    return total


#: The criteria this module contributes, in the order the runner uses.
CRITERIA = [
    ("U-1", "judge_u1_existing_library_can_be_migrated"),
]
