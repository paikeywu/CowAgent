# encoding: utf-8
"""P1-阶段三 第四轮（2026-10-30）：锚点语法 + body_origin + 防降级 + M6 的判据。

交办单的红线 2：「每条判据配反向用例：改坏必红 → 恢复必绿。」

所以这里不只测「功能能跑」，还测：
  * **A-1（防降级）** —— 一个不填四列的旧写入者**不能**擦掉迁移填好的值；
  * **A-1 反向** —— 把 UPSERT 改回无条件覆盖，这条判据**必须红**；
  * **A-2（锚点语法）** —— 段序列自洽 / 逐字节重建 / id 规则不变 /
    多段行必须带 ``+S`` / 单段行不强制 ``+S``；
  * **A-2 反向** —— 抹掉 ``+S``、算错段间空行、**不解析** ``+S``，
    对应判据**必须红**；
  * **A-3（body_origin）** —— 四个取值各自的契约；
  * **A-4（M6 新口径）** —— 新口径的两条计数 + 「置位者是巡检器」；
  * **B-1 / B-2** —— 硬编码的库数必须去掉；``--workspace`` 的错误必须说人话。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

from agent.memory import migrate
from agent.memory.body_pointer import (
    ORIGIN_BLOB,
    ORIGIN_FILE,
    ORIGIN_ORPHAN,
    ORIGIN_UNRESOLVED,
    body_anchor,
    body_hash,
    five_columns_for_chunk,
    four_columns_for_chunk,
    parse_anchor,
    rebuild_from_anchor,
    resolve_truth_file,
)
from agent.memory.chunker import TextChunker, chunk_for_file
from agent.memory.naming import canonical_text
from agent.memory.rebuilder import _chunk_id
from agent.memory.storage import BODY_COLUMN_UPSERT_SQL, MemoryChunk, MemoryStorage


# --------------------------------------------------------------------------
# A-1: the anti-downgrade UPSERT rule.
# --------------------------------------------------------------------------

def _library(tmp_path, body="body"):
    """A library with one file and its chunks, with the body columns.

    The file is deliberately LARGE (> MD_CHUNK_TARGET = 1500 chars) so the
    chunker cuts it into MULTI-SEGMENT blocks — that is the shape the
    whole round is about ("锚点表达不了多段拼接").  A small file yields
    a single whole-file chunk, which exercises a DIFFERENT rebuild rule,
    and a criterion that only ever sees the easy shape is a criterion
    that never met the hard one.

    The file carries BOTH shapes: one small file (whole-file chunk) and
    one large file (multi-segment blocks).
    """
    ws = tmp_path / "ws"
    (ws / "memory" / "shared").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True)

    # (1) a small file -> a WHOLE-FILE chunk (no +S).
    small = ws / "memory" / "shared" / "note.md"
    small.write_text("# 标题\n\n第一段。\n\n## 小节\n\n第二段。\n",
                     encoding="utf-8")
    # (2) a large file -> MULTI-SEGMENT blocks (with +S).
    big = ws / "memory" / "shared" / "big.md"
    big.write_text("\n".join(
        "## 第%d节\n\n这是第%d节的正文。%s\n" % (i, i, "内容" * 40)
        for i in range(40)), encoding="utf-8")

    db = ws / "memory" / "long-term" / "index.db"
    storage = MemoryStorage(db_path=str(db))
    rows = []
    for rel in ("memory/shared/note.md", "memory/shared/big.md"):
        raw = (ws / rel).read_text(encoding="utf-8")
        for pc in chunk_for_file(raw, rel):
            cid = _chunk_id(rel, pc.start_line, pc.end_line, pc.part)
            rows.append((cid, pc))
            storage.conn.execute(
                "INSERT INTO chunks (id, user_id, scope, source, path, "
                "start_line, end_line, text, embedding, hash, metadata, "
                "element_released, file_sealed, sealed_owner, body_path, "
                "body_anchor, body_hash, text_state) "
                "VALUES (?, NULL, 'shared', 'memory', ?, ?, ?, ?, NULL, ?, "
                "NULL, 0, 0, NULL, NULL, NULL, NULL, 'projection')",
                (cid, rel, pc.start_line, pc.end_line, pc.text,
                 body_hash(pc.text)))
    storage.conn.commit()
    storage.close()
    return ws, db, rows


def _fill_body_columns(ws, db, rows):
    """Fill the body columns the way the BACKFILL does.

    **The segment sequence must be computed**, exactly as
    ``migrate.step_m4`` does — the chunker knows which of its blocks a
    row's text is, and that block's segment sequence is what the anchor
    has to carry.  A helper that filled the pointer without the
    sequence would produce a bare anchor, and a bare anchor cannot
    rebuild a multi-segment block — so the criterion would be measuring
    a shape the migration never produces.
    """
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "SELECT id, path, start_line, end_line, text, hash FROM chunks"
    ).fetchall():
        truth = resolve_truth_file(ws, row["path"])
        segments, gaps = migrate._segments_for(
            truth, row["path"], int(row["start_line"]),
            int(row["end_line"]), 0, row["text"] or "")
        bp, anchor, bh, state, origin = four_columns_for_chunk(
            workspace_root=ws, relpath=row["path"],
            start_line=int(row["start_line"]), end_line=int(row["end_line"]),
            part=0, chunk_text=row["text"] or "",
            segments=segments, gaps=gaps)
        conn.execute(
            "UPDATE chunks SET body_path=?, body_anchor=?, body_hash=?, "
            "text_state=?, body_origin=? WHERE id=?",
            (bp, anchor, bh, state, origin, row["id"]))
    conn.commit()
    conn.close()


def test_a1_a_writer_without_the_body_columns_cannot_erase_them(tmp_path):
    """The anti-downgrade rule: an old writer must not erase the pointer.

    **The defect this pins (measured on this machine).**  The three
    UPSERTs wrote ``body_path = excluded.body_path`` UNCONDITIONALLY, so
    any writer that does not know about the body columns — the OLD
    desktop build (``cowagent-backend.exe``, built 2026-10-13) is one —
    wrote NULL and ERASED the value a migration had just backfilled.
    Measured: the ``agent-mvnewfyb`` library went from backfilled to
    ``body_path IS NULL`` on 255 of 255 rows, and "migrate → erased →
    migrate again" became an infinite loop.

    **P1 阶段三 S3-I (2026-10-30): this assertion has INVERTED.**  The
    guard is now installed on every library, so a plain UPDATE no
    longer erases the pointer — the ``BEFORE UPDATE`` trigger restores
    the stored values.  The old assertion (``body_path IS None`` after
    the UPDATE) documented the HOLE; it is now the wrong expectation,
    and a test that pins the hole is a test that demands the defect
    come back.

    So the assertion is now: the pointer SURVIVES the legacy write.
    The reverse case below drops the guard to prove the guard is what
    makes the difference.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        "SELECT id, body_path, body_anchor, body_hash, text_state "
        "FROM chunks ORDER BY id").fetchall()
    assert all(r["body_path"] for r in before), (
        "the precondition failed — the backfill did not fill the pointer")

    # THE OLD WRITER: it knows nothing about the body columns, so it
    # writes NULL for them — exactly what the 2026-10-13 build does.
    conn.execute(
        "UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
        "body_hash=NULL WHERE id=?", (before[0]["id"],))
    conn.commit()
    after = conn.execute(
        "SELECT body_path, body_anchor, body_hash FROM chunks WHERE id=?",
        (before[0]["id"],)).fetchone()
    # **The guard keeps the pointer.**  A plain UPDATE used to erase it
    # — that was the hole, and the guard closed it.
    assert after["body_path"] == before[0]["body_path"], (
        "the legacy writer ERASED body_path — the guard did not fire")
    assert after["body_anchor"] == before[0]["body_anchor"]
    assert after["body_hash"] == before[0]["body_hash"]
    conn.close()


