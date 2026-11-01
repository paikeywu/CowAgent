#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 · T3e · EE-1 五态分离 + knowledge 层纳入 · MUTANT REPLAY.

**本轮交办**（小种子 SEED，2026-11-01）：裁决两件事 + 确认 DR-2。

  ① **四态 → 五态**。工程师实测：活库 113 条 ``historical`` 在日志副本
     上逐条跑真实对账，**113/113 全部补上见证**（7 条 ``live-leak``
     同理 7/7）⇒ **见证是「现在」对「现在」的复现，不追溯「当时」**。
     ⇒ 「历史欠账」只��「从未补过」的日志上才成立。
     交办要求：**不许简化成「历史欠账一律不判红」** —— 那样「永远没人
     补」的欠账会永久隐身。判据必须能说出「这条补不上，因为 X」。

  ② **``knowledge/`` 算不算真源层**。实测（判据侧独立取证）：
     ``truth_files(ROOT)`` = 87 条，其中 ``knowledge/`` **0 条**；而
     16 个库的 ``chunks`` 指向 **2794** 条 ``knowledge/`` 引用（全部可
     解析到真文件），根日志有 **1018** 条 ``knowledge/`` 事件、**119**
     条带内容见证，共享树实存 **121** 个 md。
     ⇒ 判据侧裁决：**knowledge/ 算真源层**，判据的被测对象必须包含它。

  ③ **DR-2 保持红**，且**不进基线** —— 由 ``GB-1`` 的
     ``ENVIRONMENT_ITEMS`` 在**代码里**执行（不只是写在基线文件的头部）。

**本文件的 mutant**

本轮的核心机制是 **判据不轻信探针**：探针在日志副本上跑一次真实对账，
判据拿副本日志的**最终见证状态**去核对探针的说法。探针说「能补」却拿
不出见证、或探针说「关不上」却有见证，都记为 ``[PROBE_CLAIM_FALSE]``
并判红 —— **这就是「判据必须能说出『这条补不上，因为 X』」的落点**：
判据不是转述探针，而是拿证据核对探针。

==================  ==============================================  ==========
ID                 变异                                            期望
==================  ==============================================  ==========
MU-54              探针**谎称**能补（机制真的补不上）              必须红
                   ⇒ 判据必须抓出 [PROBE_CLAIM_FALSE]
MU-55              探针**谎称**关不上（机制真的补上了）            必须红
                   ⇒ 判据必须抓出 [PROBE_CLAIM_FALSE]
MU-56              探针的**原因**被压成一个常量                    必须红
                   ⇒ 判据必须仍然区分得出不同原因
MU-57              **knowledge/ 层被摘掉**（回到旧口径）             必须红
                   （最大的真源层对判据隐形 ⇒ 这就是本轮要堵的洞）
MU-58              对账**只对根工作区**做（14 个 agent 的日志空）    必须红
                   （机制对不上账 ⇒ 补不上）
MU-59              对账**吞掉异常**（假装成功）                      必须红
                   （不知道被伪装成能补）
MU-60              **DR-2 进入全局基线**                            必须红
                   （环境项红进基线 ⇒ 基线替环境兜底）
==================  ==============================================  ==========
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import (  # noqa: E402
    Report,
    STATE_FAIL,
    STATE_NOTRUN,
    STATE_PASS,
)
from agent.memory.acceptance import phase2_s3x  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(fn, *args):
    """Run one criterion; return (report, states, printed)."""
    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    states = {number: state for number, _name, state in report.rows}
    return report, states, report.printed


def _make_workspace(root: Path, *, files, log_events=(), db=None):
    """Build a sandbox workspace: truth files + a change log (+ a library).

    The shape is the one ``enumerate_live`` recognises:
    ``<ws>/memory/long-term/index.db`` for the ROOT, and
    ``<ws>/agents/<id>/memory/long-term/index.db`` for an agent.

    **The log file always exists** — an empty log is still "a workspace
    with a change log".  Without it the criterion reports NOTRUN and
    every mutant here would be measuring the wrong thing.
    """
    (root / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    log = root / "memory" / ".audit" / "file_changes.jsonl"
    if log_events:
        log.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False)
                      for e in log_events) + "\n",
            encoding="utf-8")
    elif not log.exists():
        log.write_text("", encoding="utf-8")
    if db is not None:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE chunks (id TEXT PRIMARY KEY, body_path TEXT, "
            "body_anchor TEXT, body_hash TEXT, text_state TEXT, "
            "body_origin TEXT, text TEXT)")
        conn.commit()
        conn.close()
    return root


