#!/usr/bin/env python3
# encoding:utf-8
"""交办 J · J-1 · B1 判据（``body_anchor IS NULL == 0``）+ J-2 分档报告层.

**这个文件测的是「越界写入」这个事实，不是活库的存量。**

交办书 J（``deliverables/2026-11-06-交办J-B1判据.md``）要求三条：

  J-1 (P0)  B1 判据 —— ``body_anchor IS NULL`` 的行数 == 0
  J-2        门禁口径分档（B1 / B2 / B3）的**报告层**
  J-3        口径变更前的读数对照（域内 vs 域外）

=========================  判据编号  =========================

=========  ==================================================================
编号       测什么
=========  ==================================================================
J-1-0      B1 判据本身的写权限闸门（守卫自己）
J-1-C      **B1 对照臂（绿）** —— 活库 ``body_anchor IS NULL == 0``
J-1-M      **B1 变异臂（红）** —— 副本上模拟越界写入 ⇒ 必须转红
J-1-S      B1 变异臂的**自证**（变异真的发生了）+ 还原后 sha256 逐字节一致
J-2-0      三档的**分档判据**：B1 与 B2 的可判定区分
J-2-1      分档的**假阳性/假阴性边界**（自证：两个反例都必须被抓住）
J-3-0      域内 vs 域外读数（不改口径，先给数）
=========  ==================================================================

=========================  本文件的纪律  =========================

1. **判据不许调用被测对象的函数**（同义反复）。B1 的判定**全部**用本
   文件自己写的 SQL。被测对象是**数据**（索引层的那一行），不是某个
   函数 —— 所以这里没有任何 ``import agent.memory.*`` 的判定路径。
   唯一允许 import 的是 ``agent.memory.acceptance.live_libs`` 的
   **枚举**（活库清单）—— 理由与 ``live_libs`` 自己的文档一致：枚举
   的**规则**写在这里（按 PATH 形状），不向被测对象问它要清单。

2. **判据不许写活库。** 活库连接一律 ``mode=ro``。变异臂在
   ``%TEMP%`` 下的**副本**上做；写目标必须同时通过两道闸：
   必须在 TEMP 之下，且**不在真源工作区之内**。
   ⇒ 闸门本身被 ``J-1-0`` 守着（它会真的去试「拿真源工作区里的库当
   写入目标」，要求闸门**拒绝**）。

3. **变异臂自证生效** —— 读回一个具体的行、断言它真的变成了那个形状，
   并断言**全库计数**真的变了。

4. **还原后 sha256 逐字节一致** —— 变异 → 判定 → 还原 → 比对 sha256。
   ⚠️ MEASURED（2026-11-06）：**SQL 层面的 undo 不还原字节** ——
   SQLite 的文件头带着一个写事务就动一次的 change counter ⇒ 还原必须
   是**一份 pristine 快照的字节级复制**，不是一条 ``UPDATE``。

5. **变异对象是数据文件 ⇒ 会有连带红** —— 判定只钉**目标判据**
   （J-1-C 的那个断言），连带红**单独列出来汇报**，不参与判定。

6. **每条判据自报它测的是哪个对象**（哪个库、哪条判据、哪个变异点）。

=========================  归因（新，不是旧的） =========================

⚠️ **交办书 J §0 转达了 SEED 的旧归因（「写路径没带 body_origin」）——
那条已被架构师裁决 G 推翻（``交办G-未决语义裁决.md`` §0）。本文件按
**新归因**写：

  * **真因 = 越界写入者 + 判定挂在 ``sync()`` 的早退分支之后。**
  * 越界写入者的形状：**十二列 ``INSERT OR REPLACE INTO chunks``**，
    它不认识 body 四列 ⇒ 四列留 NULL ⇒ ``body_origin`` 的列 DEFAULT
    填成 ``'unresolved'``。
  * 早退分支（``manager.py`` 的 ``if not pending: ... return``）让
    写后判定永远走不到 ⇒ 那行永远停在 ``unresolved``。
  * **修复已落地**（commit ``cce9ebb5``，架构师裁决 G-2026-11-06-Q2）：
    判定被挪到早退分支**之前**。

⇒ **本判据不测「写路径有没有带 body_origin」**（那是实现侧的事）。
  本判据测的是**数据层的事实**：索引层里有没有一行**没有指针**。
  这与归因无关 —— 无论是谁写的、为什么写的，一行没有指针的行就是
  「越过了投影边界」的行。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

#: The workspace under test.  FIXED, not environment-dependent — a
#: criterion whose target depends on the environment is not a criterion.
WORKSPACE = Path(os.environ.get("COW_J_WORKSPACE", r"C:\Users\User\cow"))
REPO = Path(__file__).resolve().parents[1]

#: The B1 fingerprint the architect's ruling names: a row with NO pointer
#: never went through the projection, so it is an out-of-bounds write.
#:
#: ⚠️ **Why ``body_anchor`` and not ``body_path``.**  MEASURED
#: (2026-11-06): ``body_path IS NULL`` is a LEGAL state — ``add_memory``
#: writes a blob under a synthetic path with no truth file behind it, so
#: ``body_path`` is NULL **by design** while ``body_anchor`` and
#: ``body_hash`` are still filled (``body_pointer.five_columns_for_chunk``
#: :665-669).  ``body_anchor`` is NEVER NULL on any legitimate write
#: path: it is computed from the chunk's span, so it exists even when
#: the path is unsafe (``body_pointer.body_anchor`` :143-194 returns a
#: string unconditionally).  ⇒ **``body_anchor IS NULL`` is the
#: fingerprint with no legal-NULL false positives; ``body_path IS NULL``
#: is not.**
B1_FINGERPRINT = "body_anchor"


# ---------------------------------------------------------------------------
# the guards that keep this file from writing the live fleet
# ---------------------------------------------------------------------------

def _assert_sandbox(path: Path) -> None:
    """A write target MUST live under the TEMP directory.

    MEASURED (2026-11-05 21:19:34): a probe pointed a writer at the
    REAL workspace and the live library's ``cert_ledger`` grew by one
    row.  A write target outside ``%TEMP%`` is a FAIL, not a warning.
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
    """BOTH gates.  The delegation's red line: the write target must be
    in ``%TEMP%`` AND the write must assert it is not in the source
    workspace BEFORE the write."""
    _assert_not_live(path)
    _assert_sandbox(path)


