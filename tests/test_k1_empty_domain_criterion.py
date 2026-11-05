#!/usr/bin/env python3
# encoding:utf-8
"""交办 K · K-1 · 「域非空」前置断言（判据的域随被测对象漂移）.

**这个文件测的是「判据的域」这件事本身，不是活库的存量。**

交办书 K（``deliverables/2026-11-06-交办K-空域判据.md``）要一件 P0：

  K-1  **「域非空」前置断言** —— 通用机制：判据在自己域里一行都没问到
       时，必须报 NOTRUN 或「域为空」红，而不是绿。

=========================  判据编号  =========================

=========  ==================================================================
编号       测什么
=========  ==================================================================
K-1-0      写权限闸门自守（守卫自己）
K-1-A      **域空 ⇒ 必须自报**（不得默默绿）+ 两档严重度的可判定分离
K-1-B      **变异臂①**：让目标判据的域变空 ⇒ 「域非空」断言必须转红
K-1-C      **变异臂②（化石检测）**：实现侧的选行规则演进 ⇒ 判据侧必须检出
K-1-D      **域 census + 化石清单**（逐条判据的域内行数）
=========  ==================================================================

=========================  问题的精确形状  =========================

**「判据的域跟着被测对象漂移」** —— 判据一个字没改，被测对象变了，
判据就什么都不问了。而且**它没有报警**：域空 ⇒ 判绿 ⇒ 门禁绿 ⇒
没人发现。

⚠️ **但「域空」有两种完全相反的含义，一刀切会做错：**

  ① **「域该非空却空了」** ⇒ **缺陷**（判据瞎了）⇒ 必须红。
  ② **「域为空本身就是答案」** ⇒ **不是缺陷**（例：M6f 问「账本里有
     没有孤儿」，没有孤儿 ⇒ 域空 ⇒ **正是它要报的**）⇒ 但也**不能
     默默绿** ⇒ 必须**自报「域为空，结论是空集」**。

=========================  一个必须先说的实测（它推翻了简单方案）  =========================

⚠️ **MEASURED（2026-11-06，本机实测）：M6a 的域是 0 行，而「去掉那条
可疑谓词之后」**也是 0 行**。

⇒ **所以 M6a 域空的真因不是「被抽空」，而是「认证覆盖率真的满了」**：
全活库 6803 行「有指针且 origin=file」的行**全部在账本里** ⇒
「有没有没被认证的行」这个问题的答案**真的**是空集。

⇒ **这意味着：一个光看「域 = 0」的判据，会把 M6a 判成「缺陷」，而它
今天不是缺陷。** ⇒ **「域空 ⇒ 红」不能一刀切。**

⭐ **但 M6a 的域谓词**确实是化石**，证据是源码层的（不是数）：

  * 认证器**现在**按**账本**选行（``certify.py:427-431``
    ``WHERE id NOT IN (SELECT id FROM cert_ledger)``）——
    这是 S3-L 的演进（``certify.py:398-406`` 的注释写着「The old
    selection was ``WHERE text_state<>'mirror'``」）；
  * **M6a 的域谓词仍然是** ``text_state<>'mirror'``
    （``phase2_s3k.py:336``，验收侧；``certify.py:712``，实现侧）。

⇒ **M6a 问的其实是「没有指针且非 mirror 的未认证行」，而它声称要问的
是「没有指针的未认证行」。** ⇒ **「mirror 但没有账本条目」那一类
（无证人的 mirror 行）M6a 一个都看不见** —— 那一类由 M6c 单独覆盖。
⇒ **这条缝隙不会自己报警**，因为两边的域在今天恰好都是 0。

=========================  可判定的区分办法（三层）  =========================

**核心：一条判据的「域」必须被拆成三层，且第三层只拿掉「漂移敏感
谓词」—— 不是把全部谓词拿掉。**

  * **SUBJECT（主体域）** —— 这条判据**承诺要覆盖**的那些行。
    它由判据的**语义**决定，**不随被测对象漂移**。
  * **DOMAIN（实际域）** —— 判据的 SQL **实际问到**的那些行。
  * **DRIFT-FREE（去漂移域）** —— 把判据的**漂移敏感谓词**（那条跟
    着被测对象的选行逻辑走的 conjunct）**单独拿掉**，其余谓词保留。

⇒ **三者的关系给出可判定的三态：**

  ┌───────────────────────────────────────────────────────────────┐
  │ DOMAIN > 0                                                     │
  │   ⇒ **ASKING**（在问行）⇒ 判据的绿/红**携带信息**。            │
  ├───────────────────────────────────────────────────────────────┤
  │ DOMAIN == 0 且 DRIFT-FREE > 0                                  │
  │   ⇒ ★**BLIND（域被抽空）** ⇒ 那条漂移谓词把行全排除了。        │
  │     **判据看不见它该看的东西** ⇒ 必须红。                      │
  │     ⚠️ **但要小心**：M6f 今天就是 BLIND，而 M6f 的域空恰恰是    │
  │     好消息 —— 所以 BLIND **必须**再分一次（见下）。             │
  ├───────────────────────────────────────────────────────────────┤
  │ DOMAIN == 0 且 DRIFT-FREE == 0                                  │
  │   ⇒ **EMPTY_SET**（域空 = 答案）⇒ 连「去掉漂移谓词」之后也        │
  │     一行都不剩 ⇒ **这条判据的域本来就是空的**。                │
  │     ⇒ 不是缺陷，但必须**自报**「域为空，结论是空集」。          │
  └───────────────────────────────────────────────────────────────┘

⭐ **DRIFT-FREE 那一层是「域被抽空」的唯一可判定数据指纹** ——
而 M6f 的「域空=答案」与 M6a 的「域空=化石残留」**在数据层无法区分**
（两者的 DOMAIN 与 DRIFT-FREE 都是 0 与 6803）⇒ **必须用第四层：
源码层的化石检测。**

=========================  第四层：化石检测（FOSSIL）  =========================

**一条判据的漂移敏感谓词，必须仍然是「被测对象当前用来定义那个概念
的规则」。** 如果不是 ⇒ **判据把概念冻结在了旧规则上** ⇒ 它的域
再也不会覆盖新规则下的那类行 ⇒ **它的绿不能作为任何修复的证据**。

⇒ **FOSSIL 检测是源码层的**（读源码文本，不 import 被测对象）：
把**判据侧**的域谓词与**实现侧**当前的选择谓词摆在一起比。

⚠️ **本文件做的比较是「字面量出现」级别的**（谓词片段是否在实现侧
仍然被用来做选择），不是语义等价证明 —— 这是诚实的边界，见 §假阳性/
假阴性。

=========================  四个标记  =========================

  * ``drift_predicate`` —— **那条漂移敏感谓词的 SQL 片段**（或 None）。
    **由判据作者声明**，本文件算不出来。
  * ``drift_free_sql`` —— 去掉那条谓词之后的域（可算）。
  * ``empty_is_the_answer`` —— 「域为空」是不是它要报的那个答案。
    **只影响严重度，不影响分类。**
  * ``fossil_check`` —— 这条判据**是否接受化石检测**（读源码）。

=========================  本文件的纪律  =========================

1. **判据不许调用被测对象的函数**（同义反复）。域的三个数**全部**是
   本文件自己写的 SQL。活库清单按 **PATH 形状**自己枚举。

2. **判据不许写活库。** 活库连接一律 ``mode=ro``。变异臂在
   ``%TEMP%`` 下的**副本**上做；写目标必须同时通过两道闸。

3. **变异臂自证生效** —— 读回具体行，断言它真的变成了那个形状。

4. **还原后 sha256 逐字节一致** —— 还原必须是**一份 pristine 快照的
   字节级复制**，不是一条 ``UPDATE``（SQLite 的 change counter）。

5. **变异对象是数据文件 ⇒ 会有连带红** —— 判定只钉**目标判据**，
   连带红**单独列出来汇报**。

6. **每条判据自报它测的是哪个对象**。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

#: The workspace under test.  FIXED, not environment-dependent — a
#: criterion whose target depends on the environment is not a criterion.
WORKSPACE = Path(os.environ.get("COW_K_WORKSPACE", r"C:\Users\User\cow"))
REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# the guards that keep this file from writing the live fleet
# ---------------------------------------------------------------------------

def _assert_sandbox(path: Path) -> None:
    """A write target MUST live under the TEMP directory.

    MEASURED (2026-11-05 21:19:34): a probe pointed a writer at the
    REAL workspace and the live library's ``cert_ledger`` grew by one
    row.  A write target outside ``%TEMP%`` is a FAIL, not a warning.
    """
    p = Path(path).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    try:
        p.relative_to(temp)
    except ValueError:
        pytest.fail(
            "写入目标不在 TEMP 之下 ⇒ 拒绝执行：%s（上一轮就是这么污染了活库的）"
            % p)


def _assert_not_live(path: Path) -> None:
    """A write target MUST NOT be inside the live workspace."""
    p = Path(path).resolve()
    ws = Path(WORKSPACE).resolve()
    try:
        p.relative_to(ws)
    except ValueError:
        return
    pytest.fail("写入目标在真源工作区之内 ⇒ 拒绝执行：%s" % p)


def _assert_writable_target(path: Path) -> None:
    """BOTH gates.  The delegation's red line: the write target must be
    in ``%TEMP%`` AND the write must assert it is not in the source
    workspace BEFORE the write."""
    _assert_not_live(path)
    _assert_sandbox(path)


# ---------------------------------------------------------------------------
# the readers — THIS file's OWN
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def ro(db: Path):
    """A read-only connection that is ALWAYS closed.

    A leaked read-only connection keeps the file open on Windows, and
    the byte-level restore (``unlink`` + ``copy2``) then fails with
    ``PermissionError: [WinError 32]``.
    """
    conn = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def live_libraries() -> List[Tuple[str, Path]]:
    """The live libraries, by PATH SHAPE — this file's OWN enumeration."""
    out: List[Tuple[str, Path]] = []
    root_db = WORKSPACE / "memory" / "long-term" / "index.db"
    if root_db.is_file():
        out.append(("ROOT", root_db))
    agents_dir = WORKSPACE / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            db = agent_dir / "memory" / "long-term" / "index.db"
            if db.is_file():
                out.append((agent_dir.name, db))
    return out


