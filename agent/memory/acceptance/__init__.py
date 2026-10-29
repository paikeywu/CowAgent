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

import hashlib
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
    it is a claim. The fields together are enough to tell two runs
    apart — the path says WHICH index, the row count says whether the
    corpus changed, the mtime says WHEN it changed, and the digests say
    WHETHER THE CONTENT changed.

    P0-20 S-1: the digest was added because a criterion that MODIFIES the
    object it measures cannot be trusted, and rows+mtime cannot prove it
    did not: a search rewrites ``hit_count`` on the rows it matched, which
    changes the content WITHOUT changing the row count.

    P0-21 F2: the digest was a digest of the FILE's BYTES, and the index
    runs in WAL mode — a write goes to ``index.db-wal`` and the main file
    does not move until a checkpoint. So the byte digest saw a search that
    wrote ``hit_count`` as "nothing happened": a false green. The digest
    is now a CONTENT digest computed from the rows, read THROUGH SQLite —
    which merges the committed WAL frames, so the sidecar is covered
    without the false positives a raw byte hash of the sidecar would
    produce (a checkpoint moves bytes without changing content).

    The file-level digests are kept for forensics: they say WHICH bytes
    moved, and the report prints them when something did.
    """

    path: str
    rows: int
    mtime: float
    exists: bool = True
    sha256: str = ""
    file_sha256: str = ""
    wal_sha256: str = ""

    @property
    def empty(self) -> bool:
        """True when the index has no rows — the corpus cannot answer.

        P0-22: a MISSING index file is empty too. The old definition was
        ``exists and rows == 0``, so a workspace that has no index at all
        was not "empty" — it was "absent", and the criterion reported
        "top1 is not USER.md" for a workspace that simply has no corpus
        yet. A brand-new Agent's workspace has NO index before its first
        sync, so that is the normal case, not an exotic one.

        "Empty" and "absent" are different FACTS (one has a file with no
        rows, the other has no file), and the fingerprint still says
        which: ``exists`` is False in the second case. What they SHARE is
        the answer to the question the criterion asks: there is no corpus,
        so a zero-hit result is correct behaviour and not a retrieval
        failure.
        """
        return (not self.exists) or self.rows == 0

    def render(self) -> str:
        """One line, short enough to print under every criterion."""
        if not self.exists:
            return "%s (不存在 / absent)" % self.path
        return "%s（%d 行, mtime=%s, sha256=%s%s）" % (
            self.path,
            self.rows,
            _mtime_stamp(self.mtime),
            self.sha256[:16] if self.sha256 else "-",
            ", wal=%s" % (self.wal_sha256[:8] if self.wal_sha256 else "-"),
        )

    def render_en(self) -> str:
        if not self.exists:
            return "%s (absent)" % self.path
        return "%s (%d rows, mtime=%s, sha256=%s, wal=%s)" % (
            self.path,
            self.rows,
            _mtime_stamp(self.mtime),
            self.sha256[:16] if self.sha256 else "-",
            self.wal_sha256[:8] if self.wal_sha256 else "-",
        )

    def differs_from(self, other: "IndexFingerprint") -> List[str]:
        """Which fields differ from ``other`` — the S-1 comparison.

        Returns a LIST of the differing field names, so the report can say
        WHICH thing moved instead of only that something did. An empty list
        means the index's CONTENT is identical.

        The comparison is on the CONTENT digest. The file-level digests
        are reported too, but a difference there alone is not a violation:
        a WAL checkpoint moves bytes without changing a single row, and
        treating that as "the criterion modified the index" would make the
        check cry wolf on a perfectly healthy database.
        """
        out: List[str] = []
        if self.path != other.path:
            out.append("path")
        if self.rows != other.rows:
            out.append("rows")
        if self.exists != other.exists:
            out.append("exists")
        if self.sha256 and other.sha256 and self.sha256 != other.sha256:
            out.append("sha256")
        elif bool(self.sha256) != bool(other.sha256):
            out.append("sha256")
        return out


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


def sha256_of(path: Path) -> str:
    """The digest of a file's BYTES.

    Read in chunks: the index is tens of megabytes on a real workspace,
    and reading it whole would be a memory spike in a probe.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


