"""The messages chain: the truth source for conversation content.

**Why this module exists (P1 阶段三 M · messages 外置).**

The ``messages`` table is a PROJECTION.  Its ``content`` column held every
conversation's text — 89 MB in the ROOT library alone, measured on
2026-10-30 — and the truth source for that text is the conversation
itself.  This module moves the text OUT of the database and into an
append-only JSONL chain, one file per session, and gives the readers a
single, honest way to get it back.

**What is reused from facts, and what is NOT (裁决 C-1..C-3).**

Reused: ``agent.memory.naming.canonical_text`` (imported, never
re-implemented — two normalizers are how the same text hashes two
different ways), the chain's SHAPE (``prev_hash`` / ``line_hash``, the
``"sha256:"`` prefix, ``_GENESIS_PREV_HASH``), and the append protocol
(``"a"`` mode, ``flush`` + ``os.fsync``, never truncate, never rewrite).

NOT reused, and each for a measured reason:

* ``facade._fact_line_hash`` — it joins its five fields with ``"|"``.
  A message's ``content`` is JSON text and it routinely CONTAINS a pipe
  (``"a|b"`` is ordinary prose), so the separator would be ambiguous.
  This module uses JSON canonicalisation (``sort_keys=True``,
  ``separators=(",", ":")``, ``ensure_ascii=False``) instead.
* ``facade.FACT_LINE_MAX_BYTES`` (4096) — a message row is not a new
  atomic write, it is a MOVE of bytes that already exist.  Measured:
  5,786 of 32,744 rows exceed 4096 bytes and the largest is 192,915
  bytes.  Applying facts' limit here would throw on real data.
* facts' chain-head anchor — facts anchors itself in
  ``memory/.audit/facade.jsonl`` because ``facade.append_fact`` writes
  that audit.  ``ConversationStore`` writes no such audit, so messages
  carries its OWN anchor: ``memory/messages_export/.audit/msg_chain.jsonl``.

**The three states of ``content_state`` (裁决 C-11).**

``inline`` — the text is in the DB.  ``external`` — the text is in the
JSONL and the DB row carries the pointer.  ``pending`` — the file was
written but the DB was not emptied yet (the crash window of the
two-step protocol).  A ``NULL`` ``content`` with ``content_state`` of
``inline`` is an ILLEGAL state and the readers raise
``ContentMissingError`` rather than pretending the message was empty.

**The read-back contract (裁决 C-12).**

``load_content`` is the ONLY way a reader gets a message's text.  It
re-reads the JSONL line, RECOMPUTES the line hash and compares it with
the stored ``content_hash``: a mismatch is ``ContentDriftError``.  A
read that returns an empty string instead of raising would turn "the
externalised text was lost" into "the user never said anything" — the
same silent failure the whole design exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from agent.memory.naming import canonical_text  # noqa: F401  (imported, per C-2)


# ---------------------------------------------------------------------------
# The chain's vocabulary
# ---------------------------------------------------------------------------

#: The chain's genesis — the ``prev_hash`` of the very first line.
MSG_GENESIS_PREV_HASH = "sha256:" + "0" * 64

#: The fixed field order of a message line.  The order is part of the
#: chain: a line's bytes are what a reader compares.
MSG_FIELD_ORDER = (
    "seq", "agent_id", "session_id", "role", "content",
    "created_at", "run_id", "prev_hash", "line_hash",
)

#: The ``content_state`` values.  Three, and no more (裁决 N-16).
STATE_INLINE = "inline"
STATE_EXTERNAL = "external"
STATE_PENDING = "pending"
CONTENT_STATES = (STATE_INLINE, STATE_EXTERNAL, STATE_PENDING)

#: The anchor's own chain vocabulary (裁决 C-6, v2).  ``line_hash`` is the
#: JSONL's last line hash — that is what the anchor POINTS at.
#: ``anchor_hash`` is the anchor line's own hash — that is what chains
#: the anchors.  The two names never overlap, so ``json.loads`` cannot
#: silently drop one of them.
ANCHOR_FIELD_ORDER = (
    "rel", "seq", "line_hash", "at", "action", "prev_anchor_hash", "anchor_hash",
)

#: The anchor's hash field set — ``anchor_hash`` itself is excluded.
ANCHOR_HASH_FIELDS = ("rel", "seq", "line_hash", "at", "action",
                      "prev_anchor_hash")

#: The compensation row's vocabulary (裁决 C-2, v2).  Exactly nine keys;
#: a compensation row carries NO ``content`` — it carries a correction.
COMPENSATION_FIELD_ORDER = (
    "kind", "seq", "agent_id", "session_id", "target_seq",
    "extras", "at", "prev_hash", "line_hash",
)

#: The compensation chain's own hash field set.
COMPENSATION_HASH_FIELDS = (
    "kind", "seq", "agent_id", "session_id", "target_seq",
    "extras", "at", "prev_hash",
)

#: The compensation row's kind.  One kind, one shape.
COMPENSATION_KIND = "extras_update"

#: The anchor's directory, relative to the workspace root.
EXPORT_DIRNAME = "messages_export"
AUDIT_DIRNAME = ".audit"
ANCHOR_FILENAME = "msg_chain.jsonl"
COMPENSATION_FILENAME = "extras_compensations.jsonl"
BASELINE_FILENAME = "msg_baseline.jsonl"


# ---------------------------------------------------------------------------
# Errors — every failure is loud, and every one is distinguishable
# ---------------------------------------------------------------------------

class MsgChainError(Exception):
    """Base class for the messages chain's failures."""


