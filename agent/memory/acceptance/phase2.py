#!/usr/bin/env python3
# encoding:utf-8
"""Phase-two acceptance criteria for the live-memory system (P1-阶段三).

This file is the CRITERION, not a scratch script: it is in the repository so
that ``git log`` is the history of what was checked and when.  The stage-two
criteria ran in throwaway scripts under ``tmp/``, which meant they could not
be re-run on another machine — the exact failure P0-18 was called to fix.

What makes a criterion a criterion here (the same six rules phase1 uses,
plus the ones this stage added):

  1. **every criterion prints the FINGERPRINT of what it measured** —
     index path, chunks row count, schema fingerprint AND the tables that
     fingerprint covers, plus the git commit.  A PASS without a fingerprint
     is not a PASS;
  2. **three states, never two** — PASS / FAIL / **NOTRUN**.  A criterion
     whose precondition does not hold says NOTRUN and says WHY.  NOTRUN is
     not green;
  3. **the criterion does not modify the object it measures** — the index
     digest is compared before and after (the S-1 principle from phase1);
  4. **unit criteria and corpus criteria are named apart** — ``C-A1.1(unit)``
     builds its own index and tests the RULE, ``C-A1.1(REAL)`` measures a
     real workspace and tests the CORPUS;
  5. **the workspace is explicit** — nothing is resolved through the
     registry or the environment, so one run measures one index;
  6. **the criteria are version-controlled**.

P1-阶段三 S3-A (this file's scope): the four HOLES the migration had to be
ordered behind —

  * C-A1  the schema fingerprint must cover EVERY modelled table, and it
           must SAY which tables it covered (the single-table fingerprint
           reported "consistent" for indexes whose ``files`` tables had
           already forked);
  * C-A2  the manager's two write paths must fill the four
           body-externalization columns (they wrote NULL, which is what
           INV-1 calls "points nowhere");
  * C-A3  ``files.chunk_count`` / ``chunker_version`` must be FILLED, and
           "really zero" must be distinguishable from "not measured";
  * C-A4  this file must exist and be runnable from a clean clone;
  * C-A5  the 0.114 repository must be clean (config.json / its backups
           must be ignored).

Usage:
    python -m agent.memory.acceptance.phase2 --workspace "C:\\Users\\User\\cow"
    python -m agent.memory.acceptance.phase2 --workspace DIR --only C-A1
    python agent/memory/acceptance/phase2.py --workspace DIR
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", default=None,
                    help="the workspace to measure (default: the fixed one)")
parser.add_argument("--only", default=None,
                    help="run only the criteria whose number starts with this")
parser.add_argument("--no-sync", action="store_true",
                    help="do not sync the workspace before the corpus criteria")
ARGS, _ = parser.parse_known_args()

CODE_ROOT = REPO
sys.path.insert(0, str(CODE_ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from agent.memory.acceptance import (  # noqa: E402
    DEFAULT_WORKSPACE,
    Report,
    SourceTreeWorkspaceError,
    assert_not_source_tree,
    fingerprint,
    resolve_workspace,
)


# ---------------------------------------------------------------------------
# The fingerprint of a MEASURED object
# ---------------------------------------------------------------------------

def git_commit() -> str:
    """The commit the criteria ran against — or a clear 'unknown'.

    A criterion run against an unknown build cannot be compared with a
    criterion run against a known one, so the commit is part of the
    fingerprint rather than a footer.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(CODE_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def index_fingerprint_line(workspace: str) -> str:
    """One line describing the index this run measures.

    Includes the schema fingerprint AND the tables it covers: the point of
    C-A1.4 is that "the fingerprints agree" must never be readable without
    "here is what they agreed about".
    """
    from agent.memory.schema_drift import (
        describe_schema_fingerprint,
        schema_fingerprint,
    )

    fp = fingerprint(workspace)
    schema_fp = schema_fingerprint(Path(workspace) / "memory" / "long-term" / "index.db")
    return (f"{fp.render()} | schema_fingerprint="
            f"{describe_schema_fingerprint(schema_fp)} | commit={git_commit()}")


def wait_maintenance(db_path, timeout: float = 15.0) -> bool:
    """Wait for MemoryStorage's background maintenance thread to finish.

    It opens its own connection to run the repair/integrity scan.  On
    Windows a temp directory cannot be removed while that connection is
    open (WinError 32), so the criterion would fail for a reason that has
    nothing to do with what it measures.  This is the probe's obligation,
    not a product bug — phase1.py solved it the same way.
    """
    import time

    import agent.memory.storage as st

    key = str(db_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with st._maintenance_lock:
            if key not in st._maintenance_running:
                return True
        time.sleep(0.1)
    return False


def table_columns(db_path: Path, table: str):
    """The columns a table has, or None when there is no such table."""
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not rows:
        return None
    return {row[1] for row in rows}


def make_index(root: Path, *, with_files_columns: bool = True,
               files_missing=()) -> Path:
    """A throwaway index with the current chunks schema.

    ``files_missing`` names the ``files`` columns to LEAVE OUT — the way to
    build the C-A1.3 reverse case (an index whose chunks table matches
    another index exactly, but whose files table is behind).
    """
    from agent.memory.schema_drift import EXPECTED_CHUNK_COLUMNS

    db = root / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    cols = []
    for name in EXPECTED_CHUNK_COLUMNS:
        if name == "id":
            cols.append("id TEXT PRIMARY KEY")
        elif name in ("user_id", "scope", "source", "path", "text", "hash",
                      "metadata", "body_path", "body_anchor", "body_hash",
                      "text_state", "sealed_owner"):
            cols.append(f"{name} TEXT")
        elif name in ("start_line", "end_line", "created_at", "updated_at",
                      "last_hit_at", "hit_count", "element_released",
                      "file_sealed"):
            cols.append(f"{name} INTEGER")
        elif name == "embedding":
            cols.append("embedding TEXT")
    conn.execute("CREATE TABLE chunks (" + ", ".join(cols) + ")")

    file_cols = ["path TEXT PRIMARY KEY", "source TEXT", "hash TEXT",
                 "mtime INTEGER", "size INTEGER", "updated_at INTEGER"]
    if with_files_columns:
        for name in ("chunk_count", "chunker_version"):
            if name not in files_missing:
                file_cols.append(f"{name} INTEGER")
    conn.execute("CREATE TABLE files (" + ", ".join(file_cols) + ")")
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# C-A1 · the schema fingerprint covers every modelled table
# ---------------------------------------------------------------------------

def judge_ca1_fingerprint_covers_files(report):
    """C-A1.2/C-A1.3/C-A1.4 (unit): the fingerprint sees a files-table fork.

    The reverse case is the heart of it: an index whose ``chunks`` table is
    IDENTICAL to another index's, but whose ``files`` table is missing a
    column.  The OLD (single-table) algorithm reported those two as
    consistent — that is the bug.  The NEW algorithm must report them as
    different.

    The criterion prints the old algorithm's answer too, so the report
    shows the difference rather than just asserting it.
    """
    from agent.memory.schema_drift import (
        EXPECTED_CHUNK_COLUMNS,
        schema_fingerprint,
        schema_fingerprint_tables,
    )

    detail = []
    ok = True

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = make_index(root / "good")
        forked = make_index(root / "forked", files_missing=("chunk_count",))

        good_cols = table_columns(good, "chunks")
        forked_cols = table_columns(forked, "chunks")
        same_chunks = good_cols == forked_cols
        detail.append(f"chunks 列集合相同: {same_chunks}")
        if not same_chunks:
            ok = False
            detail.append("  —— 反向用例的前置条件不成立（两个库的 chunks 不同）")

        detail.append(f"files 列集合: good={sorted(table_columns(good, 'files') or [])}")
        detail.append(f"             forked={sorted(table_columns(forked, 'files') or [])}")

        # The OLD algorithm: the chunks column set only.
        old_good = hashlib.sha256(
            "|".join(sorted(good_cols or ())).encode("utf-8")).hexdigest()
        old_forked = hashlib.sha256(
            "|".join(sorted(forked_cols or ())).encode("utf-8")).hexdigest()
        detail.append(f"旧算法（只查 chunks）指纹: good={old_good[:16]} "
                      f"forked={old_forked[:16]} → 一致? {old_good == old_forked}")
        if old_good != old_forked:
            ok = False
            detail.append("  —— 旧算法本应报『一致』（它看不见 files 的分叉），"
                          "反向用例不成立")

        # The NEW algorithm: every modelled table.
        new_good = schema_fingerprint(good)
        new_forked = schema_fingerprint(forked)
        detail.append(f"新算法指纹: good={new_good} forked={new_forked}")
        if new_good == new_forked:
            ok = False
            detail.append("  —— 新算法没能看出 files 的分叉（H2 未修）")

        # C-A1.4: the coverage must be PRINTABLE.
        tables = schema_fingerprint_tables()
        detail.append(f"指纹覆盖的表: {tables}")
        if "files" not in tables:
            ok = False
            detail.append("  —— 指纹没有覆盖 files 表")

    return report.judge(
        "C-A1.2~C-A1.4(unit)",
        "schema 指纹覆盖多表（chunks+files），能看出 files 分叉，且打印覆盖范围",
        ok, "\n".join(detail))


def judge_ca1_files_columns_real(report):
    """C-A1.1 (REAL): every live index's files table carries the columns.

    Measured over the real workspace; the expectation is the column set the
    current code writes.
    """
    from agent.memory.schema_drift import EXPECTED_FILE_COLUMNS

    workspace, _ = resolve_workspace(ARGS.workspace)
    db = Path(workspace) / "memory" / "long-term" / "index.db"
    detail = []
    ok = True
    if not db.is_file():
        return report.judge(
            "C-A1.1(REAL)", "活库的 files 表列集合 == 期望",
            False,
            f"被测库不存在: {db} —— 前置条件不成立", fingerprint=None)

    cols = table_columns(db, "files")
    missing = [c for c in EXPECTED_FILE_COLUMNS if c not in (cols or ())]
    detail.append(f"库: {db}")
    detail.append(f"files 列集合: {sorted(cols or [])}")
    detail.append(f"缺少: {missing or '无'}")
    if missing:
        ok = False
    return report.judge(
        "C-A1.1(REAL)", "活库的 files 表列集合 == 期望",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# C-A2 · the manager's write paths fill the four columns
# ---------------------------------------------------------------------------

def judge_ca2_add_memory_fills_four_columns(report):
    """C-A2.1/C-A2.2 (unit): add_memory writes the four columns.

    ``add_memory`` stores a blob under a synthetic path and writes no truth
    file, so there is no file to point at — ``body_path`` is honestly None.
    The other three columns must still be filled: a row with four empty
    columns is what INV-1 reports as "points nowhere".
    """
    import asyncio

    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    detail = []
    ok = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = MemoryConfig(workspace_root=tmp, sync_on_search=False)
            mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
            asyncio.run(mgr.add_memory(
                "四列写入判据的内容 MARKER-CA2",
                path="memory/shared/ca2-probe.md"))
            rows = mgr.storage.conn.execute(
                "SELECT body_path, body_anchor, body_hash, text_state "
                "FROM chunks WHERE path=?", ("memory/shared/ca2-probe.md",)
            ).fetchall()
            mgr.storage.close()
            wait_maintenance(cfg.get_db_path())
            # The assertions run INSIDE the workspace: a criterion that
            # measures a world it has already torn down measures nothing.
            if not rows:
                return report.judge(
                    "C-A2.1/C-A2.2(unit)",
                    "add_memory 写出的行：可填的三列（body_anchor/body_hash/text_state）非空，"
                    "body_path 为 None（blob 行无真源文件，诚实值）",
                    False, "没有写入任何 chunk 行 —— 前置条件不成立")
            for bp, ba, bh, ts in rows:
                detail.append(f"body_path={bp!r} body_anchor={ba!r} "
                              f"body_hash={(bh or '')[:23]}… text_state={ts!r}")
                if not ba:
                    ok = False
                    detail.append("  —— body_anchor 为空")
                if not bh:
                    ok = False
                    detail.append("  —— body_hash 为空")
                if not ts:
                    ok = False
                    detail.append("  —— text_state 为空")
                if bp is not None:
                    ok = False
                    detail.append("  —— body_path 非空：add_memory 写的是 blob，"
                                  "没有真源文件可指，诚实值必须是 None")
    except Exception as e:
        return report.judge(
            "C-A2.1/C-A2.2(unit)",
            "add_memory 写出的行：可填的三列（body_anchor/body_hash/text_state）非空，"
            "body_path 为 None（blob 行无真源文件，诚实值）",
            False, f"{type(e).__name__}: {e}")

    return report.judge(
        "C-A2.1/C-A2.2(unit)",
        "add_memory 写出的行：可填的三列（body_anchor/body_hash/text_state）非空，"
        "body_path 为 None（blob 行无真源文件，诚实值）",
        ok, "\n".join(detail))


def judge_ca2_sync_fills_four_columns(report):
    """C-A2.1/C-A2.2/C-A2.5 (unit): the sync path writes a resolvable pointer.

    This is the path that writes the BULK of the index, and it is the one
    whose rows a migration backfill would be undone by.  The criterion
    asserts that a synced file's chunks carry a ``body_path`` that RESOLVES
    on disk, an anchor that locates inside it, and a hash that matches what
    the anchor reads back.
    """
    import asyncio

    from agent.memory.body_pointer import resolve_truth_file
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from agent.memory.paths import path_escape_reason

    detail = []
    ok = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "memory" / "shared").mkdir(parents=True, exist_ok=True)
            body = "# 标题\n\n这是判据用的正文内容 MARKER-CA2-SYNC。\n"
            (root / "memory" / "shared" / "probe.md").write_text(
                body, encoding="utf-8")
            cfg = MemoryConfig(workspace_root=str(root), sync_on_search=False)
            mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
            asyncio.run(mgr.sync())
            rows = mgr.storage.conn.execute(
                "SELECT id, path, body_path, body_anchor, body_hash, "
                "text_state FROM chunks WHERE path=?",
                ("memory/shared/probe.md",)).fetchall()
            mgr.storage.close()
            wait_maintenance(cfg.get_db_path())

            # The assertions run INSIDE the workspace (see the note in
            # C-A2.1): the file behind the pointer must still be there.
            if not rows:
                return report.judge(
                    "C-A2.1/C-A2.2/C-A2.5(unit)", "sync 写出的行四列非空且可解析",
                    False, "sync 没有索引该文件 —— 前置条件不成立")

            from agent.memory.text_inv import read_fragment_at_anchor

            for cid, path, bp, ba, bh, ts in rows:
                detail.append(f"chunk {cid[:8]}: body_path={bp!r} "
                              f"body_anchor={ba!r} "
                              f"body_hash={(bh or '')[:23]}… text_state={ts!r}")
                if not bp or not ba or not bh or not ts:
                    ok = False
                    detail.append("  —— 四列有空的")
                    continue
                # C-A2.5: POSIX, relative, no '..'.
                if "\\" in bp or bp.startswith("/") or ".." in bp.split("/"):
                    ok = False
                    detail.append(f"  —— body_path 不安全: {bp!r}")
                reason = path_escape_reason(bp, root)
                if reason:
                    ok = False
                    detail.append(f"  —— body_path 越界: {reason}")
                # C-A2.2: the hash matches what the anchor reads back.
                truth = resolve_truth_file(root, bp)
                if truth is None:
                    ok = False
                    detail.append(f"  —— body_path 指向的文件不存在: {bp!r}")
                    continue
                frag = read_fragment_at_anchor(truth, ba)
                if frag is None:
                    ok = False
                    detail.append(f"  —— anchor {ba!r} 在文件里定位不到")
                    continue
                import hashlib

                from agent.memory.naming import canonical_text

                expected = "sha256:" + hashlib.sha256(
                    canonical_text(frag).encode("utf-8")).hexdigest()
                if bh != expected:
                    ok = False
                    detail.append(f"  —— body_hash 与文件重算不一致: "
                                  f"{bh[:23]}… != {expected[:23]}…")
    except Exception as e:
        return report.judge(
            "C-A2.1/C-A2.2/C-A2.5(unit)", "sync 写出的行四列非空且可解析",
            False, f"{type(e).__name__}: {e}")

    return report.judge(
        "C-A2.1/C-A2.2/C-A2.5(unit)", "sync 写出的行四列非空且可解析",
        ok, "\n".join(detail))


def judge_ca2_inv1_no_new_violations(report):
    """C-A2.3 (unit): a freshly synced file adds NO INV-1 violation.

    INV-1 says: a row in state projection/mirror must carry a pointer that
    resolves.  Before the fix, every newly synced row was a violation.
    """
    import asyncio

    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from agent.memory.text_inv import check_inv1

    detail = []
    ok = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "memory" / "shared").mkdir(parents=True, exist_ok=True)
            (root / "memory" / "shared" / "probe.md").write_text(
                "# t\n\nINV-1 判据用的正文。\n", encoding="utf-8")
            cfg = MemoryConfig(workspace_root=str(root), sync_on_search=False)
            mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
            asyncio.run(mgr.sync())
            conn = mgr.storage.conn
            report_inv = check_inv1(conn, root)
            # The criterion measures ITS OWN file's rows.  The temp
            # workspace also indexes the SHARED knowledge tree (that is
            # what the scan does), and those rows are not what this
            # criterion is about — asserting on them would measure the
            # shared corpus instead of the write path.
            mine = [v for v in report_inv["violations"]
                    if v.get("chunk_id") in {
                        r[0] for r in conn.execute(
                            "SELECT id FROM chunks WHERE path=?",
                            ("memory/shared/probe.md",)).fetchall()}]
            total = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE path=?",
                ("memory/shared/probe.md",)).fetchone()[0]
            mgr.storage.close()
            wait_maintenance(cfg.get_db_path())
    except Exception as e:
        return report.judge(
            "C-A2.3(unit)", "新写入的行不新增 INV-1 violation",
            False, f"{type(e).__name__}: {e}")

    detail.append(f"判据文件的 chunk 行数: {total}")
    detail.append(f"INV-1 总 verdict: {report_inv['verdict']} "
                  f"({report_inv['fingerprint']})")
    detail.append(f"判据文件的 violation 数: {len(mine)}")
    for v in mine[:5]:
        detail.append(f"  {v.get('code')}: {v.get('detail', '')[:110]}")
    if mine:
        ok = False
        detail.append("  —— 新写入的行触发了 INV-1（body_path 指向 nowhere）")
    return report.judge(
        "C-A2.3(unit)", "新写入的行不新增 INV-1 violation",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# C-A3 · the files bookkeeping columns are FILLED
# ---------------------------------------------------------------------------

def judge_ca3_files_count_is_real(report):
    """C-A3.1/C-A3.2/C-A3.3 (unit): chunk_count is the REAL count.

    The criterion distinguishes the two zeros: a file that really produced
    no chunks (count 0, written by the writer) and a file nobody measured
    (NULL).  It also checks that the count matches what is actually in the
    chunks table — not what the writer believed.
    """
    import asyncio

    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from agent.memory.chunker import TextChunker

    detail = []
    ok = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "memory" / "shared").mkdir(parents=True, exist_ok=True)
            (root / "memory" / "shared" / "a.md").write_text(
                "# a\n\n正文。\n", encoding="utf-8")
            (root / "memory" / "shared" / "b.md").write_text(
                "# b\n\n正文。\n", encoding="utf-8")
            cfg = MemoryConfig(workspace_root=str(root), sync_on_search=False)
            mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
            asyncio.run(mgr.sync())
            rows = mgr.storage.conn.execute(
                "SELECT path, chunk_count, chunker_version FROM files "
                "WHERE path LIKE 'memory/shared/%' ORDER BY path").fetchall()
            actual = dict(mgr.storage.conn.execute(
                "SELECT path, COUNT(*) FROM chunks "
                "WHERE path LIKE 'memory/shared/%' GROUP BY path").fetchall())
            mgr.storage.close()
            wait_maintenance(cfg.get_db_path())

            # The assertions run INSIDE the workspace.
            if not rows:
                return report.judge(
                    "C-A3.1~C-A3.3(unit)",
                    "files.chunk_count 是真实值，0 与 NULL 可区分",
                    False, "files 表没有本判据的行 —— 前置条件不成立")

            for path, count, version in rows:
                real = actual.get(path, 0)
                detail.append(f"{path}: chunk_count={count!r} (实际 {real}) "
                              f"chunker_version={version!r}")
                if count is None:
                    ok = False
                    detail.append("  —— chunk_count 是 NULL（没填），不是真实值")
                elif int(count) != int(real):
                    ok = False
                    detail.append(f"  —— chunk_count 与实际不符: "
                                  f"{count} != {real}")
                if version is None:
                    ok = False
                    detail.append("  —— chunker_version 是 NULL")
                elif int(version) != TextChunker.CHUNKER_VERSION:
                    ok = False
                    detail.append(
                        f"  —— chunker_version != {TextChunker.CHUNKER_VERSION}")
    except Exception as e:
        return report.judge(
            "C-A3.1~C-A3.3(unit)", "files.chunk_count 是真实值，0 与 NULL 可区分",
            False, f"{type(e).__name__}: {e}")

    return report.judge(
        "C-A3.1~C-A3.3(unit)", "files.chunk_count 是真实值，0 与 NULL 可区分",
        ok, "\n".join(detail))


