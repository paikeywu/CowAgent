"""Loud reporting for read exits that fail CLOSED (P0-16 L-4).

P0-13 L-3 made the retrieval path fail closed when the truth layer cannot
be resolved, and the reporting for it hangs off
``manager._refresh_release_projection``. But the READ exits do not go
through that function — they resolve the truth layer themselves. So a
read-only workload (no search at all) failed closed **silently**: the
content was correctly not served, but nothing was logged, no marker was
left, and the inspector never turned red. A privacy control that breaks
shut and says nothing can stay broken forever.

This module is the ONE place that reports a read refusal:

  * an ERROR log line (a log nobody reads is not a signal);
  * an append-only line in ``memory/.audit/read_denied.jsonl`` (the
    history survives the marker being cleared);
  * a marker file ``memory/.audit/read_denied.json`` that the inspector
    turns into a red report.

``clear_read_denied`` removes the marker once the truth layer resolves
again — the degraded state is over, and the jsonl keeps the history.

The reporting NEVER masks the original failure: if the audit trail cannot
be written, the original exception still propagates.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

_CST = timezone(timedelta(hours=8))

#: The marker the inspector looks for.
MARKER_NAME = "read_denied.json"
#: The append-only history of refusals.
LOG_NAME = "read_denied.jsonl"


def _utc8_now() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _audit_dir(workspace_root: Union[str, Path]) -> Path:
    return Path(workspace_root) / "memory" / ".audit"


def read_denied_marker(workspace_root: Union[str, Path]) -> Path:
    """Where the degraded-read marker lives."""
    return _audit_dir(workspace_root) / MARKER_NAME


def read_denied_log(workspace_root: Union[str, Path]) -> Path:
    """Where the append-only refusal history lives."""
    return _audit_dir(workspace_root) / LOG_NAME


def report_read_denied(workspace_root: Union[str, Path], exit_name: str,
                       path: str, error: object) -> None:
    """Record ONE read refusal — loudly, and where a巡检 finds it.

    Never raises: the caller's original failure is the important one, and
    a reporting failure must not replace it.
    """
    from common.log import logger

    message = str(error)
    logger.error(
        "[Memory] READ DENIED (%s): the release state could not be resolved "
        "(%s); refusing to serve %r (fail closed).", exit_name, message, path)
    try:
        audit_dir = _audit_dir(workspace_root)
        audit_dir.mkdir(parents=True, exist_ok=True)
        stamp = _utc8_now()
        entry = {
            "at": stamp,
            "exit": str(exit_name),
            "path": str(path),
            "error": message,
            "effect": "the read exit refused to serve the file (fail closed)",
        }
        with open(read_denied_log(workspace_root), "a", encoding="utf-8",
                  newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # The marker is a small summary — the jsonl above is the history.
        marker = read_denied_marker(workspace_root)
        count = 0
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            count = int(previous.get("count") or 0)
        except (OSError, ValueError, TypeError):
            count = 0
        marker.write_text(json.dumps({
            "at": stamp,
            "count": count + 1,
            "last_exit": str(exit_name),
            "last_path": str(path),
            "last_error": message,
            "effect": "the read exits are failing closed until the truth "
                      "layer resolves again",
        }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
    except Exception as e:      # the report must never mask the failure
        try:
            from common.log import logger as _logger
            _logger.error("[Memory] Cannot write the read-denial report: %s", e)
        except Exception:
            pass


def clear_read_denied(workspace_root: Union[str, Path]) -> None:
    """Remove the degraded marker — the truth layer resolves again.

    The append-only history in ``read_denied.jsonl`` is NOT touched: the
    refusals happened, and the record of them must survive the recovery.
    """
    try:
        read_denied_marker(workspace_root).unlink(missing_ok=True)
    except Exception:
        pass


def read_denied_state(workspace_root: Union[str, Path]) -> Optional[dict]:
    """The marker payload, or None when there is no degraded state."""
    marker = read_denied_marker(workspace_root)
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": "unreadable marker"}
    return payload if isinstance(payload, dict) else {"error": "bad marker"}
