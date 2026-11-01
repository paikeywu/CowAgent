#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-Y · the ANCHOR ``~N`` baseline (架构师 T1b 定案).

T3d · 2026-11-01 · 业务集成测试工程师
=====================================

**The ruling this implements.**  意识体内核架构师 T1b §1.4:

    | 情形 | 判据动作 | verdict |
    | 文件未变（``files.hash`` == 当前内容） | 照旧重算 ``~N`` 并比对 | PASS/FAIL |
    | 文件已变（``files.hash`` ≠ 当前内容） | **不重算** ``~N`` | **NOTRUN** |
    | 文件无 ``files.hash`` | 无法判定基准 | **NOTRUN** |

**Why NOTRUN and not PASS** — this is the heart of the ruling:

* PASS would be **lying**: "the ``~N`` matches" is a claim about a file
  state that no longer exists.  The file was edited after the
  certification, so the recorded ``~N`` describes a *past* revision.
* FAIL would be **pointing at the wrong repair**: the true cause is a
  **stale projection** (the index was never re-cut after the edit),
  and that is DR-6's business.  A FAIL here would send the engineer
  to fix the *anchor* — i.e. to re-cut the projection by hand — which
  is the wrong repair for the wrong reason.

⇒ **the降级 is on the EXPECTED VALUE's SOURCE, not on the check's
EXISTENCE.**  The syntax check, the segment/span check and the
non-negativity check all stay exactly as they were (硬要求 ③).

**The three hard requirements (架构师 T1b §1.6), and where each lands**

===========================================  ==========================
requirement                                  where
===========================================  ==========================
① 三态 + 必须报「文件已变」的 path 名单      :func:`judge_anchor_tilde_n_uses_the_certified_baseline`
② 必须有 mutant（去掉基准必须红）          MU-T13 / MU-T14
③ 必须保留 ``~N`` 的语法与非负性检查        :func:`_check_syntax_and_nonnegativity`
===========================================  ==========================

