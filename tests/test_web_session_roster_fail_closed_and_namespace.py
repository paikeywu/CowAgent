"""A roster that cannot be checked must not be written; a roster must be
readable by the conversation it was written for.

Two defects are measured here, both of which the first roster-reachability
round left behind (or introduced):

  * **N1 -- the cleaner fails open.** ``_clean_roster_for_session`` caught
    every exception and returned the roster *unchanged*. That is the exact
    opposite of what the cleaning is for: when the roster cannot be checked
    against the reachable teammates, storing it anyway writes a promise the
    conversation cannot keep. The delegate tool is then assembled for a
    conversation whose only "teammates" cannot be reached at all -- a zombie
    tool: present in the list, with nothing behind it -- while the endpoint
    answers ``success`` and the user sees nothing wrong. Under a bridge
    failure the previous round therefore had a *negative* net effect.

  * **R2 -- the prefs key namespace.** The web write side keyed the row
    under whatever the request named, so a request naming nobody wrote
    ``default::{session}``. Every reader asks under the *resolved* owner id:
    the delegate tool, the prompt builder, the bridge, the team addressing
    and the initializer all hold a resolved id, never the reserved "default"
    alias. On an install whose default Agent's real id is not the literal
    string "default", the two ends sat on different keys and the roster the
    user had just set was invisible to the conversation it was set for -- no
    delegate tool, no reachable target.

What is asserted here, on the product's own code path:

  * the **N1 arm**: with the roster check made to fail, the endpoint must
    refuse the write -- no unchecked roster may land in the store, and the
    response must not claim success;
  * the **R2 arm**: on an install whose default Agent's real id differs
    from the alias, the console writes a roster and the *bridge* must read
    it back -- and the delegate tool must appear, which is the observable
    consequence the defect had.

The expectation of "reachable" is computed **here**, independently: a local
Agent that is enabled, or a teammate another process hosts. It is not read
out of ``resolve_teammate`` or ``_clean_team_members``, because those are
the code under test -- an expectation taken from them would move with the
implementation and stay green forever.
"""

import json
import os

import pytest


# The conversation under test.
SID_N1 = "roster_fail_closed_probe"
SID_R2 = "roster_key_namespace_probe"

# An install whose default Agent's real id is NOT the literal "default".
# This is the shape that triggers R2: the console addresses the owner by the
# reserved alias, every reader holds the real id.
REAL_ID = "primary-agent"
MATE = "mate"
ARCHIVED = "archived-mate"


def _tool_names(agent):
    return sorted(t.name for t in getattr(agent, "tools", []) or [])


def _reachable_ids(members):
    """The ids of *members* that can actually be reached, computed here.

    A name is reachable when it names an enabled Agent of this instance, or a
    teammate another process hosts through the installed transport. Anything
    else -- an archived Agent, a name nobody knows -- is not reachable, and
    a roster that keeps it is a promise the hand-off cannot keep.

    Deliberately independent of ``resolve_teammate`` / ``_clean_team_members``:
    those are the code under test.
    """
    from agent.multiagent import peer as peer_of
    from agent.registry import get_agent_registry

    registry = get_agent_registry()
    out = []
    for raw in members or []:
        mid = str(raw or "").strip()
        if not mid:
            continue
        resolved = None
        try:
            resolved = registry.get_addressed(mid, require_enabled=True).id
        except Exception:
            found = peer_of(mid)
            resolved = found.id if found is not None else None
        if resolved is not None and resolved not in out:
            out.append(resolved)
    return out