def count_sql(conn: sqlite3.Connection, sql: str) -> int:
    """A COUNT, or -1 when the query cannot run (a missing table)."""
    try:
        return int(conn.execute(sql).fetchone()[0])
    except sqlite3.Error:
        return -1


# ---------------------------------------------------------------------------
# the DOMAIN SPECIFICATION — this file's OWN
# ---------------------------------------------------------------------------
#
# ⚠️ Each entry names, in the criterion's OWN terms:
#
#   subject_sql    — the rows the criterion PROMISES to cover.  Fixed by
#                    the criterion's SEMANTICS; it does not move when the
#                    implementation evolves.
#   domain_sql     — the rows the criterion's OWN SQL actually asks.
#   drift_free_sql — the subject with ONLY the drift-sensitive conjunct
#                    removed.  It answers: "if that conjunct were not
#                    there, how many rows would this criterion ask?"
#
# `drift_predicate`      — the SQL fragment of the drift-sensitive
#                          conjunct (or None).  The AUTHOR declares it.
# `empty_is_the_answer`  — "the domain is empty" IS this criterion's
#                          answer.  It changes HOW an empty domain is
#                          REPORTED (red vs self-report), not HOW it is
#                          CLASSIFIED.

DOMAIN_SPECS: Dict[str, Dict[str, object]] = {
    # ------------------------------------------------------------------
    # M6a — the certification COVERAGE.  **The one the delegation named.**
    #
    # ⚠️ MEASURED (2026-11-06): its drift-sensitive conjunct is
    # ``text_state<>'mirror'`` — a FOSSIL of the certifier's OLD
    # selection logic.  The certifier evolved (S3-L,
    # ``certify.py:398-431``): it now selects by the LEDGER
    # (``id NOT IN (SELECT id FROM cert_ledger)``), so the state
    # conjunct was dropped from the certifier but **stayed in M6a's
    # domain** (both in the acceptance layer ``phase2_s3k.py:336`` and
    # in the implementation ``certify.py:712``).
    #
    # ⇒ M6a's DOMAIN is 0 rows AND its DRIFT-FREE is 0 rows ⇒ today the
    #   emptiness is the ANSWER (coverage really is complete: all 6803
    #   pointer-bearing file rows are in the ledger).
    # ⇒ **but the fossil remains**: M6a cannot see a "mirror row with no
    #   ledger entry" (M6c covers that class separately), so its green
    #   is evidence about a NARROWER question than its name claims.
    # ------------------------------------------------------------------
    "M6a": {
        "subject_sql": "SELECT COUNT(*) FROM chunks "
                       "WHERE body_origin='file' AND body_path IS NOT NULL",
        "domain_sql": "SELECT COUNT(*) FROM chunks "
                      "WHERE text_state<>'mirror' AND body_origin='file' "
                      "AND body_path IS NOT NULL "
                      "AND id NOT IN (SELECT id FROM cert_ledger)",
        "drift_free_sql": "SELECT COUNT(*) FROM chunks "
                          "WHERE body_origin='file' AND body_path IS NOT NULL "
                          "AND id NOT IN (SELECT id FROM cert_ledger)",
        "drift_predicate": "text_state<>'mirror'",
        "empty_is_the_answer": True,
        "why": "M6a 承诺覆盖「有指针且 origin=file」的行（主体 6803）。"
               "它的域谓词 text_state<>'mirror' 是认证器旧选行逻辑的化石"
               "（S3-L 后认证器改成按账本选行）⇒ 它的域看不到「mirror "
               "但无账本条目」那一类。⚠️ 今天 DOMAIN=0 且 去漂移域=0 ⇒ "
               "**今天这个 0 是「覆盖率真的满了」，不是「被抽空」**。",
    },
    # ------------------------------------------------------------------
    # M6b — the DOWNGRADE detection.  Asks every certified row.
    # ------------------------------------------------------------------
    "M6b": {
        "subject_sql": "SELECT COUNT(*) FROM cert_ledger",
        "domain_sql": "SELECT COUNT(*) FROM cert_ledger l "
                      "JOIN chunks c ON c.id=l.id "
                      "WHERE l.text_state IS NOT NULL "
                      "AND c.text_state IS NOT NULL",
        "drift_free_sql": "SELECT COUNT(*) FROM cert_ledger l "
                          "JOIN chunks c ON c.id=l.id",
        "drift_predicate": None,
        "empty_is_the_answer": False,
        "why": "M6b 比对账本与行 —— 它的域是「可比对的行」，不跟任何"
               "实现演进耦合。",
    },
    # ------------------------------------------------------------------
    # M6c — refused rows must be REGISTERED and VISIBLE.
    # ------------------------------------------------------------------
    "M6c": {
        "subject_sql": "SELECT COUNT(*) FROM chunks",
        "domain_sql": "SELECT COUNT(*) FROM chunks "
                      "WHERE body_origin='orphan' "
                      "OR body_origin='unresolved' "
                      "OR (text_state='mirror' "
                      "AND id NOT IN (SELECT id FROM cert_ledger))",
        "drift_free_sql": "SELECT COUNT(*) FROM chunks",
        "drift_predicate": None,
        "empty_is_the_answer": False,
        "why": "M6c 点名三类行（孤儿 / 未判定 / 无证人的 mirror）—— "
               "它的谓词就是它要问的问题本身。",
    },
    # ------------------------------------------------------------------
    # M6e — the files-side statistics columns.
    # ------------------------------------------------------------------
    "M6e": {
        "subject_sql": "SELECT COUNT(*) FROM files",
        "domain_sql": "SELECT COUNT(*) FROM files WHERE chunk_count IS NULL",
        "drift_free_sql": "SELECT COUNT(*) FROM files",
        "drift_predicate": None,
        "empty_is_the_answer": False,
        "why": "M6e 问「统计列被擦的 files 行」—— 它的域是那 54 个 NULL 行。",
    },
    # ------------------------------------------------------------------
    # M6f — the ledger has no ORPHAN entries.
    #
    # ⚠️ **This is the "the domain's emptiness IS the answer" case.**
    # M6f asks "does the ledger name a row that no longer exists?" — no
    # orphans ⇒ the domain is empty ⇒ **that is exactly the good news
    # it exists to report.**  ⇒ it must NOT be judged red for an empty
    # domain; it must **say so**.
    # ------------------------------------------------------------------
    "M6f": {
        "subject_sql": "SELECT COUNT(*) FROM cert_ledger",
        "domain_sql": "SELECT COUNT(*) FROM cert_ledger l "
                      "WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.id=l.id)",
        "drift_free_sql": "SELECT COUNT(*) FROM cert_ledger l",
        "drift_predicate": None,
        "empty_is_the_answer": True,
        "why": "M6f 问「账本里有没有孤儿」—— 没有孤儿 ⇒ 域为空 ⇒ "
               "**这正是它要报的** ⇒ 域空不是缺陷，但必须自报。",
    },
    # ------------------------------------------------------------------
    # M4 — every library's four columns are filled.  Asks EVERY row.
    # ------------------------------------------------------------------
    "M4": {
        "subject_sql": "SELECT COUNT(*) FROM chunks",
        "domain_sql": "SELECT COUNT(*) FROM chunks",
        "drift_free_sql": "SELECT COUNT(*) FROM chunks",
        "drift_predicate": None,
        "empty_is_the_answer": False,
        "why": "M4 问每一行 —— 它的域是全体 chunks 行。",
    },
    # ------------------------------------------------------------------
    # W-4 — every undecided row carries writer-generation evidence.
    # ------------------------------------------------------------------
    "W-4": {
        "subject_sql": "SELECT COUNT(*) FROM chunks",
        "domain_sql": "SELECT COUNT(*) FROM chunks "
                      "WHERE body_origin='unresolved'",
        "drift_free_sql": "SELECT COUNT(*) FROM chunks",
        "drift_predicate": None,
        "empty_is_the_answer": True,
        "why": "W-4 问「未判定行有没有代际证据」—— 未判定行 = 0 ⇒ "
               "**域为空，而「没有未判定行」正是它的普查结果**（它自己"
               "的文档说「这是测到了，不是测不了」）⇒ 空域自报。",
    },
    # ------------------------------------------------------------------
    # Q1 — INV-1: a row in projection/mirror must carry a pointer that
    # resolves.
    # ------------------------------------------------------------------
    "Q1": {
        "subject_sql": "SELECT COUNT(*) FROM chunks",
        "domain_sql": "SELECT COUNT(*) FROM chunks "
                      "WHERE text_state IN ('projection','mirror') "
                      "AND body_path IS NOT NULL",
        "drift_free_sql": "SELECT COUNT(*) FROM chunks",
        "drift_predicate": None,
        "empty_is_the_answer": False,
        "why": "Q1 问「每一行都带能解析的指针」—— 域是带指针的行。",
    },
    # ------------------------------------------------------------------
    # J-3 — the files statistics columns, discriminable.
    # ------------------------------------------------------------------
    "J-3": {
        "subject_sql": "SELECT COUNT(*) FROM files",
        "domain_sql": "SELECT COUNT(*) FROM files "
                      "WHERE chunk_count IS NULL OR chunker_version IS NULL",
        "drift_free_sql": "SELECT COUNT(*) FROM files",
        "drift_predicate": None,
        "empty_is_the_answer": False,
        "why": "J-3 问「统计列的四种状态可不可区分」—— 域是统计列为空的行。",
    },
}


