#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-H · the MUTANT REPLAY for the new criteria.

Every test here has the same shape as the S3-E replay:

    1. build a disposable copy of the object (never the live index);
    2. break it in ONE specific way;
    3. run the criterion against the broken copy;
    4. assert it went RED (or NOTRUN — the third state, where the
       mutation removes a PRECONDITION rather than breaking a value);
    5. restore the object and assert it went GREEN again.

The restore step matters as much as the break step: a criterion that is
red for everything is useless, and "改坏必红" without "恢复必转绿" is
half a criterion.

The mutations replayed
----------------------
MU-15  the anti-downgrade UPSERT is broken: ``body_path`` is written
       UNCONDITIONALLY (``THEN excluded.body_path``), so a stale writer
       that does not carry the body columns erases the values the
       migration filled.  This is the defect MEASURED TWICE on this
       machine (2026-10-29 23:41 on ``agent-mvnewfyb`` and 2026-10-30
       03:28 on ``agent-mvnf7fjl``).

MU-16  the ``~N`` tail-blank count takes part in the join.  It must
       NOT: the measurement on the 16 live libraries is 2674/2674
       byte-equal with the fixed-blank join and 388/2674 with the
       ``~N``-participating join.

MU-17  ``body_origin`` is dispatched as ``'file'`` for EVERY row, so an
       ``unresolved`` row (or a blob row, or an orphan row) is judged by
       the wrong rule and never reported.

The independence rule
----------------------
The criteria's expected values are NOT taken from the object under test.
MU-15's expectation ("the decided values must survive") is checked by
running the SQL and looking at the row; MU-16's expectation (the fixed
blank join) is re-implemented in the criterion; MU-17's expectation (a
``file`` row must resolve to a truth file) is re-derived from the file
system.

So the mutants are injected into the OBJECT UNDER TEST, and the criteria
notice — which is the only way the replay proves anything.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.memory.acceptance import Report, STATE_FAIL, STATE_NOTRUN, STATE_PASS  # noqa: E402
from agent.memory.acceptance import live_libs  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _states(report: Report):
    return {number: state for number, _name, state in report.rows}


def _run(fn, *args):
    """Run one criterion; return (report, states).

    The report's PRINTED OUTPUT is captured and attached as
    ``report.printed`` — the report is part of the evidence: a criterion
    that says "FAIL" without naming WHICH library failed is not a
    usable defect report.
    """
    import contextlib
    import io

    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    return report, _states(report)


