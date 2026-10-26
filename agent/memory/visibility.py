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
"""

from __future__ import annotations

from typing import List, Optional, Tuple


def visibility_clause(
    user_id: Optional[str],
    scope_col: str = "scope",
    user_col: str = "user_id",
) -> Tuple[str, List[str]]:
    """Return ``(sql_fragment, params)`` for the one visibility predicate.

    The fragment is ANDed into the caller's WHERE clause; ``scope_col`` /
    ``user_col`` exist so a query that joins ``chunks`` can qualify the
    columns (``chunks.scope``) without re-wording the rule.
    """
    shared = f"({scope_col} = 'shared' AND {user_col} IS NULL)"
    if user_id:
        return f"({shared} OR {user_col} = ?)", [user_id]
    return shared, []