#: The columns whose VALUE decides whether the index's content changed.
#:
#: ``hash`` is the per-chunk content digest, so the body does not have to
#: be hashed again. ``hit_count`` / ``last_hit_at`` are in: a search that
#: rewrites them DID modify the index, and a criterion that hides that is
#: the false green P0-21 F2 is about.
#:
#: P1-阶段三 S3-E: the FOUR body-externalization columns are in as well.
#: They were missing, and that was a REAL hole, not a theoretical one:
#: the S-1 check compares this digest before and after the criteria run,
#: so a criterion that WIPED ``body_path`` (set it back to NULL) changed
#: the index in exactly the way S-1 exists to catch — and the digest did
#: not move, because the digest did not look at those columns.  The
#: defect this stage is about (a stale writer wiping the migration's
#: backfill) was therefore invisible to the very check that should have
#: caught it.
#:
#: ``text_state`` is in for the same reason: it is the third state of the
#: body-externalization machine, and a criterion that flipped it was
#: modifying the object it measured.
_CONTENT_COLUMNS = (
    "id", "path", "scope", "user_id", "start_line", "end_line", "hash",
    "file_sealed", "element_released", "sealed_owner",
    "hit_count", "last_hit_at",
    # P1-阶段三 S3-E: the four extern columns + the state machine.
    "body_path", "body_anchor", "body_hash", "text_state",
)


def content_digest(conn: sqlite3.Connection) -> str:
    """The digest of the index's CONTENT, read through SQLite.

    P0-21 F2: reading the rows (rather than hashing the file) is what
    makes the WAL sidecar count. SQLite merges the committed WAL frames
    into what it serves, so a write that only ever reached
    ``index.db-wal`` is visible here — and a checkpoint, which moves bytes
    without changing a row, is not.
    """
    h = hashlib.sha256()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        if not cols:
            return ""
        wanted = [c for c in _CONTENT_COLUMNS if c in cols]
        sql = ("SELECT %s FROM chunks ORDER BY id" % ", ".join(wanted))
        for row in conn.execute(sql):
            h.update(repr(tuple(row)).encode("utf-8", "replace"))
            h.update(b"\x00")
    except sqlite3.Error:
        return ""
    return h.hexdigest()


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
        digest = content_digest(conn)
    except sqlite3.Error:
        rows = 0
        digest = ""
    finally:
        conn.close()
    try:
        mtime = db.stat().st_mtime
    except OSError:
        mtime = 0.0
    wal = Path(str(db) + "-wal")
    return IndexFingerprint(path=str(db), rows=rows, mtime=mtime,
                            exists=True, sha256=digest,
                            file_sha256=sha256_of(db),
                            wal_sha256=sha256_of(wal) if wal.is_file() else "")


# ---------------------------------------------------------------------------
# The source tree is not a workspace (P0-20 L-1)
# ---------------------------------------------------------------------------
#
# The criterion must refuse to run against a workspace that lives INSIDE
# the repository, for two reasons that are both about the criterion's own
# integrity:
#
#   1. It would write runtime state into the source tree. That is how
#      ``<repo>/memory/long-term/index.db`` (2.1 MB of somebody else's
#      memory index) came to exist in the first place — and because it was
#      neither tracked nor ignored, ``git clone`` handed it to everyone.
#   2. It would measure a corpus that is a COPY of the shared knowledge
#      base, not the shared knowledge base itself. The same question then
#      has two answers depending on which index you read, and a criterion
#      that cannot name its corpus is not a criterion.
#
# The check is on the RESOLVED paths, so a symlinked or relative path
# cannot sneak past it.

