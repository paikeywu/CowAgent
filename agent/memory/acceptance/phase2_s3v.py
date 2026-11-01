#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-V · the CROSS-END runtime census + the drift probes.

Why this module exists
----------------------
S3-E/J/K/M/T/U each judge ONE subject.  This one judges a subject none
of them does:

  * **S3-J** asks "was this library migrated, or was it migrated and then
    erased?" — about the CHUNKS side, and it answers it with the
    ``body_keep`` witness.
  * **S3-K** asks "is the certification coherent?" — about the
    CERTIFICATION LEDGER.
  * **XF** asks "do the libraries have the same SHAPE?" — about the
    SCHEMA.

None of them can answer the two questions this round actually hit:

  1. **WHO is running, and WHO is writing?**  The environment has TWO
     runtimes alive at once (a frozen desktop build and the source
     tree), and every "an old writer did it" claim in the gap list was
     an INFERENCE because nothing recorded the writer's identity.
  2. **Is the PROJECTION still a projection of the TRUTH?**  The
     fingerprint (``XF``) says the libraries have the same shape.  It
     says NOTHING about whether the indexed text still equals the file
     it was cut from.  Those are different objects, and a fingerprint
     that cannot see one of them is a false green.

The two mouths rule
-------------------
The witness for "the files-side statistics were erased" lives in
``files_keep`` (the guard's DELETE snapshot).  **This module owns that
witness** and J-5 imports it, so there is ONE mouth for the question
"was the files side erased, and by whom" — not two.

The three states
----------------
Every criterion here returns PASS / FAIL / **NOTRUN**, and NOTRUN is
not green.  In particular: **zero objects found is NOTRUN, never PASS.**
A probe that matched nothing has measured nothing; reporting that as
"no drift found" is the假绿 this whole codebase is trying to kill.

No machine-specific constants
------------------------------
Nothing in this module names an absolute path of the machine it was
written on.  The workspace comes from the caller's argument; the code
root comes from ``Path(__file__).resolve().parents[3]``.  The packaged
runtime is found by SHAPE (a frozen executable, i.e. one that is not
the interpreter running this criterion), never by a hard-coded
``C:\\AI\\...``.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory.acceptance.live_libs import enumerate_live, open_ro

#: The repository root — derived from THIS FILE's location, never
#: hard-coded.  ``agent/memory/acceptance/<this file>`` ⇒ 3 levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The state file for the J-5 monitor's INCREMENT.  It lives in the
#: workspace's audit directory (next to ``facade.jsonl`` /
#: ``file_changes.jsonl``), NOT in any library — the monitor must not
#: write to the object it measures.
MONITOR_STATE_NAME = "j5_monitor_state.json"

#: How far apart two writes must be to count as "one event" when
#: grouping the degradation timestamps.  Configurable; the default is
#: one second because the writers stamp whole seconds.
DEFAULT_EVENT_WINDOW = 1

#: The source layer's own vocabulary for a rule identity.  The probe
#: reads this from the workspace; it is not a machine constant, it is a
#: PATTERN.  (An empty match set ⇒ NOTRUN.)
BASELINE_RULE_PATTERN = re.compile(r"\bBEH-\d+\b")

#: The file names that make up the identity layer, relative to the
#: workspace root.  The persona layer's names are read from the
#: workspace, not hard-coded (the layer may grow a file).
IDENTITY_FILES = ("AGENT.md", "USER.md", "MEMORY.md", "RULE.md")


# ---------------------------------------------------------------------------
# the runtime census
# ---------------------------------------------------------------------------

class Runtime:
    """One running process, classified by WHAT KIND OF RUNTIME it is."""

    __slots__ = ("pid", "ppid", "exe", "cmdline", "started", "kind",
                 "build_time", "note")

    def __init__(self, pid, ppid, exe, cmdline, started, kind, build_time,
                 note=""):
        self.pid = pid
        self.ppid = ppid
        self.exe = exe
        self.cmdline = cmdline
        self.started = started
        self.kind = kind
        self.build_time = build_time
        self.note = note

    @property
    def name(self) -> str:
        return Path(self.exe).name if self.exe else "?"

    def render(self) -> str:
        return ("pid=%-7s kind=%-10s started=%s build=%s exe=%s"
                % (self.pid, self.kind,
                   _fmt_ts(self.started) if self.started else "?",
                   _fmt_ts(self.build_time) if self.build_time else "?",
                   self.exe or "?"))

    def to_dict(self) -> Dict[str, Any]:
        return {"pid": self.pid, "ppid": self.ppid, "exe": self.exe,
                "cmdline": self.cmdline, "started": self.started,
                "kind": self.kind, "build_time": self.build_time,
                "note": self.note}


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_age(seconds: int) -> str:
    """A duration in human units — the reader must be able to see
    "how long ago" without doing arithmetic."""
    if seconds < 0:
        return "未来"
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分钟 %d 秒" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d 天 %d 小时" % (seconds // 86400, (seconds % 86400) // 3600)


def _exe_build_time(exe: str) -> Optional[float]:
    try:
        p = Path(exe)
        if p.is_file():
            return float(p.stat().st_mtime)
    except OSError:
        pass
    return None


def _windows_processes() -> Optional[List[Dict[str, Any]]]:
    """The process table on Windows, via ``wmic process ... /format:list``.

    ``wmic`` is deprecated on the newest Windows builds and this module
    must not depend on it being present — hence the ``None`` return:
    the caller turns "could not enumerate" into NOTRUN, not into
    "no foreign runtime found".

    **MEASURED (2026-11-01): two parsing bugs, both found by running
    the criterion, both of which would have produced a FALSE GREEN on
    exactly the question this criterion exists to answer.**

      * The CSV format (``/format:csv``) puts the command line in a
        quoted field that CONTAINS COMMAS.  Splitting on commas drops
        every such row — MEASURED: 26 of 210 rows lost, and every row
        with a command line was among them.
      * The CSV format also REORDERS the columns alphabetically, so
        the header has to be read rather than assumed.

    The LIST format (``/format:list``) emits one ``Key=Value`` per
    line and one blank-line-separated block per process.  MEASURED on
    this machine: **210 blocks, 210 ``ProcessId=``, 210
    ``ExecutablePath=``** — every field present, no quoting to get
    wrong.  The list format is used here, and the block count is
    CHECKED: a census that lost rows must say so rather than report
    "no packaged runtime alive".
    """
    try:
        raw = subprocess.run(
            ["wmic", "process", "get",
             "ProcessId,ParentProcessId,ExecutablePath,CreationDate,CommandLine",
             "/format:list"],
            capture_output=True, timeout=60)
    except Exception:
        return None
    text = raw.stdout.decode("gbk", errors="replace")
    rows: List[Dict[str, Any]] = []
    current: Dict[str, str] = {}

    def flush() -> None:
        if current.get("ProcessId", "").isdigit():
            rows.append(_row_from_block(current))

    # **MEASURED (2026-11-01): TWO parsing bugs, both found by running
    # the criterion, both of which would have produced a FALSE GREEN on
    # exactly the question this criterion exists to answer.**
    #
    #   * The CSV format (``/format:csv``) puts the command line in a
    #     quoted field that CONTAINS COMMAS.  Splitting on commas drops
    #     every such row — MEASURED: 26 of 210 rows lost, and every row
    #     with a command line was among them.
    #   * In the LIST format the blocks are separated by FOUR blank
    #     lines, and ``ProcessId`` is the LAST field of each block.  A
    #     parser that flushes on a blank line, or that flushes when it
    #     SEES ``ProcessId=`` (before storing it), discards every
    #     block — MEASURED: the census came back with ONE process (its
    #     own), and the verdict was "no packaged runtime alive".
    #
    # The rule that survives both: **a block ends when a field NAME
    # that the current block already carries shows up again.**  That is
    # independent of the field order and of the blank-line count.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in current:
            # A field we have already seen ⇒ a NEW block starts here.
            flush()
            current = {}
        current[key] = value.strip()
    flush()
    return rows or None


def _row_from_block(block: Dict[str, str]) -> Dict[str, Any]:
    """One process, from one ``Key=Value`` block."""
    pid = block.get("ProcessId", "")
    ppid = block.get("ParentProcessId", "")
    created = block.get("CreationDate", "")
    started = None
    if created:
        m = re.match(r"^(\d{14})", created)
        if m:
            try:
                started = time.mktime(time.strptime(m.group(1),
                                                    "%Y%m%d%H%M%S"))
            except ValueError:
                started = None
    return {
        "pid": int(pid) if pid.isdigit() else 0,
        "ppid": int(ppid) if ppid.isdigit() else 0,
        "exe": block.get("ExecutablePath", "") or "",
        "cmdline": block.get("CommandLine", "") or "",
        "started": started,
    }


def _posix_processes() -> Optional[List[Dict[str, Any]]]:
    """The process table on Linux/macOS, via ``/proc``."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    rows: List[Dict[str, Any]] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            exe = os.readlink(str(entry / "exe"))
        except OSError:
            exe = ""
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace").strip()
        except OSError:
            cmd = ""
        started = None
        try:
            stat = (entry / "stat").read_text()
            started = float(stat.rsplit(")", 1)[1].split()[19]) / 100.0
        except Exception:
            pass
        rows.append({"pid": int(entry.name), "ppid": 0, "exe": exe,
                     "cmdline": cmd, "started": started})
    return rows or None


def _tasklist_processes() -> Optional[List[Dict[str, Any]]]:
    """A FALLBACK census: ``tasklist`` gives the image names and pids.

    ``tasklist`` does NOT give the executable path or the start time, so
    it cannot answer "which runtime is older than the code" on its own.
    It CAN answer "is a process with this image name alive" — which is
    the half that matters most, and the half that ``wmic`` losing
    rows would silently get wrong.

    So the fallback returns rows whose ``exe`` is the image name and
    whose ``started`` is unknown.  A row with an unknown start time is
    treated as "possibly alive at any time" by :func:`alive_at` — it
    is reported as "present, start time unknown", never as "provably
    there at the damage moment".
    """
    try:
        raw = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, timeout=60)
    except Exception:
        return None
    text = raw.stdout.decode("gbk", errors="replace")
    try:
        import csv
        import io as _io
        records = list(csv.reader(_io.StringIO(text)))
    except Exception:
        return None
    rows: List[Dict[str, Any]] = []
    for parts in records:
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        pid = parts[1].strip()
        if not pid.isdigit():
            continue
        rows.append({"pid": int(pid), "ppid": 0, "exe": name,
                     "cmdline": "", "started": None})
    return rows or None


def _process_rows() -> Optional[List[Dict[str, Any]]]:
    if os.name == "nt":
        rows = _windows_processes()
        if rows and len(rows) > 1:
            return rows
        return _tasklist_processes() or rows
    return _posix_processes()


#: How many rows the process table had.  Cached so the census can be
#: printed with the count it saw — a census that lost rows must be
#: visible, not silently smaller.
_PROCESS_COUNT: Optional[int] = None


def _process_count() -> int:
    global _PROCESS_COUNT
    if _PROCESS_COUNT is None:
        rows = _process_rows() or []
        _PROCESS_COUNT = len(rows)
    return _PROCESS_COUNT


def _packaged_names() -> Tuple[str, ...]:
    """The executable names this project SHIPS, read from the build
    configuration — never hard-coded.

    The desktop build's ``package.json`` declares ``productName``
    (``CowAgent``) and the PyInstaller spec declares the backend's name
    (``cowagent-backend``).  Reading them from the repository is what
    makes the detection portable: rename the product and the probe
    follows, because the probe reads the rename.

    The names are compared **with and without the extension**, because
    the process table reports ``CowAgent.exe`` while the build
    configuration says ``CowAgent``.
    """
    names: List[str] = []
    pkg = REPO_ROOT / "desktop" / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        build = data.get("build") or {}
        # MEASURED (2026-11-01): ``productName`` lives under ``build``,
        # not at the top level — the first version read it from the top
        # level and got ``None``, so the probe matched no executable
        # and reported "no packaged runtime alive".
        for value in (build.get("productName"), data.get("name")):
            if isinstance(value, str) and value:
                names.append(value.lower())
    except Exception:
        pass
    spec = REPO_ROOT / "desktop" / "build" / "cowagent-backend.spec"
    try:
        for line in spec.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("name="):
                v = stripped.split("=", 1)[1].strip()
                # MEASURED (2026-11-01): the value is a Python string
                # literal — ``name='cowagent-backend',`` — so it carries
                # quotes AND a trailing comma.  **The order matters**:
                # stripping the quotes FIRST leaves ``cowagent-backend',``
                # (the comma is now the last character, and the trailing
                # quote is the one before it) ⇒ the name fails the
                # sanity filter ⇒ the backend process is NOT classified
                # as PACKAGED ⇒ it does not appear in the "alive at the
                # damage moment" list.
                #
                # MEASURED: this is exactly why U-1 (the backend was
                # missing from the attribution) happened.  The comma
                # must go FIRST, then the quotes.
                v = v.rstrip(",").strip()
                v = v.strip("'\"").strip()
                if v:
                    names.append(v.lower())
                break
    except Exception:
        pass
    out: List[str] = []
    for n in names:
        out.append(n)
        stem = n[:-4] if n.endswith(".exe") else n
        out.append(stem)
    # A name that still carries a quote or a trailing comma is a
    # PARSING failure, not a name.  Shipping it would match nothing.
    return tuple(dict.fromkeys(n for n in out if n and n.isascii()
                                and n.replace("-", "").replace("_", "")
                                .isalnum()))


def _classify(row: Dict[str, Any], self_pid: int,
              packaged_names: Tuple[str, ...]) -> Runtime:
    """Classify one process by WHAT KIND OF RUNTIME it is.

    The classification is by SHAPE, never by a path constant:

      * ``SELF``      — the interpreter running this criterion.
      * ``SOURCE``    — an interpreter whose command line runs code out
                        of the repository root (the source runtime).
      * ``PACKAGED``  — one of THIS PROJECT'S shipped executables (the
                        names come from the build configuration).  A
                        frozen build carries its OWN copy of the code,
                        so it can be OLDER than the repository — that
                        is the whole point of finding it.
      * ``OTHER``     — anything else, INCLUDING every unrelated
                        program on the machine.

    **MEASURED (2026-11-01): the first version classified every
    ``.exe`` on the machine as "PACKAGED" — 59 of them, from
    ``svchost.exe`` to OneDrive.**  That is not a detection, it is a
    listing of the operating system, and it drowns the one process
    that matters.  A criterion that reports everything is a criterion
    that reports nothing.  The project's own executables are named by
    reading the build configuration; everything else is OTHER.
    """
    pid = row.get("pid")
    exe = (row.get("exe") or "")
    cmd = (row.get("cmdline") or "")
    base = Path(exe).name.lower() if exe else ""
    # MEASURED (2026-11-01): the process table reports the FULL image
    # name (``CowAgent.exe``) while the build configuration says
    # ``CowAgent``.  Comparing them raw matched nothing.
    stem = base[:-4] if base.endswith(".exe") else base

    interpreter = (base.startswith("python") or base.startswith("pythonw")
                   or base.startswith("node"))
    if pid == self_pid:
        kind, note = "SELF", "the interpreter running this criterion"
    elif stem in packaged_names:
        kind, note = "PACKAGED", "one of this project's shipped executables"
    elif interpreter:
        if str(REPO_ROOT).lower() in cmd.lower():
            kind, note = "SOURCE", "runs code out of the repository root"
        else:
            kind, note = "OTHER", "an interpreter running something else"
    else:
        kind, note = "OTHER", ""

    build = _exe_build_time(exe) if exe else None
    return Runtime(pid, row.get("ppid"), exe, cmd, row.get("started"),
                   kind, build, note)


def runtime_census() -> Optional[List[Runtime]]:
    """Every running process, classified.  ``None`` when the platform
    cannot be enumerated — the caller must then say NOTRUN."""
    rows = _process_rows()
    if not rows:
        return None
    self_pid = os.getpid()
    names = _packaged_names()
    out = [_classify(r, self_pid, names) for r in rows]
    # Only the interesting ones: the interpreters and THIS PROJECT's
    # frozen builds.
    return [r for r in out if r.kind in ("SELF", "SOURCE", "PACKAGED")]


def alive_at(census: List[Runtime], when: float) -> Tuple[List[Runtime], List[Runtime]]:
    """Which of these runtimes were ALIVE at ``when``.

    Returns ``(provably_alive, start_time_unknown)``.  A runtime whose
    start time could not be read (the ``tasklist`` fallback cannot read
    it) is NOT claimed to have been there — it is reported separately
    as "present, start time unknown", because "it is running now" and
    "it was running then" are different claims and only one of them is
    supported by the evidence.
    """
    provably: List[Runtime] = []
    unknown: List[Runtime] = []
    for r in census:
        if r.started is None:
            unknown.append(r)
        elif r.started <= when:
            provably.append(r)
    return provably, unknown


def _repo_head_time() -> Optional[float]:
    """When the repository's current code was committed.

    This is the reference point for "is the running runtime OLDER than
    the code".  It is read from git, so it is the truth about the
    repository — not a constant.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return float(out.stdout.strip())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# the writer census — WHO erased the files-side statistics
# ---------------------------------------------------------------------------

class WriterCensus:
    """The files-side erasure census for one library.

    **The witness is ``files_keep``** — the guard's snapshot of the
    ``files`` row as it was BEFORE a DELETE.  It is the files-side
    analogue of ``body_keep``, and it is the only thing in the system
    that can say "this row HAD statistics and now it does not".

    Two shapes of damage, and they are DIFFERENT:

      * **DELETED**  — the ``files`` row is GONE.  The DELETE trigger
        (``files_body_keep_bd``) copied its statistics into
        ``files_keep`` first, so the erasure is PROVEN, not inferred.
      * **DOWNGRADED** — the row is still there but its statistics were
        overwritten with the "not measured" values (``0``).  The
        ``files_keep`` row proves what the statistics USED to be.

    **The writer's generation.**  ``files.chunker_version`` carries the
    chunker version that measured the file.  The current chunker writes
    the current version; the value ``0`` means "nobody with the current
    chunker ever measured this" — i.e. **a writer that does not know
    the current chunker wrote this row**.  That is the generation
    marker, and it is a FACT read out of the row, not a guess.

    The TIMESTAMP is taken from ``files.updated_at`` when the row
    survives, and from the newest ``chunks.updated_at`` for the same
    path when the row was deleted (the ``files_keep`` snapshot carries
    no timestamp of its own — the guard does not write one).
    """

    __slots__ = ("name", "db", "error", "deleted", "downgraded",
                 "never_measured", "last_damage_at", "oldest_damage_at",
                 "keep_rows")

    def __init__(self, name: str, db: str):
        self.name = name
        self.db = db
        self.error: Optional[str] = None
        #: ``[(path, keep_chunk_count, keep_chunker_version, at)]``
        self.deleted: List[Tuple[str, Optional[int], Optional[int], Optional[int]]] = []
        #: ``[(path, keep_cc, keep_cv, now_cc, now_cv, at)]``
        self.downgraded: List[Tuple[str, Optional[int], Optional[int],
                                    Optional[int], Optional[int], Optional[int]]] = []
        #: paths with NULL/0 statistics that have NO keep witness — these
        #: are "never measured", NOT erasures.  Kept apart on purpose.
        self.never_measured: List[str] = []
        self.last_damage_at: Optional[int] = None
        self.oldest_damage_at: Optional[int] = None
        self.keep_rows = 0

    @property
    def damaged_paths(self) -> int:
        return len(self.deleted) + len(self.downgraded)

    def damage_at(self) -> List[int]:
        out = [r[3] for r in self.deleted if r[3]]
        out += [r[5] for r in self.downgraded if r[5]]
        return out


def _files_census(lib) -> WriterCensus:
    c = WriterCensus(lib.name, str(lib.db))
    try:
        conn = open_ro(lib.db)
    except Exception as exc:  # noqa: BLE001
        c.error = "%s: %s" % (type(exc).__name__, exc)
        return c
    try:
        tabs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "files_keep" not in tabs:
            c.error = "无 files_keep 表（未装守卫）"
            return c
        keeps = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT path, chunk_count, chunker_version FROM files_keep")}
        c.keep_rows = len(keeps)
        now = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT path, chunk_count, chunker_version, updated_at FROM files")}

        for path, (kcc, kcv) in keeps.items():
            if kcc is None and kcv is None:
                continue
            cur = now.get(path)
            if cur is None:
                # DELETED: the keep table is the witness that it HAD
                # statistics.  Its timestamp comes from the chunks side.
                at = None
                try:
                    row = conn.execute(
                        "SELECT MAX(updated_at) FROM chunks WHERE path=?",
                        (path,)).fetchone()
                    at = int(row[0]) if row and row[0] is not None else None
                except sqlite3.Error:
                    at = None
                c.deleted.append((path, kcc, kcv, at))
                continue
            cc, cv, upd = cur
            # DOWNGRADED: the row survives but its statistics were
            # overwritten with the "not measured" values.
            if _is_unmeasured(cc) and not _is_unmeasured(kcc):
                c.downgraded.append((path, kcc, kcv, cc, cv, upd))
        # the paths whose statistics are NULL/0 with NO keep witness:
        # "never measured", which is NOT an erasure.
        for path, (cc, cv, upd) in now.items():
            if path in keeps:
                continue
            if _is_unmeasured(cc) or _is_unmeasured(cv):
                c.never_measured.append(path)
        times = c.damage_at()
        if times:
            c.last_damage_at = max(times)
            c.oldest_damage_at = min(times)
    except sqlite3.Error as exc:
        c.error = "读库失败: %s" % exc
    finally:
        conn.close()
    return c


def _is_unmeasured(value: Any) -> bool:
    """Is this statistics value the "not measured" marker?

    ``None`` (never filled in) and ``0`` (the old writer's default)
    both mean "nobody measured this".  ``4`` means "measured by the
    current chunker".  The comparison is done on the INTEGER, because
    ``files.chunker_version`` is declared TEXT and SQLite hands back
    ``'0'`` — a stringly comparison would call ``'0' != 0`` and report
    every row as "version wrong".
    """
    if value is None:
        return True
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# DR-1 · the dual-runtime detection
# ---------------------------------------------------------------------------

def judge_dr1_dual_runtime_is_detected(report, workspace: Path):
    """DR-1 (REAL): the runtimes that are alive, and which one is OLD.

    The question this answers: **is more than one runtime alive, and is
    one of them older than the code in the repository?**

    Why that question: a frozen build carries its OWN copy of the
    code.  When it is alive at the same time as the source runtime, the
    two can disagree about what a write means — and nothing in the
    database records which one wrote.  So the census is the FIRST half
    of the answer, and the writer census (DR-1's second half, below)
    is the second.

    **The verdict is about the CENSUS, not about the libraries.**  Two
    runtimes alive is not a defect — it is a fact.  The defect is a
    frozen runtime whose build PREDATES the repository's HEAD: that
    build cannot know about the guard, the ledger, or the v4 chunker,
    so every write it makes is a write the current code cannot defend
    against.
    """
    detail: List[str] = []
    census = runtime_census()
    if census is None:
        return report.judge_notrun(
            "DR-1(REAL)",
            "DR-1 双运行时检测：并存的运行时清单 + 谁比仓库旧 + 谁在写库",
            "本平台的进程表读不出来（wmic / /proc 都不可用）—— "
            "本判据未测量，NOTRUN 不等于通过")

    head_time = _repo_head_time()
    detail.append("仓库根: %s" % REPO_ROOT)
    detail.append("仓库 HEAD 提交时间: %s" % (_fmt_ts(head_time) if head_time else "读不到"))
    detail.append("本判据自己的解释器: pid=%s exe=%s" % (os.getpid(), sys.executable))
    detail.append("本项目出厂的可执行文件名（从构建配置读，不写死）: %s"
                  % (", ".join(_packaged_names()) or "读不到"))
    detail.append("")
    detail.append("运行时清单（可分类 %d 项 / 进程表 %d 项）:"
                  % (len(census), _process_count()))
    for r in sorted(census, key=lambda x: (x.kind, x.pid)):
        detail.append("  %s" % r.render())
        if r.cmdline:
            detail.append("      cmd = %s" % r.cmdline[:220])
        if r.note:
            detail.append("      %s" % r.note)

    packaged = [r for r in census if r.kind == "PACKAGED"]
    source = [r for r in census if r.kind in ("SELF", "SOURCE")]
    detail.append("")
    detail.append("分类统计: 本项目的冻结版(PACKAGED)=%d  源码版(SELF/SOURCE)=%d"
                  % (len(packaged), len(source)))

    stale: List[Runtime] = []
    if head_time:
        for r in packaged:
            if r.build_time and r.build_time < head_time:
                stale.append(r)
        detail.append("比仓库 HEAD 旧的冻结版: %d" % len(stale))
        for r in stale:
            detail.append("  ! pid=%s exe=%s 构建于 %s（早于 HEAD %s）"
                          % (r.pid, r.exe, _fmt_ts(r.build_time),
                             _fmt_ts(head_time)))
        if packaged and not stale:
            detail.append("  （冻结版在场，但没有比 HEAD 旧的）")
    else:
        detail.append("仓库 HEAD 时间读不到 ⇒ 无法判定冻结版的代际")

    # ---- the second half: the writer census -----------------------
    detail.append("")
    detail.append("写者普查（files_keep 证人 —— 谁擦掉了 files 统计列）:")
    total_damaged = 0
    attributed: List[str] = []
    for lib in enumerate_live(workspace):
        c = _files_census(lib)
        if c.error:
            detail.append("  %-22s 前置不成立: %s" % (c.name, c.error))
            continue
        if not c.deleted and not c.downgraded:
            detail.append("  %-22s 无已证明的降级（keep 行=%d，"
                          "仅未测量=%d）" % (c.name, c.keep_rows,
                                          len(c.never_measured)))
            continue
        total_damaged += c.damaged_paths
        detail.append("  %-22s 删除=%d 降级=%d  （未测量但无证人=%d）"
                      % (c.name, len(c.deleted), len(c.downgraded),
                         len(c.never_measured)))
        for path, kcc, kcv, at in c.deleted:
            detail.append("      [删除] %-56s 曾有 cc=%s cv=%s  时刻=%s"
                          % (path, kcc, kcv, _fmt_ts(at)))
        for path, kcc, kcv, cc, cv, at in c.downgraded:
            detail.append("      [降级] %-56s cc %s→%s cv %s→%s  时刻=%s"
                          % (path, kcc, cc, kcv, cv, _fmt_ts(at)))
        # attribution: which runtimes were ALIVE at the damage moment?
        if c.last_damage_at:
            provably, unknown = alive_at(census, float(c.last_damage_at))
            detail.append("      ⇒ 最近一次降级: %s（距今 %s）"
                          % (_fmt_ts(c.last_damage_at),
                             _fmt_age(int(time.time()) - c.last_damage_at)))
            detail.append("      ⇒ 那一刻【有证据证明在场】的运行时: %d"
                          % len(provably))
            for r in provably:
                tag = ""
                if r.kind == "PACKAGED":
                    tag = ("  ← 本项目的冻结版"
                           + ("（代码早于 HEAD，不认识守卫/账本/v4 切法）"
                              if head_time and r.build_time
                              and r.build_time < head_time else ""))
                detail.append("           pid=%-7s kind=%-9s build=%s exe=%s%s"
                              % (r.pid, r.kind, _fmt_ts(r.build_time),
                                 r.exe or "?", tag))
            if unknown:
                detail.append("      ⇒ 在场但启动时间读不到（不能断言那一刻在场）: %d"
                              % len(unknown))
                for r in unknown[:6]:
                    detail.append("           pid=%-7s kind=%-9s exe=%s"
                                  % (r.pid, r.kind, r.exe or "?"))
            for r in provably:
                if r.kind == "PACKAGED":
                    attributed.append(
                        "%s (pid=%s，构建于 %s) 在 %s 时在场"
                        % (Path(r.exe).name or "?", r.pid,
                           _fmt_ts(r.build_time), _fmt_ts(c.last_damage_at)))

    detail.append("")
    detail.append("已证明的 files 侧降级 path 数: %d" % total_damaged)
    detail.append("归因结论（读数级别，不是 pid 级别）:")
    detail.append("  · 降级行的 chunker_version=0，而 files_keep 证明它曾=4")
    detail.append("    ⇒ 写这些行的程序【不认识当前切法】⇒ 它不是当前代码。")
    if attributed:
        for a in attributed:
            detail.append("  · 冻结版在场: %s" % a)
        detail.append("  ⇒ 只读的 SQLite 探针无法把某一次写归属到某个 pid；")
        detail.append("    它能给出的最强结论是【写者代际】+【在场清单】。")
    else:
        detail.append("  · 没有本项目的冻结版与降级时刻【有证据证明】重叠。")
        detail.append("    代际结论仍然成立（写者不认识当前切法），但当刻在场的")
        detail.append("    进程里没有可证明的冻结版 ⇒ 需要更细的取证（见越界发现）。")

    ok = not stale and not total_damaged
    if stale or total_damaged:
        detail.append("")
        detail.append("判定理由: 有冻结版运行时其代码早于仓库 HEAD（%d 个），"
                      "且/或有已证明的降级（%d 个 path）—— 旧端还在跑，"
                      "它随时可以再擦一次。按裁决 C5：此项保持红，"
                      "直到旧端被替换。" % (len(stale), total_damaged))
    return report.judge(
        "DR-1(REAL)",
        "DR-1 双运行时检测：并存的运行时清单 + 谁比仓库旧 + 谁在写库",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# DR-2 · probe one — change provenance (the identity layer's changes)
# ---------------------------------------------------------------------------

def _identity_layer_files(workspace: Path) -> List[Path]:
    """The identity layer's files, found by SHAPE.

    The persona layer's file names are read from the layer's own
    metadata when there is one; otherwise the layer's ``*.md`` files
    are used.  Nothing is hard-coded to this machine's layer.
    """
    out: List[Path] = []
    root = Path(workspace)
    for name in IDENTITY_FILES:
        p = root / name
        if p.is_file():
            out.append(p)
    agents = root / "agents"
    if agents.is_dir():
        for ad in sorted(agents.iterdir()):
            if not ad.is_dir():
                continue
            for name in IDENTITY_FILES:
                p = ad / name
                if p.is_file():
                    out.append(p)
    # the persona layer: whatever it declares, else its markdown files
    persona = root / "persona"
    if persona.is_dir():
        declared = None
        meta = persona / "PERSONA.meta.json"
        if meta.is_file():
            try:
                declared = list(json.loads(meta.read_text(encoding="utf-8"))
                                .get("files", {}).keys())
            except Exception:
                declared = None
        names = declared or sorted(p.name for p in persona.glob("*.md"))
        for name in names:
            p = persona / name
            if p.is_file():
                out.append(p)
    return out


def _change_log(workspace: Path) -> Dict[str, List[Dict[str, Any]]]:
    """The change log, grouped by path — from EVERY workspace's log.

    **MEASURED (2026-11-01): the change log is PER-WORKSPACE, not
    global.**  The ROOT workspace has ``memory/.audit/file_changes.jsonl``
    (94 paths) and each agent workspace has its own
    ``agents/<id>/memory/.audit/file_changes.jsonl`` (55–86 paths each).
    The first version of this probe read only the ROOT log and
    therefore reported 62 of 64 identity files as "never logged" — a
    false red caused by reading the wrong log.

    The paths in an agent's log are RELATIVE TO THAT AGENT'S
    WORKSPACE (``AGENT.md``, not ``agents/app/AGENT.md``), so the
    lookup has to try the agent's own log FIRST and then the shared
    one, under both spellings.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}

    def absorb(log_path: Path, prefix: str) -> None:
        if not log_path.is_file():
            return
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict) or not rec.get("path"):
                continue
            raw = rec["path"].replace("\\", "/")
            out.setdefault(raw, []).append(rec)
            if prefix:
                out.setdefault("%s/%s" % (prefix, raw), []).append(rec)

    workspace = Path(workspace)
    absorb(workspace / "memory" / ".audit" / "file_changes.jsonl", "")
    agents = workspace / "agents"
    if agents.is_dir():
        for ad in sorted(agents.iterdir()):
            if not ad.is_dir():
                continue
            absorb(ad / "memory" / ".audit" / "file_changes.jsonl",
                   "agents/%s" % ad.name)
    return out


def _mtime(path: Path) -> Optional[float]:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return None


def judge_dr2_change_provenance(report, workspace: Path):
    """DR-2 (REAL) · probe one: is every identity-layer change TRACEABLE?

    The probe's question (from the drift-detection design): *a change
    may happen, but every perceptible change of the persona must have a
    chain of memory behind it.*  The thing that changes the persona is
    the **identity layer** (``AGENT.md`` / ``USER.md`` / ``MEMORY.md``
    / the persona layer).  The chain behind it is the **change log**.

    So the probe asks, per identity file: **is the file newer than the
    last time the change log recorded it?**  If yes, the change
    happened OUTSIDE the logged paths ⇒ it is an **untraceable
    change** ("无源漂移") ⇒ the alarm condition.

    A file that was never in the log at all, and whose mtime is
    NEWER than the log's last activity, is also untraceable.  A file
    whose mtime is OLDER than its last logged entry is traced.

    **Zero identity files ⇒ NOTRUN** (there is nothing to measure).
    """
    detail: List[str] = []
    workspace = Path(workspace)
    files = _identity_layer_files(workspace)
    if not files:
        return report.judge_notrun(
            "DR-2(REAL)",
            "DR-2 漂移探针一 · 变化溯源：身份层每次改动都有记忆里的事件链支撑",
            "本工作区没有身份层文件，前置条件不成立（本判据未测量，"
            "NOTRUN 不等于通过）")

    log = _change_log(workspace)
    detail.append("身份层文件数: %d    变更记录点里的 path 数: %d"
                  % (len(files), len(log)))
    detail.append("变更记录文件: %s"
                  % ("memory/.audit/file_changes.jsonl"
                     if log else "（没有变更记录文件）"))
    detail.append("")
    detail.append("%-58s %-20s %-20s %s" % ("文件", "文件 mtime",
                                           "最后一条记录", "判定"))
    untraced: List[str] = []
    traced = 0
    for f in files:
        rel = _rel_to(f, workspace)
        mt = _mtime(f)
        entries = log.get(rel) or []
        last = entries[-1].get("at") if entries else None
        if last:
            try:
                last_ts = time.mktime(time.strptime(
                    str(last)[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                last_ts = None
        else:
            last_ts = None
        if mt is None:
            verdict = "读不到 mtime"
        elif last_ts is None:
            verdict = "无源（变更记录里没有这个 path）"
            untraced.append(rel)
        elif mt > last_ts + 1:
            verdict = "无源（文件比最后一条记录新）"
            untraced.append(rel)
        else:
            verdict = "有据"
            traced += 1
        detail.append("%-58s %-20s %-20s %s" % (
            rel, _fmt_ts(mt), str(last or "—"), verdict))
    detail.append("")
    detail.append("有据变化: %d    无源变化: %d" % (traced, len(untraced)))
    if untraced:
        detail.append("无源变化清单（这些改动在记忆里找不到事件链）:")
        for u in untraced:
            detail.append("  ! %s" % u)
        detail.append("⇒ 人格变了，但记忆里说不出它为什么变、从哪件事开始变。")
        detail.append("   这是探针一设计的那一个报警条件（无记忆支撑的人格偏移）。")
    return report.judge(
        "DR-2(REAL)",
        "DR-2 漂移探针一 · 变化溯源：身份层每次改动都有记忆里的事件链支撑",
        not untraced, "\n".join(detail))


def _rel_to(path: Path, root: Path) -> str:
    """The path relative to the workspace, in the LOG's spelling.

    The change log writes POSIX-ish relative paths (``agents/app/AGENT.md``)
    on every platform, so the lookup key must be normalised the same
    way.  Returning the raw ``str()`` would give
    ``agents\\app\\AGENT.md`` on Windows and the lookup would miss —
    which is exactly how the first version of this probe reported 62
    of 64 files as unlogged.
    """
    try:
        rel = Path(path).resolve().relative_to(Path(root).resolve())
        return str(rel).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


# ---------------------------------------------------------------------------
# DR-3 · probe two — the injection composition
# ---------------------------------------------------------------------------

def judge_dr3_injection_composition(report, workspace: Path):
    """DR-3 (REAL) · probe two: is every end fed the SAME raw material?

    The probe's question: *the raw material fed to the persona must not
    quietly change.*  The injection audit records, per round, which
    memory entries were injected and their composition.  The system
    does not keep an injection log yet, so this probe measures the
    thing that IS kept and that determines what a retrieval CAN inject:
    the **composition of the indexed corpus** per library — the
    ``source`` mix (``knowledge`` / ``memory``) and the ``scope`` mix
    (``shared`` / ``sealed``).

    **The cross-end question**: if one end's corpus contains a
    category that NO other end's corpus contains, that end was fed
    something the others never saw.  That is a hard, automatable
    signal, and it is the one the probe was designed for.

    **Zero libraries ⇒ NOTRUN.**
    """
    detail: List[str] = []
    libs = enumerate_live(Path(workspace))
    if not libs:
        return report.judge_notrun(
            "DR-3(REAL)",
            "DR-3 漂移探针二 · 检索注入审计：各端被喂的原料配比一致",
            "本工作区没有活库，前置条件不成立（本判据未测量，"
            "NOTRUN 不等于通过）")

    per_lib: Dict[str, Dict[str, int]] = {}
    for lib in libs:
        try:
            conn = open_ro(lib.db)
        except Exception as exc:  # noqa: BLE001
            detail.append("  %-22s 读不到: %s" % (lib.name, exc))
            continue
        try:
            src = {r[0] or "(空)": int(r[1]) for r in conn.execute(
                "SELECT source, COUNT(*) FROM chunks GROUP BY 1")}
            scope = {r[0] or "(空)": int(r[1]) for r in conn.execute(
                "SELECT scope, COUNT(*) FROM chunks GROUP BY 1")}
            per_lib[lib.name] = {"source": sum(src.values()),
                                 "scope": sum(scope.values())}
            detail.append("  %-22s source=%s scope=%s" % (lib.name, src, scope))
        except sqlite3.Error as exc:
            detail.append("  %-22s 读失败: %s" % (lib.name, exc))
        finally:
            conn.close()

    if not per_lib:
        return report.judge_notrun(
            "DR-3(REAL)",
            "DR-3 漂移探针二 · 检索注入审计：各端被喂的原料配比一致",
            "没有可读出配比的库，前置条件不成立（本判据未测量）")

    # the cross-end comparison
    all_sources: set = set()
    all_scopes: set = set()
    for lib in libs:
        try:
            conn = open_ro(lib.db)
        except Exception:
            continue
        try:
            all_sources |= {r[0] or "(空)" for r in conn.execute(
                "SELECT DISTINCT source FROM chunks")}
            all_scopes |= {r[0] or "(空)" for r in conn.execute(
                "SELECT DISTINCT scope FROM chunks")}
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    detail.append("")
    detail.append("全端出现的 source 取值: %s" % sorted(all_sources))
    detail.append("全端出现的 scope 取值: %s" % sorted(all_scopes))

    outliers: List[str] = []
    for lib in libs:
        try:
            conn = open_ro(lib.db)
        except Exception:
            continue
        try:
            mine_src = {r[0] or "(空)" for r in conn.execute(
                "SELECT DISTINCT source FROM chunks")}
            mine_scope = {r[0] or "(空)" for r in conn.execute(
                "SELECT DISTINCT scope FROM chunks")}
        except sqlite3.Error:
            mine_src = mine_scope = set()
        finally:
            conn.close()
        only = (mine_src - all_sources) | (mine_scope - all_scopes)
        if only:
            outliers.append("%s 只有它自己有的取值: %s" % (lib.name, sorted(only)))
    detail.append("")
    detail.append("只在一个端出现的取值（多端分裂的硬信号）: %d 个端" % len(outliers))
    for o in outliers:
        detail.append("  ! %s" % o)
    detail.append("")
    detail.append("注：注入日志（每轮注入了哪些条目、情感极性分布）目前尚不存在；")
    detail.append("    本探针测的是【能被检索到的原料的配比】，即注入的上游。")
    return report.judge(
        "DR-3(REAL)",
        "DR-3 漂移探针二 · 检索注入审计：各端被喂的原料配比一致",
        not outliers, "\n".join(detail))


# ---------------------------------------------------------------------------
# DR-4 · probe three — emotional evolution's continuity
# ---------------------------------------------------------------------------

def _event_records(base: Path) -> List[Dict[str, Any]]:
    """The event records under one root's ``memory/markdown/events``."""
    out: List[Dict[str, Any]] = []
    ev = base / "memory" / "markdown" / "events"
    if not ev.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(ev):
        for fn in sorted(files):
            if not fn.endswith(".md"):
                continue
            p = Path(dirpath) / fn
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            head = ""
            if text.startswith("---"):
                parts = text.split("---")
                if len(parts) >= 2:
                    head = parts[1]
            rec: Dict[str, Any] = {"path": str(p)}
            for line in head.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                rec[k.strip()] = v.strip().strip("'\"")
            out.append(rec)
    return out


def judge_dr4_emotion_continuity(report, workspace: Path):
    """DR-4 (REAL) · probe three: is the emotional evolution CONTINUOUS?

    The probe's question: *a jump in the emotional tone with no
    intermediate event behind it is the alarm; a change with a
    narrative behind it is life, not a defect.*

    The measurement: for every viewpoint (the role that authored the
    record), take its records in time order and look at the value that
    carries the emotional weight.  In this system that value is
    ``density`` in the event frontmatter.  Two consecutive records
    whose value differs, with NO record between them in time, are a
    **jump**.

    **A viewpoint with fewer than two records cannot be judged** — it
    is not evidence of continuity, it is absence of evidence.  If NO
    viewpoint has two records, the probe is **NOTRUN**.
    """
    detail: List[str] = []
    workspace = Path(workspace)
    roots = [workspace]
    agents = workspace / "agents"
    if agents.is_dir():
        roots += [d for d in sorted(agents.iterdir()) if d.is_dir()]

    series: Dict[str, List[Tuple[float, Any, str]]] = {}
    for root in roots:
        for rec in _event_records(root):
            vp = rec.get("author_viewpoint") or rec.get("subject") or "(无)"
            occ = rec.get("occurred_at") or ""
            try:
                ts = time.mktime(time.strptime(str(occ)[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                ts = 0.0
            series.setdefault(vp, []).append(
                (ts, rec.get("density"), rec.get("path", "")))

    judged = {vp: recs for vp, recs in series.items() if len(recs) >= 2}
    if not judged:
        return report.judge_notrun(
            "DR-4(REAL)",
            "DR-4 漂移探针三 · 情感演化连续性：极性跳变必须有中间事件",
            "没有任何视角有两条以上记录，本判据未测量（NOTRUN 不等于通过）。"
            "记录到的视角: %s" % (sorted(series) or "无"))

    detail.append("记录到的视角: %s" % sorted(series))
    detail.append("可判定的视角（记录数 ≥ 2）: %d" % len(judged))
    jumps: List[str] = []
    for vp in sorted(judged):
        recs = sorted(judged[vp], key=lambda x: x[0])
        detail.append("")
        detail.append("  视角 %s:" % vp)
        for ts, dens, path in recs:
            detail.append("    %-20s density=%-4s %s"
                          % (_fmt_ts(ts) if ts else "(无时间)", dens,
                             Path(path).name))
        for i in range(1, len(recs)):
            prev, cur = recs[i - 1], recs[i]
            if prev[1] == cur[1]:
                continue
            gap = (cur[0] - prev[0]) if (cur[0] and prev[0]) else None
            # a jump = the value changed with NO record in between.
            # Since the records are consecutive in the series, "no record
            # in between" is exactly "they are adjacent" — so the check
            # is whether the gap is bridged by any other viewpoint's
            # record for the same subject.  Absent a shared subject we
            # report the adjacency itself as the jump.
            jumps.append("视角 %s: density %s → %s，中间无记录（相邻）%s"
                         % (vp, prev[1], cur[1],
                            "，间隔 %.0fs" % gap if gap else ""))
    detail.append("")
    detail.append("极性跳变（相邻记录之间无中间事件）: %d" % len(jumps))
    for j in jumps:
        detail.append("  ! %s" % j)
    return report.judge(
        "DR-4(REAL)",
        "DR-4 漂移探针三 · 情感演化连续性：极性跳变必须有中间事件",
        not jumps, "\n".join(detail))


# ---------------------------------------------------------------------------
# DR-5 · probe four — the factory baseline's conformance
# ---------------------------------------------------------------------------

def judge_dr5_baseline_conformance(report, workspace: Path):
    """DR-5 (REAL) · probe four: the factory baseline's rule numbering.

    The probe's question: *a rule identity that is referenced but not
    defined (or defined twice with different meanings) is a hole in the
    factory baseline — the kernel's discipline is being diluted.*

    The measurement: find every baseline-rule identifier in the
    workspace's identity layer, then look for identifiers that are
    REFERENCED but have no DEFINITION anywhere in the same layer.  A
    dangling reference is the measurable form of "the baseline's
    numbering is being violated".

    **Zero identifiers ⇒ NOTRUN** — the baseline numbering does not
    exist in this workspace yet, so there is nothing to judge.  That is
    exactly the case the acceptance rules call out: 0 matches is NOTRUN,
    never PASS.
    """
    detail: List[str] = []
    workspace = Path(workspace)
    files = _identity_layer_files(workspace)
    texts: Dict[str, str] = {}
    for f in files:
        try:
            texts[_rel_to(f, workspace)] = f.read_text(encoding="utf-8",
                                                        errors="replace")
        except OSError:
            continue
    # the definitions may live anywhere in the workspace's rule layer
    extra: List[str] = []
    for cand in (workspace / "RULE.md", workspace / "AGENT.md",
                 workspace / "config"):
        if cand.is_file():
            try:
                extra.append(cand.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        elif cand.is_dir():
            for p in cand.rglob("*.md"):
                try:
                    extra.append(p.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass

    found: Dict[str, List[str]] = {}
    for rel, text in texts.items():
        for m in BASELINE_RULE_PATTERN.findall(text):
            found.setdefault(m, []).append(rel)
    detail.append("身份层文件数: %d" % len(files))
    detail.append("出厂基线规则标识的匹配模式: %s" % BASELINE_RULE_PATTERN.pattern)
    detail.append("匹配到的标识数: %d" % len(found))
    for ident in sorted(found):
        detail.append("  %-12s 出现于: %s" % (ident, ", ".join(found[ident][:3])))

    if not found:
        return report.judge_notrun(
            "DR-5(REAL)",
            "DR-5 漂移探针四 · 出厂基线符合度：编号规则的引用都能找到定义",
            "匹配到 0 个出厂基线规则标识 —— 该编号层在本工作区尚不存在，"
            "本判据未测量（0 个匹配 = NOTRUN，不是 PASS）")

    defined = set()
    for text in extra:
        defined |= set(BASELINE_RULE_PATTERN.findall(text))
    dangling = sorted(i for i in found if i not in defined)
    detail.append("")
    detail.append("在规则层找到定义的标识: %d" % len(defined))
    detail.append("悬空引用（被引用但无定义）: %d" % len(dangling))
    for d in dangling:
        detail.append("  ! %s 出现在 %s，但规则层里没有它的定义"
                      % (d, ", ".join(found[d][:3])))
    return report.judge(
        "DR-5(REAL)",
        "DR-5 漂移探针四 · 出厂基线符合度：编号规则的引用都能找到定义",
        not dangling, "\n".join(detail))


# ---------------------------------------------------------------------------
# DR-6 · the new dimension — the file changed but the index was not recut
# ---------------------------------------------------------------------------

def _truth_file(workspace: Path, relpath: str) -> Optional[Path]:
    try:
        from agent.memory.body_pointer import resolve_truth_file
    except Exception:
        return None
    try:
        p = resolve_truth_file(workspace, relpath)
    except Exception:
        return None
    if p is None:
        return None
    try:
        return Path(p) if Path(p).is_file() else None
    except OSError:
        return None


#: The anchor grammar — the criterion's OWN parser.
#:
#: ``body_anchor := "L" start "-" end [ ":p" part ] [ "+S" seg ("," seg)* ]``
#: ``seg         := start "-" end [ "~" tailblanks ]``
#:
#: This is deliberately NOT ``body_pointer.parse_anchor``: a criterion
#: that calls the object under test's parser to decide what the object
#: under test produced is a tautology.  It is spelled out here so the
#: two can disagree.
ANCHOR_RE = re.compile(
    r"^L(?P<start>\d+)-(?P<end>\d+)"
    r"(?::p(?P<part>\d+))?"
    r"(?:\+S(?P<segs>\d+-\d+(?:~\d+)?(?:,\d+-\d+(?:~\d+)?)*))?$")


def _rebuild_independently(path: Path, anchor: str) -> Optional[str]:
    """Rebuild a fragment from the truth file — the ACCEPTANCE side's own
    reader.

    This is deliberately NOT the product's ``body_pointer`` reader: a
    criterion that calls the code it is judging is a tautology.  The
    grammar is small and it is spelled out here.

    **MEASURED (2026-11-01): the first version of this reader used
    ``\\+S\\d+-(.*)`` — it consumed the FIRST segment's start number as
    part of the match and then dropped it, so every multi-segment
    anchor was rebuilt from its SECOND segment onwards.**  The symptom
    was 129 rows reported as "drifted" when only 1 actually was.  A
    reader that loses a segment is worse than no reader: it manufactures
    drift.  The grammar below is the exact one the acceptance side
    already uses (``phase2_s3h.ANCHOR_RE``), re-implemented here so the
    two can be compared.
    """
    if not anchor:
        return None
    m = ANCHOR_RE.match(anchor)
    if not m:
        return None
    start = int(m.group("start"))
    end = int(m.group("end"))
    if start < 1 or end < start:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    if not lines:
        return None
    seg_text = m.group("segs")
    if seg_text is None:
        # ONE contiguous span, read VERBATIM.
        return "".join(lines[start - 1:min(end, len(lines))])
    segs: List[Tuple[int, int]] = []
    for raw in seg_text.split(","):
        raw = raw.strip()
        if "~" in raw:
            raw = raw.rsplit("~", 1)[0]
        if "-" not in raw:
            return None
        a_text, b_text = raw.split("-", 1)
        try:
            a, b = int(a_text), int(b_text)
        except ValueError:
            return None
        if a < 1 or b < a:
            return None
        segs.append((a, b))
    if not segs:
        return None
    parts: List[str] = []
    for a, b in segs:
        if a < 1 or a > len(lines):
            return None
        parts.append("".join(lines[a - 1:min(b, len(lines))]).rstrip())
    return "\n\n".join(parts)


def _canonical(text: Optional[str]) -> str:
    if text is None:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in t.split("\n")).strip()


def _drift_rows(workspace: Path, db: Path) -> Tuple[int, int]:
    """(judgeable rows, rows whose rebuilt text differs) for one library.

    The rows are the ones that carry a pointer AND an anchor — the rows
    a rebuild can speak about.  The reader is this module's own.

    **MEASURED (2026-11-01): the first version counted only 113
    judgeable rows across the fleet, while the architect's independent
    measurement had 2977.**  The gap was NOT a disagreement about the
    world — it was that this module's reader could not rebuild a
    MULTI-SEGMENT anchor (``+S``), so every multi-segment row was
    silently skipped and reported as "not judgeable".

    That is the same class of defect as the first reader bug, one level
    up: **a reader that cannot speak about a row reports the row as
    clean.**  The fix is to make the segment sequence actually work
    (the grammar was already there; the rebuild branch was not wired
    to it), and to REPORT the split so the reader's coverage is
    visible:

        (judgeable rows, rows whose rebuilt text differs,
         rows skipped because the anchor could not be rebuilt)

    The third number is what tells a reader "the drift check covered
    2977 of 2977 rows" versus "it covered 113 and I do not know about
    the other 2864".
    """
    judgeable = 0
    differ = 0
    unreadable = 0
    try:
        conn = open_ro(db)
    except Exception:
        return 0, 0, 0
    try:
        rows = conn.execute(
            "SELECT path, body_anchor, text FROM chunks "
            "WHERE body_path IS NOT NULL AND body_path<>'' "
            "AND body_anchor IS NOT NULL AND body_anchor<>''").fetchall()
    except sqlite3.Error:
        return 0, 0, 0
    finally:
        conn.close()
    for path, anchor, text in rows:
        tf = _truth_file(workspace, path)
        if tf is None:
            unreadable += 1
            continue
        got = _rebuild_independently(tf, anchor)
        if got is None:
            unreadable += 1
            continue
        judgeable += 1
        if _canonical(got) != _canonical(text or ""):
            differ += 1
    return judgeable, differ, unreadable


def judge_dr6_file_changed_not_recut(report, workspace: Path):
    """DR-6 (REAL): the file changed AFTER it was cut — the index is stale.

    **This is the dimension the fingerprint cannot see.**  ``XF`` says
    the libraries have the same SHAPE (columns / constraints /
    triggers / guard tables / indexes / chunker version / value
    domains).  It says NOTHING about whether the indexed text still
    equals the file it was cut from.  A library can be perfectly
    uniform and perfectly stale at the same time — and that is exactly
    the state this workspace is in.

    The measurement, per library:

      * for every path the library points at, compare the TRUTH FILE's
        mtime against the newest ``chunks.created_at`` for that path.
        The file being NEWER means the file was edited after the cut;
      * the drifted ROWS are the pointed rows whose fragment, rebuilt
        from the truth file by this module's OWN reader, differs from
        ``chunks.text``.

    Both numbers are reported, per library, per path, with the age of
    the gap in seconds.

    **Zero pointed rows ⇒ NOTRUN.**
    """
    detail: List[str] = []
    workspace = Path(workspace)
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "DR-6(REAL)",
            "DR-6 文件已改但索引未重切：真源 mtime 晚于切块时间 ⇒ 投影已陈旧",
            "本工作区没有活库，前置条件不成立（本判据未测量）")

    total_files = 0
    total_rows = 0
    total_judgeable = 0
    total_unreadable = 0
    lib_summaries: List[str] = []
    per_lib_rows: Dict[str, int] = {}
    for lib in libs:
        try:
            conn = open_ro(lib.db)
        except Exception as exc:  # noqa: BLE001
            detail.append("  %-22s 读不到: %s" % (lib.name, exc))
            continue
        try:
            paths = [r[0] for r in conn.execute(
                "SELECT DISTINCT path FROM chunks "
                "WHERE body_path IS NOT NULL AND body_path<>'' ORDER BY path")]
            drifted: List[Tuple[str, int, int]] = []
            for p in paths:
                tf = _truth_file(lib.workspace, p)
                if tf is None:
                    continue
                mt = _mtime(tf)
                row = conn.execute(
                    "SELECT MAX(created_at) FROM chunks WHERE path=? "
                    "AND body_path IS NOT NULL", (p,)).fetchone()
                created = int(row[0]) if row and row[0] is not None else None
                if mt is None or created is None:
                    continue
                if mt > created:
                    drifted.append((p, int(mt), created))
        except sqlite3.Error as exc:
            detail.append("  %-22s 读失败: %s" % (lib.name, exc))
            drifted = []
        finally:
            conn.close()

        judgeable, differ, unreadable = _drift_rows(lib.workspace, lib.db)
        per_lib_rows[lib.name] = differ
        total_judgeable += judgeable
        total_unreadable += unreadable
        total_rows += differ
        total_files += len(drifted)
        if drifted or differ:
            lib_summaries.append(
                "  %-22s 已改未重切的文件=%d  可判定行=%d  内容漂移行=%d  "
                "读不动的行=%d" % (lib.name, len(drifted), judgeable, differ,
                                   unreadable))
            for p, mt, created in drifted[:8]:
                detail.append("      %-56s mtime=%s 切块=%s  落后 %ds"
                              % (p, _fmt_ts(mt), _fmt_ts(created), mt - created))
        else:
            lib_summaries.append(
                "  %-22s 已改未重切的文件=%d  可判定行=%d  内容漂移行=%d  "
                "读不动的行=%d" % (lib.name, len(drifted), judgeable, differ,
                                   unreadable))

    detail.append("")
    for line in lib_summaries:
        detail.append(line)
    detail.append("")
    detail.append("合计: 已改未重切的文件=%d（跨库去重前）  可判定行=%d  "
                  "内容漂移行=%d  读不动的行=%d"
                  % (total_files, total_judgeable, total_rows, total_unreadable))
    if total_unreadable:
        detail.append("  ⚠ 有 %d 行读不动（真源文件找不到或锚点无法重建）——"
                      "这些行【没有被检验过】，不能算「干净」。"
                      % total_unreadable)
    detail.append("")
    detail.append("与 XF 的关系:")
    detail.append("  · XF（跨端指纹）测的是【结构】：列/约束/触发器文本/守卫表/索引/"
                  "chunker/取值域。")
    detail.append("  · DR-6 测的是【内容】：投影里的文字是否还等于它被切出来的那份文件。")
    detail.append("  · 两者【不能互相替代】：结构一致但内容陈旧，是同时成立的；")
    detail.append("    XF=PASS 不构成「投影是新的」的任何证据。")
    return report.judge(
        "DR-6(REAL)",
        "DR-6 文件已改但索引未重切：真源 mtime 晚于切块时间 ⇒ 投影已陈旧",
        total_rows == 0, "\n".join(detail))


# ---------------------------------------------------------------------------
# DR-7 · the fingerprint does not substitute for the drift check
# ---------------------------------------------------------------------------

def judge_dr7_fingerprint_does_not_substitute_drift(report, workspace: Path):
    """DR-7 (REAL): the proof that XF and DR-6 measure DIFFERENT objects.

    This criterion does not measure anything new.  It puts the two
    verdicts SIDE BY SIDE and checks the combination that matters:

      * **XF = PASS and DR-6 = FAIL** ⇒ the fleet is uniform AND the
        content is stale.  This is the false green: a green fingerprint
        says nothing about the content, and this criterion says so out
        loud, with the numbers.
      * **XF = FAIL and DR-6 = PASS** ⇒ the fleet's shape forked but the
        content is fine.  Also a combination a single measure cannot
        express.

    **Either library missing ⇒ NOTRUN.**
    """
    detail: List[str] = []
    workspace = Path(workspace)
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "DR-7(REAL)",
            "DR-7 指纹与漂移不可互替：XF 的结构结论不能代替 DR-6 的内容结论",
            "本工作区没有活库，前置条件不成立（本判据未测量）")

    try:
        from agent.memory.fleet_check import judge_fleet_uniformity
        fleet = judge_fleet_uniformity(workspace)
    except Exception as exc:  # noqa: BLE001
        return report.judge_notrun(
            "DR-7(REAL)",
            "DR-7 指纹与漂移不可互替：XF 的结构结论不能代替 DR-6 的内容结论",
            "实现侧不可用（%s: %s）—— 本判据读语义锚点" % (type(exc).__name__, exc))

    xf_verdict = fleet.get("verdict")
    xf_dims = fleet.get("differing_dimensions") or []
    groups = fleet.get("groups") or fleet.get("tokens") or {}
    detail.append("XF（跨端指纹 · 结构）: %s" % xf_verdict)
    detail.append("  不同的维度: %s" % (xf_dims or "无"))
    if isinstance(groups, dict):
        for fp, names in groups.items():
            detail.append("  %s → %d 个库" % (fp, len(names)))

    total_rows = 0
    total_files = 0
    total_judgeable = 0
    total_unreadable = 0
    for lib in libs:
        judgeable, differ, unreadable = _drift_rows(lib.workspace, lib.db)
        total_rows += differ
        total_judgeable += judgeable
        total_unreadable += unreadable
    detail.append("")
    detail.append("DR-6（内容漂移 · 内容）: 漂移行=%d  可判定行=%d  读不动的行=%d"
                  % (total_rows, total_judgeable, total_unreadable))

    if xf_verdict == "PASS" and total_rows:
        detail.append("")
        detail.append("★ 组合: XF=PASS 而 DR-6=FAIL")
        detail.append("  指纹一致（结构一致）并不等于内容一致（投影是新的）。")
        detail.append("  这就是「结构一致但内容漂移」的可判定证据 —— 两种测量不可互替。")
    elif xf_verdict == "FAIL" and not total_rows:
        detail.append("")
        detail.append("★ 组合: XF=FAIL 而 DR-6=PASS —— 结构分叉但内容未漂移，")
        detail.append("  同样是单一测量表达不出来的组合。")
    else:
        detail.append("")
        detail.append("当前组合: XF=%s / DR-6=%s —— 两者同向，"
                      "但它们测的仍是不同的对象。"
                      % (xf_verdict, "FAIL" if total_rows else "PASS"))

    ok = not (xf_verdict == "PASS" and total_rows)
    return report.judge(
        "DR-7(REAL)",
        "DR-7 指纹与漂移不可互替：XF 的结构结论不能代替 DR-6 的内容结论",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# the monitor's state (the INCREMENT)
# ---------------------------------------------------------------------------

def monitor_state_path(workspace: Path) -> Path:
    return Path(workspace) / "memory" / ".audit" / MONITOR_STATE_NAME


def load_monitor_state(workspace: Path) -> Dict[str, Any]:
    p = monitor_state_path(workspace)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_monitor_state(workspace: Path, state: Dict[str, Any]) -> Optional[str]:
    """Persist the monitor's state.  Returns the path, or None on failure.

    This writes ONE json file in the workspace's audit directory.  It
    does NOT touch any library: the monitor must not modify the object
    it measures.
    """
    p = monitor_state_path(workspace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return str(p)
    except OSError:
        return None


def record_monitor_state(workspace: Path, census: Dict[str, WriterCensus],
                         now: Optional[int] = None) -> Dict[str, Any]:
    """Write the census into the monitor's state and return the INCREMENT.

    The increment is the difference between this run and the previous
    one: how many paths were newly damaged, and how many stopped being
    damaged.  Without it the monitor can only ever say "there is
    damage" — it cannot say whether the debt is GROWING.
    """
    now = int(now if now is not None else time.time())
    prev = load_monitor_state(workspace)
    prev_libs = prev.get("libraries", {}) if isinstance(prev, dict) else {}

    snapshot: Dict[str, Any] = {}
    increment: Dict[str, Any] = {}
    for name, c in census.items():
        paths = sorted([p for (p, _a, _b, _t) in c.deleted]
                       + [p for (p, _a, _b, _x, _y, _t) in c.downgraded])
        snapshot[name] = {
            "damaged_paths": paths,
            "deleted": len(c.deleted),
            "downgraded": len(c.downgraded),
            "last_damage_at": c.last_damage_at,
            "oldest_damage_at": c.oldest_damage_at,
            "seen_at": now,
        }
        before = set(prev_libs.get(name, {}).get("damaged_paths", []))
        after = set(paths)
        increment[name] = {
            "new_paths": sorted(after - before),
            "cleared_paths": sorted(before - after),
            "new_count": len(after - before),
            "cleared_count": len(before - after),
            "total": len(after),
        }
    state = {"version": 1, "at": now, "libraries": snapshot}
    save_monitor_state(workspace, state)
    return {"state": state, "increment": increment,
            "first_run": not prev_libs}


#: The criteria this module contributes, in the order the runner uses.
CRITERIA = [
    ("DR-1", "judge_dr1_dual_runtime_is_detected"),
    ("DR-2", "judge_dr2_change_provenance"),
    ("DR-3", "judge_dr3_injection_composition"),
    ("DR-4", "judge_dr4_emotion_continuity"),
    ("DR-5", "judge_dr5_baseline_conformance"),
    ("DR-6", "judge_dr6_file_changed_not_recut"),
    ("DR-7", "judge_dr7_fingerprint_does_not_substitute_drift"),
]
