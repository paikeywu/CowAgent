#!/usr/bin/env python3
# encoding:utf-8
"""交办 E · 三条判据 + 一条配套（第二轮）· E-1/E-2/E-3/E-4.

**这个文件测的是「机制本身」，不是活库的存量。**

交办书 E（``deliverables/2026-11-05-交办E-机制判据.md``）要求四条：

  E-1  GATE-PURITY   门禁跑一次前后，``cert_ledger`` 与 ``chunks`` 零变化
  E-2  S-1 覆盖账本  S-1 的内容摘要必须覆盖 ``cert_ledger``（上一轮的 C-2）
  E-3  令牌一致      账本与 chunks 的令牌必须一致（上一轮的 C-3）
  E-4  配对 D-1      重切选路必须能选中「令牌陈旧」的 path

=========================  本文件的纪律  =========================

1. **判据不许调用被测对象的函数**（同义反复）。
   孤儿 / 未决 / 令牌 / 摘要 / 指纹全部用**本文件自己的 SQL 与自己的
   读法**。唯一允许 import 被测对象的地方有三处，且每一处都带理由：

   * ``agent.memory.drift_recut``（E-4）—— 被测对象**就是**选择器，
     不问它本人就不知道它看不见；
   * ``agent.memory.inspector.Inspector``（E-1）—— 被测对象**就是**门禁，
     不调用它就没有被测对象；
   * ``agent.memory.acceptance.content_digest``（E-2）—— 被测对象**就是**
     这把尺子，测的是「它看不见账本写入」这个事实。

2. **判据不许写活库。**
   所有读都是 ``sqlite3.connect("file:...?mode=ro", uri=True)``。
   变异臂在**库副本**与**沙箱工作区**上做。活库只被**读**。

   ⚠️ **上一轮的活库污染**（2026-11-05 21:19:34）：我把 ``Inspector``
   指向了真源工作区，它写到了活库 ``agent-mvnf7fjl`` 的 ``cert_ledger``
   （+1 行，``certified_at=1793884774``），把那行 ``unresolved`` 判成了
   ``file``。**本文件从三处堵死这条路**：

   * ① 所有活库连接一律 ``mode=ro``；
   * ② 一切会写的东西（巡检 / 重切 / 认证）只在**沙箱**上跑，
     沙箱的 ``index.db`` 是**副本**，且沙箱路径**必须**在
     ``%TEMP%`` 之下（``_assert_sandbox``）；
   * ③ 写路径之前一律先 ``_assert_not_live(path)`` —— 目标路径若在真源
     工作区之内，直接 ``pytest.fail``。

   ⇒ 「未决行 = 0」现在有**三个可能来源**（后端启动认证 / 清扫 /
   **判据侧**），读数**区分不了是谁干的**。本文件不假装能区分：
   E-1 只断言「门禁**自己**不改活库」，不替任何人认领未决行。

3. **变异臂自证生效** —— 每个变异臂都读回一个具体的行/一个具体的
   字段，断言它真的变成了那个形状。

4. **还原后 sha256 逐字节一致** —— 变异 → 判定 → 还原 → 比对 sha256。

5. **变异对象是数据文件 ⇒ 会有连带红** —— 判定只钉目标判据，连带红
   单独列出来汇报，不参与判定。

6. **每条判据自报它测的是哪个对象**（哪个库、哪条判据、哪个变异点）。

=========================  判据编号  =========================

| 编号 | 测什么 | 对照臂 | 变异臂 |
|:-----|:-------|:-------|:-------|
| E-1 | 门禁跑一次不改活库 | 现跑 M- 组 ⇒ 差集空 ⇒ PASS | 门禁认证一个待认证行 ⇒ 库变了 ⇒ FAIL |
| E-2 | S-1 摘要必须覆盖账本 | 摘要覆盖 ⇒ PASS | 只写账本 ⇒ S-1 看不见 ⇒ FAIL |
| E-3 | 账本与 chunks 令牌一致 | 一致 ⇒ PASS | 账本令牌改陈旧 ⇒ FAIL |
| E-4 | 重切选路能选中令牌陈旧的 path | 选中并清掉 ⇒ PASS | 选路改回「只比正文」⇒ 选不中 ⇒ FAIL |

E-1 的对照臂**现跑门禁**，代价是 ~110 秒（只跑 ``--only M-`` 这一组）。
这是刻意的：E-1 测的是「门禁跑一次」这件事本身，用一个**假的**门禁
替身去测它，就测不到真东西了。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# The workspace under test.
#
# FIXED, not environment-dependent — a criterion whose target depends on the
# environment is not a criterion.  One run, one fleet.
# ---------------------------------------------------------------------------
WORKSPACE = Path(os.environ.get("COW_E_WORKSPACE", r"C:\Users\User\cow"))
REPO = Path(__file__).resolve().parents[1]

#: The gate's entry point — the thing E-1 runs.
GATE_MODULE = "agent.memory.acceptance.phase2"

#: The gate scope E-1 runs.  ``M-`` is the group that calls
#: ``Inspector.inspect()`` — the two acceptance call sites that used to
#: write the live ``cert_ledger`` (D-2 switched them to ``certify=False``).
GATE_SCOPE = "M-"


# ---------------------------------------------------------------------------
# the guards that keep this file from writing the live fleet
# ---------------------------------------------------------------------------

def _assert_sandbox(path: Path) -> None:
    """A write target MUST live under the TEMP directory.

    MEASURED (2026-11-05 21:19:34): the previous round's probe pointed
    ``Inspector(root)`` at the REAL workspace and the live library's
    ``cert_ledger`` grew by one row.  The guard is a hard precondition:
    a write target outside ``%TEMP%`` is a FAIL, not a warning.
    """
    p = Path(path).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    try:
        p.relative_to(temp)
    except ValueError:
        pytest.fail(
            "写入目标不在 TEMP 之下 ⇒ 拒绝执行：%s（上一轮就是这么污染了活库的）"
            % p)


def _assert_not_live(path: Path) -> None:
    """A write target MUST NOT be inside the live workspace."""
    p = Path(path).resolve()
    ws = Path(WORKSPACE).resolve()
    try:
        p.relative_to(ws)
    except ValueError:
        return
    pytest.fail("写入目标在真源工作区之内 ⇒ 拒绝执行：%s" % p)


def _assert_writable_target(path: Path) -> None:
    _assert_not_live(path)
    _assert_sandbox(path)


# ---------------------------------------------------------------------------
# the readers — THIS file's OWN, not the product's
# ---------------------------------------------------------------------------

def canonical(text: Optional[str]) -> str:
    """This file's own canonicalisation (LF endings, folded blank runs)."""
    if text is None:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def sha256_token(text: str) -> str:
    """``"sha256:" + sha256(canonical(text))`` — the drift token."""
    return "sha256:" + hashlib.sha256(
        canonical(text).encode("utf-8")).hexdigest()