def judge_ca3_not_measured_is_distinguishable(report):
    """C-A3.3 (unit): "not measured" is NULL, "really zero" is 0.

    The two zeros must not look alike.  A file whose count was never
    measured reads NULL; a file that was measured and really produced no
    chunks reads 0.
    """
    from agent.memory.storage import MemoryStorage

    detail = []
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "index.db"
        storage = MemoryStorage(db_path=db)
        # Not measured: the default.
        storage.update_file_metadata("memory/shared/unmeasured.md", "memory",
                                     "h", 1, 1)
        # Measured, and really zero.
        storage.update_file_metadata("memory/shared/zero.md", "memory",
                                     "h", 1, 1, chunk_count=0,
                                     chunker_version=3)
        rows = storage.conn.execute(
            "SELECT path, chunk_count FROM files ORDER BY path").fetchall()
        storage.close()
        # The maintenance thread holds the file open on Windows; without
        # this the temp directory cannot be removed and the criterion
        # fails with WinError 32 — a FAIL about the probe, not the
        # product.
        wait_maintenance(db)
        got = {p: c for p, c in rows}

    detail.append(f"未测量的文件: chunk_count={got.get('memory/shared/unmeasured.md')!r} "
                  f"（要 None）")
    detail.append(f"实测为零的文件: chunk_count={got.get('memory/shared/zero.md')!r} "
                  f"（要 0）")
    if got.get("memory/shared/unmeasured.md") is not None:
        ok = False
        detail.append("  —— 『没填』与『真的是 0』不可区分")
    if got.get("memory/shared/zero.md") != 0:
        ok = False
        detail.append("  —— 实测为零应写 0")
    return report.judge(
        "C-A3.3(unit)", "『没填』(NULL) 与 『真的是 0』 可区分",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# C-A4 · this file exists, is in the repository, and does not modify what it
#        measures
# ---------------------------------------------------------------------------

def judge_ca4_criterion_is_in_the_repository(report):
    """C-A4.1/C-A4.2: the criterion is version-controlled and runnable.

    "Runnable from a clean clone" is checked the only way it can be: the
    file must be TRACKED by git (a clone only gets tracked files), and the
    module must be importable from the repository root.
    """
    detail = []
    ok = True
    this_file = Path(__file__).resolve()
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "--error-unmatch",
             str(this_file.relative_to(REPO)).replace(os.sep, "/")],
            capture_output=True, text=True, timeout=10)
        tracked = out.returncode == 0
    except Exception as e:
        tracked = False
        detail.append(f"git ls-files 失败: {e}")
    detail.append(f"本文件在版本库内: {tracked} ({this_file.name})")
    if not tracked:
        ok = False
        detail.append("  —— 一个不在版本库里的判据，换台机器就复现不了")

    # The module must be importable as ``agent.memory.acceptance.phase2``.
    import importlib

    try:
        mod = importlib.import_module("agent.memory.acceptance.phase2")
        detail.append(f"模块可导入: True")
    except Exception as e:
        ok = False
        detail.append(f"模块可导入: False ({type(e).__name__}: {e})")
    return report.judge(
        "C-A4.1/C-A4.2(unit)", "判据文件在版本库内且可导入",
        ok, "\n".join(detail))


