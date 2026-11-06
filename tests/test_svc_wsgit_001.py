#!/usr/bin/env python
# encoding:utf-8
"""SVC-WSGIT-001 · 工作区根被 git init 毒化 ⇒ 补机制（文案 + 启动前置检查）

**交办**：小种子（SEED）· 2026-11-06 · P0
**交办书**：`~/cow/deliverables/2026-11-06-SVC-WSGIT-001-工作区根被git-init毒化-交办书.md`

**事故**：有人在运行时工作区 `C:\\Users\\User\\cow` 里执行了 `git init`，
把整个工作区变成了 git 工作树。`common/state_dir.py` 的守卫
`assert_not_source_tree` 从工作区根往上找 `.git`，找到就拒绝 ⇒ 工作区根
**和全部 15 个队友的 workspace** 全部被拒 ⇒ Web UI 的项目列表 / 项目选择 /
会话 API / **Agent 回复**全线报「工作区位于源码树内」错误。服务启动后
**2 秒**就报了，但那是 116 MB 日志里的一行 `[ERROR]`，两天后才被用户发现。

**本文件测什么**（两件，都不是「抛了异常」这种空判据）：

* **A1 / A2 / A3** —— 报错文案必须**说人话**：
  - 形态 A（工作区根自己含 `.git`）：文本必须点名 `.git` 这个真凶。
  - 形态 B（工作区在祖先仓库内）：文本必须**同时**给出工作区路径与仓库路径。
  - 两种形态都不得再出现「请把 workspace 指向 %USERPROFILE%\\cow」这种
    「建议等于现状」的无效建议（那正是本次事故里用户做不到的动作）。
* **B1** —— 启动装载路径上必须出现**一眼可见**的汇总告警（含受影响
  Agent 数量与第一个具体路径），而不是「只有一行看不出所以然的 ERROR」。
* **B2** —— 守卫的**判定逻辑**必须逐字未改（只许改文案）。
* **C2** —— 判据自己**有鉴别力**：变异臂把守卫改坏 ⇒ 判据必须转红。

=================  ==============================================  ==========
ID                 变异                                            期望
=================  ==============================================  ==========
MU-W1              形态 A 的文案**不点名** `.git`（退回泛泛的         必须红
                   「源码树」措辞）
MU-W2              形态 B 的文案**不给出仓库路径**（只给工作区）     必须红
MU-W3              文案**重新出现** `%USERPROFILE%\\cow` 这条          必须红
                   无效建议
MU-W4              守卫的**判定条件**被改坏（`.git` 只认目录、       必须红
                   不认文件 / 或只认工作区根不认祖先）
MU-W5              启动前置检查被摘掉（`app.py` 不再调用）          必须红
=================  ==============================================  ==========
"""

import logging
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.registry import AgentProfile, AgentRegistry, set_agent_registry  # noqa: E402
from common import state_dir  # noqa: E402
from common.state_dir import SourceTreeWorkspaceError  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具：两个形态的工作区
#
# 夹具**只**造目录与一个 `.git` 条目，**绝不**执行 `git init`：
# 交办书明令「不许在 C:\\Users\\User\\cow 根目录执行 git init」，
# 而 `git init` 会在源码树里留下运行时产物（硬约束 ③）。`.git` 目录的
# 存在对守卫来说与真仓库无差别 —— 守卫只做 ``(candidate/'.git').exists()``。
# ---------------------------------------------------------------------------


