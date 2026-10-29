"""
Storage layer for memory using SQLite + FTS5

Provides vector and keyword search capabilities
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory.vector_backend import (
    SQLiteVectorBackend,
    VectorBackend,
    VectorRecord,
)
from agent.memory.visibility import SCOPE_SEALED, visibility_clause

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False
    np = None  # type: ignore[assignment]

# UPSERT (INSERT … ON CONFLICT DO UPDATE) requires SQLite ≥ 3.24.0 (2018).
# Older systems (e.g. CentOS 7 ships SQLite 3.7) fall back to INSERT OR REPLACE,
# which risks FTS5 rowid drift on chunk updates (see save_chunk docstring).
_HAS_UPSERT = sqlite3.sqlite_version_info >= (3, 24, 0)

# ---------------------------------------------------------------------------
# CJK character ranges, compiled once at module load.
# Covers: CJK Symbols/Punctuation, Japanese kana (hiragana + katakana),
#         CJK Unified Ideographs + Extension A, Korean syllables (Hangul),
#         CJK Compatibility Ideographs, and CJK Extension B–F.
# ---------------------------------------------------------------------------
_CJK_RANGES = (
    r'\u3000-\u30ff'          # CJK Symbols/Punctuation + Japanese kana
    r'\u3400-\u9fff'          # CJK Unified Ideographs (incl. Extension A)
    r'\uac00-\ud7af'          # Korean syllables (Hangul)
    r'\uf900-\ufaff'          # CJK Compatibility Ideographs
    r'\U00020000-\U0002fa1f'  # CJK Extension B–F
)
_RE_CONTAINS_CJK   = re.compile(f'[{_CJK_RANGES}]')
_RE_CJK_WORDS      = re.compile(f'[{_CJK_RANGES}]+')
_RE_TRIGRAM_TOKENS = re.compile(rf'[{_CJK_RANGES}]+|[^\W{_CJK_RANGES}]+')


# ---------------------------------------------------------------------------
# Query segmentation
# ---------------------------------------------------------------------------
# Chinese has no spaces, so a CJK run used to become ONE token: "王静静是谁"
# was matched as the literal five-character phrase, which only a page
# containing that exact run could match, and a two-character query ("事件")
# could not be indexed by the trigram tokenizer at all — it needs three.
#
# One segmenter serves both keyword paths (the trigram FTS5 stage and the LIKE
# fallback), so the two cannot drift apart again: they used to disagree about
# what a "word" is, and each was wrong in its own way.
#
# A CJK run becomes a group of alternatives (its sliding windows), and a
# non-CJK word becomes a group of one. Groups are ANDed; the alternatives
# inside a group are ORed. That keeps "中文教程 договор" meaning "both",
# while letting "王静静是谁" match a page that contains "王静静".

# Longest window first: a longer window is the more specific alternative, so
# it is tried before the shorter substrings it already implies.
_CJK_WINDOW_SIZES = (3, 2)

# How much of a CJK run a chunk must cover before it counts as a match.
#
# The sliding windows mean a chunk can match a query with a fragment of it:
# "学习" is a window of "深度学习入门". Without a threshold, updating a chunk
# from "深度学习入门" to "强化学习进阶" would leave the old text findable,
# because the two share the window "学习".
#
# Three characters is the natural threshold because it is the granularity the
# trigram tokenizer itself indexes: a 3-character window is exactly what
# FTS5 can match, so the threshold makes the two keyword paths agree. A run
# shorter than three characters is a single word and must be matched whole.
_MIN_MATCHED_CHARS = 3


def _cjk_windows(run: str) -> List[str]:
    """Sliding windows over one CJK run, deduplicated, longest first.

    "王静静是谁" -> ["王静静", "静静是", "静是谁", "王静", "静静", "静是", "是谁"]

    A two-character run yields its own two-character window ("事件"), which is
    what makes short Chinese words reachable at all.
    """
    windows: List[str] = []
    for size in _CJK_WINDOW_SIZES:
        if len(run) < size:
            continue
        for i in range(len(run) - size + 1):
            window = run[i:i + size]
            if window not in windows:
                windows.append(window)
    return windows


@dataclass(frozen=True)
class QueryGroup:
    """One unit of a query: a CJK run (any of its windows) or a whole word.

    ``weight`` is the group's length in characters. It decides how much a
    partial match counts against the group when the final score is averaged
    over the groups, so a long question is not satisfied by one short word.
    """
    text: str
    kind: str  # "cjk" | "word"
    windows: Tuple[str, ...]

    @property
    def weight(self) -> float:
        return float(len(self.text))


def _query_groups(query: str) -> List[QueryGroup]:
    """Split a query into the groups both keyword paths match against."""
    groups: List[QueryGroup] = []
    for run in _RE_CJK_WORDS.findall(query):
        windows = _cjk_windows(run)
        if windows:
            groups.append(QueryGroup(text=run, kind="cjk", windows=tuple(windows)))
    # The non-CJK part keeps its own rules: whole words of 3+ characters.
    # Anything shorter is not worth a LIKE scan of the whole corpus.
    word_text = _RE_CONTAINS_CJK.sub(' ', query)
    for word in re.findall(r'\w+', word_text):
        if len(word) >= 3:
            groups.append(QueryGroup(text=word, kind="word", windows=(word,)))
    return groups


def _group_quality(group: QueryGroup, text: str) -> float:
    """How well one group is covered by a piece of text, in [0, 1].

    For a CJK run this is the fraction of the run's characters that are
    covered by windows which actually occur in the text, discounted by how
    long the longest matching window is: a page holding the whole phrase
    deserves more than a page holding a two-character fragment of it.

    A run shorter than three characters is matched WHOLE — a two-character
    Chinese word ("事件") is a word, not a fragment of a longer one. A longer
    run needs at least three matched characters, so a chunk that shares only
    a common two-character fragment with the query ("学习" in "深度学习入门"
    and "强化学习进阶") is not a match at all.
    """
    if group.kind == "word":
        return 1.0 if group.text.lower() in text.lower() else 0.0

    matched = [w for w in group.windows if w in text]
    if not matched:
        return 0.0
    # Positions are taken inside the run, not inside the text: the same window
    # can occur many times in a long chunk, and every occurrence would
    # otherwise inflate the coverage past 1.
    covered = set()
    for window in matched:
        start = group.text.find(window)
        while start != -1:
            covered.update(range(start, start + len(window)))
            start = group.text.find(window, start + 1)
    if len(group.text) > _MIN_MATCHED_CHARS and len(covered) < _MIN_MATCHED_CHARS:
        # Only a fragment in common — not a match.
        return 0.0
    coverage = min(1.0, len(covered) / len(group.text))
    longest = max(len(w) for w in matched)
    return coverage * (0.6 + 0.4 * min(1.0, longest / 3.0))


# A chunk that contains the query STRING verbatim has matched it trivially:
# the query's characters are not special in that document, they are an
# example of themselves — a probe table, a quoted question, a test corpus.
# A chunk that matches the query's CONTENT while not containing the string is
# evidence about the subject matter, which is what the search was for.
#
# So a verbatim mention is discounted relative to a subject match. The
# discount is a factor, not a filter: a page that quotes the query is still
# returned (it is about the query), it just does not outrank a page that is
# about the thing the query names.
#
# It only changes the outcome when both kinds of match are present. When
# every candidate contains the query string — a one-word query, say — the
# factor is identical for all of them and the order is untouched.
_VERBATIM_MENTION_FACTOR = 0.5


def _verbatim_mention_factor(groups: List[QueryGroup], text: str) -> float:
    """Discount for a chunk that contains the query string verbatim."""
    lowered = text.lower()
    for group in groups:
        if group.kind == "word":
            continue
        if group.text.lower() in lowered:
            return _VERBATIM_MENTION_FACTOR
    return 1.0

# sqlite3.OperationalError subclasses DatabaseError, so "database is locked",
# "disk I/O error" and friends must be told apart from actual corruption before
# any recovery is attempted.
_CORRUPTION_MARKERS = ("malformed", "corrupt", "file is not a database", "encrypted")


# Every session's first message opens this database, so nothing that reads the
# whole index (the integrity scan, rebuilding a search index) runs on the open
# path: it runs in the background, once per process, and never moves the file.
_maintenance_started: set = set()
_maintenance_running: set = set()
_maintenance_lock = threading.Lock()
# Chunk writes and a background index rebuild must not interleave, so every
# MemoryStorage on the same file shares one write lock.
_write_locks: Dict[str, threading.RLock] = {}

_FTS_REBUILD_PENDING = "fts_rebuild_pending"
_TRIGRAM_DONE = "trigram_backfill_done"
# Rows per transaction when refilling a search index, so the write lock on the
# shared file (which also holds the conversation history) is released often.
_REFILL_BATCH = 500


def _write_lock_for(key: str) -> threading.RLock:
    with _maintenance_lock:
        return _write_locks.setdefault(key, threading.RLock())


def _is_corruption_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _CORRUPTION_MARKERS)


def _split_fts5_damage(report: str) -> tuple[list[str], list[str]]:
    """Split an integrity_check report into FTS5 findings and everything else.

    SQLite 3.44 taught integrity_check to validate FTS3/FTS5 content too, so a
    merely stale search index now shows up as a failure. Those indexes are
    derived data and get rebuilt from the chunks table, whereas other findings
    mean the b-tree itself is damaged.
    """
    fts5, other = [], []
    for line in report.splitlines():
        line = line.strip()
        if not line:
            continue
        (fts5 if "fts5" in line.lower() else other).append(line)
    return fts5, other


@dataclass
class MemoryChunk:
    """Represents a memory chunk with text and embedding"""
    id: str
    user_id: Optional[str]
    scope: str  # "shared" | "user" | "session"
    source: str  # "memory" | "session"
    path: str
    start_line: int
    end_line: int
    text: str
    embedding: Optional[List[float]]
    hash: str
    metadata: Optional[Dict[str, Any]] = None
    # P0-11 P-2 (RR-04): the release projection of this chunk.
    #
    #   element_released — the ELEMENT this chunk belongs to was released.
    #                     Set even for the version whose own front-matter
    #                     still says active (WR-11 keeps the old bytes
    #                     untouched, so the old file cannot know by itself).
    #   file_sealed      — this chunk's own file seals itself
    #                     (visibility=sealed / status=released).
    #
    # Both are integers (0/1) so the retrieval exit can filter on them in
    # SQL — the visibility predicate in agent/memory/visibility.py is the
    # one place that does.
    element_released: int = 0
    file_sealed: int = 0
    # P0-16 L-5: the OWNER token of a dark page (``agent--<author>``).
    #
    # It has its OWN column because it is a different KIND of identity:
    # ``user_id`` holds end-user ids, and the agent namespace must never
    # be reachable through it — an end-user id that happens to equal an
    # owner token (``agent--<some agent>``) would otherwise read the dark
    # page (P0-15 L-5: the collision was reproducible). The sealed branch
    # of the predicate reads THIS column and nothing else.
    sealed_owner: Optional[str] = None
    # P1-阶段一 (D-C): the four body-externalization columns.  Stage one
    # lands the COLUMNS only — the writer that fills them (the chunked body
    # store) is stage two.  Every chunk starts life as a projection (text
    # lives in this row, the way it always has).
    body_path: Optional[str] = None     # truth-layer file this body lives in
    body_anchor: Optional[str] = None   # byte span / locator inside that file
    body_hash: Optional[str] = None     # sha256 of the canonical body (drift check)
    # 'projection' (text here) | 'externalized' (text moved to body_path)
    # | 'stale' (hash drifted) | 'missing' (body file gone).
    text_state: Optional[str] = "projection"


@dataclass
class SearchResult:
    """Search result with score and snippet"""
    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str
    source: str
    user_id: Optional[str] = None


class MemorySchemaError(RuntimeError):
    """The index's SCHEMA does not match what the code needs.

    P0-17 B-2: a structural failure (a missing column, a broken index, a
    schema the current code cannot query) used to be caught and turned into
    an EMPTY result list. That is the most dangerous failure shape in this
    system, because "no matches" and "the query could not run" are
    indistinguishable from the outside — the retrieval exits reported
    "found nothing" while the truth was "the query never ran". It reads as
    a working system that happens to know nothing.

    So the two are now separated: a structural failure RAISES, and only a
    genuine absence of matches returns ``[]``.
    """


class MemoryStorage:
    """SQLite-based storage with FTS5 for keyword search"""
    
    def __init__(
        self,
        db_path: Path,
        vector_backend: Optional[VectorBackend] = None,
    ):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.vector_backend = vector_backend
        self.fts5_available = False  # Track FTS5 availability
        # RLock protects concurrent writes from the same process.
        # SQLite WAL mode handles read/write concurrency at the file level,
        # but same-process concurrent writes still need a Python-level lock.
        self._lock = _write_lock_for(str(db_path))
        self._init_db()
        if self.vector_backend is None:
            assert self.conn is not None
            self.vector_backend = SQLiteVectorBackend(self.conn)
    
    def _check_fts5_support(self) -> bool:
        """Check if SQLite has FTS5 support"""
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts5_test USING fts5(test)")
            self.conn.execute("DROP TABLE IF EXISTS fts5_test")
            return True
        except sqlite3.OperationalError as e:
            if "no such module: fts5" in str(e):
                return False
            raise

    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            # WAL and busy_timeout must be set before any long read (notably
            # integrity_check), otherwise a concurrent writer makes it fail with
            # SQLITE_BUSY, which used to be misread as corruption.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            conn.close()
            raise
        return conn

    def _schedule_maintenance(self, repair_pending: bool):
        """Start background maintenance: repair flagged search indexes and, once
        per process, scan the file for damage.

        The scan reads the whole file (minutes for a large one on a network
        filesystem). This file also holds the conversation history, which
        cannot be regenerated, so what the scan finds is never repaired by
        moving the file away:
          - FTS5 damage is rebuilt in place from the chunks table (the index is
            derived data).
          - Any other damage is only logged, for manual repair.
        Set ``memory_integrity_check: true`` in config.json for the full
        ``integrity_check`` instead of ``quick_check``.
        """
        key = str(self.db_path)
        with _maintenance_lock:
            scan = key not in _maintenance_started
            if key in _maintenance_running or not (scan or repair_pending):
                return
            _maintenance_started.add(key)
            _maintenance_running.add(key)
        threading.Thread(
            target=self._run_maintenance, args=(key, scan),
            daemon=True, name="memory-db-maintenance",
        ).start()

    def _run_maintenance(self, key: str, scan: bool):
        from common.log import logger
        conn = None
        try:
            conn = self._open_conn()
            self._repair_search_indexes(conn)
            if scan:
                self._scan_integrity(conn, key)
        except Exception as e:
            logger.warning(f"[MemoryStorage] Background maintenance of {key} failed: {e}")
        finally:
            if conn is not None:
                conn.close()
            with _maintenance_lock:
                _maintenance_running.discard(key)

    def _scan_integrity(self, conn: sqlite3.Connection, key: str):
        from common.log import logger
        report = self._integrity_report(conn)
        if report is None:
            return
        fts5_lines, other_lines = _split_fts5_damage(report)
        if fts5_lines:
            logger.warning(
                f"[MemoryStorage] FTS5 index damaged, rebuilding from chunks: "
                f"{'; '.join(fts5_lines)}"
            )
            trigram = any("chunks_fts_trigram" in ln for ln in fts5_lines)
            unicode = any(
                "chunks_fts" in ln and "chunks_fts_trigram" not in ln for ln in fts5_lines
            )
            if not (trigram or unicode):
                trigram = unicode = True
            with conn:
                if unicode:
                    conn.execute(
                        "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, '1')",
                        (_FTS_REBUILD_PENDING,),
                    )
                if trigram:
                    conn.execute("DELETE FROM _meta WHERE key = ?", (_TRIGRAM_DONE,))
            self._repair_search_indexes(conn)
        if other_lines:
            logger.error(
                f"[MemoryStorage] Database damage found in {key}, left in place "
                f"(repair manually, e.g. sqlite3 .recover): {'; '.join(other_lines)}"
            )

    @staticmethod
    def _integrity_report(conn: sqlite3.Connection) -> Optional[str]:
        """Run the check; None when the database is fine or could not be checked."""
        from common.log import logger
        pragma = "quick_check"
        try:
            from config import conf
            if conf().get("memory_integrity_check", False):
                pragma = "integrity_check"
        except Exception:
            # No config available (tests / standalone) — keep the historical
            # behaviour of running the full scan.
            pragma = "integrity_check"
        try:
            rows = conn.execute(f"PRAGMA {pragma}").fetchall()
            report = "\n".join(str(r[0]) for r in rows).strip()
        except sqlite3.DatabaseError as e:
            if not _is_corruption_error(e):
                logger.warning(f"[MemoryStorage] Integrity check skipped: {e}")
                return None
            report = str(e)
        return None if report == "ok" else report

    def _repair_search_indexes(self, conn: sqlite3.Connection):
        """Rebuild whichever search index is flagged or found broken."""
        if not self.fts5_available:
            return
        from common.log import logger
        with self._lock:
            flags = {
                row[0] for row in conn.execute(
                    "SELECT key FROM _meta WHERE key IN (?, ?)",
                    (_FTS_REBUILD_PENDING, _TRIGRAM_DONE),
                )
            }
            if _FTS_REBUILD_PENDING in flags or self._fts5_shadow_corrupt(conn):
                logger.warning("[MemoryStorage] Rebuilding FTS5 index from chunks.")
                self._recreate(conn, self._drop_fts5_objects, self._create_fts5_objects)
                self._refill_index(conn, "chunks_fts")
                with conn:
                    conn.execute("DELETE FROM _meta WHERE key = ?", (_FTS_REBUILD_PENDING,))
            if self.trigram_fts5_available and _TRIGRAM_DONE not in flags:
                if conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
                    logger.info("[MemoryStorage] Rebuilding trigram index from chunks.")
                    self._recreate(
                        conn, self._drop_trigram_objects, self._create_trigram_objects
                    )
                    self._refill_index(conn, "chunks_fts_trigram")
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, '1')",
                        (_TRIGRAM_DONE,),
                    )

    @staticmethod
    def _recreate(conn: sqlite3.Connection, drop, create):
        conn.execute("BEGIN IMMEDIATE")
        try:
            drop(conn)
            create(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _refill_index(conn: sqlite3.Connection, table: str):
        """Feed every existing chunk into an empty index, a batch at a time.

        Rows written after this starts reach the index through its triggers,
        so only rows up to the current maximum rowid are fed here."""
        top = conn.execute("SELECT MAX(rowid) FROM chunks").fetchone()[0]
        if top is None:
            return
        last = conn.execute("SELECT MIN(rowid) FROM chunks").fetchone()[0] - 1
        while last < top:
            row = conn.execute(
                "SELECT rowid FROM chunks WHERE rowid > ? AND rowid <= ? "
                "ORDER BY rowid LIMIT 1 OFFSET ?",
                (last, top, _REFILL_BATCH - 1),
            ).fetchone()
            end = row[0] if row else top
            with conn:
                conn.execute(
                    f"INSERT INTO {table}(rowid, text, id, user_id, path, source, scope) "
                    "SELECT rowid, text, id, user_id, path, source, scope FROM chunks "
                    "WHERE rowid > ? AND rowid <= ?",
                    (last, end),
                )
            last = end

    def _init_db(self):
        """Initialize database with schema.

        Runs on every session's first message, so it only issues statements
        that don't depend on the size of the database; anything that reads the
        index is left to _schedule_maintenance.
        """
        try:
            try:
                self.conn = self._open_conn()
            except sqlite3.DatabaseError as e:
                # The conversation history lives in this file too, so it stays
                # where it is for manual repair rather than being moved away.
                if _is_corruption_error(e):
                    from common.log import logger
                    logger.error(
                        f"[MemoryStorage] Database unreadable, left in place "
                        f"(repair manually, e.g. sqlite3 .recover): {self.db_path}: {e}"
                    )
                raise

            # Check FTS5 support
            self.fts5_available = self._check_fts5_support()
            if not _HAS_UPSERT:
                from common.log import logger
                logger.warning(
                    "[MemoryStorage] SQLite %s < 3.24 — UPSERT unavailable. "
                    "Falling back to INSERT OR REPLACE; FTS5 rowid may drift on "
                    "chunk updates (rebuild index periodically to recover).",
                    sqlite3.sqlite_version,
                )
            if not self.fts5_available:
                from common.log import logger
                logger.debug("[MemoryStorage] FTS5 not available, using LIKE-based keyword search")
        except Exception as e:
            from common.log import logger
            logger.error(f"[MemoryStorage] Unexpected error during database initialization: {e}")
            raise

        # Create chunks table with embeddings
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                scope TEXT NOT NULL DEFAULT 'shared',
                source TEXT NOT NULL DEFAULT 'memory',
                path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT,
                hash TEXT NOT NULL,
                metadata TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                updated_at INTEGER DEFAULT (strftime('%s', 'now')),
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at INTEGER,
                element_released INTEGER NOT NULL DEFAULT 0,
                file_sealed INTEGER NOT NULL DEFAULT 0,
                sealed_owner TEXT,
                body_path TEXT,
                body_anchor TEXT,
                body_hash TEXT,
                text_state TEXT NOT NULL DEFAULT 'projection'
            )
        """)

        # Older builds created chunks without hit tracking; add the columns
        # in place so an existing index keeps working without a rebuild.
        self._migrate_hit_columns()
        # P0-11 P-2: same for the release projection flags.
        self._migrate_release_columns()
        # P0-17 B-1/B-2: the schema is now CHECKED. The migration adds what
        # this build knows; this proves the result is queryable, so a
        # database that could not be migrated fails here — loudly, at open
        # time — instead of returning empty results for every later query.
        self._verify_schema()

        # P0-21: the write-side guard. A row that must not be public
        # cannot become public — not by this build, and not by the old
        # desktop build that is still running against the same file.
        # The guard is a BEFORE trigger, so it does not depend on the
        # order the FTS5 sync triggers happen to fire in.
        try:
            from agent.memory.index_guard import install_guard

            install_guard(self.conn)
        except Exception as e:
            # A guard that cannot be installed is a guard that does not
            # exist, so this is loud rather than a silent skip.
            from common.log import logger
            logger.error(
                f"[MemoryStorage] Index guard could not be installed on "
                f"{self.db_path}: {e}"
            )
            raise

        # Create indexes
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_user 
            ON chunks(user_id)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_scope 
            ON chunks(scope)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_hash 
            ON chunks(path, hash)
        """)

        # Internal key-value store for persistent flags (e.g. backfill tracking)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS _meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        repair_pending = False
        # Create FTS5 virtual table + triggers (only if supported).
        # If the previous process crashed mid-rebuild and left triggers
        # pointing at a missing chunks_fts (or vice versa), the missing side is
        # created here so chunk writes keep working, and the index is flagged
        # to be refilled in the background.
        if self.fts5_available:
            if self._fts5_state_inconsistent(self.conn):
                from common.log import logger
                logger.warning(
                    "[MemoryStorage] FTS5 state inconsistent (triggers/table mismatch). "
                    "Rebuilding chunks_fts in the background."
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, '1')",
                    (_FTS_REBUILD_PENDING,),
                )
                repair_pending = True
            if self._migrate_legacy_fts_triggers():
                repair_pending = True
            self._create_fts5_objects(self.conn)

        # Create trigram FTS5 table for CJK / mixed-language search
        self.trigram_fts5_available = False
        if self.fts5_available:
            try:
                self._create_trigram_objects(self.conn)
                # CREATE ... IF NOT EXISTS says nothing when the table is
                # already there: SQLite skips it without loading the module, so
                # a database written by a build with the trigram tokenizer looks
                # healthy to one without it, right up until a trigger fires.
                # Touch the table to find out now.
                self.conn.execute("SELECT 1 FROM chunks_fts_trigram LIMIT 1").fetchone()
                self.trigram_fts5_available = True
                # An empty chunks table has nothing to backfill; its trigram
                # index is kept complete by the triggers from here on.
                if not self.conn.execute(
                    "SELECT 1 FROM chunks LIMIT 1"
                ).fetchone():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO _meta(key, value) VALUES(?, '1')",
                        (_TRIGRAM_DONE,),
                    )
            except Exception:
                from common.log import logger
                logger.warning("[MemoryStorage] trigram FTS5 unavailable, CJK search will use LIKE fallback", exc_info=True)
                self._disable_trigram_index()

        # Create files metadata table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'memory',
                hash TEXT NOT NULL,
                mtime INTEGER NOT NULL,
                size INTEGER NOT NULL,
                updated_at INTEGER DEFAULT (strftime('%s', 'now'))
            )
        """)
        # P1-阶段二: files bookkeeping grows chunk_count / chunker_version so a
        # reader can tell HOW a projection was cut (and a probe can re-cut the
        # same way). Additive, in place, idempotent — a database an older build
        # wrote simply lacks the columns until this build opens it.
        self._migrate_files_columns()

        self.conn.commit()

        # P1-阶段一 (M1): persist the schema fingerprint AFTER the schema is
        # committed, so the token describes a schema that actually landed
        # rather than one still in a transaction (F-5). The gate that decides
        # whether this index may serve reads one comparable token from
        # ``_meta``; the writer and reader compare tokens, and a mismatch is
        # what a NOTRUN / refuse-to-serve decision keys on.
        try:
            from agent.memory.schema_drift import schema_fingerprint
            fp = schema_fingerprint(self.db_path)
            if fp:
                self.conn.execute(
                    "INSERT OR REPLACE INTO _meta(key, value) "
                    "VALUES('schema_fingerprint', ?)",
                    (fp,),
                )
                self.conn.commit()
        except Exception as e:
            # A fingerprint that cannot be written must not block startup —
            # the reader treats a missing token as "unknown", not "wrong" —
            # but it is recorded, not silent (F-2).
            from common.log import logger
            logger.debug(
                f"[MemoryStorage] schema fingerprint not written to "
                f"{self.db_path}: {e}"
            )

        self._schedule_maintenance(repair_pending)

    def _disable_trigram_index(self):
        """Detach a trigram index this build cannot open.

        The table and its triggers live in the database file, so one written by
        a build that has the trigram tokenizer (SQLite 3.34+) makes *every*
        insert, update and delete on ``chunks`` fail with "no such tokenizer:
        trigram" once an older build opens it - the triggers fire on all three.
        That is the whole memory and knowledge index, not just CJK search.

        Only the triggers are dropped: dropping the table itself runs the
        module's destructor, which fails for the same reason. The orphan table
        is harmless while no trigger writes to it, and searches already gate on
        ``trigram_fts5_available``. Clearing the backfill flag is what makes a
        later open by a capable build rebuild the index over the writes it
        missed.
        """
        try:
            for trigger in ("chunks_trigram_ai", "chunks_trigram_ad", "chunks_trigram_au"):
                self.conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            self.conn.execute("DELETE FROM _meta WHERE key = ?", (_TRIGRAM_DONE,))
        except Exception:
            from common.log import logger
            logger.warning("[MemoryStorage] Failed to drop trigram triggers", exc_info=True)

    def _migrate_hit_columns(self):
        """Add hit tracking columns to a chunks table created by an older build.

        PRAGMA table_info is metadata-only, so the check is cheap enough to run
        on every open; ALTER TABLE only fires when the columns are missing.
        """
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(chunks)")}
        if "hit_count" not in cols:
            self.conn.execute(
                "ALTER TABLE chunks ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0"
            )
        if "last_hit_at" not in cols:
            self.conn.execute("ALTER TABLE chunks ADD COLUMN last_hit_at INTEGER")

    def _migrate_files_columns(self):
        """Add chunk_count / chunker_version to a files table an older build wrote.

        PRAGMA table_info is metadata-only; ALTER TABLE only fires when the
        columns are missing. Idempotent.
        """
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(files)")}
        if "chunk_count" not in cols:
            self.conn.execute(
                "ALTER TABLE files ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0")
        if "chunker_version" not in cols:
            self.conn.execute(
                "ALTER TABLE files ADD COLUMN chunker_version INTEGER NOT NULL DEFAULT 0")

    def _migrate_release_columns(self):
        """Add the projection + externalization columns to an older index.

        The release projection flags default to 0 ("not released / not
        sealed") — the safe-for-visibility reading for a column that does
        not exist yet: an index written before this feature has no released
        content in it unless the truth layer says otherwise, and the
        projection refresh re-stamps every row from the disk before the
        next search anyway.

        P1-阶段一 (D-A): the column set is the SINGLE SOURCE OF TRUTH in
        ``agent.memory.schema_drift.ADDABLE_COLUMNS`` — the same map the
        drift detector reads — instead of a hand-maintained copy here. The
        four body-externalization columns (D-C) ride the same mechanism: a
        database that predates them gets them added, and their defaults
        (text here, no body file) are exactly what a row that never
        externalized should read.
        """
        from agent.memory.schema_drift import ADDABLE_COLUMNS

        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(chunks)")}
        added = []
        for name, decl in ADDABLE_COLUMNS.items():
            if name not in cols:
                self.conn.execute(
                    f"ALTER TABLE chunks ADD COLUMN {name} {decl}"
                )
                added.append(name)
        if added:
            from common.log import logger
            logger.info(
                f"[MemoryStorage] migrated chunks columns on {self.db_path}: "
                f"+{', '.join(added)}"
            )

    def _verify_schema(self):
        """Fail loudly when the index cannot answer the queries we run.

        P0-17 B-2: the migration above ADDS the columns this build knows
        about, but it cannot add a column a future build introduced, and
        a database whose file is not writable cannot be migrated at all.
        Either way the retrieval queries would fail with "no such column",
        and the retrieval path used to catch that and return an empty list
        — indistinguishable from "nothing matched".

        So the schema is CHECKED, not assumed, and a mismatch raises
        :class:`MemorySchemaError`. The check is metadata-only (one
        ``PRAGMA table_info``), so it is cheap enough to run on every open.
        """
        from agent.memory.schema_drift import (
            EXPECTED_CHUNK_COLUMNS,
            chunk_columns,
        )

        cols = chunk_columns(self.conn)
        if cols is None:
            # No chunks table at all: this database was never initialized.
            # _init_db creates it before this runs, so reaching here means
            # the CREATE was skipped — which is itself a structural
            # failure worth reporting rather than querying.
            raise MemorySchemaError(
                f"[MemoryStorage] index {self.db_path} has no 'chunks' table; "
                f"the schema was never initialized"
            )
        missing = [c for c in EXPECTED_CHUNK_COLUMNS if c not in cols]
        if missing:
            raise MemorySchemaError(
                f"[MemoryStorage] index {self.db_path} is missing the column(s) "
                f"{', '.join(missing)}; the retrieval queries cannot run "
                f"against it. Migrate it (ALTER TABLE chunks ADD COLUMN ...) "
                f"before searching."
            )

    def _migrate_legacy_fts_triggers(self) -> bool:
        """Drop external-content triggers that cannot read the previous row text.

        AFTER DELETE/UPDATE triggers must feed FTS5 the old column values, so the
        old shapes are dropped here and recreated by ``_create_*_objects``. Only
        the original trigram UPDATE trigger flags a rebuild; other stale entries
        are left to the integrity scan, which rebuilds an index it finds damaged.
        Returns True when a rebuild was flagged.
        """
        legacy = []
        for trigger, table, operation in (
            ("chunks_ad", "chunks_fts", "DELETE FROM"),
            ("chunks_au", "chunks_fts", "UPDATE"),
            ("chunks_trigram_ad", "chunks_fts_trigram", "DELETE FROM"),
            ("chunks_trigram_au", "chunks_fts_trigram", "UPDATE"),
        ):
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger,),
            ).fetchone()
            if row and row[0] and f"{operation} {table}".upper() in " ".join(row[0].upper().split()):
                legacy.append(trigger)
        for trigger in legacy:
            self.conn.execute(f"DROP TRIGGER {trigger}")
        if "chunks_trigram_au" not in legacy:
            return False
        self.conn.execute("DELETE FROM _meta WHERE key = ?", (_TRIGRAM_DONE,))
        return True

    @staticmethod
    def _fts5_state_inconsistent(conn: sqlite3.Connection) -> bool:
        """Detect a half-broken FTS5 setup (e.g. trigger exists but table doesn't)."""
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
            ).fetchone()
            table_exists = row is not None
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                "AND name IN ('chunks_ai','chunks_ad','chunks_au')"
            ).fetchone()
            trigger_count = int(row[0]) if row else 0
        except Exception:
            return False
        # Healthy = both present (3 triggers + table) or both absent.
        return table_exists != (trigger_count > 0)

    @staticmethod
    def _drop_fts5_objects(conn: sqlite3.Connection):
        # Triggers first; otherwise the next chunks write hits
        # "no such table: chunks_fts".
        conn.execute("DROP TRIGGER IF EXISTS chunks_ai")
        conn.execute("DROP TRIGGER IF EXISTS chunks_ad")
        conn.execute("DROP TRIGGER IF EXISTS chunks_au")
        conn.execute("DROP TABLE IF EXISTS chunks_fts")

    @staticmethod
    def _create_fts5_objects(conn: sqlite3.Connection):
        """Create chunks_fts virtual table and the 3 sync triggers (idempotent)."""
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text,
                id UNINDEXED,
                user_id UNINDEXED,
                path UNINDEXED,
                source UNINDEXED,
                scope UNINDEXED,
                content='chunks',
                content_rowid='rowid'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text, id, user_id, path, source, scope)
                VALUES (new.rowid, new.text, new.id, new.user_id, new.path, new.source, new.scope);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, id, user_id, path, source, scope)
                VALUES ('delete', old.rowid, old.text, old.id, old.user_id, old.path, old.source, old.scope);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, id, user_id, path, source, scope)
                VALUES ('delete', old.rowid, old.text, old.id, old.user_id, old.path, old.source, old.scope);
                INSERT INTO chunks_fts(rowid, text, id, user_id, path, source, scope)
                VALUES (new.rowid, new.text, new.id, new.user_id, new.path, new.source, new.scope);
            END
        """)

    @staticmethod
    def _drop_trigram_objects(conn: sqlite3.Connection):
        conn.execute("DROP TRIGGER IF EXISTS chunks_trigram_ai")
        conn.execute("DROP TRIGGER IF EXISTS chunks_trigram_ad")
        conn.execute("DROP TRIGGER IF EXISTS chunks_trigram_au")
        conn.execute("DROP TABLE IF EXISTS chunks_fts_trigram")

    @staticmethod
    def _create_trigram_objects(conn: sqlite3.Connection):
        """Create chunks_fts_trigram and its 3 sync triggers (idempotent)."""
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_trigram USING fts5(
                text,
                id UNINDEXED,
                user_id UNINDEXED,
                path UNINDEXED,
                source UNINDEXED,
                scope UNINDEXED,
                content='chunks',
                content_rowid='rowid',
                tokenize='trigram case_sensitive 0'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_trigram_ai
            AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts_trigram(rowid, text, id, user_id, path, source, scope)
                VALUES (new.rowid, new.text, new.id, new.user_id, new.path, new.source, new.scope);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_trigram_ad
            AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts_trigram(chunks_fts_trigram, rowid, text, id, user_id, path, source, scope)
                VALUES ('delete', old.rowid, old.text, old.id, old.user_id, old.path, old.source, old.scope);
            END
        """)
        # External-content FTS5 requires the delete+insert pattern on
        # UPDATE: a bare "UPDATE chunks_fts_trigram SET ..." leaves the
        # old tokens in the index and corrupts the trigram shadow tables
        # ("database disk image is malformed"). The special 'delete'
        # command removes the old row's tokens using its previous text.
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_trigram_au
            AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts_trigram(chunks_fts_trigram, rowid, text, id, user_id, path, source, scope)
                VALUES ('delete', old.rowid, old.text, old.id, old.user_id, old.path, old.source, old.scope);
                INSERT INTO chunks_fts_trigram(rowid, text, id, user_id, path, source, scope)
                VALUES (new.rowid, new.text, new.id, new.user_id, new.path, new.source, new.scope);
            END
        """)

    def reset_fts5(self):
        """Drop and recreate chunks_fts + triggers in one transaction.

        Used by rebuild_index to recover from FTS5 shadow-table corruption
        (bm25/ORDER BY rank may raise "database disk image is malformed"
        even when raw MATCH still works).
        """
        if not self.fts5_available:
            return
        with self._lock:
            self._recreate(self.conn, self._drop_fts5_objects, self._create_fts5_objects)

    @staticmethod
    def _fts5_shadow_corrupt(conn: sqlite3.Connection) -> bool:
        """Probe whether bm25 over chunks_fts errors out.

        Schema (table + triggers) can be intact while the underlying
        FTS5 shadow blobs are malformed — typically because the previous
        process crashed mid-write or wrote with a different SQLite build.
        A cheap MATCH probe surfaces it immediately."""
        try:
            conn.execute(
                "SELECT bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH 'a' LIMIT 1"
            ).fetchone()
            return False
        except sqlite3.DatabaseError as e:
            msg = str(e).lower()
            return "malformed" in msg or "corrupt" in msg
        except Exception:
            # Any other error (e.g. table missing) is handled by the
            # state-inconsistent path; treat as healthy here.
            return False

    def save_chunk(self, chunk: MemoryChunk):
        """Save a memory chunk (insert or update by id).

        Uses SQLite UPSERT (INSERT … ON CONFLICT DO UPDATE) instead of
        INSERT OR REPLACE.  INSERT OR REPLACE internally does DELETE+INSERT,
        which changes the row's rowid.  Because both FTS5 tables use
        content_rowid='rowid', a new rowid would leave the old FTS index
        entries pointing at a non-existent rowid and trigger
        "fts5: missing row N from content table" errors.
        ON CONFLICT DO UPDATE fires the AFTER UPDATE trigger (chunks_au /
        chunks_trigram_au) and keeps the original rowid intact.
        """
        if _HAS_UPSERT:
            _SQL = """
                INSERT INTO chunks
                (id, user_id, scope, source, path, start_line, end_line,
                 text, embedding, hash, metadata, element_released,
                 file_sealed, sealed_owner, body_path, body_anchor,
                 body_hash, text_state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                    user_id     = excluded.user_id,
                    scope       = excluded.scope,
                    source      = excluded.source,
                    path        = excluded.path,
                    start_line  = excluded.start_line,
                    end_line    = excluded.end_line,
                    text        = excluded.text,
                    embedding   = excluded.embedding,
                    hash        = excluded.hash,
                    metadata    = excluded.metadata,
                    element_released = excluded.element_released,
                    file_sealed      = excluded.file_sealed,
                    sealed_owner     = excluded.sealed_owner,
                    body_path        = excluded.body_path,
                    body_anchor      = excluded.body_anchor,
                    body_hash        = excluded.body_hash,
                    text_state       = excluded.text_state,
                    updated_at  = strftime('%s', 'now')
            """
        else:
            _SQL = """
                INSERT OR REPLACE INTO chunks
                (id, user_id, scope, source, path, start_line, end_line,
                 text, embedding, hash, metadata, element_released,
                 file_sealed, sealed_owner, body_path, body_anchor,
                 body_hash, text_state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """
        params = (
            chunk.id, chunk.user_id, chunk.scope, chunk.source, chunk.path,
            chunk.start_line, chunk.end_line, chunk.text,
            None,
            chunk.hash,
            json.dumps(chunk.metadata) if chunk.metadata else None,
            int(chunk.element_released or 0),
            int(chunk.file_sealed or 0),
            chunk.sealed_owner or None,
            chunk.body_path or None,
            chunk.body_anchor or None,
            chunk.body_hash or None,
            chunk.text_state or "projection",
        )
        with self._lock:
            try:
                self.conn.execute(_SQL, params)
                self.vector_backend.upsert([self._to_vector_record(chunk)])
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def save_chunks_batch(self, chunks: List[MemoryChunk]):
        """Save multiple chunks in a batch (insert or update by id).

        See save_chunk for why UPSERT is used instead of INSERT OR REPLACE.
        """
        if _HAS_UPSERT:
            _SQL = """
                INSERT INTO chunks
                (id, user_id, scope, source, path, start_line, end_line,
                 text, embedding, hash, metadata, element_released,
                 file_sealed, sealed_owner, body_path, body_anchor,
                 body_hash, text_state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                    user_id     = excluded.user_id,
                    scope       = excluded.scope,
                    source      = excluded.source,
                    path        = excluded.path,
                    start_line  = excluded.start_line,
                    end_line    = excluded.end_line,
                    text        = excluded.text,
                    embedding   = excluded.embedding,
                    hash        = excluded.hash,
                    metadata    = excluded.metadata,
                    element_released = excluded.element_released,
                    file_sealed      = excluded.file_sealed,
                    sealed_owner     = excluded.sealed_owner,
                    body_path        = excluded.body_path,
                    body_anchor      = excluded.body_anchor,
                    body_hash        = excluded.body_hash,
                    text_state       = excluded.text_state,
                    updated_at  = strftime('%s', 'now')
            """
        else:
            _SQL = """
                INSERT OR REPLACE INTO chunks
                (id, user_id, scope, source, path, start_line, end_line,
                 text, embedding, hash, metadata, element_released,
                 file_sealed, sealed_owner, body_path, body_anchor,
                 body_hash, text_state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """
        params_list = [
            (
                c.id, c.user_id, c.scope, c.source, c.path,
                c.start_line, c.end_line, c.text,
                None,
                c.hash,
                json.dumps(c.metadata) if c.metadata else None,
                int(c.element_released or 0),
                int(c.file_sealed or 0),
                # P0-16 L-5 / B-2: the dark page's owner token. The batch
                # path is the one the SCAN uses, so leaving this column out
                # here is what made every dark page ownerless — and an
                # ownerless dark page is invisible to its own owner.
                c.sealed_owner or None,
                c.body_path or None,
                c.body_anchor or None,
                c.body_hash or None,
                c.text_state or "projection",
            )
            for c in chunks
        ]
        with self._lock:
            try:
                self.conn.executemany(_SQL, params_list)
                self.vector_backend.upsert([
                    self._to_vector_record(chunk) for chunk in chunks
                ])
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
    
    def get_chunk(self, chunk_id: str) -> Optional[MemoryChunk]:
        """Get a chunk by ID"""
        row = self.conn.execute("""
            SELECT * FROM chunks WHERE id = ?
        """, (chunk_id,)).fetchone()
        
        if not row:
            return None
        
        return self._row_to_chunk(row)

    def record_hits(self, keys: List[Tuple[str, int, int]]):
        """Count one retrieval hit per chunk key (path, start_line, end_line).

        Called from the search path, so it stays a single small transaction
        that only touches the matched rows. Callers are expected to wrap it in
        try/except: bookkeeping must never break the search that produced it.
        """
        if not keys:
            return
        with self._lock:
            try:
                self.conn.executemany(
                    "UPDATE chunks SET hit_count = hit_count + 1, "
                    "last_hit_at = strftime('%s', 'now') "
                    "WHERE path = ? AND start_line = ? AND end_line = ?",
                    [(path, start_line, end_line) for path, start_line, end_line in keys],
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def get_hit_stats(
        self, keys: List[Tuple[str, int, int]]
    ) -> Dict[Tuple[str, int, int], Tuple[int, Optional[int]]]:
        """Read (hit_count, last_hit_at) per chunk key; absent keys are omitted."""
        stats: Dict[Tuple[str, int, int], Tuple[int, Optional[int]]] = {}
        for path, start_line, end_line in keys:
            row = self.conn.execute(
                "SELECT hit_count, last_hit_at FROM chunks "
                "WHERE path = ? AND start_line = ? AND end_line = ? LIMIT 1",
                (path, start_line, end_line),
            ).fetchone()
            if row:
                stats[(path, start_line, end_line)] = (
                    int(row["hit_count"] or 0),
                    row["last_hit_at"],
                )
        return stats

    def search_vector(
        self,
        query_embedding: List[float],
        user_id: Optional[str] = None,
        scopes: List[str] = None,
        limit: int = 10,
        identities: Optional[List[str]] = None,
        owner_tokens: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """Search the configured vector backend.

        P0-18 R4: the exception contract is the SAME as the keyword path's.
        A structural failure (the index is missing a column the visibility
        predicate names, the table is gone, the file is damaged) raises
        :class:`MemorySchemaError`, so a caller that writes
        ``except MemorySchemaError:`` catches the vector path too. A
        half-written contract is worse than none: the keyword path
        promised a type, the vector path raised a raw
        ``sqlite3.OperationalError``, and the caller's handler let it
        through.

        A failure that is NOT structural (a malformed embedding, a wrong
        argument type) is left alone — wrapping it would tell the caller
        "the index is broken" when the index is fine.
        """
        if scopes is None:
            scopes = ["shared"]
            if user_id or identities or owner_tokens:
                scopes.append("user")
                scopes.append("sealed")
        metadata_filter = {"scopes": scopes}
        if user_id:
            metadata_filter["user_id"] = user_id
        if identities:
            metadata_filter["identities"] = list(identities)
        if owner_tokens:
            metadata_filter["owner_tokens"] = list(owner_tokens)
        try:
            matches = self.vector_backend.search(
                query_embedding,
                limit=limit,
                metadata_filter=metadata_filter,
            )
        except sqlite3.Error as e:
            from common.log import logger
            logger.error(
                "[MemoryStorage] search_vector failed (structural error, "
                "refusing to report an empty result): %s", e, exc_info=True)
            raise MemorySchemaError(
                f"[MemoryStorage] vector search failed on {self.db_path}: {e}"
            ) from e
        return [
            SearchResult(
                path=match.metadata["path"],
                start_line=match.metadata["start_line"],
                end_line=match.metadata["end_line"],
                score=match.score,
                snippet=self._truncate_text(match.metadata["text"], 500),
                source=match.metadata["source"],
                user_id=match.metadata.get("user_id"),
            )
            for match in matches
        ]
    
    def search_keyword(
        self,
        query: str,
        user_id: Optional[str] = None,
        scopes: List[str] = None,
        limit: int = 10,
        identities: Optional[List[str]] = None,
        owner_tokens: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """
        Keyword search using FTS5 + LIKE fallback

        Strategy:
        1. If FTS5 available and healthy: try FTS5 first
        2. Always fall back to LIKE for CJK queries
        3. If FTS5 fails OR returns empty for non-CJK, also try LIKE so a
           broken FTS5 shadow table doesn't silently kill keyword search.

        ``identities`` are the caller's other END USER identity tokens, and
        ``owner_tokens`` are the caller's AGENT owner tokens (``agent--<id>``).
        They are two separate axes and they reach two separate columns: the
        end-user axis through ``user_id``, the owner axis through
        ``sealed_owner`` (P0-16 L-5). A dark page — indexed under its
        owner's agent token — is therefore visible to the agent that wrote
        it and to nobody else, and an end-user id that happens to look
        like an owner token cannot reach it.
        """
        if scopes is None:
            scopes = ["shared"]
            if user_id or identities or owner_tokens:
                scopes.append("user")
                scopes.append("sealed")

        # Step 1: Standard FTS5 (unicode61) — pure ASCII queries only.
        # Skipped when query contains any CJK characters: unicode61 tokenises CJK
        # as individual characters without forming meaningful tokens, so it would
        # match only the ASCII portion of a mixed query (e.g. "Python" from
        # "Python教程") and silently discard the CJK part.  Those queries go
        # directly to Step 2 (trigram), which handles both ASCII and CJK together.
        #
        # P0-17 B-2: a stage that fails for a STRUCTURAL reason is recorded
        # and the chain continues — a broken FTS5 shadow table must not kill
        # keyword search. But if EVERY stage failed, the call is an ERROR,
        # not an empty result: raising here is what stops "the query could
        # not run" from being reported as "nothing matched".
        first_failure: Optional[MemorySchemaError] = None
        fts1_attempted = False
        if (self.fts5_available
                and not MemoryStorage._contains_cjk(query)
                and MemoryStorage._build_fts_query(query)):
            fts1_attempted = True
            try:
                fts_results = self._search_fts5(query, user_id, scopes, limit,
                                                identities, owner_tokens)
            except MemorySchemaError as e:
                first_failure = first_failure or e
            else:
                if fts_results:
                    return fts_results

        # Step 2: Trigram FTS5 — CJK/mixed queries, plus fallback when unicode61
        # returned nothing (trigram indexes all scripts with 3-char sliding windows,
        # so it can catch terms that unicode61 tokenisation misses).
        if self.trigram_fts5_available and (
            MemoryStorage._contains_cjk(query) or fts1_attempted
        ):
            try:
                trigram_results = self._search_fts5_trigram(
                    query, user_id, scopes, limit, identities, owner_tokens)
            except MemorySchemaError as e:
                first_failure = first_failure or e
            else:
                if trigram_results:
                    return trigram_results

        # Step 3: LIKE fallback — last resort (FTS5 unavailable, CJK queries
        # trigram cannot match, or an FTS5 stage that found nothing).
        if (not self.fts5_available
                or MemoryStorage._contains_cjk(query)
                or fts1_attempted):
            try:
                like_results = self._search_like(query, user_id, scopes, limit,
                                                 identities, owner_tokens)
            except MemorySchemaError as e:
                first_failure = first_failure or e
            else:
                # A stage that RAN and found nothing is a genuine empty
                # result: the caller gets [].
                return like_results

        # Every stage failed: the query never ran. That is an error, not an
        # absence of matches, and saying otherwise is the silent lie this
        # whole change exists to remove.
        if first_failure is not None:
            raise first_failure
        return []
    
    def _search_fts5(
        self,
        query: str,
        user_id: Optional[str],
        scopes: List[str],
        limit: int,
        identities: Optional[List[str]] = None,
        owner_tokens: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """FTS5 full-text search"""
        fts_query = self._build_fts_query(query)
        if not fts_query:
            return []
        
        scope_placeholders = ','.join('?' * len(scopes))
        vis_sql, vis_params = visibility_clause(
            user_id, "chunks.scope", "chunks.user_id",
            "chunks.element_released", "chunks.file_sealed",
            identities=identities,
            owner_token_list=owner_tokens,
            sealed_owner_col="chunks.sealed_owner")
        sql_query = f"""
            SELECT chunks.*, bm25(chunks_fts) as rank
            FROM chunks_fts
            JOIN chunks ON chunks.rowid = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            AND chunks.scope IN ({scope_placeholders})
            AND {vis_sql}
            ORDER BY rank
            LIMIT ?
        """
        params = [fts_query] + list(scopes) + vis_params + [limit]
        
        try:
            rows = self.conn.execute(sql_query, params).fetchall()
            return [
                SearchResult(
                    path=row['path'],
                    start_line=row['start_line'],
                    end_line=row['end_line'],
                    score=self._bm25_rank_to_score(row['rank']),
                    snippet=self._snippet_around(row['text'], query, 500),
                    source=row['source'],
                    user_id=row['user_id']
                )
                for row in rows
            ]
        except sqlite3.Error as e:
            # P0-17 B-2: a STRUCTURAL failure must not become "no matches".
            # "The query could not run" and "nothing matched" are
            # indistinguishable from the outside, and the second reading is
            # a lie. So this raises.
            from common.log import logger
            logger.error(
                "[MemoryStorage] _search_fts5 failed (structural error, "
                "refusing to report an empty result): %s", e, exc_info=True)
            raise MemorySchemaError(
                f"[MemoryStorage] FTS5 search failed on {self.db_path}: {e}"
            ) from e

    @staticmethod
    def _snippet_around(text: str, query: str, max_chars: int = 500) -> str:
        """A snippet CENTRED on the query, not on the head of the chunk.

        P0-11 G4-c: the snippet must contain the 原话 the query found. A
        head-truncated snippet shows the first N characters of the chunk,
        which for a front-matter-carrying markdown file is the front-matter
        — so the words that matched were cut off and the caller saw
        metadata instead of the text they asked for.

        The window is placed around the first place the query's own
        characters appear. For CJK the query is segmented the same way the
        matcher segments it, so a two-character Chinese word is found too.
        When nothing is found (the match came from the vector path or from
        a word the segmenter dropped), the head is shown — the old
        behaviour, which is still better than nothing.
        """
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        haystack = text.lower()
        position = -1
        needle = (query or "").strip().lower()
        if needle:
            position = haystack.find(needle)
        if position < 0:
            # Fall back to the segmenter's windows: a CJK query matches
            # through a sliding window, so the whole query string may not
            # appear even though a window of it does.
            for group in _query_groups(query or ""):
                for window in group.windows:
                    pos = haystack.find(window.lower())
                    if pos >= 0:
                        position = pos
                        break
                if position >= 0:
                    break
        if position < 0:
            return text[:max_chars] + "..."
        half = max_chars // 2
        start = max(0, position - half)
        end = min(len(text), start + max_chars)
        start = max(0, end - max_chars)
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet

    def _search_like(
        self,
        query: str,
        user_id: Optional[str],
        scopes: List[str],
        limit: int,
        identities: Optional[List[str]] = None,
        owner_tokens: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """LIKE-based search.

        Used as the keyword-search fallback when FTS5 is unavailable, fails,
        or returns empty. Uses the SAME segmenter as the trigram stage, so the
        two paths cannot drift apart: a CJK run is matched by any of its
        sliding windows, which is what makes two-character Chinese words
        ("事件") reachable — the trigram tokenizer needs three characters and
        therefore cannot serve them at all.
        """
        groups = _query_groups(query)
        if not groups:
            return []

        scope_placeholders = ','.join('?' * len(scopes))

        def _build_where(active_groups):
            """LIKE clause for the given groups: ANDed, alternatives ORed."""
            group_clauses = []
            params = []
            for group in active_groups:
                alternatives = []
                for window in group.windows:
                    alternatives.append("LOWER(text) LIKE ?")
                    params.append(f'%{window.lower()}%')
                group_clauses.append("(" + " OR ".join(alternatives) + ")")
            return " AND ".join(group_clauses), params

        where_clause, params = _build_where(groups)
        params.extend(scopes)
        vis_sql, vis_params = visibility_clause(user_id,
                                                identities=identities,
                                                owner_token_list=owner_tokens,
                                                sealed_owner_col="chunks.sealed_owner")
        params.extend(vis_params)
        params.append(limit)
        sql_query = f"""
            SELECT * FROM chunks
            WHERE ({where_clause})
            AND scope IN ({scope_placeholders})
            AND {vis_sql}
            LIMIT ?
        """
        
        try:
            rows = self.conn.execute(sql_query, params).fetchall()
            results = []
            for row in rows:
                # Score by how much of the query the chunk actually covers,
                # not by how many tokens matched: the old count-based ladder
                # gave one short match (0.45) and four short matches (0.85)
                # nearly the same score, so a page quoting two words of a
                # question outranked the page that answers it.
                text_lower = row['text'].lower()
                quality = self._query_quality(groups, text_lower)
                if quality <= 0:
                    continue
                score = 0.3 + 0.55 * quality
                results.append(SearchResult(
                    path=row['path'],
                    start_line=row['start_line'],
                    end_line=row['end_line'],
                    score=score,
                    snippet=self._snippet_around(row['text'], query, 500),
                    source=row['source'],
                    user_id=row['user_id']
                ))

            if not results and len(groups) > 1:
                # The strict AND found nothing: the query asks for several
                # things and no page carries all of them. Retry each group on
                # its own and keep the best, rather than returning nothing.
                # The relaxed hits are scored against the FULL group list, so
                # a page matching only one part of a three-part question
                # still scores lower than one matching more of it.
                #
                # P0-18 L-2: the OWNER AXIS must travel with the call. The
                # relaxed path used to be called without owner_tokens, so the
                # sealed branch of the predicate degraded to the constant
                # ``(0)`` — the owner could not read their own dark page
                # through this path while the strict path could. Two paths,
                # one rule, two answers.
                #
                # P0-18 L-1: ``query=`` is passed BY KEYWORD, and it is the
                # ORIGINAL query text. The relaxed function needs it to cut
                # a snippet around the match; without it the function raised
                # NameError on the first row it found (P0-17 narrowed the
                # except clause to sqlite3.Error, which turned a silent
                # empty result into a user-visible crash).
                results = self._search_like_relaxed(
                    groups, user_id, scopes, limit,
                    identities=identities,
                    owner_tokens=owner_tokens,
                    query=query,
                )

            results.sort(key=lambda r: r.score, reverse=True)
            return results
        except sqlite3.Error as e:
            # P0-17 B-2: same rule as the FTS5 path — a structural failure
            # raises instead of masquerading as "no matches".
            from common.log import logger
            logger.error(
                "[MemoryStorage] _search_like failed (structural error, "
                "refusing to report an empty result): %s", e, exc_info=True)
            raise MemorySchemaError(
                f"[MemoryStorage] LIKE search failed on {self.db_path}: {e}"
            ) from e

    def _search_like_relaxed(
        self,
        groups: List[QueryGroup],
        user_id: Optional[str],
        scopes: List[str],
        limit: int,
        identities: Optional[List[str]] = None,
        owner_tokens: Optional[List[str]] = None,
        query: str = "",
    ) -> List[SearchResult]:
        """Per-group LIKE search, used when the ANDed query matched nothing.

        Each group is searched on its own and the hits are pooled. Scoring
        still uses every group, so a relaxed hit that covers more of the
        question outranks one that covers less — the relaxation widens the
        candidate set, it does not flatten the ranking.

        P0-18 L-1: ``query`` is the ORIGINAL query text, and it is REQUIRED
        for anything that reaches the row loop: the snippet is cut around
        the query, and the function has no other way to know what the
        caller asked for. It used to reference an undefined name of that
        spelling, so every relaxed search that found a row raised NameError.
        It defaults to ``""`` (the snippet then falls back to the head of
        the chunk) rather than being mandatory, so a caller that genuinely
        has no text to offer still gets results instead of a TypeError.

        P0-18 L-2: ``owner_tokens`` is the OWNER AXIS and it must be passed
        through unchanged. The strict path and this path answer the same
        question with the same rule; a caller that reads one of them must
        not get a different answer from the other.
        """
        scope_placeholders = ','.join('?' * len(scopes))
        seen = set()
        results = []
        for group in groups:
            alternatives = []
            params = []
            for window in group.windows:
                alternatives.append("LOWER(text) LIKE ?")
                params.append(f'%{window.lower()}%')
            where_clause = "(" + " OR ".join(alternatives) + ")"
            vis_sql, vis_params = visibility_clause(user_id,
                                                    identities=identities,
                                                    owner_token_list=owner_tokens,
                                                    sealed_owner_col="chunks.sealed_owner")
            sql = f"""
                SELECT * FROM chunks
                WHERE {where_clause}
                AND scope IN ({scope_placeholders})
                AND {vis_sql}
                LIMIT ?
            """
            params.extend(scopes)
            params.extend(vis_params)
            params.append(limit)
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                # P0-17 B-2: this path used to `continue`, which dropped the
                # group silently — the relaxed search then returned a
                # PARTIAL result set, which is worse than an empty one:
                # it looks like an answer. A structural failure raises.
                from common.log import logger
                logger.error(
                    "[MemoryStorage] _search_like_relaxed failed (structural "
                    "error, refusing to report a partial result): %s", e,
                    exc_info=True)
                raise MemorySchemaError(
                    f"[MemoryStorage] relaxed LIKE search failed on "
                    f"{self.db_path}: {e}"
                ) from e
            for row in rows:
                key = (row['path'], row['start_line'], row['end_line'])
                if key in seen:
                    continue
                seen.add(key)
                text_lower = row['text'].lower()
                quality = self._query_quality(groups, text_lower)
                if quality <= 0:
                    continue
                results.append(SearchResult(
                    path=row['path'],
                    start_line=row['start_line'],
                    end_line=row['end_line'],
                    score=0.3 + 0.55 * quality,
                    snippet=self._snippet_around(row['text'], query, 500),
                    source=row['source'],
                    user_id=row['user_id']
                ))
        return results

    @staticmethod
    def _query_quality(groups: List[QueryGroup], text: str) -> float:
        """Weighted mean of how well each query group is covered by the text.

        The weight is the group's length, so a five-character question is not
        satisfied by a two-character fragment of itself. A chunk that merely
        CONTAINS the query string is discounted: it matched the query as an
        example of itself, not as subject matter.
        """
        if not groups:
            return 0.0
        total = 0.0
        weight_sum = 0.0
        for group in groups:
            weight = group.weight
            total += _group_quality(group, text) * weight
            weight_sum += weight
        if weight_sum <= 0:
            return 0.0
        quality = min(1.0, total / weight_sum)
        return quality * _verbatim_mention_factor(groups, text)

    def replace_file_chunks(self, path: str, chunks: List[MemoryChunk],
                            file_hash: str, mtime: int, size: int):
        """Atomically replace every chunk row for one file (T1, P1-阶段二).

        The projection for one truth-layer file must never exist in a
        HALF-REBUILT state: the old rows are deleted and the new rows are
        inserted in a SINGLE transaction, and the ``files`` bookkeeping row
        (hash / mtime / size / chunker_version) is updated in the same one.
        A failure rolls the whole thing back, leaving the OLD projection
        intact — a stale-but-complete projection is strictly better than a
        partial one (the reader can still answer, and the probe marks it
        stale instead of returning garbage).

        ``hit_count`` / ``last_hit_at`` are inherited per chunk id from the
        old rows (creative E11): a rebuild must not silently reset the
        retrieval-heat signal — a chunk that was found often before is still
        the one that was found often, regardless of how many times the file
        has been re-cut.

        FTS stays consistent for free: the existing AFTER INSERT / DELETE /
        UPDATE triggers fire inside the same transaction, so the FTS shadow
        tables see exactly the same all-or-nothing boundary as ``chunks``.
        """
        # Snapshot the stats we want to survive, keyed by chunk id, BEFORE the
        # delete. The ids are deterministic (md5 of path:start:end[:part]), so
        # a rebuilt chunk lands on the SAME id and inherits its own heat.
        old_stats: Dict[str, Tuple[int, Optional[int]]] = {}
        for row in self.conn.execute(
                "SELECT id, hit_count, last_hit_at FROM chunks WHERE path = ?",
                (path,)):
            old_stats[row["id"]] = (int(row["hit_count"] or 0),
                                    row["last_hit_at"])

        if _HAS_UPSERT:
            ins_sql = """
                INSERT INTO chunks
                (id, user_id, scope, source, path, start_line, end_line,
                 text, embedding, hash, metadata, element_released,
                 file_sealed, sealed_owner, body_path, body_anchor,
                 body_hash, text_state, hit_count, last_hit_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(id) DO UPDATE SET
                    user_id = excluded.user_id, scope = excluded.scope,
                    source = excluded.source, path = excluded.path,
                    start_line = excluded.start_line, end_line = excluded.end_line,
                    text = excluded.text, embedding = excluded.embedding,
                    hash = excluded.hash, metadata = excluded.metadata,
                    element_released = excluded.element_released,
                    file_sealed = excluded.file_sealed,
                    sealed_owner = excluded.sealed_owner,
                    body_path = excluded.body_path, body_anchor = excluded.body_anchor,
                    body_hash = excluded.body_hash, text_state = excluded.text_state,
                    hit_count = excluded.hit_count, last_hit_at = excluded.last_hit_at,
                    updated_at = strftime('%s', 'now')
            """
        else:
            ins_sql = """
                INSERT OR REPLACE INTO chunks
                (id, user_id, scope, source, path, start_line, end_line,
                 text, embedding, hash, metadata, element_released,
                 file_sealed, sealed_owner, body_path, body_anchor,
                 body_hash, text_state, hit_count, last_hit_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))
            """

        params_list = []
        for c in chunks:
            hc, lha = old_stats.get(c.id, (0, None))
            params_list.append((
                c.id, c.user_id, c.scope, c.source, c.path,
                c.start_line, c.end_line, c.text,
                None,
                c.hash,
                json.dumps(c.metadata) if c.metadata else None,
                int(c.element_released or 0),
                int(c.file_sealed or 0),
                c.sealed_owner or None,
                c.body_path or None,
                c.body_anchor or None,
                c.body_hash or None,
                c.text_state or "projection",
                hc,
                lha,
            ))

        with self._lock:
            try:
                # One transaction: delete + reinsert + files-row update.
                # BEGIN IMMEDIATE takes the write lock now (not on first write)
                # so the whole unit is atomic against other writers too.
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
                if params_list:
                    self.conn.executemany(ins_sql, params_list)
                # The files bookkeeping row travels in the SAME transaction:
                # a rebuilt projection whose files row still points at the old
                # mtime is indistinguishable from a stale one.
                self.conn.execute(
                    "INSERT OR REPLACE INTO files "
                    "(path, source, hash, mtime, size, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'))",
                    (path, "memory", file_hash, mtime, size))
                self.vector_backend.upsert(
                    [self._to_vector_record(c) for c in chunks])
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def delete_by_path(self, path: str):
        """Delete all chunks and file metadata for a path."""
        with self._lock:
            try:
                self.vector_backend.delete(metadata_filter={"path": path})
                self.conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
                self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # --- _meta: internal key-value flags (e.g. chunker_version) -----------
    def get_meta(self, key: str) -> Optional[str]:
        """Read a persistent flag from the _meta table, or None if unset."""
        try:
            row = self.conn.execute(
                "SELECT value FROM _meta WHERE key = ?", (key,)
            ).fetchone()
        except Exception:
            return None
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a persistent flag to the _meta table."""
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO _meta(key, value) VALUES(?, ?)",
                (key, str(value)),
            )
            self.conn.commit()

    def get_file_hash(self, path: str) -> Optional[str]:
        """Get stored file hash"""
        row = self.conn.execute("""
            SELECT hash FROM files WHERE path = ?
        """, (path,)).fetchone()
        return row['hash'] if row else None

    def list_paths(self, source: str) -> List[str]:
        """Every path currently indexed under a source."""
        rows = self.conn.execute(
            "SELECT path FROM files WHERE source = ?", (source,)
        ).fetchall()
        return [row['path'] for row in rows]

    def list_chunk_paths(self, source: str) -> List[str]:
        """Every path that still has CHUNK rows under a source.

        ``list_paths`` reads the ``files`` table, so a chunk row whose
        ``files`` counterpart was lost is invisible to it — and therefore
        invisible to sync()'s reconcile, which is how such a row survives
        forever as an orphan (P0-10 L-1: ``memory/users/u-accept/
        memory_scope.md`` had a chunk and no files row on a real
        workspace). The reconcile needs both sets.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT path FROM chunks WHERE source = ?", (source,)
        ).fetchall()
        return [row['path'] for row in rows]

    def apply_release_projection(self, released_paths: set,
                                 sealed_paths: set,
                                 sealed_owner: Optional[Dict[str, str]] = None
                                 ) -> int:
        """Re-stamp the release flags of every indexed row from the truth layer.

        P0-11 P-2 (RR-04): the flags on a chunk row are a PROJECTION of the
        truth layer, and two things move them without the file's bytes
        changing:

          * a file released AFTER it was indexed — WR-11 keeps the old bytes
            untouched, so the scan never re-indexes it, and without this
            the released content stays retrievable forever;
          * a file that sealed itself after it was indexed — same story.

        So the flags are refreshed from the DISK (the authority) against the
        rows that exist, and only rows whose flag actually changed are
        written. Returns the number of rows changed.

        P0-14 L-2: a DARK PAGE is re-owned here as well. Its row must carry
        the owner's token under its own scope, otherwise the predicate
        cannot tell "the owner" from "everybody" — and a dark page indexed
        as a shared row would be public.
        """
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT path, element_released, file_sealed, scope, "
                "user_id, sealed_owner FROM chunks"
            ).fetchall()
        except Exception:
            return 0
        owners = sealed_owner or {}
        updates = []
        scope_updates = []
        owner_updates = []
        for row in rows:
            path = row["path"]
            want_released = 1 if path in released_paths else 0
            want_sealed = 1 if path in sealed_paths else 0
            if (int(row["element_released"] or 0) == want_released
                    and int(row["file_sealed"] or 0) == want_sealed):
                # The flags are right — but a dark page must also carry its
                # owner, and a page that used to be sealed may have been
                # re-opened on disk.
                want_scope, want_user = self._ownership_for(
                    path, want_sealed, owners)
                if want_scope is not None and (
                        row["scope"] != want_scope
                        or (row["user_id"] or "") != want_user):
                    scope_updates.append((want_scope, want_user, path))
                # P0-16 L-5 / B-2: the owner token lives in its OWN column,
                # and the projection must keep it in step — a dark page
                # whose ``sealed_owner`` is stale (or NULL) is a dark page
                # nobody can read, including its own owner.
                want_owner = (owners.get(path) or "") if want_sealed else ""
                if (row["sealed_owner"] or "") != want_owner:
                    owner_updates.append((want_owner, path))
                continue
            updates.append((want_released, want_sealed, path))
            want_scope, want_user = self._ownership_for(
                path, want_sealed, owners)
            if want_scope is not None and (
                    row["scope"] != want_scope
                    or (row["user_id"] or "") != want_user):
                scope_updates.append((want_scope, want_user, path))
            want_owner = (owners.get(path) or "") if want_sealed else ""
            if (row["sealed_owner"] or "") != want_owner:
                owner_updates.append((want_owner, path))
        if not updates and not scope_updates and not owner_updates:
            return 0
        with self._lock:
            try:
                if updates:
                    self.conn.executemany(
                        "UPDATE chunks SET element_released = ?, file_sealed = ? "
                        "WHERE path = ?", updates)
                if scope_updates:
                    self.conn.executemany(
                        "UPDATE chunks SET scope = ?, user_id = ? "
                        "WHERE path = ?", scope_updates)
                if owner_updates:
                    self.conn.executemany(
                        "UPDATE chunks SET sealed_owner = ? "
                        "WHERE path = ?", owner_updates)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return len(updates) + len(scope_updates)

    @staticmethod
    def _ownership_for(path: str, sealed: int,
                       owners: Dict[str, str]) -> Tuple[Optional[str], str]:
        """``(scope, user_id)`` a row must carry, or ``(None, "")`` to leave it.

        A dark page carries its own scope. Its OWNER does not go into the
        end-user axis: ``chunks.user_id`` holds end-user ids only, and the
        owner token lives in ``chunks.sealed_owner`` (P0-16 L-5). The
        projection must not put the owner token back into ``user_id`` —
        that is how an end-user id shaped like an owner token could read a
        dark page.

        A page that is NOT a dark page keeps whatever scope it had — the
        projection must not rewrite the ownership of ordinary rows.
        """
        if not sealed:
            return None, ""
        return SCOPE_SEALED, ""

    def update_file_metadata(self, path: str, source: str, file_hash: str, mtime: int, size: int):
        """Update file metadata"""
        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO files (path, source, hash, mtime, size, updated_at)
                VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'))
            """, (path, source, file_hash, mtime, size))
            self.conn.commit()
    
    def get_stats(self) -> Dict[str, int]:
        """Get storage statistics"""
        chunks_count = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM chunks
        """).fetchone()['cnt']

        files_count = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM files
        """).fetchone()['cnt']

        embedded_count = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM chunks WHERE embedding IS NOT NULL
        """).fetchone()['cnt']

        return {
            'chunks': chunks_count,
            'files': files_count,
            'embedded': embedded_count,
        }
    
    def close(self):
        """Close database connection"""
        if self.conn:
            try:
                self.conn.commit()  # Ensure all changes are committed
                self.conn.close()
                self.conn = None  # Mark as closed
            except Exception as e:
                from common.log import logger
                logger.warning("[MemoryStorage] Error closing database connection: %s", e)
    
    def __del__(self):
        """Destructor to ensure connection is closed"""
        try:
            self.close()
        except Exception:
            pass  # Ignore errors during cleanup
    
    # Helper methods

    @staticmethod
    def _to_vector_record(chunk: MemoryChunk) -> VectorRecord:
        return VectorRecord(
            id=chunk.id,
            embedding=chunk.embedding,
            metadata={
                "user_id": chunk.user_id,
                "scope": chunk.scope,
                "source": chunk.source,
                "path": chunk.path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "text": chunk.text,
                "metadata": chunk.metadata,
            },
        )

    @staticmethod
    def _decode_embedding(raw) -> Optional[List[float]]:
        """Decode embedding from BLOB bytes or legacy JSON string.
        Handles both numpy and numpy-free environments."""
        if raw is None:
            return None
        if isinstance(raw, (bytes, bytearray)):
            if _HAS_NUMPY:
                return np.frombuffer(raw, dtype=np.float32).tolist()
            import struct
            n = len(raw) // 4
            return list(struct.unpack(f'{n}f', raw))
        # Legacy JSON format written by older versions
        return json.loads(raw)

    def _row_to_chunk(self, row) -> MemoryChunk:
        """Convert database row to MemoryChunk.

        The four externalization columns may be absent on a database that
        predates them — but the open-time migration adds them, and
        ``_verify_schema`` fails loudly when they still are not there, so a
        missing column here is a genuine surprise worth not masking.
        """
        return MemoryChunk(
            id=row['id'],
            user_id=row['user_id'],
            scope=row['scope'],
            source=row['source'],
            path=row['path'],
            start_line=row['start_line'],
            end_line=row['end_line'],
            text=row['text'],
            embedding=self._decode_embedding(row['embedding']),
            hash=row['hash'],
            metadata=json.loads(row['metadata']) if row['metadata'] else None,
            body_path=row['body_path'],
            body_anchor=row['body_anchor'],
            body_hash=row['body_hash'],
            text_state=row['text_state'],
        )
    
    @staticmethod
    def _contains_cjk(text: str) -> bool:
        """Check if text contains CJK or related characters (Chinese, Japanese, Korean)."""
        return bool(_RE_CONTAINS_CJK.search(text))
    
    @staticmethod
    def _build_trigram_query(raw_query: str) -> Optional[str]:
        """
        Build FTS5 MATCH query for the trigram tokenizer.

        Uses the shared segmenter, so this path and the LIKE fallback agree on
        what a query means. A CJK run becomes the OR of its sliding windows
        ("王静静是谁" -> "王静静" OR "静静是" OR ... OR "是谁"); the groups
        themselves are ANDed, so a mixed query still means "both".

        The trigram tokenizer only indexes three-character windows, so a
        two-character alternative can never match on its own. It is kept in the
        expression anyway: the OR costs nothing, and it keeps the expression a
        faithful picture of the query. The two-character case is served by the
        LIKE fallback, which does match on two characters.
        """
        groups = _query_groups(raw_query)
        if not groups:
            return None
        # Escape embedded double-quotes (FTS5 uses "" inside quoted phrases)
        clauses = []
        for group in groups:
            alternatives = " OR ".join(
                f'"{w.replace(chr(34), chr(34)*2)}"' for w in group.windows
            )
            clauses.append(f"({alternatives})")
        return ' AND '.join(clauses)

    def _search_fts5_trigram(
        self,
        query: str,
        user_id: Optional[str],
        scopes: List[str],
        limit: int,
        identities: Optional[List[str]] = None,
        owner_tokens: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """Trigram FTS5 search — handles CJK and mixed queries with BM25 ranking."""
        trigram_query = self._build_trigram_query(query)
        if not trigram_query:
            return []

        scope_placeholders = ','.join('?' * len(scopes))
        vis_sql, vis_params = visibility_clause(
            user_id, "chunks.scope", "chunks.user_id",
            "chunks.element_released", "chunks.file_sealed",
            identities=identities,
            owner_token_list=owner_tokens,
            sealed_owner_col="chunks.sealed_owner")
        sql = f"""
            SELECT chunks.*, bm25(chunks_fts_trigram) as rank
            FROM chunks_fts_trigram
            JOIN chunks ON chunks.rowid = chunks_fts_trigram.rowid
            WHERE chunks_fts_trigram MATCH ?
            AND chunks.scope IN ({scope_placeholders})
            AND {vis_sql}
            ORDER BY rank
            LIMIT ?
        """
        params = [trigram_query] + list(scopes) + vis_params + [limit]

        try:
            rows = self.conn.execute(sql, params).fetchall()
            results = []
            groups = _query_groups(query)
            for row in rows:
                score = self._bm25_rank_to_score(row['rank'])
                # The same discount the LIKE path applies: a chunk that
                # contains the query string verbatim matched it as an example
                # of itself, not as subject matter. Applied here so the two
                # keyword paths agree on the order.
                text = row['text'] or ''
                factor = _verbatim_mention_factor(groups, text)
                results.append(SearchResult(
                    path=row['path'],
                    start_line=row['start_line'],
                    end_line=row['end_line'],
                    score=score * factor,
                    snippet=self._snippet_around(text, query, 500),
                    source=row['source'],
                    user_id=row['user_id']
                ))
            results.sort(key=lambda r: r.score, reverse=True)
            return results
        except sqlite3.Error as e:
            # P0-17 B-2: same rule as the FTS5 path — a structural failure
            # raises instead of masquerading as "no matches".
            from common.log import logger
            logger.error(
                "[MemoryStorage] _search_fts5_trigram failed (structural error, "
                "refusing to report an empty result): %s", e, exc_info=True)
            raise MemorySchemaError(
                f"[MemoryStorage] trigram FTS5 search failed on "
                f"{self.db_path}: {e}"
            ) from e

    @staticmethod
    def _build_fts_query(raw_query: str) -> Optional[str]:
        """
        Build FTS5 query from raw text
        
        Works best for English and word-based languages.
        For CJK characters, LIKE search will be used as fallback.
        """
        # SQLite's unicode61 tokenizer supports words outside the ASCII alphabet.
        tokens = re.findall(r'\w+', raw_query)
        if not tokens:
            return None
        
        # Quote tokens for exact matching
        quoted = [f'"{t}"' for t in tokens]
        # Use OR for more flexible matching
        return ' OR '.join(quoted)
    
    @staticmethod
    def _bm25_rank_to_score(rank: float) -> float:
        """Convert SQLite BM25 rank to a [0, 1) relevance score.

        SQLite's bm25() returns a non-positive float (0 or negative).
        More negative = more relevant.  max(0, rank) would clip every
        negative value to 0, making every score 1/(1+0) = 1.0 and
        destroying all ranking information.

        abs(rank) / (1 + abs(rank)) maps the absolute relevance magnitude
        to [0, 1): larger |rank| (stronger match) → score closer to 1.
        """
        if rank is None:
            return 0.0
        # Add a floor of 0.3 so any FTS5 match always exceeds typical
        # min_score thresholds (default 0.1).  Small-corpus ranks close to
        # 0 would otherwise produce score≈0 and be filtered out downstream.
        return 0.3 + 0.69 * (abs(rank) / (1.0 + abs(rank)))
    
    @staticmethod
    def _truncate_text(text: str, max_chars: int) -> str:
        """Truncate text to max characters"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."
    
    @staticmethod
    def compute_hash(content: str) -> str:
        """Compute SHA256 hash of content"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