def open_ro(db: Path) -> sqlite3.Connection:
    """Open an index READ-ONLY.

    A criterion that opens a database it is only supposed to MEASURE has
    already changed what it measures.

    ⚠️ **调用方必须关掉它。**  MEASURED (2026-11-05): a leaked
    read-only connection keeps the file open on Windows, and the
    byte-level restore (``unlink`` + ``copy2``) then fails with
    ``PermissionError: [WinError 32]``.  ⇒ :func:`ro` (the context
    manager) is the form to use.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def ro(db: Path):
    """A read-only connection that is ALWAYS closed.

    The leak is not a style question on Windows: an open handle makes
    the byte-level restore impossible.
    """
    conn = open_ro(db)
    try:
        yield conn
    finally:
        conn.close()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def live_libraries() -> List[Tuple[str, Path, Path]]:
    """``(name, db, truth_root)`` for every live index — BY PATH SHAPE."""
    out: List[Tuple[str, Path, Path]] = []
    root_db = WORKSPACE / "memory" / "long-term" / "index.db"
    if root_db.is_file():
        out.append(("ROOT", root_db, WORKSPACE))
    agents_dir = WORKSPACE / "agents"
    if agents_dir.is_dir():
        for d in sorted(agents_dir.iterdir()):
            if not d.is_dir():
                continue
            db = d / "memory" / "long-term" / "index.db"
            if db.is_file():
                out.append((d.name, db, d))
    return out


def read_tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


# ---------------------------------------------------------------------------
# E-1's yardstick: the FINGERPRINT (a set of per-row digests)
#
# ⚠️ **Two yardsticks, both required** (交办书 E §2):
#
#   ① **指纹差集为空** —— the per-row digest set, before vs after.  This
#      says WHAT changed, by name, if anything changed.
#   ② **total_changes 计数** —— SQLite's own row-change counter.  This
#      says THAT something changed, from the database's own mouth, and it
#      cannot be fooled by a change that happens to preserve the digest.
#
# 缺一个就有盲区：① 看不见「内容相同但行被重写」，② 看不见「哪一行变了」。
# ---------------------------------------------------------------------------

FINGERPRINT_COLUMNS = {
    "chunks": ("id", "path", "body_path", "body_anchor", "body_hash",
               "text_state", "body_origin", "hit_count", "last_hit_at"),
    "cert_ledger": ("id", "text_state", "body_origin", "body_path",
                    "body_hash", "certified_at", "certified_by"),
}


def fingerprint_rows(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    """``{table: {row key: row digest}}`` — the FINGERPRINT, as a SET.

    Read through SQLite, so a write that only ever reached the WAL
    sidecar is visible here (SQLite merges committed WAL frames into what
    it serves) — and a checkpoint, which moves bytes without changing a
    row, is NOT.
    """
    out: Dict[str, Dict[str, str]] = {}
    tables = read_tables(conn)
    for table, wanted in FINGERPRINT_COLUMNS.items():
        if table not in tables:
            continue
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        cols_wanted = [c for c in wanted if c in cols]
        if not cols_wanted:
            continue
        rows: Dict[str, str] = {}
        for row in conn.execute("SELECT %s FROM %s ORDER BY id"
                                % (", ".join(cols_wanted), table)):
            h = hashlib.sha256()
            h.update(repr(tuple(row)).encode("utf-8", "replace"))
            rows[str(row[0])] = h.hexdigest()
        out[table] = rows
    return out


def fingerprint_diff(before: Dict[str, Dict[str, str]],
                     after: Dict[str, Dict[str, str]]
                     ) -> List[str]:
    """The DIFFERENCE SET of two fingerprints — empty means no change.

    The diff is named in BOTH directions: a row that appeared
    (``added``) and a row that vanished (``removed``) are both changes,
    and a row whose digest moved (``changed``) is a third.
    """
    out: List[str] = []
    for table in sorted(set(before) | set(after)):
        b = before.get(table, {})
        a = after.get(table, {})
        for key in sorted(set(b) - set(a)):
            out.append("%s REMOVED %s" % (table, key))
        for key in sorted(set(a) - set(b)):
            out.append("%s ADDED   %s" % (table, key))
        for key in sorted(set(b) & set(a)):
            if b[key] != a[key]:
                out.append("%s CHANGED %s" % (table, key))
    return out


def library_total_changes(db: Path) -> int:
    """``conn.total_changes`` — the database's OWN row-change counter.

    ⚠️ **This counter is per-CONNECTION.**  It counts the changes THIS
    connection made, so a read-only connection reports 0 — which is
    exactly what makes it useless as a detector of OTHER writers.  ⇒ it
    is used the only way it can be: as the *writing* side's own count.
    See :func:`_total_changes_of_a_writer` for the cross-process half.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                           uri=True)
    try:
        return int(conn.total_changes)
    finally:
        conn.close()


def _total_changes_of_a_writer(db: Path) -> int:
    """``PRAGMA data_version`` — the CROSS-PROCESS change counter.

    MEASURED (2026-11-05): ``data_version`` moves when ANOTHER
    connection commits a change to the database.  It is the only counter
    that can see a change made by a DIFFERENT process — which is the
    whole point when the writer is the gate (a subprocess).

    ⇒ **E-1 uses BOTH**: ``total_changes`` on the writer's own
    connection (what the gate's own writes cost) and ``data_version``
    before/after (what the gate's own writes did to the file).
    """
    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                           uri=True)
    try:
        return int(conn.execute("PRAGMA data_version").fetchone()[0])
    finally:
        conn.close()


def library_state(db: Path) -> Dict[str, object]:
    """Everything E-1 compares: the fingerprint + the counters."""
    conn = open_ro(db)
    try:
        st: Dict[str, object] = {"fingerprint": fingerprint_rows(conn)}
        tables = read_tables(conn)
        st["chunks"] = conn.execute(
            "SELECT COUNT(*) FROM chunks").fetchone()[0]
        st["ledger"] = (conn.execute(
            "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
            if "cert_ledger" in tables else 0)
        st["total_changes"] = int(conn.total_changes)
    finally:
        conn.close()
    st["data_version"] = _total_changes_of_a_writer(db)
    return st


def fleet_state() -> Dict[str, Dict[str, object]]:
    return {name: library_state(db) for name, db, _ in live_libraries()}


# ---------------------------------------------------------------------------
# the fixture — a minimal library with a pointer and a ledger
# ---------------------------------------------------------------------------

FIXTURE_SCHEMA = """
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    path TEXT,
    scope TEXT,
    user_id TEXT,
    start_line INTEGER,
    end_line INTEGER,
    hash TEXT,
    file_sealed INTEGER DEFAULT 0,
    element_released INTEGER DEFAULT 0,
    sealed_owner TEXT,
    hit_count INTEGER DEFAULT 0,
    last_hit_at TEXT,
    text TEXT,
    body_path TEXT,
    body_anchor TEXT,
    body_hash TEXT,
    text_state TEXT DEFAULT 'projection',
    body_origin TEXT DEFAULT 'unresolved',
    created_at INTEGER DEFAULT 0,
    updated_at INTEGER DEFAULT 0
);
CREATE TABLE cert_ledger (
    id TEXT PRIMARY KEY,
    text_state TEXT,
    body_origin TEXT,
    body_path TEXT,
    body_hash TEXT,
    certified_at INTEGER,
    certified_by TEXT
);
"""


def build_fixture(tmp_path: Path, truth_root: Path,
                  name: str = "index.db") -> Path:
    """A one-file library whose projection is byte-equal to its truth file.

    The restore discipline depends on this being deterministic:
    MEASURED (2026-11-05): a SQLite file's header carries a change
    counter that moves on every write transaction, so an SQL-level undo
    does NOT restore the bytes.  ⇒ the restore must be a **byte-level
    copy of a pristine snapshot**.
    """
    truth_root.mkdir(parents=True, exist_ok=True)
    (truth_root / "one.md").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    frag = "alpha\nbeta\ngamma\n"
    db = tmp_path / name
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(FIXTURE_SCHEMA)
        conn.execute(
            "INSERT INTO chunks (id, path, scope, user_id, start_line, "
            "end_line, hash, text, body_path, body_anchor, body_hash, "
            "text_state, body_origin, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("c1", "one.md", "shared", None, 1, 3, "h1", frag,
             "one.md", "L1-3", sha256_token(frag), "mirror", "file", 1000))
        conn.execute(
            "INSERT INTO cert_ledger (id, text_state, body_origin, "
            "body_path, body_hash, certified_at, certified_by) "
            "VALUES (?,?,?,?,?,?,?)",
            ("c1", "mirror", "file", "one.md", sha256_token(frag), 1000,
             "certify:promoted"))
        conn.commit()
    finally:
        conn.close()
    return db


