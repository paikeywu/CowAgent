"""The memory visibility predicate — written once, used everywhere.

Every retrieval path (FTS5 unicode61, FTS5 trigram, LIKE fallback, vector
backend) must filter what a caller may see through THIS function and no
other. The predicate used to be inlined at four call sites with slightly
different shapes; they drifted, and one drifted version is a cross-user
leak. If the rule ever changes, it changes here.

The rule, in words:

1. A ``shared`` row is public ONLY when it carries no owner. A row claiming
   to be shared while naming a ``user_id`` is exactly the mislabel shape
   (private data, public flag), so it is visible to no one until the data
   is repaired — fail closed, not open.
2. A caller with an identity additionally sees its own rows, whatever
   scope they claim.
3. A caller without an identity (anonymous CLI, pre-routing startup) sees
   rule 1 only.
4. (P0-11 P-2, RR-04) A row whose ELEMENT was released is invisible to
   EVERYONE — anonymous, third party and owner alike. Release is a privacy
   action; if the owner could still pull the released text back out of the
   regular retrieval exit, the release would be a label rather than an
   action. The owner's own view of what was released is a different
   surface (the facade's RR-04 trusted result set / the shelf's 你释放的
   view), not this one.
5. (P0-11 P-2, RR-04) A row whose FILE seals itself (``visibility=sealed``
   or ``status=released``) is invisible to everyone as well — that is how
   a release record stays out of the shared result set.

Rules 4 and 5 are expressed against the two flags the index carries
(``element_released`` / ``file_sealed``). The flags are a PROJECTION of the
truth layer: they are computed from the files on disk by
``agent.memory.release_state`` and re-stamped whenever the truth layer
moves, so a rebuilt index is correct again (SEED 裁决 2026-10-27: the disk
is the authority, the index only mirrors it).
"""

from __future__ import annotations

from typing import List, Optional, Tuple


def visibility_clause(
    user_id: Optional[str],
    scope_col: str = "scope",
    user_col: str = "user_id",
    released_col: str = "element_released",
    sealed_col: str = "file_sealed",
) -> Tuple[str, List[str]]:
    """Return ``(sql_fragment, params)`` for the one visibility predicate.

    The fragment is ANDed into the caller's WHERE clause; ``scope_col`` /
    ``user_col`` / ``released_col`` / ``sealed_col`` exist so a query that
    joins ``chunks`` can qualify the columns (``chunks.scope``) without
    re-wording the rule.
    """
    shared = f"({scope_col} = 'shared' AND {user_col} IS NULL)"
    if user_id:
        identity = f"({shared} OR {user_col} = ?)"
        params: List[str] = [user_id]
    else:
        identity = shared
        params = []
    # Rules 4+5: released elements and self-sealing files leave the result
    # set for EVERY identity. The flags are integers (0/1); ``COALESCE``
    # keeps rows written by an older build (NULL) visible rather than
    # hiding the whole index until the next sync stamps them.
    not_released = f"COALESCE({released_col}, 0) = 0"
    not_sealed = f"COALESCE({sealed_col}, 0) = 0"
    return f"({identity} AND {not_released} AND {not_sealed})", params
