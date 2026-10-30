#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-J · 「没迁移」与「迁移后被擦」必须可区分。

The problem this module exists to solve
---------------------------------------
S3-H's M6 reads ``text_state``.  Two very different situations produce
the SAME reading:

  * **never migrated** — the library has no ``body_origin`` column, or
    the column holds ``unresolved`` and ``text_state`` is all
    ``projection``.  The prerequisite was never met.
  * **migrated, then erased by an old writer** — ``body_origin`` is all
    ``file`` (the guard held the pointer and the origin), but
    ``text_state`` was demoted back to ``projection``.

A criterion that reads only ``text_state`` gives both the same verdict.
That is the "判据比它守护的不变量更松" failure: it cannot tell a
missing prerequisite from a live defect, so it either cries wolf on
healthy libraries (nobody reads it) or waves through a broken one.

The discriminator — mechanical, and enumerable
---------------------------------------------
The basis is a chain of OBSERVED FACTS, each read from one column.
The chain is:

  **F1 (pointer present)**    ``body_path IS NOT NULL`` for every row
  **F2 (origin decided)**     ``body_origin`` exists and no row is
                              ``unresolved``
  **F3 (pointer complete)**   ``body_anchor`` and ``body_hash`` are
                              non-NULL and non-empty for every row
  **F4 (version current)**    every ``files.chunker_version`` equals the
                              current ``CHUNKER_VERSION``
  **F5 (statistics present)** no ``files.chunk_count`` / ``chunker_version``
                              is NULL

A library is **MIGRATED** iff F1 ∧ F2 ∧ F3 ∧ F4 hold.  That is the
question "has the migration's work ever landed on this library?".

Now the two situations:

  * **never migrated** → at least one of F1..F4 FAILS.  The library has
    no decided origin, or no pointer, or no current chunker version.
    ⇒ **NOTRUN**: the prerequisite was not met.  The report names the
    library and the F-condition that failed.

  * **migrated then erased** → F1 ∧ F2 ∧ F3 ∧ F4 all hold, but a row
    that SHOULD be ``mirror`` is not.  The pointer is there, the origin
    is decided, the content was chunked by the CURRENT chunker — so the
    row was demonstrably processed — and yet its state was demoted.
    ⇒ **FAIL**: a live defect.

Why F1..F4 can separate them (the basis chain, condition by condition)
-----------------------------------------------------------------------
Each condition reads a column that only the MIGRATION writes:

  F1 ``body_path``      written by ``migrate``'s backfill and by
                         ``body_pointer.four_columns_for_chunk``; the
                         guard's job is to KEEP it.  A library that was
                         never migrated has it NULL on every row; a
                         library that was migrated and then erased by an
                         old writer still has it (MEASURED: the guard
                         holds ``body_path`` through all three legacy
                         write shapes).

  F2 ``body_origin``    a column the migration ADDED and the backfill
                         FILLED.  ``unresolved`` means "nobody decided".
                         Note: the ``text_state`` demotion does NOT
                         touch ``body_origin`` — that is exactly why F2
                         separates "erased" from "never migrated": in
                         the erased case the origin is still decided.

  F3 ``body_anchor`` /  the anchor is a CONTENT fingerprint of the
     ``body_hash``      chunking.  A row carrying a v4 anchor (``+S``
                         segment syntax) proves the CURRENT chunker
                         produced it.  (This is also the witness that
                         survives an old writer's rewrite: the old
                         writer does not know the v4 grammar, so it
                         cannot forge an anchor that a v4 chunker
                         would not have produced.)

  F4 ``files.           written by the migration's files bookkeeping.
     chunker_version``  It is the ONE place the migration's own version
                         is recorded, and it is what distinguishes
                         "rebuilt by the current chunker" from "the file
                         row predates the current chunker".

The one case where BOTH hold, and how it is handled
---------------------------------------------------
``F4`` can hold while ``text_state`` is ``projection`` even on a
library that WAS migrated: that is precisely the "erased" case, and it
is reported as **FAIL**, which is the intended verdict.

There IS one shape where the two situations genuinely coincide: a
library migrated by a build whose ``CHUNKER_VERSION`` happens to equal
the current one, which was later erased, and which ALSO never had the
inspector's ``mirror`` verdict (because the inspector has not run
since).  In that shape F1..F4 hold and ``text_state`` is all
``projection`` — which is indistinguishable from "migrated, certified,
then erased".  The criterion resolves this with the WITNESS below
rather than guessing, and says so in the report.

The witness: ``body_keep``
-------------------------
The guard's snapshot table ``body_keep`` records the row's state as it
was BEFORE the last write.  So:

    ``body_keep.text_state='mirror'`` AND ``chunks.text_state<>'mirror'``
        ⇒ a DOWNGRADE IS PROVEN, not inferred.  The row WAS certified
          and now is not.

This is the strongest form of the evidence, and it is what turns
"the state looks wrong" into "the state WAS right and something
overwrote it".

The other witness: the anchor's own syntax
------------------------------------------
A row whose ``body_anchor`` carries the v4 ``+S`` segment syntax was
produced by the CURRENT chunker.  The old writer does not implement the
grammar, so it cannot produce such an anchor — but it CAN preserve one
by rewriting the row wholesale.  So the anchor's syntax proves the row
was rebuilt by the current chunker, which is F3's content-level proof.

The bulk-rewrite fingerprint
----------------------------
An old writer rewrites a WHOLE library at once, so the rows it wrote
share ONE ``created_at`` value and span MANY ``path`` values.  A normal
write touches ONE file.  So:

    a ``created_at`` second that covers ≥ 3 distinct paths AND ≥ 5 rows
    is a bulk rewrite; the timestamp is reported as the moment of the
    last bulk rewrite.