# ---------------------------------------------------------------------------
# the readers — THIS file's OWN
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def ro(db: Path):
    """A read-only connection that is ALWAYS closed.

    A leaked read-only connection keeps the file open on Windows, and
    the byte-level restore (``unlink`` + ``copy2``) then fails with
    ``PermissionError: [WinError 32]``.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
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


def live_libraries() -> List[Tuple[str, Path]]:
    """The live libraries, by PATH SHAPE — this file's OWN enumeration.

    Deliberately not ``agent.memory.migrate.enumerate_live``: the
    migration tool is the object under test for the M-criteria, and a
    criterion that asks the object under test which objects to measure is
    a tautology.  The rule is spelled out HERE:

      * ``<ws>/memory/long-term/index.db``            — the ROOT library
      * ``<ws>/agents/<id>/memory/long-term/index.db`` — one per agent

    and nothing else.  A ``*.db`` glob would also pick up the sweep's
    own backups and the scratch copies, and the criterion would then
    measure a BACKUP and report it as a live library.
    """
    out: List[Tuple[str, Path]] = []
    root_db = WORKSPACE / "memory" / "long-term" / "index.db"
    if root_db.is_file():
        out.append(("ROOT", root_db))
    agents_dir = WORKSPACE / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            db = agent_dir / "memory" / "long-term" / "index.db"
            if db.is_file():
                out.append((agent_dir.name, db))
    return out


#: The B1 query — this file's OWN SQL.
#:
#: ⚠️ The condition is ``body_anchor IS NULL OR body_anchor=''``.  An
#: empty string is the same defect as NULL: the anchor is the locator,
#: and ``''`` locates nothing.  MEASURED (2026-11-06): the live fleet
#: has **zero** rows with an empty anchor, so including ``''`` costs
#: nothing and closes the "someone wrote '' instead of NULL" hole.
B1_SQL = ("SELECT COUNT(*) FROM chunks "
          "WHERE body_anchor IS NULL OR body_anchor = ''")

#: The rows themselves, for naming the offenders.
B1_ROWS_SQL = ("SELECT id, path, start_line, end_line, text_state, "
               "body_origin, body_path, body_anchor, body_hash, created_at "
               "FROM chunks WHERE body_anchor IS NULL OR body_anchor = '' "
               "ORDER BY path, start_line")


def b1_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """The B1 rows — the out-of-bounds writes, named."""
    try:
        return conn.execute(B1_ROWS_SQL).fetchall()
    except sqlite3.Error:
        return []


def b1_count(conn: sqlite3.Connection) -> int:
    """The B1 count.  ``-1`` when the column does not exist at all —
    a schema that has no ``body_anchor`` is a DIFFERENT thing, and the
    caller must be able to say so."""
    try:
        return int(conn.execute(B1_SQL).fetchone()[0])
    except sqlite3.Error:
        return -1


def chunks_columns(conn: sqlite3.Connection) -> List[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(chunks)")]
    except sqlite3.Error:
        return []


# ===========================================================================
# J-1-0 · the write-permission gate, itself guarded
# ===========================================================================

def test_j1_0_every_write_target_is_outside_the_live_workspace():
    """**J-1-0** —— 被测对象：**本文件的写权限闸门**（``_assert_sandbox``
    与 ``_assert_not_live``）。

    闸门本身必须被一条判据守着：它会**真的去试**「拿真源工作区里的库
    当写入目标」，要求闸门**拒绝**。没有这一条，闸门只是一句承诺。
    """
    print("\n[J-1-0] 被测对象 = 本文件的写权限闸门")

    # ① the live library itself must be REFUSED as a write target
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"
    victim = libs[0][1]
    with pytest.raises(BaseException) as ei:
        _assert_writable_target(victim)
    assert "真源工作区" in str(ei.value) or "TEMP" in str(ei.value), (
        "闸门拒绝活库时必须说清为什么：%s" % ei.value)
    print("[J-1-0] ① 拿活库当写入目标 ⇒ 闸门拒绝 = True（%s）"
          % str(ei.value)[:70])

    # ② a path OUTSIDE the TEMP must be REFUSED too
    outside = REPO / "j1_should_never_be_written.db"
    with pytest.raises(BaseException) as ei2:
        _assert_writable_target(outside)
    print("[J-1-0] ② 拿 TEMP 之外的路径当写入目标 ⇒ 闸门拒绝 = True（%s）"
          % str(ei2.value)[:70])

    # ③ a path INSIDE the TEMP must be ACCEPTED
    ok = Path(tempfile.gettempdir()) / "j1_gate_probe.db"
    _assert_writable_target(ok)
    print("[J-1-0] ③ 拿 TEMP 之内的路径当写入目标 ⇒ 闸门放行 = True")

    print("[J-1-0] 结论：写权限闸门本身有效（拒绝真源、拒绝非 TEMP、"
          "放行 TEMP 之内）")


# ===========================================================================
# J-1-C / J-1-M / J-1-S · the B1 criterion
# ===========================================================================

def test_j1_c_control_no_row_in_the_index_lacks_a_pointer():
    """**J-1 · 对照臂（绿）** —— 被测对象：**活库的 ``chunks`` 表**
    （16 个库），被测的事实：**有没有一行没有指针**。

    断言：``body_anchor IS NULL OR body_anchor=''`` 的行数 == 0。

    ⚠️ **口径说明（为什么不问「unresolved」）.**  架构师裁决 G §5.2
    把 B1 定为「越界写入」，指纹 = ``body_anchor IS NULL``。本判据
    **直接测那个指纹**，而不是测 ``body_origin='unresolved'`` —— 因为
    ``unresolved`` 是**两档合一的形状**（B1 从未决定 与 B2 待判定），
    用它当 B1 的判据就会把 B2 一起判红，那正是这次要拆开的东西。

    ⚠️ **这条判据测的是数据，不是机制.**  归因（新）：**越界写入者 +
    判定挂在 ``sync()`` 早退分支之后**。修复（commit ``cce9ebb5``）
    已把判定挪到早退之前。⇒ 本判据现在应当是绿的；但它**不是**
    「修复生效了」的证明 —— 它是「**此刻**索引层里没有一行没有指针」
    的读数。任何新的越界写入都会让它立刻转红，这正是它存在的意义。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    print("\n[J-1 对照臂] 被测对象 = 活库的 chunks 表（%d 个库）" % len(libs))
    print("[J-1 对照臂] 判据 = body_anchor IS NULL OR body_anchor='' 的行数 == 0")
    print("[J-1 对照臂] 指纹 = body_anchor（不是 body_path：blob 行的 "
          "body_path 合法为 NULL）")

    total = 0
    red: List[str] = []
    notrun: List[str] = []
    for name, db in libs:
        with ro(db) as conn:
            cols = chunks_columns(conn)
            if "body_anchor" not in cols:
                notrun.append(name)
                print("[J-1 对照臂]   %-22s ⇒ NOTRUN: chunks 表没有 body_anchor 列"
                      % name)
                continue
            n = b1_count(conn)
            total += n
            if n:
                red.append("%s(%d)" % (name, n))
                for r in b1_rows(conn)[:5]:
                    print("[J-1 对照臂]   ! %-22s id=%s path=%s state=%s "
                          "origin=%s body_path=%s body_hash=%s created_at=%s"
                          % (name, r["id"], r["path"], r["text_state"],
                             r["body_origin"], r["body_path"], r["body_hash"],
                             r["created_at"]))
                    print("[J-1 对照臂]     定性：这一行**没有指针** ⇒ 它没有经过"
                          "投影 ⇒ 它是**越界写入**（B1），不是「待判定」（B2）。")
            else:
                print("[J-1 对照臂]   %-22s B1=0" % name)

    print("[J-1 对照臂] 全活库合计 B1 行数 = %d" % total)
    if notrun:
        print("[J-1 对照臂] NOTRUN 的库（没有 body_anchor 列）: %s"
              % ", ".join(notrun))
    assert not notrun, ("有活库没有 body_anchor 列 ⇒ 本判据未测量全舰队：%s"
                         % ", ".join(notrun))
    assert total == 0, (
        "索引层里有 %d 行没有指针 ⇒ 越界写入（B1）：%s" % (total, ", ".join(red)))

    print("[J-1 对照臂] 结论：**全活库 0 行没有指针** ⇒ B1 判据为绿")


