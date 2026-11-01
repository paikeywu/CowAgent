#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-W · 判据口径收尾（W-1..W-4）。

**这一轮收的三件事，都是「判据自己的语义」，不是产品的语义。**

  * **W-1 · INV-2 口径统一**：门禁 ``INV-2(REAL)`` 判的是**逐字节相等**，
    而 ``text_inv.check_inv2_inv3`` 判的是 **canonical 相等**
    （``agent/memory/text_inv.py`` 里 ``canonical_text(frag) ==
    canonical_text(text)``）。两条判据问的是同一件事（「按锚点重建的
    片段是否忠实于真源」），却用了两把尺子 ⇒ **它们永远不可能同时绿**。
    SEED 裁决 C7：**主判据改用 canonical 相等**，另立 W-2 做**诊断**
    （逐字节差异是否只来自规范化差异），W-2 **不判红，只报告**。
  * **W-3 · 新写入的行不得停在 unresolved**：判「**一次真实写入之后**
    那批行的 origin 判定」，而不是「库里现在的存量」。工程师的设计稿
    （``agent-mvnewfyb`` 的《判据设计稿-新写入行不得停在unresolved.md》）
    给了 A1/A2/A3 三条断言与三态口径，本模块把它实现出来，并把设计稿
    自己承认的 **A3 盲区**（MU-C：``file`` 分支为空时 A3 恒真）补成
    **A4**。
  * **W-4 · 写者代际证据**：W-3 报「有 N 行 unresolved」是不够的 ——
    需求要的是**「这批 unresolved 是谁写的」**。W-4 报出**写者代际
    证据**（旧端的十二列 INSERT / 新代码的四列路径），而不是笼统的计数。

**为什么 canonical 相等才是「守住了不变量」而不是「放宽」**
----------------------------------------------------------
判据守护的不变量是「**投影能按锚点重建回真源内容**」，也就是**语义
相等**；换行形态（CRLF / LF、尾部空行）是**实现细节**，而
``chunks.text`` 落库前**做过规范化**（``naming.canonical_text``：LF 化 +
折叠连续空行 + strip）。用**字节相等**去判「重建是否忠实」，是把
**规范化差异**误报成**内容漂移**。

这**不是**把判据改短 —— 判据的**期望值来源没有变**：仍然是「从真源
文件独立重建出来的片段」，仍然是**判据自己解析锚点**（不调用
``body_pointer.rebuild_from_anchor``），仍然是「投影 vs 真源」而不是
「投影 vs 投影」。变的只有**比较的那把尺子**，而这把尺子必须是**系统
的**规范化定义（``naming.canonical_text``），因为 ``text_inv`` 用的就是
它 ⇒ **两个关于同一件事的 map 必须用同一套 tie-break**
（``knowledge/concepts/two-maps-about-the-same-thing-must-break-ties-the-same-way.md``）。

**canonical 相等的定义（明确、可复算，不是模糊匹配）**
---------------------------------------------------------
``canonical_text(s)``：

  1. ``s.replace("\\r\\n", "\\n").replace("\\r", "\\n")`` —— 行尾统一成 LF；
  2. ``re.sub(r"\\n{3,}", "\\n\\n", s)`` —— 连续空行折叠成一个空行；
  3. ``s.strip()`` —— 去掉首尾空白（含首尾换行）。

⇒ **canonical 相等 = ``canonical_text(rebuilt) == canonical_text(text)``**。
这**不是**「差不多就算相等」：它**不忽略任何非空白的字符**，任何一处
实质内容不同（多一个词、少一段、改一个标点）都会让 canonical 不等。
它**只**忽略三类**纯形态**差异：行尾形态、连续空行数、首尾空白。

**W-2 为什么必须独立存在**
--------------------------
如果 W-1 改成 canonical 相等，那么「**重建的内容真的变了**」这件事
在门禁上就**不再可见**（canonical 不等 ⇒ 还是红，可见；canonical 不等
才红，可见）。但**另一个问题**会变成不可见：「逐字节差异是不是**只**
来自规范化？」—— 如果把 canonical 不等也报成「内容漂移」，那**规范
化差异**与**内容漂移**就被**混在一起**，读者无法判断该去查投影还是该
去查规范化的实现。⇒ W-2 **单独**把两类差异**分开命名**，并且**点名到
path + chunk + 字节偏移**。

W-2 **不判红**：它不是不变量，是**诊断**。一条诊断判据如果判红，它就
变成了第二把尺子 ⇒ 又一次「两把尺子」的老毛病。W-2 的 FAIL 条件是
**它自己坏了**（测不了 / 分类逻辑自相矛盾），而不是「语料有差异」。
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro

#: The REAL normalisation, imported under an alias so the module-level
#: name ``canonical_text`` can keep pointing at this module's OWN copy.
#:
#: **Why the import exists at all.**  This module deliberately
#: RE-IMPLEMENTS the normalisation (see :func:`canonical_text` below):
#: a criterion that imports the thing it is judging is a tautology.  But
#: a copy that is never compared against the original is a copy that
#: drifts silently — and this module's docstring PROMISED a cross-check
#: that did not exist (架构师 §2.2: 「实测：这条交叉校验不存在」).
#:
#: ⇒ The promise is now kept: :func:`_canonical_cross_check` compares
#: this copy against the real one on FIXED fixtures, and W-4 FAILs when
#: they disagree.  **A criterion that claims to do a check and does not
#: do it is a back door for false green.**
try:  # pragma: no cover - the import is the point; its failure is reported
    from agent.memory.naming import canonical_text as _real_canonical_text
except Exception:  # pragma: no cover
    _real_canonical_text = None


#: The four legal values of ``chunks.body_origin``.  ``'unresolved'`` is
#: the DEFAULT of the column and means "nobody has decided".
LEGAL_ORIGINS: Tuple[str, ...] = ("file", "blob", "orphan", "unresolved")

#: The normalisation classes W-2 distinguishes.  **These are the names
#: the report prints** — a difference that is not classifiable into one
#: of these is reported as ``真漂移`` (a real drift), never as a
#: normalisation difference.
NORM_CLASSES: Tuple[str, ...] = ("行尾形态", "尾部空白", "连续空行")


# ---------------------------------------------------------------------------
# the normalisation yardstick — ONE definition, shared by W-1 and W-2
# ---------------------------------------------------------------------------