class ContentMissingError(MsgChainError):
    """The row says ``inline`` but carries no content.

    This is an ILLEGAL state (裁决 C-11): the row was never externalised
    and yet has no text.  Returning ``None`` would make the message
    invisible to the turn budget and to the model — a silent loss.
    """


class ContentPendingError(MsgChainError):
    """The row is in the ``pending`` middle state.

    The file may or may not hold the text.  The inspector owns the
    recovery (裁决 C-7): it re-reads the JSONL and either heals the row
    or reports it as an orphan.  A reader must not guess.
    """


class ContentDriftError(MsgChainError):
    """The JSONL line does not hash to the stored ``content_hash``.

    The truth source moved under the pointer.  The reader refuses.
    """


class MsgAnchorError(MsgChainError):
    """The chain-head anchor does not describe the JSONL's tail."""


class MsgChainBreakError(MsgChainError):
    """The chain is not self-consistent."""


# ---------------------------------------------------------------------------
# Hashing — the ONE canonicalisation
# ---------------------------------------------------------------------------

def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(payload: Dict[str, Any]) -> str:
    """The one canonical JSON form: sorted keys, no spaces, real UTF-8."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def msg_line_hash(line: Dict[str, Any]) -> str:
    """The hash of ONE message line (裁决 C-2).

    The field set is ``seq``/``agent_id``/``session_id``/``role``/
    ``content``/``created_at``/``run_id``/``prev_hash``.  ``extras`` is
    DELIBERATELY excluded: it is derived metadata and the live code
    updates it after the row is written (``_attach_assistant_extras``),
    so hashing it would make every TTS annotation a chain break.

    ``content`` participates as the JSON TEXT it already is — it is not
    re-parsed and its keys are not re-ordered, so the hash of a line
    read back from disk is the hash of the line that was written.
    """
    payload = {
        "seq": line["seq"],
        "agent_id": line["agent_id"],
        "session_id": line["session_id"],
        "role": line["role"],
        "content": line["content"],
        "created_at": line["created_at"],
        "run_id": line["run_id"],
        "prev_hash": line["prev_hash"],
    }
    return "sha256:" + _sha256_hex(_canonical_json(payload))


def anchor_hash(anchor: Dict[str, Any]) -> str:
    """The hash of ONE anchor line (裁决 C-6, v2).

    Six fields, ``anchor_hash`` itself excluded — the same
    self-reference exclusion the message lines use.
    """
    payload = {k: anchor[k] for k in ANCHOR_HASH_FIELDS}
    return "sha256:" + _sha256_hex(_canonical_json(payload))


def compensation_hash(row: Dict[str, Any]) -> str:
    """The hash of ONE compensation row (裁决 C-2, v2).

    Same algorithm as ``msg_line_hash``, different field set — and the
    ``content`` key does not exist in a compensation row at all.
    """
    payload = {k: row[k] for k in COMPENSATION_HASH_FIELDS}
    return "sha256:" + _sha256_hex(_canonical_json(payload))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def export_root(workspace_root: Union[str, Path]) -> Path:
    """``<workspace>/memory/messages_export``."""
    return Path(workspace_root) / "memory" / EXPORT_DIRNAME


def audit_dir(workspace_root: Union[str, Path]) -> Path:
    """``<workspace>/memory/messages_export/.audit``."""
    return export_root(workspace_root) / AUDIT_DIRNAME


def anchor_path(workspace_root: Union[str, Path]) -> Path:
    """The chain-head anchor's own chain."""
    return audit_dir(workspace_root) / ANCHOR_FILENAME


