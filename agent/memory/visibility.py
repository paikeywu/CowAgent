"""The memory visibility rules — written once, used everywhere.

Every retrieval path (FTS5 unicode61, FTS5 trigram, LIKE fallback, vector
backend) AND every read exit (memory_get, the facade's read, the shelf, the
web console's file reader) must filter what a caller may see through THIS
module and no other. The predicate used to be inlined at four call sites
with slightly different shapes; they drifted, and one drifted version is a
cross-user leak. If the rule ever changes, it changes here.

THE RULES, in words (SEED 裁决 2026-10-27, P0-14):

1. A ``shared`` row is public ONLY when it carries no owner. A row claiming
   to be shared while naming a ``user_id`` is exactly the mislabel shape
   (private data, public flag), so it is not public — fail closed, not open.
2. A caller with an identity additionally sees its own rows, whatever scope
   they claim.
3. A caller without an identity (anonymous CLI, pre-routing startup) sees
   rule 1 only.
4. (P0-11 P-2, RR-04) A row whose ELEMENT was released is invisible to
   EVERYONE — anonymous, third party and owner alike. Release is a privacy
   action; if the owner could still pull the released text back out of the
   regular retrieval exit, the release would be a label rather than an
   action. The owner's own view of what was released is a different surface
   (the facade's RR-04 trusted result set / the shelf's 你释放的 view).
5. (P0-14 L-2) ``visibility: sealed`` is a DARK PAGE (暗页), NOT a release.
   Its body is open to 本视角 (the authoring agent) and to 主人格 (whoever
   reads that agent's own memory), and closed to everybody else. A sealed
   file therefore still belongs in the index — it is indexed under its own
   scope (``SCOPE_SEALED``) with the owner recorded in ``user_id``, so the
   ordinary identity rule decides who may see it.
6. (P0-13 P-2, L-1) The file that IS a release — ``status=released`` or
   ``type=release`` — leaves the index entirely and is invisible on EVERY
   read exit, for every identity.

The flags on a chunk row (``element_released`` / ``file_sealed``) are a
PROJECTION of the truth layer: they are computed from the files on disk by
``agent.memory.release_state`` and re-stamped whenever the truth layer
moves, so a rebuilt index is correct again (SEED 裁决 2026-10-27: the disk
is the authority, the index only mirrors it).

P0-13 L-3: the projection refresh is FAIL-CLOSED. When the truth layer
cannot be resolved, the caller must not fall back to a stale "nothing is
released" marker — that is how a privacy control that broke ends up
releasing exactly what it was protecting. ``agent.memory.manager`` raises
in that case; this module only carries the rules.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# The three visibility classes of a truth-layer file
# ---------------------------------------------------------------------------

VIS_OPEN = "open"          # 白页：正文对所有视角开放
VIS_RELEASED = "released"  # 已释放：对所有身份不可见（含主人格本人）
VIS_SEALED = "sealed"      # 暗页：只对本视角与主人格开放

# The scope a dark page carries in the index. It is its OWN scope value, so
# the index says what the row IS instead of hiding it behind a flag:
#
#   scope='shared' + user_id NULL            → 白页（公开）
#   scope='user'   + user_id=<end user>      → 该用户的记忆
#   scope='sealed' + user_id=<owner token>   → 暗页（只对归属者可见）
#
# A dark page whose owner cannot be determined is indexed with user_id=NULL,
# which makes it invisible to everybody (rule 1): no attribution means no
# reader, and "nobody can see it" is the safe reading of "we do not know
# who may see it".
SCOPE_SEALED = "sealed"

# The owner-token namespace. A dark page belongs to the agent that wrote it
# (front-matter ``author_viewpoint``), and that agent's own reads — which is
# how 主人格 reaches it — are what the token stands for. The prefix keeps
# the agent namespace apart from the end-user namespace that shares the
# ``chunks.user_id`` column, so an agent id can never be mistaken for a
# user id.
AGENT_OWNER_PREFIX = "agent--"


# ---------------------------------------------------------------------------
# File classification — the ONE definition every surface uses
# ---------------------------------------------------------------------------

def classify_frontmatter(fm: Optional[Dict]) -> str:
    """Return ``VIS_OPEN`` / ``VIS_RELEASED`` / ``VIS_SEALED`` for a file.

    This is the single place that answers "is this file released, or is it
    a dark page?". It used to be answered three times — the scan's
    ``_file_seals_itself``, the release-state resolver's two ``if``s, and
    the shelf's ``status != active or visibility != open`` — and the three
    answers disagreed (P0-13 L-2: a sealed dark page was treated as a
    release and hidden from its own owner).
    """
    if not fm:
        return VIS_OPEN
    if fm.get("status") == "released" or fm.get("released") is True:
        return VIS_RELEASED
    if str(fm.get("type") or "") == "release":
        return VIS_RELEASED
    if fm.get("visibility") == "sealed":
        return VIS_SEALED
    return VIS_OPEN


def is_released(fm: Optional[Dict]) -> bool:
    """True when the file IS a release record (not merely sealed)."""
    return classify_frontmatter(fm) == VIS_RELEASED


def is_sealed(fm: Optional[Dict]) -> bool:
    """True when the file is a dark page (sealed but NOT a release)."""
    return classify_frontmatter(fm) == VIS_SEALED


def agent_owner_token(agent_id: Optional[str]) -> Optional[str]:
    """The owner token for an agent id, or None when there is no agent."""
    if not agent_id or not isinstance(agent_id, str):
        return None
    agent_id = agent_id.strip()
    if not agent_id:
        return None
    return AGENT_OWNER_PREFIX + agent_id


def owner_token(fm: Optional[Dict]) -> Optional[str]:
    """The owner token of a dark page, from its ``author_viewpoint``.

    A dark page belongs to the agent that wrote it. When the
    front-matter carries no ``author_viewpoint`` the page has no
    attribution, and its owner token is None — which makes it invisible to
    everybody rather than visible to everybody.
    """
    if not fm:
        return None
    return agent_owner_token(str(fm.get("author_viewpoint") or "").strip() or None)


# ---------------------------------------------------------------------------
# Identities — what a caller holds
# ---------------------------------------------------------------------------

def identity_tokens(user_id: Optional[str] = None,
                    identities: Optional[Iterable[str]] = None) -> List[str]:
    """The distinct identity tokens a caller holds, in a stable order.

    ``user_id`` is the END USER (the legacy ``memory/users/<id>/`` axis);
    ``identities`` carries the ambient runtime identities (the agent id
    among them). A dark page's owner token lives in the agent namespace, so
    the agent id has to be part of the caller's token set — otherwise the
    agent could not read its own dark pages.

    The list is de-duplicated but NOT sorted: the order is the caller's
    precedence (explicit user id first), and the SQL parameters follow it.
    """
    out: List[str] = []
    for candidate in [user_id] + list(identities or []):
        if candidate is None:
            continue
        token = str(candidate).strip()
        if token and token not in out:
            out.append(token)
    return out


def ambient_identities() -> List[str]:
    """The identity tokens the ambient runtime context carries.

    The end user (raw id) and the agent (in the OWNER namespace, because
    that is the namespace a dark page's owner token lives in). Both are
    identities the caller legitimately holds, so both belong in the token
    set — and both are resolved HERE, at the edge, so no leaf module has
    to re-derive them.
    """
    out: List[str] = []
    try:
        from common.runtime_identity import current_identity

        identity = current_identity()
        if identity.user_id:
            token = str(identity.user_id).strip()
            if token and token not in out:
                out.append(token)
        if identity.agent_id:
            token = agent_owner_token(str(identity.agent_id).strip())
            if token and token not in out:
                out.append(token)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# The SQL predicate (retrieval side)
# ---------------------------------------------------------------------------

def visibility_clause(
    user_id: Optional[str] = None,
    scope_col: str = "scope",
    user_col: str = "user_id",
    released_col: str = "element_released",
    sealed_col: str = "file_sealed",
    identities: Optional[Iterable[str]] = None,
) -> Tuple[str, List[str]]:
    """Return ``(sql_fragment, params)`` for the one visibility predicate.

    The fragment is ANDed into the caller's WHERE clause; ``scope_col`` /
    ``user_col`` / ``released_col`` / ``sealed_col`` exist so a query that
    joins ``chunks`` can qualify the columns (``chunks.scope``) without
    re-wording the rule.

    ``identities`` adds the caller's other identity tokens (the ambient
    agent id among them) to the identity branch, so a dark page — whose
    owner token is in the agent namespace — is visible to the agent that
    wrote it and to nobody else.
    """
    tokens = identity_tokens(user_id, identities)
    # The public branch: a shared row with no owner that is not a dark
    # page. The sealed flag keeps a dark page out of it even if the row
    # was indexed with the wrong shape (a shared row that names an owner
    # is the mislabel shape, and a sealed one must never be public).
    public = (f"({scope_col} = 'shared' AND {user_col} IS NULL "
              f"AND COALESCE({sealed_col}, 0) = 0)")
    # The owned branch: the caller's own rows, whatever scope they claim.
    # This is also the branch that serves a dark page to its owner — the
    # dark page carries the owner token in ``user_id`` under its own
    # scope, so it matches here and matches nowhere else.
    if tokens:
        placeholders = ','.join('?' * len(tokens))
        owned = f"({user_col} IN ({placeholders}))"
        params: List[str] = list(tokens)
    else:
        owned = "(0)"          # no identity: the caller owns nothing
        params = []
    # Rule 4: a released element leaves the result set for EVERY identity.
    # The flag is an integer (0/1); ``COALESCE`` keeps rows written by an
    # older build (NULL) visible rather than hiding the whole index until
    # the next sync stamps them.
    not_released = f"COALESCE({released_col}, 0) = 0"
    return f"(({public} OR {owned}) AND {not_released})", params


# ---------------------------------------------------------------------------
# The read decision (read-exit side)
# ---------------------------------------------------------------------------

# The three answers a read exit may give.
READ_ALLOW = "allow"
READ_RELEASED = "released"   # invisible to every identity
READ_SEALED = "sealed"       # invisible to a caller that is not the owner

# The placeholders a read exit returns instead of a body (06 号 B1:
# "sealed 一律返回占位符 + 申请入口"). They live HERE, not in one of the
# exits, because every exit must answer the same way — and they must never
# contain a count of what was withheld: a count is itself a leak (G4-d).
SEALED_PLACEHOLDER = (
    "[sealed] This is a dark page: its body is open to the owning "
    "viewpoint and the master only. The current identity may not read it. "
    "Access entry: the borrowing desk (借阅台) is not implemented yet."
)

RELEASED_PLACEHOLDER = (
    "[released] This memory was released. The truth layer keeps the "
    "original and the tombstone records the release; the released body is "
    "not served on any read exit, to anyone."
)


def decide_read(
    path: str,
    *,
    released_paths: Iterable[str],
    sealed_paths: Iterable[str],
    sealed_owner: Optional[Dict[str, str]] = None,
    tokens: Optional[Iterable[str]] = None,
) -> Tuple[str, str]:
    """Decide whether ``path`` may be read, and why not when it may not.

    The read exits (memory_get, the facade, the shelf, the console reader)
    must not each re-derive "is this released / sealed, and who owns it" —
    that is how P0-13 found ``memory_get`` handing out a released body in
    full while every retrieval path filtered it. This function is that
    single derivation.

    Returns ``(READ_ALLOW, "")`` or ``(READ_RELEASED, reason)`` /
    ``(READ_SEALED, reason)``. The reason is for the caller's own
    diagnostics; it must never be echoed back to the caller together with
    a count of what was withheld (G4-d: a count is a leak).
    """
    released = frozenset(released_paths or ())
    sealed = frozenset(sealed_paths or ())
    owners = sealed_owner or {}
    held = set(tokens or ())

    if path in released:
        return READ_RELEASED, "released"
    if path in sealed:
        owner = owners.get(path)
        if owner and owner in held:
            return READ_ALLOW, ""
        return READ_SEALED, "sealed"
    return READ_ALLOW, ""