def judge_ca4_does_not_modify_what_it_measures(report):
    """C-A4.4: the criteria did not modify the index they measured.

    The S-1 principle from phase1: a criterion that changes the thing it
    measures is measuring a state it manufactured itself.  The comparison
    is on the CONTENT digest (rows + hit_count), not the byte digest — a
    WAL checkpoint moves bytes without changing a row.
    """
    workspace, _ = resolve_workspace(ARGS.workspace)
    fp_before = fingerprint(workspace)
    detail = [f"判据执行前: {fp_before.render()}"]

    # The corpus criteria read the index; they do not write to it.  This
    # criterion runs the SAME read path the corpus criteria use and then
    # compares the digest.
    try:
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager

        cfg = MemoryConfig(workspace_root=workspace, sync_on_search=False)
        mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
        try:
            mgr.storage.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            mgr.storage.conn.execute("SELECT path, chunk_count FROM files").fetchall()
        finally:
            mgr.storage.close()
    except Exception as e:
        detail.append(f"读被测库时异常: {type(e).__name__}: {e}")

    fp_after = fingerprint(workspace)
    detail.append(f"判据执行后: {fp_after.render()}")
    changed = fp_after.differs_from(fp_before)
    if changed:
        detail.append(f"被测库发生变化: {', '.join(changed)}")
        detail.append("  —— 判据修改了它要验收的对象；即使其它判据全绿，"
                      "本次结果不可作为『系统行为正确』的证据")
        return report.judge(
            "C-A4.4(unit)", "判据未修改被测库（前后指纹一致）",
            False, "\n".join(detail), fingerprint=fp_after)
    detail.append("被测库未变 —— 判据没有修改它要验收的对象")
    return report.judge(
        "C-A4.4(unit)", "判据未修改被测库（前后指纹一致）",
        True, "\n".join(detail), fingerprint=fp_after)


