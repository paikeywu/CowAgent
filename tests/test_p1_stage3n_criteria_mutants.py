#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 N · the S3-M criteria's MUTANT REPLAY.

Every new criterion must be able to prove that it measures the thing it
names.  A criterion that is green on a broken library is worse than no
criterion at all: it teaches the team that the gate is noise.

The rule this file follows (and the one the whole mutant suite follows):

    **改坏必红 → 还原必绿.**  The restore step matters as much as the
    break step — a criterion that is red for everything is useless, and
    "改坏必红" without "还原必绿" is half a criterion.

The mutations replayed
----------------------
MU-27   the MSG-INV group LOSES one criterion (a report that carries six
        of the seven and says green) → M-1 must go red.  "报绿但没查"
        is the hole 阶段 5 中间态判据 ① names.
MU-28   an un-externalised library is judged GREEN → M-2 must go red.
        (判据自身的判据 A/B.)
MU-29   the inspector's wiring is REMOVED (the call is gone) → M-3 must
        go red on the static half.
MU-30   ``INV-5`` is back in the deferred set → M-4 must go red.
MU-31   the status page reports ``cold_read_latency_ms = 0.0`` on a
        library with nothing to measure → M-5 must go red ("不适用"
        must not masquerade as "instant").
MU-32   the write boundary does NOT prune the ledger → M-6 must go red
        (the gone id's witness survives).
MU-33   the write boundary prunes EVERYTHING (a blanket cascade) → M-6
        must go red on the "kept" half: the surviving id's witness must
        survive, because the S3-L restore triggers read it.
MU-34   the detector is BLINDED (the M6f judge always says PASS) → M-6
        must go red on the "detector stays awake" half: a fix at the
        write boundary must not make the detector blind.

The three-state discipline is checked too: a criterion that cannot say
NOTRUN is not a criterion (MU-12's rule, applied to the new module).
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import Report, STATE_FAIL, STATE_NOTRUN, STATE_PASS  # noqa: E402
from agent.memory.acceptance import live_libs  # noqa: E402
from agent.memory.acceptance import phase2_s3m  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _states(report: Report):
    return {number: state for number, _name, state in report.rows}


def _run(fn, *args):
    """Run one criterion; return (report, states).

    The report's PRINTED OUTPUT is captured and attached as
    ``report.printed`` — the report is part of the evidence: a criterion
    that says "FAIL" without naming WHICH library failed is not a usable
    defect report.
    """
    import contextlib
    import io

    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    return report, _states(report)


def _workspace_with_messages(tmp_path: Path) -> Path:
    """A workspace with ONE library that has been externalised.

    The shape is the one the messages domain really has: the ROOT
    library holds the ``messages`` table and the JSONL truth tree lives
    beside it.  An agent library with no ``messages`` table would be
    "not applicable", which is not what these mutants need.
    """
    from agent.memory import msg_chain, msg_inv
    from agent.memory.conversation_store import ConversationStore
    from agent.memory import msg_migrate

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

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        msg_migrate.externalise_all(conn, ws, "", batch_date="2026-10-31")
    finally:
        conn.close()
    msg_inv.write_baseline(ws)
    return ws


def _un_externalised_workspace(tmp_path: Path) -> Path:
    """A workspace with a library that has a ``messages`` table but NO
    externalised truth tree — the shape 判据自身的判据 A/B is about."""
    from agent.memory.conversation_store import ConversationStore

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    db = ws / "memory" / "long-term" / "index.db"
    store = ConversationStore(db, agent_id="")
    store.append_messages("s1", [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ], channel_type="web")
    return ws


# ---------------------------------------------------------------------------
# MU-27 · the MSG-INV group loses one criterion
# ---------------------------------------------------------------------------

def test_mu27_a_group_that_lost_a_criterion_is_caught(tmp_path):
    """MU-27: 「报绿但没查」 — a group with six of the seven must be RED."""
    ws = _workspace_with_messages(tmp_path)

    report, states = _run(
        phase2_s3m.judge_m1_msg_inv_group_is_complete, ws)
    assert states["M-1(REAL)"] == STATE_PASS, report.printed

    # The mutation: the patrol's report carries six of the seven.
    from agent.memory import inspector as inspector_mod

    original_inspect = inspector_mod.Inspector.inspect

    def lossy_inspect(self, *a, **kw):
        rep = original_inspect(self, *a, **kw)
        group = rep.get("msg_inv")
        if group and isinstance(group.get("checks"), dict):
            group["checks"].pop("MSG-INV-7", None)
        return rep

    inspector_mod.Inspector.inspect = lossy_inspect
    try:
        report, states = _run(
            phase2_s3m.judge_m1_msg_inv_group_is_complete, ws)
    finally:
        inspector_mod.Inspector.inspect = original_inspect

    assert states["M-1(REAL)"] == STATE_FAIL, (
        "少了一条判据的组必须报红 —— 报绿但没查就是 阶段 5 中间态判据 ① "
        "要防的那个洞")
    assert "MSG-INV-7" in report.printed, \
        "报告必须指名缺的是哪一条"

    # and the restore turns it green again
    report, states = _run(
        phase2_s3m.judge_m1_msg_inv_group_is_complete, ws)
    assert states["M-1(REAL)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-28 · an un-externalised library judged GREEN
# ---------------------------------------------------------------------------

def test_mu28_an_un_externalised_library_judged_green_is_caught(tmp_path):
    """MU-28: 判据自身的判据 A — an un-externalised library must be
    NOTRUN, never GREEN."""
    ws = _un_externalised_workspace(tmp_path)

    report, states = _run(
        phase2_s3m.judge_m2_un_externalised_is_notrun, ws)
    assert states["M-2(REAL)"] == STATE_PASS, report.printed

    # The mutation: the measurement library answers GREEN about a
    # library that was never externalised.
    from agent.memory import msg_inv

    original = msg_inv.run_msg_invariants

    def green_everything(workspace, db, agent_id=""):
        res = original(workspace, db, agent_id)
        for chk in res.get("checks", {}).values():
            chk["verdict"] = msg_inv.GREEN
            chk["violations"] = []
        res["verdict"] = msg_inv.GREEN
        return res

    msg_inv.run_msg_invariants = green_everything
    try:
        report, states = _run(
            phase2_s3m.judge_m2_un_externalised_is_notrun, ws)
    finally:
        msg_inv.run_msg_invariants = original

    assert states["M-2(REAL)"] == STATE_FAIL, (
        "未外置的库被判 GREEN 必须报红 —— 判据自身的判据 A")
    assert "GREEN" in report.printed

    report, states = _run(
        phase2_s3m.judge_m2_un_externalised_is_notrun, ws)
    assert states["M-2(REAL)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-29 · the inspector's wiring is removed
# ---------------------------------------------------------------------------

def test_mu29_the_wiring_being_removed_is_caught(tmp_path):
    """MU-29: the inspector no longer calls ``run_msg_invariants`` → M-3
    must go red on the static half."""
    ws = _workspace_with_messages(tmp_path)

    report, states = _run(phase2_s3m.judge_m3_the_wiring_is_real, ws)
    assert states["M-3(unit)"] == STATE_PASS, report.printed

    # The mutation: the source no longer holds the call.  The check
    # reads the source through ``inspector_mod.__file__``, so the
    # mutation has to be made to the FILE — a patched module object
    # would not be what the criterion reads.
    import agent.memory.acceptance.phase2_s3m as s3m
    from agent.memory import inspector as inspector_mod

    src_path = Path(inspector_mod.__file__)
    original_src = src_path.read_text(encoding="utf-8")
    broken_src = original_src.replace("run_msg_invariants",
                                      "run_msg_invariants_XX")
    assert broken_src != original_src, "the mutation did not land"

    # The criterion reads the source through the module's ``__file__``;
    # point it at a broken copy.
    tmp_src = Path(tmp_path) / "inspector_broken.py"
    tmp_src.write_text(broken_src, encoding="utf-8")
    original_file = inspector_mod.__file__
    try:
        inspector_mod.__file__ = str(tmp_src)
        report, states = _run(
            phase2_s3m.judge_m3_the_wiring_is_real, ws)
    finally:
        inspector_mod.__file__ = original_file

    assert states["M-3(unit)"] == STATE_FAIL, (
        "接线被摘掉必须报红 —— 判据只在 tests 里跑等于线上无人巡检（W-1）")

    report, states = _run(phase2_s3m.judge_m3_the_wiring_is_real, ws)
    assert states["M-3(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-30 · INV-5 is back in the deferred set
# ---------------------------------------------------------------------------

def test_mu30_inv5_being_deferred_again_is_caught(tmp_path):
    """MU-30: ``INV-5`` back in ``DEFERRED_INVS`` → M-4 must go red."""
    ws = _workspace_with_messages(tmp_path)

    report, states = _run(
        phase2_s3m.judge_m4_inv5_is_no_longer_deferred, ws)
    assert states["M-4(unit)"] == STATE_PASS, report.printed

    from agent.memory import text_inv

    original = dict(text_inv.DEFERRED_INVS)
    text_inv.DEFERRED_INVS["INV-5"] = "messages 外置（假装还没落地）"
    try:
        report, states = _run(
            phase2_s3m.judge_m4_inv5_is_no_longer_deferred, ws)
    finally:
        text_inv.DEFERRED_INVS.clear()
        text_inv.DEFERRED_INVS.update(original)

    assert states["M-4(unit)"] == STATE_FAIL, (
        "INV-5 回到暂缓列表必须报红 —— W-3 的口径是 {INV-4, INV-7, INV-8}")
    assert "INV-5" in report.printed

    report, states = _run(
        phase2_s3m.judge_m4_inv5_is_no_longer_deferred, ws)
    assert states["M-4(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-31 · "not applicable" disguised as 0.0
# ---------------------------------------------------------------------------

def test_mu31_zero_latency_on_a_library_with_nothing_to_measure_is_caught(
        tmp_path):
    """MU-31: ``cold_read_latency_ms = 0.0`` on a library with no
    externalised message must be RED — 「不适用」不许伪装成「瞬时」."""
    ws = _un_externalised_workspace(tmp_path)

    report, states = _run(phase2_s3m.judge_m5_status_self_reports, ws)
    assert states["M-5(unit)"] == STATE_PASS, report.printed

    from agent.memory import manager as manager_mod

    original = manager_mod.MemoryManager.get_status

    def zeroed(self):
        status = original(self)
        status["cold_read_latency_ms"] = 0.0
        return status

    manager_mod.MemoryManager.get_status = zeroed
    try:
        report, states = _run(
            phase2_s3m.judge_m5_status_self_reports, ws)
    finally:
        manager_mod.MemoryManager.get_status = original

    assert states["M-5(unit)"] == STATE_FAIL, (
        "0.0 在没有外置消息的库上必须报红 —— 『不适用』必须报 None")
    assert "0.0" in report.printed

    report, states = _run(phase2_s3m.judge_m5_status_self_reports, ws)
    assert states["M-5(unit)"] == STATE_PASS


def test_mu31b_a_missing_status_key_is_caught(tmp_path):
    """MU-31b: a status page that drops a key must be RED (the key's
    absence is not "the value is fine")."""
    ws = _un_externalised_workspace(tmp_path)

    report, states = _run(phase2_s3m.judge_m5_status_self_reports, ws)
    assert states["M-5(unit)"] == STATE_PASS, report.printed

    from agent.memory import manager as manager_mod

    original = manager_mod.MemoryManager.get_status

    def dropped(self):
        status = original(self)
        status.pop("msg_chain_anchor_scope", None)
        return status

    manager_mod.MemoryManager.get_status = dropped
    try:
        report, states = _run(
            phase2_s3m.judge_m5_status_self_reports, ws)
    finally:
        manager_mod.MemoryManager.get_status = original

    assert states["M-5(unit)"] == STATE_FAIL, (
        "status 少一个键必须报红 —— 自曝的缺失不能被当成自曝的通过")

    report, states = _run(phase2_s3m.judge_m5_status_self_reports, ws)
    assert states["M-5(unit)"] == STATE_PASS


# ---------------------------------------------------------------------------
# MU-32 / MU-33 / MU-34 · the ledger's prune and the detector
# ---------------------------------------------------------------------------

def _library_with_ledger(tmp_path: Path):
    """A library with 2 chunks of one file, both certified."""
    from agent.memory import body_guard
    from agent.memory.storage import MemoryChunk, MemoryStorage

    ws = tmp_path / "ws"
    db_dir = ws / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    storage = MemoryStorage(db_path=str(db))
    storage.conn.execute(body_guard.cert_table_ddl())

    def _chunk(cid, path, start, end):
        return MemoryChunk(
            id=cid, user_id=None, scope="shared", source="memory", path=path,
            start_line=start, end_line=end, text="text of %s" % cid,
            embedding=None, hash="h-%s" % cid, metadata=None,
            body_path=path, body_anchor="L%d-%d" % (start, end),
            body_hash="bh-%s" % cid, text_state="projection",
            body_origin="file")

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


def test_mu32_a_write_boundary_that_does_not_prune_is_caught(tmp_path):
    """MU-32: the write boundary keeps the gone id's witness → M-6 red.

    The mutation is on ``replace_file_chunks`` — the WRITE PATH — and
    not on ``_prune_ledger_entries`` directly: the criterion drives the
    write path, so a mutant that only broke the prune helper (while the
    write path kept calling it) would not be caught.  Breaking the
    helper is equivalent, because the write path calls the helper —
    but breaking the CALL SITE is the stronger form, and it is what
    this test does.
    """
    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        report, states = _run(
            phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
            ws)
        assert states["M-6(REAL)"] == STATE_PASS, report.printed

        from agent.memory.storage import MemoryStorage

        original = MemoryStorage.replace_file_chunks

        def no_prune(self, path, chunks, **kw):
            # The mutation: the write path does not prune at all.
            ids = [c.id for c in chunks]
            self._prune_ledger_entries = lambda _ids: 0
            try:
                return original(self, path, chunks, **kw)
            finally:
                del self._prune_ledger_entries

        MemoryStorage.replace_file_chunks = no_prune
        try:
            report, states = _run(
                phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
                ws)
        finally:
            MemoryStorage.replace_file_chunks = original

        assert states["M-6(REAL)"] == STATE_FAIL, (
            "写边界不剪枝必须报红 —— M6f 会在一个正常状态上一直叫")
        report, states = _run(
            phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
            ws)
        assert states["M-6(REAL)"] == STATE_PASS
    finally:
        storage.close()


def test_mu33_a_blanket_cascade_is_caught(tmp_path):
    """MU-33: the write boundary prunes EVERYTHING → M-6 must go red on
    the "kept" half: the surviving id's witness must survive, because the
    S3-L restore triggers read it."""
    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        report, states = _run(
            phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
            ws)
        assert states["M-6(REAL)"] == STATE_PASS, report.printed

        from agent.memory.storage import MemoryStorage

        original = MemoryStorage._prune_ledger_entries

        def cascade(self, chunk_ids):
            # The blanket cascade: every witness of the PATH goes, not
            # only the ids that did not come back.  The survivor's
            # witness is collateral damage.
            ids = list(chunk_ids)
            path = self.conn.execute(
                "SELECT path FROM chunks WHERE id=?", (ids[0],)
            ).fetchone()
            if path is None:
                return 0
            cur = self.conn.execute(
                "DELETE FROM cert_ledger WHERE body_path=?", (path[0],))
            return max(cur.rowcount, 0)

        MemoryStorage._prune_ledger_entries = cascade
        try:
            report, states = _run(
                phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
                ws)
        finally:
            MemoryStorage._prune_ledger_entries = original

        assert states["M-6(REAL)"] == STATE_FAIL, (
            "一刀切剪枝必须报红 —— 认证状态的恢复读账本，"
            "把存活的 id 的证人也剪掉会抹掉每一次重切的认证")
        report, states = _run(
            phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
            ws)
        assert states["M-6(REAL)"] == STATE_PASS
    finally:
        storage.close()


def test_mu34_a_blinded_detector_is_caught(tmp_path):
    """MU-34: the M6f detector always says PASS → M-6 must go red on the
    "detector stays awake" half: a fix at the write boundary must not
    make the detector blind to a foreign writer's DELETE."""
    ws, db, storage = _library_with_ledger(tmp_path)
    try:
        report, states = _run(
            phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
            ws)
        assert states["M-6(REAL)"] == STATE_PASS, report.printed

        from agent.memory import ledger_orphans

        original = ledger_orphans.judge_m6f_ledger_has_no_orphans

        def blind(conn, library=""):
            rep = original(conn, library=library)
            rep["verdict"] = "PASS"
            rep["orphans"] = 0
            rep["orphan_ids"] = []
            return rep

        ledger_orphans.judge_m6f_ledger_has_no_orphans = blind
        try:
            report, states = _run(
                phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
                ws)
        finally:
            ledger_orphans.judge_m6f_ledger_has_no_orphans = original

        assert states["M-6(REAL)"] == STATE_FAIL, (
            "检测器被致盲必须报红 —— 写边界会剪枝不等于外来删除可以被放过")
        report, states = _run(
            phase2_s3m.judge_m6_ledger_orphans_are_pruned_at_the_write_boundary,
            ws)
        assert states["M-6(REAL)"] == STATE_PASS
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# the three-state discipline, applied to the new module
# ---------------------------------------------------------------------------

def test_the_new_module_uses_the_third_state():
    """The new criteria must be able to say NOTRUN — a criterion without
    the third state reports "I could not measure" as "it is broken"."""
    src = Path(phase2_s3m.__file__).read_text(encoding="utf-8")
    assert src.count("judge_notrun(") >= 6, \
        "每条判据都要有一条 NOTRUN 的路 —— 前置不成立不是不通过"


def test_the_new_module_is_in_the_gate():
    """The module must be WIRED into phase2.py — a criterion file that
    nobody loads patrols nothing (the same hole W-1 closed)."""
    src = (REPO / "agent" / "memory" / "acceptance" / "phase2.py").read_text(
        encoding="utf-8")
    assert "_load_s3m" in src, "phase2.py 里没有 _load_s3m"
    assert "phase2_s3m" in src, "phase2.py 没有 import phase2_s3m"
    for group in ("M-1", "M-2", "M-3", "M-4", "M-5", "M-6"):
        assert '("%s"' % group in src, \
            "判据组 %s 没有接进 phase2.py" % group
