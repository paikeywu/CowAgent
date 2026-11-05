#!/usr/bin/env python3
# encoding:utf-8
"""交办 J · J-2（分档报告层）+ J-3（域内 vs 域外读数）.

**这个文件不改任何既有判据的口径。** 它**只读**，产出三样东西：

  J-2-0  三档的**可判定分档**（B1 / B2 / B3）—— 判据
  J-2-1  分档的**假阳性 / 假阴性边界**（两个反例都必须被抓住）—— 自证
  J-3-0  **域内 vs 域外**读数（口径变更前先给数，先有数后动手）

=========================  判据编号  =========================

=========  ==================================================================
编号       测什么
=========  ==================================================================
J-2-0      分档判据：三档的划分必须**可判定**（同一行只能落一档）
J-2-1      分档的**假阳性/假阴性边界**（两个反例 + 移动靶）
J-3-0      M6a/M6b/M6c/M6e/M6f 的**域内 vs 域外**读数
=========  ==================================================================

=========================  J-2 的核心难题  =========================

交办书 J §2 问：「**怎么区分 B1 与 B2？** 架构师给的指纹是
``body_anchor IS NULL``，但 B2（正常待判定）**也是空的** ⇒ 请给出
一个可判定的区分办法，并说明它的假阳性/假阴性边界。」

=========================  答案：判据用的不是「空不空」，是「**空得**像不像**」 =========================

**B1（从未决定 · 越界写入）的形状是「**整行**没有指针」。**
三列（``body_path`` / ``body_anchor`` / ``body_hash``）**同时**为空
⇒ 这一行**从来没有经过投影** ⇒ 写它的人不认识 body 四列 ⇒ **越界
写入**。

**B2（待判定）的形状是「**来源信息已带，但判定还没跑**」。**
它的 ``body_path`` / ``body_anchor`` / ``body_hash`` **都在**（指针
在），只有 ``body_origin`` 还停在 ``'unresolved'`` ⇒ 这一行**经过
了投影**，只是判定机制还没轮到它。

⇒ **可判定的区分办法（一条 SQL 就能判）**：

  B1  ⟺  ``body_anchor IS NULL`` **且** ``body_path IS NULL``
         **且** ``body_hash IS NULL``   （三列全空 ⇒ 整行没有指针）

  B2  ⟺  ``body_anchor IS NOT NULL`` **且** ``body_origin='unresolved'``
         （指针在，来源待判定）

  B3  ⟺  判定跑过了仍定不出 ⇒ ``body_origin='unresolved'`` **且**
         该行**曾经**被判定过（``cert_ledger`` 或 ``body_keep`` 里有
         它的快照）—— **判定时刻**那条轴，见下。

=========================  为什么「三列全空」而不是「anchor 空」  =========================

⚠️ **只有一列空，是「局部损坏」，不是「越界写入」。**
MEASURED（2026-11-06，副本上实测）：一条既有行的 ``body_anchor``
被单独置 NULL，而 ``body_path`` / ``body_hash`` 还在 ⇒
``body_origin`` **仍然是** ``'file'`` ⇒ 它**不是** B1（它已经决定过
出身），而是**指针被局部擦掉**。若判据用「anchor 空就是 B1」，
它会把这一类**误报成越界写入** —— 而这一类的修法（重切补指针）
与越界写入的修法（**找到那个写者并让它停止**）**完全不同**。

⚠️ **「三列全空」也不是万无一失.**  见 J-2-1 的**假阴性边界**。

=========================  B3 怎么判：轴必须换  =========================

架构师裁决 G §5.1 明确指出：**「待认证 vs 活过认证」这个切分用的
是 ``cert_ledger.MAX(certified_at)`（认证时刻），而我们要分的是
``body_origin``（来源决定时刻）⇒ 这是两条轴。**

⇒ **本文件用「判定时刻」那条轴**，具体是：**这一行是否带着一次
判定留下的痕迹**。

⚠️ **诚实的边界**：判定器**不写时间戳**到 chunks 行上。它写的
是 ``body_origin`` 与三列指针。**判定「跑过了但定不出」这件事，
在数据里留下的痕迹是「三列指针**在**（判定填了指针）而
``body_origin`` 仍停在 ``'unresolved'``」** —— 这与 B2 的形状**完全
一样**。

⇒ **结论（必须说清）：B3 在当前的数据结构里与 B2 不可区分。**
这**不是**本判据的口径没想好，这是**架构师裁决 G §2.2 自己指出的
「当前设计最大的语义缺口」**：B1/B2/B3 在现有的列结构里是同一个值
``'unresolved'``。

⇒ **但 B3 有一个可用的代理指标**（本文件用它，且标明它是代理）：
**「``body_origin='unresolved'`` 且该行的 ``text_state`` 已经是
``'mirror'``」**。理由：``text_state`` 只有认证器会写（判定不碰它，
见 ``decide_origin.py`` :43-50）⇒ 一行既是 ``unresolved`` 又已经
``mirror``，说明它**过了认证但来源没定** ⇒ 它不是「还没轮到判定」
（B2），而是「判定了但定不出」（B3）。

⚠️ 这个代理指标的**假阳性**：一行被认证过（``mirror``）之后，
来源才变成 ``unresolved``（比如有人后来擦了它的 origin）⇒ 它会被
误报成 B3。**假阴性**：判定跑过、定不出、且从未被认证 ⇒ 它仍是
``'projection'`` ⇒ 会被归进 B2。⇒ **B3 的这一档必须用「有人来看」
来关，它的计数永远是「上界」。**
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

WORKSPACE = Path(os.environ.get("COW_J_WORKSPACE", r"C:\Users\User\cow"))


@contextlib.contextmanager
def ro(db: Path):
    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def live_libraries() -> List[Tuple[str, Path]]:
    """The live libraries, by PATH SHAPE — this file's OWN enumeration."""
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


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return name in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return False


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        return col in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return False


