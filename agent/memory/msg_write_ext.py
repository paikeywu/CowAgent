"""Write-time externalisation: the DB row's text moves OUT at WRITE time.

**What this is (P1 阶段三 M · 缺口 A · 方案 B「写入口外置」).**

Until now the externalisation was a ONE-SHOT MIGRATION: the text left
the ``messages`` table when ``msg_migrate.externalise_session()`` was
run, and every message written afterwards landed back in the table as
``inline``.  Measured on the live ROOT library (2026-11-01 03:00):
39,511 message rows, **7,061 of them inline** — a number that GREW
while the migration was being discussed, because the migration is not
part of the product.

This module makes the externalisation an **invariant of the write**:
after ``ConversationStore.append_messages()`` commits, the rows it just
wrote are moved into the session's append-only JSONL immediately.

**The five steps, and WHY they are in this order** (the same order
``msg_migrate.externalise_session`` uses — one protocol, not two):

  1. build the line and its hash (``msg_chain.build_msg_line``);
  2. append it to the session's JSONL, ``flush()`` + ``os.fsync()``;
  3. read the file back and re-hash every line just written;
  4. write the chain-head anchor (``line:<physical line number>``);
  5. ONE ``UPDATE`` that empties ``content`` AND fills the pointer AND
     flips ``content_state`` to ``external``.

Step 5 is last because the reverse order loses the text.  Step 5 is
ONE statement because two statements leave the illegal middle state
``content IS NULL AND content_state='inline'`` behind a crash — and
that state is MSG-INV-6 RED.

**The crash window, and why it is a ``pending`` state and not a
re-run.**  Between step 2 and step 5 the row exists in BOTH places: the
JSONL holds the line and the DB still holds the text.  A crash there
must NOT be handled by "running the batch again" — that would append
the SAME seqs a second time, and the JSONL would carry two copies of
one message (MSG-INV-5 RED, and the chain's ``prev_hash`` would
describe a history that never happened).

So the row is marked ``pending`` BEFORE the file write, and the
``pending`` row carries the pointer's three fields
(``content_path`` / ``content_anchor`` / ``content_hash``) — the
inspector's self-healing needs all three (``msg_inv.py:523``), and a
``pending`` row without them is MSG-INV-6 RED in a way nothing can
repair.

**The recovery is IDEMPOTENT, and the JSONL is the authority** (R7):

  * the JSONL's lines are IMMUTABLE once written (R4) — a line that is
    on disk is never rewritten, so "skip" is the ONLY legal repair and
    "rewrite" is not an option;
  * therefore, before appending, this module reads the session's file
    and asks "which of my seqs are already there?";
  * a seq that is already there is NOT appended again — its existing
    line is re-used, its ``line_hash`` and its PHYSICAL LINE NUMBER are
    read back, and the DB row is simply completed to ``external``;
  * the ``prev_hash`` of the first NEW line continues from the file's
    last line, so the chain stays one chain across the whole life of
    the session.

**Why the content source is the DB row, not the caller's dict** (R9):
the DB row's ``content`` is ALREADY masked — ``append_messages`` masks
at the database throat (``conversation_store.py:1150-1153``) — and
``msg_chain.build_msg_line`` masks before it hashes.  Masking after
hashing would make the stored hash describe text that is no longer
there, and every read would raise ``ContentDriftError`` (MSG-INV-7
RED).

**Why the content is REALLY emptied** (R6): the delete path's
``_archive_before_delete`` selects ``content IS NOT NULL`` and appends
those rows to the JSONL.  A row whose ``content`` was merely pointed
at (and left in place) would be appended a SECOND time by the next
delete — the JSONL would hold two copies of one message (MSG-INV-5
RED).  So step 5 empties the column, not merely points at it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from agent.memory import msg_chain

_CST = timezone(timedelta(hours=8))

#: The anchor action this module writes.  A NEW name, so the anchor
#: chain says which mechanism produced the line (``migrate_out`` is
#: the batch tool's, ``append_msg`` is the delete path's archive).
ANCHOR_ACTION = "write_out"

#: The anchor action of the delete path's archive — reused so the
#: recovery can recognise a line the delete path wrote.
DELETE_ARCHIVE_ACTION = "append_msg"


def _now_iso() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _today() -> str:
    """The batch date, in the SAME timezone the migration tool uses.

    Reused from ``msg_migrate`` rather than re-derived: two calendars
    are how one session's chain ends up in two directories.
    """
    from agent.memory import msg_migrate

    return msg_migrate._today()


class WriteExternalisationError(RuntimeError):
    """The externalisation failed.

    Raised, never swallowed: a write that claims to have externalised
    and did not is the "silent failure" this whole module exists to
    prevent.  The caller (``append_messages``) decides what to do —
    the DB rows are already committed, so the honest options are
    "leave them inline and report" or "raise"; it never deletes.
    """


def _read_file_lines(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Every parsed line of the JSONL, and its PHYSICAL line count.

    The physical count is what the anchor names (``line:<n>``), and it
    is NOT the same as the number of PARSED lines when the file holds
    an unparseable line — so the two are counted separately and the
    physical one is what the anchor uses.

    Returns ``(lines, physical_line_count)``.
    """
    if not path.is_file():
        return [], 0
    parsed: List[Dict[str, Any]] = []
    physical = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            physical += 1
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                parsed.append(obj)
    return parsed, physical


