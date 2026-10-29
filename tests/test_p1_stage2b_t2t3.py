"""P1-阶段二-B T2+T3 反向用例。

T2：单一变更事件记录点 + shelf 滞后判据。
T3：读路径探针（mtime 校验 + 缓存 + stale 计数进 get_status）+
    检索 fail-visible / 内容端点 fail-closed 行为差异。
"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from agent.memory import changes, index_guard, rebuilder, text_inv
from agent.memory.freshness import FreshnessProbe
from agent.memory.storage import MemoryStorage


# ---- T2 ---------------------------------------------------------------

def _seed(tmp_path):
    ws = tmp_path
    md = ws / "memory" / "markdown" / "events"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    f = md / "2026-10-29-note--aaaaaaaa.md"
    f.write_text("---\nid: e1\nhash8: aaaaaaaa\n---\n\nhello\n", encoding="utf-8")
    return ws, f


def test_change_log_records_each_source(tmp_path):
    ws, _ = _seed(tmp_path)
    changes.record_change(ws, "memory/markdown/events/x.md", "facade-write",
                          actor="default")
    changes.record_change(ws, "memory/markdown/events/x.md",
                          "import-human-edit", actor="human")
    changes.record_change(ws, "memory/markdown/events/x.md", "rebuilder")
    evs = changes.read_changes(ws)
    assert [e["source"] for e in evs] == [
        "facade-write", "import-human-edit", "rebuilder"]


def test_change_log_rejects_unknown_source(tmp_path):
    ws, _ = _seed(tmp_path)
    with pytest.raises(ValueError):
        changes.record_change(ws, "x.md", "bogus-source")


def test_rebuilder_records_change(tmp_path):
    ws, f = _seed(tmp_path)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuilder.rebuild_file(st, ws, f)
    st.close()
    evs = changes.read_changes(ws)
    assert any(e["source"] == "rebuilder" for e in evs)


def test_shelf_lag_notrun_when_no_changes(tmp_path):
    ws, _ = _seed(tmp_path)
    r = text_inv.check_shelf_lag(ws)
    assert r["verdict"] == "NOTRUN"  # 无变更事件 -> 显式 NOTRUN，不静默 PASS


def test_shelf_lag_red_when_truth_newer_then_green(tmp_path):
    ws, _ = _seed(tmp_path)
    # a change is recorded but NO shelf exists -> shelf maximally behind
    changes.record_change(ws, "memory/markdown/events/x.md", "facade-write",
                          mtime="2099-01-01T00:00:00+08:00")
    r = text_inv.check_shelf_lag(ws)
    assert r["verdict"] == "RED"  # 报红：该跑 batch 重建
    # batch 重建落地一个 shelf 页，generated_at 晚于变更 -> 转绿
    shelf = ws / "memory" / "shelf"
    shelf.mkdir(parents=True, exist_ok=True)
    (shelf / "index.md").write_text(
        "---\ngenerated_at: '2099-12-31T00:00:00+08:00'\n---\n\nbody\n",
        encoding="utf-8")
    r2 = text_inv.check_shelf_lag(ws)
    assert r2["verdict"] == "GREEN"


# ---- T3 ---------------------------------------------------------------

def test_probe_flags_stale_on_newer_mtime(tmp_path):
    ws, _ = _seed(tmp_path)
    p = FreshnessProbe(ws)
    # indexed mtime 1.0, real file mtime now (>>1) -> stale
    assert p.is_stale("memory/markdown/events/2026-10-29-note--aaaaaaaa.md",
                      1.0) is True
    assert p.stale_count == 1


def test_probe_fresh_when_index_current(tmp_path):
    ws, f = _seed(tmp_path)
    p = FreshnessProbe(ws)
    real = f.stat().st_mtime
    assert p.is_stale("memory/markdown/events/2026-10-29-note--aaaaaaaa.md",
                      real) is False
    assert p.stale_count == 0


def test_probe_cache_prevents_repeated_stat(tmp_path):
    ws, _ = _seed(tmp_path)
    calls = {"n": 0}

    def counting_statter(path):
        calls["n"] += 1
        return path.stat().st_mtime

    p = FreshnessProbe(ws, statter=counting_statter)
    rel = "memory/markdown/events/2026-10-29-note--aaaaaaaa.md"
    p.is_stale(rel, 0.0)
    p.is_stale(rel, 0.0)
    p.is_stale(rel, 0.0)
    assert calls["n"] == 1  # 缓存生效：重复命中不重复 stat（防 DoS）
    assert p.cache_hits == 2


def test_probe_cache_expires_after_ttl(tmp_path):
    ws, _ = _seed(tmp_path)
    now = {"t": 0.0}
    calls = {"n": 0}

    def counting_statter(path):
        calls["n"] += 1
        return path.stat().st_mtime

    p = FreshnessProbe(ws, ttl=10.0, clock=lambda: now["t"],
                       statter=counting_statter)
    rel = "memory/markdown/events/2026-10-29-note--aaaaaaaa.md"
    p.is_stale(rel, 0.0)
    now["t"] = 100.0  # 越过 TTL
    p.is_stale(rel, 0.0)
    assert calls["n"] == 2  # 过期后重新 stat


def test_content_endpoint_fail_closed_on_stale(tmp_path):
    ws, f = _seed(tmp_path)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuilder.rebuild_file(st, ws, f)
    # 强行把索引 files.mtime 改回过去 -> 真源比索引新 -> 内容端点必须拒绝
    st.conn.execute(
        "UPDATE files SET mtime=0 WHERE path=?",
        (rebuilder._posix_rel(f, ws),))
    st.conn.commit()
    st.close()

    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    cfg = MemoryConfig(workspace_root=str(ws))
    mgr = MemoryManager(config=cfg)
    out = mgr.read_content_fresh(
        "memory/markdown/events/2026-10-29-note--aaaaaaaa.md")
    assert out["ok"] is False  # fail-closed：陈旧即拒绝，不拿旧内容冒充事实
    assert out["stale"] is True
    # 且 stale 计数进了 get_status（自曝，不撞运气）
    assert mgr.get_status()["freshness"]["stale"] >= 1
    mgr.close()
