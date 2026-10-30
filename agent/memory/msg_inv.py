"""The messages-domain criteria: MSG-INV-1..7 (P1 阶段三 M).

**These are the IMPLEMENTATION-SIDE criteria.**  They live here, in
the implementation, because each one asks a question about the
implementation's own artefacts — the JSONL chain, the anchor, the
compensation chain, the DB projection — and the answer must be the
SAME answer wherever it is asked.  The acceptance layer (the test
engineer's territory) calls these functions and reports the verdicts
per library; it does not re-implement them.

**The three states, never two.**  A criterion that cannot be measured
is ``NOTRUN``, not ``GREEN``.  A criterion that measured something and
found it bad is ``FAIL``.  A criterion that measured something and
found it good is ``GREEN``.  The difference between the first two is
the difference between "there is no problem" and "nobody looked".

**Every criterion prints what it LOOKED at.**  A criterion that reports
green without saying how many files and how many lines it examined is
indistinguishable from a criterion that examined nothing.

**The four criteria-about-criteria (裁决 §7).**

  * A — on a library that has NOT been externalised, MSG-INV-1..7 must
    be ``NOTRUN``, not ``GREEN``.  A green on an un-externalised
    library means the criterion never touches the externalised path.
  * B — a missing ``messages_export/`` or a missing baseline is
    ``NOTRUN``.
  * C — every fingerprint prints the files and lines it covered.
  * D — a session whose ``extras`` were updated after the
    externalisation must still pass MSG-INV-1..5.

**The three-way anchor comparison (裁决 §8.2).**  The anchor's claim is
checked against the JSONL's SELF-REPORTED hash AND against the
RE-COMPUTED hash.  Checking only the self-reported one is a false
green: an attacker who rewrites the last line and its claim passes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent.memory import msg_chain

GREEN = "GREEN"
RED = "RED"
NOTRUN = "NOTRUN"

# The reason codes — one per failure mode, so the operator knows which
# repair each finding needs.
CODE_CHAIN_BROKEN = "msg-chain-broken"
CODE_SHRUNK = "msg-shrunk"
CODE_HISTORY_REWRITTEN = "msg-history-rewritten"
CODE_ANCHOR_MISSING = "msg-head-anchor-missing"
CODE_ANCHOR_MISMATCH = "msg-anchor-mismatch"
CODE_ANCHOR_SEQ_MISMATCH = "msg-anchor-seq-mismatch"
CODE_ANCHOR_CHAIN_BROKEN = "msg-anchor-chain-broken"
CODE_COMPENSATION_COUNT = "msg-compensation-count-mismatch"
CODE_COMPENSATION_IN_MAIN = "msg-compensation-in-main-chain"
CODE_CONTENT_NULL_INLINE = "msg-content-null-inline"
CODE_PENDING_ORPHANED = "msg-pending-orphaned"
CODE_READPATH_DIVERGED = "msg-readpath-diverged"

REASON_TEXT = {
    CODE_CHAIN_BROKEN: "messages 主链哈希链断裂",
    CODE_SHRUNK: "messages 主链行数减少（物理删除）",
    CODE_HISTORY_REWRITTEN: "messages 主链历史行被改写",
    CODE_ANCHOR_MISSING: "messages 链头锚缺失",
    CODE_ANCHOR_MISMATCH: "messages 链头锚与主链末行哈希不符",
    CODE_ANCHOR_SEQ_MISMATCH: "messages 链头锚与主链末行 seq 不符",
    CODE_ANCHOR_CHAIN_BROKEN: "messages 链头锚自身成链断裂",
    CODE_COMPENSATION_COUNT: "messages 补偿行与 DB 计数不一致",
    CODE_COMPENSATION_IN_MAIN: "messages 主链混入了补偿行",
    CODE_CONTENT_NULL_INLINE: "messages 出现 content IS NULL 且 state=inline 的非法状态",
    CODE_PENDING_ORPHANED: "messages 存在 pending 行且真源无对应行",
    CODE_READPATH_DIVERGED: "messages 外置前后读路径结果不一致",
}


def _v(code: str, detail: str, path: str = "", severity: str = "high",
       **fields: Any) -> Dict[str, Any]:
    return {"reason": REASON_TEXT.get(code, code), "code": code,
            "path": path, "detail": detail, "severity": severity, **fields}


def _result(inv: str, verdict: str, fingerprint: str,
            violations: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"inv": inv, "verdict": verdict, "fingerprint": fingerprint,
            "violations": violations}


# ---------------------------------------------------------------------------
# The shared preconditions
# ---------------------------------------------------------------------------

def _export_root(workspace: Union[str, Path]) -> Path:
    return msg_chain.export_root(workspace)


def _main_chain_files(workspace: Union[str, Path]) -> List[Path]:
    """Every registered JSONL — the files under ``messages_export/``.

    The ``.audit`` directory is EXCLUDED: the anchor, the compensation
    chain and the baseline are not message lines, and counting them as
    such would make every count wrong.
    """
    root = _export_root(workspace)
    if not root.is_dir():
        return []
    out: List[Path] = []
    for p in sorted(root.rglob("*.jsonl")):
        if msg_chain.AUDIT_DIRNAME in p.parts:
            continue
        out.append(p)
    return out


def _compensation_file(workspace: Union[str, Path]) -> Path:
    return msg_chain.compensation_path(workspace)


def _baseline_file(workspace: Union[str, Path]) -> Path:
    return msg_chain.baseline_path(workspace)


#: The public alias — the baseline's location is part of the contract
#: (裁决 C-5: the baseline lives in ``messages_export/.audit/``).
baseline_path = msg_chain.baseline_path


def _precondition(workspace: Union[str, Path]) -> Optional[str]:
    """Why the criteria cannot be measured — or None when they can.

    This is criterion B of the four criteria-about-criteria: a missing
    truth tree is ``NOTRUN``, never ``GREEN``.
    """
    root = _export_root(workspace)
    if not root.is_dir():
        return ("the messages truth tree %s does not exist — this library "
                "has not been externalised" % root)
    files = _main_chain_files(workspace)
    if not files:
        return ("the messages truth tree %s holds no JSONL — nothing has "
                "been externalised" % root)
    return None


# ---------------------------------------------------------------------------
# MSG-INV-1 — the chain is self-consistent
# ---------------------------------------------------------------------------

def check_msg_inv1(workspace: Union[str, Path]) -> Dict[str, Any]:
    """MSG-INV-1: the main chain's hashes and links are intact.

    Checks every line of every registered JSONL: the ``prev_hash``
    linkage, the genesis, and the RE-COMPUTED ``line_hash``.
    """
    why = _precondition(workspace)
    if why:
        return _result("MSG-INV-1", NOTRUN, "files=0 lines=0", [
            _v(CODE_CHAIN_BROKEN, why, severity="mid")])

    files = _main_chain_files(workspace)
    out: List[Dict[str, Any]] = []
    total = 0
    for path in files:
        violations = msg_chain.verify_msg_file(path)
        lines = len(msg_chain.read_msg_lines(path))
        total += lines
        for v in violations:
            out.append(_v(CODE_CHAIN_BROKEN, v, path.as_posix()))
    fp = "files=%d lines=%d" % (len(files), total)
    return _result("MSG-INV-1", RED if out else GREEN, fp, out)


# ---------------------------------------------------------------------------
# MSG-INV-2 — the line count never shrinks
# ---------------------------------------------------------------------------

def check_msg_inv2(workspace: Union[str, Path]) -> Dict[str, Any]:
    """MSG-INV-2: the JSONL's line count never goes DOWN.

    The count is taken from the FILE, never from the DB: the DB is a
    projection and it may be pruned (the delete paths are allowed to
    drop externalised rows), while the JSONL is the truth source and
    may only grow.
    """
    why = _precondition(workspace)
    if why:
        return _result("MSG-INV-2", NOTRUN, "files=0 lines=0", [
            _v(CODE_SHRUNK, why, severity="mid")])

    files = _main_chain_files(workspace)
    total = sum(len(msg_chain.read_msg_lines(p)) for p in files)
    baseline = _read_baseline(workspace)
    out: List[Dict[str, Any]] = []
    fp = "files=%d lines=%d" % (len(files), total)
    if not baseline:
        return _result("MSG-INV-2", NOTRUN, fp + " baseline=missing", [
            _v(CODE_SHRUNK,
               "the baseline %s is missing — the line-count monotonicity "
               "cannot be measured" % _baseline_file(workspace),
               severity="mid")])
    prev = 0
    for entry in baseline:
        prev = max(prev, int(entry.get("total_lines") or 0))
    if total < prev:
        out.append(_v(CODE_SHRUNK,
                      "the JSONL holds %d lines but the baseline recorded %d "
                      "— the truth source was truncated" % (total, prev)))
    return _result("MSG-INV-2", RED if out else GREEN, fp, out)


# ---------------------------------------------------------------------------
# MSG-INV-3 — the history was not rewritten
# ---------------------------------------------------------------------------

def check_msg_inv3(workspace: Union[str, Path]) -> Dict[str, Any]:
    """MSG-INV-3: no historical line was rewritten.

    The baseline stores the RE-COMPUTED hashes, not the file's
    self-reported ones — a self-reported hash is a CLAIM, and a claim
    that moves with the content proves nothing.  This is 裁决 S-4.
    """
    why = _precondition(workspace)
    if why:
        return _result("MSG-INV-3", NOTRUN, "files=0 lines=0", [
            _v(CODE_HISTORY_REWRITTEN, why, severity="mid")])

    files = _main_chain_files(workspace)
    baseline = _read_baseline(workspace)
    total = sum(len(msg_chain.read_msg_lines(p)) for p in files)
    fp = "files=%d lines=%d" % (len(files), total)
    if not baseline:
        return _result("MSG-INV-3", NOTRUN, fp + " baseline=missing", [
            _v(CODE_HISTORY_REWRITTEN,
               "the baseline %s is missing — the content monotonicity "
               "cannot be measured" % _baseline_file(workspace),
               severity="mid")])

    out: List[Dict[str, Any]] = []
    # The baseline's per-file hashes: rel -> [recomputed hashes].
    known: Dict[str, List[str]] = {}
    for entry in baseline:
        for rel, hashes in (entry.get("line_hashes") or {}).items():
            known[rel] = list(hashes)
    root = _export_root(workspace).parent
    for path in files:
        rel = path.relative_to(root).as_posix()
        if rel not in known:
            continue
        lines = msg_chain.read_msg_lines(path)
        now = []
        for line in lines:
            try:
                now.append(msg_chain.msg_line_hash(line))
            except (KeyError, TypeError):
                now.append("")
        was = known[rel]
        for i in range(min(len(was), len(now))):
            if was[i] != now[i]:
                out.append(_v(CODE_HISTORY_REWRITTEN,
                              "%s line %d was rewritten — the baseline "
                              "recorded %s and the file now hashes to %s"
                              % (rel, i + 1, was[i], now[i]), rel))
                break
    return _result("MSG-INV-3", RED if out else GREEN, fp, out)


# ---------------------------------------------------------------------------
# MSG-INV-4 — the chain-head anchor
# ---------------------------------------------------------------------------

def check_msg_inv4(workspace: Union[str, Path]) -> Dict[str, Any]:
    """MSG-INV-4: the anchor describes the JSONL's tail, and is itself a chain.

    Four checks (裁决 C-5 · M-4), plus the THIRD comparison of 裁决 §8.2
    (the anchor against the RE-COMPUTED hash of the last line).
    """
    why = _precondition(workspace)
    if why:
        return _result("MSG-INV-4", NOTRUN, "files=0 lines=0", [
            _v(CODE_ANCHOR_MISSING, why, severity="mid")])

    files = _main_chain_files(workspace)
    total = sum(len(msg_chain.read_msg_lines(p)) for p in files)
    fp = "files=%d lines=%d" % (len(files), total)
    apath = msg_chain.anchor_path(workspace)
    if not apath.is_file():
        return _result("MSG-INV-4", RED, fp, [
            _v(CODE_ANCHOR_MISSING,
               "the anchor %s does not exist — every JSONL must have an "
               "anchor row" % apath, apath.as_posix())])

    out: List[Dict[str, Any]] = []
    root = _export_root(workspace).parent
    anchors = msg_chain.read_msg_lines(apath)
    by_rel = {a.get("rel"): a for a in anchors if a.get("rel")}
    for path in files:
        rel = path.relative_to(root).as_posix()
        a = by_rel.get(rel)
        if a is None:
            out.append(_v(CODE_ANCHOR_MISSING,
                          "%s has no anchor row" % rel, rel))
            continue
        lines = msg_chain.read_msg_lines(path)
        if not lines:
            out.append(_v(CODE_ANCHOR_MISMATCH,
                          "%s has no lines but is anchored" % rel, rel))
            continue
        last = lines[-1]
        if a.get("line_hash") != last.get("line_hash"):
            out.append(_v(CODE_ANCHOR_MISMATCH,
                          "%s: the anchor claims %s and the last line "
                          "reports %s" % (rel, a.get("line_hash"),
                                           last.get("line_hash")), rel))
        if int(a.get("seq") if a.get("seq") is not None else -1) != \
                int(last.get("seq") if last.get("seq") is not None else -2):
            out.append(_v(CODE_ANCHOR_SEQ_MISMATCH,
                          "%s: the anchor claims seq=%s and the last line "
                          "has seq=%s" % (rel, a.get("seq"), last.get("seq")),
                          rel))
        # The THIRD comparison: the RE-COMPUTED hash.
        try:
            recomputed = msg_chain.msg_line_hash(last)
        except (KeyError, TypeError):
            recomputed = ""
        if recomputed and recomputed != a.get("line_hash"):
            out.append(_v(CODE_ANCHOR_MISMATCH,
                          "%s: the last line RE-COMPUTES to %s but the "
                          "anchor claims %s — the anchor is stale or the "
                          "line was rewritten" % (rel, recomputed,
                                                  a.get("line_hash")), rel))
    # The anchor's own chain.
    prev = msg_chain.MSG_GENESIS_PREV_HASH
    for i, a in enumerate(anchors, start=1):
        if a.get("prev_anchor_hash") != prev:
            out.append(_v(CODE_ANCHOR_CHAIN_BROKEN,
                          "the anchor chain breaks at line %d" % i,
                          apath.as_posix()))
        try:
            if a.get("anchor_hash") != msg_chain.anchor_hash(a):
                out.append(_v(CODE_ANCHOR_CHAIN_BROKEN,
                              "the anchor's own hash does not match at line %d"
                              % i, apath.as_posix()))
        except (KeyError, TypeError):
            out.append(_v(CODE_ANCHOR_CHAIN_BROKEN,
                          "the anchor at line %d cannot be hashed" % i,
                          apath.as_posix()))
        prev = a.get("anchor_hash", prev)
    return _result("MSG-INV-4", RED if out else GREEN, fp, out)


# ---------------------------------------------------------------------------
# MSG-INV-5 — the two phases agree
# ---------------------------------------------------------------------------

def check_msg_inv5(workspace: Union[str, Path], conn: sqlite3.Connection,
                    agent_id: str = "") -> Dict[str, Any]:
    """MSG-INV-5: the DB projection and the JSONL agree, in both directions.

    Four expectations (裁决 C-2 v2):

    1. the main chain's MESSAGE lines == the DB's externalised rows;
    2. the main chain holds NO compensation lines (``kind`` must be
       absent from every line);
    3. the compensation file's lines == the DB's compensated rows;
    4. every compensation row's ``target_seq`` names a DB row that is
       externalised.
    """
    why = _precondition(workspace)
    if why:
        return _result("MSG-INV-5", NOTRUN, "files=0 lines=0", [
            _v(CODE_COMPENSATION_COUNT, why, severity="mid")])

    files = _main_chain_files(workspace)
    out: List[Dict[str, Any]] = []
    message_lines = 0
    compensation_in_main = 0
    for path in files:
        for line in msg_chain.read_msg_lines(path):
            if "kind" in line:
                compensation_in_main += 1
            else:
                message_lines += 1
    total_lines = message_lines + compensation_in_main
    fp = "files=%d lines=%d" % (len(files), total_lines)

    if compensation_in_main:
        out.append(_v(CODE_COMPENSATION_IN_MAIN,
                      "%d line(s) in the MAIN chain carry a 'kind' key — the "
                      "compensation chain lives in its own file, and the "
                      "main chain has ONE shape" % compensation_in_main))

    comp_file = _compensation_file(workspace)
    comp_lines = msg_chain.read_msg_lines(comp_file) if comp_file.is_file() else []

    # The DB side.
    try:
        db_external = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content_state = 'external'"
        ).fetchone()[0]
    except sqlite3.Error as e:
        return _result("MSG-INV-5", NOTRUN, fp + " db=unreadable", [
            _v(CODE_COMPENSATION_COUNT,
               "cannot read the messages table: %s" % e, severity="mid")])
    try:
        db_compensated = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE extras_compensated = 1"
        ).fetchone()[0]
    except sqlite3.Error:
        db_compensated = None

    if message_lines != db_external:
        out.append(_v(CODE_COMPENSATION_COUNT,
                      "the main chain holds %d message line(s) but the DB "
                      "has %d externalised row(s)" % (message_lines,
                                                      db_external)))
    if db_compensated is not None and len(comp_lines) != db_compensated:
        out.append(_v(CODE_COMPENSATION_COUNT,
                      "the compensation file holds %d line(s) but the DB has "
                      "%d compensated row(s)" % (len(comp_lines),
                                                 db_compensated)))
    # Every compensation row must name a row that exists.
    for row in comp_lines:
        if row.get("kind") != msg_chain.COMPENSATION_KIND:
            out.append(_v(CODE_COMPENSATION_COUNT,
                          "a compensation row has kind=%r — the only kind is "
                          "%r" % (row.get("kind"), msg_chain.COMPENSATION_KIND)))
            continue
        try:
            found = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE agent_id = ? AND "
                "session_id = ? AND seq = ? AND content_state = 'external'",
                (row.get("agent_id") or "", row.get("session_id") or "",
                 int(row.get("target_seq") or -1)),
            ).fetchone()[0]
        except sqlite3.Error:
            found = 0
        if not found:
            out.append(_v(CODE_COMPENSATION_COUNT,
                          "a compensation row targets seq=%s of session %r — "
                          "no externalised row carries that seq"
                          % (row.get("target_seq"), row.get("session_id"))))
    return _result("MSG-INV-5", RED if out else GREEN, fp, out)


# ---------------------------------------------------------------------------
# MSG-INV-6 — the three states of content
# ---------------------------------------------------------------------------

def check_msg_inv6(workspace: Union[str, Path],
                   conn: sqlite3.Connection) -> Dict[str, Any]:
    """MSG-INV-6: the three states of ``content`` are not conflated.

    * ``content IS NULL AND content_state = 'inline'`` must be 0 — that
      is an ILLEGAL state (the text exists nowhere);
    * ``content_state = 'pending'`` must be 0 after the inspector's
      self-healing — and the inspector's recovery is what this
      criterion reads.

    The ``pending`` rows are RECOVERED here, because 裁决 C-7 makes the
    INSPECTOR the owner of the crash recovery ("巡检是唯一『随时可能
    发现不一致』的地方").  A pending row whose line IS in the JSONL is
    healed (the state becomes 'external' and the pointer is filled);
    a pending row whose line is NOT there is reported RED — the text
    may be gone, and a human must look.
    """
    why = _precondition(workspace)
    if why:
        return _result("MSG-INV-6", NOTRUN, "files=0 lines=0", [
            _v(CODE_CONTENT_NULL_INLINE, why, severity="mid")])

    files = _main_chain_files(workspace)
    total = sum(len(msg_chain.read_msg_lines(p)) for p in files)
    fp = "files=%d lines=%d" % (len(files), total)
    out: List[Dict[str, Any]] = []

    try:
        null_inline = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NULL AND "
            "content_state = 'inline'"
        ).fetchone()[0]
    except sqlite3.Error as e:
        return _result("MSG-INV-6", NOTRUN, fp + " db=unreadable", [
            _v(CODE_CONTENT_NULL_INLINE,
               "cannot read the messages table: %s" % e, severity="mid")])
    if null_inline:
        out.append(_v(CODE_CONTENT_NULL_INLINE,
                      "%d row(s) carry content IS NULL with "
                      "content_state='inline' — the text exists nowhere" %
                      null_inline))

    # The pending rows: heal what can be healed, report what cannot.
    try:
        pending = conn.execute(
            "SELECT id, agent_id, session_id, seq, content_path, "
            "content_anchor, content_hash FROM messages "
            "WHERE content_state = 'pending'"
        ).fetchall()
    except sqlite3.Error:
        pending = []
    healed = 0
    for row in pending:
        rid, aid, sid, seq, rel, anchor, chash = row
        ok = False
        if rel and anchor:
            try:
                reader = msg_chain.ContentReader(workspace)
                text = reader.text_of(rel, anchor)
                if chash:
                    recomputed = msg_chain._hash_of_line_text(text)
                    ok = (not recomputed) or (recomputed == chash)
                else:
                    ok = True
            except msg_chain.MsgChainError:
                ok = False
        if ok:
            conn.execute(
                "UPDATE messages SET content_state = 'external' WHERE id = ?",
                (rid,))
            healed += 1
        else:
            out.append(_v(CODE_PENDING_ORPHANED,
                          "the row (id=%s, session=%r, seq=%s) is 'pending' "
                          "and its JSONL line is not there — the text may be "
                          "gone; a human must look" % (rid, sid, seq)))
    if healed:
        conn.commit()
    fp = fp + " healed=%d pending=%d" % (healed, len(pending))
    return _result("MSG-INV-6", RED if out else GREEN, fp, out)


# ---------------------------------------------------------------------------
# MSG-INV-7 — the read paths are equivalent
# ---------------------------------------------------------------------------

def check_msg_inv7(workspace: Union[str, Path], conn: sqlite3.Connection,
                   agent_id: str = "", sessions: Optional[List[str]] = None,
                   max_turns_values: Tuple[int, ...] = (3, 30, 200),
                   ) -> Dict[str, Any]:
    """MSG-INV-7: the readers return the SAME result before and after.

    The measurement: for each session, the reader's result is compared
    against the content the DB would have returned had the content not
    been externalised.  The reference is the JSONL's own lines — the
    truth source — so the comparison does not need a pre-migration
    snapshot.

    ``max_turns`` is checked at THREE values (3 / 30 / 200), because
    the budget's semantics only break at its BOUNDARY: a single
    ``max_turns=10**6`` would pass on a reader that returns everything.

    **The criterion's own criterion:** on a library that has NOT been
    externalised, this must be ``NOTRUN`` — not ``GREEN``.  A green
    there means the reader was never asked for externalised content.
    """
    why = _precondition(workspace)
    if why:
        return _result("MSG-INV-7", NOTRUN, "files=0 lines=0", [
            _v(CODE_READPATH_DIVERGED, why, severity="mid")])

    files = _main_chain_files(workspace)
    total = sum(len(msg_chain.read_msg_lines(p)) for p in files)
    fp = "files=%d lines=%d" % (len(files), total)
    out: List[Dict[str, Any]] = []

    from agent.memory.conversation_store import ConversationStore

    try:
        db_path = _db_path_of(conn)
    except Exception as e:
        return _result("MSG-INV-7", NOTRUN, fp + " db=unknown", [
            _v(CODE_READPATH_DIVERGED,
               "cannot determine the database path: %s" % e, severity="mid")])
    if db_path is None:
        return _result("MSG-INV-7", NOTRUN, fp + " db=unknown", [
            _v(CODE_READPATH_DIVERGED,
               "the database path is unknown — the equivalence check needs "
               "the real store", severity="mid")])

    store = ConversationStore(db_path, agent_id=agent_id)

    pairs = _sessions_with_external_rows(conn, agent_id)
    if not pairs:
        return _result("MSG-INV-7", NOTRUN, fp + " sessions=0", [
            _v(CODE_READPATH_DIVERGED,
               "no session holds an externalised row — the read paths were "
               "never asked for externalised content", severity="mid")])

    checked = 0
    for (owner, sid) in pairs:
        # The reader is instantiated for the OWNER of the session: a
        # session belongs to the PAIR (agent_id, session_id), and a
        # reader bound to the wrong agent sees nothing.
        reader_store = (store if owner == agent_id
                        else ConversationStore(db_path, agent_id=owner))
        expected = _expected_contents(conn, sid, owner)
        if not expected:
            continue
        for mt in max_turns_values:
            try:
                got = reader_store.load_messages(sid, max_turns=mt)
            except Exception as e:
                # The reader RAISED — the text could not be recovered.
                # That is a RED, not a crash: a criterion that dies on
                # the thing it measures measures nothing.
                out.append(_v(CODE_READPATH_DIVERGED,
                              "session %r (agent %r) at max_turns=%d: the "
                              "reader raised %s: %s — the content could not "
                              "be read back"
                              % (sid, owner, mt, type(e).__name__, e)))
                checked += 1
                continue
            got_content = [m.get("content") for m in got]
            # The reference: the truth source's own text, in seq order.
            want = _expected_sequence(conn, sid, owner, mt, expected)
            if got_content != want:
                out.append(_v(CODE_READPATH_DIVERGED,
                              "session %r (agent %r) at max_turns=%d: the "
                              "reader returned %d message(s) and the truth "
                              "source says %d"
                              % (sid, owner, mt, len(got_content),
                                 len(want))))
            checked += 1
    fp = fp + " sessions=%d comparisons=%d" % (len(pairs), checked)
    return _result("MSG-INV-7", RED if out else GREEN, fp, out)


def _db_path_of(conn: sqlite3.Connection) -> Optional[Path]:
    """The file behind an open connection, when it can be found."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _sessions_with_external_rows(conn: sqlite3.Connection,
                                 agent_id: str) -> List[Tuple[str, str]]:
    """The ``(agent_id, session_id)`` pairs that hold externalised rows.

    **The pair, not the session alone.**  The ``messages`` table's
    uniqueness is ``(agent_id, session_id, seq)`` and two Agents
    legitimately share a ``session_id`` — so a session id alone does
    not identify whose conversation it is.  Measured on the ROOT
    copy: the session
    ``delegate_agent-mvnfkp3d_agent-mvnerelm_e2914220f505bc3f``
    belongs to ``agent-mvnerelm``, and querying it as the default
    Agent returned 0 rows while the truth source held 220 — a false
    divergence report.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT agent_id, session_id FROM messages "
            "WHERE content_state = 'external' "
            + ("AND agent_id = ? " if agent_id else "")
            + "ORDER BY agent_id, session_id",
            (agent_id,) if agent_id else (),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(r[0] or "", r[1]) for r in rows]


def _expected_contents(conn: sqlite3.Connection, session_id: str,
                       agent_id: str) -> Dict[int, str]:
    """``seq -> content`` for the session's externalised rows."""
    try:
        rows = conn.execute(
            "SELECT seq, content_hash FROM messages "
            "WHERE session_id = ? AND content_state = 'external' "
            + ("AND agent_id = ? " if agent_id else ""),
            (session_id, agent_id) if agent_id else (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {int(r[0]): r[1] for r in rows}


def _expected_sequence(conn: sqlite3.Connection, session_id: str,
                        agent_id: str, max_turns: int,
                        external: Dict[int, str]) -> List[Any]:
    """The reader's expected result, computed from the truth source.

    The reference implementation is deliberately simple and INDEPENDENT
    of the code under test: the rows are read in seq order, the
    externalised ones are replaced by the text from the JSONL, and the
    budget is applied by counting the visible user messages.

    **The context boundary matters (measured).**  ``load_messages``
    honours ``sessions.context_start_seq`` — the rows before the
    boundary are stored but excluded from the LLM's context.  A
    reference that forgets the boundary returns MORE rows than the
    reader, and the criterion reports a divergence that is not one.
    Measured on the ROOT copy: the reference said 435 messages where
    the reader said 221.
    """
    try:
        ctx = conn.execute(
            "SELECT context_start_seq FROM sessions "
            "WHERE session_id = ?"
            + ("AND agent_id = ? " if agent_id else ""),
            (session_id, agent_id) if agent_id else (session_id,),
        ).fetchone()
        ctx_start = int(ctx[0]) if ctx and ctx[0] is not None else 0
    except sqlite3.Error:
        ctx_start = 0
    try:
        rows = conn.execute(
            "SELECT seq, role, content, content_path, content_anchor, "
            "content_hash, content_state FROM messages "
            "WHERE session_id = ? AND seq >= ? "
            + ("AND agent_id = ? " if agent_id else "")
            + "ORDER BY seq ASC",
            (session_id, ctx_start, agent_id) if agent_id
            else (session_id, ctx_start),
        ).fetchall()
    except sqlite3.Error:
        return []
    reader = msg_chain.ContentReader(_workspace_of(conn))
    prepared: List[Tuple[int, str, Any]] = []
    for seq, role, content, rel, anchor, chash, state in rows:
        text = content
        if state == msg_chain.STATE_EXTERNAL and rel and anchor:
            text = reader.text_of(rel, anchor)
        try:
            value = json.loads(text)
        except Exception:
            value = text
        if role == "assistant" and isinstance(value, list):
            value = [b for b in value if b.get("type") != "thinking"]
        prepared.append((int(seq), role, value))

    # The budget, applied the SAME way the store applies it.
    #
    # **The store's rule, exactly (measured).**  Walking newest-first:
    #   * a visible user message that arrives AFTER the budget is spent
    #     ENDS the walk — and the store returns ONLY the rows it kept,
    #     not the rows it collected as "older" on the way;
    #   * a non-visible row that arrives after the budget is spent is
    #     collected as "older" and returned ONLY when the walk ends
    #     without hitting a visible row (i.e. the session had fewer
    #     than ``max_turns`` visible turns, so everything is returned).
    #
    # The reference originally returned ``kept + older`` in BOTH cases,
    # and the two disagreed by 214 rows on the ROOT copy — which is a
    # false divergence report, not a real one.
    visible = 0
    kept: List[Tuple[int, str, Any]] = []
    older: List[Tuple[int, str, Any]] = []
    for entry in reversed(prepared):
        seq, role, value = entry
        is_visible = (role == "user" and _is_visible(value))
        if visible >= max_turns:
            if is_visible:
                # The store returns ONLY what it kept.
                return [value for _s, _r, value in reversed(kept)]
            older.append(entry)
            continue
        kept.append(entry)
        if is_visible:
            visible += 1
    # The session had fewer visible turns than the budget: everything.
    out = kept + older
    out.reverse()
    return [value for _seq, _role, value in out]


def _is_visible(value: Any) -> bool:
    """The visibility predicate — the SAME rule the reader uses.

    **A message with text AND a tool_result is visible.**  The reader's
    ``_is_visible_user_message`` asks "does this carry a text block?" —
    it does NOT ask "is it a pure text message?".  The reference
    originally asked the stricter question, and the two disagreed on
    every tool-heavy turn: measured on the ROOT copy, the reader found
    38 visible user messages where the reference found 34, and at
    ``max_turns=3`` the reader returned 221 rows where the reference
    said 435.

    The reference must implement the SAME rule, independently — not a
    different one.  It is written out here rather than imported so
    that a change to the reader's rule does not silently change the
    reference with it (a criterion that measures the implementation
    against the implementation measures nothing).
    """
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(isinstance(b, dict) and b.get("type") == "text"
                   for b in value)
    return False


def _workspace_of(conn: sqlite3.Connection) -> Path:
    """The workspace the connection's database lives under."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2]).resolve().parents[2]
    return Path(".")


# ---------------------------------------------------------------------------
# The baseline — the re-computed hashes M-3 compares against
# ---------------------------------------------------------------------------

def _read_baseline(workspace: Union[str, Path]) -> List[Dict[str, Any]]:
    path = _baseline_file(workspace)
    if not path.is_file():
        return []
    return msg_chain.read_msg_lines(path)


def write_baseline(workspace: Union[str, Path]) -> Dict[str, Any]:
    """Record the current re-computed hashes as the new baseline.

    The baseline is APPENDED, never overwritten: the history of what
    was believed is itself a fact, and a baseline that is rewritten in
    place cannot show that it was rewritten.
    """
    path = _baseline_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = _export_root(workspace).parent
    per_file: Dict[str, List[str]] = {}
    total = 0
    for p in _main_chain_files(workspace):
        rel = p.relative_to(root).as_posix()
        hashes = []
        for line in msg_chain.read_msg_lines(p):
            try:
                hashes.append(msg_chain.msg_line_hash(line))
            except (KeyError, TypeError):
                hashes.append("")
        per_file[rel] = hashes
        total += len(hashes)
    entry = {
        "rel": "*",
        "line_count": total,
        "total_lines": total,
        "line_hashes": per_file,
        "captured_at": _now(),
    }
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return {"baseline": str(path), "files": len(per_file), "lines": total}


def _now() -> str:
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The family — every library of a workspace
# ---------------------------------------------------------------------------

def run_msg_invariants(workspace: Union[str, Path], db: Union[str, Path],
                       agent_id: str = "") -> Dict[str, Any]:
    """Every MSG-INV criterion for ONE library.

    The three states are kept apart: ``NOTRUN`` is not ``GREEN`` and
    not ``RED``.  The fingerprint of every criterion says how many files
    and lines it looked at.
    """
    ws = Path(workspace)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        checks = {
            "MSG-INV-1": check_msg_inv1(ws),
            "MSG-INV-2": check_msg_inv2(ws),
            "MSG-INV-3": check_msg_inv3(ws),
            "MSG-INV-4": check_msg_inv4(ws),
            "MSG-INV-5": check_msg_inv5(ws, conn, agent_id),
            "MSG-INV-6": check_msg_inv6(ws, conn),
            "MSG-INV-7": check_msg_inv7(ws, conn, agent_id),
        }
    finally:
        conn.close()
    violations: List[Dict[str, Any]] = []
    for c in checks.values():
        violations.extend(c["violations"])
    verdicts = {k: v["verdict"] for k, v in checks.items()}
    if any(v == RED for v in verdicts.values()):
        overall = RED
    elif any(v == NOTRUN for v in verdicts.values()):
        overall = NOTRUN
    else:
        overall = GREEN
    return {
        "scope": "messages 外置域 (MSG-INV-1..7)",
        "library": str(db),
        "verdict": overall,
        "checks": checks,
        "violations": violations,
        "violation_count": len(violations),
    }
