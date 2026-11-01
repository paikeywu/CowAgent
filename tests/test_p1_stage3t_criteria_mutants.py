#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 T · the S3-T criteria's MUTANT REPLAY.

Every new criterion must be able to prove that it measures the thing it
names.  A criterion that is green on a broken library is worse than no
criterion at all: it teaches the team that the gate is noise.

The rule this file follows (and the one the whole mutant suite follows):

    **改坏必红 → 还原必绿.**  The restore step matters as much as the
    break step — a criterion that is red for everything is useless, and
    "改坏必红" without "还原必绿" is half a criterion.

The mutations replayed
----------------------
MU-35   I-1's age rule is neutered ⇒ I-1 must go RED.  Without this,
        the backlog rule is a comment.
MU-36   the write path's externalisation call is REMOVED ⇒ I-2 must go
        red on the behavioural half (the row stays inline).
MU-37   the truth tree and the DB disagree ⇒ I-3 must go RED, in BOTH
        directions (a line with no row, and a row with no line).
MU-38   the write boundary's ledger prune is REMOVED ⇒ I-4 must go RED
        (the re-cut leaves the vanished id's witness behind).

The three-state discipline is checked too: a criterion that cannot say
NOTRUN is not a criterion (MU-12's rule, applied to the new module), and
"0 个对象" must be NOTRUN — never a PASS on an empty corpus.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import Report, STATE_FAIL, STATE_NOTRUN, STATE_PASS  # noqa: E402
from agent.memory.acceptance import phase2_s3t  # noqa: E402


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


def _backlog_workspace(tmp_path: Path, age_seconds: int = 7200) -> Path:
    """A workspace whose ROOT library carries an OLD inline row.

    The row is written with a ``created_at`` that is already OLD, so
    the criterion's age rule has something to bite on without the test
    having to sleep.

    ``externalise=False`` is the switch the product itself provides for
    "a store that is ABOUT the pre-externalisation state" (the write
    path externalises by default since 缺口 A landed).
    """
    from agent.memory.conversation_store import ConversationStore

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    db = ws / "memory" / "long-term" / "index.db"
    store = ConversationStore(db, agent_id="", externalise=False)
    store.append_messages("s1", [
        {"role": "user", "content": "积压的一问"},
        {"role": "assistant", "content": "积压的一答"},
    ], channel_type="web")

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE messages SET created_at = ?",
                     (int(time.time()) - age_seconds,))
        conn.commit()
    finally:
        conn.close()
    return ws


def _fresh_workspace(tmp_path: Path) -> Path:
    """A workspace whose ROOT library carries only FRESH inline rows.

    This is the "合法的瞬时态" shape — the criterion must PASS here,
    which is what makes MU-35 meaningful: a criterion that fails on
    every inline row cannot tell the transient state from the backlog.
    """
    return _backlog_workspace(tmp_path, age_seconds=1)


def _externalised_workspace(tmp_path: Path, name: str = "ws") -> Path:
    """A workspace whose messages were externalised (the JSONL exists)."""
    from agent.memory import msg_inv
    from agent.memory import msg_migrate
    from agent.memory.conversation_store import ConversationStore

    ws = tmp_path / name
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    db = ws / "memory" / "long-term" / "index.db"
    store = ConversationStore(db, agent_id="")
    store.append_messages("s1", [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ], channel_type="web")

    conn = sqlite3.connect(str(db))
    try:
        msg_migrate.externalise_all(conn, ws, "", batch_date="2026-10-31")
    finally:
        conn.close()
    msg_inv.write_baseline(ws)
    return ws


def _ledger_workspace(tmp_path: Path) -> Path:
    """A workspace whose ROOT library has chunks AND certification
    ledger entries for them — the shape I-4 needs.

    I-4's subject is the ledger's behaviour at the write boundary, so
    the fixture has to carry a ledger with entries for the ids a re-cut
    will make vanish.
    """
    from agent.memory.storage import MemoryChunk, MemoryStorage

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    db = ws / "memory" / "long-term" / "index.db"
    storage = MemoryStorage(db_path=str(db))
    try:
        for i, (s, e) in enumerate([(1, 10), (11, 20), (21, 30)]):
            storage.save_chunk(MemoryChunk(
                id="chunk-%d" % i, user_id=None, scope="shared",
                source="memory", path="MEMORY.md", start_line=s, end_line=e,
                text="body %d" % i, embedding=None, hash="h%d" % i,
                metadata=None, body_path=None, body_anchor=None,
                body_hash=None, text_state="mirror", body_origin="file"))
        conn = storage.conn
        for i in range(3):
            conn.execute(
                "INSERT INTO cert_ledger (id, text_state, body_origin, "
                "body_path, body_hash, certified_at, certified_by) "
                "VALUES (?,?,?,?,?,?,?)",
                ("chunk-%d" % i, "mirror", "file", "MEMORY.md", "h%d" % i,
                 111, "backfill"))
        conn.commit()
    finally:
        storage.close()
    return ws


# ---------------------------------------------------------------------------
# MU-35 · I-1's age rule is neutered
# ---------------------------------------------------------------------------

def test_mu35_a_neutered_age_rule_is_caught(tmp_path):
    """MU-35: the age distribution decides ⇒ I-1 must follow it.

    **The mutation is on the CRITERION's own measurement, not on the
    product.**  That is the point: the question is whether I-1's rule
    has any teeth.  A rule that cannot fail cannot pass.

    The mutation replaces the age list with zeros — "no row is old" —
    and the criterion must then report PASS on a library whose rows are
    two hours old.  That proves the age is what decides the verdict.
    """
    ws = _backlog_workspace(tmp_path, age_seconds=7200)

    # The un-mutated criterion: an old inline row IS a backlog.
    report, states = _run(phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
    assert states["I-1(REAL)"] == STATE_FAIL, report.printed
    assert "超龄" in report.printed, report.printed

    # The mutation: the ages are flattened to zero.
    original = phase2_s3t._inline_backlog

    def neutered(conn, now):
        info = original(conn, now)
        info["ages"] = [0] * len(info["ages"])
        return info

    phase2_s3t._inline_backlog = neutered
    try:
        report, states = _run(
            phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
    finally:
        phase2_s3t._inline_backlog = original

    assert states["I-1(REAL)"] == STATE_PASS, (
        "把年龄分布抹平后判定必须变绿 —— 这证明判定确实由年龄决定，"
        "而不是由别的什么决定；报告里必须还说得出『积压 N 条』")

    # And the restore brings the red back.
    report, states = _run(phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
    assert states["I-1(REAL)"] == STATE_FAIL, report.printed


def test_mu35b_a_fresh_inline_row_is_a_pass(tmp_path):
    """MU-35's companion: a FRESH inline row is a PASS.

    The transient state ("written a second ago, the externalisation is
    in flight") must not be judged FAIL — otherwise the criterion
    demands that the write path and the patrol be one transaction.
    """
    ws = _fresh_workspace(tmp_path)
    report, states = _run(phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
    assert states["I-1(REAL)"] == STATE_PASS, report.printed
    assert "积压" in report.printed


def test_mu35c_the_threshold_is_configurable(tmp_path):
    """MU-35's third leg: the age threshold is CONFIGURABLE, and the
    same corpus flips verdict when it moves.

    A threshold written into the source is a constant that belongs to
    one machine's tempo.  The criterion must be re-tunable through the
    environment WITHOUT editing the source.
    """
    ws = _backlog_workspace(tmp_path, age_seconds=120)

    old = os.environ.get("COW_I1_MAX_AGE_SECONDS")
    try:
        os.environ["COW_I1_MAX_AGE_SECONDS"] = "60"
        report, states = _run(
            phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
        assert states["I-1(REAL)"] == STATE_FAIL, report.printed

        os.environ["COW_I1_MAX_AGE_SECONDS"] = "100000"
        report, states = _run(
            phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
        assert states["I-1(REAL)"] == STATE_PASS, (
            "把阈值放到远大于行龄 ⇒ 判定必须转绿（阈值可配是硬要求）")
    finally:
        if old is None:
            os.environ.pop("COW_I1_MAX_AGE_SECONDS", None)
        else:
            os.environ["COW_I1_MAX_AGE_SECONDS"] = old


# ---------------------------------------------------------------------------
# MU-36 · the write path's externalisation is removed
# ---------------------------------------------------------------------------

def test_mu36_a_write_path_that_does_not_externalise_is_caught(tmp_path):
    """MU-36: 写入口不外置 ⇒ I-2 的行为证必须报红。

    The mutation is on the PRODUCT (the write path), so this is the
    proof that the behavioural half of I-2 measures the real write path
    and not the fixture's shape.
    """
    from agent.memory import conversation_store as cs

    report, states = _run(phase2_s3t.judge_i2_write_path_externalises,
                          _externalised_workspace(tmp_path))
    assert states["I-2(REAL)"] == STATE_PASS, report.printed

    # The mutation: the write path stops externalising.  The static
    # half (the AST check) still finds the call — so the BEHAVIOURAL
    # half is the one that has to catch this.
    original = cs.ConversationStore._externalise_after_commit

    def no_externalise(self, conn, session_id, seqs):
        return {"skipped": "mutant"}

    cs.ConversationStore._externalise_after_commit = no_externalise
    try:
        report, states = _run(
            phase2_s3t.judge_i2_write_path_externalises,
            _externalised_workspace(tmp_path, "ws-mutant"))
    finally:
        cs.ConversationStore._externalise_after_commit = original

    assert states["I-2(REAL)"] == STATE_FAIL, (
        "写入口不外置时 I-2 必须报红 —— 静态证明会继续通过，"
        "只有行为证能抓住这个变异")
    assert "external" in report.printed

    # The restore turns it green again.
    report, states = _run(phase2_s3t.judge_i2_write_path_externalises,
                          _externalised_workspace(tmp_path))
    assert states["I-2(REAL)"] == STATE_PASS, report.printed


# ---------------------------------------------------------------------------
# MU-37 · the truth tree and the DB disagree
# ---------------------------------------------------------------------------

def test_mu37a_a_jsonl_line_without_a_db_row_is_caught(tmp_path):
    """MU-37 (direction 1): the JSONL holds a line the DB does not know
    about ⇒ I-3 must be RED (a double-write / a lost pointer)."""
    ws = _externalised_workspace(tmp_path)

    report, states = _run(phase2_s3t.judge_i3_jsonl_and_db_conserve, ws)
    assert states["I-3(REAL)"] == STATE_PASS, report.printed

    from agent.memory import msg_chain
    root = msg_chain.export_root(ws)
    files = [p for p in sorted(root.rglob("*.jsonl"))
             if msg_chain.AUDIT_DIRNAME not in p.parts]
    assert files, "fixture must have a JSONL truth tree"
    line = {
        "seq": 999, "agent_id": "", "session_id": "s1", "role": "user",
        "content": "a line the DB does not know about",
        "created_at": 0, "run_id": "", "extras": "",
        "prev_hash": "0" * 64, "line_hash": "f" * 64,
    }
    with open(files[0], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    report, states = _run(phase2_s3t.judge_i3_jsonl_and_db_conserve, ws)
    assert states["I-3(REAL)"] == STATE_FAIL, (
        "JSONL 多出一行而 DB 没有 ⇒ I-3 必须报红")
    assert "JSONL 有" in report.printed


def test_mu37b_a_db_row_without_a_jsonl_line_is_caught(tmp_path):
    """MU-37 (direction 2): the DB has an external row the JSONL does
    not have ⇒ I-3 must be RED too.

    Without this half, a criterion that only checks "the file has
    nothing extra" would pass while the text is nowhere — and that is
    the more dangerous half: the row claims to be externalised, and
    there is no line to read back.
    """
    ws = _externalised_workspace(tmp_path)
    db = ws / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        # The row keeps its (agent_id, session_id) but its SEQ moves to
        # one the JSONL does not have — so the row claims to be
        # externalised and there is no line to read back.
        conn.execute(
            "UPDATE messages SET content_state='external', content=NULL, "
            "content_path='messages_export/2026-10-31/ghost.jsonl', "
            "content_anchor='line:1', seq=998 WHERE session_id='s1' AND seq=0")
        conn.commit()
    finally:
        conn.close()

    report, states = _run(phase2_s3t.judge_i3_jsonl_and_db_conserve, ws)
    assert states["I-3(REAL)"] == STATE_FAIL, report.printed
    assert "DB 有" in report.printed


def test_mu37c_the_comparison_is_on_sets_not_counts(tmp_path):
    """MU-37's third leg: two wrongs must not cancel each other out.

    A count-based comparison (``lines == rows``) would call this
    library GREEN: the JSONL has one line too many AND the DB has one
    row too many, so the COUNTS match.  The SET comparison must still
    see the difference.
    """
    ws = _externalised_workspace(tmp_path)
    db = ws / "memory" / "long-term" / "index.db"

    # The JSONL gains a line ...
    from agent.memory import msg_chain
    root = msg_chain.export_root(ws)
    files = [p for p in sorted(root.rglob("*.jsonl"))
             if msg_chain.AUDIT_DIRNAME not in p.parts]
    line = {
        "seq": 999, "agent_id": "", "session_id": "s1", "role": "user",
        "content": "ghost line", "created_at": 0, "run_id": "", "extras": "",
        "prev_hash": "0" * 64, "line_hash": "f" * 64,
    }
    with open(files[0], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ... and the DB gains a row with a DIFFERENT key.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO messages (agent_id, session_id, seq, role, content, "
            "content_state, content_path, content_anchor, created_at) "
            "VALUES ('', 's1', 998, 'user', NULL, 'external', "
            "'messages_export/2026-10-31/ghost.jsonl', 'line:1', 0)")
        conn.commit()
        n_lines = None
    finally:
        conn.close()

    report, states = _run(phase2_s3t.judge_i3_jsonl_and_db_conserve, ws)
    assert states["I-3(REAL)"] == STATE_FAIL, (
        "两边的数量相同但内容不同 ⇒ 必须报红（这就是『比集合不比数量』）")
    assert "JSONL 有" in report.printed and "DB 有" in report.printed


# ---------------------------------------------------------------------------
# MU-38 · the write boundary's ledger prune is removed
# ---------------------------------------------------------------------------

def test_mu38_a_write_boundary_that_does_not_prune_is_caught(tmp_path):
    """MU-38: 重切时不再剪账本 ⇒ I-4 必须报红。

    The mutation is on the PRODUCT (the write path's prune), so this
    proves I-4 measures the real mechanism and not its own idea of it.
    """
    from agent.memory import storage as storage_mod

    ws = _ledger_workspace(tmp_path)

    report, states = _run(
        phase2_s3t.judge_i4_recut_leaves_no_ledger_orphans, ws)
    assert states["I-4(REAL)"] == STATE_PASS, report.printed

    # The mutation: the write boundary stops pruning the ledger.
    original = storage_mod.MemoryStorage._prune_ledger_entries

    def no_prune(self, chunk_ids):
        return 0

    storage_mod.MemoryStorage._prune_ledger_entries = no_prune
    try:
        report, states = _run(
            phase2_s3t.judge_i4_recut_leaves_no_ledger_orphans, ws)
    finally:
        storage_mod.MemoryStorage._prune_ledger_entries = original

    assert states["I-4(REAL)"] == STATE_FAIL, (
        "重切后账本里还留着消失 id 的条目 ⇒ I-4 必须报红"
        "（写边界不再剪枝 ⇒ 机制没了）")
    assert "孤儿" in report.printed

    # The restore turns it green again.
    report, states = _run(
        phase2_s3t.judge_i4_recut_leaves_no_ledger_orphans, ws)
    assert states["I-4(REAL)"] == STATE_PASS, report.printed


# ---------------------------------------------------------------------------
# The three-state discipline (MU-12's rule, applied to the new module)
# ---------------------------------------------------------------------------

def test_zero_objects_is_notrun_never_pass(tmp_path):
    """「0 个对象 = NOTRUN，不是 PASS」 — the behaviour must be TESTED.

    An empty corpus must be reported as "there was nothing to measure"
    (NOTRUN), never as "measured zero, therefore fine" (PASS).  A
    criterion that reports PASS on an empty corpus is reporting on a
    corpus it never read.
    """
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)

    # A library with a messages table but NO ROWS.
    import sqlite3 as _s
    db = ws / "memory" / "long-term" / "index.db"
    conn = _s.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, agent_id TEXT, "
            "session_id TEXT, seq INTEGER, role TEXT, content TEXT, "
            "content_path TEXT, content_anchor TEXT, content_hash TEXT, "
            "content_state TEXT NOT NULL DEFAULT 'inline', "
            "extras_compensated INTEGER, created_at INTEGER, extras TEXT, "
            "run_id TEXT)")
        conn.commit()
    finally:
        conn.close()

    report, states = _run(phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
    assert states["I-1(REAL)"] == STATE_NOTRUN, (
        "空的 messages 表 ⇒ 0 个对象 ⇒ 必须是 NOTRUN，不是 PASS")
    assert "0 个对象" in report.printed


def test_no_workspace_is_notrun(tmp_path):
    """No live library at all ⇒ NOTRUN (there was nothing to measure)."""
    ws = tmp_path / "empty"
    ws.mkdir()
    report, states = _run(phase2_s3t.judge_i1_inline_backlog_is_bounded, ws)
    assert states["I-1(REAL)"] == STATE_NOTRUN, report.printed


def test_the_module_declares_its_criteria():
    """The four criteria are in the module's CRITERIA list — the wiring
    table is the contract, and a criterion that is not listed is a
    criterion that does not run."""
    ids = [cid for cid, _fn in phase2_s3t.CRITERIA]
    assert ids == ["I-1", "I-2", "I-3", "I-4"], ids


def test_the_module_has_no_machine_specific_paths():
    """The criteria must be portable: no absolute path from THIS
    machine, and no hard-coded corpus constant (团队规范 §3.6)."""
    src = (REPO / "agent" / "memory" / "acceptance" / "phase2_s3t.py") \
        .read_text(encoding="utf-8")
    for needle in ("C:\\Users", "C:/Users", "\\\\Users\\"):
        assert needle not in src, (
            "判据里出现了本机绝对路径: %r" % needle)
    # The workspace is DISCOVERED, never written down.
    assert "DEFAULT_WORKSPACE" not in src