def _existing_seq_map(path: Path) -> Dict[int, Dict[str, Any]]:
    """``seq -> line`` for the lines a session's JSONL already holds.

    This is the authority for the idempotent recovery: a seq that is
    in here was already written, and its line is IMMUTABLE (R4).

    **The LAST occurrence wins — and so does the physical map.**  The
    two maps MUST agree: the anchor names a PHYSICAL line and the row
    records that line's hash, so if the hash came from one occurrence
    and the anchor from another, the reader would recompute a
    different hash and raise ``ContentDriftError``.

    (This was a real defect: the hash map took the last occurrence
    while the physical map took the first, so a file holding the same
    seq twice produced a pointer to one line with the hash of
    another.  Measured on this machine, 2026-11-01.)

    The LAST occurrence is the right one to prefer: the file is
    append-only and the chain is ordered by WRITE, so the newest line
    for a seq is the one that carries the newest text.  A seq that
    appears MORE THAN ONCE is a double-write — a defect in its own
    right — so it is counted and reported rather than hidden.
    """
    lines, _physical = _read_file_lines(path)
    out: Dict[int, Dict[str, Any]] = {}
    dupes: List[int] = []
    for line in lines:
        seq = line.get("seq")
        if seq is None:
            continue
        try:
            s = int(seq)
        except (TypeError, ValueError):
            continue
        if s in out:
            dupes.append(s)
        out[s] = line
    _existing_seq_map.duplicates = dupes  # type: ignore[attr-defined]
    return out


def _seq_physical_map(path: Path) -> Dict[int, int]:
    """``seq -> PHYSICAL line number`` for one JSONL.

    The anchor names a physical line, and the physical line is NOT the
    seq (R8): a session's seq has holes (the delete paths create them)
    and the file's lines are numbered from 1 in file order.

    **The LAST occurrence wins, exactly as in :func:`_existing_seq_map`.**
    The two maps must agree on the SAME line — the anchor names one
    physical line and the row records that line's hash, so a
    disagreement makes the reader recompute a different hash and raise
    ``ContentDriftError``.
    """
    out: Dict[int, int] = {}
    if not path.is_file():
        return out
    physical = 0
    with open(path, encoding="utf-8") as f:
        for raw in f:
            physical += 1
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            seq = obj.get("seq")
            if seq is None:
                continue
            try:
                out[int(seq)] = physical
            except (TypeError, ValueError):
                continue
    return out


