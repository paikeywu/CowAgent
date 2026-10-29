"""路径安全校验 —— **唯一真源**（P1-阶段二-C C-6）。

INV-11（``text_inv.check_inv11``）与内容端点（``manager.read_content_fresh``）
必须用**同一个**规则判定「一个相对路径是否安全地留在工作区根内」。
复制两份规则 = 两份规则迟早分叉 = 判据形同虚设。

规则（与 P0-21 / INV-11 一致）：
  1. 禁反斜杠（``\\``）—— 归一后的分隔符只能是 ``/``。
  2. 禁绝对路径（``/`` 开头，或 Windows 盘符 ``C:`` 形态）。
  3. 禁 ``..`` 段。
  4. 解析后（realpath）必须仍在工作区根内（``commonpath``）。

**fail-closed**：无法判定 = 不安全。任何异常路径都判为不安全，绝不放行。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple


def path_escape_reason(relpath: str, root: Path | str) -> Optional[str]:
    """Return None when ``relpath`` is safely inside ``root``; else the reason.

    The reason string is meant to be shown to a human AND to be asserted on
    in tests, so it names the rule that fired ("contains a backslash",
    "absolute path", "contains a '..' segment", "resolves outside the
    workspace root", "cannot be resolved within the root").
    """
    if relpath is None:
        return "empty path"
    raw = str(relpath)
    if not raw.strip():
        return "empty path"
    if "\\" in raw:
        return "contains a backslash"
    norm = raw.lstrip("/")
    if not norm:
        return "empty path"
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        return "absolute path"
    if ".." in norm.split("/"):
        return "contains a '..' segment"
    try:
        root_res = Path(root).resolve()
        real = (root_res / norm).resolve()
        if os.path.commonpath([str(root_res), str(real)]) != str(root_res):
            return "resolves outside the workspace root"
    except (OSError, ValueError):
        return "cannot be resolved within the root"
    return None


def safe_join(root: Path | str, relpath: str) -> Tuple[Optional[Path], Optional[str]]:
    """Join ``relpath`` onto ``root`` after checking it stays inside.

    Returns ``(path, None)`` when safe, ``(None, reason)`` when not.
    """
    reason = path_escape_reason(relpath, root)
    if reason is not None:
        return None, reason
    root_res = Path(root).resolve()
    return (root_res / str(relpath).lstrip("/")), None
