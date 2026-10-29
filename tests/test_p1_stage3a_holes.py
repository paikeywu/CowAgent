# encoding: utf-8
"""P1-阶段三 S3-A：补洞的判据（进版本库，可复现）。

交办单要求的每条判据都配一条**反向用例** —— 修复前的代码跑同一判据必须
FAIL。一条抓不住坏事的判据是装饰。

本文件测的是**规则**（在临时工作区里自建索引），所以它和
``agent/memory/acceptance/phase2.py`` 里的判据互补：那一份是可运行的
验收报告（跑在真实工作区上），这一份是随代码走的单元判据（跑在 CI 上）。
"""

import asyncio
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent.memory import body_pointer, schema_drift
from agent.memory.chunker import TextChunker
from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager
from agent.memory.naming import canonical_text
from agent.memory.storage import MemoryStorage, SearchResult
from agent.memory.text_inv import check_inv1, read_fragment_at_anchor


def _ws(tmp_path):
    """A workspace with a memory/ tree and nothing else."""
    (tmp_path / "memory" / "shared").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _manager(tmp_path):
    cfg = MemoryConfig(workspace_root=str(tmp_path), sync_on_search=False)
    return MemoryManager(cfg, embedding_provider=None, reranker=None)


# ---------------------------------------------------------------------------
# C-A1 · the fingerprint covers every modelled table
# ---------------------------------------------------------------------------

def test_ca1_fingerprint_sees_a_files_table_fork(tmp_path):
    """C-A1.2/C-A1.3: a files-table fork changes the fingerprint.

    The reverse case: the OLD (chunks-only) fingerprint reports two indexes
    with IDENTICAL chunks columns as CONSISTENT — even when one of them is
    missing ``files.chunk_count``.  That is the false green H2 was about.
    """
    from agent.memory.acceptance.phase2 import make_index

    good = make_index(tmp_path / "good")
    forked = make_index(tmp_path / "forked", files_missing=("chunk_count",))

    # The chunks tables are identical.
    assert schema_drift.table_columns(_conn(good), "chunks") == \
        schema_drift.table_columns(_conn(forked), "chunks")

    # The OLD algorithm: chunks only -> consistent (the bug).
    old = hashlib.sha256(
        "|".join(sorted(schema_drift.table_columns(_conn(good), "chunks"))).encode()
    ).hexdigest()
    old_forked = hashlib.sha256(
        "|".join(sorted(schema_drift.table_columns(_conn(forked), "chunks"))).encode()
    ).hexdigest()
    assert old == old_forked, (
        "the reverse case does not hold: the old algorithm already saw the "
        "fork, so this criterion would not prove anything")

    # The NEW algorithm: every modelled table -> different.
    assert schema_drift.schema_fingerprint(good) != \
        schema_drift.schema_fingerprint(forked), (
        "the fingerprint did not see the files-table fork — H2 is not fixed")


def _conn(db_path):
    conn = sqlite3.connect(str(db_path))
    return conn


def test_ca1_fingerprint_reports_the_tables_it_covers():
    """C-A1.4: the fingerprint's COVERAGE is visible."""
    tables = schema_drift.schema_fingerprint_tables()
    assert "chunks" in tables
    assert "files" in tables, (
        "the fingerprint does not cover the files table — H2 is not fixed")
    described = schema_drift.describe_schema_fingerprint("abc")
    assert "tables=" in described
    assert "files" in described


def test_ca1_drift_detector_reports_the_files_table(tmp_path):
    """C-A1.1: the drift detector sees a files-table gap."""
    from agent.memory.acceptance.phase2 import make_index

    db = make_index(tmp_path / "ws", files_missing=("chunk_count",))
    missing = schema_drift.missing_columns_all(db)
    assert "chunk_count" in missing, (
        "the drift detector did not see the missing files column — H2 is "
        "not fixed")
    by_table = schema_drift.missing_columns_by_table(db)
    assert by_table["files"] == ["chunk_count"]


def test_ca1_repair_adds_the_files_columns(tmp_path):
    """The repair brings a files table up to the current shape."""
    from agent.memory.acceptance.phase2 import make_index

    db = make_index(tmp_path / "ws", files_missing=("chunk_count",))
    added = schema_drift.repair_columns(db)
    assert "chunk_count" in added
    assert not schema_drift.missing_file_columns(db)


# ---------------------------------------------------------------------------
# C-A2 · the write paths fill the four columns
# ---------------------------------------------------------------------------