**Why the comparison goes through ``file_hash.hash_matches``** — the
engineer's T2d normalisation (提交 ``2a5947f0``).  ``files.hash`` is
stored in **TWO historical shapes** (``sha256:``-prefixed by the
rebuilder, bare hex by the manager's scan path) and the two writers
also disagree on the **digest BASIS** (canonical text vs raw text).
A naive string comparison reads 1109 of 1193 live rows as "the file
changed" — **92.9% false alarms**.  ⇒ **one entry point, no private
copy**: ``hash_matches`` understands both shapes and both bases.

**The observable NOTRUN (交办 ③).**  A NOTRUN that only says "not
measured" gives a false green's twin: a reader cannot tell "I could
not measure" from "there was nothing to measure".  Here we DID measure
— we measured that the file CHANGED — so the report MUST name:

* the **count** of rows dropped per library,
* the **path list** (deduplicated) of the files that changed,
* the **stored hash** and the **current hash** for each changed file,
  so a reader can verify the "changed" verdict independently.

⇒ the NOTRUN is **not a silent hole**: it is a *pointer to the real
cause* (DR-6), carrying the evidence.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro
from agent.memory.acceptance.phase2_s3h import (
    parse_anchor_grammar,
    tail_blanks_of,
)
from agent.memory.body_pointer import resolve_truth_file
from agent.memory.file_hash import hash_matches, normalize_hash

ANCHOR_BASELINE_NAME = (
    "锚点 ~N 以认证时指纹为基准：文件未变⇒照旧比对(PASS/FAIL)，"
    "文件已变⇒NOTRUN（不重算期望值，但必须报出 path 名单）")


# ---------------------------------------------------------------------------
# The three states of ONE pointed row
# ---------------------------------------------------------------------------

class RowVerdict:
    """What the criterion says about ONE pointed row.

    Four outcomes, and the difference between them is the whole point:

    ==============  =========  ==========================================
    outcome         state      meaning
    ==============  =========  ==========================================
    ``measured_ok`` PASS       the file is unchanged AND ``~N`` matches
    ``measured_bad`` FAIL      the file is unchanged AND ``~N`` does NOT
                                 match ⇒ the anchor really is broken
    ``file_changed`` NOTRUN    the file changed after certification ⇒
                                 the expected value is stale
    ``no_baseline``  NOTRUN    there is no ``files.hash`` to compare
                                 against ⇒ nothing to measure against
    ==============  =========  ==========================================

    The syntax / span checks are NOT here: they do not depend on the
    file's content, so they are **never** downgraded (硬要求 ③).
    """

    __slots__ = ("outcome", "path", "detail")

    def __init__(self, outcome: str, path: str, detail: str = ""):
        self.outcome = outcome
        self.path = path
        self.detail = detail

    def __repr__(self) -> str:
        return "RowVerdict(%s, %s)" % (self.outcome, self.path)


# ---------------------------------------------------------------------------
# 硬要求 ③ · the checks that are NEVER downgraded
# ---------------------------------------------------------------------------

def _check_syntax_and_nonnegativity(anchor: Optional[str]) -> Tuple[bool, str]:
    """The ``~N`` SYNTAX and its NON-NEGATIVITY — never downgraded.

    硬要求 ③: *"必须保留 ``~N`` 的语法与非负性检查（降级的是**期望值来源**，
    不是检查的存在性）"*.

    :func:`parse_anchor_grammar` returns ``None`` for an anchor that
    does not match the grammar at all, and it returns ``None`` for a
    segment whose ``~N`` is negative or non-numeric (the parser rejects
    ``tail < 0``).  ⇒ a **broken syntax is still a FAIL**, even when
    the file has changed — because a broken syntax is a fact about the
    anchor STRING, and the anchor string does not go stale.

    (硬要求 ③ is why this function is called BEFORE the baseline
    comparison, and why its outcome is not part of the NOTRUN set.)
    """
    if anchor is None:
        return False, "锚点为空"
    parsed = parse_anchor_grammar(anchor)
    if parsed is None:
        return False, ("锚点不合语法（含 ~N 非负性检查）：%s" % anchor)
    return True, ""


def _segment_span_is_consistent(parsed: Dict[str, object]) -> Tuple[bool, str]:
    """The ``+S`` span invariant — also never downgraded.

    The first segment must start at ``L<start>`` and the last must end
    at ``L<end>``.  This is a fact about the anchor STRING alone ⇒ it
    does not go stale when the file does.
    """
    segs = parsed["segments"]
    if not segs:
        return False, "段序列为空"
    if segs[0].start != parsed["start"] or segs[-1].end != parsed["end"]:
        return False, ("段/跨度不自洽：+S 首段起点=%d（应 %d），末段终点=%d（应 %d）"
                       % (segs[0].start, parsed["start"],
                          segs[-1].end, parsed["end"]))
    return True, ""


# ---------------------------------------------------------------------------
# The baseline comparison — through the engineer's ONE entry point
# ---------------------------------------------------------------------------

class BaselineVerdict:
    """The ``files.hash`` baseline comparison for ONE file.

    ``state`` is one of:

    * ``"match"``   — the stored fingerprint still matches the file
      ⇒ the expected value (``~N``) is still valid ⇒ measure it;
    * ``"changed"`` — the file changed after certification ⇒ the
      expected value is stale ⇒ NOTRUN (with evidence);
    * ``"absent"``  — there is no ``files.hash`` row, or the truth file
      cannot be read ⇒ nothing to compare against ⇒ NOTRUN.
    """

    __slots__ = ("state", "stored", "current", "reason")

    def __init__(self, state: str, stored: Optional[str] = None,
                 current: Optional[str] = None, reason: str = ""):
        self.state = state
        self.stored = stored
        self.current = current
        self.reason = reason


def compare_against_certified_baseline(stored_hash: Optional[str],
                                       truth: Optional[Path]
                                       ) -> BaselineVerdict:
    """Is the truth file still the one the ``files.hash`` was taken from?

    **The comparison goes through ``agent.memory.file_hash.hash_matches``
    — the engineer's T2d normalisation, NOT a private copy.**  Two
    historical shapes and two digest bases are in play; a private
    comparison would read 92.9% of the live rows as "changed".

    The current digest is computed **from the canonical text** — the
    same basis the rebuilder uses — so the reported "current hash" is
    comparable with a prefixed stored value by eye.
    """
    if not stored_hash:
        return BaselineVerdict("absent", reason="files 表没有这一行的指纹")
    if truth is None:
        return BaselineVerdict("absent", stored_hash, reason="真源文件定位不到")
    try:
        raw = truth.read_bytes()
        content = truth.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return BaselineVerdict("absent", stored_hash,
                               reason="真源文件读不到（%s: %s）"
                                      % (type(e).__name__, e))
    current = hashlib.sha256(
        content.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()
    if hash_matches(stored_hash, content, raw_bytes=raw):
        return BaselineVerdict("match", stored_hash, current)
    return BaselineVerdict("changed", stored_hash, current,
                           reason="认证后的指纹与当前内容不一致")


# ---------------------------------------------------------------------------
# The criterion
# ---------------------------------------------------------------------------

def judge_anchor_tilde_n_uses_the_certified_baseline(report: Report,
                                                      workspace: Path):
    """ANCHOR-B (REAL): the ``~N`` expectation comes from the certified
    fingerprint, and a changed file degrades to **NOTRUN with evidence**.

    **Three states, and the report MUST carry the changed-file list**
    (硬要求 ①).

    The measured expectation (T3d, 2026-11-01, live corpus, 16
    libraries, 3307 pointed rows):

    ==============================  ======  ===================================
    outcome                         rows    what it means
    ==============================  ======  ===================================
    锚点不合语法 / 段跨度不自洽      0      FAIL — never downgraded
    文件已变 ⇒ NOTRUN                153    stale projection (DR-6's business)
    无基准 ⇒ NOTRUN                   0    no ``files.hash`` to compare against
    ~N 不符（文件**未变**）           0    a genuinely broken anchor
    实测通过                         3007    the check still has teeth
    ==============================  ======  ===================================

    ⇒ **every one of the 102 rows the architect measured as "changed
    after certification" is a NOTRUN, and there is NOT ONE real ``~N``
    mismatch left.**  The old criterion called those 102 rows FAIL and
    pointed the engineer at the ANCHOR; the truth is that the FILES
    changed and the PROJECTION is stale.

    **The NOTRUN is observable** (交办 ③): the report names the count,
    the deduplicated path list, and the stored/current hash of every
    changed file — so a reader can verify the "changed" verdict
    independently, and can see exactly which files need re-cutting.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "ANCHOR(REAL)", ANCHOR_BASELINE_NAME,
            "没有活库 —— 不可判定，不是通过。")

    total_rows = 0
    syntax_bad = 0
    span_bad = 0
    measured_ok = 0
    measured_bad = 0
    file_changed = 0
    no_baseline = 0
    red_libraries: List[str] = []
    notrun_libraries: List[str] = []
    changed_files: Dict[Tuple[str, str], BaselineVerdict] = {}

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            conn.row_factory = sqlite3.Row
            files: Dict[str, str] = {
                r["path"]: r["hash"]
                for r in conn.execute("SELECT path, hash FROM files")
            }
            rows = conn.execute(
                "SELECT body_path, body_anchor FROM chunks "
                "WHERE body_path IS NOT NULL AND body_anchor IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        lib_meas = lib_bad = lib_changed = lib_nobase = 0
        lib_syntax = lib_span = 0
        for row in rows:
            bpath = row["body_path"]
            banchor = row["body_anchor"]
            total_rows += 1

            # --- 硬要求 ③ · the syntax and the span never degrade ---
            ok, why = _check_syntax_and_nonnegativity(banchor)
            if not ok:
                syntax_bad += 1
                lib_syntax += 1
                continue
            parsed = parse_anchor_grammar(banchor)
            ok, why = _segment_span_is_consistent(parsed)
            if not ok:
                span_bad += 1
                lib_span += 1
                continue
            if not parsed["has_segments"]:
                # No ``+S`` ⇒ no ``~N`` to check.  Nothing to degrade.
                continue

            # --- the baseline comparison (the ONE entry point) ---
            stored = files.get(bpath)
            truth = resolve_truth_file(lib.workspace, bpath)
            base = compare_against_certified_baseline(stored, truth)
            if base.state == "absent":
                no_baseline += 1
                lib_nobase += 1
                continue
            if base.state == "changed":
                file_changed += 1
                lib_changed += 1
                changed_files[(lib.name, bpath)] = base
                continue

            # --- the file is unchanged ⇒ the expectation is valid ---
            try:
                content = truth.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                no_baseline += 1
                lib_nobase += 1
                continue
            lines = content.split("\n")
            bad = False
            for seg in parsed["segments"]:
                if seg.start < 1 or seg.start > len(lines):
                    continue
                end = min(seg.end, len(lines))
                if tail_blanks_of(lines, seg.start, end) != seg.tailblanks:
                    bad = True
                    break
            if bad:
                measured_bad += 1
                lib_bad += 1
            else:
                measured_ok += 1
                lib_meas += 1

        # --- the PER-LIBRARY three-state (架构师 T1b: 9 个库 NOTRUN，
        #     其余 7 个库 GREEN) ---
        if lib_bad or lib_syntax or lib_span:
            red_libraries.append(lib.name)
        elif lib_changed or lib_nobase:
            notrun_libraries.append(lib.name)
        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        detail.append(
            "      有指针行=%-5d 实测通过=%-5d ~N 不符=%-4d 文件已变(NOTRUN)=%-4d "
            "无基准(NOTRUN)=%-3d 语法/跨度坏=%d"
            % (len(rows), lib_meas, lib_bad, lib_changed, lib_nobase,
               lib_syntax + lib_span))

    detail.append("")
    detail.append(
        "合计: 有指针行=%d  实测通过=%d  ~N 不符(FAIL)=%d  文件已变(NOTRUN)=%d  "
        "无基准(NOTRUN)=%d  语法/跨度坏(FAIL)=%d"
        % (total_rows, measured_ok, measured_bad, file_changed,
           no_baseline, syntax_bad + span_bad))
    detail.append("基准来源: files.hash（认证时的真源指纹）；"
                  "比较走 agent.memory.file_hash.hash_matches"
                  "（工程师 T2d 的唯一归一入口，两派历史形态都兼容）")

    # --- 交办 ③ · the NOTRUN must be OBSERVABLE ---
    if changed_files:
        detail.append("")
        detail.append("★ 「文件已变」名单（%d 个文件 / %d 行）—— 这些行"
                      "**未测量**：期望值（旧 ~N）已随文件改动而失效。"
                      "真因是**投影陈旧**，由 DR-6 报，不是锚点坏了："
                      % (len(changed_files), file_changed))
        for (lib_name, path), base in sorted(changed_files.items()):
            detail.append(
                "    ~ %s::%s" % (lib_name, path))
            detail.append(
                "        认证时指纹=%s  当前内容指纹=%s  %s"
                % (base.stored, base.current, base.reason))
    else:
        detail.append("")
        detail.append("★ 「文件已变」名单：无（0 个文件）")

    if total_rows == 0:
        return report.judge_notrun(
            "ANCHOR(REAL)", ANCHOR_BASELINE_NAME,
            "\n".join(detail) + "\n没有带指针的行 —— 不可判定，不是通过。")

    if syntax_bad or span_bad or measured_bad:
        detail.append("")
        detail.append("  —— FAIL 的库: %s" % (", ".join(red_libraries) or "（无）"))
        return report.judge(
            "ANCHOR(REAL)", ANCHOR_BASELINE_NAME,
            False, "\n".join(detail))

    # --- the whole-criterion state ---
    #
    # A library whose rows were ALL measured and are fine is GREEN.
    # A library with rows that COULD NOT be measured (the file changed,
    # or there is no baseline) is NOTRUN — **not green**.  The criterion
    # as a whole is NOTRUN if ANY library is NOTRUN, because the
    # criterion did not measure everything it set out to measure, and
    # "not measured" must never be reported as "measured fine".
    if notrun_libraries:
        detail.append("")
        detail.append("  —— NOTRUN 的库（文件已变 / 无基准 ⇒ 期望值已失效，"
                      "本轮未测量）: %s" % ", ".join(notrun_libraries))
        detail.append("     真因是**投影陈旧**（DR-6 报），不是锚点坏了；"
                      "重新切块后本判据会自动回到 GREEN。")
        return report.judge_notrun(
            "ANCHOR(REAL)", ANCHOR_BASELINE_NAME, "\n".join(detail))

    return report.judge(
        "ANCHOR(REAL)", ANCHOR_BASELINE_NAME,
        True, "\n".join(detail))