def _make_library(root: Path, name: str, *, anchor: str = "L1-3",
                  text: str = "body text", body_origin: str = "file",
                  chunker_version=4, truth_rel: str = "MEMORY.md"):
    """A live library with one pointed row, plus its truth file."""
    db_dir = root / "memory" / "long-term"
    db_dir.mkdir(parents=True, exist_ok=True)
    (root / truth_rel).write_text("# head\n\nbody\n", encoding="utf-8")
    db = db_dir / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, source TEXT,
                path TEXT, start_line INTEGER, end_line INTEGER, text TEXT,
                embedding BLOB, hash TEXT, metadata TEXT,
                created_at INTEGER, updated_at INTEGER,
                hit_count INTEGER DEFAULT 0, last_hit_at INTEGER,
                element_released INTEGER DEFAULT 0, file_sealed INTEGER DEFAULT 0,
                sealed_owner TEXT,
                body_path TEXT, body_anchor TEXT, body_hash TEXT,
                text_state TEXT DEFAULT 'projection',
                body_origin TEXT NOT NULL DEFAULT 'unresolved'
            )""")
        conn.execute("""
            CREATE TABLE files (
                path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL,
                size INTEGER, updated_at INTEGER,
                chunk_count INTEGER, chunker_version INTEGER
            )""")
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash, "
            "body_path, body_anchor, body_hash, text_state, body_origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("id-" + name, truth_rel, 1, 3, text,
             "sha256:" + "0" * 64, truth_rel, anchor,
             "sha256:" + "0" * 64, "mirror", body_origin))
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, updated_at, "
            "chunk_count, chunker_version) VALUES (?,?,?,?,?,?,?,?)",
            (truth_rel, "memory", "h", 0.0, 10, 0, 1, chunker_version))
        conn.commit()
    finally:
        conn.close()
    return db


def _workspace(tmp: Path, **kw) -> Path:
    ws = tmp / "ws"
    _make_library(ws, "ROOT", **kw)
    return ws


def _criterion_body(module, func_name: str) -> str:
    """The EXECUTABLE body of a criterion — docstrings removed.

    The independence checks scan the criterion's source for calls to the
    object under test.  A naive substring scan would hit the criterion's
    own DOCSTRING, which necessarily NAMES the functions it refuses to
    call ("it does NOT call storage.save_chunk").  Stripping the
    docstrings first is what makes the check about the CODE.
    """
    import ast

    src = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            # Drop the docstring, then unparse the rest.
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                body = body[1:]
            clone = ast.FunctionDef(name=node.name, args=node.args,
                                    body=body, decorator_list=[],
                                    returns=None,
                                    type_comment=None)
            ast.fix_missing_locations(clone)
            return ast.unparse(clone)
    raise AssertionError("找不到判据函数 %s" % func_name)


def _called_names(module, func_name: str):
    """The names a criterion ACTUALLY CALLS (AST, not substring).

    A substring scan cannot tell a CALL from a mention: the criterion's
    own output text says "does not call body_pointer.rebuild_from_anchor",
    and a naive scan flags that prose as a violation.  So the check
    walks the AST and collects the callees — the only thing that
    matters for the independence rule.

    Returns the set of dotted names (``storage.save_chunk``,
    ``rebuild_from_grammar``, …).
    """
    import ast

    src = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                body = body[1:]
            names = set()
            for sub in ast.walk(ast.Module(body=body, type_ignores=[])):
                if isinstance(sub, ast.Call):
                    names.add(ast.unparse(sub.func))
            return names
    raise AssertionError("找不到判据函数 %s" % func_name)


# ---------------------------------------------------------------------------
# MU-15 · the anti-downgrade UPSERT
# ---------------------------------------------------------------------------

def test_mu15_a_stale_writer_must_not_erase_the_decided_body_columns(tmp_path):
    """MU-15: the anti-downgrade UPSERT is the load-bearing rule.

    The mutation: ``body_path`` (and the other three) are written
    UNCONDITIONALLY — ``THEN excluded.body_path`` with no
    ``CASE WHEN excluded.body_path IS NOT NULL`` guard.  That is the
    exact SQL that wiped ``agent-mvnewfyb``'s four columns at 23:41 on
    2026-10-29 and ``agent-mvnf7fjl``'s at 03:28 on 2026-10-30.

    The criterion must notice — and it must notice WITHOUT asking the
    implementation what the right answer is.
    """
    from agent.memory.acceptance import phase2_s3h

    report, states = _run(
        phase2_s3h.judge_upsert_does_not_downgrade_the_body_columns,
        tmp_path)
    assert states["UPSERT(unit)"] == STATE_PASS, report.printed

    # --- the mutation: the unconditional overwrite ---
    import agent.memory.storage as storage
    original = storage.BODY_COLUMN_UPSERT_SQL
    mutated = original.replace(
        "body_path        = CASE WHEN excluded.body_path IS NOT NULL\n"
        "                                             THEN excluded.body_path\n"
        "                                             ELSE chunks.body_path END,",
        "body_path        = excluded.body_path,")
    assert mutated != original, "MU-15 的变异必须真的改了 SQL"
    storage.BODY_COLUMN_UPSERT_SQL = mutated
    try:
        report, states = _run(
            phase2_s3h.judge_upsert_does_not_downgrade_the_body_columns,
            tmp_path)
    finally:
        storage.BODY_COLUMN_UPSERT_SQL = original
    assert states["UPSERT(unit)"] == STATE_FAIL, (
        "旧写入者能擦掉已判定值时，UPSERT 判据必须报红 —— 这正是"
        "agent-mvnewfyb / agent-mvnf7fjl 被擦那两次的成因")
    assert "body_path" in report.printed, "要指名是哪一列被降级"

    # --- the restore turns it green again ---
    report, states = _run(
        phase2_s3h.judge_upsert_does_not_downgrade_the_body_columns,
        tmp_path)
    assert states["UPSERT(unit)"] == STATE_PASS, report.printed


def test_mu15_the_criterion_does_not_ask_the_implementation(tmp_path):
    """MU-15's independence check.

    A criterion that asks ``storage`` what the UPSERT SHOULD do is a
    tautology.  The expectation here is "the decided values survive" —
    a property of the SQL, verified by running the SQL against a
    throwaway row, not by calling a function that returns the answer.

    The scan walks the AST and collects the names the criterion ACTUALLY
    CALLS.  A substring scan would flag the criterion's own prose
    ("it does NOT call storage.save_chunk") as a violation, which is
    why this is a call-graph check and not a text search.
    """
    from agent.memory.acceptance import phase2_s3h

    called = _called_names(
        phase2_s3h, "judge_upsert_does_not_downgrade_the_body_columns")
    for forbidden in ("save_chunk", "save_chunks_batch",
                      "replace_file_chunks"):
        assert not any(forbidden in name for name in called), (
            "UPSERT 判据不许调用 storage.%s 算期望值 —— "
            "那是被测对象自己的答案（实际调用: %s）"
            % (forbidden, sorted(called)))
    # It DOES read the SQL fragment — that is the thing under test.
    assert "BODY_COLUMN_UPSERT_SQL" in _criterion_body(
        phase2_s3h, "judge_upsert_does_not_downgrade_the_body_columns")


# ---------------------------------------------------------------------------
# MU-16 · the '~N' gap count must not take part in the join
# ---------------------------------------------------------------------------

def test_mu16_the_gap_count_must_not_take_part_in_the_join(tmp_path):
    """MU-16: if ``~N`` joined the segments, the corpus would not match.

    The measurement that settled it (on the 16 live libraries, 2674
    pointed rows):

        fixed-blank-line join (``~N`` NOT in the join):  2674 / 2674
        ``~N``-participating join:                        388 / 2674

    So the implementation (which does NOT join with ``~N``) is right,
    and the architect's §4.3 wording is not.  The criterion must
    assert the MEASURED rule.

    The mutation here injects the gap-joined rebuild into the
    implementation's rebuilder and asks the criterion whether it
    notices.  The criterion's own reader is independent, so what it
    notices is the DATA: a gap-joined rebuild produces a different
    fragment, and the stored ``chunks.text`` no longer matches.

    **The fixture is derived, not hand-written.**  The truth file and
    the anchor are fixed, and the stored ``chunks.text`` is computed
    with the FIXED-BLANK rule — the rule under test.  Writing the text
    by hand would make the test depend on my getting the join right by
    accident; deriving it makes the fixture state the rule explicitly:
    "the stored text is the fixed-blank join, so a gap-joined rebuild
    CANNOT reproduce it".
    """
    from agent.memory.acceptance import phase2_s3h

    # A truth file with a blank line between two segments — exactly the
    # shape the '~N' count describes.
    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    truth_text = "# head\n\nbody line one\n\nbody line two\n"
    (ws / "MEMORY.md").write_text(truth_text, encoding="utf-8")

    # Two segments: line 1 ("# head") and lines 2-5 ("body line one",
    # blank, "body line two").  The second segment's '~1' says one
    # trailing blank line was stripped by the chunker's rstrip.
    anchor = "L1-5+S1-1~0,2-5~1"

    # The stored text is the FIXED-BLANK join — computed here, in the
    # test, from the truth file.  This is the rule the criterion
    # asserts.  The line list is ``splitlines(keepends=True)`` — the
    # one the rebuilder reads — so the fixture states the rule rather
    # than my guess at it.
    lines = truth_text.splitlines(keepends=True)
    seg1 = "".join(lines[0:1]).rstrip()
    seg2 = "".join(lines[1:5]).rstrip()
    chunk_text = "\n\n".join([seg1, seg2])
    # The two rules MUST differ on this fixture, or the mutant is
    # undetectable here.
    gap_joined = seg1 + "\n" * 1 + seg2
    assert chunk_text != gap_joined, (
        "夹具必须让两种 join 产生不同的结果，否则这个 mutant 在这里测不出来")

    db = ws / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE chunks (
                id TEXT PRIMARY KEY, path TEXT, start_line INTEGER,
                end_line INTEGER, text TEXT, hash TEXT,
                body_path TEXT, body_anchor TEXT, body_hash TEXT,
                text_state TEXT DEFAULT 'projection',
                body_origin TEXT NOT NULL DEFAULT 'unresolved')""")
        conn.execute("""
            CREATE TABLE files (
                path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime REAL,
                size INTEGER, updated_at INTEGER,
                chunk_count INTEGER, chunker_version INTEGER)""")
        conn.execute(
            "INSERT INTO chunks (id, path, start_line, end_line, text, hash, "
            "body_path, body_anchor, body_hash, text_state, body_origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("id-1", "MEMORY.md", 1, 5, chunk_text, "sha256:" + "0" * 64,
             "MEMORY.md", anchor, "sha256:" + "0" * 64, "mirror", "file"))
        conn.execute(
            "INSERT INTO files (path, source, hash, mtime, size, updated_at, "
            "chunk_count, chunker_version) VALUES (?,?,?,?,?,?,?,?)",
            ("MEMORY.md", "memory", "h", 0.0, 10, 0, 1, 4))
        conn.commit()
    finally:
        conn.close()

    report, states = _run(
        phase2_s3h.judge_inv2_anchor_rebuild_is_byte_equal, ws)
    assert states["INV-2(REAL)"] == STATE_PASS, report.printed

    # --- the mutation: the implementation joins with '~N' ---
    import agent.memory.body_pointer as bp
    original = bp.rebuild_from_anchor

    def gap_joined(path, anchor_text):
        parsed = bp.parse_anchor(anchor_text)
        if parsed is None:
            return None
        raw_lines = Path(path).read_text(encoding="utf-8").splitlines(
            keepends=True)
        if not raw_lines:
            return None
        segs = parsed.segments
        if not parsed.has_segments:
            seg = segs[0]
            return "".join(raw_lines[seg.start - 1:min(seg.end, len(raw_lines))])
        parts = []
        for i, seg in enumerate(segs):
            end = min(seg.end, len(raw_lines))
            parts.append("".join(raw_lines[seg.start - 1:end]).rstrip())
            if i < len(segs) - 1:
                parts.append("\n" * segs[i + 1].tailblanks)
        return "".join(parts)

    bp.rebuild_from_anchor = gap_joined
    try:
        report, states = _run(
            phase2_s3h.judge_inv2_anchor_rebuild_is_byte_equal, ws)
    finally:
        bp.rebuild_from_anchor = original
    assert states["INV-2(REAL)"] == STATE_FAIL, (
        "『~N』参与 join 时，判据必须报红 —— 实测只有 388/2674 能对上，"
        "参与 join 的口径在健康语料上就是错的")
    assert "INV-2(REAL)" in report.printed

    # --- the restore turns it green again ---
    report, states = _run(
        phase2_s3h.judge_inv2_anchor_rebuild_is_byte_equal, ws)
    assert states["INV-2(REAL)"] == STATE_PASS, report.printed