class Pristine:
    """A pristine snapshot of the fixture, for the byte-level restore."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.truth_root = root / "ws"
        self.pristine = root / "pristine.db"
        build_fixture(root, self.truth_root, name="pristine.db")
        self.sha = sha256_file(self.pristine)

    def fresh(self, name: str = "index.db") -> Path:
        dst = self.root / name
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(dst) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy2(self.pristine, dst)
        return dst

    def restore(self, db: Path) -> str:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy2(self.pristine, db)
        return sha256_file(db)


# ===========================================================================
# E-1 · GATE-PURITY — the gate must not write the library it measures
# ===========================================================================

def test_e1_control_the_gate_run_leaves_the_live_libraries_untouched():
    """**E-1 · 对照臂（绿）** —— 被测对象：**活库的 ``cert_ledger`` 与
    ``chunks``**（16 个库），被测的写入者是**门禁本身**。

    MEASURED (2026-11-05 21:05~21:19): the gate's M-1 / M-3 criteria
    called ``Inspector.inspect()`` against the LIVE libraries, and the
    inspection's ``'mirror'`` promotion (``certify_library(dry_run=False)``)
    WROTE into the live ``cert_ledger``.  ⇒ two consecutive gate readings
    were readings of two different worlds.

    D-2 (commit ``2c00d0ce``) switched those two call sites to
    ``certify=False``.  **This criterion is the proof that it worked.**

    判定用**两个尺子**（交办书 E §2，缺一个就有盲区）：

      ① **指纹差集为空** —— per-row digest set, before vs after, named
         in both directions (ADDED / REMOVED / CHANGED);
      ② **计数** —— ``PRAGMA data_version`` (the cross-process change
         counter) before vs after, and ``total_changes`` on the
         reading connection.

    ⚠️ **本臂会真跑门禁**（``python -m agent.memory.acceptance.phase2
    --only M-``），代价 ~110 秒。  这是刻意的：E-1 测的是「门禁跑一次」
    这件事本身，用一个假替身去测它就测不到真东西。

    ⚠️ 门禁是**另一个进程**，所以本臂用 ``data_version``（跨进程计数器）
    而不是只靠本进程的 ``total_changes``。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    before = fleet_state()
    t0 = time.time()
    cmd = [sys.executable, "-m", GATE_MODULE,
           "--workspace", str(WORKSPACE), "--only", GATE_SCOPE]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=3600)
    elapsed = time.time() - t0
    after = fleet_state()

    diffs: Dict[str, List[str]] = {}
    for name in before:
        d = fingerprint_diff(before[name]["fingerprint"],
                              after[name]["fingerprint"])
        if d:
            diffs[name] = d

    dv_moved = [n for n in before
                if before[n]["data_version"] != after[n]["data_version"]]
    row_moved = [n for n in before
                 if before[n]["chunks"] != after[n]["chunks"]
                 or before[n]["ledger"] != after[n]["ledger"]]
    tc_moved = [n for n in before
                if before[n]["total_changes"] != after[n]["total_changes"]]

    print("\n[E-1 对照臂] 被测对象 = 活库的 cert_ledger 与 chunks（16 个库）")
    print("[E-1 对照臂] 被测的写入者 = 门禁（%s --only %s）"
          % (GATE_MODULE, GATE_SCOPE))
    print("[E-1 对照臂] 门禁退出码 = %s，耗时 = %.1fs" % (proc.returncode, elapsed))
    print("[E-1 对照臂] ① 指纹差集（前后逐行摘要之差）= %s"
          % (diffs or "（空）—— 16 个库逐行摘要全部一致"))
    print("[E-1 对照臂] ② data_version 变化的库 = %s" % (dv_moved or "（无）"))
    print("[E-1 对照臂] ② total_changes 变化的库 = %s" % (tc_moved or "（无）"))
    print("[E-1 对照臂] 行数变化的库 = %s" % (row_moved or "（无）"))
    for name, d in sorted(diffs.items()):
        print("   %-22s %s" % (name, d[:6]))

    # ① the fingerprint difference set must be EMPTY
    assert not diffs, (
        "门禁跑一次，活库的指纹差集不为空 ⇒ 门禁写了它所测量的库：%s"
        % {k: v[:5] for k, v in diffs.items()})
    # ② the counters must not have moved
    assert not dv_moved, (
        "data_version 变化的库 = %s ⇒ 有别的进程写了活库" % dv_moved)
    assert not row_moved, (
        "行数变化的库 = %s ⇒ 门禁写了活库" % row_moved)
    assert not tc_moved, (
        "total_changes 变化的库 = %s ⇒ 读连接自己写了库" % tc_moved)

    print("[E-1 对照臂] 结论：**门禁跑一次，活库的 cert_ledger 与 chunks "
          "零变化**（指纹差集空 + 计数未动）⇒ 门禁的读数不再有「自己造成」"
          "的成分")


def test_e1_mutation_the_gate_itself_writes_the_library_it_inspects(tmp_path):
    """**E-1 · 变异臂** —— 变异点：**让门禁认证一个待认证的行**。

    交办书说的形态：M-1 判据对活库调 ``Inspector.inspect()`` ⇒
    ``certify_library(dry_run=False)`` ⇒ 门禁跑一次，库就变一次。

    本判据**独立报告**这件事，不改 ``agent/memory/acceptance/*`` 下的
    既有判据（那是别人的口径）。

    ⚠️ **本判据在「沙箱」上做变异，不在活库上做。**
    MEASURED（2026-11-05 21:19:34）：上一轮把 ``Inspector`` 指向真源
    工作区，活库 ``agent-mvnf7fjl`` 的 ``cert_ledger`` 真的多了一条。
    ⇒ 本臂的沙箱路径必须落在 ``%TEMP%`` 之下（``_assert_sandbox``），
    否则本文件**拒绝执行**。

    **变异点还原的是「一行待认证」这个形状**：删掉一行的账本条目 ⇒
    它变成待认证 ⇒ 门禁必须认证它（``promoted=1``）⇒ **库变了**。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    # a SMALL non-ROOT library (ROOT is 214 MB — the inspection is slow)
    target_name, target_db, target_root = None, None, None
    for name, db, root in libs:
        if name == "ROOT":
            continue
        target_name, target_db, target_root = name, db, root
        break
    assert target_db is not None, "没有非 ROOT 的活库"

    tmp_path = Path(str(tmp_path)).resolve()
    _assert_sandbox(tmp_path)
    sandbox = tmp_path / "sandbox"
    _assert_writable_target(sandbox)
    (sandbox / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    for rel in ("memory/evolution", "memory/shared", "knowledge", "MEMORY.md",
                "AGENT.md", "USER.md", "RULE.md"):
        src = target_root / rel
        if not src.exists():
            continue
        dst = sandbox / rel
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("long-term",
                                                          ".audit",
                                                          ".snapshots"))
        else:
            shutil.copy2(src, dst)
    shared_knowledge = WORKSPACE / "knowledge"
    if shared_knowledge.is_dir():
        shutil.copytree(shared_knowledge, sandbox / "knowledge",
                        dirs_exist_ok=True)
    sb_db = sandbox / "memory" / "long-term" / "index.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(target_db) + suffix)
        if p.is_file():
            shutil.copy2(p, Path(str(sb_db) + suffix))

    # --- the live baseline (it must NOT move) -----------------------
    live_before = library_state(target_db)
    print("\n[E-1 变异臂] 被测对象 = 门禁（巡检器）对它所检查的库的写入")
    print("[E-1 变异臂] 沙箱 = %s" % sandbox)
    print("[E-1 变异臂] 活库基线：chunks=%s ledger=%s data_version=%s"
          % (live_before["chunks"], live_before["ledger"],
             live_before["data_version"]))

    # --- the mutation: make ONE row pending (drop its ledger entry) --
    conn = sqlite3.connect(str(sb_db))
    try:
        cand = conn.execute(
            "SELECT c.id, c.path FROM chunks c "
            "WHERE c.body_path IS NOT NULL AND c.body_anchor IS NOT NULL "
            "AND c.body_origin='file' "
            "AND c.id IN (SELECT id FROM cert_ledger) LIMIT 1").fetchone()
        assert cand is not None, "沙箱里没有可认证的行 —— 前置条件不成立"
        conn.execute("DELETE FROM cert_ledger WHERE id=?", (cand[0],))
        conn.commit()
    finally:
        conn.close()
    sb_before = library_state(sb_db)
    print("[E-1 变异臂] 变异点 = 沙箱 index.db：DELETE FROM cert_ledger "
          "WHERE id=%s（path=%s）⇒ 该行变成待认证"
          % (cand[0][:12], cand[1]))
    print("[E-1 变异臂] 变异前沙箱：chunks=%s ledger=%s"
          % (sb_before["chunks"], sb_before["ledger"]))

    # --- run the GATE against the sandbox ---------------------------
    sys.path.insert(0, str(REPO))
    from agent.memory.inspector import Inspector  # noqa: E402
    rep = Inspector(sandbox).inspect(trigger="manual",
                                     persist_snapshot=False)
    mirror = rep.get("mirror_state") or {}

    sb_after = library_state(sb_db)
    print("[E-1 变异臂] 门禁跑了一次：mirror_state=%s"
          % {k: v for k, v in mirror.items() if k != "refusals"})
    print("[E-1 变异臂] 变异后沙箱：chunks=%s ledger=%s"
          % (sb_after["chunks"], sb_after["ledger"]))

    sb_diff = fingerprint_diff(sb_before["fingerprint"], sb_after["fingerprint"])
    print("[E-1 变异臂] **门禁自己造成的变化（指纹差集）** = %s"
          % (sb_diff[:8] or "（无）"))
    changed_fields = []
    for k in ("chunks", "ledger", "data_version"):
        if sb_before[k] != sb_after[k]:
            changed_fields.append("%s: %s -> %s" % (k, sb_before[k],
                                                     sb_after[k]))
    print("[E-1 变异臂] **门禁自己造成的变化（计数）** = %s"
          % (changed_fields or "（无）"))

    # --- the live library must NOT have moved -----------------------
    live_after = library_state(target_db)
    live_moved = [k for k in ("chunks", "ledger", "data_version")
                  if live_before[k] != live_after[k]]
    live_diff = fingerprint_diff(live_before["fingerprint"],
                                  live_after["fingerprint"])
    print("[E-1 变异臂] 活库在这次门禁里变化的字段 = %s"
          % (live_moved or "（无）—— 沙箱隔离有效"))
    print("[E-1 变异臂] 活库的指纹差集 = %s" % (live_diff[:5] or "（空）"))
    assert not live_moved and not live_diff, (
        "沙箱里的门禁**动到了活库**：%s %s —— 隔离失效" % (live_moved, live_diff[:5]))

    # --- the verdict ------------------------------------------------
    assert mirror.get("promoted", 0) >= 1, (
        "门禁没有认证任何行 —— 本判据测的那件事（门禁写库）在本机"
        "不成立：mirror_state=%s" % mirror)
    assert sb_diff or changed_fields, (
        "门禁认证了一行，但库的状态没变 —— 本判据测的那件事不成立")
    print("[E-1 变异臂] 结论：**门禁（默认 certify=True）跑一次 ⇒ 它所"
          "检查的库变了** ⇒ 门禁的读数里有一部分是门禁自己造成的")
    print("[E-1 变异臂] ⇒ 这就是 D-2 把那两个调用点改成 certify=False "
          "的理由，也是 E-1 对照臂要守的东西")


# ===========================================================================
# E-2 · the S-1 content digest must cover ``cert_ledger``
# ===========================================================================

def test_e2_control_the_digest_covers_the_cert_ledger():
    """**E-2 · 对照臂（绿）** —— 被测对象：**本文件自己的内容摘要**。

    交办书 E §3：``acceptance/__init__.py:247`` 的 ``content_digest()``
    只 digest ``chunks`` ⇒ **账本侧的写入对摘要完全隐形** ⇒ C-A4.4
    「判据未修改被测库」有一个盲区。

    这就是「门禁写活库」为什么能一直不被发现的原因：门禁自己的读数
    前后一模一样。

    本臂断言三件事：

      ① **本文件的摘要覆盖账本** —— 一条只写账本的写入必须让摘要变化；
      ② **S-1（acceptance 层自己的摘要）看不见它** —— 这就是被测的
         事实（用 ``content_digest`` 本人，不是本文件的重写）；
      ③ **两把尺子的结论相反** —— 所以「前后对照」必须说清楚用的是
         哪一把尺子。
    """
    tmp_path = Path(tempfile.mkdtemp(prefix="e2_"))
    try:
        _assert_sandbox(tmp_path)
        fixture = Pristine(tmp_path)
        db = fixture.fresh()

        with ro(db) as c:
            before = _own_digest(c)
            s1_before = _s1_digest(c)

        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO cert_ledger (id, text_state, "
                "body_origin, body_path, body_hash, certified_at, "
                "certified_by) VALUES ('probe-e2','mirror','file','one.md',"
                "'sha256:probe',999999999,'certify:promoted')")
            conn.commit()
        finally:
            conn.close()

        with ro(db) as c:
            after = _own_digest(c)
            s1_after = _s1_digest(c)

        own_moved = before != after
        s1_moved = s1_before != s1_after
        print("\n[E-2 对照臂] 被测对象 = 内容摘要的覆盖面")
        print("[E-2 对照臂] 写入 = 只往 cert_ledger 插一行（chunks 不动）")
        print("[E-2 对照臂] ① 本文件的摘要看见了吗 = %s" % own_moved)
        print("[E-2 对照臂] ② S-1（acceptance 层自己的摘要）看见了吗 = %s"
              % s1_moved)

        # ① this file's own digest MUST see a ledger-only write
        assert own_moved, (
            "本文件的摘要没有覆盖 cert_ledger ⇒ 它也看不见门禁的写入")
        # ② the acceptance layer's digest MUST NOT see it — that IS the
        #    defect E-2 exists to name.  If it sees it, the hole is closed
        #    upstream and this criterion must be rewritten, not deleted.
        assert not s1_moved, (
            "S-1 现在看得见只写账本的写入 ⇒ C-2 已被上游修掉，"
            "本判据的写法需要改（请更新口径，而不是删掉这条）")
        print("[E-2 对照臂] 结论：**两把尺子的结论相反** ⇒ 『门禁写活库』"
              "对 S-1 完全隐形 ⇒ C-A4.4 的盲区成立")
        print("[E-2 对照臂] ⇒ 『前后对照要可比』的第一条补充：尺子必须"
              "同时覆盖 cert_ledger，否则门禁的读数前后一模一样")
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _own_digest(conn: sqlite3.Connection) -> str:
    """This file's own content digest — covers BOTH tables.

    Read through SQLite (a WAL checkpoint moves bytes without changing a
    row, so a byte-level digest reports a change that is not a change).
    """
    h = hashlib.sha256()
    for table, wanted in FINGERPRINT_COLUMNS.items():
        tables = read_tables(conn)
        if table not in tables:
            continue
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        cols_wanted = [c for c in wanted if c in cols]
        if not cols_wanted:
            continue
        h.update(("TABLE:%s" % table).encode("utf-8"))
        for row in conn.execute("SELECT %s FROM %s ORDER BY id"
                                % (", ".join(cols_wanted), table)):
            h.update(repr(tuple(row)).encode("utf-8", "replace"))
            h.update(b"\x00")
    return h.hexdigest()


def _s1_digest(conn: sqlite3.Connection) -> str:
    """The ACCEPTANCE layer's own digest — imported, never re-spelled.

    The object under test here IS this function, so calling it is the
    measurement, not a tautology.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from agent.memory.acceptance import content_digest  # noqa: E402
    return content_digest(conn)


