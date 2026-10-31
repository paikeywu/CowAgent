# encoding:utf-8
"""交办 #2 · 判据 —— CONNECT 超时不被 stall 阈值改变。

**这个判据守的是什么。**

2026-10-31 修掉了 Claude 流式请求的跨线程死锁（读者线程与看门狗线程
抢同一把 ``BufferedReader`` 锁）。修法是把「结束这条流」的决定权从
看门狗线程移到读者线程：给 socket 一个 **READ** 超时，让 ``recv()``
自己超时返回，读者再自己判定「这条流已经安静超过 stall 阈值了」。

修法只该动 **read** 侧。connect 侧（建立连接的时间预算）是另一回事，
必须原地不动 —— 因为 ``requests`` 的 ``timeout=`` 单值会被展开成
``(connect=该值, read=该值)``，所以「只改 read」这件事必须写成
**元组**才成立。

本文件把这件事钉死：

  * ``C1`` —— 流式路径传给 ``requests.post`` 的 ``timeout`` 必须是元组，
    且 **connect 半 == 180**，read 半 == 由 stall 阈值推导的值；
  * ``C2`` —— 流式路径与非流式路径的 **connect 超时必须一致**。

**判据读的是「真正传给 requests 的那个值」**，不是自己重算一遍期望值
再拿去和被测函数比对 —— 后者是同义反复：改了规则，两边一起动，检查
照样是绿的。做法是 **拦截 ``requests.post``**，把它的 ``timeout``
实参原样抓出来。

**BREAKER（两条判据各自有）**：

  * 把流式路径的 ``timeout=(180, read)`` 改回单值 ``read``
    ⇒ ``C1`` 转红（不是元组）；
  * 把流式路径的 connect 半改成别的数
    ⇒ ``C1`` 与 ``C2`` 同时转红。
"""

import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from models.claudeapi import claude_api_bot  # noqa: E402
from models.claudeapi.claude_api_bot import (  # noqa: E402
    CONNECT_TIMEOUT,
    STREAM_STALL_SECONDS,
    ClaudeAPIBot,
)


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
        # A response good enough for the call sites under test: 200, and
        # (for the streaming path) an empty body the reader can finish.
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


def _bot(monkeypatch, model="claude-opus-4-1"):
    bot = ClaudeAPIBot.__new__(ClaudeAPIBot)
    monkeypatch.setitem(claude_api_bot.conf(), "model", model)
    monkeypatch.setitem(claude_api_bot.conf(), "character_desc", "")
    monkeypatch.setitem(claude_api_bot.conf(), "claude_api_key", "test")
    monkeypatch.setitem(claude_api_bot.conf(), "claude_api_base", "http://127.0.0.1:1/v1")
    monkeypatch.setitem(claude_api_bot.conf(), "proxy", "")
    return bot


def _expected_read_timeout():
    """The read half the production code is supposed to use.

    This is the RULE, spelled out independently of the implementation:
    ``max(1.0, STREAM_STALL_SECONDS * 1.2)``.  It is compared against the
    value that actually reached ``requests.post``, so a change to the rule
    and a change to the implementation cannot move together silently.
    """
    return max(1.0, float(STREAM_STALL_SECONDS) * 1.2)


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
    """The connect half of a timeout value, or None when it is not a tuple.

    Only a TUPLE names the two halves separately.  A bare number leaves
    the connect budget implicit (requests expands it to ``(v, v)``), which
    is precisely the shape this criterion forbids on the streaming path
    — so the criterion must not re-derive the expansion rule itself: it
    asks what was actually handed over.
    """
    if isinstance(timeout_value, tuple) and len(timeout_value) == 2:
        return timeout_value[0]
    return None


def _read_of(timeout_value):
    if isinstance(timeout_value, tuple) and len(timeout_value) == 2:
        return timeout_value[1]
    return None


# --------------------------------------------------------------------------
# C1 — the streaming path passes a (connect, read) TUPLE, connect == 180
# --------------------------------------------------------------------------

def test_c1_streaming_timeout_is_a_tuple_whose_connect_half_is_180(monkeypatch):
    """C1: the streaming path must NOT collapse the connect budget.

    ``requests`` expands a single float into ``(connect=v, read=v)``, so a
    single value silently moves BOTH budgets.  The streaming path needs a
    read timeout derived from the stall limit; the connect budget must
    stay at 180 — the value every request path used before the fix.

    The assertion reads the ``timeout`` argument that actually reached
    ``requests.post`` (captured by intercepting the call), so it cannot
    pass by re-deriving the value itself.
    """
    timeouts = _stream_timeouts(monkeypatch)

    for t in timeouts:
        assert isinstance(t, tuple), (
            "C1: the streaming path must pass a (connect, read) tuple, got "
            f"{t!r} ({type(t).__name__}); a single value would move the "
            "CONNECT budget as well")
        assert len(t) == 2, f"C1: expected a 2-tuple, got {t!r}"

    for t in timeouts:
        connect = _connect_of(t)
        assert connect == 180, (
            "C1: the CONNECT timeout of the streaming path must stay at "
            f"180 s, got {connect!r} (full timeout {t!r})")

    for t in timeouts:
        read = _read_of(t)
        expected = _expected_read_timeout()
        assert read == pytest.approx(expected), (
            "C1: the READ timeout of the streaming path must follow the "
            f"stall limit (max(1.0, STREAM_STALL_SECONDS*1.2) = {expected!r}), "
            f"got {read!r} (full timeout {t!r})")