def _insert_chunk(db: Path, body_path: str, text: str = "x"):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT OR REPLACE INTO chunks (id, body_path, body_anchor, "
        "body_hash, text_state, body_origin, text) "
        "VALUES (?,?,?,?,?,?,?)",
        ("id-" + body_path, body_path, "L1-1", "sha256:x", "projection",
         "file", text))
    conn.commit()
    conn.close()


def _broken_mechanism(monkeypatch):
    """Make the reconciliation record NOTHING for ``external-edit``.

    This is the mechanism-side break (the engineer's MU-B1).  With it,
    a readable truth file **cannot** be closed ⇒ the probe's verdict
    ``unclosable`` is genuine, and the copy log holds no witness.
    """
    from agent.memory import changes

    original = changes.record_change

    def records_nothing(root, relpath, source, **kw):
        if source == "external-edit":
            return {"path": relpath, "source": source, "dropped": True}
        return original(root, relpath, source, **kw)

    monkeypatch.setattr(changes, "record_change", records_nothing)
    return original


# ---------------------------------------------------------------------------
# MU-54 · the probe LIES about being able to close a path
# ---------------------------------------------------------------------------

def test_muC1_a_probe_that_lies_about_being_able_to_close_must_be_red(
        tmp_path, monkeypatch):
    """MU-54: the probe claims "closable" for a path it CANNOT close.

    The mutation removes the mechanism's ability to record (so the path
    is genuinely unclosable) and then makes the probe **claim the
    opposite**.  A criterion that takes the probe's word for it would go
    GREEN on a broken mechanism ⇒ that is the "假守护者" shape.

    **The discriminating assertion.**  Without the lying probe the
    criterion is RED with reason ``NO_EVENT`` (the truth).  With the
    lying probe the criterion must still be RED — but with reason
    ``PROBE_CLAIM_FALSE`` (the lie).  So the test asserts on the
    REASON, not merely on the colour: a criterion that is red for the
    wrong reason has not caught anything.
    """
    ws = _make_workspace(
        tmp_path / "ws",
        files={"MEMORY.md": "# head\n\nbody\n"},
        db=tmp_path / "ws" / "memory" / "long-term" / "index.db",
    )
    _insert_chunk(ws / "memory" / "long-term" / "index.db", "MEMORY.md")

    _broken_mechanism(monkeypatch)
    original = phase2_s3x._probe_reconciliation

    def lying_probe(ws_, cands):
        out = original(ws_, cands)
        out["unclosable"] = {}          # <- the lie
        return out

    # --- prove the mutation took effect ---
    p_orig = original(ws, [("MEMORY.md", ws / "MEMORY.md")])
    p_mut = lying_probe(ws, [("MEMORY.md", ws / "MEMORY.md")])
    assert set(p_orig.get("unclosable", {})) - set(p_mut.get("unclosable", {})) \
        == {"MEMORY.md"}, (
        "变异臂没有生效：原探针没有把 MEMORY.md 判成补不上（%s）"
        % p_orig.get("unclosable"))

    # --- the truth (no lie): RED, with the honest reason ---
    monkeypatch.setattr(phase2_s3x, "_probe_reconciliation", original)
    _r, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert states["EE-1(REAL)"] == STATE_FAIL, (
        "机制补不上时判据必须红\n" + printed)
    assert "[NO_EVENT]" in printed, (
        "判据必须说出『补不上，因为 NO_EVENT』\n" + printed)
    # The assertion must match the REASON LINE, not the token anywhere:
    # the criterion's own explanatory prose mentions the token too.
    assert "[PROBE_CLAIM_FALSE]" not in printed, (
        "诚实探针下不应出现 PROBE_CLAIM_FALSE 的原因行\n" + printed)

    # --- the lie: still RED, but now the lie is named ---
    monkeypatch.setattr(phase2_s3x, "_probe_reconciliation", lying_probe)
    _r, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert states["EE-1(REAL)"] == STATE_FAIL, (
        "探针谎称『能补』时判据必须红 —— 否则机制坏了也判绿\n" + printed)
    assert "[PROBE_CLAIM_FALSE]" in printed, (
        "判据必须抓出探针在撒谎（PROBE_CLAIM_FALSE）—— "
        "『判据能说出这条补不上，因为 X』的落点\n" + printed)


