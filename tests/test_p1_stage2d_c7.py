"""P1-阶段二-D · C-7 反向用例（绝对路径判定 + 归一化单一真源）。

C-7 的形状：``path_escape_reason`` 曾把 ``lstrip("/")`` 放在绝对路径判定
**之前**，于是 ``startswith("/")`` 永远为假 —— POSIX 绝对路径这条契约形同
虚设（``/etc/passwd`` 被放行）；同时调用方 ``read_content_fresh`` 自行
``lstrip`` 了一次，把 ``/etc/passwd`` 改写成 ``etc/passwd``，落回工作区内。

**重要：本文件的判据一律经模块属性调用（``paths.path_escape_reason(...)``），
不直接 import 函数名。** 否则 monkeypatch 换掉模块属性时，判据仍在跑旧
函数引用，破坏验证就成了自说自话 —— 判据证明不了任何事。

每条用例钉死一个点：
  * ``/etc/passwd`` 必须被判为 "absolute path"（不被放行、不被重定向）。
  * Windows 盘符同样被拒。
  * 调用方不得自行归一化（单一真源）。
  * 工作区内的合法相对路径仍然服务（不许把好路径也堵了）。
  * 破坏验证：把绝对路径判定摘掉，本文件的判据必须 FAIL。
"""
import inspect
from pathlib import Path

import pytest

from agent.memory import paths


# ---- 判据 1+2: 绝对路径必须被拒，且 reason 必须是 "absolute path" ----

def test_c7_posix_absolute_path_is_rejected():
    """C-7 判据 1+2: `/etc/passwd` → 非 None，且 reason == 'absolute path'."""
    root = Path("C:/workspace-root")
    reason = paths.path_escape_reason("/etc/passwd", root)
    assert reason is not None, "C-7: a POSIX absolute path was let through"
    assert reason == "absolute path", \
        f"C-7: expected 'absolute path', got {reason!r}"


def test_c7_windows_drive_absolute_path_is_rejected():
    """C-7: the Windows drive form is rejected too."""
    reason = paths.path_escape_reason("C:/Windows/win.ini",
                                      Path("C:/workspace-root"))
    assert reason == "absolute path"


def test_c7_safe_join_does_not_redirect_an_absolute_path():
    """C-7: safe_join must REFUSE, never redirect into the workspace root."""
    root = Path("C:/workspace-root")
    path, why = paths.safe_join(root, "/etc/passwd")
    assert path is None, \
        f"C-7: safe_join returned {path} — it redirected instead of refusing"
    assert why == "absolute path"


def test_c7_leading_slash_is_not_stripped_before_judgement():
    """C-7: the judgement sees the RAW input (the lstrip came too early)."""
    # If the lstrip happens before the test, this returns None (allowed).
    assert paths.path_escape_reason("/etc/passwd", Path("C:/root")) is not None
    # A safe-looking path with a leading '/' is still absolute — the rule
    # is about the spelling, not about where it would land.
    assert paths.path_escape_reason("/memory/a.md", Path("C:/root")) \
        == "absolute path"


# ---- 判据 3: 调用方不得自行归一化 ----

def test_c7_content_endpoint_does_not_normalize_on_its_own():
    """C-7: read_content_fresh must hand the RAW path to safe_join."""
    from agent.memory import manager as manager_mod
    src = inspect.getsource(manager_mod.MemoryManager.read_content_fresh)
    assert "lstrip" not in src, \
        "C-7: read_content_fresh still strips a leading '/' itself — that " \
        "is what turned '/etc/passwd' into 'etc/passwd'"
    assert "safe_join(self.config.get_workspace(), relpath)" in src, \
        "C-7: read_content_fresh must pass the raw relpath to safe_join"


def test_c7_paths_is_the_only_place_that_normalizes():
    """C-7: no second normalizer outside paths.py."""
    import agent.memory
    root = Path(agent.memory.__file__).parent
    offenders = []
    for mod in root.glob("*.py"):
        if mod.name == "paths.py":
            continue
        text = mod.read_text(encoding="utf-8")
        if '.replace("\\\\", "/").lstrip("/")' in text:
            offenders.append(mod.name)
    assert not offenders, \
        f"C-7: these modules still inline their own normalization: {offenders}"


# ---- 判据 4: 好路径不许被堵 ----

def test_c7_legal_relative_path_inside_the_root_is_allowed(tmp_path):
    """C-7: a legal relative path inside the root still passes."""
    root = tmp_path / "ws"
    (root / "memory" / "markdown").mkdir(parents=True)
    target = root / "memory" / "markdown" / "a.md"
    target.write_text("hello\n", encoding="utf-8")
    assert paths.path_escape_reason("memory/markdown/a.md", root) is None
    joined, why = paths.safe_join(root, "memory/markdown/a.md")
    assert joined is not None and why is None
    assert joined.resolve() == target.resolve()