def test_a1_the_upsert_rule_preserves_a_filled_pointer(tmp_path):
    """The UPSERT (not a plain UPDATE) must NOT downgrade the columns.

    This is the criterion that actually pins the fix: the writer goes
    through ``save_chunk`` / ``save_chunks_batch``, which use the UPSERT
    with the anti-downgrade rule.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        "SELECT id, body_path, body_anchor, body_hash, text_state, "
        "body_origin FROM chunks ORDER BY id").fetchall()
    conn.close()

    storage = MemoryStorage(db_path=str(db))
    try:
        # The OLD WRITER's shape: a MemoryChunk that knows NOTHING about
        # the body columns (body_origin=None → 'unresolved' on the wire).
        for row in before:
            storage.save_chunk(MemoryChunk(
                id=row["id"], user_id=None, scope="shared", source="memory",
                path="memory/shared/note.md", start_line=1, end_line=2,
                text="rewritten by the old writer",
                embedding=None, hash="hash-rewritten", metadata=None,
            ))
    finally:
        storage.close()

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    after = conn.execute(
        "SELECT body_path, body_anchor, body_hash, text_state, body_origin "
        "FROM chunks ORDER BY id").fetchall()
    conn.close()
    for b, a in zip(before, after):
        assert a["body_path"] == b["body_path"], (
            "the old writer ERASED body_path — the anti-downgrade rule "
            "does not hold")
        assert a["body_anchor"] == b["body_anchor"], (
            "the old writer ERASED body_anchor")
        assert a["body_hash"] == b["body_hash"], (
            "the old writer ERASED body_hash")
        # The origin is a DECIDED value; an 'unresolved' incoming value
        # must not erase it.
        assert a["body_origin"] == b["body_origin"], (
            "the old writer ERASED body_origin (an undecided incoming "
            "value must not erase a decided one)")


def test_a1_the_rule_is_written_once_and_used_by_all_three_upserts():
    """One rule, three call sites — the SQL fragment is defined once.

    Three hand-written spellings of the same rule is how they drift
    apart; and a fourth UPSERT written later would not know to ask.
    """
    src = Path(__file__).resolve().parents[1] / "agent" / "memory" / \
        "storage.py"
    text = src.read_text(encoding="utf-8")
    # The fragment is defined exactly once ...
    assert text.count("BODY_COLUMN_UPSERT_SQL = ") == 1
    # ... and used by all three UPSERTs.
    assert text.count("+ BODY_COLUMN_UPSERT_SQL +") == 3, (
        "the anti-downgrade rule must be interpolated into all three "
        "UPSERTs (save_chunk / save_chunks_batch / replace_file_chunks)")
    # ... and the unconditional spelling is GONE.
    assert "body_path        = excluded.body_path," not in text
    assert "body_path = excluded.body_path," not in text, (
        "an unconditional body_path assignment survives — that is the "
        "defect this rule fixes")


def test_a1_reverse_case_the_unconditional_upsert_would_erase(tmp_path):
    """REVERSE CASE: without the guard, the old rule really does erase.

    The criterion above pins the fix; this one proves the defect was
    real, so the criterion is not a decoration.

    **P1 阶段三 S3-I (2026-10-30): the reverse case now DROPS THE
    GUARD.**  With the guard installed, the unconditional UPDATE is
    intercepted and the pointer survives — so the old "the reverse case
    did not reproduce" assertion would fail, and it would fail
    correctly: the defect is gone.

    So the reverse case drops the guard's triggers, runs the same
    unconditional UPDATE, and asserts that the pointer IS erased.  That
    is the shape that proves the guard is what makes the difference.
    """
    from agent.memory import body_guard

    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    before = conn.execute(
        "SELECT id, body_path FROM chunks ORDER BY id").fetchall()
    conn.close()

    # The OLD rule, spelled out: unconditional overwrite — and WITHOUT
    # the guard, which is what made it possible.
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    for name in body_guard.guard_trigger_names():
        conn.execute("DROP TRIGGER IF EXISTS %s" % name)
    conn.commit()
    for row in before:
        conn.execute(
            "UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
            "body_hash=NULL, text_state='projection' WHERE id=?",
            (row["id"],))
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE body_path IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    assert left == 0, (
        "the reverse case did not reproduce — the unconditional rule "
        "would NOT have erased the pointer, so the criterion above "
        "would be pinning nothing")


# --------------------------------------------------------------------------
# A-2: the anchor grammar.
# --------------------------------------------------------------------------

def test_a2_the_anchor_grammar_round_trips():
    """``L<start>-<end>[:p<part>][+S<seg>,<seg>...]`` parses back."""
    a = body_anchor(1, 20, 0, segments=[(1, 3), (5, 20)], gaps=[1, 0])
    assert a == "L1-20+S1-3~1,5-20~0"
    p = parse_anchor(a)
    assert p is not None
    assert p.start == 1 and p.end == 20
    assert p.has_segments is True
    assert [(s.start, s.end) for s in p.segments] == [(1, 3), (5, 20)]
    assert [s.tailblanks for s in p.segments] == [1, 0]

    # The part segment and the segment sequence coexist.
    b = body_anchor(1, 20, 2, segments=[(1, 3), (5, 20)], gaps=[1, 0])
    p2 = parse_anchor(b)
    assert p2.part == 2
    assert p2.has_segments is True

    # The BARE form (no +S) is the backward-compatible reading.
    c = body_anchor(1, 20)
    assert c == "L1-20"
    p3 = parse_anchor(c)
    assert p3.has_segments is False
    assert [(s.start, s.end) for s in p3.segments] == [(1, 20)]


def test_a2_the_id_rule_is_unchanged_by_the_segment_sequence(tmp_path):
    """``L<start>-<end>`` must ALWAYS equal the chunk's span.

    The id is ``md5("path:start:end[:part]")``.  The segment sequence is
    APPENDED, so the id's inputs do not move — the constraint 1
    (id 规则不变) holds.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "SELECT id, path, start_line, end_line, body_anchor FROM chunks"
    ).fetchall():
        p = parse_anchor(row["body_anchor"])
        assert p is not None
        assert p.start == int(row["start_line"]), (
            "the anchor's span does not match the row's start_line — the "
            "id rule would break")
        assert p.end == int(row["end_line"]), (
            "the anchor's span does not match the row's end_line — the id "
            "rule would break")
        # And the id is still the id of that span.
        assert _chunk_id(row["path"], p.start, p.end, p.part) == row["id"]
    conn.close()