# ---------------------------------------------------------------------------
# MU-55 · the probe LIES about being unable to close a path
# ---------------------------------------------------------------------------

def test_muC2_a_probe_that_lies_about_being_broken_must_be_red(
        tmp_path, monkeypatch):
    """MU-55: the probe claims "unclosable" for a path it DID close.

    The mirror of MU-54: the mechanism works, the path is closed (the
    copy log holds a matching witness), but the probe says "the
    mechanism is broken".  Believing it would point the fixer at a
    mechanism that works — the same category of error as reporting a
    stale projection as content corruption.

    **The discriminating assertion**: without the lie the criterion is
    GREEN (it is only a debt); with the lie it must be RED with
    ``PROBE_CLAIM_FALSE``.
    """
    ws = _make_workspace(
        tmp_path / "ws",
        files={"MEMORY.md": "# head\n\nbody\n"},
        db=tmp_path / "ws" / "memory" / "long-term" / "index.db",
    )
    _insert_chunk(ws / "memory" / "long-term" / "index.db", "MEMORY.md")

    original = phase2_s3x._probe_reconciliation

    def lying_probe(ws_, cands):
        out = original(ws_, cands)
        out["unclosable"] = {
            rel: ("NO_EVENT", "被变异臂谎称成补不上")
            for rel, _f in cands}
        return out

    # --- prove the mutation took effect ---
    p_orig = original(ws, [("MEMORY.md", ws / "MEMORY.md")])
    p_mut = lying_probe(ws, [("MEMORY.md", ws / "MEMORY.md")])
    assert set(p_mut.get("unclosable", {})) - set(p_orig.get("unclosable", {})) \
        == {"MEMORY.md"}, (
        "变异臂没有生效：unclosable 集合没有变大")

    # --- the truth: GREEN (a debt, not a defect) ---
    _r, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert states["EE-1(REAL)"] == STATE_PASS, (
        "机制能补上时判据不该红 —— 它只是一笔欠账\n" + printed)
    assert "待回填" in printed, (
        "欠账必须仍然被点名 —— 不许因为『能补』就让它隐身\n" + printed)

    # --- the lie: RED, and the lie is named ---
    monkeypatch.setattr(phase2_s3x, "_probe_reconciliation", lying_probe)
    _r, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert states["EE-1(REAL)"] == STATE_FAIL, (
        "探针谎称『关不上』时判据必须红 —— 否则修复方向会被指错\n" + printed)
    assert "[PROBE_CLAIM_FALSE]" in printed, (
        "判据必须抓出探针在撒谎\n" + printed)


# ---------------------------------------------------------------------------
# MU-56 · the reasons must stay distinguishable
# ---------------------------------------------------------------------------

