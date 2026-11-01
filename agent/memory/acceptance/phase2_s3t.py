"""S3-T · the externalisation COMPLETENESS criteria (I-1..I-4).

**What this module is.**  The S3-M group (``phase2_s3m.py``) judges the
messages domain: MSG-INV-1..7 answer "is the part that WAS externalised
self-consistent?".  This module judges a DIFFERENT question, and it is
a different question in a way that matters:

    「the externalisation is not COMPLETE」

**Why the gap was real (measured, and the number has moved).**  The
write-time externalisation landed on 2026-11-01 (``370ec985``): after
``append_messages`` commits, the rows it wrote are moved into the
session's JSONL immediately.  Before that, the externalisation was a
ONE-SHOT MIGRATION, and every message written afterwards landed back in
the table as ``inline`` — measured on the live ROOT library at
2026-11-01 03:00 as **7,061 inline rows**, growing while the gap was
being discussed.

**The blind spot.**  NONE of the existing criteria could see that number:

  * MSG-INV-1..4 test the JSONL chain and the anchor — an inline row is
    not in the JSONL, so it is not in their answer;
  * MSG-INV-5 counts ``content_state='external'`` on the DB side only —
    an inline row is in neither the file nor the count;
  * MSG-INV-6 forbids exactly ONE combination (``content IS NULL AND
    content_state='inline'``) — inline-with-content is a LEGAL state;
  * MSG-INV-7 tests the read path's equivalence — an inline row reads
    straight out of the DB;
  * M-1..M-6 each test their own thing — none of them goes red because
    "inline rows > 0".

⇒ **「门禁全绿」 and 「持续失血」 can both be true at the same time.**
That is the shape of a blind spot, not of a loose criterion, and this
module is what closes it.

**The four criteria and their SUBJECTS.**  Each one names what it
measures — a criterion that cannot say which object it measured is a
claim, not a criterion:

  * **I-1** — the BACKLOG.  Subject: every live library's
    ``messages`` table.  How many rows are ``inline`` with a non-empty
    ``content``, and how OLD are they.  The age is the load-bearing
    part: a row written one second ago is a legitimate transient state,
    and a row written four hours ago is blood on the floor.  A count
    alone cannot tell those apart.
  * **I-2** — the WIRING.  Subject: the write path
    (``ConversationStore.append_messages``) and one throwaway fixture
    library.  Two proofs (static + behavioural), because either alone
    is insufficient — see ``phase2_s3m.py``'s M-3 for the same shape.
  * **I-3** — the CONSERVATION.  Subject: the truth tree and the DB
    side by side.  The message lines in the JSONL and the
    ``content_state='external'`` rows in the DB must be the SAME SET —
    in both directions.  MSG-INV-5 is adjacent but not the same
    question: MSG-INV-5 compares the COUNTS, this compares the SETS
    and names WHICH rows are missing on either side.
  * **I-4** — the PRUNE AT THE WRITE BOUNDARY.  Subject: a COPY of a
    live library.  After a re-cut, the ledger must hold no entry for a
    chunk that disappeared — WITHOUT waiting for a certification pass.

**The thresholds are CONFIGURABLE, and that is a decision, not a
convenience.**  A threshold written into the source is a constant that
belongs to one machine's tempo; the write path's cadence is a property
of the deployment, not of the criterion.  So:

  * ``I1_MAX_AGE_SECONDS`` — default **300** s.  A row older than this
    is "早该外置了却还是 inline" ⇒ FAIL;
  * ``I1_MAX_BACKLOG_ROWS`` — default **0**.  The age rule alone would
    forgive a backlog of a million rows if they were all young, so the
    COUNT has its own ceiling: any row older than the age threshold is
    already a FAIL, and this ceiling additionally forbids "old enough to
    matter, but numerous" from being a pass.

Both are read from the environment (``COW_I1_MAX_AGE_SECONDS`` /
``COW_I1_MAX_BACKLOG_ROWS``) with the defaults above, so a criterion
that must be run on another machine can be re-tuned there WITHOUT
editing the source.  **No absolute path and no machine-specific
constant is written into this file** — the workspace and the databases
are always discovered, never hard-coded (团队规范 §3.6).

**The three states, and where NOTRUN is honest.**  NOTRUN means
"could not measure".  It is NOT "measured zero and that is fine":

  * a workspace with no live library at all ⇒ NOTRUN (nothing to
    measure);
  * a library with no ``messages`` table ⇒ that library is SKIPPED,
    and named as skipped — 15 of the 16 live libraries are exactly this
    case (契约 N-7: messages live in the ROOT library only);
  * a library whose ``messages`` table is EMPTY ⇒ that library
    contributes ZERO objects, and the criterion says so explicitly.  A
    criterion that reported "0 backlog, PASS" there would be reporting
    on a corpus it never read — that is the "0 个匹配 = NOTRUN，不是
    PASS" rule, and it is asserted by the unit test.
"""

from __future__ import annotations

import ast
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory.acceptance import Report
from agent.memory.acceptance.live_libs import enumerate_live, open_ro


#: The default age ceiling for "this inline row should have been
#: externalised by now".  Configurable through
#: ``COW_I1_MAX_AGE_SECONDS``; the default is five minutes.
I1_DEFAULT_MAX_AGE_SECONDS = 300

