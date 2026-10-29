"""Projection rebuilder — rebuild the chunks projection from the truth layer.

The markdown/JSONL files on disk are the TRUTH; the SQLite ``chunks`` rows are
a rebuildable PROJECTION. When the projection drifts (the read-path probe marks
a file stale, the inspector reports drift, a human edits a file through the
import channel, or migration M7 runs), this module re-cuts the file with the
SAME chunker the writer used and atomically swaps the projection rows in.

Hard rules (design §4.3 — each one was a review verdict, not a nicety):

* **Never half-rebuild.** The whole projection for one file is swapped in one
  transaction via ``storage.replace_file_chunks``. A failure leaves the OLD
  projection intact, logs ERROR, drops an ``.audit/`` marker, and the inspector
  reports red. A stale-but-complete projection beats a partial one.
* **Idempotent.** Same input file -> identical row count / id set (with part)
  / hash set, however many times it runs.
* **chunk id rule unchanged**: ``md5("path:start:end[:part]")`` (with part).
* **body_anchor carries the part segment**: ``L<start>-<end>[:p<part>]`` —
  without part the anchor is not unique and INV-2/INV-3 read the wrong text.
* **body_path is POSIX, relative, no backslash / absolute / ``..``** (INV-11).
* **body_hash = sha256(naming.canonical_text(fragment))** — the ONLY canonical
  implementation (naming.py:58), imported, never re-derived.
* **release/sealed flags are RE-COMPUTED from the truth layer** (tombstones
  are the final authority for release state); a front-matter / tombstone
  disagreement is reported, and the tombstone wins — the OLD row is never
  trusted.
* **hit_count / last_hit_at are inherited by chunk id** (creative E11):
  delete+insert must not silently reset the retrieval-heat signal.
* **Per-path write lock** is reused (storage's own lock).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.log import logger

from agent.memory.chunker import TextChunker, chunk_for_file
from agent.memory.naming import canonical_text
from agent.memory import changes
from agent.memory.body_pointer import (
    STATE_PROJECTION,
    body_anchor,
    body_hash,
    four_columns_for_chunk,
    posix_body_path,
)
from agent.memory.storage import MemoryChunk, MemoryStorage

#: The chunker version stamped into ``files.chunker_version``. Bump it when the
#: cutting strategy changes, so a probe can re-cut the same way.
CHUNKER_VERSION = 3


class RebuildError(RuntimeError):
    """A rebuild that could not complete; the old projection is kept."""


def _chunk_id(path: str, start_line: int, end_line: int, part: int = 0) -> str:
    """Same rule as manager._generate_chunk_id — md5("path:start:end[:part]").

    Defined HERE (not imported from manager) so the rebuilder does not pull the
    whole manager in, and so the id rule lives next to the code that relies on
    it. Kept byte-identical with manager.py:1365 on purpose: changing one
    without the other silently orphans every existing chunk.
    """
    content = f"{path}:{start_line}:{end_line}"
    if part:
        content += f":{part}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _posix_rel(path: Path, workspace_root: Path) -> str:
    """Workspace-relative POSIX path, asserted safe (INV-11).

    P1-阶段三 (C-A2): the SAFETY RULE is not re-spelled here.  The
    writer-side pointer builder (``agent.memory.body_pointer``) owns it and
    it delegates to ``agent.memory.paths`` — the ONE rule since P1-阶段二-C.
    This function keeps its name and its raise-on-unsafe behaviour (the
    rebuilder must NOT write a projection for a path it cannot point at),
    but the DECISION comes from the shared module.
    """
    rel = path.resolve().relative_to(workspace_root.resolve()).as_posix()
    safe = posix_body_path(rel)
    if safe is None:
        raise RebuildError(
            f"unsafe body_path {rel!r} (backslash / absolute / '..')")
    return safe


def _body_anchor(start_line: int, end_line: int, part: int = 0) -> str:
    """``L<start>-<end>[:p<part>]`` — the part segment keeps anchor<->id 1:1.

    P1-阶段三 (C-A2): the ANCHOR GRAMMAR is the ONE definition
    (``agent.memory.body_pointer.body_anchor``), shared with the manager's
    write path and with the checker's parser.  Two spellings of the anchor
    format is how "the anchor points nowhere" happens while both sides
    believe they agree.
    """
    return body_anchor(start_line, end_line, part)


def _body_hash(fragment: str) -> str:
    """sha256 of the canonical fragment — the drift-check token (INV-3).

    P1-阶段三 (C-A2): delegates to the shared builder, which in turn
    imports ``naming.canonical_text`` — the ONE canonical implementation.
    """
    return body_hash(fragment)


def recompute_release_flags(
    workspace_root: Path | str,
) -> "Tuple[object, List[str]]":
    """Re-derive release/sealed flags from the truth layer.

    Returns ``(release_state, mismatches)``. ``release_state`` is the
    ``ReleaseState`` resolved from tombstones + front-matter (tombstones are the
    final authority for release state). ``mismatches`` lists paths whose
    front-matter and tombstone disagree — each one is reported (and the
    tombstone wins), because an editable front-matter must not be able to
    smuggle a sealed file back into public view.
    """
    from agent.memory.release_state import release_mismatches, resolve

    state = resolve(workspace_root)
    mm = release_mismatches(workspace_root)
    mismatches: List[str] = list(mm.get("tombstone_only", ())) + \
        list(mm.get("frontmatter_only", ()))
    return state, mismatches


def rebuild_file(
    storage: MemoryStorage,
    workspace_root: Path | str,
    file_path: Path | str,
    *,
    user_id: Optional[str] = None,
    scope: str = "shared",
    release_state=None,
) -> Dict:
    """Rebuild the projection for ONE truth-layer file, atomically.

    ``file_path`` is the absolute (or workspace-relative) path of a markdown
    truth file. The new chunks are cut from its canonical text, stamped with
    body_path / body_anchor / body_hash / text_state='projection', given the
    release flags re-derived from the truth layer, and swapped in via
    ``replace_file_chunks`` — all or nothing.

    Returns a small dict describing what was written. Raises ``RebuildError``
    (or the underlying error) on failure, with the old projection untouched.
    """
    workspace = Path(workspace_root)
    fp = Path(file_path)
    if not fp.is_absolute():
        fp = workspace / fp
    if not fp.is_file():
        raise RebuildError(f"truth file not found: {fp}")

    # P1-阶段三 (S3-C): the relative path comes from the INDEX KEY, not
    # from the filesystem location of the resolved file.
    #
    # The knowledge tree lives in the SHARED root when the agent has no
    # copy of its own, so the resolved file is NOT under the agent's
    # workspace — and ``relative_to(workspace_root)`` raised "not in the
    # subpath of", which made the rebuild fail for every knowledge file
    # in every agent library (63-69 errors per library, measured).
    #
    # The index key (``knowledge/<relative>``) and the file's location
    # are DIFFERENT things; the key is what the row stores and what the
    # pointer must be.  So the key is derived from the KEY SPACE, and
    # the file is found through the ONE resolution rule.
    rel = _rel_key_for(fp, workspace)

    raw = fp.read_text(encoding="utf-8")
    canonical = canonical_text(raw)
    file_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    stat = fp.stat()

    chunker = TextChunker()
    # G-3: the chunking DECISION lives in ONE place (chunker.chunk_for_file)
    # and is the same one the writer makes. This used to call
    # ``chunk_markdown`` unconditionally, while the writer's ``add_memory``
    # called ``chunk_text`` — so a rebuild produced a DIFFERENT set of
    # chunk ids than the write did (measured on the real corpus: 56 of 86
    # files diverged). A rebuild must refresh the projection, not replace
    # the chunk identities: every hit count, reference and audit trail
    # points at an id.
    pieces = chunk_for_file(raw, rel)

    # Release/sealed flags: re-derived from the truth layer, never inherited.
    # A front-matter / tombstone disagreement is reported (the tombstone wins)
    # because an editable front-matter must not smuggle a released file back.
    mismatches: List[str] = []
    if release_state is None:
        release_state, mismatches = recompute_release_flags(workspace)
    element_released, file_sealed = release_state.flags_for(rel)
    sealed_owner = release_state.owner_for(rel) if file_sealed else None

    mm_for_this = [m for m in mismatches if _norm(m) == _norm(rel)]
    for m in mm_for_this:
        logger.error(
            f"[rebuilder] release mismatch on {rel}: front-matter and "
            f"tombstone disagree; TOMBSTONE WINS. Inspect the front-matter — "
            f"it may have been edited.")
    if mm_for_this:
        _audit_mismatch(workspace, rel, mm_for_this)

    # F-A1: a dark page must enter as scope='sealed' carrying its owner, NOT as
    # scope='shared'. If it went in 'shared', the index_guard corrective path
    # would fire and overwrite sealed_owner from the guard table (whose
    # owner_token is NULL at registration) — losing the very owner the
    # rebuilder just derived, so the owner could no longer read its own dark
    # page (violates P0-21). Entering 'sealed' bypasses the corrective trigger
    # entirely (it only fires on NEW.scope='shared').
    if file_sealed:
        scope = "sealed"
        user_id = user_id or sealed_owner

    chunks: List[MemoryChunk] = []
    for pc in pieces:
        cid = _chunk_id(rel, pc.start_line, pc.end_line, pc.part)
        # P1-阶段三 (S3-C): the four body columns come from the WRITER'S
        # single definition (``body_pointer.four_columns_for_chunk``) —
        # the same call the manager's write path makes.  The rebuilder
        # used to spell the rule out itself and hashed the CHUNKER'S
        # TEXT, which is a different hash from the one the checker
        # recomputes (it hashes the FRAGMENT at the anchor).  For a
        # re-assembled chunk the two differ, and the result was the
        # same fact reported twice: INV-2 RED (the truth) AND INV-3 RED
        # (which looks like corruption).  One definition, one hash.
        body_path, body_anchor, body_hash, text_state, body_origin = (
            four_columns_for_chunk(
                workspace_root=workspace,
                relpath=rel,
                start_line=pc.start_line,
                end_line=pc.end_line,
                part=pc.part,
                chunk_text=pc.text,
                # P1-阶段三 第四轮: the segment sequence.  A rebuild that
                # drops it would write a bare span for a chunk that was
                # assembled from several ranges — and the reader would
                # rebuild the wrong text.  The rebuilt anchor must carry
                # the same information the chunker produced.
                segments=list(pc.segments or []),
                gaps=list(pc.gaps or []),
            )
        )
        chunks.append(MemoryChunk(
            id=cid,
            user_id=user_id,
            scope=scope,
            source="memory",
            path=rel,
            start_line=pc.start_line,
            end_line=pc.end_line,
            text=pc.text,
            embedding=None,
            # The ROW's content hash — the chunker's text.  This is the
            # optimistic lock the conditional backfill compares against,
            # NOT the drift token.
            hash=_body_hash(pc.text),
            metadata=None,
            element_released=element_released,
            file_sealed=file_sealed,
            sealed_owner=sealed_owner,
            body_path=body_path or rel,
            body_anchor=body_anchor or _body_anchor(pc.start_line,
                                                      pc.end_line, pc.part),
            body_hash=body_hash or _body_hash(pc.text),
            text_state=text_state or STATE_PROJECTION,
            body_origin=body_origin,
        ))

    try:
        storage.replace_file_chunks(
            rel, chunks, file_hash=file_hash,
            mtime=int(stat.st_mtime), size=stat.st_size,
            chunker_version=CHUNKER_VERSION)
    except Exception as e:
        # Never half-rebuild: the old projection is still in place (the swap
        # rolled back). Log ERROR and mark .audit/ so the inspector reports red
        # instead of the failure going quiet.
        logger.error(
            f"[rebuilder] failed to rebuild {rel} on {storage.db_path}: {e}; "
            f"old projection kept")
        _audit_marker(workspace, rel, str(e))
        raise RebuildError(f"rebuild of {rel} failed, old projection kept: {e}") from e

    # T2: the rebuild consumed a truth-layer change — record it in the single
    # change log so the index projection and the shelf read the SAME record.
    changes.record_change(workspace, rel, "rebuilder")

    return {
        "path": rel,
        "chunks": len(chunks),
        "ids": [c.id for c in chunks],
        "hashes": [c.hash for c in chunks],
        "file_hash": file_hash,
        "release_mismatch": bool(mm_for_this),
    }



def _rel_key_for(fp: Path, workspace_root: Path) -> str:
    """The INDEX KEY for a truth file — not its filesystem location.

    The key space is the workspace root for everything except the
    knowledge tree, which lives in the shared root when the agent has
    no copy of its own.  So the key is derived by asking the ONE
    resolution rule (in reverse): is this file inside the knowledge
    directory?  Then the key starts with ``knowledge/``; otherwise the
    key is the path relative to the workspace root.

    This is the counterpart of ``body_pointer.resolve_truth_file``: that
    one turns a key into a file, this one turns a file into a key.  Two
    rules that are not inverses of each other is how a rebuild writes a
    pointer that points nowhere.
    """
    from agent.memory.paths import normalize_relpath
    from agent.memory.body_pointer import _knowledge_dirs

    fp_resolved = Path(fp).resolve()
    ws_resolved = Path(workspace_root).resolve()

    # P1-阶段三 第四轮: EVERY knowledge directory is a candidate, not just
    # the one ``_knowledge_dir`` happens to name.  The same shadowing
    # defect that broke ``resolve_truth_file`` broke this inverse rule:
    # an agent that has a ``knowledge/`` directory of its own could not
    # be given an index key for a file in the SHARED tree, and the
    # rebuild failed with "truth file ... is outside the workspace" —
    # measured: 70 rebuild errors in ``agents/agent-mvnewfyb``.
    #
    # The two rules must stay INVERSES of each other (that is what the
    # comment above says), so they must consult the SAME set of
    # directories.  Both now call ``_knowledge_dirs``.
    for knowledge_dir in _knowledge_dirs(workspace_root):
        try:
            rel_to_knowledge = fp_resolved.relative_to(
                Path(knowledge_dir).resolve())
            key = "knowledge/" + rel_to_knowledge.as_posix()
            safe = posix_body_path(key)
            if safe is None:
                raise RebuildError(
                    "unsafe body_path %r (backslash / absolute / '..')" % key)
            return safe
        except ValueError:
            continue

    try:
        rel = fp_resolved.relative_to(ws_resolved).as_posix()
    except ValueError:
        raise RebuildError(
            "truth file %s is outside the workspace %s and outside the "
            "knowledge directory — it has no index key" % (fp_resolved,
                                                           ws_resolved))
    safe = posix_body_path(rel)
    if safe is None:
        raise RebuildError(
            "unsafe body_path %r (backslash / absolute / '..')" % rel)
    return safe


def _norm(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def _audit_mismatch(workspace: Path, rel: str, mismatches: List[str]) -> None:
    """Report a front-matter/tombstone disagreement to .audit/ (RED signal).

    G-2: the path now comes from the ONE definition (``audit_dir()``), so
    the marker lands where the other audit trails live.
    """
    try:
        audit = audit_dir(workspace)
        audit.mkdir(parents=True, exist_ok=True)
        (audit / "release_mismatch.jsonl").open("a", encoding="utf-8").write(
            '{"path": %s, "mismatch": %s}\n' % (
                _json_str(rel), _json_str(",".join(mismatches))))
    except Exception:
        logger.warning(f"[rebuilder] could not write mismatch marker for {rel}")


def _audit_marker(workspace: Path, rel: str, error: str) -> None:
    """Drop a .audit/ marker for a failed rebuild — the inspector reads it.

    G-2: this used to write to ``<workspace>/.audit/`` while every other
    audit trail (the change log, the release projection marker) lives
    under ``<workspace>/memory/.audit/`` — so even a reader that knew
    the name would have looked in the wrong place. The path now comes
    from ONE definition (audit_dir()), shared with the inspector.

    The docstring used to say "the inspector reads it" while NOTHING
    read it. It does now: ``inspector._check_rebuild_failed``.
    """
    try:
        audit = audit_dir(workspace)
        audit.mkdir(parents=True, exist_ok=True)
        (audit / "rebuild_failures.jsonl").open("a", encoding="utf-8").write(
            '{"path": %s, "error": %s}\n' % (
                _json_str(rel), _json_str(error)))
    except Exception:
        # The audit marker failing must not mask the rebuild failure itself.
        logger.warning(f"[rebuilder] could not write audit marker for {rel}")


def audit_dir(workspace: Path | str) -> Path:
    """The ONE definition of where the audit trails live (G-2).

    ``<workspace>/memory/.audit`` — the same place the change log and the
    release-projection marker live, and the same place the inspector
    looks. It used to be written as ``<workspace>/.audit`` by the
    rebuilder and read as ``<workspace>/memory/.audit`` by the inspector:
    two spellings of one directory, and a marker nobody could find.
    """
    return Path(workspace) / "memory" / ".audit"


def read_release_mismatches(workspace: Path | str) -> List[Dict[str, Any]]:
    """The recorded front-matter/tombstone disagreements — the G-2 reader.

    A mismatch means the truth layer says two different things about
    whether an element is released. The tombstone wins (the rebuilder
    enforces it), but the disagreement itself is a finding: an editable
    front-matter tried to smuggle a released file back into public view.
    """
    out: List[Dict[str, Any]] = []
    path = audit_dir(workspace) / "release_mismatch.jsonl"
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [{"path": "<unreadable>", "mismatch": f"cannot read: {e}"}]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append({"path": "<malformed>", "mismatch": line[:120]})
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_release_mismatches(workspace: Path | str) -> List[Dict[str, Any]]:
    """The recorded front-matter/tombstone disagreements — the G-2 reader.

    A mismatch means the truth layer says two different things about
    whether an element is released. The tombstone wins (the rebuilder
    enforces it), but the disagreement itself is a finding: an editable
    front-matter tried to smuggle a released file back into public view.
    """
    out: List[Dict[str, Any]] = []
    path = audit_dir(workspace) / "release_mismatch.jsonl"
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [{"path": "<unreadable>", "mismatch": f"cannot read: {e}"}]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append({"path": "<malformed>", "mismatch": line[:120]})
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def read_rebuild_failures(workspace: Path | str) -> List[Dict[str, Any]]:
    """The recorded rebuild failures — the reader for the G-2 check.

    Returns one dict per line of ``rebuild_failures.jsonl``. An
    unreadable/malformed log is NOT swallowed: it is returned as a
    synthetic entry so the caller reports it (a marker that cannot be
    parsed is itself a finding — fail-visible, never fail-silent).
    """
    out: List[Dict[str, Any]] = []
    path = audit_dir(workspace) / "rebuild_failures.jsonl"
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [{"path": "<unreadable>", "error": f"cannot read the log: {e}"}]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append({"path": "<malformed>", "error": f"cannot parse: {line[:120]}"})
            continue
        if isinstance(rec, dict):
            out.append(rec)
        else:
            out.append({"path": "<malformed>", "error": f"not a record: {line[:120]}"})
    return out


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)
