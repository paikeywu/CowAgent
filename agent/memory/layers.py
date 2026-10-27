"""Live-memory layer segmentation — ONE definition of the layers the index
must never touch.

Three call sites need this set and must never disagree:

  * ``agent/memory/manager.py``   — the scan that feeds the index
  * ``agent/memory/inspector.py`` — check 5 (index-layer hygiene)
  * ``agent/evolution/executor.py`` — the change-watch ignore list

Before this module the set was written out three times, and P0-11 caught the
cost: ``_tombstones`` was added to the scan exclusion but the inspector's
copy still believed tombstone rows were legitimate, so a tombstone sat in the
shared index (scope='shared', user_id=NULL) and its audit fields —
``original_path``, ``consent_ref``, the confirmation token hash — were
readable by ANY caller (V2-Q-01).

A segment is a PATH COMPONENT, never a substring: a truth-layer file called
``含shelf的事件--a1b2c3d4.md`` is a legal element name and must not be
mistaken for the shelf layer.
"""

from __future__ import annotations

from typing import Tuple

# Path components that are NOT part of the indexed truth layer.
#
#   shelf/       — rebuildable projection (directory cards)
#   facts/       — append-only evidence room (JSONL, referenced by pointer)
#   .snapshots/  — the inspector's baseline
#   .audit/      — the inspector's + facade's audit trail
#   _tombstones/ — release tombstones: audit records about a released
#                  element, never its content. Owner-scoped by nature
#                  (P0-3 RR-04), so indexing them anonymously would leak
#                  the release metadata of every release, ever.
EXCLUDED_INDEX_SEGMENTS: Tuple[str, ...] = (
    "shelf",
    "facts",
    ".snapshots",
    ".audit",
    "_tombstones",
)


def excluded_segments(path: str) -> Tuple[str, ...]:
    """The excluded segments this path sits under, in path order.

    Matching is per component: ``memory/markdown/_tombstones/x.md`` yields
    ``('_tombstones',)`` while ``memory/markdown/events/含shelf.md`` yields
    ``()``.
    """
    parts = path.replace("\\", "/").split("/")
    return tuple(s for s in parts if s in EXCLUDED_INDEX_SEGMENTS)


def is_excluded_index_path(path: str) -> bool:
    """True when this path lies under a layer the index must not carry."""
    return bool(excluded_segments(path))
