"""P1-阶段二-B 缺陷修复反向用例（T2-B1 / T2-B2 / T2-B3）。

每条用例钉死一个缺陷：
  * T2-B1: manager 经 __new__ 绕过 __init__ 也不得抛 AttributeError。
  * T2-B2: shelf-lag 比较 INSTANTS 而非字符串；解析失败 fail-closed 判红。
  * T2-B3: mtime 缺失的变更事件回退到 at，仍参与滞后比较。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.memory import changes, text_inv
from agent.memory.manager import MemoryManager
from agent.memory.storage import SearchResult


# ---- T2-B1 ------------------------------------------------------------

def _bare_manager():
    """A manager built WITHOUT running __init__ (the failing path)."""
    m = MemoryManager.__new__(MemoryManager)
    m.reranker = None
    m.embedding_provider = None
    m._dirty = False
    m.config = SimpleNamespace(max_results=10, min_score=0.0,
                               sync_on_search=False, vector_weight=0.7,
                               keyword_weight=0.3)
    m.storage = SimpleNamespace(search_keyword=lambda **_: [
        SearchResult(path="memory/shared/a.md", start_line=1, end_line=1,
                     score=1.0, snippet="s", source="memory")])
    return m


def test_t2b1_new_built_manager_search_does_not_raise():
    """T2-B1: __new__ path must not AttributeError in search()."""
    m = _bare_manager()
    out = asyncio.run(m.search("q", min_score=0.0))
    assert len(out) == 1


def test_t2b1_new_built_manager_get_status_does_not_raise():
    """T2-B1: __new__ path must not AttributeError in get_status()."""
    m = _bare_manager()
    m.storage = SimpleNamespace(get_stats=lambda: {"embedded": 0, "chunks": 0})
    m.get_freshness_status = lambda: {"stale_hits": 0}
    m.get_freshness_status = MemoryManager.get_freshness_status.__get__(m)
    # get_status() needs a real storage for some fields; the point of this
    # case is that it does not blow up on the missing _freshness attribute.
    try:
        m.get_status()
    except AttributeError as e:
        pytest.fail(f"AttributeError on the _freshness attribute: {e}")
    except Exception:
        pass  # other failures are not this defect


def test_t2b1_freshness_attribute_exists_on_class():
    """T2-B1: the attribute is guaranteed at CLASS level (any instance)."""
    assert hasattr(MemoryManager, "_freshness")
    assert MemoryManager._freshness is None
    assert _bare_manager()._freshness is None


# ---- T2-B2 / T2-B3 ----------------------------------------------------

def _ws_with_shelf(tmp_path, generated_at: str, *, change_mtime=None,
                   change_exists=True):
    ws = tmp_path
    (ws / "memory" / "shelf").mkdir(parents=True)
    ev = ws / "memory" / "markdown" / "events"
    ev.mkdir(parents=True)
    (ev / "x.md").write_text("x\n", encoding="utf-8")
    (ws / "memory" / "shelf" / "index.md").write_text(
        f"---\nkind: shelf-index\ngenerated_at: '{generated_at}'\n---\n\nb\n",
        encoding="utf-8")
    changes.record_change(ws, "memory/markdown/events/x.md", "facade-write",
                          mtime=change_mtime)
    return ws


def test_t2b2_same_instant_different_format_is_green(tmp_path):
    """T2-B2: the SAME instant in two spellings must be GREEN."""
    # 2026-10-29T14:45:35+08:00 == 2026-10-29 06:45:35+00:00
    ws = _ws_with_shelf(tmp_path, "2026-10-29 06:45:35+00:00",
                        change_mtime="2026-10-29T14:45:35+08:00")
    r = text_inv.check_shelf_lag(ws)
    assert r["verdict"] == "GREEN", r


def test_t2b2_genuinely_older_shelf_is_red(tmp_path):
    """T2-B2: a shelf that IS older must be RED."""
    ws = _ws_with_shelf(tmp_path, "2026-10-29 06:45:35+00:00",
                        change_mtime="2026-10-29T15:45:35+08:00")
    r = text_inv.check_shelf_lag(ws)
    assert r["verdict"] == "RED", r


def test_t2b2_unparsable_generated_at_is_red_not_green(tmp_path):
    """T2-B2: an unparsable stamp is fail-closed RED (not GREEN/NOTRUN)."""
    ws = _ws_with_shelf(tmp_path, "not-a-date",
                        change_mtime="2026-10-29T14:45:35+08:00")
    r = text_inv.check_shelf_lag(ws)
    assert r["verdict"] == "RED", r
    assert r["violations"], r


def test_t2b2_same_format_same_instant_is_green(tmp_path):
    """T2-B2: same spelling, same instant -> GREEN (no false red)."""
    ws = _ws_with_shelf(tmp_path, "2026-10-29T14:45:35+08:00",
                        change_mtime="2026-10-29T14:45:35+08:00")
    r = text_inv.check_shelf_lag(ws)
    assert r["verdict"] == "GREEN", r


def test_t2b3_deleted_file_change_still_participates(tmp_path):
    """T2-B3: a change whose file is gone falls back to its `at` stamp."""
    ws = tmp_path
    (ws / "memory" / "shelf").mkdir(parents=True)
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "shelf" / "index.md").write_text(
        "---\ngenerated_at: '2026-10-29 06:45:35+00:00'\n---\n\nb\n",
        encoding="utf-8")
    ev = changes.record_change(ws, "memory/markdown/events/gone.md",
                               "facade-write")
    assert ev["mtime"] is None            # the file does not exist
    latest = changes.latest_change_mtime(ws)
    assert latest is not None             # the fallback kicked in
    assert latest == ev["at"]
    # And it takes part in the comparison (RED: the record is newer).
    r = text_inv.check_shelf_lag(ws)
    assert r["verdict"] == "RED", r


def test_t2b3_parse_ts_accepts_both_spellings():
    """T2-B3/B2: the parser understands both generator spellings."""
    a = text_inv._parse_ts("2026-10-29T14:45:35+08:00")
    b = text_inv._parse_ts("2026-10-29 06:45:35+00:00")
    assert a is not None and b is not None
    assert a == b
    assert text_inv._parse_ts("not-a-date") is None
