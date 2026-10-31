# encoding:utf-8
"""交办 #3 · 判据 —— 请求路径的 connect 预算必须来自那个命名常量。

**这个判据守的是什么。**

2026-10-31 修掉了 Claude 流式请求的跨线程死锁，connect 预算被改成了
显式的 ``(connect, read)`` 元组，并且把 connect 那一半命名为
:data:`CONNECT_TIMEOUT`。已有的判据（``test_claude_stream_connect_timeout.py``）
守住了「connect 半的值是 180」「两条路径一致」「元组不能塌成单值」。

但它有一个盲区：**helper 函数里内联的字面量扫不到。**

已有的那条「不许硬编码 180」的检查解析的是 ``ast.Call`` 节点里
``timeout=`` 关键字的字面量。于是：

    CONNECT_TIMEOUT = 180

    def _stream_timeout():
        return (180, _stream_read_timeout())   # <-- 内联字面量

这里 ``return (180, ...)`` **不是 Call 节点**，扫不到。改完之后：

* ``CONNECT_TIMEOUT`` 与实际使用值**脱钩**；
* 将来有人把 ``CONNECT_TIMEOUT`` 改成 90，流式路径**仍然是 180**；
* 常量变成谎言，两条路径悄悄漂移。

已有的判据 M-5（改常量）能抓、M-6（helper 内联）抓不到，说明这条规则
**只守住了改常量的一侧，没守住内联的一侧**。

本文件补上那一侧。

**判据读的是「真正传给 requests.post 的那个值」**，不是自己重算一遍。
做法是 **拦截 ``requests.post``**，把它的 ``timeout`` 实参原样抓出来，
再回答一个关于**来源**的问题：这个 connect 预算**是不是那个命名常量本身**。

来源怎么判定？靠**身份**，不靠数值：

* 把 ``claude_api_bot.CONNECT_TIMEOUT`` 换成一个**哨兵对象**（一个
  独特的 float 子类，值故意不等于 180）；
* 重新走一遍请求路径，抓 ``requests.post`` 收到的 ``timeout``；
* 如果抓到的 connect 半**就是那个哨兵对象**（``is`` 相等），说明请求路径
  真的从那个常量取值 —— 不管常量此刻是 180、是 90，还是一个怪东西；
* 如果抓到的是别的数（180），说明有人绕过了常量，内联了字面量 ⇒ **转红**。

这样一来：

* 判据**不重算**期望值（期望值就是「哨兵本人」），
* 判据**不依赖数值 180**（把常量改成 60 也照样抓得住内联），
* 判据**不依赖源码长相**（helper 搬家、改名都不影响），
* 判据**能区分「常量改了」和「常量被绕过」**：前者哨兵照样出现（绿），
  后者哨兵消失（红）。

**BREAKER**：把 ``_stream_timeout`` 的 ``(CONNECT_TIMEOUT, ...)`` 改成
``(180, ...)`` ⇒ connect 半不再是哨兵 ⇒ **转红**。

**另一条独立判据**：``ast`` 层面扫「请求路径里出现的数字字面量 180」。
它与上面那条同源但独立：上面那条看**运行时**（值从哪来），这条看
**源码结构**（值写在哪）。两条都绿才叫通过。
"""

import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from models.claudeapi import claude_api_bot  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class _CapturedPost:
    """替换 ``requests.post``，把 ``timeout`` 实参原样记录下来。"""

    def __init__(self, monkeypatch):
        self.calls = []
        self._real_post = claude_api_bot.requests.post
        monkeypatch.setattr(claude_api_bot.requests, "post", self)

    def __call__(self, url, **kwargs):
        self.calls.append(kwargs)

        class _Resp:
            status_code = 200
            text = ""

            def iter_lines(self, *a, **k):
                return iter(())

            def json(self):
                return {"content": [{"type": "text", "text": ""}],
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                        "stop_reason": "end_turn"}

            def close(self):
                pass

        return _Resp()


class _Sentinel(float):
    """A unique float that is NOT 180.

    Used as the value of ``CONNECT_TIMEOUT`` for the duration of one test.
    Identity (``is``) is what the criterion compares, so the sentinel is
    recognisable no matter what its numeric value is -- the criterion never
    has to hard-code 180 to be meaningful.
    """

    __slots__ = ()

    def __repr__(self):
        return "<SENTINEL CONNECT_TIMEOUT>"


def _bot(monkeypatch, model="claude-opus-4-1"):
    bot = claude_api_bot.ClaudeAPIBot.__new__(claude_api_bot.ClaudeAPIBot)
    monkeypatch.setitem(claude_api_bot.conf(), "model", model)
    monkeypatch.setitem(claude_api_bot.conf(), "character_desc", "")
    monkeypatch.setitem(claude_api_bot.conf(), "claude_api_key", "test")
    monkeypatch.setitem(claude_api_bot.conf(), "claude_api_base", "http://127.0.0.1:1/v1")
    monkeypatch.setitem(claude_api_bot.conf(), "proxy", "")
    return bot


