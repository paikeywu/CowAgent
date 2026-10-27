"""Acceptance criteria for the live-memory system — the PHASE-ONE probe.

Why this module exists in the repository
----------------------------------------

The probe used to live in ``tmp/``, which is git-ignored. A criterion that
has no history cannot be audited: "the last run was 8/8" says nothing
about WHICH index was measured, or even whether the same code was running.
That is exactly the failure P0-18 was called to fix — the probe resolved
its workspace through the registry, the registry's default agent inherits
``agent_workspace``, and ``agent_workspace`` is overridden by an
environment variable. So one probe, three contexts, three different
indexes, three different answers to the same question ("who is Wang
Jingjing?"), and a green 8/8 that meant nothing about which corpus it
had read.

So the criteria live in the repository, they PRINT THE FINGERPRINT OF THE
INDEX THEY MEASURED, and a run without a fingerprint is not a pass.

What changed in P0-18
--------------------

1. **Every criterion prints the index fingerprint** (path + chunks rows +
   mtime). A PASS without a fingerprint is not a PASS.
2. **T3/T6 assert the corpus is non-empty first.** A zero-hit result on
   an empty index is CORRECT behaviour, and it used to be reported as a
   retrieval failure — the wrong verdict for the right number.
3. **Unit criteria and integration criteria are named apart**:
   ``T1b(unit)`` builds its own index in a temporary directory and is
   about the RULE; ``T3(REAL)`` measures a real workspace and is about
   the CORPUS. Mixing them made "the rule works" and "this workspace has
   a corpus" indistinguishable in the output.
4. **The default workspace no longer follows the environment.** Without
   ``--workspace`` the probe uses the fixed default below; the registry
   and ``AGENT_WORKSPACE`` are not consulted. One probe, one index.
5. **A failing criterion asks "does the thing it measures exist?"** before
   it reports a retrieval failure.
6. **The probe is version-controlled.** ``git log`` on this file is the
   history of the criteria.

Usage
-----

    python -m agent.memory.acceptance.phase1 --workspace "C:\\Users\\User\\cow"
    python -m agent.memory.acceptance.phase1 --baseline
    python agent/memory/acceptance/phase1.py --workspace DIR

``tmp/probe_phase1_accept.py`` remains as a thin forwarder so the old
invocation keeps working.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


#: The workspace the probe measures when none is given.
#:
#: It is FIXED, on purpose. The old probe asked the registry for the
#: default agent, and the default agent's workspace is derived from
#: ``agent_workspace``, which the environment can override — so the probe
#: measured a different index depending on who ran it. A criterion whose
#: target depends on the environment is not a criterion.
#:
#: This is the INSTANCE ROOT: the workspace that holds the shared
#: knowledge base, the USER.md / AGENT.md identity files, and the memory
#: index of the default agent. It is the corpus the phase-one criteria
#: are written against (they assert that "Wang Jingjing" resolves to
#: USER.md, which only that corpus can answer).
DEFAULT_WORKSPACE = r"C:\Users\User\cow"


@dataclass(frozen=True)
class IndexFingerprint:
    """Which index a criterion actually read.

    A criterion that cannot say which index it read is not a criterion:
    it is a claim. The three fields together are enough to tell two runs
    apart — the path says WHICH index, the row count says whether the
    corpus changed, and the mtime says WHEN it changed.
    """

    path: str
    rows: int
    mtime: float
    exists: bool = True

    @property
    def empty(self) -> bool:
        """True when the index has no rows — the corpus cannot answer."""
        return self.exists and self.rows == 0

    def render(self) -> str:
        """One line, short enough to print under every criterion."""
        if not self.exists:
            return "%s (不存在 / absent)" % self.path
        return "%s（%d 行, mtime=%s）" % (
            self.path,
            self.rows,
            _mtime_stamp(self.mtime),
        )

    def render_en(self) -> str:
        if not self.exists:
            return "%s (absent)" % self.path
        return "%s (%d rows, mtime=%s)" % (
            self.path,
            self.rows,
            _mtime_stamp(self.mtime),
        )


def _mtime_stamp(ts: float) -> str:
    import datetime

    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)


def index_path_for(workspace: str) -> Path:
    """Where the workspace keeps its index.

    Same layout rule as ``common.state_dir.memory_index_db``: the index
    lives under the workspace root, not under the per-user root.
    """
    return Path(workspace) / "memory" / "long-term" / "index.db"


def fingerprint(workspace: str) -> IndexFingerprint:
    """The fingerprint of the index this workspace uses.

    Read-only, and it NEVER creates the index: a criterion that opens a
    database it is only supposed to measure has already changed what it
    measures.
    """
    db = index_path_for(workspace)
    if not db.is_file():
        return IndexFingerprint(path=str(db), rows=0, mtime=0.0, exists=False)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    except sqlite3.Error:
        return IndexFingerprint(path=str(db), rows=0, mtime=0.0, exists=False)
    try:
        row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        rows = int(row[0]) if row else 0
    except sqlite3.Error:
        rows = 0
    finally:
        conn.close()
    try:
        mtime = db.stat().st_mtime
    except OSError:
        mtime = 0.0
    return IndexFingerprint(path=str(db), rows=rows, mtime=mtime, exists=True)


def resolve_workspace(explicit: Optional[str]) -> Tuple[str, str]:
    """The workspace to measure, and WHY that one.

    ``explicit`` wins. Otherwise the FIXED default. The registry and the
    environment are NOT consulted: P0-18 measured that
    ``AGENT_WORKSPACE`` changes which index the default agent resolves
    to, so a probe that follows it measures whatever the caller happened
    to have set.
    """
    if explicit:
        return os.path.abspath(explicit), "命令行指定 / --workspace"
    return DEFAULT_WORKSPACE, "固定默认 / fixed default (not env-dependent)"


class Report:
    """The result table.

    Every criterion prints its own fingerprint line, so the output says
    what was measured even when the criterion is about a temporary
    directory it built itself.
    """

    def __init__(self):
        self.rows: List[Tuple[str, str, bool]] = []
        self.fingerprints: Dict[str, IndexFingerprint] = {}

    def set_fingerprint(self, label: str, fp: IndexFingerprint) -> None:
        self.fingerprints[label] = fp

    def judge(self, number, name, ok, detail, fingerprint=None):
        """Record one criterion and print it with its fingerprint."""
        self.rows.append((number, name, bool(ok)))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {number}: {name}")
        if fingerprint is not None:
            print(f"         被测库 / index: {fingerprint.render()}")
        for line in str(detail).splitlines():
            print(f"         {line}")
        return ok

    def summary(self):
        passed = sum(1 for r in self.rows if r[2])
        total = len(self.rows)
        print()
        print("=" * 72)
        print("判据通过: %d/%d" % (passed, total))
        for number, name, ok in self.rows:
            print("  %s  %s. %s" % ("PASS" if ok else "FAIL", number, name))
        print("=" * 72)
        return passed == total