def test_ca2_add_memory_fills_the_four_columns(tmp_path):
    """C-A2.1: add_memory writes the four columns.

    REVERSE CASE: before the fix, ``text_state`` was never written by the
    manager at all (the whole file had ZERO hits for it), so every row came
    back with four NULL columns.
    """
    ws = _ws(tmp_path)
    mgr = _manager(ws)
    try:
        asyncio.run(mgr.add_memory("四列判据的内容", path="memory/shared/x.md"))
        rows = mgr.storage.conn.execute(
            "SELECT body_path, body_anchor, body_hash, text_state "
            "FROM chunks WHERE path=?", ("memory/shared/x.md",)).fetchall()
    finally:
        mgr.storage.close()
    assert rows, "no chunk row was written"
    for bp, ba, bh, ts in rows:
        # add_memory stores a blob under a synthetic path — there is no
        # truth file to point at, so body_path is honestly None.  The other
        # three must be filled.
        assert ba, "body_anchor was not written"
        assert bh, "body_hash was not written"
        assert ts, "text_state was not written"


def test_ca2_sync_writes_a_resolvable_pointer(tmp_path):
    """C-A2.2/C-A2.5: the sync path writes a pointer that RESOLVES.

    REVERSE CASE: before the fix, ``body_path`` was NULL on every synced
    row, and the pointer the writer wrote for a knowledge file pointed at
    the agent's own workspace while the file lives in the SHARED root.
    """
    ws = _ws(tmp_path)
    (ws / "memory" / "shared" / "probe.md").write_text(
        "# t\n\n正文内容。\n", encoding="utf-8")
    mgr = _manager(ws)
    try:
        asyncio.run(mgr.sync())
        rows = mgr.storage.conn.execute(
            "SELECT body_path, body_anchor, body_hash, text_state "
            "FROM chunks WHERE path=?", ("memory/shared/probe.md",)).fetchall()
    finally:
        mgr.storage.close()
    assert rows, "the sync did not index the file"
    for bp, ba, bh, ts in rows:
        assert bp == "memory/shared/probe.md"
        assert ba and bh and ts
        assert "\\" not in bp
        assert not bp.startswith("/")
        assert ".." not in bp.split("/")
        # The hash is the hash of the FRAGMENT AT THE ANCHOR — the same
        # bytes INV-3 reads back.
        frag = read_fragment_at_anchor(ws / bp, ba)
        assert frag is not None, f"the anchor {ba!r} locates nothing"
        expected = "sha256:" + hashlib.sha256(
            canonical_text(frag).encode("utf-8")).hexdigest()
        assert bh == expected, (
            "body_hash does not match what the anchor reads back — the "
            "writer and the checker are computing different things")


def test_ca2_new_rows_do_not_add_inv1_violations(tmp_path):
    """C-A2.3: a freshly synced file adds NO INV-1 violation."""
    ws = _ws(tmp_path)
    (ws / "memory" / "shared" / "probe.md").write_text(
        "# t\n\n正文。\n", encoding="utf-8")
    mgr = _manager(ws)
    try:
        asyncio.run(mgr.sync())
        conn = mgr.storage.conn
        mine = {r[0] for r in conn.execute(
            "SELECT id FROM chunks WHERE path=?",
            ("memory/shared/probe.md",)).fetchall()}
        result = check_inv1(conn, ws)
    finally:
        mgr.storage.close()
    violations = [v for v in result["violations"] if v.get("chunk_id") in mine]
    assert not violations, (
        f"the freshly written rows are INV-1 violations: {violations[:3]}")


def test_ca2_body_path_is_refused_for_an_unsafe_path(tmp_path):
    """C-A2.5: an unsafe path is REFUSED, not sanitized.

    Sanitizing a bad path is how an injection turns into a valid-looking
    pointer at a different file.
    """
    ws = _ws(tmp_path)
    for bad in ("..\\escape.md", "/etc/passwd", "C:/Windows/x.md",
                "a/../../b.md"):
        assert body_pointer.posix_body_path(bad) is None, (
            f"the unsafe path {bad!r} was accepted")


def test_ca2_the_resolution_rule_is_shared(tmp_path):
    """The writer and the checker must resolve a file the SAME way.

    The knowledge tree lives in the SHARED root when the agent has no copy
    of its own; a reader that guesses the workspace root finds nothing and
    reports every knowledge row as "the body file does not exist".
    """
    ws = _ws(tmp_path)
    # A file in the workspace resolves.
    (ws / "memory" / "shared" / "a.md").write_text("x", encoding="utf-8")
    found = body_pointer.resolve_truth_file(ws, "memory/shared/a.md")
    assert found is not None and found.is_file()

    # A file that is not there does not.
    assert body_pointer.resolve_truth_file(ws, "memory/shared/nope.md") is None