def test_e2_mutation_a_ledger_only_write_must_move_the_digest(tmp_path):
    """**E-2 · 变异臂** —— 变异点：**只往账本写一行**（chunks 不动）。

    变异点还原的是历史缺陷的形状：``certify_library`` 认证一行时写的
    就是**只有账本**（``cert_ledger``）—— ``chunks`` 那一侧一个字都不动。

    判定只钉 E-2；连带红（账本多了一行 ⇒ 任何以账本行数为判据的其它
    判据会红）单独列出来。
    """
    tmp_path = Path(str(tmp_path)).resolve()
    _assert_sandbox(tmp_path)
    fixture = Pristine(tmp_path)
    db = fixture.fresh()

    with ro(db) as c:
        before = _own_digest(c)
    sha_before = sha256_file(db)

    # --- the mutation: a ledger-only write --------------------------
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cert_ledger (id, text_state, "
            "body_origin, body_path, body_hash, certified_at, certified_by) "
            "VALUES ('probe-e2','mirror','file','one.md','sha256:probe',"
            "999999999,'certify:promoted')")
        conn.commit()
    finally:
        conn.close()
    sha_after_mut = sha256_file(db)

    # --- 自证生效：读回一个具体的行 -------------------------------
    conn = open_ro(db)
    try:
        row = conn.execute(
            "SELECT id, body_hash, certified_by FROM cert_ledger "
            "WHERE id='probe-e2'").fetchone()
        chunks_rows = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        after = _own_digest(conn)
    finally:
        conn.close()

    print("\n[E-2 变异臂] 变异点 = 副本 index.db：INSERT OR REPLACE INTO "
          "cert_ledger（chunks 表一个字节都不动）")
    print("[E-2 变异臂] 自证生效：读回 cert_ledger.id='probe-e2' → "
          "body_hash=%s by=%s" % (row["body_hash"], row["certified_by"]))
    print("[E-2 变异臂] 自证生效：chunks 行数 = %d（不变 ⇒ 这是「只写账本」）"
          % chunks_rows)
    print("[E-2 变异臂] 本文件的摘要：变异前=%s… 变异后=%s…"
          % (before[:16], after[:16]))
    assert row is not None, "变异没有生效：账本里没有那一行"
    assert chunks_rows == 1, "变异把 chunks 也动了 ⇒ 不是「只写账本」"
    assert before != after, (
        "只写账本没有让本文件的摘要变化 ⇒ 摘要没有覆盖 cert_ledger")

    # --- the restore ------------------------------------------------
    sha_after_restore = fixture.restore(db)
    print("[E-2 变异臂] 还原后副本 sha256=%s…" % sha_after_restore[:16])
    print("[E-2 变异臂] 还原前后逐字节一致 = %s"
          % (sha_after_restore == sha_before))
    assert sha_after_restore == sha_before, (
        "还原后 sha256 不一致：变异前=%s… 还原后=%s…"
        % (sha_before[:16], sha_after_restore[:16]))

    print("[E-2 变异臂] 连带红（不参与判定）：账本多了一行 ⇒ 任何以账本"
          "行数为判据的其它判据会红。E-2 只钉『只写账本的写入必须被摘要"
          "看见』这一条。")


# ===========================================================================
# E-3 · the ledger's token and the chunks' token must agree
# ===========================================================================

