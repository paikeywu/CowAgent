"""``recut-safe`` — the behavioural assertion that guards the re-cut.

**What this replaces.**  The re-cut used to be gated by
``--require-old-runtime-retired``: "the old desktop build must be gone".
That assertion judged a PROXY — a process being alive — instead of the
thing it was protecting.  The invariant it was protecting is:

> **I-RECUT-SAFE** — after a re-cut, the re-cut file's ``files``
> statistics columns and the ``chunks`` body columns must not be degraded
> to ``0`` / ``NULL`` / ``'unresolved'`` by a legacy writer.

The architect's ruling (2026-11-01 · 缺口收口 · 漂移族裁决 §2.3) replaces
the proxy with the invariant, as a four-part behavioural formula:

    S1 (guard in place) ∧ S2 (guard effective) ∧ (S3 (old build writes
    nothing) ∨ S4 (old build cannot reach the live libraries))
        ⇒ the re-cut is allowed

**Why S2 is a RESIDENT probe and not a one-shot.**  S3 and S4 are
statements about NOW — they can go stale the moment the probe returns.
S2 is a statement about the LIBRARY: if the guard's triggers are on the
library and they actually hold the protected columns, then a legacy
write CANNOT degrade the boundary, whenever it happens.  So S2 does not
decay with time and does not have to be re-measured per sweep; it has to
be re-measured per LIBRARY.  **Changing the assertion ≠ removing the
protection.**

**Why every arm has a CONTROL.**  A probe that cannot see a broken guard
is a probe that always says "protected".  So each S2 measurement runs
twice on a copy: once with the guard, once with the guard DROPPED.  If
the two arms agree, the probe is blind and the result is void — that is
the discipline this repo already uses for the mutation suite.

**This module NEVER writes to a live library.**  Every measurement runs
on a ``tempfile`` copy.  The only thing it reads from the live library
is its SCHEMA (the trigger names), which is read-only.

**What it reports.**  Every arm reports ``pass`` / ``fail`` / ``notrun``
plus the EVIDENCE for that verdict, and the whole report carries one
line saying what the verdict is about — so a reader can never mistake
"the old build is running" for "the re-cut is unsafe".
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

#: The old desktop build.  Read-only identification.
OLD_BUILD_DIR = Path(r"C:\AI\CowAgent")
OLD_BUILD_EXECUTABLES = ("CowAgent.exe", "cowagent-backend.exe")

#: The port the old backend listens on.
OLD_BUILD_PORT = 9876

#: The default observation window for S3, in seconds.  120 s is the
#: window the architect's own probes used and the shortest one that
#: distinguishes "quiet" from "happened to not write right now".
DEFAULT_S3_WINDOW = 120

#: The sampling interval of the S3 window.
DEFAULT_S3_SAMPLE = 30

#: The tables whose ROW CONTENT is digested for S3.  A row-level digest
#: (not a file hash) is the only thing that sees a WAL-committed write:
#: in WAL mode a real write changes the sidecar, not the main file.
S3_TABLES = (
    ("chunks", "id, path, text, body_origin, text_state, created_at, "
               "updated_at"),
    ("files", "path, hash, mtime, size, chunk_count, chunker_version, "
              "updated_at"),
    ("cert_ledger", "id, body_path, body_hash, certified_at"),
    ("messages", "id, role, length(content), created_at"),
)

#: The verdict's one-line self-report.  The architect's rule: a verdict
#: must say what it MEASURED, so nobody reads "the old build is alive"
#: as "the re-cut is unsafe".
MEASURES = ("本次判定的是「写入边界不可被降级」（S1∧S2∧(S3∨S4)），"
            "不是「旧端是否运行」")


# ---------------------------------------------------------------------------
# S1 · the guard is in place
# ---------------------------------------------------------------------------

def guard_names() -> Tuple[str, ...]:
    """The guard's own trigger list — the ONE definition."""
    from agent.memory.body_guard import guard_trigger_names
    return guard_trigger_names()