def compensation_path(workspace_root: Union[str, Path]) -> Path:
    """The extras-compensation chain (its OWN file — never the main chain)."""
    return audit_dir(workspace_root) / COMPENSATION_FILENAME


def baseline_path(workspace_root: Union[str, Path]) -> Path:
    """The re-computed-hash baseline for M-2/M-3."""
    return audit_dir(workspace_root) / BASELINE_FILENAME


def session_filename(session_id: str, agent_id: str = "") -> str:
    """The JSONL file name of one session (裁决 C-1, v2).

    ``{normalize_slug(session_id)[:32]}-{sha256(agent_id|session_id)[:8]}.jsonl``

    The slug alone is NOT enough: ``naming.normalize_slug`` truncates to
    40 bytes, so two long session ids CAN wash out to the same slug.
    The sha256 prefix makes a collision impossible in practice (32 bits
    over a few dozen sessions) and the original ``session_id`` is stored
    on every line anyway — the file name is an alias, never an identity.

    **The agent_id is part of the hash, and that is an errata on the
    contract's literal wording (裁决 C-1 says ``sha256(session_id)``).**
    The reason is measured from the schema itself: the ``messages``
    table's uniqueness is ``(agent_id, session_id, seq)``, and the code
    that builds the table says so in as many words — "two Agents
    legitimately share a ``session_id`` (the same IM user talking to
    both)".  So the DB's "session" is the PAIR, and the truth source
    must be one file per PAIR.

    With the contract's literal rule, two agents sharing a
    ``session_id`` would write to the SAME file: their two ``seq``
    spaces would interleave in one chain, the chain's per-file ``seq``
    would repeat, and the DB's pointer would be ambiguous — a real
    corruption, in the one case the contract's own invariant ("不同
    session 必不同名") was meant to prevent.

    Measured on the ROOT library (2026-10-31): 22 session ids, 0
    shared between agents — so the defect is LATENT, not live.  It is
    closed here because it costs one hash input.
    """
    from agent.memory.naming import normalize_slug

    slug = normalize_slug(session_id)[:32] or "session"
    digest = hashlib.sha256(
        ("%s\x00%s" % (agent_id or "", session_id)).encode("utf-8")
    ).hexdigest()[:8]
    return "%s-%s.jsonl" % (slug, digest)


def session_relpath(session_id: str, batch_date: str,
                    agent_id: str = "") -> str:
    """The JSONL's path relative to ``memory/`` (POSIX separators).

    ``messages_export/<YYYY-MM-DD>/<session>.jsonl``
    """
    return "%s/%s/%s" % (EXPORT_DIRNAME, batch_date,
                         session_filename(session_id, agent_id))


def session_path(workspace_root: Union[str, Path], session_id: str,
                 batch_date: str, agent_id: str = "") -> Path:
    """The JSONL's absolute path."""
    return (Path(workspace_root) / "memory"
            / session_relpath(session_id, batch_date, agent_id))


# ---------------------------------------------------------------------------
# Building a message line
# ---------------------------------------------------------------------------

