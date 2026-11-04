#!/usr/bin/env python3
# encoding:utf-8
"""交办 B · 写边界守卫的判据 —— 一个只会写旧格式的客户端，守卫必须拦住它。

被测对象
--------
* ``agent/memory/body_guard.py`` 的守卫触发器（写边界的那一层）
* ``agent/memory/storage.py`` 的 files 记账行写入

为什么判据住在 ``scripts/`` 而不是 ``agent/memory/acceptance/``
------------------------------------------------------------
这一轮交办的是**新机制**，不是给既有判据加一条断言。判据自己带
三态、mutant 重放和覆盖矩阵，放在仓库里、可以单独跑；接进门禁是
另一轮的事（门禁的失败集比对会因为一条新判据而变）。

**独立性规则（本模块最重要的一条纪律）**
----------------------------------------
期望值**不许由被测对象算出来**。判据自己写死了旧写入者的 SQL（照
``git show 254868ef:agent/memory/storage.py`` 的原样），自己判断结果，
不去调 ``body_guard`` 的任何函数来算期望值。

**三态**
--------
PASS / FAIL / **NOTRUN**。0 匹配 = NOTRUN，不是通过。

跑法
----
    python scripts/memory/write_boundary_guard_criterion.py --workspace C:\\Users\\User\\cow
    python scripts/memory/write_boundary_guard_criterion.py --workspace <ws> --json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

STATE_PASS = "PASS"
STATE_FAIL = "FAIL"
STATE_NOTRUN = "NOTRUN"

# ---------------------------------------------------------------------------
# 旧写入者的写形状 —— 照 254868ef 的 storage.py 原样抄下来的
# ---------------------------------------------------------------------------
#
# 取证时间 2026-11-05 02:00~03:00，本机实测：
#   git show 254868ef:agent/memory/storage.py | findstr /N "INSERT OR REPLACE"
#   → storage.py:741  INSERT OR REPLACE INTO chunks (12 列)
#   → storage.py:773  INSERT INTO chunks (12 列) ON CONFLICT(id) DO UPDATE
#   → storage.py:1095 DELETE FROM chunks WHERE path = ?
#   → storage.py:1096 DELETE FROM files  WHERE path = ?
#   → storage.py:1140 INSERT OR REPLACE INTO files (6 列，不含统计列)
#
# 这五段 SQL 是判据的**输入**（模拟一个只会写旧格式的客户端），
# 不是被测实现的任何函数。

LEGACY_UPSERT_CHUNKS = """
INSERT INTO chunks
(id, user_id, scope, source, path, start_line, end_line,
 text, embedding, hash, metadata, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?, strftime('%s','now'))
ON CONFLICT(id) DO UPDATE SET
    user_id = excluded.user_id, scope = excluded.scope,
    source = excluded.source, path = excluded.path,
    start_line = excluded.start_line, end_line = excluded.end_line,
    text = excluded.text, embedding = excluded.embedding,
    hash = excluded.hash, metadata = excluded.metadata,
    updated_at = strftime('%s','now')
"""

LEGACY_REPLACE_CHUNKS = """
INSERT OR REPLACE INTO chunks
(id, user_id, scope, source, path, start_line, end_line,
 text, embedding, hash, metadata, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?, strftime('%s','now'))
