"""P1-阶段一（地基）：四列进库 + schema 指纹 + 守卫升级 + 前置缺陷清零。

每条判据配反向用例 —— 一条抓不到坏事的判据是装饰，不是验收。
本阶段只落地基（四列 / 指纹 / 守卫透传），重建器与回填在阶段二/三。
"""
import sqlite3
from pathlib import Path

import pytest

from agent.memory.manager import MemoryConfig
from agent.memory.storage import MemoryChunk, MemoryStorage
from agent.memory import index_guard, schema_drift


def _new_db(tmp_path):
    return MemoryStorage(db_path=tmp_path / "index.db")


# --- T1 · D-C：四列进库，写入透传 -------------------------------------------

def test_a_fresh_index_has_the_four_columns(tmp_path):
    """T1 正向：新建库的 chunks 表含四个外置列。"""
    s = _new_db(tmp_path)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(chunks)")}
    s.close()
    assert {"body_path", "body_anchor", "body_hash", "text_state"} <= cols


def test_save_chunk_defaults_text_state_to_projection(tmp_path):
    """T1：默认写入的 chunk text_state='projection'，外置三列为 NULL。"""
    s = _new_db(tmp_path)
    s.save_chunk(MemoryChunk(id="c1", user_id=None, scope="shared", source="memory",
                             path="p.md", start_line=0, end_line=3, text="hello",
                             embedding=None, hash="h"))
    row = s.conn.execute(
        "SELECT body_path, body_anchor, body_hash, text_state FROM chunks WHERE id='c1'"
    ).fetchone()
    s.close()
    assert tuple(row) == (None, None, None, "projection")


def test_save_chunks_batch_carries_the_four_columns(tmp_path):
    """T1：批量路径透传四列（扫描走的就是这条路径，漏了它等于没加）。"""
    s = _new_db(tmp_path)
    s.save_chunks_batch([MemoryChunk(
        id="c2", user_id=None, scope="shared", source="memory", path="p.md",
        start_line=0, end_line=9, text="w", embedding=None, hash="h2",
        body_path="bp", body_anchor="0:10", body_hash="bh",
        text_state="externalized")])
    row = s.conn.execute(
        "SELECT body_path, body_hash, text_state FROM chunks WHERE id='c2'"
    ).fetchone()
    s.close()
    assert tuple(row) == ("bp", "bh", "externalized")


def test_row_to_chunk_round_trips_the_four_columns(tmp_path):
    """T1：_row_to_chunk 读回四列，不丢。"""
    s = _new_db(tmp_path)
    s.save_chunk(MemoryChunk(id="c3", user_id=None, scope="shared", source="memory",
                             path="p.md", start_line=0, end_line=3, text="x",
                             embedding=None, hash="h", text_state="externalized",
                             body_path="bp", body_anchor="a", body_hash="hh"))
    back = s._row_to_chunk(
        s.conn.execute("SELECT * FROM chunks WHERE id='c3'").fetchone())
    s.close()
    assert back.text_state == "externalized" and back.body_path == "bp"


# --- T2 · D-A：单一真源 + 旧库原地迁移 -------------------------------------

