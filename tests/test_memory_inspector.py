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