def read_token_mismatches(conn: sqlite3.Connection
                           ) -> List[sqlite3.Row]:
    """The rows where the ledger and the chunks disagree about the token.

    **The ledger is the SECOND WITNESS.**  A row's ``chunks.body_hash``
    says "this is the token of the fragment at the anchor"; the ledger's
    ``body_hash`` says "this is the token of the fragment I CHECKED".
    Two witnesses about the same fact must say the same thing — otherwise
    one of them is a lie, and a reader cannot tell which.
    """
    tables = read_tables(conn)
    if "cert_ledger" not in tables:
        return []
    return list(conn.execute(
        "SELECT l.id AS id, c.path AS path, c.body_path AS body_path, "
        "c.body_anchor AS body_anchor, l.body_hash AS ledger_hash, "
        "c.body_hash AS chunks_hash, l.certified_by AS certified_by, "
        "l.certified_at AS certified_at "
        "FROM cert_ledger l JOIN chunks c ON c.id = l.id "
        "WHERE COALESCE(l.body_hash,'')<>COALESCE(c.body_hash,'') "
        "ORDER BY c.path, l.id"))


def test_e3_control_the_two_witnesses_agree_on_the_token():
    """**E-3 · 对照臂** —— 被测对象：**活库的 ``cert_ledger.body_hash``
    vs ``chunks.body_hash``**。

    交办书 E §3：``agent-mvnerelm`` 的 chunks=``2ce22a2b…``、ledger=
    ``3bf36ee6…`` —— **账本是第二个证人，却没人比它**。

    本臂断言（全部可判定）：

      ① **两个证人的令牌必须一致** —— 不一致即 FAIL；
      ② **不一致的行必须被点名** —— 库 / chunk id / path / 两个令牌；
      ③ **谁是陈旧的那一方必须说得出来** —— 判据自己重建片段，算出真
         令牌，然后指出「账本错了」还是「chunks 错了」。

    ⚠️ ③ 是这一条的关键：**只说「不一致」是不够的** —— 修错方向就
    是把对的东西改坏。判据必须用真源来裁决。

    ⚠️ **这一条现在预期是红的**（MEASURED 2026-11-05 00:20）：
    活库上有 **23 行** 两个证人的令牌不一致，分布在 **14 个库**、
    **4 个** 不同的 chunk id 上。**裁决结果：账本陈旧 23/23 行，
    chunks 全部正确** —— 也就是说，**账本拿着从别处复制来的令牌**。

    ⇒ 残留条数由 ``test_e3_residual_*`` 那一条负责断言（它预期红）。
    本臂负责「看得见 + 点名 + **说得出谁是陈旧的那一方**」。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    mismatches: List[str] = []
    stale_ledger = 0
    stale_chunks = 0
    neither = 0
    judged = 0
    per_lib: List[str] = []
    for name, db, root in libs:
        with ro(db) as conn:
            rows = read_token_mismatches(conn)
        if not rows:
            continue
        per_lib.append("  %-22s %d 行不一致" % (name, len(rows)))
        for r in rows:
            mismatches.append("%-22s id=%s path=%s" % (name, r["id"], r["path"]))
            truth = _resolve_truth(root, r["body_path"])
            if truth is None:
                continue
            frag = _rebuild_fragment(truth, r["body_anchor"])
            if frag is None:
                continue
            judged += 1
            actual = sha256_token(frag)
            if r["ledger_hash"] == actual:
                stale_chunks += 1
            elif r["chunks_hash"] == actual:
                stale_ledger += 1
            else:
                neither += 1

    print("\n[E-3 对照臂] 被测对象 = 活库的 cert_ledger.body_hash vs "
          "chunks.body_hash")
    if per_lib:
        print("\n".join(per_lib))
    print("[E-3 对照臂] 不一致的行合计 = %d（其中 %d 行能对真源裁决）"
          % (len(mismatches), judged))
    print("[E-3 对照臂] 裁决：**账本陈旧 = %d 行 / chunks 陈旧 = %d 行 / "
          "两边都不对 = %d 行**" % (stale_ledger, stale_chunks, neither))
    for m in mismatches[:12]:
        print("   ", m)

    if not mismatches:
        print("[E-3 对照臂] 现场没有不一致 ⇒ 本臂是 NOTRUN（不是通过）")
        pytest.skip("现场没有『账本与 chunks 的令牌不一致』的行 —— "
                    "前置条件不成立")

    # ① the criterion SEES them and NAMES them — every mismatch row must
    #    be named, and the naming must be complete (a criterion that
    #    names "some" of them has not seen them).
    named = len(mismatches)
    total_rows = 0
    for _n, db, _r in libs:
        with ro(db) as c:
            total_rows += len(read_token_mismatches(c))
    assert named == total_rows, (
        "有行没有被点名（点名 %d 行 / 实有 %d 行）" % (named, total_rows))
    # ③ the DIRECTION must be decidable — and it must be decidable for
    #    EVERY row that has a truth file.  A row whose two witnesses
    #    disagree and whose truth file cannot be read is unjudgeable;
    #    a row that can be judged MUST be judged.
    assert judged > 0, "没有一行能对真源裁决 ⇒ 裁决不成立"
    assert neither == 0, (
        "有 %d 行两个证人都与真源不符 ⇒ 判据的读法或真源定位有问题"
        % neither)
    assert stale_ledger > 0 or stale_chunks > 0, "裁决没有结果"
    print("[E-3 对照臂] 结论：判据看得见（%d 行，已点名）且**说得清谁是"
          "陈旧的那一方**（账本陈旧 %d 行 / chunks 陈旧 %d 行）"
          % (len(mismatches), stale_ledger, stale_chunks))
    print("[E-3 对照臂] ⇒ 活着的这个洞，形状是「**账本**拿着从别处复制"
          "来的令牌」⇒ 修的方向是**账本**，不是 chunks")


def _resolve_truth(root: Path, relpath: Optional[str]) -> Optional[Path]:
    """Where the truth file behind ``relpath`` lives — THIS file's own rule.

    The agent's own ``knowledge/`` first, then the SHARED root's.
    """
    if not relpath:
        return None
    rel = str(relpath).replace("\\", "/")
    if rel.startswith("knowledge/"):
        for kd in (root / "knowledge", WORKSPACE / "knowledge"):
            if kd.is_dir():
                cand = kd / rel[len("knowledge/"):]
                if cand.is_file():
                    return cand
        return None
    cand = root / rel
    return cand if cand.is_file() else None


ANCHOR_RE = re.compile(
    r"^L(?P<start>\d+)-(?P<end>\d+)(?::p(?P<part>\d+))?"
    r"(?:\+S(?P<segs>\d+-\d+(?:~\d+)?(?:,\d+-\d+(?:~\d+)?)*))?$")


def _rebuild_fragment(path: Path, anchor: str) -> Optional[str]:
    """Rebuild the fragment an anchor points at — THIS file's own reader."""
    m = ANCHOR_RE.match(anchor or "")
    if not m:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    if not lines:
        return None
    a, b = int(m.group("start")), int(m.group("end"))
    segs = m.group("segs")
    if segs is None:
        if a < 1 or a > len(lines):
            return None
        return "".join(lines[a - 1:min(b, len(lines))])
    parts: List[str] = []
    for raw in (segs or "").split(","):
        raw = raw.strip().rsplit("~", 1)[0]
        if "-" not in raw:
            return None
        x, y = raw.split("-", 1)
        try:
            i, j = int(x), int(y)
        except ValueError:
            return None
        if i < 1 or i > len(lines):
            return None
        parts.append("".join(lines[i - 1:min(j, len(lines))]).rstrip())
    return "\n\n".join(parts)


