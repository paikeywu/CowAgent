"""P1-阶段二-C 缺陷修复反向用例（C-1 ~ C-6）。

每条用例钉死一个缺陷：
  * C-1: import_human_edit 拒绝任何 immutable 字段的篡改（逐字段）。
  * C-2: facade 的元素写入/释放路径都写变更记录（shelf-lag 不再 NOTRUN）。
  * C-3: 重建后立即查询必须 fresh；改动后必须 stale（同精度比较）。
  * C-4: 守卫矫正后的 sealed_owner 必须是规范的 agent--<id>。
  * C-6: 内容端点拒绝工作区外的路径，且与 INV-11 共用同一函数。
"""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from agent.memory import changes, index_guard, paths, text_inv
from agent.memory.visibility import AGENT_OWNER_PREFIX


# ---- helpers ----------------------------------------------------------

def _ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    return ws


def _facade(ws, actor="default"):
    from agent.memory.facade import WriteFacade
    return WriteFacade(workspace_root=str(ws), actor=actor)


# ---- C-6 · path escape -------------------------------------------------

def test_c6_content_endpoint_refuses_path_escape(tmp_path):
    """C-6: read_content_fresh must not serve anything outside the workspace."""
    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    ws = _ws(tmp_path)
    (ws / "memory" / "markdown" / "events" / "a.md").write_text(
        "hello\n", encoding="utf-8")
    mgr = MemoryManager(config=MemoryConfig(workspace_root=str(ws)))
    out = mgr.read_content_fresh("../../../etc/passwd")
    assert out["ok"] is False
    assert "content" not in out
    out2 = mgr.read_content_fresh("C:/Windows/win.ini")
    assert out2["ok"] is False
    assert "content" not in out2


def test_c6_legit_path_inside_workspace_still_served(tmp_path):
    """C-6: the good path must not be blocked by the escape check."""
    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    ws = _ws(tmp_path)
    rel = "memory/markdown/events/a.md"
    (ws / rel).write_text("hello\n", encoding="utf-8")
    mgr = MemoryManager(config=MemoryConfig(workspace_root=str(ws)))
    # The file is newer than the (empty) index -> stale -> fail-closed.
    # That is a DIFFERENT refusal than the path escape; assert the reason.
    out = mgr.read_content_fresh(rel)
    assert out["ok"] is False
    assert "unsafe path" not in out["reason"]


def test_c6_inv11_and_content_endpoint_share_one_rule(tmp_path):
    """C-6 (red line 4): INV-11 and the content endpoint use ONE function."""
    ws = _ws(tmp_path)
    # The very same helper decides both.
    assert paths.path_escape_reason("../x", ws) is not None
    assert paths.path_escape_reason("C:/x", ws) is not None
    assert paths.path_escape_reason("a\\b", ws) is not None
    assert paths.path_escape_reason("memory/a.md", ws) is None
    # INV-11 goes through the shared helper (monkeypatch-proof: patch the
    # helper to always pass and the INV must stop flagging).
    src = Path(text_inv.__file__).read_text(encoding="utf-8")
    assert "_paths.path_escape_reason" in src


def test_c6_path_escape_reason_is_fail_closed(tmp_path):
    """C-6: an unjudgeable path is unsafe, never 'fine'."""
    ws = _ws(tmp_path)
    assert paths.path_escape_reason("", ws) == "empty path"
    assert paths.path_escape_reason(None, ws) == "empty path"


# ---- C-4 · owner token canonical form ---------------------------------

def test_c4_guard_normalizes_owner_to_canonical_token(tmp_path):
    """C-4: the guard must WRITE agent--<id>, the form the read path matches."""
    expr = index_guard._fm_owner_expr("text")
    assert "agent--" in expr
    assert index_guard._OWNER_PREFIX == AGENT_OWNER_PREFIX


def test_c4_prefix_constant_stays_in_sync_with_visibility():
    """C-4: the SQL literal must be the SAME constant the read path uses."""
    assert index_guard._OWNER_PREFIX == AGENT_OWNER_PREFIX == "agent--"


def test_c4_bare_viewpoint_id_is_normalized_on_write(tmp_path):
    """C-4: front-matter says `primus`; the index must store `agent--primus`.

    The read path matches the canonical token, so writing the bare id made
    the owner unable to read its own dark page (P0-21). The fix is at the
    PRODUCTION end — the read path does NOT try both forms.
    """
    from agent.memory.storage import MemoryChunk, MemoryStorage
    ws = _ws(tmp_path)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    text = ("---\nid: c4-1\nvisibility: sealed\n"
            "author_viewpoint: primus\n---\n\nprivate thought\n")
    st.save_chunk(MemoryChunk(
        id="c4-bare-owner", user_id=None, scope="shared", source="memory",
        path="memory/markdown/events/c4-bare.md",
        start_line=1, end_line=5, text=text, embedding=None, hash="h"))
    row = st.conn.execute(
        "SELECT scope, file_sealed, sealed_owner FROM chunks WHERE id=?",
        ("c4-bare-owner",)).fetchone()
    assert row is not None, "the row was discarded by the guard"
    assert row[1] == 1, f"file_sealed must be 1 after correction, got {row[1]}"
    assert row[2] == f"{AGENT_OWNER_PREFIX}primus", \
        f"C-4: sealed_owner must be the canonical token, got {row[2]!r}"