def test_mu16_the_criterion_does_not_call_the_rebuilder(tmp_path):
    """MU-16's independence check.

    The criterion's VERDICT must not come from the implementation's
    reader: if it did, the mutant above could not be caught, because
    the criterion would be asking the (mutated) implementation what the
    right answer is.

    So the property is precise: the criterion computes the verdict with
    its OWN reader (``rebuild_from_grammar``), and it calls the
    implementation's reader ONLY for the CROSS-CHECK (two independent
    readings must agree).  The cross-check is an ADDITIONAL assertion,
    not the source of the verdict — which is exactly why the mutant is
    caught even though the implementation's reader is the one that
    changed.
    """
    from agent.memory.acceptance import phase2_s3h

    called = _called_names(
        phase2_s3h, "judge_inv2_anchor_rebuild_is_byte_equal")
    # The criterion's own reader IS used.
    assert "rebuild_from_grammar" in called
    assert "parse_anchor_grammar" in called
    # The implementation's reader is used ONLY for the cross-check.
    assert any("rebuild_from_anchor" in name for name in called), (
        "交叉校验必须真的调用实现侧的读法 —— 否则『两份拼写』的漂移就没人看得见")
    # And the verdict is computed from the criterion's own reader:
    # the byte comparison is against ``rebuilt``, not against
    # ``theirs``.
    body = _criterion_body(
        phase2_s3h, "judge_inv2_anchor_rebuild_is_byte_equal")
    verdict_line = [ln for ln in body.splitlines()
                    if "lib_byte += 1" in ln or "byte_equal ==" in ln]
    assert verdict_line, "找不到逐字节判定的代码"
    for ln in verdict_line:
        assert "theirs" not in ln, (
            "逐字节判定必须用判据自己的读法（rebuilt），不能用实现侧的（theirs）"
            " —— 否则就是拿被测对象算期望值")


