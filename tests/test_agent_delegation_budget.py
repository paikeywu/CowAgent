"""The delegation budget must not throw away work that actually happened.

The deadline in ``_local_run_deadline`` only SETS the cancel event. What the
executor does with that is abort the turn - and it answers with whatever the
turn had produced so far. Measured 2026-11-03 against the real bridge
(``agents/agent-mvnewfyb/deliverables/evidence/2026-11-03-delegate-fix/
mechanism_probe.txt`` and a direct capture of the real fixture):

  * cut off with NOTHING produced  -> the executor hands back the
    cancellation placeholder ``_(Cancelled)_`` - a stand-in, not an answer
  * cut off after producing text   -> that text comes back, and it is real
    but INCOMPLETE
  * finished inside its budget     -> the complete answer

The old code dropped the reply on the floor in every one of those cases and
returned a bare "exceeded its time budget", so the caller could not tell "the
teammate did nothing" from "the teammate did most of it". Same defect
4e5266d2 fixed on the sub-agent side.

These tests pin the invariant: work that happened is handed back, and the
caller can tell all three outcomes apart.

THE FAKES HERE MUST REPRODUCE THE REAL MECHANISM. An earlier version of
this file used a fake that returned a finished answer after the deadline,
which the real executor never does - that fake made the test pass while
testing nothing but itself. The fixture in
``tests/test_agent_delegation_timeout.py`` drives the real bridge and the
real cancel registry; these tests borrow its shape.
"""
import time

import pytest

from bridge.reply import CANCELLED_PLACEHOLDER, Reply, ReplyType

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


class _CancelledWithNothing(FakeBridge):
    """The real executor's answer when the turn was cut off with no output."""

    def __init__(self, delay=0.4, registry=None):
        super().__init__(registry)
        self.delay = delay

    def agent_reply(self, query, context=None, on_event=None):
        self.calls.append((query, context, on_event))
        time.sleep(self.delay)
        return Reply(ReplyType.TEXT, CANCELLED_PLACEHOLDER)


class _CancelledWithPartialOutput(FakeBridge):
    """The real executor's answer when the turn was cut off after some text.

    ``agent/protocol/agent_stream.py`` keeps the partial response and only
    falls back to the placeholder when the response is empty - so a cut-off
    turn CAN carry real (incomplete) text.
    """

    def __init__(self, delay=0.4, partial="已核对到第 3 项，第 4 项还没查完", registry=None):
        super().__init__(registry)
        self.delay = delay
        self.partial = partial

    def agent_reply(self, query, context=None, on_event=None):
        self.calls.append((query, context, on_event))
        time.sleep(self.delay)
        return Reply(ReplyType.TEXT, self.partial)


def _policy(seconds):
    # ``AgentDelegateTool(config=...)`` is handed the agent_delegation SECTION
    # itself, not the whole config dict - ``DelegationPolicy.from_config``
    # reads ``timeout_seconds`` straight off it.
    return {"timeout_seconds": seconds}


def test_over_budget_with_partial_output_hands_it_back():
    """K-1: the text exists, so it must survive the budget expiry."""
    bridge = _CancelledWithPartialOutput(delay=0.4)
    tool = _tool(bridge=bridge, config=_policy(0.05))

    result = tool.execute({"agent_id": "research", "task": "Check"})

    # Not a bare failure: the work happened, so the text rides along.
    assert result.status == "success"
    assert result.result["status"] == "budget_expired"
    assert result.result["budget_expired"] is True
    # ...and the caller is told it is partial, so it is not read as a
    # finished answer.
    assert result.result["partial"] is True
    assert result.result["content"] == "已核对到第 3 项，第 4 项还没查完"


def test_over_budget_with_no_output_is_a_failure():
    """K-2: the cancellation placeholder is NOT an answer.

    The executor hands back ``_(Cancelled)_`` when the turn was cut off
    without producing anything. Reporting that as a successful answer would
    tell the caller "the teammate answered" when it answered nothing.
    """
    tool = _tool(bridge=_CancelledWithNothing(delay=0.4), config=_policy(0.05))

    result = tool.execute({"agent_id": "research", "task": "Check"})

    assert result.status == "error"
    assert "exceeded its" in str(result.result)
    # The placeholder must never be presented as an answer.
    assert CANCELLED_PLACEHOLDER not in str(result.result)


def test_a_run_that_fits_its_budget_is_untouched():
    """K-3: the normal path keeps its old shape."""
    tool = _tool(bridge=FakeBridge(), config=_policy(30))

    result = tool.execute({"agent_id": "research", "task": "Check"})

    assert result.status == "success"
    assert result.result["status"] == "done"
    assert result.result["content"] == "delegated result"
    assert "budget_expired" not in result.result


def test_the_three_outcomes_are_distinguishable():
    """K-4: the negative control.

    If the over-budget-with-output case returned the same shape as a normal
    completion, the caller could not tell them apart and the "output
    survives" claim would be worthless. This is the check that keeps K-1
    honest.
    """
    over = _tool(bridge=_CancelledWithPartialOutput(delay=0.4), config=_policy(0.05))
    over_result = over.execute({"agent_id": "research", "task": "Check"})

    nothing = _tool(bridge=_CancelledWithNothing(delay=0.4), config=_policy(0.05))
    nothing_result = nothing.execute({"agent_id": "research", "task": "Check"})

    normal = _tool(bridge=FakeBridge(), config=_policy(30))
    normal_result = normal.execute({"agent_id": "research", "task": "Check"})

    # Three distinct outcomes.
    assert over_result.status == "success"
    assert nothing_result.status == "error"
    assert normal_result.status == "success"

    # The two successes are still told apart.
    assert over_result.result["status"] == "budget_expired"
    assert normal_result.result["status"] == "done"
    assert over_result.result["partial"] is True
    assert "budget_expired" not in normal_result.result

    # And the failure says why, with no placeholder masquerading as an answer.
    assert "exceeded its" in str(nothing_result.result)
    assert CANCELLED_PLACEHOLDER not in str(nothing_result.result)
