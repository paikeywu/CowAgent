"""P1-阶段二-B T5：INV 第 9 检查组反向用例。

每条 INV：改坏必报红 → 恢复必转绿；空库显式 NOTRUN（不静默 PASS）。
跑的是 03 号 INV 的 chunks 落地版 6 条（INV-1/2/3/6/9/11），不是笼统全绿。
"""
import sqlite3
from pathlib import Path

import pytest

from agent.memory import index_guard, rebuilder, text_inv
from agent.memory.storage import MemoryStorage


def _seed(tmp_path, body="hello world\n\nsecond para\n", name="2026-10-29-note--aaaaaaaa.md"):
    ws = tmp_path
    md = ws / "memory" / "markdown" / "events"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    f = md / name
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\nauthor_viewpoint: agent--primus\n---\n\n" + body,
        encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuilder.rebuild_file(st, ws, f)
    return ws, f, st


def _run(st, ws, **kw):
    conn = sqlite3.connect(str(ws / "memory" / "long-term" / "index.db"))
    try:
        return text_inv.run_text_invariants(conn, ws, **kw)
    finally:
        conn.close()


def _verdict(rep, inv):
    return rep["checks"][inv]["verdict"]


# -- INV-1 指针存在性 ---------------------------------------------------------

def test_inv1_green_on_seeded(tmp_path):
    ws, f, st = _seed(tmp_path)
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-1") == "GREEN"
    assert rep["checks"]["INV-1"]["fingerprint"].startswith("chunks=")


def test_inv1_red_on_null_pointer(tmp_path):
    """A row whose pointer is gone must be RED.

    **P1 阶段三 S3-I (2026-10-30): the guard's triggers are DROPPED
    here.**  The guard restores the protected columns from the stored
    row, so ``UPDATE chunks SET body_anchor=NULL`` is intercepted and
    the pointer survives — and INV-1 then correctly reports GREEN for a
    row that IS pointed.

    The reverse case is about the CRITERION: it asks "does INV-1 report
    a row whose pointer is missing?".  For the row's pointer to
    actually be missing, the guard has to be dropped first — otherwise
    the case measures the guard, not the criterion.
    """
    from agent.memory import body_guard

    ws, f, st = _seed(tmp_path)
    for name in body_guard.guard_trigger_names():
        st.conn.execute("DROP TRIGGER IF EXISTS %s" % name)
    st.conn.commit()
    st.conn.execute("UPDATE chunks SET body_anchor=NULL WHERE text_state='projection'")
    st.conn.commit()
    rep = _run(st, ws)
    assert _verdict(rep, "INV-1") == "RED"
    assert any(v["code"] == "inv1-pointer-missing" for v in rep["violations"])
    # restore -> green
    rebuilder.rebuild_file(st, ws, f)
    rep2 = _run(st, ws)
    st.close()
    assert _verdict(rep2, "INV-1") == "GREEN"


def test_inv1_red_on_missing_file(tmp_path):
    ws, f, st = _seed(tmp_path)
    st.conn.execute(
        "UPDATE chunks SET body_path='memory/markdown/events/GONE.md'")
    st.conn.commit()
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-1") == "RED"


# -- INV-2/3 正文可重建 + 片段哈希 --------------------------------------------

def test_inv23_green_on_seeded(tmp_path):
    ws, f, st = _seed(tmp_path)
    rep = _run(st, ws, full=True)
    st.close()
    assert _verdict(rep, "INV-2/3") == "GREEN"


def test_inv23_red_on_drifted_text(tmp_path):
    ws, f, st = _seed(tmp_path)
    st.conn.execute("UPDATE chunks SET text='TAMPERED' WHERE text_state='projection'")
    st.conn.commit()
    rep = _run(st, ws, full=True)
    assert _verdict(rep, "INV-2/3") == "RED"
    assert any(v["code"] == "inv2-body-not-rebuildable" for v in rep["violations"])
    rebuilder.rebuild_file(st, ws, f)
    rep2 = _run(st, ws, full=True)
    st.close()
    assert _verdict(rep2, "INV-2/3") == "GREEN"


def test_inv23_red_on_bad_hash(tmp_path):
    ws, f, st = _seed(tmp_path)
    st.conn.execute("UPDATE chunks SET body_hash='sha256:deadbeef' WHERE text_state='projection'")
    st.conn.commit()
    rep = _run(st, ws, full=True)
    st.close()
    assert _verdict(rep, "INV-2/3") == "RED"
    assert any(v["code"] == "inv3-fragment-hash-mismatch" for v in rep["violations"])


# -- INV-6 时序 ---------------------------------------------------------------

def test_inv6_green_on_seeded(tmp_path):
    ws, f, st = _seed(tmp_path)
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-6") == "GREEN"


def test_inv6_red_on_future_mtime(tmp_path):
    ws, f, st = _seed(tmp_path)
    st.conn.execute("UPDATE files SET mtime=99999999999")
    st.conn.commit()
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-6") == "RED"
    assert any(v["code"] == "inv6-time-disorder" for v in rep["violations"])


# -- INV-9 释放有同意源 --------------------------------------------------------

def test_inv9_notrun_when_no_release(tmp_path):
    ws, f, st = _seed(tmp_path)
    rep = _run(st, ws)
    st.close()
    # 无释放记录 -> NOTRUN，不静默 PASS
    assert _verdict(rep, "INV-9") == "NOTRUN"


def test_inv9_red_on_released_without_consent(tmp_path):
    ws, f, st = _seed(tmp_path)
    st.conn.execute("UPDATE chunks SET element_released=1")
    st.conn.commit()
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-9") == "RED"
    assert any(v["code"] == "inv9-release-no-consent" for v in rep["violations"])


# -- INV-11 路径安全 -----------------------------------------------------------

def test_inv11_green_on_seeded(tmp_path):
    ws, f, st = _seed(tmp_path)
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-11") == "GREEN"


def test_inv11_red_on_backslash(tmp_path):
    ws, f, st = _seed(tmp_path)
    st.conn.execute(
        "UPDATE chunks SET body_path=replace(body_path,'/','\\')")
    st.conn.commit()
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-11") == "RED"
    assert any(v["code"] == "inv11-unsafe-path" for v in rep["violations"])


def test_inv11_red_on_dotdot(tmp_path):
    ws, f, st = _seed(tmp_path)
    st.conn.execute("UPDATE chunks SET body_path='../escape.md'")
    st.conn.commit()
    rep = _run(st, ws)
    st.close()
    assert _verdict(rep, "INV-11") == "RED"


# -- NOTRUN 口径 + 范围声明 ----------------------------------------------------

def test_empty_index_inv_checks_notrun_not_silent_pass(tmp_path):
    ws = tmp_path
    (ws / "memory" / "long-term").mkdir(parents=True)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    rep = _run(st, ws)
    st.close()
    # 空库：INV-1/6/9/11 显式 NOTRUN，绝不静默 PASS
    for inv in ("INV-1", "INV-6", "INV-9", "INV-11"):
        assert _verdict(rep, inv) == "NOTRUN", inv
    assert rep["checks"]["INV-2/3"]["verdict"] == "NOTRUN"


def test_report_declares_chunks_landing_scope_and_deferred(tmp_path):
    ws, f, st = _seed(tmp_path)
    rep = _run(st, ws)
    st.close()
    # 报告必须写明跑的是 chunks 落地版 6 条 + 登记暂缓项，禁止笼统"INV 全绿"
    assert "chunks 落地版" in rep["scope"]
    assert set(rep["deferred"]) == {"INV-4", "INV-5", "INV-7", "INV-8"}