# ---------------------------------------------------------------------------
# MU-17 · body_origin must be dispatched, not assumed
# ---------------------------------------------------------------------------

def test_mu17_unresolved_rows_must_be_caught(tmp_path):
    """MU-17: ``body_origin`` must not be dispatched as ``'file'`` for all.

    The mutation: ``text_inv.check_inv1`` treats every row as
    ``'file'``, so an ``unresolved`` row is judged by the "the pointer
    must resolve" rule instead of the "nobody has decided" rule — and
    an ``unresolved`` row with no pointer is then reported as a
    generic pointer violation instead of the distinct
    ``inv1-origin-unresolved`` code.

    The damage is not the VERDICT (both are RED) — it is the REASON
    CODE.  "the pointer is missing" and "nobody has decided whether
    this row has a truth source" are two different repairs, and a
    report that conflates them sends the operator to fix the wrong
    thing.  The architect's §6.2 is explicit that the reason codes
    must be separate so they can be counted.

    So the mutation is caught by the INV-1 reason code, and the test
    asserts on the CODE, not on the verdict.
    """
    from agent.memory.acceptance import phase2_s3h

    ws = tmp_path / "ws"
    _make_library(ws, "ROOT", body_origin="unresolved", text="")
    # The unresolved row has no pointer — the shape a stale writer
    # leaves behind.
    db = ws / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
            "body_hash=NULL WHERE body_origin='unresolved'")
        conn.commit()
    finally:
        conn.close()

    # --- before the mutation: the M6 mouth sees the unresolved row ---
    report, states = _run(phase2_s3h.judge_m6_body_origin_mouth, ws)
    assert states["M6(REAL)"] == STATE_FAIL, (
        "unresolved != 0 时 M6 必须报红 —— 它是『没人判定过』的可见待办")
    assert "unresolved" in report.printed

    # --- the mutation: check_inv1 dispatches every row as 'file' ---
    from agent.memory import text_inv
    from agent.memory.acceptance import phase2_s3e

    original = text_inv.check_inv1

    def all_file(conn, root):
        # The mutated dispatch: 'unresolved' is never recognised, so the
        # row is judged by the pointer rule and gets the generic code.
        out = []
        for row in conn.execute(
                "SELECT id, body_path, body_anchor, body_hash, text_state, "
                "body_origin FROM chunks "
                "WHERE text_state IN ('projection','mirror')"):
            cid, bpath, banchor, bhash, _state, origin = tuple(row)
            if not bpath or not banchor:
                out.append({"reason": "gap-rule:inv1-pointer-missing",
                            "code": "inv1-pointer-missing",
                            "path": str(bpath or "?"),
                            "detail": "chunk %s: points nowhere" % cid,
                            "severity": "high", "chunk_id": cid,
                            "body_origin": "file"})
        return {"inv": "INV-1",
                "verdict": "RED" if out else "GREEN",
                "fingerprint": "chunks=%d origins={'file': %d}"
                               % (len(out), len(out)),
                "violations": out, "origin_counts": {"file": len(out)}}

    # The Q1 criterion prints the violations' CODES — that is where the
    # distinction is visible to a reader.
    text_inv.check_inv1 = all_file
    try:
        report, states = _run(phase2_s3e.judge_q1_inv1_every_library, ws)
    finally:
        text_inv.check_inv1 = original
    assert "inv1-origin-unresolved" not in report.printed, (
        "变异的分派把『没人判定』说成了『指针缺失』")
    assert "inv1-pointer-missing" in report.printed

    # --- the restore: the distinct code is back ---
    report, states = _run(phase2_s3e.judge_q1_inv1_every_library, ws)
    assert "inv1-origin-unresolved" in report.printed, (
        "还原后必须报出『没人判定过』这个独立 reason code")