To exclude the "normal write of a batch of content" false positive: a
normal write covers ONE path.  The threshold is on the DISTINCT PATH
COUNT, not on the row count — a single large file legitimately
produces many rows in one second, but never rows from several
different files at once unless the writer is rewriting the library.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.memory.acceptance.live_libs import (
    enumerate_live,
    open_ro,
    table_columns,
)

# R8: J-3 and M6e ask the SAME question ("is this NULL statistic
# 'never measured' or 'erased'?") about the SAME rows, so they must use
# the SAME discriminator.  The discriminator lives with M6e (it was
# written there, with the measurement that backs it); J-3 imports it
# instead of growing a second, weaker copy.
#
# The alternative — keeping a library-level rule in J-3 — is exactly
# what produced the 17/17 disagreement measured in R7: two mouths for
# one question.
from agent.memory.acceptance.phase2_s3k import (  # noqa: E402
    _chunks_side_state,
)

#: The four columns the migration owns on ``chunks``.
CHUNK_COLUMNS = ("body_path", "body_anchor", "body_hash", "text_state")

#: The two columns the migration owns on ``files``.
FILES_STAT_COLUMNS = ("chunk_count", "chunker_version")

#: The four legal values of ``body_origin``.
ORIGIN_VALUES = ("file", "blob", "orphan", "unresolved")

#: The states a row may hold.
STATE_VALUES = ("projection", "mirror", "validated")

#: A bulk rewrite touches many paths at once.  A normal write touches
#: ONE file, so the threshold is on the DISTINCT PATH COUNT.
BULK_PATH_THRESHOLD = 3

#: ... and it is only called a bulk rewrite if it also wrote rows.
BULK_ROW_THRESHOLD = 5


# ---------------------------------------------------------------------------
# the profile: every fact the discriminator reads, for one library
# ---------------------------------------------------------------------------

