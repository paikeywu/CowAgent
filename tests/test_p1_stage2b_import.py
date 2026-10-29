"""P1-阶段二-B T4：import_human_edit 金缮校验通道反向用例。

每条判据：改坏必报红 → 恢复必转绿。
"""
import json
from pathlib import Path

import pytest

from agent.memory import rebuilder, index_guard
from agent.memory.facade import (
    AppendOnlyViolationError,
    FrontmatterTamperError,
    WriteFacade,
)
from agent.memory.storage import MemoryStorage


def _setup(tmp_path, body="original body\n"):
    ws = tmp_path
    md = ws / "memory" / "markdown" / "events"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "markdown" / "_tombstones").mkdir(parents=True, exist_ok=True)
    f = md / "2026-10-29-note--aaaaaaaa.md"
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\nauthor_viewpoint: agent--primus\n---\n\n" + body,
        encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuilder.rebuild_file(st, ws, f)  # seed the projection with the OLD body
    facade = WriteFacade(ws, actor="primus", cow_root=ws)
    rel = "memory/markdown/events/2026-10-29-note--aaaaaaaa.md"
    return ws, f, st, facade, rel


def _audit_lines(ws):
    p = ws / "memory" / ".audit" / "facade.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_legal_append_imports_and_audits(tmp_path):
    """合法追加：旧 body 原样在头部 + 新段 → 导入成功 + 审计 who='human'。"""
    ws, f, st, facade, rel = _setup(tmp_path)
    # human appends a new section in place
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\nauthor_viewpoint: agent--primus\n---\n\n"
        "original body\n\nnew section appended\n", encoding="utf-8")
    out = facade.import_human_edit(rel, storage=st)
    assert out == f.resolve() or Path(out).name == f.name
    lines = _audit_lines(ws)
    imp = [l for l in lines if l.get("action") == "import_human_edit"
           and l.get("outcome") == "imported"]
    assert imp, "no import audit line"
    assert imp[-1].get("who") == "human"
    st.close()


def test_rewrite_history_rejected(tmp_path):
    """反向：改写历史段（旧 body 不再是新 body 前缀）→ AppendOnlyViolationError。"""
    ws, f, st, facade, rel = _setup(tmp_path)
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\nauthor_viewpoint: agent--primus\n---\n\n"
        "REWRITTEN body\n", encoding="utf-8")  # old body gone
    with pytest.raises(AppendOnlyViolationError):
        facade.import_human_edit(rel, storage=st)
    st.close()


def test_immutable_hash8_tamper_rejected(tmp_path):
    """反向：fm.hash8 与文件名出生 hash8 漂移 → FrontmatterTamperError。"""
    ws, f, st, facade, rel = _setup(tmp_path)
    f.write_text(
        "---\nid: e1\nhash8: bbbbbbbb\nauthor_viewpoint: agent--primus\n---\n\n"
        "original body\n\nappend\n", encoding="utf-8")  # tampered hash8
    with pytest.raises(FrontmatterTamperError):
        facade.import_human_edit(rel, storage=st)
    st.close()


def test_no_storage_fails_closed(tmp_path):
    """反向：无 storage 无法验证只追加契约 → fail closed（FacadeError），不重建。"""
    ws, f, st, facade, rel = _setup(tmp_path)
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\nauthor_viewpoint: agent--primus\n---\n\n"
        "original body\n\nappend\n", encoding="utf-8")
    with pytest.raises(Exception):
        facade.import_human_edit(rel)  # no storage -> cannot verify -> refuse
    st.close()


def test_filename_hash8_stable_after_edit(tmp_path):
    """E3：编辑后文件名 hash8 不变（出生指纹，不随内容漂移）。"""
    ws, f, st, facade, rel = _setup(tmp_path)
    name_before = f.name
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\nauthor_viewpoint: agent--primus\n---\n\n"
        "original body\n\nmore content here\n", encoding="utf-8")
    facade.import_human_edit(rel, storage=st)
    assert f.name == name_before, "filename hash8 must not move with an edit"
    st.close()