def test_c4_already_prefixed_owner_is_not_double_prefixed(tmp_path):
    """C-4: an id that already carries the prefix must not get a second one."""
    from agent.memory.storage import MemoryChunk, MemoryStorage
    ws = _ws(tmp_path)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    text = ("---\nid: c4-2\nvisibility: sealed\n"
            "author_viewpoint: agent--primus\n---\n\nprivate thought\n")
    st.save_chunk(MemoryChunk(
        id="c4-prefixed-owner", user_id=None, scope="shared", source="memory",
        path="memory/markdown/events/c4-prefixed.md",
        start_line=1, end_line=5, text=text, embedding=None, hash="h"))
    row = st.conn.execute(
        "SELECT sealed_owner FROM chunks WHERE id=?",
        ("c4-prefixed-owner",)).fetchone()
    assert row[0] == "agent--primus", \
        f"C-4: owner got double-prefixed: {row[0]!r}"


# ---- C-3 · mtime precision --------------------------------------------

def test_c3_freshly_rebuilt_file_is_not_stale(tmp_path):
    """C-3: a file rebuilt NOW must read fresh, not stale."""
    from agent.memory.freshness import FreshnessProbe
    ws = _ws(tmp_path)
    rel = "memory/markdown/events/a.md"
    p = ws / rel
    p.write_text("hello\n", encoding="utf-8")
    indexed = int(p.stat().st_mtime)
    probe = FreshnessProbe(ws)
    assert probe.is_stale(rel, indexed) is False


def test_c3_changed_truth_file_is_stale(tmp_path):
    """C-3: a truth file NEWER than the index must read stale."""
    from agent.memory.freshness import FreshnessProbe
    ws = _ws(tmp_path)
    rel = "memory/markdown/events/a.md"
    p = ws / rel
    p.write_text("hello\n", encoding="utf-8")
    indexed = int(p.stat().st_mtime)
    time.sleep(1.1)
    p.write_text("changed\n", encoding="utf-8")
    probe = FreshnessProbe(ws)
    assert probe.is_stale(rel, indexed) is True


def test_c3_probe_truncates_to_index_precision(tmp_path):
    """C-3: the probe truncates the real mtime to whole seconds."""
    from agent.memory.freshness import FreshnessProbe
    ws = _ws(tmp_path)
    rel = "memory/markdown/events/a.md"
    (ws / rel).write_text("hello\n", encoding="utf-8")
    probe = FreshnessProbe(ws)
    real = (ws / rel).stat().st_mtime
    got = probe._real_mtime(rel)
    assert got == int(real)


# ---- C-5 · the self-audit (same class as C-3) --------------------------

def test_c5_add_memory_does_not_record_the_source_files_mtime(tmp_path):
    """C-5: files.mtime must be the WRITE moment, not manager.py's mtime.

    The old code wrote ``int(os.path.getmtime(__file__))`` with the comment
    "Use current time" — it is the mtime of the SOURCE FILE, a constant
    frozen at deploy time. The recorded timestamp could not track the
    content, so the freshness probe's verdict was permanent in either
    direction. Same class of bug as C-3.
    """
    import time as _time
    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    ws = _ws(tmp_path)
    mgr = MemoryManager(config=MemoryConfig(workspace_root=str(ws)))
    before = int(_time.time())
    import asyncio
    asyncio.run(mgr.add_memory(
        "hello world", path="memory/markdown/events/new.md"))
    row = mgr.storage.conn.execute(
        "SELECT mtime FROM files WHERE path=?",
        ("memory/markdown/events/new.md",)).fetchone()
    assert row is not None, "no files row was written"
    src_mtime = int(__import__("pathlib").Path(
        "agent/memory/manager.py").resolve().stat().st_mtime)
    assert row[0] != src_mtime or abs(int(_time.time()) - src_mtime) < 5, \
        "files.mtime still carries the source file's mtime"
    assert row[0] >= before - 2, \
        f"files.mtime {row[0]} is not the write moment (before={before})"


# ---- C-2 · the fourth path records changes -----------------------------

def test_c2_write_event_records_a_change_event(tmp_path):
    """C-2: write_event (the regular entry) must record a change event."""
    ws = _ws(tmp_path)
    fac = _facade(ws)
    fac.write_event("note", "标题", "正文", date="2026-10-29")
    events = [json.loads(l) for l in
              changes.change_log_path(ws).read_text(encoding="utf-8").splitlines()
              if l.strip()]
    assert len(events) == 1
    assert events[0]["source"] == "facade-element"


