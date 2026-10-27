"""
Memory manager for AgentMesh

Provides high-level interface for memory operations
"""

import json
import os
from typing import FrozenSet, List, Optional, Dict, Any, Sequence, Tuple
from pathlib import Path
import hashlib
from datetime import datetime

from agent.memory.config import MemoryConfig, get_default_memory_config
from agent.memory.storage import MemoryStorage, MemoryChunk, SearchResult
from agent.memory.chunker import TextChunker
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

        return top
    
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
        
        # Chunk content
        chunks = self.chunker.chunk_text(content)
        
        # Generate embeddings (if provider available)
        texts = [chunk.text for chunk in chunks]
        if self.embedding_provider:
            embeddings = self.embedding_provider.embed_batch(texts)
        else:
            # No embeddings, just use None
            embeddings = [None] * len(texts)
        
        # Create memory chunks
        memory_chunks = []
        for chunk, embedding in zip(chunks, embeddings):
            chunk_id = self._generate_chunk_id(path, chunk.start_line, chunk.end_line, chunk.part)
            chunk_hash = MemoryStorage.compute_hash(chunk.text)
            
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
                metadata=metadata
            ))
        
        # Save to storage
        self.storage.save_chunks_batch(memory_chunks)
        
        # Update file metadata
        file_hash = MemoryStorage.compute_hash(content)
        self.storage.update_file_metadata(
            path=path,
            source=source,
            file_hash=file_hash,
            mtime=int(os.path.getmtime(__file__)),  # Use current time
            size=len(content)
        )
    
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
            # Markdown files (memory + knowledge) get structure-aware chunking;
            # anything else (rare) falls back to the plain char splitter.
            if file_path.suffix.lower() == '.md':
                chunks = self.chunker.chunk_markdown(content)
            else:
                chunks = self.chunker.chunk_text(content)
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
                ))
            self.storage.save_chunks_batch(memory_chunks)
            stat = entry["file_path"].stat()
            self.storage.update_file_metadata(
                path=rel_path,
                source=entry["source"],
                file_hash=entry["file_hash"],
                mtime=int(stat.st_mtime),
                size=stat.st_size,
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
    
    def _generate_chunk_id(self, path: str, start_line: int, end_line: int,
                           part: int = 0) -> str:
        """Generate unique chunk ID; `part` separates the pieces of one split line."""
        content = f"{path}:{start_line}:{end_line}"
        if part:
            content += f":{part}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    @staticmethod
    def _compute_temporal_decay(path: str, half_life_days: float = 30.0) -> float:
        """
        Compute temporal decay multiplier for dated memory files.
        
        Inspired by OpenClaw's temporal-decay: exponential decay based on file date.
        MEMORY.md and non-dated files are "evergreen" (no decay, multiplier=1.0).
        Daily files like memory/2025-03-01.md decay based on age.
        
        Formula: multiplier = exp(-ln2/half_life * age_in_days)
        """
        import re
        import math
        
        # Normalize legacy backslash paths so the anchor below also matches
        # paths produced on Windows checkouts.
        normalized = path.replace("\\", "/")

        # Decay applies only to daily diary files: memory/YYYY-MM-DD.md, the
        # per-user memory/users/<id>/YYYY-MM-DD.md and memory/dreams/YYYY-MM-DD.md.
        # Dated knowledge pages (knowledge/analysis/foo-2026-09-02.md) are
        # event time anchors rather than perishable content, so they stay
        # evergreen and are never decayed.
        match = re.search(
            r'^memory/(?:users/[^/]+/|dreams/)?(\d{4})-(\d{2})-(\d{2})\.md$', normalized
        )
        if not match:
            return 1.0  # evergreen: MEMORY.md, knowledge pages, handoffs...
        
        try:
            file_date = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            )
            age_days = (datetime.now() - file_date).days
            if age_days <= 0:
                return 1.0
            
            decay_lambda = math.log(2) / half_life_days
            return math.exp(-decay_lambda * age_days)
        except (ValueError, OverflowError):
            return 1.0
    
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

    def _merge_results(
        self,
        vector_results: List[SearchResult],
        keyword_results: List[SearchResult],
        vector_weight: float,
        keyword_weight: float
    ) -> List[SearchResult]:
        """Merge vector and keyword search results with temporal decay.

        Each channel is rank-normalized to a common (0, 1] scale first, then a
        chunk is scored by the weighted average over only the channels that
        actually returned it. A strong single-channel hit is therefore not
        diluted by a 0.0 from the missing channel, and the two channels' raw
        scores no longer have to be comparable to be fused fairly.
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

            # Apply temporal decay for dated memory files
            combined_score *= self._compute_temporal_decay(result.path)

            merged_results.append(SearchResult(
                path=result.path,
                start_line=result.start_line,
                end_line=result.end_line,
                score=combined_score,
                snippet=result.snippet,
                source=result.source,
                user_id=result.user_id
            ))

        merged_results.sort(key=lambda r: r.score, reverse=True)
        return merged_results

    def _rerank(
        self,
        query: str,
        results: List[SearchResult]
    ) -> List[SearchResult]:
        """Reorder candidates by the reranker's relevance scores.

        Replaces each candidate's fused score with the reranker's score (still
        subject to temporal decay), then re-sorts. Returns ``results``
        unchanged when no reranker is configured, and falls back to them when
        rerank fails, the same way vector search degrades to keyword-only.
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

        reranked = []
        for result, score in zip(results, scores):
            decayed = score * self._compute_temporal_decay(result.path)
            reranked.append(SearchResult(
                path=result.path,
                start_line=result.start_line,
                end_line=result.end_line,
                score=decayed,
                snippet=result.snippet,
                source=result.source,
                user_id=result.user_id
            ))

        reranked.sort(key=lambda r: r.score, reverse=True)
        return reranked

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
