"""读路径探针（P1 阶段二-B T3）：命中后校验 files.mtime，失配标 stale。

设计 §5.1 防线② + 风控两条裁决：
  * **带缓存**（P1-N2）：海量命中若无缓存会对磁盘反复 stat 自我 DoS。同一
    ``path`` 在 ``TTL`` 内只 stat 一次。
  * **fail 语义分通道**（风控 T3）：检索通道（多候选 + 元数据）可
    fail-visible——陈旧结果照返回但 ``stale`` 计数上升并标在结果上；内容
    服务/下钻/可读性端点必须 **fail-closed**——陈旧即返回 = 拿旧内容冒充
    事实，宁可不答。

探针只读真源文件 mtime，与索引 files.mtime 比对；不改写任何状态（stale
的后台重建由调用方调度，探针自身只负责如实报告）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 缓存生存期（秒）。同一 path 在此窗口内不重复 stat。
DEFAULT_TTL_SECONDS = 30.0


class FreshnessProbe:
    """Per-Manager read-path freshness probe with an mtime cache.

    ``clock`` is injectable so tests can age the cache without sleeping;
    ``statter`` is injectable so tests can simulate a changed mtime without
    touching the filesystem clock.
    """

    def __init__(self, workspace_root: Path | str,
                 *, ttl: float = DEFAULT_TTL_SECONDS,
                 clock: Optional[Callable[[], float]] = None,
                 statter: Optional[Callable[[Path], float]] = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.ttl = ttl
        self._clock = clock or time.monotonic
        self._statter = statter or (lambda p: p.stat().st_mtime)
        # path -> (cached_at_monotonic, real_mtime_or_None)
        self._cache: Dict[str, Tuple[float, Optional[float]]] = {}
        # Monotonic counters surfaced via get_status().
        self.stale_count = 0
        self.checked_count = 0
        self.cache_hits = 0

    def _real_mtime(self, relpath: str) -> Optional[int]:
        """The truth file's current mtime, cached. None when unreadable.

        C-3: the value is TRUNCATED to whole seconds — the same precision
        the index stores. ``files.mtime`` is an INTEGER column written as
        ``int(stat.st_mtime)``, so comparing it against a float
        ``st_mtime`` made every freshly rebuilt file look stale (the float
        is always >= the truncated int, and the comparison was strict
        ``>``). That constant false red drowned the real ones — and masked
        other defects behind it. Same precision on both sides = the
        comparison means what it says.
        """
        now = self._clock()
        cached = self._cache.get(relpath)
        if cached is not None and (now - cached[0]) < self.ttl:
            self.cache_hits += 1
            return cached[1]
        real = self.workspace_root / relpath.replace("\\", "/")
        try:
            mtime = int(self._statter(real))
        except OSError:
            mtime = None
        self._cache[relpath] = (now, mtime)
        return mtime

    def is_stale(self, relpath: str, indexed_mtime: Optional[float]) -> bool:
        """True when the truth file is newer than the index recorded.

        A missing indexed mtime cannot be proven fresh, so it reads stale
        (fail on the safe side for a content endpoint, fail-visible for
        retrieval — the caller decides which). A truth file that no longer
        exists (mtime None) is stale too: the index is serving a ghost.
        """
        self.checked_count += 1
        real = self._real_mtime(relpath)
        # C-3: both sides in whole seconds (the index column is INTEGER).
        indexed = int(indexed_mtime) if indexed_mtime is not None else None
        stale = real is None or indexed is None or real > indexed
        if stale:
            self.stale_count += 1
        return stale

    def status(self) -> Dict[str, Any]:
        return {
            "checked": self.checked_count,
            "stale": self.stale_count,
            "cache_hits": self.cache_hits,
        }