def session_target_file(conn: sqlite3.Connection,
                        workspace: Union[str, Path],
                        agent_id: str,
                        session_id: str,
                        *, batch_date: Optional[str] = None,
                        ) -> Tuple[str, Path, str]:
    """The session's truth file: the one it ALREADY has, or today's.

    **Why this is not simply "today's date".**  The file name is
    ``{slug(session_id)}-{sha256(agent_id|session_id)[:8]}.jsonl`` and it
    lives under ``messages_export/<batch-date>/`` — so the DATE is part
    of the path, but the date is the **migration batch date**, not the
    message's date.  Measured on the live ROOT library (2026-11-01):
    all fifteen session files live under ``2026-10-31/`` even though
    their messages were written between 2026-10-25 and 2026-10-31.

    So a session's rows are a CONTIGUOUS SEQ SPACE in ONE file, and the
    chain's ``prev_hash`` continues from that file's last line.  If the
    write-time externaliser put today's messages in a DIFFERENT
    directory, the session would be split across two files and two
    chains — which breaks the contract ``session_filename`` states in
    so many words ("the truth source must be one file per PAIR") and
    makes the session's chain discontinuous.

    Therefore: **a session that already has an externalised row keeps
    the file that row points at**; a session with none yet gets
    today's batch date.

    Returns ``(rel, path, batch_date)``.
    """
    row = conn.execute(
        "SELECT content_path FROM messages "
        "WHERE agent_id = ? AND session_id = ? AND content_state = 'external' "
        "AND content_path IS NOT NULL AND content_path <> '' "
        "ORDER BY seq ASC LIMIT 1",
        (agent_id, session_id),
    ).fetchone()
    if row and row[0]:
        rel = str(row[0])
        # The rel is relative to ``memory/`` (``messages_export/<date>/<file>``),
        # so the absolute path is the workspace's ``memory/`` plus the rel.
        path = Path(workspace) / "memory" / rel
        # Recover the batch date from the rel so the report says where the
        # lines went (``messages_export/<date>/<file>`` -> ``<date>``).
        parts = rel.split("/")
        batch = parts[1] if len(parts) >= 3 else (batch_date or _today())
        return rel, path, batch

    batch = batch_date or _today()
    rel = msg_chain.session_relpath(session_id, batch, agent_id)
    return rel, msg_chain.session_path(workspace, session_id, batch, agent_id), batch


def _heal_rows_already_in_the_file(conn: sqlite3.Connection,
                                   agent_id: str,
                                   session_id: str,
                                   rel: str,
                                   existing_by_seq: Dict[int, Dict[str, Any]],
                                   physical_by_seq: Dict[int, int],
                                   ) -> int:
    """Point the rows whose line is ALREADY on disk at that line.

    **The crash window's residue.**  A crash between the file write and
    the DB emptying leaves the row holding its text AND its line being
    in the JSONL.  Nothing else would ever touch it — the write path
    only externalises the seqs it just wrote, and the batch tool only
    runs on demand — so it would sit at ``inline`` for ever, and the
    delete path (which selects ``content IS NOT NULL``) would archive
    it a SECOND time.

    Healing writes nothing: the line is on disk and immutable (R4), so
    the row is simply completed to ``external`` — the same ONE-statement
    shape as step 5 (R2).

    The hash is RE-COMPUTED from the line, not taken on trust: the
    file's self-reported hash is a claim.

    Returns the number of rows healed.
    """
    if not existing_by_seq:
        return 0
    rows = conn.execute(
        "SELECT seq FROM messages "
        "WHERE agent_id = ? AND session_id = ? "
        "AND content IS NOT NULL AND content_state <> 'external'",
        (agent_id, session_id),
    ).fetchall()
    healed = 0
    for (seq,) in rows:
        try:
            s = int(seq)
        except (TypeError, ValueError):
            continue
        line = existing_by_seq.get(s)
        if line is None:
            # Not in the file — it is a row the file does not know
            # about.  It cannot be healed here (appending it would
            # break the chain: the file is ordered by WRITE, not by
            # seq).  It stays inline and the batch tool will take it.
            continue
        physical_no = physical_by_seq.get(s, 0)
        if not physical_no:
            continue
        try:
            recomputed = msg_chain.msg_line_hash(line)
        except (KeyError, TypeError):
            continue
        cur = conn.execute(
            "UPDATE messages SET content = NULL, content_path = ?, "
            "content_anchor = ?, content_hash = ?, content_state = 'external' "
            "WHERE agent_id = ? AND session_id = ? AND seq = ?",
            (rel, "line:%d" % physical_no, recomputed, agent_id, session_id, s),
        )
        healed += max(cur.rowcount, 0)
    if healed:
        conn.commit()
    return healed


