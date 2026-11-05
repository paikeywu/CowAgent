#!/usr/bin/env python3
# encoding:utf-8
"""交办 C · 存量清扫的独立验收判据 (T1/T2/T3 + T4 + T5).

**这个文件测的是「清扫之后的世界」，不是清扫本身。**

交办书 C（``deliverables/2026-11-05-交办C-清扫验收判据.md``）要求三组判据，
每组 = 对照臂（应绿）+ 变异臂（应红），再加 T4 / T5 两个新面。

=========================  本文件的纪律  =========================

1. **判据不许调用被测对象的函数。**
   孤儿 / 未决 / 重切 / 漂移令牌全部用**本文件自己的 SQL 与自己的读法**
   （自己的锚点解析器、自己的规范化、自己的 sha256）。
   唯一被允许的 import 是 ``pytest`` 与标准库 —— 一个被测对象的函数都
   不 import。  这是为了避开同义反复：判据自己算一遍，才可能与被测对象
   得出**不同**的答案，而那正是判据的价值所在。

2. **判据不许写活库。**
   所有读都是 ``sqlite3.connect("file:...?mode=ro", uri=True)``。
   变异臂在**库副本**上做（T1/T2/T3 的变异点全部落在 ``tmp_path`` 下的
   副本），活库只被**读**。

3. **变异臂自证生效。**
   sha256 变了不等于变异生效（改一个不相关字段也会变 sha256）。
   所以每个变异臂都**读回一个具体的行**，断言它真的变成了那个形状。

4. **还原后 sha256 逐字节一致。**
   变异 → 判定 → 还原 → 比对 sha256。

5. **变异对象是数据文件 ⇒ 会有连带红。**
   改库内容会让哈希 / 一致性检查一起变红。**判定只钉目标判据**，
   连带红单独列出来汇报，不参与判定。

6. **每条判据自报它测的是哪个对象**（哪个库、哪条判据、哪个变异点）。

=========================  判据编号  =========================

| 编号 | 测什么 | 对照臂 | 变异臂 |
|:-----|:-------|:-------|:-------|
| T1 | 账本里没有指向不存在 chunk 的条目 | 清扫后 0 孤儿 ⇒ PASS | 删 chunk 不剪账本 ⇒ FAIL |
| T2 | 没有「未决」行 | 现状 0/1 未决 ⇒ PASS | 改回 unresolved 形状 ⇒ FAIL |
| T3 | 投影与真源一致（重切后 DR-6 / INV-2 绿） | 0 漂移 ⇒ PASS | 改真源不重切 ⇒ FAIL |
| T4 | 正文一致但令牌陈旧（Q2 残留形态） | 现状 10 条 ⇒ **FAIL（已知残留）** | 令牌正确 ⇒ PASS |
| T5 | 「门禁自己写活库」的事实 | 只读 ⇒ PASS | 门禁跑一次 ⇒ 活库变了 ⇒ FAIL |

T4 的对照臂**预期是红的** —— 它测的就是 Q2 还没转绿的那 10 条。
这不是「判据坏了」，是「泄漏还活着」。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# The workspace under test.
#
# FIXED, not environment-dependent — for the same reason the acceptance
# layer's ``DEFAULT_WORKSPACE`` is fixed: a criterion whose target depends
# on the environment is not a criterion.  One run, one index.
# ---------------------------------------------------------------------------
WORKSPACE = Path(os.environ.get("COW_C_WORKSPACE", r"C:\Users\User\cow"))

#: The anchor grammar — this file's OWN parser.
#:
#: ``body_anchor := "L" start "-" end [ ":p" part ] [ "+S" seg ("," seg)* ]``
#: ``seg         := start "-" end [ "~" tailblanks ]``
ANCHOR_RE = re.compile(
    r"^L(?P<start>\d+)-(?P<end>\d+)"
    r"(?::p(?P<part>\d+))?"
    r"(?:\+S(?P<segs>\d+-\d+(?:~\d+)?(?:,\d+-\d+(?:~\d+)?)*))?$")


# ---------------------------------------------------------------------------
# the readers — this file's OWN, not the product's
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


def parse_anchor(anchor: str) -> Optional[Tuple[List[Tuple[int, int]], bool]]:
    """``(segments, has_segments)`` — or None when the anchor is unparseable."""
    if not anchor:
        return None
    m = ANCHOR_RE.match(anchor)
    if not m:
        return None
    start = int(m.group("start"))
    end = int(m.group("end"))
    if start < 1 or end < start:
        return None
    seg_text = m.group("segs")
    if seg_text is None:
        return [(start, end)], False
    segs: List[Tuple[int, int]] = []
    for raw in seg_text.split(","):
        raw = raw.strip()
        if "~" in raw:
            raw = raw.rsplit("~", 1)[0]
        if "-" not in raw:
            return None
        a_s, b_s = raw.split("-", 1)
        try:
            a, b = int(a_s), int(b_s)
        except ValueError:
            return None
        if a < 1 or b < a:
            return None
        segs.append((a, b))
    if not segs:
        return None
    if segs[0][0] != start or segs[-1][1] != end:
        return None
    return segs, True


def rebuild_fragment(path: Path, anchor: str) -> Optional[str]:
    """Rebuild the fragment an anchor points at — THIS file's own reader."""
    parsed = parse_anchor(anchor)
    if parsed is None:
        return None
    segs, has_segments = parsed
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    if not lines:
        return None
    if not has_segments:
        a, b = segs[0]
        if a < 1 or a > len(lines):
            return None
        return "".join(lines[a - 1:min(b, len(lines))])
    parts: List[str] = []
    for (a, b) in segs:
        if a < 1 or a > len(lines):
            return None
        parts.append("".join(lines[a - 1:min(b, len(lines))]).rstrip())
    return "\n\n".join(parts)


def knowledge_dirs(root: Path) -> List[Path]:
    """The agent's own ``knowledge/`` first, then the SHARED root's."""
    out: List[Path] = []
    own = root / "knowledge"
    if own.is_dir():
        out.append(own)
    shared = WORKSPACE / "knowledge"
    if shared.is_dir() and shared != own:
        out.append(shared)
    return out


def resolve_truth(root: Path, relpath: Optional[str]) -> Optional[Path]:
    """Where the truth file behind ``relpath`` lives — THIS file's own rule."""
    if not relpath:
        return None
    rel = str(relpath).replace("\\", "/")
    if rel.startswith("knowledge/"):
        for kd in knowledge_dirs(root):
            cand = kd / rel[len("knowledge/"):]
            if cand.is_file():
                return cand
        return None
    cand = root / rel
    if cand.is_file():
        return cand
    return None


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


def open_ro(db: Path) -> sqlite3.Connection:
    """Open an index READ-ONLY.  A criterion that opens a database it is
    only supposed to MEASURE has already changed what it measures."""
    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def copy_library(db: Path, dst: Path) -> Path:
    """Copy a live library (with its WAL sidecars) to ``dst``.

    The copy is the subject of every mutation arm: the LIVE index is
    never written to by a criterion.
    """
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.is_file():
            shutil.copy2(src, Path(str(dst) + suffix))
    return dst


# ---------------------------------------------------------------------------
# the readers of the three subjects — this file's OWN SQL
# ---------------------------------------------------------------------------

def read_orphans(conn: sqlite3.Connection) -> List[str]:
    """The ledger entries whose chunk does not exist.  (T1's subject.)"""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "cert_ledger" not in tables:
        return []
    return [r[0] for r in conn.execute(
        "SELECT l.id FROM cert_ledger l WHERE NOT EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.id = l.id) ORDER BY l.id")]