# ---------------------------------------------------------------------------
# C-A3 · the files bookkeeping columns are FILLED
# ---------------------------------------------------------------------------

def test_ca3_chunk_count_is_the_real_count(tmp_path):
    """C-A3.1/C-A3.2: the count is the file's REAL chunk count."""
    ws = _ws(tmp_path)
    (ws / "memory" / "shared" / "a.md").write_text(
        "# a\n\n正文。\n", encoding="utf-8")
    mgr = _manager(ws)
    try:
        asyncio.run(mgr.sync())
        row = mgr.storage.conn.execute(
            "SELECT chunk_count, chunker_version FROM files WHERE path=?",
            ("memory/shared/a.md",)).fetchone()
        actual = mgr.storage.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE path=?",
            ("memory/shared/a.md",)).fetchone()[0]
    finally:
        mgr.storage.close()
    assert row is not None, "no files row was written"
    assert row[0] == actual, (
        f"chunk_count says {row[0]}, the index really holds {actual}")
    assert row[1] == TextChunker.CHUNKER_VERSION


def test_ca3_not_measured_is_distinguishable_from_really_zero(tmp_path):
    """C-A3.3: NULL means "not measured", 0 means "measured, and it is zero".

    REVERSE CASE: the columns used to be ``NOT NULL DEFAULT 0``, so every
    pre-existing row claimed "this file produced zero chunks" — a false
    fact that cannot be told apart from a file that really produced none.
    """
    db = tmp_path / "index.db"
    storage = MemoryStorage(db_path=db)
    try:
        storage.update_file_metadata("memory/shared/unmeasured.md", "memory",
                                     "h", 1, 1)
        storage.update_file_metadata("memory/shared/zero.md", "memory",
                                     "h", 1, 1, chunk_count=0,
                                     chunker_version=3)
        rows = dict(storage.conn.execute(
            "SELECT path, chunk_count FROM files").fetchall())
    finally:
        storage.close()
    assert rows["memory/shared/unmeasured.md"] is None, (
        "'not measured' must be NULL, not 0")
    assert rows["memory/shared/zero.md"] == 0, (
        "'measured and really zero' must be 0")


# ---------------------------------------------------------------------------
# C-A4 · the criterion file is in the repository
# ---------------------------------------------------------------------------

def test_ca4_phase2_criterion_is_tracked_by_git():
    """C-A4.1: the criterion is version-controlled.

    A criterion that is not in the repository cannot be re-run on another
    machine — which is the failure P0-18 was called to fix.
    """
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    target = repo / "agent" / "memory" / "acceptance" / "phase2.py"
    assert target.is_file(), "acceptance/phase2.py does not exist"
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch",
         "agent/memory/acceptance/phase2.py"],
        capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, (
        "acceptance/phase2.py is not tracked by git — the criterion cannot "
        "be reproduced on another machine")


# ---------------------------------------------------------------------------
# C-A5 · the deployed repository stays clean
# ---------------------------------------------------------------------------

def test_ca5_config_backups_are_ignored():
    """C-A5.1: ``config.json`` and its backups are ignored.

    The 0.114 host carried ``?? config.json.bak-20261028-120030``, which
    made the deploy script's "the working copy is clean" check red.
    """
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    for name in ("config.json", "config.json.bak-20261028-120030"):
        out = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", name],
            capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, (
            f"{name} is not ignored — the deploy script's clean-working-copy "
            "check will be red on the host")


def test_ca5_no_tracked_config_backup_is_silently_ignored():
    """The new pattern must not swallow a TRACKED file.

    A ``gitignore`` pattern that matches a tracked file is a lie waiting to
    happen: the file stays tracked, and the next checkout behaves as if the
    pattern were not there.
    """
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True,
        timeout=20)
    tracked = [line for line in out.stdout.splitlines()
               if line.startswith("config.json.bak-")]
    assert not tracked, (
        f"a tracked file matches the new ignore pattern: {tracked}")


# ---------------------------------------------------------------------------
# S3-B · D-A and D-C (the behavioural halves; the registration halves live
#          in test_p1_stage2f_g1g4.py)
# ---------------------------------------------------------------------------

