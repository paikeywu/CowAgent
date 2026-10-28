import json
import sqlite3
import struct
import pytest
from agent.memory import vector_backend

@pytest.mark.parametrize("numpy_enabled", [False, True])
@pytest.mark.parametrize("damaged", [b"x", "invalid JSON", '["invalid", 1]', struct.pack("2f", float("nan"), 1.0), json.dumps([10 ** 400, 1])])
def test_one_damaged_row_cannot_hide_a_healthy_match(monkeypatch, numpy_enabled, damaged):
    if numpy_enabled and vector_backend.np is None:
        pytest.skip("NumPy is optional")
    monkeypatch.setattr(vector_backend, "_HAS_NUMPY", numpy_enabled)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 这张表必须与生产 schema 一致：向量检索的可见性谓词
    # (agent/memory/visibility.py) 引用 file_sealed / element_released /
    # sealed_owner 三列。生产库由 storage.py 的 _migrate_release_columns()
    # 补齐它们 —— 测试手写的旧 schema 没有，于是 search() 报
    # "no such column: file_sealed"，把「损坏行不隐藏健康行」这条
    # 真正要验的语义整个盖掉了。
    # 列集以 agent/memory/schema_drift.py 的 EXPECTED 为准。
    conn.execute(
        "CREATE TABLE chunks ("
        "id TEXT, embedding BLOB, user_id TEXT, scope TEXT, source TEXT, path TEXT, "
        "start_line INTEGER, end_line INTEGER, text TEXT, metadata TEXT, "
        "hash TEXT, created_at TEXT, updated_at TEXT, "
        "hit_count INTEGER NOT NULL DEFAULT 0, last_hit_at TEXT, "
        "element_released INTEGER NOT NULL DEFAULT 0, "
        "file_sealed INTEGER NOT NULL DEFAULT 0, sealed_owner TEXT)"
    )
    backend = vector_backend.SQLiteVectorBackend(conn)
    for identifier, embedding in [("broken", damaged), ("healthy", backend._encode_embedding([1.0, 0.0]))]:
        conn.execute(
            "INSERT INTO chunks (id, embedding, user_id, scope, source, path, "
            "start_line, end_line, text, metadata) "
            "VALUES (?, ?, NULL, 'shared', 'memory', ?, 1, 1, ?, NULL)",
            (identifier, embedding, identifier + ".md", identifier),
        )
    try:
        matches = backend.search([1.0, 0.0], limit=1)
        assert [match.id for match in matches] == ["healthy"]
        assert matches[0].score == pytest.approx(1.0)
    finally:
        conn.close()