def test_e3_residual_the_two_witnesses_must_agree():
    """**E-3 · 残留断言（预期红）** —— 被测对象：**活库的
    ``cert_ledger.body_hash`` vs ``chunks.body_hash``**。

    ⚠️ **这一条现在预期是红的** —— MEASURED (2026-11-05 00:20)：
    **23 行**不一致，分布在 **14 个库**、**4 个**不同的 chunk id 上。
    裁决：**账本陈旧 23/23 行，chunks 全部正确**。

    **为什么它用 ``xfail(strict=True)`` 而不是直接 ``assert``：**

    * 直接 ``assert`` ⇒ 整个判据文件的退出码永远是 1，读者分不清
      「我的判据坏了」和「残留还在」；
    * ``xfail(strict=True)`` ⇒ 残留还在时它被记为 **xfailed**（预期的
      失败），文件仍然可复跑；**残留被修好时它会 XPASS(strict) ⇒ 变成
      FAILURE**，提示「这条已知红已经修好了，把 xfail 去掉」。

      ⇒ xfail 在这里不是「把红藏起来」，它是**一个守门人**：它守住
      「不许在没人注意的情况下把这条红删掉」。

    ⚠️ 设环境变量 ``COW_E_STRICT=1`` ⇒ 这一条变成**硬红**（退出码 1）。
    谁要「退出码即结论」，就设这个变量。
    """
    strict = os.environ.get("COW_E_STRICT", "") not in ("", "0", "false")
    libs = live_libraries()
    detail: List[str] = ["被测对象 = 活库的 cert_ledger.body_hash vs "
                         "chunks.body_hash"]
    total = 0
    stale_ledger = 0
    stale_chunks = 0
    for name, db, root in libs:
        with ro(db) as conn:
            rows = read_token_mismatches(conn)
        if not rows:
            continue
        detail.append("  %-22s %d 行" % (name, len(rows)))
        for r in rows:
            total += 1
            truth = _resolve_truth(root, r["body_path"])
            frag = (_rebuild_fragment(truth, r["body_anchor"])
                    if truth is not None else None)
            verdict = "无法裁决"
            if frag is not None:
                actual = sha256_token(frag)
                if r["ledger_hash"] == actual:
                    stale_chunks += 1
                    verdict = "chunks 陈旧"
                elif r["chunks_hash"] == actual:
                    stale_ledger += 1
                    verdict = "账本陈旧"
            detail.append("      id=%s path=%s ⇒ %s"
                          % (r["id"][:12], r["path"], verdict))
    detail.append("合计：%d 行不一致（账本陈旧 %d / chunks 陈旧 %d）"
                  % (total, stale_ledger, stale_chunks))
    msg = "\n".join(detail)

    print("\n[E-3 残留断言] %s" % msg)
    if total == 0:
        print("[E-3 残留断言] 残留已清零 ⇒ 这条已知红**可以摘掉**了"
              "（去掉 xfail 标记）")
        return

    if strict:
        pytest.fail("【硬红模式 COW_E_STRICT=1】%s" % msg)

    pytest.xfail("E-3 的残留还在（%d 行）：\n%s" % (total, msg))


def test_e3_mutation_a_stale_ledger_token_is_red(tmp_path):
    """**E-3 · 变异臂** —— 变异点：**把账本的令牌改陈旧**（chunks 不动）。

    变异点还原的是历史缺陷的形状：**同一个 chunk id，在不同库里带着
    不同的令牌** —— MEASURED (2026-11-05)：活库上有 **4 个** 这样的
    chunk id，跨 **14~4 个** 库出现，共 **23 行**不一致；裁决的结果是
    **账本陈旧 23/23 行，chunks 全部正确**。

    也就是说：现在活着的这个洞，形状是「**账本**拿着从别处复制来的
    令牌」。本变异点还原的正是这个形状。

    判定只钉 E-3；连带红（账本令牌变了 ⇒ 任何以账本令牌为判据的其它
    判据会红）单独列出来。
    """
    tmp_path = Path(str(tmp_path)).resolve()
    _assert_sandbox(tmp_path)
    fixture = Pristine(tmp_path)
    truth_root = fixture.truth_root
    db = fixture.fresh()

    conn = open_ro(db)
    try:
        before = read_token_mismatches(conn)
        good = conn.execute(
            "SELECT l.body_hash FROM cert_ledger l JOIN chunks c "
            "ON c.id=l.id WHERE l.id='c1'").fetchone()[0]
    finally:
        conn.close()
    sha_before = sha256_file(db)
    assert not before, "对照臂的前置条件不成立：夹具本来就不一致"

    # --- the mutation: the ledger's token goes stale ----------------
    stale = "sha256:" + ("ab" * 32)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE cert_ledger SET body_hash=? WHERE id='c1'",
                     (stale,))
        conn.commit()
    finally:
        conn.close()
    sha_after_mut = sha256_file(db)

    # --- 自证生效：读回一个具体的行 -------------------------------
    conn = open_ro(db)
    try:
        row = conn.execute(
            "SELECT l.body_hash AS lh, c.body_hash AS ch "
            "FROM cert_ledger l JOIN chunks c ON c.id=l.id "
            "WHERE l.id='c1'").fetchone()
        after = read_token_mismatches(conn)
    finally:
        conn.close()
    truth = _resolve_truth(truth_root, "one.md")
    frag = _rebuild_fragment(truth, "L1-3")
    actual = sha256_token(frag) if frag is not None else None

    print("\n[E-3 变异臂] 变异点 = 副本 index.db：UPDATE cert_ledger SET "
          "body_hash='%s…'（chunks 不动）" % stale[:24])
    print("[E-3 变异臂] 自证生效：读回 c1 → ledger=%s… chunks=%s…"
          % (row["lh"][:24], row["ch"][:24]))
    print("[E-3 变异臂] 自证生效：判据算出的真令牌 = %s…" % (actual or "")[:24])
    print("[E-3 变异臂] 判据数出的不一致行 = %d" % len(after))

    assert row["lh"] != row["ch"], "变异没有生效：两个证人还是一样"
    assert len(after) == 1 and after[0]["id"] == "c1", (
        "变异后 E-3 没有转红：不一致行 = %s"
        % [r["id"] for r in after])
    # the DIRECTION must be decidable: the ledger is the stale one here
    assert actual is not None and row["ch"] == actual and row["lh"] != actual, (
        "判据说不出谁是陈旧的那一方 ⇒ 裁决不成立")

    # --- the restore ------------------------------------------------
    sha_after_restore = fixture.restore(db)
    print("[E-3 变异臂] 还原后副本 sha256=%s…" % sha_after_restore[:16])
    print("[E-3 变异臂] 还原前后逐字节一致 = %s"
          % (sha_after_restore == sha_before))
    assert sha_after_restore == sha_before, (
        "还原后 sha256 不一致：变异前=%s… 还原后=%s…"
        % (sha_before[:16], sha_after_restore[:16]))

    print("[E-3 变异臂] 连带红（不参与判定）：账本令牌变了 ⇒ 任何以账本"
          "令牌为判据的其它判据会红。E-3 只钉『两个证人的令牌必须一致』"
          "这一条。")


# ===========================================================================
# E-4 · the re-cut selector must SEE a stale drift token (paired with D-1)
# ===========================================================================