def build_msg_line(*, seq: int, agent_id: str, session_id: str, role: str,
                    content: str, created_at: int, run_id: str,
                    prev_hash: str, extras: str = "") -> Dict[str, Any]:
    """One message line, with its hash filled in.

    ``extras`` is carried on the line (so nothing is lost) but is NOT
    part of the hash — see ``msg_line_hash``.
    """
    line: Dict[str, Any] = {
        "seq": int(seq),
        "agent_id": agent_id or "",
        "session_id": session_id,
        "role": role,
        "content": content,
        "created_at": int(created_at),
        "run_id": run_id or "",
        "prev_hash": prev_hash,
        "line_hash": "",
    }
    if extras:
        line["extras"] = extras
    line["line_hash"] = msg_line_hash(line)
    return line


def read_msg_lines(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Every line of one JSONL, parsed.  Unparseable lines are skipped
    but COUNTED by the caller through :func:`read_msg_lines_with_errors`."""
    return read_msg_lines_with_errors(path)[0]


def read_msg_lines_with_errors(path: Union[str, Path]
                               ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The parsed lines plus the list of unparseable line descriptions."""
    out: List[Dict[str, Any]] = []
    errors: List[str] = []
    real = Path(path)
    if not real.is_file():
        return out, errors
    with open(real, encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as e:
                errors.append("line %d: unparseable (%s)" % (i, e))
    return out, errors


# ---------------------------------------------------------------------------
# Writing — append only, fsync, never truncate
# ---------------------------------------------------------------------------

def append_msg_line(workspace_root: Union[str, Path], session_id: str,
                    batch_date: str, line: Dict[str, Any]) -> Path:
    """Append ONE message line to its session's JSONL.

    The file is opened in ``"a"`` mode and fsynced.  A line that is
    already on disk is never rewritten — the chain is append-only, and
    so is the file.
    """
    path = session_path(workspace_root, session_id, batch_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return path


def append_anchor(workspace_root: Union[str, Path], *, rel: str, seq: int,
                  line_hash: str, action: str, at: str) -> Dict[str, Any]:
    """Append ONE anchor line to the chain-head anchor.

    The anchor is a chain of its own: each line carries the previous
    line's ``anchor_hash`` as its ``prev_anchor_hash``.
    """
    path = anchor_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = MSG_GENESIS_PREV_HASH
    if path.is_file():
        lines = read_msg_lines(path)
        if lines:
            prev = lines[-1].get("anchor_hash") or MSG_GENESIS_PREV_HASH
    anchor: Dict[str, Any] = {
        "rel": rel,
        "seq": int(seq),
        "line_hash": line_hash,
        "at": at,
        "action": action,
        "prev_anchor_hash": prev,
        "anchor_hash": "",
    }
    anchor["anchor_hash"] = anchor_hash(anchor)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(anchor, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return anchor


def append_compensation(workspace_root: Union[str, Path], *,
                         agent_id: str, session_id: str, target_seq: int,
                         extras: Dict[str, Any], at: str) -> Dict[str, Any]:
    """Append ONE extras-compensation row to its OWN chain.

    The compensation chain lives in ``.audit/extras_compensations.jsonl``
    and never mixes with the main chain — a main chain that carried two
    line shapes would force every criterion to branch on ``kind``, and
    a criterion that branches is a criterion that can be wrong in two
    ways instead of one.
    """
    path = compensation_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = MSG_GENESIS_PREV_HASH
    seq = 1
    if path.is_file():
        lines = read_msg_lines(path)
        if lines:
            prev = lines[-1].get("line_hash") or MSG_GENESIS_PREV_HASH
            seq = int(lines[-1].get("seq") or 0) + 1
    row: Dict[str, Any] = {
        "kind": COMPENSATION_KIND,
        "seq": seq,
        "agent_id": agent_id or "",
        "session_id": session_id,
        "target_seq": int(target_seq),
        "extras": extras,
        "at": at,
        "prev_hash": prev,
        "line_hash": "",
    }
    row["line_hash"] = compensation_hash(row)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return row


# ---------------------------------------------------------------------------
# Reading — the ONE entry point (裁决 C-12)
# ---------------------------------------------------------------------------

def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read one column from a dict OR a sqlite3.Row."""
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row.get(key, default)  # type: ignore[union-attr]
        except AttributeError:
            return default
    return default if value is None else value


class ContentReader:
    """Reads message text out of the truth source — one pass per file.

    裁决 C-12's performance rule: a single ``load_messages(max_turns=30)``
    can back-fill 1,866 rows, and opening the file once per row would
    turn ONE read into 1,866 reads.

    **How the rule is met.**  The first time a file is needed, it is
    read ONCE — line by line — into a map keyed by the physical line
    number.  Every later row that points into that file is a lookup.
    The naive alternative ("open the file, find the line I want") opens
    the file once PER ROW, because the second row wants a different
    line of the same file — that is the shape the contract forbids.

    **The memory shape, stated plainly.**  The map holds the file's
    TEXT, so the reader's footprint for one session is the size of
    that session's messages.  That is the SAME order of magnitude the
    pre-externalisation database read had (measured on the ROOT
    library: the biggest session is 33.7 MB of message text) — the
    externalisation moves the bytes from the FILE to the JSONL, it does
    not pretend the process does not need them.

    The reader lives for ONE call.  The truth source is append-only, so
    a cache that outlived the call would serve a line that has since
    been followed by another one — harmless for the content, wrong for
    the anchor — and the whole point of the cache is to avoid reading
    the same bytes twice inside one operation.
    """

    def __init__(self, workspace_root: Union[str, Path]) -> None:
        self._root = Path(workspace_root)
        self._files: Dict[str, Dict[int, str]] = {}

    def _lines_of(self, rel: str) -> Dict[int, str]:
        """Every physical line of one file, read in ONE pass."""
        cached = self._files.get(rel)
        if cached is not None:
            return cached
        out: Dict[int, str] = {}
        path = self._root / "memory" / rel
        if path.is_file():
            with open(path, encoding="utf-8") as f:
                for i, raw in enumerate(f, start=1):
                    out[i] = raw.rstrip("\n")
        self._files[rel] = out
        return out

    def text_of(self, rel: str, anchor: str) -> str:
        """The ``content`` FIELD of the line the anchor points at.

        Not the whole line: the line is the chain's record, and the
        content is what the readers want.  The line's ``content`` field
        is the JSON TEXT the database used to hold — so it is handed
        back exactly, and the reader's ``json.loads`` sees what it saw
        before the externalisation.
        """
        n = parse_line_anchor(anchor)
        lines = self._lines_of(rel)
        if n not in lines:
            if not (self._root / "memory" / rel).is_file():
                raise MsgChainError("the truth file %s does not exist" % (rel,))
            raise MsgChainError("the truth file %s has no line %d" % (rel, n))
        return content_of_line(lines[n])

    def close(self) -> None:
        """Drop the cached files.  The reader is disposable."""
        self._files.clear()

    def hash_of(self, rel: str, anchor: str) -> str:
        """The RE-COMPUTED hash of the line the anchor points at.

        This is the third comparison of 裁决 §8.2: the anchor's claim
        and the file's SELF-REPORTED hash are two claims, and the
        reader computes the truth itself.
        """
        n = parse_line_anchor(anchor)
        lines = self._lines_of(rel)
        if n not in lines:
            return ""
        return _hash_of_line_text(lines[n])


def content_of_line(raw: str) -> str:
    """The ``content`` field of one raw JSONL line.

    The line is the chain's record; the content is the text the
    database used to hold.  The content field is itself JSON TEXT (the
    database stored ``json.dumps(content)``), so it is returned as the
    string it is — the reader's own ``json.loads`` then sees exactly
    what it saw before the externalisation.

    A line that is not a JSON object, or that has no ``content``, is a
    defect in the truth source — so it RAISES rather than returning an
    empty string.  An empty string here would be the "the user never
    said anything" lie.
    """
    try:
        line = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise MsgChainError("the truth line is not parseable JSON: %s" % e)
    if not isinstance(line, dict):
        raise MsgChainError(
            "the truth line is a %s, not an object" % type(line).__name__)
    if "content" not in line:
        raise MsgChainError("the truth line has no 'content' field")
    content = line["content"]
    if content is None:
        raise MsgChainError("the truth line's 'content' is NULL")
    if not isinstance(content, str):
        raise MsgChainError(
            "the truth line's 'content' is a %s, not the JSON TEXT the "
            "database stored" % type(content).__name__)
    return content


def parse_line_anchor(anchor: str) -> int:
    """The physical line number an anchor names.

    The anchor is ``line:<n>`` — a 1-based PHYSICAL line number, not a
    byte offset and not a message ``seq``.  (裁决 C-8: messages do NOT
    reuse chunks' ``L<start>-<end>+S...`` grammar; a message is exactly
    one line, so a line number says everything there is to say.)

    **The name is not ``parse_anchor`` on purpose.**  The chunks side
    already has a ``parse_anchor`` (a different grammar, a different
    meaning), and an implementation-side test asserts that there is
    EXACTLY ONE anchor parser in the whole tree — "two parsers for one
    grammar is how 'the anchor points nowhere' happens while both sides
    believe they agree".  The messages side has its own grammar, so it
    has its own name.
    """
    if not isinstance(anchor, str) or not anchor.startswith("line:"):
        raise MsgChainError(
            "content_anchor %r is not the 'line:<n>' form" % (anchor,))
    try:
        n = int(anchor.split(":", 1)[1])
    except (IndexError, ValueError):
        raise MsgChainError("content_anchor %r has no line number" % (anchor,))
    if n < 1:
        raise MsgChainError("content_anchor %r is not 1-based" % (anchor,))
    return n


def load_content(row: Any, workspace_root: Union[str, Path],
                  *, reader: Optional[ContentReader] = None
                  ) -> str:
    """The message's text — from the DB or from the JSONL.

    Dispatched on ``content_state`` (裁决 C-12):

    * ``inline`` — the text is in the row.  A ``NULL`` here is
      ``ContentMissingError`` (an illegal state, not an empty message).
    * ``external`` — the text is in the JSONL at ``content_path`` /
      ``content_anchor``.  The line is read, its hash RECOMPUTED and
      compared with ``content_hash``: a mismatch is ``ContentDriftError``.
    * ``pending`` — the crash window.  ``ContentPendingError``; the
      inspector owns the recovery.

    Every failure RAISES.  It never returns an empty string and never
    returns ``None``: an empty return would be indistinguishable from a
    legitimately empty message, which is the silent failure this whole
    design is built to prevent.
    """
    state = _row_get(row, "content_state", STATE_INLINE) or STATE_INLINE
    content = _row_get(row, "content", None)

    if state == STATE_INLINE:
        if content is None:
            raise ContentMissingError(
                "message row has content IS NULL with content_state='inline' "
                "— the text was never externalised, or it was lost")
        return content

    if state == STATE_PENDING:
        raise ContentPendingError(
            "message row is in content_state='pending' — the JSONL may hold "
            "the text but the DB was never emptied; the inspector must "
            "recover this row")

    if state != STATE_EXTERNAL:
        raise MsgChainError(
            "unknown content_state %r — the three states are inline / "
            "external / pending" % (state,))

    rel = _row_get(row, "content_path", None)
    anchor = _row_get(row, "content_anchor", None)
    expected = _row_get(row, "content_hash", None)
    if not rel or not anchor:
        raise ContentMissingError(
            "message row says content_state='external' but carries no "
            "content_path/content_anchor (%r / %r)" % (rel, anchor))

    rd = reader if reader is not None else ContentReader(workspace_root)
    text = rd.text_of(str(rel), str(anchor))

    # **The drift check.**  A reader that returns the text without
    # verifying it cannot tell a moved file from a good one, and a
    # moved file is exactly the failure this chain exists to catch.
    #
    # The token is the LINE's hash — the whole record, not just the
    # content — because the line is what the chain protects: a row
    # whose ``role`` was rewritten is as much a drift as a row whose
    # text was.
    if expected:
        line_hash = rd.hash_of(str(rel), str(anchor))
        if line_hash and line_hash != expected:
            raise ContentDriftError(
                "message content drifted: %s %s hashes to %s but the row "
                "records %s" % (rel, anchor, line_hash, expected))
    return text


def _hash_of_line_text(text: str) -> str:
    """The hash of a JSONL line's OWN payload, re-computed.

    The reader does not know the line's other columns (they are in the
    file), so the recomputation happens where the file is read.
    """
    try:
        line = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(line, dict):
        return ""
    try:
        return msg_line_hash(line)
    except KeyError:
        return ""


def load_contents(rows: Iterable[Any], workspace_root: Union[str, Path],
                  ) -> List[str]:
    """The texts of many rows, read in ONE pass per file.

    The rows are grouped by ``content_path``; each file is opened once
    and the wanted lines are located inside that single pass.
    """
    rd = ContentReader(workspace_root)
    return [load_content(row, workspace_root, reader=rd) for row in rows]


# ---------------------------------------------------------------------------
# The chain's own verification — used by the criteria
# ---------------------------------------------------------------------------

def verify_msg_file(path: Union[str, Path]) -> List[str]:
    """Every violation of ONE JSONL's chain.  Empty list = clean.

    Checks: the ``prev_hash`` linkage, the genesis, the re-computed
    ``line_hash``, and the presence of every key the line shape
    requires.
    """
    violations: List[str] = []
    lines, errors = read_msg_lines_with_errors(path)
    violations.extend(errors)
    prev = MSG_GENESIS_PREV_HASH
    for i, line in enumerate(lines, start=1):
        missing = [k for k in MSG_FIELD_ORDER if k not in line]
        if missing:
            violations.append("line %d: missing key(s) %s"
                              % (i, ",".join(missing)))
            prev = line.get("line_hash", prev)
            continue
        if line.get("prev_hash") != prev:
            violations.append("prev_hash break at line %d" % i)
        try:
            if line.get("line_hash") != msg_line_hash(line):
                violations.append("line_hash mismatch at line %d" % i)
        except (KeyError, TypeError) as e:
            violations.append("line %d: cannot hash the line (%s)" % (i, e))
        prev = line.get("line_hash", prev)
    return violations


def verify_anchor(workspace_root: Union[str, Path]) -> List[str]:
    """Every violation of the chain-head anchor.  Empty list = clean.

    Four checks, matching 裁决 C-5's M-4:

    1. every registered JSONL has an anchor row;
    2. the anchor's ``line_hash`` equals the JSONL's last line's
       ``line_hash``;
    3. the anchor's ``seq`` equals the JSONL's last line's ``seq``;
    4. the anchor chain is self-consistent.

    And the THREE-WAY comparison of 裁决 §8.2: the anchor's claim is
    checked against the JSONL's SELF-REPORTED hash AND against the
    RE-COMPUTED hash.  Comparing only the self-reported one is a false
    green — an attacker who rewrites the last line and its claim passes
    it.
    """
    violations: List[str] = []
    root = Path(workspace_root)
    apath = anchor_path(root)
    anchors = read_msg_lines(apath) if apath.is_file() else []
    by_rel: Dict[str, Dict[str, Any]] = {}
    for a in anchors:
        rel = a.get("rel")
        if rel:
            by_rel[rel] = a

    prev = MSG_GENESIS_PREV_HASH
    for i, a in enumerate(anchors, start=1):
        if a.get("prev_anchor_hash") != prev:
            violations.append("anchor chain break at line %d" % i)
        try:
            if a.get("anchor_hash") != anchor_hash(a):
                violations.append("anchor_hash mismatch at line %d" % i)
        except (KeyError, TypeError) as e:
            violations.append("anchor line %d: cannot hash the anchor (%s)"
                              % (i, e))
        prev = a.get("anchor_hash", prev)

    for rel, a in by_rel.items():
        real = root / "memory" / rel
        if not real.is_file():
            violations.append("the anchor names %s but the file is gone" % rel)
            continue
        lines, errors = read_msg_lines_with_errors(real)
        if errors:
            violations.append("%s: %s" % (rel, errors[0]))
        if not lines:
            violations.append("%s: the anchored file has no lines" % rel)
            continue
        last = lines[-1]
        if a.get("line_hash") != last.get("line_hash"):
            violations.append("anchor mismatch: %s" % rel)
        if int(a.get("seq") or -1) != int(last.get("seq") or -2):
            violations.append("anchor seq mismatch: %s" % rel)
        # The THIRD comparison: the re-computed hash of the last line.
        try:
            recomputed = msg_line_hash(last)
        except (KeyError, TypeError):
            recomputed = ""
        if recomputed and recomputed != a.get("line_hash"):
            violations.append("anchor drift: %s" % rel)
    return violations