def _delegate_targets(agent_bridge, session_id, host_id):
    """The hand-off targets the delegate tool would offer, read the way the
    tool itself reads them -- through the bridge's registry."""
    from agent.tools.agent_delegate.agent_delegate import AgentDelegateTool

    tool = AgentDelegateTool({})
    tool.agent_bridge = agent_bridge
    return [
        entry["id"]
        for entry in tool._team_members(
            {"session_id": session_id, "agent_id": host_id}, host_id
        )
    ]


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A three-Agent install whose session-prefs store is the test's own.

    ``archived-mate`` exists but is out of service, which is the whole point:
    it is a name the roster may hold but the hand-off cannot reach.
    """
    import bridge.bridge as bridge_module
    import common.state_dir as state_dir
    from agent.registry import AgentProfile, AgentRegistry, set_agent_registry
    from agent.workspace import session_prefs

    roots = {}
    for name in (REAL_ID, MATE, ARCHIVED):
        root = tmp_path / name
        root.mkdir()
        roots[name] = root

    registry = AgentRegistry(
        [
            AgentProfile(REAL_ID, "Primary", str(roots[REAL_ID])),
            AgentProfile(MATE, "Mate", str(roots[MATE])),
            AgentProfile(ARCHIVED, "Archived", str(roots[ARCHIVED]), enabled=False),
        ],
        default_agent_id=REAL_ID,
    )
    set_agent_registry(registry)

    # session_prefs resolves its store file at call time, so redirecting
    # shared_root keeps this test's rosters out of the real one.
    monkeypatch.setattr(state_dir, "shared_root", lambda *a, **k: tmp_path)

    import channel.web.api.sessions as sessions_api

    monkeypatch.setattr(sessions_api, "_require_auth", lambda: None)
    monkeypatch.setattr(sessions_api.web, "header", lambda *a, **k: None)

    # A real Bridge and a real AgentBridge: the runtime cache and the tool
    # assembly under test live in those two, and a stand-in would not
    # reproduce them.
    bridge = bridge_module.Bridge.new_instance()
    agent_bridge = bridge.get_agent_bridge()
    # The handlers reach the bridge through the process-wide singleton, so
    # point the module attribute at this test's instance: one bridge, so the
    # runtime it retires is the one that answers this conversation.
    monkeypatch.setattr(bridge_module, "Bridge", lambda: bridge)
    # The bridge holds its own copy of the registry; keep it in step with the
    # pinned one, the way the console's runtime reload does in production.
    agent_bridge.agent_registry = registry

    yield {
        "agent_bridge": agent_bridge,
        "bridge": bridge,
        "registry": registry,
        "session_prefs": session_prefs,
        "roots": roots,
    }

    set_agent_registry(None)


def _post_settings(payload, session_id):
    """The console's own call: POST to the session settings endpoint."""
    import channel.web.api.sessions as sessions_api

    body = json.dumps(payload).encode()
    original = sessions_api.web.data
    sessions_api.web.data = lambda: body
    try:
        return json.loads(sessions_api.SessionSettingsHandler().POST(session_id))
    finally:
        sessions_api.web.data = original


def test_n1_a_roster_that_cannot_be_checked_is_not_stored(install):
    """The N1 arm: a failed check must refuse the write, not store it.

    The bridge is made to fail while the owner is still resolvable -- the
    shape of a real outage: the registry answers, the check does not.

    What is asserted, in order:

      * the endpoint does **not** answer ``success``;
      * **no** roster is written under the owner -- not the unchecked one,
        and not an empty one either (a silently empty roster would tell the
        user the invitation succeeded);
      * a roster that was already stored before the failure is left exactly
        as it was -- refusing the write must not destroy what was there.
    """
    ab = install["agent_bridge"]
    prefs = install["session_prefs"]

    # A roster that is already stored, and is healthy.
    prefs.set_prefs(SID_N1, REAL_ID, members=[MATE])
    before = prefs.get_prefs(SID_N1, REAL_ID).get("members")

    # The check fails while the owner still resolves.
    original_clean = ab._clean_team_members

    def boom(*args, **kwargs):
        raise RuntimeError("simulated bridge failure")

    ab._clean_team_members = boom
    try:
        response = _post_settings(
            {"members": [ARCHIVED]}, session_id=SID_N1
        )
    finally:
        ab._clean_team_members = original_clean

    assert response.get("status") != "success", (
        "session %r / agent %r: a roster that could not be checked must not be "
        "acknowledged as stored; the response was %r"
        % (SID_N1, REAL_ID, response)
    )
    assert response.get("status") == "error", (
        "session %r / agent %r: the refusal must be visible as an error, got %r"
        % (SID_N1, REAL_ID, response)
    )

    stored = prefs.get_prefs(SID_N1, REAL_ID).get("members")
    assert stored == before, (
        "session %r / agent %r: the refused write must leave the stored "
        "roster exactly as it was (%r before, %r after)"
        % (SID_N1, REAL_ID, before, stored)
    )
    assert ARCHIVED not in (stored or []), (
        "session %r / agent %r: the unchecked roster must never reach the "
        "store; stored roster was %r" % (SID_N1, REAL_ID, stored)
    )

    # The consequence, measured through the product: the roster that is
    # stored names a reachable teammate, so the delegate tool is not a
    # zombie.
    agent = ab.get_agent(session_id=SID_N1, agent_id=REAL_ID)
    names = _tool_names(agent)
    assert not (("agent_delegate" in names) and not _reachable_ids(stored)), (
        "session %r / agent %r: the conversation offers the delegate tool "
        "but no teammate is reachable -- a zombie tool; stored roster was %r"
        % (SID_N1, REAL_ID, stored)
    )