def test_muC3_the_reasons_must_not_be_collapsed_into_one(tmp_path,
                                                          monkeypatch):
    """MU-56: the probe's REASON is collapsed into a single constant.

    The ruling requires the criterion to say *"this one cannot be
    closed, because X"*.  A criterion that prints one constant reason
    satisfies the letter of that and none of its spirit: the reader
    cannot tell a path the mechanism cannot see (``NO_EVENT``) from a
    path whose witness is empty (``EMPTY_WITNESS``) or wrong
    (``WITNESS_MISMATCH``).

    **The fixture produces three different reasons** (an unreadable
    path, an event with no witness, and a witness that does not
    reproduce), and the criterion must print **all three**.

    **The mutation is proved to have taken effect**: the reason set
    must shrink.
    """
    from agent.memory import changes

    ws = _make_workspace(
        tmp_path / "ws",
        files={"MEMORY.md": "# head\n\nbody\n",
               "AGENT.md": "# agent\n"},
        db=tmp_path / "ws" / "memory" / "long-term" / "index.db",
    )
    db = ws / "memory" / "long-term" / "index.db"
    _insert_chunk(db, "MEMORY.md")
    _insert_chunk(db, "AGENT.md")
    # A DIRECTORY where a file should be ⇒ unreadable ⇒ UNREADABLE.
    # It must live where ``truth_files`` really looks (``memory/``), or
    # it never enters the candidate set and the mutant measures nothing.
    (ws / "memory" / "BROKEN.md").mkdir(parents=True, exist_ok=True)

    # The mechanism records the event but **without a witness** ⇒
    # EMPTY_WITNESS for MEMORY.md; and a CONSTANT witness for AGENT.md
    # ⇒ WITNESS_MISMATCH.
    original_record = changes.record_change

    def weird_record(root, relpath, source, **kw):
        if source != "external-edit":
            return original_record(root, relpath, source, **kw)
        extra = dict(kw.get("extra") or {})
        if relpath == "AGENT.md":
            extra["content_sha256"] = "0" * 64
        else:
            extra.pop("content_sha256", None)
        return original_record(root, relpath, source,
                               **{k: v for k, v in kw.items() if k != "extra"},
                               extra=extra)

    monkeypatch.setattr(changes, "record_change", weird_record)

    original = phase2_s3x._probe_reconciliation

    def constant_reason(ws_, cands):
        out = original(ws_, cands)
        out["unclosable"] = {
            rel: ("NO_EVENT", "被变异臂压成常量原因")
            for rel in out.get("unclosable", {})}
        return out

    cands = [("MEMORY.md", ws / "MEMORY.md"),
             ("AGENT.md", ws / "AGENT.md"),
             ("memory/BROKEN.md", ws / "memory" / "BROKEN.md")]
    p_orig = original(ws, cands)
    r_orig = {v[0] for v in p_orig.get("unclosable", {}).values()}
    p_mut = constant_reason(ws, cands)
    r_mut = {v[0] for v in p_mut.get("unclosable", {}).values()}
    assert len(r_orig) >= 2, (
        "夹具没有造出多种原因（%s）—— 这个 mutant 测不到东西" % r_orig)
    assert r_mut != r_orig, (
        "变异臂没有生效：原因集合没有变化（%s）" % r_orig)

    # The broken path must really be in the truth set, or the fixture
    # does not exercise the UNREADABLE branch.
    truth = {r for r, _f in phase2_s3x._truth_files(ws)}
    assert "memory/BROKEN.md" in truth, (
        "夹具的不可读路径不在真源集里（%s）—— 测不到 UNREADABLE 分支"
        % sorted(truth))

    # --- the truth: the criterion prints the DIFFERENT reasons ---
    monkeypatch.setattr(phase2_s3x, "_probe_reconciliation", original)
    _r, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")

    # **UNREADABLE is the criterion's OWN named state** (读不到=N), not
    # one of the probe's unclosable reasons: the criterion skips an
    # unreadable file *before* it consults the probe.  So it is asserted
    # in its own bucket, and the probe's reasons are asserted on the
    # reasons the criterion actually reports.
    assert "读不到=1" in printed, (
        "读不到的文件必须在自己的桶里可见 —— 它既不是见证成立，"
        "也不是机制关不上\n" + printed)
    for reason in sorted(r_orig - {"UNREADABLE"}):
        assert reason in printed, (
            "判据没有报出原因 %s —— 『能说出因为 X』落空了\n%s"
            % (reason, printed))

    # --- the mutation: the reasons collapse ⇒ the criterion must not
    #     keep the original set ---
    monkeypatch.setattr(phase2_s3x, "_probe_reconciliation", constant_reason)
    _r, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert "被变异臂压成常量原因" in printed, (
        "变异臂没有生效：判据没有转述探针的新原因\n" + printed)
    for reason in sorted(r_orig - {"UNREADABLE"}):
        assert reason not in printed, (
            "变异臂生效后判据仍在报原来的原因 %s —— "
            "它没有转述探针的结论\n%s" % (reason, printed))


