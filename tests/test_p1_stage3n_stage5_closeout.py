# encoding: utf-8
"""P1 阶段三 N · 阶段五收口 + 账本孤儿机制 —— 实现侧判据与破坏用例.

覆盖交办的 W-1..W-5 与任务二（cert_ledger 孤儿），每条都带破坏用例
（改坏→红→还原→绿）：

  * **W-1** MSG-INV-1..7 接线进 inspector —— 巡检报告必须有 msg_inv 组、
    7 条判据各有 verdict；未外置库必须 NOTRUN（不许假绿）；破坏用例：
    篡改 JSONL → 巡检红；GREEN 时才追加基线。
  * **W-2** 补偿链写路径 —— extras 更新发生在 external 行上 → 补偿文件
    +1、DB 置位；同一行两次更新 → MSG-INV-5 仍 GREEN（按去重目标行对账）；
    inline 行不产生补偿。
  * **W-3** DEFERRED_INVS["INV-5"] 移除 —— 由 test_p1_stage2b_inv.py 的
    集合断言锁定（同提交改）。
  * **W-4** get_status 暴露 msg_chain_anchor_scope / cold_read_latency_ms。
  * **W-5** 阶段 4 崩溃 → 双写守卫必须拒绝续跑；整批回滚（backup API +
    删当批目录）后可干净重跑。
  * **任务二** replace_file_chunks / delete_by_path 在同事务级联清账本：
    消失的 id 的证人条目必须删，存活的 id 的证人条目必须留（S3-L 恢复
    机制依赖它）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory import body_guard  # noqa: E402
from agent.memory import ledger_orphans  # noqa: E402
from agent.memory import msg_chain  # noqa: E402
from agent.memory import msg_inv  # noqa: E402
from agent.memory import msg_migrate  # noqa: E402
from agent.memory.conversation_store import ConversationStore  # noqa: E402
from agent.memory.inspector import Inspector  # noqa: E402
from agent.memory.storage import MemoryChunk, MemoryStorage  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _workspace(tmp_path):
    """A workspace with ONE index and a two-turn conversation.

    **The rows stay INLINE on purpose.**  This file is about the BATCH
    externalisation (``msg_migrate``) and about what the criteria say
    about a library that was never externalised — so the fixture must
    start in that state.  The write path now externalises by default
    (P1 阶段三 · 缺口 A: the externalisation is an invariant of the
    write, not a one-shot action), so the fixture says so explicitly
    instead of relying on the old default.
    """
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    db = ws / "memory" / "long-term" / "index.db"
    store = ConversationStore(db, agent_id="", externalise=False)
    store.append_messages("s1", [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
    ], channel_type="web")
    return ws, db, store


def _externalise(ws, db, agent_id=""):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return msg_migrate.externalise_all(conn, ws, agent_id,
                                           batch_date="2026-10-31")
    finally:
        conn.close()


def _conn(db):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _jsonl_of(ws):
    files = [p for p in (ws / "memory" / "messages_export").rglob("*.jsonl")
             if ".audit" not in p.parts]
    assert files, "the externalisation wrote no JSONL"
    return files[0]


def _line_count(path):
    return sum(1 for raw in path.read_text(encoding="utf-8").splitlines()
               if raw.strip())


# ---------------------------------------------------------------------------
# W-1 — MSG-INV-1..7 接线进巡检
# ---------------------------------------------------------------------------

def test_w1_the_patrol_reports_every_msg_inv_criterion(tmp_path):
    """巡检报告必须有 msg_inv 组，7 条判据各有 verdict，且不许缺项。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    # The patrol needs the baseline the migration writes (MSG-INV-2/3 前置).
    msg_inv.write_baseline(ws)

    report = Inspector(ws).inspect(trigger="manual")
    group = report.get("msg_inv")
    assert group is not None, "the patrol report has no msg_inv group"
    assert set(group["checks"]) == {
        "MSG-INV-1", "MSG-INV-2", "MSG-INV-3", "MSG-INV-4",
        "MSG-INV-5", "MSG-INV-6", "MSG-INV-7"}
    for name, check in group["checks"].items():
        assert check["verdict"] in (msg_inv.GREEN, msg_inv.NOTRUN,
                                    msg_inv.RED), name
        assert check["verdict"] == msg_inv.GREEN, \
            "%s should be GREEN on a healthy library: %s" % (
                name, check["violations"])
    assert group["verdict"] == msg_inv.GREEN