class MigrationProfile:
    """Every OBSERVED FACT about one library, read from its own columns.

    Nothing here is inferred and nothing here calls the product: each
    field is a COUNT read straight out of SQLite, so a reader can
    re-derive it with the same SQL.
    """

    def __init__(self, lib):
        self.name = lib.name
        self.kind = lib.kind
        self.db = str(lib.db)
        self.error: Optional[str] = None

        # --- the columns that must exist ---------------------------
        self.chunk_cols: List[str] = []
        self.file_cols: List[str] = []
        self.has_origin_col = False

        # --- the counters -------------------------------------------
        self.rows = 0
        self.files_rows = 0
        self.body_path_null = 0
        self.body_anchor_null = 0
        self.body_hash_null = 0
        self.text_states: Dict[str, int] = {}
        self.body_origins: Dict[str, int] = {}
        self.files_cv: Dict[str, int] = {}
        self.files_cc_null = 0
        self.files_cv_null = 0
        #: R8: the PATHS whose ``chunker_version`` is NULL.  The
        #: library-level counter alone cannot say WHICH file was never
        #: measured; the path is what the chunks-side discriminator
        #: needs.  (This is what turned 18 legitimate NULLs into 18
        #: false "version wrong" verdicts.)
        self.files_cv_null_paths: List[str] = []
        #: R8: per path, the chunks-side discriminator's answer:
        #: ``(decided rows, v4 anchors)``.  Computed while the
        #: connection is still open, because the profile closes it
        #: before the criterion body runs.
        self.files_cv_null_state: Dict[str, Tuple[int, int]] = {}
        #: R8: the LEDGER as the witness.  ``cert_ledger`` is written by
        #: the CERTIFIER, when it certifies — so "in the ledger" means
        #: exactly "was certified".  This is what lets J-2 tell an
        #: ERASURE from a REFUSAL:
        #:
        #:   * in the ledger, but not ``mirror`` now ⇒ it WAS certified
        #:     and was demoted ⇒ **erased** (a live defect).
        #:   * NOT in the ledger, and not ``mirror`` ⇒ it was never
        #:     certified — the certifier REFUSED it (the content
        #:     drifted) ⇒ **not an erasure**.
        #:
        #: MEASURED (R8, agent-mvnes449): 2 rows were ``projection`` /
        #: ``orphan`` with NO ledger entry; the ledger held 249 entries,
        #: all ``mirror``, with ZERO intersection with the non-mirror
        #: rows.  The old J-2 counted them as "erased" — a verdict the
        #: evidence did not support.
        self.ledger_rows = 0
        self.ledger_nonmirror_ids: List[str] = []
        #: R8: does this library HAVE a ledger at all?  The ledger
        #: SPEAKS, or it does not: where there is no ledger, the
        #: criterion cannot claim "this row was never certified", so it
        #: falls back to the original reading.
        self.has_ledger = False

        # --- the witnesses ------------------------------------------
        self.downgrade_proven = 0        # body_keep=mirror & chunks<>'mirror'
        self.promoted_since = 0          # body_keep<>'mirror' & chunks=mirror
        self.keep_rows = 0
        self.anchor_v4 = 0               # rows whose anchor carries '+S'
        self.anchor_total = 0
        self.created_at_top: List[Tuple[int, int, int]] = []  # (ts, rows, paths)
        self.last_write: Optional[int] = None

        self._read(lib)

    # -- the readers ------------------------------------------------
    def _read(self, lib) -> None:
        try:
            conn = open_ro(lib.db)
        except Exception as e:  # pragma: no cover - unreadable db
            self.error = "打不开库: %s: %s" % (type(e).__name__, e)
            return
        try:
            self.chunk_cols = list(table_columns(conn, "chunks"))
            self.file_cols = list(table_columns(conn, "files"))
            self.has_origin_col = "body_origin" in self.chunk_cols
            self.rows = self._count(conn, "SELECT COUNT(*) FROM chunks")
            self.files_rows = self._count(conn, "SELECT COUNT(*) FROM files")

            if "body_path" in self.chunk_cols:
                self.body_path_null = self._count(
                    conn, "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL")
            if "body_anchor" in self.chunk_cols:
                self.body_anchor_null = self._count(
                    conn, "SELECT COUNT(*) FROM chunks "
                          "WHERE body_anchor IS NULL OR body_anchor=''")
            if "body_hash" in self.chunk_cols:
                self.body_hash_null = self._count(
                    conn, "SELECT COUNT(*) FROM chunks "
                          "WHERE body_hash IS NULL OR body_hash=''")
            if "text_state" in self.chunk_cols:
                self.text_states = self._dist(
                    conn, "SELECT text_state, COUNT(*) FROM chunks GROUP BY 1")
            if self.has_origin_col:
                self.body_origins = self._dist(
                    conn, "SELECT body_origin, COUNT(*) FROM chunks GROUP BY 1")
            if "chunk_count" in self.file_cols:
                self.files_cc_null = self._count(
                    conn, "SELECT COUNT(*) FROM files WHERE chunk_count IS NULL")
            if "chunker_version" in self.file_cols:
                self.files_cv_null = self._count(
                    conn, "SELECT COUNT(*) FROM files WHERE chunker_version IS NULL")
                self.files_cv = self._dist(
                    conn, "SELECT chunker_version, COUNT(*) FROM files GROUP BY 1")
                # R8: WHICH paths are NULL — the discriminator needs the
                # path, not just the count.
                try:
                    self.files_cv_null_paths = [
                        r[0] for r in conn.execute(
                            "SELECT path FROM files WHERE chunker_version IS NULL "
                            "ORDER BY path").fetchall()]
                except sqlite3.Error:
                    self.files_cv_null_paths = []
                # R8: the discriminator's answer, computed NOW (the
                # connection is open here and closed by the time the
                # criterion body runs).
                for _p in self.files_cv_null_paths:
                    try:
                        self.files_cv_null_state[_p] = _chunks_side_state(
                            conn, _p)
                    except Exception:
                        self.files_cv_null_state[_p] = (0, 0)

            # -- the witnesses --------------------------------------
            self._read_keep(conn)
            self._read_ledger(conn)
            self._read_anchors(conn)
            self._read_bulk(conn)
        except sqlite3.Error as e:
            self.error = "读库失败: %s" % e
        finally:
            conn.close()

    def _read_keep(self, conn) -> None:
        """``body_keep`` — the guard's snapshot table."""
        try:
            self.keep_rows = self._count(conn, "SELECT COUNT(*) FROM body_keep")
        except sqlite3.Error:
            return
        if self.keep_rows == 0:
            return
        try:
            self.downgrade_proven = self._count(
                conn,
                "SELECT COUNT(*) FROM body_keep k JOIN chunks c ON c.id=k.id "
                "WHERE k.text_state='mirror' AND c.text_state<>'mirror'")
            self.promoted_since = self._count(
                conn,
                "SELECT COUNT(*) FROM body_keep k JOIN chunks c ON c.id=k.id "
                "WHERE k.text_state<>'mirror' AND c.text_state='mirror'")
        except sqlite3.Error:
            pass

    def _read_ledger(self, conn) -> None:
        """R8: the certification ledger — the witness for J-2.

        The ledger holds one row per CERTIFIED chunk.  A chunk that is
        in the ledger but is not ``mirror`` now was certified and then
        demoted — an ERASURE.  A chunk that is not in the ledger was
        never certified — the certifier REFUSED it (the content
        drifted), and a refusal is not an erasure.

        ``body_keep`` cannot answer this question: the guard writes it,
        and the guard only runs when a write was intercepted, so a
        newly written row is simply absent from it.
        """
        try:
            tabs = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error:
            return
        if "cert_ledger" not in tabs:
            return
        self.has_ledger = True
        try:
            self.ledger_rows = self._count(
                conn, "SELECT COUNT(*) FROM cert_ledger")
            self.ledger_nonmirror_ids = [
                r[0] for r in conn.execute(
                    "SELECT l.id FROM cert_ledger l JOIN chunks c ON c.id=l.id "
                    "WHERE c.text_state<>'mirror' ORDER BY l.id").fetchall()]
        except sqlite3.Error:
            pass

    def _read_anchors(self, conn) -> None:
        """The anchor's own syntax — a content fingerprint of the chunker."""
        if "body_anchor" not in self.chunk_cols:
            return
        try:
            rows = conn.execute(
                "SELECT body_anchor FROM chunks "
                "WHERE body_anchor IS NOT NULL AND body_anchor<>''").fetchall()
        except sqlite3.Error:
            return
        self.anchor_total = len(rows)
        self.anchor_v4 = sum(1 for (a,) in rows if a and "+S" in a)

    def _read_bulk(self, conn) -> None:
        """The bulk-rewrite fingerprint: one timestamp, many paths."""
        if "created_at" not in self.chunk_cols:
            return
        try:
            self.last_write = conn.execute(
                "SELECT MAX(updated_at) FROM chunks").fetchone()[0]
            rows = conn.execute(
                "SELECT created_at, COUNT(*), COUNT(DISTINCT path) FROM chunks "
                "GROUP BY created_at ORDER BY COUNT(*) DESC LIMIT 5").fetchall()
        except sqlite3.Error:
            return
        self.created_at_top = [(int(r[0]), int(r[1]), int(r[2])) for r in rows]

    # -- the small helpers ------------------------------------------
    @staticmethod
    def _count(conn, sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0] or 0)
        except sqlite3.Error:
            return 0

    @staticmethod
    def _dist(conn, sql: str) -> Dict[str, int]:
        """A GROUP BY distribution, with NUMERIC keys normalised.

        SQLite hands back a column's value in its declared type, and
        ``files.chunker_version`` is declared TEXT — so the distribution
        arrives as ``{'4': 93}``, not ``{4: 93}``.  Comparing that with
        ``{current_version}`` is False, and the comparison is exactly
        what F4 turns on.  So the keys are normalised here, ONCE, and
        every comparison downstream is between integers.

        This was a real bug in the first version of this module: F4
        failed on every library because ``{'4'} != {4}`` — and the
        criterion reported "never migrated" for sixteen libraries that
        had all been migrated.  A criterion whose own type mismatch
        hides the state it is measuring is worse than no criterion.
        """
        out: Dict[str, int] = {}
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:
            return {}
        for k, v in rows:
            key = k
            if isinstance(key, str):
                try:
                    key = int(key)
                except ValueError:
                    pass
            out[key] = int(v)
        return out

    # -- the F-chain ------------------------------------------------
    def f_chain(self, current_chunker_version: int) -> List[Tuple[str, bool, str]]:
        """The five facts, each with the column it reads.

        Returns ``(fact, holds, the column it reads)``.
        """
        f1 = self.body_path_null == 0 and self.rows > 0
        f2 = self.has_origin_col and self.body_origins.get("unresolved", 0) == 0
        f3 = (self.body_anchor_null == 0 and self.body_hash_null == 0
              and self.rows > 0)
        f4 = (self.files_cv_null == 0 and bool(self.files_cv)
              and set(self.files_cv) == {current_chunker_version})
        f5 = self.files_cc_null == 0
        return [
            ("F1 指针已回填 (body_path 非 NULL)", f1, "chunks.body_path"),
            ("F2 来源已判定 (无 unresolved)", f2, "chunks.body_origin"),
            ("F3 指针完整 (body_anchor/body_hash 非空)", f3,
             "chunks.body_anchor / chunks.body_hash"),
            ("F4 用当前切法重建 (files.chunker_version == %d)" % current_chunker_version,
             f4, "files.chunker_version"),
            ("F5 files 统计列已回填 (chunk_count 非 NULL)", f5,
             "files.chunk_count"),
        ]

    def is_migrated(self, current_chunker_version: int) -> bool:
        chain = self.f_chain(current_chunker_version)
        return all(ok for (_n, ok, _c) in chain[:4])

    def bulk_rewrite(self) -> Optional[Tuple[int, int, int]]:
        """The last bulk rewrite: (created_at, rows, distinct paths).

        A normal write touches ONE file, so the threshold is on the
        DISTINCT PATH COUNT — a single large file legitimately produces
        many rows in one second, but rows from SEVERAL files at once
        mean the writer was rewriting the library.
        """
        for (ts, rows, paths) in self.created_at_top:
            if paths >= BULK_PATH_THRESHOLD and rows >= BULK_ROW_THRESHOLD:
                return (ts, rows, paths)
        return None


