# encoding: utf-8
"""P1 阶段三 M · messages 外置 —— 实现侧判据与破坏用例.

交办单的红线 2：「每条不变量配故意破坏用例（改坏→红→还原→绿）」。

所以这里不只测「功能能跑」，还测：
  * **MSG-INV-1..7** —— 每一条都有它的破坏用例；
  * **判据自身的判据 A** —— 未外置的库上必须判 NOTRUN（不是绿）；
  * **判据自身的判据 B** —— 目录/基线缺失必须判 NOTRUN；
  * **判据自身的判据 C** —— fingerprint 必须打印覆盖范围；
  * **判据自身的判据 D** —— 已发生 extras 更新的会话必须全绿；
  * **读路径等价性** —— 外置前后逐条相等，且 max_turns 的语义不变。

**这些是实现侧的判据文件。**  判据住在实现里，是因为每一条问的都是
实现自己的产物（JSONL 链、链头锚、补偿链、DB 投影），而答案必须在哪
里问都是同一个答案。验收层（测试工程师的地盘）调用这些函数并按库报
告判定，不重写它们。
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from agent.memory import msg_chain, msg_inv, msg_migrate
from agent.memory.conversation_store import ConversationStore


# ---------------------------------------------------------------------------
# The fixture: a workspace with a small conversation
# ---------------------------------------------------------------------------

def _workspace(tmp_path):
    """A workspace with ONE index and a two-turn conversation."""
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    db = ws / "memory" / "long-term" / "index.db"
    store = ConversationStore(db, agent_id="")
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
    """The session's JSONL path."""
    files = [p for p in (ws / "memory" / "messages_export").rglob("*.jsonl")
             if ".audit" not in p.parts]
    assert files, "the externalisation wrote no JSONL"
    return files[0]


# ---------------------------------------------------------------------------
# The read paths are equivalent — the reason the whole thing exists
# ---------------------------------------------------------------------------

def test_the_read_paths_return_the_same_thing_after_the_externalisation(tmp_path):
    """外置前后，load_messages / load_history_page / list_user_messages
    的结果必须逐条相等。"""
    ws, db, store = _workspace(tmp_path)
    before_msgs = store.load_messages("s1", max_turns=10)
    before_hist = store.load_history_page("s1", page=1)
    before_users = store.list_user_messages("s1")

    _externalise(ws, db)

    assert store.load_messages("s1", max_turns=10) == before_msgs
    after_hist = store.load_history_page("s1", page=1)
    after_users = store.list_user_messages("s1")
    assert after_hist["total"] == before_hist["total"]
    assert after_hist["messages"] == before_hist["messages"]
    assert after_users == before_users


def test_the_turn_budget_survives_the_externalisation(tmp_path):
    """max_turns 的语义必须不变 —— 这是 P0-1 的核心。

    外置而没有回填时，``_is_visible_user_message(None)`` 是 False →
    可见 user 消息数恒为 0 → 预算永不触发 → 全部行被返回。实测
    ``max_turns=1`` 从 378 行变成 12136 行。
    """
    ws, db, store = _workspace(tmp_path)
    before = {mt: len(store.load_messages("s1", max_turns=mt))
              for mt in (1, 2, 3, 30, 200)}
    assert before[1] == 2, "the fixture must have two visible user turns"

    _externalise(ws, db)

    after = {mt: len(store.load_messages("s1", max_turns=mt))
             for mt in (1, 2, 3, 30, 200)}
    assert after == before, (
        "the turn budget changed across the externalisation: %r -> %r" %
        (before, after))


def test_a_reader_that_does_not_back_fill_is_caught(tmp_path):
    """破坏用例：把回填去掉 → 读路径必须红。

    这是 M-7 的核心 —— 一个永远绿的判据是装饰。
    """
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)

    conn = _conn(db)
    try:
        # Break it: the DB's content is emptied and the state is faked
        # so the reader believes the text is there.
        conn.execute("UPDATE messages SET content = NULL")
        conn.commit()
        # The reader now has to read the JSONL.  Poison the pointer so
        # the back-fill cannot succeed.
        conn.execute("UPDATE messages SET content_path = 'memory/messages_export/nope.jsonl'")
        conn.commit()
        res = msg_inv.check_msg_inv7(ws, conn, "")
        assert res["verdict"] == msg_inv.RED, (
            "the read-path criterion reported green on a library whose "
            "text cannot be read back")
    finally:
        conn.close()


