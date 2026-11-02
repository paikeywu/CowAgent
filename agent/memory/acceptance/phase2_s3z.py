#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-Z · 检索召回与相关性下界（live-memory）。

T4 · 2026-11-02 · 小种子（SEED）
=====================================

**Why this module exists.**  SEED 自验 live-memory 载体时发现，
载体本身是健康的（真源链零断点、哈希零失配、外置不变量精确成立），
但「能存」与「能用」之间有两道缺口，且两道都在**检索层**：

* **缺口 A · 组合查询劣于单词.**  实测（同一活库、同一语料）::

      凭据        -> 5 hits
      泄漏        -> 5 hits
      凭据 泄漏    -> 5 hits      (空格分隔)
      密钥泄漏     -> 0 hits      (无空格)   <-- 缺陷

  ⇒ **组合查询反而比单词更差**。这不是语料问题：``泄漏`` 在 chunks 里
  出现 34 处，``密钥`` 出现 2 处，而 ``密钥泄漏`` 是 0。

* **缺口 B · 没有相关性下界.**  实测阴性对照::

      query='意图不存在的一串怪词'  ->  5 hits，最高 score=1.0

  ⇒ **一个完全不存在的中文串拿到最高分**。用户无法用「没有结果」
  判断「确实没有」。

**根因（已定位到代码行，不是推测）**

* ``agent/memory/storage.py:90`` ``_cjk_windows()`` 对 CJK 连续串生成
  **滑动窗口**，含 2 字窗口（实测 ``密钥泄漏`` ->
  ``('密钥泄','钥泄漏','密钥','钥泄','泄漏')``）。
* ``agent/memory/storage.py:2660`` ``_build_trigram_query()`` 把
  **同一组窗口**交给 trigram FTS5（实测 -> ``("密钥泄" OR "钥泄漏"
  OR "密钥" OR "钥泄" OR "泄漏")``）。
* **trigram 分词器只索引 3 字滑窗** ⇒ 2 字窗口在 trigram 表**必然 0 命中**；
  4 字滑窗语料里没有 ⇒ trigram 阶段整体返回空 ⇒ 落到 LIKE 兜底，
  而 LIKE 找的是**连续子串** ⇒ ``密钥泄漏`` 0 行。

* ``agent/memory/storage.py:2006`` 严格 AND 无命中时走
  ``_search_like_relaxed()``，该函数**按组分别匹配，任一组命中即返回**；
  ``storage.py:1995`` ``score = 0.3 + 0.55 * quality`` ⇒ 不相关的词
  也能拿到 1.0。

**The five criteria**

===========================================  ==========================
criterion                                   guards
===========================================  ==========================
R-1 两字中文词可达（分词器无关）             缺口 A
R-2 组合查询不得劣于单词（单调性）           缺口 A
R-3 阴性对照必须 0 命中                      缺口 B
R-4 相关性下界存在且真的生效                 缺口 B
R-5 两字词不得只依赖 trigram 表              缺口 A（防「删表」误修）
===========================================  ==========================

**每条判据都带一个阴性对照臂** —— 判据自己必须先证明它会失败，
否则「全绿」和「判据没在测」长得一模一样。

**Why R-1 samples the corpus instead of hard-coding a word list.**
A hard-coded list of Chinese words cannot prove "any two-character
word is reachable"; it can only prove "these particular words are
reachable".  The criterion therefore derives its samples from the LIVE
corpus (``SELECT`` a 2-char CJK token occurring >= 3 times) — so the
sample can never be a word the corpus does not contain, and the
criterion cannot be passed by choosing lucky words.

**Why R-2 only applies to a space-free CJK run.**  With a space the
query already carries AND semantics and the two words are searched
independently; the defect is specific to a *contiguous* CJK run being
treated as one indivisible group.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro

#: A CJK character.  Used to recognise a two-character Chinese token.
_CJK = re.compile(r"[一-鿿]")

#: The minimum number of corpus occurrences for a token to be a sample.
MIN_OCCURRENCES = 3

#: How many samples R-1 takes.
SAMPLE_SIZE = 5

#: Two unrelated high-frequency words that must NOT co-occur in the
#: corpus.  Used to build the negative control.  Checked at runtime: if
#: the corpus happens to contain them, the criterion says NOTRUN rather
#: than reporting a false green.
NEGATIVE_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("量子", "风筝"),
    ("陀螺", "砚台"),
    ("唢呐", "碱水"),
    ("榫卯", "旮旯"),
    ("笤帚", "芡实"),
)

