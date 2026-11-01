"""T2e: the write-path witness hook.

**The hole.**  ``agent.memory.external_edit`` can witness any truth-layer
file — that part works (the criterion's own probe proves it: all four
leaking paths close on a copy of the log).  But the mechanism only RUNS
from ``MemoryManager.sync()`` (manager.py:1417), and ``sync()`` is a
*scan* trigger, not a *write* trigger.  So:

  * a file written by the ``write`` / ``edit`` tools is only witnessed
    when the agent happens to run a sync afterwards;
  * a file written by ``agent.evolution.record.append_session_evolution``
    is never witnessed at all — that path appends to
    ``memory/evolution/YYYY-MM-DD.md`` and does not touch the index;
  * a file written by ANOTHER process (the old desktop runtime) is
    witnessed only if that process's own sync runs.

⇒ EE-1 sees a **live-leak** for exactly these paths: the mechanism was
alive, the file moved, and nothing recorded it.

**The fix.**  Put the witness AT THE WRITE PATH, not at the sync.  A
write path that lands a truth-layer file records the change with the
content witness at the moment it lands.  Then:

  * the leak does not depend on a sync ever running;
  * the reconciliation stays as the BACKSTOP for edits we did not make
    (a human in an editor, another process) — the two are complementary,
    not competing;
  * "re-witness on every read" is still forbidden: the witness is
    computed from the content ONCE, at write time.

**Why not "just call sync()" from the write path.**  A sync is an
expensive, asynchronous, index-mutating operation.  A write path must
not be able to take down an index rebuild, and must not have to wait
for an embedding provider.  Witnessing is cheap, local and
append-only ⇒ it belongs at the write path.

**What this module must NOT do.**
  * It must never raise into the caller: a bookkeeping failure must not
    lose the user's file (the file is already written by the time we
    are called).
  * It must not fabricate a witness for a file it cannot read.
  * It must not record the same content twice: the log is append-only
    and a duplicate witness is noise that makes ``latest_change_mtime``
    advance for no reason (A3).

The public entry point is :func:`witness_write`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from common.log import logger


def _workspace_root_for(path: Path) -> Optional[Path]:
    """The workspace root that owns ``path``, or None.

    A path is owned by the nearest ancestor that looks like a
    workspace: a directory that holds ``memory/.audit/`` (it has a
    change log) or that is itself an agent directory under an
    ``agents/`` folder.  Walking UP from the file finds the agent's
    own workspace before the shared root, which is what the log is
    keyed by — the root's log and the agent's log are different files
    with different paths inside them.
    """
    p = Path(path).resolve()
    for parent in p.parents:
        if (parent / "memory" / ".audit").is_dir():
            return parent
        if parent.name == "agents" and parent.parent.parent.name != "agents":
            # <root>/agents/<agent-id>/  -> the workspace is <root>/agents/<agent-id>
            return parent.parent
    return None


def _relative_to_workspace(root: Path, path: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def witness_write(absolute_path: str | os.PathLike,
                  *,
                  actor: str = "write-path",
                  source: Optional[str] = None,
                  workspace_root: Optional[str | os.PathLike] = None
                  ) -> Optional[dict]:
    """Record the change of a truth-layer file that just landed on disk.

    Returns the recorded event, or ``None`` when there was nothing to
    record (not a truth-layer file, no workspace, unreadable, or the
    content is already witnessed).

    **Never raises.**  A write that already happened must not be
    undone by a bookkeeping failure.
    """
    try:
        from agent.memory import changes, external_edit
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[witness_write] memory modules unavailable: {e}")
        return None

    try:
        path = Path(absolute_path)
        if not path.is_file():
            return None

        root = None
        if workspace_root is not None:
            root = Path(workspace_root).resolve()
        else:
            root = _workspace_root_for(path)
        if root is None:
            # No workspace owns this file => it is not a truth-layer
            # file of ours (a temp file, a repo under development).
            return None

        rel = _relative_to_workspace(root, path)
        if rel is None:
            return None

        # Only the TRUTH LAYER is worth witnessing.  A file that the
        # index never scans is not a truth-layer file: witnessing it
        # would make the log claim a provenance it does not have and
        # would grow the log with every scratch file.
        if not _is_truth_layer(root, rel):
            return None

        digest = external_edit._sha256_file(path)
        if digest is None:
            # Unreadable => we cannot vouch for it.  Recording a
            # witness we did not compute would be a lie, so we record
            # nothing and say so.
            logger.warning(
                "[witness_write] %s is unreadable; no witness recorded" % rel)
            return None

        # Do not record the same content twice (A3: a re-read must not
        # advance latest_change_mtime).
        try:
            witnessed = external_edit._witnessed_hashes(root)
            prior = witnessed.get(external_edit.normalize_relpath(rel))
            if prior == digest:
                return None
        except Exception:
            # A failure to READ the log must not block recording.
            pass

        src = source or external_edit.SOURCE_EXTERNAL
        if src not in changes.SOURCES:
            # A caller that cannot name its source is itself an
            # anomaly; the reconciler's source is the honest fallback
            # (it means "we found this on disk").  We do NOT add a new
            # member to SOURCES: the criterion side carries a closed
            # LEGAL_SOURCES tuple, and a new member would be an
            # unexplained difference between the two lists.
            src = external_edit.SOURCE_EXTERNAL

        return changes.record_change(
            root, rel, src, actor=actor,
            extra={external_edit.WITNESS_KEY: digest,
                   "reason": "write-path"},
        )
    except Exception as e:
        logger.warning("[witness_write] skipped %s: %s" % (absolute_path, e))
        return None


def _is_truth_layer(root: Path, rel: str) -> bool:
    """Whether ``rel`` is a truth-layer file of ``root``.

    The set is the SAME one the reconciliation uses
    (``external_edit.truth_files``) — the product decision "what is the
    truth layer" must live in ONE place, or the write path and the
    backstop would disagree about which files need a witness.
    """
    try:
        from agent.memory import external_edit
        for r, _f in external_edit.truth_files(root):
            if r == rel:
                return True
        return False
    except Exception:
        # If the set cannot be computed, fall back to the SHAPE of the
        # truth layer: identity files + the markdown trees.  Failing
        # open here means a file that IS a truth file still gets
        # witnessed; failing closed would silently re-open the hole.
        parts = rel.split("/")
        if len(parts) == 1:
            return parts[0] in ("MEMORY.md", "USER.md", "AGENT.md", "RULE.md")
        return parts[0] in ("memory", "knowledge", "persona")