def externalise_messages(
    conn: sqlite3.Connection,
    workspace: Union[str, Path],
    agent_id: str,
    session_id: str,
    seqs: Sequence[int],
    *,
    batch_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Externalise the given seqs of ONE session, right after the write.

    ``seqs`` are the sequence numbers ``append_messages`` just
    committed.  They are NOT assumed to be contiguous and NOT assumed
    to be new: a seq that is already in the JSONL is treated as
    already-written (the idempotent recovery), because the file is the
    authority and a line on disk is immutable.

    Returns a report — never raises for a "nothing to do" case, and
    raises :class:`WriteExternalisationError` when the truth source
    could not be written (the caller must know: a write that silently
    did not externalise is the failure this module exists to prevent).
    """
    out: Dict[str, Any] = {
        "session_id": session_id,
        "agent_id": agent_id,
        "batch_date": batch_date or _today(),
        "requested": len(seqs),
        "written": 0,
        "recovered": 0,
        "healed": 0,
        "emptied": 0,
        "skipped": 0,
        "rel": "",
        "first_line_no": 0,
        "errors": [],
    }
    if not seqs:
        return out

    # The session's file: the one it already has, or today's.
    rel, path, batch = session_target_file(
        conn, workspace, agent_id, session_id, batch_date=batch_date)
    out["batch_date"] = batch
    out["rel"] = rel

    # 0. the rows to move.  Only rows that still hold their text and
    #    are NOT already external are candidates: an ``external`` row
    #    has been through this module already, and a row with no text
    #    has nothing to move.
    placeholders = ",".join("?" for _ in seqs)
    rows = conn.execute(
        "SELECT seq, role, content, created_at, run_id, extras "
        "FROM messages WHERE agent_id = ? AND session_id = ? "
        "AND seq IN (%s) AND content IS NOT NULL "
        "AND content_state <> 'external' "
        "ORDER BY seq ASC" % placeholders,
        (agent_id, session_id, *seqs),
    ).fetchall()

    # The lines already on disk, and the chain's current head.
    existing, physical = _read_file_lines(path)
    existing_by_seq = _existing_seq_map(path)
    physical_by_seq = _seq_physical_map(path)
    prev = msg_chain.MSG_GENESIS_PREV_HASH
    if existing:
        prev = existing[-1].get("line_hash") or msg_chain.MSG_GENESIS_PREV_HASH
    # The physical line number of the FIRST line this call would
    # write.  It is the FILE's line count, not the number of parsed
    # lines: an unparseable line is still a physical line, and the
    # anchor must name the physical one.
    first_line_no = physical + 1

    # 0b. **The rows left behind by a crash, healed.**
    #
    # A crash between step 2 (the file write) and step 5 (the DB
    # emptying) leaves the rows holding their text AND their line in
    # the JSONL.  Nothing else will ever touch them: the write path
    # only externalises the seqs it just wrote, and the backfill only
    # runs on demand.  So they would sit at ``inline`` for ever — and
    # the delete path selects ``content IS NOT NULL``, so it would
    # archive them a SECOND time (R6's failure).
    #
    # Healing them is safe: their line is already on disk (R4 — it is
    # immutable), so nothing is written and the chain is untouched.
    # They are found by asking the FILE which seqs it holds.
    #
    # A row that is inline and NOT in the file is NOT healed here: its
    # seq is below the file's tail, and appending it would break the
    # chain (the JSONL is append-only and ordered by WRITE, not by
    # seq).  Those rows are reported as ``deferred`` — they need the
    # batch tool, which writes a session's rows in seq order.
    healed = _heal_rows_already_in_the_file(
        conn, agent_id, session_id, rel, existing_by_seq, physical_by_seq)
    out["healed"] = healed

    if not rows:
        out["skipped"] = len(seqs)
        return out

    # 1. build the lines — and split the rows into "already written"
    #    (the idempotent recovery) and "to be written".
    to_write: List[Tuple[tuple, Dict[str, Any]]] = []
    recovered: List[Tuple[tuple, Dict[str, Any], int]] = []
    for row in rows:
        seq, role, content, created_at, run_id, extras = row
        seq = int(seq)
        prior = existing_by_seq.get(seq)
        if prior is not None:
            # **The idempotent recovery (R7).**  The line is already on
            # disk: it is IMMUTABLE (R4), so it is NOT rewritten.  Its
            # hash and its PHYSICAL LINE NUMBER are read back from the
            # file, and the DB row is completed to ``external``.
            recovered.append((row, prior, physical_by_seq.get(seq, 0)))
            continue
        line = msg_chain.build_msg_line(
            seq=seq, agent_id=agent_id, session_id=session_id,
            role=role or "", content=content,
            created_at=int(created_at or 0), run_id=run_id or "",
            prev_hash=prev, extras=extras or "",
        )
        prev = line["line_hash"]
        to_write.append((row, line))

    out["recovered"] = len(recovered)

    if to_write:
        # ---- 1b. the crash-window marker -----------------------------
        # The rows are marked ``pending`` with their pointer's three
        # fields BEFORE the file is written (R5).  A crash after the
        # file write leaves a ``pending`` row whose pointer is
        # complete — which is exactly what the inspector's self-
        # healing needs (``msg_inv.py:523`` requires rel AND anchor
        # to be non-empty) — and the text is still in the DB, so the
        # row is never ``NULL + inline`` (the MSG-INV-6 illegal
        # state).
        #
        # The content is NOT emptied here: emptying it before the
        # file holds the text would lose the text if the write failed.
        for offset, (row, line) in enumerate(to_write):
            conn.execute(
                "UPDATE messages SET content_state = 'pending', "
                "content_path = ?, content_anchor = ?, content_hash = ? "
                "WHERE agent_id = ? AND session_id = ? AND seq = ?",
                (rel, "line:%d" % (first_line_no + offset), line["line_hash"],
                 agent_id, session_id, int(row[0])),
            )
        conn.commit()

        # ---- 2. write the lines ---------------------------------------
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                for _row, line in to_write:
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            # **The rows go BACK to ``inline``.**  They still hold their
            # text (step 5 has not run), so ``inline`` + text NOT NULL is
            # the legal state and the reader still works.  Leaving them
            # ``pending`` with a pointer to a line that does NOT exist
            # would be worse than useless: MSG-INV-6's heal cannot repair
            # a ``pending`` row whose anchor does not resolve
            # (``msg_inv.py:523`` needs rel AND anchor, and
            # ``reader.text_of`` raises when the line is absent), so the
            # row would sit in ``msg-pending-orphaned`` for ever — a
            # permanent red over a failure that is trivially repairable
            # by simply not claiming the row was pending.
            self_rows = [int(r[0]) for r, _line in to_write]
            ph = ",".join("?" for _ in self_rows)
            conn.execute(
                "UPDATE messages SET content_state = 'inline', content_path = '', "
                "content_anchor = '', content_hash = '' "
                "WHERE agent_id = ? AND session_id = ? AND seq IN (%s)" % ph,
                (agent_id, session_id, *self_rows),
            )
            conn.commit()
            raise WriteExternalisationError(
                "the session %r's truth file %s could not be appended to: %s "
                "— the rows were put back to 'inline' (they still hold their "
                "text), so nothing is lost" % (session_id, path, e))

        # ---- 3. read back and verify ----------------------------------
        written, written_physical = _read_file_lines(path)
        tail = written[len(written) - len(to_write):] if len(written) >= len(to_write) else []
        if len(tail) != len(to_write):
            raise WriteExternalisationError(
                "the truth file %s holds %d line(s) but %d were written — the "
                "write was truncated" % (path, len(written), len(to_write)))
        for i, (got, (_row, want)) in enumerate(zip(tail, to_write)):
            if got.get("line_hash") != want["line_hash"]:
                raise WriteExternalisationError(
                    "line %d of %s does not hash to what was written — the "
                    "file was changed under the writer" % (i + 1, path))
        out["written"] = len(to_write)
        out["first_line_no"] = first_line_no

    # ---- 4. the anchor ------------------------------------------------
    # The anchor names the file's TAIL, with the PHYSICAL line number
    # (R8) — not the seq.  A session's seq has holes (the delete paths
    # create them) and the two numbers must not be confused.
    #
    # It is written EVEN IN THE RECOVERY PATH: a crash between the
    # file write and the anchor write leaves the file without an
    # anchor, and MSG-INV-4 is RED until the anchor exists.  The
    # helper is idempotent — it writes only when the current tail
    # anchor does not already describe the file's tail.
    final_lines, final_physical = _read_file_lines(path)
    if final_lines:
        last = final_lines[-1]
        _write_anchor(workspace, rel, int(last.get("seq") or 0),
                      str(last.get("line_hash") or ""),
                      physical=final_physical)

    # ---- 5. the DB pointer — ONE UPDATE per row -----------------------
    # The content is emptied AND the pointer filled AND the state
    # flipped in the SAME statement (R2).  Two statements would leave
    # ``content IS NULL AND content_state='inline'`` behind a crash —
    # the MSG-INV-6 illegal state.
    pointers: List[Tuple[str, int, str, int]] = []
    if to_write:
        for offset, (_row, line) in enumerate(to_write):
            pointers.append((rel, first_line_no + offset,
                             str(line["line_hash"]), int(_row[0])))
    for row, prior, physical_no in recovered:
        # The recovered line's own hash and physical line number.  The
        # hash is RE-COMPUTED from the line, not taken on trust: the
        # file's self-reported hash is a claim (裁决 §8.2).
        recomputed = msg_chain.msg_line_hash(prior)
        pointers.append((rel, physical_no, recomputed, int(row[0])))
    for p_rel, line_no, line_hash, seq in pointers:
        cur = conn.execute(
            "UPDATE messages SET content = NULL, content_path = ?, "
            "content_anchor = ?, content_hash = ?, content_state = 'external' "
            "WHERE agent_id = ? AND session_id = ? AND seq = ?",
            (p_rel, "line:%d" % line_no, line_hash, agent_id, session_id, seq),
        )
        out["emptied"] += max(cur.rowcount, 0)
    conn.commit()
    return out


def _write_anchor(workspace: Union[str, Path], rel: str, seq: int,
                  line_hash: str, *, physical: int,
                  action: str = ANCHOR_ACTION) -> Dict[str, Any]:
    """Write the chain-head anchor for the file — if it is not already there.

    **Why the "if it is not already there".**  The anchor file is
    APPEND-ONLY and its own chain is verified (MSG-INV-4 checks every
    anchor's ``prev_anchor_hash`` and its own hash).  Writing a
    second anchor for the same file with the same tail is harmless for
    the criterion (it reads the LAST anchor per ``rel``), but it grows
    a file that is supposed to be a record of EVENTS.  So the anchor is
    written only when the current tail anchor does not already describe
    the file's tail.
    """
    apath = msg_chain.anchor_path(workspace)
    if apath.is_file():
        anchors = msg_chain.read_msg_lines(apath)
        for a in reversed(anchors):
            if a.get("rel") == rel:
                if (a.get("line_hash") == line_hash
                        and int(a.get("seq") if a.get("seq") is not None else -1) == int(seq)):
                    return a
                break
    return msg_chain.append_anchor(
        workspace, rel=rel, seq=seq, line_hash=line_hash,
        action=action, at=_now_iso(),
    )


def session_chain_state(workspace: Union[str, Path], agent_id: str,
                        session_id: str,
                        batch_date: Optional[str] = None) -> Dict[str, Any]:
    """What the session's JSONL currently holds — the recovery's input.

    Exposed so the caller (and the tests) can ask "is this seq already
    written?" without re-implementing the file's layout.
    """
    batch_date = batch_date or _today()
    rel = msg_chain.session_relpath(session_id, batch_date, agent_id)
    path = msg_chain.session_path(workspace, session_id, batch_date, agent_id)
    lines, physical = _read_file_lines(path)
    return {
        "rel": rel,
        "path": str(path),
        "exists": path.is_file(),
        "lines": len(lines),
        "physical_lines": physical,
        "seqs": sorted(_existing_seq_map(path)),
        "last_line_hash": lines[-1].get("line_hash") if lines else "",
    }