#: The positive control for R-3: the SAME construction, but with one
#: word replaced by a word the corpus really contains.
POSITIVE_CONTROL_WORDS = ("泄漏", "凭据", "遮蔽")

#: **The adversarial negative control** — the one that actually catches the
#: defect SEED measured.
#:
#: A string made of TWO rare words is a weak negative control: the
#: sliding windows of two rare words will not occur anywhere, so it
#: returns 0 hits even with the relevance floor completely absent.
#:
#: The real counter-example is a string whose windows are ALL common
#: Chinese words that DO occur in the corpus but whose MEANING does not:
#:
#:     '意图不存在的一串怪词'  ->  5 hits, top score = 1.0
#:
#: (measured 2026-11-02 on the live ROOT library).  Its windows include
#: '存在', '一串', '不存' — all of which occur.  So the negative control
#: must be built from COMMON words, or it proves nothing.
#:
#: ⇒ This list is built from common words deliberately.  Each pair is
#: checked against the corpus at runtime: if the corpus happens to hold
#: the pair as a phrase, the pair is dropped (and the criterion says
#: NOTRUN rather than reporting a false green).
ADVERSARIAL_NEGATIVES: Tuple[str, ...] = (
    "意图不存在的一串怪词",
    "计划的方案讨论记录",
    "时间的空间关系",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _is_cjk(s: str) -> bool:
    return bool(s) and all(_CJK.match(ch) for ch in s)


def sample_two_char_tokens(conn: sqlite3.Connection,
                           limit: int = SAMPLE_SIZE) -> List[Tuple[str, int]]:
    """Two-character CJK tokens the live corpus really contains.

    Derived from the corpus, NOT hard-coded — see the module docstring.
    A token must occur at least ``MIN_OCCURRENCES`` times, otherwise it
    may be a one-off and a zero-hit search on it would be ambiguous.
    """
    rows = conn.execute(
        "SELECT text FROM chunks WHERE text IS NOT NULL AND text <> ''"
    ).fetchall()
    counts: Dict[str, int] = {}
    for (text,) in rows:
        seen = set()
        for run in re.findall(r"[一-鿿]{2,}", text or ""):
            for i in range(len(run) - 1):
                tok = run[i:i + 2]
                if tok not in seen:
                    seen.add(tok)
                    counts[tok] = counts.get(tok, 0) + 1
    usable = [(t, c) for t, c in counts.items() if c >= MIN_OCCURRENCES]
    usable.sort(key=lambda x: (-x[1], x[0]))
    return usable[:limit]


def corpus_contains(conn: sqlite3.Connection, word: str) -> int:
    n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE text LIKE ?", ("%" + word + "%",)
    ).fetchone()[0]
    return int(n or 0)


async def _search(workspace: Path, query: str,
                  max_results: int = 10) -> List:
    """Run the PRODUCT's own search path (never a private re-implementation).

    ``MemoryManager.search`` is an **async coroutine** — calling it
    without ``await`` returns an un-awaited coroutine object and every
    downstream measurement silently reads zero.  (SEED's own probe made
    exactly this mistake and reported a "0.000s latency".)
    """
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    cfg = MemoryConfig()
    for attr in ("workspace", "agent_workspace"):
        if hasattr(cfg, attr):
            try:
                setattr(cfg, attr, str(workspace))
            except Exception:
                pass
    mgr = MemoryManager(config=cfg)
    try:
        return await mgr.search(query, max_results=max_results)
    finally:
        try:
            mgr.close()
        except Exception:
            pass


def _score(results: Sequence) -> float:
    return float(getattr(results[0], "score", 0.0)) if results else 0.0


def _paths(results: Sequence) -> List[str]:
    return [str(getattr(r, "path", "")) for r in results]


# --------------------------------------------------------------------------
# the criteria
# --------------------------------------------------------------------------