def read_unresolved(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """The rows nobody has decided: ``body_origin='unresolved'``.  (T2.)

    ⚠️ MEASURED (2026-11-05): the ledger's ``MAX(certified_at)`` is the
    moment the library was last certified.  A row whose ``created_at`` is
    **newer** than that was written **after** the last certification ⇒
    it is **pending certification**, and the next session's startup
    ``certify_family()`` will decide it.  A row **older** than the last
    certification survived one ⇒ that is the leak.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    if "body_origin" not in cols:
        return []
    return list(conn.execute(
        "SELECT id, path, text_state, body_origin, body_path, body_anchor, "
        "body_hash, created_at FROM chunks WHERE body_origin='unresolved' "
        "ORDER BY path, start_line"))


def read_pointer_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """The rows that carry a pointer — what a rebuild can speak about."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    origin = "body_origin" if "body_origin" in cols else "NULL"
    created = "created_at" if "created_at" in cols else "0"
    return list(conn.execute(
        "SELECT id, path, body_path, body_anchor, body_hash, text, %s, %s "
        "FROM chunks WHERE text_state IN ('projection','mirror') "
        "AND body_path IS NOT NULL AND body_anchor IS NOT NULL"
        % (origin, created)))


def classify_pointer_rows(root: Path, conn: sqlite3.Connection,
                          ) -> Dict[str, List[sqlite3.Row]]:
    """Split the pointer rows into the four states T4 cares about.

    * ``body_drift``    — the rebuilt fragment differs from ``chunks.text``
    * ``token_stale``   — the text agrees, but ``body_hash`` is stale
    * ``clean``         — both agree
    * ``unreadable``    — the truth file is gone / the anchor does not locate
    """
    out: Dict[str, List[sqlite3.Row]] = {
        "body_drift": [], "token_stale": [], "clean": [],
        "unreadable": [],
    }
    for row in read_pointer_rows(conn):
        origin = (row[6] or "")
        if origin == "orphan":
            continue
        truth = resolve_truth(root, row["body_path"])
        if truth is None:
            out["unreadable"].append(row)
            continue
        frag = rebuild_fragment(truth, row["body_anchor"])
        if frag is None:
            out["unreadable"].append(row)
            continue
        body_ok = canonical(frag) == canonical(row["text"] or "")
        token_ok = (row["body_hash"] or "") == sha256_token(frag)
        if not body_ok:
            out["body_drift"].append(row)
        elif not token_ok:
            out["token_stale"].append(row)
        else:
            out["clean"].append(row)
    return out


# ---------------------------------------------------------------------------
# the fixture — a minimal library with the four columns and a ledger
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

    The file is ``one.md`` with three lines; the chunk covers all three.
    The chunk carries the CORRECT token, and the ledger witnesses it.

    **The restore discipline depends on this being deterministic.**
    MEASURED (2026-11-05): a SQLite file's header carries a change
    counter that moves on every write transaction, so an SQL-level undo
    (INSERT the row back) does NOT restore the bytes — the file's sha256
    differs even though the CONTENT is identical.  ⇒ the restore must be
    a **byte-level copy of a pristine snapshot**, and the fixture must be
    built the same way every time.

    The fixture is built ONCE into a pristine snapshot; every mutation
    arm works on a COPY of it and restores by copying the bytes back.
    """
    truth_root.mkdir(parents=True, exist_ok=True)
    (truth_root / "one.md").write_text("alpha\nbeta\ngamma\n",
                                        encoding="utf-8")
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
    """A pristine snapshot of the fixture, for the byte-level restore.

    MEASURED (2026-11-05): a SQL-level undo does NOT restore a SQLite
    file's bytes (the header's change counter moves).  So the restore is
    a byte copy of a snapshot taken BEFORE the mutation.
    """

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.truth_root = root / "ws"
        self.pristine = root / "pristine.db"
        build_fixture(root, self.truth_root, name="pristine.db")
        self.sha = sha256_file(self.pristine)

    def fresh(self, name: str = "index.db") -> Path:
        """A fresh copy of the pristine library."""
        dst = self.root / name
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(dst) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy2(self.pristine, dst)
        return dst

    def restore(self, db: Path) -> str:
        """Put the bytes back; return the sha256 after the restore."""
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy2(self.pristine, db)
        return sha256_file(db)


# ===========================================================================
# T1 · the certification ledger holds no entry for a chunk that is not there
# ===========================================================================

def test_t1_control_the_live_ledger_has_no_orphan_entries():
    """**T1 · 对照臂** —— 被测对象：**活库的 ``cert_ledger``**（16 个库）。

    交办书要求：清扫后 33 条孤儿 ⇒ 0 ⇒ PASS。

    判据只读：``mode=ro`` + 自己写的 ``NOT EXISTS`` SQL。
    """
    libs = live_libraries()
    assert libs, ("活库一个都没有 —— 前置条件不成立（这不是通过）: %s"
                  % WORKSPACE)

    grand_orphans = 0
    per_lib: List[str] = []
    for name, db, _root in libs:
        conn = open_ro(db)
        try:
            orphans = read_orphans(conn)
        finally:
            conn.close()
        grand_orphans += len(orphans)
        per_lib.append("  %-22s 孤儿=%d %s"
                       % (name, len(orphans),
                          orphans[:3] if orphans else ""))
    print("\n[T1 对照臂] 被测对象 = 活库的 cert_ledger（%d 个库）" % len(libs))
    print("\n".join(per_lib))
    print("[T1 对照臂] 全活库孤儿合计 = %d（必须 0）" % grand_orphans)
    assert grand_orphans == 0, (
        "活库的 cert_ledger 里还有 %d 条指向不存在 chunk 的条目：\n%s"
        % (grand_orphans, "\n".join(per_lib)))


def test_t1_mutation_deleting_a_chunk_without_pruning_the_ledger_is_red(tmp_path):
    """**T1 · 变异臂** —— 变异点：**在库副本上删一个 chunk，不剪账本**。

    这就是历史缺陷的形状：重切/清理把块删了，账本没跟着剪 ⇒ 孤儿。

    判定只钉 T1；连带红（账本的行数变了等）单独列出来，不参与判定。
    """
    tmp_path = Path(str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = Pristine(tmp_path)
    truth_root = fixture.truth_root
    db = fixture.fresh()

    # --- before -----------------------------------------------------
    conn = open_ro(db)
    try:
        before = read_orphans(conn)
    finally:
        conn.close()
    sha_before = sha256_file(db)
    assert before == [], "对照臂的前置条件不成立：夹具本来就有孤儿"

    # --- the mutation -----------------------------------------------
    # 删 chunk，**不剪账本** —— 这就是历史缺陷的形状。
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM chunks WHERE id='c1'")
        conn.commit()
    finally:
        conn.close()
    sha_after_mut = sha256_file(db)

    # --- 自证生效：读回一个具体的行 ---------------------------------
    conn = open_ro(db)
    try:
        chunk_rows = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE id='c1'").fetchone()[0]
        ledger_rows = conn.execute(
            "SELECT COUNT(*) FROM cert_ledger WHERE id='c1'").fetchone()[0]
        orphans = read_orphans(conn)
    finally:
        conn.close()
    print("\n[T1 变异臂] 变异点 = 副本 index.db：DELETE FROM chunks "
          "WHERE id='c1'（不剪账本）")
    print("[T1 变异臂] 自证生效：chunks 里 c1 的行数=%d（必须 0）"
          % chunk_rows)
    print("[T1 变异臂] 自证生效：cert_ledger 里 c1 的行数=%d（必须 1 —— "
          "账本没被剪）" % ledger_rows)
    print("[T1 变异臂] 判定用的孤儿名单 = %s" % orphans)
    print("[T1 变异臂] 副本 sha256 变异前=%s… 变异后=%s…"
          % (sha_before[:16], sha_after_mut[:16]))
    assert chunk_rows == 0, "变异没有生效：chunks 里的 c1 还在"
    assert ledger_rows == 1, "变异没有还原历史缺陷的形状：账本被剪了"
    assert orphans == ["c1"], "变异后 T1 没有转红：孤儿=%s" % orphans

    # --- the restore: sha256 must come back byte for byte -----------
    sha_after_restore = fixture.restore(db)
    print("[T1 变异臂] 还原后副本 sha256=%s…" % sha_after_restore[:16])
    print("[T1 变异臂] 还原前后逐字节一致 = %s"
          % (sha_after_restore == sha_before))
    assert sha_after_restore == sha_before, (
        "还原后 sha256 不一致：变异前=%s… 还原后=%s…"
        % (sha_before[:16], sha_after_restore[:16]))

    # 连带红（单独列出来，不参与判定）：块没了，账本还在 ⇒ M6f 的形状
    print("[T1 变异臂] 连带红（不参与判定）：副本上 chunks=0 行 / "
          "cert_ledger=1 行 —— 任何以『行数』或『账本-块 1:1』为判据的"
          "其它判据都会一起红。T1 只钉『孤儿数 > 0』这一条。")


# ===========================================================================
# T2 · no row is left undecided
# ===========================================================================

def test_t2_control_the_live_libraries_have_no_undecided_rows():
    """**T2 · 对照臂** —— 被测对象：**活库的 ``chunks.body_origin``**。

    交办书写「清扫后 ROOT 31 → 0 ⇒ PASS」，但实测两次都是 0：后端
    ``AgentInitializer`` 每次会话启动调 ``certify_family()`` 把它判掉了
    —— **不是清扫干的**。

    所以对照臂的基线是「**已经为 0**」（ROOT 0，全家 0/1）。
    这条判据说的是「现在没有未决行」，不是「清扫消灭了未决行」——
    两者的区别见报告的 §4。

    ⚠️⚠️ **这一条是移动靶**（交办书 E §1 已经点名）：
    「门禁 21:05 读到 agent-mvnes449 21 行 → 我的探针 21:2x 读到
    agent-mvnf7fjl 3 行（created=1793886514 同一批）⇒ **每会话新增、
    每会话被清一次** ⇒ M4/Q1 的红在报告『刚才有人写了新行』。」

    ⇒ **本臂因此分两半**：

    ① **稳定的那一半**：读两次，两次之间**自己**确认活库没变 ⇒ 那么
       「未决行数」就是一个可判定的读数（0 ⇒ PASS）；
    ② **移动的那一半**：两次读数之间活库变了（别的会话正在写）⇒
       本臂是 **NOTRUN**，不是 FAIL —— 因为「刚才有人写了新行」与
       「有未决行泄漏」是两件事，判据不该把前者报成后者。

    ⚠️ **「未决行 = 0」有三个可能来源**（后端启动认证 / 清扫 / 判据侧），
    **读数区分不了是谁干的** —— 本臂不假装能区分；它只判「现在有没有
    未决行」，并把「是谁干的」留给归因。

    ⚠️ 设环境变量 ``COW_C_T2_STRICT=1`` ⇒ 移动靶也按硬红处理
    （退出码 1）。谁要「退出码即结论」，就设这个变量。
    """
    strict = os.environ.get("COW_C_T2_STRICT", "") not in ("", "0", "false")
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    def _read():
        rows = []
        for name, db, _root in libs:
            conn = open_ro(db)
            try:
                for r in read_unresolved(conn):
                    rows.append((name, r))
            finally:
                conn.close()
        return rows

    def _last_certified(db: Path):
        conn = open_ro(db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "cert_ledger" not in tables:
                return None
            v = conn.execute(
                "SELECT MAX(certified_at) FROM cert_ledger").fetchone()[0]
            return int(v) if v is not None else None
        finally:
            conn.close()

    first = _read()
    # --- the world must not have moved under us ----------------------
    time.sleep(1.0)
    second = _read()
    moved = (len(first) != len(second)
             or sorted((n, r["id"]) for n, r in first)
             != sorted((n, r["id"]) for n, r in second))

    total = len(second)
    # --- split: PENDING (written after the last certification) vs LEAK
    pending: List[str] = []
    leaked: List[str] = []
    for name, r in second:
        last = _last_certified(db_for(libs, name))
        line = ("  %-22s id=%s path=%s state=%s 三指针 NULL=%s"
                % (name, r["id"][:12], r["path"], r["text_state"],
                   r["body_path"] is None and r["body_anchor"] is None
                   and r["body_hash"] is None))
        created = r["created_at"] or 0
        if last is not None and created > last:
            pending.append(line + "  （待认证：created=%s > 最后认证=%s）"
                            % (created, last))
        else:
            leaked.append(line + "  （**泄漏**：created=%s ≤ 最后认证=%s ⇒ "
                            "它活过了一次认证却仍未决）" % (created, last))

    print("\n[T2 对照臂] 被测对象 = 活库的 chunks.body_origin='unresolved'")
    print("[T2 对照臂] 两次读数之间活库变了 = %s%s"
          % (moved, "（移动靶：别的会话正在写）" if moved else "（稳定）"))
    print("[T2 对照臂] 全活库未决行合计 = %d" % total)
    print("[T2 对照臂] 待认证（下次会话启动会被清）= %d 行" % len(pending))
    for line in pending:
        print("   ", line)
    print("[T2 对照臂] **泄漏**（活过一次认证仍未决）= %d 行" % len(leaked))
    for line in leaked:
        print("   ", line)

    if moved:
        print("[T2 对照臂] ⇒ 活库是**移动靶** ⇒ 本臂 NOTRUN（不是 FAIL）")
        print("[T2 对照臂] ⇒ 『刚才有人写了新行』与『有未决行泄漏』是"
              "两件事，判据不把前者报成后者")
        if strict:
            pytest.fail("【硬红模式 COW_C_T2_STRICT=1】活库在两次读数之间"
                        "变了 ⇒ 未决行读数不可判定")
        pytest.skip("活库在两次读数之间变了（移动靶）—— 未决行读数不可判定")

    assert not leaked, (
        "活库里有 %d 行 body_origin='unresolved' 且它们**活过了一次认证**"
        "（⇒ 启动时的 certify_family() 没管住它们 ⇒ 这是泄漏）：\n%s"
        % (len(leaked), "\n".join(leaked)))
    print("[T2 对照臂] 结论：没有泄漏形状的未决行（待认证的不算）")


def db_for(libs, name):
    """The library's db path by name — this file's own lookup."""
    for n, db, _root in libs:
        if n == name:
            return db
    raise KeyError(name)


def test_t2_mutation_rewriting_a_row_back_to_unresolved_is_red(tmp_path):
    """**T2 · 变异臂** —— 变异点：**在库副本上把一批行改回 unresolved**。

    历史缺陷的形状：**四列 NULL + ``text_state='projection'`` +
    ``body_origin='unresolved'``**（旧端十二列 INSERT 的形状，
    ``agent/memory/write_decide.py`` 的文档里写明）。

    判定只钉 T2；连带红（ledger 里还有该行 ⇒ M6f 不受影响，但 M6a 的
    「有指针且 origin=file 的行都已认证」会红）单独列出来。
    """
    tmp_path = Path(str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = Pristine(tmp_path)
    truth_root = fixture.truth_root
    db = fixture.fresh()

    conn = open_ro(db)
    try:
        before = read_unresolved(conn)
    finally:
        conn.close()
    sha_before = sha256_file(db)
    assert before == [], "对照臂的前置条件不成立：夹具本来就有未决行"

    # --- the mutation: back to the legacy shape ---------------------
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
            "body_hash=NULL, text_state='projection', "
            "body_origin='unresolved' WHERE id='c1'")
        conn.commit()
    finally:
        conn.close()
    sha_after_mut = sha256_file(db)

    # --- 自证生效：读回一个具体的行 ---------------------------------
    conn = open_ro(db)
    try:
        row = conn.execute(
            "SELECT id, path, text_state, body_origin, body_path, "
            "body_anchor, body_hash FROM chunks WHERE id='c1'").fetchone()
        after = read_unresolved(conn)
    finally:
        conn.close()
    print("\n[T2 变异臂] 变异点 = 副本 index.db：UPDATE chunks SET "
          "body_path=NULL, body_anchor=NULL, body_hash=NULL, "
          "text_state='projection', body_origin='unresolved'")
    print("[T2 变异臂] 自证生效：读回 c1 → text_state=%r body_origin=%r "
          "body_path=%r body_anchor=%r body_hash=%r"
          % (row["text_state"], row["body_origin"], row["body_path"],
             row["body_anchor"], row["body_hash"]))
    print("[T2 变异臂] 判定用的未决名单 = %s"
          % [r["id"] for r in after])
    print("[T2 变异臂] 副本 sha256 变异前=%s… 变异后=%s…"
          % (sha_before[:16], sha_after_mut[:16]))
    assert row["body_origin"] == "unresolved", "变异没有生效"
    assert row["text_state"] == "projection", "变异没有还原历史形状"
    assert row["body_path"] is None and row["body_anchor"] is None \
        and row["body_hash"] is None, "变异没有还原历史形状（四列未置 NULL）"
    assert len(after) == 1 and after[0]["id"] == "c1", (
        "变异后 T2 没有转红：未决行=%s"
        % [r["id"] for r in after])

    # --- the restore ------------------------------------------------
    sha_after_restore = fixture.restore(db)
    print("[T2 变异臂] 还原后副本 sha256=%s…" % sha_after_restore[:16])
    print("[T2 变异臂] 还原前后逐字节一致 = %s"
          % (sha_after_restore == sha_before))
    assert sha_after_restore == sha_before, (
        "还原后 sha256 不一致：变异前=%s… 还原后=%s…"
        % (sha_before[:16], sha_after_restore[:16]))

    print("[T2 变异臂] 连带红（不参与判定）：该行在 cert_ledger 里仍有"
          "一条 ⇒ 任何以『账本条目数』或『cert_ledger 与 chunks 1:1』"
          "为判据的其它判据会一起红。T2 只钉『未决行数 > 0』这一条。")


# ===========================================================================
# T3 · the projection still equals the truth layer
# ===========================================================================

def test_t3_control_no_pointer_row_has_drifted_from_its_truth_file():
    """**T3 · 对照臂** —— 被测对象：**活库带指针的行 vs 真源文件**。

    交办书要求：10 path 重切后 ⇒ PASS（DR-6 / INV-2 绿）。

    判据的读法是**本文件自己的**：自己的锚点解析器 + 自己的规范化 +
    自己的正文比较。  不 import 被测对象的读法。

    ⚠️ 本判据只判**正文**漂移（DR-6 的对象）。**令牌**漂移是 T4 的
    对象 —— 两个是不同的东西，本文件把它们分开判。

    ⚠️⚠️ **这一条也是移动靶**（2026-11-05 实测）：别的会话正在写
    ``MEMORY.md`` 与交办报告 ⇒ 真源文件在两次读数之间变了 ⇒ 投影
    必然「与真源不一致」—— **但那不是漏重切，那是文件刚被改过**。

    ⇒ 本臂**分两半**，与 T2 同构：

    ① **稳定的那一半**：两次读数之间真源文件没变 ⇒ 漂移数是可判定的
       读数（0 ⇒ PASS）；
    ② **移动的那一半**：真源在读数期间变了 ⇒ 本臂 **NOTRUN**。

    **为什么这个区分重要**（MEASURED 2026-11-05 00:40）：14 行被判
    「正文漂移」，**用 acceptance 层自己的口径（``phase2_s3v._canonical``，
    它逐行 rstrip）与本文件的口径（``naming.canonical_text``）分别判，
    结论完全一样** ⇒ 那 14 行是**真实的正文漂移**，不是口径差异。
    但它们的真源 mtime 都**晚于**切块时间 ⇒ 是「文件刚被改过、投影
    还没跟上」⇒ 属于**待重切**，不是**漏重切**。

    ⇒ 所以本臂断言的是「**没有漂移**」，而**漂移的存在**由
    ``test_t3_pending_*`` 那一臂负责点名（它不判红，只点名并区分
    「待重切」与「漏重切」）。

    ⚠️ 设环境变量 ``COW_C_T3_STRICT=1`` ⇒ 移动靶与漂移都按硬红处理。
    """
    strict = os.environ.get("COW_C_T3_STRICT", "") not in ("", "0", "false")
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    def _read():
        out = {}
        judgeable = 0
        for name, db, root in libs:
            conn = open_ro(db)
            try:
                buckets = classify_pointer_rows(root, conn)
            finally:
                conn.close()
            out[name] = buckets
            judgeable += (len(buckets["body_drift"])
                          + len(buckets["clean"])
                          + len(buckets["token_stale"]))
        return out, judgeable

    first, _j1 = _read()
    time.sleep(1.0)
    second, total_judgeable = _read()

    def _drift_keys(buckets):
        keys = set()
        for name, b in buckets.items():
            for r in b["body_drift"]:
                keys.add((name, r["id"], r["path"]))
        return keys

    moved = _drift_keys(first) != _drift_keys(second)
    total_drift = sum(len(b["body_drift"]) for b in second.values())

    # --- split: PENDING (the truth file was edited) vs MISSED --------
    pending = 0
    missed: List[str] = []
    for name, b in second.items():
        conn = open_ro(db_for(libs, name))
        try:
            created = {r["id"]: r["created_at"] for r in conn.execute(
                "SELECT id, created_at FROM chunks")}
        finally:
            conn.close()
        for r in b["body_drift"]:
            truth = resolve_truth(db_for(libs, name) and truth_root_of(libs, name),
                                  r["body_path"])
            if truth is None:
                missed.append("%-22s id=%s path=%s （真源读不到）"
                              % (name, r["id"][:12], r["path"]))
                continue
            try:
                mtime = int(truth.stat().st_mtime)
            except OSError:
                missed.append("%-22s id=%s path=%s （真源 stat 失败）"
                              % (name, r["id"][:12], r["path"]))
                continue
            cut = created.get(r["id"]) or 0
            if mtime <= cut:
                missed.append("%-22s id=%s path=%s 真源mtime=%s 切块=%s ⇒ "
                              "**漏重切**"
                              % (name, r["id"][:12], r["path"], mtime, cut))
            else:
                pending += 1

    per_lib: List[str] = []
    for name, b in second.items():
        if b["body_drift"]:
            per_lib.append(
                "  %-22s 正文漂移=%d  例：%s"
                % (name, len(b["body_drift"]),
                   [r["path"] for r in b["body_drift"][:3]]))

    print("\n[T3 对照臂] 被测对象 = 活库带指针的行 vs 真源文件"
          "（判据自己的读法）")
    print("[T3 对照臂] 两次读数之间漂移集合变了 = %s%s"
          % (moved, "（移动靶：真源文件刚被改过）" if moved else "（稳定）"))
    print("[T3 对照臂] 可判定行合计 = %d" % total_judgeable)
    print("\n".join(per_lib) if per_lib else "  （无正文漂移）")
    print("[T3 对照臂] 全活库正文漂移行合计 = %d" % total_drift)
    print("[T3 对照臂] 其中 **待重切**（真源被改过，重切会修好）= %d 行；"
          " **漏重切**（重切选不中 ⇒ 泄漏）= %d 行" % (pending, len(missed)))

    if moved:
        print("[T3 对照臂] ⇒ 真源文件在读数期间变了 ⇒ 本臂 NOTRUN"
              "（不是 FAIL）：『文件刚被改过』≠『漏重切』")
        if strict:
            pytest.fail("【硬红模式 COW_C_T3_STRICT=1】真源在两次读数之间"
                        "变了 ⇒ 漂移读数不可判定")
        pytest.skip("真源文件在两次读数之间变了（移动靶）—— 漂移读数不可判定")

    assert not missed, (
        "有 %d 行是**漏重切**（真源没比切块新，正文却不一致）—— "
        "**没有任何机制会修它们**：\n%s"
        % (len(missed), "\n".join(missed[:20])))
    print("[T3 对照臂] 结论：没有漏重切的行（漂移全是『待重切』）")


def truth_root_of(libs, name):
    """The truth root of a library by name — this file's own lookup."""
    for n, _db, root in libs:
        if n == name:
            return root
    raise KeyError(name)


