"""
Memory manager for AgentMesh

Provides high-level interface for memory operations
"""

import json
import os
import time
from typing import FrozenSet, List, Optional, Dict, Any, Sequence, Tuple
from pathlib import Path
import hashlib
from datetime import datetime

from agent.memory.config import MemoryConfig, get_default_memory_config
from agent.memory.storage import MemoryStorage, MemoryChunk, SearchResult
from agent.memory.chunker import TextChunker, chunk_for_file
from agent.memory.embedding import EmbeddingProvider, EmbeddingCache
from agent.memory.layers import excluded_segments
from agent.memory.release_state import ReleaseStateCache
from agent.memory.reranker import Reranker
from agent.memory.summarizer import MemoryFlushManager, create_memory_files_if_needed
from agent.memory.visibility import (
    READ_ALLOW,
    SCOPE_SEALED,
    VIS_OPEN,
    VIS_RELEASED,
    VIS_SEALED,
    ambient_end_user_ids,
    ambient_owner_tokens,
    classify_frontmatter,
    identity_tokens,
    owner_token,
    owner_tokens,
)


def _utc8_now() -> str:
    """Local timestamp for the audit trail (UTC+8, the workspace's clock)."""
    from datetime import timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H:%M:%S+08:00")


def _real(path: Path) -> Path:
    """Symlink-resolved path, for prefix comparisons that must not care how a
    directory was reached (``/var`` vs ``/private/var`` on macOS)."""
    try:
        return Path(os.path.realpath(path))
    except OSError:
        return Path(path)


def _index_rel_path(file_path: Path, roots: Sequence[Tuple[Path, str]]) -> Optional[str]:
    """Index key for a scanned file, or None when no known root contains it.

    Scanned files do not all come from one root: ``common.state_dir`` sends an
    Agent that has no local ``knowledge/`` of its own to the SHARED root, so
    sync() legitimately walks files outside the workspace. Keying those off the
    workspace alone raises ValueError, and because that ran inside sync()'s file
    loop a single such file aborted the whole sync — taking down every retrieval
    that goes through the ``search()`` in front of it (#3175).

    Each root carries the prefix its files are keyed under, so shared knowledge
    keeps the ``knowledge/note.md`` key it would have had inside the workspace:
    the index keeps its shape when an Agent gains or loses a private copy, and
    nothing has to be reindexed. Keys are POSIX-style on every platform, which
    is also the spelling ``add_memory`` writes — otherwise the same file is
    indexed twice on Windows, once per separator.

    Roots are matched as given first and only then symlink-resolved, so the
    common case costs no extra stat calls while a workspace reached through a
    symlink still matches instead of losing its files to the skip path.
    """
    for resolve in (False, True):
        target = _real(file_path) if resolve else file_path
        for root, prefix in roots:
            base = _real(root) if resolve else root
            try:
                rel = target.relative_to(base)
            except ValueError:
                continue
            return (Path(prefix) / rel).as_posix() if prefix else rel.as_posix()
    return None


# The files at the workspace root that say who the Agent is and who the user
# is. They were never indexed: sync() added only MEMORY.md explicitly, and
# the two directory walks (memory/ and knowledge/) never reach the root. So
# the identity of the person was in USER.md and could not be found by search —
# "检索不到身份，检索就与身份无关".
#
# They are indexed under source "memory" and scope "shared": they are the
# workspace's own documents, not a user's private memory.
_ROOT_IDENTITY_FILES = ("MEMORY.md", "USER.md", "AGENT.md", "RULE.md")