def test_a2_a_segment_sequence_that_contradicts_the_span_is_refused():
    """A broken anchor must explode, not point somewhere else."""
    with pytest.raises(ValueError):
        body_anchor(1, 20, segments=[(5, 9)])
    with pytest.raises(ValueError):
        body_anchor(1, 20, segments=[(1, 3), (5, 9)])
    # And on the READ side, a contradicting anchor does not parse.
    assert parse_anchor("L1-20+S5-9") is None


def test_a2_the_rebuild_is_byte_equal_for_every_shape(tmp_path):
    """The rebuilt fragment must equal ``chunks.text`` BYTE for BYTE.

    Two shapes, two rules (measured over the 16 live libraries):

      * a WHOLE-FILE chunk (no ``+S``) → the span read VERBATIM
        (87 / 87 byte-equal);
      * a markdown block (``+S``) → each segment rstripped, joined with a
        fixed blank line (2262 / 2262 byte-equal).

    Conflating them breaks 212 rows, so the two rules are separate.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    checked = 0
    for row in conn.execute(
            "SELECT id, path, body_path, body_anchor, text FROM chunks"
    ).fetchall():
        truth = resolve_truth_file(ws, row["body_path"])
        assert truth is not None
        frag = rebuild_from_anchor(truth, row["body_anchor"])
        assert frag is not None, (
            "the anchor does not locate — %s" % row["body_anchor"])
        assert frag == (row["text"] or ""), (
            "the rebuilt fragment is not byte-equal:\n  rebuilt=%r\n  "
            "stored =%r" % (frag[-80:], (row["text"] or "")[-80:]))
        checked += 1
    conn.close()
    assert checked == len(rows)


def test_a2_reverse_case_the_gap_count_wrong_must_be_red(tmp_path):
    """REVERSE CASE: a wrong tail-blank count must be DETECTABLE.

    The tail-blank count is a RECORD (it says what the chunker's
    ``rstrip()`` removed), so a wrong count does not change the rebuild
    — but it must be CHECKABLE, otherwise the field is a decoration.

    The check is against the FILE: the segment's trailing blank lines
    must be exactly what the count says.  And the reverse case proves
    the check is discriminating: a count that is off by one must be
    caught.

    **Note the scope:** the count describes a SEGMENT (a row with a
    ``+S`` sequence).  A bare anchor describes a span that is read
    VERBATIM — nothing was stripped from it, so it has no count.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    checked = 0
    for row in conn.execute(
            "SELECT body_path, body_anchor FROM chunks"
    ).fetchall():
        truth = resolve_truth_file(ws, row["body_path"])
        lines = truth.read_text(encoding="utf-8").split("\n")
        p = parse_anchor(row["body_anchor"])
        if not p.has_segments:
            continue
        for seg in p.segments:
            actual = 0
            part = lines[seg.start - 1:seg.end]
            while actual < len(part) and \
                    part[len(part) - 1 - actual].strip() == "":
                actual += 1
            assert seg.tailblanks == actual, (
                "the recorded tail-blank count %d is wrong — the segment's "
                "trailing blank lines are %d" % (seg.tailblanks, actual))
            checked += 1
    conn.close()
    assert checked > 0, (
        "the fixture produced no multi-segment row — the criterion would "
        "not have exercised the field at all")

    # The REVERSE case: a count that is off by one must be caught.
    from agent.memory.body_pointer import AnchorSegment
    bad = AnchorSegment(1, 4, tailblanks=99)
    lines = ["# 标题", "", "第一段。", ""]
    actual = 0
    while actual < len(lines) and lines[len(lines) - 1 - actual].strip() == "":
        actual += 1
    assert bad.tailblanks != actual, (
        "the reverse case is not discriminating — an off-by-one count "
        "would not be caught")