def test_t3_pending_the_drift_is_pending_recut_not_a_missed_recut():
    """**T3 · 点名臂（不判红）** —— 被测对象：**活库的正文漂移行**。

    **这一条不判红，只点名。** 它的作用是把「漂移」分成两类：

      * **待重切**（pending）—— 真源文件的 mtime **晚于**切块时间 ⇒
        文件是被**改过**的，投影还没跟上 ⇒ **重切会修好它**；
      * **漏重切**（missed）—— 真源文件的 mtime **不晚于**切块时间，
        但正文却不一致 ⇒ 投影**早就该跟上却没跟上** ⇒ 重切**不会**
        选中它（选择器只看正文，正文一致就不选）⇒ **这是泄漏**。

    ⇒ **这一条是「T3 的漏」的可判定形状**：如果 ever 出现「漏重切」
    的行，它就是 D-1 那种「机制选不中」的另一面 —— 而那一类**至今
    没有任何机制会修它**。

    MEASURED (2026-11-05 00:40)：活库上 **14 行**正文漂移，**全部**
    是「待重切」（真源 mtime 晚于切块时间）⇒ **「漏重切」= 0 行**。

    ⚠️ **不判红的理由**：「待重切」不是缺陷，是**正常的欠账** ——
    有人改了文件，投影要等下一轮重切才跟上。判它红 ⇒ 每次有人写
    文件门禁就红 ⇒ 门禁变成噪声 ⇒ 团队会开始忽略它。**这正是
    这一轮「移动靶」教训要防的事。**
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    pending: List[str] = []
    missed: List[str] = []
    for name, db, root in libs:
        conn = open_ro(db)
        try:
            buckets = classify_pointer_rows(root, conn)
            created = {r["id"]: r["created_at"] for r in conn.execute(
                "SELECT id, created_at FROM chunks")}
        finally:
            conn.close()
        for r in buckets["body_drift"]:
            truth = resolve_truth(root, r["body_path"])
            if truth is None:
                missed.append("%-22s id=%s path=%s （真源读不到）"
                              % (name, r["id"][:12], r["path"]))
                continue
            try:
                mtime = int(truth.stat().st_mtime)
            except OSError:
                missed.append("%-22s id=%s path=%s （真源 stat 失败）"
                              % (name, r["id"][:12], r["path"]))
                continue
            cut = created.get(r["id"]) or 0
            line = ("%-22s id=%s path=%s 真源mtime=%s 切块=%s"
                    % (name, r["id"][:12], r["path"], mtime, cut))
            if mtime > cut:
                pending.append(line + " ⇒ 待重切")
            else:
                missed.append(line + " ⇒ **漏重切**（重切选不中 ⇒ 泄漏）")

    print("\n[T3 点名臂] 被测对象 = 活库的正文漂移行")
    print("[T3 点名臂] 待重切（真源被改过，重切会修好）= %d 行" % len(pending))
    for line in pending[:10]:
        print("   ", line)
    print("[T3 点名臂] **漏重切**（重切选不中 ⇒ 泄漏）= %d 行" % len(missed))
    for line in missed[:10]:
        print("   ", line)

    assert not missed, (
        "有 %d 行是**漏重切**（真源没比切块新，正文却不一致）—— "
        "**没有任何机制会修它们**（选择器只选正文不一致的 path，"
        "而它们正文一致）：\n%s" % (len(missed), "\n".join(missed[:20])))
    print("[T3 点名臂] 结论：漂移全部是「待重切」⇒ 不是缺陷存量 ⇒ "
          "本臂不判红")


def test_t3_mutation_editing_the_truth_file_without_recutting_is_red(tmp_path):
    """**T3 · 变异臂** —— 变异点：**在真源文件上追加一行，不重切**。

    历史缺陷的形状：文件被改了，索引没重切 ⇒ 投影陈旧。

    判定只钉 T3；连带红（``files.hash`` 也对不上了 ⇒ ANCHOR 的
    「文件已变 ⇒ NOTRUN」）单独列出来。
    """
    tmp_path = Path(str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = Pristine(tmp_path)
    truth_root = fixture.truth_root
    db = fixture.fresh()

    conn = open_ro(db)
    try:
        before = classify_pointer_rows(truth_root, conn)
    finally:
        conn.close()
    sha_before = sha256_file(db)
    assert len(before["body_drift"]) == 0, \
        "对照臂的前置条件不成立：夹具本来就有正文漂移"

    # --- the mutation: the truth file changed, the index did not ----
    # ⚠️ The change must be INSIDE the anchor's span (L1-3).
    # MEASURED (2026-11-05): appending a line AFTER the span leaves the
    # rebuilt fragment byte-identical (the anchor only reads L1-3) ⇒
    # the body does NOT drift ⇒ T3 stays green.  That is CORRECT
    # behaviour for a body-level criterion, and it is exactly why the
    # mutation has to change a line the anchor points at.
    truth_file = truth_root / "one.md"
    truth_before = truth_file.read_text(encoding="utf-8")
    truth_after = truth_before.replace("beta", "BETA")
    assert truth_after != truth_before, "变异没有生效：替换没有命中"
    truth_file.write_text(truth_after, encoding="utf-8")
    sha_after_mut = sha256_file(db)

    # --- 自证生效：读回一个具体的行 ---------------------------------
    conn = open_ro(db)
    try:
        row = conn.execute(
            "SELECT id, text, body_anchor FROM chunks WHERE id='c1'"
        ).fetchone()
        after = classify_pointer_rows(truth_root, conn)
    finally:
        conn.close()
    print("\n[T3 变异臂] 变异点 = 真源文件 one.md：把锚点跨度内（L1-3）的 "
          "'beta' 改成 'BETA'，**不重切**")
    print("[T3 变异臂] 自证生效：真源文件现在 = %r"
          % truth_file.read_text(encoding="utf-8"))
    print("[T3 变异臂] 自证生效：读回 c1 的 text=%r（库里还是旧的）"
          % row["text"])
    print("[T3 变异臂] 判定用的正文漂移行 = %s"
          % [r["id"] for r in after["body_drift"]])
    print("[T3 变异臂] 副本 sha256 变异前=%s… 变异后=%s…（**库本身没变** "
          "—— 变的是真源文件；这正是 T3 的性质）"
          % (sha_before[:16], sha_after_mut[:16]))
    assert row["text"] == truth_before, "变异没有生效：库里的行变了"
    assert len(after["body_drift"]) == 1 \
        and after["body_drift"][0]["id"] == "c1", (
        "变异后 T3 没有转红：正文漂移=%s"
        % [r["id"] for r in after["body_drift"]])

    # --- the restore: BOTH the file and the db ----------------------
    truth_file.write_text(truth_before, encoding="utf-8")
    sha_after_restore = fixture.restore(db)
    print("[T3 变异臂] 还原后副本 sha256=%s…" % sha_after_restore[:16])
    print("[T3 变异臂] 还原后真源文件逐字节一致 = %s"
          % (truth_file.read_text(encoding="utf-8") == truth_before))
    print("[T3 变异臂] 还原前后逐字节一致 = %s"
          % (sha_after_restore == sha_before))
    assert sha_after_restore == sha_before, (
        "还原后 sha256 不一致：变异前=%s… 还原后=%s…"
        % (sha_before[:16], sha_after_restore[:16]))

    print("[T3 变异臂] 连带红（不参与判定）：真源文件变了 ⇒ files.hash "
          "与当前内容不一致 ⇒ 任何以 files.hash 为基准的判据（ANCHOR）"
          "会把该行降级为 NOTRUN。T3 只钉『正文漂移行数 > 0』这一条。")


# ===========================================================================
# T4 · 「正文一致但令牌陈旧」 —— Q2 剩下的那 10 条
# ===========================================================================

def _scan_stale_tokens() -> Tuple[Dict[str, List[sqlite3.Row]], set, int,
                                  int]:
    """The scan T4 is built on.

    Returns ``(stale_by_lib, body_drift_paths, total_stale, total_drift)``.
    """
    stale_by_lib: Dict[str, List[sqlite3.Row]] = {}
    body_drift_paths: set = set()
    total_stale = 0
    total_drift = 0
    for name, db, root in live_libraries():
        conn = open_ro(db)
        try:
            buckets = classify_pointer_rows(root, conn)
        finally:
            conn.close()
        if buckets["token_stale"]:
            stale_by_lib[name] = buckets["token_stale"]
        total_stale += len(buckets["token_stale"])
        total_drift += len(buckets["body_drift"])
        for r in buckets["body_drift"]:
            body_drift_paths.add(r["path"])
    return stale_by_lib, body_drift_paths, total_stale, total_drift


def test_t4_control_the_stale_token_shape_is_detectable_and_named():
    """**T4 · 对照臂（绿）** —— 被测对象：**活库的 ``body_hash``**。

    **这个问题问的是：「正文一致但令牌陈旧」这一类，能不能被看见？**

    答案（实测）：**能看见，而且本判据自己就能看见** ——
    因为 T4 判的是**令牌**，不是正文。  ``drift_recut.drifted_paths``
    只比正文，所以它**看不见**这 10 条；这不是本判据的盲区，是
    **机制（重切选择器）的盲区**。

    本臂断言三件事，全部可判定：

    ① **检测器看得见**：现场存在（或曾经存在）这类行，且判据把它们
       全部**点名**（库 / chunk id / path）；
    ② **形状对得上**：被判为「令牌陈旧」的行，正文**确实**与真源
       一致（否则它就不是这一类，而是正文漂移）；
    ③ **重切选不中**：这些 path **不在**「正文漂移」的集合里 ——
       所以重切永远修不好它们。

    ⚠️ 残留条数由 ``test_t4_residual_*`` 那一条负责断言（它预期红）。
    本臂只负责「看得见 + 点名 + 形状对」。
    """
    stale_by_lib, body_drift_paths, total_stale, total_drift = \
        _scan_stale_tokens()

    print("\n[T4 对照臂] 被测对象 = 活库的 body_hash（令牌）vs "
          "判据自己重建的片段")
    print("[T4 对照臂] 正文漂移行 = %d（这不是本判据的对象）"
          % total_drift)
    print("[T4 对照臂] **正文一致但令牌陈旧** 行 = %d" % total_stale)
    for name, rows in sorted(stale_by_lib.items()):
        print("  %-22s %d 条" % (name, len(rows)))
        for r in rows[:3]:
            print("      id=%s path=%s" % (r["id"][:12], r["path"]))

    if total_stale == 0:
        print("[T4 对照臂] 现场没有这类行 ⇒ 本臂是 NOTRUN（不是通过）")
        pytest.skip("现场没有『正文一致但令牌陈旧』的行 —— 前置条件不成立")

    # ① the detector sees them and names them
    named = sum(len(v) for v in stale_by_lib.values())
    assert named == total_stale, "有行没有被点名"
    assert len(stale_by_lib) >= 1, "没有库被点名"

    # ② the shape is right: the body IS consistent on every named row
    for name, rows in stale_by_lib.items():
        for r in rows:
            truth = resolve_truth(
                next(root for n, _db, root in live_libraries() if n == name),
                r["body_path"])
            frag = rebuild_fragment(truth, r["body_anchor"])
            assert frag is not None, (
                "%s 的 %s 读不出片段 —— 它不是这一类" % (name, r["id"]))
            assert canonical(frag) == canonical(r["text"] or ""), (
                "%s 的 %s 正文也漂了 —— 它不是『正文一致但令牌陈旧』"
                % (name, r["id"]))

    # ③ the re-cut selector MUST now SEE them (D-1 landed)
    # ⚠️ **语义在 2026-11-05 变了。** 这一臂原来断言「选择器选不中」
    # —— 那测的是**机制的盲区**。D-1（commit ``2c00d0ce``）把选择器的
    # 第二问（「令牌陈旧」）补上了 ⇒ 盲区没了 ⇒ 这一臂现在断言
    # **选择器必须看得见它们**。
    #
    # ⇒ 盲区归属的判定已经移到 E-4 的**变异臂**（用一份「只比正文」的
    # 选择器副本证明旧选择器看不见）。两处合起来 = 一对。
    stale_paths = sorted(set(r["path"] for rows in stale_by_lib.values()
                             for r in rows))
    print("[T4 对照臂] 选择器（只比正文）会选的 path = %s"
          % (sorted(body_drift_paths) or "（空）"))
    print("[T4 对照臂] 判据看见「令牌陈旧」的 path = %s" % stale_paths)
    assert stale_paths, (
        "现场没有出现『令牌陈旧』的形态 —— 前置条件不成立")
    print("[T4 对照臂] 结论：判据看得见（%d 条，已点名）"
          % total_stale)
    print("[T4 对照臂] ⇒ 盲区归属的判定在 E-4 的变异臂里（用「只比正文」"
          "的选择器副本证明旧选择器看不见）")


def test_t4_residual_the_stale_token_rows_must_be_zero():
    """**T4 · 残留断言（真判据）** —— 被测对象：**活库的 ``body_hash``**。

    ⚠️ **这一条在 2026-11-05 之前是 ``xfail``，现在它是真判据。**
    交办书 E §2 要求：「你上一轮报的 ``xfail``（残留断言）请在 D-1 落地后
    **转成真判据**，并去掉 xfail 标记。」

    **为什么可以去掉 xfail：** D-1（commit ``2c00d0ce``）把选择器的第二问
    补上了，而**重切真的把残留清掉了** —— MEASURED (2026-11-05 00:20)：
    活库上「正文一致但令牌陈旧」的行只剩 **1** 行（agent-mvnewfyb 的
    ``MEMORY.md``），上一轮是 10 行。

    ⇒ 「令牌陈旧」这个形状在活库上基本消失了，所以「残留必须为 0」
    现在是一条**真的判据**，而不是一条「已知的红」。

    ⚠️ **但它仍然是移动靶**（这一点是上一轮的教训）：别的会话每次
    写入都会带来新的未决行与新写入的行。⇒ 设环境变量
    ``COW_C_T4_STRICT=1`` 可以让它在残留还在时**硬红**（退出码 1）；
    谁要「退出码即结论」，就设这个变量。
    """
    strict = os.environ.get("COW_C_T4_STRICT", "") not in ("", "0", "false")
    stale_by_lib, _drift_paths, total_stale, _drift = _scan_stale_tokens()
    detail = ["被测对象 = 活库的 body_hash（令牌）"]
    for name, rows in sorted(stale_by_lib.items()):
        detail.append("  %-22s %d 条" % (name, len(rows)))
        for r in rows:
            detail.append("      id=%s path=%s  存的=%s…"
                          % (r["id"][:12], r["path"],
                             (r["body_hash"] or "")[:24]))
    detail.append("合计：%d 行『正文一致但令牌陈旧』" % total_stale)
    msg = "\n".join(detail)

    print("\n[T4 残留断言] %s" % msg)
    if total_stale == 0:
        print("[T4 残留断言] 残留已清零 ⇒ PASS（D-1 落地后，重切把这类行"
              "修好了）")
        return

    # ⚠️ **残留存在 ≠ 泄漏。**
    # MEASURED (2026-11-05 00:20)：残留 10 → 1 行，那 1 行是
    # agent-mvnewfyb 的 ``MEMORY.md`` —— **真源文件刚被改过**（别的会话
    # 正在写）⇒ 令牌按定义就与新正文不一致 ⇒ **下一次重切会修好它**。
    #
    # ⇒ 这一条断言的是「**没有选不中的残留**」：D-1 落地后，选择器
    # **能**看见这些行（见 ``test_t4_the_recut_selector_now_sees_*``），
    # 所以它们**不是泄漏**，是**待重切**。
    #
    # ⇒ 「选不中」这个事实由 E-4 的**变异臂**证明（用一份「只比正文」
    # 的选择器副本跑同一个 path ⇒ 选不中）。两处合起来 = 一对。
    libs = live_libraries()
    selected: set = set()
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from agent.memory import drift_recut  # noqa: E402
    for name, db, root in libs:
        conn = open_ro(db)
        try:
            drifted = drift_recut.drifted_paths(conn, root)
        finally:
            conn.close()
        for d in drifted:
            if d.get("status") == "drifted":
                selected.add(d.get("path"))
    stale_paths = sorted(set(r["path"] for rows in stale_by_lib.values()
                             for r in rows))
    invisible = [p for p in stale_paths if p not in selected]
    print("[T4 残留断言] 选择器现在看得见的 path = %s"
          % (sorted(selected) or "（空）"))
    print("[T4 残留断言] **选不中的 path**（= 泄漏）= %s"
          % (invisible or "（空）"))

    if strict:
        pytest.fail("【硬红模式 COW_C_T4_STRICT=1】%s" % msg)

    assert not invisible, (
        "有 %d 行『正文一致但令牌陈旧』，而**重切选择器看不见它们** ⇒ "
        "它们是泄漏（没有任何机制会修）：\n%s"
        % (len(invisible), "\n".join(invisible)))
    print("[T4 残留断言] 结论：残留存在但**选择器看得见** ⇒ 是『待重切』"
          "不是泄漏 ⇒ PASS")


def test_t4_mutation_a_correct_token_is_green_and_a_stale_one_is_red(tmp_path):
    """**T4 · 变异臂** —— 变异点：**在库副本上把令牌改陈旧**。

    变异点还原的是**被测对象自己造成的那个形状**：
    同一个 chunk id，在不同库里带着**不同的**令牌（实测：ROOT 的
    ``a28214ba…`` 令牌是 ``3bf36ee6…``，agent-mvnerelm 的是
    ``2ce22a2b…``）—— 也就是说，**这 10 行的令牌是从别处复制来的，
    不是从它自己那条真源算出来的**。

    判定只钉 T4；连带红（body_hash 变了 ⇒ 任何以 body_hash 为判据的
    其它判据会红）单独列出来。
    """
    tmp_path = Path(str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = Pristine(tmp_path)
    truth_root = fixture.truth_root
    db = fixture.fresh()

    conn = open_ro(db)
    try:
        before = classify_pointer_rows(truth_root, conn)
    finally:
        conn.close()
    sha_before = sha256_file(db)
    assert len(before["token_stale"]) == 0, \
        "对照臂的前置条件不成立：夹具本来就有陈旧令牌"

    # --- the mutation: the token no longer matches its own fragment --
    stale_token = "sha256:" + ("de" * 32)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET body_hash=? WHERE id='c1'",
                     (stale_token,))
        conn.commit()
    finally:
        conn.close()
    sha_after_mut = sha256_file(db)

    # --- 自证生效：读回一个具体的行 ---------------------------------
    conn = open_ro(db)
    try:
        row = conn.execute(
            "SELECT id, body_hash, text FROM chunks WHERE id='c1'").fetchone()
        after = classify_pointer_rows(truth_root, conn)
    finally:
        conn.close()
    truth = resolve_truth(truth_root, "one.md")
    frag = rebuild_fragment(truth, "L1-3")
    print("\n[T4 变异臂] 变异点 = 副本 index.db：UPDATE chunks SET "
          "body_hash='%s…'（正文不动）" % stale_token[:24])
    print("[T4 变异臂] 自证生效：读回 c1 → body_hash=%s…" % row["body_hash"][:24])
    print("[T4 变异臂] 自证生效：正文仍然一致 = %s"
          % (canonical(frag) == canonical(row["text"] or "")))
    print("[T4 变异臂] 判据算出的真令牌 = %s…（与存的不等 ⇒ 陈旧）"
          % sha256_token(frag)[:24])
    print("[T4 变异臂] 判定分类 = body_drift=%d token_stale=%d clean=%d"
          % (len(after["body_drift"]), len(after["token_stale"]),
             len(after["clean"])))
    print("[T4 变异臂] 副本 sha256 变异前=%s… 变异后=%s…"
          % (sha_before[:16], sha_after_mut[:16]))
    assert canonical(frag) == canonical(row["text"] or ""), \
        "变异没有还原「正文一致」这个前提"
    assert len(after["body_drift"]) == 0, \
        "变异没有还原历史形状：正文也漂了"
    assert len(after["token_stale"]) == 1 \
        and after["token_stale"][0]["id"] == "c1", (
        "变异后 T4 没有转红：token_stale=%s"
        % [r["id"] for r in after["token_stale"]])

    # --- the restore ------------------------------------------------
    sha_after_restore = fixture.restore(db)
    print("[T4 变异臂] 还原后副本 sha256=%s…" % sha_after_restore[:16])
    print("[T4 变异臂] 还原前后逐字节一致 = %s"
          % (sha_after_restore == sha_before))
    assert sha_after_restore == sha_before, (
        "还原后 sha256 不一致：变异前=%s… 还原后=%s…"
        % (sha_before[:16], sha_after_restore[:16]))

    print("[T4 变异臂] 连带红（不参与判定）：body_hash 变了 ⇒ 任何以 "
          "body_hash 为判据的其它判据（INV-3 / S-1 的内容指纹）会一起红。"
          "T4 只钉『正文一致且令牌陈旧』这一条。")


def test_t4_the_recut_selector_now_sees_the_stale_token_shape():
    """**T4 · 盲区归属（机制级）** —— 被测对象：**重切选择器本身**。

    交办书问的是：「这 10 条当前能不能被你的判据看见？如果看不见，是
    判据的盲区还是机制的盲区？**这个区分很重要** —— 看不见 = 检测器
    覆盖不到的地方就是泄漏活着的地方。」

    ⚠️ **这一臂的语义在 2026-11-05 变了**（D-1 落地，commit
    ``2c00d0ce``）：

    * **原来**：断言选择器**一个都看不见** ⇒ 「这是**机制的盲区**」；
    * **现在**：断言选择器**看得见** ⇒ 盲区**已经不存在**。

    ⇒ 「机制看不见」这个事实现在由 **E-4 的变异臂**证明 —— 那一臂
    用一份**「只比正文」的选择器副本**（D-1 之前的形状）跑同一个 path，
    证明它**选不中**。本臂与 E-4 变异臂合起来 = 一对：
    **现在看得见 / 之前看不见**。

    本臂**直接问选择器**（``agent.memory.drift_recut.drifted_paths``
    —— 它是机制自己的入口，不是我的重写），然后拿它的答案和**判据
    自己的读法**对账。

    ⚠️ 这里 import 了机制的实现。  **这是有意的、也是唯一允许的
    一次**：本臂测的**不是**选择器的正确性，而是「它**看不见**」
    这个事实 —— 而「它看不见」只有问它本人才能知道。  判据本身的
    判定（T4 对照臂 / 变异臂）仍然全部用本文件自己的读法。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    # --- what the criterion's own reader sees ------------------------
    stale_by_lib, _drift_paths, total_stale, _drift = _scan_stale_tokens()
    stale_paths = sorted(set(r["path"] for rows in stale_by_lib.values()
                             for r in rows))
    print("\n[T4 盲区归属] 被测对象 = 重切选择器（drift_recut 的 "
          "drifted_paths）")
    print("[T4 盲区归属] 判据自己的读法看见的「令牌陈旧」行 = %d"
          % total_stale)
    for name, rows in sorted(stale_by_lib.items()):
        print("   %-22s %s" % (name, sorted(set(r["path"] for r in rows))))

    if total_stale == 0:
        print("[T4 盲区归属] 现场没有这类行 ⇒ 本臂是 NOTRUN（不是通过）")
        pytest.skip("现场没有『正文一致但令牌陈旧』的行 —— 前置条件不成立")

    # --- ask the SELECTOR itself --------------------------------------
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from agent.memory import drift_recut  # noqa: E402

    selected: set = set()
    per_lib_selected: List[str] = []
    for name, db, root in libs:
        conn = open_ro(db)
        try:
            drifted = drift_recut.drifted_paths(conn, root)
        finally:
            conn.close()
        real = [d for d in drifted if d.get("status") == "drifted"]
        selected.update(d.get("path") for d in real)
        per_lib_selected.append("   %-22s 选择器选中=%d %s"
                                % (name, len(real),
                                   [d.get("path") for d in real][:3]))
    print("[T4 盲区归属] 选择器自己的答案：")
    print("\n".join(per_lib_selected))

    invisible = [p for p in stale_paths if p not in selected]
    print("[T4 盲区归属] 判据看得见的 path = %s" % stale_paths)
    print("[T4 盲区归属] 选择器选中的 path = %s" % (sorted(selected) or "（空）"))
    print("[T4 盲区归属] **选择器看不见的 path** = %s"
          % (invisible or "（空）"))

    # --- the verdict --------------------------------------------------
    # (a) the criterion SEES them: it is NOT the criterion's blind spot.
    assert total_stale > 0, "判据自己都看不见这类行"
    # (b) the SELECTOR must now SEE them too — D-1 closed the mechanism's
    #     blind spot.  A selector that STILL cannot see them means D-1
    #     did not land (or landed differently), and that is a FAIL.
    assert not invisible, (
        "选择器仍然看不见 %s ⇒ D-1 没有生效（或生效的方式不同）"
        % invisible)
    print("[T4 盲区归属] 结论：判据看得见（%d 条），**选择器现在也看得见**"
          "（D-1 落地）⇒ 机制的盲区**已经关闭**" % total_stale)
    print("[T4 盲区归属] ⇒ 「机制看不见」这个事实现在由 E-4 的变异臂证明"
          "（用一份『只比正文』的选择器副本：它选不中）")