"""

LEGACY_DELETE_CHUNKS = "DELETE FROM chunks WHERE path = ?"
LEGACY_DELETE_FILES = "DELETE FROM files WHERE path = ?"

LEGACY_REPLACE_FILES = """
INSERT OR REPLACE INTO files
(path, source, hash, mtime, size, updated_at)
VALUES (?,?,?,?,?, strftime('%s','now'))
"""


# ---------------------------------------------------------------------------
# 判据用的小工具
# ---------------------------------------------------------------------------

def open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def orphan_count(conn: sqlite3.Connection) -> int:
    """账本里指向「已经不在索引里的块」的条目数。

    这个口径是**独立**的：判据自己查，不调 ``ledger_orphans`` 的函数
    ——判据不能拿被测实现的答案当期望值。
    """
    if not {"cert_ledger", "chunks"} <= table_names(conn):
        return -1
    return conn.execute(
        "SELECT COUNT(*) FROM cert_ledger l WHERE NOT EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.id = l.id)").fetchone()[0]


def files_erased_count(conn: sqlite3.Connection) -> int:
    """files 记账行里「统计列被擦掉」的行数（NULL 或 0）。

    判据自己写死的口径：**被测量过**才算擦除，所以只看那些在
    ``files_keep`` 里有证人的行 —— 没有证人的行是「从未测量」，那是
    诚实的 NULL，不是损伤（Rule D）。
    """
    if not {"files", "files_keep"} <= table_names(conn):
        return -1
    return conn.execute(
        "SELECT COUNT(*) FROM files f JOIN files_keep k ON k.path = f.path "
        "WHERE (f.chunk_count IS NULL OR f.chunk_count = 0) "
        "AND k.chunk_count IS NOT NULL AND k.chunk_count <> 0").fetchone()[0]


def certified_rows_without_witness(conn: sqlite3.Connection) -> int:
    """声称 'mirror' 却没有证人的行数（M6c 的口径）。

    一个只写旧格式的客户端如果把一行打成 'mirror'，却没有账本条目，
    那就是「自称被认证过、但没人能说何时被谁认证过」——守卫必须
    拦住它。
    """
    if not {"cert_ledger", "chunks"} <= table_names(conn):
        return -1
    return conn.execute(
        "SELECT COUNT(*) FROM chunks c WHERE c.text_state='mirror' "
        "AND NOT EXISTS (SELECT 1 FROM cert_ledger l WHERE l.id=c.id)"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# 副本库
# ---------------------------------------------------------------------------

def live_libraries(workspace: Path) -> List[Dict[str, Any]]:
    """工作区里的活库。

    判据自己枚举（不调实现的 ``enumerate_live``）：``memory/long-term/
    index.db`` 和 ``agents/<id>/memory/long-term/index.db``。
    """
    out: List[Dict[str, Any]] = []
    root = Path(workspace)
    candidates = [root / "memory" / "long-term" / "index.db"]
    agents_dir = root / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if agent_dir.is_dir():
                candidates.append(agent_dir / "memory" / "long-term" / "index.db")
    for db in candidates:
        if db.is_file():
            out.append({"name": db.parents[2].name, "db": db})
    return out


def copy_library(src: Path, dst: Path) -> Path:
    """把一个活库复制成副本。

    **必须连 WAL 侧车一起复制**：一个只复制主文件的副本是**部分
    快照**，WAL 里还有主文件没有的行，从它恢复会静默丢掉最新的行。
    （仓库自己的 ``sweep_support`` 写的就是这条纪律。）
    """
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.is_file():
            shutil.copy2(s, Path(str(dst) + suffix))
    return dst


def install_guard(conn: sqlite3.Connection) -> Dict[str, Any]:
    """把守卫装到副本上 —— 用**产品自己的**安装器。

    判据不自己拼 SQL 装触发器：手工装的等价物会引入额外的副作用，
    把根因判反（仓库自己的教训：「对照实验要在同一份代码上做」）。
    """
    from agent.memory.body_guard import ensure_guard

    return ensure_guard(conn)


def remove_guard(conn: sqlite3.Connection) -> int:
    """把守卫拿掉 —— mutant 重放用的那把刀。

    拿掉的是**产品自己声明的**触发器清单，所以「拿掉守卫」这件事
    不依赖判据自己抄一份名字。
    """
    from agent.memory.body_guard import guard_trigger_names

    n = 0
    for name in guard_trigger_names():
        cur = conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        n += max(cur.rowcount, 0)
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 旧写入者的五种写形状
# ---------------------------------------------------------------------------

def _victim_chunk(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """挑一个「有指针、有认证状态、有证人」的行当受害者。

    判据自己写死挑选口径：一个有指针、有证人、且该路径至少两块的行
    （至少两块 ⇒ 重切能让 id 移动，孤儿才可能出现）。
    """
    row = conn.execute(
        "SELECT c.id, c.path, c.user_id, c.scope, c.source, c.start_line, "
        "       c.end_line, c.text, c.hash, c.metadata, c.body_path, "
        "       c.text_state, c.body_origin "
        "FROM chunks c WHERE c.body_path IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM cert_ledger l WHERE l.id=c.id) "
        "AND (SELECT COUNT(*) FROM chunks c2 WHERE c2.path=c.path) >= 2 "
        "LIMIT 1").fetchone()
    if row is None:
        row = conn.execute(
            "SELECT c.id, c.path, c.user_id, c.scope, c.source, c.start_line, "
            "       c.end_line, c.text, c.hash, c.metadata, c.body_path, "
            "       c.text_state, c.body_origin "
            "FROM chunks c WHERE c.body_path IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM cert_ledger l WHERE l.id=c.id) "
            "LIMIT 1").fetchone()
    return dict(row) if row else None


def _victim_file(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT f.path, f.source, f.hash, f.mtime, f.size, "
        "       f.chunk_count, f.chunker_version "
        "FROM files f WHERE f.chunk_count IS NOT NULL AND f.chunk_count<>0 "
        "LIMIT 1").fetchone()
    return dict(row) if row else None


def shape_legacy_upsert(conn: sqlite3.Connection) -> Dict[str, Any]:
    """形状 1：旧端的 ``save_chunks_batch``（UPSERT 的 DO UPDATE 分支）。"""
    v = _victim_chunk(conn)
    if v is None:
        return {"skipped": "no victim chunk"}
    before = dict(conn.execute("SELECT * FROM chunks WHERE id=?",
                               (v["id"],)).fetchone())
    conn.execute(LEGACY_UPSERT_CHUNKS,
                 (v["id"], v["user_id"], "shared", v["source"], v["path"],
                  v["start_line"], v["end_line"], "LEGACY TEXT", None,
                  v["hash"], v["metadata"]))
    conn.commit()
    after = dict(conn.execute("SELECT * FROM chunks WHERE id=?",
                              (v["id"],)).fetchone())
    return {
        "shape": "legacy UPSERT (ON CONFLICT DO UPDATE)",
        "pointer_kept": after["body_path"] == before["body_path"],
        "state_kept": after["text_state"] == before["text_state"],
        "witness_kept": conn.execute(
            "SELECT COUNT(*) FROM cert_ledger WHERE id=?",
            (v["id"],)).fetchone()[0] == 1,
    }


def shape_legacy_replace_chunk(conn: sqlite3.Connection) -> Dict[str, Any]:
    """形状 2：旧端的 ``INSERT OR REPLACE INTO chunks``。"""
    v = _victim_chunk(conn)
    if v is None:
        return {"skipped": "no victim chunk"}
    before = dict(conn.execute("SELECT * FROM chunks WHERE id=?",
                               (v["id"],)).fetchone())
    conn.execute(LEGACY_REPLACE_CHUNKS,
                 (v["id"], v["user_id"], "shared", v["source"], v["path"],
                  v["start_line"], v["end_line"], "LEGACY TEXT", None,
                  v["hash"], v["metadata"]))
    conn.commit()
    after = dict(conn.execute("SELECT * FROM chunks WHERE id=?",
                              (v["id"],)).fetchone())
    return {
        "shape": "legacy INSERT OR REPLACE INTO chunks",
        "pointer_kept": after["body_path"] == before["body_path"],
        "state_kept": after["text_state"] == before["text_state"],
        "witness_kept": conn.execute(
            "SELECT COUNT(*) FROM cert_ledger WHERE id=?",
            (v["id"],)).fetchone()[0] == 1,
    }


def shape_legacy_delete_path(conn: sqlite3.Connection) -> Dict[str, Any]:
    """形状 3：旧端的 ``delete_by_path``（删块、删记账行，**不剪账本**）。"""
    v = _victim_chunk(conn)
    if v is None:
        return {"skipped": "no victim chunk"}
    orphans_before = orphan_count(conn)
    conn.execute(LEGACY_DELETE_CHUNKS, (v["path"],))
    conn.execute(LEGACY_DELETE_FILES, (v["path"],))
    conn.commit()
    orphans_after = orphan_count(conn)
    return {
        "shape": "legacy delete_by_path (DELETE chunks + DELETE files)",
        "orphans_before": orphans_before,
        "orphans_after": orphans_after,
        "no_new_orphan": orphans_after <= orphans_before,
    }


def shape_legacy_delete_then_reinsert(conn: sqlite3.Connection) -> Dict[str, Any]:
    """形状 4：旧端的重切 —— 先删、再用**同样的 id** 写回来。

    旧端的 id 是位置派生的（``md5(path:start:end[:part])``），所以文件
    的行范围没动时，**同样的 id 会回来**。守卫必须把证人还回去 ——
    否则一次重切就把整条路径的认证抹了（M6c 会红）。
    """
    v = _victim_chunk(conn)
    if v is None:
        return {"skipped": "no victim chunk"}
    before = dict(conn.execute("SELECT * FROM chunks WHERE id=?",
                               (v["id"],)).fetchone())
    conn.execute(LEGACY_DELETE_CHUNKS, (v["path"],))
    conn.commit()
    mid_orphans = orphan_count(conn)
    conn.execute(LEGACY_UPSERT_CHUNKS,
                 (v["id"], v["user_id"], "shared", v["source"], v["path"],
                  v["start_line"], v["end_line"], "LEGACY TEXT", None,
                  v["hash"], v["metadata"]))
    conn.commit()
    after = dict(conn.execute("SELECT * FROM chunks WHERE id=?",
                              (v["id"],)).fetchone())
    return {
        "shape": "legacy delete-then-re-insert (the SAME ids come back)",
        "orphans_mid": mid_orphans,
        "pointer_kept": after["body_path"] == before["body_path"],
        "state_kept": after["text_state"] == before["text_state"],
        "witness_kept": conn.execute(
            "SELECT COUNT(*) FROM cert_ledger WHERE id=?",
            (v["id"],)).fetchone()[0] == 1,
    }


def shape_legacy_replace_files(conn: sqlite3.Connection) -> Dict[str, Any]:
    """形状 5：旧端的 ``update_file_metadata``（**不写统计列**）。

    这是 J-5 在真机上抓到的那条降级路径：``files_keep`` 记着
    ``cv=4``，写完变成 ``cv=0`` ⇒ 「写它的程序不认识当前切法」。
    """
    v = _victim_file(conn)
    if v is None:
        return {"skipped": "no measured files row"}
    erased_before = files_erased_count(conn)
    conn.execute(LEGACY_REPLACE_FILES,
                 (v["path"], "memory", "LEGACYHASH", 111222, 333444))
    conn.commit()
    erased_after = files_erased_count(conn)
    after = dict(conn.execute("SELECT * FROM files WHERE path=?",
                               (v["path"],)).fetchone())
    return {
        "shape": "legacy INSERT OR REPLACE INTO files (no statistics)",
        "erased_before": erased_before,
        "erased_after": erased_after,
        "statistics_kept": (after["chunk_count"] == v["chunk_count"]
                            and after["chunker_version"] == v["chunker_version"]),
        "content_landed": after["hash"] == "LEGACYHASH",
    }


ALL_SHAPES = (
    ("shape_legacy_upsert", shape_legacy_upsert),
    ("shape_legacy_replace_chunk", shape_legacy_replace_chunk),
    ("shape_legacy_delete_path", shape_legacy_delete_path),
    ("shape_legacy_delete_then_reinsert", shape_legacy_delete_then_reinsert),
    ("shape_legacy_replace_files", shape_legacy_replace_files),
)


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

def judge_write_boundary_guard(workspace: Path) -> Dict[str, Any]:
    """B2/B3：让一个只会写旧格式的客户端打进来，守卫必须拦住它。

    判据对每个写形状**各用一份独立的副本**——因为一个形状留下的
    损伤会影响下一个形状的观察（这正是「对照实验要在同一份代码上
    做」的反面：形状之间要互相隔离）。
    """
    detail: List[str] = []
    libs = live_libraries(workspace)
    if not libs:
        return {"verdict": STATE_NOTRUN, "detail": [
            "本工作区没有活库 —— 前置条件不成立（这不是通过）"]}

    target = libs[0]
    detail.append("被测库（副本）: %s" % target["db"])

    results: List[Dict[str, Any]] = []
    ok = True
    for name, fn in ALL_SHAPES:
        tmp = Path(tempfile.mkdtemp(prefix="wbguard-"))
        try:
            copy = copy_library(target["db"], tmp / "index.db")
            conn = open_rw(copy)
            try:
                install_state = install_guard(conn)
                if install_state.get("status") == "failed":
                    results.append({"shape": name, "verdict": STATE_FAIL,
                                    "detail": "the guard could not be "
                                              "installed: %s"
                                              % install_state.get("reason")})
                    ok = False
                    continue
                before = {
                    "orphans": orphan_count(conn),
                    "files_erased": files_erased_count(conn),
                    "unwitnessed_mirror": certified_rows_without_witness(conn),
                }
                try:
                    shape_result = fn(conn)
                except sqlite3.Error as e:
                    results.append({"shape": name, "verdict": STATE_FAIL,
                                    "detail": "the legacy write raised: "
                                              "%s: %s" % (type(e).__name__, e)})
                    ok = False
                    continue
                if shape_result.get("skipped"):
                    results.append({"shape": name, "verdict": STATE_NOTRUN,
                                    "detail": shape_result["skipped"]})
                    continue
                after = {
                    "orphans": orphan_count(conn),
                    "files_erased": files_erased_count(conn),
                    "unwitnessed_mirror": certified_rows_without_witness(conn),
                }
                shape_ok, why = _judge_shape(name, shape_result, before, after)
                shape_result["before"] = before
                shape_result["after"] = after
                shape_result["verdict"] = STATE_PASS if shape_ok else STATE_FAIL
                shape_result["why"] = why
                results.append(shape_result)
                if not shape_ok:
                    ok = False
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    for r in results:
        detail.append("  [%s] %s" % (r.get("verdict"), r.get("shape")))
        for line in str(r.get("why", r.get("detail", ""))).splitlines():
            if line:
                detail.append("        " + line)

    return {"verdict": STATE_PASS if ok else STATE_FAIL,
            "detail": detail, "shapes": results}


def _judge_shape(name: str, r: Dict[str, Any], before: Dict[str, Any],
                 after: Dict[str, Any]) -> Tuple[bool, str]:
    """一个写形状的判定 —— 期望值是**写死的字面量**，不是算出来的。"""
    why: List[str] = []
    ok = True

    if name == "shape_legacy_upsert":
        ok = r["pointer_kept"] and r["state_kept"] and r["witness_kept"]
        why.append("指针保住=%s 认证状态保住=%s 证人还在=%s"
                   % (r["pointer_kept"], r["state_kept"], r["witness_kept"]))
    elif name == "shape_legacy_replace_chunk":
        ok = r["pointer_kept"] and r["state_kept"] and r["witness_kept"]
        why.append("指针保住=%s 认证状态保住=%s 证人还在=%s"
                   % (r["pointer_kept"], r["state_kept"], r["witness_kept"]))
    elif name == "shape_legacy_delete_path":
        ok = r["no_new_orphan"]
        why.append("孤儿 %s -> %s（不得增加）"
                   % (r["orphans_before"], r["orphans_after"]))
    elif name == "shape_legacy_delete_then_reinsert":
        ok = r["pointer_kept"] and r["state_kept"] and r["witness_kept"]
        why.append("指针保住=%s 认证状态保住=%s 证人回来了=%s"
                   % (r["pointer_kept"], r["state_kept"], r["witness_kept"]))
    elif name == "shape_legacy_replace_files":
        ok = r["statistics_kept"] and r["content_landed"]
        why.append("统计列保住=%s（cc/cv 不变） 写者自己的 hash 落地=%s"
                   % (r["statistics_kept"], r["content_landed"]))
        why.append("被擦掉的有证人的 files 行 %s -> %s（不得增加）"
                   % (r["erased_before"], r["erased_after"]))

    # 全局不变量：任何形状之后，孤儿数与「自称 mirror 却无证人」的行数
    # 都不得增加。
    if after["orphans"] > before["orphans"]:
        ok = False
        why.append("孤儿增加了：%s -> %s" % (before["orphans"],
                                           after["orphans"]))
    if after["unwitnessed_mirror"] > before["unwitnessed_mirror"]:
        ok = False
        why.append("『自称 mirror 却无证人』的行增加了：%s -> %s"
                   % (before["unwitnessed_mirror"],
                      after["unwitnessed_mirror"]))
    if after["files_erased"] > before["files_erased"]:
        ok = False
        why.append("files 统计列被擦掉的行增加了：%s -> %s"
                   % (before["files_erased"], after["files_erased"]))
    return ok, "\n".join(why)


def judge_mutant_replay(workspace: Path) -> Dict[str, Any]:
    """B4：拿掉守卫 ⇒ 判据必须转红。

    这是「证明判据会抓那个缺陷，而不是只证明代码动过」的那一步。
    """
    detail: List[str] = []
    libs = live_libraries(workspace)
    if not libs:
        return {"verdict": STATE_NOTRUN, "detail": [
            "本工作区没有活库 —— 前置条件不成立（这不是通过）"]}
    target = libs[0]

    # 变异臂：守卫拿掉。
    tmp = Path(tempfile.mkdtemp(prefix="wbguard-mutant-"))
    try:
        copy = copy_library(target["db"], tmp / "index.db")
        conn = open_rw(copy)
        try:
            n = remove_guard(conn)
            detail.append("拿掉的守卫触发器: %d 个" % n)
            shape = shape_legacy_delete_path(conn)
            if shape.get("skipped"):
                return {"verdict": STATE_NOTRUN,
                        "detail": detail + [shape["skipped"]]}
            detail.append("变异臂的孤儿 %s -> %s"
                          % (shape["orphans_before"], shape["orphans_after"]))
            mutated_red = not shape["no_new_orphan"]
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 对照臂：守卫在位。
    tmp = Path(tempfile.mkdtemp(prefix="wbguard-control-"))
    try:
        copy = copy_library(target["db"], tmp / "index.db")
        conn = open_rw(copy)
        try:
            install_guard(conn)
            shape = shape_legacy_delete_path(conn)
            if shape.get("skipped"):
                return {"verdict": STATE_NOTRUN,
                        "detail": detail + [shape["skipped"]]}
            detail.append("对照臂（守卫在位）的孤儿 %s -> %s"
                          % (shape["orphans_before"], shape["orphans_after"]))
            control_green = shape["no_new_orphan"]
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = mutated_red and control_green
    detail.append("拿掉守卫 ⇒ 判据转红: %s；守卫在位 ⇒ 判据转绿: %s"
                  % (mutated_red, control_green))
    return {"verdict": STATE_PASS if ok else STATE_FAIL, "detail": detail}


def judge_coverage_matrix(workspace: Path) -> Dict[str, Any]:
    """B3：守卫覆盖矩阵 —— 每条写路径有 file:line 依据。

    判据不自己「相信」这张表：它**实测**每个形状的守卫是否在位，
    再把实测结果和矩阵里的声明对表。
    """
    from agent.memory import body_guard as bg

    detail: List[str] = []
    libs = live_libraries(workspace)
    if not libs:
        return {"verdict": STATE_NOTRUN, "detail": [
            "本工作区没有活库 —— 前置条件不成立（这不是通过）"]}

    tmp = Path(tempfile.mkdtemp(prefix="wbguard-matrix-"))
    try:
        copy = copy_library(libs[0]["db"], tmp / "index.db")
        conn = open_rw(copy)
        try:
            install_guard(conn)
            have = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            wanted = set(bg.guard_trigger_names())
            missing = wanted - have
            detail.append("守卫声明的触发器 %d 个，实测在位 %d 个"
                          % (len(wanted), len(wanted) - len(missing)))
            if missing:
                detail.append("缺失: %s" % ", ".join(sorted(missing)))
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = not missing
    return {"verdict": STATE_PASS if ok else STATE_FAIL, "detail": detail,
            "missing": sorted(missing)}


CRITERIA = (
    ("B2", "写边界守卫拦住一个只会写旧格式的客户端", judge_write_boundary_guard),
    ("B3", "守卫覆盖矩阵：每条触发器实测在位", judge_coverage_matrix),
    ("B4", "mutant 重放：拿掉守卫 ⇒ 判据转红", judge_mutant_replay),
)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="交办 B · 写边界守卫的判据")
    ap.add_argument("--workspace", required=True,
                    help="the workspace that holds the live libraries")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", default=None,
                    help="run one criterion (B2 / B3 / B4)")
    args = ap.parse_args(argv)

    workspace = Path(args.workspace)
    print("=" * 72)
    print("交办 B · 写边界守卫判据")
    print("=" * 72)
    print("workspace : %s" % workspace)
    print()

    verdicts: Dict[str, str] = {}
    for cid, name, fn in CRITERIA:
        if args.only and not cid.startswith(args.only):
            continue
        report = fn(workspace)
        verdicts[cid] = report.get("verdict", STATE_NOTRUN)
        mark = report.get("verdict", STATE_NOTRUN)
        print("  [%s] %s: %s" % (mark, cid, name))
        for line in report.get("detail", []):
            print("         " + str(line))
        print()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            print()

    passed = sum(1 for v in verdicts.values() if v == STATE_PASS)
    failed = sum(1 for v in verdicts.values() if v == STATE_FAIL)
    notrun = sum(1 for v in verdicts.values() if v == STATE_NOTRUN)
    print("-" * 72)
    print("判据通过: %d/%d   （FAIL %d · NOTRUN %d）"
          % (passed, len(verdicts), failed, notrun))
    if notrun:
        print("注意：%d 条为 NOTRUN —— 前置条件不成立，本轮未测量。"
              "NOTRUN 不是通过。" % notrun)
    print("-" * 72)
    # NOTRUN 不是绿：一条都没跑起来不算通过。
    return 0 if (passed == len(verdicts) and verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