# ---------------------------------------------------------------------------
# the shared measurement
# ---------------------------------------------------------------------------

def current_chunker_version() -> Tuple[int, str]:
    """The CURRENT chunker version and where it is defined."""
    try:
        from agent.memory.chunker import TextChunker
        return int(TextChunker.CHUNKER_VERSION), "chunker.TextChunker.CHUNKER_VERSION"
    except Exception as e:  # pragma: no cover
        return -1, "无法读取 chunker.TextChunker.CHUNKER_VERSION (%s)" % e


def _fmt_ts(ts: Optional[int]) -> str:
    if ts is None:
        return "无"
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(ts)


def _profile_all(workspace: Path) -> List[MigrationProfile]:
    return [MigrationProfile(lib) for lib in enumerate_live(workspace)]


# ---------------------------------------------------------------------------
# J-2: the discriminator
# ---------------------------------------------------------------------------

def judge_migration_state_is_discriminable(report, workspace: Path):
    """J-2 (REAL): 「没迁移」与「迁移后被擦」必须给出不同的结论。

    The verdict per library:

      * **NOTRUN** — the prerequisite was not met.  At least one of
        F1..F4 fails, so the migration's work never landed on this
        library.  The report NAMES the library and the failing
        condition.  (NOTRUN is not a pass: it says "not measured".)
      * **FAIL** — the library was migrated (F1..F4 all hold) and a
        row that should be ``mirror`` is not.  That is a live defect:
        the pointer is there, the origin is decided, the content was
        chunked by the current chunker — so the row was demonstrably
        processed — and yet its state was demoted.
      * **PASS** — migrated, and every ``file`` row is ``mirror``.

    Every library reports its own F-chain, so a reader can see WHICH
    fact decided it.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "J-2(REAL)",
            "J-2 判别：「没迁移」(NOTRUN) 与「迁移后被擦」(FAIL) 可区分（逐库报 F1~F5 依据链）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    version, version_src = current_chunker_version()
    detail.append("当前 CHUNKER_VERSION: %d（真源 %s）" % (version, version_src))
    detail.append("")
    detail.append("判别依据链（每条事实读哪一列）：")
    detail.append("  F1 指针已回填      读 chunks.body_path          —— 只有迁移会写它")
    detail.append("  F2 来源已判定      读 chunks.body_origin        —— 只有迁移会加列并填它")
    detail.append("  F3 指针完整        读 chunks.body_anchor/body_hash —— 内容级指纹")
    detail.append("  F4 用当前切法重建  读 files.chunker_version      —— 迁移的 files 记账")
    detail.append("  F5 files 统计列    读 files.chunk_count         —— 迁移的 files 记账")
    detail.append("  ⇒ F1∧F2∧F3∧F4 成立 = 迁移的工作曾落在本库上")
    detail.append("")

    ok = True
    notrun_libs: List[str] = []
    red_libs: List[str] = []
    for prof in _profile_all(workspace):
        if prof.error:
            notrun_libs.append(prof.name)
            detail.append("  %-22s 前置不成立: %s" % (prof.name, prof.error))
            continue
        chain = prof.f_chain(version)
        migrated = prof.is_migrated(version)
        detail.append("  %-22s 路径=%s" % (prof.name, prof.db))
        detail.append("      行数=%-6d files 行数=%-5d" % (prof.rows, prof.files_rows))
        for (fact, holds, column) in chain:
            detail.append("      [%s] %-46s 列: %s"
                          % ("OK" if holds else "--", fact, column))
        detail.append("      text_state 分布: %s" % (prof.text_states or "无"))
        detail.append("      body_origin 分布: %s" % (prof.body_origins or "无"))
        detail.append("      files.chunker_version 分布: %s" % (prof.files_cv or "无"))
        detail.append("      body_keep 行数=%-5d 降级证据(keep=mirror & chunks≠mirror)=%-5d "
                      "升级后置位(keep≠mirror & chunks=mirror)=%d"
                      % (prof.keep_rows, prof.downgrade_proven, prof.promoted_since))
        detail.append("      锚点带 v4 段语法(+S): %d / %d" % (prof.anchor_v4, prof.anchor_total))

        if not migrated:
            failed = [name for (name, holds, _c) in chain[:4] if not holds]
            detail.append("      ⇒ 结论: NOTRUN（未迁移）—— 未成立: %s"
                          % ", ".join(failed))
            notrun_libs.append(prof.name)
            continue

        # --- migrated.  Now: is the state right? -------------------
        file_not_mirror = 0
        for state, n in prof.text_states.items():
            if state != "mirror":
                file_not_mirror += n
        detail.append("      ⇒ 迁移已完成（F1~F4 全成立）")

        # R8: "not mirror" is NOT one thing.  It is TWO, and the LEDGER
        # is what tells them apart:
        #
        #   * the LEDGER HAS the row and the row is not mirror now ⇒ it
        #     WAS certified and was demoted ⇒ **erased** (a live defect).
        #   * the LEDGER has NO row for it ⇒ it was NEVER certified —
        #     the certifier REFUSED it (the content drifted under it)
        #     ⇒ a refusal is NOT an erasure, and calling it one is a
        #     verdict the evidence does not support.
        #
        # MEASURED (R8, agent-mvnes449): 2 rows were `projection` /
        # `orphan` with NO ledger entry; the ledger held 249 entries,
        # all `mirror`, with ZERO intersection with the non-mirror
        # rows.  The old rule ("any non-mirror row ⇒ erased") called
        # them erased — the same shape of mistake as J-3's "NULL is a
        # wrong version": a category error between "not what we want"
        # and "what went wrong".
        #
        # **The ledger SPEAKS, or it does not.**  Where the ledger says
        # nothing about a row (the library has no ledger at all), the
        # criterion falls back to the ORIGINAL reading — "migrated, and
        # a row that should be mirror is not" ⇒ erased.  That is the
        # MU-20 case: a library with no ledger at all, where the
        # demotion is the only fact there is.  The exemption applies
        # only where the ledger actually says "this row was never
        # certified".
        erased = list(prof.ledger_nonmirror_ids)
        refused = 0
        if prof.has_ledger:
            # The ledger is present: it covers every row, and its
            # silence about a non-mirror row is a REFUSAL (the
            # certifier saw it and declined).
            refused = file_not_mirror - len(erased)
        witness = ("cert_ledger" if prof.has_ledger
                   else "无账本（按『迁移已完成 + 现在不是 mirror』判读）")

        detail.append("      cert_ledger 存在=%s 条目=%-6d 其中现在非 mirror 的=%-4d "
                      "（这是【被擦】的行）"
                      % (prof.has_ledger, prof.ledger_rows,
                         len(prof.ledger_nonmirror_ids)))
        detail.append("      body_keep 里曾是 mirror 现在不是的行=%-4d"
                      % prof.downgrade_proven)
        detail.append("      非 mirror 的行=%-6d 其中【从未认证】（认证器拒绝）=%-4d"
                      % (file_not_mirror, refused))

        if erased:
            detail.append("      ⇒ 结论: FAIL（迁移后被擦）—— 曾被认证、现在不是 mirror 的行数=%d"
                          % len(erased))
            detail.append("      ⇒ 证据: %s" % witness)
            for i in erased[:5]:
                detail.append("         被擦的行 id=%s" % i)
            if prof.downgrade_proven and witness == "cert_ledger":
                detail.append("      ⇒ 旁证: body_keep 里也有 %d 行曾是 mirror —— "
                              "降级是【被证明的】，不是推断的"
                              % prof.downgrade_proven)
            red_libs.append(prof.name)
            ok = False
        elif refused:
            detail.append("      ⇒ 结论: PASS（已迁移；%d 行【从未被认证】—— "
                          "认证器诚实拒绝了它们，正文已漂移，不是被擦）" % refused)
            detail.append("      （这一类的归属是 M6c：被登记且可见；"
                          "它不是降级，也不是迁移缺陷。）")
        elif file_not_mirror:
            # No ledger to speak about the row, and the row is not
            # mirror: the migration's work landed (F1~F4) and this row
            # is not in the certified state ⇒ it was demoted.
            detail.append("      ⇒ 结论: FAIL（迁移后被擦）—— 非 mirror 的行数=%d"
                          % file_not_mirror)
            detail.append("      ⇒ 证据: %s（该库没有账本，降级由『迁移已完成』"
                          "与『现在不是 mirror』两项事实共同确立）" % witness)
            red_libs.append(prof.name)
            ok = False
        else:
            detail.append("      ⇒ 结论: PASS（已迁移且全部 mirror）")

    detail.append("")
    detail.append("未迁移（NOTRUN）的库: %s" % (", ".join(notrun_libs) or "无"))
    detail.append("迁移后被擦（FAIL）的库: %s" % (", ".join(red_libs) or "无"))
    detail.append("两种状态已分开：NOTRUN 的是「迁移没落过」，FAIL 的是「迁移落过、之后被改回去」。")
    # --- the verdict ORDER -------------------------------------------
    #
    # A measured defect OUTRANKS an unmet prerequisite.  If one library
    # was migrated and then erased (a live defect) and another was
    # never migrated (an unmet prerequisite), the criterion is FAIL —
    # reporting NOTRUN would hide the defect behind the library that
    # was merely not ready.  NOTRUN is for "nothing was measured",
    # not for "something was measured and it is broken".
    if red_libs:
        detail.append("")
        detail.append("判定优先级: 存在【已测出】的降级 ⇒ FAIL。"
                      "未迁移的库只是「没测」，不能盖住已经测出来的缺陷。")
        return report.judge(
            "J-2(REAL)",
            "J-2 判别：「没迁移」(NOTRUN) 与「迁移后被擦」(FAIL) 可区分（逐库报 F1~F5 依据链）",
            False, "\n".join(detail))
    if notrun_libs:
        return report.judge_notrun(
            "J-2(REAL)",
            "J-2 判别：「没迁移」(NOTRUN) 与「迁移后被擦」(FAIL) 可区分（逐库报 F1~F5 依据链）",
            "\n".join(detail))
    return report.judge(
        "J-2(REAL)",
        "J-2 判别：「没迁移」(NOTRUN) 与「迁移后被擦」(FAIL) 可区分（逐库报 F1~F5 依据链）",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# J-3: the files statistics columns
# ---------------------------------------------------------------------------

def judge_files_statistics_are_not_erased(report, workspace: Path):
    """J-3 (REAL): the ``files`` statistics columns, per library, three states.