def probe_s1_guard_in_place(db: Union[str, Path]) -> Dict[str, Any]:
    """**S1** — the guard's triggers are on this library.

    "In place" means BY NAME.  The bodies are S2's business: a trigger
    can be present and still be the wrong version, and a trigger that is
    present but wrong-bodied is exactly what a column-set fingerprint
    cannot see (the repo's own §1.1 finding: the trigger NAMES were
    identical across 16 libraries while the BODIES differed in 14).
    """
    out: Dict[str, Any] = {"arm": "S1", "name": "guard in place",
                           "verdict": "NOTRUN", "missing": [], "detail": ""}
    path = Path(db)
    if not path.is_file():
        out["detail"] = "the index does not exist"
        return out
    try:
        conn = sqlite3.connect("file:%s?mode=ro"
                                % str(path).replace("\\", "/"), uri=True)
    except sqlite3.Error as e:
        out["detail"] = "cannot open the index read-only: %s" % e
        return out
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
    except sqlite3.Error as e:
        out["detail"] = "cannot read sqlite_master: %s" % e
        return out
    finally:
        conn.close()
    wanted = guard_names()
    missing = [n for n in wanted if n not in have]
    out["missing"] = missing
    out["wanted"] = list(wanted)
    out["present"] = len(wanted) - len(missing)
    out["verdict"] = "PASS" if not missing else "FAIL"
    out["detail"] = ("%d/%d guard trigger(s) present%s"
                     % (out["present"], len(wanted),
                        ("" if not missing
                         else "; missing: " + ", ".join(missing))))
    return out


# ---------------------------------------------------------------------------
# S2 · the guard is EFFECTIVE
# ---------------------------------------------------------------------------

def _files_probe_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """A ``files`` row that has MEASURED statistics (not NULL, not 0)."""
    try:
        return conn.execute(
            "SELECT path, chunk_count, chunker_version FROM files "
            "WHERE chunk_count IS NOT NULL AND chunker_version IS NOT NULL "
            "AND chunk_count > 0 LIMIT 1").fetchone()
    except sqlite3.Error:
        return None