def test_w1_a_green_patrol_appends_a_baseline(tmp_path):
    """GREEN 巡检必须追加基线（MSG-INV-2/3 的对账基准随追加更新）。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    msg_inv.write_baseline(ws)
    before = _line_count(msg_chain.baseline_path(ws))

    Inspector(ws).inspect(trigger="manual")
    after = _line_count(msg_chain.baseline_path(ws))
    assert after == before + 1, \
        "a GREEN patrol must append ONE baseline entry (%d -> %d)" % (
            before, after)


def test_w1_an_un_externalised_library_is_notrun_never_green(tmp_path):
    """判据自身的判据 A/B：未外置库上巡检必须 NOTRUN，不许假绿。"""
    ws, db, store = _workspace(tmp_path)  # no externalisation
    report = Inspector(ws).inspect(trigger="manual")
    group = report["msg_inv"]
    assert group["verdict"] == msg_inv.NOTRUN
    assert all(c["verdict"] == msg_inv.NOTRUN
               for c in group["checks"].values())
    assert not msg_chain.baseline_path(ws).is_file(), \
        "a NOTRUN patrol must NOT write a baseline"


def test_w1_a_tampered_chain_turns_the_patrol_red(tmp_path):
    """破坏用例：篡改 JSONL 一行 → 巡检 msg_inv 组红、违规入报告、
    且不写基线；还原 → 绿。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    msg_inv.write_baseline(ws)
    path = _jsonl_of(ws)
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    line = json.loads(lines[0])
    line["content"] = "tampered — 被改写的内容"
    lines[0] = json.dumps(line, ensure_ascii=False)
    before = _line_count(msg_chain.baseline_path(ws))
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = Inspector(ws).inspect(trigger="manual")
        assert report["msg_inv"]["verdict"] == msg_inv.RED
        assert any(v.get("code") in (msg_inv.CODE_CHAIN_BROKEN,
                                     msg_inv.CODE_HISTORY_REWRITTEN)
                   for v in report["violations"]), \
            "the tamper must surface into the patrol's violations"
        assert _line_count(msg_chain.baseline_path(ws)) == before, \
            "a RED patrol must NOT write a baseline (it would launder " \
            "the tampered state)"
    finally:
        path.write_text(original, encoding="utf-8")
    restored = Inspector(ws).inspect(trigger="manual")
    assert restored["msg_inv"]["verdict"] == msg_inv.GREEN, \
        restored["msg_inv"]["violations"]


# ---------------------------------------------------------------------------
# W-2 — 补偿链写路径
# ---------------------------------------------------------------------------

def test_w2_an_extras_update_writes_a_compensation_row(tmp_path):
    """external 行的 extras 更新 → 补偿文件 +1、DB 置位、MSG-INV-5 仍绿。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)

    seq = store.attach_extras_to_last_assistant(
        "s1", {"audio": {"url": "x.mp3"}})
    assert seq is not None, "the extras update itself failed"

    comp = msg_chain.compensation_path(ws)
    assert comp.is_file(), "the compensation chain was not written"
    rows = msg_chain.read_msg_lines(comp)
    assert len(rows) == 1
    assert rows[0]["kind"] == msg_chain.COMPENSATION_KIND
    assert rows[0]["target_seq"] == seq
    assert rows[0]["extras"] == {"audio": {"url": "x.mp3"}}

    conn = _conn(db)
    try:
        flag = conn.execute(
            "SELECT extras_compensated FROM messages "
            "WHERE session_id='s1' AND seq=?", (seq,)).fetchone()[0]
        assert flag == 1
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
    finally:
        conn.close()


def test_w2_a_second_update_of_the_same_row_stays_green(tmp_path):
    """判据自身的判据 D 的边界：同一行两次 extras 更新 = 两条补偿事件 +
    一个置位标记 → MSG-INV-5 必须仍绿（按去重目标行对账）。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    seq = store.attach_extras_to_last_assistant("s1", {"audio": {"url": "a"}})
    assert seq is not None
    seq2 = store.attach_extras_to_last_assistant("s1", {"audio": {"url": "b"}})
    assert seq2 == seq

    rows = msg_chain.read_msg_lines(msg_chain.compensation_path(ws))
    assert len(rows) == 2, "every update EVENT must be recorded"
    assert [r["seq"] for r in rows] == [1, 2], \
        "the compensation chain is itself a chain"
    conn = _conn(db)
    try:
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
    finally:
        conn.close()