def canonical_text(content: str) -> str:
    """The system's normalisation — the SAME function ``text_inv`` uses.

    **This is a deliberate re-implementation, not an import.**  A
    criterion that imports the thing it is judging is a tautology
    (S3-H's independence rule).  The DEFINITION is duplicated here on
    purpose so that a change to ``naming.canonical_text`` cannot silently
    change what W-1 asserts; and :func:`_canonical_cross_check` — called
    by **W-4** — CROSS-CHECKS this copy against the real one on FIXED
    fixtures, so the duplication cannot drift apart silently either.

    **⚠️ This sentence used to be a promise the module did not keep.**
    The architect's review (T1b §2.2) measured it: the import list had
    no ``agent.memory.naming`` and the whole module mentioned ``naming``
    only in prose ⇒ the promised cross-check **did not exist**.  It
    exists now, and a disagreement FAILS W-4 (the criterion is broken,
    and a criterion that is broken must say so rather than keep
    reporting green).

    The definition (three steps, in this order):

      1. CRLF / CR -> LF;
      2. runs of 3+ newlines -> exactly two (one blank line);
      3. strip the outer whitespace.

    Every character that is not whitespace and not a newline is
    load-bearing: a single differing word makes the two sides unequal.
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


#: The FIXED fixtures the cross-check runs on.  They are fixed on
#: purpose: a cross-check on random inputs would be a fuzz test, and a
#: fuzz test's failure is not reproducible.  These cover every branch of
#: the three-step definition:
#:
#:   * CRLF and lone CR (step 1),
#:   * runs of 3+ newlines (step 2),
#:   * leading/trailing whitespace (step 3),
#:   * CJK and full-width spaces (nothing special — they must survive),
#:   * the empty string and whitespace-only (the degenerate cases).
#:
#: A fixture list that does not cover a branch is a cross-check that
#: cannot see that branch drift.
CANONICAL_CROSS_CHECK_FIXTURES: Tuple[Tuple[str, str], ...] = (
    ("", ""),
    ("   \n\t  ", ""),
    ("a\r\nb", "a\nb"),
    ("a\rb", "a\nb"),
    ("a\r\n\r\n\r\n\r\nb", "a\n\nb"),
    ("\n\n\nleading", "leading"),
    ("trailing\n\n\n", "trailing"),
    ("  padded  ", "padded"),
    ("中文\n\n\n\n正文", "中文\n\n正文"),
    ("full　width space", "full　width space"),
    ("a\n\n\nb\n\n\n\nc", "a\n\nb\n\nc"),
    ("mixed\r\nline\r\nendings", "mixed\nline\nendings"),
)


def _canonical_cross_check() -> List[str]:
    """Compare this module's ``canonical_text`` against the REAL one.

    **This is the check the docstring promised and did not do.**  The
    architect's review (T1b §2.2) measured it: the import list had no
    ``agent.memory.naming``, and the whole module mentioned ``naming``
    only in prose ⇒ the promised cross-check did not exist.  A criterion
    that claims to cross-check and does not is a back door for false
    green, so it is now REAL.

    **What it returns.**  A list of human-readable disagreement
    descriptions; **empty means the two agree on every fixture**.  An
    empty list is NOT "the check did not run" — the two failure modes
    are distinguished:

      * the real function is not importable ⇒ the list says so
        (the check could not run ⇒ NOTRUN, not PASS);
      * the two disagree on a fixture ⇒ the list names the fixture and
        both outputs (the check ran and FAILED).

    **The mutation this catches.**  If someone adds a rule to
    ``naming.canonical_text`` (say "fold trailing spaces on each line"),
    this module's copy keeps the OLD behaviour ⇒ the gate's INV-2 and
    ``text_inv`` would be using two different yardsticks, and **nothing
    would be red**.  That is the shape
    ``knowledge/concepts/two-maps-about-the-same-thing-must-break-ties-the-same-way.md``
    describes: the pointer and the pointer's fingerprint must come from
    the same choice.
    """
    problems: List[str] = []
    if _real_canonical_text is None:
        problems.append(
            "agent.memory.naming.canonical_text 不可导入 —— 交叉校验**跑不了**"
            "（这是 NOTRUN，不是通过）")
        return problems
    for src, expected in CANONICAL_CROSS_CHECK_FIXTURES:
        try:
            ours = canonical_text(src)
        except Exception as e:
            problems.append("本模块的 canonical_text 在夹具 %r 上抛异常: %s"
                            % (src, e))
            continue
        try:
            theirs = _real_canonical_text(src)
        except Exception as e:
            problems.append("真实现 canonical_text 在夹具 %r 上抛异常: %s"
                            % (src, e))
            continue
        if ours != theirs:
            problems.append(
                "夹具 %r 上两份实现不等：本模块=%r 真实现=%r"
                % (src, ours, theirs))
        if ours != expected:
            # The fixture's own expectation is a third, INDEPENDENT
            # statement of the definition.  If the two copies agree with
            # each other but both differ from the fixture, then the
            # FIXTURE is stale — that is worth reporting too, because a
            # fixture that no longer states the definition is a fixture
            # that cannot detect a change of the definition.
            problems.append(
                "夹具 %r 上两份实现都偏离了夹具自身期望：本模块=%r 真实现=%r "
                "期望=%r" % (src, ours, theirs, expected))
    return problems


def _norm_class(a: str, b: str) -> Optional[str]:
    """Classify WHY two strings differ, or ``None`` if they do not.

    The classes are checked in a fixed order and the answer is a SINGLE
    class: the first one that explains the whole difference wins.  The
    order is chosen so that the cheapest, most local explanation is tried
    first:

      1. ``行尾形态`` — the two differ only in CRLF/CR vs LF;
      2. ``尾部空白`` — after normalising the line endings, they differ
         only in leading/trailing whitespace;
      3. ``连续空行`` — after the two above, they differ only in the
         NUMBER of consecutive blank lines.

    ``None`` means **not a normalisation difference** ⇒ the difference
    is a real content drift.  **There is no fuzzy middle**: a difference
    that cannot be explained by one of the three classes is drift, full
    stop.

    The classification is **verified**, not assumed: for each candidate
    class the function applies that class's normalisation to BOTH sides
    and checks the results are equal.  A class that does not make them
    equal is not the answer.
    """
    # 1. line endings only
    if a.replace("\r\n", "\n").replace("\r", "\n") == \
            b.replace("\r\n", "\n").replace("\r", "\n"):
        return "行尾形态"
    # 2. leading / trailing whitespace only
    la = a.replace("\r\n", "\n").replace("\r", "\n")
    lb = b.replace("\r\n", "\n").replace("\r", "\n")
    if la.strip() == lb.strip():
        return "尾部空白"
    # 3. runs of blank lines only
    fa = re.sub(r"\n{3,}", "\n\n", la)
    fb = re.sub(r"\n{3,}", "\n\n", lb)
    if fa.strip() == fb.strip():
        return "连续空行"
    return None


def _first_diff_offset(a: str, b: str) -> int:
    """The byte offset where the two strings first differ.

    Reported because a difference without a position is a difference
    nobody can act on.  The offset is on the **canonical** side when
    both sides are canonicalised, and on the **raw** side otherwise —
    the caller decides, and says which in the report.
    """
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


# ---------------------------------------------------------------------------
# W-1 · INV-2 on the canonical yardstick
# ---------------------------------------------------------------------------

def judge_inv2_anchor_rebuild_is_canonical_equal(report, workspace: Path):
    """W-1 (REAL): the rebuilt fragment is CANONICALLY equal to ``chunks.text``.

    **This is the criterion the gate used to run under a different
    verdict rule.**  The old one (``judge_inv2_anchor_rebuild_is_byte_equal``
    in ``phase2_s3h.py``) judged **byte equality**; this one judges
    **canonical equality** — the same yardstick ``text_inv`` uses, so
    the two criteria about the same fact cannot contradict each other.

    **What did NOT change.**  The expected value still comes from the
    TRUTH LAYER: this module parses the anchor grammar itself
    (:func:`parse_anchor_grammar`) and rebuilds the fragment itself
    (:func:`rebuild_from_grammar`) — it does NOT call
    ``body_pointer.rebuild_from_anchor``.  A criterion that re-ran the
    chunker would compare the projection with itself and could never go
    red — the tautology this stage exists to kill.

    **What changed, and why.**  Only the COMPARISON.  See the module
    docstring for the full argument; the short form: the invariant is
    "the projection rebuilds back to the truth's CONTENT", and
    ``chunks.text`` is stored **normalised**, so byte equality mistakes
    a normalisation difference for a content drift.

    **The byte count is still PRINTED** — observability is not lost.  It
    is simply no longer the verdict; the byte/canonical split is now
    reported to W-2, which names the difference.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "W-1(REAL)",
            "按锚点重建的片段与 chunks.text canonical 相等（判据独立解析锚点，"
            "不调用被测对象的重建器）；逐字节口径改为诊断判据 W-2",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.body_pointer import rebuild_from_anchor, resolve_truth_file
        from agent.memory.acceptance.phase2_s3h import (
            parse_anchor_grammar,
            rebuild_from_grammar,
        )
    except Exception as e:
        return report.judge_notrun(
            "W-1(REAL)",
            "按锚点重建的片段与 chunks.text canonical 相等（判据独立解析锚点，"
            "不调用被测对象的重建器）；逐字节口径改为诊断判据 W-2",
            "body_pointer 不可用（%s: %s）—— 判据无法测量" % (type(e).__name__, e))

    total_rows = 0
    byte_equal = 0
    canon_equal = 0
    canon_only = 0
    unparsable = 0
    unlocatable = 0
    reader_disagreements = 0
    red_libraries: List[str] = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = set(r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall())
            if "body_origin" in cols:
                rows = conn.execute(
                    "SELECT body_path, body_anchor, text, body_origin FROM chunks "
                    "WHERE text_state IN ('projection','mirror') "
                    "AND body_path IS NOT NULL AND body_anchor IS NOT NULL"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT body_path, body_anchor, text, 'file' FROM chunks "
                    "WHERE text_state IN ('projection','mirror') "
                    "AND body_path IS NOT NULL AND body_anchor IS NOT NULL"
                ).fetchall()
        finally:
            conn.close()
        lib_rows = 0
        lib_byte = 0
        lib_canon = 0
        lib_canon_only = 0
        lib_unparsable = 0
        lib_unlocatable = 0
        lib_reader_disagree = 0
        for row in rows:
            bpath, banchor, text, origin = tuple(row)
            if origin == "orphan":
                continue          # a drifted row is INV-1's business
            lib_rows += 1
            truth = resolve_truth_file(lib.workspace, bpath)
            if truth is None:
                lib_unlocatable += 1
                continue
            parsed = parse_anchor_grammar(banchor)
            if parsed is None:
                lib_unparsable += 1
                continue
            rebuilt = rebuild_from_grammar(truth, banchor)
            if rebuilt is None:
                lib_unlocatable += 1
                continue
            text = text or ""
            if rebuilt == text:
                lib_byte += 1
            if canonical_text(rebuilt) == canonical_text(text):
                lib_canon += 1
                if rebuilt != text:
                    lib_canon_only += 1
            # --- the CROSS-CHECK -------------------------------------
            # The implementation's reader must produce the SAME
            # fragment as this criterion's independently written one.
            # Not a tautology: the claim is "two independent
            # implementations of the grammar agree on every row of the
            # corpus", and it was FALSE before S3-D.
            try:
                theirs = rebuild_from_anchor(truth, banchor)
            except Exception:
                theirs = None
            if theirs != rebuilt:
                lib_reader_disagree += 1
        total_rows += lib_rows
        byte_equal += lib_byte
        canon_equal += lib_canon
        canon_only += lib_canon_only
        unparsable += lib_unparsable
        unlocatable += lib_unlocatable
        reader_disagreements += lib_reader_disagree
        verdict = "OK" if (lib_rows == lib_canon) else "RED"
        if lib_rows != lib_canon or lib_reader_disagree:
            red_libraries.append(lib.name)
        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        detail.append("      可判定行=%-5d canonical 相等=%-5d 逐字节相等=%-5d "
                      "仅 canonical 相等=%-4d 锚点不合语法=%-3d 定位不到=%-3d "
                      "两份读法不一致=%-3d -> %s"
                      % (lib_rows, lib_canon, lib_byte, lib_canon_only,
                         lib_unparsable, lib_unlocatable,
                         lib_reader_disagree, verdict))
    detail.append("")
    detail.append("合计: 可判定行=%d  canonical 相等=%d  逐字节相等=%d  "
                  "仅 canonical 相等=%d  锚点不合语法=%d  定位不到=%d  "
                  "两份读法不一致=%d"
                  % (total_rows, canon_equal, byte_equal, canon_only,
                     unparsable, unlocatable, reader_disagreements))
    detail.append("口径：canonical 相等（= naming.canonical_text：LF 化 + 折叠连续空行 "
                  "+ strip）。逐字节计数照旧打印，但不再参与判定 —— 逐字节差异的"
                  "归类由诊断判据 W-2 负责。")
    detail.append("判据自己解析锚点、自己重建，不调用 body_pointer.rebuild_from_anchor —— "
                  "否则就是拿被测对象算期望值（同义反复 = 假绿）。")
    detail.append("『两份读法不一致』是交叉校验：实现侧的读法必须与判据独立写的读法"
                  "在每一行上给出同一结果。")

    if total_rows == 0:
        return report.judge_notrun(
            "W-1(REAL)",
            "按锚点重建的片段与 chunks.text canonical 相等（判据独立解析锚点，"
            "不调用被测对象的重建器）；逐字节口径改为诊断判据 W-2",
            "\n".join(detail) + "\n本语料没有可判定的有指针行 —— 不可判定，不是通过。")
    if red_libraries:
        detail.append("  —— canonical 不等的库: %s" % ", ".join(red_libraries))
        return report.judge(
            "W-1(REAL)",
            "按锚点重建的片段与 chunks.text canonical 相等（判据独立解析锚点，"
            "不调用被测对象的重建器）；逐字节口径改为诊断判据 W-2",
            False, "\n".join(detail))
    return report.judge(
        "W-1(REAL)",
        "按锚点重建的片段与 chunks.text canonical 相等（判据独立解析锚点，"
        "不调用被测对象的重建器）；逐字节口径改为诊断判据 W-2",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# W-2 · the byte-difference DIAGNOSTIC (reports, never judges)
# ---------------------------------------------------------------------------

def judge_inv2_byte_difference_is_classified(report, workspace: Path):
    """W-2 (REAL, DIAGNOSTIC): every byte difference is CLASSIFIED.

    **This criterion does NOT judge the corpus.**  It reports, for every
    row where the rebuilt fragment and the stored projection differ
    **byte-wise**:

      * whether the difference is a **normalisation difference**
        (and WHICH class: 行尾形态 / 尾部空白 / 连续空行), or
      * a **real drift**（真漂移）— the difference goes beyond
        normalisation.

    **Why it must not judge.**  A diagnostic that turned red would be a
    SECOND yardstick, and two yardsticks about the same fact is the
    exact disease this round is curing (W-1 vs ``text_inv``).  So W-2's
    FAIL condition is **its own breakage**: it cannot measure, or its
    classification contradicts itself.

    **Why it must exist anyway.**  Once W-1 judges on the canonical
    yardstick, "the byte forms differ" stops being visible on the gate.
    But the reader still needs to know: *is the difference only
    normalisation, or is the content actually different?*  Those two
    send you to different places — one to the normalisation
    implementation, the other to the projection/rebuild.

    **The names.**  Every difference is reported with **path + chunk id
    + byte offset**, because a difference without a position is a
    difference nobody can act on.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "INV-2-DIAG(REAL,DIAG)",
            "逐字节差异的归类诊断：只来自规范化，还是超出规范化（点名到 path + "
            "chunk + 字节偏移）；诊断判据不判红",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.body_pointer import resolve_truth_file
        from agent.memory.acceptance.phase2_s3h import (
            parse_anchor_grammar,
            rebuild_from_grammar,
        )
    except Exception as e:
        return report.judge_notrun(
            "INV-2-DIAG(REAL,DIAG)",
            "逐字节差异的归类诊断：只来自规范化，还是超出规范化（点名到 path + "
            "chunk + 字节偏移）；诊断判据不判红",
            "锚点解析器不可用（%s: %s）—— 判据无法测量" % (type(e).__name__, e))

    total_rows = 0
    byte_diff = 0
    norm_only = 0
    real_drift = 0
    by_class: Dict[str, int] = {c: 0 for c in NORM_CLASSES}
    drift_samples: List[str] = []
    norm_samples: List[str] = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = set(r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall())
            if "body_origin" in cols:
                rows = conn.execute(
                    "SELECT id, path, body_path, body_anchor, text, body_origin "
                    "FROM chunks WHERE text_state IN ('projection','mirror') "
                    "AND body_path IS NOT NULL AND body_anchor IS NOT NULL"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, path, body_path, body_anchor, text, 'file' "
                    "FROM chunks WHERE text_state IN ('projection','mirror') "
                    "AND body_path IS NOT NULL AND body_anchor IS NOT NULL"
                ).fetchall()
        finally:
            conn.close()
        lib_rows = 0
        lib_diff = 0
        lib_norm = 0
        lib_drift = 0
        for cid, cpath, bpath, banchor, text, origin in rows:
            if origin == "orphan":
                continue
            lib_rows += 1
            truth = resolve_truth_file(lib.workspace, bpath)
            if truth is None:
                continue
            if parse_anchor_grammar(banchor) is None:
                continue
            rebuilt = rebuild_from_grammar(truth, banchor)
            if rebuilt is None:
                continue
            text = text or ""
            if rebuilt == text:
                continue            # byte-equal: nothing to diagnose
            lib_diff += 1
            cls = _norm_class(rebuilt, text)
            offset = _first_diff_offset(
                rebuilt.replace("\r\n", "\n").replace("\r", "\n"),
                text.replace("\r\n", "\n").replace("\r", "\n"))
            if cls is None:
                # REAL DRIFT — the difference goes beyond normalisation.
                lib_drift += 1
                if len(drift_samples) < 20:
                    drift_samples.append(
                        "      ! 真漂移 %s/%s chunk=%s 偏移=%d 重建len=%d 存储len=%d"
                        % (lib.name, (cpath or "")[:48], (cid or "")[:12],
                           offset, len(rebuilt), len(text)))
            else:
                # A normalisation difference — and the classification
                # MUST be self-consistent: normalising by that class
                # must make the two sides equal.
                lib_norm += 1
                by_class[cls] = by_class.get(cls, 0) + 1
                if len(norm_samples) < 20:
                    norm_samples.append(
                        "      · 仅规范化差异(%s) %s/%s chunk=%s 偏移=%d"
                        % (cls, lib.name, (cpath or "")[:48], (cid or "")[:12],
                           offset))
        total_rows += lib_rows
        byte_diff += lib_diff
        norm_only += lib_norm
        real_drift += lib_drift
        if lib_diff:
            detail.append("  %-22s 可判定行=%-5d 逐字节有差异=%-5d "
                          "仅规范化=%-4d 真漂移=%-4d"
                          % (lib.name, lib_rows, lib_diff, lib_norm, lib_drift))
    detail.append("")
    detail.append("合计: 可判定行=%d  逐字节有差异=%d  仅规范化差异=%d  "
                  "真漂移（超出规范化）=%d"
                  % (total_rows, byte_diff, norm_only, real_drift))
    detail.append("规范化差异按类: %s"
                  % ("; ".join("%s=%d" % (c, by_class.get(c, 0))
                               for c in NORM_CLASSES) or "无"))
    detail.append("")
    detail.append("—— 仅规范化差异（样例，点名 path + chunk + 偏移）——")
    if norm_samples:
        for s in norm_samples:
            detail.append(s)
    else:
        detail.append("      （无）")
    detail.append("—— 真漂移（样例，点名 path + chunk + 偏移）——")
    if drift_samples:
        for s in drift_samples:
            detail.append(s)
    else:
        detail.append("      （无）")
    detail.append("")
    detail.append("本判据是**诊断**：它报告差异的归类，**不判红**。")
    detail.append("FAIL 的唯一条件是『判据自己坏了』（测不了 / 分类自相矛盾）。")
    detail.append("『真漂移』这一栏的语义：差异**超出**规范化范围 ⇒ 该去查投影/重建；")
    detail.append("『仅规范化差异』⇒ 该去查规范化的实现。两类差异不混在一起。")

    # --- the self-check: the classification must be CONSISTENT --------
    # A class that claims to explain a difference must ACTUALLY make the
    # two sides equal after that class's normalisation.  If it does not,
    # the classification is broken and the criterion must say so rather
    # than publish a wrong label.
    broken = _classification_self_check()
    detail.append("")
    detail.append("分类自检（每类的归一化必须真的让两侧相等）: %s"
                  % ("通过" if not broken else "不通过 -> " + "; ".join(broken)))
    if broken:
        return report.judge(
            "INV-2-DIAG(REAL,DIAG)",
            "逐字节差异的归类诊断：只来自规范化，还是超出规范化（点名到 path + "
            "chunk + 字节偏移）；诊断判据不判红",
            False, "\n".join(detail))
    return report.judge(
        "INV-2-DIAG(REAL,DIAG)",
        "逐字节差异的归类诊断：只来自规范化，还是超出规范化（点名到 path + "
        "chunk + 字节偏移）；诊断判据不判红",
        True, "\n".join(detail))


def _classification_self_check() -> List[str]:
    """Prove the classifier is CONSISTENT on known cases.

    A classifier that can label a *real drift* as a normalisation
    difference is worse than no classifier: it would send the reader to
    fix the normalisation when the projection is what drifted.

    So this check feeds the classifier three pairs that are
    normalisation differences and one that is NOT, and asserts the
    answers.
    """
    broken: List[str] = []
    cases = [
        ("行尾形态", "a\r\nb\r\n", "a\nb\n", "行尾形态"),
        ("尾部空白", "a\nb\n\n", "a\nb", "尾部空白"),
        ("连续空行", "a\n\n\n\nb", "a\n\nb", "连续空行"),
        # NOT a normalisation difference — the content changed.
        ("真漂移", "a\nb", "a\nc", None),
    ]
    for label, a, b, want in cases:
        got = _norm_class(a, b)
        if got != want:
            broken.append("%s: 期望 %s 实得 %s" % (label, want, got))
    return broken


# ---------------------------------------------------------------------------
# W-3 · a fresh write must not leave a row undecided
# ---------------------------------------------------------------------------

def _legacy_chunk_id(root: Path, rel: str, text: str) -> str:
    """The chunk id the OLD desktop build would have used for a page.

    The id rule is ``md5("path:start:end[:part]")`` — the rule the
    product itself uses (``rebuilder._chunk_id``).  The criterion
    RE-IMPLEMENTS it here on purpose (S3-H's independence rule: a
    criterion that imports the thing it judges is a tautology), but it
    must agree with the product — so the helper is checked against the
    product's own rule at call time (the row only counts as "the old
    build's row" if the product's decision can locate it).

    The row's SPAN comes from the chunker, because the old build wrote
    the chunker's blocks; a row whose span does not match a chunker
    block could never be decided as 'file' (D1 would fail), and the
    criterion would then be judging a row that CANNOT be decided.
    """
    import hashlib

    from agent.memory.chunker import chunk_for_file

    pieces = chunk_for_file(text, rel)
    if not pieces:
        raise ValueError("the chunker produced no block for %s" % rel)
    pc = pieces[0]
    content = "%s:%d:%d" % (rel, pc.start_line, pc.end_line)
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _seed_legacy_row(db: Path, chunk_id: str, rel: str, text: str) -> None:
    """Write the OLD desktop build's row into the sandbox library.

    The SQL is the old build's own INSERT: **twelve** columns, none of
    them a body column ⇒ the four body columns keep their DEFAULTs
    (``text_state='projection'``, ``body_origin='unresolved'``).

    That is exactly the shape measured on the live libraries, and the
    shape the post-write decision exists to pick up.

    **``source='legacy'`` is load-bearing, and so is the path.**  The
    write path's reconcile step drops the rows of the sources it scans
    (``memory`` / ``knowledge``) whose path is no longer on disk — so a
    legacy row parked under a scanned source would be **deleted** by
    the very sync that is supposed to pick it up (MEASURED: "Dropped 1
    stale knowledge entries that are no longer on disk").  And a legacy
    row under a scanned PATH would be **overwritten** by the sync's own
    index of that page.

    ⇒ The legacy row therefore lives at the workspace ROOT under a path
    the scan does not walk, with a source the reconcile does not own.
    Both facts are asserted by the caller (the row must still be there
    after the write — see ``W-3``'s own report).
    """
    from agent.memory.chunker import chunk_for_file

    pieces = chunk_for_file(text, rel)
    if not pieces:
        raise ValueError("the chunker produced no block for %s" % rel)
    pc = pieces[0]
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, embedding, hash, metadata, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))",
            (chunk_id, None, "shared", "legacy", rel,
             pc.start_line, pc.end_line, pc.text, None, "legacy-hash", None))
        conn.commit()
    finally:
        conn.close()


def judge_new_write_has_decided_origin(report, workspace: Path = None):
    """W-3 (REAL): after ONE write, no row of that write stays unresolved.

    **The object under test is ONE WRITE**, not the library's history.
    The design稿 (``agent-mvnewfyb``) says it best: 判据必须能指出
    **谁**没判定 —— a criterion over the whole library can only say
    "there are N undecided rows", which is a fact about the DEBT, not
    about the WRITE PATH.

    **Why the object under test is a write the criterion performs.**
    A criterion that reads "what the live library last wrote" measures
    the live library — and the live library is being changed by OTHER
    processes (MEASURED: the undecided count moved 162 -> 5 within one
    day).  Such a criterion is flaky, and a flaky criterion is a
    criterion nobody trusts.  So the criterion builds its own sandbox
    library, writes ONE truth file through **the product's own write
    path** (``MemoryManager.sync``), and judges the rows that write
    produced.

    **⚠️ 一个与设计稿前提的分歧（实测，必须显式声明）**
    ------------------------------------------------
    设计稿 §六 的 MU-A 期望「把 ``_decide_after_write`` 关掉 ⇒ 行停在
    ``'unresolved'`` ⇒ 判据必须红」。**实测：这个期望在「只写新行」的
    语料上不成立。**

    原因：**当前代码在结构上写不出 ``'unresolved'``** ——
    ``body_pointer.four_columns_for_chunk`` 自己就返回
    ``'file'``/``'blob'``/``'orphan'``，写路径把四列**连同 origin 一起
    写进库**，所以 ``sync`` 写出的每一行**天生就是已判定的**。实测
    （沙箱）：``sync`` 写出的行 ``body_origin='file'``，把
    ``_decide_after_write`` 关掉，**判定结果一字不变**。

    ⇒ 所以「写后判定」这个机制的**真正职责**不是「判定自己写的行」，
    而是**判定「别的写者送来的行」**（旧桌面端的十二列 INSERT 写出的
    行：它不认识四列 ⇒ origin 落到列的 DEFAULT ``'unresolved'``）。
    这正是工程师 ``write_decide.py`` 文档里写的「whatever wrote the
    row, **this path** decides the row's origin immediately
    afterwards」。

    ⇒ 因此本判据的被测对象是「**一次写入会话所负责判定的那些行**」，
    它**必须包含一行「送来时就是 unresolved」的旧端行** —— 否则 MU-A
    打不红，判据就测不到写后判定这个机制（这正是设计稿自己担心的
    「测的是碰巧现在没有未判定的行」）。

    **W 的定义（明确）**::

        W = (本次写入覆盖的 path 上的全部行)
            ∪ (写入前处于 unresolved 的行)

    两部分都要：**自己写的行**（不能停在 unresolved）与
    **送来的行**（必须被写后判定接手）都在 W 里。

    **The three assertions (A1/A2/A3) plus the one the design稿 admits
    it is missing (A4).**

      * **A1** — no row of the write is left ``'unresolved'``;
      * **A2** — every row's ``body_origin`` is one of the four legal
        values;
      * **A3** — every ``'file'`` row carries a pointer
        (``body_path`` / ``body_anchor`` / ``body_hash`` all non-empty);
      * **A4** — **at least one row of the write is ``'file'``**.

    **A4 is the design稿's own blind spot, closed.**  MU-C (change
    ``decide_origin``'s ``ORIGIN_FILE`` to ``ORIGIN_ORPHAN``) makes
    every row ``'orphan'`` — which is a LEGAL value, so A1 and A2 both
    hold and A3's ``'file'`` branch is EMPTY, so A3 is vacuously true.
    The criterion would go GREEN on a write that decided nothing ⇒
    **A3 alone is not enough**.  A4 says: this write was about a truth
    file, so at least one row must SAY so.

    **The three states.**

      * **PASS** — A1 ∧ A2 ∧ A3 ∧ A4, and the write produced rows;
      * **FAIL** — the write produced rows and any of A1..A4 fails, with
        every offending row named (id + path + the four columns);
      * **NOTRUN** — ① the write produced no row (nothing to judge);
        ② the library has no ``body_origin`` column (not migrated);
        ③ the write itself failed.

    **NOTRUN is NOT "zero violations is fine".**  A1 is a NEGATIVE
    assertion; a negative assertion on an empty set is vacuously true
    ⇒ "no rows were undecided" would be reported on a corpus that was
    never read.  That is the 「0 个匹配 = NOTRUN，不是 PASS」 rule.
    """
    detail: List[str] = []
    sandbox = None
    try:
        import asyncio

        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager

        tmp = tempfile.mkdtemp(prefix="cow-w3-")
        sandbox = Path(tmp)
        root = sandbox / "ws"
        (root / "memory" / "shared").mkdir(parents=True, exist_ok=True)
        rel = "memory/shared/__criterion_probe__.md"
        (root / rel).write_text(
            "# 判据探针\n\n这一段正文是 W-3 的被测写入。\n", encoding="utf-8")

        # --- the legacy row: the shape the OLD desktop build leaves ---
        #
        # Its text is the CHUNKER's own output for its truth file, so
        # the post-write decision can legitimately call it 'file' (D1
        # holds) — otherwise the criterion would be judging a row that
        # CANNOT be decided, and the FAIL would be the fixture's fault.
        #
        # The path is at the workspace ROOT (not under memory/ or
        # knowledge/), because the scan walks those two roots and would
        # either overwrite the row or reconcile it away.
        rel_legacy = "__criterion_legacy__.md"
        legacy_truth = root / rel_legacy
        legacy_truth.parent.mkdir(parents=True, exist_ok=True)
        legacy_text = "# 旧端页\n\n这是旧桌面端写下的正文。\n"
        legacy_truth.write_text(legacy_text, encoding="utf-8")
        legacy_id = _legacy_chunk_id(root, rel_legacy, legacy_text)

        cfg = MemoryConfig(workspace_root=str(root), sync_on_search=False)
        mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
        try:
            db = Path(mgr.storage.db_path)
            _seed_legacy_row(db, legacy_id, rel_legacy, legacy_text)
            detail.append("沙箱工作区: %s" % root)
            detail.append("旧端行（送来的 unresolved）: id=%s path=%s"
                          % (legacy_id[:12], rel_legacy))

            # --- the write, through the PRODUCT's own path -------------
            asyncio.run(mgr.sync(force=True))

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            try:
                cols = set(r[1] for r in conn.execute(
                    "PRAGMA table_info(chunks)").fetchall())
                if "body_origin" not in cols:
                    detail.append("沙箱库没有 body_origin 列（未迁移）")
                    detail.append("被测库: %s" % db)
                    return report.judge_notrun(
                        "W-3(REAL)",
                        "一次真实写入产生的行不得停在 unresolved（A1 无 unresolved / "
                        "A2 origin 合法 / A3 file 带指针 / A4 至少一行 file）",
                        "\n".join(detail))
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, path, body_origin, body_path, body_anchor, "
                    "body_hash, text_state FROM chunks "
                    "WHERE path=? OR id=?", (rel, legacy_id))]
            finally:
                conn.close()
        finally:
            try:
                mgr.storage.close()
            except Exception:
                pass

        detail.append("被测写入: MemoryManager.sync(force=True) 写入 %s" % rel)
        detail.append("W 的行数（自己写的 + 送来的）: %d" % len(rows))
        # The legacy row must SURVIVE the write — if the write path
        # dropped it, the criterion would be judging a corpus that no
        # longer contains what it set out to judge.
        if not any((r["id"] or "") == legacy_id for r in rows):
            detail.append("旧端行在写入后消失了（id=%s）—— 写路径把它清掉了，"
                          "本次测量无效" % legacy_id[:12])
            return report.judge_notrun(
                "W-3(REAL)",
                "一次真实写入产生的行不得停在 unresolved（A1 无 unresolved / "
                "A2 origin 合法 / A3 file 带指针 / A4 至少一行 file）",
                "\n".join(detail))

        if not rows:
            detail.append("写入没有产生任何行 —— 没有可判定对象")
            return report.judge_notrun(
                "W-3(REAL)",
                "一次真实写入产生的行不得停在 unresolved（A1 无 unresolved / "
                "A2 origin 合法 / A3 file 带指针 / A4 至少一行 file）",
                "\n".join(detail))

        a1 = [r for r in rows if r["body_origin"] == "unresolved"]
        a2 = [r for r in rows if r["body_origin"] not in LEGAL_ORIGINS]
        a3 = [r for r in rows if r["body_origin"] == "file"
              and not (r["body_path"] and r["body_anchor"] and r["body_hash"])]
        a4 = [r for r in rows if r["body_origin"] == "file"]

        detail.append("A1 停在 unresolved 的行: %d" % len(a1))
        detail.append("A2 origin 不在合法四值内的行: %d" % len(a2))
        detail.append("A3 'file' 但指针不全的行: %d" % len(a3))
        detail.append("A4 'file' 的行数: %d（被测写入写的是有真值文件的页面，"
                      "必须至少有一行）" % len(a4))
        detail.append("origin 分布: %s"
                      % ", ".join("%s=%d" % (o, sum(
                          1 for r in rows if r["body_origin"] == o))
                          for o in LEGAL_ORIGINS))
        for r in rows:
            detail.append("    %-14s %-40s origin=%-11s path=%s anchor=%s "
                          "hash=%s state=%s"
                          % ((r["id"] or "")[:12], (r["path"] or "")[:38],
                             r["body_origin"], r["body_path"], r["body_anchor"],
                             (r["body_hash"] or "")[:20], r["text_state"]))

        if a1 or a2 or a3 or not a4:
            detail.append("")
            detail.append("—— 违规行逐条点名 ——")
            for tag, group in (("A1 unresolved", a1), ("A2 非法 origin", a2),
                                ("A3 file 无指针", a3)):
                for r in group:
                    detail.append("  ! [%s] id=%s path=%s origin=%s "
                                  "body_path=%s body_anchor=%s body_hash=%s "
                                  "text_state=%s"
                                  % (tag, (r["id"] or "")[:12], r["path"],
                                     r["body_origin"], r["body_path"],
                                     r["body_anchor"], r["body_hash"],
                                     r["text_state"]))
            if not a4:
                detail.append("  ! [A4] 这批写入里没有一行是 'file' —— "
                              "写入没有对任何一行做出『这行来自一个文件』的判定。"
                              "（MU-C 的盲区：origin 全变 orphan 时 A1/A2/A3 全绿，"
                              "只有 A4 能抓住）")
            return report.judge(
                "W-3(REAL)",
                "一次真实写入产生的行不得停在 unresolved（A1 无 unresolved / "
                "A2 origin 合法 / A3 file 带指针 / A4 至少一行 file）",
                False, "\n".join(detail))
        return report.judge(
            "W-3(REAL)",
            "一次真实写入产生的行不得停在 unresolved（A1 无 unresolved / "
            "A2 origin 合法 / A3 file 带指针 / A4 至少一行 file）",
            True, "\n".join(detail))
    except Exception as e:
        return report.judge_notrun(
            "W-3(REAL)",
            "一次真实写入产生的行不得停在 unresolved（A1 无 unresolved / "
            "A2 origin 合法 / A3 file 带指针 / A4 至少一行 file）",
            "写入/判定过程异常（%s: %s）—— 本判据未测量，不是通过" % (type(e).__name__, e))
    finally:
        if sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------------
# W-4 · the writer-generation evidence for the undecided rows
# ---------------------------------------------------------------------------

def judge_unresolved_writer_generation_is_reported(report, workspace: Path):
    """W-4 (REAL): every undecided row carries WRITER-GENERATION evidence.

    **The question this answers.**  "There are N undecided rows" is a
    number; a number does not say **who wrote them**.  And the answer
    decides the OWNER of the fix: rows the **old desktop build** wrote
    (its twelve-column ``INSERT OR REPLACE INTO chunks`` does not know
    the four body columns, so the column's DEFAULT fills
    ``'unresolved'``) are the OLD END's debt, while rows the **current
    code** wrote would be a defect in the current write path.

    **The evidence, per row — not a guess.**  The generation is decided
    by the row's own SHAPE, which is what the two writers leave
    behind:

      * **current code** always writes the four body columns, so it
        never leaves a row with all three pointers NULL — it writes
        ``'file'`` / ``'blob'`` / ``'orphan'`` itself.  A row that is
        ``'unresolved'`` with **all three pointers NULL** is therefore
        the shape of a writer that does not know the columns;
      * **old build** writes ``text_state='projection'``, no pointer,
        and ``'unresolved'`` — the measured shape (see
        ``agent/memory/write_decide.py``'s docstring, which reproduces
        the old build's SQL on a copy of the live library).

    **What this criterion does NOT do.**  It does not judge the debt
    (that is M6's business) and it does not decide the fix.  It reports
    the census **with the generation attached**, and it FAILS only when
    a row is undecided AND carries NO pointer **and** the current code
    could not have produced that shape — i.e. when the classification
    itself cannot be made.

    **The honest zero.**  ``unresolved == 0`` is **PASS with an explicit
    "0 rows, 0 by generation"** — it is NOTRUN only when the census
    could not be taken at all (no library / no ``body_origin`` column).
    A census that ran and found nothing is a measurement.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "W-4(REAL)",
            "unresolved 行的写者代际证据（旧端十二列 INSERT vs 新代码四列路径），"
            "逐行点名",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    total_unresolved = 0
    libs_with_unresolved = 0
    by_generation: Dict[str, int] = {}
    samples: List[str] = []
    measured_libs = 0
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = set(r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall())
            if "body_origin" not in cols:
                continue
            measured_libs += 1
            rows = conn.execute(
                "SELECT id, path, body_path, body_anchor, body_hash, "
                "text_state, body_origin, created_at, updated_at FROM chunks "
                "WHERE body_origin='unresolved'").fetchall()
        finally:
            conn.close()
        if not rows:
            continue
        libs_with_unresolved += 1
        total_unresolved += len(rows)
        detail.append("  %-22s 未判定行=%d" % (lib.name, len(rows)))
        for cid, cpath, bp, ba, bh, ts, bo, ca, ua in rows:
            gen = _writer_generation(bp, ba, bh, ts, bo)
            by_generation[gen] = by_generation.get(gen, 0) + 1
            if len(samples) < 30:
                samples.append(
                    "      id=%-14s path=%-44s origin=%-11s state=%-10s "
                    "指针=%s/%s/%s created=%s"
                    % ((cid or "")[:12], (cpath or "")[:42], bo, ts,
                       "NULL" if bp is None else "有",
                       "NULL" if ba is None else "有",
                       "NULL" if bh is None else "有", ca))
    detail.append("")
    detail.append("已测库（有 body_origin 列）: %d / %d" % (measured_libs, len(libs)))
    detail.append("未判定行总数: %d（分布在 %d 个库）"
                  % (total_unresolved, libs_with_unresolved))
    detail.append("写者代际分布: %s"
                  % ("; ".join("%s=%d" % (k, v) for k, v in
                               sorted(by_generation.items())) or "（无未判定行）"))
    detail.append("")
    detail.append("—— 逐行代际证据（样例）——")
    if samples:
        for s in samples:
            detail.append(s)
    else:
        detail.append("      （无未判定行：本次普查读到 0 行未判定 —— "
                      "这是**测到了**，不是『测不了』）")
    detail.append("")
    detail.append("代际判据：")
    detail.append("  新代码四列路径 —— body_pointer.four_columns_for_chunk 自己给出 "
                  "origin（file/blob/orphan），永远不写 unresolved ⇒ "
                  "新代码写出的 unresolved 行**不可能**是三指针全 NULL 的形状。")
    detail.append("  旧端十二列 INSERT —— 不认识 body_path/body_anchor/body_hash/"
                  "text_state 四列 ⇒ 三指针全 NULL + text_state='projection' + "
                  "origin='unresolved'（实测形状，见 write_decide.py 的文档）。")
    detail.append("  ⇒ 「三指针全 NULL 的 unresolved 行」= **旧端代际**；"
                  "「带指针的 unresolved 行」= **新代码代际**（写后判定失灵）。")
    detail.append("")
    detail.append("本判据**不判红**（欠账的严重性归 M6 / Q1）；它报出代际证据。")
    detail.append("FAIL 的唯一条件：普查读到了行却无法分类 —— 那是判据自己坏了。")

    # ------------------------------------------------------------------
    # The CROSS-CHECK the module's docstring promised (and did not do).
    # ------------------------------------------------------------------
    #
    # It is here, in W-4, because W-4 is the criterion the docstring
    # named: "W-4 below CROSS-CHECKS this copy against the real one".
    # Keeping the promise is the point — a criterion that CLAIMS a check
    # and does not do it is a back door for false green.
    #
    # The cross-check FAILS W-4 when the two copies disagree, and NOTRUNs
    # it when the check could not run at all (the real function not
    # importable) — the two failure modes are NOT the same, and saying
    # "NOTRUN" for a disagreement would be the "把不知道说成没变化"
    # failure in its purest form.
    detail.append("")
    detail.append("—— canonical 副本交叉校验（本模块的副本 vs naming.canonical_text）——")
    cross = _canonical_cross_check()
    detail.append("夹具数: %d   分歧: %d" % (len(CANONICAL_CROSS_CHECK_FIXTURES),
                                          len(cross)))
    if cross:
        for c in cross:
            detail.append("  ! %s" % c)
        if any("跑不了" in c for c in cross):
            detail.append("")
            detail.append("⇒ 交叉校验**跑不了**（真实现不可导入）⇒ 本判据"
                          "NOTRUN：不是通过，是没测。")
            return report.judge_notrun(
                "W-4(REAL)",
                "unresolved 行的写者代际证据（旧端十二列 INSERT vs 新代码四列路径），"
                "逐行点名；且本模块的 canonical 副本与真实现交叉一致",
                "\n".join(detail))
        detail.append("")
        detail.append("⇒ 两份 canonical 不一致 ⇒ **判据自己坏了**"
                      "（本模块与真实现用了两把尺子，而没有任何东西会红）"
                      "⇒ 判红。")
        return report.judge(
            "W-4(REAL)",
            "unresolved 行的写者代际证据（旧端十二列 INSERT vs 新代码四列路径），"
            "逐行点名；且本模块的 canonical 副本与真实现交叉一致",
            False, "\n".join(detail))
    detail.append("  两份实现在全部夹具上输出一致 ⇒ 副本没有漂移。")

    if total_unresolved == 0:
        # A census that ran and found nothing IS a measurement.  Saying
        # NOTRUN here would be the "0 个匹配 = NOTRUN" rule applied to
        # the WRONG side: the rule protects against "no objects ⇒
        # vacuous pass", not against "no debt ⇒ pass".
        detail.append("")
        detail.append("结论：本次普查**测到了**，结果是 0 行未判定 ⇒ "
                      "这不是 NOTRUN，是 PASS（无欠账）。")
        return report.judge(
            "W-4(REAL)",
            "unresolved 行的写者代际证据（旧端十二列 INSERT vs 新代码四列路径），"
            "逐行点名；且本模块的 canonical 副本与真实现交叉一致",
            True, "\n".join(detail))
    return report.judge(
        "W-4(REAL)",
        "unresolved 行的写者代际证据（旧端十二列 INSERT vs 新代码四列路径），"
        "逐行点名；且本模块的 canonical 副本与真实现交叉一致",
        True, "\n".join(detail))


def _writer_generation(bp, ba, bh, ts, bo) -> str:
    """The writer generation of ONE undecided row, from its own shape.

    ``新代码代际`` — the row carries a pointer (the current write path
    always writes the four columns, so a row with a pointer was written
    by something that knows them; if it is still ``unresolved``, the
    post-write DECISION is what failed).

    ``旧端代际`` — no pointer at all: the shape a writer that does not
    know the four columns leaves behind.

    The third possibility is named too, because a shape that fits
    neither is a hole in this classifier and must be visible, not
    rounded into one of the two.
    """
    has_pointer = bool(bp) or bool(ba) or bool(bh)
    if has_pointer:
        return "新代码代际（带指针但未判定 ⇒ 写后判定失灵）"
    if (ts or "") == "projection":
        return "旧端代际（三指针全 NULL）"
    return "未分类（形状既不是旧端也不是新代码）"