def _legacy_db(path: Path):
    """一个**没有**任何 projection/externalization 列的旧 schema 库。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, "
        "source TEXT, path TEXT, start_line INTEGER, end_line INTEGER, text TEXT, "
        "embedding TEXT, hash TEXT, metadata TEXT, created_at INTEGER, "
        "updated_at INTEGER, hit_count INTEGER NOT NULL DEFAULT 0, last_hit_at INTEGER)")
    conn.commit()
    conn.close()


def test_storage_migration_reads_the_single_source_of_truth(tmp_path):
    """T2 反向用例：storage 迁移与 schema_drift 读的是**同一份**列定义。

    若有人往 ADDABLE_COLUMNS 加了一列而 storage 迁移用另一份清单，这条就会
    暴露：旧库打开后，schema_drift 眼中的「期望列」必须全部就位。
    """
    db = tmp_path / "old.db"
    _legacy_db(db)
    s = MemoryStorage(db_path=db)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(chunks)")}
    s.close()
    missing = [c for c in schema_drift.EXPECTED_CHUNK_COLUMNS if c not in cols]
    assert missing == [], f"storage 迁移后仍缺列（说明两处清单分叉）: {missing}"


def test_a_legacy_db_gets_the_four_columns_in_place(tmp_path):
    """T2 正向：无四列的旧库打开后四列原地补齐，且 text_state 无 NULL。"""
    db = tmp_path / "old.db"
    _legacy_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO chunks (id, scope, source, path, start_line, end_line, text, hash) "
                 "VALUES ('e1', 'shared', 'memory', 'p.md', 0, 2, 'x', 'h')")
    conn.commit(); conn.close()

    s = MemoryStorage(db_path=db)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(chunks)")}
    null_state = s.conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE text_state IS NULL").fetchone()[0]
    s.close()
    assert {"body_path", "body_anchor", "body_hash", "text_state"} <= cols
    assert null_state == 0


# --- T4 · M1：schema 指纹进 _meta + get_status ------------------------------

def test_schema_fingerprint_is_written_to_meta(tmp_path):
    """T4 正向：打开库后 _meta 里有 schema_fingerprint，且与重算一致。"""
    s = _new_db(tmp_path)
    row = s.conn.execute(
        "SELECT value FROM _meta WHERE key='schema_fingerprint'").fetchone()
    db = s.db_path
    s.close()
    assert row and row[0]
    assert schema_drift.schema_fingerprint(db) == row[0]


def test_schema_fingerprint_is_none_for_a_missing_db(tmp_path):
    """T4 反向用例：不存在的库指纹是 None（'未知'），不是某个具体值。

    「未知」与「已知但不对」必须可区分，否则判据会把没建库的当成 schema 错。
    """
    assert schema_drift.schema_fingerprint(tmp_path / "nope.db") is None


def test_two_differently_migrated_dbs_share_a_fingerprint(tmp_path):
    """T4：两个走同一迁移路径的库指纹一致（判据的可比性来源）。"""
    a = tmp_path / "a.db"; b = tmp_path / "b.db"
    _legacy_db(a); _legacy_db(b)
    MemoryStorage(db_path=a).close()
    MemoryStorage(db_path=b).close()
    assert schema_drift.schema_fingerprint(a) == schema_drift.schema_fingerprint(b)


# --- T5 · M2：守卫矫正行透传四列 ---------------------------------------------

def test_a_guard_corrected_row_keeps_its_text_state(tmp_path):
    """T5 反向用例：被守卫矫正的行 text_state 不得变 NULL。

    守卫的矫正 INSERT 若不透传四列，被矫正行会读回 text_state=NULL ——
    而阶段二的外置写者正是以 text_state 为键，NULL 会让它误判。
    """
    s = _new_db(tmp_path)
    # shelf 路径是守卫要拦的「排除层」；shared scope 触发矫正。
    s.conn.execute(
        "INSERT INTO chunks (id, user_id, scope, source, path, start_line, "
        "end_line, text, hash) VALUES ('g1', NULL, 'shared', 'memory', "
        "'memory/shelf/index.md', 0, 3, 'x', 'h')")
    s.conn.commit()
    row = s.conn.execute(
        "SELECT scope, text_state FROM chunks WHERE id='g1'").fetchone()
    s.close()
    assert row[0] == "sealed", "守卫应当把它矫正为 sealed"
    assert row[1] == "projection", f"矫正行的 text_state 丢了: {row[1]!r}"


def test_guard_installs_on_a_db_missing_the_four_columns(tmp_path):
    """T5：守卫在缺四列的库上能自愈安装（先补列再装触发器）。"""
    db = tmp_path / "old.db"
    _legacy_db(db)
    conn = sqlite3.connect(str(db))
    result = index_guard.ensure_guard(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    conn.close()
    assert result["status"] in ("present", "installed", "repaired")
    assert {"body_path", "body_anchor", "body_hash", "text_state"} <= cols
