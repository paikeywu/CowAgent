"""路径安全校验 —— **唯一真源**（P1-阶段二-C C-6 · P1-阶段二-D C-7）。

INV-11（``text_inv.check_inv11``）与内容端点（``manager.read_content_fresh``）
必须用**同一个**规则判定「一个相对路径是否安全地留在工作区根内」。
复制两份规则 = 两份规则迟早分叉 = 判据形同虚设。

规则（与 P0-21 / INV-11 一致）：
  1. 禁反斜杠（``\\``）—— 归一后的分隔符只能是 ``/``。
  2. 禁绝对路径（``/`` 开头，或 Windows 盘符 ``C:`` 形态）。
  3. 禁 ``..`` 段。
  4. 解析后（realpath）必须仍在工作区根内（``commonpath``）。

**fail-closed**：无法判定 = 不安全。任何异常路径都判为不安全，绝不放行。

**C-7（P2 · 规则严格性回归 + 死代码）**：本模块曾把 ``lstrip("/")``
放在绝对路径判定**之前**，于是 ``startswith("/")`` 永远为假 —— POSIX 绝对
路径这条契约形同虚设（``/etc/passwd`` 被放行）。判定现在一律基于**原始
输入**，归一化（去掉多余分隔符）只在**判定通过之后**用于拼路径：

  * 判定顺序：反斜杠 → **绝对路径（用 raw）** → ``..`` 段 → realpath 边界。
  * ``normalize_relpath`` 是**唯一**的归一化入口；调用方不得自行
    ``replace`` / ``lstrip``（那正是让 C-6 的规则被绕过的原因）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple


def normalize_relpath(relpath: str) -> str:
    """The ONE normalizer: forward slashes, no leading separators.

    This is a **key** normalizer (the spelling the state layer stores and
    queries by), NOT a security step. Stripping a leading ``/`` here
    would silently redirect an absolute path into the workspace root,
    which is exactly what C-7 was about; the security decision is made by
    :func:`path_escape_reason` on the RAW input, and only a path that
    passed that check is normalized for use.

    The semantics are byte-identical to the ``replace("\\\\", "/").lstrip("/")``
    that the callers used to spell out inline — so routing them through
    here changes no stored key.
    """
    return str(relpath or "").replace("\\", "/").lstrip("/")


def path_escape_reason(relpath: str, root: Path | str) -> Optional[str]:
    """Return None when ``relpath`` is safely inside ``root``; else the reason.

    The reason string is meant to be shown to a human AND to be asserted on
    in tests, so it names the rule that fired ("contains a backslash",
    "absolute path", "contains a '..' segment", "resolves outside the
    workspace root", "cannot be resolved within the root").

    C-7: every check below reads the **RAW** input. The absolute-path
    test used to run against a ``lstrip("/")``-ed copy, which made it
    unreachable; an absolute path is now rejected, not redirected.
    """
    if relpath is None:
        return "empty path"
    raw = str(relpath)
    if not raw.strip():
        return "empty path"
    if "\\" in raw:
        return "contains a backslash"
    # C-7: the absolute-path test must see the RAW input. `raw[1] == ":"`
    # is the Windows drive form (`C:/...`).
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return "absolute path"
    norm = normalize_relpath(raw)
    if not norm:
        return "empty path"
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

    C-7: this function takes the **caller's raw relative path** and owns
    the whole normalization — a caller must NOT pre-strip a leading ``/``,
    because that would turn "reject this absolute path" into "serve this
    file inside the workspace". The absolute-path rule is enforced here,
    on the raw value.
    """
    reason = path_escape_reason(relpath, root)
    if reason is not None:
        return None, reason
    root_res = Path(root).resolve()
    return (root_res / normalize_relpath(relpath)), None