def test_mu17_the_m6_mouth_counts_unresolved_and_orphan(tmp_path):
    """MU-17's second half: the M6 mouth has THREE parts.

    The architect's §4.5 ruling replaced the old mouth
    (``text_state NOT IN ('mirror') == 0``, unsatisfiable once a blob
    row exists) with:

        1. ``body_origin='file' AND text_state<>'mirror'`` == 0
        2. ``body_origin='unresolved'``                 == 0
        3. ``body_origin='orphan'``                     == 0

    A criterion that only checks part 1 would pass on a library with
    unresolved rows — the false green the architect called out as the
    most important defence.
    """
    from agent.memory.acceptance import phase2_s3h

    # (a) unresolved rows -> FAIL
    ws_a = tmp_path / "ws_a"
    _make_library(ws_a, "ROOT", body_origin="unresolved")
    db = ws_a / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE chunks SET body_origin='unresolved'")
        conn.commit()
    finally:
        conn.close()
    report, states = _run(phase2_s3h.judge_m6_body_origin_mouth, ws_a)
    assert states["M6(REAL)"] == STATE_FAIL, (
        "unresolved 行必须让 M6 报红 —— 这是架构师说的最关键的假红防线")
    assert "unresolved" in report.printed

    # (b) orphan rows -> FAIL
    ws_b = tmp_path / "ws_b"
    _make_library(ws_b, "ROOT", body_origin="orphan")
    conn = sqlite3.connect(str(ws_b / "memory" / "long-term" / "index.db"))
    try:
        conn.execute("UPDATE chunks SET body_origin='orphan'")
        conn.commit()
    finally:
        conn.close()
    report, states = _run(phase2_s3h.judge_m6_body_origin_mouth, ws_b)
    assert states["M6(REAL)"] == STATE_FAIL, (
        "orphan 行必须让 M6 报红 —— 真源已漂移的账不能没人管")
    assert "orphan" in report.printed

    # (c) blob rows -> PASS (a legal terminal state; the count is
    #     reported but not judged)
    ws_c = tmp_path / "ws_c"
    _make_library(ws_c, "ROOT", body_origin="blob")
    conn = sqlite3.connect(str(ws_c / "memory" / "long-term" / "index.db"))
    try:
        conn.execute(
            "UPDATE chunks SET body_origin='blob', body_path=NULL")
        conn.commit()
    finally:
        conn.close()
    report, states = _run(phase2_s3h.judge_m6_body_origin_mouth, ws_c)
    assert states["M6(REAL)"] == STATE_PASS, (
        "blob 是合法终态，不该判红 —— 旧的『text_state NOT IN (mirror)』"
        "口径正是因此永远不可满足")