def _chunks_probe_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """A ``chunks`` row that carries a pointer (the four body columns)."""
    try:
        return conn.execute(
            "SELECT id, path, body_path, body_anchor, body_hash, text_state, "
            "body_origin FROM chunks WHERE body_path IS NOT NULL LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None


def _legacy_files_insert(conn: sqlite3.Connection, path: str) -> None:
    """The old build's SIX-column ``files`` INSERT."""
    conn.execute(
        "INSERT OR REPLACE INTO files "
        "(path, source, hash, mtime, size, updated_at) "
        "VALUES (?, 'memory', 'sha256:legacy', 111, 111, 111)", (path,))


def _legacy_chunks_insert(conn: sqlite3.Connection, cid: str) -> None:
    """The old build's TWELVE-column ``chunks`` INSERT (no body columns)."""
    conn.execute(
        "INSERT OR REPLACE INTO chunks "
        "(id, user_id, scope, source, path, start_line, end_line, text, "
        " embedding, hash, metadata, element_released, file_sealed, "
        " sealed_owner) "
        "SELECT id, user_id, scope, source, path, start_line, end_line, "
        " text, embedding, hash, metadata, element_released, file_sealed, "
        " sealed_owner FROM chunks WHERE id=?", (cid,))


def _drop_guard_triggers(conn: sqlite3.Connection) -> int:
    """Drop the guard's triggers from a connection (the CONTROL arm).

    **This exists as a named seam on purpose.**  The control arm is what
    makes S2 meaningful — a probe that cannot see a broken guard must
    say so — so the control arm must itself be testable.  A monkeypatch
    that reaches into ``sqlite3.connect`` cannot set ``row_factory``
    correctly; a named function can be replaced wholesale.
    """
    dropped = 0
    for name in guard_names():
        cur = conn.execute("DROP TRIGGER IF EXISTS %s" % name)
        dropped += max(cur.rowcount, 0)
    return dropped


def probe_s2_guard_effective(db: Union[str, Path]) -> Dict[str, Any]:
    """**S2** — the guard actually holds the protected columns.

    **Runs on a COPY.**  The live library is never written to.

    **Two arms, and the control is not optional.**  The measurement runs
    once with the guard and once with the guard DROPPED.  If the control
    arm does NOT degrade the columns, the probe cannot see a broken guard
    ⇒ the verdict is NOTRUN ("the probe is blind"), not PASS.  A probe
    that always says "protected" is not a probe.
    """
    out: Dict[str, Any] = {
        "arm": "S2", "name": "guard effective", "verdict": "NOTRUN",
        "files_protected": None, "chunks_protected": None,
        "control_files_degraded": None, "control_chunks_degraded": None,
        "detail": "",
    }
    src = Path(db)
    if not src.is_file():
        out["detail"] = "the index does not exist"
        return out

    tmp = Path(tempfile.mkdtemp(prefix="recut_safe_s2_"))
    try:
        # ---- ARM A: with the guard -------------------------------------
        copy_a = tmp / "with_guard.db"
        shutil.copyfile(src, copy_a)
        conn = sqlite3.connect(str(copy_a))
        conn.row_factory = sqlite3.Row
        try:
            frow = _files_probe_row(conn)
            crow = _chunks_probe_row(conn)
            if frow is None and crow is None:
                out["detail"] = ("this library has neither a measured "
                                 "statistics row nor a pointer row — there "
                                 "is nothing for the guard to protect")
                return out
            if frow is not None:
                before = (frow["chunk_count"], frow["chunker_version"])
                _legacy_files_insert(conn, frow["path"])
                after = conn.execute(
                    "SELECT chunk_count, chunker_version FROM files "
                    "WHERE path=?", (frow["path"],)).fetchone()
                out["files_before"] = list(before)
                out["files_after"] = [after["chunk_count"],
                                      after["chunker_version"]]
                out["files_protected"] = (out["files_after"] == list(before))
            if crow is not None:
                cols = ("body_path", "body_anchor", "body_hash", "text_state",
                        "body_origin")
                before = tuple(crow[k] for k in cols)
                _legacy_chunks_insert(conn, crow["id"])
                row2 = conn.execute(
                    "SELECT %s FROM chunks WHERE id=?" % ", ".join(cols),
                    (crow["id"],)).fetchone()
                after = tuple(row2[k] for k in cols)
                out["chunks_before"] = list(before)
                out["chunks_after"] = list(after)
                out["chunks_protected"] = (after == before)
            conn.commit()
        finally:
            conn.close()

        # ---- ARM B: the CONTROL (the guard dropped) --------------------
        copy_b = tmp / "without_guard.db"
        shutil.copyfile(src, copy_b)
        conn2 = sqlite3.connect(str(copy_b))
        conn2.row_factory = sqlite3.Row
        try:
            _drop_guard_triggers(conn2)
            if frow is not None:
                before = (frow["chunk_count"], frow["chunker_version"])
                _legacy_files_insert(conn2, frow["path"])
                after = conn2.execute(
                    "SELECT chunk_count, chunker_version FROM files "
                    "WHERE path=?", (frow["path"],)).fetchone()
                out["control_files_after"] = [after["chunk_count"],
                                              after["chunker_version"]]
                out["control_files_degraded"] = (
                    [after["chunk_count"], after["chunker_version"]]
                    != list(before))
            if crow is not None:
                cols = ("body_path", "body_anchor", "body_hash", "text_state",
                        "body_origin")
                before = tuple(crow[k] for k in cols)
                _legacy_chunks_insert(conn2, crow["id"])
                row2 = conn2.execute(
                    "SELECT %s FROM chunks WHERE id=?" % ", ".join(cols),
                    (crow["id"],)).fetchone()
                after = tuple(row2[k] for k in cols)
                out["control_chunks_after"] = list(after)
                out["control_chunks_degraded"] = (after != before)
            conn2.commit()
        finally:
            conn2.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- the verdict -------------------------------------------------
    arms = [out.get("files_protected"), out.get("chunks_protected")]
    controls = [out.get("control_files_degraded"),
                out.get("control_chunks_degraded")]
    if all(a is None for a in arms):
        out["detail"] = "nothing measurable on this library"
        return out
    # A control arm that did NOT degrade proves the probe is BLIND.
    measured_controls = [c for c in controls if c is not None]
    if measured_controls and not any(measured_controls):
        out["verdict"] = "NOTRUN"
        out["detail"] = ("the control arm (guard dropped) did NOT degrade "
                         "the columns ⇒ this probe cannot see a broken "
                         "guard ⇒ the verdict is void, not PASS")
        return out
    if any(a is False for a in arms if a is not None):
        out["verdict"] = "FAIL"
        out["detail"] = ("the legacy write degraded a protected column "
                         "with the guard in place")
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("the legacy six-column files INSERT and the legacy "
                     "twelve-column chunks INSERT did NOT change the "
                     "protected columns (control arm degraded them: %s)"
                     % ", ".join("%s=%s" % (k, v) for k, v in zip(
                         ("files", "chunks"), measured_controls)))
    return out


# ---------------------------------------------------------------------------
# S3 · the old build writes nothing
# ---------------------------------------------------------------------------

def _digest_table(conn: sqlite3.Connection, table: str,
                  cols: str) -> Tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    try:
        cur = conn.execute("SELECT %s FROM %s" % (cols, table))
    except sqlite3.Error:
        return ("<no-table>", 0)
    for row in cur:
        h.update(repr(tuple(row)).encode("utf-8", "replace"))
        n += 1
    return (h.hexdigest()[:16], n)


def snapshot_libraries(workspace: Union[str, Path]
                       ) -> Dict[str, Dict[str, Tuple[str, int]]]:
    """A row-level digest of every live library."""
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    out: Dict[str, Dict[str, Tuple[str, int]]] = {}
    for lib in enumerate_live(root):
        db = Path(lib["db"])
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect("file:%s?mode=ro"
                                    % str(db).replace("\\", "/"), uri=True)
        except sqlite3.Error:
            continue
        try:
            out[lib["name"]] = {tbl: _digest_table(conn, tbl, cols)
                                for tbl, cols in S3_TABLES}
        finally:
            conn.close()
    return out


def probe_s3_zero_writes(workspace: Union[str, Path],
                         window: int = DEFAULT_S3_WINDOW,
                         sample: int = DEFAULT_S3_SAMPLE) -> Dict[str, Any]:
    """**S3** — the live libraries do not change over the window.

    **A row-level digest, not a file hash.**  In WAL mode a real write
    changes the sidecar, not the main file ⇒ a main-file hash reports
    "unchanged" for a real write (the repo's own §1.1 finding).  A
    row-level digest sees the committed rows and does not misreport a
    checkpoint (which moves bytes, not rows).

    **The window is the MINIMUM observation, not the only one.**  The
    first snapshot is taken at T0 and compared with the LAST one; the
    window is the time between them.  So a write that happened just
    BEFORE T0 but is still visible in the first snapshot is NOT caught
    by the "changed between snapshots" comparison — and that is the
    right answer for a windowed observation (it says "nothing changed
    during the window"), but it is NOT the answer to "is anything
    writing at all".

    ⇒ So S3 reports **two** facts: the windowed change list (the
    window's answer) and the per-library ``last_write`` age (the
    "is anything writing" answer).  The second one is what catches a
    writer that wrote a moment ago.  A writer that wrote within the
    last ``max_age`` seconds is reported as a FAIL of S3's
    "nothing is writing" reading, because the point of S3 is not "the
    libraries are frozen" — it is "no one is writing them NOW".
    """
    out: Dict[str, Any] = {"arm": "S3", "name": "old build writes nothing",
                           "verdict": "NOTRUN", "window": window,
                           "changed": [], "recent_writes": [],
                           "detail": ""}
    if window <= 0:
        out["detail"] = "the observation window must be positive"
        return out
    step = max(1, min(sample, window))
    before = snapshot_libraries(workspace)
    if not before:
        out["detail"] = "no live library found"
        return out
    changed: List[str] = []
    elapsed = 0
    while elapsed < window:
        time.sleep(step)
        elapsed += step
        after = snapshot_libraries(workspace)
        for name, tables in before.items():
            other = after.get(name, {})
            for tbl, sig in tables.items():
                if other.get(tbl) != sig:
                    changed.append("%s/%s" % (name, tbl))
        before = after
    out["changed"] = sorted(set(changed))

    # --- the "is anything writing NOW" reading ------------------------
    #
    # A write that landed just before the window started is still a
    # write in progress (the old build's writes come in bursts).  The
    # newest ``chunks.created_at`` across the fleet says when the last
    # write was — and if that is inside the freshness bound, somebody
    # wrote recently enough that a concurrent re-cut is not safe.
    recent = recent_writes(workspace, max_age=window)
    out["recent_writes"] = recent
    if changed:
        out["verdict"] = "FAIL"
        out["detail"] = ("%d s window: %d library/table digest(s) changed%s"
                         % (window, len(out["changed"]),
                            ("" if not out["changed"]
                             else " -> " + ", ".join(out["changed"]))))
    elif recent:
        out["verdict"] = "FAIL"
        out["detail"] = ("%d s window: no digest changed, BUT %d library/"
                         "table(s) were written within the last %d s (%s) "
                         "⇒ somebody wrote recently"
                         % (window, len(recent), window,
                            ", ".join("%s@%ss" % (r["library"],
                                                  r["age_seconds"])
                                      for r in recent[:5])))
    else:
        out["verdict"] = "PASS"
        out["detail"] = ("%d s window: %d library/table digest(s) changed; "
                         "no write within the last %d s either"
                         % (window, len(out["changed"]), window))
    return out


def recent_writes(workspace: Union[str, Path],
                  max_age: int = DEFAULT_S3_WINDOW) -> List[Dict[str, Any]]:
    """The libraries that were written within the last ``max_age`` seconds.

    Read-only.  The signal is the newest ``chunks.created_at`` — the
    moment a row was cut — compared against the wall clock.  It is a
    *burst* detector: a writer that wrote a moment ago is still a
    writer.
    """
    import time as _time

    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    now = int(_time.time())
    out: List[Dict[str, Any]] = []
    for lib in enumerate_live(root):
        db = Path(lib["db"])
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect("file:%s?mode=ro"
                                    % str(db).replace("\\", "/"), uri=True)
        except sqlite3.Error:
            continue
        try:
            row = conn.execute(
                "SELECT MAX(created_at) FROM chunks").fetchone()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        newest = row[0] if row else None
        if not newest:
            continue
        try:
            newest_i = int(newest)
        except (TypeError, ValueError):
            continue
        age = now - newest_i
        if age <= max_age:
            out.append({"library": lib["name"], "newest_created_at": newest_i,
                        "age_seconds": age})
    out.sort(key=lambda r: r["age_seconds"])
    return out


# ---------------------------------------------------------------------------
# S4 · the old build cannot reach the live libraries
# ---------------------------------------------------------------------------

def _port_owner_rows() -> List[str]:
    """The processes holding the old build's port."""
    out: List[str] = []
    try:
        res = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in res.stdout.splitlines():
        if ":%d" % OLD_BUILD_PORT in line and "LISTENING" in line.upper():
            out.append(line.strip()[:200])
    return out


def _client_rows() -> List[str]:
    """The ESTABLISHED clients of the old build's port."""
    out: List[str] = []
    try:
        res = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in res.stdout.splitlines():
        if ":%d" % OLD_BUILD_PORT in line and "ESTABLISHED" in line.upper():
            out.append(line.strip()[:200])
    return out


def _pid_exe(pid: int) -> str:
    try:
        res = subprocess.run(
            ["wmic", "process", "where", "ProcessId=%d" % pid, "get",
             "ExecutablePath", "/format:list"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in res.stdout.splitlines():
        if "ExecutablePath=" in line:
            return line.split("=", 1)[1].strip()
    return ""


def _pid_of(line: str) -> int:
    parts = line.split()
    for p in reversed(parts):
        if p.isdigit():
            return int(p)
    return 0


def probe_s4_cannot_reach(workspace: Union[str, Path]) -> Dict[str, Any]:
    """**S4** — the old build is not connected to the live libraries.

    **What it actually measures.**  The old build's own HTTP port: if
    every ESTABLISHED client of that port belongs to the old build's own
    process tree, then the old build is talking to itself, not to the
    live libraries.  A listener that nobody but itself talks to cannot
    be a writer of the live libraries.

    **What it does NOT claim.**  It does not claim the old build cannot
    write the libraries by some other path — that is a different
    question, and the honest answer is that the old build's memory
    subsystem is not present in the frozen bundle at all (measured: the
    frozen PYZ carries no ``agent.memory`` module).  This arm measures
    the network path, and says so.
    """
    out: Dict[str, Any] = {"arm": "S4", "name": "old build cannot reach the "
                           "live libraries", "verdict": "NOTRUN",
                           "listener": [], "clients": [], "detail": ""}
    listeners = _port_owner_rows()
    clients = _client_rows()
    out["listener"] = listeners
    out["clients"] = clients
    if not listeners:
        out["verdict"] = "PASS"
        out["detail"] = ("nothing listens on %d ⇒ there is no old-build "
                         "endpoint that could be a writer" % OLD_BUILD_PORT)
        return out
    # Whose process is the listener?  And whose are the clients?
    listener_pids = {_pid_of(l) for l in listeners}
    listener_exes = {p: _pid_exe(p) for p in listener_pids}
    out["listener_exe"] = {str(k): v for k, v in listener_exes.items()}
    client_pids = {_pid_of(c) for c in clients}
    client_exes = {p: _pid_exe(p) for p in client_pids}
    out["client_exe"] = {str(k): v for k, v in client_exes.items()}
    old_exes = {str(OLD_BUILD_DIR).lower()}
    foreign = []
    for pid, exe in client_exes.items():
        if not exe:
            continue
        low = exe.lower()
        if not any(low.startswith(e) for e in old_exes):
            foreign.append("%s(pid=%s)" % (exe, pid))
    out["foreign_clients"] = foreign
    if not clients:
        out["verdict"] = "PASS"
        out["detail"] = ("something listens on %d but NOBODY is connected "
                         "to it ⇒ it is not currently a writer"
                         % OLD_BUILD_PORT)
        return out
    if foreign:
        out["verdict"] = "FAIL"
        out["detail"] = ("%d client(s) of %d do NOT belong to the old "
                         "build's tree: %s ⇒ the port can reach the live "
                         "libraries" % (len(foreign), OLD_BUILD_PORT,
                                        ", ".join(foreign)))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("all %d client(s) of %d belong to the old build's own "
                     "process tree ⇒ the old build talks to itself, not to "
                     "the live libraries" % (len(clients), OLD_BUILD_PORT))
    return out


# ---------------------------------------------------------------------------
# the family: one verdict for the whole workspace
# ---------------------------------------------------------------------------

def judge_recut_safe(workspace: Union[str, Path],
                     window: int = DEFAULT_S3_WINDOW,
                     sample: int = DEFAULT_S3_SAMPLE,
                     ) -> Dict[str, Any]:
    """The ``recut-safe`` criterion over every live library.

    The verdict is the formula, and the report says which term failed:
    ``S1 ∧ S2 ∧ (S3 ∨ S4)``.
    """
    from agent.memory.migrate import enumerate_live

    root = Path(workspace)
    detail: List[str] = []
    libs = enumerate_live(root)
    detail.append("受测库: %d 个" % len(libs))
    detail.append("判定对象: %s" % MEASURES)
    detail.append("")

    s1: Dict[str, Any] = {}
    s2: Dict[str, Any] = {}
    for lib in libs:
        s1[lib["name"]] = probe_s1_guard_in_place(lib["db"])
        s2[lib["name"]] = probe_s2_guard_effective(lib["db"])

    s1_fail = [n for n, r in s1.items() if r["verdict"] == "FAIL"]
    s1_nr = [n for n, r in s1.items() if r["verdict"] == "NOTRUN"]
    s2_fail = [n for n, r in s2.items() if r["verdict"] == "FAIL"]
    s2_nr = [n for n, r in s2.items() if r["verdict"] == "NOTRUN"]

    detail.append("S1 守卫在位: %d/%d 库齐全（FAIL %d · NOTRUN %d）"
                  % (len(libs) - len(s1_fail) - len(s1_nr), len(libs),
                     len(s1_fail), len(s1_nr)))
    for n in s1_fail:
        detail.append("  ! %s: %s" % (n, s1[n]["detail"]))
    for n in s1_nr:
        detail.append("  ? %s: %s" % (n, s1[n]["detail"]))
    detail.append("S2 守卫有效: %d/%d 库守住（FAIL %d · NOTRUN %d）"
                  % (len(libs) - len(s2_fail) - len(s2_nr), len(libs),
                     len(s2_fail), len(s2_nr)))
    for n in s2_fail:
        detail.append("  ! %s: %s" % (n, s2[n]["detail"]))
    for n in s2_nr:
        detail.append("  ? %s: %s" % (n, s2[n]["detail"]))
    detail.append("")

    s3 = probe_s3_zero_writes(root, window=window, sample=sample)
    s4 = probe_s4_cannot_reach(root)
    detail.append("S3 旧端零写入（观察窗 %ds）: %s —— %s"
                  % (s3.get("window"), s3.get("verdict"), s3.get("detail")))
    detail.append("S4 旧端连不到活库: %s —— %s"
                  % (s4.get("verdict"), s4.get("detail")))
    detail.append("")

    s12_ok = (not s1_fail and not s1_nr and not s2_fail and not s2_nr)
    s34_ok = (s3["verdict"] == "PASS" or s4["verdict"] == "PASS")

    if s12_ok and s34_ok:
        verdict = "PASS"
        detail.append("判定式 S1 ∧ S2 ∧ (S3 ∨ S4) ⇒ 成立 ⇒ 允许重切。")
    elif not s12_ok:
        verdict = "FAIL"
        detail.append("判定式不成立：S1∧S2 有一项不过 ⇒ 禁止重切。")
    else:
        verdict = "FAIL"
        detail.append("判定式不成立：S3 与 S4 同时不过 ⇒ 禁止重切。")

    return {
        "criterion": "RECUT-SAFE",
        "name": "重切安全：写入边界不可被降级（S1∧S2∧(S3∨S4)）",
        "workspace": str(root),
        "measures": MEASURES,
        "verdict": verdict,
        "library_count": len(libs),
        "s1": s1, "s2": s2, "s3": s3, "s4": s4,
        "formula": {"S1": not s1_fail and not s1_nr,
                    "S2": not s2_fail and not s2_nr,
                    "S3": s3["verdict"] == "PASS",
                    "S4": s4["verdict"] == "PASS"},
        "detail": "\n".join(detail),
    }


def format_report(report: Dict[str, Any]) -> str:
    return report.get("detail", "")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description="The recut-safe criterion: is a re-cut allowed?  It "
                    "measures the WRITE BOUNDARY, not whether the old "
                    "build is running.")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--window", type=int, default=DEFAULT_S3_WINDOW,
                    help="the S3 observation window in seconds "
                         "(default %d)" % DEFAULT_S3_WINDOW)
    ap.add_argument("--sample", type=int, default=DEFAULT_S3_SAMPLE,
                    help="the S3 sampling interval in seconds")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = judge_recut_safe(args.workspace, window=args.window,
                               sample=args.sample)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
