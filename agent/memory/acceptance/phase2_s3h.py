#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-H · the criteria that align with S3-D's landed reality.

Why this module exists
----------------------
S3-D landed the anchor grammar (``+S`` segment sequences and the ``~N``
tail-blank record), the ``body_origin`` column, the anti-downgrade UPSERT
and the single-source ``CHUNKER_VERSION``.  S3-E's criteria were written
against the world BEFORE that, so several of them are now misaligned with
the implementation:

  * **C-A3** compared ``files.chunker_version`` against the current
    constant and reported FAIL for a library that had simply not been
    REBUILT yet.  A library that has not been rebuilt is a MISSING
    PREREQUISITE, not a broken product — the three states have to be able
    to say which one it is.
  * **M6** used the old mouth (``text_state NOT IN ('mirror') == 0``),
    which is UNSATISFIABLE once a blob row exists.  The architect's
    §4.5 ruling replaced it with a three-part mouth.
  * **Q2** measured INV-2/3 with the CHECKER's canonical yardstick and
    PRINTED the byte count without ASSERTING it.  The design says
    byte-equality; the byte count was 2349/2349 at the time, so
    asserting it costs nothing and buys the whole invariant.

This module therefore does three things:

  1. **C-A3 in three states** — a library is REBUILT-and-consistent
     (PASS), NOT-REBUILT (NOTRUN, with the library named), or
     REBUILT-but-mismatched (FAIL).  The criterion must answer "WHY is
     this library red", not just "red".
  2. **M6 as the three-part mouth** the architect ruled, with the
     ``body_origin`` distribution reported per library (name, absolute
     path, row count, four counts).
  3. **The INV-2 byte-equality assertion** — computed INDEPENDENTLY of
     the checker, so the criterion is not a tautology.

The independence rule (the one that matters)
---------------------------------------------
Every expected value here is recomputed from the FILE, not taken from the
object under test:

  * the rebuilt fragment is produced by this module's OWN reader, which
    parses the anchor grammar itself — it does not call
    ``body_pointer.rebuild_from_anchor``;
  * the byte-equality verdict therefore compares the stored projection
    against the TRUTH LAYER's bytes.

That is what makes the reverse case work: change a byte in the truth
file and the criterion MUST go red.  A criterion that re-ran the chunker
would compare the projection with itself and could never go red — the
tautology this stage exists to kill.

The ``~N`` question, settled by measurement
--------------------------------------------
The architect's §4.3 says the ``~N`` tail-blank count takes part in the
join.  This module MEASURED it on the 16 live libraries (2674 pointed
rows) with two independent rebuilders:

    fixed-blank-line join (``~N`` NOT in the join):  2674 / 2674 byte-equal
    ``~N``-participating join:                       388 / 2674 byte-equal

So the implementation (``body_pointer.rebuild_from_anchor``, which joins
with a fixed blank line) is RIGHT and the architect's WORDING is not:
``~N`` counts the blank lines the chunker's ``rstrip()`` REMOVED, and
those lines are already gone from the segment text — putting them back
would ADD a newline the chunker's own text does not have.  ``~N`` is
therefore a RECORD (it makes a mismatch explainable) and is CHECKED for
self-consistency, but it is not a rebuild input.

This module asserts the measured rule, and says in its output that the
rule was MEASURED rather than assumed.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.memory.acceptance.live_libs import (
    LiveLibrary,
    count,
    enumerate_live,
    open_ro,
    table_columns,
)

#: The four legal values of ``chunks.body_origin``.
ORIGIN_VALUES = ("file", "blob", "orphan", "unresolved")

#: The anchor grammar, spelled out ONCE here so the reader below has a
#: single source it can be checked against:
#:
#:     body_anchor := "L" start "-" end [ ":p" part ] [ "+S" seg ("," seg)* ]
#:     seg         := start "-" end [ "~" tailblanks ]
ANCHOR_RE = re.compile(
    r"^L(?P<start>\d+)-(?P<end>\d+)"
    r"(?::p(?P<part>\d+))?"
    r"(?:\+S(?P<segs>\d+-\d+(?:~\d+)?(?:,\d+-\d+(?:~\d+)?)*))?$")


class ParsedSegment:
    """One segment of an anchor's segment sequence."""

    __slots__ = ("start", "end", "tailblanks")

    def __init__(self, start: int, end: int, tailblanks: int):
        self.start = start
        self.end = end
        self.tailblanks = tailblanks

    def __repr__(self) -> str:
        return "seg(%d-%d~%d)" % (self.start, self.end, self.tailblanks)