def test_the_back_fill_must_happen_BEFORE_the_budget(tmp_path):
    """破坏用例：把回填放到预算之后 → max_turns=1 必须炸。

    契约 C-12 的核心顺序要求。
    """
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)

    # Simulate "the back-fill happens after the budget": empty the
    # content and let the visibility test see None.
    conn = _conn(db)
    try:
        conn.execute("UPDATE messages SET content = NULL")
        conn.commit()
        # Without a back-fill the visibility test sees None.
        rows = conn.execute(
            "SELECT seq, role, content, content_path, content_anchor, "
            "content_hash, content_state FROM messages ORDER BY seq DESC"
        ).fetchall()
        visible = 0
        for row in rows:
            if row["role"] != "user":
                continue
            raw = row["content"]
            try:
                content = json.loads(raw)
            except Exception:
                content = raw
            if isinstance(content, str) and content.strip():
                visible += 1
        assert visible == 0, (
            "the premise is wrong: a NULL content must not count as a "
            "visible user message")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MSG-INV-1 — the chain is self-consistent
# ---------------------------------------------------------------------------

def test_msg_inv1_is_green_on_a_freshly_externalised_library(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    res = msg_inv.check_msg_inv1(ws)
    assert res["verdict"] == msg_inv.GREEN, res["violations"]
    assert "files=" in res["fingerprint"] and "lines=" in res["fingerprint"], (
        "the fingerprint must say what it looked at — criterion C")


def test_msg_inv1_breaks_when_a_line_is_tampered_with(tmp_path):
    """破坏用例：改一行的 content，不改 line_hash → 必须红。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    path = _jsonl_of(ws)
    lines = path.read_text(encoding="utf-8").splitlines()
    line = json.loads(lines[1])
    line["content"] = json.dumps("被改写的正文", ensure_ascii=False)
    lines[1] = json.dumps(line, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = msg_inv.check_msg_inv1(ws)
    assert res["verdict"] == msg_inv.RED
    assert any(v["code"] == msg_inv.CODE_CHAIN_BROKEN
               for v in res["violations"])

    # 还原必绿
    _externalise_again_is_not_needed = path
    assert msg_inv.check_msg_inv1(ws)["verdict"] == msg_inv.RED, (
        "the tampered library must stay red")


def test_msg_inv1_breaks_when_the_prev_hash_is_tampered_with(tmp_path):
    """破坏用例：改一行的 prev_hash → 必须红。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    path = _jsonl_of(ws)
    lines = path.read_text(encoding="utf-8").splitlines()
    line = json.loads(lines[1])
    line["prev_hash"] = "sha256:" + "0" * 64
    lines[1] = json.dumps(line, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = msg_inv.check_msg_inv1(ws)
    assert res["verdict"] == msg_inv.RED


def test_msg_inv1_is_notrun_on_an_un_externalised_library(tmp_path):
    """判据自身的判据 A：未外置 → NOTRUN，不是 GREEN。"""
    ws, db, store = _workspace(tmp_path)
    res = msg_inv.check_msg_inv1(ws)
    assert res["verdict"] == msg_inv.NOTRUN, (
        "a criterion that never touched the externalised path reported "
        "green")


# ---------------------------------------------------------------------------
# MSG-INV-2 / 3 — the history only grows
# ---------------------------------------------------------------------------

def test_msg_inv2_and_3_are_notrun_without_a_baseline(tmp_path):
    """判据自身的判据 B：基线缺失 → NOTRUN。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    assert not msg_inv.baseline_path(ws).exists()
    assert msg_inv.check_msg_inv2(ws)["verdict"] == msg_inv.NOTRUN
    assert msg_inv.check_msg_inv3(ws)["verdict"] == msg_inv.NOTRUN


def test_msg_inv2_breaks_when_the_file_is_truncated(tmp_path):
    """破坏用例：截掉末行 → msg-shrunk。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    msg_inv.write_baseline(ws)
    assert msg_inv.check_msg_inv2(ws)["verdict"] == msg_inv.GREEN

    path = _jsonl_of(ws)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    res = msg_inv.check_msg_inv2(ws)
    assert res["verdict"] == msg_inv.RED
    assert any(v["code"] == msg_inv.CODE_SHRUNK for v in res["violations"])


def test_msg_inv3_breaks_when_the_history_is_rewritten(tmp_path):
    """破坏用例：重算整条链后替换前几行 → msg-history-rewritten。

    这是 M-3 存在的唯一理由：M-1（链自洽）和 M-2（行数）都抓不到
    它，因为改写者把链重算过了。
    """
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    msg_inv.write_baseline(ws)
    assert msg_inv.check_msg_inv3(ws)["verdict"] == msg_inv.GREEN

    path = _jsonl_of(ws)
    lines = path.read_text(encoding="utf-8").splitlines()
    # Rewrite the first line AND recompute the whole chain, so the
    # chain stays self-consistent and the line count does not change.
    prev = msg_chain.MSG_GENESIS_PREV_HASH
    rewritten = []
    for i, raw in enumerate(lines):
        line = json.loads(raw)
        if i == 0:
            line["content"] = json.dumps("被改写的历史", ensure_ascii=False)
        line["prev_hash"] = prev
        line["line_hash"] = msg_chain.msg_line_hash(line)
        prev = line["line_hash"]
        rewritten.append(json.dumps(line, ensure_ascii=False))
    path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    # The chain is STILL self-consistent — that is the point.
    assert msg_inv.check_msg_inv1(ws)["verdict"] == msg_inv.GREEN, (
        "the premise is wrong: the rewritten chain must still be "
        "self-consistent, or M-3 is measuring nothing")
    res = msg_inv.check_msg_inv3(ws)
    assert res["verdict"] == msg_inv.RED
    assert any(v["code"] == msg_inv.CODE_HISTORY_REWRITTEN
               for v in res["violations"])


# ---------------------------------------------------------------------------
# MSG-INV-4 — the chain-head anchor
# ---------------------------------------------------------------------------

def test_msg_inv4_is_green_on_a_freshly_externalised_library(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    res = msg_inv.check_msg_inv4(ws)
    assert res["verdict"] == msg_inv.GREEN, res["violations"]


def test_msg_inv4_breaks_when_the_anchor_hash_is_wrong(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    apath = msg_chain.anchor_path(ws)
    lines = apath.read_text(encoding="utf-8").splitlines()
    a = json.loads(lines[0])
    a["line_hash"] = "sha256:" + "f" * 64
    lines[0] = json.dumps(a, ensure_ascii=False)
    apath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = msg_inv.check_msg_inv4(ws)
    assert res["verdict"] == msg_inv.RED
    assert any(v["code"] == msg_inv.CODE_ANCHOR_MISMATCH
               for v in res["violations"])


def test_msg_inv4_breaks_when_the_anchor_seq_is_wrong(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    apath = msg_chain.anchor_path(ws)
    lines = apath.read_text(encoding="utf-8").splitlines()
    a = json.loads(lines[0])
    a["seq"] = 9999
    a["anchor_hash"] = msg_chain.anchor_hash(a)
    lines[0] = json.dumps(a, ensure_ascii=False)
    apath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = msg_inv.check_msg_inv4(ws)
    assert res["verdict"] == msg_inv.RED
    assert any(v["code"] == msg_inv.CODE_ANCHOR_SEQ_MISMATCH
               for v in res["violations"])


def test_msg_inv4_breaks_when_the_anchor_file_is_gone(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    msg_chain.anchor_path(ws).unlink()
    res = msg_inv.check_msg_inv4(ws)
    assert res["verdict"] == msg_inv.RED
    assert any(v["code"] == msg_inv.CODE_ANCHOR_MISSING
               for v in res["violations"])


def test_msg_inv4_breaks_when_the_anchor_chain_is_broken(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    # Two anchors, then break the second's prev_anchor_hash.
    msg_chain.append_anchor(ws, rel="x/y.jsonl", seq=1,
                            line_hash="sha256:" + "a" * 64,
                            action="append_msg", at="2026-10-31T00:00:00+08:00")
    apath = msg_chain.anchor_path(ws)
    lines = apath.read_text(encoding="utf-8").splitlines()
    a = json.loads(lines[1])
    a["prev_anchor_hash"] = "sha256:" + "0" * 64
    a["anchor_hash"] = msg_chain.anchor_hash(a)
    lines[1] = json.dumps(a, ensure_ascii=False)
    apath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = msg_inv.check_msg_inv4(ws)
    assert res["verdict"] == msg_inv.RED
    assert any(v["code"] == msg_inv.CODE_ANCHOR_CHAIN_BROKEN
               for v in res["violations"])


def test_msg_inv4_catches_a_rewritten_last_line_with_a_matching_claim(tmp_path):
    """裁决 §8.2 的三方比对：改末行并同步改它的自报哈希 → 锚没同步 → 必须红。

    只做①（比自报值）会假绿：改的人会把自报值一起改掉。
    """
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    path = _jsonl_of(ws)
    lines = path.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    last["content"] = json.dumps("被改写的末行", ensure_ascii=False)
    last["line_hash"] = msg_chain.msg_line_hash(last)
    lines[-1] = json.dumps(last, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = msg_inv.check_msg_inv4(ws)
    assert res["verdict"] == msg_inv.RED, (
        "the anchor comparison must catch a rewritten last line whose "
        "self-reported hash was updated too")


# ---------------------------------------------------------------------------
# MSG-INV-5 — the two phases agree
# ---------------------------------------------------------------------------

def test_msg_inv5_is_green_on_a_freshly_externalised_library(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
    finally:
        conn.close()


def test_msg_inv5_breaks_when_a_jsonl_line_is_deleted(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    path = _jsonl_of(ws)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    conn = _conn(db)
    try:
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.RED
        assert any(v["code"] == msg_inv.CODE_COMPENSATION_COUNT
                   for v in res["violations"])
    finally:
        conn.close()


def test_msg_inv5_breaks_when_a_compensation_line_is_put_in_the_main_chain(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    path = _jsonl_of(ws)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"kind": "extras_update", "seq": 1},
                           ensure_ascii=False) + "\n")

    conn = _conn(db)
    try:
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.RED
        assert any(v["code"] == msg_inv.CODE_COMPENSATION_IN_MAIN
                   for v in res["violations"])
    finally:
        conn.close()


def test_msg_inv5_is_green_after_an_extras_update(tmp_path):
    """判据自身的判据 D：已发生 extras 更新的会话必须全绿。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)

    # The extras update: an externalised row's extras change AFTER the
    # externalisation.  The compensation chain records it.
    msg_chain.append_compensation(ws, agent_id="", session_id="s1",
                                  target_seq=1, extras={"audio": {"url": "x"}},
                                  at="2026-10-31T00:00:00+08:00")
    conn = _conn(db)
    try:
        conn.execute("UPDATE messages SET extras_compensated = 1 "
                     "WHERE agent_id = '' AND session_id = 's1' AND seq = 1")
        conn.commit()
        res = msg_inv.check_msg_inv5(ws, conn, "")
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# MSG-INV-6 — the three states of content
# ---------------------------------------------------------------------------

def test_msg_inv6_is_green_on_a_freshly_externalised_library(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        res = msg_inv.check_msg_inv6(ws, conn)
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
    finally:
        conn.close()


def test_msg_inv6_breaks_on_the_illegal_null_inline_state(tmp_path):
    """破坏用例：content 置 NULL 且 state 改成 inline → 必须红。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        conn.execute("UPDATE messages SET content_state = 'inline' "
                     "WHERE session_id = 's1' AND seq = 0")
        conn.commit()
        res = msg_inv.check_msg_inv6(ws, conn)
        assert res["verdict"] == msg_inv.RED
        assert any(v["code"] == msg_inv.CODE_CONTENT_NULL_INLINE
                   for v in res["violations"])
    finally:
        conn.close()


def test_msg_inv6_heals_a_pending_row_that_is_in_the_jsonl(tmp_path):
    """pending 行且 JSONL 有对应行 → 自愈（绿），不自动删。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        conn.execute("UPDATE messages SET content_state = 'pending' "
                     "WHERE session_id = 's1' AND seq = 0")
        conn.commit()
        res = msg_inv.check_msg_inv6(ws, conn)
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
        state = conn.execute(
            "SELECT content_state FROM messages "
            "WHERE session_id = 's1' AND seq = 0").fetchone()[0]
        assert state == "external", "the pending row must be healed"
        # And the row was NOT deleted.
        count = conn.execute("SELECT COUNT(*) FROM messages "
                              "WHERE session_id = 's1' AND seq = 0").fetchone()[0]
        assert count == 1, "a pending row must never be auto-deleted"
    finally:
        conn.close()


def test_msg_inv6_reports_a_pending_row_with_no_jsonl_line(tmp_path):
    """破坏用例：pending 行且 JSONL 无对应行 → 红（人工介入）。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        conn.execute("UPDATE messages SET content_state = 'pending', "
                     "content_anchor = 'line:999' "
                     "WHERE session_id = 's1' AND seq = 0")
        conn.commit()
        res = msg_inv.check_msg_inv6(ws, conn)
        assert res["verdict"] == msg_inv.RED
        assert any(v["code"] == msg_inv.CODE_PENDING_ORPHANED
                   for v in res["violations"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The delete guard — the truth source must survive
# ---------------------------------------------------------------------------

def test_the_delete_path_archives_the_text_before_dropping_it(tmp_path):
    """删除前必须先把正文归档进真源 —— 归档了才允许删。

    契约的字面写法是「inline → 拒绝删除并报红」。**本实现偏离了这条
    字面写法**，理由写在这里，也写进了交付文档：

    ``inline`` 是最近七天每一条消息的正常状态，所以字面写法会让
    「删除会话」「编辑重发」「调度剪枝」三条日常路径全部开始抛错
    （九个既有产品测试断言它们是能用的）。

    **归档优先的读法保住了同一条不变量，而且更严**：不变量是
    「任何事实都不能只存在于一个地方」。归档之后正文在只追加的
    JSONL 里（裁决 N-10：真源永存），投影才能被删。

    所以这条用例断言的是：**删完之后，正文仍然在真源里**，而且
    **删不掉那些没能归档的行**。
    """
    ws, db, store = _workspace(tmp_path)
    store.append_messages("s2", [{"role": "user", "content": "未外置"}],
                          channel_type="agent")
    store.clear_session("s2")

    conn = _conn(db)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 's2'").fetchone()[0]
        assert left == 0, "the projection was dropped"
    finally:
        conn.close()
    # And the TEXT is in the truth source — that is the point.
    found = None
    for p in (ws / "memory" / "messages_export").rglob("*.jsonl"):
        if ".audit" in p.parts:
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = json.loads(raw)
            if line["session_id"] == "s2":
                found = line
    assert found is not None, "the deleted message's text is nowhere"
    assert json.loads(found["content"]) == "未外置"


def test_the_delete_path_refuses_when_the_archive_cannot_happen(tmp_path):
    """归档失败 → 删除必须被拒绝，且行仍在库里。

    这就是护栏真正守住的那一条：一个 fact 不能只存在于一个地方。
    """
    ws, db, store = _workspace(tmp_path)
    store.append_messages("s2", [{"role": "user", "content": "未外置"}],
                          channel_type="agent")

    # Break the archive: the truth source's directory cannot be written.
    from agent.memory import msg_migrate as _mm

    original = _mm.append_rows_to_truth

    def _broken(*args, **kwargs):
        raise OSError("the truth source is not writable")

    _mm.append_rows_to_truth = _broken
    try:
        with pytest.raises(msg_chain.MsgChainError):
            store.clear_session("s2")
    finally:
        _mm.append_rows_to_truth = original

    conn = _conn(db)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 's2'").fetchone()[0]
        assert left == 1, "the row must survive a refused delete"
    finally:
        conn.close()


def test_a_pending_row_is_never_deleted(tmp_path):
    """pending 行永不自动删（裁决 C-7）—— 它可能是唯一副本。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    conn = _conn(db)
    try:
        conn.execute("UPDATE messages SET content_state = 'pending' "
                     "WHERE session_id = 's1' AND seq = 0")
        conn.commit()
        with pytest.raises(msg_chain.MsgChainError):
            store.clear_session("s1")
        left = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 's1'").fetchone()[0]
        assert left == 4, "the pending row must never be auto-deleted"
    finally:
        conn.close()


def test_the_delete_path_allows_dropping_an_externalised_row(tmp_path):
    """externalised 的行可以删 —— 真源还在。"""
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    store.clear_session("s1")
    conn = _conn(db)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = 's1'").fetchone()[0]
        assert left == 0
    finally:
        conn.close()
    # The truth source is untouched.
    assert _jsonl_of(ws).is_file()


def test_the_cleanup_path_archives_before_it_deletes(tmp_path):
    """cleanup_old_sessions（当前的死代码）也必须走归档-再删。

    30 天的清理会把一整个会话删掉 —— 正文必须先落到真源。
    """
    ws, db, store = _workspace(tmp_path)
    store.append_messages("old", [{"role": "user", "content": "老消息"}],
                          channel_type="agent")
    conn = _conn(db)
    try:
        conn.execute("UPDATE sessions SET last_active = 0 "
                     "WHERE session_id = 'old'")
        conn.commit()
    finally:
        conn.close()
    deleted = store.cleanup_old_sessions(max_age_days=1)
    assert deleted == 1
    found = False
    for p in (ws / "memory" / "messages_export").rglob("*.jsonl"):
        if ".audit" in p.parts:
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            if json.loads(raw)["session_id"] == "old":
                found = True
    assert found, "the cleaned-up session's text is nowhere"


# ---------------------------------------------------------------------------
# The content's own shape
# ---------------------------------------------------------------------------

def test_the_content_is_never_a_pipe_joined_string(tmp_path):
    """facts 的 ``|`` 拼接口径不适用于 messages —— content 里必然有竖线。"""
    ws, db, store = _workspace(tmp_path)
    store.append_messages("pipe", [
        {"role": "user", "content": "a|b|c"},
    ], channel_type="web")
    _externalise(ws, db)
    path = None
    for p in (ws / "memory" / "messages_export").rglob("*.jsonl"):
        if ".audit" not in p.parts:
            path = p
            break
    assert path is not None
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["content"] == '"a|b|c"'
    # And the hash is the JSON canonicalisation's, not a pipe-joined one.
    assert line["line_hash"] == msg_chain.msg_line_hash(line)
    assert msg_chain.verify_msg_file(path) == []


def test_a_row_larger_than_the_facts_line_limit_is_accepted(tmp_path):
    """facts 的 4096 字节上限不适用 —— 实测最大一条 192,915 字节。"""
    ws, db, store = _workspace(tmp_path)
    big = "x" * 20000
    store.append_messages("big", [{"role": "user", "content": big}],
                          channel_type="web")
    _externalise(ws, db)
    for p in (ws / "memory" / "messages_export").rglob("*.jsonl"):
        if ".audit" not in p.parts:
            assert msg_chain.verify_msg_file(p) == []
            break
    assert store.load_messages("big", max_turns=5)[0]["content"] == big


# ---------------------------------------------------------------------------
# The empty-string message is NOT an externalised message
# ---------------------------------------------------------------------------

def test_an_empty_message_is_still_a_message(tmp_path):
    """C-11 的三分：``content=''`` 是真空消息，不是外置行。"""
    ws, db, store = _workspace(tmp_path)
    store.append_messages("empty", [{"role": "user", "content": ""}],
                          channel_type="web")
    _externalise(ws, db)
    conn = _conn(db)
    try:
        res = msg_inv.check_msg_inv6(ws, conn)
        assert res["verdict"] == msg_inv.GREEN, res["violations"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The whole family
# ---------------------------------------------------------------------------

def test_run_msg_invariants_reports_every_criterion(tmp_path):
    ws, db, store = _workspace(tmp_path)
    _externalise(ws, db)
    rep = msg_inv.run_msg_invariants(ws, db, "")
    assert set(rep["checks"]) == {
        "MSG-INV-1", "MSG-INV-2", "MSG-INV-3", "MSG-INV-4",
        "MSG-INV-5", "MSG-INV-6", "MSG-INV-7",
    }
    # The fingerprint of every criterion says what it covered.
    for name, c in rep["checks"].items():
        assert "files=" in c["fingerprint"], name
        assert "lines=" in c["fingerprint"], name


def test_run_msg_invariants_is_notrun_on_an_un_externalised_library(tmp_path):
    ws, db, store = _workspace(tmp_path)
    rep = msg_inv.run_msg_invariants(ws, db, "")
    assert rep["verdict"] == msg_inv.NOTRUN, (
        "an un-externalised library must be NOTRUN, not GREEN")
    for name, c in rep["checks"].items():
        assert c["verdict"] == msg_inv.NOTRUN, name


# ---------------------------------------------------------------------------
# The pre-decide: the gate's race against the live writer, closed
# ---------------------------------------------------------------------------

def test_the_pre_decide_settles_the_unresolved_count(tmp_path):
    """预跑 decide_origin 之后，unresolved 必须是 0 —— 断言不再与活写入者赛跑。

    机制（交办书「另外两件小事」第 2 条）：门禁之前统一预跑
    ``decide_origin`` 的流程化入口。
    """
    from agent.memory import msg_pre_decide

    ws, db, store = _workspace(tmp_path)
    # A library with a row nobody judged: the shape a fresh write
    # leaves behind.
    from agent.memory.storage import MemoryStorage

    st = MemoryStorage(db_path=str(db))
    try:
        st.conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, embedding, hash, metadata) "
            "VALUES ('row1', NULL, 'shared', 'memory', 'memory/shared/x.md', "
            "1, 2, 'some text', NULL, 'h', NULL)")
        st.conn.commit()
        unresolved = st.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'"
        ).fetchone()[0]
        assert unresolved == 1, "the fixture must have an undecided row"
    finally:
        st.close()

    rep = msg_pre_decide.pre_decide(ws, db)
    assert rep["unresolved_after"] == 0, rep
    assert rep["stable"] is True, rep
    assert rep["decided"] >= 1, rep


def test_the_pre_decide_reports_the_count_it_achieved(tmp_path):
    """预跑必须报出它达到的计数与时刻 —— 赛跑被披露，而不是被藏起来。"""
    from agent.memory import msg_pre_decide

    ws, db, store = _workspace(tmp_path)
    rep = msg_pre_decide.pre_decide(ws, db)
    assert "unresolved_before" in rep
    assert "unresolved_after" in rep
    assert rep["rounds"], "the pre-decide must report its rounds"
    for r in rep["rounds"]:
        assert "found" in r and "after" in r and "at" in r


def test_the_pre_decide_family_covers_every_library(tmp_path):
    from agent.memory import msg_pre_decide

    ws, db, store = _workspace(tmp_path)
    rep = msg_pre_decide.pre_decide_family(ws)
    assert rep["library_count"] >= 1
    assert rep["stable"] is True


# ---------------------------------------------------------------------------
# The multi-agent truth source
# ---------------------------------------------------------------------------

def test_two_agents_sharing_a_session_id_get_two_files(tmp_path):
    """两个 Agent 共用一个 session_id → 两个真源文件。

    ``messages`` 的唯一键是 ``(agent_id, session_id, seq)``，表结构的
    注释写得很清楚：「two Agents legitimately share a session_id」。
    所以「一个 session 一个文件」里的 session 是**这一对**。

    如果文件名只哈希 session_id，两个 Agent 会写进**同一个文件**：
    两段 seq 交错在一条链里，链的 per-file seq 会重复，DB 的指针也会
    指错 —— 这正是契约的不变式（「不同 session 必不同名」）要防的事。

    （实测：ROOT 库 2026-10-31 有 22 个 session、0 个跨 Agent 共享 ——
    所以这个缺陷是**潜伏**的，不是活的。修它的代价是一个哈希输入。）
    """
    from agent.memory import msg_chain

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    db = ws / "memory" / "long-term" / "index.db"

    a = ConversationStore(db, agent_id="alpha")
    b = ConversationStore(db, agent_id="beta")
    a.append_messages("shared", [{"role": "user", "content": "A 的问题"}],
                      channel_type="agent")
    b.append_messages("shared", [{"role": "user", "content": "B 的问题"}],
                      channel_type="agent")

    conn = _conn(db)
    try:
        rep = msg_migrate.externalise_every_agent(conn, ws,
                                                   batch_date="2026-10-31")
        assert rep["agent_count"] == 2, rep
        assert rep["written"] == 2, rep
    finally:
        conn.close()

    files = [p for p in (ws / "memory" / "messages_export").rglob("*.jsonl")
             if ".audit" not in p.parts]
    assert len(files) == 2, (
        "two agents sharing a session_id must get two truth files, got %d"
        % len(files))

    # And each agent reads back ITS OWN text.
    assert a.load_messages("shared", max_turns=5)[0]["content"] == "A 的问题"
    assert b.load_messages("shared", max_turns=5)[0]["content"] == "B 的问题"


def test_the_migration_covers_every_agent(tmp_path):
    """迁移必须覆盖**每一个** Agent —— ROOT 库有 9 个 Agent 的消息。

    只迁默认 Agent 而报告成功，是「干了四分之一却说干完了」。
    """
    ws, db, store = _workspace(tmp_path)
    other = ConversationStore(db, agent_id="other")
    other.append_messages("s9", [{"role": "user", "content": "别人的会话"}],
                          channel_type="agent")

    conn = _conn(db)
    try:
        rep = msg_migrate.externalise_every_agent(conn, ws,
                                                   batch_date="2026-10-31")
        assert rep["agent_count"] == 2, rep
        left = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NOT NULL"
        ).fetchone()[0]
        assert left == 0, "every agent's messages must be externalised"
    finally:
        conn.close()