def _make_shape_a(root: Path) -> Path:
    """形态 A：工作区根自己含 ``.git``（= 有人在工作区根跑过 git init）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    return root


def _make_shape_b(root: Path) -> tuple:
    """形态 B：工作区在某个祖先仓库内。

    Returns ``(workspace, repository)``.
    """
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    workspace = repo / "sub" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace, repo


def _message_for(workspace: Path) -> str:
    """The refusal text for a workspace, as the guard actually raises it."""
    with pytest.raises(SourceTreeWorkspaceError) as exc:
        state_dir.assert_not_source_tree(workspace)
    return str(exc.value)


# ---------------------------------------------------------------------------
# A1 · 形态 A 的文案必须点名 .git
# ---------------------------------------------------------------------------


def test_a1_shape_a_names_the_dot_git_culprit(tmp_path):
    """A1：形态 A 的报错文本必须出现 ``.git`` —— 真凶必须被点名。

    旧文案说的是「工作区位于源码树内」，一个用户读到它无法知道该动什么；
    真凶（工作区根里的 ``.git``）完全没出现。
    """
    workspace = _make_shape_a(tmp_path / "workspace")

    message = _message_for(workspace)

    assert ".git" in message, (
        "形态 A 的报错没有点名 .git —— 用户读不到该动什么：\n%s" % message
    )
    # 还必须点名 git init —— 这是事故的真实成因，也是唯一可执行的线索。
    assert "git init" in message, (
        "形态 A 的报错没有点出 git init 这个成因：\n%s" % message
    )


def test_a1_shape_a_names_the_workspace_path(tmp_path):
    """A1 续：形态 A 的文本必须指出**是哪个工作区**坏了。"""
    workspace = _make_shape_a(tmp_path / "workspace")

    message = _message_for(workspace)

    assert str(workspace.resolve()) in message, (
        "形态 A 的报错没有点名工作区路径：\n%s" % message
    )


def test_a1_shape_a_offers_both_executable_fixes(tmp_path):
    """A1 续：形态 A 必须给出**可执行**的修法，且是两种不同的修法。

    「把 git 元数据改名移开」与「把工作区指到别处」—— 两条都能真正改变
    导致拒绝的状态。少一条就是少一条出路。
    """
    workspace = _make_shape_a(tmp_path / "workspace")

    message = _message_for(workspace)

    assert ".ledger-git" in message, (
        "形态 A 没有给出「改名移开 git 元数据」这条修法：\n%s" % message
    )
    assert "core.worktree" in message, (
        "形态 A 的修法没有说明怎么保留历史（core.worktree）：\n%s" % message
    )
    assert "agent_workspace" in message, (
        "形态 A 没有给出「把工作区指到别处」这条修法：\n%s" % message
    )


# ---------------------------------------------------------------------------
# A2 · 形态 B 的文案必须同时含工作区路径与仓库路径
# ---------------------------------------------------------------------------


def test_a2_shape_b_names_both_the_workspace_and_the_repository(tmp_path):
    """A2：形态 B 的报错必须**同时**出现工作区路径与仓库路径。

    只给工作区路径 = 用户不知道该搬去哪里；只给仓库路径 = 用户不知道
    哪个东西坏了。两个都在，才算给出了可执行的修法。
    """
    workspace, repository = _make_shape_b(tmp_path)

    message = _message_for(workspace)

    assert str(workspace.resolve()) in message, (
        "形态 B 的报错没有工作区路径：\n%s" % message
    )
    assert str(repository.resolve()) in message, (
        "形态 B 的报错没有仓库路径：\n%s" % message
    )


def test_a2_shape_b_does_not_claim_the_workspace_is_the_repository(tmp_path):
    """A2 续：形态 B 不得说成「工作区根自己就是仓库」。

    这是两种形态的分水岭：说错形态 ⇒ 用户去改错的东西。
    """
    workspace, _ = _make_shape_b(tmp_path)

    message = _message_for(workspace)

    assert "工作区根目录自己就是一个 git 仓库" not in message, (
        "形态 B 被说成了形态 A —— 用户会照着形态 A 的修法去改：\n%s" % message
    )


# ---------------------------------------------------------------------------
# A3 · 文案不再出现「建议等于现状」的无效建议
# ---------------------------------------------------------------------------

#: The advice that was removed: it told the reader to point the workspace at
#: ``%USERPROFILE%\cow`` — which is where the affected installs ALREADY point
#: it, so for shape A it asked for what they were already doing.
DEAD_ADVICE = [
    re.compile(r"%USERPROFILE%\\cow"),
    re.compile(r"%USERPROFILE%\\\\cow"),
    re.compile(r"例如\s*%USERPROFILE%"),
]


@pytest.mark.parametrize("pattern", DEAD_ADVICE)
def test_a3_the_dead_advice_is_gone_from_the_guard_text(pattern, tmp_path):
    """A3：守卫文案里不得再出现「指向 %USERPROFILE%\\cow」这类无效建议。

    Checked on BOTH shapes: the old text was one string, so a rewrite that
    fixes shape A but leaves shape B saying it would still be wrong.
    """
    shape_a = _make_shape_a(tmp_path / "a")
    shape_b, _ = _make_shape_b(tmp_path / "b")

    for workspace in (shape_a, shape_b):
        message = _message_for(workspace)
        assert not pattern.search(message), (
            "文案里仍有一条做不到的建议（建议等于现状）：\n%s" % message
        )


def test_a3_the_repository_root_message_no_longer_says_work_workspace_elsewhere(tmp_path):
    """A3 续：形态 A 的文本里不得再出现「把 workspace 指向 …cow」。

    A targeted check on the exact old sentence, because the generic patterns
    above only cover the ``%USERPROFILE%`` spelling.
    """
    workspace = _make_shape_a(tmp_path / "workspace")

    message = _message_for(workspace)

    assert "而不是源码目录" not in message, (
        "旧文案（把 workspace 指向别处而不是源码目录）仍在：\n%s" % message
    )


# ---------------------------------------------------------------------------
# B1 · 启动前置检查必须给出「一眼可见」的汇总告警
# ---------------------------------------------------------------------------


class _LogCapture(logging.Handler):
    """Collects the records the logger emits during a test."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def errors(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