def test_da_decay_reads_the_truth_layers_occurred_at():
    """D-A: the decay consults the truth layer, not only the path regex."""
    import datetime

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=100)
    stamp = old.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    path = "memory/markdown/events/2026-05-01/some-event--deadbeef.md"

    assert MemoryManager._compute_temporal_decay(path, occurred_at=stamp) < 1.0, (
        "an element whose front-matter declares a 100-day-old occurred_at "
        "does not decay — D-A is not fixed")
    # Without a stated date, the legacy rule does not match this path
    # either, so it is evergreen.
    assert MemoryManager._compute_temporal_decay(path) == 1.0


def test_da_knowledge_pages_stay_evergreen():
    """D-A: a knowledge page keeps its evergreen reading."""
    stamp = "2020-01-01T00:00:00+08:00"
    assert MemoryManager._compute_temporal_decay(
        "knowledge/analysis/x.md", occurred_at=stamp) == 1.0
    assert MemoryManager._compute_temporal_decay(
        "knowledge/concepts/y.md", occurred_at=stamp) == 1.0


def test_da_legacy_diary_rule_still_works():
    """D-A: the legacy daily-diary rule is preserved."""
    assert MemoryManager._compute_temporal_decay("memory/2025-01-01.md") < 1.0
    assert MemoryManager._compute_temporal_decay(
        "memory/users/u-1/2025-01-01.md") < 1.0
    assert MemoryManager._compute_temporal_decay("MEMORY.md") == 1.0


def test_dc_the_fused_score_survives_the_reranker():
    """D-C: the reranker FUSES with the retrieval score; it does not replace it."""
    from agent.memory.reranker import Reranker

    class _Fixed(Reranker):
        def rerank(self, query, documents):
            return [1.0 for _ in documents]

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.reranker = _Fixed()
    mgr.config = type("C", (), {"rerank_weight": 0.5})()

    result = SearchResult(path="memory/shared/a.md", start_line=1, end_line=1,
                          score=0.8, snippet="s", source="memory",
                          user_id=None, decay=1.0)
    out = mgr._rerank("q", [result])
    # Rank-normalized rerank for one candidate is 1.0, so the fused score
    # is (0.8 + 1.0) / 2 = 0.9 — NOT the pure reranker score.
    assert abs(out[0].score - 0.9) < 1e-9, (
        "the reranker replaced the fused score — D-C is not fixed")


def test_dc_the_decay_is_applied_exactly_once():
    """D-C: the decay is applied once, not twice."""
    import math

    from agent.memory.reranker import Reranker

    class _Fixed(Reranker):
        def rerank(self, query, documents):
            return [1.0 for _ in documents]

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.reranker = _Fixed()
    mgr.config = type("C", (), {"rerank_weight": 0.5})()

    decay = math.exp(-math.log(2) / 30.0 * 100)
    result = SearchResult(path="memory/2026-01-01.md", start_line=1, end_line=1,
                          score=0.8, snippet="s", source="memory",
                          user_id=None, decay=decay)
    got = mgr._rerank("q", [result])[0].score
    assert abs(got - 0.9 * decay) < 1e-9, (
        f"the decay was applied more than once (got {got}, "
        f"expected {0.9 * decay})")
    assert abs(got - 0.9 * decay * decay) > 1e-9, (
        "the score is the decay applied TWICE — D-C is not fixed")


def test_dc_the_degradation_behaviour_is_unchanged():
    """C-B2.4: the reranker's degradation semantics did not change."""
    from agent.memory.reranker import Reranker

    class _Broken(Reranker):
        def rerank(self, query, documents):
            raise RuntimeError("model unavailable")

    results = [SearchResult(path="memory/shared/a.md", start_line=1, end_line=1,
                            score=0.5, snippet="s", source="memory",
                            user_id=None)]

    mgr = MemoryManager.__new__(MemoryManager)
    mgr.reranker = None
    assert mgr._rerank("q", results) is results, (
        "no reranker must leave the results untouched")

    mgr.reranker = _Broken()
    assert mgr._rerank("q", results) is results, (
        "a failing reranker must keep the fused order")


def test_the_decay_factor_is_carried_not_multiplied(tmp_path):
    """The factor travels on the result; the score is undecayed.

    This is what makes "the decay is applied once" a property of the code
    rather than of the call order.
    """
    mgr = MemoryManager.__new__(MemoryManager)
    result = SearchResult(path="memory/2025-01-01.md", start_line=1, end_line=1,
                          score=0.5, snippet="s", source="memory",
                          user_id=None)
    merged = mgr._merge_results([], [result], 0.7, 0.3)
    assert len(merged) == 1
    assert merged[0].decay is not None and merged[0].decay < 1.0
    # The score itself is NOT decayed yet.
    assert merged[0].score > 0.5