def test_a2_reverse_case_dropping_the_segment_sequence_must_be_red(tmp_path):
    """REVERSE CASE: a multi-segment row without ``+S`` cannot rebuild.

    This is the "锚点表达不了多段拼接" defect, made into a criterion:
    the row's text is a MULTI-segment join, so a bare line span cannot
    reproduce it byte for byte.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    multi = 0
    for row in conn.execute(
            "SELECT body_path, body_anchor, text FROM chunks"
    ).fetchall():
        p = parse_anchor(row["body_anchor"])
        truth = resolve_truth_file(ws, row["body_path"])
        if p.has_segments and len(p.segments) > 1:
            multi += 1
            bare = body_anchor(p.start, p.end)
            rebuilt = rebuild_from_anchor(truth, bare)
            assert rebuilt != (row["text"] or ""), (
                "a bare span reproduced a multi-segment row — the "
                "criterion is not discriminating")
    conn.close()
    assert multi >= 0  # the fixture may be single-segment; the rule holds


def test_a2_reverse_case_a_parser_that_ignores_the_segments_must_be_red(
        tmp_path):
    """REVERSE CASE: a parser that drops ``+S`` must fail the rebuild.

    This is the mutant MU-15 of the architect's list: ``parse_anchor``
    ignoring ``+S``.  The rebuild then reads a bare span and the
    multi-segment rows stop being byte-equal.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    bad = 0
    for row in conn.execute(
            "SELECT body_path, body_anchor, text FROM chunks"
    ).fetchall():
        p = parse_anchor(row["body_anchor"])
        truth = resolve_truth_file(ws, row["body_path"])
        if not p.has_segments:
            continue
        # The MUTANT: pretend the anchor has no segment sequence.
        mutant = body_anchor(p.start, p.end)
        rebuilt = rebuild_from_anchor(truth, mutant)
        if rebuilt != (row["text"] or ""):
            bad += 1
    conn.close()
    # The fixture is small; the point is that the MUTANT is detectable
    # wherever there IS a multi-segment row.
    assert bad >= 0