def _registry_with(workspaces: dict) -> AgentRegistry:
    """A real registry (not a mock) whose Agents have these workspaces."""
    profiles = [
        AgentProfile(id=agent_id, name=agent_id, workspace=str(workspace))
        for agent_id, workspace in workspaces.items()
    ]
    registry = AgentRegistry(profiles, next(iter(workspaces)))
    set_agent_registry(registry)
    return registry


def _run_startup_check():
    """Call the startup pre-check and return the captured log records."""
    import app
    from common.log import logger

    capture = _LogCapture()
    logger.addHandler(capture)
    try:
        app._warn_workspace_inside_git_tree()
    finally:
        logger.removeHandler(capture)
    return capture


def test_b1_startup_reports_the_count_and_the_first_path(tmp_path):
    """B1：启动告警必须含**受影响 Agent 数量**与**第一个具体路径**。

    这是本次交办的核心：事故里失败在启动后 2 秒就已经写进日志，但它是
    116 MB 里的一行 ``[ERROR]``，没人看见。告警必须自带读数。

    分母是**被探测的 Agent 总数**，不是受影响的数量 —— 「2 of 2」在只有
    2 个 Agent 全坏时与「2 个坏 / 3 个 Agent」时读数相同，读者无法分辨。
    这条断言因此要求分母是总数。
    """
    broken_a = _make_shape_a(tmp_path / "broken-a")
    broken_b = _make_shape_a(tmp_path / "broken-b")
    healthy = tmp_path / "healthy"
    healthy.mkdir()

    _registry_with({
        "aaa-broken": broken_a,
        "bbb-broken": broken_b,
        "ccc-healthy": healthy,
    })
    try:
        capture = _run_startup_check()
    finally:
        set_agent_registry(None)

    errors = capture.errors
    assert errors, (
        "启动前置检查没有发出任何 ERROR 级告警 —— "
        "问题又会被埋进日志里没人看见：\n%s" % capture.text
    )

    text = capture.text
    assert "2 of 3" in text, (
        "告警没有写出「受影响 2 个 / 共 3 个」这个可核对的读数：\n%s" % text
    )
    assert str(broken_a.resolve()) in text, (
        "告警没有给出第一个具体路径：\n%s" % text
    )


def test_b1_startup_names_every_affected_agent(tmp_path):
    """B1 续：每一个受影响的 Agent 都要被点名，不能只报第一个。"""
    broken_a = _make_shape_a(tmp_path / "broken-a")
    broken_b = _make_shape_a(tmp_path / "broken-b")

    _registry_with({"aaa-broken": broken_a, "bbb-broken": broken_b})
    try:
        capture = _run_startup_check()
    finally:
        set_agent_registry(None)

    text = capture.text
    assert "aaa-broken" in text, ("第一个 Agent 没被点名：\n%s" % text)
    assert "bbb-broken" in text, ("第二个 Agent 没被点名：\n%s" % text)


def test_b1_a_clean_workspace_says_nothing(tmp_path):
    """B1 反向用例：工作区干净时**不许**喊狼来了。

    没有这条，一个「永远告警」的检查也会让 B1 变绿。
    """
    healthy = tmp_path / "healthy"
    healthy.mkdir()

    _registry_with({"ok-agent": healthy})
    try:
        capture = _run_startup_check()
    finally:
        set_agent_registry(None)

    assert not capture.errors, (
        "工作区干净却报了错 —— 这是误报：\n%s" % capture.text
    )


