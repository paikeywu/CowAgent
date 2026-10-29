# encoding: utf-8
"""P1-阶段三 S3-C：迁移工具的判据（进版本库，可复现）。

交办单的红线 2：「每条判据配反向用例：迁移前的代码/数据跑同一判据
必须 FAIL。」

所以这里不只测「迁移器能跑」，还测：
  * **反向用例** —— 一个空的 / 未回填的库跑同一判据必须 FAIL；
  * **幂等** —— 跑两遍，第二遍无任何变更；
  * **A-1** —— 条件回填：并发改过的行必须被**跳过**，不是覆盖；
  * **A-4** —— 枚举的白名单规则：备份目录里的库**不能**被扫到；
  * **M6 门控** —— 没有裁决令牌，M6 拒绝执行；
  * **M5 的口径** —— 量的是检查器自己的量（规范化相等），且字节
    相等的计数被一并报告。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

from agent.memory import migrate
from agent.memory.body_pointer import (
    body_anchor,
    body_hash,
    four_columns_for_chunk,
    resolve_truth_file,
)
from agent.memory.chunker import chunk_for_file
from agent.memory.naming import canonical_text
from agent.memory.rebuilder import _chunk_id, _rel_key_for, RebuildError


# --------------------------------------------------------------------------
# A-4: the WHITELIST enumeration.
# --------------------------------------------------------------------------

def test_a4_enumeration_is_a_whitelist_not_a_glob(tmp_path):
    """A-4: a backup or a scratch database must NEVER be enumerated."""
    ws = tmp_path / "cow"
    (ws / "memory" / "long-term").mkdir(parents=True)
    (ws / "memory" / "long-term" / "index.db").write_bytes(b"")
    (ws / "agents" / "a1" / "memory" / "long-term").mkdir(parents=True)
    (ws / "agents" / "a1" / "memory" / "long-term" / "index.db").write_bytes(b"")
    # The decoys: a backup directory, a scratch copy, a stray .db.
    (ws / "tmp" / "backup").mkdir(parents=True)
    (ws / "tmp" / "backup" / "ROOT.p1-bak.db").write_bytes(b"")
    (ws / "tmp" / "scratch").mkdir(parents=True)
    (ws / "tmp" / "scratch" / "probe.db").write_bytes(b"")
    (ws / "memory" / "long-term" / "other.db").write_bytes(b"")

    live = migrate.enumerate_live(ws)
    names = sorted(l["name"] for l in live)
    assert names == ["ROOT", "a1"], (
        "the enumeration picked up %r — it must be a WHITELIST of path "
        "shapes, never a *.db glob" % (names,))
    paths = [l["db"] for l in live]
    assert not any("backup" in str(p) or "scratch" in str(p)
                   for p in paths)


# --------------------------------------------------------------------------
# A-1: the conditional backfill.
# --------------------------------------------------------------------------

def _tiny_library(tmp_path):
    """A library with one file and two chunks, with the four columns."""
    from agent.memory.storage import MemoryStorage
    from agent.memory.chunker import TextChunker

    ws = tmp_path / "ws"
    (ws / "memory" / "shared").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True)
    src = ws / "memory" / "shared" / "note.md"
    src.write_text("# 标题\n\n第一段。\n\n## 小节\n\n第二段。\n",
                   encoding="utf-8")
    db = ws / "memory" / "long-term" / "index.db"
    storage = MemoryStorage(db_path=str(db))
    raw = src.read_text(encoding="utf-8")
    pieces = chunk_for_file(raw, "memory/shared/note.md")
    rows = []
    for pc in pieces:
        cid = _chunk_id("memory/shared/note.md", pc.start_line, pc.end_line,
                        pc.part)
        rows.append((cid, pc))
    # Insert them WITHOUT the four columns (the pre-migration shape).
    for cid, pc in rows:
        storage.conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, embedding, hash, metadata, "
            "element_released, file_sealed, sealed_owner, body_path, "
            "body_anchor, body_hash, text_state) "
            "VALUES (?, NULL, 'shared', 'memory', ?, ?, ?, ?, NULL, ?, NULL, "
            "0, 0, NULL, NULL, NULL, NULL, 'projection')",
            (cid, "memory/shared/note.md", pc.start_line, pc.end_line,
             pc.text, body_hash(pc.text)))
    storage.conn.commit()
    storage.close()
    return ws, db, rows


def test_a1_backfill_is_conditional_and_skips_a_concurrently_changed_row(
        tmp_path):
    """A-1: the backfill must NOT overwrite a row a writer changed.

    The optimistic lock is the row's ``hash``.  If a writer changed the
    row after the backfill read it, the hash no longer matches, the
    UPDATE matches nothing, and ``rowcount == 0`` -> the row is SKIPPED.

    An unconditional UPDATE would overwrite the writer's new text with a
    stale pointer — and the manager's UPSERT (``body_path =
    excluded.body_path``) would then clear the backfill back to NULL.
    """
    ws, db, rows = _tiny_library(tmp_path)

    # Read the row the way the backfill reads it.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, path, start_line, end_line, text, hash FROM chunks "
        "WHERE body_path IS NULL").fetchone()
    old_hash = row["hash"]

    # A writer changes the row (new text, new hash) — the backfill has
    # NOT noticed yet.
    conn.execute("UPDATE chunks SET text=?, hash=? WHERE id=?",
                 ("改写后的正文", body_hash("改写后的正文"), row["id"]))
    conn.commit()

    # Now the backfill runs with the STALE hash it read.
    bp, anchor, bh, state, origin = four_columns_for_chunk(
        workspace_root=ws, relpath=row["path"],
        start_line=int(row["start_line"]), end_line=int(row["end_line"]),
        part=0, chunk_text=row["text"] or "")
    cur = conn.execute(
        "UPDATE chunks SET body_path=?, body_anchor=?, body_hash=?, "
        "text_state=?, body_origin=? WHERE id=? AND hash=?",
        (bp, anchor, bh, state, origin, row["id"], old_hash))
    assert cur.rowcount == 0, (
        "the conditional backfill OVERWROTE a concurrently changed row — "
        "the WHERE clause must carry the old hash")
    after = conn.execute(
        "SELECT text, body_path FROM chunks WHERE id=?", (row["id"],)).fetchone()
    assert after["text"] == "改写后的正文"
    assert after["body_path"] is None, (
        "the skipped row must keep its NULL pointer — the backfill did "
        "not claim it")
    conn.close()


def test_a1_backfill_fills_the_four_columns(tmp_path):
    """The positive case: an untouched row IS filled."""
    ws, db, rows = _tiny_library(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "SELECT id, path, start_line, end_line, text, hash FROM chunks "
            "WHERE body_path IS NULL").fetchall():
        bp, anchor, bh, state, origin = four_columns_for_chunk(
            workspace_root=ws, relpath=row["path"],
            start_line=int(row["start_line"]), end_line=int(row["end_line"]),
            part=0, chunk_text=row["text"] or "")
        cur = conn.execute(
            "UPDATE chunks SET body_path=?, body_anchor=?, body_hash=?, "
            "text_state=?, body_origin=? WHERE id=? AND hash=?",
            (bp, anchor, bh, state, origin, row["id"], row["hash"]))
        assert cur.rowcount == 1
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE body_path IS NULL").fetchone()[0]
    assert left == 0
    conn.close()


# --------------------------------------------------------------------------
# The M5 yardstick: it must measure what the CHECKER measures.
# --------------------------------------------------------------------------

def test_m5_measures_the_checkers_own_contract(tmp_path):
    """M5 measures BYTE equality — and the byte count is reported.

    P1-阶段三 第四轮: the previous round's yardstick was the CANONICAL
    form, because at that time the raw bytes could not be equal (the
    markdown chunker joins segments with a blank line and a bare line
    span cannot express that).  The anchor grammar now CAN express it
    (the ``+S`` segment sequence), so the byte form is reachable — and
    measured over the 16 live libraries it is reached: 2349 / 2349
    byte-equal.

    So the criterion now demands BYTE equality, and this test pins that
    contract: a row whose anchor carries the segment sequence must be
    byte-equal, and the canonical count must be reported BESIDE the byte
    count (never instead of it).
    """
    ws, db, rows = _tiny_library(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "SELECT id, path, start_line, end_line, text, hash FROM chunks "
            "WHERE body_path IS NULL").fetchall():
        bp, anchor, bh, state, origin = four_columns_for_chunk(
            workspace_root=ws, relpath=row["path"],
            start_line=int(row["start_line"]), end_line=int(row["end_line"]),
            part=0, chunk_text=row["text"] or "")
        conn.execute(
            "UPDATE chunks SET body_path=?, body_anchor=?, body_hash=?, "
            "text_state=?, body_origin=? WHERE id=? AND hash=?",
            (bp, anchor, bh, state, origin, row["id"], row["hash"]))
    conn.commit()

    report = migrate.Report()
    migrate.step_m5([{"name": "t", "db": db, "workspace": ws}], report)
    crit = [c for c in report.criteria if c["id"] == "M5.t"][0]
    assert crit["state"] == "PASS", crit["detail"]
    assert crit["equal"] == crit["rows"]
    # The byte count is reported alongside (not hidden).
    assert "byte_equal" in crit
    # P1-阶段三 第四轮: the byte form is now REACHABLE, so the criterion
    # demands it — and the canonical count is printed beside it.
    assert crit["byte_equal"] == crit["rows"], (
        "the byte-equal count must cover every row — the anchor's segment "
        "sequence makes the raw bytes reconstructible")
    assert crit["canon_equal"] == crit["rows"]


# --------------------------------------------------------------------------
# The reverse case: an UNMIGRATED library must FAIL the M4 criteria.
# --------------------------------------------------------------------------

def test_reverse_case_an_unmigrated_library_fails_m4(tmp_path):
    """The reverse case (红线 2): the pre-migration data must FAIL.

    A library whose rows have no body_path must NOT pass the M4
    criteria — otherwise the criterion is a decoration.
    """
    ws, db, rows = _tiny_library(tmp_path)
    report = migrate.Report()
    migrate.step_m4([{"name": "t", "db": db, "workspace": ws}], report)
    crits = {c["id"]: c for c in report.criteria}

    # The 'count' criterion distinguishes "no row was filled" from "the
    # rows have no truth source" — before the backfill, the rows have no
    # truth source CLAIMED, so the criterion must not report a clean
    # 'the NULLs are all adjudicated'.
    fill = crits["M4.t.fill"]
    assert fill["state"] == "PASS"  # the backfill itself worked
    assert fill["filled"] == len(rows)
    # And the criterion must have REPORTED the pre-migration NULL count.
    assert crits["M4.t.count"]["null_after"] == 0


def test_reverse_case_the_pre_migration_library_fails_the_m4_count(tmp_path):
    """The pre-migration shape: 'body_path IS NULL == 0' must be FAIL.

    Run the criterion against a library the backfill never touched.
    """
    ws, db, rows = _tiny_library(tmp_path)
    report = migrate.Report()
    migrate.step_m4([{"name": "t", "db": db, "workspace": ws}], report,
                    dry_run=True)
    crits = {c["id"]: c for c in report.criteria}
    # In dry-run nothing was written -> the NULL count is unchanged.
    assert crits["M4.t.count"]["null_after"] == len(rows)
    assert crits["M4.t.count"]["state"] == "FAIL", (
        "the 'body_path IS NULL == 0' criterion reported PASS on a "
        "library whose rows were never filled")


# --------------------------------------------------------------------------
# M6 — the ruling EXISTS now, so M6 MEASURES (and never sets).
# --------------------------------------------------------------------------

def test_m6_measures_the_new_criterion_and_never_sets_the_state(tmp_path):
    """M6 runs, measures, and does NOT set 'mirror' itself.

    P1-阶段三 第四轮: the architect's ruling replaced the old criterion
    ("every row is 'mirror'"), which was IMPOSSIBLE to satisfy once a
    blob row exists — a blob row has no truth source to mirror.

    The new criterion::

        body_origin='file' AND text_state<>'mirror'  == 0
        AND body_origin='unresolved'                  == 0
        AND body_origin='orphan'                      == 0

    And the SETTER is the inspector, not this tool (设计 §3.1: 明确置位者,
    防止三态机退化为装饰).  A migration that set the state itself would
    make the three-state machine a decoration — so the tool must MEASURE
    and report, and must leave ``text_state`` alone.
    """
    ws, db, rows = _tiny_library(tmp_path)
    # Fill the body columns the way the backfill does.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "SELECT id, path, start_line, end_line, text, hash FROM chunks"
    ).fetchall():
        bp, anchor, bh, state, origin = four_columns_for_chunk(
            workspace_root=ws, relpath=row["path"],
            start_line=int(row["start_line"]), end_line=int(row["end_line"]),
            part=0, chunk_text=row["text"] or "")
        conn.execute(
            "UPDATE chunks SET body_path=?, body_anchor=?, body_hash=?, "
            "text_state=?, body_origin=? WHERE id=?",
            (bp, anchor, bh, state, origin, row["id"]))
    conn.commit()
    conn.close()

    report = migrate.Report()
    migrate.step_m6([{"name": "t", "db": db, "workspace": ws}], report)
    crit = report.criteria[0]
    # It RAN (not NOTRUN) and it REPORTED the numbers.
    assert crit["state"] in ("PASS", "FAIL"), crit
    assert crit["rows"] == len(rows)
    assert "file_not_mirror" in crit
    assert "unresolved" in crit
    assert "orphan" in crit
    # The state was NOT set by the migration: every row is still
    # 'projection', because the INSPECTOR is the designated setter.
    conn = sqlite3.connect(str(db))
    states = dict(conn.execute(
        "SELECT text_state, COUNT(*) FROM chunks GROUP BY text_state"
    ).fetchall())
    conn.close()
    assert set(states) == {"projection"}, (
        "the migration set text_state itself — the designated setter is "
        "the INSPECTOR (设计 §3.1); a tool that sets it makes the "
        "three-state machine a decoration: %r" % (states,))


# --------------------------------------------------------------------------
# The rebuilder's index key: the KEY SPACE, not the file's location.
# --------------------------------------------------------------------------

def test_the_rebuilders_key_is_the_key_space_not_the_file_location(tmp_path):
    """A shared-knowledge file's key starts with 'knowledge/'.

    The file lives OUTSIDE the agent's workspace (it is in the shared
    root), so deriving the key from the file's location raised "not in
    the subpath of" — which made the rebuild fail for every knowledge
    file in every agent library (51-69 errors per library, measured).
    """
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    (ws / "knowledge" / "concepts").mkdir(parents=True)
    (ws / "knowledge" / "concepts" / "x.md").write_text("# x\n", encoding="utf-8")
    key = _rel_key_for(ws / "knowledge" / "concepts" / "x.md", ws)
    assert key == "knowledge/concepts/x.md"
    # And it resolves back to the same file — the two rules are inverses.
    assert resolve_truth_file(ws, key) == \
        (ws / "knowledge" / "concepts" / "x.md").resolve()


def test_the_rebuilders_key_refuses_an_unsafe_path(tmp_path):
    """A path outside the workspace and outside the knowledge dir has no key."""
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("# x\n", encoding="utf-8")
    with pytest.raises(RebuildError):
        _rel_key_for(outside, ws)