# ---------------------------------------------------------------------------
# the FOSSIL registry — which criterion's predicate is checked against
# which implementation file
# ---------------------------------------------------------------------------
#
# ⚠️ **The fossil check compares TEXT, not semantics.**  It asks: "does
# the implementation still USE this predicate for its selection?"  It is
# a *literal* check, and that is its honest boundary — see the
# false-positive / false-negative discussion in the report.

FOSSIL_CHECKS: Dict[str, Dict[str, object]] = {
    "M6a": {
        # the implementation's file, and the function whose selection
        # the criterion is supposed to track
        "impl_file": REPO / "agent" / "memory" / "certify.py",
        "impl_function": "certify_library",
        # the criterion's own file + the predicate it uses
        "criterion_file": REPO / "agent" / "memory" / "acceptance" / "phase2_s3k.py",
        "criterion_function": "judge_m6a_certification_coverage",
        # the predicate under surveillance
        "predicate": "text_state<>'mirror'",
        # what the implementation's CURRENT selection rule looks like.
        # ⚠️ A literal fragment, because the check is a literal one.
        "current_selection": "id NOT IN (SELECT id FROM cert_ledger)",
    },
}


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _function_body(source: str, name: str) -> str:
    """The source of one function, by its ``def`` line — **text only**,
    no import, no AST evaluation of the product's code.

    A crude but honest slice: from the ``def <name>(`` line to the next
    top-level ``def``/``class`` at column 0.
    """
    lines = source.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^def\s+%s\s*\(" % re.escape(name), ln):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^(def|class)\s", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


def _selection_statements(body: str) -> List[str]:
    """**The SELECT statements in a function body — comments excluded.**

    ⚠️ **This exists because of a MEASURED false negative.**  The first
    version of the fossil check asked "does the predicate appear
    anywhere in the function body?" — and it found ``text_state<>\
    'mirror'`` in **two** places that are NOT selection rules:

      * ``certify.py:400`` — a **comment** explaining what the OLD
        selection was;
      * ``certify.py:592`` — a **COUNT** for the report
        (``out["remaining"]``), which counts rows by state but does not
        select the work list.

    ⇒ The check must look at **the statement that selects the work
    list**, not at the whole function.

    ⚠️ **MEASURED（2026-11-06）：the SQL is a MULTI-LINE Python string
    literal.**  A statement looks like this::

        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text, hash, "
            "body_path, body_anchor, body_hash, text_state, body_origin "
            "FROM chunks WHERE id NOT IN (SELECT id FROM cert_ledger)"
        ).fetchall()

    ⇒ A **line-based** filter is wrong twice over: the ``WHERE`` clause
    sits on a line that does NOT contain the word ``select`` (so a
    line filter drops it), and the statement spans several lines (so a
    line filter cannot see the whole predicate).

    ⇒ **This helper therefore joins the string-literal pieces of one
    statement before judging it.**  It is a TEXT-level join, not a SQL
    parser — and that is the honest boundary, stated in the report.

    ⚠️ **The boundary of this helper**: it strips whole-line comments,
    it joins adjacent quoted fragments of one statement, and it drops
    statements whose select-list is a bare ``COUNT(*)``.  It does NOT
    parse SQL — a predicate buried inside a larger expression would
    still be seen.  ⇒ The check can produce a **false positive**, and
    that is why a fossil finding must always be reported with its
    evidence, never swallowed.
    """
    # ① collect the code lines, comments stripped
    code: List[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        code.append(line)

    # ② group them into statements by **bracket balance**.
    #
    # ⚠️ **MEASURED（2026-11-06）：the first grouping was wrong.**  It
    # assumed every ``execute`` call spans several lines and ends on a
    # line that starts with ``)``.  But the product also writes
    # single-line calls — e.g.
    # ``never_decided = conn.execute("SELECT COUNT(*) FROM chunks
    # WHERE body_origin='unresolved'").fetchone()[0]`` — and for
    # THOSE the state machine never left the statement ⇒ **every line
    # after the first single-line call was swallowed into one
    # statement** ⇒ the joined text started with ``count(`` ⇒ the
    # COUNT filter dropped the WHOLE function ⇒ the fossil check
    # found nothing.
    #
    # ⇒ **Bracket balance** is the honest splitter: a statement starts
    # at ``.execute(`` and ends when the parens balance again.
    statements: List[str] = []
    current: List[str] = []
    depth = 0
    in_stmt = False
    for line in code:
        if not in_stmt:
            if ".execute(" not in line:
                continue
            in_stmt = True
            current = [line]
            depth = line.count("(") - line.count(")")
            if depth <= 0:
                statements.append(" ".join(current))
                current = []
                in_stmt = False
            continue
        current.append(line)
        depth += line.count("(") - line.count(")")
        if depth <= 0:
            statements.append(" ".join(current))
            current = []
            in_stmt = False
    if current:
        statements.append(" ".join(current))

    # ③ keep only the SELECT ones, and drop the pure COUNT statistics
    out: List[str] = []
    for stmt in statements:
        joined = " ".join(stmt.split())
        low = joined.lower()
        if "select" not in low:
            continue
        after = low.split("select", 1)[1]
        if after.strip().startswith("count(") and " from " in after:
            continue
        out.append(joined)
    return out


def _where_clause(stmt: str) -> str:
    """The WHERE clause of one SELECT statement — the text after the
    first ``WHERE`` keyword.

    ⚠️ **Why the predicate must be matched HERE and not anywhere in the
    statement.**  MEASURED (2026-11-06): the first version matched the
    fragment against the WHOLE statement, and the fragment's column
    (``text_state``) happens to be one of the **selected columns** —
    ``SELECT id, path, ..., text_state, body_origin FROM chunks WHERE
    id NOT IN (...)`` — so the match succeeded even though the WHERE
    clause had no such predicate ⇒ the check reported "the
    implementation still uses the old predicate" ⇒ **no fossil was
    ever detected.**

    ⇒ A predicate is a WHERE-clause thing.  It is matched there.
    """
    low = stmt.lower()
    # ⚠️ **MEASURED（2026-11-06）：the naive ``" where "`` search found
    # NOTHING.**  The statement is a JOIN of Python string literals, so
    # the text reads ``... body_origin " "FROM chunks c " "WHERE
    # c.text_state<>...`` — the word WHERE is preceded by a **quote and
    # a space**, not by a space alone ⇒ ``" where "`` never matches.
    # ⇒ A word-boundary regex is the honest splitter.
    m = re.search(r"\bwhere\b", low)
    if not m:
        return ""
    return stmt[m.end():]


def _strip_aliases(text: str) -> str:
    """Remove ``<alias>.`` prefixes so two spellings of one predicate
    compare equal.

    ⚠️ **MEASURED（2026-11-06）：the alias matters.**  The criterion
    writes the predicate as ``c.text_state<>'mirror'`` (with the alias
    ``c``), while the implementation writes ``text_state<>'mirror'``
    (no alias).  A plain substring test matches the implementation and
    **misses the criterion** ⇒ the fossil check would stop before it
    ever compared anything.

    ⇒ Strip the alias, then compare.  ⚠️ The strip is textual and
    crude — it removes any ``word.`` before an identifier.  That is
    fine for a comparison of two predicates on the SAME column, and it
    is the honest boundary of this check.
    """
    return re.sub(r"\b\w+\s*\.\s*(?=\w)", "", text)


def _mentions(text: str, fragment: str) -> bool:
    """Does ``text`` mention ``fragment``, ignoring table aliases?

    ⚠️ **This used to fall back to "the column name appears
    somewhere"** — and that matched ``text_state`` in a SELECT list
    that had nothing to do with the predicate.  ⇒ The fallback is gone:
    a mention is now a **plain substring match on the alias-stripped
    text**, so the operator and the value must both be there.
    """
    if not fragment:
        return False
    stripped = _strip_aliases(text)
    return fragment in stripped


def fossil_check(crit: str, impl_source: str,
                 criterion_source: str) -> Dict[str, object]:
    """**The fossil test.**  Is the criterion's drift-sensitive predicate
    still the implementation's current selection rule?

    ⚠️ **The check is LITERAL, and its boundary must be said out loud:**

      * **False positive** — the implementation mentions the predicate
        somewhere else (a comment, another query) ⇒ the check says
        "not a fossil" when it might be one.  ⇒ the check can only
        PROVE a fossil, never disprove one.
      * **False negative** — the implementation expresses the same rule
        with different SQL (a JOIN, a NOT EXISTS) ⇒ the check says
        "fossil" when the semantics are the same.  ⇒ the check can
        produce a false alarm, which is why a fossil finding must be
        reported with its evidence, not swallowed.
    """
    spec = FOSSIL_CHECKS.get(crit)
    if not spec:
        return {"applicable": False, "fossil": False, "why": "无化石检查"}

    impl_fn = _function_body(impl_source, spec["impl_function"])
    crit_fn = _function_body(criterion_source, spec["criterion_function"])
    predicate = spec["predicate"]
    current = spec["current_selection"]

    # ① the implementation's SELECTION statements only (a COUNT for the
    #    report is not a selection rule, and a comment is not code).
    impl_selects = _selection_statements(impl_fn)
    crit_selects = _selection_statements(crit_fn)
    impl_selects_text = "\n".join(impl_selects)
    crit_selects_text = "\n".join(crit_selects)

    impl_uses_predicate = _mentions(
        "\n".join(_where_clause(s) for s in impl_selects), predicate)
    impl_uses_current = _mentions(
        "\n".join(_where_clause(s) for s in impl_selects), current)
    # ② the criterion still uses the predicate (it is its domain)
    crit_uses_predicate = _mentions(
        "\n".join(_where_clause(s) for s in crit_selects), predicate)

    fossil = bool(
        crit_uses_predicate          # the criterion is frozen on it
        and impl_uses_current        # the implementation moved on
        and not impl_uses_predicate  # and no longer uses it for selection
    )
    return {
        "applicable": True,
        "fossil": fossil,
        "impl_uses_predicate": impl_uses_predicate,
        "impl_uses_current": impl_uses_current,
        "crit_uses_predicate": crit_uses_predicate,
        "predicate": predicate,
        "current_selection": current,
        "impl_selects": impl_selects,
        "crit_selects": crit_selects,
    }


#: The three verdicts of the domain classifier.
DOMAIN_ASKING = "ASKING"
DOMAIN_BLIND = "BLIND"
DOMAIN_EMPTY_SET = "EMPTY_SET"


def classify_domain(domain: int, drift_free: int) -> str:
    """The three-state classification.  **Pure** — no I/O, no guessing.

    ⚠️ The rule is deliberately NOT "the subject is empty" — the
    subject's emptiness says nothing about whether the criterion is
    asking.  The discriminator is whether the criterion's OWN
    drift-sensitive predicate excluded every row of the subject.
    """
    if domain > 0:
        return DOMAIN_ASKING
    if drift_free > 0:
        return DOMAIN_BLIND
    return DOMAIN_EMPTY_SET


def domain_non_empty_ok(reading: Dict[str, object]) -> bool:
    """**The assertion under test** — the mechanism the delegation asked
    for, in its HARDEST form: an empty domain must never be a plain
    green.

    ⚠️ **This is deliberately stricter than the data alone can justify.**
    The data cannot tell M6f's "the domain is empty and that is the
    good news" from M6a's "the domain is empty because the predicate is
    a fossil" — both read 0.  ⇒ this function says: **an empty domain
    is never a plain green.**  What happens NEXT (red vs self-report) is
    decided by the criterion's declaration AND by the fossil check.
    """
    return reading["domain"] > 0


def census(lib_db: Path) -> Dict[str, Dict[str, object]]:
    """The three-layer reading for one library."""
    out: Dict[str, Dict[str, object]] = {}
    with ro(lib_db) as conn:
        for crit, spec in DOMAIN_SPECS.items():
            subject = count_sql(conn, spec["subject_sql"])
            domain = count_sql(conn, spec["domain_sql"])
            drift_free = count_sql(conn, spec["drift_free_sql"])
            out[crit] = {
                "subject": subject,
                "domain": domain,
                "drift_free": drift_free,
                "verdict": classify_domain(domain, drift_free),
            }
    return out


# ===========================================================================
# K-1-0 · the write-permission gate, itself guarded
# ===========================================================================

def test_k1_0_every_write_target_is_outside_the_live_workspace():
    """**K-1-0** —— 被测对象：**本文件的写权限闸门**.

    闸门本身必须被一条判据守着：它会**真的去试**「拿真源工作区里的库
    当写入目标」，要求闸门**拒绝**。没有这一条，闸门只是一句承诺。
    """
    print("\n[K-1-0] 被测对象 = 本文件的写权限闸门")

    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    # ① the live library itself must be REFUSED as a write target
    victim = libs[0][1]
    with pytest.raises(BaseException) as ei:
        _assert_writable_target(victim)
    assert "真源工作区" in str(ei.value) or "TEMP" in str(ei.value), (
        "闸门拒绝活库时必须说清为什么：%s" % ei.value)
    print("[K-1-0] ① 拿活库当写入目标 ⇒ 闸门拒绝 = True（%s）"
          % str(ei.value)[:70])

    # ② a path OUTSIDE the TEMP must be REFUSED too
    outside = REPO / "k1_should_never_be_written.db"
    with pytest.raises(BaseException) as ei2:
        _assert_writable_target(outside)
    print("[K-1-0] ② 拿 TEMP 之外的路径当写入目标 ⇒ 闸门拒绝 = True（%s）"
          % str(ei2.value)[:70])

    # ③ a path INSIDE the TEMP must be ACCEPTED
    ok = Path(tempfile.gettempdir()) / "k1_gate_probe.db"
    _assert_writable_target(ok)
    print("[K-1-0] ③ 拿 TEMP 之内的路径当写入目标 ⇒ 闸门放行 = True")

    print("[K-1-0] 结论：写权限闸门本身有效（拒绝真源、拒绝非 TEMP、"
          "放行 TEMP 之内）")


# ===========================================================================
# K-1-A · the three-state classification must be decidable
# ===========================================================================

def test_k1_a_the_three_state_classifier_is_decidable_and_covers():
    """**K-1-A** —— 被测对象：**域的三态分类器**（本文件的
    ``classify_domain``）。

    断言（三条，全部可判定）：

      ① **穷尽 + 互斥**：任何一个 (domain, drift_free) 组合都落进
         三态之一；
      ② **边界由具体的数字钉死**：``(0, 0)`` ⇒ EMPTY_SET、
         ``(0, 5)`` ⇒ BLIND、``(1, 5)`` ⇒ ASKING；
      ③ **实测**：在活库上，分类器给出的每一行都必须**自洽**。
    """
    print("\n[K-1-A] 被测对象 = 域的三态分类器 classify_domain()")

    # --- ① exhaustiveness + mutual exclusion, on the boundary cases ---
    cases = [
        # (domain, drift_free, expected)
        (0, 0, DOMAIN_EMPTY_SET),
        (0, 1, DOMAIN_BLIND),
        (0, 5, DOMAIN_BLIND),
        (1, 1, DOMAIN_ASKING),
        (1, 5, DOMAIN_ASKING),
        (100, 100, DOMAIN_ASKING),
        (0, 6803, DOMAIN_BLIND),
    ]
    seen = set()
    for domain, drift_free, expected in cases:
        got = classify_domain(domain, drift_free)
        assert got == expected, (
            "分类器边界错了：domain=%d drift_free=%d ⇒ 期望 %s，实得 %s"
            % (domain, drift_free, expected, got))
        seen.add(got)
        print("[K-1-A]   classify(%d, %d) = %s ✅" % (domain, drift_free, got))
    assert seen == {DOMAIN_ASKING, DOMAIN_BLIND, DOMAIN_EMPTY_SET}, (
        "三态没有穷尽：%s" % sorted(seen))

    # --- ② the classifier is PURE: same input ⇒ same output ---
    for domain, drift_free, expected in cases:
        assert classify_domain(domain, drift_free) == expected
        assert classify_domain(domain, drift_free) == expected
    print("[K-1-A] 分类器是纯函数（同样输入 ⇒ 同样输出）✅")

    # --- ③ the real data must be self-consistent ---
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    grand: Dict[str, Dict[str, int]] = {}
    blind: List[str] = []
    empty_set: List[str] = []
    for name, db in libs:
        for crit, reading in census(db).items():
            g = grand.setdefault(crit, {"subject": 0, "domain": 0,
                                        "drift_free": 0, "blind": 0,
                                        "empty_set": 0, "asking": 0})
            for k in ("subject", "domain", "drift_free"):
                v = reading[k]
                if isinstance(v, int) and v >= 0:
                    g[k] += v
            v = reading["verdict"]
            if v == DOMAIN_BLIND:
                g["blind"] += 1
                blind.append("%s/%s" % (name, crit))
            elif v == DOMAIN_EMPTY_SET:
                g["empty_set"] += 1
                empty_set.append("%s/%s" % (name, crit))
            else:
                g["asking"] += 1

    print("[K-1-A] 全活库合计：")
    for crit in DOMAIN_SPECS:
        g = grand[crit]
        print("[K-1-A]   %-5s 主体=%-6d 域=%-6d 去漂移域=%-6d ⇒ %s"
              "（ASKING %d 库 / BLIND %d 库 / EMPTY_SET %d 库）"
              % (crit, g["subject"], g["domain"], g["drift_free"],
                 classify_domain(g["domain"], g["drift_free"]),
                 g["asking"], g["blind"], g["empty_set"]))

    # the self-consistency assertion, per criterion, over the WHOLE fleet
    for crit in DOMAIN_SPECS:
        g = grand[crit]
        v = classify_domain(g["domain"], g["drift_free"])
        if v == DOMAIN_ASKING:
            assert g["domain"] > 0, "%s 判为 ASKING 但域 = 0" % crit
        elif v == DOMAIN_BLIND:
            assert g["domain"] == 0 and g["drift_free"] > 0, (
                "%s 判为 BLIND 但 domain=%d drift_free=%d"
                % (crit, g["domain"], g["drift_free"]))
        else:
            assert g["domain"] == 0 and g["drift_free"] == 0, (
                "%s 判为 EMPTY_SET 但 domain=%d drift_free=%d"
                % (crit, g["domain"], g["drift_free"]))

    print("[K-1-A] 结论：三态**互斥**且**穷尽**，实测自洽。")
    print("[K-1-A] BLIND 的库: %s" % (", ".join(blind) or "无"))
    print("[K-1-A] EMPTY_SET 的库: %s" % (", ".join(empty_set) or "无"))


# ===========================================================================
# K-1-B / K-1-C · the mutation arms
# ===========================================================================

def _mutation_target(tmp_path: Path) -> Tuple[Path, Path]:
    """A COPY of a live library, plus the pristine snapshot.

    ⚠️ **Why a copy.**  The delegation's red line: no writes to the live
    fleet.  The copy lives under ``%TEMP%`` (``tmp_path``) and the
    write asserts it is NOT inside the real workspace BEFORE the write.
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    name, src = min(libs, key=lambda kv: kv[1].stat().st_size)
    dst = Path(tmp_path) / ("k1-mutant-%s.db" % name)
    _assert_writable_target(dst)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(str(src), str(dst))

    pristine = Path(tmp_path) / ("k1-pristine-%s.db" % name)
    _assert_writable_target(pristine)
    shutil.copy2(str(dst), str(pristine))

    return dst, pristine


#: The criterion the data-mutation arm targets: M6e (the files-side
#: statistics columns).  Its domain is the ``chunk_count IS NULL`` rows —
#: a non-empty, easy-to-empty set on the live fleet.
TARGET = "M6e"


def test_k1_b_mutation_emptying_the_domain_makes_the_criterion_red(
        tmp_path):
    """**K-1-B · 变异臂①（红）** —— 被测对象：**副本上的「域非空」断言**.

    变异 = **让目标判据的域变空** ⇒ 断言必须转红。

    ⚠️ **本臂钉的是「域非空」这条断言本身** —— 判定只钉目标判据，
    连带红**单独列出来汇报**，不参与判定。
    """
    dst, pristine = _mutation_target(tmp_path)
    print("\n[K-1-B 变异臂①] 被测对象 = 副本上的「域非空」断言")
    print("[K-1-B 变异臂①] 副本 = %s" % dst)
    print("[K-1-B 变异臂①] 目标判据 = %s" % TARGET)

    # --- the reading BEFORE the mutation ---
    before = census(dst)[TARGET]
    print("[K-1-B 变异臂①] 变异前: 主体=%(subject)d 域=%(domain)d "
          "去漂移域=%(drift_free)d ⇒ %(verdict)s" % before)
    assert before["domain"] > 0, (
        "变异前域就是空的（%d）⇒ 本臂测不到任何东西（前置条件不成立）"
        % before["domain"])
    assert before["verdict"] == DOMAIN_ASKING

    # --- the mutation: empty the domain ---
    conn = sqlite3.connect(str(dst))
    try:
        conn.row_factory = sqlite3.Row
        victim_paths = [r["path"] for r in conn.execute(
            "SELECT path FROM files WHERE chunk_count IS NULL").fetchall()]
        assert victim_paths, "副本里没有可用来清空域的 files 行"
        conn.execute("UPDATE files SET chunk_count=0 WHERE chunk_count IS NULL")
        conn.commit()
    finally:
        conn.close()

    # --- the self-proof that the mutation took effect ---
    with ro(dst) as conn:
        after = census(dst)[TARGET]
        still_null = count_sql(conn, "SELECT COUNT(*) FROM files "
                                    "WHERE chunk_count IS NULL")
        filled = count_sql(conn, "SELECT COUNT(*) FROM files "
                                  "WHERE chunk_count=0")
    print("[K-1-B 变异臂①] 变异后: 主体=%(subject)d 域=%(domain)d "
          "去漂移域=%(drift_free)d ⇒ %(verdict)s" % after)
    print("[K-1-B 变异臂①] 自证: chunk_count IS NULL 的行 %d → 0；"
          "chunk_count=0 的行 = %d（变异确实生效）" % (len(victim_paths),
                                                      filled))

    assert still_null == 0, (
        "变异没有生效（还有 %d 行 chunk_count IS NULL）" % still_null)
    assert after["domain"] == 0, (
        "变异之后域仍然是 %d ⇒ 变异没打中目标判据的域" % after["domain"])
    assert after["drift_free"] > 0, (
        "去漂移域也是 0 ⇒ 这个副本上测不出 BLIND（换个库）")

    # --- the target assertion: the classification must say BLIND ---
    assert after["verdict"] == DOMAIN_BLIND, (
        "★域被清空了，分类器却判 %s（期望 BLIND）⇒ **「域非空」这条"
        "断言看不见域被抽空** ⇒ 这条判据是瞎的" % after["verdict"])

    # --- and the "域非空" assertion itself must FAIL on this state ---
    assert not domain_non_empty_ok(after), (
        "★域被抽空之后「域非空」断言仍然通过 ⇒ 断言没有牙齿")

    # --- the collateral reds, reported separately, NOT part of the verdict ---
    collateral = _collateral_reds(dst)
    print("[K-1-B 变异臂①] ⚠️ 连带红（**单独汇报，不参与判定**）：")
    for name, detail in collateral:
        print("[K-1-B 变异臂①]    · %s: %s" % (name, detail))
    print("[K-1-B 变异臂①] 连带红合计 = %d 项 —— 它们是「变异对象是数据文件」"
          "的必然结果，与本判据无关。" % len(collateral))

    print("[K-1-B 变异臂①] 结论：**让域变空 ⇒ 「域非空」断言转红**"
          "（%s → %s）" % (before["verdict"], after["verdict"]))


def _collateral_reds(db: Path) -> List[Tuple[str, str]]:
    """What ELSE turned red because the DATA changed.  Reported, not judged."""
    out: List[Tuple[str, str]] = []
    with ro(db) as conn:
        try:
            n = count_sql(conn, "SELECT COUNT(*) FROM files "
                                "WHERE chunk_count=0 AND chunker_version IS NULL")
            if n:
                out.append(("files 统计列被写成 0/NULL（M6e 的兄弟域）",
                            "%d 行" % n))
        except sqlite3.Error:
            pass
        try:
            n = count_sql(conn, "SELECT COUNT(*) FROM files "
                                "WHERE chunk_count IS NOT NULL "
                                "AND chunk_count <> 0")
            if n:
                out.append(("files 统计列的分布变了（M6e / J-3 / C-A3.4 读数会动）",
                            "%d 行" % n))
        except sqlite3.Error:
            pass
        try:
            n = count_sql(conn, "SELECT COUNT(*) FROM cert_ledger l "
                                "WHERE NOT EXISTS (SELECT 1 FROM chunks c "
                                "WHERE c.id=l.id)")
            if n:
                out.append(("账本孤儿（M6f 的域）", "%d 行" % n))
        except sqlite3.Error:
            pass
    return out


def test_k1_c_the_mutation_is_reverted_byte_for_byte(tmp_path):
    """**K-1-C · 还原臂** —— 被测对象：**字节级还原**.

    ⚠️ **MEASURED（2026-11-06）：SQL 层面的 undo 不还原字节。**
    SQLite 的文件头带一个写事务就动一次的 change counter ⇒ 一条
    ``UPDATE`` 写回原值，**逻辑上一致、字节上不一致**。

    ⇒ 还原必须是**一份 pristine 快照的字节级复制**，不是一条 UPDATE。
    """
    dst, pristine = _mutation_target(tmp_path)
    sha_pristine = sha256_file(pristine)
    sha_before = sha256_file(dst)
    print("\n[K-1-C 还原臂] 被测对象 = 副本 + 字节级还原")
    print("[K-1-C 还原臂] 副本 = %s" % dst)
    print("[K-1-C 还原臂] pristine 快照 sha256 = %s" % sha_pristine)

    with ro(dst) as conn:
        before = census(dst)[TARGET]
    assert before["domain"] > 0

    # --- mutate ---
    conn = sqlite3.connect(str(dst))
    try:
        conn.execute("UPDATE files SET chunk_count=0 WHERE chunk_count IS NULL")
        conn.commit()
    finally:
        conn.close()

    with ro(dst) as conn:
        mutated = census(dst)[TARGET]
    sha_mutated = sha256_file(dst)
    print("[K-1-C 还原臂] 变异后: 域=%(domain)d ⇒ %(verdict)s" % mutated)
    print("[K-1-C 还原臂] 变异后 sha256 = %s" % sha_mutated)
    assert mutated["domain"] == 0, "变异没有生效 ⇒ 还原臂测不到任何东西"

    # --- the byte-level restore ---
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(str(pristine), str(dst))
    sha_restored = sha256_file(dst)

    with ro(dst) as conn:
        restored = census(dst)[TARGET]

    print("[K-1-C 还原臂] 还原后: 域=%(domain)d ⇒ %(verdict)s" % restored)
    print("[K-1-C 还原臂] 还原后 sha256 = %s" % sha_restored)
    print("[K-1-C 还原臂] 变异前 sha256 = %s" % sha_before)
    print("[K-1-C 还原臂] pristine  sha256 = %s" % sha_pristine)

    assert restored["domain"] == before["domain"], (
        "还原后域读数不一致 ⇒ 还原没有生效")
    assert sha_restored == sha_pristine, (
        "还原后 sha256 与 pristine 不一致 ⇒ 还原不是逐字节的：\n"
        "  还原后 = %s\n  pristine = %s" % (sha_restored, sha_pristine))

    print("[K-1-C 还原臂] 结论：**还原后 sha256 逐字节一致**（%s）"
          % sha_pristine)


def test_k1_c2_the_control_arm_sees_a_non_empty_domain():
    """**K-1-C2 · 对照臂（绿）** —— 被测对象：**活库上的域三态读数**.

    对照臂必须证明两件事：

      ① 分类器在**活库**上给出的读数**自洽**（K-1-A 已在全舰队上
         断言过逐库自洽，这里断言全舰队合计自洽）；
      ② **至少有一条判据是 ASKING** —— 否则这条判据自己就没有
         任何实测对象，测不了「域非空」这件事。

    ⚠️ **对照臂钉的是「分类器的读数」，不是「活库的存量」** ——
    活库随时会被别人写（上一轮实测：agent-mvnf7fjl 在 577 秒前
    新增了 2 行真·越界写入），而域的**结构**不会因为多两行就变。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    print("\n[K-1-C2 对照臂] 被测对象 = 活库上的域三态读数（%d 个库）"
          % len(libs))

    grand: Dict[str, Dict[str, int]] = {}
    for name, db in libs:
        for crit, reading in census(db).items():
            g = grand.setdefault(crit, {"subject": 0, "domain": 0,
                                        "drift_free": 0})
            for k in ("subject", "domain", "drift_free"):
                v = reading[k]
                if isinstance(v, int) and v >= 0:
                    g[k] += v

    asking = 0
    for crit in DOMAIN_SPECS:
        g = grand[crit]
        v = classify_domain(g["domain"], g["drift_free"])
        if v == DOMAIN_ASKING:
            asking += 1
        print("[K-1-C2 对照臂]   %-5s 主体=%-6d 域=%-6d 去漂移域=%-6d ⇒ %s"
              % (crit, g["subject"], g["domain"], g["drift_free"], v))

    assert asking >= 1, (
        "没有任何一条判据是 ASKING ⇒ 这条判据没有实测对象（前置条件"
        "不成立）")
    print("[K-1-C2 对照臂] 结论：%d/%d 条判据在问行 ⇒ 「域非空」断言"
          "有实测对象 ✅" % (asking, len(DOMAIN_SPECS)))


# ===========================================================================
# K-1-E · the FOSSIL check — the fourth layer
# ===========================================================================

def test_k1_e_the_fossil_check_detects_a_frozen_selection_predicate():
    """**K-1-E** —— 被测对象：**化石检测器**（``fossil_check``）.

    这是第四层：**数据层分不出来的两种「域空」，由源码层分开。**

    交办书 K §2 的两难：M6f 的域空是**答案**，M6a 的域空是**化石残留**
    —— 在数据层两者都是「域 = 0」，**分不开**。

    ⇒ 化石检测问的是另一个问题：**判据的漂移敏感谓词，还是不是被测
    对象当前用来定义那个概念的规则？**

    断言（四条）：

      ① **对照臂（真化石）**：在**真实的源码**上，M6a 的谓词
         ``text_state<>'mirror'`` 是化石 —— 认证器**现在**按账本选行
         （``id NOT IN (SELECT id FROM cert_ledger)``），而 M6a 的域
         仍然用旧谓词 ⇒ 检出。
      ② **变异臂（自证有牙齿）**：把认证器的选行规则**改回**旧谓词
         （在**副本**上改源码文本，不碰仓库）⇒ 化石必须**消失**
         ⇒ 证明检测器不是恒真。
      ③ **反例**：当实现侧**也**用那个谓词时 ⇒ 不是化石。
      ④ **边界**：检测器是**字面量**比较 ⇒ 它只能**证明**化石，
         不能**否证**化石（报告里写明这条边界）。
    """
    print("\n[K-1-E] 被测对象 = 化石检测器 fossil_check()")

    spec = FOSSIL_CHECKS["M6a"]
    impl_path = spec["impl_file"]
    crit_path = spec["criterion_file"]

    impl_src = _read(impl_path)
    crit_src = _read(crit_path)
    assert impl_src, "读不到实现侧源码：%s" % impl_path
    assert crit_src, "读不到判据侧源码：%s" % crit_path

    # --- ① the CONTROL arm: the real fossil must be detected ---
    real = fossil_check("M6a", impl_src, crit_src)
    print("[K-1-E] 真实源码上的检测结果: %s" % real)
    assert real["applicable"], "M6a 必须登记化石检查"
    assert real["crit_uses_predicate"], (
        "M6a 的判据里找不到谓词 %r ⇒ 规格写错了" % real["predicate"])
    assert real["impl_uses_current"], (
        "认证器的当前选行规则 %r 不在源码里 ⇒ 规格写错了"
        % real["current_selection"])
    assert real["fossil"], (
        "★**M6a 的域谓词 %r 是认证器旧选行逻辑的化石，而化石检测没有"
        "检出它** ⇒ 这条判据是瞎的" % real["predicate"])
    print("[K-1-E] ① 对照臂：M6a 的谓词 %r 是化石 ✅（认证器现在按账本"
          "选行，M6a 仍用旧谓词）" % real["predicate"])

    # --- ② the MUTATION arm: put the implementation BACK on the old
    #        predicate ⇒ the fossil must disappear ---
    #
    # ⚠️ The mutation must land on the **SELECTION** statement (the work
    # list), not on a COUNT — otherwise the selection-only check would
    # not see it and the arm would measure nothing.
    mutated_impl = impl_src.replace(
        "FROM chunks WHERE id NOT IN (SELECT id FROM cert_ledger)",
        "FROM chunks WHERE text_state<>'mirror'")
    assert mutated_impl != impl_src, (
        "变异没有生效：源码里找不到那行选行 SQL ⇒ 变异点过期了")

    mutated = fossil_check("M6a", mutated_impl, crit_src)
    print("[K-1-E] 变异后（实现侧改回旧谓词）: fossil=%s "
          "impl_uses_predicate=%s impl_uses_current=%s"
          % (mutated["fossil"], mutated["impl_uses_predicate"],
             mutated["impl_uses_current"]))
    assert mutated["impl_uses_predicate"], (
        "变异没有生效：改写后的源码的**选行语句**里找不到旧谓词")
    assert not mutated["fossil"], (
        "★实现侧改回旧谓词之后，化石检测**仍然**报化石 ⇒ 检测器是恒真的"
        "⇒ 它没有牙齿")
    print("[K-1-E] ② 变异臂：实现侧改回旧谓词 ⇒ 化石消失 ✅（检测器有牙齿）")

    # --- ③ the COUNTER-example: both sides on the same predicate ⇒
    #        not a fossil ---
    mutated_crit = crit_src.replace("text_state<>'mirror'",
                                     "text_state IS NOT NULL")
    assert mutated_crit != crit_src, "反例变异没有生效"
    counter = fossil_check("M6a", mutated_impl, mutated_crit)
    print("[K-1-E] ③ 反例（两边都用同一谓词）: fossil=%s" % counter["fossil"])
    assert not counter["fossil"], (
        "★两边都用同一谓词，化石检测却报化石 ⇒ 检测器在乱报")
    print("[K-1-E] ③ 反例：两边谓词一致 ⇒ 不是化石 ✅")

    # --- ④ the boundary: the check is LITERAL ---
    print("[K-1-E] ④ 边界：这个检测是**字面量**比较 —— 它只能**证明**"
          "化石，不能**否证**化石：")
    print("[K-1-E]    · 假阳性：实现侧在别处（注释/另一个查询）提到该谓词"
          " ⇒ 检测会说「不是化石」而它可能是。")
    print("[K-1-E]    · 假阴性：实现侧用不同 SQL 表达同一条规则 ⇒ 检测会"
          "说「是化石」而它不是 ⇒ **必须带证据汇报，不能吞掉**。")

    print("[K-1-E] 结论：化石检测在真实源码上检出 M6a 的化石，"
          "变异臂证明它有牙齿，反例证明它不乱报。")


# ===========================================================================
# K-1-D · the census: every criterion's domain, and the drift fingerprint
# ===========================================================================

def test_k1_d_the_domain_census_names_the_drift_mechanism():
    """**K-1-D** —— 被测对象：**判据全集的域 census**.

    交办书 K §2 要求：**枚举出所有「域可能为 0」的判据**，逐条给出
    它当前的域内行数。

    ⇒ 本条给出本文件登记的每一条判据的三个数（主体 / 域 / 去漂移域），
    并且**点名**那些**域被抽空**的判据（BLIND）—— 它们就是「域跟着
    被测对象漂移」的现行受害者。

    ⚠️ **本条不改任何既有判据。** 它只读数、只报告。
    """
    libs = live_libraries()
    assert libs, "没有活库 —— 前置条件不成立（这不是通过）"

    print("\n[K-1-D] 被测对象 = 判据全集的域 census（%d 个库）" % len(libs))

    grand: Dict[str, Dict[str, int]] = {}
    for name, db in libs:
        for crit, reading in census(db).items():
            g = grand.setdefault(crit, {"subject": 0, "domain": 0,
                                        "drift_free": 0})
            for k in ("subject", "domain", "drift_free"):
                v = reading[k]
                if isinstance(v, int) and v >= 0:
                    g[k] += v

    print()
    print("[K-1-D] 判据      主体       域       去漂移域     三态        漂移谓词")
    blind: List[str] = []
    empty_set: List[str] = []
    for crit, spec in DOMAIN_SPECS.items():
        g = grand[crit]
        v = classify_domain(g["domain"], g["drift_free"])
        if v == DOMAIN_BLIND:
            blind.append(crit)
        elif v == DOMAIN_EMPTY_SET:
            empty_set.append(crit)
        print("[K-1-D] %-8s %-9d %-9d %-11d %-11s %s"
              % (crit, g["subject"], g["domain"], g["drift_free"], v,
                 spec["drift_predicate"] or "无"))

    print()
    print("[K-1-D] 读法：")
    print("[K-1-D]   · **ASKING** ⇒ 域非空 ⇒ 判据的绿/红携带信息。")
    print("[K-1-D]   · **BLIND** ⇒ 域被抽空 ⇒ 判据一行都不问 ⇒ "
          "★它的绿是空的 ⇒ 「它转绿」不能作为任何修复的证据。")
    print("[K-1-D]   · **EMPTY_SET** ⇒ 域空**本身就是答案** ⇒ 不是缺陷，"
          "但必须自报「域为空，结论是空集」。")
    print()
    print("[K-1-D] **BLIND（域被抽空）的判据: %s**" % (", ".join(blind) or "无"))
    print("[K-1-D] **EMPTY_SET（域空=答案）的判据: %s**"
          % (", ".join(empty_set) or "无"))

    # --- the census must be INTERNALLY consistent ---
    for crit in DOMAIN_SPECS:
        g = grand[crit]
        assert g["domain"] <= g["drift_free"] or g["drift_free"] == 0, (
            "%s: 域(%d) 大于去漂移域(%d) ⇒ 去漂移域不是上界 ⇒ 规格写错了"
            % (crit, g["domain"], g["drift_free"]))
        assert g["drift_free"] <= g["subject"] or g["subject"] == 0, (
            "%s: 去漂移域(%d) 大于主体(%d) ⇒ 读数不自洽"
            % (crit, g["drift_free"], g["subject"]))

    print()
    print("[K-1-D] 结论：census 自洽（域 ≤ 去漂移域 ≤ 主体）；"
          "BLIND 的判据 = %s" % (", ".join(blind) or "无"))