# ---------------------------------------------------------------------------
# MU-57 · the knowledge layer must be IN the subject
# ---------------------------------------------------------------------------

def test_muC4_the_knowledge_layer_must_not_be_droppable(tmp_path,
                                                        monkeypatch):
    """MU-57: the ``knowledge/`` layer is removed from the subject.

    **This is the hole the ruling closes.**  Measured on the live
    workspace: the borrowed ``truth_files`` set lists **0** knowledge
    entries while the index points at **2794** knowledge chunk rows and
    the ROOT log already carries **1018** knowledge events (**119** with
    a witness).  A criterion that watches only the borrowed set is
    watching the *smaller* half of the truth layer.

    The mutation makes ``_index_knowledge_files`` return nothing ⇒ the
    knowledge files fall out of the subject ⇒ they are no longer
    reported at all.

    **The mutation is proved to have taken effect**: the file set must
    actually shrink.
    """
    ws = _make_workspace(
        tmp_path / "ws",
        files={"MEMORY.md": "# head\n\nbody\n",
               "knowledge/concepts/thing.md": "# concept\n\ntext\n"},
        db=tmp_path / "ws" / "memory" / "long-term" / "index.db",
    )
    db = ws / "memory" / "long-term" / "index.db"
    _insert_chunk(db, "MEMORY.md")
    _insert_chunk(db, "knowledge/concepts/thing.md")

    original = phase2_s3x._index_knowledge_files

    def no_knowledge(ws_, lib):
        return []                        # <- the mutation

    from agent.memory.acceptance.live_libs import enumerate_live
    libs = enumerate_live(ws)
    assert libs, "夹具没有造出活库"
    k_orig = original(ws, libs[0])
    k_mut = no_knowledge(ws, libs[0])
    assert len(k_orig) > len(k_mut), (
        "变异臂没有生效：knowledge 文件集没有变小（%d -> %d）"
        % (len(k_orig), len(k_mut)))
    assert any(r.startswith("knowledge/") for r, _f in k_orig), (
        "夹具的 knowledge 层没有进入索引 —— 这个 mutant 测不到东西")

    monkeypatch.setattr(phase2_s3x, "_index_knowledge_files", no_knowledge)
    _report, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert "knowledge/concepts/thing.md" not in printed, (
        "变异臂没有生效：knowledge 文件仍然出现在报告里\n" + printed)

    monkeypatch.setattr(phase2_s3x, "_index_knowledge_files", original)
    _report, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert "knowledge/concepts/thing.md" in printed, (
        "还原后 knowledge 文件必须重新出现在报告里 —— "
        "它是被测对象的一部分\n" + printed)


# ---------------------------------------------------------------------------
# MU-58 · the reconciliation must see EVERY workspace's own log
# ---------------------------------------------------------------------------

def test_muC5_the_reconciliation_must_reach_each_workspaces_own_log(
        tmp_path, monkeypatch):
    """MU-58: the reconciliation only runs against the ROOT's log.

    The engineer's T2f hole 2 was exactly this: the CLI reconciled every
    workspace's files into the ROOT's log, leaving each agent's own log
    empty ⇒ **an agent's provenance is invisible to every reader of the
    agent's log**.

    The mutation makes the probe reconcile only the ROOT workspace ⇒ an
    agent workspace's files cannot be closed ⇒ the criterion must be
    RED (the mechanism cannot serve that workspace).

    **The mutation is proved to have taken effect**: the agent's files
    must move from "closable" to "unclosable".
    """
    ws = _make_workspace(tmp_path / "ws", files={"MEMORY.md": "# root\n"})
    agent = _make_workspace(tmp_path / "ws" / "agents" / "agent-x",
                            files={"MEMORY.md": "# agent\n"})

    original = phase2_s3x._probe_reconciliation

    def root_only(ws_, cands):
        """Reconcile against the ROOT's log only."""
        if Path(ws_).name != "ws":
            out = {"events": {}, "second_pass_added": 0,
                   "mtime_advanced": False, "unclosable": {},
                   "post_witness": {}, "error": None}
            for rel, real in cands:
                out["unclosable"][rel] = (
                    "NO_EVENT", "被变异臂限制成只对根工作区对账")
            return out
        return original(ws_, cands)

    p_orig = original(agent, [("MEMORY.md", agent / "MEMORY.md")])
    p_mut = root_only(agent, [("MEMORY.md", agent / "MEMORY.md")])
    assert set(p_mut.get("unclosable", {})) - set(p_orig.get("unclosable", {})) \
        == {"MEMORY.md"}, (
        "变异臂没有生效：agent 工作区的 unclosable 集合没有变大")

    monkeypatch.setattr(phase2_s3x, "_probe_reconciliation", root_only)
    _report, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert states["EE-1(REAL)"] == STATE_FAIL, (
        "只对根工作区对账必须被判红 —— 否则每个 agent 的溯源都是空的\n"
        + printed)

    monkeypatch.setattr(phase2_s3x, "_probe_reconciliation", original)
    _report, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert states["EE-1(REAL)"] == STATE_PASS, printed