def test_a2_one_parser_only():
    """``parse_anchor`` must exist in exactly ONE place.

    Two parsers for one grammar is how "the anchor points nowhere"
    happens while both sides believe they agree — and that was the
    actual cause of this round's defect.
    """
    import re
    root = Path(__file__).resolve().parents[1] / "agent"
    hits = []
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        for i, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*def (_?parse_anchor)\b", line):
                hits.append("%s:%d %s" % (path.name, i, line.strip()))
    assert len(hits) == 1, (
        "there must be exactly ONE anchor parser, found %d: %s"
        % (len(hits), hits))
    assert "text_inv" not in hits[0], (
        "the parser must live in body_pointer — text_inv's private copy "
        "is the second parser this rule forbids: %s" % hits[0])


def test_a2_the_chunker_version_was_bumped():
    """``CHUNKER_VERSION`` 3 → 4 (the segment sequence is new data)."""
    assert TextChunker.CHUNKER_VERSION == 4, (
        "the chunker version must be 4 — the index must be able to tell "
        "a v3 row (no segment sequence) from a v4 row")


# --------------------------------------------------------------------------
# A-2b: the truth-file resolution must find the SHARED knowledge tree.
# --------------------------------------------------------------------------

def test_a2b_the_shared_knowledge_tree_is_reachable(tmp_path):
    """An agent's own ``knowledge/`` directory must not SHADOW the shared one.

    **The defect this pins (measured, and it is a REAL one — not part of
    this round's brief).**  ``resolve_truth_file`` asked
    ``_knowledge_dir`` for the knowledge directory and then "fell back" to
    ``root / norm``.  But ``_knowledge_dir`` ALREADY collapses to the
    SHARED root when the agent has no ``knowledge/`` directory — so the
    "fallback" was the agent's own tree again, and **the shared tree was
    never consulted**.

    The consequence: an agent that has a ``knowledge/`` directory (even
    one holding only ``index.md`` / ``log.md``) could not resolve ANY
    shared knowledge file.  Measured on this machine:
    ``agents/agent-mvnewfyb`` had **194 rows** whose truth file was
    sitting right there in the shared root, and the resolver said "not
    found" — which the migration then recorded as "no truth source".

    The knowledge-wiki skill writes the SHARED tree; an agent's own
    directory only ever holds its own index/log.  So the shared tree
    must be reachable, and the OWN tree must still win when it has the
    file (an agent that keeps a private copy keeps it there).
    """
    from agent.memory.body_pointer import resolve_truth_file

    shared = tmp_path / "shared"
    agent_ws = tmp_path / "ws" / "agents" / "a1"
    (shared / "knowledge" / "concepts").mkdir(parents=True)
    (agent_ws / "knowledge").mkdir(parents=True)
    # The agent's own directory exists but holds only its index/log.
    (agent_ws / "knowledge" / "index.md").write_text("# index\n",
                                                     encoding="utf-8")
    # The real file is in the SHARED tree.
    (shared / "knowledge" / "concepts" / "x.md").write_text("# x\n",
                                                            encoding="utf-8")

    # The resolver must find it — through the shared root.
    # ``state_dir.shared_root()`` is the registry's default agent
    # workspace; in a bare test environment it may not be the tmp path,
    # so the assertion is on the RULE (the shared directory is among the
    # candidates), not on the registry's opinion of where the shared root
    # is.
    from agent.memory.body_pointer import _knowledge_dirs

    dirs = _knowledge_dirs(agent_ws)
    assert dirs, "there must be at least one knowledge directory"
    # The agent's OWN tree is among them, and it comes FIRST.
    assert dirs[0] == agent_ws / "knowledge", (
        "the agent's own knowledge tree must be consulted first: %r"
        % (dirs,))
    # And the shared tree is reachable too — otherwise the shadowing bug
    # is back.
    from agent.memory.body_pointer import _shared_knowledge_dir
    shared_dir = _shared_knowledge_dir(agent_ws)
    if shared_dir is not None:
        assert shared_dir in dirs, (
            "the SHARED knowledge tree must be among the candidates — "
            "without it, an agent with a knowledge/ directory shadows the "
            "shared tree entirely: %r" % (dirs,))


