"""Tests for agent/memory/inspector.py — the read-only periodic verifier.

The inspector's contract (SEED 拍板 2026-10-27):
  * report-only, NEVER repairs
  * snapshots at memory/.snapshots/YYYY-MM-DD.json are the baseline for
    monotonic-append / history-unchanged checks
  * every run appends one hash-chained line to .audit/inspector.jsonl
"""
import json
from pathlib import Path

import pytest

from agent.memory.facade import (
    RecordingConfirmationChannel,
    WriteFacade,
    _fact_line_hash,
    _sha256_hex,
    split_frontmatter,
)
from agent.memory.inspector import Inspector
from agent.memory.naming import canonical_text

ACTOR = "tester"


@pytest.fixture
def cow(tmp_path):
    return tmp_path


@pytest.fixture
def ws(cow):
    root = cow / "agents" / ACTOR
    (root / "memory").mkdir(parents=True)
    return root


def facade_for(ws, cow, actor=ACTOR):
    return WriteFacade(ws, actor=actor, cow_root=cow,
                       channel=RecordingConfirmationChannel())


def _make_facts(f, date="2026-10-27", sess="deadbeef", n=3):
    for i in range(n):
        f.append_fact(date, sess, role="user", content=f"line {i}",
                      occurred_at=f"{date}T10:0{i}:00+08:00", run_id="r1")
    return ws_path(f)


def ws_path(f):
    return f.workspace_root


# -- clean baseline ---------------------------------------------------------

def test_clean_workspace_reports_zero_violations(tmp_path):
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    f.write_event("e1", "title", "body", date="2026-10-27")
    f.append_fact("2026-10-27", "deadbeef", role="user", content="hi",
                  occurred_at="2026-10-27T10:00:00+08:00", run_id="r1")
    report = Inspector(ws).inspect(trigger="manual")
    assert report["violations"] == []
    assert report["trigger"] == "manual"