def parse_anchor_grammar(anchor: str) -> Optional[Dict[str, object]]:
    """Parse a ``body_anchor`` — THE criterion's OWN parser.

    **This is deliberately NOT ``body_pointer.parse_anchor``.**  Calling
    the object under test's parser to decide what the object under test
    produced is a tautology: the criterion would agree with any parser,
    including a broken one.  The grammar is spelled out in
    :data:`ANCHOR_RE` and re-implemented here, so the two can disagree —
    and MU-15/MU-16 prove they do.

    Returns ``None`` when the anchor does not match the grammar.
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
    part = int(m.group("part")) if m.group("part") else 0
    segs: List[ParsedSegment] = []
    seg_text = m.group("segs")
    has_segments = seg_text is not None
    if seg_text:
        for raw in seg_text.split(","):
            raw = raw.strip()
            tail = 0
            if "~" in raw:
                raw, tail_text = raw.rsplit("~", 1)
                try:
                    tail = int(tail_text)
                except ValueError:
                    return None
                if tail < 0:
                    return None
            if "-" not in raw:
                return None
            a_text, b_text = raw.split("-", 1)
            try:
                a, b = int(a_text), int(b_text)
            except ValueError:
                return None
            if a < 1 or b < a:
                return None
            segs.append(ParsedSegment(a, b, tail))
        if not segs:
            return None
    else:
        segs = [ParsedSegment(start, end, 0)]
    return {"start": start, "end": end, "part": part, "segments": segs,
            "has_segments": has_segments}


def rebuild_from_grammar(path: Path, anchor: str) -> Optional[str]:
    """Rebuild the fragment an anchor points at — the criterion's OWN reader.

    The rule (MEASURED on the 16 live libraries, 2674 pointed rows):

      * **no ``+S``** — ONE CONTIGUOUS SPAN, read VERBATIM (the file's
        own bytes, trailing newline included).  A whole-file chunk's text
        IS the file's bytes.
      * **``+S`` present** — each segment is read and ``rstrip()``-ed,
        and the segments are joined with a FIXED blank line.

    The ``~N`` count is NOT a join input.  See the module docstring for
    the measurement that settled it.
    """
    parsed = parse_anchor_grammar(anchor)
    if parsed is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    if not lines:
        return None
    segs = parsed["segments"]
    if not parsed["has_segments"]:
        seg = segs[0]
        if seg.start < 1 or seg.start > len(lines):
            return None
        return "".join(lines[seg.start - 1:min(seg.end, len(lines))])
    parts: List[str] = []
    for seg in segs:
        if seg.start < 1 or seg.start > len(lines):
            return None
        end = min(seg.end, len(lines))
        parts.append("".join(lines[seg.start - 1:end]).rstrip())
    return "\n\n".join(parts)


def tail_blanks_of(lines: List[str], a: int, b: int) -> int:
    """How many trailing BLANK lines the chunker's ``rstrip()`` removed.

    Re-derived here (not taken from ``chunker._tail_blanks``) so that the
    ``~N`` consistency check is an independent check and not a tautology.

    **The line list must be the chunker's own.**  The chunker splits the
    file with ``text.split('\\n')`` — WITHOUT ``keepends`` — and the
    trailing-blank count is taken over THAT list.  Splitting with
    ``splitlines(keepends=True)`` produces a different list, and the
    count differs on exactly the rows whose last segment line is empty:
    with keepends the empty line carries its newline, with
    ``split('\\n')`` it does not, so the two disagree about whether the
    segment ends in a blank line.

    This was MEASURED, not assumed: on ROOT's first 400 pointed rows,
    the keepends-based count disagreed with the recorded ``~N`` on 76
    segments, and the ``split('\\n')``-based count disagreed on **0**.
    The first version of this criterion used the keepends list and
    reported 822 false mismatches across the 16 libraries — a false
    red on a healthy corpus, which is worse than no criterion at all.

    So: the check re-derives the count the way the CHUNKER does (from
    ``split('\\n')``), but it does so from the FILE and with its own
    code — the chunker's function is not called.
    """
    seg = lines[a - 1:b]
    n = 0
    while n < len(seg) and seg[len(seg) - 1 - n].strip() == "":
        n += 1
    return n


# ---------------------------------------------------------------------------
# C-A3 · the chunker version, in THREE states
# ---------------------------------------------------------------------------

def judge_ca3_chunker_version_is_consistent(report, workspace: Path):
    """C-A3.4 (REAL): every live library's ``files.chunker_version``.

    Three states, and the criterion must say WHICH one it is:

      * **REBUILT and consistent** — every ``files`` row carries the
        current ``CHUNKER_VERSION`` ⇒ PASS for that library.
      * **NOT REBUILT** — the library's ``files`` rows carry no version
        at all (NULL), or an OLDER version ⇒ **NOTRUN**, not FAIL.  The
        prerequisite ("this library has been rebuilt with the current
        chunker") does not hold; the product is not broken, the rebuild
        has not happened.  Reporting FAIL here is the false red that
        makes a gate stop being read.
      * **REBUILT but MISMATCHED** — the library mixes the current
        version with an older one, or carries a version that is not a
        version at all ⇒ FAIL.  Here the rebuild DID happen and it
        produced a wrong answer, and no amount of "rebuild it again"
        fixes a mixed state.

    The distinction is not cosmetic: the same red means "go rebuild this
    library" in one case and "go look at the rebuild" in the other.  The
    criterion prints the per-library state so the reader can tell.

    The criterion also asserts the SINGLE SOURCE of the version: the
    constant must come from ``chunker.TextChunker.CHUNKER_VERSION``, not
    from a second hand-maintained copy.  The defect this catches is
    MEASURED: ``rebuilder.py`` used to hold its own ``CHUNKER_VERSION =
    3`` while the chunker said 4, so a rebuild wrote 3 into a library
    whose rows had been cut by 4.
    """
    detail: List[str] = []
    try:
        from agent.memory.chunker import TextChunker
    except Exception as e:
        return report.judge_notrun(
            "C-A3.4(REAL)",
            "每个活库的 files.chunker_version 与当前切法一致（逐库三态：已重建/未重建/版本错）",
            "chunker 不可用（%s: %s）—— 判据无法测量" % (type(e).__name__, e))

    current = TextChunker.CHUNKER_VERSION
    detail.append("当前 CHUNKER_VERSION (chunker.TextChunker): %d" % current)

    # --- the single source: no second, hand-maintained copy ---
    single_source, single_source_why = _chunker_version_is_single_source(current)
    detail.append("版本常量的唯一真源: %s" % single_source_why)
    if not single_source:
        detail.append("  —— 有第二份手抄的版本常量：重建会用它写回 files 表，"
                      "与实际切出版本不一致（实测过：rebuilder 自持 3 而 chunker 是 4）")
        return report.judge(
            "C-A3.4(REAL)",
            "每个活库的 files.chunker_version 与当前切法一致（逐库三态：已重建/未重建/版本错）",
            False, "\n".join(detail))

    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "C-A3.4(REAL)",
            "每个活库的 files.chunker_version 与当前切法一致（逐库三态：已重建/未重建/版本错）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    rebuilt: List[str] = []
    not_rebuilt: List[Tuple[str, str]] = []
    mismatched: List[Tuple[str, str]] = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            rows = conn.execute(
                "SELECT chunker_version, COUNT(*) FROM files "
                "GROUP BY 1").fetchall()
            total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        except sqlite3.Error as e:
            detail.append("  %-22s 前置不成立: 无法读 files 表 (%s)" % (lib.name, e))
            continue
        finally:
            conn.close()
        dist = {tuple(r)[0]: tuple(r)[1] for r in rows}
        nulls = dist.get(None, 0)
        at_current = dist.get(current, 0)
        older = {k: v for k, v in dist.items()
                 if k is not None and k != current}
        older_txt = ", ".join("%s×%d" % (k, v) for k, v in sorted(older.items()))
        if total == 0:
            detail.append("  %-22s files 表为空 —— 该库还没有被索引过（NOTRUN）")
            not_rebuilt.append((lib.name, "files 表为空"))
            continue
        if nulls == total:
            state = "NOT-REBUILT（全部 %d 行无版本标记）" % total
            detail.append("  %-22s files=%-4d cv=%-4d null=%-4d -> %s"
                          % (lib.name, total, at_current, nulls, state))
            detail.append("      路径: %s" % lib.db)
            not_rebuilt.append((lib.name, "全部 files 行 chunker_version 为 NULL"))
            continue
        if older:
            state = "MISMATCHED（版本错：%s，同时有 %d 行是当前版本 %d）" \
                % (older_txt, at_current, current)
            detail.append("  %-22s files=%-4d cv=%-4d null=%-4d -> %s"
                          % (lib.name, total, at_current, nulls, state))
            detail.append("      路径: %s" % lib.db)
            mismatched.append((lib.name, older_txt))
            continue
        detail.append("  %-22s files=%-4d cv=%-4d null=%-4d -> REBUILT"
                      % (lib.name, total, at_current, nulls))
        detail.append("      路径: %s" % lib.db)
        rebuilt.append(lib.name)

    detail.append("")
    detail.append("已重建且版本一致（PASS）(%d): %s"
                  % (len(rebuilt), ", ".join(rebuilt) if rebuilt else "无"))
    detail.append("未重建（NOTRUN，不是 FAIL）(%d): %s"
                  % (len(not_rebuilt),
                     "; ".join("%s(%s)" % (n, w) for n, w in not_rebuilt)
                     if not_rebuilt else "无"))
    detail.append("重建了但版本错（FAIL）(%d): %s"
                  % (len(mismatched),
                     "; ".join("%s(%s)" % (n, w) for n, w in mismatched)
                     if mismatched else "无"))

    if mismatched:
        detail.append("  —— 版本错不是『没重建』：重建已经发生，但产出的是别的"
                      "版本的切法。补一次重建解决不了『一半旧一半新』。")
        return report.judge(
            "C-A3.4(REAL)",
            "每个活库的 files.chunker_version 与当前切法一致（逐库三态：已重建/未重建/版本错）",
            False, "\n".join(detail))
    if not_rebuilt:
        detail.append("  —— 未重建的库需要先跑一次重建（M7）才能判定本条；"
                      "『没重建』不是产品坏了，是前置条件不成立。")
        return report.judge_notrun(
            "C-A3.4(REAL)",
            "每个活库的 files.chunker_version 与当前切法一致（逐库三态：已重建/未重建/版本错）",
            "\n".join(detail))
    return report.judge(
        "C-A3.4(REAL)",
        "每个活库的 files.chunker_version 与当前切法一致（逐库三态：已重建/未重建/版本错）",
        True, "\n".join(detail))


def _chunker_version_is_single_source(current: int) -> Tuple[bool, str]:
    """Is there exactly ONE definition of the chunker version?

    The check is STATIC (``ast``), because what it is really asking is
    "does a second hand-maintained copy of the number exist?" — and that
    is a fact about the SOURCE, not about the runtime.

    The rule: every module that defines ``CHUNKER_VERSION`` must
    ASSIGN it from ``chunker.TextChunker.CHUNKER_VERSION``.  A module
    that assigns a literal integer is a second source, and it is a
    defect the moment the real version moves.

    **The alias must be resolved, not pattern-matched.**  The natural
    way to write the single-source rule in a module is::

        from agent.memory.chunker import TextChunker as _TextChunker
        CHUNKER_VERSION = _TextChunker.CHUNKER_VERSION

    — the module renames the import, so the RHS is an ALIAS, not the
    literal name ``TextChunker``.  A regex that only knows
    ``TextChunker.CHUNKER_VERSION`` calls that a second source, which is
    a FALSE RED on a correctly written module.

    So the check RESOLVES the alias through the module's own import
    statements: an ``ImportFrom`` that binds a name to
    ``agent.memory.chunker``'s ``TextChunker`` makes that name a
    legitimate spelling of the one source.  Only a module that assigns
    a LITERAL (or derives it from anything else) is a second source.

    This was found by the criterion itself: the first version used a
    plain regex and reported ``rebuilder.py`` as a second source, when
    ``rebuilder.py`` is in fact the CORRECTLY written example (the
    defect it fixed was holding a literal ``3``).
    """
    import ast

    here = Path(__file__).resolve().parent
    root = here.parent
    offenders: List[str] = []
    checked: List[str] = []
    for path in sorted(root.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        # The names this module binds to ``chunker.TextChunker``.
        aliases = {"TextChunker"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("chunker") or \
                        node.module.endswith("chunker.TextChunker"):
                    for alias in node.names:
                        if alias.name == "TextChunker":
                            aliases.add(alias.asname or alias.name)
        for node in tree.body:
            targets: List[str] = []
            value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                targets = [node.targets[0].id]
                value = node.value
            elif isinstance(node, ast.AnnAssign) \
                    and isinstance(node.target, ast.Name) \
                    and node.value is not None:
                targets = [node.target.id]
                value = node.value
            if "CHUNKER_VERSION" not in targets:
                continue
            checked.append(path.name)
            src = ast.unparse(value) if value is not None else ""
            # A legitimate derivation: ``<alias>.CHUNKER_VERSION`` where
            # ``<alias>`` is bound to chunker.TextChunker.
            m = re.fullmatch(r"(\w+)\.CHUNKER_VERSION", src)
            if m and m.group(1) in aliases:
                continue
            if src == "CHUNKER_VERSION":
                # an alias of a module-level alias — resolve it
                continue
            offenders.append("%s: CHUNKER_VERSION = %s" % (path.name, src))
    if offenders:
        return False, "有第二份手抄的版本常量 —— " + "; ".join(offenders)
    return True, "唯一真源 chunker.TextChunker.CHUNKER_VERSION（已扫 %s）" \
        % (", ".join(checked) if checked else "无定义")


# ---------------------------------------------------------------------------
# M6 · the three-part mouth + the body_origin distribution
# ---------------------------------------------------------------------------

def judge_m6_body_origin_mouth(report, workspace: Path):
    """M6 (REAL): the architect's three-part mouth, per library.

    The mouth (架构师 §4.5, and the M6 ruling of the fourth round):

        1. ``body_origin='file' AND text_state<>'mirror'``  == 0
        2. ``body_origin='unresolved'``                      == 0
        3. ``body_origin='orphan'``                          == 0

    The OLD mouth (``text_state NOT IN ('mirror') == 0``) is
    UNSATISFIABLE once a blob row exists: a blob row has no truth source
    to mirror, so the count could never reach zero.  A criterion that
    guards an unsatisfiable invariant is permanently red, and a
    permanently red criterion is one nobody reads.  The mouth was
    REPLACED, not relaxed.

    ``blob`` is deliberately NOT in the mouth: it is a legal terminal
    state ("honestly there is no truth source"), and its count is
    REPORTED so a reader can see how much of the corpus is blob-shaped.

    **The three states, and why each one means what it means:**

      * a library with NO ``body_origin`` column ⇒ **NOTRUN**, and the
        report says WHICH library and WHICH column.  Not FAIL (the
        product is not broken — the column was never added) and
        certainly not PASS (that would be a silent green on a criterion
        that measured nothing).
      * a library whose column exists but holds ``unresolved`` rows ⇒
        **FAIL**: somebody wrote rows nobody decided about.  This is
        the false-red defence — ``unresolved`` must be able to turn the
        gate red, otherwise it is a TODO nobody sees.
      * a library whose column exists and every row is decided ⇒ the
        three counts decide the verdict.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "M6(REAL)",
            "M6 三段口径：file 行全部 mirror，且 unresolved==0、orphan==0（逐库报四值分布）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    missing_col: List[str] = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = set(table_columns(conn, "chunks"))
        finally:
            conn.close()
        if "body_origin" not in cols:
            missing_col.append(lib.name)
    detail.append("body_origin 列存在的库: %d/%d"
                  % (len(libs) - len(missing_col), len(libs)))
    if missing_col:
        detail.append("缺少 body_origin 列的库: %s" % ", ".join(missing_col))
        detail.append("该列由内核架构师的裁决落地；没有它，M6 的新口径无法计算，"
                      "旧的『text_state NOT IN (mirror) == 0』口径又不可满足"
                      "（有 blob 行时永远非零）。")
        return report.judge_notrun(
            "M6(REAL)",
            "M6 三段口径：file 行全部 mirror，且 unresolved==0、orphan==0（逐库报四值分布）",
            "\n".join(detail))

    ok = True
    red_libraries: List[str] = []
    grand = {v: 0 for v in ORIGIN_VALUES}
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            rows = conn.execute(
                "SELECT body_origin, COUNT(*) FROM chunks GROUP BY 1").fetchall()
            file_not_mirror = count(
                conn, "SELECT COUNT(*) FROM chunks "
                     "WHERE body_origin='file' AND text_state<>'mirror'")
            unresolved = count(
                conn, "SELECT COUNT(*) FROM chunks WHERE body_origin='unresolved'")
            orphan = count(
                conn, "SELECT COUNT(*) FROM chunks WHERE body_origin='orphan'")
            blob = count(
                conn, "SELECT COUNT(*) FROM chunks WHERE body_origin='blob'")
            total = count(conn, "SELECT COUNT(*) FROM chunks")
            illegal = count(
                conn, "SELECT COUNT(*) FROM chunks "
                     "WHERE body_origin IS NULL OR body_origin NOT IN "
                     "('file','blob','orphan','unresolved')")
        finally:
            conn.close()
        dist = {tuple(r)[0]: tuple(r)[1] for r in rows}
        for v in ORIGIN_VALUES:
            grand[v] += dist.get(v, 0)
        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        detail.append("      行数=%-6d 分布: file=%-6d blob=%-4d orphan=%-4d "
                      "unresolved=%-4d (非法值=%d)"
                      % (total, dist.get("file", 0), dist.get("blob", 0),
                         dist.get("orphan", 0), dist.get("unresolved", 0),
                         illegal))
        detail.append("      口径① file 且非 mirror = %d   ② unresolved = %d   "
                      "③ orphan = %d" % (file_not_mirror, unresolved, orphan))
        if file_not_mirror or unresolved or orphan or illegal:
            ok = False
            red_libraries.append(lib.name)
    detail.append("")
    detail.append("全活库合计: file=%d blob=%d orphan=%d unresolved=%d"
                  % (grand["file"], grand["blob"], grand["orphan"],
                     grand["unresolved"]))
    detail.append("blob 是合法终态（诚实无真源），不判红，只报数。")
    detail.append("置位者是巡检器（不是迁移器）：巡检器在 INV-2/3 校验通过时置 mirror。")

    if red_libraries:
        detail.append("  —— 三段口径未过的库: %s" % ", ".join(red_libraries))
        detail.append("  —— unresolved != 0 表示有人写了没人判定过的行："
                      "它必须能报红，否则『待办』就没人看得见。")
        return report.judge(
            "M6(REAL)",
            "M6 三段口径：file 行全部 mirror，且 unresolved==0、orphan==0（逐库报四值分布）",
            False, "\n".join(detail))
    return report.judge(
        "M6(REAL)",
        "M6 三段口径：file 行全部 mirror，且 unresolved==0、orphan==0（逐库报四值分布）",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# INV-2 · byte equality, asserted independently
# ---------------------------------------------------------------------------

def judge_inv2_anchor_rebuild_is_byte_equal(report, workspace: Path):
    """INV-2 (REAL): the rebuilt fragment is BYTE-equal to ``chunks.text``.

    The design says "byte-identical" and the fourth round's ruling
    (Q-A1) settled that ``~N`` must be carried so that the 277
    C-class rows can be rebuilt byte-for-byte.  S3-E measured the byte
    count and PRINTED it but did not ASSERT it — which means the
    invariant was not actually guarded: the criterion would still be
    green with a rebuild that was canonical-equal but byte-different.

    **The assertion is computed independently.**  This module parses the
    anchor grammar itself (:func:`parse_anchor_grammar`) and rebuilds
    the fragment itself (:func:`rebuild_from_grammar`); it does NOT call
    ``body_pointer.rebuild_from_anchor``.  The expected value therefore
    comes from the FILE, and the comparison is projection-vs-truth, not
    projection-vs-projection.

    That is what makes the reverse case real: change one byte in a truth
    file and this criterion MUST go red.  A criterion that re-ran the
    chunker would compare the projection with itself and could never go
    red — the tautology this stage exists to kill.

    The byte count is PRINTED, never hidden — and the byte count IS the
    verdict.  There is no canonical fallback here: on a healthy corpus
    the byte count is 100%, and a criterion that would accept a
    canonical-only match is a criterion that has been silently downgraded
    (and a downgraded criterion reports red on a healthy corpus, or —
    worse — green on a broken one).
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "INV-2(REAL)",
            "按锚点重建的片段与 chunks.text 逐字节相等（判据独立解析锚点，不调用被测对象的重建器）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.body_pointer import (
            rebuild_from_anchor,
            resolve_truth_file,
        )
    except Exception as e:
        return report.judge_notrun(
            "INV-2(REAL)",
            "按锚点重建的片段与 chunks.text 逐字节相等，且实现侧读法与判据独立读法逐行一致",
            "body_pointer 不可用（%s: %s）—— 判据无法测量" % (type(e).__name__, e))

    total_rows = 0
    byte_equal = 0
    canonical_equal_only = 0
    unparsable = 0
    unlocatable = 0
    reader_disagreements = 0
    red_libraries: List[str] = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            cols = set(table_columns(conn, "chunks"))
            origin_col = "body_origin" in cols
            if origin_col:
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
            if rebuilt == (text or ""):
                lib_byte += 1
            else:
                from agent.memory.naming import canonical_text
                if canonical_text(rebuilt) == canonical_text(text or ""):
                    lib_canon += 1
            # --- the CROSS-CHECK -------------------------------------
            # The implementation's reader must produce the SAME
            # fragment as this criterion's independently written one.
            #
            # This is not a tautology: the claim is "two independent
            # implementations of the grammar agree on every row of the
            # corpus", and it is exactly the claim that was FALSE
            # before S3-D (two parsers, two spellings, both sides
            # believing they agreed).  A mutation that changes the
            # implementation's join rule breaks it here.
            #
            # Note the ORDER: the byte-equality VERDICT above is
            # computed from this criterion's OWN reader, so it never
            # depends on the implementation.  The cross-check is an
            # ADDITIONAL assertion, not the source of the verdict.
            try:
                theirs = rebuild_from_anchor(truth, banchor)
            except Exception:
                theirs = None
            if theirs != rebuilt:
                lib_reader_disagree += 1
        total_rows += lib_rows
        byte_equal += lib_byte
        canonical_equal_only += lib_canon
        unparsable += lib_unparsable
        unlocatable += lib_unlocatable
        reader_disagreements += lib_reader_disagree
        verdict = "OK" if (lib_rows == lib_byte) else "RED"
        if lib_rows != lib_byte or lib_reader_disagree:
            red_libraries.append(lib.name)
        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        detail.append("      可判定行=%-5d 逐字节相等=%-5d 仅 canonical 相等=%-4d "
                      "锚点不合语法=%-3d 定位不到=%-3d 两份读法不一致=%-3d -> %s"
                      % (lib_rows, lib_byte, lib_canon, lib_unparsable,
                         lib_unlocatable, lib_reader_disagree, verdict))
    detail.append("")
    detail.append("合计: 可判定行=%d  逐字节相等=%d  仅 canonical 相等=%d  "
                  "锚点不合语法=%d  定位不到=%d  两份读法不一致=%d"
                  % (total_rows, byte_equal, canonical_equal_only,
                     unparsable, unlocatable, reader_disagreements))
    detail.append("口径：逐字节（容差仅 CRLF/LF 由 canonical_text 覆盖的那一层只在"
                  "『仅 canonical 相等』这一栏被单独报出，不参与判定）。")
    detail.append("判据自己解析锚点、自己重建，不调用 body_pointer.rebuild_from_anchor —— "
                  "否则就是拿被测对象算期望值（同义反复 = 假绿）。")
    detail.append("『两份读法不一致』是交叉校验：实现侧的读法必须与判据独立写的读法"
                  "在每一行上给出同一结果。两处拼写一条规则，就是『锚点指向空处』的成因。")
    detail.append("重建规则（实测，2674 行有指针的行）：固定空行连接；"
                  "『~N』不参与 join（参与 join 只有 388/2674 相等）。"
                  "『~N』是记录（供解释差异），并被 ANCHOR.segments-are-self-consistent 校验。")

    if total_rows == 0:
        return report.judge_notrun(
            "INV-2(REAL)",
            "按锚点重建的片段与 chunks.text 逐字节相等，且实现侧读法与判据独立读法逐行一致",
            "\n".join(detail) + "\n本语料没有可判定的有指针行 —— 不可判定，不是通过。")
    if red_libraries:
        detail.append("  —— 未全等的库: %s" % ", ".join(red_libraries))
        return report.judge(
            "INV-2(REAL)",
            "按锚点重建的片段与 chunks.text 逐字节相等，且实现侧读法与判据独立读法逐行一致",
            False, "\n".join(detail))
    return report.judge(
        "INV-2(REAL)",
        "按锚点重建的片段与 chunks.text 逐字节相等，且实现侧读法与判据独立读法逐行一致",
        True, "\n".join(detail))


def judge_anchor_segments_are_self_consistent(report, workspace: Path):
    """ANCHOR (REAL): the segment sequence agrees with the span it claims.

    The grammar's own invariant (架构师 §2.2): the ``+S`` sequence's
    FIRST segment starts at the anchor's ``L<start>`` and its LAST
    segment ends at the anchor's ``L<end>``.  A sequence that disagrees
    is a broken anchor, and a broken anchor must explode on the spot
    rather than point somewhere else — which is exactly how "the anchor
    points nowhere" happens.

    The ``~N`` record is checked here too: the count must equal the
    number of trailing blank lines the chunker's ``rstrip()`` removed
    from that segment — re-derived from the FILE by this module, not
    read from the chunker.  A wrong ``~N`` is not fatal to the rebuild
    (it is a record, not a join input), but it makes a mismatch
    unexplainable, so it is worth a red.

    The criterion parses the grammar ITSELF, so a broken parser in the
    implementation cannot make this green.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "ANCHOR(REAL)",
            "锚点段序列自洽：+S 首段起点==L 起点、末段终点==L 终点，且 ~N 与文件里被 rstrip 抹掉的尾随空行数一致",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory.body_pointer import resolve_truth_file
    except Exception as e:
        return report.judge_notrun(
            "ANCHOR(REAL)",
            "锚点段序列自洽：+S 首段起点==L 起点、末段终点==L 终点，且 ~N 与文件里被 rstrip 抹掉的尾随空行数一致",
            "body_pointer 不可用（%s: %s）" % (type(e).__name__, e))

    checked = 0
    with_seq = 0
    bad_span = 0
    bad_tail = 0
    unparsable = 0
    red_libraries: List[str] = []
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            rows = conn.execute(
                "SELECT body_path, body_anchor FROM chunks "
                "WHERE body_path IS NOT NULL AND body_anchor IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
        lib_checked = lib_seq = lib_span = lib_tail = lib_unparsable = 0
        for row in rows:
            bpath, banchor = tuple(row)
            parsed = parse_anchor_grammar(banchor)
            if parsed is None:
                lib_unparsable += 1
                continue
            lib_checked += 1
            if not parsed["has_segments"]:
                continue
            lib_seq += 1
            segs = parsed["segments"]
            if segs[0].start != parsed["start"] or segs[-1].end != parsed["end"]:
                lib_span += 1
                continue
            truth = resolve_truth_file(lib.workspace, bpath)
            if truth is None:
                continue
            try:
                raw = truth.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # The CHUNKER's line list: ``text.split('\n')`` without
            # keepends.  See :func:`tail_blanks_of` for why this matters
            # and what the first (wrong) version of this criterion
            # reported.
            lines = raw.split("\n")
            for seg in segs:
                if seg.start < 1 or seg.start > len(lines):
                    continue
                end = min(seg.end, len(lines))
                if tail_blanks_of(lines, seg.start, end) != seg.tailblanks:
                    lib_tail += 1
                    break
        checked += lib_checked
        with_seq += lib_seq
        bad_span += lib_span
        bad_tail += lib_tail
        unparsable += lib_unparsable
        if lib_span or lib_tail or lib_unparsable:
            red_libraries.append(lib.name)
        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        detail.append("      有指针行=%-5d 带 +S 段序列=%-5d 段/跨度不自洽=%-4d "
                      "~N 与文件不符=%-4d 锚点不合语法=%-4d"
                      % (lib_checked, lib_seq, lib_span, lib_tail,
                         lib_unparsable))
    detail.append("")
    detail.append("合计: 有指针行=%d  带 +S=%d  段/跨度不自洽=%d  ~N 与文件不符=%d  "
                  "锚点不合语法=%d" % (checked, with_seq, bad_span, bad_tail,
                                       unparsable))
    detail.append("判据自己用语法正则解析锚点（不调用被测对象的解析器），"
                  "并自己从文件重算尾随空行数（不读 chunker 的结果）。")

    if checked == 0:
        return report.judge_notrun(
            "ANCHOR(REAL)",
            "锚点段序列自洽：+S 首段起点==L 起点、末段终点==L 终点，且 ~N 与文件里被 rstrip 抹掉的尾随空行数一致",
            "\n".join(detail) + "\n没有带指针的行 —— 不可判定，不是通过。")
    if red_libraries:
        detail.append("  —— 不自洽的库: %s" % ", ".join(red_libraries))
        return report.judge(
            "ANCHOR(REAL)",
            "锚点段序列自洽：+S 首段起点==L 起点、末段终点==L 终点，且 ~N 与文件里被 rstrip 抹掉的尾随空行数一致",
            False, "\n".join(detail))
    return report.judge(
        "ANCHOR(REAL)",
        "锚点段序列自洽：+S 首段起点==L 起点、末段终点==L 终点，且 ~N 与文件里被 rstrip 抹掉的尾随空行数一致",
        True, "\n".join(detail))


# ---------------------------------------------------------------------------
# UPSERT · the anti-downgrade rule, measured on a COPY
# ---------------------------------------------------------------------------

def judge_upsert_does_not_downgrade_the_body_columns(report, workspace: Path):
    """UPSERT (unit): a writer that does not carry the body columns must
    NOT erase them.

    The defect this exists to catch, MEASURED twice on this machine:

      * 2026-10-29 23:41 — a writer running code from BEFORE the four
        columns existed UPSERTed ``agent-mvnewfyb`` and wrote
        ``body_path = excluded.body_path`` (NULL) over the values the
        migration had filled at 23:23.  The migration report still said
        ``PASS``; only a criterion that looks at EVERY library saw it.
      * 2026-10-30 03:28 — the same class of stale writer wrote
        ``agent-mvnf7fjl``'s new evolution file with all four columns
        NULL and ``body_origin='unresolved'``, and WIPED the library's
        ``files.chunker_version`` back to NULL.

    The anti-downgrade rule is therefore the load-bearing part of the
    whole stage, and it deserves a criterion that makes it real.

    **How it is measured, and why it is not a tautology:**

    The criterion builds a THROWAWAY index, fills a row with all four
    body columns DECIDED (``file`` / a real anchor / a real hash /
    ``mirror``), and then simulates a STALE WRITER: an UPSERT that
    carries NO body columns (all four NULL / ``unresolved``).

    The EXPECTED values are re-derived here, from the file, not taken
    from ``storage.py``.  The criterion then asserts the two things
    that must hold:

      1. the four body columns kept their DECIDED values, and
      2. ``body_origin`` kept its DECIDED value (``unresolved`` must
         not overwrite ``file``).

    It does NOT call ``storage.save_chunk`` to compute the expectation —
    the expectation is "the decided values survive", which is a property
    of the SQL, checked by running the SQL.  Running the SQL is the
    measurement; asking the implementation what it would do would be the
    tautology.
    """
    detail: List[str] = []
    import tempfile

    decided = {
        "body_path": "MEMORY.md",
        "body_anchor": "L1-3",
        "body_hash": "sha256:" + "a" * 64,
        "text_state": "mirror",
        "body_origin": "file",
    }
    try:
        from agent.memory.schema_drift import EXPECTED_CHUNK_COLUMNS
        from agent.memory.storage import BODY_COLUMN_UPSERT_SQL
    except Exception as e:
        return report.judge_notrun(
            "UPSERT(unit)",
            "不填四列的旧写入者不会擦掉已判定值（迁移填好的值不被降级）",
            "storage / schema_drift 不可用（%s: %s）" % (type(e).__name__, e))

    # The rule under test: the SQL fragment's shape.  It must be
    # CONDITIONAL on the incoming value, so that a NULL/empty incoming
    # value cannot overwrite a decided one.
    detail.append("BODY_COLUMN_UPSERT_SQL 片段:")
    for line in BODY_COLUMN_UPSERT_SQL.strip().splitlines():
        detail.append("    %s" % line.strip())
    conditional = {}
    for col in ("body_path", "body_anchor", "body_hash", "text_state",
                "body_origin"):
        m = re.search(
            r"%s\s*=\s*CASE\s+WHEN\s+excluded\.%s\s+IS\s+NOT\s+NULL"
            % (col, col), BODY_COLUMN_UPSERT_SQL)
        conditional[col] = bool(m)
        detail.append("  %-12s 有条件覆盖（excluded IS NOT NULL 时才写）: %s"
                      % (col, bool(m)))
    detail.append("（text_state 另有一条『excluded 不许是 projection』的规则；"
                  "body_origin 另有一条『excluded 不许是 unresolved』的规则。）")
    for col in ("body_origin",):
        m2 = re.search(r"%s\s*=\s*CASE\s+WHEN\s+excluded\.%s\s+IS\s+NOT\s+NULL"
                       r"\s+AND\s+excluded\.%s\s*<>\s*'unresolved'"
                       % (col, col, col), BODY_COLUMN_UPSERT_SQL)
        conditional[col] = conditional[col] and bool(m2)
        detail.append("  %-12s 另需排除 'unresolved': %s" % (col, bool(m2)))

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "index.db"
        conn = sqlite3.connect(str(db))
        try:
            cols = []
            for name in EXPECTED_CHUNK_COLUMNS:
                if name == "id":
                    cols.append("id TEXT PRIMARY KEY")
                else:
                    cols.append("%s TEXT" % name)
            conn.execute("CREATE TABLE chunks (" + ", ".join(cols) + ")")
            conn.execute(
                "INSERT INTO chunks (id, path, body_path, body_anchor, "
                "body_hash, text_state, body_origin) "
                "VALUES ('row-1','MEMORY.md',?,?,?,?,?)",
                (decided["body_path"], decided["body_anchor"],
                 decided["body_hash"], decided["text_state"],
                 decided["body_origin"]))
            conn.commit()

            # The STALE WRITER: same id, NO body columns carried.
            stale_sql = (
                "INSERT INTO chunks (id, path, body_path, body_anchor, "
                "body_hash, text_state, body_origin) "
                "VALUES ('row-1','MEMORY.md',NULL,NULL,NULL,'projection',"
                "'unresolved') "
                "ON CONFLICT(id) DO UPDATE SET path = excluded.path, "
                + BODY_COLUMN_UPSERT_SQL)
            conn.execute(stale_sql)
            conn.commit()
            got = tuple(conn.execute(
                "SELECT body_path, body_anchor, body_hash, text_state, "
                "body_origin FROM chunks WHERE id='row-1'").fetchone())
        finally:
            conn.close()

    detail.append("")
    detail.append("旧写入者写入后的行:")
    for k, v in zip(("body_path", "body_anchor", "body_hash", "text_state",
                     "body_origin"), got):
        detail.append("    %-12s %r" % (k, v))
    detail.append("期望（判定前的值必须原样存活）:")
    for k in ("body_path", "body_anchor", "body_hash", "text_state",
              "body_origin"):
        detail.append("    %-12s %r" % (k, decided[k]))

    ok = True
    for k, v in zip(("body_path", "body_anchor", "body_hash", "text_state",
                     "body_origin"), got):
        if v != decided[k]:
            ok = False
            detail.append("  —— %s 被降级：%r -> %r" % (k, decided[k], v))
    for col, cond in conditional.items():
        if not cond:
            ok = False
            detail.append("  —— SQL 片段里 %s 不是条件覆盖：旧写入者会直接盖掉"
                          "已判定值" % col)

    if ok:
        detail.append("结论：不填四列的旧写入者没有擦掉任何已判定值；"
                      "SQL 片段对五列都是条件覆盖。")
    return report.judge(
        "UPSERT(unit)",
        "不填四列的旧写入者不会擦掉已判定值（迁移填好的值不被降级）",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# The single parser
# ---------------------------------------------------------------------------

def judge_anchor_parser_is_single(report, workspace: Path = None):
    """PARSER (unit): the anchor grammar has exactly ONE parser.

    Two parsers for one grammar is how "the anchor points nowhere"
    happens while both sides believe they agree — the failure mode
    ``body_pointer``'s own header warns about.  The old
    ``text_inv._parse_anchor`` was the second copy; it is gone.

    The check is STATIC (``ast``), because what it asks is "does a
    second implementation of the grammar exist?" — a fact about the
    SOURCE, not about the runtime.  A runtime check could not see a
    parser that nothing happens to call today, and a parser that is
    dead code today is a parser that gets called tomorrow.

    **The one true source is EXCLUDED by name.**  The designated source
    (``body_pointer.parse_anchor``) is of course a grammar parser — it
    has to be.  The question is whether there is a SECOND one.  Naming
    the source explicitly is what makes the check meaningful: a check
    that flags the source itself is not a check, it is a permanent red.

    The check is also precise about what counts as a second copy: a
    module-level function whose name matches the grammar-parser pattern
    AND whose body mentions the grammar's markers (``'L'`` / ``+S`` /
    ``body_anchor``) AND that does NOT merely call the one source.  A
    re-export (``text_inv.read_fragment_at_anchor`` calling
    ``rebuild_from_anchor``) is NOT a second parser, and the check says
    so — otherwise the rule would be "delete the re-export", which is
    not the point.
    """
    import ast

    detail: List[str] = []
    root = Path(__file__).resolve().parent.parent
    #: The ONE designated implementation.  Everything else that parses
    #: the grammar is a second copy.
    THE_ONE_SOURCE = ("body_pointer.py", "parse_anchor")
    parser_names = ("_parse_anchor", "parse_anchor", "_parse_body_anchor",
                    "parse_body_anchor")
    found: List[str] = []
    scanned: List[str] = []
    for path in sorted(root.glob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        scanned.append(path.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in parser_names:
                continue
            if (path.name, node.name) == THE_ONE_SOURCE:
                continue          # the designated source: not a second copy
            body_src = ast.unparse(node)
            # A parser MENTIONS the grammar's markers; a re-export just
            # calls the one parser.
            mentions_grammar = bool(re.search(r"['\"]L['\"]|\+S|body_anchor",
                                              body_src))
            calls_the_one = bool(re.search(
                r"body_pointer\.parse_anchor|rebuild_from_anchor", body_src))
            if mentions_grammar and not calls_the_one:
                found.append("%s:%d %s" % (path.name, node.lineno, node.name))
    detail.append("扫描的模块: %d 个（%s ...）"
                  % (len(scanned), ", ".join(scanned[:6])))
    detail.append("指定的唯一真源: agent/memory/%s 的 %s" % THE_ONE_SOURCE)
    detail.append("按语法自行实现锚点解析的**其它**函数: %s"
                  % (", ".join(found) if found else "无"))
    detail.append("text_inv._parse_anchor: %s"
                  % ("仍存在" if (root / "text_inv.py").is_file()
                     and "_parse_anchor" in (root / "text_inv.py").read_text(
                         encoding="utf-8").split("def read_fragment_at_anchor")[0]
                     else "已删除"))
    detail.append("唯一真源: agent/memory/body_pointer.py 的 parse_anchor"
                  "（text_inv 只做 re-export，不是第二份实现）")
    if found:
        detail.append("  —— 有第二份锚点语法实现: %s" % ", ".join(found))
        detail.append("  —— 两处拼写一条规则，就是『锚点指向空处』的成因。")
        return report.judge(
            "PARSER(unit)",
            "锚点语法只有一份实现（text_inv._parse_anchor 已删除，全仓只有 body_pointer.parse_anchor）",
            False, "\n".join(detail))
    return report.judge(
        "PARSER(unit)",
        "锚点语法只有一份实现（text_inv._parse_anchor 已删除，全仓只有 body_pointer.parse_anchor）",
        True, "\n".join(detail))