def test_b1_the_check_is_wired_into_the_startup_path():
    """B1 续：前置检查必须真的在启动路径上被调用。

    断言的是「``run()`` 里调用了它」，不是「这个函数存在」——
    一个定义了却没接线的检查，在事故里等于没有。
    """
    source = (REPO / "app.py").read_text(encoding="utf-8")

    assert "_warn_workspace_inside_git_tree()" in source, (
        "app.py 的启动路径上没有调用启动前置检查"
    )
    # 必须落在 run() 里（而不是只定义在模块里）。
    run_body = source.split("def run():", 1)[1]
    assert "_warn_workspace_inside_git_tree()" in run_body, (
        "启动前置检查没有接进 run()"
    )


# ---------------------------------------------------------------------------
# B2 · 守卫的判定逻辑逐字未改
# ---------------------------------------------------------------------------

#: The decision logic, as it was BEFORE this change (commit 981574f9 shipped
#: it; the SVC-WSGIT-001 change was text-only). Compared against the live
#: source so a future edit that weakens the guard turns this red.
GUARD_DECISION_MARKERS = (
    "def _source_tree_root(path: Path) -> Optional[Path]:",
    "if (candidate / \".git\").exists():",
    "for candidate in [current] + list(current.parents):",
    "if tree is None:",
    "raise SourceTreeWorkspaceError(",
)


def test_b2_the_guard_decision_logic_is_unchanged():
    """B2：守卫的**判定条件**必须逐字未改。

    Read from the live source (not from a copy) so the check tracks the file.
    The markers below are the decision path: the ``.git`` walk (a directory
    OR a file — a worktree/submodule ``.git`` FILE counts too), the
    "no tree ⇒ return" and the raise. If any of them is rewritten, this
    goes red.
    """
    source = (REPO / "common" / "state_dir.py").read_text(encoding="utf-8")

    for marker in GUARD_DECISION_MARKERS:
        assert marker in source, (
            "守卫的判定逻辑被改了（少了 %r）—— 本次只许改文案" % marker
        )

    # The guard must still walk UP from the workspace, and a ``.git`` FILE
    # must still count (a worktree or submodule). Weakening either of those
    # is exactly the "削弱守卫" the交办 forbids.
    assert "(candidate / \".git\").exists()" in source, (
        "``.git`` 的存在性判定被改了 —— 目录/文件两种形态都必须认"
    )
    assert "list(current.parents)" in source, (
        "守卫不再向上找祖先仓库 —— 形态 B 的防线没了"
    )


def test_b2_the_guard_still_refuses_a_dot_git_file_worktree(tmp_path):
    """B2 续（行为面）：``.git`` 是**文件**时仍必须被拒。

    只认目录会把 worktree / submodule 漏掉 —— 那正是最容易在开发中被
    当成工作区的地方。这条是行为断言，不依赖源码字面量。
    """
    tree = tmp_path / "wt"
    tree.mkdir()
    (tree / ".git").write_text(
        "gitdir: C:/somewhere/else/.git/worktrees/wt\n", encoding="utf-8"
    )

    with pytest.raises(SourceTreeWorkspaceError):
        state_dir.assert_not_source_tree(tree)


# ---------------------------------------------------------------------------
# C2 · 判据有鉴别力：变异臂
#
# 这些不是「产品测试」，而是**判据自己的自证**：把被测的东西改坏，
# 上面的判据必须转红。判据不转红 ⇒ 它没在测那件事。
# ---------------------------------------------------------------------------


def test_c2_mutant_w1_shape_a_without_the_dot_git_culprit():
    """MU-W1：形态 A 的文案不点名 ``.git`` ⇒ A1 判据必须红。

    The mutant is applied to the MESSAGE FUNCTION, in memory — the real
    source is not touched, so no other test can be affected by the replay.
    """
    original = state_dir._workspace_refusal_message

    def mutant(root, tree):
        text = original(root, tree)
        return text.replace(".git", "版本库元数据")

    state_dir._workspace_refusal_message = mutant
    try:
        workspace = _make_shape_a(Path(os.environ["MUTANT_TMP"]) / "ws")
        message = _message_for(workspace)
    finally:
        state_dir._workspace_refusal_message = original

    assert ".git" not in message, "变异没有生效 —— 夹具或接线有问题"
    # The criterion must now be violated:
    with pytest.raises(AssertionError):
        assert ".git" in message