# ---------------------------------------------------------------------------
# C-A5 · the deployed repository is clean
# ---------------------------------------------------------------------------

def judge_ca5_gitignore_covers_config(report):
    """C-A5.1 (unit): config.json and its backups are ignored.

    The 0.114 host had ``?? config.json.bak-20261028-120030`` in the
    working copy, which made the deploy script's "the working copy is
    clean" criterion permanently red — the deployment script was failing
    itself.
    """
    detail = []
    ok = True
    gitignore = REPO / ".gitignore"
    if not gitignore.is_file():
        return report.judge(
            "C-A5.1(unit)", ".gitignore 覆盖 config.json 与 config.json.bak-*",
            False, "仓库根没有 .gitignore")
    text = gitignore.read_text(encoding="utf-8")
    patterns = [line.strip() for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")]
    detail.append(f".gitignore 模式数: {len(patterns)}")

    def covered(name: str) -> bool:
        """Does git IGNORE this path?  Asked of git itself, not of a guess."""
        try:
            out = subprocess.run(
                ["git", "-C", str(REPO), "check-ignore", "-q", name],
                capture_output=True, text=True, timeout=10)
            return out.returncode == 0
        except Exception:
            return False

    for name in ("config.json", "config.json.bak-20261028-120030",
                 "config.json.bak-20261029-010101"):
        ignored = covered(name)
        detail.append(f"git check-ignore {name} → {'忽略' if ignored else '未忽略'}")
        if not ignored:
            ok = False
    return report.judge(
        "C-A5.1(unit)", ".gitignore 覆盖 config.json 与 config.json.bak-*",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

CRITERIA = [
    ("C-A1", judge_ca1_fingerprint_covers_files),
    ("C-A1", judge_ca1_files_columns_real),
    ("C-A2", judge_ca2_add_memory_fills_four_columns),
    ("C-A2", judge_ca2_sync_fills_four_columns),
    ("C-A2", judge_ca2_inv1_no_new_violations),
    ("C-A3", judge_ca3_files_count_is_real),
    ("C-A3", judge_ca3_not_measured_is_distinguishable),
    ("C-A4", judge_ca4_criterion_is_in_the_repository),
    ("C-A4", judge_ca4_does_not_modify_what_it_measures),
    ("C-A5", judge_ca5_gitignore_covers_config),
]

# ---------------------------------------------------------------------------
# P1-阶段三 S3-E · the criteria that close the six gaps
# ---------------------------------------------------------------------------
#
# They live in their own module (``phase2_s3e``) for one reason: the
# enumeration of the live libraries is the piece most likely to be subtly
# wrong (it picked the wrong ROOT once already), so it deserves its own
# file where somebody will actually look at it.
#
# They take the workspace as an ARGUMENT, because the S3-E criteria are
# about a workspace, not about the default one.

_S3E_CRITERIA = [
    ("C-A6", "judge_ca6_every_live_library_is_profiled"),
    ("C-A6", "judge_ca6_agent_workspace_root_is_the_agent_dir"),
    ("C-A1", "judge_ca1_files_columns_every_live_library"),
    ("M1", "judge_m1_fingerprints_agree_across_libraries"),
    ("M4", "judge_m4_every_library_was_backfilled"),
    ("Q1", "judge_q1_inv1_every_library"),
    ("Q2", "judge_q2_inv2_inv3_every_library"),
    ("Q5", "judge_q5_no_library_regressed"),
    ("R-S3-5", "judge_r_s3_5_mirror_has_a_setter"),
    ("S-1", "judge_s1_criteria_do_not_modify_what_they_measure"),
    ("S-6", "judge_s6_criterion_names_say_what_they_measure"),
]

# ---------------------------------------------------------------------------
# S3-H · the criteria that align with S3-D's landed reality.
#
# They live in their own module (``phase2_s3h``) because they are the
# ones that had to CHANGE against the implementation: the chunker version
# in three states, the M6 three-part mouth, the byte-equality assertion
# computed independently, the anti-downgrade UPSERT and the single
# parser.  Each of those has a reason to exist that the S3-E ones do
# not, and burying them in the S3-E module would hide that.
#
# M6 is REPLACED, not duplicated: the S3-E ``judge_m6_body_origin_mouth``
# used the old (unsatisfiable) mouth, and keeping two M6 criteria in the
# report would mean one of them is always red for a reason nobody can
# fix.  The S3-H one replaces it.
_S3H_CRITERIA = [
    ("C-A3", "judge_ca3_chunker_version_is_consistent"),
    ("M6", "judge_m6_body_origin_mouth"),
    ("INV-2", "judge_inv2_anchor_rebuild_is_byte_equal"),
    ("ANCHOR", "judge_anchor_segments_are_self_consistent"),
    ("UPSERT", "judge_upsert_does_not_downgrade_the_body_columns"),
    ("PARSER", "judge_anchor_parser_is_single"),
]

# ---------------------------------------------------------------------------
# S3-J · the criteria that tell 「没迁移」 and 「迁移后被擦」 apart.
#
# They live in their own module (``phase2_s3j``) because they answer a
# question none of the others do: WHEN a library's state looks wrong, is
# that because the migration never ran (a missing prerequisite) or
# because the migration ran and something overwrote its work (a live
# defect)?  Those two need different verdicts, and a criterion that
# cannot tell them apart is either crying wolf or waving things
# through.
_S3J_CRITERIA = [
    ("J-2", "judge_migration_state_is_discriminable"),
    ("J-3", "judge_files_statistics_are_not_erased"),
    ("J-4", "judge_last_bulk_rewrite_is_reported"),
    ("J-5", "judge_old_writer_erasure_monitor"),
]

# The criteria that GUARD the criteria (MU-12 / MU-14 / the replay).
_GUARD_CRITERIA = [
    ("MU-12", "judge_mu12_three_states_are_real"),
    ("MU-14", "judge_mu14_coverage_is_not_overreported"),
    ("MU-REPLAY", "judge_mutation_replay_is_registered"),
]


def _load_s3e():
    """The S3-E criterion functions, bound to the workspace."""
    from agent.memory.acceptance import phase2_s3e

    ws = Path(resolve_workspace(ARGS.workspace)[0])
    out = []
    for group, attr in _S3E_CRITERIA:
        fn = getattr(phase2_s3e, attr)

        def make(fn=fn, ws=ws):
            def run(report):
                return fn(report, ws)
            return run

        out.append((group, make()))
    return out


def _load_guards():
    """The criteria that guard the criteria (they take no workspace)."""
    from agent.memory.acceptance import phase2_guards

    out = []
    for group, attr in _GUARD_CRITERIA:
        fn = getattr(phase2_guards, attr)

        def make(fn=fn):
            def run(report):
                return fn(report)
            return run

        out.append((group, make()))
    return out


def _load_s3h():
    """The S3-H criterion functions, bound to the workspace."""
    from agent.memory.acceptance import phase2_s3h

    ws = Path(resolve_workspace(ARGS.workspace)[0])
    out = []
    for group, attr in _S3H_CRITERIA:
        fn = getattr(phase2_s3h, attr)

        def make(fn=fn, ws=ws):
            def run(report):
                return fn(report, ws)
            return run

        out.append((group, make()))
    return out


def _load_s3j():
    """The S3-J criterion functions, bound to the workspace."""
    from agent.memory.acceptance import phase2_s3j

    ws = Path(resolve_workspace(ARGS.workspace)[0])
    out = []
    for group, attr in _S3J_CRITERIA:
        fn = getattr(phase2_s3j, attr)

        def make(fn=fn, ws=ws):
            def run(report):
                return fn(report, ws)
            return run

        out.append((group, make()))
    return out


def main() -> int:
    workspace, how = resolve_workspace(ARGS.workspace)

    try:
        assert_not_source_tree(workspace, REPO)
    except SourceTreeWorkspaceError as e:
        print("=" * 72)
        print("阶段三验收 · 拒绝执行")
        print("=" * 72)
        print(str(e))
        return 2

    print("=" * 72)
    print("记忆体系升级 · 阶段三验收（S3-A 补洞 + S3-E 判据落地）")
    print("=" * 72)
    print(f"code root : {CODE_ROOT}")
    print(f"workspace : {workspace}  （{how}）")
    print(f"被测库    : {index_fingerprint_line(workspace)}")
    print()

    report = Report()

    def guarded(fn, *args, **kwargs):
        """A criterion that raises IS a failure — never a silent pass."""
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            import traceback
            return report.judge(
                getattr(fn, "__name__", "?"), "判据执行异常（按 FAIL 计）", False,
                f"{type(e).__name__}: {e}\n" + "\n".join(
                    traceback.format_exc().splitlines()[-4:]))

    for group, fn in CRITERIA:
        if ARGS.only and not group.startswith(ARGS.only):
            continue
        guarded(fn, report)

    # The S3-E criteria.
    for group, fn in _load_s3e():
        if ARGS.only and not group.startswith(ARGS.only):
            continue
        guarded(fn, report)

    # The S3-H criteria (aligned with S3-D's landed implementation).
    for group, fn in _load_s3h():
        if ARGS.only and not group.startswith(ARGS.only):
            continue
        guarded(fn, report)

    # The S3-J criteria (「没迁移」 vs 「迁移后被擦」).
    for group, fn in _load_s3j():
        if ARGS.only and not group.startswith(ARGS.only):
            continue
        guarded(fn, report)

    # The criteria that guard the criteria.
    for group, fn in _load_guards():
        if ARGS.only and not group.startswith(ARGS.only):
            continue
        guarded(fn, report)

    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