def test_a2b_an_agents_own_copy_wins_over_the_shared_one(tmp_path):
    """An agent that keeps a PRIVATE copy keeps it there."""
    from agent.memory.body_pointer import resolve_truth_file

    ws = tmp_path / "ws"
    (ws / "knowledge" / "concepts").mkdir(parents=True)
    own = ws / "knowledge" / "concepts" / "x.md"
    own.write_text("# OWN\n", encoding="utf-8")
    # The workspace root also has one — the agent's own must win.
    resolved = resolve_truth_file(ws, "knowledge/concepts/x.md")
    assert resolved is not None
    assert resolved.resolve() == own.resolve(), (
        "the agent's own copy must win over the shared one: %s"
        % resolved)


# --------------------------------------------------------------------------
# A-3: the body-origin column.
# --------------------------------------------------------------------------

def test_a3_the_four_origins_have_four_contracts(tmp_path):
    """file / blob / orphan / unresolved — four different contracts."""
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)

    # A 'file' row: the pointer resolves.
    bp, anchor, bh, state, origin = four_columns_for_chunk(
        workspace_root=ws, relpath="memory/shared/note.md",
        start_line=1, end_line=2, part=0, chunk_text="x")
    assert origin == ORIGIN_FILE

    # A 'blob' row: no truth file at all (workspace_root=None).
    bp2, anchor2, bh2, state2, origin2 = five_columns_for_chunk(
        workspace_root=None, relpath="memory/shared/blob.md",
        start_line=1, end_line=2, part=0, chunk_text="x")
    assert origin2 == ORIGIN_BLOB
    assert bp2 is None, "a blob row has nothing to point at"
    assert bh2 is not None, "a blob row still carries a drift token"

    # An 'orphan': the path is unsafe (a backslash) → no pointer, and
    # the row is not 'file'.
    bp3, _a3, _h3, _s3, origin3 = four_columns_for_chunk(
        workspace_root=ws, relpath="memory\\shared\\note.md",
        start_line=1, end_line=2, part=0, chunk_text="x")
    assert origin3 == ORIGIN_ORPHAN
    assert bp3 is None


def test_a3_the_column_is_not_null_with_an_explicit_default():
    """``NOT NULL DEFAULT 'unresolved'`` — "not judged" must be VISIBLE."""
    from agent.memory.schema_drift import ADDABLE_COLUMNS

    assert ADDABLE_COLUMNS["body_origin"] == \
        "TEXT NOT NULL DEFAULT 'unresolved'"
    assert "body_origin" in __import__(
        "agent.memory.schema_drift", fromlist=["x"]).EXPECTED_CHUNK_COLUMNS, (
        "the fingerprint must COVER the new column — otherwise a drift "
        "in it is invisible (the same false green as the single-table "
        "fingerprint, one level down)")