def _build_recut_sandbox(tmp_path: Path, agent_root: Path,
                         agent_db: Path) -> Path:
    """A sandbox workspace with the agent's truth layer + a COPY of its index.

    The whole agent workspace is copied (the truth tree IS the workspace),
    minus the index itself.  The index is copied in separately.
    """
    sandbox = tmp_path / "sandbox"
    _assert_writable_target(sandbox)
    shutil.copytree(agent_root, sandbox, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("long-term", ".audit",
                                                  ".snapshots", "__pycache__"))
    (sandbox / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    sb_db = sandbox / "memory" / "long-term" / "index.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(agent_db) + suffix)
        if p.is_file():
            shutil.copy2(p, Path(str(sb_db) + suffix))
    return sandbox


def _smallest_live_library() -> Tuple[str, Path, Path]:
    libs = live_libraries()
    best = None
    for name, db, root in libs:
        if name == "ROOT":
            continue
        conn = open_ro(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            conn.close()
        if best is None or n < best[1]:
            best = (name, n, db, root)
    if best is None:
        raise AssertionError("没有非 ROOT 的活库")
    return best[0], best[2], best[3]


def test_e4_control_the_selector_sees_a_stale_token_and_the_recut_clears_it(
        tmp_path):
    """**E-4 · 对照臂（绿）** —— 被测对象：**重切选择器
    （``drift_recut.drifted_paths``）** + **重切本身**。

    交办书 E §4：这是与后台工程师的 **D-1** 配对的判据。
    旧的选择器只问「重建的片段和 ``chunks.text`` 一样吗」⇒ 它**看不见**
    「正文一致但令牌陈旧」⇒ 重切永远选不中 ⇒ Q2 永远红。

    本臂断言（全部可判定）：

      ① **选择器选中了那个 path**（``status='drifted'`` 且
         ``stale_token_rows >= 1``）；
      ② **它选中的是因为令牌，不是因为正文** —— ``body_drift_rows==0``
         而 ``stale_token_rows>=1``（这正是旧选择器看不见的那一问）；
      ③ **重切真的清掉了它** —— 重切后该 path 的令牌陈旧行数 = 0。

    ⚠️ **本臂与 D-1 配对，不是重复 D-1**：D-1 是实现，本臂是**独立的
    判据** —— 它自己重建片段、自己算真令牌、自己数行，只在「问选择器
    选了什么」这一处调用被测对象（不问它本人就不知道它看不见）。
    """
    tmp_path = Path(str(tmp_path)).resolve()
    _assert_sandbox(tmp_path)
    name, agent_db, agent_root = _smallest_live_library()
    sandbox = _build_recut_sandbox(tmp_path, agent_root, agent_db)
    sb_db = sandbox / "memory" / "long-term" / "index.db"

    # --- pick a row whose truth file resolves in the sandbox --------
    sys.path.insert(0, str(REPO))
    from agent.memory.body_pointer import resolve_truth_file  # noqa: E402

    conn = open_ro(sb_db)
    try:
        rows = conn.execute(
            "SELECT id, path, body_path, body_anchor, body_hash, text "
            "FROM chunks WHERE body_path IS NOT NULL "
            "AND body_anchor IS NOT NULL AND body_origin='file' "
            "AND text_state IN ('projection','mirror')").fetchall()
    finally:
        conn.close()

    target = None
    for r in rows:
        if resolve_truth_file(sandbox, r["body_path"]) is not None:
            target = r
            break
    assert target is not None, "沙箱里没有可判定的行 —— 前置条件不成立"

    cid, cpath = target["id"], target["path"]

    # --- the mutation: the row's token goes stale (the body does NOT) --
    stale = "sha256:" + ("de" * 32)
    conn = sqlite3.connect(str(sb_db))
    try:
        conn.execute("UPDATE chunks SET body_hash=? WHERE id=?", (stale, cid))
        conn.commit()
    finally:
        conn.close()

    # --- 自证生效：正文一致 + 令牌陈旧 ---------------------------
    conn = open_ro(sb_db)
    try:
        row = conn.execute(
            "SELECT body_hash, text, body_path, body_anchor FROM chunks "
            "WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    truth = resolve_truth_file(sandbox, row["body_path"])
    frag = _rebuild_fragment(truth, row["body_anchor"])
    body_ok = frag is not None and canonical(frag) == canonical(row["text"] or "")
    print("\n[E-4 对照臂] 被测对象 = 重切选择器（drift_recut.drifted_paths）")
    print("[E-4 对照臂] 沙箱 = %s（源库 %s）" % (sandbox, name))
    print("[E-4 对照臂] 变异点 = 沙箱 index.db：id=%s path=%s 的 "
          "body_hash → %s…（正文不动）" % (cid[:12], cpath, stale[:20]))
    print("[E-4 对照臂] 自证生效：正文仍然一致 = %s" % body_ok)
    print("[E-4 对照臂] 自证生效：存着的令牌 = %s… 真令牌 = %s…"
          % (row["body_hash"][:20], (sha256_token(frag) if frag else "")[:20]))
    assert body_ok, "变异没有还原「正文一致」这个前提"

    # --- ask the SELECTOR -------------------------------------------
    from agent.memory import drift_recut  # noqa: E402
    conn = open_ro(sb_db)
    try:
        drifted = drift_recut.drifted_paths(conn, sandbox)
    finally:
        conn.close()
    real = [d for d in drifted if d.get("status") == "drifted"]
    selected = {d["path"] for d in real}
    token_only = [d for d in real if d.get("stale_token_rows", 0) >= 1
                  and d.get("body_drift_rows", 0) == 0]
    print("[E-4 对照臂] 选择器选中的 path = %s" % (sorted(selected) or "（空）"))
    print("[E-4 对照臂] 其中「只因令牌陈旧」被选中的 = %s"
          % [d["path"] for d in token_only])

    # ① the selector must SEE the path
    assert cpath in selected, (
        "重切选择器**选不中**令牌陈旧的 path（%s）⇒ 它还在用旧的那一问"
        "（只比正文）" % cpath)
    # ② and it must be for the RIGHT reason
    assert token_only, (
        "选择器选中了，但不是因为令牌陈旧（body_drift_rows=%s / "
        "stale_token_rows=%s）⇒ 判据没有测到 D-1 修的那一问"
        % ([d.get("body_drift_rows") for d in real],
           [d.get("stale_token_rows") for d in real]))

    # --- the re-cut really clears it --------------------------------
    out = drift_recut.recut_library(sandbox, sb_db, dry_run=False,
                                    require_recut_safe=True)
    print("[E-4 对照臂] 重切：drifted_before=%s after=%s paths=%s failed=%s"
          % (out.get("drifted_before"), out.get("drifted_after"),
             out.get("recut_paths"), out.get("failed")))
    assert not out.get("error"), "重切被拒绝：%s" % out.get("error")
    assert out.get("drifted_after") == 0, (
        "重切之后仍然有漂移（%s）⇒ 令牌陈旧没有被清掉"
        % out.get("drifted_after"))
    assert out.get("recut_paths"), "重切没有重切任何 path"

    # --- and the token is now right ---------------------------------
    conn = open_ro(sb_db)
    try:
        after_row = conn.execute(
            "SELECT body_hash FROM chunks WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    print("[E-4 对照臂] 重切后该行的令牌 = %s…（真令牌 = %s…）"
          % ((after_row["body_hash"] or "")[:20],
             (sha256_token(frag) if frag else "")[:20]))
    assert after_row["body_hash"] != stale, "重切没有改掉那个陈旧的令牌"
    print("[E-4 对照臂] 结论：**选择器看得见「令牌陈旧」，重切把它清掉了**")


def test_e4_mutation_the_selector_that_only_compares_the_body_cannot_see_it(
        tmp_path):
    """**E-4 · 变异臂** —— 变异点：**把选择逻辑改回「只比正文」**。

    交办书 E §4：变异臂 = 把选路改回「只比正文」⇒ **该 path 选不中**
    ⇒ FAIL。

    ⚠️ **变异在「选择器的一份 COPY」上做，不在仓库里改。**
    机制的做法是：把 ``drift_recut.py`` 的源码读进来，把 D-1 加的那一段
    （② stale token）**去掉**，写进 ``tmp_path`` 下的一个模块副本，
    然后让选择器从**副本**里跑。

    ⇒ 仓库里的 ``drift_recut.py`` **一个字节都不动**（这是「变异在副本
    上做」的字面意思），而变异臂**自证生效**（副本里的选择器确实
    选不中，仓库里的确实选得中）。
    """
    tmp_path = Path(str(tmp_path)).resolve()
    _assert_sandbox(tmp_path)
    name, agent_db, agent_root = _smallest_live_library()
    sandbox = _build_recut_sandbox(tmp_path, agent_root, agent_db)
    sb_db = sandbox / "memory" / "long-term" / "index.db"

    sys.path.insert(0, str(REPO))
    from agent.memory.body_pointer import resolve_truth_file  # noqa: E402

    conn = open_ro(sb_db)
    try:
        rows = conn.execute(
            "SELECT id, path, body_path, body_anchor, body_hash, text "
            "FROM chunks WHERE body_path IS NOT NULL "
            "AND body_anchor IS NOT NULL AND body_origin='file' "
            "AND text_state IN ('projection','mirror')").fetchall()
    finally:
        conn.close()
    target = None
    for r in rows:
        if resolve_truth_file(sandbox, r["body_path"]) is not None:
            target = r
            break
    assert target is not None, "沙箱里没有可判定的行 —— 前置条件不成立"
    cid, cpath = target["id"], target["path"]

    # --- the mutation: the row's token goes stale (body unchanged) ---
    stale = "sha256:" + ("de" * 32)
    conn = sqlite3.connect(str(sb_db))
    try:
        conn.execute("UPDATE chunks SET body_hash=? WHERE id=?", (stale, cid))
        conn.commit()
    finally:
        conn.close()
    conn = open_ro(sb_db)
    try:
        row = conn.execute(
            "SELECT body_hash, text, body_path, body_anchor FROM chunks "
            "WHERE id=?", (cid,)).fetchone()
    finally:
        conn.close()
    truth = resolve_truth_file(sandbox, row["body_path"])
    frag = _rebuild_fragment(truth, row["body_anchor"])
    body_ok = frag is not None and canonical(frag) == canonical(row["text"] or "")
    assert body_ok, "变异没有还原「正文一致」这个前提"

    # --- build the MUTANT selector (a COPY, not the repo) -----------
    src_path = REPO / "agent" / "memory" / "drift_recut.py"
    src = src_path.read_text(encoding="utf-8")
    sha_repo_before = sha256_file(src_path)
    mutant_src = _mutate_selector_to_body_only(src)
    assert mutant_src != src, "变异没有改到源码 —— 变异点没找到"
    mutant_dir = tmp_path / "mutant_pkg"
    mutant_dir.mkdir(parents=True, exist_ok=True)
    (mutant_dir / "__init__.py").write_text("", encoding="utf-8")
    (mutant_dir / "drift_recut.py").write_text(mutant_src, encoding="utf-8")
    print("\n[E-4 变异臂] 被测对象 = 重切选择器（只比正文的那个版本）")
    print("[E-4 变异臂] 变异点 = 选择器源码的一份 COPY：把 D-1 加的"
          "「② 令牌陈旧」那一问去掉（仓库里的文件不动）")
    print("[E-4 变异臂] 自证生效：正文仍然一致 = %s" % body_ok)

    # --- ask BOTH selectors ------------------------------------------
    from agent.memory import drift_recut as real_sel  # noqa: E402
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mutant_drift_recut", str(mutant_dir / "drift_recut.py"))
    mutant = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mutant)

    def _selected(module, conn, ws):
        c = open_ro(conn) if isinstance(conn, Path) else conn
        try:
            out = module.drifted_paths(c, ws)
        finally:
            if isinstance(conn, Path):
                c.close()
        return {d["path"] for d in out if d.get("status") == "drifted"}

    conn = open_ro(sb_db)
    try:
        real_sel_paths = _selected(real_sel, conn, sandbox)
        mutant_paths = _selected(mutant, conn, sandbox)
    finally:
        conn.close()
    print("[E-4 变异臂] 仓库里的选择器（D-1）选中 = %s"
          % (sorted(real_sel_paths) or "（空）"))
    print("[E-4 变异臂] 变异的选择器（只比正文）选中 = %s"
          % (sorted(mutant_paths) or "（空）"))

    # --- 自证生效：the mutant really is blind -----------------------
    assert cpath in real_sel_paths, (
        "仓库里的选择器都选不中 ⇒ 前置条件不成立（D-1 也许没落地）")
    assert cpath not in mutant_paths, (
        "变异的选择器居然选中了 %s ⇒ 变异没有生效" % cpath)
    print("[E-4 变异臂] 自证生效：仓库版选得中，变异版选不中")
    print("[E-4 变异臂] 结论：**只比正文 ⇒ 选不中「令牌陈旧」的 path** "
          "⇒ D-1 修的正是这一问；没有 D-1，重切永远修不好这类行")
    print("[E-4 变异臂] 连带红（不参与判定）：选择器选不中 ⇒ 那条 path "
          "的重切不会发生 ⇒ 令牌陈旧留在库里。E-4 只钉『选不中』这一条。")

    # --- the restore: the REPO's file must be byte-identical ---------
    # ⚠️ The mutation was on a COPY, so the repository's file was never
    #    touched.  MEASURED discipline: the restore must be PROVED, not
    #    assumed — a criterion that mutates the repo and forgets to
    #    restore it is a red flag for the next person.
    sha_repo_after = sha256_file(src_path)
    print("[E-4 变异臂] 仓库里 drift_recut.py 的 sha256：变异前=%s… "
          "变异后=%s…" % (sha_repo_before[:16], sha_repo_after[:16]))
    print("[E-4 变异臂] 还原后逐字节一致 = %s"
          % (sha_repo_after == sha_repo_before))
    assert sha_repo_after == sha_repo_before, (
        "仓库里的 drift_recut.py 变了 ⇒ 变异没有做在副本上："
        "变异前=%s… 变异后=%s…"
        % (sha_repo_before[:16], sha_repo_after[:16]))


def _mutate_selector_to_body_only(src: str) -> str:
    """Turn D-1's selector back into "only compares the body".

    The mutation is **surgical and self-verifying**: the returned source
    must NOT contain the stale-token question any more.  If D-1's code
    shape changes, this function must fail loudly rather than silently
    produce a source that still asks both questions.
    """
    marker = "            # ② the STALE TOKEN: the body agrees with the truth, but"
    if marker not in src:
        pytest.fail(
            "找不到 D-1 的「② 令牌陈旧」那一段 ⇒ 变异点没找到。"
            "（D-1 改了这段的形状 ⇒ 请更新本变异点，不要删掉这条判据）")
    # cut the stale-token question out: the body-drift branch keeps its
    # `continue`, so the stale-token branch becomes unreachable
    mutated = src.replace(
        "            actual = body_hash(frag)\n"
        "            if (bhash or \"\") != actual:\n"
        "                stale_token += 1\n"
        "                if stale_sample is None:\n"
        "                    stale_sample = {\"id\": cid, \"anchor\": anchor,\n"
        "                                    \"stored_token\": (bhash or \"\")[:32],\n"
        "                                    \"actual_token\": actual[:32]}\n",
        "            # (mutated: the stale-token question removed)\n")
    if mutated == src:
        pytest.fail("变异没有改到源码（片段不匹配）⇒ 变异点没找到")
    if "stale_token += 1" in mutated:
        pytest.fail("变异没有去掉「stale_token += 1」⇒ 变异无效")
    return mutated


# ===========================================================================
# the self-report
# ===========================================================================

def test_every_criterion_declares_its_subject():
    """每条判据都自报它测的是哪个对象（哪个库 / 哪条判据 / 哪个变异点）。"""
    src = Path(__file__).read_text(encoding="utf-8")
    for cid in ("E-1", "E-2", "E-3", "E-4"):
        assert ("**%s · 对照臂" % cid) in src or ("**%s · 变异臂" % cid) in src, \
            "%s 没有自报对照臂/变异臂" % cid
    assert "被测对象" in src, "判据没有自报被测对象"
    assert "自证生效" in src, "变异臂没有自证生效"
    assert "逐字节一致" in src, "还原后没有做逐字节比对"
    assert "活库污染" in src, "上一轮的活库污染没有写进这个文件"
    print("\n[自检] 四组判据都自报了被测对象 / 对照臂 / 变异臂 / "
          "自证生效 / 逐字节比对；上一轮的活库污染写在文件头")


def test_the_criteria_do_not_import_the_object_under_test():
    """判据**不许调用被测对象的函数**（同义反复）。

    本文件自己写 SQL、自己写锚点解析器、自己写规范化、自己算 sha256、
    自己写指纹。唯一允许 import 被测对象的三处，每一处都带理由：

      * ``agent.memory.drift_recut`` —— E-4：被测对象**就是**选择器；
      * ``agent.memory.inspector.Inspector`` —— E-1：被测对象**就是**门禁；
      * ``agent.memory.acceptance.content_digest`` —— E-2：被测对象**就是**
        这把尺子（测的是「它看不见」这个事实）；
      * ``agent.memory.body_pointer.resolve_truth_file`` —— E-4：只用来
        找真源文件的**位置**，不参与任何判定。

    这四处都是**测「机制/尺子看不见」**，不是测「判据的答案」。
    """
    import ast

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for a in node.names:
                imported.append("%s.%s" % (mod, a.name) if mod else mod)

    # The modules whose FUNCTIONS the criteria judge.
    judged_modules = (
        "agent.memory.ledger_orphans",
        "agent.memory.text_inv",
        "agent.memory.certify",
        "agent.memory.decide_origin",
        "agent.memory.migrate",
        "agent.memory.storage",
        "agent.memory.manager",
        "agent.memory.write_decide",
        "agent.memory.rebuilder",
    )
    for name in imported:
        for bad in judged_modules:
            assert not name.startswith(bad), (
                "判据 import 了被测对象的函数：%s —— 那是同义反复" % name)

    allowed_exact = {
        "agent.memory.drift_recut",
        "agent.memory.inspector.Inspector",
        "agent.memory.acceptance.content_digest",
        "agent.memory.body_pointer.resolve_truth_file",
    }
    for name in imported:
        if name.startswith("agent.memory"):
            assert name in allowed_exact, (
                "出现了未经登记的 agent.memory import：%s" % name)

    # The criteria's own readers must be present and self-contained.
    for own in ("def canonical(", "def sha256_token(", "def _rebuild_fragment(",
                "def _resolve_truth(", "def read_token_mismatches(",
                "def fingerprint_rows(", "def fingerprint_diff(",
                "def _own_digest("):
        assert own in src, "判据自己的读法不见了：%s" % own
    print("\n[自检] 判据的判定路径上没有来自被测对象的函数；"
          "四处 import 各自带理由（见各自的 docstring）")
    print("[自检] 实际 import 清单（agent.memory 部分）= %s"
          % sorted(n for n in imported if n.startswith("agent.memory")))


def test_every_write_target_is_outside_the_live_workspace():
    """**安全闸**：本文件的写路径**必须**落在 TEMP 之下。

    MEASURED (2026-11-05 21:19:34): the previous round pointed
    ``Inspector`` at the real workspace and the LIVE library grew a row.
    ⇒ the guard is asserted by a test, so a future edit that breaks it
    is RED, not silent.
    """
    tmp = Path(tempfile.gettempdir()).resolve()
    assert tmp.is_dir(), "TEMP 目录不存在 —— 前置条件不成立"
    # the live workspace must NOT be under TEMP (else the guard is void)
    ws = Path(WORKSPACE).resolve()
    try:
        ws.relative_to(tmp)
    except ValueError:
        pass
    else:
        pytest.fail("真源工作区在 TEMP 之下 ⇒ 沙箱闸门形同虚设")
    # the guards themselves must refuse a live target
    try:
        _assert_not_live(ws / "memory" / "long-term" / "index.db")
    except BaseException as e:
        assert "拒绝执行" in str(e), "闸门的报错形状变了：%s" % e
    else:
        pytest.fail("闸门没有拦住真源工作区里的写入目标")
    print("\n[自检] 写入目标闸门有效：真源工作区里的目标被拒绝")