def test_n1_the_refusal_is_visible_to_the_user(install):
    """The refusal must say something the console can show.

    A refusal nobody can see is not a refusal: the console shows the
    endpoint's message, so an empty or missing one would leave the user
    looking at a roster that did not change, with no explanation.
    """
    ab = install["agent_bridge"]

    original_clean = ab._clean_team_members

    def boom(*args, **kwargs):
        raise RuntimeError("simulated bridge failure")

    ab._clean_team_members = boom
    try:
        response = _post_settings({"members": [MATE]}, session_id=SID_N1)
    finally:
        ab._clean_team_members = original_clean

    message = str(response.get("message") or "")
    assert message.strip(), (
        "session %r / agent %r: the refusal must carry a message the console "
        "can display; the response was %r" % (SID_N1, REAL_ID, response)
    )
    assert "roster" in message.lower() or "teammate" in message.lower(), (
        "session %r / agent %r: the message must say what went wrong with "
        "the roster; got %r" % (SID_N1, REAL_ID, message)
    )


def test_r2_the_bridge_reads_back_the_roster_the_console_wrote(install):
    """The R2 arm: the roster the console wrote must be the roster the
    conversation reads.

    The install's default Agent has the real id ``primary-agent``, and the
    console names nobody -- the owner is addressed by the reserved "default"
    alias. The write used to land under ``default::{session}`` while every
    reader asked under ``primary-agent::{session}`` and found nothing.

    The observable consequence is asserted, not the key: the delegate tool
    must be offered to the real host, and the teammate must be reachable.
    """
    ab = install["agent_bridge"]
    prefs = install["session_prefs"]

    prefs.set_prefs(SID_R2, REAL_ID, members=None)

    # The console names nobody: the owner is the reserved alias.
    response = _post_settings({"members": [MATE]}, session_id=SID_R2)
    assert response.get("status") == "success", (
        "session %r / agent %r: the write must succeed; got %r"
        % (SID_R2, REAL_ID, response)
    )

    # What the conversation itself reads, under the id it holds.
    stored = prefs.get_prefs(SID_R2, REAL_ID).get("members")
    assert stored == [MATE], (
        "session %r / agent %r: the bridge reads the roster under the "
        "resolved owner id, and must find the teammate the console invited; "
        "stored roster was %r" % (SID_R2, REAL_ID, stored)
    )

    # The consequence: the delegate tool is offered and the teammate is
    # reachable. Before the fix the tool was absent and the target list was
    # empty -- a roster the user could see but the conversation could not.
    agent = ab.get_agent(session_id=SID_R2, agent_id=REAL_ID)
    assert "agent_delegate" in _tool_names(agent), (
        "session %r / agent %r: a roster the console wrote must leave the "
        "conversation able to delegate" % (SID_R2, REAL_ID)
    )
    targets = _delegate_targets(ab, SID_R2, REAL_ID)
    assert MATE in targets, (
        "session %r / agent %r: the invited teammate must be a reachable "
        "hand-off target; targets were %r" % (SID_R2, REAL_ID, targets)
    )
    assert _reachable_ids(stored) == [MATE], (
        "session %r / agent %r: the stored roster must name a reachable "
        "teammate, got %r" % (SID_R2, REAL_ID, stored)
    )