The question this answers: are the ``files`` rows' statistics
columns (``chunk_count`` / ``chunker_version``) NULL because the
library was never migrated, or because an old writer erased them?

The discriminator is the SAME F-chain as J-2, restricted to the
``files`` table:

  * ``chunker_version`` is NULL on every row AND the chunks table
    has no pointer at all ⇒ **never migrated** ⇒ NOTRUN.
  * ``chunker_version`` is NULL on every row BUT the chunks table
    IS migrated (F1..F3 hold, the anchors carry the v4 syntax) ⇒
    the files bookkeeping was erased while the chunks bookkeeping
    survived ⇒ **FAIL**, and the report says which one was erased.
  * ``chunker_version`` holds a value that is NOT the current
    version ⇒ **FAIL**: the library was rebuilt by an older chunker.

So the three verdicts are distinguishable by OBSERVED facts, and
every library prints its own numbers.

------------------------------------------------------------------------
R8 (第八轮) — ONE MOUTH.  Two fixes, both forced by measurement:

**(a) NULL is not a version.**  The old code computed
``others = {k: v for k, v in prof.files_cv.items() if k != version}``
— and since ``None != version``, a row that was **never filled in**
was reported as "**a non-current version**".  That is a different
claim: one says "the value is wrong", the other says "there is no
value".  The criterion's own prose says "holds a value that is NOT
the current version", and NULL holds no value at all.

