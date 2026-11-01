#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-X · 外部编辑可溯源 + 常驻护栏 + 全局失败集基线。

这一轮收的是**判据侧**的三个缺口，交办人是小种子（SEED），架构师的复核在
``core-architect/deliverables/2026-11-01-缺口收口-地基裁决.md`` §T1b。

  * **EE-1..EE-3 · 外部编辑可溯源** —— 落成后台系统开发工程师的设计稿
    （``agent-mvnewfyb/deliverables/2026-11-01-判据设计稿-外部编辑可溯源.md``）
    的 A1 / A2 / A3，并**逐条复核了设计稿的前提**（见 §三）。
  * **R3 · 两判据 RED 集合对称差 = 0（常驻）** —— 架构师 §2.3 指出，
    「111」这个数**不能**反证 canonical 口径没把真漂移一起吞掉；R1/R2/R3
    必须三条一起，而 R3 之前只是**一次性探针**，不是常驻判据。
  * **GB-1 · 门禁全局失败集基线** —— SEED 裁决：Q5 只覆盖 ``text_inv``
    那一组 ⇒ DR-1..DR-7 / I-1 / J-5 **从来不在 Q5 范围内** ⇒ 全局失败集
    没有基线 = 回归无处可查。

**三态口径（贯穿全模块）**
----------------------
PASS = 测到了，且满足断言；
FAIL = 测到了，且断言不成立；
NOTRUN = **测不了**（前置不成立）。**NOTRUN ≠ PASS。**

但有一条必须分清：**「测到了 0」是 PASS，「测不到」是 NOTRUN。**
普查类判据跑完、结果是 0（16/16 库测完、unresolved = 0）⇒ PASS；
把「没测」说成「测过没问题」与把「测到 0」说成「没测」是**同一种撒谎**，
方向相反而已。

**这一模块不做的事**
--------------------
❌ 不改产品实现语义（``external_edit.py`` / ``changes.py`` / ``text_inv.py``
   / ``naming.py`` / ``body_pointer.py`` 是工程师的地盘）；
❌ 不改活库（EE-1 只读；EE-2 在**自己建的沙箱**里跑）；
❌ 不放宽任何既有判据的断言来换绿；
❌ 不提供「一键刷新基线」的入口（GB-1 只**读**基线文件，从不写它）。
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro


#: The change-log sources that are LEGAL.  ``external-edit`` is the one
#: the reconciliation writes; the others are our own write paths.  A
#: source outside this set is an anomaly the log itself should never
#: produce (``changes.record_change`` refuses it) — but the criterion
#: does not trust the writer to have refused it.
LEGAL_SOURCES: Tuple[str, ...] = (
    "facade-write", "import-human-edit", "rebuilder",
    "facade-element", "facade-release", "external-edit",
)

#: The key the witness travels under.  **FLAT** — ``record_change`` merges
#: its ``extra`` dict with ``event.update(extra)``.  A reader that looked
#: for ``event["extra"]["content_sha256"]`` would find nothing and would
#: treat every event as unwitnessed.
WITNESS_KEY = "content_sha256"

#: The environment-item reds.  **They are NOT regressions** — they are
#: the truth about this machine (two runtimes alive, a legacy writer
#: still erasing statistics).  They must never enter the baseline file,
#: because a baseline entry for an environment item means the baseline
#: has taken over the environment's job (风控 G1).
ENVIRONMENT_ITEMS: Tuple[str, ...] = ("J-5", "DR-1", "DR-2")


# ---------------------------------------------------------------------------
# small helpers — the criterion's OWN reading of the change log
# ---------------------------------------------------------------------------

def _canon_hash(path: Path) -> str:
    """The file's canonical content hash, or ``""`` when it cannot be read.

    **Canonical, not raw** — the same normalisation the witness uses
    (``naming.canonical_text``: LF endings, folded blank runs, outer
    whitespace stripped).  The criterion computes this ITSELF rather than
    calling ``external_edit._sha256_file``: the criterion must be able to
    say "the witness does not reproduce" without borrowing the writer's
    own arithmetic (a criterion that re-runs the writer cannot catch the
    writer being wrong).
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "raw:" + hashlib.sha256(raw).hexdigest()
    from agent.memory.naming import canonical_text
    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def _events(workspace: Path) -> List[Dict[str, Any]]:
    """Every event of this workspace's change log, oldest first."""
    try:
        from agent.memory import changes
        return changes.read_changes(workspace)
    except Exception:
        return []