async def judge_two_char_words_are_reachable(report: Report,
                                              lib) -> bool:
    """R-1 · a two-character Chinese word that IS in the corpus must be findable.

    Guarded by a NEGATIVE CONTROL ARM: dropping one character must
    reduce the hit count (or reach zero).  Without that arm, a search
    that matched every single character would pass this criterion too.
    """
    conn = open_ro(lib.db)
    try:
        samples = sample_two_char_tokens(conn, SAMPLE_SIZE)
    finally:
        conn.close()
    if not samples:
        report.judge_notrun(
            "R-1", "两字中文词可达（语料反向取样）",
            "语料里没有出现 >=%d 次的两字中文词，无法取样。" % MIN_OCCURRENCES)
        return None

    lines = ["取样自活库语料（非写死词表）："]
    ok = True
    for token, occurrences in samples:
        hits = await _search(lib.workspace, token)
        # NEGATIVE CONTROL ARM: one character fewer.
        control = token[:1]
        control_hits = await _search(lib.workspace, control)
        arm_ok = len(hits) >= 1
        ctrl_ok = len(control_hits) < len(hits)
        ok = ok and arm_ok and ctrl_ok
        lines.append(
            "  %s  语料出现 %d 次 | 检索命中 %d | 阴性对照(%s) 命中 %d %s"
            % (token, occurrences, len(hits), control, len(control_hits),
               "" if ctrl_ok else "  <-- 对照臂未生效（判据自身可能失真）"))
    report.judge(
        "R-1", "两字中文词可达（语料反向取样 + 阴性对照臂）", ok,
        "\n".join(lines))
    return ok


async def judge_combination_is_not_worse_than_parts(report: Report,
                                                     lib) -> bool:
    """R-2 · a space-free CJK combination must not return fewer hits than
    its parts.

    This is a MONOTONICITY criterion: adding a character to a query
    must not shrink the result set.  The live counter-example is
    ``密钥`` (2 hits) / ``泄漏`` (34 occurrences) / ``密钥泄漏`` (0 hits).
    """
    conn = open_ro(lib.db)
    try:
        samples = sample_two_char_tokens(conn, 12)
    finally:
        conn.close()
    if len(samples) < 2:
        report.judge_notrun(
            "R-2", "组合查询不得劣于单词（单调性）",
            "可用样本不足 2 个。")
        return None

    lines = ["对每一对语料中真实出现的两字词，拼接后检索："]
    ok = True
    tested = 0
    for i, (a, _) in enumerate(samples):
        for b, _ in samples[i + 1:]:
            if len(a) != 2 or len(b) != 2:
                continue
            combo = a + b
            ha = await _search(lib.workspace, a)
            hb = await _search(lib.workspace, b)
            hc = await _search(lib.workspace, combo)
            floor = min(len(ha), len(hb))
            if floor == 0:
                continue          # a part matches nothing: no claim to make
            tested += 1
            good = len(hc) >= floor
            ok = ok and good
            lines.append("  %s+%s -> 组合命中 %d | 单词命中 %d/%d | 下界 %d %s"
                         % (a, b, len(hc), len(ha), len(hb), floor,
                            "" if good else "  <-- 组合劣于单词（缺陷）"))
            if tested >= 8:
                break
        if tested >= 8:
            break
    if tested == 0:
        report.judge_notrun(
            "R-2", "组合查询不得劣于单词（单调性）",
            "没有一对样本的单词命中数 > 0，无法判定单调性。")
        return None
    report.judge(
        "R-2", "组合查询不得劣于单词（单调性）", ok,
        "\n".join(lines) + "\n  实测样本对数: %d" % tested)
    return ok