def _is_inside(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` or lives under it."""
    try:
        child_r = Path(os.path.realpath(str(child)))
        parent_r = Path(os.path.realpath(str(parent)))
    except OSError:
        return False
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


class SourceTreeWorkspaceError(RuntimeError):
    """The workspace points into the source tree — refused, not warned."""


def assert_not_source_tree(workspace: str, source_root: Path) -> None:
    """Refuse a workspace that is inside the source tree.

    ``source_root`` is the repository root (the directory that holds
    ``.git``). The workspace is refused when it IS that directory or
    lives under it.

    Raises ``SourceTreeWorkspaceError`` with a message that says what to
    do instead — a refusal that does not tell the caller the alternative
    is a refusal that just blocks the work.
    """
    ws = Path(workspace)
    if _is_inside(ws, source_root):
        raise SourceTreeWorkspaceError(
            "拒绝在源码树内验收：workspace %r 位于源码树 %r 之内。\n"
            "  理由：① 验收会把运行时状态（memory/long-term/index.db）写进源码树，"
            "且该文件既未被跟踪也未被忽略，clone 仓库的人会拿到别人的记忆索引；\n"
            "        ② 源码树内的 knowledge/ 是共享知识库的副本而非真源，"
            "同一问题在两个索引里答案不同，判据说不清自己量的是哪份语料。\n"
            "  请改指向真实工作区（例如 --workspace \"C:\\\\Users\\\\User\\\\cow\"）。"
            % (str(ws), str(source_root))
        )


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


#: The three states a criterion can be in.
#:
#: P1-阶段三 S3-E (NOTRUN):
#:
#:   * ``PASS``  — the criterion measured something and the measurement was
#:     good;
#:   * ``FAIL``  — the criterion measured something and the measurement was
#:     BAD;
#:   * ``NOTRUN`` — the criterion could NOT measure, because its
#:     precondition does not hold.  **NOTRUN is not green.**
#:
#: The line between FAIL and NOTRUN is the whole point of the third state:
#:
#:   * FAIL   = "I looked, and it is broken."
#:   * NOTRUN = "I could not look."
#:
#: A criterion that cannot distinguish the two reports a FAIL for a missing
#: prerequisite, and then everybody learns to read the FAIL as "the product
#: is broken" — which is how a deployment gets blamed for a criterion that
#: simply had nothing to measure.  (P1 阶段二踩了三次假红, each of which
#: looked exactly like "the deployment is broken".)
#:
#: The subtle one, and the one this module exists to make expressible:
#:
#:   "The migration ran, and the check STILL cannot decide" is NOTRUN
#:   only if the check's own precondition is absent.  If the migration ran
#:   and the check is still unable to judge, that is a FAIL of the
#:   CHECK, not a NOTRUN — because a NOTRUN there would let the very
#:   defect this stage is about (a migration that did not take, or an
#:   invariant that stays unjudgeable) hide behind "not run".
#:
#: See :meth:`Report.judge` and :meth:`Report.judge_notrun`.
STATE_PASS = "PASS"
STATE_FAIL = "FAIL"
STATE_NOTRUN = "NOTRUN"


class Report:
    """The result table.

    Every criterion prints its own fingerprint line, so the output says
    what was measured even when the criterion is about a temporary
    directory it built itself.

    P1-阶段三 S3-E: the table carries THREE states (PASS / FAIL / NOTRUN),
    not two.  The rows are ``(number, name, state)`` where ``state`` is
    one of the three constants above — previously a plain ``bool``.
    """

    def __init__(self):
        # P1-阶段三 S3-E: the third state.  ``rows`` is a list of
        # ``(number, name, state)`` triples; the state is a STRING, not a
        # bool, so NOTRUN cannot be silently folded into FAIL.
        self.rows: List[Tuple[str, str, str]] = []
        self.fingerprints: Dict[str, IndexFingerprint] = {}

    def set_fingerprint(self, label: str, fp: IndexFingerprint) -> None:
        self.fingerprints[label] = fp

    def judge(self, number, name, ok, detail, fingerprint=None, notrun=False):
        """Record one criterion and print it with its fingerprint.

        ``ok`` keeps its old meaning (a truthy value means PASS) so that
        every existing call site — phase1.py's ten, and phase2.py's — keeps
        working UNCHANGED.  The new keyword ``notrun=True`` moves the
        criterion into the third state; it is the explicit way to say
        "I could not measure this".

        A criterion must not report NOTRUN for a broken object: use
        ``judge_notrun`` for that, and pass ``ok=False`` when the thing
        you measured is bad.
        """
        if notrun:
            return self.judge_notrun(number, name, detail, fingerprint=fingerprint)
        self.rows.append((number, name, STATE_PASS if ok else STATE_FAIL))
        mark = STATE_PASS if ok else STATE_FAIL
        print(f"  [{mark}] {number}: {name}")
        if fingerprint is not None:
            print(f"         被测库 / index: {fingerprint.render()}")
        for line in str(detail).splitlines():
            print(f"         {line}")
        return ok

    def judge_notrun(self, number, name, detail, fingerprint=None):
        """Record a criterion that COULD NOT be measured.

        The detail MUST say which precondition did not hold — a NOTRUN
        without a reason is indistinguishable from a criterion that was
        simply never run, and that is the "静默 PASS" the P1 red lines
        forbid.
        """
        self.rows.append((number, name, STATE_NOTRUN))
        print(f"  [{STATE_NOTRUN}] {number}: {name}")
        print(f"         前置条件不成立 —— 本判据未测量，NOTRUN 不等于通过")
        if fingerprint is not None:
            print(f"         被测库 / index: {fingerprint.render()}")
        for line in str(detail).splitlines():
            print(f"         {line}")
        return None

    def summary(self):
        passed = sum(1 for r in self.rows if r[2] == STATE_PASS)
        failed = sum(1 for r in self.rows if r[2] == STATE_FAIL)
        notrun = sum(1 for r in self.rows if r[2] == STATE_NOTRUN)
        total = len(self.rows)
        print()
        print("=" * 72)
        print("判据通过: %d/%d   （FAIL %d · NOTRUN %d）" % (passed, total, failed, notrun))
        for number, name, state in self.rows:
            print("  %-6s %s. %s" % (state, number, name))
        print("=" * 72)
        if notrun:
            print("注意：%d 条判据为 NOTRUN —— 前置条件不成立，本轮未测量。"
                  "NOTRUN 不是通过。" % notrun)
        # The gate is: everything PASSed.  A NOTRUN is NOT a pass, so a
        # report with NOTRUNs must not return True.
        return passed == total