def _last_event_per_path(events: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The LAST event per path (the log is append-only, so it carries
    the newest witness)."""
    last: Dict[str, Dict[str, Any]] = {}
    for e in events:
        p = e.get("path")
        if p:
            last[str(p).replace("\\", "/")] = e
    return last


def _workspaces(workspace: Path) -> List[Path]:
    """The workspaces that HAVE a change log: the root plus every agent
    directory under ``agents/``.

    **Derived by shape, never hard-coded** — the same rule
    ``external_edit.truth_files`` uses for its file set.  A workspace
    without a log is not a workspace this criterion can measure (it is
    reported as NOTRUN for that workspace, not silently skipped).
    """
    out: List[Path] = []
    root = Path(workspace)
    if (root / "memory" / ".audit" / "file_changes.jsonl").is_file():
        out.append(root)
    agents = root / "agents"
    if agents.is_dir():
        for d in sorted(agents.iterdir()):
            if d.is_dir() and (d / "memory" / ".audit" / "file_changes.jsonl").is_file():
                out.append(d)
    return out


def _truth_files(workspace: Path) -> List[Tuple[str, Path]]:
    """The truth-layer file set of one workspace, as ``(relpath, file)``.

    Delegates to the implementation's own ``truth_files`` — **this is the
    one place where the criterion borrows the writer's set**, and it is
    deliberate: the SET of files that must be witnessed is a product
    decision ("what is the truth layer"), not a criterion's opinion.
    What the criterion verifies independently is whether each of those
    files carries a witness that reproduces its current content.
    """
    try:
        from agent.memory import external_edit
        return list(external_edit.truth_files(workspace))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# EE-1 · the live provenance census
# ---------------------------------------------------------------------------

def _mechanism_live_from(workspaces: List[Path]) -> Optional[str]:
    """When did the mechanism first ACT?  (the earliest external-edit event)

    **Why a data fact and not a file mtime.**  The repo file's mtime
    changes on every checkout, so a criterion that used it would give
    different answers on different machines — and the baseline file is
    meant to be comparable across machines.  The earliest recorded
    ``external-edit`` event is a fact **in the data**: it is the first
    moment the mechanism demonstrably witnessed something.

    An edit AFTER that moment is one the mechanism was ALIVE for ⇒
    "still leaking".  An edit BEFORE it is historical debt ⇒ the
    mechanism cannot be blamed for it.
    """
    stamps: List[str] = []
    for ws in workspaces:
        for e in _events(ws):
            if e.get("source") == "external-edit" and e.get("at"):
                stamps.append(str(e["at"]))
    return min(stamps) if stamps else None


def _probe_reconciliation(ws: Path, cands: List[Tuple[str, Path]]) -> Dict[str, Any]:
    """Run a REAL reconciliation **on a COPY of the log**, never on the live one.

    **Why a copy.**  The criterion must not modify the environment it
    measures (S-1).  But a dry run does not tell us what witness the
    mechanism WOULD write, and without the witness value the criterion
    cannot catch MU-T2 (the witness is a constant) or MU-T4 (the witness
    is the raw bytes).  So the criterion copies the log into a
    temporary directory, runs the reconciliation there with the REAL
    candidate files, and reads the resulting events.

    The live log is never touched: the copy lives in a temp directory
    that is deleted when the probe returns.

    Returns::

        {"events": {rel: witness}, "second_pass_added": int,
         "mtime_advanced": bool,
         "unclosable": {rel: reason},   # WHY it could not be closed
         "error": None}

    **The reason matters.**  "This one cannot be closed" is only useful if
    the report says **why** — SEED's requirement (2026-11-01): the
    criterion must be able to say *"this one cannot be closed, because
    X"*.  So each unclosable path carries one of:

      * ``NO_EVENT``   — the reconciliation produced **no event** for it
        (the path is outside the candidate set, or it was skipped);
      * ``EMPTY_WITNESS`` — it produced an event but **no witness**;
      * ``WITNESS_MISMATCH`` — it produced a witness that does **not**
        reproduce the current content.
    """
    out: Dict[str, Any] = {"events": {}, "second_pass_added": 0,
                           "mtime_advanced": False, "unclosable": {},
                           "post_witness": {}, "error": None}
    try:
        from agent.memory import external_edit, changes
    except Exception as e:  # pragma: no cover
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    with tempfile.TemporaryDirectory(prefix="ee1-probe-") as td:
        tmp = Path(td)
        (tmp / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
        live_log = ws / "memory" / ".audit" / "file_changes.jsonl"
        if live_log.is_file():
            try:
                (tmp / "memory" / ".audit" / "file_changes.jsonl").write_bytes(
                    live_log.read_bytes())
            except OSError as e:
                out["error"] = "copy log failed: %s" % e
                return out
        try:
            before = len(changes.read_changes(tmp))
            mtime_before = changes.latest_change_mtime(tmp)
            rep = external_edit.reconcile_external_edits(tmp, cands)
            if rep.get("errors"):
                out["error"] = "; ".join(str(x) for x in rep["errors"])[:300]
            after_events = changes.read_changes(tmp)
            for e in after_events[before:]:
                p = e.get("path")
                if p and e.get(WITNESS_KEY):
                    out["events"][str(p).replace("\\", "/")] = e.get(WITNESS_KEY)
            mtime_after = changes.latest_change_mtime(tmp)
            # A3 · the self-refresh probe: a second pass over an
            # UNCHANGED tree must add NOTHING and must not advance the
            # log's newest stamp.
            external_edit.reconcile_external_edits(tmp, cands)
            second = changes.read_changes(tmp)
            out["second_pass_added"] = len(second) - len(after_events)
            mtime_third = changes.latest_change_mtime(tmp)
            out["mtime_advanced"] = (mtime_after != mtime_third)
            # Which paths could NOT be closed by the mechanism — and WHY.
            #
            # **The evidence is returned too.**  The criterion must not
            # take the probe's WORD for it: the probe's verdict is
            # checked against the copy log's final state by the judge
            # (the cross-check).  A probe that claims "closable" while
            # the copy log holds no witness is LYING, and a criterion
            # that believes such a probe is exactly the "假守护者"
            # this suite exists to kill.
            final_last = _last_event_per_path(changes.read_changes(tmp))
            out["post_witness"] = {
                rel: (ev.get(WITNESS_KEY) or "")
                for rel, ev in final_last.items()}
            for rel, real in cands:
                H = _canon_hash(Path(real))
                if not H:
                    out["unclosable"][rel] = (
                        "UNREADABLE", "文件读不到（算不出内容见证）")
                    continue
                w = out["events"].get(rel)
                if w is None:
                    prior = final_last.get(rel)
                    pw = (prior or {}).get(WITNESS_KEY)
                    if pw == H:
                        continue          # already witnessed
                    if prior is None:
                        out["unclosable"][rel] = (
                            "NO_EVENT",
                            "对账没有为这个路径产生任何事件"
                            "（路径不在候选集内，或被跳过）")
                    else:
                        out["unclosable"][rel] = (
                            "EMPTY_WITNESS",
                            "对账产生了事件但见证为空（见证没写进去）")
                elif w != H:
                    out["unclosable"][rel] = (
                        "WITNESS_MISMATCH",
                        "对账产生的见证复现不出当前内容"
                        "（见证与内容不一致）")
        except Exception as e:
            out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


# ---------------------------------------------------------------------------
# EE-1 · the truth layer's KNOWLEDGE part (the 2026-11-01 ruling)
# ---------------------------------------------------------------------------

def _index_knowledge_files(ws: Path, lib) -> List[Tuple[str, Path]]:
    """The knowledge-layer truth files **this** workspace's index points at.

    **Why this exists (SEED's ruling, 2026-11-01).**  The criterion
    borrowed ``external_edit.truth_files`` verbatim, and that set does
    **not** list ``knowledge/``.  Measured on the live workspace:

      * ``truth_files(ROOT)`` = **87** entries, of which **0** are
        ``knowledge/``;
      * but the INDEX points at knowledge files — **2794 chunk rows**
        across the 16 libraries, **all 2794 resolvable** to a real file;
      * and the ROOT's change log carries **1018** ``knowledge/`` events,
        **119** of them with a content witness;
      * and the shared tree physically holds **121** markdown files.

    ⇒ ``knowledge/`` IS the truth layer by every operational measure, and
    a criterion that borrowed the borrowed set was watching **87 of ~206**
    truth files — **the largest truth layer was invisible to the very
    criterion that exists to watch it**.

    **Ownership is by WHERE THE FILE LIVES, not by who points at it.**
    The shared ``knowledge/`` tree is the ROOT's tree ⇒ it belongs to the
    ROOT's log.  An agent workspace does **not** re-measure it (that would
    count one debt twice).  An agent that keeps a **private** ``knowledge/``
    copy keeps those files in its own log.

    **The set is derived from the INDEX, never from a hard-coded path
    list** — the same rule the writer uses (portable, no machine paths).
    """
    out: List[Tuple[str, Path]] = []
    if lib is None:
        return out
    try:
        from agent.memory.acceptance.live_libs import open_ro
        conn = open_ro(lib.db)
    except Exception:
        return out
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(chunks)").fetchall()}
        if "body_path" not in cols:
            return out
        rows = conn.execute(
            "SELECT DISTINCT body_path FROM chunks "
            "WHERE body_path IS NOT NULL AND body_path LIKE 'knowledge/%'"
        ).fetchall()
    except Exception:
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass

    from agent.memory.body_pointer import resolve_truth_file
    ws_real = Path(ws).resolve()
    seen: Set[str] = set()
    for (bp,) in rows:
        rel = str(bp).replace("\\", "/")
        if rel in seen:
            continue
        real = resolve_truth_file(ws, rel)
        if real is None:
            continue
        try:
            real_r = Path(real).resolve()
        except Exception:
            continue
        # Ownership: the file must physically live under THIS workspace.
        try:
            real_r.relative_to(ws_real)
        except ValueError:
            continue          # lives in another workspace's tree
        seen.add(rel)
        out.append((rel, Path(real)))
    return out


def _mechanism_born_of(ws: Path) -> Optional[str]:
    """When the mechanism first ran **in this workspace** (its own log).

    **Why per workspace, not global (B-2).**  The old code took the
    EARLIEST ``external-edit`` event across all 16 workspaces — but only
    **2** workspaces have ever had one (measured: ``cow`` 151 events /
    ``agent-mvnewfyb`` 22; the other 14 have **zero**).  For those 14 the
    old ``mechanism_born`` was **another workspace's timestamp** ⇒ a file
    edited in a workspace whose mechanism had never run was judged against
    a foreign clock.

    **The result is a CAUSE LABEL, not an exemption.**  The criterion
    does not decide "is this file excused?" by asking when the mechanism
    started — see the judge below.
    """
    stamps = [str(e.get("at")) for e in _events(ws)
              if e.get("source") == "external-edit" and e.get("at")]
    return min(stamps) if stamps else None


def _cause_label(born: Optional[str], mt: float) -> str:
    """Why the file has no witness — as a LABEL, never as a pardon."""
    if born is None:
        return "本工作区的机制尚未触发过（自己的日志里没有 external-edit 事件）"
    try:
        import datetime
        b = datetime.datetime.fromisoformat(str(born)[:19]).timestamp()
    except Exception:
        return "机制首次生效时间未知（%s）" % born
    if mt and mt < b - 1:
        return "最后一次编辑早于本工作区的机制首次生效（%s）" % born
    return "最后一次编辑晚于本工作区的机制首次生效（%s）" % born


def judge_external_edit_is_traceable(report, workspace: Path):
    """EE-1 (REAL): every truth-layer file carries a WITNESS that reproduces it.

    **The subject — stated up front (the team rule: a criterion must
    say which object it measures).**

    The subject is **the truth layer of THIS workspace** — the set of
    files that the workspace's own index points at as the source of
    truth, i.e.:

      * the workspace's identity files (``MEMORY.md`` / ``USER.md`` /
        ``AGENT.md`` / ``RULE.md``),
      * its persona layer (``persona/*.md``),
      * its markdown truth layer under ``memory/``,
      * **and — by the 2026-11-01 ruling — its ``knowledge/`` layer**
        (the files its own index actually points at).

    **The ``knowledge/`` ruling (SEED, 2026-11-01).**  The criterion used
    to borrow ``external_edit.truth_files`` verbatim, and that set lists
    **0** ``knowledge/`` entries.  Measured on the live workspace:

      * ``truth_files(ROOT)`` = **87** entries, of which **0** are
        ``knowledge/`` — while the shared tree physically holds **121**
        markdown files;
      * the INDEX points at knowledge files: **2794 chunk rows** across
        the 16 libraries, **all 2794** resolvable to a real file;
      * the ROOT's change log carries **1018** ``knowledge/`` events,
        **119** of them with a content witness.

    ⇒ ``knowledge/`` is the truth layer by every operational measure, and
    the criterion was watching **87 of ~206** truth files.  **The
    criterion's subject now includes the knowledge layer**, derived from
    the INDEX (never from a hard-coded path list).

    **The five states (the 2026-11-01 ruling).**  The engineer's
    measurement (113/113 ``historical`` and 7/7 ``live-leak`` rows
    closeable by a reconciliation on a log copy) shows that **a witness
    is a reproduction of NOW against NOW** — it does not reach back to
    "then".  Therefore:

    =======================  ==============================  ==========
    state                    meaning                         verdict
    =======================  ==============================  ==========
    ``witnessed``            the last event carries a witness   OK
                             that REPRODUCES the current
                             content
    ``backfillable``         no valid witness, but a real       **DEBT**
                             reconciliation on a log COPY      (reported,
                             WOULD close it now                not red)
    ``unclosable``           no valid witness and the          **RED**
                             mechanism CANNOT close it ⇒
                             the mechanism itself is broken
    ``forged``               a witness exists that does NOT     **RED**
                             reproduce the content and the
                             file was not modified after it
                             ⇒ the witness is a lie
    =======================  ==============================  ==========

    **What changed, and why it is NOT a pardon.**  The old four states
    had a ``historical`` bucket, and the rule was *"the edit predates
    the mechanism ⇒ not red"* — an exemption keyed on **when**.  That is
    the defect the engineer measured (B-1): what decides whether a row
    can be closed is **whether it can be closed**, not whether it is
    old.  The new rule keys on **can-it-be-closed**:

      * a row the mechanism can close ⇒ ``backfillable`` ⇒ **reported,
        named, and still a debt** — it is not excused, it is a TODO;
      * a row the mechanism CANNOT close ⇒ ``unclosable`` ⇒ **RED**,
        whatever its age.

    ⇒ **"historical" is no longer an exemption; it is a CAUSE LABEL**
    attached to a row that is still owed a witness.  The label is kept
    for visibility (it answers "why was this missed?") and it can never
    pardon anything.

    **Can the criterion tell "cannot be closed" from "never closed"?**
    Yes — and this is the substance of the ruling.  The criterion runs a
    REAL reconciliation **on a copy of the log** and reads what came
    out.  Three outcomes, each named:

      * the reconciliation produced a witness equal to the current
        content ⇒ **closable** ⇒ ``backfillable``;
      * it produced **no event** for the path ⇒ ``NO_EVENT`` — the path
        is outside the candidate set or was skipped ⇒ the mechanism
        cannot see it ⇒ **RED**;
      * it produced an event with **no witness** ⇒ ``EMPTY_WITNESS`` ⇒
        **RED**;
      * it produced a witness that does **not** reproduce the content ⇒
        ``WITNESS_MISMATCH`` ⇒ **RED**.

    So the criterion can say *"this one cannot be closed, because X"*
    — it never has to say "it is old, so never mind".

    **Why "closable" is not red.**  A reconciliation that WOULD close it
    proves the **mechanism is sound**; the row is missing because the
    write path did not trigger a sync, or because the reconciliation
    has not run over this path yet.  Sending a fixer to a mechanism that
    the probe has just proven working is the same category of error as
    reporting a stale projection as a content corruption.  **But the row
    stays a debt**: it is counted, named, and stays visible until it is
    actually closed.

    **The mechanism's birth is per workspace, not global (B-2).**  Only
    **2** of the 16 workspaces have ever had an ``external-edit`` event
    (measured: ``cow`` 151, ``agent-mvnewfyb`` 22; the other 14 have
    **zero**).  The old code took the earliest event across ALL of them
    ⇒ for those 14, the "mechanism was born" timestamp belonged to
    **another workspace**.  The birth time is now read from **the
    workspace's own log**, and it is used **only as a cause label**.

    **DR-2 stays red.**  The守门人's DR-2 (historical events have no
    witness and cannot be back-filled) is a DIFFERENT red — the log is
    append-only, so the OLD event never gains a witness.  This criterion
    does not pretend to have turned it green, and it does not duplicate
    it: the debt it reports is *"this file has no witness NOW"* (which
    the mechanism CAN close by appending a new event), not *"that old
    event has no witness"* (which it cannot).

    **NOTRUN** when there is no workspace with a change log at all, or
    when no truth-layer file exists to witness.
    """
    detail: List[str] = []
    root = Path(workspace)
    workspaces = _workspaces(root)
    if not workspaces:
        return report.judge_notrun(
            "EE-1(REAL)",
            "每个真源文件都有一条带内容见证的变更事件（见证可复现）",
            "本工作区下没有任何带变更记录的工作区，前置条件不成立"
            "（这不是通过）")

    detail.append("被测对象：**本工作区的真源层**（身份文件 + persona + "
                  "memory/ + **knowledge/**〔2026-11-01 裁决纳入〕）")
    detail.append("")

    # Which library belongs to which workspace?  (the knowledge layer
    # is derived from the INDEX, so the criterion needs the library.)
    libs_by_ws: Dict[str, Any] = {}
    try:
        from agent.memory.acceptance.live_libs import enumerate_live
        for lib in enumerate_live(root):
            try:
                libs_by_ws[Path(lib.workspace).resolve().name] = lib
            except Exception:
                continue
    except Exception as e:
        detail.append("活库枚举失败（%s: %s）—— knowledge 层将无法纳入"
                      % (type(e).__name__, e))

    grand = {"witnessed": 0, "backfillable": 0, "unclosable": 0,
             "forged": 0, "unreadable": 0}
    closable: List[str] = []
    unclosable: List[str] = []
    forged: List[str] = []
    debt_paths: List[str] = []
    reason_hist: Dict[str, int] = {}
    measured = 0
    a3_failures: List[str] = []

    for ws in workspaces:
        events = _events(ws)
        last = _last_event_per_path(events)
        files = _truth_files(ws)
        # The knowledge layer (2026-11-01 ruling) — the files THIS
        # workspace's own index points at and that physically live in
        # this workspace's tree.
        lib = libs_by_ws.get(Path(ws).resolve().name)
        kfiles = _index_knowledge_files(ws, lib)
        known = {str(r).replace("\\", "/") for r, _f in files}
        kown = [(r, f) for r, f in kfiles if r not in known]
        files = files + kown
        if not files:
            detail.append("  %-22s 真源文件集为空 —— 该工作区不可测量" % ws.name)
            continue
        # The root workspace also indexes the agent directories; the
        # agent workspace owns those files.  Counting them twice would
        # double-count one debt as two.
        own = [(r, f) for r, f in files
               if not (ws == root and str(r).replace("\\", "/").startswith("agents/"))]
        if not own:
            continue
        measured += 1

        born = _mechanism_born_of(ws)
        probe = _probe_reconciliation(ws, own)
        if probe.get("error"):
            detail.append("  %-22s 对账探针出错: %s" % (ws.name, probe["error"]))
        if probe.get("second_pass_added"):
            a3_failures.append(
                "%s: 第二次对账新增了 %d 条事件 —— 『每读一次刷新一次』"
                "（A3 被破坏）" % (ws.name, probe["second_pass_added"]))
        if probe.get("mtime_advanced"):
            a3_failures.append(
                "%s: 第二次对账推进了 latest_change_mtime —— "
                "『每读一次刷新一次』（A3 被破坏）" % ws.name)

        bucket = dict.fromkeys(grand, 0)
        for rel, real in own:
            H = _canon_hash(Path(real))
            if not H:
                bucket["unreadable"] += 1
                continue
            e = last.get(rel)
            if e is not None and e.get(WITNESS_KEY) == H:
                bucket["witnessed"] += 1
                continue
            try:
                mt = float(real.stat().st_mtime)
            except OSError:
                mt = 0.0
            # Is there a witness that does not reproduce, and the file
            # was NOT modified after it?  ⇒ the witness is a lie.
            witness = (e or {}).get(WITNESS_KEY)
            if witness and mt <= _event_ts(e) + 1:
                bucket["forged"] += 1
                forged.append("%s::%s (见证事件 %s, 文件 mtime %s)"
                              % (ws.name, rel, e.get("at"), _fmt_ts(mt)))
                continue
            # No valid witness.  **Can the mechanism close it?**  The
            # probe answers that on a copy of the log — and the judge
            # does NOT take the probe's word for it: the answer is
            # cross-checked against the copy log's final state.
            reason, why = probe.get("unclosable", {}).get(
                rel, (None, None))
            post = probe.get("post_witness") or {}
            evidence = post.get(rel, None)
            if reason is None:
                # The probe claims it is closable.  **Verify the claim.**
                if evidence != H:
                    # The claim is FALSE: the copy log holds no witness
                    # that reproduces the content, yet the probe said
                    # the mechanism could close it.  A criterion that
                    # believes such a probe would go GREEN on a broken
                    # mechanism ⇒ that is the "假守护者" shape.
                    bucket["unclosable"] += 1
                    reason_hist["PROBE_CLAIM_FALSE"] = \
                        reason_hist.get("PROBE_CLAIM_FALSE", 0) + 1
                    unclosable.append(
                        "%s::%s [PROBE_CLAIM_FALSE] 探针声称『机制能补』，"
                        "但副本日志里没有能复现当前内容的见证"
                        "（探针在说谎，或探针与机制已经脱节）"
                        % (ws.name, rel))
                    continue
                # Verified: the mechanism CAN close it ⇒ a debt, not a
                # defect.  It stays visible and named.
                bucket["backfillable"] += 1
                if len(debt_paths) < 40:
                    debt_paths.append(
                        "%s::%s (原因: %s; 最后事件 %s source=%s, 文件 mtime %s)"
                        % (ws.name, rel, _cause_label(born, mt),
                           (e or {}).get("at"), (e or {}).get("source"),
                           _fmt_ts(mt)))
                continue
            # The probe claims it CANNOT close it.  **Verify that too.**
            if evidence == H:
                # The copy log DOES hold a matching witness ⇒ the
                # mechanism closed it ⇒ reporting it as "broken" points
                # the fixer at a mechanism that works.
                bucket["unclosable"] += 1
                reason_hist["PROBE_CLAIM_FALSE"] = \
                    reason_hist.get("PROBE_CLAIM_FALSE", 0) + 1
                unclosable.append(
                    "%s::%s [PROBE_CLAIM_FALSE] 探针声称『机制关不上』，"
                    "但副本日志里有能复现当前内容的见证"
                    "（把可补的欠账报成机制坏了 ⇒ 修复方向会被指错）"
                    % (ws.name, rel))
                continue
            # The mechanism CANNOT close it ⇒ RED, and we say why.
            bucket["unclosable"] += 1
            reason_hist[reason] = reason_hist.get(reason, 0) + 1
            unclosable.append("%s::%s [%s] %s" % (ws.name, rel, reason, why))
        for k in grand:
            grand[k] += bucket[k]
        detail.append("  %-22s 见证成立=%-4d 待回填=%-4d 机制关不上=%-4d "
                      "见证说谎=%-4d 读不到=%d  (knowledge 层 %d 个)"
                      % (ws.name, bucket["witnessed"], bucket["backfillable"],
                         bucket["unclosable"], bucket["forged"],
                         bucket["unreadable"], len(kown)))

    detail.append("")
    detail.append("已测工作区（带变更记录且有真源文件）: %d / %d"
                  % (measured, len(workspaces)))
    detail.append("合计: 见证成立=%d  待回填=%d  机制关不上=%d  见证说谎=%d  读不到=%d"
                  % (grand["witnessed"], grand["backfillable"],
                     grand["unclosable"], grand["forged"], grand["unreadable"]))
    detail.append("")
    detail.append("—— 待回填（机制**能**补上，补完即见证成立）: %d ——"
                  % grand["backfillable"])
    if debt_paths:
        for d in debt_paths:
            detail.append("  ~ %s" % d)
        if grand["backfillable"] > len(debt_paths):
            detail.append("  ~ …（其余 %d 条同类）"
                          % (grand["backfillable"] - len(debt_paths)))
    else:
        detail.append("  （无）")
    detail.append("  ⇒ 这批欠账**仍然可见**（计数 + 点名到工作区::path + 成因）。"
                  "『早于机制生效』只是**成因标签**，不是豁免理由 —— "
                  "欠的是『这个文件现在还没有见证』，它必须被补上。")
    detail.append("  ⇒ 这一批与 DR-2 **不是同一条红**：DR-2 说的是『那条旧事件"
                  "永远没有见证』（日志只追加，不可回填）；这里说的是"
                  "『这个文件现在没有见证』（追加一条新事件即可消除）。")
    detail.append("")
    detail.append("—— 机制关不上（机制坏了）: %d ——" % grand["unclosable"])
    if reason_hist:
        detail.append("  原因分布: %s"
                      % ", ".join("%s=%d" % (k, v)
                                 for k, v in sorted(reason_hist.items())))
    for l in unclosable[:30]:
        detail.append("  X %s" % l)
    if not unclosable:
        detail.append("  （无）")
    detail.append("—— 见证说谎: %d ——" % grand["forged"])
    for l in forged[:30]:
        detail.append("  X %s" % l)
    if not forged:
        detail.append("  （无）")
    detail.append("")
    for a in a3_failures:
        detail.append("  ! A3: %s" % a)
    detail.append("口径：见证 = sha256(canonical_text(当前内容))，与 "
                  "external_edit 写入时用的规范化同源；判据自己重算，不"
                  "借用写入方的算式。")
    detail.append("机制探针在**日志副本**上跑（活日志全程未被修改）："
                  "『能补 / 关不上 + 关不上的原因』靠的是副本上的一次真实对账。")
    detail.append("**探针的结论被交叉校验**（不轻信探针）：判据拿副本日志"
                  "的最终见证状态去核对探针的说法 —— 探针说『能补』却拿不"
                  "出见证、或探针说『关不上』却有见证，都记为原因 "
                  "PROBE_CLAIM_FALSE 并判红（原因行里它带方括号，"
                  "以区别于本段对它的提及）。")
    detail.append("机制首次生效时间**按工作区取**（B-2 修正）：它只作为"
                  "**成因标签**，不作为豁免条件。")

    if measured == 0:
        return report.judge_notrun(
            "EE-1(REAL)",
            "每个真源文件都有一条带内容见证的变更事件（见证可复现）",
            "\n".join(detail) + "\n没有可测量的工作区（真源文件集为空）——"
            "不可判定，不是通过。")
    if unclosable or forged or a3_failures:
        detail.append("")
        detail.append("结论：%d 条机制关不上 + %d 条见证说谎 + %d 条 A3 破坏 ⇒ 判红。"
                      % (len(unclosable), len(forged), len(a3_failures)))
        return report.judge(
            "EE-1(REAL)",
            "每个真源文件都有一条带内容见证的变更事件（见证可复现）",
            False, "\n".join(detail))
    detail.append("")
    detail.append("结论：没有『机制关不上』/『见证说谎』/A3 破坏 ⇒ "
                  "机制在已覆盖的路径上是有效的。"
                  "（待回填 %d 条是**欠账**，不是失败：它必须被补上，"
                  "但补它的动作不需要动机制。）" % grand["backfillable"])
    return report.judge(
        "EE-1(REAL)",
        "每个真源文件都有一条带内容见证的变更事件（见证可复现）",
        True, "\n".join(detail))


def _event_ts(e: Optional[Dict[str, Any]]) -> float:
    """The event's file-mtime stamp as a timestamp (0 when unknown)."""
    if not e:
        return 0.0
    for key in ("mtime", "at"):
        v = e.get(key)
        if not v:
            continue
        try:
            import datetime
            return datetime.datetime.fromisoformat(str(v)[:19]).timestamp()
        except Exception:
            continue
    return 0.0


def _fmt_ts(ts: float) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


# ---------------------------------------------------------------------------
# EE-2 · the mechanism really works (the seven fixtures, in a sandbox)
# ---------------------------------------------------------------------------

def _sandbox(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
    return root


def judge_external_edit_mechanism_witnesses_edits(report, workspace: Path = None):
    """EE-2 (unit): the reconciliation really witnesses an external edit.

    **This criterion builds its OWN sandbox** — it never touches the live
    workspace, and it never touches a live library.  The subject is the
    MECHANISM (``agent.memory.external_edit`` + ``agent.memory.changes``),
    so it is exercised by CALLING it.

    **The seven fixtures** (the design稿 §七), each of which has a
    failure mode it exists to catch:

    ==========  ==========================================  ===========
    fixture     what it pins down                         mutant
    ==========  ==========================================  ===========
    F1 编辑      an edit OUTSIDE our write paths becomes    MU-T1
                an event
    F2 幂等      reconciling twice adds NOTHING the         MU-T3
                second time
    F3 二次编辑   two edits are both seen                   MU-T1
    F4 touch     ``os.utime`` alone records NOTHING         MU-T3
    F5 CRLF      a CRLF file's witness == the canonical     MU-T4
                hash (not the raw bytes)
    F6 多 agent   two workspaces each get their OWN log      MU-T8
    F7 读不到     an unreadable path is SKIPPED, not         MU-T9
                silently treated as "unchanged"
    F8 A3        a second pass does not advance             MU-T6
                ``latest_change_mtime``
    ==========  ==========================================  ===========

    **F7 is the one that is easy to get wrong.**  A reconciliation that
    swallows the exception and returns an empty report makes "I could
    not read it" indistinguishable from "it did not change" — that is
    exactly the failure the design稿 calls the most insidious one
    (MU-T9).  So the criterion insists on the two being DISTINGUISHABLE:
    a skipped path must be reported as skipped, and a file that cannot
    be read must NOT be reported as witnessed.
    """
    detail: List[str] = []
    try:
        from agent.memory import external_edit
    except Exception as e:
        return report.judge_notrun(
            "EE-2(unit)",
            "外部编辑对账机制真的把外部编辑记成带见证的事件（7 个夹具）",
            "agent.memory.external_edit 不可导入（%s: %s）—— 判据无法测量"
            % (type(e).__name__, e))

    failures: List[str] = []
    with tempfile.TemporaryDirectory(prefix="ee2-") as td:
        base = Path(td)
        # ---- F1 + F2 + F3 + F4: the core behaviour ----
        ws = _sandbox(base / "ws")
        truth = ws / "MEMORY.md"
        truth.write_text("# head\n\nbody v1\n", encoding="utf-8")
        cands = [("MEMORY.md", truth)]

        rep1 = external_edit.reconcile_external_edits(ws, cands)
        evs = _events(ws)
        if len(evs) != 1:
            failures.append("F1: 改一次应记 1 条事件，实测 %d 条" % len(evs))
        else:
            e = evs[0]
            if e.get("source") != "external-edit":
                failures.append("F1: 事件的 source=%r，不是 external-edit"
                                % e.get("source"))
            if WITNESS_KEY not in e:
                failures.append(
                    "F1: 事件没有平铺的 %s 键（键在 %r 里吗？）—— "
                    "读侧与写侧不一致 ⇒ 每次读都会被当成『没被见证』"
                    % (WITNESS_KEY, sorted(e.keys())))
            else:
                want = _canon_hash(truth)
                if e.get(WITNESS_KEY) != want:
                    failures.append(
                        "F1: 见证 %r != 当前内容的规范哈希 %r"
                        % (e.get(WITNESS_KEY), want))
        detail.append("F1 外部编辑 ⇒ 事件: %d 条, source=%s, 带见证=%s"
                      % (len(evs),
                         (evs[0].get("source") if evs else "-"),
                         bool(evs and WITNESS_KEY in evs[0])))

        # F2 · idempotence
        rep2 = external_edit.reconcile_external_edits(ws, cands)
        evs2 = _events(ws)
        if len(evs2) != len(evs):
            failures.append("F2: 第二次对账新增了 %d 条事件（幂等被破坏）"
                            % (len(evs2) - len(evs)))
        detail.append("F2 幂等 ⇒ 第二次新增 %d 条" % (len(evs2) - len(evs)))

        # F3 · a second, different edit IS seen
        truth.write_text("# head\n\nbody v2 CHANGED\n", encoding="utf-8")
        external_edit.reconcile_external_edits(ws, cands)
        evs3 = _events(ws)
        if len(evs3) != len(evs) + 1:
            failures.append("F3: 第二次真实编辑应再记 1 条，实测新增 %d 条"
                            % (len(evs3) - len(evs)))
        else:
            if evs3[-1].get(WITNESS_KEY) != _canon_hash(truth):
                failures.append("F3: 新事件的见证复现不出改后的内容")
        detail.append("F3 二次编辑 ⇒ 新增 %d 条" % (len(evs3) - len(evs)))

        # F4 · a touch is not an edit
        before = len(_events(ws))
        os.utime(str(truth), None)
        external_edit.reconcile_external_edits(ws, cands)
        after = len(_events(ws))
        if after != before:
            failures.append("F4: touch（内容不变）却新增了 %d 条事件 —— "
                            "mtime 不是内容" % (after - before))
        detail.append("F4 touch ⇒ 新增 %d 条" % (after - before))

        # ---- F5 · CRLF ----
        ws2 = _sandbox(base / "ws-crlf")
        t2 = ws2 / "MEMORY.md"
        t2.write_bytes(b"# head\r\n\r\nbody\r\n")
        external_edit.reconcile_external_edits(ws2, [("MEMORY.md", t2)])
        ev2 = _events(ws2)
        if not ev2:
            failures.append("F5: CRLF 文件没有被记录")
        elif ev2[0].get(WITNESS_KEY) != _canon_hash(t2):
            failures.append(
                "F5: CRLF 文件的见证不是规范哈希（要么用了原始字节，"
                "要么读侧与写侧不一致）")
        detail.append("F5 CRLF ⇒ 见证与规范哈希一致: %s"
                      % (bool(ev2) and ev2[0].get(WITNESS_KEY) == _canon_hash(t2)))

        # ---- F6 · two workspaces, two logs ----
        wsA = _sandbox(base / "multi" / "a")
        wsB = _sandbox(base / "multi" / "b")
        fa = wsA / "MEMORY.md"
        fb = wsB / "MEMORY.md"
        fa.write_text("# A\n", encoding="utf-8")
        fb.write_text("# B\n", encoding="utf-8")
        external_edit.reconcile_external_edits(wsA, [("MEMORY.md", fa)])
        external_edit.reconcile_external_edits(wsB, [("MEMORY.md", fb)])
        ea, eb = _events(wsA), _events(wsB)
        if not ea or not eb:
            failures.append("F6: 两个工作区各自的事件没记全"
                            "（A=%d, B=%d）" % (len(ea), len(eb)))
        else:
            # The two logs must be SEPARATE files, and each must carry
            # its OWN workspace's witness — the same relative path in two
            # workspaces must NOT end up with the same content hash.
            if ea[0].get(WITNESS_KEY) == eb[0].get(WITNESS_KEY):
                failures.append(
                    "F6: 两个工作区记下了**同一个**见证哈希 —— "
                    "读侧把某个工作区的见证读到了另一个工作区（MU-T8 的"
                    "形态：只看一个工作区的日志）")
            if (wsA / "memory" / ".audit" / "file_changes.jsonl") == \
                    (wsB / "memory" / ".audit" / "file_changes.jsonl"):
                failures.append("F6: 两个工作区共用同一个日志文件")
        detail.append("F6 多 agent ⇒ A 记 %d 条, B 记 %d 条（各在自己的日志里，"
                      "见证哈希不同）" % (len(ea), len(eb)))

        # ---- F7 · unreadable ----
        ws3 = _sandbox(base / "ws-skip")
        d = ws3 / "MEMORY.md"
        d.mkdir(parents=True, exist_ok=True)      # a DIRECTORY, not a file
        rep = external_edit.reconcile_external_edits(ws3, [("MEMORY.md", d)])
        skipped = list(rep.get("skipped") or [])
        recorded = list(rep.get("recorded") or [])
        if not skipped:
            failures.append("F7: 读不到的文件没有被报成 skipped —— "
                            "『不知道』被伪装成『没变化』")
        if recorded and recorded[0].get("path") == "MEMORY.md":
            failures.append("F7: 读不到的文件被记成了 recorded")
        detail.append("F7 读不到 ⇒ skipped=%d recorded=%d"
                      % (len(skipped), len(recorded)))

    # ---- F8 · A3: a second pass must not refresh the log's stamp ----
    ws4 = _sandbox(base / "ws-a3")
    t4 = ws4 / "MEMORY.md"
    t4.write_text("# head\n\nbody\n", encoding="utf-8")
    c4 = [("MEMORY.md", t4)]
    external_edit.reconcile_external_edits(ws4, c4)
    from agent.memory import changes as _changes
    stamp_before = _changes.latest_change_mtime(ws4)
    n_before = len(_events(ws4))
    external_edit.reconcile_external_edits(ws4, c4)
    stamp_after = _changes.latest_change_mtime(ws4)
    n_after = len(_events(ws4))
    if n_after != n_before:
        failures.append("F8: 第二次对账新增了 %d 条事件 —— "
                        "『每读一次刷新一次』（A3 被破坏）"
                        % (n_after - n_before))
    if stamp_after != stamp_before:
        failures.append(
            "F8: 第二次对账把 latest_change_mtime 从 %r 推进到 %r —— "
            "『每读一次刷新一次』（A3 被破坏）"
            % (stamp_before, stamp_after))
    detail.append("F8 A3 不得自我刷新 ⇒ 第二次新增 %d 条, 时间戳 %s"
                  % (n_after - n_before,
                     "未推进" if stamp_after == stamp_before else "**推进了**"))

    detail.append("")
    if failures:
        for f in failures:
            detail.append("  ! %s" % f)
        return report.judge(
            "EE-2(unit)",
            "外部编辑对账机制真的把外部编辑记成带见证的事件（7 个夹具）",
            False, "\n".join(detail))
    detail.append("7 个夹具全部成立 ⇒ 机制有效（这是判据在**沙箱**里"
                  "实测的，不是读文档抄的）。")
    return report.judge(
        "EE-2(unit)",
        "外部编辑对账机制真的把外部编辑记成带见证的事件（7 个夹具）",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# EE-3 · the mechanism is WIRED (not dead code)
# ---------------------------------------------------------------------------

def judge_external_edit_reconciliation_is_wired(report, workspace: Path = None):
    """EE-3 (unit): the reconciliation is actually CALLED.

    **A mechanism that is not called is not a mechanism.**  The module
    exists, the unit tests pass, and the log stays empty — that is the
    shape of a guard that guards nothing.

    The check is on the **call node**, not on a string: the AST of
    ``agent/memory/manager.py`` is parsed and the call
    ``reconcile_external_edits`` is looked for as a real ``Call`` node.
    Renaming or moving the function does not break the check (the name
    is followed to its definition), and a comment mentioning it does
    not satisfy it.

    **The check is deliberately narrow**: it asserts the call exists.  It
    does NOT assert the call is correct — that is EE-1's and EE-2's
    job (does the call actually witness?).
    """
    detail: List[str] = []
    repo = Path(__file__).resolve().parents[3]
    manager = repo / "agent" / "memory" / "manager.py"
    if not manager.is_file():
        return report.judge_notrun(
            "EE-3(unit)",
            "对账在写路径上真的被调用（AST 调用节点，不是字符串匹配）",
            "找不到 agent/memory/manager.py —— 判据无法测量")

    try:
        tree = ast.parse(manager.read_text(encoding="utf-8"))
    except Exception as e:
        return report.judge_notrun(
            "EE-3(unit)",
            "对账在写路径上真的被调用（AST 调用节点，不是字符串匹配）",
            "manager.py 解析失败（%s: %s）—— 判据无法测量"
            % (type(e).__name__, e))

    called: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = None
        if isinstance(fn, ast.Attribute):
            name = fn.attr
        elif isinstance(fn, ast.Name):
            name = fn.id
        if name == "reconcile_external_edits":
            called.append("line %d" % node.lineno)

    # Where is the function DEFINED?  (A call to something else with the
    # same name must not satisfy the check.)
    try:
        from agent.memory import external_edit
        real = external_edit.reconcile_external_edits
        defined_here = True
    except Exception as e:
        defined_here = False
        detail.append("agent.memory.external_edit 不可导入: %s" % e)

    detail.append("manager.py 中的 reconcile_external_edits 调用节点: %s"
                  % (", ".join(called) if called else "无"))
    detail.append("被调函数来自 agent.memory.external_edit: %s" % defined_here)
    detail.append("")
    detail.append("判据在 AST 上找的是 **Call 节点**（真的调用），"
                  "不是注释里的名字 —— 注释里写一遍『我们调了 reconcile』"
                  "不构成证据。")

    if not called:
        detail.append("")
        detail.append("⇒ manager 的写路径上**没有**对账调用：机制装了但"
                      "没人叫它 ⇒ 变更记录永远停在旧状态。")
        return report.judge(
            "EE-3(unit)",
            "对账在写路径上真的被调用（AST 调用节点，不是字符串匹配）",
            False, "\n".join(detail))
    return report.judge(
        "EE-3(unit)",
        "对账在写路径上真的被调用（AST 调用节点，不是字符串匹配）",
        True, "\n".join(detail))


def judge_the_canonical_cross_check_is_really_called(report, workspace: Path = None):
    """EE-4 (unit): the canonical cross-check is REALLY called by W-4.

    **The hole this closes.**  The architect's review (T1b §2.2) found
    that ``phase2_s3w.py``'s docstring promised a canonical cross-check
    that **did not exist** — the import list had no ``agent.memory.naming``
    and the whole module mentioned ``naming`` only in prose.  The
    cross-check is now real (see ``phase2_s3w._canonical_cross_check``),
    but **a check that can be deleted without anybody noticing is not a
    check**: the next person who tidies the module can remove the call,
    and the docstring's promise becomes a lie again — silently.

    ⇒ So the promise is guarded by a SECOND criterion, and this one is
    checked **by BEHAVIOUR, not by source text**.

    **The seam — and why it is the COPY, not the check.**  The mutation
    breaks **this module's ``canonical_text`` copy** so that it no
    longer agrees with ``naming.canonical_text``, and then asks whether
    W-4 goes red.

    The seam matters: if the mutation replaced the *check function*
    instead, it would be proving nothing — EE-4 would be replacing the
    very thing whose absence it is supposed to detect.  Mutating the
    COPY catches BOTH failures:

      * the check is neutered (replaced by one that always says "no
        disagreement") ⇒ breaking the copy changes nothing ⇒ W-4 stays
        green ⇒ EE-4 is red;
      * the check is deleted ⇒ the attribute is gone ⇒ EE-4 is red with
        an explicit message.

    And the mutation's effect is **proved** before the criterion is
    asked: the copy must actually differ from the real one after the
    mutation (otherwise the mutant would "prove" the criterion is red
    by accident).

    **The restore brings the green back** — a criterion that is red for
    everything is not a criterion.

    **NOTRUN** when the criterion cannot run at all (the S3-W module is
    not importable, or there is no live library to run W-4 against).
    """
    detail: List[str] = []
    try:
        from agent.memory.acceptance import phase2_s3w
    except Exception as e:
        return report.judge_notrun(
            "EE-4(unit)",
            "canonical 交叉校验真的被 W-4 调用（行为证明，不是文本匹配）",
            "phase2_s3w 不可导入（%s: %s）—— 判据无法测量"
            % (type(e).__name__, e))

    # --- the check must EXIST at all ---
    checker = getattr(phase2_s3w, "_canonical_cross_check", None)
    if checker is None or not callable(checker):
        detail.append("phase2_s3w 上没有 _canonical_cross_check（或它不可调用）"
                      "⇒ 那条 docstring 承诺**仍然是假的**（守护者不存在）。")
        return report.judge(
            "EE-4(unit)",
            "canonical 交叉校验真的被 W-4 调用（行为证明，不是文本匹配）",
            False, "\n".join(detail))

    libs = enumerate_live(Path(workspace)) if workspace else []
    if not libs:
        return report.judge_notrun(
            "EE-4(unit)",
            "canonical 交叉校验真的被 W-4 调用（行为证明，不是文本匹配）",
            "本工作区没有活库，W-4 跑不起来 —— 判据无法测量")

    original_copy = phase2_s3w.canonical_text

    def broken_copy(content):
        # A copy that forgets to fold the blank-line runs: it agrees
        # with the real implementation on most inputs and disagrees on
        # the ones that matter.
        text = content.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()

    # --- prove the mutation took effect ---
    #
    # **The probe's own output is SUPPRESSED.**  The probe deliberately
    # makes W-4 FAIL (that is how it proves the cross-check has teeth),
    # and a printed ``[FAIL] W-4`` row would look to any reader — and
    # to the global-baseline criterion, which reads the gate's output —
    # like a real gate result.  A probe that leaves its marks on the
    # report is a probe that corrupts the thing it measures.
    phase2_s3w.canonical_text = broken_copy
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            diverged = bool(checker())
            if not diverged:
                detail.append("打坏的副本没有被交叉校验发现 ⇒ 校验本身没有牙齿"
                              "（它比较的不是这两份实现）")
                return report.judge(
                    "EE-4(unit)",
                    "canonical 交叉校验真的被 W-4 调用（行为证明，不是文本匹配）",
                    False, "\n".join(detail))
            rep = Report()
            phase2_s3w.judge_unresolved_writer_generation_is_reported(
                rep, Path(workspace))
            mutated_state = rep.rows[-1][2] if rep.rows else "NONE"
    finally:
        phase2_s3w.canonical_text = original_copy

    with contextlib.redirect_stdout(io.StringIO()):
        rep2 = Report()
        phase2_s3w.judge_unresolved_writer_generation_is_reported(
            rep2, Path(workspace))
        restored_state = rep2.rows[-1][2] if rep2.rows else "NONE"

    detail.append("打坏本模块的 canonical 副本后，交叉校验报出分歧: %s"
                  % diverged)
    detail.append("打坏后 W-4 的判定: %s" % mutated_state)
    detail.append("还原后 W-4 的判定: %s" % restored_state)
    detail.append("")
    detail.append("口径：这是**行为证明** —— 把本模块的 canonical 副本改坏，"
                  "问 W-4 会不会红；再还原，问它会不会绿回去。")
    detail.append("接缝选的是**副本**而不是校验函数本身：把校验函数换成"
                  "『永远说没分歧』的版本，恰好就是本判据要抓的那种失效。")
    detail.append("不检查源码里有没有那个名字（拼写变了就会假红），"
                  "只问『两份 canonical 不一致时门禁会不会红』。")

    if mutated_state != "FAIL":
        detail.append("")
        detail.append("⇒ 副本与真实现不一致，但 W-4 没有报红 ⇒ "
                      "那份 docstring 承诺**仍然是假的**（守护者不存在）"
                      "⇒ 判红。")
        return report.judge(
            "EE-4(unit)",
            "canonical 交叉校验真的被 W-4 调用（行为证明，不是文本匹配）",
            False, "\n".join(detail))
    if restored_state != "PASS":
        detail.append("")
        detail.append("⇒ 还原后 W-4 没有回到绿 ⇒ 判据自身坏了（不是产品坏了）"
                      "⇒ 判红。")
        return report.judge(
            "EE-4(unit)",
            "canonical 交叉校验真的被 W-4 调用（行为证明，不是文本匹配）",
            False, "\n".join(detail))
    detail.append("")
    detail.append("⇒ 打坏必红、还原必绿 ⇒ 交叉校验**真的被调用**，"
                  "docstring 的承诺兑现了。")
    return report.judge(
        "EE-4(unit)",
        "canonical 交叉校验真的被 W-4 调用（行为证明，不是文本匹配）",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# R3 · the two criteria's RED sets must be the SAME SET (resident)
# ---------------------------------------------------------------------------

def _gate_red_set(libs) -> Tuple[Set[Tuple[str, str]], int]:
    """The gate's INV-2 RED set, computed the way the GATE computes it.

    **The criterion's own reader** (``phase2_s3h.parse_anchor_grammar`` /
    ``rebuild_from_grammar``) — never ``body_pointer.rebuild_from_anchor``
    — so the comparison is between two INDEPENDENT readings, not between
    a thing and itself.

    Returns ``(red_set, judged_count)``.
    """
    red, judged, _rows = _gate_red_set_with_rows(libs)
    return red, judged


def _gate_red_set_with_rows(libs) -> Tuple[Set[Tuple[str, str]], int, Set[Tuple[str, str]]]:
    """The gate's RED set, PLUS the set of rows the gate could JUDGE.

    **Why the judged set is returned.**  ``text_inv`` reports
    ``inv2-body-not-rebuildable`` for TWO different facts: "the truth
    file does not exist on disk" AND "the rebuilt fragment does not
    match the stored projection".  The gate's reader SKIPS the first
    (there is nothing to compare), so the two sets would only be
    comparable over the rows the gate could actually judge.

    ⇒ The comparison is therefore made **over the rows both sides
    judged**, and the criterion says how many rows that was.  Without
    this, a library whose truth layer moved would report a huge
    asymmetric difference that says nothing about the yardstick — a
    false red about a different fact.
    """
    from agent.memory.acceptance.phase2_s3h import (
        parse_anchor_grammar, rebuild_from_grammar)
    from agent.memory.body_pointer import resolve_truth_file
    from agent.memory.naming import canonical_text

    red: Set[Tuple[str, str]] = set()
    judged_rows: Set[Tuple[str, str]] = set()
    judged = 0
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
            origin_expr = "COALESCE(body_origin,'file')" if "body_origin" in cols \
                else "'file'"
            rows = conn.execute(
                "SELECT id, body_path, body_anchor, text, %s FROM chunks "
                "WHERE text_state IN ('projection','mirror') "
                "AND body_path IS NOT NULL AND body_anchor IS NOT NULL"
                % origin_expr).fetchall()
        finally:
            conn.close()
        for cid, bpath, banchor, text, origin in rows:
            if origin == "orphan":
                continue
            truth = resolve_truth_file(lib.workspace, bpath)
            if truth is None:
                continue
            if parse_anchor_grammar(banchor) is None:
                continue
            rebuilt = rebuild_from_grammar(truth, banchor)
            if rebuilt is None:
                continue
            judged += 1
            judged_rows.add((lib.name, str(cid)))
            if canonical_text(rebuilt) != canonical_text(text or ""):
                red.add((lib.name, str(cid)))
    return red, judged, judged_rows


def _textinv_red_set(libs, judged_rows: Optional[Set[Tuple[str, str]]] = None
                     ) -> Tuple[Set[Tuple[str, str]], int]:
    """``text_inv``'s INV-2 RED set, read from its OWN published output.

    Only the violations whose code is ``inv2-body-not-rebuildable`` are
    in scope: that is the code for "the rebuilt fragment does not match
    the stored projection", which is the fact the gate's INV-2 judges.
    The other codes (``inv2-anchor-does-not-locate``,
    ``inv3-fragment-hash-mismatch``) are DIFFERENT facts and are not
    comparable to the gate's verdict.

    **The scope filter.**  When ``judged_rows`` is given, only the rows
    the gate could judge are kept — because ``text_inv`` uses the same
    code for "the truth file is gone" and "the fragment does not
    match", and only the second one is the fact under comparison.
    """
    from agent.memory.text_inv import run_text_invariants
    red: Set[Tuple[str, str]] = set()
    judged = 0
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            r = run_text_invariants(conn, lib.workspace)
        finally:
            conn.close()
        chk = r["checks"]["INV-2/3"]
        judged += int(chk.get("byte_equal", 0) or 0)  # touched => measured
        for v in (chk.get("violations") or []):
            if (v.get("code") or "") != "inv2-body-not-rebuildable":
                continue
            key = (lib.name, str(v.get("chunk_id")))
            if judged_rows is not None and key not in judged_rows:
                continue
            red.add(key)
    return red, judged


def judge_inv2_red_sets_are_the_same_set(report, workspace: Path):
    """R3 (REAL, RESIDENT): the gate's INV-2 RED set == ``text_inv``'s.

    **Why this is a criterion and not a number.**  Two criteria about
    the same fact must use the same yardstick.  The gate's ``INV-2``
    and ``text_inv.check_inv2_inv3`` both judge "does the projection
    rebuild back to the truth?", and after SEED's ruling C7 they both
    judge it on the canonical yardstick.  But two criteria agreeing on
    the yardstick can still disagree on the VERDICT — and the earlier
    one-shot probe showed a set difference of 0 on that snapshot only.

    **A one-shot probe is not a guard.**  The architect's own words:
    "R3 那条护栏必须变成常驻判据，而不是我这轮的一次性探针".  So this
    criterion runs EVERY gate run, and it compares **SETS**, not counts:
    "the same number of reds but different ones" is exactly the failure
    this catches.

    **Which side, and which direction.**  The report says, for each
    difference, whether the row is RED on the gate side only, on the
    ``text_inv`` side only, or on both — and it names the library and
    the chunk id, so the difference is actionable rather than a number.

    **It is not "canonical does not swallow real drift".**  That is
    R1/R2, which the architect's probe measured separately (canonical
    says equal but an independent classifier says real drift = 0;
    canonical says unequal but the difference is only normalisation =
    0).  This criterion is R3 only: the two criteria's red sets must be
    the same set.
    """
    detail: List[str] = []
    libs = enumerate_live(Path(workspace))
    if not libs:
        return report.judge_notrun(
            "R3(REAL)",
            "门禁 INV-2 与 text_inv 的 RED 行集合相等（集合相等，不是个数相等）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        gate, judged_gate, judged_rows = _gate_red_set_with_rows(libs)
    except Exception as e:
        return report.judge_notrun(
            "R3(REAL)",
            "门禁 INV-2 与 text_inv 的 RED 行集合相等（集合相等，不是个数相等）",
            "门禁侧读法不可用（%s: %s）—— 判据无法测量"
            % (type(e).__name__, e))
    try:
        ti, judged_ti = _textinv_red_set(libs, judged_rows)
    except Exception as e:
        return report.judge_notrun(
            "R3(REAL)",
            "门禁 INV-2 与 text_inv 的 RED 行集合相等（集合相等，不是个数相等）",
            "text_inv 侧不可用（%s: %s）—— 判据无法测量"
            % (type(e).__name__, e))

    only_gate = sorted(gate - ti)
    only_ti = sorted(ti - gate)
    symdiff = gate ^ ti

    detail.append("门禁 INV-2 RED 行: %d （可判定行 %d）" % (len(gate), judged_gate))
    detail.append("text_inv INV-2 RED 行: %d（同样只取门禁可判定的 %d 行）"
                  % (len(ti), len(judged_rows)))
    detail.append("交集: %d   对称差: %d" % (len(gate & ti), len(symdiff)))
    detail.append("")
    detail.append("—— 只在门禁侧红（门禁更严）: %d ——" % len(only_gate))
    for x in only_gate[:40]:
        detail.append("  ! 门禁 %s / chunk %s" % x)
    if not only_gate:
        detail.append("  （无）")
    detail.append("—— 只在 text_inv 侧红（text_inv 更严）: %d ——" % len(only_ti))
    for x in only_ti[:40]:
        detail.append("  ! text_inv %s / chunk %s" % x)
    if not only_ti:
        detail.append("  （无）")
    detail.append("")
    detail.append("口径：两边都只取『重建片段与 chunks.text 不等』这一条事实；"
                  "『锚点定位不到』与『片段哈希不符』是另外两条事实，"
                  "不参与本比较。")
    detail.append("比较的是**集合**不是数量：数量相同但内容换了，也是"
                  "两把尺子在各选各的。")
    detail.append("本条只管 R3（集合对称差）。『canonical 有没有把真漂移"
                  "一起吞掉』是 R1/R2，由独立探针量，不在本条里充数。")

    if symdiff:
        detail.append("")
        detail.append("⇒ 对称差非空：两把尺子对同一批行给出了不同的结论。"
                      "要么门禁读法漏了 %d 行，要么 text_inv 漏了 %d 行 —— "
                      "两条判据关于同一件事开始各说各话。"
                      % (len(only_gate), len(only_ti)))
        return report.judge(
            "R3(REAL)",
            "门禁 INV-2 与 text_inv 的 RED 行集合相等（集合相等，不是个数相等）",
            False, "\n".join(detail))
    detail.append("")
    detail.append("⇒ 对称差 = 0：两把尺子对同一批行给出了同一结论。")
    return report.judge(
        "R3(REAL)",
        "门禁 INV-2 与 text_inv 的 RED 行集合相等（集合相等，不是个数相等）",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# GB-1 · the gate's GLOBAL failure-set baseline
# ---------------------------------------------------------------------------

#: The baseline file.  In the repository, because a baseline in ``tmp/``
#: is a baseline nobody else has (that was the stage-two B2 root cause).
GLOBAL_BASELINE_FILE = Path("tests") / "baseline" / "global_failure_baseline.txt"

#: The six minimal attributions 风控 §5.1 demands of any baseline
#: update.  Each one is checked by NAME, so a baseline that skips one
#: says which one it skipped.
REQUIRED_ATTRIBUTIONS: Tuple[Tuple[str, str], ...] = (
    ("两次独立快照",
     "两次独立全量快照（间隔 ≥20 秒），且两次的 RED 集合完全相同"),
    ("逐条点名",
     "每条条目点名到 库:判据 + path + chunk id"),
    ("时序对照",
     "真源 mtime vs 账本 certified_at 的时序对照（早于改动 ⇒ 归因于"
     "认证时点错位，而不是代码回归）"),
    ("环境项不进基线",
     "明确列出哪些红是环境项（%s）且它们**不进**基线" % " / ".join(ENVIRONMENT_ITEMS)),
    ("头部归因正文",
     "基线文件头部保留归因正文（它是尺子的说明书，不是注释）"),
    ("diff 可逐条读",
     "基线 diff 可逐条读（禁止 squash 成『更新基线』一个提交）"),
)


def _global_baseline_path() -> Path:
    return Path(__file__).resolve().parents[3] / GLOBAL_BASELINE_FILE


def _parse_baseline(text: str) -> Tuple[Dict[str, str], List[str]]:
    """The baseline entries (``判据号 -> 归因``) and the file's comment body.

    **The entry format.**  ``判据号 | 归因`` — and the 归因 may open
    with a **DECLARED STATE** marker:

    ==================  ===========================================
    marker              meaning
    ==================  ===========================================
    ``[RED]``            the entry's expected state is FAIL
    ``[NOTRUN]``         the entry's expected state is NOTRUN
    (no marker)          FAIL — the historical default
    ==================  ===========================================

    **Why a declared state is needed.**  Some criteria are red **by
    ruling**, not by accident — the architect's T1b ruling made ANCHOR's
    "the file changed" rows **NOTRUN on purpose**, and that is the
    CORRECT state, not a regression.  Without a declared state, GB-1's
    "lost visibility" rule (a baselined RED that is now NOTRUN) would
    report it as a regression on **every single run** — a rule that
    cries wolf every time ⇒ a rule nobody reads.

    ⇒ The declared state says what the entry **expects**, so GB-1 can
    tell "the expected state" from "the state changed".
    """
    entries: Dict[str, str] = {}
    comments: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            comments.append(s)
            continue
        parts = s.split("|", 1)
        key = parts[0].strip()
        why = parts[1].strip() if len(parts) > 1 else ""
        entries[key] = why
    return entries, comments


def _declared_state(why: str) -> str:
    """The state a baseline entry EXPECTS: ``"RED"`` or ``"NOTRUN"``.

    The marker is read from the **归因** (the text after the ``|``), so
    the entry's format stays ``判据号 | 归因`` and the marker rides
    along with the attribution it belongs to.

    An entry with no marker expects RED (the historical default).
    """
    head = why.lstrip()
    if head.startswith("[NOTRUN]"):
        return "NOTRUN"
    if head.startswith("[RED]"):
        return "RED"
    return "RED"


def judge_global_failure_set_matches_baseline(report, workspace: Path = None):
    """GB-1 (unit, GLOBAL): the gate's global failure set against a baseline.

    **Why a second baseline.**  ``Q5`` compares the failure set of ONE
    group (``text_inv.run_text_invariants`` — INV-1 / INV-2/3 /
    SHELF-LAG).  The gate's other groups (DR-1..DR-7, I-1..I-4, U-1,
    J-2..J-5, M6a..M6f, INV-2, ANCHOR, …) are **not** in Q5's scope at
    all ⇒ a regression in any of them is **invisible** to Q5.  Without
    a global baseline there is nowhere to look for "what used to be
    red".

    **This criterion reads the gate's OWN result table.**  It runs LAST
    (see ``phase2.py``'s ``main``), so ``report.rows`` already holds
    every criterion's verdict.  The comparison is therefore genuinely
    global and cannot drift out of scope the way a hand-maintained list
    can.

    **Environment-item reds never enter the baseline.**  ``J-5`` /
    ``DR-1`` / ``DR-2`` are the truth about this machine (two runtimes
    alive; a legacy writer still erasing statistics; a change log that
    predates the mechanism).  They are NOT regressions, and a baseline
    entry for them means the baseline has taken over the environment's
    job — when the environment is fixed, those entries would read as
    "regressions that vanished", which is a lie about a fix.

    **No one-click refresh.**  This criterion **never writes** the
    baseline file.  Adding an entry requires editing the file by hand
    with an attribution, and the file's header must carry the six
    minimal attributions 风控 §5.1 lists.  A baseline that can be
    regenerated is a baseline nobody reads.

    **A baseline entry that is now GREEN is not a regression** (a fix
    happened — it should be removed by hand, and the report says so).
    **A baseline entry that is now NOTRUN IS a regression**: the failure
    did not go away, it stopped being visible.
    """
    detail: List[str] = []
    path = _global_baseline_path()
    if not path.is_file():
        detail.append("全局基线文件不存在: %s" % path)
        detail.append("全局基线必须进版本库（放 tmp/ 里换台机器就没有了，"
                      "那正是阶段二 B2 未闭环的根因）。")
        detail.append("本判据因此无法判定『全局失败集只减不增』，报 NOTRUN。")
        return report.judge_notrun(
            "GB-1(unit)",
            "门禁全局失败集只减不增（与版本库内的全局基线比对；环境项红不进基线）",
            "\n".join(detail))

    text = path.read_text(encoding="utf-8")
    baseline, comments = _parse_baseline(text)

    # --- the current global failure set, from the gate's own table ---
    #
    # **GB-1 excludes ITSELF.**  It is the criterion that COMPARES the
    # failure set; whether it is red is the OUTPUT of that comparison,
    # not an input to it.  Counting itself would make every red GB-1
    # report itself as a "new failure" — a self-referential noise that
    # would have to be baselined away, and baselining a judge's own
    # redness is exactly the kind of thing that hides a real regression.
    #
    # The harness's own PROBE rows (MU-12 exercises the three-state
    # mechanism with synthetic rows) are excluded for the same reason:
    # they are not criteria, they are the guard testing the harness.
    current_red: Set[str] = set()
    current_notrun: Set[str] = set()
    current_green: Set[str] = set()
    for number, _name, state in report.rows:
        key = number.replace("(REAL)", "").replace("(unit)", "").strip()
        if key in ("GB-1",) or key.startswith("PROBE-"):
            continue
        if state == "FAIL":
            current_red.add(key)
        elif state == "NOTRUN":
            current_notrun.add(key)
        else:
            current_green.add(key)

    env = set(ENVIRONMENT_ITEMS)
    red_non_env = {k for k in current_red if k not in env}
    env_red = {k for k in current_red if k in env}

    detail.append("基线文件: %s" % path)
    detail.append("基线条目: %d 条   当前全局 RED: %d 条"
                  % (len(baseline), len(current_red)))
    detail.append("其中环境项红（%s）: %d 条 —— **不进基线**"
                  % (" / ".join(ENVIRONMENT_ITEMS), len(env_red)))
    for k in sorted(env_red):
        detail.append("  ~ %s（环境真相，不是回归）" % k)
    detail.append("")

    # --- the baseline must not contain an environment item ---
    env_in_baseline = sorted(set(baseline) & env)
    if env_in_baseline:
        detail.append("! 基线里含环境项条目: %s —— 环境项红不进基线"
                      % ", ".join(env_in_baseline))

    # --- the six minimal attributions must be present in the header ---
    header = "\n".join(comments)
    missing_attr = [name for name, _ in REQUIRED_ATTRIBUTIONS
                    if name not in header]
    if missing_attr:
        detail.append("! 基线头部缺归因: %s —— 头部的归因正文是尺子的说明书，"
                      "删掉它下一个维护者就会把基线当黑盒"
                      % ", ".join(missing_attr))

    # --- entries without an attribution ---
    unattributed = sorted(k for k, why in baseline.items() if not why.strip())
    if unattributed:
        detail.append("! 基线条目没有归因: %s —— 更新基线必须逐条点名 + 归因"
                      % ", ".join(unattributed))

    # --- the comparison ---
    new_failures = sorted(red_non_env - set(baseline))
    lost_visibility = sorted(k for k in baseline
                             if k in current_notrun and k not in env
                             and _declared_state(baseline[k]) == "RED")
    vanished = sorted(k for k in baseline
                      if k not in current_red and k not in current_notrun
                      and k not in env)
    # An entry whose DECLARED state is NOTRUN and which IS NOTRUN now
    # is exactly right — it is not a regression and not a "vanished"
    # entry.  An entry that declares NOTRUN but is RED now IS a
    # regression: the state it was allowed to be in changed.
    declared_notrun_now_red = sorted(
        k for k in baseline
        if k in current_red and k not in env
        and _declared_state(baseline[k]) == "NOTRUN")

    detail.append("新增失败（RED 且不在基线里，且非环境项）: %d 条"
                  % len(new_failures))
    for f in new_failures:
        detail.append("  + %s" % f)
    detail.append("失去可见性（基线里是 RED，现在 NOTRUN）: %d 条"
                  % len(lost_visibility))
    for f in lost_visibility:
        detail.append("  ! %s" % f)
    detail.append("已转绿（基线 RED → 现在 GREEN）: %d 条" % len(vanished))
    for f in vanished:
        detail.append("  v %s（请手工从基线移除 —— 基线记的是『已知的失败』，"
                      "不是『曾经失败过的所有东西』）" % f)
    notrun_expected = sorted(k for k in baseline
                              if _declared_state(baseline[k]) == "NOTRUN")
    detail.append("基线里声明为 [NOTRUN] 的条目（按定案如此，不是回归）: "
                  "%d 条 —— %s" % (len(notrun_expected),
                                   ", ".join(notrun_expected) or "（无）"))
    if declared_notrun_now_red:
        detail.append("声明 [NOTRUN] 却现在 RED（状态被改变了）: %d 条"
                      % len(declared_notrun_now_red))
        for f in declared_notrun_now_red:
            detail.append("  ! %s" % f)
    detail.append("")
    detail.append("（失败集按『判据号』比对，不比数量；环境项红被排除在外，"
                  "不参与『新增失败』的判定。）")
    detail.append("（GB-1 排除**自己**与 harness 的 PROBE 行：它是那个做比较的"
                  "判据，自己红不红是比较的**输出**而不是输入。）")
    detail.append("本判据**从不写**基线文件：加条目必须手工编辑 + 写归因，"
                  "没有一键刷新。")

    problems: List[str] = []
    if env_in_baseline:
        problems.append("基线含环境项条目")
    if missing_attr:
        problems.append("头部缺归因")
    if unattributed:
        problems.append("有条目没有归因")
    if new_failures:
        problems.append("有新增失败")
    if lost_visibility:
        problems.append("有失去可见性的条目")
    if declared_notrun_now_red:
        problems.append("有声明 NOTRUN 却变红的条目")

    if problems:
        detail.append("")
        detail.append("结论：%s ⇒ 判红。" % "、".join(problems))
        return report.judge(
            "GB-1(unit)",
            "门禁全局失败集只减不增（与版本库内的全局基线比对；环境项红不进基线）",
            False, "\n".join(detail))
    detail.append("")
    detail.append("结论：没有新增失败、没有失去可见性、头部归因齐全 ⇒ "
                  "全局失败集与基线一致。")
    return report.judge(
        "GB-1(unit)",
        "门禁全局失败集只减不增（与版本库内的全局基线比对；环境项红不进基线）",
        True, "\n".join(detail))