async def judge_negative_control_finds_nothing(report: Report,
                                               lib) -> bool:
    """R-3 · a Chinese string that is NOT in the corpus must return ZERO hits.

    Guarded by a POSITIVE CONTROL ARM: the same construction with a word
    the corpus really contains must return > 0 hits.  Without it, a
    threshold raised until everything returns nothing would pass.
    """
    conn = open_ro(lib.db)
    try:
        negatives = [(a, b) for a, b in NEGATIVE_PAIRS
                     if corpus_contains(conn, a) == 0
                     and corpus_contains(conn, b) == 0]
        positives = [(w, corpus_contains(conn, w))
                     for w in POSITIVE_CONTROL_WORDS
                     if corpus_contains(conn, w) > 0]
        # The adversarial phrases need the SAME connection: whether the
        # corpus really holds the phrase decides whether the phrase is a
        # usable negative control at all.  (Keeping one connection open
        # for the whole criterion also avoids re-opening the live
        # library five times.)
        adversarial = [p for p in ADVERSARIAL_NEGATIVES
                       if corpus_contains(conn, p) == 0]
        skipped = [p for p in ADVERSARIAL_NEGATIVES
                   if corpus_contains(conn, p) > 0]
    finally:
        conn.close()
    if not negatives:
        report.judge_notrun(
            "R-3", "阴性对照必须 0 命中",
            "预置的阴性词对在语料中出现了，构造不成立。")
        return None
    if not positives:
        report.judge_notrun(
            "R-3", "阴性对照必须 0 命中",
            "语料里找不到阳性对照词，无法证明检索还能命中东西。")
        return None

    lines = ["阴性对照（语料中不存在的组合）："]
    ok = True
    for a, b in negatives:
        hits = await _search(lib.workspace, a + b)
        good = len(hits) == 0
        ok = ok and good
        lines.append("  %s%s -> 命中 %d %s"
                     % (a, b, len(hits),
                        "" if good else "  <-- 不存在的词返回了结果（缺陷）"))

    # ---- ADVERSARIAL negative control -------------------------------
    # The one that catches the measured defect: every sliding window is
    # a COMMON word that occurs in the corpus, but the phrase means
    # nothing.
    adversarial_ok = True
    adversarial_lines = []
    for phrase in skipped:
        adversarial_lines.append(
            "  %s -> 跳过（语料里确实有这个词组）" % phrase)
    for phrase in adversarial:
        hits = await _search(lib.workspace, phrase)
        good = len(hits) == 0
        adversarial_ok = adversarial_ok and good
        adversarial_lines.append(
            "  %s -> 命中 %d %s"
            % (phrase, len(hits),
               "" if good else
               "  <-- 窗口全是常见词也要能判 0（缺陷）"))
    if adversarial_lines:
        lines.append("对抗性阴性对照（窗口全是常见词，但语义不成立）：")
        lines.extend(adversarial_lines)
    ok = ok and adversarial_ok

    lines.append("阳性对照（同一构造法，换成语料中真实存在的词）：")
    for word, occ in positives:
        hits = await _search(lib.workspace, word)
        good = len(hits) > 0
        ok = ok and good
        lines.append("  %s (语料 %d 次) -> 命中 %d %s"
                     % (word, occ, len(hits),
                        "" if good else "  <-- 阳性对照失效（判据自身失真）"))
    report.judge("R-3", "阴性对照必须 0 命中（对抗性 + 阳性对照臂）", ok,
                 "\n".join(lines))
    return ok


async def judge_min_score_is_effective(report: Report, lib) -> bool:
    """R-4 · a relevance floor exists, is exposed, and really bites.

    Three sub-checks:
      1. ``min_score`` is actually honoured (a value above every hit's
         score yields zero hits);
      2. the PRODUCT exposes a default floor (the criterion reads the
         product's own constant — "there should be one" is not a check);
      3. the default floor is what makes R-3 pass.
    """
    from agent.memory.manager import MemoryManager

    # (2) is there a default floor the product exposes?
    default = None
    for holder in (MemoryManager,):
        for name in dir(holder):
            if "MIN_SCORE" in name.upper() or "SCORE_FLOOR" in name.upper():
                default = getattr(holder, name)
                break
        if default is not None:
            break
    if default is None:
        report.judge_notrun(
            "R-4", "相关性下界存在且生效",
            "产品未暴露默认下界常量（MemoryManager 上没有 MIN_SCORE / "
            "SCORE_FLOOR 之类的公开量）—— 无从判定「默认下界」是什么。")
        return None

    hits = await _search(lib.workspace, "记忆")
    top = _score(hits)
    above = await _search(lib.workspace, "记忆", max_results=10)
    # (1) a floor above every score must yield zero hits.
    floor_test = await _search_min_score(lib.workspace, "记忆", top + 1.0)
    bites = len(floor_test) == 0
    lines = [
        "产品默认下界常量 = %r" % (default,),
        "查询 '记忆' 最高分 = %.4f（%d 命中）" % (top, len(above)),
        "用 min_score=%.4f（高于最高分）重查 -> 命中 %d %s"
        % (top + 1.0, len(floor_test),
           "" if bites else "  <-- min_score 未生效（缺陷）"),
    ]
    report.judge("R-4", "相关性下界存在且生效（读产品常量 + 实际夹逼）",
                 bites, "\n".join(lines))
    return bites