class MemoryManager:
    """
    Memory manager with hybrid search capabilities
    
    Provides long-term memory for agents with vector and keyword search
    """

    # P1 阶段二-B T2-B1: the read-path freshness probe's CLASS-level default.
    # ``search()`` and ``get_status()`` read ``self._freshness``; an instance
    # built via ``__new__`` never runs __init__, so an assignment inside
    # __init__ cannot cover that path (it raised AttributeError and took the
    # whole retrieval down). A class attribute guarantees the name exists on
    # every instance; __init__ shadows it with the real probe.
    _freshness = None

    def __init__(
        self,
        config: Optional[MemoryConfig] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        llm_model: Optional[Any] = None,
        reranker: Optional[Reranker] = None
    ):
        """
        Initialize memory manager

        Args:
            config: Memory configuration (uses global config if not provided)
            embedding_provider: Custom embedding provider (optional)
            llm_model: LLM model for summarization (optional)
            reranker: Reranker that reorders search candidates (optional)
        """
        # P1 阶段二-B T2-B1: the freshness probe is re-assigned below (in a
        # try/except) once the config is known. The CLASS attribute below is
        # the real guarantee: a caller may build a manager via ``__new__``
        # WITHOUT running this __init__ at all (several tests do exactly
        # that), so an assignment inside __init__ alone cannot cover every
        # path. The class-level default makes the attribute exist on EVERY
        # instance; __init__ then shadows it with the real probe.
        self._freshness = None

        self.config = config or get_default_memory_config()
        
        # Initialize storage
        db_path = self.config.get_db_path()
        self.storage = MemoryStorage(db_path)
        
        # Initialize chunker
        self.chunker = TextChunker(
            max_tokens=self.config.chunk_max_tokens,
            overlap_tokens=self.config.chunk_overlap_tokens
        )
        
        # Embedding provider is owned by the caller (agent_initializer is the
        # canonical entry point and handles legacy/explicit + state validation).
        # When None is passed, memory degrades to keyword-only search instead
        # of silently re-initializing a vendor here, which would bypass the
        # caller's state checks and risk corrupting the index.
        self.embedding_provider = embedding_provider
        if self.embedding_provider is None:
            from common.log import logger
            # WARN, not INFO: a memory that can only do keyword search is a
            # degraded memory, and a degradation that is never logged is
            # indistinguishable from health ("静默降级比崩溃更危险").
            logger.warning(
                "[MemoryManager] No embedding provider; memory will use "
                "keyword search only (semantic matches will not be found)"
            )

        # Cache for query embeddings (avoids redundant API calls within a session)
        self._embedding_cache = EmbeddingCache()

        # P1 阶段二-B T3: the read-path freshness probe. Cached (P1-N2: a flood
        # of hits must not self-DoS on repeated stat). Retrieval is
        # fail-visible; content endpoints are fail-closed (风控 T3 裁决).
        try:
            from agent.memory.freshness import FreshnessProbe
            self._freshness = FreshnessProbe(self.config.get_workspace())
        except Exception:
            self._freshness = None

        # Optional reranker; None means un-reranked retrieval
        self.reranker = reranker

        # Initialize memory flush manager
        workspace_dir = self.config.get_workspace()
        self.flush_manager = MemoryFlushManager(
            workspace_dir=workspace_dir,
            llm_model=llm_model
        )
        
        # Ensure workspace directories exist
        self._init_workspace()
        
        # P0-11 P-2 (RR-04): the release projection. The flags on a chunk row
        # mirror the truth layer, and the truth layer moves without the index
        # moving (a release leaves the old bytes untouched), so the state is
        # re-resolved from the disk and re-stamped whenever the truth layer's
        # signature changed.
        self._release_state = ReleaseStateCache(self.config.get_workspace())
        self._release_signature = None

        self._dirty = False
    
    def _init_workspace(self):
        """Initialize workspace directories"""
        memory_dir = self.config.get_memory_dir()
        memory_dir.mkdir(parents=True, exist_ok=True)
        
        # Create default memory files
        workspace_dir = self.config.get_workspace()
        create_memory_files_if_needed(workspace_dir)
    
    @staticmethod
    def _file_visibility(content: str) -> Tuple[str, Optional[str]]:
        """``(visibility_class, owner_token)`` for a scanned file.

        ``visibility_class`` is one of open / released / sealed; the owner
        token is only meaningful for a dark page (it names the agent that
        wrote it, and only that agent may read it).

        P0-14 L-2: the rule is the ONE in ``agent.memory.visibility``. The
        old local rule ("visibility: sealed OR status: released → not part
        of the shared retrieval surface") treated every dark page as a
        release, so a dark page was invisible even to the agent that wrote
        it — directly against 06 §5.2 ("正文只对本视角与主人格开放").

        A file whose front-matter cannot be parsed is treated as RELEASED:
        we cannot read its visibility, and "cannot tell" must not mean
        "serve it" (P0-13 L-3: fail closed).
        """
        if not content.startswith("---\n"):
            return VIS_OPEN, None
        try:
            from agent.memory.frontmatter import split_frontmatter
            fm, _body = split_frontmatter(content)
        except Exception:
            return VIS_RELEASED, None
        return classify_frontmatter(fm), owner_token(fm)

    def _ensure_guards(self) -> Dict[str, Any]:
        """Make sure EVERY index that exists has the write-side guard (P0-22).

        This is the MECHANISM, not a snapshot: the guard is re-checked
        before every sync and at startup, over every index that exists on
        disk — the roster's AND the layout's (``agents/default`` is not in
        the roster, and a roster-only sweep leaves it unguarded).

        The check is read-only for a healthy index (the trigger body is
        compared against the current definition), so the common case costs
        one SELECT per index and writes nothing.

        A failure is LOUD: an index whose guard could not be installed is
        logged at ERROR level, because that index is one the old desktop
        build can turn a dark page into a public row.
        """
        from agent.memory.schema_migrate import ensure_guard_all

        workspace = self.config.get_workspace()
        try:
            states = ensure_guard_all(extra=workspace)
        except Exception as e:
            from common.log import logger
            logger.error(
                "[MemoryManager] The guard sweep failed: %s: %s",
                type(e).__name__, e)
            return {}
        repaired = {root: state for root, state in states.items()
                    if state.get("status") in ("installed", "repaired")}
        failed = {root: state for root, state in states.items()
                  if state.get("status") == "failed"}
        if repaired:
            from common.log import logger
            for root, state in sorted(repaired.items()):
                logger.warning(
                    "[MemoryManager] The write-side guard was %s on %s (%s)",
                    state.get("status"), root,
                    state.get("reason") or "no longer the current definition")
        if failed:
            from common.log import logger
            for root, state in sorted(failed.items()):
                logger.error(
                    "[MemoryManager] The write-side guard could NOT be "
                    "installed on %s: %s — rows that must not be public can "
                    "be written public on that index",
                    root, state.get("reason"))
        self._guard_states = states
        return states

    def guard_report(self, minutes: int = 60) -> Dict[str, Any]:
        """What did the guard drop? (P0-22)

        The operator-facing answer to "这次 sync 有多少行被守卫挡下了？挡了
        哪些？": the count, the paths, and the events themselves — read from
        the index, so the answer does not depend on anybody having been
        watching the log.
        """
        from agent.memory.index_guard import guard_report as _report

        try:
            return _report(self.storage.conn, minutes=minutes)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _discard_count(self) -> int:
        """How many writes the guard has dropped on THIS index."""
        from agent.memory.index_guard import discard_count

        try:
            return discard_count(self.storage.conn)
        except Exception:
            return 0

    def _certify_after_write(self) -> Dict[str, Any]:
        """Certify the rows this workspace just wrote (P1-阶段三 S3-K).

        **The question this answers:** "a knowledge page was written —
        when does its projection row become ``'mirror'``?"

        **The answer, before this method existed: never.**  The
        ``'mirror'`` state has a designated setter (the inspector), but
        the inspector only ran when somebody ran it, and only ever
        looked at ONE workspace.  So a row written after the last
        inspection stayed ``'projection'`` forever, and the other
        fifteen libraries of the family were never certified at all.

        **The mechanism now:** the write path certifies what it wrote,
        immediately, in-process.  There is no new daemon, no new
        scheduler entry and no new trigger point — the certification
        rides on the write that made the row uncertifiable.

        **Why after the write and not inside it.**  The certification
        reads the TRUTH LAYER and compares the fragment with the stored
        text.  The truth file must exist and be complete before the
        comparison means anything, and the file is written by the
        caller (the knowledge skill writes the page, then the index is
        updated).  Certifying inside the write would race the file.

        **What it does NOT do.**  It never certifies a row that does not
        pass the byte test, and it never certifies a row whose text is
        not a chunk of its file.  A refusal is counted and logged; the
        row stays ``'projection'``, which is the honest state for "not
        verified yet".

        The certification is REPORT-ONLY for the caller: a failure
        here must never break the write that succeeded.
        """
        try:
            from agent.memory.certify import certify_library
        except Exception:
            return {"error": "certify module unavailable"}
        try:
            workspace = Path(self.config.get_workspace())
            db = Path(self.storage.db_path) if hasattr(self.storage, "db_path") \
                else None
            if db is None:
                return {"error": "the storage does not expose its path"}
            report = certify_library(workspace, db, name=workspace.name)
        except Exception as e:
            from common.log import logger
            logger.warning(
                "[MemoryManager] The certification after the write did not "
                "complete: %s: %s", type(e).__name__, e)
            return {"error": "%s: %s" % (type(e).__name__, e)}
        if report.get("promoted") or report.get("derived"):
            from common.log import logger
            logger.info(
                "[MemoryManager] certified %d row(s) (%d pointer(s) "
                "derived) in %s", report.get("promoted", 0),
                report.get("derived", 0), report.get("db"))
        return report

    def _report_guard_discards(self, before: int) -> Dict[str, Any]:
        """Say out loud what the guard dropped during this sync (P0-22).

        ``RAISE(IGNORE)`` is a silent operation, and a silent correction
        is the failure mode the guard itself was created to end. So the
        delta around the write phase is turned into an ERROR log line and
        a report the operator can read back — "挡了多少、挡了哪些".

        The log is at ERROR level on purpose: a dropped write means a
        writer tried to publish something that must not be published, and
        the reader of the log needs to see it without turning on a filter.
        """
        after = self._discard_count()
        dropped = max(0, after - before)
        if not dropped:
            return {"dropped": 0}
        from agent.memory.index_guard import recent_discards
        try:
            events = recent_discards(self.storage.conn, minutes=5, limit=50)
        except Exception:
            events = []
        paths: Dict[str, int] = {}
        for item in events:
            paths[item.get("path") or "?"] = paths.get(item.get("path") or "?", 0) + 1
        try:
            from common.log import logger
            logger.error(
                "[MemoryManager] The write-side guard dropped %d write(s) "
                "during this sync on %s — the rows that must not be public "
                "were rewritten (paths: %s). The writer was told nothing; "
                "the truth layer and the index now agree, but the writer "
                "does not know it.",
                dropped, self.config.get_workspace(),
                ", ".join(sorted(paths)[:10]) or "?")
        except Exception:
            pass
        return {"dropped": dropped, "paths": paths, "events": events}

    def _record_guard_paths(self, release_state) -> int:
        """Tell the write-side guard what the truth layer says (P0-21).

        The guard is a database trigger: it keeps a row that must not be
        public from being written public, whoever writes it — including
        the old desktop build that knows nothing about ``visibility:
        sealed``. For that it needs to know which paths are sealed, which
        are released, and who owns a dark page.

        The truth layer is the authority for all three, and this is the
        one place in the write path that has it in hand. The guard's own
        front-matter fallback covers the window before the first sync
        (the old build indexing a dark page this build has never seen),
        but it deliberately does not guess an owner: a dark page whose
        owner is unknown is invisible to everybody, and the next sync
        fills the owner in.

        Returns the number of paths recorded.
        """
        from agent.memory.index_guard import (
            CLASS_RELEASED,
            CLASS_SEALED,
            record_paths,
        )

        entries = []
        owners = dict(release_state.sealed_owner or {})
        for path in release_state.sealed_paths:
            entries.append((path, CLASS_SEALED, owners.get(path)))
        for path in release_state.released_paths:
            entries.append((path, CLASS_RELEASED, None))
        if not entries:
            return 0
        try:
            return record_paths(self.storage.conn, entries)
        except Exception as e:
            # The guard is a safety net, not the primary mechanism: the
            # primary one is that the scan never writes a dark page as a
            # shared row in the first place. A failure here must not stop
            # the sync, but it must be loud — a guard that is silently not
            # installed is a guard that does not exist.
            from common.log import logger
            logger.warning(
                f"[MemoryManager] Could not record the guarded paths: {e}")
            return 0

    def _refresh_release_projection(self) -> None:
        """Re-stamp the release flags of the index from the truth layer.

        P0-11 P-2 (RR-04): a release does NOT change the released file's
        bytes (WR-11 keeps the old version untouched), so the scan never
        re-indexes it and the index would keep serving the released text.
        The flags are therefore refreshed from the DISK — the authority —
        before every retrieval, but only when the truth layer's signature
        actually moved (the refresh itself is a write).

        This is a PROJECTION refresh, not a repair: it only sets the two
        release flags (and re-owns the dark pages), and only on rows that
        already exist. It never deletes, never rewrites content, and never
        touches the truth layer.

        P0-13 L-3 — FAIL CLOSED. When the truth layer cannot be resolved,
        this RAISES instead of continuing with the stale marker. The old
        behaviour (swallow the error, keep the stale flags, warn in the
        log) meant that a refresh failure right after a release served the
        released body again — a privacy control that breaks open, silently.
        The failure is loud: an error log, an audit line, and a marker file
        the inspector reports.
        """
        try:
            cache = getattr(self, "_release_state", None)
            if cache is None:
                # A manager built without a truth layer (a stub in a unit
                # test): there is nothing that could be released, so there
                # is nothing to judge. Not a fail-open hole — a real
                # manager always has the cache.
                return
            state = cache.get()
        except Exception as e:
            self._report_projection_failure("resolve", e)
            raise
        # The state is known again — the retrieval is no longer degraded,
        # so the marker goes away even when nothing had to be re-stamped.
        self._clear_projection_failure()
        if state.signature == self._release_signature:
            return
        try:
            changed = self.storage.apply_release_projection(
                set(state.released_paths), set(state.sealed_paths),
                dict(state.sealed_owner))
        except Exception as e:
            self._report_projection_failure("apply", e)
            raise
        self._release_signature = state.signature
        if changed:
            from common.log import logger
            logger.info(
                "[MemoryManager] Release projection refreshed: %d path(s) "
                "re-flagged", changed)

    # -- the fail-closed marker (P0-13 L-3) ---------------------------------

    @property
    def _projection_failure_marker(self) -> Path:
        """Where the degraded-projection marker lives (audit layer)."""
        return self.config.get_memory_dir() / ".audit" / "release_projection.json"

    def _report_projection_failure(self, stage: str, error: Exception) -> None:
        """Record a failed projection refresh — loudly, and where a巡检 finds it.

        Three places, because "a log line nobody reads" is not a signal:
        the error log, an audit file under ``memory/.audit/`` (excluded from
        the index by policy), and a marker file the inspector turns into a
        red report.
        """
        from common.log import logger
        logger.error(
            "[MemoryManager] Release projection %s FAILED: %s — the "
            "retrieval exits fail closed until it succeeds", stage, error)
        marker = None
        try:
            marker = self._projection_failure_marker
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({
                "stage": stage,
                "error": str(error),
                "at": _utc8_now(),
                "effect": "retrieval exits fail closed",
            }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n")
        except Exception as e:      # the marker must never mask the failure
            logger.error("[MemoryManager] Cannot write the projection "
                         "failure marker: %s", e)

    def _clear_projection_failure(self) -> None:
        """Remove the degraded marker after a successful refresh."""
        try:
            self._projection_failure_marker.unlink(missing_ok=True)
        except Exception:
            pass

    # -- the shared read decision (P0-13 L-1) --------------------------------

    def release_state(self):
        """The truth layer's release state — the authority for every read.

        Read exits must not each re-derive "is this released / sealed" —
        that is how ``memory_get`` came to hand out a released body in
        full while every retrieval path filtered it (P0-13 L-1).
        """
        return self._release_state.get()

    def read_decision(self, rel_path: str) -> Tuple[str, str, FrozenSet[str]]:
        """``(decision, reason, released_fact_ids)`` for a read exit.

        The third element is the set of fact rows (the 感受 of released
        elements) that the reading view must mask — P0-16 F-1. It travels
        with the decision so a read exit cannot invent a masking rule of
        its own.

        Raises when the truth layer cannot be resolved: an unknown release
        state must fail the read, not serve the content (P0-13 L-3).
        P0-16 L-4: that refusal is LOUD — an ERROR log, an audit line and
        a marker the inspector reports red.
        """
        cache = getattr(self, "_release_state", None)
        if cache is None:
            return READ_ALLOW, "", frozenset()
        from agent.memory.read_audit import (
            clear_read_denied,
            report_read_denied,
        )
        workspace = self.config.get_workspace()
        try:
            state = cache.get()
        except Exception as e:
            report_read_denied(workspace, "MemoryManager.read_decision",
                               rel_path, e)
            raise
        # The truth layer resolves again → the degraded state is over.
        clear_read_denied(workspace)
        # The sealed branch is matched against the OWNER axis only
        # (P0-16 L-5).
        owners = owner_tokens(ambient_owner_tokens())
        return state.decide(rel_path, owners)

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        max_results: Optional[int] = None,
        min_score: Optional[float] = None,
        include_shared: bool = True
    ) -> List[SearchResult]:
        """
        Search memory with hybrid search (vector + keyword)
        
        Args:
            query: Search query
            user_id: User ID for scoped search
            max_results: Maximum results to return
            min_score: Minimum score threshold
            include_shared: Include shared memories
            
        Returns:
            List of search results sorted by relevance
        """
        max_results = max_results or self.config.max_results
        # min_score=0 means "no threshold", so only None takes the default.
        min_score = self.config.min_score if min_score is None else min_score

        # An explicit user_id wins; otherwise follow the ambient runtime
        # identity, so CLI / scheduler / sub-agent paths that never thread a
        # user id still see exactly what their identity may see — no more,
        # no less. None stays None: anonymous sees shared only.
        if user_id is None:
            try:
                from common.runtime_identity import current_identity
                ambient = current_identity().user_id
                if ambient:
                    user_id = str(ambient)
            except Exception:
                pass

        # P0-14 L-2 / P0-16 L-5: the caller's identities, kept on TWO axes.
        # The end user is matched against ``user_id``; the agent is matched
        # against ``sealed_owner`` (the owner token a dark page is indexed
        # under). Mixing them into one token set is what let an end-user
        # id that happened to equal ``agent--<some agent>`` read a dark
        # page (P0-15 L-5).
        identities = ambient_end_user_ids()
        tokens = identity_tokens(user_id, identities)
        owners = owner_tokens(ambient_owner_tokens())

        # Determine scopes
        scopes = []
        if include_shared:
            scopes.append("shared")
        if tokens or owners:
            # 'user' rows belong to an end user; 'sealed' rows are dark
            # pages, and only their owner's token can match them.
            scopes.append("user")
            scopes.append(SCOPE_SEALED)
        
        if not scopes:
            return []
        
        # Sync if needed
        if self.config.sync_on_search and self._dirty:
            await self.sync()

        # P0-11 P-2 (RR-04): the release flags are refreshed from the truth
        # layer BEFORE the query runs, so a released element leaves the
        # result set for every identity — anonymous, third party and owner
        # alike. The refresh is a projection of the disk, not a repair.
        self._refresh_release_projection()

        from common.log import logger

        # Perform vector search (if embedding provider available).
        # Failures degrade silently to keyword-only — no exception is raised.
        vector_results = []
        if self.embedding_provider:
            try:
                provider_name = type(self.embedding_provider).__name__
                model_name = getattr(self.embedding_provider, 'model', '')
                cached = self._embedding_cache.get(query, provider_name, model_name)
                if cached is not None:
                    query_embedding = cached
                else:
                    query_embedding = self.embedding_provider.embed_query(query)
                    self._embedding_cache.put(query, provider_name, model_name, query_embedding)
                vector_results = self.storage.search_vector(
                    query_embedding=query_embedding,
                    user_id=user_id,
                    scopes=scopes,
                    identities=identities,
                    owner_tokens=owners,
                    limit=max_results * 2  # Get more candidates for merging
                )
                logger.info(f"[MemoryManager] Vector search found {len(vector_results)} results for query: {query}")
            except Exception as e:
                logger.error(
                    f"[MemoryManager] Vector search failed, falling back to keyword-only: {e}"
                )

        # Perform keyword search (also runs as fallback when vector failed)
        keyword_results = self.storage.search_keyword(
            query=query,
            user_id=user_id,
            scopes=scopes,
            identities=identities,
            owner_tokens=owners,
            limit=max_results * 2
        )
        logger.info(f"[MemoryManager] Keyword search found {len(keyword_results)} results for query: {query}")

        # Merge results
        merged = self._merge_results(
            vector_results,
            keyword_results,
            self.config.vector_weight,
            self.config.keyword_weight
        )

        # min_score is a cutoff on the fused score, so it is applied before
        # rerank; the reranker only reorders the candidates that pass it.
        filtered = [r for r in merged if r.score >= min_score]
        filtered = self._rerank(query, filtered)
        # The hit boost runs last, as a pure reordering multiplier over the
        # final candidates: it rewards chunks that proved useful before but
        # can neither rescue below-threshold results nor bypass the reranker.
        filtered = self._apply_hit_boost(filtered)
        top = filtered[:max_results]

        # Count one retrieval hit per returned chunk, as ranking feedback for
        # the hit-popularity boost. Opt-in only: with the boost disabled the
        # write transaction is skipped entirely. Bookkeeping must never break
        # the search.
        if top and self._hit_boost_enabled():
            try:
                self.storage.record_hits(
                    [(r.path, r.start_line, r.end_line) for r in top]
                )
            except Exception as e:
                logger.warning(f"[MemoryManager] Hit write-back failed: {e}")

        # P1 阶段二-B T3: read-path freshness probe on the RETRIEVAL channel.
        # Fail-VISIBLE: stale candidates are still returned (retrieval has
        # many candidates + metadata) but each is flagged and the stale
        # counter rises, so get_status() self-reports the drift (风控: 自曝
        # 不能是撞运气自曝). Content endpoints use read_content_fresh() which
        # is fail-CLOSED.
        if top and self._freshness is not None:
            try:
                mtime_by_path = self._files_mtime_map(
                    {r.path for r in top})
                for r in top:
                    r.stale = self._freshness.is_stale(
                        r.path, mtime_by_path.get(r.path))
            except Exception as e:
                # The probe must never break retrieval — a probe failure is
                # logged, the results still returned (fail-visible channel).
                logger.warning(f"[MemoryManager] freshness probe failed: {e}")

        return top

    def _files_mtime_map(self, paths) -> Dict[str, Optional[float]]:
        """files.mtime for the given paths, one query. Missing -> None."""
        out: Dict[str, Optional[float]] = {}
        try:
            for p in paths:
                row = self.storage.conn.execute(
                    "SELECT mtime FROM files WHERE path=?", (p,)).fetchone()
                out[p] = float(row[0]) if row and row[0] is not None else None
        except Exception:
            pass
        return out

    def read_content_fresh(self, relpath: str) -> Dict[str, Any]:
        """Content endpoint (fail-CLOSED): serve the truth file only if fresh.

        风控 T3 裁决: a content/drill-down/readability endpoint must NOT serve
        stale content — returning old content as if it were fact is worse than
        refusing. So when the truth file is newer than the index recorded
        (or unreadable), this refuses with ok=False + reason, instead of
        serving. Returns the fresh body text when ok=True.
        """
        # C-6/C-7: the path rule is SHARED with INV-11 (agent.memory.paths)
        # — one rule, two callers, and the NORMALIZATION lives there too.
        # The caller must NOT strip a leading "/" itself: that turned
        # "reject this absolute path" into "serve this file inside the
        # workspace" (C-7), and it silently defeated the absolute-path
        # rule the design states. Fail-CLOSED: an unjudgeable path is
        # refused, never served.
        from agent.memory.paths import normalize_relpath, safe_join
        real, why = safe_join(self.config.get_workspace(), relpath)
        if real is None:
            return {"ok": False, "path": str(relpath),
                    "reason": f"unsafe path: {why} — refusing to serve "
                              f"content outside the workspace"}
        norm = normalize_relpath(relpath)
        indexed = self._files_mtime_map({norm}).get(norm)
        if self._freshness is None:
            return {"ok": False, "path": norm,
                    "reason": "freshness probe unavailable (fail-closed)"}
        if self._freshness.is_stale(norm, indexed):
            return {"ok": False, "path": norm, "stale": True,
                    "reason": "content is stale (index behind truth file); "
                              "refusing to serve old content as fact"}
        try:
            body = real.read_text(encoding="utf-8")
        except OSError as e:
            return {"ok": False, "path": norm,
                    "reason": f"truth file unreadable: {e}"}
        return {"ok": True, "path": norm, "stale": False, "content": body}

    def get_freshness_status(self) -> Dict[str, Any]:
        """The probe counters, for get_status()."""
        if self._freshness is None:
            return {"checked": 0, "stale": 0, "cache_hits": 0,
                    "probe": "unavailable"}
        s = self._freshness.status()
        s["probe"] = "active"
        return s

    async def add_memory(
        self,
        content: str,
        user_id: Optional[str] = None,
        scope: str = "shared",
        source: str = "memory",
        path: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Add new memory content
        
        Args:
            content: Memory content
            user_id: User ID for user-scoped memory
            scope: Memory scope ("shared", "user", "session")
            source: Memory source ("memory" or "session")
            path: File path (auto-generated if not provided)
            metadata: Additional metadata
        """
        if not content.strip():
            return

        # P0-16 L-5: an end-user id that claims the agent namespace is
        # REJECTED ON WRITE. The two axes are separate namespaces
        # (``chunks.user_id`` for end users, ``chunks.sealed_owner`` for
        # agent owner tokens); an id shaped like an owner token would
        # otherwise make a dark page readable by an end user. Accepting
        # it quietly would leave the collision in the index for every
        # later reader to trip over.
        if user_id is not None:
            from agent.memory.visibility import is_reserved_end_user_id

            if is_reserved_end_user_id(user_id):
                raise ValueError(
                    f"user_id {user_id!r} claims the agent namespace "
                    f"(prefix 'agent--'); the end-user axis and the agent "
                    f"owner axis are separate — choose another id"
                )

        # Generate path if not provided
        if not path:
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()[:8]
            if user_id and scope == "user":
                # The directory name is a filesystem-safe slug, never the raw
                # id: ':' is drive syntax on Windows, and an unparseable
                # directory name used to degrade every identity to 'a'.
                # The LOGICAL id stays verbatim in the chunks.user_id column;
                # users.json at the index root maps slug -> logical id.
                from agent.memory.identity_ns import user_dir_name
                dir_name = user_dir_name(
                    user_id, workspace=self.config.get_workspace()
                )
                path = f"memory/users/{dir_name}/memory_{content_hash}.md"
            else:
                path = f"memory/shared/memory_{content_hash}.md"
        
        # Chunk content.
        #
        # G-3: this channel is NOT part of the file-projection path — it
        # stores a memory WITHOUT writing a truth file, so there is no
        # rebuild that could contradict it, and its path is a synthetic
        # name for a blob of text rather than a markdown document. It
        # therefore keeps the plain splitter. The FILE projection path
        # (sync() below and rebuilder.rebuild_file) is the one that must
        # agree, and both call chunker.chunk_for_file — the single
        # decision point.
        chunks = self.chunker.chunk_text(content)
        
        # Generate embeddings (if provider available)
        texts = [chunk.text for chunk in chunks]
        if self.embedding_provider:
            embeddings = self.embedding_provider.embed_batch(texts)
        else:
            # No embeddings, just use None
            embeddings = [None] * len(texts)
        
        # Create memory chunks
        #
        # P1-阶段三 (H1 · C-A2): the four body-externalization columns are
        # filled HERE.  This write path did not fill them at all (the whole
        # file had ZERO hits for `text_state`), so every row it wrote had
        # `body_path IS NULL` — a pointer that points nowhere, which is
        # exactly what INV-1 reports.  A migration would have backfilled the
        # existing rows and the next sync would have written new NULL rows
        # again, and INV-1 would have gone RED on the rows the migration had
        # just fixed.
        #
        # `add_memory` stores a blob of text under a SYNTHETIC path — it
        # does not write a truth file — so there is no file to point at and
        # no fragment to hash.  The honest value for `body_path` is None:
        # the row has no truth-layer body yet.  The anchor and the hash are
        # still filled from the chunk itself, so the row carries a complete
        # drift token rather than four empty columns.
        memory_chunks = []
        for chunk, embedding in zip(chunks, embeddings):
            chunk_id = self._generate_chunk_id(path, chunk.start_line, chunk.end_line, chunk.part)
            chunk_hash = MemoryStorage.compute_hash(chunk.text)

            # The path is the row's OWN key; it is checked with the ONE
            # path-safety rule (agent.memory.paths), never with a second
            # spelling of it.
            body_path, body_anchor, body_hash, text_state, body_origin = (
                self._body_columns_for(
                    relpath=path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    part=chunk.part,
                    chunk_text=chunk.text,
                    workspace_root=None,
                    segments=list(chunk.segments or []),
                    gaps=list(chunk.gaps or []),
                )
            )

            memory_chunks.append(MemoryChunk(
                id=chunk_id,
                user_id=user_id,
                scope=scope,
                source=source,
                path=path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                text=chunk.text,
                embedding=embedding,
                hash=chunk_hash,
                metadata=metadata,
                body_path=body_path,
                body_anchor=body_anchor,
                body_hash=body_hash,
                text_state=text_state,
                body_origin=body_origin,
            ))
        
        # Save to storage
        self.storage.save_chunks_batch(memory_chunks)
        
        # Update file metadata
        file_hash = MemoryStorage.compute_hash(content)
        # P1-阶段三 (C-A3): the real chunk count and the chunker version,
        # so the files row states a fact instead of defaulting to 0.
        self.storage.update_file_metadata(
            path=path,
            source=source,
            file_hash=file_hash,
            # C-5: this used to be ``int(os.path.getmtime(__file__))`` — the
            # mtime of manager.py ITSELF, with a comment claiming "current
            # time". That is a CONSTANT (frozen at deploy time), so the
            # recorded mtime never tracked the content: the freshness probe
            # compared a content that changed against a timestamp that
            # could not — a permanent verdict, in either direction. Same
            # class of bug as C-3. Now it is the actual write moment, in
            # whole seconds — the same precision the other writers use.
            mtime=int(time.time()),
            size=len(content),
            chunk_count=len(memory_chunks),
            chunker_version=TextChunker.CHUNKER_VERSION,
        )

        # P1-阶段三 S3-K: certify what this write produced.
        #
        # ``add_memory`` writes a BLOB — no truth file, so its own row
        # is honestly ``'blob'`` and stays ``'projection'`` (a blob has
        # nothing to mirror).  But the certification still runs, because
        # this call may ALSO have written rows that DO have a truth
        # file, and those must not be left uncertified.
        self._certify_after_write()

    async def sync(self, force: bool = False):
        """
        Synchronize memory from files.

        Two-pass design to amortize embedding HTTP cost:
          1. Walk all files, chunk those whose hash changed, collect pending
             chunks across files. No embedding calls yet.
          2. Run a single embed_batch over the union of pending chunks (the
             provider auto-paginates by vendor cap), then persist per-file.

        For workspaces with many small files (101 files / ~1 chunk each), this
        cuts ~100 HTTP calls down to ~ceil(total_chunks / vendor_cap).

        Args:
            force: Force full reindex
        """
        memory_dir = self.config.get_memory_dir()
        workspace_dir = self.config.get_workspace()
        from common.log import logger

        # P0-22: the guard is re-checked before every sync, over EVERY
        # index that exists on disk. This is the mechanism that makes the
        # guard a property of the system rather than a thing somebody did
        # once: an index whose trigger was deleted (or left over from an
        # older build) is repaired here, before a single row is written.
        self._ensure_guards()
        # The number of writes the guard has dropped SO FAR on this index.
        # The delta around the write phase below is what this sync
        # dropped — the answer to "这次 sync 挡了多少行".
        discards_before = self._discard_count()

        # Was the index empty before this sync? Only then can we be sure every
        # chunk came from the CURRENT chunking algorithm (a fresh index, or one
        # cleared by rebuild-index), which is what lets us stamp
        # chunker_version. A non-empty index may still hold chunks from an
        # older algorithm, so it must not be stamped.
        try:
            files_before = int(self.storage.get_stats().get("files", 0) or 0)
        except Exception:
            files_before = -1  # unknown -> do not stamp

        files_to_scan: List[tuple] = []  # (file_path, source, scope, user_id)

        # The workspace root holds the identity documents (USER.md says who
        # the user is, AGENT.md who the Agent is, RULE.md how it behaves).
        # The two directory walks below never reach the root, so without this
        # they were invisible to search — "王静静是谁" could not be answered
        # even though USER.md says exactly who she is.
        for name in _ROOT_IDENTITY_FILES:
            identity_file = Path(workspace_dir) / name
            if identity_file.exists():
                files_to_scan.append((identity_file, "memory", "shared", None))

        if memory_dir.exists():
            from agent.memory.identity_ns import parse_user_dir_name

            def _resolve_user_segment(raw_segment):
                """Directory name -> LOGICAL user id via identity_ns
                (user--<slug> -> users.json -> id; bare legacy names resolve
                to themselves). An unresolvable segment raises: it must not
                silently become an id."""
                if raw_segment is None:
                    return None
                logical = parse_user_dir_name(raw_segment, workspace=workspace_dir)
                # P0-16 L-5: a legacy bare segment can still be spelled
                # ``agent--<something>``. That is the agent namespace, and
                # the end-user axis may not carry it — so the row is
                # indexed as SHARED (no owner) rather than as a user row.
                # Fail closed: an id we cannot place belongs to nobody.
                if logical is not None:
                    from agent.memory.visibility import is_reserved_end_user_id

                    if is_reserved_end_user_id(logical):
                        return None
                return logical

            for file_path in memory_dir.rglob("*.md"):
                rel_parts = file_path.relative_to(workspace_dir).parts
                if any(part.startswith('.') for part in rel_parts):
                    continue
                # Dream diaries are narrative reflections produced by Deep
                # Dream; their factual content has already been distilled
                # into MEMORY.md. Indexing them adds noisy near-duplicates
                # that crowd out the authoritative entry in retrieval.
                if "dreams" in rel_parts:
                    continue
                # Live-memory v2 layers: shelf/ is a REBUILDABLE projection
                # (directory cards for humans) — indexing it would create a
                # self-loop where search results are full of summaries of
                # summaries. facts/ is the append-only evidence room (JSONL,
                # never .md today); it is referenced by pointer, not indexed.
                # .snapshots/ + .audit/ are the inspector's own bookkeeping
                # (baseline + report trail, P0-8) — same reason as shelf/.
                # _tombstones/ (P0-11 P-3) are the release ledger: they carry
                # original_path / consent_ref / the confirmation token hash,
                # and a tombstone row in the index would be scope='shared' +
                # user_id=NULL, i.e. readable by ANY caller (V2-Q-01).
                # markdown/ stays indexed on purpose: it IS the truth layer.
                #
                # The set itself lives in agent/memory/layers.py so the scan
                # and the inspector's check 5 cannot drift apart again.
                if excluded_segments("/".join(rel_parts)):
                    continue
                try:
                    if "users" in rel_parts:
                        # memory/users/<id>/... wins over a "daily" segment:
                        # in users/<id>/daily/<date>.md the part after
                        # "daily" is the FILENAME, not the user.
                        user_idx = rel_parts.index("users") + 1
                        user_id = _resolve_user_segment(
                            rel_parts[user_idx] if user_idx < len(rel_parts) else None
                        )
                        scope = "user"
                    elif "daily" in rel_parts:
                        if len(rel_parts) > 3:
                            user_idx = rel_parts.index("daily") + 1
                            user_id = _resolve_user_segment(
                                rel_parts[user_idx] if user_idx < len(rel_parts) else None
                            )
                            scope = "user"
                        else:
                            user_id = None
                            scope = "shared"
                    else:
                        user_id = None
                        scope = "shared"
                except Exception as e:
                    logger.warning(
                        f"[MemoryManager] Skipping {file_path}: user "
                        f"directory does not resolve ({e})"
                    )
                    continue
                files_to_scan.append((file_path, "memory", scope, user_id))

        from common import state_dir
        from config import conf
        knowledge_dir: Optional[Path] = None
        if conf().get("knowledge", True):
            # Resolve through state_dir so an Agent without its own knowledge/
            # scans the shared base rather than an empty (or missing) local one.
            knowledge_dir = Path(state_dir.knowledge_dir(base=workspace_dir))
            if knowledge_dir.exists():
                for file_path in knowledge_dir.rglob("*.[mM][dD]"):
                    # The root index.md / log.md only restate pages indexed on
                    # their own, and change on every page write, which re-embeds
                    # the whole file each time.
                    if file_path.parent == knowledge_dir and file_path.name in ("index.md", "log.md"):
                        continue
                    files_to_scan.append((file_path, "knowledge", "shared", None))

        # Pass 1: inline chunking + change detection. Inlined (instead of
        # calling self._prepare_file_for_sync) so this method does not depend
        # on any sibling helpers — keeps it robust against partial reloads
        # where the class object is older than the method's source.
        pending: List[Dict[str, Any]] = []
        workspace_dir_path = self.config.get_workspace()

        # Roots a scanned file may legitimately sit under, most specific first,
        # each paired with the prefix its files are keyed under. Resolved once
        # rather than per file: shared_root() goes through the Agent registry.
        # knowledge_dir comes first so a knowledge file keys the same whether it
        # is this Agent's own copy or the shared one it fell back to.
        index_roots: List[Tuple[Path, str]] = []
        if knowledge_dir is not None:
            index_roots.append((knowledge_dir, "knowledge"))
        index_roots.append((Path(workspace_dir_path), ""))
        try:
            index_roots.append((state_dir.shared_root(), ""))
        except Exception:
            # No resolvable shared root (registry not ready). The workspace
            # still covers every file that is not a shared fallback.
            pass

        scanned: Dict[str, set] = {}

        for file_path, source, scope, user_id in files_to_scan:
            rel_path = _index_rel_path(file_path, index_roots)
            if rel_path is None:
                # Defensive: every scan target above resolves under one of the
                # roots. Skip just this file so one oddity cannot cost the Agent
                # its whole memory, and say so rather than dropping it silently.
                logger.warning(
                    f"[MemoryManager] Skipping {file_path}: it sits under none of "
                    f"this Agent's roots, so it has no stable index key"
                )
                continue
            # Recorded before the read, so a file that is present but unreadable
            # still counts as scanned and keeps the rows it already has.
            scanned.setdefault(source, set()).add(rel_path)
            try:
                content = file_path.read_text(encoding='utf-8')
            except Exception as e:
                # Was silent, which made a half-indexed workspace look healthy.
                logger.warning(f"[MemoryManager] Skipping {file_path}: cannot read it ({e})")
                continue
            file_hash = MemoryStorage.compute_hash(content)
            # P0-11 P-2 / G4-e: a file that IS a release does not belong in
            # the index at all. The release event carries the consent_ref
            # and the release metadata in its body, and a release record is
            # the owner's business — indexing it made every release's
            # metadata anonymously readable (V2-Q-01).
            #
            # The check is on the FILE's own front-matter, so it is a
            # property of the disk, not of the index. A file that is
            # released but whose front-matter still says active (the
            # original version — WR-11 leaves its bytes untouched) is
            # handled by the release projection instead: it stays in the
            # index, flagged, and the visibility predicate hides it.
            #
            # The path is removed from the scan set, so the reconcile drops
            # any rows an older build left for it.
            #
            # P0-14 L-2: a DARK PAGE (visibility: sealed, not a release) is
            # NOT a release. It stays in the index under its own scope
            # with its owner recorded, so the owner can read it and nobody
            # else can (06 §5.2).
            visibility, sealed_owner = self._file_visibility(content)
            if visibility == VIS_RELEASED:
                scanned.get(source, set()).discard(rel_path)
                continue
            if visibility == VIS_SEALED:
                scope = SCOPE_SEALED
                # P0-16 L-5: the owner token does NOT go into the end-user
                # axis. ``chunks.user_id`` holds END USER ids only; the
                # owner of a dark page lives in ``chunks.sealed_owner``.
                # Writing it into both columns is what let an end-user id
                # shaped like an owner token read a dark page — the row
                # matched on the axis that was never supposed to carry it.
                user_id = None
            previous_hash = self.storage.get_file_hash(rel_path)
            if previous_hash == file_hash:
                continue
            # G-3: the chunking DECISION lives in ONE place
            # (chunker.chunk_for_file) — the rebuilder calls the same
            # function, so a rebuild refreshes the projection instead of
            # replacing the chunk identities.
            chunks = chunk_for_file(content, rel_path)
            if not chunks and previous_hash is None:
                continue
            pending.append({
                "file_path": file_path,
                "rel_path": rel_path,
                "source": source,
                "scope": scope,
                "user_id": user_id,
                # P0-16 L-5 / B-2: the dark page's owner token travels on its
                # OWN column. The `user_id` above keeps the legacy
                # end-user/owner sharing for backwards compatibility, but
                # the sealed branch of the predicate reads ONLY
                # `sealed_owner` — so an end-user id that happens to look
                # like an owner token cannot reach a dark page.
                "sealed_owner": sealed_owner if visibility == VIS_SEALED else None,
                "file_hash": file_hash,
                "chunks": chunks,
                "texts": [c.text for c in chunks],
            })

        # Reconcile before the early return below: an index full of stale rows
        # is exactly the case where nothing changed and `pending` is empty.
        #
        # sync() only ever added and overwrote. Anything that left a scanned
        # root — a page the user deleted, or every shared page at once when an
        # Agent is switched to its own knowledge/ — stayed in the index and kept
        # being returned by memory_search long after no tool could open it.
        #
        # P0-10 L-1 (two holes closed here):
        #   ① EXCLUDED paths. The live-memory layers the scan skips (shelf/,
        #      facts/, .snapshots/, .audit/) were indexed before the skip
        #      existed; their rows stayed behind and, being scope='shared' +
        #      user_id=NULL, stayed anonymously searchable — a released
        #      record's directory card leaking into third-party retrieval
        #      (V2-Q-01). They are stale by definition now, so the reconcile
        #      drops them.
        #   ② ORPHAN chunk rows. list_paths() reads the `files` table; a
        #      chunk row whose `files` counterpart was lost is invisible
        #      there, so the reconcile could never see it and it survived
        #      forever. list_chunk_paths() closes that.
        #
        # Only a source whose root resolved and exists is reconciled. An empty
        # scan set then means the files are genuinely gone, rather than that the
        # root was momentarily unresolvable, which must not cost a whole index.
        reconcilable = []
        if memory_dir.exists():
            reconcilable.append("memory")
        if knowledge_dir is not None and knowledge_dir.exists():
            reconcilable.append("knowledge")
        for source in reconcilable:
            try:
                indexed = set(self.storage.list_paths(source))
                indexed |= set(self.storage.list_chunk_paths(source))
                stale = indexed - scanned.get(source, set())
            except Exception as e:
                logger.warning(f"[MemoryManager] Cannot reconcile {source} index: {e}")
                continue
            dropped = 0
            for stale_path in stale:
                try:
                    self.storage.delete_by_path(stale_path)
                    dropped += 1
                except Exception as e:
                    logger.warning(f"[MemoryManager] Cannot drop stale entry {stale_path}: {e}")
            if dropped:
                logger.info(
                    f"[MemoryManager] Dropped {dropped} stale {source} "
                    f"entries that are no longer on disk"
                )

        if not pending:
            # P0-11 P-2: nothing changed on disk, but the truth layer's
            # RELEASE state may have moved (a release writes a new version
            # and a tombstone, and leaves the released file's own bytes
            # untouched — so the scan above sees no change for it). The
            # flags are re-stamped from the disk before returning, otherwise
            # a released element would stay retrievable until the next
            # file change.
            self._refresh_release_projection()
            self._dirty = False
            # Nothing was written, but the guard may still have dropped
            # something (another writer shares this index), so the delta is
            # reported here too — a silent drop is a silent drop.
            self._report_guard_discards(discards_before)
            return

        # Pass 2: single batched embed across all pending chunks.
        # CRITICAL: never touch the index until we hold valid embeddings.
        # If embed_batch fails, leave the existing index intact (chunks +
        # file_hash) so the next sync will retry the same files. Writing
        # NULL embeddings + updating file_hash here would mark the file as
        # "successfully synced" and silently strand it without vectors.
        all_texts: List[str] = []
        for entry in pending:
            all_texts.extend(entry["texts"])

        if not all_texts or not self.embedding_provider:
            # Empty files still replace their index entry without an API call.
            # Keyword-only indexes persist nonempty chunks without embeddings.
            all_embeddings: List[Optional[List[float]]] = [None] * len(all_texts)
        else:
            try:
                all_embeddings = self.embedding_provider.embed_batch(all_texts)
            except Exception as e:
                from common.log import logger
                logger.error(
                    f"[MemoryManager] Batch embedding failed for {len(all_texts)} "
                    f"chunks across {len(pending)} files: {e}. "
                    f"Index left untouched; will retry on next sync."
                )
                # Bail before touching storage. self._dirty stays True so
                # callers know there is pending work.
                return

        # Pass 3: inline persist — same self-contained reasoning as Pass 1.
        cursor = 0

        # P0-11 P-2 (RR-04): the release flags are stamped on every chunk as
        # it is written, from the TRUTH LAYER (the authority) — not from the
        # index, which is the projection. Resolved once per sync, because a
        # release does not change the released file's bytes (WR-11), so the
        # scan above would never re-index it.
        try:
            release_state = self._release_state.get()
        except Exception as e:
            logger.warning(f"[MemoryManager] Cannot resolve the release state: {e}")
            release_state = None

        # P0-21: record the truth layer's verdict in the guard table BEFORE
        # the rows go in. The guard is what the old desktop build's writes
        # go through, and the owner token is known here — the guard's
        # front-matter fallback deliberately does not guess one.
        if release_state is not None:
            self._record_guard_paths(release_state)

        for entry in pending:
            n = len(entry["texts"])
            entry_embeddings = all_embeddings[cursor:cursor + n]
            cursor += n

            rel_path = entry["rel_path"]
            self.storage.delete_by_path(rel_path)
            if os.sep != "/":
                # Keys used to be spelled with the platform separator. Drop that
                # row too, so a Windows index carried over from an older build
                # does not keep a second copy of the same file under
                # "knowledge\note.md" and return it twice.
                self.storage.delete_by_path(rel_path.replace("/", os.sep))
            if release_state is not None:
                element_released, file_sealed = release_state.flags_for(rel_path)
            else:
                element_released, file_sealed = 0, 0
            memory_chunks = []
            for chunk, embedding in zip(entry["chunks"], entry_embeddings):
                chunk_id = self._generate_chunk_id(rel_path, chunk.start_line, chunk.end_line, chunk.part)
                chunk_hash = MemoryStorage.compute_hash(chunk.text)
                # P1-阶段三 (H1 · C-A2): the four body-externalization
                # columns are filled on THIS write path too.  It is the
                # path that writes the bulk of the index (every file the
                # scan finds), so leaving the four columns NULL here meant
                # that a migration's backfill was undone by the very next
                # sync — and INV-1 ("points nowhere") went RED on the rows
                # the migration had just fixed.
                #
                # `entry["file_path"]` is the truth file this chunk was cut
                # from, and the workspace root is the root the path is
                # relative to.  The path-safety rule is the ONE rule
                # (agent.memory.paths) — not a second spelling.
                body_path, body_anchor, body_hash, text_state, body_origin = (
                    self._body_columns_for(
                        relpath=rel_path,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        part=chunk.part,
                        chunk_text=chunk.text,
                        workspace_root=workspace_dir_path,
                        source_file=entry.get("file_path"),
                        segments=list(chunk.segments or []),
                        gaps=list(chunk.gaps or []),
                    )
                )
                memory_chunks.append(MemoryChunk(
                    id=chunk_id,
                    user_id=entry["user_id"],
                    scope=entry["scope"],
                    source=entry["source"],
                    path=rel_path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    text=chunk.text,
                    embedding=embedding,
                    hash=chunk_hash,
                    metadata=None,
                    element_released=element_released,
                    file_sealed=file_sealed,
                    # P0-16 L-5 / B-2: the owner token of a dark page, on its
                    # own column. Without this the column is always NULL and
                    # the sealed branch of the predicate is always false —
                    # which would hide every dark page from its own owner.
                    sealed_owner=entry.get("sealed_owner"),
                    body_path=body_path,
                    body_anchor=body_anchor,
                    body_hash=body_hash,
                    text_state=text_state,
                    body_origin=body_origin,
                ))
            self.storage.save_chunks_batch(memory_chunks)
            stat = entry["file_path"].stat()
            # P1-阶段三 (C-A3): the files bookkeeping row now carries the
            # REAL chunk count and the chunker version that produced it.
            # Before this, every row said "0 chunks" — a fact that was
            # false for every file in the index and could not be told
            # apart from a file that really produced none.
            self.storage.update_file_metadata(
                path=rel_path,
                source=entry["source"],
                file_hash=entry["file_hash"],
                mtime=int(stat.st_mtime),
                size=stat.st_size,
                chunk_count=len(memory_chunks),
                chunker_version=TextChunker.CHUNKER_VERSION,
            )

        # Stamp the chunker version only when we rebuilt from an empty index,
        # i.e. every chunk in it was produced by the current algorithm. An
        # index that already had files may still contain chunks from an older
        # chunker (file hashes do not change when only the chunker does), so it
        # stays unstamped and /memory status will suggest a rebuild.
        if files_before == 0:
            try:
                self.storage.set_meta(
                    "chunker_version", str(TextChunker.CHUNKER_VERSION)
                )
            except Exception:
                pass

        # P0-11 P-2: the projection is refreshed once more at the end of the
        # sync, so files that were indexed BEFORE the release (and whose
        # bytes never changed) carry the right flags too. Without this the
        # released content would survive in the index until the next edit.
        self._refresh_release_projection()

        # P0-22: what did the guard drop during this sync? Said out loud,
        # with the paths — the guard corrects the data AFTER the write, so
        # without this the two sides would hold two different beliefs
        # about the same row and neither would know.
        self._report_guard_discards(discards_before)

        # P1-阶段三 S3-K: certify the rows this sync just wrote.  Without
        # this, a row written after the last inspection stays
        # 'projection' forever — the 'mirror' state would be a claim
        # about the past, not about the current index.
        self._certify_after_write()

        self._dirty = False

    def flush_memory(
        self,
        messages: list,
        user_id: Optional[str] = None,
        reason: str = "threshold",
        max_messages: int = 10,
        context_summary_callback=None,
    ) -> bool:
        """
        Flush conversation summary to daily memory file.

        Args:
            messages: Conversation message list
            user_id: Optional user ID
            reason: "threshold" | "overflow" | "daily_summary"
            max_messages: Max recent messages to include (0 = all)
            context_summary_callback: Optional callback(str) invoked with the
                daily summary text for in-context injection

        Returns:
            True if flush was dispatched
        """
        success = self.flush_manager.flush_from_messages(
            messages=messages,
            user_id=user_id,
            reason=reason,
            max_messages=max_messages,
            context_summary_callback=context_summary_callback,
        )
        if success:
            self._dirty = True
        return success
    
    def get_status(self) -> Dict[str, Any]:
        """Get memory status

        The degradation is REPORTED, not merely implied. A vector search that
        silently fell back to keyword-only looks exactly like a working
        vector search from the outside — same tool, same results, smaller
        recall. The one field that tells the two apart is ``search_mode``,
        and it now says plainly which one is running and what was lost.
        """
        stats = self.storage.get_stats()
        vector_enabled = self.embedding_provider is not None
        embedded = stats.get('embedded', 0)
        chunks = stats['chunks']
        if vector_enabled:
            search_mode = 'hybrid (vector + keyword)'
        else:
            search_mode = 'keyword only (FTS5)'
        # Named, not implied: "降级自曝必须覆盖正常路径" — a degraded index
        # that reports the same fields as a healthy one has degraded into
        # silence.
        degraded = not vector_enabled and chunks > 0

        # Who is asking: retrieval visibility is defined relative to an
        # identity, so the status must say which one it is running under.
        # "anonymous" is a first-class answer — it means shared-only.
        identity_label = "anonymous"
        try:
            from common.runtime_identity import current_identity
            ident = current_identity()
            parts = []
            if ident.agent_id:
                parts.append(f"agent={ident.agent_id}")
            if ident.user_id:
                parts.append(f"user={ident.user_id}")
            if parts:
                identity_label = ", ".join(parts)
        except Exception:
            pass

        rerank_status = self._reranker_status()

        # P1-阶段一 (M1): the schema fingerprint, so the gate that decides
        # whether this index may serve can compare one token instead of
        # re-deriving the column set. A missing token reads as "unknown"
        # — distinct from a schema that IS known.
        #
        # P1-阶段三 (C-A1.4): the fingerprint is reported TOGETHER WITH the
        # tables it covers. The old single-table fingerprint said
        # "consistent" for two indexes whose `files` tables had already
        # forked; a reader who is not told WHAT was compared cannot tell
        # that verdict apart from a real one.
        schema_fp = None
        schema_fp_tables = ""
        try:
            row = self.storage.conn.execute(
                "SELECT value FROM _meta WHERE key='schema_fingerprint'"
            ).fetchone()
            schema_fp = row[0] if row else None
            from agent.memory.schema_drift import schema_fingerprint_tables

            schema_fp_tables = ",".join(schema_fingerprint_tables())
        except Exception:
            schema_fp = None

        # Which knowledge root this Agent actually reads: its own knowledge/
        # when present, otherwise the shared copy. Retrieval reports
        # "knowledge/..." paths either way; this field says whose they are.
        knowledge_root = 'unknown'
        try:
            import os as _os
            from common import state_dir
            root = _os.path.realpath(str(state_dir.knowledge_dir(base=self.config.get_workspace())))
            own = _os.path.realpath(str(self.config.get_workspace() / 'knowledge'))
            knowledge_root = 'own' if root == own else f'shared ({root})'
        except Exception:
            pass

        return {
            'chunks': stats['chunks'],
            'files': stats['files'],
            'workspace': str(self.config.get_workspace()),
            'dirty': self._dirty,
            'identity': identity_label,
            'knowledge_root': knowledge_root,
            'embedding_enabled': vector_enabled,
            'embedding_provider': self.config.embedding_provider if self.embedding_provider else 'disabled',
            'embedding_model': self.config.embedding_model if self.embedding_provider else 'N/A',
            'embedded_chunks': embedded,
            'search_mode': search_mode,
            'rerank_enabled': self.reranker is not None,
            # Reachability, not just configuration: an HTTP reranker that is
            # configured but down must read "unreachable", not "enabled".
            'rerank_status': rerank_status,
            # Why the vector channel is not running, in words. Empty when it
            # IS running.
            'degraded': degraded,
            'degradation_reason': (
                'vector search is disabled: retrieval is keyword-only '
                '(FTS5 + LIKE), so semantic matches are not found'
                if degraded else ''
            ),
            'schema_fingerprint': schema_fp,
            # WHAT the fingerprint covers. Without it, "the fingerprints
            # agree" is indistinguishable from "the fingerprints agree on
            # the one table the fingerprint looked at".
            'schema_fingerprint_tables': schema_fp_tables,
            # P1 阶段二-B T3: the read-path freshness probe counters — stale
            # hits must be self-reported, not discovered by luck (风控: 自曝
            # 不能是撞运气自曝).
            'freshness': self.get_freshness_status(),
            'embedding_coverage': (
                round(embedded / chunks, 4) if chunks else 0.0
            ),
        }

    def _reranker_status(self) -> str:
        """configured+reachable / configured+unreachable / local / off.

        "Off" needs no probe. A local reranker loads its model on first use,
        so there is nothing to ping; report it as configured. An HTTP
        reranker is probed at its /health endpoint with a short timeout —
        this is a status check, so it must stay cheap and must never raise.
        """
        reranker = self.reranker
        if reranker is None:
            return 'off'
        base_url = getattr(reranker, 'base_url', None)
        if not base_url:
            return 'local (model loads on first use)'
        try:
            import requests
            resp = requests.get(f"{base_url}/health", timeout=3)
            if resp.ok:
                return f'reachable ({base_url})'
            return f'unreachable ({base_url} -> HTTP {resp.status_code})'
        except Exception as e:
            return f'unreachable ({base_url}: {type(e).__name__})'
    
    def mark_dirty(self):
        """Mark memory as dirty (needs sync)"""
        self._dirty = True
    
    def close(self):
        """Close memory manager and release resources"""
        self.storage.close()
    
    # Helper methods
    
    @staticmethod
    def _body_columns_for(
        *,
        relpath: str,
        start_line: int,
        end_line: int,
        part: int,
        chunk_text: str,
        workspace_root: Optional[Path],
        source_file: Optional[Path] = None,
        segments: Optional[List[Tuple[int, int]]] = None,
        gaps: Optional[List[int]] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str, str]:
        """The body-externalization columns for one chunk row.

        P1-阶段三 (H1 · C-A2).  Returns
        ``(body_path, body_anchor, body_hash, text_state, body_origin)``.

        The knowledge of HOW to build them lives in
        ``agent.memory.body_pointer`` — the ONE module — so the two writers
        (this one and the rebuilder) cannot drift apart.  The path-safety
        rule is ``agent.memory.paths`` — also the ONE rule, imported rather
        than re-spelled.

        ``workspace_root=None`` means "there is no truth file behind this
        row": ``add_memory`` stores a blob of text under a synthetic path
        without writing a file.  Such a row carries no ``body_path`` (there
        is nothing to point at) but still carries an anchor and a drift
        token, so it is not four empty columns — and its origin is
        ``'blob'``, which says **honestly** that there is no truth source
        (as opposed to ``'unresolved'``, which says nobody has decided).

        ``source_file`` is the absolute path of the file the chunk was cut
        from.  It is used to read the fragment, so the hash is computed from
        the SAME bytes the invariant checker will read back (INV-3).

        P1-阶段三 第四轮: ``segments`` / ``gaps`` carry the chunk's segment
        sequence into the anchor — a chunk assembled from several
        non-contiguous line ranges cannot be located by a bare line span.
        """
        from agent.memory.body_pointer import five_columns_for_chunk

        return five_columns_for_chunk(
            workspace_root=workspace_root,
            relpath=relpath,
            start_line=start_line,
            end_line=end_line,
            part=part,
            chunk_text=chunk_text,
            segments=segments,
            gaps=gaps,
        )

    def _generate_chunk_id(self, path: str, start_line: int, end_line: int,
                           part: int = 0) -> str:
        """Generate unique chunk ID; `part` separates the pieces of one split line."""
        content = f"{path}:{start_line}:{end_line}"
        if part:
            content += f":{part}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    @staticmethod
    def _compute_temporal_decay(
        path: str,
        half_life_days: float = 30.0,
        occurred_at: Optional[str] = None,
    ) -> float:
        """
        Compute temporal decay multiplier for dated memory files.

        Inspired by OpenClaw's temporal-decay: exponential decay based on file date.
        MEMORY.md and non-dated files are "evergreen" (no decay, multiplier=1.0).
        Daily files like memory/2025-03-01.md decay based on age.

        Formula: multiplier = exp(-ln2/half_life * age_in_days)

        P1-阶段三 (D-A) · **FIXED**.  The date is now read from the TRUTH
        LAYER FIRST: the front-matter's ``occurred_at``, which is what the v2
        element system writes.  The old implementation read the date from
        the PATH with a regex that only matched ``memory/YYYY-MM-DD.md`` (and
        the per-user / dreams variants), so every v2 element file --
        ``memory/markdown/events/2026-10-28/<slug>--<hash8>.md`` -- silently
        returned 1.0.  "Silently" is the operative word: the decay was not
        applied AND nothing said so.

        The order of consultation:

        1. **``occurred_at``** (the front-matter, or an explicit argument) --
           the element's own statement of WHEN it happened.  This is the
           authoritative date in the v2 structure.
        2. **The legacy path regex** -- for the daily-diary structure that
           predates the element system (``memory/YYYY-MM-DD.md``).
        3. **Evergreen** -- everything else (knowledge pages, MEMORY.md,
           handoff notes, undated elements).

        ``knowledge/`` pages stay evergreen **even when they carry an
        ``occurred_at``**: a knowledge page is a distilled analysis whose
        date is a time anchor, not a freshness marker.  That distinction was
        already encoded in the old regex (a dated knowledge page never
        decayed) and it is preserved here -- the rule is about the LAYER, not
        about the presence of a date.

        The timestamp is parsed with ``text_inv.parse_timestamp`` -- the ONE
        parser -- so the decay side and the shelf-lag side agree on what a
        timestamp means (P1 阶段二-B T2-B2 was exactly this class of bug).

        Fail-closed on an unparseable date: an ``occurred_at`` we cannot read
        is treated as evergreen rather than as "now", because guessing
        "now" would make an old element look fresh -- the opposite of the
        error we are fixing.
        """
        import re

        # Normalize legacy backslash paths so the anchor below also matches
        # paths produced on Windows checkouts.
        normalized = path.replace("\\", "/")

        # Decay applies only to daily diary files: memory/YYYY-MM-DD.md, the
        # per-user memory/users/<id>/YYYY-MM-DD.md and memory/dreams/YYYY-MM-DD.md.
        # Dated knowledge pages (knowledge/analysis/foo-2026-09-02.md) are
        # event time anchors rather than perishable content, so they stay
        # evergreen and are never decayed.
        #
        # P1-阶段三 (D-A): a knowledge page stays evergreen EVEN IF it carries
        # an ``occurred_at`` -- the LAYER decides, not the presence of a date.
        is_knowledge = normalized.startswith("knowledge/")

        # --- 1. the truth layer's own date ---------------------------------
        if occurred_at:
            stamp = MemoryManager._parse_occurred_at(occurred_at)
            if stamp is None:
                # Unparseable: fall through to the path rule, then evergreen.
                pass
            elif is_knowledge:
                return 1.0
            else:
                return MemoryManager._decay_from_date(
                    stamp.date(), half_life_days)

        # --- 2. the legacy path rule ---------------------------------------
        match = re.search(
            r'^memory/(?:users/[^/]+/|dreams/)?(\d{4})-(\d{2})-(\d{2})\.md$', normalized
        )
        if not match:
            # --- 3. evergreen ------------------------------------------------
            return 1.0  # evergreen: MEMORY.md, knowledge pages, handoffs...

        try:
            file_date = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
        except ValueError:
            return 1.0
        return MemoryManager._decay_from_date(file_date.date(), half_life_days)

    @staticmethod
    def _parse_occurred_at(value: str):
        """The moment an element's ``occurred_at`` states, or None.

        P1-阶段三 (D-A): the parsing is delegated to
        ``text_inv.parse_timestamp`` -- the ONE parser -- so the decay side and
        the shelf-lag side cannot disagree about what a timestamp means.
        """
        from agent.memory.text_inv import parse_timestamp

        return parse_timestamp(value)

    @staticmethod
    def _decay_from_date(file_date, half_life_days: float) -> float:
        """The decay multiplier for a date, with the documented formula.

        ``multiplier = exp(-ln2/half_life * age_in_days)``; a date in the
        future (or today) is evergreen.
        """
        import math

        try:
            age_days = (datetime.now().date() - file_date).days
            if age_days <= 0:
                return 1.0

            decay_lambda = math.log(2) / half_life_days
            return math.exp(-decay_lambda * age_days)
        except (ValueError, OverflowError, TypeError):
            return 1.0

    @staticmethod
    def _occurred_at_of(workspace_root, relpath: str) -> Optional[str]:
        """The ``occurred_at`` the truth file behind ``relpath`` declares.

        P1-阶段三 (D-A): the decay needs the element's own date, and the
        element's date lives in the FILE.  This reads it -- read-only, from
        the workspace root, with the ONE front-matter parser
        (``agent.memory.frontmatter.split_frontmatter``).

        Returns None when the file does not exist, carries no front-matter,
        or declares no ``occurred_at``.  None means "no date stated", which
        the caller turns into "fall back to the legacy path rule".
        """
        if not workspace_root or not relpath:
            return None
        try:
            from agent.memory.frontmatter import split_frontmatter
            from agent.memory.paths import normalize_relpath

            target = Path(workspace_root) / normalize_relpath(relpath)
            if not target.is_file():
                return None
            fm, _ = split_frontmatter(target.read_text(encoding="utf-8"))
        except Exception:
            return None
        value = fm.get("occurred_at")
        if value is None:
            return None
        return str(value)
    
    @staticmethod
    def _normalize_weights(vector_weight: float, keyword_weight: float) -> tuple:
        """Clamp fusion weights into a valid (non-negative, finite) pair.

        The weighted average below is renormalized over only the channels that
        actually returned a chunk, so only the *ratio* between the two weights
        matters. Negative and non-finite weights (NaN, +/-inf) are clamped to 0,
        and an all-zero pair falls back to equal weighting, so a misconfigured
        value can never produce a negative or meaningless combined score.
        """
        import math

        try:
            v = float(vector_weight)
            k = float(keyword_weight)
        except (TypeError, ValueError):
            return 0.5, 0.5
        v = v if (math.isfinite(v) and v > 0) else 0.0
        k = k if (math.isfinite(k) and k > 0) else 0.0
        if v == 0.0 and k == 0.0:
            return 0.5, 0.5
        return v, k

    @staticmethod
    def _rank_normalize(results: List[SearchResult]) -> Dict[tuple, float]:
        """Rank-normalize one channel's already-sorted results onto (0, 1].

        ``search_vector`` and ``search_keyword`` both return their results in
        descending relevance order, so the position index is the rank. The top
        hit maps to 1.0 and each later hit decays linearly to 1/N. This puts
        raw cosine similarity and the BM25-derived keyword score — which live
        on incommensurable scales — onto a common scale before fusion.

        :return: {(path, start_line, end_line): normalized_score}
        """
        n = len(results)
        if n == 0:
            return {}
        return {
            (r.path, r.start_line, r.end_line): (n - i) / n
            for i, r in enumerate(results)
        }

    def _decay_for_path(self, path: str,
                        half_life_days: float = 30.0) -> float:
        """The decay factor for one indexed path.

        P1-阶段三 (D-A).  The order of consultation:

        1. the truth file's own front-matter ``occurred_at`` — the v2 element
           structure's authoritative date;
        2. the legacy path regex (daily diaries);
        3. evergreen.

        The file is read only for paths that are NOT already decidable by
        the legacy rule, and the read is bounded: one file per candidate
        path, front-matter only (the first block), and the front-matter
        parser is the ONE parser.

        The workspace root is resolved defensively: a manager built without
        a config (several tests do) simply gets the legacy behaviour rather
        than an exception.
        """
        try:
            workspace = self.config.get_workspace()
        except Exception:
            workspace = None
        occurred_at = None
        if workspace is not None:
            occurred_at = self._occurred_at_of(workspace, path)
        return self._compute_temporal_decay(
            path, half_life_days=half_life_days, occurred_at=occurred_at)

    def _merge_results(
        self,
        vector_results: List[SearchResult],
        keyword_results: List[SearchResult],
        vector_weight: float,
        keyword_weight: float
    ) -> List[SearchResult]:
        """Merge vector and keyword search results.

        Each channel is rank-normalized to a common (0, 1] scale first, then a
        chunk is scored by the weighted average over only the channels that
        actually returned it. A strong single-channel hit is therefore not
        diluted by a 0.0 from the missing channel, and the two channels' raw
        scores no longer have to be comparable to be fused fairly.

        P1-阶段三 (D-C): the temporal decay is NOT multiplied into the score
        here any more -- it is CARRIED on the result (``SearchResult.decay``)
        and applied once, at the end of the ranking chain.

        Why the factor travels instead of being applied:

        * the old code applied the decay in BOTH ``_merge_results`` and
          ``_rerank``, so a reranked result was decayed TWICE (``decay**2``)
          -- a 100-day-old element with a 30-day half-life was scored at
          0.0056 instead of 0.0992;
        * applying it at the end means "the decay is applied once" is a
          property of the CODE, not of the order in which two methods happen
          to be called.  A third ranking stage cannot introduce a second
          multiplication by accident.

        The UNDECAYED score is what travels, so the reranker can fuse with
        the retrieval signal on the same scale (see ``_rerank``).
        """
        vector_weight, keyword_weight = self._normalize_weights(vector_weight, keyword_weight)
        vector_norm = self._rank_normalize(vector_results)
        keyword_norm = self._rank_normalize(keyword_results)

        # Keep the first (vector-first) result per chunk for reconstruction;
        # either channel carries the same snippet/source/path metadata.
        result_map: Dict[tuple, SearchResult] = {}
        for result in vector_results + keyword_results:
            key = (result.path, result.start_line, result.end_line)
            result_map.setdefault(key, result)

        merged_results = []
        for key in set(vector_norm) | set(keyword_norm):
            result = result_map[key]
            vec = vector_norm.get(key)
            kw = keyword_norm.get(key)
            hit_v = vec is not None
            hit_k = kw is not None
            numerator = (
                vector_weight * (vec if hit_v else 0.0)
                + keyword_weight * (kw if hit_k else 0.0)
            )
            denominator = (
                (vector_weight if hit_v else 0.0)
                + (keyword_weight if hit_k else 0.0)
            )
            combined_score = (numerator / denominator) if denominator > 0 else 0.0

            merged_results.append(SearchResult(
                path=result.path,
                start_line=result.start_line,
                end_line=result.end_line,
                score=combined_score,
                snippet=result.snippet,
                source=result.source,
                user_id=result.user_id,
                # P1-阶段三 (D-C): the decay FACTOR travels with the score;
                # it is applied once, at the end of the ranking chain.
                # P1-阶段三 (D-A): the factor is looked up per PATH, so the
                # truth layer's own ``occurred_at`` is consulted (the v2
                # element structure) rather than only the legacy path regex.
                decay=self._decay_for_path(result.path),
            ))

        merged_results.sort(key=lambda r: r.score, reverse=True)
        return merged_results

    def _rerank(
        self,
        query: str,
        results: List[SearchResult]
    ) -> List[SearchResult]:
        """Reorder candidates by the reranker's relevance scores.

        P1-阶段三 (D-C) -- **FIXED**.  Two things were wrong here:

          1. The reranker score REPLACED the fused score outright (no
             renormalization), so the keyword/vector channels' contribution
             was discarded whenever a reranker was configured.
          2. The temporal decay was applied to the rerank score here, while
             the fused score was ALREADY decayed upstream -- the decay was
             multiplied twice on the rerank path.

        What it does now:

        * **The rerank score is FUSED with the retrieval score, not
          substituted for it.**  Both are on the (0, 1] retrieval scale (the
          reranker providers already rank-normalize onto it), so the two are
          combined by a weighted average and renormalized over the channels
          that actually produced a value.  The final score therefore
          reflects BOTH the retrieval channels and the reranker: the
          reranker can promote a candidate, but it can no longer throw away
          the evidence that brought it there.

        * **The decay is applied ONCE, at the end.**  The fused score is
          kept UNDECAYED through the fusion, the decay factor travels on
          the result, and the final score is ``fused * decay`` -- exactly one
          multiplication.

        The DEGRADATION behaviour is unchanged: no reranker configured
        returns ``results`` untouched, and a rerank that raises keeps the
        fused order.  The stage-two degradation criteria still hold.
        """
        if self.reranker is None or not results:
            return results

        from common.log import logger

        try:
            scores = self.reranker.rerank(query, [r.snippet for r in results])
            if len(scores) != len(results):
                raise ValueError(
                    f"expected {len(results)} scores, got {len(scores)}"
                )
        except Exception as e:
            logger.warning(f"[MemoryManager] Rerank failed, keeping fused order: {e}")
            return results

        # The reranker's own scale is (0, 1] with the top hit at 1.0 -- the
        # same shape the fusion channels use.  Normalizing it here (rather
        # than trusting the provider) is what makes the two comparable when
        # a provider returns raw logits.
        rerank_norm = self._rank_normalize_scores(scores)
        rerank_weight = self._rerank_weight()

        reranked = []
        for result, rerank_score in zip(results, rerank_norm):
            # D-C fix 1: FUSE, do not substitute.  The retrieval score and
            # the rerank score are both on (0, 1]; the weighted average is
            # renormalized over the channels that produced a value, exactly
            # like ``_merge_results`` does for the vector/keyword channels.
            fused = self._fuse_scores(result.score, rerank_score,
                                      rerank_weight=rerank_weight)
            # D-C fix 2: the decay is applied ONCE, here, and only here.
            decay = 1.0 if result.decay is None else float(result.decay)
            reranked.append(SearchResult(
                path=result.path,
                start_line=result.start_line,
                end_line=result.end_line,
                score=fused * decay,
                snippet=result.snippet,
                source=result.source,
                user_id=result.user_id,
                decay=decay,
            ))

        reranked.sort(key=lambda r: r.score, reverse=True)
        return reranked

    def _rerank_weight(self) -> float:
        """The reranker's share of the final score (P1-阶段三 D-C).

        Read from the config when there is one; the default is 0.5 (the two
        signals contribute equally).  A manager built without a config (the
        unit tests do) gets the default rather than an exception — the
        fusion must never be the thing that breaks a search.
        """
        config = getattr(self, "config", None)
        try:
            weight = float(getattr(config, "rerank_weight", 0.5))
        except (TypeError, ValueError):
            return 0.5
        if not (0.0 <= weight <= 1.0):
            return 0.5
        return weight

    @staticmethod
    def _rank_normalize_scores(scores: List[float]) -> List[float]:
        """Map a score list onto the retrieval scale: the top hit becomes 1.0.

        The same shape ``_rank_normalize`` gives the fusion channels, so a
        reranked score and a fused score carry comparable numbers.  A
        provider that already returns that scale is unchanged by it (the
        normalization is order-preserving and the top hit is already 1.0).
        """
        n = len(scores)
        if n == 0:
            return []
        order = sorted(range(n), key=lambda i: scores[i], reverse=True)
        normalized = [0.0] * n
        for rank, index in enumerate(order):
            normalized[index] = (n - rank) / n
        return normalized

    @staticmethod
    def _fuse_scores(retrieval_score: float, rerank_score: float,
                     rerank_weight: float = 0.5) -> float:
        """Combine the retrieval score and the rerank score.

        The rerank weight is the reranker's share of the final signal.  At
        0.5 the two contribute equally -- the reranker is a strong opinion,
        not the sole authority, and the retrieval channels' evidence is never
        discarded.

        The result is renormalized over the values that actually exist, so a
        missing channel contributes 0 to the WEIGHT, not to the score -- the
        same rule ``_merge_results`` uses for its two channels.
        """
        try:
            w = float(rerank_weight)
        except (TypeError, ValueError):
            w = 0.5
        if not (0.0 <= w <= 1.0):
            w = 0.5
        retrieval_weight = 1.0 - w
        numerator = retrieval_weight * float(retrieval_score) + w * float(rerank_score)
        denominator = retrieval_weight + w
        if denominator <= 0:
            return float(retrieval_score)
        return numerator / denominator

    def _hit_boost_params(self):
        """(weight, cap) of the hit-popularity boost; weight 0 means off."""
        config = getattr(self, "config", None)
        weight = float(getattr(config, "hit_boost_weight", 0.0) or 0.0)
        cap = float(getattr(config, "hit_boost_max", 1.25) or 1.25)
        return weight, cap

    def _hit_boost_enabled(self) -> bool:
        """Opt-in: a retrieval hit is not proof of usefulness, so by default
        both the re-ranking and the per-search hit write-back stay off."""
        weight, cap = self._hit_boost_params()
        return weight > 0 and cap > 1.0

    def _apply_hit_boost(
        self,
        results: List[SearchResult]
    ) -> List[SearchResult]:
        """Nudge chunks that were retrieved often (and recently) up the ranking.

        The multiplier is ``1 + w*log2(1 + hits)`` plus a same-sized recency
        term that decays over a week since ``last_hit_at``, capped at
        ``hit_boost_max``. The log keeps the curve flat at the high end and the
        cap bounds the feedback loop, so repeated queries can only re-order
        near-equal candidates, never crowd out everything else. The boost is
        opt-in: with the default ``hit_boost_weight = 0`` this returns the
        results untouched and ``search()`` skips the hit write-back too.

        Uses getattr lookups so a bare instance (tests call the pure fusion
        helpers without storage/config) degrades to a no-op, and any storage
        failure degrades to the unboosted order like rerank does.
        """
        if not results:
            return results
        storage = getattr(self, "storage", None)
        if storage is None or not hasattr(storage, "get_hit_stats"):
            return results
        if not self._hit_boost_enabled():
            return results

        weight, cap = self._hit_boost_params()

        import math
        import time

        from common.log import logger

        try:
            keys = [(r.path, r.start_line, r.end_line) for r in results]
            stats = storage.get_hit_stats(keys)
        except Exception as e:
            logger.warning(f"[MemoryManager] Hit stats unavailable, skipping boost: {e}")
            return results

        now = time.time()
        boosted = []
        for result in results:
            hits, last_hit_at = stats.get(
                (result.path, result.start_line, result.end_line), (0, None)
            )
            factor = 1.0 + weight * math.log2(1.0 + max(0, int(hits or 0)))
            if last_hit_at:
                age_days = max(0.0, (now - float(last_hit_at)) / 86400.0)
                factor += weight * math.exp(-age_days / 7.0)
            boosted.append(SearchResult(
                path=result.path,
                start_line=result.start_line,
                end_line=result.end_line,
                score=result.score * min(factor, cap),
                snippet=result.snippet,
                source=result.source,
                user_id=result.user_id
            ))

        boosted.sort(key=lambda r: r.score, reverse=True)
        return boosted
