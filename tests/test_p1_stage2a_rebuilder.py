"""P1-阶段二-A：投影重建器 T1 反向用例。

每条判据配一条抓得住坏事的反向用例。
"""
import hashlib
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent.memory.storage import MemoryStorage
from agent.memory import index_guard, rebuilder
from agent.memory.release_state import release_mismatches


def _ws(tmp_path):
    ws = tmp_path
    md = ws / "memory" / "markdown" / "events"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    return ws, md


def _mkfile(md, name="2026-10-29-test.md",
            fm="---\nid: e1\n---\n", body="hello world\n\nsecond para\n"):
    f = md / name
    f.write_text(fm + body, encoding="utf-8")
    return f


def _storage(ws):
    return MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")


def test_rebuild_is_idempotent(tmp_path):
    """同输入跑两次，行数/id 集合/hash 集合完全一致。"""
    ws, md = _ws(tmp_path)
    f = _mkfile(md)
    st = _storage(ws)
    r1 = rebuilder.rebuild_file(st, ws, f)
    r2 = rebuilder.rebuild_file(st, ws, f)
    st.close()
    assert r1["ids"] == r2["ids"]
    assert r1["hashes"] == r2["hashes"]
    assert r1["chunks"] == r2["chunks"]


def test_hit_stats_survive_rebuild(tmp_path):
    """E11：delete+insert 不得静默清零命中热度。"""
    ws, md = _ws(tmp_path)
    f = _mkfile(md)
    st = _storage(ws)
    r = rebuilder.rebuild_file(st, ws, f)
    st.conn.execute("UPDATE chunks SET hit_count=5 WHERE id=?", (r["ids"][0],))
    st.conn.commit()
    rebuilder.rebuild_file(st, ws, f)
    hits = st.conn.execute(
        "SELECT hit_count FROM chunks WHERE path=?", (r["path"],)).fetchall()
    st.close()
    assert 5 in [h[0] for h in hits], "hit_count was reset by the rebuild"


def test_atomic_swap_leaves_old_projection_on_failure(tmp_path):
    """单事务硬约束：swap 中途失败 → 旧投影完整保留。"""
    ws, md = _ws(tmp_path)
    f = _mkfile(md)
    st = _storage(ws)
    r = rebuilder.rebuild_file(st, ws, f)
    before = st.conn.execute(
        "SELECT id, hash FROM chunks WHERE path=? ORDER BY id", (r["path"],)
    ).fetchall()

    def boom(*a, **k):
        raise RuntimeError("injected swap failure")
    real = st.replace_file_chunks
    st.replace_file_chunks = boom
    with pytest.raises(Exception):
        rebuilder.rebuild_file(st, ws, f)
    st.replace_file_chunks = real
    after = st.conn.execute(
        "SELECT id, hash FROM chunks WHERE path=? ORDER BY id", (r["path"],)
    ).fetchall()
    st.close()
    assert before == after, "old projection was clobbered by a failed swap"
    assert (ws / ".audit" / "rebuild_failures.jsonl").is_file(), \
        "failed rebuild left no .audit/ marker"


def test_body_path_is_posix_and_relative(tmp_path):
    """INV-11：body_path POSIX、相对、无反斜杠。"""
    ws, md = _ws(tmp_path)
    f = _mkfile(md)
    st = _storage(ws)
    rebuilder.rebuild_file(st, ws, f)
    bp = st.conn.execute(
        "SELECT DISTINCT body_path FROM chunks WHERE path=?",
        ("memory/markdown/events/2026-10-29-test.md",)).fetchone()[0]
    st.close()
    assert "\\" not in bp and not bp.startswith("/") and ".." not in bp.split("/")


def test_release_mismatch_detected_and_tombstone_wins(tmp_path):
    """T1-4：墓碑释放但 front-matter 未标 → 报红，且以墓碑为准。"""
    ws, md = _ws(tmp_path)
    f = _mkfile(md, fm="---\nid: e1\nstatus: active\n---\n", body="secret\n")
    tb = ws / "memory" / "markdown" / "_tombstones"
    tb.mkdir(parents=True)
    (tb / "e1.tombstone.md").write_text(
        "---\noriginal_path: memory/markdown/events/2026-10-29-test.md\n---\n",
        encoding="utf-8")

    mm = release_mismatches(ws)
    assert mm["tombstone_only"] == ["memory/markdown/events/2026-10-29-test.md"]
    assert mm["frontmatter_only"] == []

    st = _storage(ws)
    r = rebuilder.rebuild_file(st, ws, f)
    assert r["release_mismatch"] is True
    flag = st.conn.execute(
        "SELECT element_released FROM chunks WHERE path=?", (r["path"],)
    ).fetchone()[0]
    st.close()
    assert flag == 1, "tombstone did not win — the file should be released"
    assert (ws / ".audit" / "release_mismatch.jsonl").is_file()


def test_no_mismatch_when_consistent(tmp_path):
    """反向：front-matter 与墓碑一致 → 不报红。"""
    ws, md = _ws(tmp_path)
    f = _mkfile(md)
    st = _storage(ws)
    r = rebuilder.rebuild_file(st, ws, f)
    st.close()
    assert r["release_mismatch"] is False


def test_sealed_dark_page_owner_survives(tmp_path):
    """F-A1 反向用例：暗页 owner 透传到 chunks.sealed_owner，不被守卫改写为 NULL。

    暗页若以 scope='shared' 写入，守卫矫正会从 guard 表取 owner（注册时恒
    NULL）覆盖 rebuilder 算出的正确 owner → 主人格读不到自己的暗页（P0-21）。
    rebuilder 必须以 scope='sealed' 自带 owner 进入，绕开矫正。
    """
    ws = tmp_path
    md = ws / "memory" / "markdown" / "letters"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    f = md / "2026-10-01-secret--aaaaaaaa.md"
    f.write_text(
        "---\nid: s1\nvisibility: sealed\nauthor_viewpoint: agent--primus\n---\n\nprivate thought\n",
        encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    r = rebuilder.rebuild_file(st, ws, f)
    row = st.conn.execute(
        "SELECT scope, file_sealed, sealed_owner FROM chunks WHERE path=?",
        (r["path"],)).fetchone()
    assert row is not None
    assert row[0] == "sealed", f"dark page must be sealed scope, got {row[0]}"
    assert row[1] == 1, "file_sealed must be 1"
    assert row[2] is not None and "primus" in row[2], \
        f"F-A1: sealed_owner lost -> {row[2]!r}"
    st.close()


def test_files_row_carries_chunk_count_and_version(tmp_path):
    """F-A2 反向用例：files.chunk_count/chunker_version 写入，非恒 0。"""
    ws, md = _ws(tmp_path)
    f = _mkfile(md)
    st = _storage(ws)
    index_guard.ensure_guard(st.conn)
    r = rebuilder.rebuild_file(st, ws, f)
    row = st.conn.execute(
        "SELECT chunk_count, chunker_version FROM files WHERE path=?",
        (r["path"],)).fetchone()
    assert row is not None
    assert row[0] == r["chunks"], f"chunk_count {row[0]} != {r['chunks']}"
    assert row[1] == rebuilder.CHUNKER_VERSION, \
        f"chunker_version {row[1]} != {rebuilder.CHUNKER_VERSION}"
    st.close()