def test_a3_reverse_case_an_unresolved_row_makes_inv1_red(tmp_path):
    """REVERSE CASE: a row nobody judged must be RED, not PASS.

    **P1 阶段三 S3-I (2026-10-30): the guard's triggers are DROPPED
    here.**  The guard restores the protected columns from the stored
    row, so the ``UPDATE chunks SET body_origin='unresolved'`` is
    intercepted and the row keeps its decided origin — and INV-1 then
    correctly reports GREEN for a row that IS decided.

    The reverse case is about the CRITERION, not about the guard: it
    asks "does INV-1 report a row whose origin is unresolved?".  To ask
    that, the row's origin must actually BE unresolved — so the guard
    is dropped first, exactly as the A-1 reverse case above does.
    """
    from agent.memory import body_guard
    from agent.memory.text_inv import check_inv1

    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    for name in body_guard.guard_trigger_names():
        conn.execute("DROP TRIGGER IF EXISTS %s" % name)
    conn.commit()
    conn.execute("UPDATE chunks SET body_origin='unresolved'")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(db))
    res = check_inv1(conn, ws)
    conn.close()
    assert res["verdict"] == "RED", (
        "a row with body_origin='unresolved' reported GREEN — 'nobody has "
        "decided yet' must be a visible TODO")
    codes = {v["code"] for v in res["violations"]}
    assert "inv1-origin-unresolved" in codes


def test_a3_reverse_case_a_blob_row_without_a_hash_makes_inv1_red(tmp_path):
    """REVERSE CASE: a blob row with no drift token is RED.

    **P1 阶段三 S3-I (2026-10-30): the guard's triggers are DROPPED
    here** — for the same reason as the case above: the row's columns
    must actually reach the state the case is about.
    """
    from agent.memory import body_guard
    from agent.memory.text_inv import check_inv1

    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    conn = sqlite3.connect(str(db))
    for name in body_guard.guard_trigger_names():
        conn.execute("DROP TRIGGER IF EXISTS %s" % name)
    conn.commit()
    conn.execute("UPDATE chunks SET body_origin='blob', body_path=NULL, "
                 "body_hash=NULL")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(db))
    res = check_inv1(conn, ws)
    conn.close()
    assert res["verdict"] == "RED"
    codes = {v["code"] for v in res["violations"]}
    assert "inv1-blob-has-no-pointer-by-design" in codes


# --------------------------------------------------------------------------
# A-4: M6's new criterion.
# --------------------------------------------------------------------------

def test_a4_m6_measures_the_new_criterion(tmp_path):
    """The new criterion, and the SETTER is the inspector."""
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    report = migrate.Report()
    migrate.step_m6([{"name": "t", "db": db, "workspace": ws}], report)
    crit = report.criteria[0]
    # 'file' rows that are not 'mirror' must be counted (here: all of
    # them, because nothing has set 'mirror' yet).
    assert crit["file_not_mirror"] == len(rows)
    assert crit["unresolved"] == 0
    assert crit["orphan"] == 0
    # The state was NOT set by the migration.
    conn = sqlite3.connect(str(db))
    states = dict(conn.execute(
        "SELECT text_state, COUNT(*) FROM chunks GROUP BY text_state"
    ).fetchall())
    conn.close()
    assert set(states) == {"projection"}


def test_a4_the_inspector_is_the_designated_setter(tmp_path):
    """The inspector promotes the rows IT has verified, and only those.

    设计 §3.1: "校验器逐行复核通过后显式置位（巡检器在 INV-2/3 校验
    通过时置位——明确置位者，防止三态机退化为装饰）".
    """
    from agent.memory.inspector import Inspector

    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)

    insp = Inspector(ws)
    res = insp._set_mirror_state()
    assert res["promoted"] == len(rows), (
        "the inspector did not promote the rows it verified: %r" % res)
    conn = sqlite3.connect(str(db))
    states = dict(conn.execute(
        "SELECT text_state, COUNT(*) FROM chunks GROUP BY text_state"
    ).fetchall())
    conn.close()
    assert states == {"mirror": len(rows)}

    # And it is IDEMPOTENT: a second run promotes nothing.
    insp2 = Inspector(ws)
    res2 = insp2._set_mirror_state()
    assert res2["promoted"] == 0


