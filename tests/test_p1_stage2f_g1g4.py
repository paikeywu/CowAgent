"""P1-阶段二-F · G1~G4 门禁漏洞修复的反向用例。

  G-1: 生产巡检必须全量（300 行坏 1 行 → 必报红 + 定位到那一行）。
  G-2: .audit/ 失败标记必须有读者（重建失败 → 巡检报红）。
  G-3: 重建器与写入器必须产出同一套 chunk 身份（真实语料）。
  G-4: D-A / D-C 的真实状态登记（未修就如实登记，不许声称已修）。

真实语料：仓库自带的 ``knowledge/`` 页面（真实写的知识页，结构各异：
长文、表格、代码块、多级标题），不是自造的简单样本。
"""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KNOWLEDGE = Path(r"C:\Users\User\cow\knowledge")


# ---- G-1 · sampling blind spot -----------------------------------------

def _build_300_row_index(tmp_path, break_one=False):
    """A 300-row projection with ONE corrupted row."""
    from agent.memory import index_guard
    from agent.memory.storage import MemoryStorage

    ws = tmp_path / "ws"
    md = ws / "memory" / "markdown" / "events" / "2026-10-29"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    body = "line one\nline two\n"
    for i in range(300):
        (md / f"e{i:03d}--0000000{i%10}.md").write_text(body, encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    from agent.memory.rebuilder import rebuild_file
    for f in sorted(md.glob("*.md")):
        rebuild_file(st, ws, f)
    if break_one:
        # corrupt exactly ONE row's text (the truth file stays correct)
        target = st.conn.execute(
            "SELECT id FROM chunks ORDER BY id LIMIT 1").fetchone()[0]
        st.conn.execute("UPDATE chunks SET text='CORRUPTED' WHERE id=?", (target,))
        st.conn.commit()
        return ws, st, target
    return ws, st, None


def test_g1_one_bad_row_in_300_is_found(tmp_path):
    """G-1: 300 rows, 1 broken -> the check must be RED (no sampling)."""
    from agent.memory import text_inv
    ws, st, target = _build_300_row_index(tmp_path, break_one=True)
    try:
        res = text_inv.check_inv2_inv3(st.conn, ws)
        assert res["verdict"] == "RED", (
            f"G-1: a corrupted row must turn the check red, got "
            f"{res['verdict']!r} (fingerprint={res.get('fingerprint')!r})")
        assert res["violations"], "RED but no violations were reported"
        chunk_ids = {v.get("chunk_id") for v in res["violations"]}
        assert target in chunk_ids, (
            f"G-1: the violation must name the broken chunk id {target!r}; "
            f"got {chunk_ids!r}")
    finally:
        st.close()


def test_g1_default_is_full_not_sampled(tmp_path):
    """G-1: the DEFAULT must be exhaustive — omission must not degrade."""
    from agent.memory import text_inv
    import inspect
    sig = inspect.signature(text_inv.check_inv2_inv3)
    assert sig.parameters["full"].default is True, (
        "G-1: check_inv2_inv3 must default to full=True")
    sig2 = inspect.signature(text_inv.run_text_invariants)
    assert sig2.parameters["full"].default is True, (
        "G-1: run_text_invariants must default to full=True")


def test_g1_production_inspection_path_is_full(tmp_path):
    """G-1: the inspector's call site must pass full=True EXPLICITLY."""
    src = (REPO / "agent" / "memory" / "inspector.py").read_text(
        encoding="utf-8")
    assert "run_text_invariants(\n                conn, self.workspace_root, full=True)" \
        in src, (
            "G-1: the production inspection path must pass full=True "
            "explicitly")


def test_g1_the_production_inspection_path_finds_the_bad_row(tmp_path):
    """G-1: the PRODUCTION path (Inspector.inspect) must be exhaustive.

    This is the criterion the mutant replay needs: the first version of
    the G-1 check called ``check_inv2_inv3`` DIRECTLY, so it kept the
    default (full) and stayed green even when the production call site
    was mutated back to sampling — a blind spot in the criterion, not a
    property of the code. This one goes through the real entry point.
    """
    from agent.memory.inspector import Inspector
    ws, st, target = _build_300_row_index(tmp_path, break_one=True)
    try:
        insp = Inspector(workspace_root=ws)
        report = insp.inspect(trigger="manual", persist_snapshot=False)
        text_inv_report = report.get("text_inv") or {}
        check = (text_inv_report.get("checks") or {}).get("INV-2/3") or {}
        assert check.get("verdict") == "RED", (
            f"G-1: the production inspection must find the corrupted row; "
            f"verdict={check.get('verdict')!r} "
            f"fingerprint={check.get('fingerprint')!r}")
        chunk_ids = {v.get("chunk_id") for v in check.get("violations", [])}
        assert target in chunk_ids, (
            f"G-1: the violation must name the broken chunk id {target!r}; "
            f"got {chunk_ids!r}")
    finally:
        st.close()


# ---- G-2 · the marker must have a reader --------------------------------

def test_g2_rebuild_failure_makes_the_inspection_red(tmp_path):
    """G-2: a failed rebuild -> the inspection must report it."""
    from agent.memory.inspector import Inspector
    from agent.memory import index_guard
    from agent.memory.storage import MemoryStorage
    from agent.memory.rebuilder import rebuild_file, RebuildError

    ws = tmp_path / "ws"
    md = ws / "memory" / "markdown" / "events" / "2026-10-29"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    f = md / "note--aaaaaaaa.md"
    f.write_text("hello\n", encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuild_file(st, ws, f)

    # force a rebuild failure
    from agent.memory import rebuilder
    original = st.replace_file_chunks

    def boom(*a, **k):
        raise RuntimeError("forced rebuild failure")

    st.replace_file_chunks = boom
    try:
        with pytest.raises(RebuildError):
            rebuild_file(st, ws, f)
    finally:
        st.replace_file_chunks = original

    marker = ws / "memory" / ".audit" / "rebuild_failures.jsonl"
    assert marker.is_file(), (
        f"G-2: the failure marker must be written under memory/.audit/, "
        f"not {ws / '.audit'} — found nothing at {marker}")
    assert "forced rebuild failure" in marker.read_text(encoding="utf-8")

    # and the inspector must READ it
    insp = Inspector(workspace_root=ws)
    report = insp.inspect(trigger="manual", persist_snapshot=False)
    codes = [v.get("code") for v in report.get("violations", [])]
    assert "rebuild-failed" in codes, (
        f"G-2: the inspection must report the failed rebuild; "
        f"got codes={codes!r}")
    st.close()


def test_g2_every_audit_marker_has_a_reader_or_is_registered():
    """G-2: every .audit/ marker must have a reader, or be registered."""
    from agent.memory import rebuilder
    # rebuild_failures.jsonl -> read by the inspector (G-2)
    assert hasattr(rebuilder, "read_rebuild_failures")
    # release_mismatch.jsonl -> read by the inspector (G-2)
    assert hasattr(rebuilder, "read_release_mismatches")
    src = (REPO / "agent" / "memory" / "inspector.py").read_text(
        encoding="utf-8")
    assert "read_rebuild_failures" in src, (
        "G-2: the inspector must call the reader")
    assert "read_release_mismatches" in src, (
        "G-2: the inspector must read the release-mismatch marker too")


def test_g2_release_mismatch_makes_the_inspection_red(tmp_path):
    """G-2: a front-matter/tombstone disagreement -> the inspection reports it."""
    from agent.memory.inspector import Inspector
    from agent.memory.rebuilder import _audit_mismatch

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    _audit_mismatch(ws, "memory/markdown/events/x.md", ["frontmatter_only"])
    insp = Inspector(workspace_root=ws)
    report = insp.inspect(trigger="manual", persist_snapshot=False)
    codes = [v.get("code") for v in report.get("violations", [])]
    assert "release-mismatch" in codes, (
        f"G-2: the inspection must report the release mismatch; "
        f"got codes={codes!r}")


# ---- G-3 · one chunking decision ----------------------------------------

def _real_corpus_files(limit=3):
    """REAL corpus: the knowledge pages this team actually wrote."""
    files = sorted(p for p in KNOWLEDGE.rglob("*.md") if p.is_file())
    assert len(files) >= 3, (
        f"the real corpus must be available for the G-3 check; found "
        f"{len(files)} files under {KNOWLEDGE}")
    return files[:limit]


def test_g3_real_corpus_ids_are_identical_after_rebuild(tmp_path):
    """G-3: write -> rebuild must yield the SAME chunk ids (real corpus)."""
    from agent.memory import index_guard
    from agent.memory.chunker import chunk_for_file
    from agent.memory.rebuilder import rebuild_file, _chunk_id
    from agent.memory.storage import MemoryStorage

    files = _real_corpus_files()
    ws = tmp_path / "ws"
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)

    for src in files:
        raw = src.read_text(encoding="utf-8")
        rel = f"memory/markdown/events/2026-10-29/{src.name}"
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(raw, encoding="utf-8")

        # what the WRITER would produce (the single decision point)
        writer_ids = {
            _chunk_id(rel, c.start_line, c.end_line, c.part)
            for c in chunk_for_file(raw, rel)}

        rebuild_file(st, ws, dst)
        rows = st.conn.execute(
            "SELECT id FROM chunks WHERE path=?", (rel,)).fetchall()
        rebuild_ids = {r[0] for r in rows}

        assert writer_ids == rebuild_ids, (
            f"G-3: writer and rebuilder must agree on chunk identity for "
            f"{src.name}: writer={len(writer_ids)} rebuild={len(rebuild_ids)} "
            f"only_writer={sorted(writer_ids - rebuild_ids)[:3]} "
            f"only_rebuild={sorted(rebuild_ids - writer_ids)[:3]}")
    st.close()


def test_g3_hit_count_survives_the_rebuild(tmp_path):
    """G-3: hit_count / body_hash / body_anchor must survive a rebuild."""
    from agent.memory import index_guard
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    src = _real_corpus_files(1)[0]
    ws = tmp_path / "ws"
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    raw = src.read_text(encoding="utf-8")
    rel = "memory/markdown/events/2026-10-29/x.md"
    dst = ws / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(raw, encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    r = rebuild_file(st, ws, dst)
    st.conn.execute("UPDATE chunks SET hit_count=7 WHERE path=?", (rel,))
    st.conn.commit()
    before = st.conn.execute(
        "SELECT id, hit_count, body_anchor, body_hash FROM chunks "
        "WHERE path=? ORDER BY id", (rel,)).fetchall()

    rebuild_file(st, ws, dst)
    after = st.conn.execute(
        "SELECT id, hit_count, body_anchor, body_hash FROM chunks "
        "WHERE path=? ORDER BY id", (rel,)).fetchall()
    assert [r[0] for r in before] == [r[0] for r in after], (
        f"G-3: the chunk id set changed across a rebuild")
    assert [r[2] for r in before] == [r[2] for r in after], (
        "G-3: body_anchor changed across a rebuild")
    assert [r[3] for r in before] == [r[3] for r in after], (
        "G-3: body_hash changed across a rebuild")
    assert sum(r[1] for r in after) == sum(r[1] for r in before) == 14, (
        f"G-3: hit_count was lost across a rebuild: "
        f"before={[r[1] for r in before]} after={[r[1] for r in after]}")
    st.close()


def test_g3_the_decision_has_one_source():
    """G-3: the chunking decision must live in ONE function."""
    from agent.memory import chunker
    assert hasattr(chunker, "chunk_for_file")
    # the rebuilder must not decide for itself any more
    src = (REPO / "agent" / "memory" / "rebuilder.py").read_text(
        encoding="utf-8")
    assert "chunk_for_file(raw, rel)" in src, (
        "G-3: the rebuilder must call the shared decision function")
    assert "chunker.chunk_markdown(raw)" not in src, (
        "G-3: the rebuilder still decides for itself")


# ---- G-4 · the real D-A / D-C status ------------------------------------

def _g4_registration(source: str, marker: str) -> bool:
    """Is the UNFIXED-status registration for ``marker`` present?

    The check is deliberately LOCAL: the registration must live inside the
    docstring of the function that carries the defect. A global
    ``"D-A" in source`` test would pass on a mention ANYWHERE in the
    file — the mutant that deletes the registration line but leaves a
    passing mention of "D-A" elsewhere would slip through (which is
    exactly what the first version of this criterion did: it was
    caught by the mutant replay, not by reasoning).
    """
    for block in source.split("\n    "):
        if marker in block and "STILL UNFIXED" in block:
            return True
    return False


def test_g4_da_is_registered_as_unfixed():
    """G-4: D-A (decay from occurred_at) is NOT fixed — say so in code."""
    src = (REPO / "agent" / "memory" / "manager.py").read_text(
        encoding="utf-8")
    assert _g4_registration(src, "D-A"), (
        "G-4: D-A must be registered in _compute_temporal_decay's docstring "
        "as unfixed (the registration was deleted)")


def test_g4_dc_is_registered_as_unfixed():
    """G-4: D-C (reranker fusion / double decay) is NOT fixed."""
    src = (REPO / "agent" / "memory" / "manager.py").read_text(
        encoding="utf-8")
    assert _g4_registration(src, "D-C"), (
        "G-4: D-C must be registered in _rerank's docstring as unfixed "
        "(the registration was deleted)")


def test_g4_no_number_collision_left_in_the_tests():
    """G-4: no test may use D-A/D-C to mean something else."""
    for p in (REPO / "tests").glob("test_*.py"):
        t = p.read_text(encoding="utf-8", errors="replace")
        # A mention is legitimate only inside a collision notice: the block
        # that EXPLAINS the collision and points at the real status.
        for block in t.split("\n\n"):
            if "D-A" not in block and "D-C" not in block:
                continue
            legitimate = any(k in block for k in (
                "COLLISION", "collision", "G-4", "STILL UNFIXED",
                "registered", "NUMBER COLLISION"))
            if legitimate:
                continue
            for line in block.splitlines():
                if "D-A" in line or "D-C" in line:
                    raise AssertionError(
                        f"G-4: {p.name} still uses D-A/D-C for something "
                        f"else: {line.strip()[:100]}")