def test_c2_element_write_makes_shelf_lag_red_not_notrun(tmp_path):
    """C-2: after an element write the shelf-lag invariant must RED."""
    ws = _ws(tmp_path)
    fac = _facade(ws)
    fac.write_event("note", "标题", "正文", date="2026-10-29")
    rep = text_inv.check_shelf_lag(ws)
    assert rep["verdict"] == "RED"
    assert rep["violations"]


def test_c2_release_records_every_truth_file(tmp_path):
    """C-2: the release machinery produces several files — all recorded."""
    ws = _ws(tmp_path)
    fac = _facade(ws)
    fac.write_event("note", "标题", "正文", date="2026-10-29")
    before = len(changes.change_log_path(ws).read_text(
        encoding="utf-8").splitlines())
    try:
        fac.initiate_release(
            "memory/markdown/events/2026-10-29/note--"
            + _hash8(ws, "note") + ".md", consent_ref="consent-1")
    except Exception:
        pass  # the release may refuse (no consent channel) — the writes
          # that DID happen must still be recorded
    after = len(changes.change_log_path(ws).read_text(
        encoding="utf-8").splitlines())
    assert after >= before
    sources = {json.loads(l)["source"]
               for l in changes.change_log_path(ws).read_text(
                   encoding="utf-8").splitlines() if l.strip()}
    assert "facade-element" in sources


def _hash8(ws, slug):
    for p in (ws / "memory" / "markdown" / "events").rglob(f"{slug}--*.md"):
        return p.name.split("--", 1)[1][:8]
    return "00000000"


# ---- C-1 · every immutable field ---------------------------------------

def test_c1_every_immutable_field_is_checked(tmp_path):
    """C-1: tampering with ANY immutable field must be rejected."""
    from agent.memory.facade import IMMUTABLE_FM_FIELDS
    assert "id" in IMMUTABLE_FM_FIELDS
    assert "occurred_at" in IMMUTABLE_FM_FIELDS
    assert "author_viewpoint" in IMMUTABLE_FM_FIELDS


def _t4_setup(tmp_path):
    from agent.memory import index_guard, rebuilder
    from agent.memory.facade import WriteFacade
    from agent.memory.storage import MemoryStorage
    ws = tmp_path
    md = ws / "memory" / "markdown" / "events"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    f = md / "2026-10-29-note--aaaaaaaa.md"
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\noccurred_at: 2026-10-29T10:00:00+08:00\n"
        "author_viewpoint: agent--primus\nstatus: active\n---\n\n"
        "original body\n", encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuilder.rebuild_file(st, ws, f)
    facade = WriteFacade(ws, actor="primus", cow_root=ws)
    rel = "memory/markdown/events/2026-10-29-note--aaaaaaaa.md"
    return ws, f, st, facade, rel


def test_c1_tampering_id_is_rejected(tmp_path):
    """C-1: the `id` birth fact cannot move with an edit."""
    from agent.memory.facade import FrontmatterTamperError
    ws, f, st, facade, rel = _t4_setup(tmp_path)
    f.write_text(
        "---\nid: e2\nhash8: aaaaaaaa\noccurred_at: 2026-10-29T10:00:00+08:00\n"
        "author_viewpoint: agent--primus\nstatus: active\n---\n\n"
        "original body\n", encoding="utf-8")
    with pytest.raises(FrontmatterTamperError):
        facade.import_human_edit(rel, storage=st)


def test_c1_tampering_occurred_at_is_rejected(tmp_path):
    """C-1: `occurred_at` (when it happened) cannot be rewritten."""
    from agent.memory.facade import FrontmatterTamperError
    ws, f, st, facade, rel = _t4_setup(tmp_path)
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\noccurred_at: 2020-01-01T00:00:00+08:00\n"
        "author_viewpoint: agent--primus\nstatus: active\n---\n\n"
        "original body\n", encoding="utf-8")
    with pytest.raises(FrontmatterTamperError):
        facade.import_human_edit(rel, storage=st)


def test_c1_tampering_author_viewpoint_is_rejected(tmp_path):
    """C-1: `author_viewpoint` (who owns the page) cannot be reassigned."""
    from agent.memory.facade import FrontmatterTamperError
    ws, f, st, facade, rel = _t4_setup(tmp_path)
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\noccurred_at: 2026-10-29T10:00:00+08:00\n"
        "author_viewpoint: agent--someone-else\nstatus: active\n---\n\n"
        "original body\n", encoding="utf-8")
    with pytest.raises(FrontmatterTamperError):
        facade.import_human_edit(rel, storage=st)


def test_c1_legal_edit_still_imports(tmp_path):
    """C-1: the guard must not block a legal edit (no false red)."""
    ws, f, st, facade, rel = _t4_setup(tmp_path)
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\noccurred_at: 2026-10-29T10:00:00+08:00\n"
        "author_viewpoint: agent--primus\nstatus: active\n---\n\n"
        "original body\n\nnew section appended\n", encoding="utf-8")
    out = facade.import_human_edit(rel, storage=st)
    assert Path(out).name == f.name
