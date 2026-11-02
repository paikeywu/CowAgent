"""The delegation budget must not throw away work that actually happened.

The deadline in ``_local_run_deadline`` only SETS the cancel event - it does
not kill the teammate's run. Measured 2026-11-03: a teammate whose budget ran
out kept working and finished its answer afterwards, and the bridge handed that
answer back as an ordinary reply. The old code dropped that reply on the floor
and returned a bare "exceeded its time budget", so the caller could not tell
"the teammate produced nothing" from "the teammate finished, but the budget
expired first".

Same defect 4e5266d2 fixed on the sub-agent side.

These tests pin the invariant: an answer that exists is handed back, and the
caller can tell over-budget-with-output from over-budget-without-output.
"""
import time

import pytest

from bridge.reply import Reply, ReplyType

from tests.test_agent_delegation import FakeBridge, _context, _tool


@pytest.fixture(autouse=True)
def _team_members(monkeypatch):
    """Same roster stub as the sibling module: primary + research.

    ``autouse`` only covers the module that defines it, so this file needs
    its own copy - without it every delegation is rejected as "not a
    teammate", which is a different failure from the one under test.
    """
    roster = {"members": ["primary", "research"]}

    def fake_get_prefs(session_id, agent_id=None):
        return dict(roster)

    from agent.workspace import session_prefs

    monkeypatch.setattr(session_prefs, "get_prefs", fake_get_prefs)
    return roster


class _SlowBridge(FakeBridge):
    """Finishes AFTER the budget expires, with a real answer.

    ``delay`` must exceed the policy's ``timeout_seconds`` for the expiry to
    fire while the teammate is still running.
    """

    def __init__(self, delay=0.4, answer="结论：队友在预算到点后完成了", registry=None):
        super().__init__(registry)
        self.delay = delay
        self.answer = answer

    def agent_reply(self, query, context=None, on_event=None):
        self.calls.append((query, context, on_event))
        time.sleep(self.delay)
        return Reply(ReplyType.TEXT, self.answer)


class _EmptyBridge(FakeBridge):
    """Finishes after the budget expires, with nothing to say."""

    def __init__(self, delay=0.4, registry=None):
        super().__init__(registry)
        self.delay = delay

    def agent_reply(self, query, context=None, on_event=None):
        self.calls.append((query, context, on_event))
        time.sleep(self.delay)
        return Reply(ReplyType.TEXT, "")


def _policy(seconds):
    # ``AgentDelegateTool(config=...)`` is handed the agent_delegation SECTION
    # itself, not the whole config dict - ``DelegationPolicy.from_config``
    # reads ``timeout_seconds`` straight off it.
    return {"timeout_seconds": seconds}


def test_over_budget_with_output_hands_the_answer_back():
    """K-1: the answer exists, so it must survive the budget expiry."""
    bridge = _SlowBridge(delay=0.4, answer="结论：队友在预算到点后完成了")
    tool = _tool(bridge=bridge, config=_policy(0.05))

    result = tool.execute({"agent_id": "research", "task": "Check"})

    # Not a bare failure any more: the work happened, so the answer rides
    # along with it.
    assert result.status == "success"
    assert result.result["status"] == "budget_expired"
    assert result.result["budget_expired"] is True
    assert result.result["content"] == "结论：队友在预算到点后完成了"
    # The caller can still tell this apart from a run that finished in time.
    assert result.result["status"] != "done"


def test_over_budget_without_output_says_so_explicitly():
    """K-2: no answer came back, and the caller must be able to tell."""
    tool = _tool(bridge=_EmptyBridge(delay=0.4), config=_policy(0.05))

    result = tool.execute({"agent_id": "research", "task": "Check"})

    assert result.status == "error"
    assert "exceeded its" in str(result.result)
    # The two over-budget outcomes must NOT look the same to the caller.
    assert result.result != "budget_expired"


def test_a_run_that_fits_its_budget_is_untouched():
    """K-3: the normal path keeps its old shape."""
    tool = _tool(bridge=FakeBridge(), config=_policy(30))

    result = tool.execute({"agent_id": "research", "task": "Check"})

    assert result.status == "success"
    assert result.result["status"] == "done"
    assert result.result["content"] == "delegated result"
    assert "budget_expired" not in result.result


def test_the_two_over_budget_outcomes_are_distinguishable():
    """K-4: the negative control.

    If the over-budget case returned the same shape as a normal completion,
    the caller could not tell them apart and the "output survives" claim
    would be worthless. This is the check that keeps K-1 honest.
    """
    over = _tool(bridge=_SlowBridge(delay=0.4), config=_policy(0.05))
    over_result = over.execute({"agent_id": "research", "task": "Check"})

    normal = _tool(bridge=FakeBridge(), config=_policy(30))
    normal_result = normal.execute({"agent_id": "research", "task": "Check"})

    assert over_result.status == "success"
    assert normal_result.status == "success"
    # Same overall verdict (both produced an answer)...
    assert over_result.status == normal_result.status
    # ...but different provenance, and the caller can act on it.
    assert over_result.result["status"] == "budget_expired"
    assert normal_result.result["status"] == "done"
    assert over_result.result["budget_expired"] is True
    assert "budget_expired" not in normal_result.result