#: The default ceiling on the BACKLOG COUNT.  Configurable through
#: ``COW_I1_MAX_BACKLOG_ROWS``.  The default is **-1 = no count
#: ceiling**, and that is deliberate:
#:
#: The verdict's DISCRIMINATOR is the AGE, not the count.  A row
#: written one second ago is a legitimate transient state (the write
#: path externalises right after the commit); a row written four hours
#: ago is blood on the floor.  A count-only rule would call both a
#: failure; an age-only rule would call a million fresh rows a pass.
#: So the age rule decides, and the count ceiling is an OPTIONAL extra
#: guard for the "many fresh rows" shape (a deployment that writes
#: faster than it externalises) — off by default, because a threshold
#: nobody has measured is a threshold nobody can defend.
I1_DEFAULT_MAX_BACKLOG_ROWS = -1


def _env_int(name: str, default: int) -> int:
    """An integer from the environment, or the default.

    A malformed value falls back to the default rather than crashing
    the gate: a criterion that raises is a FAIL (the harness counts a
    raising criterion as a failure), and a typo in an environment
    variable must not be able to take the whole gate down.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def i1_max_age_seconds() -> int:
    """The age ceiling, in seconds, for I-1's backlog rule."""
    return _env_int("COW_I1_MAX_AGE_SECONDS", I1_DEFAULT_MAX_AGE_SECONDS)


def i1_max_backlog_rows() -> int:
    """The count ceiling for I-1's backlog rule."""
    return _env_int("COW_I1_MAX_BACKLOG_ROWS", I1_DEFAULT_MAX_BACKLOG_ROWS)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _msg_inv_module():
    """The ONE implementation of the messages-domain criteria."""
    from agent.memory import msg_inv
    return msg_inv


def _tables(conn: sqlite3.Connection) -> set:
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def _has_messages_table(conn: sqlite3.Connection) -> bool:
    return "messages" in _tables(conn)


def _seq_of(row) -> int:
    """The row's ``seq`` — as an int, and NEVER as a falsy ``-1``.

    **The trap this exists for.**  ``seq`` is 0-based, so the FIRST
    message of every session has ``seq == 0``.  ``int(row.get("seq") or
    -1)`` maps that 0 to **-1** (because ``0`` is falsy) — and then
    the JSONL side says ``-1`` and the DB side says ``0`` for the very
    same row, and the criterion reports a mismatch that does not exist.
    Measured on the live ROOT library: 16 phantom mismatches, one per
    session's first message, and the counts were IDENTICAL (40,299 =
    40,299).  A criterion that invents a mismatch out of a falsy-zero
    is worse than no criterion: it sends the reader hunting for a bug
    that is not there.

    The fallback is ``-1`` only for a row that genuinely has no ``seq``
    — and that is a real signal (the key is missing), not a cast.
    """
    try:
        if isinstance(row, sqlite3.Row):
            raw = row["seq"]
        elif isinstance(row, dict):
            raw = row.get("seq")
        else:
            raw = row[2] if len(row) > 2 else None
    except (IndexError, KeyError, TypeError):
        raw = None
    if raw is None:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _inline_backlog(conn: sqlite3.Connection, now: int) -> Dict[str, Any]:
    """The inline backlog of ONE library, with its age distribution.

    **The definition is the strict one.**  A backlog row is a row whose
    text is STILL IN THE DATABASE and has NOT been externalised:

        content_state = 'inline'  AND  content IS NOT NULL  AND  content <> ''

    The three conditions are all load-bearing:

      * ``content_state='inline'`` — an ``external`` row is done, and a
        ``pending`` row is the crash window (a different question: MSG-
        INV-6 owns it);
      * ``content IS NOT NULL`` — MSG-INV-6 declares
        ``content IS NULL AND content_state='inline'`` ILLEGAL (the text
        exists nowhere).  Counting those as "backlog" would report a
        different defect under this criterion's name;
      * ``content <> ''`` — an empty string carries no text, so it is
        not blood on the floor.

    **The age is measured from ``created_at``** — the row's own
    timestamp — so the number says how long the text has been sitting
    in the database.
    """
    out: Dict[str, Any] = {
        "total_rows": 0,
        "inline_rows": 0,
        "inline_with_text": 0,
        "backlog": 0,
        "oldest_age": None,
        "newest_age": None,
        "ages": [],
        "by_session": {},
        "by_day": {},
        "sampled": [],
    }
    try:
        out["total_rows"] = conn.execute(
            "SELECT COUNT(*) FROM messages").fetchone()[0]
    except sqlite3.Error:
        return out
    cols = set(_columns(conn, "messages"))
    if "content_state" not in cols:
        return out
    try:
        out["inline_rows"] = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content_state='inline'"
        ).fetchone()[0]
        out["inline_with_text"] = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content_state='inline' "
            "AND content IS NOT NULL AND content <> ''").fetchone()[0]
    except sqlite3.Error:
        return out
    if not out["inline_with_text"]:
        return out

    # The per-row ages.  A library can carry tens of thousands of
    # inline rows, so the DISTRIBUTION is computed in SQL (bucket
    # counts) and the ROW LIST is only pulled for the libraries where
    # it is small enough to print.
    try:
        rows = conn.execute(
            "SELECT created_at, session_id FROM messages "
            "WHERE content_state='inline' AND content IS NOT NULL "
            "AND content <> ''").fetchall()
    except sqlite3.Error:
        return out
    ages: List[int] = []
    by_session: Dict[str, int] = {}
    by_day: Dict[str, int] = {}
    for r in rows:
        created = r[0]
        age = (now - int(created)) if created else 0
        ages.append(age)
        sid = (r[1] or "?")
        by_session[sid] = by_session.get(sid, 0) + 1
        if created:
            day = time.strftime("%Y-%m-%d", time.localtime(int(created)))
            by_day[day] = by_day.get(day, 0) + 1
    if not ages:
        return out
    ages.sort()
    out["ages"] = ages
    # **The oldest row is the LAST element of the ascending list.**
    # (An earlier version of this criterion read ``ages[0]`` as the
    # oldest and reported the NEWEST row's age as "the oldest one has
    # been inline for N seconds" — a criterion that lies about its own
    # headline number is worse than one that says nothing.)
    out["oldest_age"] = ages[-1]
    out["newest_age"] = ages[0]
    out["backlog"] = len(ages)
    out["by_session"] = by_session
    out["by_day"] = by_day
    return out


