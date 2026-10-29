"""单一变更事件记录点（P1 阶段二-B T2，SEED 裁决：不推翻 P0 批量重建拍板）。

P0-10-27 拍板（shelf.py 模块头, "do not revisit"）：书架是投影，**批量全量
重建、无增量、无守护、无写时触发**——半更新危险。设计 §5.4 真正要防的是
「两张投影各自为政、检索是新内容而书架是旧内容」，**不要求写时同步重建**。

所以「单一信号源」落地为单一变更**记录**点：facade 写入 /
import_human_edit / 重建器把每一次真源文件变更 append 到同一个
``memory/.audit/file_changes.jsonl`` 队列。两张投影（索引、书架）读同一份
变更记录，不会各自为政；shelf 侧用「shelf 滞后判据」（真源 mtime 晚于 shelf
generated_at → 报红，提示该跑 batch 重建）兜住时序。

APPEND-ONLY。本模块绝不改写、绝不删除历史事件；消费（batch 重建）由既有
批量流程触发，这里只负责如实记录。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_CST = timezone(timedelta(hours=8))

#: 变更来源（谁触发的）。facade 写入 / 人类编辑导入 / 重建器重建。
#:
#: C-2: the set had only three members, and the FOURTH path — the regular
#: facade entries (``write_event`` / ``write_letter`` / ``write_entity`` /
#: ``revise``) and the release machinery — wrote no events at all. A missing
#: source is not a missing log line: it is the shelf-lag invariant sitting
#: at NOTRUN (silently green) while the truth layer moved.
SOURCES = frozenset({
    "facade-write",
    "import-human-edit",
    "rebuilder",
    # C-2: the element/release paths.
    "facade-element",
    "facade-release",
})


def _now() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def change_log_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root) / "memory" / ".audit" / "file_changes.jsonl"


def record_change(workspace_root: Path | str, relpath: str, source: str,
                  *, actor: Optional[str] = None, mtime: Optional[str] = None,
                  extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append one file-change event. Returns the recorded event.

    ``relpath`` is normalized to forward-slashes so every consumer reads the
    same spelling. ``source`` must be one of :data:`SOURCES` — a caller that
    cannot name its source is itself an anomaly worth surfacing.
    """
    if source not in SOURCES:
        raise ValueError(
            f"unknown change source {source!r}; must be one of {sorted(SOURCES)}")
    root = Path(workspace_root)
    norm = relpath.replace("\\", "/").lstrip("/")
    if mtime is None:
        real = root / norm
        try:
            mtime = datetime.fromtimestamp(
                real.stat().st_mtime, _CST).isoformat(timespec="seconds")
        except OSError:
            mtime = None  # file may be a released/deleted path; record anyway
    event: Dict[str, Any] = {
        "path": norm, "source": source, "actor": actor,
        "mtime": mtime, "at": _now(),
    }
    if extra:
        event.update(extra)
    log = change_log_path(root)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return event


def read_changes(workspace_root: Path | str) -> List[Dict[str, Any]]:
    """Every recorded change event, oldest first. Empty when none."""
    log = change_log_path(workspace_root)
    if not log.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with open(log, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                # a torn tail line is an inspection finding, not a crash
                continue
    return out


def latest_change_mtime(workspace_root: Path | str) -> Optional[str]:
    """The newest recorded change time across all change events, or None.

    The shelf-lag invariant compares a shelf page's generated_at against the
    newest truth-layer change — if any recorded change is newer than the page,
    the page is stale and the inspector must report it.

    P1 阶段二-B T2-B3: an event whose file does not exist (a released/deleted
    path) records ``mtime=None``. Filtering those out made the invariant
    blind to exactly the changes that delete a memory — so the comparison
    MISSED them. Such an event now falls back to its own ``at`` (the record
    time), which always exists. The fallback is PER EVENT: one event with a
    missing mtime must not push the others out of the comparison.
    """
    stamps: List[str] = []
    for e in read_changes(workspace_root):
        stamp = e.get("mtime") or e.get("at")
        if stamp:
            stamps.append(str(stamp))
    return max(stamps) if stamps else None