def test_c1_the_connect_budget_is_not_derived_from_the_stall_limit(monkeypatch):
    """C1 (second form): the connect budget must not MOVE when the stall limit moves.

    This is the property the single-value form silently violated: with
    ``timeout=read_timeout`` the connect budget was a function of
    ``STREAM_STALL_SECONDS``.  Shrinking the stall limit (the test suite
    does exactly that) shrank the connect budget with it.

    Both readings are taken from the same intercepted ``requests.post``
    call, so this cannot be satisfied by a rule that both sides share.
    """
    monkeypatch.setattr(claude_api_bot, "STREAM_STALL_SECONDS", 3.0)
    t_small = _stream_timeouts(monkeypatch)[0]

    monkeypatch.setattr(claude_api_bot, "STREAM_STALL_SECONDS", 600.0)
    t_big = _stream_timeouts(monkeypatch)[0]

    assert _connect_of(t_small) == _connect_of(t_big) == 180, (
        "C1: the CONNECT budget must not follow the stall limit; "
        f"got {t_small!r} (stall 3s) and {t_big!r} (stall 600s)")
    assert _read_of(t_small) == pytest.approx(max(1.0, 3.0 * 1.2))
    assert _read_of(t_big) == pytest.approx(max(1.0, 600.0 * 1.2))


# --------------------------------------------------------------------------
# C2 — the streaming and non-streaming paths agree on the CONNECT budget
# --------------------------------------------------------------------------

def test_c2_streaming_and_sync_paths_agree_on_the_connect_timeout(monkeypatch):
    """C2: both paths must give the connect phase the same budget.

    A drift here is invisible in production — the connect phase only
    matters on a bad route, where the difference shows up as a request
    that dies at 108 s instead of 180 s.  The two values are read from
    two different intercepted calls, so this is a comparison between
    two OBSERVED values, not a comparison with a re-derived rule.
    """
    stream_timeouts = _stream_timeouts(monkeypatch)
    sync_timeouts = _sync_timeouts(monkeypatch)

    stream_connects = [_connect_of(t) for t in stream_timeouts]
    sync_connects = [_connect_of(t) for t in sync_timeouts]

    assert None not in stream_connects, (
        "C2: the streaming path must pass a (connect, read) tuple; got "
        f"{stream_timeouts!r}")
    assert None not in sync_connects, (
        "C2: the sync path must pass a (connect, read) tuple; got "
        f"{sync_timeouts!r}")

    assert set(stream_connects) == set(sync_connects), (
        "C2: the streaming and non-streaming paths must agree on the "
        f"CONNECT timeout; streaming={stream_connects!r} sync={sync_connects!r}")
    assert set(stream_connects) == {180}, (
        "C2: the shared CONNECT timeout must be 180 s, got "
        f"{sorted(set(stream_connects))!r}")


def test_c2_the_sync_path_connect_budget_survives_a_stall_limit_change(monkeypatch):
    """C2 (second form): the sync path's connect budget is independent too.

    The sync path states its budget as ``(CONNECT, CONNECT)`` — both
    halves 180, matching what ``requests`` used to expand the bare
    ``timeout=180`` into.  If somebody later makes the sync path's read
    half follow the stall limit, this goes red, and the connect half
    stays put.
    """
    monkeypatch.setattr(claude_api_bot, "STREAM_STALL_SECONDS", 3.0)
    small = _sync_timeouts(monkeypatch)[0]
    monkeypatch.setattr(claude_api_bot, "STREAM_STALL_SECONDS", 600.0)
    big = _sync_timeouts(monkeypatch)[0]

    assert small == big, (
        "C2: the sync path's timeout must not follow the stall limit; got "
        f"{small!r} (stall 3s) and {big!r} (stall 600s)")
    assert _connect_of(small) == _connect_of(big) == CONNECT_TIMEOUT, (
        "C2: the sync path's CONNECT half must stay at "
        f"{CONNECT_TIMEOUT!r}; got {small!r} and {big!r}")


# --------------------------------------------------------------------------
# the rule itself — what the two halves MEAN
# --------------------------------------------------------------------------

def test_the_connect_timeout_is_a_named_constant_and_is_180():
    """The connect budget must be ONE named value, not a literal repeated.

    A literal repeated in four places is exactly how the two paths drift
    apart: changing one of them is a one-character edit nobody notices.

    The check reads the CALL NODES (via ``ast``), not the raw text: a
    prose mention of ``timeout=180`` in a docstring is not a hard-coded
    timeout, and renaming or moving a function must not make this
    criterion red.
    """
    assert CONNECT_TIMEOUT == 180, (
        f"the CONNECT timeout must remain 180 s, got {CONNECT_TIMEOUT!r}")

    path = os.path.join(REPO, "models", "claudeapi", "claude_api_bot.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    hard_coded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "post":
            continue
        for kw in node.keywords:
            if kw.arg != "timeout":
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value == 180:
                hard_coded.append(node.lineno)

    assert not hard_coded, (
        "C2: no request path may hard-code the literal 180 as its "
        f"timeout (lines {hard_coded}); the connect budget must go through "
        "the named constant so the paths cannot drift apart")