# ---------------------------------------------------------------------------
# The C-A3 three states
# ---------------------------------------------------------------------------

def test_ca3_distinguishes_not_rebuilt_from_wrong_version(tmp_path):
    """C-A3.4's three states: the criterion must tell them apart.

    "Not rebuilt" is a MISSING PREREQUISITE (NOTRUN); "rebuilt with the
    wrong version" is a BROKEN PRODUCT (FAIL).  The same red means two
    different things, and the criterion has to say which.
    """
    from agent.memory.acceptance import phase2_s3h

    # (a) not rebuilt: the files rows carry no version at all
    ws_a = tmp_path / "ws_a"
    _make_library(ws_a, "ROOT", chunker_version=None)
    report, states = _run(
        phase2_s3h.judge_ca3_chunker_version_is_consistent, ws_a)
    assert states["C-A3.4(REAL)"] == STATE_NOTRUN, (
        "没重建的库是前置条件不成立（NOTRUN），不是 FAIL —— "
        "『没重建』和产品坏了是两回事")
    assert "未重建" in report.printed
    assert "NOTRUN" in report.printed

    # (b) rebuilt with the wrong version: MIXED -> FAIL
    ws_b = tmp_path / "ws_b"
    _make_library(ws_b, "ROOT", chunker_version=3)
    report, states = _run(
        phase2_s3h.judge_ca3_chunker_version_is_consistent, ws_b)
    assert states["C-A3.4(REAL)"] == STATE_FAIL, (
        "版本错的库必须报 FAIL —— 重建已经发生，产出的是别的版本的切法")
    assert "版本错" in report.printed

    # (c) rebuilt and consistent -> PASS
    ws_c = tmp_path / "ws_c"
    _make_library(ws_c, "ROOT", chunker_version=4)
    report, states = _run(
        phase2_s3h.judge_ca3_chunker_version_is_consistent, ws_c)
    assert states["C-A3.4(REAL)"] == STATE_PASS, report.printed