def _stream_timeouts(monkeypatch):
    """The ``timeout`` values the STREAMING path handed to ``requests.post``."""
    cap = _CapturedPost(monkeypatch)
    bot = _bot(monkeypatch)
    list(bot._handle_stream_response({
        "model": "m",
        "messages": [],
        "max_tokens": 1024,
    }))
    assert cap.calls, "the streaming path never called requests.post"
    return [c.get("timeout") for c in cap.calls]


def _sync_timeouts(monkeypatch):
    """The ``timeout`` values the NON-STREAMING path handed to ``requests.post``."""
    cap = _CapturedPost(monkeypatch)
    bot = _bot(monkeypatch)
    bot._handle_sync_response({
        "model": "m",
        "messages": [],
        "max_tokens": 1024,
    })
    assert cap.calls, "the sync path never called requests.post"
    return [c.get("timeout") for c in cap.calls]


def _connect_of(timeout_value):
    """The connect half of a timeout value, or None when it is not a tuple."""
    if isinstance(timeout_value, tuple) and len(timeout_value) == 2:
        return timeout_value[0]
    return None


# --------------------------------------------------------------------------
# C3 — the connect budget must COME FROM the named constant
# --------------------------------------------------------------------------

def test_c3_the_streaming_connect_budget_comes_from_the_named_constant(monkeypatch):
    """C3: the streaming path's connect half must BE the constant.

    The constant is replaced by a sentinel before the request is made.  If
    the request path reads the constant, the sentinel is what reaches
    ``requests.post``.  If somebody hard-codes the number instead, the
    sentinel never appears -- and that is exactly the drift this criterion
    exists to catch.

    Note what this does NOT do: it never compares against 180.  Changing
    ``CONNECT_TIMEOUT`` to 60 or 90 keeps this green (the constant is still
    the source), while inlining the literal makes it red.
    """
    sentinel = _Sentinel(4242.0)
    monkeypatch.setattr(claude_api_bot, "CONNECT_TIMEOUT", sentinel)

    timeouts = _stream_timeouts(monkeypatch)

    assert timeouts, "the streaming path never called requests.post"
    for t in timeouts:
        connect = _connect_of(t)
        assert connect is not None, (
            "C3: the streaming path must pass a (connect, read) tuple, got "
            f"{t!r}")
        assert connect is sentinel, (
            "C3: the streaming path's CONNECT budget must come from the "
            f"named constant CONNECT_TIMEOUT; got {connect!r} instead of the "
            f"sentinel {sentinel!r} (full timeout {t!r}). A literal inlined "
            "in the helper decouples the value from the constant, so the "
            "constant becomes a lie and the two paths drift apart")


def test_c3_the_sync_connect_budget_comes_from_the_named_constant(monkeypatch):
    """C3 (second form): the same rule for the non-streaming path.

    The sync path states its budget as ``(CONNECT_TIMEOUT, CONNECT_TIMEOUT)``.
    If it inlines the literal instead, the two paths stop sharing one
    number -- and a later edit to the constant moves one path and not the
    other.
    """
    sentinel = _Sentinel(4242.0)
    monkeypatch.setattr(claude_api_bot, "CONNECT_TIMEOUT", sentinel)

    timeouts = _sync_timeouts(monkeypatch)

    assert timeouts, "the sync path never called requests.post"
    for t in timeouts:
        connect = _connect_of(t)
        assert connect is not None, (
            f"C3: the sync path must pass a (connect, read) tuple, got {t!r}")
        assert connect is sentinel, (
            "C3: the sync path's CONNECT budget must come from the named "
            f"constant CONNECT_TIMEOUT; got {connect!r} instead of the "
            f"sentinel {sentinel!r} (full timeout {t!r})")


def test_c3_the_connect_budget_moves_with_the_constant(monkeypatch):
    """C3 (third form): change the constant, the request must follow.

    This is the drift the constant was supposed to prevent, stated the
    other way round: if the request path really reads the constant, then
    changing the constant changes what reaches ``requests.post``.

    Together with the two tests above this closes both sides:

      * constant changed, path reads it  -> this test passes, C3 stays green;
      * path inlines a literal          -> the two tests above go red.
    """
    for value in (180.0, 90.0, 7.5):
        monkeypatch.setattr(claude_api_bot, "CONNECT_TIMEOUT", value)
        for t in _stream_timeouts(monkeypatch):
            assert _connect_of(t) == value, (
                "C3: the streaming path's CONNECT budget must follow "
                f"CONNECT_TIMEOUT; expected {value!r}, got {_connect_of(t)!r} "
                f"(full timeout {t!r})")


# --------------------------------------------------------------------------
# C4 — the source form: no request path may spell the connect number out
# --------------------------------------------------------------------------