def test_r2_the_console_reads_back_what_it_wrote(install):
    """The read side must ask under the same key the write side used.

    A write that lands under one key and a read that asks under another is
    worse than not writing at all: the console would report an empty roster
    for a conversation that has one, and the user would conclude the
    invitation did not take.
    """
    import channel.web.api.sessions as sessions_api

    prefs = install["session_prefs"]

    prefs.set_prefs(SID_R2, REAL_ID, members=None)
    response = _post_settings({"members": [MATE]}, session_id=SID_R2)
    assert response.get("status") == "success"

    # The state the endpoint echoes back is what the console renders.
    team = response.get("team") or {}
    member_ids = [m.get("id") for m in (team.get("members") or [])]
    assert MATE in member_ids, (
        "session %r / agent %r: the console must see the teammate it just "
        "invited; the echoed team state was %r" % (SID_R2, REAL_ID, team)
    )

    # The same, read through the settings GET, which asks the store under
    # the owner the console addressed. ``web.input`` returns an object with
    # named attributes, so the stand-in has to as well.
    class _Params:
        agent = ""
        agent_id = ""

    original_input = sessions_api.web.input
    sessions_api.web.input = lambda **kwargs: _Params()
    try:
        state = json.loads(sessions_api.SessionSettingsHandler().GET(SID_R2))
    finally:
        sessions_api.web.input = original_input

    assert state.get("status") == "success", (
        "session %r / agent %r: the settings read must succeed; got %r"
        % (SID_R2, REAL_ID, state)
    )
    read_ids = [
        m.get("id") for m in ((state.get("team") or {}).get("members") or [])
    ]
    assert MATE in read_ids, (
        "session %r / agent %r: the settings read must report the roster that "
        "is stored, not an empty one; it reported %r"
        % (SID_R2, REAL_ID, read_ids)
    )


def test_r2_deleting_the_session_removes_the_row_it_wrote(install):
    """What the write side keys, the delete side has to key the same way.

    A row that can be written but not deleted would follow the user around
    for ever: the conversation is gone, the roster is not.
    """
    import channel.web.api.sessions as sessions_api

    prefs = install["session_prefs"]

    prefs.set_prefs(SID_R2, REAL_ID, members=None)
    response = _post_settings({"members": [MATE]}, session_id=SID_R2)
    assert response.get("status") == "success"
    assert prefs.get_prefs(SID_R2, REAL_ID).get("members") == [MATE], (
        "precondition: the roster must be stored under the resolved owner id"
    )

    # The delete path: the console names nobody either.
    original_input = sessions_api.web.input
    original_data = sessions_api.web.data
    sessions_api.web.input = lambda **kwargs: {"agent_id": ""}
    sessions_api.web.data = lambda: b"{}"
    try:
        deleted = json.loads(
            sessions_api.SessionDetailHandler().DELETE(SID_R2)
        )
    finally:
        sessions_api.web.input = original_input
        sessions_api.web.data = original_data

    assert deleted.get("status") == "success", (
        "session %r / agent %r: the delete must succeed; got %r"
        % (SID_R2, REAL_ID, deleted)
    )
    assert not prefs.get_prefs(SID_R2, REAL_ID).get("members"), (
        "session %r / agent %r: deleting the conversation must drop the "
        "roster row that was written for it"
    )


def test_the_control_arm_a_live_teammate_is_still_written(install):
    """The control arm: the fix must not close the door on the normal path.

    Everything above measures a refusal or a namespace. This one measures
    that an ordinary invitation -- a live teammate, the owner left implicit
    -- still lands in the store under the resolved owner id.
    """
    prefs = install["session_prefs"]

    prefs.set_prefs(SID_R2, REAL_ID, members=None)
    response = _post_settings({"members": [MATE]}, session_id=SID_R2)

    assert response.get("status") == "success", (
        "session %r / agent %r: an ordinary invitation must succeed; got %r"
        % (SID_R2, REAL_ID, response)
    )
    stored = prefs.get_prefs(SID_R2, REAL_ID).get("members")
    assert stored == [MATE], (
        "session %r / agent %r: a live teammate must be stored as asked, got %r"
        % (SID_R2, REAL_ID, stored)
    )
    assert not prefs.get_prefs(SID_R2, None).get("members"), (
        "session %r / agent %r: the roster must not also be written under the "
        "reserved alias -- one conversation, one row"
    )
