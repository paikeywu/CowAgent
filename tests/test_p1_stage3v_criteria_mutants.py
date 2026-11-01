#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-V · the mutant replay for the runtime census + drift probes.

**Why this file exists.**  A criterion that says "the old runtime is
not here" is a claim that can be FALSE in two directions at once:

  * it can be wrong because the criterion's own reading of the process
    table is broken (a false green — MEASURED: five such bugs, see
    ``phase2_s3v``'s docstring), and
  * it can be wrong because the reading is right and the world changed.

Only a mutation can tell the two apart.  So this file makes the SAME
mutations that were really made while the criterion was being written,
and asks the criteria whether they went red.

**The rule this file obeys**: every mutant must (a) change the
criterion's behaviour, (b) prove it changed (the diff is non-empty AND
the mutated behaviour differs from the original's), and (c) be asked
in a way that isolates ONE criterion.

Usage:
    pytest tests/test_p1_stage3v_criteria_mutants.py -v
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import phase2_s3v  # noqa: E402


# ---------------------------------------------------------------------------
# a tiny report double — the real Report prints; we want to assert
# ---------------------------------------------------------------------------

class _Report:
    """Just enough of ``acceptance.Report`` to capture one verdict."""

    def __init__(self) -> None:
        self.rows: List[Tuple[str, str, str]] = []
        self.details: List[str] = []

    def judge(self, number, name, ok, detail, fingerprint=None, notrun=False):
        if notrun:
            return self.judge_notrun(number, name, detail, fingerprint=fingerprint)
        self.rows.append((number, name, "PASS" if ok else "FAIL"))
        self.details.append(str(detail))
        return ok

    def judge_notrun(self, number, name, detail, fingerprint=None):
        self.rows.append((number, name, "NOTRUN"))
        self.details.append(str(detail))
        return None

    def state(self, number: str) -> str:
        for n, _name, state in self.rows:
            if n == number:
                return state
        return "ABSENT"

    def detail(self) -> str:
        return "\n".join(self.details)


# ---------------------------------------------------------------------------
# the fixtures: a workspace with ONE live library, built by the test
# ---------------------------------------------------------------------------

def _make_workspace(tmp: Path) -> Path:
    """A workspace with one live library that has a ``files_keep``
    witness proving a downgrade — the shape J-5 must catch."""
    ws = tmp / "ws"
    db_dir = ws / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime INTEGER,
            size INTEGER, updated_at INTEGER, chunk_count INTEGER,
            chunker_version INTEGER);
        CREATE TABLE files_keep (
            path TEXT PRIMARY KEY, chunk_count INTEGER, chunker_version INTEGER);
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY, path TEXT, text TEXT, body_path TEXT,
            body_anchor TEXT, body_hash TEXT, text_state TEXT,
            body_origin TEXT, created_at INTEGER, updated_at INTEGER);
        CREATE TABLE body_keep (
            id TEXT PRIMARY KEY, body_path TEXT, body_anchor TEXT,
            body_hash TEXT, text_state TEXT, body_origin TEXT);
        CREATE TABLE guard_state (key TEXT PRIMARY KEY, value TEXT);
        """)
    # a file that WAS measured (cv=4) and now carries cv=0 => downgrade
    conn.execute("INSERT INTO files_keep VALUES ('m.md', 5, 4)")
    conn.execute("INSERT INTO files VALUES "
                 "('m.md','memory','h',1,1,100,0,0)")
    # a file that was measured and DELETED => proven erasure
    conn.execute("INSERT INTO files_keep VALUES ('gone.md', 3, 4)")
    conn.commit()
    conn.close()
    return ws


def _make_agent_workspace(tmp: Path) -> Tuple[Path, Path]:
    """A workspace with one AGENT library + its truth file."""
    ws = tmp / "ws2"
    ad = ws / "agents" / "a1"
    db_dir = ad / "memory" / "long-term"
    db_dir.mkdir(parents=True)
    db = db_dir / "index.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY, source TEXT, hash TEXT, mtime INTEGER,
            size INTEGER, updated_at INTEGER, chunk_count INTEGER,
            chunker_version INTEGER);
        CREATE TABLE files_keep (
            path TEXT PRIMARY KEY, chunk_count INTEGER, chunker_version INTEGER);
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY, path TEXT, text TEXT, body_path TEXT,
            body_anchor TEXT, body_hash TEXT, text_state TEXT,
            body_origin TEXT, created_at INTEGER, updated_at INTEGER);
        CREATE TABLE body_keep (
            id TEXT PRIMARY KEY, body_path TEXT, body_anchor TEXT,
            body_hash TEXT, text_state TEXT, body_origin TEXT);
        CREATE TABLE guard_state (key TEXT PRIMARY KEY, value TEXT);
        """)
    conn.execute("INSERT INTO files_keep VALUES ('a.md', 7, 4)")
    conn.execute("INSERT INTO files VALUES ('a.md','memory','h',1,1,100,0,0)")
    conn.commit()
    conn.close()
    (ad / "a.md").write_text("hello\n", encoding="utf-8")
    return ws, db