def test_ca3_reports_the_subject_of_every_library(tmp_path):
    """B-3: every criterion names its subject.

    A criterion that says "FAIL" without naming WHICH library is not a
    usable defect report — the reader has to guess which of the 16 to
    go and look at.
    """
    from agent.memory.acceptance import phase2_s3h

    ws = tmp_path / "ws"
    _make_library(ws, "ROOT", body_origin="unresolved")
    report, states = _run(phase2_s3h.judge_m6_body_origin_mouth, ws)
    assert "ROOT" in report.printed, "要指名是哪个库"
    assert str(ws / "memory" / "long-term" / "index.db") in report.printed, \
        "要给出被测对象的绝对路径"
    assert "行数=" in report.printed, "要给出行数"
    assert "unresolved=" in report.printed, "要给出具体计数"
    for v in ("file=", "blob=", "orphan=", "unresolved="):
        assert v in report.printed, "四值分布的每一项都要报出来: %s" % v


def test_the_gate_can_still_catch_a_whole_library_being_wrong(tmp_path):
    """The capability that caught ``agent-mvnewfyb`` must survive.

    A copy of a library whose rows have no pointer at all must make the
    gate red — and the report must NAME that library.  This is the
    ability that found the first round's biggest defect (a library
    migrated at 23:23 and wiped at 23:41 by a stale writer), so it
    gets a regression test of its own.

    The breakage is the SAME shape as that defect: the four body
    columns are emptied, which is what a writer running pre-column
    code does.  ``M4`` is the criterion that names the library.
    """
    from agent.memory.acceptance import phase2_s3e

    ws = tmp_path / "ws"
    _make_library(ws, "ROOT")
    agent_dir = ws / "agents" / "agent-victim"
    _make_library(agent_dir, "agent-victim")

    report, states = _run(
        phase2_s3e.judge_m4_every_library_was_backfilled, ws)
    assert states["M4(REAL)"] == STATE_PASS, report.printed

    # Now break the victim library: the four body columns emptied —
    # exactly what a stale writer's UPSERT does.
    victim = agent_dir / "memory" / "long-term" / "index.db"
    conn = sqlite3.connect(str(victim))
    try:
        conn.execute(
            "UPDATE chunks SET body_path=NULL, body_anchor=NULL, "
            "body_hash=NULL, body_origin='unresolved'")
        conn.commit()
    finally:
        conn.close()

    report, states = _run(
        phase2_s3e.judge_m4_every_library_was_backfilled, ws)
    assert states["M4(REAL)"] == STATE_FAIL, (
        "一个库整体没迁对必须报红 —— 这是本阶段最有价值的判据能力")
    assert "agent-victim" in report.printed, \
        "报告要指名是哪个库（不能只说『有一个库坏了』）"

    # --- and the restore turns it green again ---
    conn = sqlite3.connect(str(victim))
    try:
        conn.execute(
            "UPDATE chunks SET body_path='MEMORY.md', body_anchor='L1-3', "
            "body_hash='sha256:" + "0" * 64 + "', body_origin='file'")
        conn.commit()
    finally:
        conn.close()
    report, states = _run(
        phase2_s3e.judge_m4_every_library_was_backfilled, ws)
    assert states["M4(REAL)"] == STATE_PASS, report.printed