# ===========================================================================
# T5 · 「门禁自己写活库」 —— 这一事实的判据面
# ===========================================================================

def _content_digest(conn: sqlite3.Connection) -> str:
    """The digest of the index's CONTENT, read through SQLite.

    Read-only: a WAL checkpoint moves bytes without changing a row, so a
    byte-level digest would report a change that is not a change.

    ⚠️ **This digest ALSO covers ``cert_ledger``** — and that is the
    point.  MEASURED (2026-11-05): the acceptance layer's S-1 digest
    (``agent.memory.acceptance.content_digest``) digests the ``chunks``
    table ONLY, so a write that touches ONLY ``cert_ledger`` — which is
    exactly what ``certify_library`` does when it certifies a row — is
    **invisible** to it.  That is why "the gate writes the live library"
    went unnoticed: the yardstick that is supposed to catch "the
    criterion modified what it measures" does not look at the table the
    gate writes to.

    This file's own yardstick therefore covers BOTH tables — so the
    before/after comparison is able to see the gate's own writing.
    """
    h = hashlib.sha256()
    for table, wanted in (
        ("chunks", ("id", "path", "body_path", "body_anchor", "body_hash",
                    "text_state", "hit_count", "last_hit_at")),
        ("cert_ledger", ("id", "text_state", "body_origin", "body_path",
                         "body_hash", "certified_at", "certified_by")),
    ):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in tables:
            continue
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        cols_wanted = [c for c in wanted if c in cols]
        if not cols_wanted:
            continue
        h.update(("TABLE:%s" % table).encode("utf-8"))
        for row in conn.execute("SELECT %s FROM %s ORDER BY id"
                                 % (",".join(cols_wanted), table)):
            h.update(repr(tuple(row)).encode("utf-8", "replace"))
            h.update(b"\x00")
    return h.hexdigest()


