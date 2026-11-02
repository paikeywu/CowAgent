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


# ---------------------------------------------------------------------------
# The TEAM's own documents under ``agents/`` (P1-阶段三 2026-11-02, G-3/Q3)
# ---------------------------------------------------------------------------
#
# The index carries exactly TWO classes of a teammate's files:
#
#   agents/<id>/AGENT.md             the role's identity definition
#   agents/<id>/deliverables/**/*.md the role's delivered knowledge
#
# Everything else under ``agents/<id>/`` is that role's OWN business and
# stays in that role's own library.  In particular ``agents/<id>/memory/``
# is NOT indexed here: it is that role's memory, and a second copy in the
# shared library would be a SECOND AUTHORITY for the same content — two
# places to look, two places to be stale, and no way to say which one is
# the truth.
#
# The rule is a WHITELIST, not a blacklist: a new directory under a role's
# folder (``agents/<id>/plans/``, say) is not indexed until somebody says
# it should be.  A blacklist would silently start indexing whatever the
# next directory happens to be named.

#: The one file per role that IS an identity definition.
AGENT_IDENTITY_FILENAME = "AGENT.md"

#: The directory per role whose markdown files ARE delivered knowledge.
AGENT_DELIVERABLES_DIRNAME = "deliverables"


def agent_index_allowed(path: str) -> bool:
    """True when this path under ``agents/`` may enter the index.

    The whitelist has exactly two members (see above).  Everything else
    under a role's folder — the role's ``memory/``, its ``knowledge/``,
    anything new — is that role's own business.

    The path is expected to be relative to the team root and to start
    with ``agents/``; anything else is refused, because this rule is
    about the TEAM's documents and a path that does not name a team
    directory is not one.
    """
    parts = path.replace("\\", "/").split("/")
    if len(parts) < 3 or parts[0] != "agents":
        return False
    rest = parts[2:]
    if not rest:
        return False
    if len(rest) == 1:
        # agents/<id>/AGENT.md
        return rest[0] == AGENT_IDENTITY_FILENAME
    if rest[0] == AGENT_DELIVERABLES_DIRNAME:
        # agents/<id>/deliverables/... — markdown only, and the
        # deliverables directory itself is not a file.
        return len(rest) > 1 and rest[-1].lower().endswith(".md")
    return False


def is_excluded_index_path(path: str) -> bool:
    """True when this path lies under a layer the index must not carry."""
    return bool(excluded_segments(path))