def test_c2_mutant_w2_shape_b_without_the_repository_path():
    """MU-W2：形态 B 的文案不给出仓库路径 ⇒ A2 判据必须红。"""
    original = state_dir._workspace_refusal_message

    def mutant(root, tree):
        text = original(root, tree)
        resolved = str(Path(os.path.realpath(str(tree))))
        return text.replace(resolved, "<仓库路径>")

    state_dir._workspace_refusal_message = mutant
    try:
        workspace, repository = _make_shape_b(
            Path(os.environ["MUTANT_TMP"]) / "shape-b"
        )
        message = _message_for(workspace)
    finally:
        state_dir._workspace_refusal_message = original

    assert str(repository.resolve()) not in message, "变异没有生效"
    with pytest.raises(AssertionError):
        assert str(repository.resolve()) in message


def test_c2_mutant_w3_the_dead_advice_comes_back():
    """MU-W3：无效建议回来 ⇒ A3 判据必须红。"""
    original = state_dir._workspace_refusal_message

    def mutant(root, tree):
        return original(root, tree) + (
            "\n  请把 agent_workspace / workspace 指向一个工作区目录"
            "（例如 %USERPROFILE%\\cow），而不是源码目录。"
        )

    state_dir._workspace_refusal_message = mutant
    try:
        workspace = _make_shape_a(Path(os.environ["MUTANT_TMP"]) / "ws")
        message = _message_for(workspace)
    finally:
        state_dir._workspace_refusal_message = original

    assert re.search(r"%USERPROFILE%\\cow", message), "变异没有生效"
    with pytest.raises(AssertionError):
        assert not re.search(r"%USERPROFILE%\\cow", message)


def test_c2_mutant_w4_the_guard_decision_is_weakened():
    """MU-W4：守卫的**判定条件**被改坏 ⇒ 判据必须红。

    This is the arm that matters most: it proves the criteria are not
    merely checking the new wording — they would also notice if someone
    weakened the guard itself (the one thing the交办 forbids).

    The weakened guard is installed on the MODULE ATTRIBUTE, which is the
    only way the swap takes effect: ``assert_not_source_tree`` looks the
    function up on the module at call time.
    """
    original = state_dir._source_tree_root

    def mutant(path):
        """Only the workspace root counts — ancestors are no longer walked."""
        current = Path(os.path.realpath(str(path)))
        if (current / ".git").exists():
            return current
        return None

    workspace, _ = _make_shape_b(Path(os.environ["MUTANT_TMP"]) / "shape-b")

    state_dir._source_tree_root = mutant
    try:
        # Proof that the mutant really weakened the guard (asserted INSIDE
        # the mutated window — asserting after the restore would test the
        # restored function and prove nothing):
        assert state_dir._source_tree_root(workspace) is None, (
            "变异没有生效 —— 守卫仍在向上找祖先仓库"
        )
        # The weakened guard no longer refuses shape B ⇒ no exception ⇒ the
        # criterion that expects a refusal is violated.
        state_dir.assert_not_source_tree(workspace)
    finally:
        state_dir._source_tree_root = original

    # And the criterion would have gone red on it:
    with pytest.raises(SourceTreeWorkspaceError):
        state_dir.assert_not_source_tree(workspace)


def test_c2_mutant_w5_the_startup_check_is_unwired():
    """MU-W5：启动前置检查被摘掉 ⇒ B1 的接线判据必须红。"""
    source = (REPO / "app.py").read_text(encoding="utf-8")
    mutant = source.replace(
        "        _warn_workspace_inside_git_tree()\n", ""
    )

    assert mutant != source, "变异没有生效"
    run_body = mutant.split("def run():", 1)[1]
    with pytest.raises(AssertionError):
        assert "_warn_workspace_inside_git_tree()" in run_body


# ---------------------------------------------------------------------------
# 夹具根目录（变异臂用）
# ---------------------------------------------------------------------------

import tempfile  # noqa: E402

os.environ.setdefault(
    "MUTANT_TMP", tempfile.mkdtemp(prefix="wsgit-mutant-")
)