def test_w2_an_inline_row_needs_no_compensation(tmp_path):
    """inline 行的 extras 只在 DB —— 不产生补偿、不置位。"""
    ws, db, store = _workspace(tmp_path)  # nothing externalised
    seq = store.attach_extras_to_last_assistant("s1", {"audio": {"url": "x"}})
    assert seq is not None
    assert not msg_chain.compensation_path(ws).is_file()
    conn = _conn(db)
    try:
        flag = conn.execute(
            "SELECT extras_compensated FROM messages "
            "WHERE session_id='s1' AND seq=?", (seq,)).fetchone()[0]
        assert flag == 0
    finally:
        conn.close()


def test_w2_the_criterion_breaks_when_the_compensation_is_missing(tmp_path):
    """破坏用例：DB 置位但补偿文件没有对应行 → MSG-INV-5 必须红。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        conn.execute("UPDATE messages SET extras_compensated=1 "
                     "WHERE session_id='s1' AND seq=1")
        conn.commit()
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.RED
        assert any(v["code"] == msg_inv.CODE_COMPENSATION_COUNT
                   for v in res["violations"])
        # 还原
        conn.execute("UPDATE messages SET extras_compensated=0 "
                     "WHERE session_id='s1' AND seq=1")
        conn.commit()
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# W-4 — query_status 自曝
# ---------------------------------------------------------------------------

def _manager_for(ws):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    return MemoryManager(MemoryConfig(workspace_root=str(ws)))


def test_w4_get_status_exposes_the_anchor_scope_and_cold_read(tmp_path):
    """get_status 必须携带两个新键；外置库上冷读延迟是实测数字。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    mgr = _manager_for(ws)
    try:
        status = mgr.get_status()
    finally:
        mgr.close()
    assert status["msg_chain_anchor_scope"] == "same_dir"
    latency = status["cold_read_latency_ms"]
    assert isinstance(latency, float) and latency >= 0, \
        "an externalised library must MEASURE its cold read, got %r" % (
            latency,)


def test_w4_a_library_without_messages_reports_not_applicable(tmp_path):
    """没有外置消息的库：键在、值为 None —— 「不适用」不许伪装成「零延迟」。"""
    ws, db, store = _workspace(tmp_path)  # no externalisation
    mgr = _manager_for(ws)
    try:
        status = mgr.get_status()
    finally:
        mgr.close()
    assert status["msg_chain_anchor_scope"] == "same_dir"
    assert status["cold_read_latency_ms"] is None


# ---------------------------------------------------------------------------
# W-5 — 阶段 4 崩溃：禁止断点续跑；整批回滚
# ---------------------------------------------------------------------------