# ---- 判据 5: 破坏验证（把绝对路径判定摘掉 → 判据必须 FAIL） ----

def _mutant_without_absolute_rule(relpath, root):
    """The same function WITHOUT the absolute-path rule (the C-7 bug)."""
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
    if ".." in norm.split("/"):
        return "contains a '..' segment"
    return None


def test_c7_removing_the_absolute_path_rule_breaks_the_criterion(
        tmp_path, monkeypatch):
    """C-7: if the absolute-path rule is deleted, the criteria MUST fail.

    This is the criterion that proves the criterion is real: a mutation
    that removes the rule has to turn the suite red. If this test still
    passed after the mutation, the criterion was measuring nothing.

    The test drives the criterion through the module attribute, so the
    monkeypatch really does replace the function the criterion calls.
    """
    root = tmp_path / "ws"
    root.mkdir(parents=True)

    # The criterion BEFORE the mutation — it must be green.
    assert paths.path_escape_reason("/etc/passwd", root) == "absolute path"

    # The mutation: the C-7 bug, reintroduced.
    monkeypatch.setattr(paths, "path_escape_reason",
                        _mutant_without_absolute_rule)

    # THE CRITERION, phrased exactly as the acceptance criteria say it
    # ("must be non-None, reason == 'absolute path'"). Under the
    # mutation it MUST go red — that is the proof. A criterion that
    # cannot fail is not a criterion.
    with pytest.raises(AssertionError):
        reason = paths.path_escape_reason("/etc/passwd", root)
        assert reason is not None, (
            "C-7 破坏验证失败：删掉绝对路径判定后，判据仍然放行 —— "
            "说明这条判据没有真正依赖那条规则")
        assert reason == "absolute path", (
            "C-7 破坏验证失败：删掉绝对路径判定后，reason 不是 "
            "'absolute path'")

    # And the mutant really is the C-7 bug: it lets the path through.
    assert paths.path_escape_reason("/etc/passwd", root) is None


def test_c7_the_criterion_goes_red_when_the_rule_is_removed():
    """C-7: the criterion really depends on the rule (static proof).

    The absolute-path branch must be reachable — it is NOT dead code.
    """
    src = inspect.getsource(paths.path_escape_reason)
    assert 'raw.startswith("/")' in src, \
        "C-7: the absolute-path test must read the RAW input (raw), not a " \
        "stripped copy"
    assert paths.path_escape_reason("/etc/passwd", Path("C:/root")) \
        == "absolute path"


# ---- normalize_relpath 的语义不变（存储键归一） ----

def test_c7_normalize_relpath_keeps_the_legacy_key_semantics():
    """C-7: routing the callers through the one normalizer changes no key."""
    norm = paths.normalize_relpath
    assert norm("memory\\a.md") == "memory/a.md"
    assert norm("/memory/a.md") == "memory/a.md"
    assert norm("memory//a.md") == "memory//a.md"
    assert norm("") == ""
    assert norm(None) == ""


# ---- 端点层：绝对路径必须被拒且不返回内容 ----

def _manager(tmp_path):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    ws = tmp_path / "ws"
    (ws / "memory" / "markdown").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    return MemoryManager(config=MemoryConfig(workspace_root=str(ws))), ws


def test_c7_content_endpoint_refuses_absolute_paths(tmp_path):
    """C-7 判据: read_content_fresh('/etc/passwd') → ok=False, 无内容."""
    mgr, _ws = _manager(tmp_path)
    out = mgr.read_content_fresh("/etc/passwd")
    assert out["ok"] is False
    assert "absolute path" in out["reason"]
    assert "content" not in out, "the refusal must not carry any content"


def test_c7_content_endpoint_refuses_windows_absolute_paths(tmp_path):
    """C-7 判据: read_content_fresh('C:/Windows/win.ini') → ok=False."""
    mgr, _ws = _manager(tmp_path)
    out = mgr.read_content_fresh("C:/Windows/win.ini")
    assert out["ok"] is False
    assert "absolute path" in out["reason"]
    assert "content" not in out


def test_c7_content_endpoint_still_serves_a_legal_path(tmp_path):
    """C-7 判据 5: 好路径不许被堵 —— 工作区内的合法相对路径仍正常返回."""
    mgr, ws = _manager(tmp_path)
    rel = "memory/markdown/a.md"
    (ws / rel).write_text("hello\n", encoding="utf-8")
    import time
    mgr.storage.update_file_metadata(
        path=rel, source="memory", file_hash="h",
        mtime=int(time.time()), size=6)
    out = mgr.read_content_fresh(rel)
    assert out["ok"] is True, f"a legal path was refused: {out}"
    assert out["content"] == "hello\n"