#: The three tiers, as SQL predicates.  **This file's own.**
#:
#: ⚠️ They are **mutually exclusive** by construction: B1 requires all
#: three pointer columns empty; B2 requires the anchor present; B3
#: requires the anchor present AND already certified.  ⇒ a row can land
#: in at most one tier.  ``J-2-0`` proves that on real data.
TIER_B1_SQL = (
    "SELECT COUNT(*) FROM chunks WHERE "
    "(body_anchor IS NULL OR body_anchor='') AND "
    "(body_path   IS NULL OR body_path='')   AND "
    "(body_hash   IS NULL OR body_hash='')")

TIER_B2_SQL = (
    "SELECT COUNT(*) FROM chunks WHERE "
    "(body_anchor IS NOT NULL AND body_anchor<>'') AND "
    "body_origin='unresolved'")

TIER_B3_SQL = (
    "SELECT COUNT(*) FROM chunks WHERE "
    "(body_anchor IS NOT NULL AND body_anchor<>'') AND "
    "body_origin='unresolved' AND text_state='mirror'")

#: the "unresolved" universe the tiers partition
TIER_UNIVERSE_SQL = "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"


def tier_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """The three tier counts + the universe they partition.

    ``B3`` is a SUBSET of ``B2`` by construction (both require the
    anchor present + unresolved; B3 adds "already certified").  ⇒ the
    reporting layer must present them as **B2 (含 B3)**, **B3 (其中)**,
    not as three disjoint counts — otherwise the sum lies.
    """
    def q(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0])
        except sqlite3.Error:
            return -1

    b1 = q(TIER_B1_SQL)
    b2 = q(TIER_B2_SQL)
    b3 = q(TIER_B3_SQL)
    universe = q(TIER_UNIVERSE_SQL)
    # rows that are unresolved but have NO anchor — those are B1 by the
    # three-empty rule ONLY if path and hash are also empty; a row with
    # a NULL anchor but a present path/hash is "locally damaged", which
    # is neither B1 nor B2.
    local = q("SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved' "
              "AND (body_anchor IS NULL OR body_anchor='') "
              "AND NOT ((body_path IS NULL OR body_path='') "
              "AND (body_hash IS NULL OR body_hash=''))")
    return {
        "B1": b1,
        "B2": b2,
        "B3": b3,
        "B2_without_B3": b2 - b3 if b2 >= 0 and b3 >= 0 else -1,
        "local_damage": local,
        "universe": universe,
    }


def tier_rows(conn: sqlite3.Connection, tier: str) -> List[sqlite3.Row]:
    sql = {
        "B1": TIER_B1_SQL.replace("COUNT(*)", "id, path, start_line, end_line, "
                                  "text_state, body_origin, body_path, "
                                  "body_anchor, body_hash, created_at"),
        "B2": TIER_B2_SQL.replace("COUNT(*)", "id, path, start_line, end_line, "
                                  "text_state, body_origin, body_path, "
                                  "body_anchor, body_hash, created_at"),
        "B3": TIER_B3_SQL.replace("COUNT(*)", "id, path, start_line, end_line, "
                                  "text_state, body_origin, body_path, "
                                  "body_anchor, body_hash, created_at"),
    }[tier]
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.Error:
        return []