**(b) The granularity must match the question.**  The library-level
rule ("all cv NULL ⇒ never migrated") cannot answer the
path-level question "was THIS file's statistic erased?".  A library
can hold BOTH migrated files (cv = 4) and files that were written
after the migration and never measured (cv = NULL).  Under the old
rule those NULL rows were invisible: the library was not "all NULL",
so they fell into the "some cv is present" branch, where they were
called "version wrong".

So J-3 now asks M6e's question **per path**, with M6e's
discriminator: a NULL statistic on a path whose chunks side shows
no decided row and no v4 anchor was **never measured** (not a
defect); a NULL statistic on a path whose chunks side IS migrated
was **erased** (a defect).

**The two criteria can no longer disagree.**  They ask the same
question of the same rows with the same discriminator, so for every
row their conclusions MUST agree.  That is the property the
criterion set was missing, and it is asserted by
``tests/test_p1_stage3l_j3_m6e_agreement.py``.
"""
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "J-3(REAL)",
            "J-3 files 统计列：区分「从未迁移」/「被擦成 NULL」/「版本错」（逐库三态）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    version, version_src = current_chunker_version()
    detail.append("当前 CHUNKER_VERSION: %d（真源 %s）" % (version, version_src))
    detail.append("")
    ok = True
    for prof in _profile_all(workspace):
        if prof.error:
            detail.append("  %-22s 前置不成立: %s" % (prof.name, prof.error))
            continue
        detail.append("  %-22s 路径=%s" % (prof.name, prof.db))
        detail.append("      files 行数=%-5d chunk_count IS NULL=%-5d "
                      "chunker_version IS NULL=%-5d  cv 分布=%s"
                      % (prof.files_rows, prof.files_cc_null,
                         prof.files_cv_null, prof.files_cv or "无"))
        detail.append("      chunks 行数=%-6d body_path NULL=%-5d "
                      "锚点带 v4 段语法=%d/%d"
                      % (prof.rows, prof.body_path_null,
                         prof.anchor_v4, prof.anchor_total))

        if prof.files_rows == 0:
            detail.append("      ⇒ 结论: NOTRUN（没有 files 行可判）")
            continue

        # ---- R8: the PATH-level mouth (the same one M6e uses) -----
        #
        # A library can hold BOTH migrated files (cv = 4) and files
        # written after the migration that were never measured (cv =
        # NULL).  The old library-level rule could not see the second
        # kind: the library was not "all NULL", so its NULL rows fell
        # into the "some cv is present" branch and were called "version
        # wrong".  MEASURED (R8, 16 live libraries): 18 rows across
        # agent-mvnewfyb (14) and agent-mvnf7fjl (4) — all of them
        # "never measured", none of them a wrong version.
        #
        # The discriminator is per PATH: the chunks side tells us
        # whether the statistic was ever known.
        null_paths = prof.files_cv_null_paths
        erased_paths: List[str] = []
        never_measured_paths: List[str] = []
        for path in null_paths:
            decided, v4 = prof.files_cv_null_state.get(path, (0, 0))
            if decided == 0 and v4 == 0:
                never_measured_paths.append(path)
            else:
                erased_paths.append(path)

        # The version check — only over the NON-NULL values.  NULL is
        # "no value", not "a value that is not the current one".
        others = {k: v for k, v in prof.files_cv.items()
                  if k is not None and k != version}

        if erased_paths:
            detail.append("      ⇒ 结论: FAIL（被擦成 NULL：%d 个 path 的统计列被清空，"
                          "但 chunks 侧已迁移 —— 已判定行/v4 锚点都在）"
                          % len(erased_paths))
            for p in erased_paths[:5]:
                detail.append("         被擦的 path: %s" % p)
            ok = False
        elif others:
            detail.append("      ⇒ 结论: FAIL（版本错：存在非当前版本的 chunker_version %s）"
                          % others)
            ok = False
        elif never_measured_paths:
            detail.append("      ⇒ 结论: PASS（%d 个 path 的统计列是 NULL，但 chunks 侧"
                          "从未判定过它们 ⇒ 从未测量，不是被擦；其余 %d 个 path 齐备）"
                          % (len(never_measured_paths),
                             prof.files_rows - len(never_measured_paths)))
            for p in never_measured_paths[:5]:
                detail.append("         合法 NULL（从未测量）: %s" % p)
        elif prof.files_cc_null:
            detail.append("      ⇒ 结论: FAIL（chunk_count 有 NULL：files 统计列被部分擦除）")
            ok = False
        else:
            detail.append("      ⇒ 结论: PASS（files 统计列齐备且版本为 %d）" % version)
    detail.append("")
    detail.append("三态可分：从未迁移(NOTRUN) / 被擦成 NULL(FAIL) / 版本错(FAIL) —— "
                  "依据是 chunks 侧是否已迁移（F1~F3 + 锚点 v4 语法）。")
    detail.append("R8: NULL 是「没填过」不是「填错了」；口径与 M6e 同源，"
                  "同一批行的结论必然一致。")
    return report.judge(
        "J-3(REAL)",
        "J-3 files 统计列：区分「从未迁移」/「被擦成 NULL」/「版本错」（逐库三态）",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# J-4: the bulk-rewrite timestamp
# ---------------------------------------------------------------------------

def judge_last_bulk_rewrite_is_reported(report, workspace: Path):
    """J-4 (REAL): when was the library last rewritten wholesale?

    An old writer rewrites a WHOLE library at once, so the rows it
    wrote share one ``created_at`` and span MANY ``path`` values.  A
    normal write touches ONE file.

    **The false-positive defence.**  The threshold is on the DISTINCT
    PATH COUNT, not on the row count: a single large file legitimately
    produces dozens of rows in one second, so a row-count threshold
    would flag healthy libraries.  Rows from several DIFFERENT files at
    the same instant can only come from a writer that was rewriting the
    library.

    The criterion reports the timestamp, and the number of rows and
    paths it covered.  It is a REPORT: the rewrite itself is not a
    defect (the migration writes rows too).  What makes it a defect is
    the STATE that the rewrite left behind — and that is J-2's job.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "J-4(REAL)",
            "J-4 最后一次批量重写的时间戳（按 created_at 跨路径聚类，阈值看路径数不看行数）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    detail.append("批量重写的判据: 同一个 created_at 秒里，跨 ≥%d 个不同 path 且写了 ≥%d 行"
                  % (BULK_PATH_THRESHOLD, BULK_ROW_THRESHOLD))
    detail.append("为什么看路径数不看行数: 一个大文件本来就可能在一秒里产生很多行；"
                  "只有【同时来自多个不同文件】才说明写者在重扫整库。")
    detail.append("")
    reported = 0
    for prof in _profile_all(workspace):
        if prof.error:
            detail.append("  %-22s 前置不成立: %s" % (prof.name, prof.error))
            continue
        detail.append("  %-22s 路径=%s" % (prof.name, prof.db))
        detail.append("      最后写入(updated_at 最大值): %s (%s)"
                      % (prof.last_write, _fmt_ts(prof.last_write)))
        if not prof.created_at_top:
            detail.append("      没有 created_at 列 —— 无法判断批量重写")
            continue
        detail.append("      created_at 聚类（行数最多的 3 个）:")
        for (ts, rows, paths) in prof.created_at_top[:3]:
            detail.append("         %-12s %s  行数=%-5d 跨路径=%d"
                          % (ts, _fmt_ts(ts), rows, paths))
        bulk = prof.bulk_rewrite()
        if bulk:
            ts, rows, paths = bulk
            reported += 1
            detail.append("      ⇒ 最后一次批量重写: %s (%s)  行数=%d 跨路径=%d"
                          % (ts, _fmt_ts(ts), rows, paths))
        else:
            detail.append("      ⇒ 未见批量重写特征（没有单个 created_at 跨 ≥%d 个路径）"
                          % BULK_PATH_THRESHOLD)
    detail.append("")
    detail.append("报告了批量重写特征的库: %d / %d" % (reported, len(libs)))
    detail.append("注意：批量重写本身不是缺陷（迁移也写行）。缺陷是它留下的【状态】——见 J-2。")
    return report.judge(
        "J-4(REAL)",
        "J-4 最后一次批量重写的时间戳（按 created_at 跨路径聚类，阈值看路径数不看行数）",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# J-5: the continuous monitor
# ---------------------------------------------------------------------------

def judge_old_writer_erasure_monitor(report, workspace: Path):
    """J-5 (REAL): is an old writer erasing the libraries right now?

    This is the CONTINUOUS criterion: it is meant to be run again and
    again (by the gate, by a cron, by a human).  It answers one
    question — "has an old writer touched the libraries since I last
    looked?" — and it answers it with EVIDENCE, not with a feeling.

    The evidence it looks for, in order of strength:

      1. **A downgrade is proven** — ``body_keep`` says ``mirror`` and
         the row now says otherwise.  That is proof, not inference.
      2. **A bulk rewrite** — one timestamp, many paths (J-4's
         fingerprint), which is WHEN.
      3. **The guard's own record** — the guard's blocked-write counter
         (``guard_state``) shows that the old writer IS hitting the
         guard, which is the smoking gun for "an old writer is running".

    The criterion is a REPORT with a verdict:
      * **FAIL** when a downgrade is proven (the state was right and
        something overwrote it);
      * **PASS** otherwise, with the evidence printed either way.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "J-5(REAL)",
            "J-5 持续监测：旧写入者是否又擦过库（降级证据 + 批量重写指纹 + 守卫拦截计数）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    version, _src = current_chunker_version()
    detail.append("这是可持续重复运行的判据：每次运行都回答「旧写入者是否又来过」。")
    detail.append("证据按强度排序：① 已证明的降级  ② 批量重写指纹（时间）  ③ 守卫拦截计数")
    detail.append("")

    ok = True
    proven = 0
    bulks = 0
    for prof in _profile_all(workspace):
        if prof.error:
            detail.append("  %-22s 前置不成立: %s" % (prof.name, prof.error))
            continue
        guard = _guard_counter(prof.db)
        bulk = prof.bulk_rewrite()
        detail.append("  %-22s 路径=%s" % (prof.name, prof.db))
        detail.append("      已证明的降级 (body_keep=mirror & chunks≠mirror): %d"
                      % prof.downgrade_proven)
        detail.append("      最近一次置位 (body_keep≠mirror & chunks=mirror): %d"
                      % prof.promoted_since)
        detail.append("      守卫拦截计数: %s" % (guard if guard else "读不到"))
        if bulk:
            ts, rows, paths = bulk
            bulks += 1
            detail.append("      批量重写: %s (%s) 行数=%d 跨路径=%d"
                          % (ts, _fmt_ts(ts), rows, paths))
        else:
            detail.append("      批量重写: 未见")
        if prof.downgrade_proven:
            detail.append("      ⇒ 判定: 旧写入者【擦过】这个库（降级已被证明）")
            proven += 1
            ok = False
        else:
            detail.append("      ⇒ 判定: 没有已证明的降级")

    detail.append("")
    detail.append("已证明降级的库: %d    有批量重写指纹的库: %d    活库总数: %d"
                  % (proven, bulks, len(libs)))
    if proven:
        detail.append("恢复建议：")
        detail.append("  1. 停掉还在跑旧代码的写入者（实测：桌面打包版 cowagent-backend.exe "
                      "构建于 2026-10-13，早于当前切法）")
        detail.append("  2. 重跑迁移（migrate）回填四列，再跑巡检器（inspector）置位 mirror")
        detail.append("  3. 复跑本判据，确认降级证据归零")
    else:
        detail.append("恢复建议：无需恢复。若之后出现降级证据，按上面的三步处理。")
    return report.judge(
        "J-5(REAL)",
        "J-5 持续监测：旧写入者是否又擦过库（降级证据 + 批量重写指纹 + 守卫拦截计数）",
        ok, "\n".join(detail))


def _guard_counter(db: str) -> Optional[str]:
    """The guard's own blocked-write counter, if it keeps one."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute("SELECT key, value FROM guard_state").fetchall()
        return ", ".join("%s=%s" % (k, v) for k, v in rows) or "（空）"
    except sqlite3.Error:
        return None
    finally:
        conn.close()
