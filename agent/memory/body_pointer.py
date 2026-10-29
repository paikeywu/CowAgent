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
from typing import Optional, Tuple

from agent.memory.naming import canonical_text

#: The state a freshly written row carries: the text lives in this row.
STATE_PROJECTION = "projection"


def body_hash(fragment: str) -> str:
    """``"sha256:" + sha256(canonical_text(fragment))`` — the drift token.

    The canonical implementation of the canonicalization is
    ``agent.memory.naming.canonical_text`` — IMPORTED, never re-derived.
    A second copy of that rule is how two writers end up disagreeing about
    what "the same text" means.
    """
    return "sha256:" + hashlib.sha256(
        canonical_text(fragment).encode("utf-8")).hexdigest()


def body_anchor(start_line: int, end_line: int, part: int = 0) -> str:
    """``L<start>-<end>[:p<part>]`` — the fragment's locator.

    The part segment keeps the anchor 1:1 with the chunk id (which is
    ``md5("path:start:end[:part]")``): an anchor without the part is not
    unique, and INV-2/INV-3 would then read the wrong text.
    """
    anchor = f"L{start_line}-{end_line}"
    if part:
        anchor += f":p{part}"
    return anchor


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
        knowledge_dir = _knowledge_dir(root)
        if knowledge_dir is not None:
            candidate = knowledge_dir / norm[len("knowledge/"):]
            if candidate.is_file():
                return candidate
        # Fall through to the workspace root: an agent that has its own
        # knowledge/ tree keeps it there, and the shared fallback must not
        # shadow it.
    candidate = root / norm
    if candidate.is_file():
        return candidate
    if norm.startswith("knowledge/"):
        knowledge_dir = _knowledge_dir(root)
        if knowledge_dir is not None:
            candidate = knowledge_dir / norm[len("knowledge/"):]
            if candidate.is_file():
                return candidate
    return None


def _knowledge_dir(workspace_root: Path) -> Optional[Path]:
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



def read_fragment(path: Path | str, anchor: str) -> Optional[str]:
    """The fragment the anchor points at, read from the file. None if not.

    This is the SAME read the invariant checker performs (``text_inv``
    computes ``sha256(canonical_text(fragment))`` from it), so the hash this
    module writes and the hash the checker recomputes are computed from the
    same bytes — the property INV-3 needs.

    The anchor grammar is the checker's grammar, so it is imported from the
    checker rather than spelled out again: two parsers for one anchor format
    is how "the anchor points nowhere" happens while both sides believe
    they agree.
    """
    from agent.memory.text_inv import read_fragment_at_anchor

    return read_fragment_at_anchor(Path(path), anchor)


def four_columns_for_chunk(
    *,
    workspace_root: Path | str,
    relpath: str,
    start_line: int,
    end_line: int,
    part: int = 0,
    chunk_text: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
    """The four column values for one chunk row.

    Returns ``(body_path, body_anchor, body_hash, text_state)``.

    * ``body_path`` — the workspace-relative POSIX path, or None when the
      path is not safe (the caller must then NOT write the row's pointer,
      rather than write a wrong one);
    * ``body_anchor`` — always present (the chunker's line span);
    * ``body_hash`` — the hash of the FRAGMENT AT THE ANCHOR when the file
      can be read, and the hash of the chunk's own text when it cannot (a
      file that is momentarily unreadable must not silently produce a row
      with no drift token at all);
    * ``text_state`` — ``'projection'`` (the text lives in this row).

    The workspace root is used for two things: the path-safety check
    (absolute / ``..`` / backslash / outside-the-root) and finding the
    truth file (which for a ``knowledge/`` path is the SHARED knowledge
    tree when the agent has no copy of its own — see
    :func:`resolve_truth_file`).
    """
    safe_path = posix_body_path(relpath)
    anchor = body_anchor(start_line, end_line, part)
    if safe_path is None:
        return None, anchor, body_hash(chunk_text), STATE_PROJECTION

    from agent.memory.paths import path_escape_reason

    reason = path_escape_reason(safe_path, workspace_root)
    if reason is not None:
        # The path is unsafe for THIS workspace: the row must not carry a
        # pointer that resolves somewhere it must not.
        return None, anchor, body_hash(chunk_text), STATE_PROJECTION

    digest = body_hash(chunk_text)
    truth_file = resolve_truth_file(workspace_root, safe_path)
    if truth_file is not None:
        try:
            fragment = read_fragment(truth_file, anchor)
        except (OSError, UnicodeDecodeError, ValueError):
            fragment = None
        if fragment is not None:
            digest = body_hash(fragment)
    return safe_path, anchor, digest, STATE_PROJECTION