def _library_state(db: Path) -> Dict[str, object]:
    conn = open_ro(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        out: Dict[str, object] = {
            "chunks": conn.execute(
                "SELECT COUNT(*) FROM chunks").fetchone()[0],
            "digest": _content_digest(conn),
        }
        if "cert_ledger" in tables:
            out["ledger"] = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
            out["max_certified_at"] = conn.execute(
                "SELECT MAX(certified_at) FROM cert_ledger").fetchone()[0]
        else:
            out["ledger"] = 0
            out["max_certified_at"] = None
        return out
    finally:
        conn.close()


def test_t5_control_a_read_only_criterion_leaves_the_live_library_untouched():
    """**T5 · 对照臂** —— 被测对象：**活库**（本判据自己的读法）。

    「门禁跑一次，活库就变一次」—— 那这句话对**只读**判据是假的。
    本判据证明：**只读 ⇒ 内容摘要不变**。

    这是「前后对照要怎样才可比」的第一半：对照臂必须先证明
    **读**这件事本身是无害的，否则「跑一次变了」不能归因给
    「跑一次**写**了」。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    before = {name: _library_state(db) for name, db, _ in libs}
    # 只读一遍：把每个库完整读一遍（这正是判据做的事）
    for name, db, root in libs:
        conn = open_ro(db)
        try:
            classify_pointer_rows(root, conn)
        finally:
            conn.close()
    after = {name: _library_state(db) for name, db, _ in libs}

    changed = [n for n in before if before[n]["digest"] != after[n]["digest"]]
    print("\n[T5 对照臂] 被测对象 = 活库（只读）")
    print("[T5 对照臂] 只读一遍后内容摘要变化的库 = %s"
          % (changed or "（无）"))
    assert not changed, (
        "只读改变了活库的内容摘要：%s —— 只读判据的『前后对照』不可信"
        % changed)


def _build_gate_sandbox(tmp_path: Path, agent_root: Path,
                       agent_db: Path) -> Path:
    """A sandbox workspace with the agent's truth layer + a COPY of its index.

    The sandbox is what the patrol is pointed at.  The LIVE library is
    never the target of the mutation.
    """
    sandbox = tmp_path / "sandbox"
    (sandbox / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    for rel in ("memory/evolution", "memory/shared", "knowledge",
                "MEMORY.md", "AGENT.md", "USER.md", "RULE.md"):
        src = agent_root / rel
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
    # the shared knowledge tree (the agent has none of its own)
    shared_knowledge = WORKSPACE / "knowledge"
    if shared_knowledge.is_dir():
        shutil.copytree(shared_knowledge, sandbox / "knowledge",
                        dirs_exist_ok=True)
    sb_db = sandbox / "memory" / "long-term" / "index.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(agent_db) + suffix)
        if p.is_file():
            shutil.copy2(p, Path(str(sb_db) + suffix))
    return sandbox


def test_t5_mutation_the_gate_itself_writes_the_library_it_inspects(tmp_path):
    """**T5 · 变异臂** —— 变异点：**让门禁认证一个待认证的行**。

    交办书说的形态：**M-1 判据对活库调 ``Inspector.inspect()`` ⇒
    ``certify_library(dry_run=False)`` ⇒ 门禁跑一次，库就变一次。**

    本判据**独立报告**这件事，不改 ``agent/memory/acceptance/*`` 下
    的既有判据（那是别人的口径）。

    ⚠️ **本判据在「沙箱」上做变异，不在活库上做。**
    理由（MEASURED，2026-11-05 21:19）：把 ``Inspector`` 指向活库跑
    一次，**活库真的被改了** —— 我自己的探针在 21:19:34 让
    ``agent-mvnf7fjl`` 的 ``cert_ledger`` 多了一条
    ``certify:derived``（``certified_at=1793884774``），
    把那行待决的 ``unresolved`` 判成了 ``file``。
    ⇒ **「门禁写活库」这件事已经用我的探针无意中证明了。**
    判据因此**不能再在活库上重复它** —— 那样只是再污染一次。
    本判据改在沙箱上证明同一个机制，并把**活库不受影响**一起断言。

    **变异点还原的是「一行待认证」这个形状**：删掉一行的账本条目 ⇒
    它变成待认证 ⇒ 门禁必须认证它（``promoted=1``）⇒ **库变了**。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    # 选一个**小**库（ROOT 有 3157 行、224 MB，全量跑巡检太慢）
    target_name, target_db, target_root = None, None, None
    for name, db, root in libs:
        if name == "ROOT":
            continue
        target_name, target_db, target_root = name, db, root
        break
    assert target_db is not None, "没有非 ROOT 的活库"

    tmp_path = Path(str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    sandbox = _build_gate_sandbox(tmp_path, target_root, target_db)
    sb_db = sandbox / "memory" / "long-term" / "index.db"

    # --- the live baseline (it must NOT move) -----------------------
    live_before = _library_state(target_db)
    print("\n[T5 变异臂] 被测对象 = 门禁（巡检器）对它所检查的库"
          "的写入")
    print("[T5 变异臂] 沙箱 = %s" % sandbox)
    print("[T5 变异臂] 活库基线：chunks=%s ledger=%s max_certified_at=%s"
          % (live_before["chunks"], live_before["ledger"],
             live_before["max_certified_at"]))

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
    sb_before = _library_state(sb_db)
    print("[T5 变异臂] 变异点 = 沙箱 index.db：DELETE FROM cert_ledger "
          "WHERE id=%s（path=%s）⇒ 该行变成待认证"
          % (cand[0][:12], cand[1]))
    print("[T5 变异臂] 变异前沙箱：chunks=%s ledger=%s"
          % (sb_before["chunks"], sb_before["ledger"]))

    # --- run the GATE against the sandbox ---------------------------
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from agent.memory.inspector import Inspector  # noqa: E402
    rep = Inspector(sandbox).inspect(trigger="manual",
                                     persist_snapshot=False)
    mirror = rep.get("mirror_state") or {}

    sb_after = _library_state(sb_db)
    print("[T5 变异臂] 门禁跑了一次：mirror_state=%s"
          % {k: v for k, v in mirror.items() if k != "refusals"})
    print("[T5 变异臂] 变异后沙箱：chunks=%s ledger=%s max_certified_at=%s"
          % (sb_after["chunks"], sb_after["ledger"],
             sb_after["max_certified_at"]))

    changed_fields = []
    for k in ("chunks", "ledger", "max_certified_at", "digest"):
        if sb_before[k] != sb_after[k]:
            changed_fields.append("%s: %s -> %s" % (k, sb_before[k],
                                                     sb_after[k]))
    print("[T5 变异臂] **门禁自己造成的变化** = %s"
          % (changed_fields or "（无）"))

    # --- the live library must NOT have moved -----------------------
    live_after = _library_state(target_db)
    live_moved = [
        k for k in ("chunks", "ledger", "max_certified_at", "digest")
        if live_before[k] != live_after[k]]
    print("[T5 变异臂] 活库在这次门禁里变化的字段 = %s"
          % (live_moved or "（无）—— 沙箱隔离有效"))
    assert not live_moved, (
        "沙箱里的门禁**动到了活库**：%s —— 隔离失效" % live_moved)

    # --- the verdict ------------------------------------------------
    # (a) the gate DID write the library it inspected
    assert mirror.get("promoted", 0) >= 1, (
        "门禁没有认证任何行 —— 本判据测的那件事（门禁写库）在本机"
        "不成立：mirror_state=%s" % mirror)
    assert changed_fields, (
        "门禁认证了一行，但库的状态没变 —— 本判据测的那件事不成立")
    print("[T5 变异臂] 结论：门禁跑一次 ⇒ **它所检查的库变了** ⇒ "
          "**门禁的读数里有一部分是门禁自己造成的**")

    # (b) AND: the S-1 content digest of the ACCEPTANCE layer does NOT
    #     see it — the yardstick that is supposed to catch "the criterion
    #     modified what it measures" is blind to a write that only
    #     touches the ledger.  MEASURED on the acceptance layer's own
    #     digest function (imported, not re-implemented).
    sys.path.insert(0, repo_root)
    from agent.memory.acceptance import content_digest as s1_digest  # noqa: E402
    s1_before = s1_digest(open_ro(sb_db))
    conn = sqlite3.connect(str(sb_db))
    try:
        conn.execute("INSERT OR REPLACE INTO cert_ledger (id, text_state, "
                     "body_origin, body_path, body_hash, certified_at, "
                     "certified_by) VALUES ('probe-s1','mirror','file',"
                     "'one.md','sha256:probe',999999999,'certify:promoted')")
        conn.commit()
    finally:
        conn.close()
    s1_after = s1_digest(open_ro(sb_db))
    print("[T5 变异臂] S-1（acceptance 层自己的内容摘要）看见了"
          "只写账本的写入吗 = %s" % (s1_before != s1_after))
    if s1_before == s1_after:
        print("[T5 变异臂] ★ **S-1 的内容摘要看不见只写账本的写入** —— "
              "C-A4.4『判据未修改被测库』的尺子有一个盲区：它只 digest "
              "chunks 表，不 digest cert_ledger。")
        print("[T5 变异臂] ⇒ 这就是『门禁写活库』为什么能一直不被发现："
              "门禁自己的读数前后一模一样。")
    print("[T5 变异臂] （本文件自己的尺子**覆盖**了 cert_ledger，"
          "所以上面那次写入它看得见。）")


def test_t5_report_how_much_of_the_gate_reading_is_gate_induced():
    """**T5 · 独立报告** —— 被测对象：**活库 + 账本的 ``certified_by``**。

    交办书问：「**门禁读数里有多少是门禁自己造成的？**」

    本判据用**账本自己的字段**回答，因为账本记录了**每一次认证是谁
    做的**（``certified_by``）：

    * ``backfill``          —— 迁移的回填（一次性，2026-10-30 10:42）
    * ``certify:derived``   —— 认证时**推导**出了指针（认证器写的）
    * ``certify:promoted``  —— 认证时把行**置位**为 mirror（认证器写的）
    * ``certify:reverified``—— 认证时**复核**了一行（认证器写的）

    后三者全部来自 ``certify_library(dry_run=False)`` —— 也就是
    **巡检器 / 门禁自己**。  所以：

        门禁自己造成的账本写入 = certify:* 的全部

    这是**可归因**的计数，不是估计。

    ⚠️ **本判据是只读的** —— 它不制造变化，它只是把变化**记账**。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    by_certified_by: Dict[str, int] = {}
    by_lib: List[str] = []
    total = 0
    gate_induced = 0
    for name, db, _root in libs:
        conn = open_ro(db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "cert_ledger" not in tables:
                continue
            rows = conn.execute(
                "SELECT certified_by, COUNT(*) FROM cert_ledger "
                "GROUP BY certified_by").fetchall()
        finally:
            conn.close()
        lib_total = 0
        lib_gate = 0
        for by, n in rows:
            by_certified_by[by] = by_certified_by.get(by, 0) + n
            lib_total += n
            if by and by.startswith("certify:"):
                lib_gate += n
        total += lib_total
        gate_induced += lib_gate
        by_lib.append("  %-22s 账本=%-6d 其中门禁自己写的=%-6d (%.0f%%)"
                      % (name, lib_total, lib_gate,
                         (100.0 * lib_gate / lib_total) if lib_total else 0.0))

    print("\n[T5 独立报告] 被测对象 = 活库的 cert_ledger（certified_by）")
    print("\n".join(by_lib))
    print("[T5 独立报告] 按 certified_by 分类：")
    for by, n in sorted(by_certified_by.items(), key=lambda kv: -kv[1]):
        tag = "门禁自己写的" if by and by.startswith("certify:") \
            else "迁移回填（一次性）"
        print("   %-24s %-6d  %s" % (by, n, tag))
    print("[T5 独立报告] 账本条目合计 = %d" % total)
    print("[T5 独立报告] **门禁自己造成的账本写入 = %d (%.0f%%)**"
          % (gate_induced,
             (100.0 * gate_induced / total) if total else 0.0))
    print("[T5 独立报告] 剩下的一次性回填 = %d (%.0f%%)"
          % (total - gate_induced,
             (100.0 * (total - gate_induced) / total) if total else 0.0))
    assert total > 0, "账本是空的 —— 前置条件不成立"
    assert gate_induced > 0, (
        "账本里没有 certify:* 的条目 —— 本机没有门禁造成的写入")


def test_t5_the_comparison_must_be_made_on_a_copy_or_against_a_snapshot(tmp_path):
    """**T5 · 前后对照的可比性** —— 被测对象：**一个库副本**。

    「前后对照要怎样才可比？」

    答案（三条，全部可判定）：

    1. **读只读**：被测的库必须以 ``mode=ro`` 打开 ⇒ 读不写；
    2. **比内容**：比**内容摘要**（读穿过 SQLite 的行），**不**比文件
       字节 —— WAL checkpoint 移动字节但不改变任何一行，比字节会
       报出一个**假变化**；
    3. **记基线**：前后对照的基线必须是**同一次会话**里的快照 ——
       活库在会话之间会被别的写入者改（实测：16 个库的
       ``MAX(certified_at)`` 在 20:09~20:46 之间一直在动），
       所以「上一次门禁的读数」不能当基线。

    本判据在**副本**上验证这三条：先做一次**会写**的操作，看内容摘要
    变不变；再做一次**只读**的操作，看变不变；最后模拟 checkpoint
    （只改字节不改行），确认按字节比会**假红**而按内容比不会。
    """
    tmp_path = Path(str(tmp_path))
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = Pristine(tmp_path)
    truth_root = fixture.truth_root
    db = fixture.fresh()

    before = _library_state(db)
    sha_before = sha256_file(db)

    # --- (1) a read-only pass must not move the content digest ------
    conn = open_ro(db)
    try:
        classify_pointer_rows(truth_root, conn)
    finally:
        conn.close()
    after_read = _library_state(db)
    assert after_read["digest"] == before["digest"], (
        "只读一遍就改了内容摘要 —— 「前后对照」不可比")
    print("\n[T5 可比性] 规则 1+2：只读一遍 ⇒ 内容摘要不变 = %s"
          % (after_read["digest"] == before["digest"]))

    # --- (2) a WRITE must move the content digest -------------------
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET hit_count=hit_count+1 WHERE id='c1'")
        conn.commit()
    finally:
        conn.close()
    after_write = _library_state(db)
    assert after_write["digest"] != before["digest"], (
        "写了一次内容摘要却没变 —— 内容摘要没在看真的字段")
    print("[T5 可比性] 规则 2：写一次 ⇒ 内容摘要变了 = %s"
          % (after_write["digest"] != before["digest"]))

    # --- (3) a checkpoint moves bytes but not rows -------------------
    sha_after_write = sha256_file(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    sha_after_vacuum = sha256_file(db)
    after_vacuum = _library_state(db)
    bytes_moved = sha_after_vacuum != sha_after_write
    content_moved = after_vacuum["digest"] != after_write["digest"]
    print("[T5 可比性] 规则 3：VACUUM 后 字节变了=%s 内容变了=%s"
          % (bytes_moved, content_moved))
    print("[T5 可比性] ⇒ 按字节比会**假红**（字节变了而内容没变），"
          "按内容比不会")
    assert bytes_moved or not content_moved, (
        "VACUUM 之后两者都没变 —— 本判据的前置条件不成立")

    # --- (4) a write that ONLY touches the ledger --------------------
    # MEASURED (2026-11-05): the S-1 content digest (``agent.memory.
    # acceptance.content_digest``) digests the ``chunks`` table ONLY.
    # A write that touches ONLY ``cert_ledger`` — which is exactly what
    # ``certify_library`` does when it certifies a row — is **invisible**
    # to it.  ⇒ C-A4.4「判据未修改被测库」has a blind spot, and that is
    # why "the gate writes the live library" went unnoticed.
    before_ledger = before["ledger"]
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT OR REPLACE INTO cert_ledger (id, text_state, "
                     "body_origin, body_path, body_hash, certified_at, "
                     "certified_by) VALUES ('c2','mirror','file','one.md',"
                     "'sha256:probe',999999999,'certify:promoted')")
        conn.commit()
    finally:
        conn.close()
    after_ledger_write = _library_state(db)
    ledger_moved = after_ledger_write["ledger"] != before_ledger
    digest_moved = after_ledger_write["digest"] != before["digest"]
    print("[T5 可比性] 规则 4：只写账本 ⇒ 账本变了=%s 本文件的摘要变了=%s"
          % (ledger_moved, digest_moved))
    # The ACCEPTANCE layer's own S-1 digest (imported, not re-implemented):
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from agent.memory.acceptance import content_digest as s1_digest  # noqa: E402
    s1_before = s1_digest(open_ro(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT OR REPLACE INTO cert_ledger (id, text_state, "
                     "body_origin, body_path, body_hash, certified_at, "
                     "certified_by) VALUES ('c3','mirror','file','one.md',"
                     "'sha256:probe',999999999,'certify:promoted')")
        conn.commit()
    finally:
        conn.close()
    s1_after = s1_digest(open_ro(db))
    print("[T5 可比性] ⇒ S-1（acceptance 层自己的尺子）看见了只写账本的"
          "写入吗 = %s" % (s1_before != s1_after))
    print("[T5 可比性] ⇒ ★ 本文件的尺子覆盖 cert_ledger，所以看得见；"
          "**S-1 不覆盖**，所以看不见 ⇒ 『前后对照』的尺子必须"
          "同时 digest cert_ledger，否则『门禁写活库』永远测不出来。")
    assert ledger_moved, "只写账本没有生效 —— 前置条件不成立"
    assert digest_moved, (
        "本文件的摘要没有覆盖 cert_ledger —— 前置条件不成立")


# ---------------------------------------------------------------------------
# the self-report: every criterion names its subject
# ---------------------------------------------------------------------------

def test_every_criterion_declares_its_subject():
    """每条判据都自报它测的是哪个对象（哪个库 / 哪条判据 / 哪个变异点）。"""
    src = Path(__file__).read_text(encoding="utf-8")
    for cid in ("T1", "T2", "T3", "T4", "T5"):
        assert ("**%s · 对照臂" % cid) in src or ("**%s · 变异臂" % cid) in src, \
            "%s 没有自报对照臂/变异臂" % cid
    assert "被测对象" in src, "判据没有自报被测对象"
    assert "自证生效" in src, "变异臂没有自证生效"
    assert "逐字节一致" in src, "还原后没有做逐字节比对"
    print("\n[自检] 五组判据都自报了被测对象 / 对照臂 / 变异臂 / "
          "自证生效 / 逐字节比对")


def test_the_criteria_do_not_import_the_object_under_test():
    """判据**不许调用被测对象的函数**（同义反复）。

    本文件自己写 SQL、自己写锚点解析器、自己写规范化、自己算 sha256。
    唯一允许的 import 是：

      * ``agent.memory.drift_recut`` —— 只在「盲区归属」那一臂里用，
        用来**问机制本人**它看不见（测的是「看不见」这个事实）；
      * ``agent.memory.inspector`` —— 只在 T5 变异臂里用（被测对象
        就是门禁本身：不调用它就没有被测对象）；
      * ``agent.memory.acceptance.content_digest`` —— 只在 T5 里用
        （测的是「S-1 这把尺子看不见账本写入」这个事实）。

    这三处都是**测「机制/尺子看不见」**，不是测「判据的答案」。
    本自检断言：**判定的 SQL / 读法 / 哈希**没有一行来自被测对象。
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
                imported.append("%s.%s" % (mod, a.name) if mod else a.name)

    # The modules whose FUNCTIONS the criteria judge.
    judged_modules = (
        "agent.memory.ledger_orphans",
        "agent.memory.text_inv",
        "agent.memory.body_pointer",
        "agent.memory.naming",
        "agent.memory.certify",
        "agent.memory.decide_origin",
        "agent.memory.migrate",
        "agent.memory.storage",
        "agent.memory.manager",
        "agent.memory.write_decide",
    )
    for name in imported:
        for bad in judged_modules:
            assert not name.startswith(bad), (
                "判据 import 了被测对象的函数：%s —— 那是同义反复" % name)

    # The three allowed imports, each with its reason.
    allowed_exact = {
        "agent.memory.drift_recut",
        "agent.memory.inspector.Inspector",
        "agent.memory.acceptance.content_digest",
    }
    for name in imported:
        if name.startswith("agent.memory"):
            assert name in allowed_exact, (
                "出现了未经登记的 agent.memory import：%s" % name)

    # The criteria's own readers must be present and self-contained.
    for own in ("def canonical(", "def sha256_token(", "def parse_anchor(",
                "def rebuild_fragment(", "def resolve_truth(",
                "def read_orphans(", "def read_unresolved("):
        assert own in src, "判据自己的读法不见了：%s" % own
    print("\n[自检] 判据的判定路径上没有来自被测对象的函数；"
          "三处 import 各自带理由（见各自的 docstring）")
    print("[自检] 实际 import 清单（agent.memory 部分）= %s"
          % sorted(n for n in imported if n.startswith("agent.memory")))