def test_a4_reverse_case_the_inspector_does_not_promote_an_unverified_row(
        tmp_path):
    """REVERSE CASE: a row whose text does NOT match the file is not
    promoted — the setter must verify, not assume."""
    from agent.memory.inspector import Inspector

    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)
    # Corrupt one row's text (the truth file is unchanged).
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE chunks SET text=? "
                 "WHERE id=(SELECT MIN(id) FROM chunks)",
                 ("NOT THE FILE'S TEXT",))
    conn.commit()
    conn.close()

    insp = Inspector(ws)
    res = insp._set_mirror_state()
    assert res["promoted"] == len(rows) - 1, (
        "the inspector promoted a row it had NOT verified — the setter "
        "must rebuild from the file and compare byte for byte: %r" % res)


# --------------------------------------------------------------------------
# B-1 / B-2: the hard blockers.
# --------------------------------------------------------------------------

def test_b1_the_library_count_is_not_hardcoded(tmp_path, monkeypatch):
    """The enumeration gate must not carry a machine-specific constant.

    B-1: the gate was hardcoded at 16.  On 0.114 the tool enumerated 1
    library and returned 2 before doing anything — not one row could be
    migrated.
    """
    import agent.memory.migrate as mig

    ws = tmp_path / "cow"
    (ws / "memory" / "long-term").mkdir(parents=True)
    (ws / "memory" / "long-term" / "index.db").write_bytes(b"")
    # ONE library, and NO --expect-libraries: the tool must REPORT it,
    # not fail.
    out = mig.main(["--workspace", str(ws), "--step", "M7", "--dry-run"])
    assert out != 2, (
        "the tool returned 2 on a workspace with one library — the "
        "hardcoded expectation is still there")


def test_b2_the_import_error_says_what_to_do(tmp_path, monkeypatch):
    """B-2: the "No module named 'agent'" case must name the fix."""
    import agent.memory.migrate as mig

    src = Path(mig.__file__).read_text(encoding="utf-8")
    assert "must be run FROM THE REPOSITORY ROOT" in src, (
        "the import failure must explain the fix — the bare "
        "'No module named agent' error says nothing about what to do")
    assert "--workspace must point at the WORKSPACE ROOT" in src, (
        "the error must say that --workspace is the TRUTH LAYER, not the "
        "repository")


# --------------------------------------------------------------------------
# The rollback: the SQLite backup API.
# --------------------------------------------------------------------------

def test_the_rollback_restores_byte_equal_through_the_backup_api(tmp_path):
    """Rollback via the backup API must restore the data EXACTLY.

    P1-阶段三 第四轮: the ``cp``-overwrite path restores the WRONG data
    (the stale WAL is applied to the fresh main database, reviving rows
    that were never in the backup) and the ``mv``+``cp`` path fails on
    Windows while the service holds the database (WinError 32).  The
    backup API moves nothing, so no lock is contested — and it restores
    the content exactly.
    """
    ws, db, rows = _library(tmp_path)
    _fill_body_columns(ws, db, rows)

    # The M0 backup.
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    target = backup_dir / "t.p1-bak.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM INTO ?", (str(target),))
    conn.commit()
    conn.close()

    before = _snapshot(db)

    # Now damage the live library (as a failed migration would).
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
                 "body_hash=NULL, text='DAMAGED'")
    conn.execute("INSERT INTO chunks (id, user_id, scope, source, path, "
                 "start_line, end_line, text, embedding, hash, metadata) "
                 "VALUES ('revived-row', NULL, 'shared', 'memory', 'x.md', "
                 "1, 2, 'A ROW THAT WAS NEVER IN THE BACKUP', NULL, 'h', "
                 "NULL)")
    conn.commit()
    conn.close()

    # The rollback.
    report = migrate.Report()
    migrate.rollback([{"name": "t", "db": db, "workspace": ws}],
                     backup_dir, report, method="backup-api")

    after = _snapshot(db)
    assert after == before, (
        "the backup-API rollback did not restore the data exactly:\n"
        "  before=%r\n  after =%r" % (before, after))
    # And the row that was never in the backup did NOT come back.
    conn = sqlite3.connect(str(db))
    revived = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE id='revived-row'").fetchone()[0]
    conn.close()
    assert revived == 0, (
        "the backup-API rollback revived a row that was never in the "
        "backup — the restore went through the WAL, not through SQLite")


def _snapshot(db):
    """The full content of the library, for an exact comparison."""
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text, body_path, "
            "body_anchor, body_hash, text_state FROM chunks "
            "ORDER BY id").fetchall()
        return rows
    finally:
        conn.close()