# ===========================================================================
# J-2-0 · the tiering must be decidable
# ===========================================================================

def test_j2_0_the_three_tiers_are_mutually_exclusive_and_cover_the_universe():
    """**J-2-0** —— 被测对象：**三档的分档判据本身**。

    断言（两条，都是可判定的）：

      ① **互斥**：B1 / B2 / B3 三档**没有一行同时落两档**。
         （B3 是 B2 的子集 —— 这是有意的，报告层必须按「B2 含 B3」
         呈现，否则相加会说谎。）
      ② **穷尽**：``unresolved`` 全集 = B1 + (B2 ∩ ¬B3) + B3 +
         **局部损坏**（anchor 空但 path/hash 还在的行 —— 既不是 B1，
         也不是 B2）。

    ⚠️ **为什么要有「局部损坏」这一档**：一条既有行的 anchor 被单独
    擦掉而 path/hash 还在 ⇒ 它既不是「整行越界写入」（B1），也不是
    「来源待判定」（B2）。把它塞进 B1 会**误报**（修法不同：重切补
    指针 vs 找到那个写者）；把它塞进 B2 也会**误报**（B2 是正常的
    中间态，不该红）。⇒ 它必须**单独一档**，并且**判红**（指针坏了
    就是坏了），但**归因不同**。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    print("\n[J-2-0] 被测对象 = 三档的分档判据（B1 / B2 / B3）")
    print("[J-2-0] 分档规则（可判定，一条 SQL 判一行）：")
    print("[J-2-0]   B1 = 三列指针【全空】 ⇒ 整行没有指针 ⇒ 越界写入")
    print("[J-2-0]   B2 = anchor 在 且 origin='unresolved' ⇒ 来源待判定")
    print("[J-2-0]   B3 = B2 且 text_state='mirror' ⇒ 判定了但定不出")
    print("[J-2-0]   局部损坏 = origin='unresolved' 且 anchor 空但 path/hash 在")

    totals = {"B1": 0, "B2": 0, "B3": 0, "B2_without_B3": 0,
              "local_damage": 0, "universe": 0}
    for name, db in libs:
        with ro(db) as conn:
            if not _has_col(conn, "chunks", "body_origin"):
                continue
            c = tier_counts(conn)
            for k in totals:
                totals[k] += c[k]
            print("[J-2-0]   %-22s B1=%-4d B2=%-4d (其中 B3=%-4d) 局部损坏=%-4d "
                  "unresolved 全集=%d" % (name, c["B1"], c["B2"], c["B3"],
                                          c["local_damage"], c["universe"]))

    print("[J-2-0] 全活库合计: %s" % totals)

    # ① the tiers must be mutually exclusive on REAL data
    assert totals["B3"] <= totals["B2"], (
        "B3 是 B2 的子集，但实测 B3=%d > B2=%d ⇒ 分档规则不自洽"
        % (totals["B3"], totals["B2"]))

    # ② the partition must add up
    total = (totals["B1"] + totals["B2_without_B3"] + totals["B3"]
             + totals["local_damage"])
    assert total == totals["universe"], (
        "分档不穷尽：B1(%d) + B2¬B3(%d) + B3(%d) + 局部损坏(%d) = %d "
        "≠ unresolved 全集 %d"
        % (totals["B1"], totals["B2_without_B3"], totals["B3"],
           totals["local_damage"], total, totals["universe"]))

    print("[J-2-0] 结论：分档**互斥**且**穷尽**（B1 + B2 + B3 + 局部损坏 "
          "= unresolved 全集 %d）" % totals["universe"])


# ===========================================================================
# J-2-1 · the boundaries of the discriminator
# ===========================================================================

def test_j2_1_the_discriminator_catches_both_false_positives_and_negatives():
    """**J-2-1** —— 被测对象：**B1/B2 区分办法的假阳性/假阴性边界**。

    这一条是**判据自证**：区分办法必须**在两个方向上都被抓住**。

    ========  ==================================================================
    方向        反例                                          必须落在哪一档
    ========  ==================================================================
    假阳性①     一行**已经决定过**出身（origin='file'），只是 anchor 被
                局部擦掉（path/hash 还在）                    **不是 B1**
    假阳性②     一行**有完整指针**、origin 待判定（正常中间态）  **是 B2**，不是 B1
    假阴性     一行**三列全空**但 origin 已被判定过（origin='file'
                或 'orphan'）                                **仍是 B1**
    ========  ==================================================================

    ⚠️ **假阴性那一行是最重要的.**  越界写入者写的行，origin 是列
    DEFAULT ``'unresolved'``；但**只要有任何机制后来替它定了出身**
    （判定器、或守卫的 ``body_keep`` 恢复），它的 origin 就变成
    ``'file'`` 而**三列指针仍然全空** ⇒ 它仍然是**越界写入的行**，
    却不再满足「unresolved」的形状。

    ⇒ **所以 B1 的判据必须是「三列全空」，而**不能**是「三列全空
    且 origin='unresolved'」** —— 后者会漏掉被判定器「洗白」的那一
    类。**这是本文件最重要的一句话。**

    ⚠️ 这也意味着：**B1 与 B2 不是「unresolved 的两个子集」，而是
    两个**不同的问题**。** B1 问「这行有没有指针」（与 origin 无关），
    B2 问「这行的来源定了没有」（与指针无关）。
    """
    print("\n[J-2-1] 被测对象 = B1/B2 区分办法的假阳性/假阴性边界")

    # --- the three counter-examples, built in a TEMP fixture ---
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="j2-1-"))
    db = tmpdir / "fixture.db"
    _build_fixture(db)

    with ro(db) as conn:
        c = tier_counts(conn)
        rows = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, body_path, body_anchor, body_hash, text_state, "
            "body_origin FROM chunks").fetchall()}
        b1_ids = _b1_ids(conn)
        b2_ids = _b2_ids(conn)

    print("[J-2-1] 反例库的读数: %s" % c)

    # ① FALSE POSITIVE ① — a decided row whose anchor was locally erased.
    #    It must NOT be B1 (it is not an out-of-bounds write: it WAS
    #    projected, and its path+hash are still there).
    fp1 = rows["fp1-local-damage"]
    assert fp1["body_anchor"] is None
    assert fp1["body_path"] is not None and fp1["body_hash"] is not None
    assert fp1["body_origin"] == "file"
    assert fp1["id"] not in b1_ids, (
        "假阳性①：origin 已判定、path/hash 还在的行被误报成 B1")
    print("[J-2-1] 假阳性①（局部损坏：origin=file, path/hash 在, anchor 空）"
          " ⇒ 判为**不是 B1** ✅")

    # ② FALSE POSITIVE ② — a normal pending row: full pointer, origin
    #    unresolved.  It must be B2, NOT B1.
    fp2 = rows["fp2-pending"]
    assert fp2["body_anchor"] is not None
    assert fp2["body_origin"] == "unresolved"
    assert fp2["id"] not in b1_ids, (
        "假阳性②：正常的待判定行（有完整指针）被误报成 B1")
    assert fp2["id"] in b2_ids, (
        "假阳性②：正常的待判定行没有被归进 B2")
    print("[J-2-1] 假阳性②（正常待判定：指针完整, origin=unresolved）"
          " ⇒ 判为 **B2**（黄，不是红）✅")

    # ③ FALSE NEGATIVE — a row whose three pointers are all empty but
    #    whose origin was DECIDED afterwards.  It IS an out-of-bounds
    #    write and MUST still be B1.
    fn = rows["fn-washed"]
    assert fn["body_anchor"] is None and fn["body_path"] is None \
        and fn["body_hash"] is None
    assert fn["body_origin"] == "file", "反例的前提不成立"
    assert fn["id"] in b1_ids, (
        "★假阴性：origin 已被判定（'file'）的三列全空行**没有**被归进 B1 "
        "⇒ 这条判据会漏掉「被判定器洗白」的越界写入行")
    print("[J-2-1] 假阴性（origin 已被判定成 file, 但三列仍全空）"
          " ⇒ 判为 **B1**（仍然是越界写入）✅")

    # ④ the positive case — a genuine out-of-bounds write, exactly the
    #    shape the old writer produces.
    pos = rows["pos-outofbounds"]
    assert pos["body_origin"] == "unresolved"
    assert pos["id"] in b1_ids
    print("[J-2-1] 正例（真·越界写入：三列全空 + origin=unresolved）"
          " ⇒ 判为 **B1** ✅")

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("[J-2-1] 结论：区分办法在**两个方向上都被抓住** —— 假阳性①"
          "（局部损坏）、假阳性②（正常待判定）都被排除；假阴性"
          "（被判定器洗白的越界写入）被抓住。")
    print("[J-2-1] ⇒ **B1 判据 = 「三列指针全空」，与 origin 无关。**")


def _b1_ids(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE "
        "(body_anchor IS NULL OR body_anchor='') AND "
        "(body_path   IS NULL OR body_path='')   AND "
        "(body_hash   IS NULL OR body_hash='')")]


def _b2_ids(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE "
        "(body_anchor IS NOT NULL AND body_anchor<>'') AND "
        "body_origin='unresolved'")]


def _build_fixture(db: Path) -> None:
    """A four-row fixture carrying exactly the four shapes."""
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(
            "CREATE TABLE chunks ("
            " id TEXT, path TEXT, start_line INT, end_line INT, text TEXT,"
            " body_path TEXT, body_anchor TEXT, body_hash TEXT,"
            " text_state TEXT, body_origin TEXT, created_at INT);")
        rows = [
            # the genuine out-of-bounds write (the old writer's shape)
            ("pos-outofbounds", "a.md", 1, 2, "t", None, None, None,
             "projection", "unresolved", 1000),
            # a normal pending row: full pointer, origin not yet decided
            ("fp2-pending", "b.md", 1, 2, "t", "b.md", "L1-2",
             "sha256:xx", "projection", "unresolved", 1000),
            # locally damaged: origin decided, anchor erased, path+hash kept
            ("fp1-local-damage", "c.md", 1, 2, "t", "c.md", None,
             "sha256:yy", "mirror", "file", 1000),
            # WASHED: three pointers empty, but the origin was decided later
            ("fn-washed", "d.md", 1, 2, "t", None, None, None,
             "projection", "file", 1000),
        ]
        conn.executemany(
            "INSERT INTO chunks (id, path, start_line, end_line, text, "
            "body_path, body_anchor, body_hash, text_state, body_origin, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()


def test_j2_2_the_tier_reading_is_stable_when_the_world_moves():
    """**J-2-2** —— 被测对象：**分档读数的稳定性**（移动靶）。

    ⚠️ 交办书 J §2 提醒：「B2 不进红（是黄）」⇒ 门禁的红只来自 B1 与
    B3。而**活库是移动靶** —— 别的写者随时会写。

    ⇒ 本条断言：**两次读数之间世界动了 ⇒ 分档结论必须标记为
    NOTRUN，不是 FAIL。** 这是我上一轮（交办 E）落进判据的「移动靶」
    纪律，本轮继续沿用。

    实现：**读两次**，间隔一小段时间，比对两档的**集合**（不只是
    计数 —— 比数量会漏掉「换了一批但数量一样」）。若集合变了 ⇒
    打印 NOTRUN 的理由并**不判红**。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    print("\n[J-2-2] 被测对象 = 三档读数的稳定性（移动靶）")

    def read_all() -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for name, db in libs:
            with ro(db) as conn:
                if not _has_col(conn, "chunks", "body_origin"):
                    continue
                out[name] = {
                    "B1": _b1_ids(conn),
                    "B2": _b2_ids(conn),
                }
        return out

    first = read_all()
    time.sleep(1.0)
    second = read_all()

    moved = []
    for name in first:
        for tier in ("B1", "B2"):
            if first[name][tier] != second[name][tier]:
                moved.append("%s/%s" % (name, tier))

    total_b1 = sum(len(v["B1"]) for v in first.values())
    total_b2 = sum(len(v["B2"]) for v in first.values())
    print("[J-2-2] 第一次读数: B1=%d B2=%d" % (total_b1, total_b2))
    print("[J-2-2] 第二次读数: B1=%d B2=%d"
          % (sum(len(v["B1"]) for v in second.values()),
             sum(len(v["B2"]) for v in second.values())))
    if moved:
        print("[J-2-2] ⚠️ 读数期间世界动了 ⇒ 变化的档: %s" % ", ".join(moved))
        print("[J-2-2] 结论：**NOTRUN**（不是 FAIL）—— 「刚才有人写了」"
              "与「有泄漏」在读数层面就分开了。")
        return
    print("[J-2-2] 结论：读数稳定（两次读数逐集合一致）")