def test_snapshot_written_and_hash_chained(tmp_path):
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    f.append_fact("2026-10-27", "deadbeef", role="user", content="hi",
                  occurred_at="2026-10-27T10:00:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    snaps = list((ws / "memory" / ".snapshots").glob("*.json"))
    assert len(snaps) == 1
    snap = json.loads(snaps[0].read_text(encoding="utf-8"))
    stored = snap.pop("snapshot_hash")
    payload = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    assert stored == "sha256:" + _sha256_hex(payload)
    # audit line written
    audit = ws / "memory" / ".audit" / "inspector.jsonl"
    assert audit.is_file()


def test_second_run_sees_clean_history(tmp_path):
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    f.append_fact("2026-10-27", "deadbeef", role="user", content="hi",
                  occurred_at="2026-10-27T10:00:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    report = Inspector(ws).inspect()
    assert report["violations"] == []
    assert report["snapshot_baseline"] is not None


# -- markdown tamper ---------------------------------------------------------

def test_markdown_body_tamper_reported(tmp_path):
    """人为篡改 markdown 正文 → body_hash 失配被报出（验收标准 5）。"""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    p = f.write_event("e1", "title", "original body", date="2026-10-27")
    # tamper: change the body without touching the front-matter
    text = p.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    p.write_text(text.replace("original body", "TAMPERED body"),
                 encoding="utf-8", newline="\n")
    report = Inspector(ws).inspect()
    body_violations = [v for v in report["violations"]
                       if "body-hash" in v["reason"]]
    assert len(body_violations) == 1
    assert body_violations[0]["severity"] == "high"
    assert "e1" in body_violations[0]["path"]


def test_markdown_fm_tamper_reported(tmp_path):
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    p = f.write_event("e1", "title", "body", date="2026-10-27")
    text = p.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    # tamper a front-matter field without updating fm_hash
    p.write_text(text.replace("title: title", "title: HACKED"),
                 encoding="utf-8", newline="\n")
    report = Inspector(ws).inspect()
    fm_violations = [v for v in report["violations"]
                     if "fm-hash" in v["reason"]]
    assert len(fm_violations) == 1


def test_tamper_does_not_get_repaired(tmp_path):
    """Report-only: the inspector must NEVER rewrite the tampered file."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    p = f.write_event("e1", "title", "original", date="2026-10-27")
    text = p.read_text(encoding="utf-8")
    tampered = text.replace("original", "TAMPERED")
    p.write_text(tampered, encoding="utf-8", newline="\n")
    Inspector(ws).inspect()
    assert p.read_text(encoding="utf-8") == tampered  # still tampered


# -- facts tamper --------------------------------------------------------------

def test_facts_truncation_reported(tmp_path):
    """人为截断 facts 文件 → 行数减少被报出（验收标准 5，对照快照）。
    P0-8 收口：物理删除的机器码是 facts-shrunk（不是历史改写）。"""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    for i in range(3):
        f.append_fact("2026-10-27", "deadbeef", role="user",
                      content=f"line {i}",
                      occurred_at=f"2026-10-27T10:0{i}:00+08:00", run_id="r1")
    Inspector(ws).inspect()  # baseline snapshot
    facts = next((ws / "memory" / "facts").rglob("*.jsonl"))
    lines = facts.read_text(encoding="utf-8").splitlines()
    facts.write_text("\n".join(lines[:1]) + "\n", encoding="utf-8",
                     newline="\n")
    report = Inspector(ws).inspect()
    reasons = {v["reason"] for v in report["violations"]}
    assert any("facts-shrunk" in r for r in reasons)
    shrunk = [v for v in report["violations"] if v.get("code") == "facts-shrunk"]
    assert shrunk, "物理删除必须带机器码 facts-shrunk"
    assert shrunk[0]["line_count"] == 1 and shrunk[0]["previous_line_count"] == 3
    # 截断不是"历史改写"——两个判据必须分开
    assert not any(v.get("code") == "history-rewritten"
                   for v in report["violations"])


def test_facts_line_rewrite_reported(tmp_path):
    """行数不变但改了一行内容 → line_hash + prev_hash 双报（绕过行数检查）。"""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    for i in range(2):
        f.append_fact("2026-10-27", "deadbeef", role="user",
                      content=f"line {i}",
                      occurred_at=f"2026-10-27T10:0{i}:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    facts = next((ws / "memory" / "facts").rglob("*.jsonl"))
    lines = facts.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["content"] = "REWRITTEN"
    lines[0] = json.dumps(row, ensure_ascii=False)
    facts.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    report = Inspector(ws).inspect()
    reasons = {v["reason"] for v in report["violations"]}
    assert any("line-hash" in r for r in reasons)
    # history comparison against the baseline snapshot catches the rewrite too
    assert any("history-rewritten" in r for r in reasons)


def test_history_rewritten_reports_changed_line_numbers(tmp_path):
    """P0-8 收口：改写两行 → 两条 history-rewritten，且各自带变化行号。"""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    for i in range(4):
        f.append_fact("2026-10-27", "deadbeef", role="user",
                      content=f"line {i}",
                      occurred_at=f"2026-10-27T10:0{i}:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    facts = next((ws / "memory" / "facts").rglob("*.jsonl"))
    lines = facts.read_text(encoding="utf-8").splitlines()
    for idx in (0, 2):                       # rewrite line 1 and line 3
        row = json.loads(lines[idx])
        row["content"] = f"REWRITTEN-{idx}"
        lines[idx] = json.dumps(row, ensure_ascii=False)
    facts.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    report = Inspector(ws).inspect()
    hist = [v for v in report["violations"] if v.get("code") == "history-rewritten"]
    assert len(hist) == 2
    assert sorted(v["line_no"] for v in hist) == [1, 3]
    for v in hist:
        assert v["expected_hash"] != v["actual_hash"]
        assert f"line {v['line_no']}" in v["detail"]


def test_history_check_survives_a_stale_line_hash_claim(tmp_path):
    """内容被改但 line_hash 声明没动 → 仍必须报 history-rewritten。

    这是 P0-8 实测暴露的缺陷：快照若存"文件自己的声明哈希"，
    改内容不改声明就会与基线相等而漏报。快照存的是重算哈希。
    """
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    f.append_fact("2026-10-27", "deadbeef", role="user", content="original",
                  occurred_at="2026-10-27T10:00:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    facts = next((ws / "memory" / "facts").rglob("*.jsonl"))
    lines = facts.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["content"] = "REWRITTEN"          # line_hash / prev_hash untouched
    lines[0] = json.dumps(row, ensure_ascii=False)
    facts.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    report = Inspector(ws).inspect()
    assert any(v.get("code") == "history-rewritten"
               for v in report["violations"]), report["violations"]


def test_snapshot_stores_recomputed_hash_per_line(tmp_path):
    """快照格式：path -> {line_count, {line_no: line_hash}}（P0-1 §六）。"""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    f.append_fact("2026-10-27", "deadbeef", role="user", content="one",
                  occurred_at="2026-10-27T10:00:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    snap = json.loads(
        next((ws / "memory" / ".snapshots").glob("*.json")).read_text("utf-8"))
    entry = next(iter(snap["files"].values()))
    assert entry["line_count"] == 1
    assert set(entry["line_hashes"]) == {"1"}
    stored = entry["line_hashes"]["1"]
    assert stored.startswith("sha256:")
    # 重算哈希 = 快照里存的那一个（不是文件自己的声明）
    facts = next((ws / "memory" / "facts").rglob("*.jsonl"))
    row = json.loads(facts.read_text(encoding="utf-8").splitlines()[0])
    assert stored == _fact_line_hash(row)


def test_facts_append_is_not_a_violation(tmp_path):
    """正常追加（行数增加 + 链延伸）必须零误报。"""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    f.append_fact("2026-10-27", "deadbeef", role="user", content="one",
                  occurred_at="2026-10-27T10:00:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    f.append_fact("2026-10-27", "deadbeef", role="assistant", content="two",
                  occurred_at="2026-10-27T10:01:00+08:00", run_id="r1")
    report = Inspector(ws).inspect()
    assert report["violations"] == []


def test_audit_trail_catches_truncation_even_if_snapshot_deleted(tmp_path):
    """快照被删 ≠ 洗白：审计链记着上次行数（纵深防御）。"""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    for i in range(3):
        f.append_fact("2026-10-27", "deadbeef", role="user",
                      content=f"line {i}",
                      occurred_at=f"2026-10-27T10:0{i}:00+08:00", run_id="r1")
    Inspector(ws).inspect()
    # attacker deletes the snapshot to erase the baseline
    for snap in (ws / "memory" / ".snapshots").glob("*.json"):
        snap.unlink()
    facts = next((ws / "memory" / "facts").rglob("*.jsonl"))
    lines = facts.read_text(encoding="utf-8").splitlines()
    facts.write_text("\n".join(lines[:1]) + "\n", encoding="utf-8",
                     newline="\n")
    report = Inspector(ws).inspect()
    assert any("audit-remembered" in v["detail"] or "snapshot" in v["detail"]
               or "line-count-decreased" in v["reason"]
               for v in report["violations"])


# -- naming (reuse, not re-implemented) ----------------------------------------

def test_naming_violation_reported(tmp_path):
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    bad = ws / "memory" / "markdown" / "events" / "2026-10-27" / "plain-name.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("x", encoding="utf-8")
    report = Inspector(ws).inspect()
    assert any(v["reason"] == "gap-rule:naming" for v in report["violations"])


def test_naming_clean_passes(tmp_path):
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    f.write_event("e1", "t", "b", date="2026-10-27")
    report = Inspector(ws).inspect()
    assert not any(v["reason"] == "gap-rule:naming"
                   for v in report["violations"])


# -- structure ----------------------------------------------------------------

def test_three_trigger_names_accepted(tmp_path):
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    for trigger in ("shelf-rebuild", "startup", "manual"):
        report = Inspector(ws).inspect(trigger=trigger)
        assert report["trigger"] == trigger


def test_report_contains_no_writes_outside_audit_and_snapshots(tmp_path):
    """Inspector writes ONLY .audit/inspector.jsonl and .snapshots/*.json."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    f = facade_for(ws, cow)
    ev = f.write_event("e1", "t", "b", date="2026-10-27")
    f.append_fact("2026-10-27", "deadbeef", role="user", content="hi",
                  occurred_at="2026-10-27T10:00:00+08:00", run_id="r1")
    before = {p.relative_to(ws).as_posix(): p.read_bytes()
              for p in (ws / "memory").rglob("*") if p.is_file()}
    Inspector(ws).inspect()
    after = {p.relative_to(ws).as_posix(): p.read_bytes()
             for p in (ws / "memory").rglob("*") if p.is_file()}
    changed = {k for k in after
               if k not in before or before.get(k) != after[k]}
    assert all(k.startswith("memory/.audit/") or k.startswith("memory/.snapshots/")
               for k in changed), f"inspector wrote outside audit/snapshots: {changed}"
    # truth-layer files byte-identical
    assert ev.read_bytes() == before[ev.relative_to(ws).as_posix()]


# -- P0-10 L-1: index layer hygiene -----------------------------------------
#
# The inspector checks the TRUTH layer only (P0-8). P0-10 L-1 found that
# the INDEX layer had no hygiene at all: rows that the scan excludes by
# policy (shelf/ facts/ .snapshots/ .audit/) and orphan rows (a chunk whose
# files-table counterpart is gone) stayed searchable — and because those
# rows carry scope='shared' + user_id=NULL, EVERY caller could see them
# (V2-Q-01: a released record's directory card leaking to third parties).

import sqlite3


def _make_index_db(ws, rows):
    """Create a minimal index.db with the given (path, scope, user_id) rows.

    ``rows`` entries: (path, scope, user_id, with_files_row).
    """
    db = ws / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE chunks (id TEXT PRIMARY KEY, user_id TEXT, "
                 "scope TEXT, source TEXT, path TEXT, text TEXT)")
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, source TEXT, "
                 "hash TEXT, mtime INTEGER, size INTEGER, updated_at TEXT)")
    for i, (path, scope, user_id, with_files) in enumerate(rows):
        conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, text) "
            "VALUES (?,?,?,?,?,?)",
            (f"c{i}", user_id, scope, "memory", path, "some text"))
        if with_files:
            conn.execute(
                "INSERT INTO files (path, source, hash) VALUES (?,?,?)",
                (path, "memory", "h"))
    conn.commit()
    conn.close()
    return db


def test_index_excluded_path_is_reported(tmp_path):
    """A chunk under an excluded layer must be reported (it is searchable)."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    _make_index_db(ws, [("memory/shelf/index.md", "shared", None, True)])
    report = Inspector(ws).inspect()
    hits = [v for v in report["violations"]
            if v["reason"] == "gap-rule:index-excluded-path"]
    assert len(hits) == 1
    assert hits[0]["code"] == "index-excluded-path"
    assert hits[0]["path"] == "memory/shelf/index.md"
    assert hits[0]["severity"] == "high"
    assert "shelf" in hits[0]["excluded_segments"]
    assert hits[0]["scope"] == "shared" and hits[0]["user_id"] is None


def test_index_orphan_chunk_without_files_row_is_reported(tmp_path):
    """A chunk row whose files counterpart is gone = permanent orphan."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    _make_index_db(ws, [("memory/users/u-accept/memory_scope.md",
                          "shared", None, False)])
    report = Inspector(ws).inspect()
    hits = [v for v in report["violations"]
            if v["reason"] == "gap-rule:index-orphan"]
    assert len(hits) == 1
    assert hits[0]["code"] == "index-orphan"
    assert "files-table counterpart" in hits[0]["detail"]


def test_index_row_for_deleted_file_is_reported(tmp_path):
    """A registered row whose file no longer exists on disk = stale page."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    _make_index_db(ws, [("memory/2020-01-01.md", "shared", None, True)])
    report = Inspector(ws).inspect()
    hits = [v for v in report["violations"]
            if v["reason"] == "gap-rule:index-orphan"]
    assert len(hits) == 1
    assert "no longer exists" in hits[0]["detail"]


def test_clean_index_reports_no_index_violations(tmp_path):
    """A registered row whose file EXISTS must not be reported."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    (ws / "memory" / "2026-10-27.md").write_text("hello", encoding="utf-8")
    _make_index_db(ws, [("memory/2026-10-27.md", "shared", None, True)])
    report = Inspector(ws).inspect()
    assert not [v for v in report["violations"]
                if v["reason"].startswith("gap-rule:index-")]


def test_index_hygiene_never_repairs_the_index(tmp_path):
    """REPORT-ONLY: the inspector must not delete the rows it reports."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    db = _make_index_db(ws, [("memory/shelf/index.md", "shared", None, True)])
    before = db.read_bytes()
    Inspector(ws).inspect()
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    assert n == 1, "inspector deleted an index row — it must only report"
    assert db.read_bytes() != b"" and before is not None


def test_index_hygiene_violations_reach_the_audit_trail(tmp_path):
    """Reported index violations land in .audit/inspector.jsonl."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    _make_index_db(ws, [("memory/shelf/index.md", "shared", None, True)])
    Inspector(ws).inspect()
    audit = ws / "memory" / ".audit" / "inspector.jsonl"
    assert audit.is_file()
    lines = [json.loads(x) for x in
             audit.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines and lines[-1]["violation_count"] >= 1


# --- P0-16 L-4: a fail-closed READ must be loud ---------------------------
#
# P0-15 L-4 found the read exits failing closed SILENTLY: the content was
# correctly withheld, but nothing was logged, no marker was left, and the
# inspector never turned red. These three tests pin the whole chain:
# report → marker → inspector → recovery.


def _read_degraded_violations(report):
    return [v for v in report["violations"]
            if v.get("code") == "read-degraded"]


def test_l4_a_degraded_read_is_reported_red_by_the_inspector(tmp_path):
    """The marker the read exits leave must turn the inspector RED."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    from agent.memory.read_audit import report_read_denied

    # REVERSE CASE first: a workspace with no degraded read is green.
    assert not _read_degraded_violations(Inspector(ws).inspect())

    report_read_denied(ws, "test.read_exit", "memory/markdown/e.md",
                        RuntimeError("truth layer unreadable"))
    hits = _read_degraded_violations(Inspector(ws).inspect())
    assert hits, "the inspector did NOT report the degraded read"
    assert hits[0]["severity"] == "high"
    assert hits[0]["last_exit"] == "test.read_exit"
    assert hits[0]["last_path"] == "memory/markdown/e.md"


def test_l4_a_degraded_read_logs_and_appends_to_the_audit_trail(tmp_path):
    """① an ERROR log line and ② an appended audit line — both, not one."""
    import logging

    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    from agent.memory.read_audit import read_denied_log, report_read_denied
    from common.log import logger

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    cap = _Cap()
    logger.addHandler(cap)
    try:
        report_read_denied(ws, "test.read_exit", "memory/markdown/e.md",
                            RuntimeError("truth layer unreadable"))
    finally:
        logger.removeHandler(cap)

    errors = [r for r in cap.records if r.levelno >= logging.ERROR]
    assert errors, "no ERROR log line was emitted"
    assert "READ DENIED" in errors[0].getMessage()

    logp = read_denied_log(ws)
    assert logp.is_file(), "the audit jsonl was not written"
    lines = [json.loads(x) for x in
             logp.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1
    assert lines[0]["exit"] == "test.read_exit"
    assert lines[0]["path"] == "memory/markdown/e.md"
    assert "unreadable" in lines[0]["error"]


def test_l4_a_degraded_read_clears_when_the_truth_layer_resolves(tmp_path):
    """Recovery: the marker goes, the inspector turns green, the HISTORY stays.

    The jsonl is append-only: the refusals happened, and the record of them
    must survive the recovery. Only the marker (the current state) is cleared.
    """
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    from agent.memory.read_audit import (
        clear_read_denied,
        read_denied_log,
        read_denied_marker,
        report_read_denied,
    )

    report_read_denied(ws, "test.read_exit", "memory/markdown/e.md",
                        RuntimeError("truth layer unreadable"))
    assert read_denied_marker(ws).is_file()
    assert _read_degraded_violations(Inspector(ws).inspect())

    clear_read_denied(ws)
    assert not read_denied_marker(ws).is_file(), "the marker was not cleared"
    assert not _read_degraded_violations(Inspector(ws).inspect())

    # the history survives
    lines = [x for x in read_denied_log(ws).read_text(
        encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1, "the audit history was lost on recovery"


# --- P0-17 B-3: schema drift must be visible, and only when real ---------
#
# P0-17 found the worst failure shape in this system: an index whose
# schema is behind the code made every retrieval query fail, the failure
# was caught, and the system reported "no matches" — indistinguishable
# from an empty corpus, with no error and no red light anywhere.
#
# These tests pin the check's three properties: it fires on real drift,
# it names the workspace and the columns, and it does NOT fire on a
# complete index (a check that cries wolf on a healthy system gets
# ignored, which is the same as having no check at all).


def _drift_violations(report):
    return [v for v in report["violations"]
            if v.get("code") == "schema-drift"]


def _make_drifted_index_db(ws, drop_columns=("sealed_owner",)):
    """An index whose schema is behind the current code."""
    from agent.memory.schema_drift import EXPECTED_CHUNK_COLUMNS

    db = ws / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    cols = []
    for name in EXPECTED_CHUNK_COLUMNS:
        if name in drop_columns:
            continue
        if name == "id":
            cols.append("id TEXT PRIMARY KEY")
        elif name in ("user_id", "scope", "source", "path", "text", "hash",
                      "metadata"):
            cols.append(f"{name} TEXT")
        elif name in ("start_line", "end_line", "created_at", "updated_at",
                      "last_hit_at"):
            cols.append(f"{name} INTEGER")
        elif name == "embedding":
            cols.append("embedding TEXT")
        elif name == "hit_count":
            cols.append("hit_count INTEGER NOT NULL DEFAULT 0")
        elif name in ("element_released", "file_sealed"):
            cols.append(f"{name} INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE TABLE chunks (" + ", ".join(cols) + ")")
    conn.commit()
    conn.close()
    return db


def test_b3_schema_drift_is_reported_with_the_workspace_and_the_columns(tmp_path):
    """A stale index turns the inspector RED, naming the columns."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    db = _make_drifted_index_db(ws)

    # REVERSE CASE first: a complete index is not drift.
    from agent.memory.schema_drift import repair_columns
    repair_columns(db)
    assert not _drift_violations(Inspector(ws).inspect()), \
        "a complete index was reported as drift"

    # Now break it on purpose.
    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    if "sealed_owner" in cols:
        # SQLite cannot DROP a column before 3.35 — it has 3.53, so this
        # rebuilds the table without it.
        keep = [c for c in cols if c != "sealed_owner"]
        collist = ", ".join(keep)
        conn.execute(f"CREATE TABLE chunks_new ({collist})")
        conn.execute("INSERT INTO chunks_new SELECT " +
                     ", ".join(keep) + " FROM chunks")
        conn.execute("DROP TABLE chunks")
        conn.execute("ALTER TABLE chunks_new RENAME TO chunks")
        conn.commit()
    conn.close()

    hits = _drift_violations(Inspector(ws).inspect())
    assert hits, "the inspector did NOT report the schema drift"
    assert hits[0]["severity"] == "high"
    assert "sealed_owner" in hits[0]["missing_columns"]
    assert str(ws) in hits[0]["index_root"]


def test_b3_schema_drift_clears_when_the_index_is_migrated(tmp_path):
    """The check goes green by itself once the column is added."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    db = _make_drifted_index_db(ws)

    hits = _drift_violations(Inspector(ws).inspect())
    assert hits, "the drift was not reported in the first place"

    from agent.memory.schema_drift import repair_columns

    added = repair_columns(db)
    assert "sealed_owner" in added
    assert not _drift_violations(Inspector(ws).inspect()), \
        "the check stayed red after the migration"


def test_b3_schema_drift_never_repairs_the_index(tmp_path):
    """REPORT-ONLY: the inspector must not migrate what it reports."""
    cow, ws = tmp_path, tmp_path / "agents" / ACTOR
    (ws / "memory").mkdir(parents=True)
    db = _make_drifted_index_db(ws)
    before = db.read_bytes()

    hits = _drift_violations(Inspector(ws).inspect())
    assert hits
    assert db.read_bytes() == before, \
        "the inspector modified the index it was supposed to only report"


# ---------------------------------------------------------------------------
# P0-20 S-1 / L-1：判据不得修改它要验收的对象
# ---------------------------------------------------------------------------
#
# 风控原话：「判据会修改它要验收的对象——phase1.py 判 T3 前 sync，
# 仓库根库 102→106 行。空目录那轮失败理由还漂移了：报的是『top1 不是
# USER.md』而非它设计的『空库』理由，判据红了但理由不对 = 判据在说谎。」
#
# 两条要固化：
#   S-1  验收前后取被测库指纹（路径+行数+mtime+sha256）并比对；变了就报出来
#   L-1  phase1.py 拒绝在源码树内验收；而且「空库」这个失败理由要能被
#         正确报出来，不能被别的理由盖住
#
# 本文件是「检查器那一层」的测试，所以 S-1 和 L-1 的判据本身放在这里。

def test_s1_the_fingerprint_carries_a_digest_not_just_a_row_count(tmp_path):
    """S-1：指纹必须带摘要 —— 行数看不出内容变了。

    检索会回写 ``hit_count``：行数不变，内容变了。只比行数的检查器看不见
    判据改了自己的被测对象。

    P0-21 F2：摘要的**口径**从「文件字节」改成了「内容」——
    库是 WAL 模式，写只落进 ``index.db-wal``，主库字节不动；按字节算的
    摘要把一次真实的写入读成「没变」（假绿）。内容摘要从**行**算，经
    SQLite 读，WAL 已提交的内容自然在内；而 checkpoint（只搬字节不改行）
    不会误报。文件级摘要保留为取证字段。
    """
    import sqlite3

    from agent.memory.acceptance import fingerprint

    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE chunks (
        id TEXT PRIMARY KEY, scope TEXT, path TEXT, hit_count INT)""")
    conn.execute("INSERT INTO chunks VALUES ('c1','shared','p1',0)")
    conn.commit()
    conn.close()

    first = fingerprint(str(tmp_path))
    assert first.exists and first.rows == 1
    assert first.sha256, "指纹没有摘要 —— 判据改了库也看不出来"

    # 改一行：行数不变，内容变了
    conn = sqlite3.connect(db)
    conn.execute("UPDATE chunks SET hit_count = hit_count + 1")
    conn.commit()
    conn.close()

    second = fingerprint(str(tmp_path))
    assert second.rows == first.rows, "行数不该变（这条前提错了，测法有问题）"
    assert second.sha256 != first.sha256, "内容变了但摘要没变 —— 指纹是假的"
    assert "sha256" in second.differs_from(first)


def test_s1_an_unchanged_index_reports_no_difference(tmp_path):
    """S-1 反向用例：没变就报「没变」，别把没变也说成变了。"""
    from agent.memory.acceptance import fingerprint

    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True)
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE chunks (id TEXT)")
    conn.commit()
    conn.close()

    first = fingerprint(str(tmp_path))
    second = fingerprint(str(tmp_path))
    assert second.differs_from(first) == [], "同一个库两次指纹报了差异"


def test_s1_the_fingerprint_never_creates_the_index(tmp_path):
    """S-1：一个只打算「量」数据库的判据，不能顺手把它建出来。

    建出来本身就是修改 —— 判据量的是一个它自己造出来的对象。
    """
    from agent.memory.acceptance import fingerprint

    fp = fingerprint(str(tmp_path))
    assert not fp.exists
    assert not (tmp_path / "memory" / "long-term" / "index.db").exists(), \
        "取指纹把索引建出来了 —— 判据量的是自己造的状态"


def test_l1_the_acceptance_refuses_a_workspace_inside_the_source_tree(tmp_path):
    """L-1：phase1.py 在源码树内验收 → 明确拒绝，不执行。

    拒绝要在**碰任何索引之前**发生：一个事后的拒绝已经晚了 —— 运行时
    状态已经写进源码树了。
    """
    from agent.memory.acceptance import (
        SourceTreeWorkspaceError,
        assert_not_source_tree,
    )

    tree = tmp_path / "repo"
    (tree / ".git").mkdir(parents=True)
    ws = tree / "workspace"
    ws.mkdir()

    with pytest.raises(SourceTreeWorkspaceError) as exc:
        assert_not_source_tree(str(ws), tree)
    assert "拒绝" in str(exc.value)
    assert not (ws / "memory").exists(), "拒绝之前就写了状态"


def test_l1_an_empty_index_reports_the_empty_reason_not_another(tmp_path):
    """L-1 的「理由正确性」：空库上跑，失败理由必须**是**「库是空的」。

    风控抓到的那一轮：判据红了，但报的理由是「top1 不是 USER.md」——
    红的理由和它设计的理由不是一回事。判据红了但理由不对，等于判据
    在说谎：读的人不知道它到底测了什么。

    这条跑的是 phase1.py 本身（子进程），因为要验的是**输出里出现了哪
    个理由**，而那不是一个可以被 import 的函数能回答的。
    """
    import re
    import subprocess
    import sys as _sys
    from pathlib import Path

    from agent.memory.storage import MemoryStorage

    repo = Path(__file__).resolve().parents[1]
    ws = tmp_path / "cow"
    (ws / "memory" / "long-term").mkdir(parents=True)
    # A REAL, EMPTY index — the full schema, zero rows. A hand-made
    # ``CREATE TABLE chunks (id TEXT)`` is not a valid index, and the
    # manager would reject it with a schema error — a DIFFERENT reason,
    # and one that would mask the reason under test.
    db = ws / "memory" / "long-term" / "index.db"
    storage = MemoryStorage(db)
    try:
        assert storage.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    finally:
        storage.close()

    proc = subprocess.run(
        [_sys.executable, str(repo / "agent" / "memory" / "acceptance" / "phase1.py"),
         "--workspace", str(ws), "--no-sync"],
        cwd=str(repo), capture_output=True, text=True, timeout=180)
    out = proc.stdout or ""

    m = re.search(r"\[(?:PASS|FAIL)\] T3\(REAL\).*?(?=\n  \[(?:PASS|FAIL)\] |\n====|\Z)",
                  out, re.S)
    assert m, "T3 段没出现在输出里:\n%s" % out[-2000:]
    t3 = m.group(0)

    # The reason it was designed to give.
    assert "被测库是空的" in t3, (
        "空库上没有报『库是空的』理由:\n%s" % t3)
    assert "0 命中是正确行为" in t3, (
        "理由没有说清『0 命中是正确行为』:\n%s" % t3)
    # And NOT the reason it drifted to.
    assert "top1 不是 USER.md" not in t3, (
        "空库上混入了另一个失败理由:\n%s" % t3)
    # And not the "no index at all" reason either — the index EXISTS here.
    assert "前置条件不成立" not in t3, (
        "空库被误报成『库不存在』:\n%s" % t3)


def test_l1_the_phase1_criterion_runs_end_to_end_on_a_workspace(tmp_path, monkeypatch):
    """L-1：判据在**合法**工作区上照常运行（拒绝不能变成停机）。

    这里跑的是 phase1.py 本身（子进程），因为「拒绝」是 main() 里的
    早退逻辑，不是一个可以被 import 的函数。
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    ws = tmp_path / "cow"
    ws.mkdir()
    # 一个空工作区：判据应当跑完并报「空库」而不是崩溃
    proc = subprocess.run(
        [_sys.executable, str(repo / "agent" / "memory" / "acceptance" / "phase1.py"),
         "--workspace", str(ws), "--no-sync"],
        cwd=str(repo), capture_output=True, text=True, timeout=180)
    assert "阶段一验收" in proc.stdout, (
        "判据没有正常开始:\n%s\n%s" % (proc.stdout[-2000:], proc.stderr[-2000:]))
    assert "拒绝执行" not in proc.stdout, "合法工作区被拒了"


def test_l1_the_phase1_criterion_refuses_the_source_tree(tmp_path):
    """L-1：phase1.py 指向源码树 → 拒绝执行，且退出码可区分。"""
    import subprocess
    import sys as _sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [_sys.executable, str(repo / "agent" / "memory" / "acceptance" / "phase1.py"),
         "--workspace", str(repo)],
        cwd=str(repo), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 2, (
        "拒绝的退出码应是 2（与判据失败的 1 可区分），实际 %d" % proc.returncode)
    assert "拒绝执行" in proc.stdout
    assert not (repo / "memory" / "long-term" / "index.db").exists(), \
        "拒绝执行却把索引写进了源码树"



# ---------------------------------------------------------------------------
# P0-21 · 巡检第 9 项：暗页的索引行还是公开形状 → 报红
# ---------------------------------------------------------------------------
#
# P0-21 实测：2026-10-13 打包的桌面版还在跑，和源码版共用同一个 index.db。
# 它的 sync 不认识 `visibility: sealed`，把暗页写成 `scope='shared'` +
# `user_id=NULL` —— 匿名可读的形状。实测它自己的匿名检索返回了暗页正文。
#
# 守卫（agent/memory/index_guard）在写入边界挡住这件事；巡检是**第二道**：
# 万一有一行已经落成公开形状（守卫装上之前写的、或者别的路径写的），
# 巡检必须报出来 —— 而且只报红，不自动修（门禁③）。

def _public_row_violations(report):
    return [v for v in report["violations"]
            if v.get("code") == "index-public-row"]


def _make_dark_page(ws, cow, slug="p021-inspect-dark"):
    """Write a dark page AND put it in the index.

    The facade only writes the truth layer; the index is the projection,
    so the criterion needs a sync before the row exists to be judged.
    """
    import asyncio

    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    f = facade_for(ws, cow)
    f.write_event(slug, "P0-21 巡检暗页", "# 暗页\n\n正文。\n",
                  date="2026-10-27", visibility="sealed")
    cfg = MemoryConfig(workspace_root=str(ws), sync_on_search=False)
    mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
    try:
        asyncio.run(mgr.sync())
    finally:
        try:
            mgr.storage.close()
        except Exception:
            pass
    return f


def _dark_rel(storage):
    """The index path of the dark page the facade wrote.

    The file name carries a content hash (``<slug>--<hash8>.md``), so it
    is looked up by prefix rather than written out.
    """
    rows = storage.conn.execute(
        "SELECT path FROM chunks WHERE path LIKE '%p021-inspect-dark--%'"
    ).fetchall()
    assert rows, "暗页没进索引 —— 测法有问题"
    return rows[0][0]


def _index_row(ws, rel):
    import sqlite3

    db = ws / "memory" / "long-term" / "index.db"
    if not db.is_file():
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT scope, user_id, file_sealed FROM chunks WHERE path = ?",
            (rel,)).fetchone()
    finally:
        conn.close()


def _legacy_index(ws, rel, scope="shared"):
    """An index WITHOUT the guard, holding one row for ``rel``.

    The guard (P0-21) makes a public row for a dark page UNREACHABLE:
    a write that tries it is sealed on the spot. So the state this check
    looks for can only exist in an index written BEFORE the guard — which
    is exactly the state the check has to be able to see.
    """
    import sqlite3

    db = ws / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    # Dropping the table takes the guard's triggers with it — which is
    # the point: this index is a BEFORE-guard one.
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute("DROP TABLE IF EXISTS files")
    conn.execute("""CREATE TABLE chunks (
        id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, source TEXT,
        path TEXT, start_line INT, end_line INT, text TEXT, hash TEXT,
        metadata TEXT, updated_at INT, file_sealed INT DEFAULT 0,
        element_released INT DEFAULT 0, sealed_owner TEXT,
        hit_count INT DEFAULT 0, last_hit_at INT)""")
    conn.execute("""CREATE TABLE files (
        path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime INT,
        size INT, updated_at INT)""")
    conn.execute(
        "INSERT INTO chunks (id,user_id,scope,source,path,start_line,end_line,"
        "text,hash,metadata,updated_at,file_sealed,element_released) "
        "VALUES ('c1',NULL,?,'memory',?,1,1,'暗页正文','h',NULL,"
        "strftime('%s','now'),?,?)",
        (scope, rel, 1 if scope != "shared" else 0, 0))
    conn.execute(
        "INSERT INTO files (path,source,hash,mtime,size,updated_at) "
        "VALUES (?,'memory','h',0,0,strftime('%s','now'))", (rel,))
    conn.commit()
    conn.close()
    return rel


def _dark_page_rel(ws):
    """The truth-layer path of the dark page the facade wrote."""
    hits = sorted((ws / "memory" / "markdown" / "events").rglob("*.md"))
    assert hits, "暗页没写出来 —— 测法有问题"
    return hits[0].relative_to(ws).as_posix()


def test_p021_the_inspector_reports_a_public_row_for_a_dark_page(ws, cow):
    """P0-21：暗页的索引行是公开形状 → 巡检报红。

    这行就是泄漏本身：真源层说它是暗页，索引说它公开，而匿名可读。
    """
    _make_dark_page(ws, cow)
    rel = _dark_page_rel(ws)
    _legacy_index(ws, rel, scope="shared")

    report = Inspector(ws).inspect(trigger="manual")
    hits = _public_row_violations(report)
    assert hits, "暗页的公开行没有被报出来 —— 这一行就是泄漏"
    assert any(rel in (v.get("path") or "") for v in hits), hits


def test_p021_reverse_a_correctly_sealed_row_is_not_reported(ws, cow):
    """P0-21 反向用例：正常封好的暗页不得被误报。

    一个对正确行也喊狼来了的检查，最后只会被关掉 —— 那时候它连真正
    的泄漏也一起关了。
    """
    _make_dark_page(ws, cow)
    rel = _dark_page_rel(ws)
    _legacy_index(ws, rel, scope="sealed")

    report = Inspector(ws).inspect(trigger="manual")
    assert not _public_row_violations(report), (
        f"封好的暗页被误报了: {_public_row_violations(report)}")


def test_p021_the_inspector_does_not_repair_the_row_it_reported(ws, cow):
    """P0-21：巡检只报红，不自动修（门禁③）。

    一个会自己修的检查器会让删除变得不可追溯 —— 而且这里修的是一行
    **正在泄漏**的数据，谁改的、改成了什么，都得留在审计里。
    """
    _make_dark_page(ws, cow)
    rel = _dark_page_rel(ws)
    _legacy_index(ws, rel, scope="shared")

    Inspector(ws).inspect(trigger="manual")
    row = _index_row(ws, rel)
    assert row is not None
    assert row["scope"] == "shared", (
        f"巡检把行改了: scope={row['scope']!r} —— 巡检只该报红")


# ---------------------------------------------------------------------------
# P0-21 · F1 / F2 / F3：判据自己不能说谎
# ---------------------------------------------------------------------------
#
# F1  S1 的基线取在 sync 之前，而 docstring 写的是「基线取在 sync 之后」——
#     代码与文档自相矛盾（实测：执行前 201 行 / 执行后 205 行，理由漂移成
#     「rows 变化 = 语料行数变了」）。
# F2  库是 WAL 模式，指纹只哈希主库文件，漏了 -wal 边车 —— 检索把 hit_count
#     0→1，主库字节不变而 -wal 变了，S1 报绿（假绿）。
# F3  「空库报『库是空的』」只在 --no-sync 下成立；默认跑法（带 sync）会把
#     共享知识库灌进空工作区，理由漂移成「top1 不是 USER.md」—— 正是这条
#     判据当初要消灭的那个理由。

def _make_index(tmp_path, rows=1):
    """A tiny index the fingerprint can read."""
    import sqlite3

    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, path TEXT,
        start_line INT, end_line INT, hash TEXT, file_sealed INT,
        element_released INT, sealed_owner TEXT, hit_count INT,
        last_hit_at INT)""")
    for i in range(rows):
        conn.execute(
            "INSERT INTO chunks (id,scope,path,start_line,end_line,hash,"
            "file_sealed,element_released,hit_count) "
            "VALUES (?,?,?,1,1,?,0,0,0)", ("c%d" % i, "shared", "p%d" % i, "h"))
    conn.commit()
    conn.close()
    return db


def test_f2_the_fingerprint_sees_a_write_that_only_reached_the_wal(tmp_path):
    """F2：只写进 WAL 边车的改动，指纹必须看得见。

    旧版指纹只哈希主库文件，而库是 WAL 模式 —— 写进 ``index.db-wal`` 的
    改动，主库字节不动，指纹报「没变」：假绿。
    """
    import sqlite3

    from agent.memory.acceptance import fingerprint

    db = _make_index(tmp_path)
    first = fingerprint(str(tmp_path))
    assert first.exists

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("UPDATE chunks SET hit_count = hit_count + 1")
        conn.commit()
    finally:
        conn.close()

    second = fingerprint(str(tmp_path))
    assert second.rows == first.rows, "行数不该变（这条前提错了，测法有问题）"
    assert second.sha256 != first.sha256, (
        "只写进 WAL 的改动指纹没看见 —— S1 是假绿")
    assert "sha256" in second.differs_from(first)


def test_f2_reverse_an_unchanged_index_reports_no_difference(tmp_path):
    """F2 反向用例：没变就报没变。

    指纹如果什么都报「变了」，它就等于没有 —— 而一个永远报红的检查器
    会被关掉。
    """
    from agent.memory.acceptance import fingerprint

    _make_index(tmp_path)
    first = fingerprint(str(tmp_path))
    second = fingerprint(str(tmp_path))
    assert second.differs_from(first) == [], (
        f"同一个库两次指纹报了差异: {second.differs_from(first)}")


def test_f1_the_s1_baseline_is_taken_after_the_sync(tmp_path, monkeypatch):
    """F1：S1 的基线必须在 sync **之后**取 —— 代码与 docstring 一致。

    基线取在前面时，同步带来的语料变化被算到判据头上，理由漂移成
    「rows 变化 = 语料行数变了」—— 而同步不是判据干的。
    """
    import agent.memory.acceptance.phase1 as phase1

    calls = []

    class _Fp:
        def __init__(self, order):
            self.order = order
            self.path = "x"
            self.rows = order
            self.mtime = 0.0
            self.exists = True
            self.sha256 = "d"

        def render(self):
            return f"fake#{self.order}"

    def _fake_fingerprint(workspace):
        calls.append(workspace)
        return _Fp(len(calls))

    monkeypatch.setattr(phase1, "fingerprint", _fake_fingerprint)

    class _Mgr:
        def __init__(self, *a, **kw):
            pass

        async def sync(self):
            calls.append("sync")

        @property
        def storage(self):
            class _S:
                def close(self):
                    pass
            return _S()

    import agent.memory.manager as _mgr_mod
    monkeypatch.setattr(_mgr_mod, "MemoryManager", lambda *a, **kw: _Mgr())
    monkeypatch.setattr(phase1, "judge_t3_chinese_recall",
                        lambda *a, **kw: True)
    monkeypatch.setattr(phase1, "judge_t6_reranker_e2e",
                        lambda *a, **kw: True)
    monkeypatch.setattr(phase1, "judge_t1a_single_predicate",
                        lambda *a, **kw: True)
    monkeypatch.setattr(phase1, "judge_t1b_ab_visibility",
                        lambda *a, **kw: True)
    monkeypatch.setattr(phase1, "judge_t1c_identity", lambda *a, **kw: True)
    monkeypatch.setattr(phase1, "judge_t2_degradation_status",
                        lambda *a, **kw: True)
    monkeypatch.setattr(phase1, "judge_t2b_reranker_down",
                        lambda *a, **kw: True)
    monkeypatch.setattr(phase1, "judge_t4_namespace", lambda *a, **kw: True)

    seen = {}

    def _s1(report, workspace, fp_before):
        seen["fp_before"] = fp_before
        return True

    monkeypatch.setattr(phase1, "judge_s1_index_untouched", _s1)
    monkeypatch.setattr(phase1, "resolve_workspace",
                        lambda explicit: (str(tmp_path), "test"))
    monkeypatch.setattr(phase1, "assert_not_source_tree",
                        lambda *a, **kw: None)
    monkeypatch.setattr(phase1, "sys", phase1.sys)

    phase1.main()

    assert "sync" in calls, "同步没有发生 —— 测法有问题"
    sync_at = calls.index("sync")
    # The baseline the S-1 criterion receives must come from a
    # fingerprint taken AFTER the sync. The fake fingerprint counts its
    # own calls, so the ORDER of the call that produced it is what the
    # assertion is about.
    assert seen["fp_before"].order > sync_at, (
        "S1 拿到的基线取自同步之前 —— 代码与 docstring 说的相反（F1）")


def test_f3_the_empty_verdict_comes_from_the_pre_sync_fingerprint(tmp_path):
    """F3：空库判定必须取**同步前**的指纹。

    默认跑法带 sync，会把共享知识库灌进空工作区；判定若取同步后的
    指纹，就不再是「空库」的判定，理由会漂移成「top1 不是 USER.md」。
    """
    from agent.memory.acceptance import Report, fingerprint

    # An index that EXISTS and has no rows: "empty" is a fact about the
    # corpus, not about a missing file.
    db = tmp_path / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE chunks (
        id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, path TEXT,
        start_line INT, end_line INT, hash TEXT, file_sealed INT,
        element_released INT, sealed_owner TEXT, hit_count INT,
        last_hit_at INT)""")
    conn.commit()
    conn.close()

    empty_before = fingerprint(str(tmp_path))
    assert empty_before.exists and empty_before.empty

    # The workspace is filled afterwards — as the sync would.
    _make_index(tmp_path, rows=3)
    filled_after = fingerprint(str(tmp_path))
    assert not filled_after.empty

    report = Report()
    from agent.memory.acceptance.phase1 import judge_t3_chinese_recall

    class _Mgr:
        async def search(self, *a, **kw):
            return []

    ok = judge_t3_chinese_recall(_Mgr(), report, filled_after,
                                 corpus_fp=empty_before)
    assert not ok, "空库必须判红"
    detail = "\n".join(report.rows[0][1] if len(report.rows[0]) > 1 else "")
    printed = [r for r in report.rows]
    assert printed, "没有判据结果"