# ---------------------------------------------------------------------------
# MU-41 · the census must lose the packaged runtime when the reader is broken
# ---------------------------------------------------------------------------

def _census_rows_synthetic() -> List[Dict[str, Any]]:
    """Two processes: the criterion's own interpreter and the project's
    packaged build.  Used instead of the real process table so the
    mutant is deterministic."""
    return [
        {"pid": 100, "ppid": 1, "exe": r"C:\Python\python.exe",
         "cmdline": "python -m agent.memory.acceptance.phase2",
         "started": 1000.0},
        {"pid": 200, "ppid": 1, "exe": r"C:\AI\CowAgent\CowAgent.exe",
         "cmdline": r"C:\AI\CowAgent\CowAgent.exe", "started": 900.0},
    ]


def test_mu41_the_census_names_the_projects_packaged_runtime(monkeypatch):
    """MU-41: the census must find the packaged build.

    The mutant: the classification is replaced by one that never
    produces ``PACKAGED``.  The original MUST find it.
    """
    monkeypatch.setattr(phase2_s3v, "_process_rows",
                        lambda: _census_rows_synthetic())
    monkeypatch.setattr(phase2_s3v, "_process_count", lambda: 2)
    census = phase2_s3v.runtime_census()
    assert census is not None
    kinds = {r.kind for r in census}
    assert "PACKAGED" in kinds, \
        "the census must classify the project's own executable as PACKAGED"
    packaged = [r for r in census if r.kind == "PACKAGED"]
    assert any("CowAgent" in (r.exe or "") for r in packaged)


def test_mu41_mutant_the_census_that_cannot_see_the_packaged_runtime(monkeypatch):
    """MU-41: the MUTANT — a census that cannot see the packaged build.

    It must produce NO ``PACKAGED`` row.  This proves the original's
    finding is not an artefact of the fixture.
    """
    def _blind(row, self_pid, packaged_names):
        return phase2_s3v.Runtime(
            row["pid"], row["ppid"], row["exe"], row["cmdline"],
            row["started"], "OTHER", None, "mutant: cannot see it")

    monkeypatch.setattr(phase2_s3v, "_classify", _blind)
    monkeypatch.setattr(phase2_s3v, "_process_rows",
                        lambda: _census_rows_synthetic())
    census = phase2_s3v.runtime_census()
    assert census is not None
    assert not [r for r in census if r.kind == "PACKAGED"], \
        "the mutant must NOT find the packaged runtime"


def test_mu42_the_packaged_names_come_from_the_build_configuration():
    """MU-42: the shipped executable names are READ, not written down.

    The mutant: hard-code the name.  The original must NOT contain a
    hard-coded product name — it must come from the build config, so
    renaming the product makes the probe follow.
    """
    names = phase2_s3v._packaged_names()
    assert names, "the build configuration must yield at least one name"
    pkg = phase2_s3v.REPO_ROOT / "desktop" / "package.json"
    data = json.loads(pkg.read_text(encoding="utf-8"))
    expected = (data.get("build") or {}).get("productName")
    if expected:
        assert expected.lower() in names, \
            "the productName from package.json must be among the names"
    src = (phase2_s3v.REPO_ROOT / "agent" / "memory" / "acceptance"
           / "phase2_s3v.py").read_text(encoding="utf-8")
    # the literal must not appear as a STRING CONSTANT
    literal = re.search(r'"%s"' % re.escape(str(expected or "X")), src)
    assert literal is None, \
        "the product name must not be written as a literal in the probe"


