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
   scope (``SCOPE_SEALED``) with the owner recorded in ``sealed_owner``, so
   the ordinary identity rule decides who may see it.
6. (P0-13 P-2, L-1) The file that IS a release — ``status=released`` or
   ``type=release`` — leaves the index entirely and is invisible on EVERY
   read exit, for every identity.
7. (P0-16 F-1) A released element's FEELING rows live in the evidence room
   (facts layer), and the evidence room is append-only: its rows are never
   rewritten and its hash chain is never broken. But "released" must mean
   released on EVERY read path — so a read of a facts file replaces the
   rows of released elements with a fixed placeholder line. The mask lives
   in the READ VIEW only; the disk keeps the 原话.
8. (P0-16 L-5) The two identity axes are SEPARATE COLUMNS. ``user_id``
   holds END USER ids only; the owner token of a dark page lives in its own
   column (``sealed_owner``). The predicate's sealed branch reads that
   column and NOTHING else, so an end-user id that happens to look like an
   owner token can never read a dark page.

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

from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple


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


def is_reserved_end_user_id(user_id: Optional[str]) -> bool:
    """True when ``user_id`` claims the AGENT namespace.

    P0-16 L-5: the end-user axis and the agent-owner axis are separate
    namespaces, and an end-user id that STARTS with ``agent--`` would
    otherwise be indistinguishable from an owner token. Such an id is
    REJECTED ON WRITE — not silently accepted and not merely ignored at
    read time, because accepting it means the collision is already in the
    index and every later reader has to special-case it.

    The check is on the PREFIX, not on a probability: the prefix is the
    namespace marker, and a namespace marker is either there or not.
    """
    if not user_id or not isinstance(user_id, str):
        return False
    return user_id.strip().startswith(AGENT_OWNER_PREFIX)


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
    """The END USER ids a caller holds, in a stable order.

    ``user_id`` is the END USER (the legacy ``memory/users/<id>/`` axis);
    ``identities`` carries more end-user ids (the ambient runtime user id
    among them).

    P0-16 L-5: this is the END USER axis ONLY. The agent's owner token is
    NOT mixed in here — it lives in the agent namespace and is matched
    through its own column (``sealed_owner``). Mixing the two axes in one
    token set is what let an end-user id that happened to equal
    ``agent--<some agent>`` read a dark page.

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


def owner_tokens(tokens: Optional[Iterable[str]] = None) -> List[str]:
    """The AGENT owner tokens a caller holds, in a stable order.

    The owner token is the agent namespace's name for "this dark page is
    mine" (``agent--<agent id>``). It is kept apart from the end-user
    tokens all the way into the SQL predicate (P0-16 L-5).
    """
    out: List[str] = []
    for candidate in list(tokens or []):
        if candidate is None:
            continue
        token = str(candidate).strip()
        if token and token not in out:
            out.append(token)
    return out


def ambient_end_user_ids() -> List[str]:
    """The END USER id the ambient runtime context carries (if any)."""
    try:
        from common.runtime_identity import current_identity

        identity = current_identity()
        if identity.user_id:
            token = str(identity.user_id).strip()
            if token:
                return [token]
    except Exception:
        pass
    return []


def ambient_owner_tokens() -> List[str]:
    """The AGENT owner token the ambient runtime context carries (if any).

    The agent is in the OWNER namespace, because that is the namespace a
    dark page's owner token lives in. Resolved HERE, at the edge, so no
    leaf module has to re-derive it.
    """
    try:
        from common.runtime_identity import current_identity

        identity = current_identity()
        if identity.agent_id:
            token = agent_owner_token(str(identity.agent_id).strip())
            if token:
                return [token]
    except Exception:
        pass
    return []


def ambient_identities() -> List[str]:
    """Every identity token the ambient runtime context carries.

    Both axes, in one list — for callers that only need "who am I". The
    retrieval path does NOT use this: it keeps the two axes apart (P0-16
    L-5) and hands each to its own branch of the predicate.
    """
    out: List[str] = []
    for token in ambient_end_user_ids() + ambient_owner_tokens():
        if token not in out:
            out.append(token)
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
    owner_token_list: Optional[Iterable[str]] = None,
    sealed_owner_col: str = "sealed_owner",
) -> Tuple[str, List[str]]:
    """Return ``(sql_fragment, params)`` for the one visibility predicate.

    The fragment is ANDed into the caller's WHERE clause; ``scope_col`` /
    ``user_col`` / ``released_col`` / ``sealed_col`` / ``sealed_owner_col``
    exist so a query that joins ``chunks`` can qualify the columns
    (``chunks.scope``) without re-wording the rule.

    ``identities`` carries the caller's other END USER ids (the ambient
    user id among them) — the end-user axis.

    ``owner_token_list`` carries the caller's AGENT owner tokens (the
    ambient agent id among them, already in the ``agent--<id>`` form) — the
    owner axis.

    P0-16 L-5: the two axes are matched against two DIFFERENT columns. A
    dark page is visible ONLY through ``sealed_owner`` (the owner axis);
    an ordinary row is visible through ``user_id`` (the end-user axis). The
    sealed branch never looks at ``user_id``, so an end-user id that
    happens to equal an owner token cannot read a dark page — and an
    owner token can never be mistaken for an end-user id.
    """
    tokens = identity_tokens(user_id, identities)
    owners = owner_tokens(owner_token_list)
    # The public branch: a shared row with no owner that is not a dark
    # page. The sealed flag keeps a dark page out of it even if the row
    # was indexed with the wrong shape (a shared row that names an owner
    # is the mislabel shape, and a sealed one must never be public).
    public = (f"({scope_col} = 'shared' AND {user_col} IS NULL "
              f"AND COALESCE({sealed_col}, 0) = 0)")
    params: List[str] = []
    # The owned branch: the caller's own rows, whatever scope they claim.
    # A DARK PAGE is not in this branch — its owner lives in the agent
    # namespace and is matched by the sealed branch below, so that the two
    # axes can never be crossed (P0-16 L-5).
    if tokens:
        placeholders = ','.join('?' * len(tokens))
        owned = (f"({user_col} IN ({placeholders}) "
                 f"AND {scope_col} <> '{SCOPE_SEALED}')")
        params.extend(tokens)
    else:
        owned = "(0)"          # no end-user identity: the caller owns nothing
    # The sealed branch (L-5): a dark page is visible to its owner and to
    # nobody else. The owner token lives in its OWN column, so the end
    # user axis is not consulted at all.
    if owners:
        owner_placeholders = ','.join('?' * len(owners))
        sealed_branch = (f"({scope_col} = '{SCOPE_SEALED}' AND "
                         f"{sealed_owner_col} IN ({owner_placeholders}))")
        params.extend(owners)
    else:
        sealed_branch = "(0)"  # no owner token: the caller owns no dark page
    # Rule 4: a released element leaves the result set for EVERY identity.
    # The flag is an integer (0/1); ``COALESCE`` keeps rows written by an
    # older build (NULL) visible rather than hiding the whole index until
    # the next sync stamps them.
    not_released = f"COALESCE({released_col}, 0) = 0"
    return (f"(({public} OR {owned} OR {sealed_branch}) AND "
            f"{not_released})"), params


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
    released_fact_ids: Optional[Iterable[str]] = None,
) -> Tuple[str, str, FrozenSet[str]]:
    """Decide whether ``path`` may be read, and why not when it may not.

    The read exits (memory_get, the facade, the shelf, the console reader)
    must not each re-derive "is this released / sealed, and who owns it" —
    that is how P0-13 found ``memory_get`` handing out a released body in
    full while every retrieval path filtered it. This function is that
    single derivation.

    Returns ``(READ_ALLOW, "")`` or ``(READ_RELEASED, reason)`` /
    ``(READ_SEALED, reason)``, plus the THIRD element: the element ids of
    the fact rows that belong to released elements and must therefore be
    masked in the reading view (P0-16 F-1). The reason is for the
    caller's own diagnostics; it must never be echoed back to the caller
    together with a count of what was withheld (G4-d: a count is a leak).

    The third element travels with the decision on purpose: the masking
    rule is part of the SAME visibility judgement, so a read exit cannot
    apply it with a rule of its own — it can only apply what this function
    hands it.
    """
    released = frozenset(released_paths or ())
    sealed = frozenset(sealed_paths or ())
    owners = sealed_owner or {}
    held = set(tokens or ())
    mask = frozenset(released_fact_ids or ())

    if path in released:
        return READ_RELEASED, "released", mask
    if path in sealed:
        owner = owners.get(path)
        if owner and owner in held:
            return READ_ALLOW, "", mask
        return READ_SEALED, "sealed", mask
    return READ_ALLOW, "", mask