def _timeout_producers(tree):
    """Every expression in the file that PRODUCES a timeout value.

    A timeout reaches ``requests.post`` through a helper, so the helper's
    ``return`` statement is just as much a "request path" as the call site
    is.  This yields the expressions that can hold a timeout:

      * ``return <expr>`` inside any function (the helper's own value);
      * ``timeout=<expr>`` in any call (the value handed to requests).

    Only the CONNECT half is interesting, so the caller decides which
    element of a tuple it is looking at.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            yield node.value, ("return", node.lineno)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "timeout":
                    yield kw.value, ("timeout=", node.lineno)


def _connect_half(expr):
    """The connect half of a timeout EXPRESSION, or None when not a tuple.

    A tuple expression's first element is the connect half; anything else
    is not a tuple at all (a bare number, or a call), and the caller
    handles that case separately.
    """
    if isinstance(expr, ast.Tuple) and expr.elts:
        return expr.elts[0]
    return None


def test_c4_no_request_path_inlines_the_connect_number():
    """C4: the connect budget must not be spelled out as a number.

    This is the SOURCE-form counterpart of C3.  Where C3 asks "at runtime,
    does the value come from the constant?", C4 asks "in the source, is the
    connect half a number rather than the constant's name?".

    Both are needed.  C3 alone would accept a helper that returns the
    constant *or* an expression that happens to evaluate to the sentinel;
    C4 alone would accept a constant that is defined but never used.
    Together they say: the connect budget is the constant, and the constant
    is what the request paths use.

    The scan looks at the CONNECT HALF of every timeout expression only.
    The READ half legitimately holds a computed number (the stall-derived
    read timeout), and the module's own ``CONNECT_TIMEOUT = 180``
    assignment is where the number is supposed to live.
    """
    path = os.path.join(REPO, "models", "claudeapi", "claude_api_bot.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    offenders = []
    for expr, (kind, lineno) in _timeout_producers(tree):
        half = _connect_half(expr)
        if half is None:
            continue
        if isinstance(half, ast.Constant) and isinstance(half.value, (int, float)) \
                and not isinstance(half.value, bool):
            offenders.append((lineno, kind, ast.dump(half)))

    assert not offenders, (
        "C4: the CONNECT half of a request path must come from the named "
        "constant, not from an inline number; offenders (line, kind, "
        f"literal): {offenders}. An inline literal decouples the value from "
        "CONNECT_TIMEOUT, so editing the constant no longer moves this "
        "path and the two paths drift apart")


def test_c4_the_constant_is_defined_once_and_read_from_the_helpers():
    """C4 (second form): the constant must exist and the helpers must use it.

    Without this, a module could satisfy C4 by having no constant at all
    and no inline number either (a timeout computed some other way), and
    the "named constant" rule would be vacuous.

    The check is on NAMES, resolved through the module's own namespace, so
    moving the helper or renaming the function does not make it red.
    """
    assert hasattr(claude_api_bot, "CONNECT_TIMEOUT"), (
        "C4: the module must expose a named CONNECT_TIMEOUT constant")

    # NOTE: the constant's VALUE is deliberately NOT asserted here.
    # "The value must be 180" and "the value must come from the constant"
    # are two different rules, and they live in two different files:
    #
    #   value     -> tests/test_claude_stream_connect_timeout.py
    #   provenance-> this file
    #
    # Asserting the value here as well would make the same fact live in
    # two places, so a change to the budget would have to be made twice
    # and one copy could be forgotten.  One rule, one home.
    #
    # It also keeps the separation observable: changing the constant's
    # value makes the OTHER file red while this one stays green -- which
    # is exactly the proof that this file is about provenance and not
    # about the number.

    path = os.path.join(REPO, "models", "claudeapi", "claude_api_bot.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    # How many times is the constant ASSIGNED?  More than one assignment
    # means the "one named value" rule is already broken.
    assignments = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id == "CONNECT_TIMEOUT"
    ]
    assert len(assignments) == 1, (
        "C4: CONNECT_TIMEOUT must be assigned exactly once, found "
        f"{len(assignments)} assignments at lines {assignments}")

    # And it must be READ somewhere: a constant that is assigned and never
    # used is not the source of anything.
    reads = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "CONNECT_TIMEOUT"
        and isinstance(node.ctx, ast.Load)
    ]
    assert reads, (
        "C4: CONNECT_TIMEOUT is assigned but never read; the request paths "
        "are not using it")


# --------------------------------------------------------------------------
# the mutation surface this criterion is defending
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mutant", [
    "the streaming helper inlines the connect number",
    "the sync helper inlines the connect number",
])
def test_c3_c4_documented_surface(mutant):
    """A placeholder that keeps the two criteria's names visible.

    The two C3 tests and the two C4 tests are the criteria themselves; this
    test only records which surfaces they defend, so a future reader can
    see the coverage without reading the source.
    """
    assert mutant, "the mutant description must not be empty"