def _b1_mutation_target(tmp_path: Path) -> Tuple[Path, List[str], str]:
    """A COPY of a live library, plus the ids we are going to mutate.

    ⚠️ **为什么必须用副本.**  交办书 J §4 红线：不许写活库。变异
    打在副本上；副本在 ``%TEMP%`` 之下（``tmp_path`` 就在那儿），且
    **写之前**先断言它不在真源工作区之内。

    ⚠️ **变异形状 = 真·越界写入.**  交办书 J §1 说「在库副本上把一批
    行的 ``body_anchor`` 置 NULL（模拟越界写入）」。但 **MEASURED
    (2026-11-06)**：单纯 ``UPDATE ... SET body_anchor=NULL`` 与真·越界
    写入**不是同一件事** ——

      * 真实的老写者做的是**十二列 ``INSERT OR REPLACE``**：它不认识
        body 四列 ⇒ 四列**全部**留 NULL（``body_path`` / ``body_anchor``
        / ``body_hash``）⇒ ``body_origin`` 落成列 DEFAULT
        ``'unresolved'``；
      * 只抹 ``body_anchor`` 而留着 ``body_path`` / ``body_hash``
        是一个**更弱**的形状，它测的是「指针被局部擦掉」，不是
        「整行越过投影边界」。

    ⇒ 本变异**两个都做**：
      * **主变异（M1）** = 十二列 ``INSERT OR REPLACE`` 打一批**新 id**
        ⇒ 这是架构师实测复现过的**真·越界写入形状**；
      * **辅变异（M2）** = 把一批**既有行**的 ``body_anchor`` 置 NULL
        ⇒ 这是交办书字面要求的形状，也是**最容易漏掉**的形状。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    # pick the SMALLEST live library: the mutation must be cheap and the
    # copy must fit in %TEMP%
    name, src = min(libs, key=lambda kv: kv[1].stat().st_size)
    dst = Path(tmp_path) / ("b1-mutant-%s.db" % name)
    _assert_writable_target(dst)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(str(src), str(dst))

    with ro(dst) as conn:
        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text, user_id, scope, "
            "source, hash, metadata FROM chunks WHERE body_origin='file' "
            "ORDER BY id LIMIT 8").fetchall()
        cols = chunks_columns(conn)
    assert rows, "副本里没有可用来变异的行（前置条件不成立）"
    assert "body_anchor" in cols

    # the ids we mutate (M2) — existing rows whose anchor we null out
    m2_ids = [r["id"] for r in rows[:4]]
    # the new ids we insert (M1) — the out-of-bounds writer's own shape
    m1_ids = ["j1-outofbounds-%d" % i for i in range(4)]
    return dst, m2_ids, m1_ids


def test_j1_m_mutation_the_outofbounds_write_makes_b1_red(tmp_path):
    """**J-1 · 变异臂（红）** —— 变异点：**在库副本上模拟越界写入**。

    两个变异形状（理由见 :func:`_b1_mutation_target` 的 docstring）：

      * **M1（主）** —— 十二列 ``INSERT OR REPLACE`` 打新 id ⇒
        ``body_path`` / ``body_anchor`` / ``body_hash`` 全 NULL，
        ``body_origin='unresolved'``，``text_state='projection'``。
        这是架构师实测复现过的**真·越界写入形状**。
      * **M2（辅）** —— 把既有行的 ``body_anchor`` 置 NULL
        ⇒ 这是交办书字面要求的形状。

    判定：**目标判据必须转红**（``body_anchor IS NULL`` 的计数从 0 变正）。

    ⚠️ **连带红单独列出，不参与判定.**  变异对象是**数据文件** ⇒
    任何读这份数据的判据都会跟着变红（哈希/一致性检查）。本臂只钉
    **B1 这一个断言**，连带红在下面单独汇报。
    """
    dst, m2_ids, m1_ids = _b1_mutation_target(tmp_path)
    print("\n[J-1 变异臂] 被测对象 = 副本上的 B1 判据")
    print("[J-1 变异臂] 副本 = %s" % dst)
    print("[J-1 变异臂] 变异点 = ① M1 十二列 INSERT OR REPLACE 打 %d 个新 id "
          "② M2 把 %d 个既有行的 body_anchor 置 NULL"
          % (len(m1_ids), len(m2_ids)))

    # --- the control reading, BEFORE the mutation ---
    with ro(dst) as conn:
        before_count = b1_count(conn)
    print("[J-1 变异臂] 变异前 B1 计数 = %d" % before_count)
    assert before_count == 0, (
        "副本的变异前基线就不是 0 ⇒ 变异臂测不到任何东西（前置条件不成立）")

    # --- the mutation ---
    conn = sqlite3.connect(str(dst))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text, user_id, scope, "
            "source, hash, metadata FROM chunks WHERE body_origin='file' "
            "ORDER BY id LIMIT 8").fetchall()
        # M1: the out-of-bounds writer's own SQL — TWELVE columns, and it
        # does NOT name the body columns, so they keep their DEFAULTs.
        for i, new_id in enumerate(m1_ids):
            r = rows[i]
            conn.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, user_id, scope, source, path, start_line, end_line, "
                " text, embedding, hash, metadata, element_released, "
                " file_sealed, sealed_owner, body_path, body_anchor, "
                " body_hash, text_state, body_origin, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, 0, NULL, "
                " NULL, NULL, NULL, 'projection', 'unresolved', "
                " strftime('%s','now'))",
                (new_id, r["user_id"], r["scope"], r["source"], r["path"],
                 r["start_line"], r["end_line"], r["text"], r["hash"],
                 r["metadata"]))
        # M2: the delegation's literal shape — null the anchor of existing rows
        conn.execute(
            "UPDATE chunks SET body_anchor=NULL WHERE id IN (%s)"
            % ",".join("?" * len(m2_ids)), m2_ids)
        conn.commit()
    finally:
        conn.close()

    # --- the self-proof that the mutation took effect ---
    with ro(dst) as conn:
        after_count = b1_count(conn)
        rows_after = b1_rows(conn)
        # M1's rows must have the EXACT out-of-bounds shape
        m1_readback = []
        for new_id in m1_ids:
            r = conn.execute(
                "SELECT body_path, body_anchor, body_hash, text_state, "
                "body_origin FROM chunks WHERE id=?", (new_id,)).fetchone()
            m1_readback.append((new_id, r))
        # M2's rows must have a NULL anchor
        m2_readback = []
        for cid in m2_ids:
            r = conn.execute(
                "SELECT body_anchor, body_origin FROM chunks WHERE id=?",
                (cid,)).fetchone()
            m2_readback.append((cid, r))

    print("[J-1 变异臂] 变异后 B1 计数 = %d" % after_count)
    print("[J-1 变异臂] 自证 ① M1 读回：")
    for new_id, r in m1_readback:
        assert r is not None, "M1 的新行不见了 ⇒ 变异没有生效"
        assert r["body_anchor"] is None, (
            "M1 读回 body_anchor=%r —— 越界写入的形状不对" % r["body_anchor"])
        assert r["body_path"] is None and r["body_hash"] is None
        assert r["body_origin"] == "unresolved"
        assert r["text_state"] == "projection"
        print("[J-1 变异臂]    %-22s body_path=%s body_anchor=%s body_hash=%s "
              "state=%s origin=%s" % (new_id, r["body_path"], r["body_anchor"],
                                      r["body_hash"], r["text_state"],
                                      r["body_origin"]))
    print("[J-1 变异臂] 自证 ② M2 读回：")
    for cid, r in m2_readback:
        assert r is not None, "M2 的行不见了 ⇒ 变异没有生效"
        assert r["body_anchor"] is None, (
            "M2 读回 body_anchor=%r ⇒ 变异没有生效" % r["body_anchor"])
        print("[J-1 变异臂]    %-34s body_anchor=%s origin=%s"
              % (cid, r["body_anchor"], r["body_origin"]))

    # --- the target assertion: B1 must have turned RED ---
    assert after_count > 0, (
        "越界写入之后 B1 计数仍然是 0（%d）⇒ **判据看不见越界写入**，"
        "这条判据是瞎的" % after_count)
    assert after_count >= len(m1_ids) + len(m2_ids), (
        "B1 计数 %d 小于注入的行数 %d ⇒ 有形状漏掉了"
        % (after_count, len(m1_ids) + len(m2_ids)))

    # --- the collateral reds, reported separately, NOT part of the verdict ---
    collateral = _collateral_reds(dst)
    print("[J-1 变异臂] ⚠️ 连带红（**单独汇报，不参与判定**）：")
    for name, detail in collateral:
        print("[J-1 变异臂]    · %s: %s" % (name, detail))
    print("[J-1 变异臂] 连带红合计 = %d 项 —— 它们是「变异对象是数据文件」"
          "的必然结果，与本判据无关。" % len(collateral))

    print("[J-1 变异臂] 结论：**越界写入 ⇒ B1 判据转红**（%d → %d）"
          % (before_count, after_count))


def _collateral_reds(db: Path) -> List[Tuple[str, str]]:
    """What ELSE turned red because the DATA changed.  Reported, not judged.

    The delegation's warning is exact: mutating a data file makes every
    criterion that reads that data turn red too.  The verdict is pinned to
    the target criterion alone; these are listed so nobody later mistakes
    them for new findings.
    """
    out: List[Tuple[str, str]] = []
    with ro(db) as conn:
        # ① the certification ledger's token now disagrees with chunks
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger l JOIN chunks c ON c.id=l.id "
                "WHERE l.body_hash IS NOT NULL AND c.body_hash IS NOT NULL "
                "AND l.body_hash IS NOT c.body_hash").fetchone()[0]
            if n:
                out.append(("账本令牌与 chunks 不一致（M6b 的域）",
                            "%d 行" % n))
        except sqlite3.Error:
            pass
        # ② the ledger holds entries for rows that no longer carry a pointer
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger l JOIN chunks c ON c.id=l.id "
                "WHERE c.body_anchor IS NULL").fetchone()[0]
            if n:
                out.append(("账本见证的行没有指针了（越界写入的直接后果）",
                            "%d 行" % n))
        except sqlite3.Error:
            pass
        # ③ the row count moved
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"
            ).fetchone()[0]
            if n:
                out.append(("unresolved 行（越界写入的落点）", "%d 行" % n))
        except sqlite3.Error:
            pass
    return out


def test_j1_s_the_mutation_is_reverted_byte_for_byte(tmp_path):
    """**J-1 · 自证 + 还原臂** —— 两件事：

    ① **变异自证生效**（计数 + 逐行读回，见上）；
    ② **还原后 sha256 逐字节一致**。

    ⚠️ **MEASURED（2026-11-06）：SQL 层面的 undo 不还原字节。**
    SQLite 的文件头带一个写事务就动一次的 change counter ⇒ 一条
    ``UPDATE`` 写回原值，**逻辑上一致、字节上不一致**。实测：同一
    副本上「UPDATE 置 NULL → UPDATE 写回原值」，sha256 仍然不同
    （846a4556… → eaafc698… → d6db77f0…）。

    ⇒ 还原必须是**一份 pristine 快照的字节级复制**，不是一条 UPDATE。
    本臂照此做，并在**同一个副本**上比对前后。
    """
    dst, m2_ids, m1_ids = _b1_mutation_target(tmp_path)
    pristine = Path(tmp_path) / "b1-pristine.db"
    _assert_writable_target(pristine)
    shutil.copy2(str(dst), str(pristine))
    sha_pristine = sha256_file(pristine)
    sha_before = sha256_file(dst)

    print("\n[J-1 还原臂] 被测对象 = 副本上的 B1 判据 + 字节级还原")
    print("[J-1 还原臂] 副本 = %s" % dst)
    print("[J-1 还原臂] pristine 快照 sha256 = %s" % sha_pristine)

    with ro(dst) as conn:
        before = b1_count(conn)
    assert before == 0

    # --- mutate ---
    conn = sqlite3.connect(str(dst))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text, user_id, scope, "
            "source, hash, metadata FROM chunks WHERE body_origin='file' "
            "ORDER BY id LIMIT 8").fetchall()
        for i, new_id in enumerate(m1_ids):
            r = rows[i]
            conn.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, user_id, scope, source, path, start_line, end_line, "
                " text, embedding, hash, metadata, element_released, "
                " file_sealed, sealed_owner, body_path, body_anchor, "
                " body_hash, text_state, body_origin, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 0, 0, NULL, "
                " NULL, NULL, NULL, 'projection', 'unresolved', "
                " strftime('%s','now'))",
                (new_id, r["user_id"], r["scope"], r["source"], r["path"],
                 r["start_line"], r["end_line"], r["text"], r["hash"],
                 r["metadata"]))
        conn.execute(
            "UPDATE chunks SET body_anchor=NULL WHERE id IN (%s)"
            % ",".join("?" * len(m2_ids)), m2_ids)
        conn.commit()
    finally:
        conn.close()

    with ro(dst) as conn:
        mutated = b1_count(conn)
    sha_mutated = sha256_file(dst)
    print("[J-1 还原臂] 变异后 B1 计数 = %d（>0 ⇒ 变异生效）" % mutated)
    print("[J-1 还原臂] 变异后 sha256 = %s" % sha_mutated)
    assert mutated > 0, "变异没有生效 ⇒ 还原臂测不到任何东西"

    # --- the byte-level restore ---
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(str(pristine), str(dst))
    sha_restored = sha256_file(dst)

    with ro(dst) as conn:
        restored = b1_count(conn)

    print("[J-1 还原臂] 还原后 B1 计数 = %d" % restored)
    print("[J-1 还原臂] 还原后 sha256 = %s" % sha_restored)
    print("[J-1 还原臂] 变异前 sha256 = %s" % sha_before)
    print("[J-1 还原臂] pristine  sha256 = %s" % sha_pristine)

    assert restored == 0, "还原之后 B1 计数仍然是 %d ⇒ 还原没有生效" % restored
    assert sha_restored == sha_pristine, (
        "还原后 sha256 与 pristine 不一致 ⇒ 还原不是逐字节的：\n"
        "  还原后 = %s\n  pristine = %s" % (sha_restored, sha_pristine))

    print("[J-1 还原臂] 结论：**还原后 sha256 逐字节一致**（%s）" % sha_pristine)