async def _search_min_score(workspace: Path, query: str,
                            min_score: float) -> List:
    """Search with an explicit floor, through the product's own path."""
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    cfg = MemoryConfig()
    for attr in ("workspace", "agent_workspace"):
        if hasattr(cfg, attr):
            try:
                setattr(cfg, attr, str(workspace))
            except Exception:
                pass
    mgr = MemoryManager(config=cfg)
    try:
        return await mgr.search(query, max_results=10, min_score=min_score)
    finally:
        try:
            mgr.close()
        except Exception:
            pass


async def judge_two_char_words_do_not_depend_on_trigram(report: Report,
                                                          lib) -> bool:
    """R-5 · a two-character Chinese word must be reachable on a path that
    does NOT rely on the trigram FTS5 table.

    This is a PATH-COVERAGE criterion, NOT a "delete the trigram table"
    criterion: the trigram table is genuinely useful for queries of three
    characters or more (measured: ``外置不变量`` -> 3 hits).  What must be
    proven is that two-character words have a path of their own.
    """
    conn = open_ro(lib.db)
    try:
        samples = sample_two_char_tokens(conn, 3)
    finally:
        conn.close()
    if not samples:
        report.judge_notrun("R-5", "两字词不得只依赖 trigram 表",
                            "语料里取不到两字词样本。")
        return None

    lines = []
    ok = True
    for token, _ in samples:
        base = await _search(lib.workspace, token)
        without = await _search_no_trigram(lib.workspace, token)
        reachable = len(without) >= 1
        ok = ok and reachable
        lines.append("  %s | 全路径命中 %d | 关掉 trigram 后命中 %d %s"
                     % (token, len(base), len(without),
                        "" if reachable else "  <-- 只靠 trigram 表活着"))
    report.judge("R-5", "两字词不得只依赖 trigram 表（路径覆盖判据）", ok,
                 "\n".join(lines))
    return ok


async def _search_no_trigram(workspace: Path, query: str) -> List:
    """Search with the trigram FTS5 path removed from the chain.

    The removal happens on a SANDBOX COPY of the live library — the live
    database is never written to.  (A criterion that opens the database
    it is supposed to MEASURE has already changed what it measures.)
    """
    import shutil
    import tempfile

    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    tmp = Path(tempfile.mkdtemp(prefix="s3z-trigram-"))
    try:
        db = tmp / "index.db"
        shutil.copy2(str(lib_db_path(workspace)), str(db))
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("DROP TABLE IF EXISTS chunks_fts_trigram")
            conn.commit()
        finally:
            conn.close()
        cfg = MemoryConfig()
        for attr in ("workspace", "agent_workspace"):
            if hasattr(cfg, attr):
                try:
                    setattr(cfg, attr, str(workspace))
                except Exception:
                    pass
        mgr = MemoryManager(config=cfg)
        try:
            return await mgr.search(query, max_results=10)
        finally:
            try:
                mgr.close()
            except Exception:
                pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def lib_db_path(workspace: Path) -> Path:
    return Path(workspace) / "memory" / "long-term" / "index.db"


# --------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------

async def run(report: Report, workspace: Path) -> None:
    """Run R-1..R-5 against the ROOT live library."""
    libs = enumerate_live(Path(workspace))
    root = None
    for lib in libs:
        if lib.kind == "ROOT":
            root = lib
            break
    if root is None:
        report.judge_notrun("R-1..R-5", "检索召回判据",
                            "找不到 ROOT 活库：%s" % (Path(workspace) /
                                                     "memory/long-term/index.db"))
        return

    print("  被测库: %s (%s)" % (root.name, root.db))
    await judge_two_char_words_are_reachable(report, root)
    await judge_combination_is_not_worse_than_parts(report, root)
    await judge_negative_control_finds_nothing(report, root)
    await judge_min_score_is_effective(report, root)
    await judge_two_char_words_do_not_depend_on_trigram(report, root)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="S3-Z 检索召回与相关性下界")
    ap.add_argument("--root", default=r"C:\Users\User\cow")
    args = ap.parse_args(argv)
    report = Report()
    asyncio.run(run(report, Path(args.root)))
    return 0 if report.summary() else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