def _crash_after_write(ws, db, monkeypatch):
    """Simulate a stage-4 crash BETWEEN the JSONL write and the DB
    emptying (the anchor write raises).  The patch is UNDONE before
    returning: the crash is an event, not a permanent state."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash after the file write")
    monkeypatch.setattr(msg_chain, "append_anchor", boom)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(Exception):
            msg_migrate.externalise_all(conn, ws, "",
                                        batch_date="2026-10-31")
    finally:
        conn.close()
        monkeypatch.undo()


def test_w5_a_crashed_stage4_cannot_double_write(tmp_path, monkeypatch):
    """崩溃后直接重跑 externalise：双写守卫必须拒绝，JSONL 行数不变。"""
    ws, db, store = _workspace(tmp_path)
    _crash_after_write(ws, db, monkeypatch)

    path = _jsonl_of(ws)
    lines_after_crash = _line_count(path)
    assert lines_after_crash == 4, "the crashed run wrote the file"
    conn = _conn(db)
    try:
        still_inline = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NOT NULL"
        ).fetchone()[0]
        assert still_inline == 4, "the crash left the DB untouched"
    finally:
        conn.close()

    # Resume is FORBIDDEN: the guard refuses, and nothing is appended.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(msg_migrate.MsgMigrationError) as exc:
            msg_migrate.externalise_all(conn, ws, "", batch_date="2026-10-31")
        assert "DOUBLE-WRITE" in str(exc.value)
    finally:
        conn.close()
    assert _line_count(path) == lines_after_crash, \
        "the guard must fire BEFORE anything is appended"


def test_w5_the_whole_batch_rollback_allows_a_clean_rerun(tmp_path,
                                                          monkeypatch):
    """整批回滚 = backup API 恢复 + 删当批目录；回滚后重跑必须干净全绿。"""
    ws, db, store = _workspace(tmp_path)
    backup = ws / "memory" / ".backup" / "msg" / "messages.p1-bak.db"
    msg_migrate.backup_database(db, backup)
    _crash_after_write(ws, db, monkeypatch)

    rep = msg_migrate.rollback_externalise_batch(
        db, backup, ws, batch_date="2026-10-31")
    assert rep["restore"]["integrity_check"] == "ok"
    assert rep["removed"], "the crashed batch's directory must be removed"
    assert not (msg_chain.export_root(ws) / "2026-10-31").exists()

    conn = _conn(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NULL"
        ).fetchone()[0] == 0, "the restore must bring the content back"
    finally:
        conn.close()

    # The clean re-run: succeeds, and every criterion is green.
    _externalise(ws, db)
    msg_inv.write_baseline(ws)
    conn = _conn(db)
    try:
        for check in (msg_inv.check_msg_inv1(ws),
                      msg_inv.check_msg_inv2(ws),
                      msg_inv.check_msg_inv3(ws),
                      msg_inv.check_msg_inv4(ws),
                      msg_inv.check_msg_inv5(ws, conn, ""),
                      msg_inv.check_msg_inv6(ws, conn)):
            assert check["verdict"] == msg_inv.GREEN, \
                "%s: %s" % (check["inv"], check["violations"])
    finally:
        conn.close()


def test_w5_a_rollback_without_the_backup_restores_nothing(tmp_path):
    """反向用例：backup API 不是 cp —— 恢复后行数与备份时一致（不多不少）。"""
    ws, db, store = _workspace(tmp_path)
    backup = ws / "memory" / ".backup" / "msg" / "messages.p1-bak.db"
    msg_migrate.backup_database(db, backup)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        external = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content_state='external'"
        ).fetchone()[0]
        assert external == 4
    finally:
        conn.close()
    rep = msg_migrate.rollback_externalise_batch(
        db, backup, ws, batch_date="2026-10-31")
    assert rep["restore"]["messages"] == 4, \
        "the restore resurrects NOTHING and drops NOTHING"
    conn = _conn(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NULL"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 任务二 — 账本孤儿：写入边界防御
# ---------------------------------------------------------------------------

def _chunk(cid, path, start, end):
    return MemoryChunk(
        id=cid, user_id=None, scope="shared", source="memory", path=path,
        start_line=start, end_line=end, text="text of %s" % cid,
        embedding=None, hash="h-%s" % cid, metadata=None,
        body_path=path, body_anchor="L%d-%d" % (start, end),
        body_hash="bh-%s" % cid, text_state="projection",
        body_origin="file")


def _library_with_ledger(tmp_path):
    """A library with 2 chunks of one file, both certified."""
    ws = tmp_path / "ws"
    db_dir = ws / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    storage = MemoryStorage(db_path=str(db))
    storage.conn.execute(body_guard.cert_table_ddl())
    c1, c2 = _chunk("c-aaa", "memory/page.md", 1, 5), \
        _chunk("c-bbb", "memory/page.md", 6, 10)
    storage.replace_file_chunks("memory/page.md", [c1, c2], file_hash="H",
                                mtime=1, size=1, chunker_version=1)
    for cid in ("c-aaa", "c-bbb"):
        assert body_guard.record_certification(
            storage.conn, cid, "mirror", "file", "memory/page.md", "bh",
            certified_by="test")
    storage.conn.commit()
    return ws, db, storage


def test_ledger_a_recut_prunes_the_orphaned_witness(tmp_path):
    """重切后 c-bbb 消失：它的证人条目必须在同事务被清；c-aaa 的必须留
    （S3-L 恢复机制读它）；M6f 必须 PASS。"""
    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        # The re-cut: c-aaa survives (same id), c-bbb is gone.
        c1 = _chunk("c-aaa", "memory/page.md", 1, 5)
        storage.replace_file_chunks("memory/page.md", [c1], file_hash="H2",
                                    mtime=2, size=1, chunker_version=2)
        ids = {r[0] for r in storage.conn.execute(
            "SELECT id FROM cert_ledger")}
        assert "c-aaa" in ids, \
            "the surviving chunk's witness was pruned — that breaks S3-L"
        assert "c-bbb" not in ids, \
            "the gone chunk's witness survived — the orphan is back"
        verdict = ledger_orphans.judge_m6f_ledger_has_no_orphans(
            storage.conn)
        assert verdict["verdict"] == "PASS", verdict
    finally:
        storage.close()


def test_ledger_delete_by_path_prunes_the_witness(tmp_path):
    """删文件：其 chunk 的证人条目必须随删除同事务清掉，M6f PASS。"""
    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        storage.delete_by_path("memory/page.md")
        n = storage.conn.execute(
            "SELECT COUNT(*) FROM cert_ledger").fetchone()[0]
        assert n == 0, "deleting the file left %d witness(es) behind" % n
        verdict = ledger_orphans.judge_m6f_ledger_has_no_orphans(
            storage.conn)
        assert verdict["verdict"] == "PASS", verdict
    finally:
        storage.close()


def test_ledger_prune_is_atomic_with_the_recut(tmp_path):
    """破坏用例：重切中途失败（files 行写入前抛错）→ 整事务回滚，
    chunk 和证人条目都在 —— 不许出现「chunk 回来了、证人没了」。"""
    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        c1 = _chunk("c-aaa", "memory/page.md", 1, 5)

        class _Boom(Exception):
            pass

        def bad_upsert(records):
            raise _Boom("vector backend down mid-recut")
        storage.vector_backend.upsert = bad_upsert
        with pytest.raises(_Boom):
            storage.replace_file_chunks("memory/page.md", [c1],
                                        file_hash="H2", mtime=2, size=1,
                                        chunker_version=2)
        # The whole transaction rolled back: BOTH the old chunks AND
        # their witnesses are still there.
        chunks = {r[0] for r in storage.conn.execute(
            "SELECT id FROM chunks")}
        ledger = {r[0] for r in storage.conn.execute(
            "SELECT id FROM cert_ledger")}
        assert chunks == {"c-aaa", "c-bbb"}, chunks
        assert ledger == {"c-aaa", "c-bbb"}, ledger
    finally:
        storage.close()


def test_ledger_a_library_without_a_ledger_still_recuts(tmp_path):
    """没有 cert_ledger 表的库（旧库、从未装过守卫的形状）：重切必须
    正常工作 —— 级联清理不许把「没有账本」变成错误。

    实测约束（S3-L）：守卫触发器（chunks_body_keep_bu / *_ai）的 body
    引用 cert_ledger，所以「有守卫、没账本」的库上任何 chunks 写入都会
    炸 —— 这是 S3-L 量出来的既有行为，不是本轮引入的。  因此本用例模拟
    的是「从未装过守卫的旧库」：连引用账本的触发器一起没有。

    **交办 B (2026-11-05): the ledger guard's triggers join that list.**
    The new ``chunks_cert_revoke_bd`` and ``chunks_cert_restore_bi``
    name ``cert_ledger`` and ``cert_revoked`` in their bodies, so a
    library with neither table cannot carry them either.  MEASURED
    (2026-11-05): SQLite does **not** short-circuit the AND in a
    trigger's WHEN clause — a trigger whose body names a missing table
    fails at RUN time with "no such table", even when the WHEN clause
    checks ``sqlite_master`` first.  So the shape that must be
    tolerated is "no ledger, and no trigger that names it" — exactly
    the shape this test simulates.
    """
    ws = tmp_path / "ws"
    db_dir = ws / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    storage = MemoryStorage(db_path=str(db))
    try:
        storage.conn.execute("DROP TABLE IF EXISTS cert_ledger")
        storage.conn.execute("DROP TABLE IF EXISTS cert_revoked")
        for trig in ("chunks_body_keep_bu", "chunks_body_keep_bi_ai",
                     "chunks_body_keep_st_ai", "chunks_cert_revoke_bd",
                     "chunks_cert_restore_bi"):
            storage.conn.execute("DROP TRIGGER IF EXISTS %s" % trig)
        storage.conn.commit()
        c1 = _chunk("c-aaa", "memory/page.md", 1, 5)
        storage.replace_file_chunks("memory/page.md", [c1], file_hash="H",
                                    mtime=1, size=1, chunker_version=1)
        storage.replace_file_chunks("memory/page.md", [], file_hash="H2",
                                    mtime=2, size=1, chunker_version=2)
        assert storage.conn.execute(
            "SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    finally:
        storage.close()


def test_ledger_the_detector_still_catches_a_foreign_delete(tmp_path):
    """M6f 兜底仍有效：绕过写路径直接 DELETE chunk（外部写入者的形状）
    → 孤儿产生 → M6f 必须 FAIL。

    **交办 B (2026-11-05): the premise moved to the DATABASE layer.**
    The ledger guard now revokes the witness at the write boundary, so
    a foreign writer's raw DELETE no longer leaves an orphan behind —
    MEASURED on this machine, 2026-11-05:

        with the guard:      DELETE FROM chunks WHERE id='c-bbb'
                             => ledger=['c-aaa']  orphans=0
        with the guard gone: the same DELETE
                             => ledger=['c-aaa','c-bbb']  orphans=1

    So the shape that still produces an orphan is a **witness whose
    chunk is not in the index** — which is what the detector's own
    subject is.  That is what this test now asserts, and it is the
    stronger form: the detector must report an orphan that exists, no
    matter which mechanism created it.
    """
    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        # The detector's own subject: a witness for a chunk that is not
        # in the index.
        storage.conn.execute(
            "INSERT OR REPLACE INTO cert_ledger (id, text_state, "
            "body_origin, body_path, body_hash, certified_at, "
            "certified_by) VALUES ('never-existed', 'mirror', 'file', "
            "'memory/never-existed.md', 'bh-none', 1, 'foreign')")
        storage.conn.commit()
        verdict = ledger_orphans.judge_m6f_ledger_has_no_orphans(
            storage.conn)
        assert verdict["verdict"] == "FAIL", verdict
        assert verdict["orphans"] == 1, verdict
    finally:
        storage.close()


def test_the_guard_revokes_the_witness_of_a_foreign_delete(tmp_path):
    """**交办 B (2026-11-05): the guard's new half.**

    A foreign writer's raw DELETE (which does NOT go through the write
    path) must not leave an orphan behind: the guard revokes the
    witness at the write boundary, and the revocation is LOGGED (so the
    orphan is explained, not erased).

    And the guard's absence must bring the orphan back — so this test
    also proves the guard is what prevents it.
    """
    from agent.memory import body_guard as bg

    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        storage.conn.execute("DELETE FROM chunks WHERE id='c-bbb'")
        storage.conn.commit()
        orphans = storage.conn.execute(
            "SELECT COUNT(*) FROM cert_ledger l WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.id=l.id)").fetchone()[0]
        assert orphans == 0, (
            "外来 DELETE 之后必须有 0 个孤儿 —— 守卫在写边界撤销了证人")
        revoked = storage.conn.execute(
            "SELECT id, text_state, certified_by, revoked_reason "
            "FROM cert_revoked").fetchall()
        assert len(revoked) == 1, (
            "撤销必须留痕 —— 孤儿要『被解释』，不是被抹掉")
        row = revoked[0]
        assert row["id"] == "c-bbb", row
        assert row["text_state"] == "mirror", row
        assert row["certified_by"] == "test", row
        assert row["revoked_reason"], row
    finally:
        storage.close()

    # And with the guard gone, the orphan comes back — so the guard is
    # what prevents it.
    ws2, db2, storage2 = _library_with_ledger(tmp_path / "two")
    try:
        for name in bg.guard_trigger_names():
            storage2.conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        storage2.conn.commit()
        storage2.conn.execute("DELETE FROM chunks WHERE id='c-bbb'")
        storage2.conn.commit()
        orphans = storage2.conn.execute(
            "SELECT COUNT(*) FROM cert_ledger l WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.id=l.id)").fetchone()[0]
        assert orphans == 1, (
            "拿掉守卫 ⇒ 孤儿必须回来 —— 否则这条判据测的不是守卫")
    finally:
        storage2.close()