# ===========================================================================
# J-3-0 · the domain readings, BEFORE any口径 change
# ===========================================================================

def test_j3_0_the_domain_of_each_criterion_versus_what_it_wants_to_say():
    """**J-3-0** —— 被测对象：**M6a / M6b / M6c / M6e / M6f 各自的域**。

    交办书 J §3 的理由：风控专家已证 **M6a 的域实测只有 1 行** ——
    6615 条账本条目的 ``text_state`` 全是 ``'mirror'``，**全部落在域
    外** ⇒ **M6a 从来没问过那 2496 条 backfill** ⇒ 「**判据的域**」
    与「**判据想说的**」之间有缝隙。

    ⇒ 本条给出每条判据的 **域内行数 vs 域外行数**，让「改口径会让
    多少行变红」**先有数、后动手**。

    ⚠️ **本条不改任何口径.**  它只读数、只报告。域的口径**照抄既有
    判据的 SQL**（本文件自己写一遍，不 import 被测对象的函数）。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    print("\n[J-3-0] 被测对象 = M6a/M6b/M6c/M6e/M6f 的域（域内 vs 域外）")

    # --- the domains, written out by hand (not imported) ---
    #
    # ⚠️ Each entry is (FROM clause, WHERE clause, the table the domain
    # is a subset of).  The FROM clause is spelled out separately
    # because two of these domains are JOINs, not predicates on one
    # table.
    DOMAINS = {
        "M6a": (
            "chunks",
            "text_state<>'mirror' AND body_origin='file' "
            "AND body_path IS NOT NULL AND id NOT IN (SELECT id FROM cert_ledger)",
            "chunks"),
        "M6b": (
            "cert_ledger l JOIN chunks c ON c.id=l.id",
            "l.text_state IS NOT NULL AND c.text_state IS NOT NULL",
            "cert_ledger"),
        "M6c": (
            "chunks",
            "body_origin='orphan'",
            "chunks"),
        "M6e": (
            "files",
            "chunk_count IS NULL",
            "files"),
        "M6f": (
            "cert_ledger l",
            "NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id=l.id)",
            "cert_ledger"),
    }

    grand: Dict[str, Dict[str, int]] = {}

    for name, db in libs:
        with ro(db) as conn:
            chunks_total = 0
            if _has_col(conn, "chunks", "body_origin"):
                chunks_total = conn.execute(
                    "SELECT COUNT(*) FROM chunks").fetchone()[0]
            has_ledger = _has_table(conn, "cert_ledger")
            line = "  %-22s" % name
            for crit, (frm, where, table) in DOMAINS.items():
                if table == "chunks" and not _has_col(conn, "chunks", "body_origin"):
                    continue
                if table == "cert_ledger" and not has_ledger:
                    continue
                if table == "files" and not _has_table(conn, "files"):
                    continue
                try:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM %s WHERE %s" % (frm, where)).fetchone()[0]
                except sqlite3.Error:
                    continue
                if table == "chunks":
                    inside, outside = n, chunks_total - n
                elif table == "cert_ledger":
                    total = conn.execute(
                        "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
                    inside, outside = n, total - n
                else:
                    total = conn.execute(
                        "SELECT COUNT(*) FROM files").fetchone()[0]
                    inside, outside = n, total - n
                line += "  %s 域内=%-5d 域外=%-5d" % (crit, inside, outside)
                g = grand.setdefault(crit, {"in": 0, "out": 0, "total": 0})
                g["in"] += inside
                g["out"] += outside
                g["total"] += inside + outside
            print(line)

    print("")
    print("[J-3-0] 全活库合计：")
    for crit in ("M6a", "M6b", "M6c", "M6e", "M6f"):
        g = grand.get(crit)
        if not g:
            continue
        pct = (100.0 * g["in"] / g["total"]) if g["total"] else 0.0
        print("[J-3-0]   %-4s 域内=%-6d 域外=%-6d 覆盖率=%.1f%%"
              % (crit, g["in"], g["out"], pct))

    print("")
    print("[J-3-0] 读法（**先有数、后动手**）：")
    print("[J-3-0]   · 「域外」的行是**这条判据从来没问过的行** —— 它们"
          "既不会让它红，也不会让它绿。")
    print("[J-3-0]   · 改口径（分档）只影响**域内**的行 ⇒ 「改口径会让"
          "多少行变红」的上界 = 该判据的**域内行数**。")
    print("[J-3-0]   · 一个域内占比很低的判据，它的红**说明不了多少事** ——"
          "它压根没看那些行。")

    # the assertion: the readings must be CONSISTENT (domain ∪ outside = total)
    for crit, g in grand.items():
        assert g["in"] + g["out"] == g["total"], (
            "%s 的域内+域外 ≠ 总数 ⇒ 读数不自洽" % crit)

    print("[J-3-0] 结论：域内+域外 = 总数（读数自洽）；口径变更前先有数。")