# ---------------------------------------------------------------------------
# MU-59 · "I don't know" must not be dressed as "I can close it"
# ---------------------------------------------------------------------------

def test_muC6_swallowing_the_error_must_not_look_closable(tmp_path,
                                                          monkeypatch):
    """MU-59: the reconciliation SWALLOWS its exception and reports success.

    The design稿's most insidious failure: the reconciliation cannot
    read a path, swallows the error, and returns an empty report ⇒
    "I could not read it" becomes indistinguishable from "it did not
    change" ⇒ the criterion would classify the row as CLOSABLE.

    The mutation makes the reconciliation swallow ⇒ the probe sees no
    event and no error ⇒ the row must NOT be reported as a clean
    closable; the criterion must be red, or must name the uncertainty.
    """
    from agent.memory import external_edit

    ws = _make_workspace(
        tmp_path / "ws",
        files={"MEMORY.md": "# head\n\nbody\n"},
    )

    def swallow(root, cands, *a, **kw):
        return {"recorded": [], "skipped": [c[0] for c in cands],
                "errors": []}       # <- the error is swallowed

    monkeypatch.setattr(external_edit, "reconcile_external_edits", swallow)
    _report, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    # Whatever the verdict, the criterion must NOT claim the row is a
    # clean closable while the reconciliation silently did nothing.
    assert not ("机制关不上=0" in printed and "待回填=0" in printed), (
        "对账吞掉异常后，判据仍然说『机制关不上 = 0』且『待回填 = 0』—— "
        "把『不知道』当成了『不需要补』\n" + printed)


# ---------------------------------------------------------------------------
# MU-60 · DR-2 must NOT be allowed into the global baseline
# ---------------------------------------------------------------------------

def test_muC7_an_environment_item_must_not_enter_the_global_baseline():
    """MU-60: DR-2 (an environment item) is put INTO the baseline file.

    The engineer and the守门人 both registered **DR-2 as an expected
    red** (历史不可回填 ⇒ the old event never gains a witness).  The
    ruling for this round: **DR-2 does not enter the global baseline** —
    and that must be enforced **in code**, not merely written in the
    baseline file's header.

    The mutation adds DR-2 to the baseline file's entries ⇒ GB-1 must
    be RED.

    **The mutation is proved to have taken effect**: the parsed
    baseline must actually contain DR-2 afterwards.
    """
    from agent.memory.acceptance import phase2_s3x as mod

    assert "DR-2" in mod.ENVIRONMENT_ITEMS, (
        "DR-2 必须是环境项 —— 守门人已登记它是预期红，不可回填")

    path = mod._global_baseline_path()
    original = path.read_text(encoding="utf-8")
    try:
        # --- the mutation: DR-2 enters the baseline ---
        mutated = original.rstrip("\n") + (
            "\nDR-2 | 被变异臂塞进基线的环境项（这是错的）\n")
        path.write_text(mutated, encoding="utf-8")

        baseline, _comments = mod._parse_baseline(path.read_text("utf-8"))
        assert "DR-2" in baseline, (
            "变异臂没有生效：DR-2 没有出现在解析后的基线条目里")

        report = Report()
        buf = io.StringIO()
        # GB-1 reads the gate's OWN table.  Feed it a table that
        # contains DR-2 as RED, so the environment rule is exercised.
        report.rows.append(("DR-2", "变化溯源", STATE_FAIL))
        with contextlib.redirect_stdout(buf):
            mod.judge_global_failure_set_matches_baseline(report)
        printed = buf.getvalue()
        states = {n: s for n, _nm, s in report.rows}
        assert states["GB-1(unit)"] == STATE_FAIL, (
            "环境项 DR-2 进入基线必须被判红 —— "
            "环境项进基线 = 基线替环境兜底\n" + printed)
        assert "环境项" in printed, printed
    finally:
        path.write_text(original, encoding="utf-8")

    # --- the restore must bring the green back ---
    report = Report()
    buf = io.StringIO()
    report.rows.append(("DR-2", "变化溯源", STATE_FAIL))
    with contextlib.redirect_stdout(buf):
        phase2_s3x.judge_global_failure_set_matches_baseline(report)
    printed = buf.getvalue()
    states = {n: s for n, _nm, s in report.rows}
    assert states["GB-1(unit)"] == STATE_PASS, (
        "还原后 GB-1 必须回到绿 —— 环境项红不进基线，DR-2 的红是允许的\n"
        + printed)