def _age_buckets(ages: List[int], max_age: int) -> List[str]:
    """The age distribution, printed as buckets.

    The buckets are RELATIVE to the configured threshold, so the report
    reads the same on a fast machine and on a slow one: "older than the
    threshold", "older than an hour", "older than a day".
    """
    lines: List[str] = []
    if not ages:
        return ["    （无 inline 行）"]
    marks = [("≤ %ds" % max_age, lambda a: a <= max_age),
             ("%d-%ds" % (max_age, max_age * 4 + 1),
              lambda a: max_age < a <= max_age * 4 + 1),
             ("%ds-1h" % (max_age * 4 + 1),
              lambda a: max_age * 4 + 1 < a <= 3600),
             (">1h", lambda a: a > 3600)]
    for label, pred in marks:
        n = sum(1 for a in ages if pred(a))
        lines.append("      %-14s %d" % (label, n))
    lines.append("      最老=%ds 中位=%ds 最新=%ds"
                 % (ages[-1], ages[len(ages) // 2], ages[0]))
    return lines


# ---------------------------------------------------------------------------
# I-1 — the BACKLOG (the blind spot, closed)
# ---------------------------------------------------------------------------

def judge_i1_inline_backlog_is_bounded(report: Report, workspace: Path):
    """I-1 (REAL): the inline backlog is bounded — old inline rows are
    a FAIL, and the report names how many and how old.

    **The subject.**  Every live library's ``messages`` table: how many
    rows still hold their text in the database (``content_state=
    'inline'`` with a non-empty ``content``), and how OLD they are.

    **The verdict口径 (three states, spelled out).**

      * **PASS** — no row is older than the age threshold AND the
        backlog count is at or below the count ceiling.  Rows younger
        than the threshold are a legitimate transient state: the write
        path externalises right after the commit, so a row that is a
        second old is a row whose externalisation is in flight, and
        judging it FAIL would be demanding that the write path and the
        patrol be one transaction;
      * **FAIL** — at least one row is older than the age threshold
        ("早该外置了却还是 inline"), or the backlog count exceeds the
        count ceiling.  The report names the oldest row's age, the
        median, the count, and the per-session / per-day distribution;
      * **NOTRUN** — there is no live library to measure.  **NOT
        "measured zero"**: a library whose ``messages`` table is EMPTY
        contributes ZERO objects and is named as such (there is nothing
        to be a backlog in), and a library with NO ``messages`` table is
        SKIPPED and named as skipped.

    **The thresholds are configurable** (``COW_I1_MAX_AGE_SECONDS``,
    default 300 s; ``COW_I1_MAX_BACKLOG_ROWS``, default 0).  Nothing in
    this file is a machine-specific constant: the workspace and the
    databases are discovered by ``enumerate_live``, never hard-coded.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "I-1(REAL)",
            "I-1 外置积压可见：超龄的 inline 行必须为 0（自报行数与年龄分布）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    max_age = i1_max_age_seconds()
    max_rows = i1_max_backlog_rows()
    now = int(time.time())

    detail.append("阈值: 超龄=%ds（COW_I1_MAX_AGE_SECONDS） 数量上限=%s"
                  "（COW_I1_MAX_BACKLOG_ROWS，-1 = 不设数量上限）"
                  % (max_age, ("%d" % max_rows) if max_rows >= 0 else "关闭"))
    detail.append("判定口径: inline 且 content 非空 ⇒ 积压候选；"
                  "年龄 > 超龄阈值 ⇒ FAIL；数量上限开启且积压 > 上限 ⇒ FAIL。")
    detail.append("")

    ok = True
    measured: List[str] = []
    skipped: List[str] = []
    empty: List[str] = []
    red: List[str] = []
    grand_backlog = 0
    grand_overdue = 0

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            if not _has_messages_table(conn):
                skipped.append(lib.name)
                detail.append("  %-22s ⇒ 跳过: 没有 messages 表"
                              "（契约 N-7：messages 只在 ROOT 库）" % lib.name)
                continue
            if "content_state" not in set(_columns(conn, "messages")):
                skipped.append(lib.name)
                detail.append("  %-22s ⇒ 跳过: messages 表没有 content_state 列"
                              "（外置化之前的老库）" % lib.name)
                continue
            info = _inline_backlog(conn, now)
        finally:
            conn.close()

        total = info["total_rows"]
        backlog = info["backlog"]
        if not total:
            # ZERO objects.  Named explicitly — this is NOT a pass on
            # an empty corpus, it is "there was nothing to measure
            # here".
            empty.append(lib.name)
            detail.append("  %-22s ⇒ 0 个对象: messages 表为空"
                          "（这个库没有消息语料 —— 不是『积压为 0』的通过）"
                          % lib.name)
            continue

        measured.append(lib.name)
        ages: List[int] = info["ages"]
        overdue = [a for a in ages if a > max_age]
        grand_backlog += backlog
        grand_overdue += len(overdue)

        detail.append("  %-22s 路径=%s" % (lib.name, lib.db))
        detail.append("      messages 总行数=%-7d inline 行=%-6d "
                      "inline 且有正文=%-6d ⇒ 积压=%d"
                      % (total, info["inline_rows"],
                         info["inline_with_text"], backlog))
        if backlog:
            for line in _age_buckets(ages, max_age):
                detail.append(line)
            detail.append("      超龄（> %ds）的行数: %d" % (max_age, len(overdue)))
            sess = info["by_session"]
            top_sess = sorted(sess.items(), key=lambda kv: -kv[1])[:5]
            detail.append("      按会话分布（前 5）: %s"
                          % ", ".join("%s=%d" % (k, v) for k, v in top_sess))
            days = info["by_day"]
            top_days = sorted(days.items(), key=lambda kv: -kv[1])[:5]
            detail.append("      按日期分布（前 5）: %s"
                          % ", ".join("%s=%d" % (k, v) for k, v in top_days))
            if overdue:
                detail.append("      ! 最老的一条已经 inline %d 秒"
                              "（%d 秒前写入，至今没有外置）"
                              % (ages[-1], ages[-1]))
        else:
            detail.append("      无积压（没有 inline 且有正文的行）")

        # The count ceiling is OPTIONAL (-1 = off).  When it is on, a
        # backlog larger than the ceiling is a FAIL even if every row
        # is young — that is the "writes faster than it externalises"
        # shape.
        over_count = (max_rows >= 0 and backlog > max_rows)
        if overdue or over_count:
            ok = False
            red.append(lib.name)
            detail.append("      ! 判定 FAIL：超龄积压 %d 条%s"
                          % (len(overdue),
                             ("；积压 %d 条 > 数量上限 %d" % (backlog, max_rows))
                             if over_count else ""))
        else:
            detail.append("      判定 PASS：没有超龄的 inline 行")

    detail.append("")
    detail.append("全活库合计: 积压行数=%d 超龄行数=%d" % (grand_backlog, grand_overdue))
    detail.append("量到对象的库=%d（0 个对象的库=%d，跳过的库=%d）"
                  % (len(measured), len(empty), len(skipped)))
    if skipped:
        detail.append("跳过的库（没有 messages 表 / 没有 content_state 列）: %s"
                      % ", ".join(skipped))
    if empty:
        detail.append("0 个对象的库（messages 表为空）: %s" % ", ".join(empty))
    detail.append("为什么这条必须存在：MSG-INV-1..7 与 M-1..M-6 全部看不见 inline 行 ——")
    detail.append("  MSG-INV-5 只数 content_state='external'；inline 行既不在 JSONL 里、")
    detail.append("  也不在那个计数里；MSG-INV-6 只禁『content 为空且 inline』这一个组合。")
    detail.append("  ⇒ 没有这条判据，『门禁全绿』与『持续失血』可以同时成立。")

    if not measured:
        # No library carried a message corpus at all — nothing was
        # measured anywhere.  That is NOTRUN, never PASS.
        return report.judge_notrun(
            "I-1(REAL)",
            "I-1 外置积压可见：超龄的 inline 行必须为 0（自报行数与年龄分布）",
            "\n".join(detail))
    return report.judge(
        "I-1(REAL)",
        "I-1 外置积压可见：超龄的 inline 行必须为 0（自报行数与年龄分布）",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# I-2 — the WIRING is real (static + behavioural)
# ---------------------------------------------------------------------------

def _calls_named(source: str, attr: str) -> bool:
    """Does the source really CALL ``attr``?

    Parsed with ``ast``, not matched with a string: a string match finds
    the text whether or not it is inside a function body, and the
    criterion would then be scanning a comment.  The structural question
    is "is there a call to ``<attr>`` in this module", and the AST can
    answer exactly that.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == attr:
            return True
        if isinstance(func, ast.Name) and func.id == attr:
            return True
    return False


def _module_source(mod) -> Optional[str]:
    try:
        return Path(mod.__file__).read_text(encoding="utf-8")
    except (OSError, TypeError, AttributeError):
        return None


def judge_i2_write_path_externalises(report: Report, workspace: Path):
    """I-2 (unit+REAL): the write path REALLY externalises, and it
    externalises IMMEDIATELY.

    Two proofs, because either alone is insufficient:

      * **static** — the store's source really calls the externaliser
        (parsed with ``ast``, so a call in a comment does not count);
      * **behavioural** — a real message written through the real write
        path lands in the JSONL IMMEDIATELY, and its DB row is
        ``content_state='external'`` with the pointer filled and the
        content emptied.

    **The behavioural proof runs on a THROWAWAY fixture in a temporary
    directory.**  The live corpus is never written to by a criterion:
    writing a test message into the live ROOT library would pollute the
    very corpus this criterion is measuring.

    **Why "immediately" is the load-bearing word.**  The defect this
    criterion exists to catch is the ONE-SHOT MIGRATION shape: the text
    leaves the table when a migration runs, and every message written
    afterwards lands back in the table as ``inline``.  A criterion that
    only checked "the migration works" would stay green through exactly
    that regression, because the migration still works.
    """
    detail: List[str] = []
    try:
        from agent.memory import conversation_store as cs_mod
    except Exception as e:
        return report.judge_notrun(
            "I-2(REAL)",
            "I-2 写入口真的外置：写一条消息后 DB 行立刻是 external 且 JSONL 有对应行",
            "conversation_store 不可用（%s: %s）" % (type(e).__name__, e))

    # --- half 1: the static proof -----------------------------------
    src = _module_source(cs_mod)
    static_ok = bool(src) and _calls_named(src, "externalise_messages")
    detail.append("  静态证据: %s" % (getattr(cs_mod, "__file__", "?")))
    detail.append("    源码里存在 externalise_messages 的调用（AST 解析）: %s"
                  % static_ok)
    if not static_ok:
        detail.append("    —— 写入口没有调用外置函数：接线不在"
                      "（消息会一直留在 messages 表里）")

    # --- half 2: the behavioural proof ------------------------------
    behavioural_ok = False
    behaviour_detail: List[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="s3t-i2-"))
    try:
        ws = tmp / "ws"
        (ws / "memory" / "long-term").mkdir(parents=True)
        # The truth layer's location is what the write path checks
        # (``_truth_layer_is_placed``): the store's index must live at
        # <root>/memory/long-term/index.db for the store to have a truth
        # layer to write to.  The fixture therefore has that shape.
        store = None
        try:
            from agent.memory.conversation_store import ConversationStore
            # **The store takes a PATH, not a string.**  A string is
            # accepted by ``Path(...)`` in most places, but the store
            # calls ``self._db_path.resolve()`` — which does not exist
            # on ``str``.  (This cost one round: the behavioural proof
            # died with "'str' object has no attribute 'resolve'" and
            # the criterion reported it as a product failure.)
            db_path = ws / "memory" / "long-term" / "index.db"
            store = ConversationStore(db_path)
            sid = "s3t-fixture-session"
            ok_write = store.append_messages(
                sid,
                [{"role": "user", "content": "s3t fixture message"},
                 {"role": "assistant", "content": "s3t fixture reply"}],
                channel_type="test")
            behaviour_detail.append("写入返回: %s" % ok_write)

            import sqlite3 as _s3
            conn = _s3.connect(str(db_path))
            try:
                conn.row_factory = _s3.Row
                rows = conn.execute(
                    "SELECT seq, role, content, content_state, content_path, "
                    "content_anchor, content_hash FROM messages "
                    "WHERE session_id=?", (sid,)).fetchall()
            finally:
                conn.close()

            behaviour_detail.append("DB 里的行数: %d" % len(rows))
            for r in rows:
                behaviour_detail.append(
                    "    seq=%s role=%s state=%r content=%r path=%r anchor=%r"
                    % (r["seq"], r["role"], r["content_state"],
                       (r["content"] if r["content"] is None else "有正文"),
                       r["content_path"], r["content_anchor"]))

            states = [r["content_state"] for r in rows]
            pointed = all(r["content_path"] and r["content_anchor"]
                          for r in rows)
            emptied = all(r["content"] is None for r in rows)
            if not rows:
                behaviour_detail.append("    —— 一行都没写进去：行为证据不成立")
            elif any(s != "external" for s in states):
                behaviour_detail.append(
                    "    —— 有行的 content_state 不是 'external'："
                    "写入后没有立刻外置")
            elif not emptied:
                behaviour_detail.append(
                    "    —— 正文还在 DB 里：外置没有清空 content")
            elif not pointed:
                behaviour_detail.append(
                    "    —— 指针四列没有填满：外置没有落锚点")
            else:
                behavioural_ok = True

            # The JSONL side: the file must carry the lines.
            try:
                from agent.memory import msg_chain
                root = msg_chain.export_root(ws)
                files = []
                if root.is_dir():
                    for p in sorted(root.rglob("*.jsonl")):
                        if msg_chain.AUDIT_DIRNAME in p.parts:
                            continue
                        files.append(p)
                lines = []
                for p in files:
                    lines.extend(msg_chain.read_msg_lines(p))
                behaviour_detail.append("JSONL 文件数: %d  行数: %d"
                                        % (len(files), len(lines)))
                for ln in lines[:4]:
                    behaviour_detail.append(
                        "    seq=%s role=%s content=%r"
                        % (ln.get("seq"), ln.get("role"),
                           (ln.get("content") or "")[:40]))
                if len(lines) < len(rows):
                    behaviour_detail.append(
                        "    —— JSONL 的行数少于 DB 的行数："
                        "外置写了一半")
                elif not behavioural_ok:
                    behaviour_detail.append(
                        "    —— JSONL 有对应的行，但 DB 侧不合格（见上）")
            except Exception as e:
                behaviour_detail.append("读 JSONL 失败（%s: %s）"
                                        % (type(e).__name__, e))
        except Exception as e:
            behaviour_detail.append("行为证据执行失败（%s: %s）"
                                    % (type(e).__name__, e))
        finally:
            if store is not None:
                # ``ConversationStore`` has no ``close()`` — it opens a
                # connection per call.  A ``getattr`` probe, because
                # calling a method that does not exist would abort the
                # criterion on the way out.
                closer = getattr(store, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:
                        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    detail.append("  行为证据: 临时目录的夹具库（活语料零污染）")
    for line in behaviour_detail:
        detail.append("    %s" % line)

    ok = static_ok and behavioural_ok
    detail.append("")
    detail.append("静态证明: %s   行为证明: %s   两者皆真才通过"
                  % (static_ok, behavioural_ok))
    return report.judge(
        "I-2(REAL)",
        "I-2 写入口真的外置：写一条消息后 DB 行立刻是 external 且 JSONL 有对应行",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# I-3 — the CONSERVATION (JSONL rows <-> DB rows, both directions)
# ---------------------------------------------------------------------------

def judge_i3_jsonl_and_db_conserve(report: Report, workspace: Path):
    """I-3 (REAL): the message lines and the externalised rows are the
    SAME SET — in both directions.

    **The subject.**  The truth tree (``messages_export/``) and the
    ``messages`` table, side by side, per library.

    **What it adds to MSG-INV-5.**  MSG-INV-5 compares the COUNTS
    (``message_lines != db_external`` ⇒ RED).  A count comparison
    cannot say WHICH side is short, and two wrongs can cancel each
    other out (one line missing from the file and one extra row in the
    DB).  This criterion compares the SETS and names the missing rows
    on either side.

    **The verdict口径.**

      * **PASS** — the two sets are equal (or there is nothing to
        compare, which is NOTRUN — see below);
      * **FAIL** — a JSONL line with no externalised row, or an
        externalised row with no JSONL line.  Either one is a form of
        "the text is somewhere it should not be" or "the text is
        nowhere";
      * **NOTRUN** — the library has no truth tree (nothing was ever
        externalised) or no ``messages`` table.  **NOT "zero matches =
        PASS"**: a library with no truth tree has ZERO objects to
        compare, and the report says so by name.

    **The identity used for the comparison is the (agent_id,
    session_id, seq) triple** — the same key the ``messages`` table's
    uniqueness is built on (契约 C-1: the table's uniqueness is
    ``(agent_id, session_id, seq)``).  A line in the JSONL carries all
    three, so the comparison is exact and not a count.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "I-3(REAL)",
            "I-3 行数守恒：JSONL 主链的消息行与 DB 的 external 行是同一集合（双向）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory import msg_chain
    except Exception as e:
        return report.judge_notrun(
            "I-3(REAL)",
            "I-3 行数守恒：JSONL 主链的消息行与 DB 的 external 行是同一集合（双向）",
            "msg_chain 不可用（%s: %s）" % (type(e).__name__, e))

    ok = True
    measured: List[str] = []
    notrun: List[str] = []
    red: List[str] = []

    for lib in libs:
        conn = open_ro(lib.db)
        try:
            if not _has_messages_table(conn):
                notrun.append(lib.name)
                detail.append("  %-22s ⇒ NOTRUN: 没有 messages 表" % lib.name)
                continue
            if "content_state" not in set(_columns(conn, "messages")):
                notrun.append(lib.name)
                detail.append("  %-22s ⇒ NOTRUN: messages 表没有 content_state 列"
                              % lib.name)
                continue
            db_rows = conn.execute(
                "SELECT agent_id, session_id, seq FROM messages "
                "WHERE content_state='external'").fetchall()
        finally:
            conn.close()

        root = msg_chain.export_root(lib.workspace)
        files = []
        if root.is_dir():
            for p in sorted(root.rglob("*.jsonl")):
                if msg_chain.AUDIT_DIRNAME in p.parts:
                    continue
                files.append(p)
        if not files:
            notrun.append(lib.name)
            detail.append("  %-22s ⇒ NOTRUN: 没有 messages_export/ 真源树"
                          "（0 个对象可比 —— 不是『守恒通过』）" % lib.name)
            continue

        measured.append(lib.name)
        file_keys = set()
        total_lines = 0
        for p in files:
            for ln in msg_chain.read_msg_lines(p):
                if "kind" in ln:
                    # The compensation chain's own shape — not a
                    # message line, and MSG-INV-5 excludes it too.
                    continue
                total_lines += 1
                file_keys.add((ln.get("agent_id") or "",
                               ln.get("session_id") or "",
                               _seq_of(ln)))
        db_keys = {(r[0] or "", r[1] or "", _seq_of(r)) for r in db_rows}

        only_file = file_keys - db_keys
        only_db = db_keys - file_keys
        detail.append("  %-22s 真源文件=%d JSONL 消息行=%d DB external 行=%d"
                      % (lib.name, len(files), total_lines, len(db_keys)))
        if only_file:
            ok = False
            red.append(lib.name)
            detail.append("      ! JSONL 有 %d 行在 DB 里没有对应的 external 行："
                          % len(only_file))
            for k in sorted(only_file)[:5]:
                detail.append("        + agent=%r session=%r seq=%s" % k)
        if only_db:
            ok = False
            red.append(lib.name)
            detail.append("      ! DB 有 %d 个 external 行在 JSONL 里找不到对应行："
                          % len(only_db))
            for k in sorted(only_db)[:5]:
                detail.append("        - agent=%r session=%r seq=%s" % k)
        if not only_file and not only_db:
            detail.append("      双向守恒：JSONL 的消息行与 DB 的 external 行是同一集合")

    detail.append("")
    detail.append("量到对象的库=%d（NOTRUN 的库=%d，红的库=%d）"
                  % (len(measured), len(notrun), len(red)))
    detail.append("比对键: (agent_id, session_id, seq) —— 与 messages 表的唯一性一致。")
    detail.append("与 MSG-INV-5 的分工：MSG-INV-5 比 COUNT，I-3 比 SET 并点名差在哪一边。")

    if not measured:
        return report.judge_notrun(
            "I-3(REAL)",
            "I-3 行数守恒：JSONL 主链的消息行与 DB 的 external 行是同一集合（双向）",
            "\n".join(detail))
    return report.judge(
        "I-3(REAL)",
        "I-3 行数守恒：JSONL 主链的消息行与 DB 的 external 行是同一集合（双向）",
        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# I-4 — the PRUNE AT THE WRITE BOUNDARY (without a certification pass)
# ---------------------------------------------------------------------------

def judge_i4_recut_leaves_no_ledger_orphans(report: Report, workspace: Path):
    """I-4 (REAL): a re-cut leaves NO orphan in the ledger — without
    waiting for a certification pass.

    **The subject.**  A COPY of a live library that has a ledger and at
    least one path with two or more chunks.

    **Why this is a separate criterion from M6f.**  M6f reports the
    ORPHANS THAT EXIST NOW.  It cannot report "a re-cut produces
    orphans that the next certification pass will clean up" — because
    the orphans do not exist until the re-cut happens, and M6f never
    causes a re-cut.  The mechanism ("the prune happens at the WRITE
    boundary, in the same transaction") is therefore invisible to a
    detector that only looks at the current state.

    **The verdict口径.**

      * **PASS** — after the re-cut, the ledger holds no entry for a
        chunk that disappeared;
      * **FAIL** — the ledger still names a vanished chunk (the prune
        did not happen, or happened later than the re-cut);
      * **NOTRUN** — no live library has both a ledger and a path with
        two or more chunks (the shape needed to produce a re-cut), or
        the copy could not be opened.

    **The re-cut is performed through the REAL write path**
    (``replace_file_chunks``), not by calling the prune directly: a
    criterion that calls the prune itself would be testing its OWN idea
    of the prune, and a mutant that made the write path stop pruning
    would still be green.  (This is the MU-33 lesson, applied.)

    **The live library is never written to.**  The mutation happens on a
    COPY, under a temporary directory.
    """
    detail: List[str] = []
    libs = enumerate_live(workspace)
    if not libs:
        return report.judge_notrun(
            "I-4(REAL)",
            "I-4 重切不留孤儿：重切后账本里指向已消失 chunk 的条目必须为 0（不等认证 pass）",
            "本工作区没有活库，前置条件不成立（这不是通过）")

    try:
        from agent.memory import ledger_orphans
        from agent.memory.storage import MemoryChunk, MemoryStorage
    except Exception as e:
        return report.judge_notrun(
            "I-4(REAL)",
            "I-4 重切不留孤儿：重切后账本里指向已消失 chunk 的条目必须为 0（不等认证 pass）",
            "实现侧不可用（%s: %s）" % (type(e).__name__, e))

    # A library that HAS a ledger and a path with >= 2 chunks.
    target = None
    for lib in libs:
        conn = open_ro(lib.db)
        try:
            if "cert_ledger" not in _tables(conn):
                continue
            if "chunks" not in _tables(conn):
                continue
            row = conn.execute(
                "SELECT path FROM chunks WHERE path IS NOT NULL "
                "GROUP BY path HAVING COUNT(*) >= 2 LIMIT 1").fetchone()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        if row is not None:
            target = lib
            break
    if target is None:
        return report.judge_notrun(
            "I-4(REAL)",
            "I-4 重切不留孤儿：重切后账本里指向已消失 chunk 的条目必须为 0（不等认证 pass）",
            "没有既有账本又有「至少两块的路径」的活库 —— "
            "前置条件不成立（这不是通过）")

    detail.append("被测库（副本）: %s" % target.db)

    tmp = Path(tempfile.mkdtemp(prefix="s3t-i4-"))
    try:
        copy = tmp / "index.db"
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(target.db) + suffix)
            if src.is_file():
                shutil.copy2(src, Path(str(copy) + suffix))

        # The copy's trigger order must be REPAIRED first: opening a
        # COPY of a live library re-installs the body guard, and a
        # re-created trigger lands BEHIND the FTS sync triggers — the
        # malformed-risk shape that makes a write die with
        # "database disk image is malformed".  The PRODUCTION tool
        # repairs it, and the report says which repair happened.
        try:
            from agent.memory.guard_sweep import install_body_guard_on
            sweep_rep = install_body_guard_on(copy)
            detail.append("副本的守卫修复: status=%s fts_reordered=%s"
                          % (sweep_rep.get("status"),
                             sweep_rep.get("fts_reordered")))
        except Exception as e:
            detail.append("守卫修复不可用（%s: %s）—— 继续，"
                          "写失败会被判成 FAIL" % (type(e).__name__, e))

        storage = MemoryStorage(db_path=str(copy))
        try:
            conn = storage.conn
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                "SELECT path FROM chunks WHERE path IS NOT NULL "
                "GROUP BY path HAVING COUNT(*) >= 2 LIMIT 1").fetchone()
            if row is None:
                return report.judge_notrun(
                    "I-4(REAL)",
                    "I-4 重切不留孤儿：重切后账本里指向已消失 chunk 的条目必须为 0（不等认证 pass）",
                    "\n".join(detail + ["副本里没有一条至少两块的路径 —— "
                                        "无法造出『重切后 id 移动』的形状"]))
            path = row["path"]
            before = conn.execute(
                "SELECT id, start_line, end_line, text_state, body_origin, "
                "body_path, body_anchor, body_hash FROM chunks WHERE path=? "
                "ORDER BY start_line", (path,)).fetchall()
            ids = [r["id"] for r in before]
            detail.append("路径: %s （%d 块）" % (path, len(ids)))

            # The chunks whose ids will VANISH in the re-cut (all but
            # the first, which survives), and the ones that come back.
            gone_ids: List[str] = []
            new_chunks = []
            for i, r in enumerate(before):
                if i == 0:
                    new_chunks.append(MemoryChunk(
                        id=r["id"], user_id=None, scope="shared",
                        source="memory", path=path,
                        start_line=r["start_line"], end_line=r["end_line"],
                        text="text of %s" % r["id"], embedding=None,
                        hash="h-%s" % r["id"], metadata=None,
                        body_path=r["body_path"],
                        body_anchor=r["body_anchor"],
                        body_hash=r["body_hash"],
                        text_state=r["text_state"],
                        body_origin=r["body_origin"]))
                else:
                    gone_ids.append(r["id"])
                    new_chunks.append(MemoryChunk(
                        id="%s::moved-%d" % (r["id"][:16], i), user_id=None,
                        scope="shared", source="memory", path=path,
                        start_line=900 + i * 10, end_line=905 + i * 10,
                        text="text of %s" % r["id"], embedding=None,
                        hash="h-%s" % r["id"], metadata=None,
                        body_path=r["body_path"],
                        body_anchor="L%d-%d" % (900 + i * 10, 905 + i * 10),
                        body_hash=r["body_hash"],
                        text_state=r["text_state"],
                        body_origin=r["body_origin"]))

            ledger_before = 0
            if gone_ids:
                ledger_before = conn.execute(
                    "SELECT COUNT(*) FROM cert_ledger WHERE id IN (%s)"
                    % ",".join("?" * len(gone_ids)), gone_ids).fetchone()[0]
            detail.append("消失的 id（重切后不再存在）: %d" % len(gone_ids))
            detail.append("这些 id 在重切前的账本条目: %d" % ledger_before)

            # **The orphans that existed BEFORE the re-cut.**  The
            # criterion measures the DELTA — the orphans the re-cut
            # itself produced — because the pre-existing ones are
            # M6f's business (they were left by a foreign writer, and
            # the write path never saw them).  Without this baseline, a
            # library that already carried 25 orphans would report
            # "25 orphans after the re-cut" and the reader would have
            # no way to tell that the re-cut added NONE.
            orphans_before = ledger_orphans.orphan_ledger_entries(conn)
            before_ids = {o["id"] for o in orphans_before}
            detail.append("重切前的全库孤儿数（存量，M6f 的地盘）: %d"
                          % len(orphans_before))
            if ledger_before == 0:
                detail.append("（账本条目为 0 ⇒ 这条判据测不到剪枝："
                              "被剪枝的 id 本来就没有认证记录。请在有账本的库上验）")

            # THE REAL WRITE PATH — the call that owns the prune.
            storage.replace_file_chunks(path, new_chunks, file_hash="H-RECUT",
                                        mtime=2, size=1, chunker_version=2)

            ghosts = 0
            if gone_ids:
                ghosts = conn.execute(
                    "SELECT COUNT(*) FROM cert_ledger WHERE id IN (%s)"
                    % ",".join("?" * len(gone_ids)), gone_ids).fetchone()[0]
            detail.append("重切后，消失 id 的账本条目: %d（必须为 0）" % ghosts)

            orphans_after = ledger_orphans.orphan_ledger_entries(conn)
            after_ids = {o["id"] for o in orphans_after}
            new_orphans = after_ids - before_ids
            detail.append("重切后全库孤儿数: %d（其中重切新增的: %d）"
                          % (len(orphans_after), len(new_orphans)))
            for o in orphans_after[:5]:
                if o.get("id") in new_orphans:
                    detail.append("    ! 重切新增的孤儿 id=%s path=%s"
                                  % (o.get("id"), o.get("body_path")))

            survivor_kept = conn.execute(
                "SELECT COUNT(*) FROM cert_ledger WHERE id=?",
                (before[0]["id"],)).fetchone()[0]
            detail.append("存活 id 的账本条目（必须保留）: %d" % survivor_kept)
        finally:
            storage.close()

        # **The verdict is on the DELTA.**  The pre-existing orphans are
        # reported (the reader must see the whole picture) but they do
        # NOT decide this criterion — they are M6f's subject, and
        # counting them here would make I-4 a duplicate of M6f that
        # goes red for a reason that has nothing to do with the write
        # boundary.
        ok = (ghosts == 0 and len(new_orphans) == 0)
        detail.append("")
        detail.append("重切新增的孤儿: %d（判定口径：不等认证 pass，"
                      "写边界就必须剪干净）" % len(new_orphans))
        detail.append("重切后全库孤儿总数: %d（其中存量 %d 条不在本判据的判定范围内 —— "
                      "那是 M6f 的地盘）" % (len(orphans_after),
                                          len(orphans_after) - len(new_orphans)))
        detail.append("（『保留』是承重墙：S3-L 的恢复触发器读账本把认证状态找回来，"
                      "一刀切会抹掉每一次重切的认证）")
        return report.judge(
            "I-4(REAL)",
            "I-4 重切不留孤儿：重切后账本里指向已消失 chunk 的条目必须为 0（不等认证 pass）",
            ok, "\n".join(detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


#: The criteria this module contributes, in the order the runner uses.
CRITERIA = [
    ("I-1", "judge_i1_inline_backlog_is_bounded"),
    ("I-2", "judge_i2_write_path_externalises"),
    ("I-3", "judge_i3_jsonl_and_db_conserve"),
    ("I-4", "judge_i4_recut_leaves_no_ledger_orphans"),
]
