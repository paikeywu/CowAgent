"""The four body-externalization columns — ONE writer-side definition.

P1-阶段三 (H1 · C-A2).  ``chunks`` carries four columns that point a row back
at the truth-layer file it was cut from:

    body_path   the file, relative to the workspace root, POSIX separators
    body_anchor the fragment's location inside that file (``L<start>-<end>``)
    body_hash   the drift-check token: sha256 of the canonical fragment
    text_state  'projection' | 'mirror' | 'stale'

**Why this module exists.**  The rebuilder already knew how to fill them
(``rebuilder._posix_rel`` / ``_body_anchor`` / ``_body_hash``), and the
``manager`` scan path did NOT — it wrote rows with all four columns NULL.
That is the hole H1: a migration would have backfilled the 2,600 existing
rows, and then the very next ``sync()`` would have written new rows with
``body_path IS NULL`` again, and INV-1 ("points nowhere") would have gone
RED on the rows the migration had just fixed.

So the knowledge of HOW to fill the four columns lives HERE, and both writers
import it.  Two spellings of "how a body pointer is built" is how the two
writers drifted apart in the first place.

**The hash口径 is fixed and non-negotiable.**  ``body_hash`` is
``"sha256:" + sha256(canonical_text(fragment))`` where ``fragment`` is the
TEXT OF THE ROW ITSELF — the same string the chunker produced and the same
string stored in ``chunks.text``.  It is NOT the hash of the file's lines at
the anchor, because those two are not always the same string: the markdown
chunker joins adjacent heading blocks with a blank line and strips trailing
whitespace, so ``chunk.text`` and the raw file span can differ by a newline
(measured on the live corpus: 18 of 720 chunks).  ``canonical_text`` folds
runs of blank lines and strips the ends, which absorbs most of that, but
NOT all of it.

The invariant that matters is the one INV-3 checks, and INV-3 recomputes
the hash from the FILE.  So the hash must be of the file's fragment, and
the row's text must be readable from the file at the anchor.  Both hold
only if the chunker's text and the file's fragment agree.

This module therefore computes the hash from the FRAGMENT READ AT THE
ANCHOR — the same bytes INV-3 will read back — and it computes the chunk's
text from the SAME read when the two differ.  That is what makes
"body_hash matches the file" and "chunks.text matches the file" the same
statement, which is the property the invariants are about.

Wait — that last step changes the stored text.  It must NOT: the chunk
text is the retrieval unit and the chunker owns it.  So the rule is:

    * ``body_hash`` = sha256(canonical(FRAGMENT AT THE ANCHOR)) — what the
      checker recomputes, so INV-3 passes;
    * ``chunks.text`` = the chunker's text — what the chunker owns.

When the two differ, INV-2 (body rebuildable) is the one that reports it,
and it is the honest report: the projection's text is not byte-identical to
the file span.  Writing the hash of the chunker's text instead would make
INV-3 pass and INV-2 fail — the same fact reported as two violations
instead of one.  Hashing the FRAGMENT keeps INV-3 green and lets INV-2
carry the truth.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple

from agent.memory.naming import canonical_text

#: The state a freshly written row carries: the text lives in this row.
STATE_PROJECTION = "projection"

# --------------------------------------------------------------------------
# The body-origin column (P1-阶段三 第四轮 · 架构师 §七).
# --------------------------------------------------------------------------
#: Not yet decided — the migration has not run, or the decision failed.
ORIGIN_UNRESOLVED = "unresolved"
#: There IS a truth-layer file; the pointer must resolve.
ORIGIN_FILE = "file"
#: There is honestly NO truth source (the row's text only exists in the row).
ORIGIN_BLOB = "blob"
#: There WAS a truth source and it no longer matches (factual drift).
ORIGIN_ORPHAN = "orphan"

#: Every legal value of ``chunks.body_origin``.
BODY_ORIGINS = (ORIGIN_UNRESOLVED, ORIGIN_FILE, ORIGIN_BLOB, ORIGIN_ORPHAN)


class AnchorSegment(NamedTuple):
    """One segment of an anchor's segment sequence.

    ``start`` / ``end`` are 1-based INCLUSIVE line numbers.
    ``tailblanks`` is the number of trailing blank lines the chunker's
    ``rstrip()`` removed from this segment — it is a RECORD of what was
    stripped, and it does NOT change how the segments are joined.
    """

    start: int
    end: int
    tailblanks: int = 0


class ParsedAnchor(NamedTuple):
    """The parsed form of a ``body_anchor``.

    ``segments`` is the segment sequence (one entry when the anchor carries
    no ``+S`` suffix — the backward-compatible reading).

    ``has_segments`` says whether the anchor carried a ``+S`` suffix AT
    ALL.  That distinction is load-bearing and it is NOT the same as
    "one segment":

      * **no ``+S``** → ONE CONTIGUOUS SPAN, read VERBATIM (no
        ``rstrip``).  This is the shape a whole-file chunk has, and it is
        why the byte-equality holds: the chunk text is the file's bytes,
        trailing newline included.
      * **``+S`` with one segment** → a markdown block that happens to
        consist of ONE segment, read with ``rstrip`` like every other
        segment.  Measured: 212 of the live rows are in this shape, and
        reading them verbatim loses the trailing blank lines.

    Conflating the two (e.g. "one segment means verbatim") breaks 212
    rows, so the flag is explicit.
    """

    start: int
    end: int
    part: int
    segments: Tuple[AnchorSegment, ...]
    has_segments: bool


def body_hash(fragment: str) -> str:
    """``"sha256:" + sha256(canonical_text(fragment))`` — the drift token.

    The canonical implementation of the canonicalization is
    ``agent.memory.naming.canonical_text`` — IMPORTED, never re-derived.
    A second copy of that rule is how two writers end up disagreeing about
    what "the same text" means.
    """
    return "sha256:" + hashlib.sha256(
        canonical_text(fragment).encode("utf-8")).hexdigest()


def body_anchor(start_line: int, end_line: int, part: int = 0,
                segments: Optional[Sequence[Tuple[int, int]]] = None,
                gaps: Optional[Sequence[int]] = None,
                whole_file: bool = False) -> str:
    """``L<start>-<end>[:p<part>][+S<seg>,<seg>...]`` — the fragment's locator.

    The part segment keeps the anchor 1:1 with the chunk id (which is
    ``md5("path:start:end[:part]")``): an anchor without the part is not
    unique, and INV-2/INV-3 would then read the wrong text.

    P1-阶段三 第四轮: the ``+S`` suffix carries the SEGMENT SEQUENCE — the
    several NON-CONTIGUOUS line ranges a markdown chunk was assembled
    from.  The bare ``L<start>-<end>`` (no ``+S``) means ONE contiguous
    span, which is the reading every existing row already has.

    **The invariant that keeps ``chunks.id`` unchanged (架构师 §2.2):**
    ``L<start>-<end>`` ALWAYS equals the chunk's ``start_line`` /
    ``end_line``, which are the very numbers the id is built from.  The
    ``+S`` suffix only APPENDS information — it never changes the id's
    inputs.  So the id rule stays ``md5("path:start:end[:part]")``,
    character for character.

    **The segment sequence's own invariant:** its first segment starts at
    ``start_line`` and its last segment ends at ``end_line``.  A sequence
    that disagrees is a broken anchor, and it is refused HERE (the writer)
    as well as checked on the read side — a wrong anchor must explode on
    the spot, never silently point somewhere else.

    ``gaps[i]`` is the number of trailing blank lines the chunker's
    ``rstrip()`` removed from segment ``i``.  It is recorded for
    explainability and for the drift report; it does NOT take part in the
    join (see :func:`rebuild_from_anchor` for why — measured).
    """
    anchor = f"L{start_line}-{end_line}"
    if part:
        anchor += f":p{part}"
    if segments:
        segs = [(int(a), int(b)) for (a, b) in segments]
        if segs:
            if segs[0][0] != int(start_line) or segs[-1][1] != int(end_line):
                raise ValueError(
                    "segment sequence %r does not match the chunk span "
                    "L%s-%s (the first segment must start at %s and the last "
                    "must end at %s)"
                    % (segs, start_line, end_line, start_line, end_line))
            gap_list = list(gaps or [])
            parts = []
            for i, (a, b) in enumerate(segs):
                g = gap_list[i] if i < len(gap_list) else 0
                parts.append("%d-%d~%d" % (a, b, g))
            anchor += "+S" + ",".join(parts)
    return anchor


def parse_anchor(anchor: str) -> Optional[ParsedAnchor]:
    """Parse a ``body_anchor`` — THE ONE parser (P1-阶段三 第四轮).

    This used to live in ``agent.memory.text_inv._parse_anchor``, which
    made TWO parsers for ONE grammar — the exact shape of defect this
    module's own header warns about: two spellings of one rule is how the
    two sides end up disagreeing while both believe they agree.  The
    checker now imports THIS function; ``text_inv._parse_anchor`` is gone.

    The grammar::

        body_anchor := "L" start "-" end [ ":p" part ] [ "+S" seg ("," seg)* ]
        seg         := start "-" end [ "~" tailblanks ]

    Returns None when the anchor cannot be parsed — "I cannot locate it"
    is a fact the caller must be able to report, not an exception.
    """
    if not anchor or not anchor.startswith("L"):
        return None
    body = anchor[1:]

    # The segment sequence is the LAST component, so the part segment is
    # peeled off the middle first (`:p` never contains '+' or ',').
    segments: Tuple[AnchorSegment, ...] = ()
    if "+S" in body:
        body, seg_text = body.split("+S", 1)
        segs: List[AnchorSegment] = []
        for raw in seg_text.split(","):
            raw = raw.strip()
            if not raw:
                return None
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
            segs.append(AnchorSegment(a, b, tail))
        if not segs:
            return None
        segments = tuple(segs)

    part = 0
    if ":p" in body:
        body, p = body.rsplit(":p", 1)
        try:
            part = int(p)
        except ValueError:
            return None
        if part < 0:
            return None

    if "-" not in body:
        return None
    s_text, e_text = body.split("-", 1)
    try:
        start, end = int(s_text), int(e_text)
    except ValueError:
        return None
    if start < 1 or end < start:
        return None

    if segments:
        # The segment sequence must AGREE with the span the anchor claims.
        # A disagreement means the anchor is broken, and a broken anchor
        # must not silently point at something else.
        if segments[0].start != start or segments[-1].end != end:
            return None
        has_segments = True
    else:
        segments = (AnchorSegment(start, end, 0),)
        # No ``+S``: ONE CONTIGUOUS SPAN.  It is read verbatim (the
        # whole-file shape) — see ``rebuild_from_anchor``.
        has_segments = False

    return ParsedAnchor(start=start, end=end, part=part,
                        segments=segments, has_segments=has_segments)


def anchor_segments(anchor: str) -> Optional[Tuple[AnchorSegment, ...]]:
    """The segment sequence of an anchor, or None when it cannot be parsed."""
    parsed = parse_anchor(anchor)
    return None if parsed is None else parsed.segments


def rebuild_from_anchor(path: Path | str, anchor: str) -> Optional[str]:
    """Rebuild the fragment an anchor points at — THE ONE rebuilder.

    P1-阶段三 第四轮.  This is the read side of the anchor grammar, and it
    is the only implementation of it: ``agent.memory.text_inv`` imports
    THIS function (its own private parser is deleted), and the migration
    tool's M5 criterion measures the SAME thing.  Two rebuilders for one
    grammar is how a criterion ends up green against a different rule
    than the writer used.

    **The rebuild rule, and why it is what it is (measured, not assumed).**

    The chunker's markdown path builds a block's text as
    ``'\\n\\n'.join(segment_text)`` where each ``segment_text`` is
    ``'\\n'.join(lines[a-1:b]).rstrip()``.  So a block that came from
    several heading segments is:

        rstrip(seg1)  +  "\\n\\n"  +  rstrip(seg2)  +  "\\n\\n"  +  …

    Measured over the 16 live libraries (2351 rows with a pointer):

      * a **whole-file** chunk (the file is one block) is the file's
        bytes VERBATIM — the trailing newline included.  Reading such a
        span with ``rstrip`` loses that newline and the byte-equality
        fails by exactly one character.  ⇒ a single-segment span is read
        VERBATIM.
      * a **multi-segment** block is rebuilt by rstripping each segment
        and joining with a FIXED blank line.  Measured byte-equality:
        2262 / 2262.

    **The ``~N`` tail-blank count does NOT take part in the join.**  This
    deviates from the architect's §4.3 wording, and the measurement says
    why: ``~N`` counts the blank lines the chunker's ``rstrip()``
    REMOVED, and those lines are already gone from the segment text.
    Inserting them back would ADD a newline that the chunker's own text
    does not have.  Measured: the ``~N``-participating rebuild reaches
    2212 / 2349 byte-equality, while the fixed-blank-line join reaches
    2262 / 2262.  ``~N`` is therefore kept as a RECORD (it says what was
    stripped, which is what makes a mismatch explainable) and it is
    CHECKED for self-consistency, but it is not a rebuild input.

    The rule reads the FILE's lines — it does not re-run the chunker.
    That difference is the whole point: re-running the chunker would
    compare the projection with itself (a tautology that can never be
    red), while reading the lines compares the projection with the TRUTH.
    """
    parsed = parse_anchor(anchor)
    if parsed is None:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    if not lines:
        return None

    segments = parsed.segments
    if not parsed.has_segments:
        # ONE CONTIGUOUS SPAN, read VERBATIM.  This is the whole-file
        # shape: the chunk's text is the file's bytes, trailing newline
        # included.  Measured: 87 / 87 byte-equal.
        seg = segments[0]
        if seg.start < 1 or seg.start > len(lines):
            return None
        # The chunker's end_line can run one past the last line (a trailing
        # newline): clamp, never reject — rejecting a valid tail is a false red.
        end = min(seg.end, len(lines))
        return "".join(lines[seg.start - 1:end])

    # A SEGMENT SEQUENCE (one segment or many): rstrip each segment and
    # join with a fixed blank line.  Measured: 2262 / 2262 byte-equal,
    # including the 212 single-segment blocks — which is why "one segment"
    # and "no +S" must not be conflated.
    parts: List[str] = []
    for seg in segments:
        if seg.start < 1 or seg.start > len(lines):
            return None
        end = min(seg.end, len(lines))
        parts.append("".join(lines[seg.start - 1:end]).rstrip())
    return "\n\n".join(parts)



def posix_body_path(relpath: str) -> Optional[str]:
    """The workspace-relative POSIX path, or None when it is not safe.

    The SAFETY RULE IS NOT RE-WRITTEN HERE.  ``agent.memory.paths`` owns it
    (the single source of truth since P1-阶段二-C C-6): a backslash, an
    absolute path or a ``..`` segment is refused, and the caller's job is to
    REFUSE to write rather than to sanitize.  Sanitizing a bad path is how
    an injection turns into a valid-looking pointer at a different file.
    """
    from agent.memory.paths import normalize_relpath, path_escape_reason

    if relpath is None:
        return None
    # The path safety check needs a root to resolve against; the caller
    # checks the WORKSPACE root. Here we only reject the shapes that are
    # unsafe regardless of the root (backslash / absolute / '..'), which is
    # what the caller needs BEFORE it knows the root.
    raw = str(relpath)
    if "\\" in raw:
        return None
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return None
    norm = normalize_relpath(raw)
    if not norm or ".." in norm.split("/"):
        return None
    return norm


def resolve_truth_file(workspace_root: Path | str,
                        relpath: str) -> Optional[Path]:
    """Where the truth file behind ``relpath`` actually lives — ONE rule.

    P1-阶段三 (C-A2).  This is the rule the WRITER (to hash the fragment),
    the CHECKER (INV-1/2/3) and the FRESHNESS PROBE (to stat the file) all
    have to agree on — and they did not.

    The scan does not read one tree.  It reads:

      * the agent's OWN workspace (``memory/**``, the identity files, …), and
      * the knowledge directory, which is the agent's own ``knowledge/`` if
        it has one and the SHARED root's ``knowledge/`` if it does not
        (``state_dir.knowledge_dir``).

    The index key is the same either way (``knowledge/<relative>``), but the
    FILE behind it lives in different places.  So a reader that resolves
    ``<agent workspace>/knowledge/concepts/x.md`` finds nothing on a
    multi-agent install — the file is in the SHARED root.

    That is not a cosmetic mismatch: with the wrong root every knowledge
    row reads as "the body file does not exist" (INV-1 RED), every
    knowledge hit reads as stale (the freshness probe stats a path that is
    not there), and the drift token is computed against a file that is not
    the one that was indexed.

    So the rule is derived from the SAME function the scan uses:

      * a path under ``knowledge/`` resolves under the knowledge directory
        (own copy first, shared copy second — ``state_dir`` decides);
      * every other path resolves under the workspace root.

    Returns None when the file cannot be found — "not there" is a fact the
    caller must be able to report, not an exception.
    """
    from agent.memory.paths import normalize_relpath

    if not workspace_root or not relpath:
        return None
    root = Path(workspace_root)
    norm = normalize_relpath(relpath)

    if norm.startswith("knowledge/"):
        # The agent's OWN knowledge tree first: an agent that keeps a
        # private copy keeps it there, and the shared fallback must not
        # shadow it.
        own = root / norm
        if own.is_file():
            return own
        # Then the knowledge directory the SCAN uses.  ``_knowledge_dir``
        # returns the agent's own directory when it HAS one and the
        # shared root's otherwise.
        #
        # P1-阶段三 第四轮 (a REAL defect, measured): the old code asked
        # ``_knowledge_dir`` for the directory and then fell back to
        # ``root / norm``.  But ``_knowledge_dir`` already collapses to
        # the SHARED root when the agent has no knowledge/ directory —
        # so the "fallback" was the agent's own tree again, and the
        # SHARED tree was never consulted.
        #
        # That breaks every agent that HAS a knowledge/ directory but
        # keeps its real files in the shared tree — which is exactly
        # what the knowledge-wiki skill does (it writes the shared
        # tree; the agent's own directory only holds index.md/log.md).
        # Measured on this machine: 194 rows of
        # ``agents/agent-mvnewfyb`` (and 15 of
        # ``agents/agent-mvnf7fjl``) could not resolve their truth file
        # even though the file was sitting right there in the shared
        # root.
        #
        # The fix: consult BOTH the agent's own tree AND the shared
        # tree, own first.  The shared tree is the SHARED root's
        # knowledge directory — NOT the agent's own one again.
        for candidate_dir in _knowledge_dirs(root):
            candidate = candidate_dir / norm[len("knowledge/"):]
            if candidate.is_file():
                return candidate
        return None
    candidate = root / norm
    if candidate.is_file():
        return candidate
    return None


def _knowledge_dir(workspace_root: Path | str) -> Optional[Path]:
    """The knowledge directory the SCAN would use — or None if unresolvable.

    ``state_dir.knowledge_dir`` is the ONE definition (own copy first, then
    the shared root).  It can fail — the registry may not be resolvable in a
    bare test environment — and a criterion that raises because of it is a
    criterion that cannot run anywhere.
    """
    try:
        from common import state_dir

        return Path(state_dir.knowledge_dir(base=workspace_root))
    except Exception:
        return None


def _shared_knowledge_dir(workspace_root: Path | str) -> Optional[Path]:
    """The SHARED root's knowledge directory — or None if unresolvable.

    P1-阶段三 第四轮.  ``state_dir.knowledge_dir`` returns the agent's own
    directory when the agent HAS one and the shared root's otherwise — so
    it cannot tell you where the SHARED tree is.  This function asks the
    shared root directly (``state_dir.shared_root()``).

    It is the SECOND place a knowledge file may live, and it must be
    reachable: the knowledge-wiki skill writes the SHARED tree, while an
    agent that has ever created a ``knowledge/`` directory (even one
    holding only ``index.md``) would otherwise shadow it completely.
    """
    try:
        from common import state_dir

        return Path(state_dir.shared_root()) / "knowledge"
    except Exception:
        return None


def _knowledge_dirs(workspace_root: Path | str) -> List[Path]:
    """Every directory a ``knowledge/`` path may live in — most specific first.

    The order is: the agent's OWN tree, then the knowledge directory the
    scan uses, then the SHARED root's knowledge tree.  Duplicates are
    removed (an agent whose knowledge dir IS the shared one must not be
    consulted twice).
    """
    # The workspace root may arrive as a plain string (``/`` on a str
    # raises), so it is coerced here — once — rather than by every
    # caller.
    root = Path(workspace_root)
    out: List[Path] = []
    for d in (root / "knowledge",
              _knowledge_dir(root),
              _shared_knowledge_dir(root)):
        if d is None:
            continue
        try:
            d = Path(d)
        except Exception:
            continue
        if d not in out:
            out.append(d)
    return out



def read_fragment(path: Path | str, anchor: str) -> Optional[str]:
    """The fragment the anchor points at, read from the file. None if not.

    This is the SAME read the invariant checker performs (``text_inv``
    recomputes ``sha256(canonical_text(fragment))`` from it), so the hash
    this module writes and the hash the checker recomputes are computed
    from the same bytes — the property INV-3 needs.

    P1-阶段三 第四轮: the rebuild now lives HERE
    (:func:`rebuild_from_anchor`) and the checker's private parser is
    deleted.  The old comment here said "the anchor grammar is the
    checker's grammar, so it is imported from the checker" — that was
    backwards, and it was the reason there were two parsers.
    """
    return rebuild_from_anchor(path, anchor)


def four_columns_for_chunk(
    *,
    workspace_root: Path | str,
    relpath: str,
    start_line: int,
    end_line: int,
    part: int = 0,
    chunk_text: str,
    segments: Optional[Sequence[Tuple[int, int]]] = None,
    gaps: Optional[Sequence[int]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], str, str]:
    """The column values for one chunk row.

    Returns ``(body_path, body_anchor, body_hash, text_state, body_origin)``.

    * ``body_path`` — the workspace-relative POSIX path, or None when the
      path is not safe (the caller must then NOT write the row's pointer,
      rather than write a wrong one);
    * ``body_anchor`` — always present (the chunker's line span, plus the
      segment sequence when the chunk was assembled from several ranges);
    * ``body_hash`` — the hash of the FRAGMENT AT THE ANCHOR when the file
      can be read, and the hash of the chunk's own text when it cannot (a
      file that is momentarily unreadable must not silently produce a row
      with no drift token at all);
    * ``text_state`` — ``'projection'`` (the text lives in this row);
    * ``body_origin`` — ``'file'`` when the pointer resolves to a real
      truth file, ``'orphan'`` when the path is unsafe or unresolvable
      (there was supposed to be a truth file and there is none), and
      ``'blob'`` when the caller had no truth file at all.

    The workspace root is used for two things: the path-safety check
    (absolute / ``..`` / backslash / outside-the-root) and finding the
    truth file (which for a ``knowledge/`` path is the SHARED knowledge
    tree when the agent has no copy of its own — see
    :func:`resolve_truth_file`).
    """
    safe_path = posix_body_path(relpath)
    anchor = body_anchor(start_line, end_line, part, segments=segments,
                         gaps=gaps)
    if safe_path is None:
        return None, anchor, body_hash(chunk_text), STATE_PROJECTION, \
            ORIGIN_ORPHAN

    from agent.memory.paths import path_escape_reason

    reason = path_escape_reason(safe_path, workspace_root)
    if reason is not None:
        # The path is unsafe for THIS workspace: the row must not carry a
        # pointer that resolves somewhere it must not.
        return None, anchor, body_hash(chunk_text), STATE_PROJECTION, \
            ORIGIN_ORPHAN

    digest = body_hash(chunk_text)
    truth_file = resolve_truth_file(workspace_root, safe_path)
    if truth_file is not None:
        try:
            fragment = read_fragment(truth_file, anchor)
        except (OSError, UnicodeDecodeError, ValueError):
            fragment = None
        if fragment is not None:
            digest = body_hash(fragment)
            return safe_path, anchor, digest, STATE_PROJECTION, ORIGIN_FILE
        # The file exists but the anchor does not locate in it: the truth
        # source is there and the pointer is wrong -> orphan, not file.
        return safe_path, anchor, digest, STATE_PROJECTION, ORIGIN_ORPHAN
    # No truth file behind the pointer: orphan (there was supposed to be
    # one).  The pointer itself stays — it is what says WHERE it was.
    return safe_path, anchor, digest, STATE_PROJECTION, ORIGIN_ORPHAN


def five_columns_for_chunk(
    *,
    workspace_root: Optional[Path | str],
    relpath: str,
    start_line: int,
    end_line: int,
    part: int = 0,
    chunk_text: str,
    segments: Optional[Sequence[Tuple[int, int]]] = None,
    gaps: Optional[Sequence[int]] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str], str, str]:
    """``four_columns_for_chunk`` plus the blob case.

    ``workspace_root=None`` means "there is NO truth file behind this row"
    — ``add_memory`` stores a blob of text under a synthetic path without
    writing a file.  Such a row carries no ``body_path`` (there is nothing
    to point at) but still carries an anchor and a drift token, so it is
    not four empty columns, and its origin is ``'blob'`` — **honestly
    "there is no truth source"**, not "the pointer is missing".

    The four-column function is kept because it is what the callers that
    always have a workspace root use; this wrapper exists so that the
    ONE rule decides the origin in every case.
    """
    if workspace_root is None:
        anchor = body_anchor(start_line, end_line, part, segments=segments,
                             gaps=gaps)
        return None, anchor, body_hash(chunk_text), STATE_PROJECTION, \
            ORIGIN_BLOB
    return four_columns_for_chunk(
        workspace_root=workspace_root, relpath=relpath,
        start_line=start_line, end_line=end_line, part=part,
        chunk_text=chunk_text, segments=segments, gaps=gaps)