# ---------------------------------------------------------------------------
# The ruling's own invariants (no mutation needed — these are the claims)
# ---------------------------------------------------------------------------

def test_the_criterion_declares_its_subject(tmp_path):
    """The criterion must SAY which object it measures.

    The team rule: **判据必须自报它测的是哪个对象**.  EE-1's subject
    is the truth layer — and by the 2026-11-01 ruling that includes the
    ``knowledge/`` layer.  A reader who cannot tell what is being
    measured cannot tell what the verdict is about.
    """
    ws = _make_workspace(
        tmp_path / "ws",
        files={"MEMORY.md": "# head\n\nbody\n",
               "knowledge/concepts/thing.md": "# concept\n"},
        db=tmp_path / "ws" / "memory" / "long-term" / "index.db",
    )
    _insert_chunk(ws / "memory" / "long-term" / "index.db",
                   "knowledge/concepts/thing.md")
    _report, _states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert "被测对象" in printed, (
        "判据没有自报被测对象 —— 读者无法知道它测的是什么\n" + printed)
    assert "knowledge/" in printed, (
        "判据没有自报 knowledge/ 层在被测对象里\n" + printed)


def test_the_backfillable_debt_is_named_and_counted(tmp_path):
    """A debt that can be closed must still be COUNTED and NAMED.

    The ruling's whole point: **不许把它简化成「历史欠账一律不判红」**
    —— that would make a debt nobody ever closes permanently invisible.
    So a closable-but-unwitnessed file must appear in the report, with
    its path, and the count must be in the summary.
    """
    ws = _make_workspace(
        tmp_path / "ws",
        files={"MEMORY.md": "# head\n\nbody\n"},
        db=tmp_path / "ws" / "memory" / "long-term" / "index.db",
    )
    _insert_chunk(ws / "memory" / "long-term" / "index.db", "MEMORY.md")

    _report, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, tmp_path / "ws")
    assert "待回填" in printed, (
        "『待回填』这个态必须出现在报告里 —— 否则欠账没有名字\n" + printed)
    assert "MEMORY.md" in printed, (
        "欠账必须点名到 path\n" + printed)
    # And the criterion is green (the mechanism CAN close it) — but the
    # debt is still there.
    assert states["EE-1(REAL)"] == STATE_PASS, printed


def test_an_empty_workspace_is_notrun_not_pass(tmp_path):
    """A workspace with no change log ⇒ NOTRUN, never PASS.

    "0 个对象" is NOTRUN; "测到了 0" is PASS.  They are different
    claims, and a criterion that cannot tell them apart lies in one
    direction or the other.
    """
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / "MEMORY.md").write_text("# head\n", encoding="utf-8")

    _report, states, printed = _run(
        phase2_s3x.judge_external_edit_is_traceable, ws)
    assert states["EE-1(REAL)"] == STATE_NOTRUN, (
        "没有变更记录 ⇒ 前置不成立 ⇒ NOTRUN，不是 PASS\n" + printed)