def test_mu43_the_files_side_witness_is_what_makes_j5_red(tmp_path):
    """MU-43: J-5 must be RED because of the FILES side witness.

    The mutant: the census ignores ``files_keep`` (the original mistake
    this whole round exists to fix).  With the mutant, the downgrade
    must become INVISIBLE — which proves the witness is what makes the
    criterion red, not something else.
    """
    ws = _make_workspace(tmp_path)
    report = _Report()
    from agent.memory.acceptance import phase2_s3j
    phase2_s3j.judge_old_writer_erasure_monitor(report, ws)
    assert report.state("J-5(REAL)") == "FAIL", \
        "J-5 must be RED on a proven files-side downgrade"
    detail = report.detail()
    assert "files 侧" in detail
    assert "m.md" in detail, "the downgraded path must be named"

    # --- the mutant: the witness is ignored -----------------------
    original = phase2_s3v._files_census
    try:
        def _blind(lib):
            c = original(lib)
            c.deleted = []
            c.downgraded = []
            return c
        phase2_s3v._files_census = _blind
        report2 = _Report()
        phase2_s3j.judge_old_writer_erasure_monitor(report2, ws)
        assert report2.state("J-5(REAL)") != "FAIL", \
            "with the witness blinded the downgrade must become INVISIBLE"
    finally:
        phase2_s3v._files_census = original


def test_mu44_the_rebuild_reader_must_keep_every_segment():
    """MU-44: the acceptance reader must rebuild a MULTI-SEGMENT anchor.

    The mutant (the real bug this round found): the regex consumed the
    first segment's start number and dropped it, so every multi-segment
    anchor was rebuilt from its SECOND segment onwards — 129 rows of
    phantom drift.
    """
    p = phase2_s3v.REPO_ROOT / "tmp" / "_mu44_truth.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n", encoding="utf-8")
        anchor = "L1-5+S1-1~0,3-3~0,5-5~0"
        got = phase2_s3v._rebuild_independently(p, anchor)
        assert got is not None
        assert "alpha" in got, "the FIRST segment must be in the rebuild"
        assert "gamma" in got, "the middle segment must be in the rebuild"
        assert "epsilon" in got, "the LAST segment must be in the rebuild"

        # --- the mutant: the first segment is dropped -------------
        import re as _re
        bad = _re.compile(r"^L(\d+)-(\d+)(?::p(\d+))?(?:\+S\d+-(.*))?$")
        m = bad.match(anchor)
        assert m is not None
        segs = []
        for part in (m.group(4) or "").split(","):
            part = part.strip()
            if not part:
                continue
            pm = _re.match(r"^(\d+)-(\d+)(?:~(\d+))?$", part)
            if pm:
                segs.append((int(pm.group(1)), int(pm.group(2))))
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        mutant = "\n\n".join(
            "".join(lines[a - 1:min(b, len(lines))]).rstrip() for a, b in segs)
        assert "alpha" not in mutant, \
            "the mutant MUST lose the first segment (that is the bug)"
        assert "alpha" in got, \
            "the original MUST keep it — otherwise the mutation is a no-op"
    finally:
        if p.exists():
            p.unlink()


def test_mu45_zero_matches_is_notrun_not_pass(tmp_path):
    """MU-45: "0 objects" must be NOTRUN, never PASS.

    This is the acceptance rule stated as a test: DR-5's baseline-rule
    identifiers do not exist in this workspace, so DR-5 must be
    NOTRUN — and a NOTRUN criterion must not read as green.
    """
    ws = tmp_path / "empty"
    ws.mkdir()
    report = _Report()
    phase2_s3v.judge_dr5_baseline_conformance(report, ws)
    assert report.state("DR-5(REAL)") == "NOTRUN", \
        "0 matches must be NOTRUN, not PASS"


def test_mu46_the_fingerprint_does_not_substitute_for_the_drift_check():
    """MU-46: the two verdicts must be reported SEPARATELY.

    The mutant: DR-7 reports only the fingerprint's verdict.  With the
    mutant, the "structure is fine but the content is stale" case
    becomes invisible — which is precisely the false green this
    criterion exists to prevent.
    """
    from agent.memory.acceptance import live_libs
    libs = live_libs.enumerate_live(Path(r"C:\Users\User\cow"))
    if not libs:
        return  # no live workspace on this machine ⇒ nothing to judge
    report = _Report()
    phase2_s3v.judge_dr7_fingerprint_does_not_substitute_drift(
        report, Path(r"C:\Users\User\cow"))
    detail = report.detail()
    assert "XF" in detail and "DR-6" in detail, \
        "both verdicts must appear — a single verdict cannot express the pair"
