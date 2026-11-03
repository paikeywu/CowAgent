"""Adding a teammate to a live Web session must grow the tool list.

The web console lets the user invite somebody into a conversation that is
already running (``POST /api/sessions/<id>/settings`` with ``members``). The
conversation then *is* a team conversation, but a runtime fixes its tool list
when it is built and the ``agent_delegate`` tool is only assembled for a
conversation that has teammates. So a roster written after the runtime exists
left the conversation a team with no way to reach its team: the next turn
answered as a solo chat, and the only way out was a restart.

The bridge already retires a session's runtimes when it rewrites a roster
(``_retire_session_runtimes``); the web path wrote the roster and did not, so
the two paths disagreed.

What is measured here, on the product's own code path:

  * the **diff** arm: the runtime exists, the roster arrives afterwards, and
    the *next* turn's tool list must contain ``agent_delegate``;
  * the **control** arm: the roster is written *before* the runtime is built,
    which has always worked, and must keep working.

Both arms drive the real ``SessionSettingsHandler.POST`` and the real
``AgentBridge.get_agent`` — a fake bridge would not reproduce the mechanism
under test (the caching of the runtime by ``(agent_id, session_id)``), so a
green result would prove nothing.
"""

import json

import pytest


SID = "roster_delegate_tool_probe"

# The console posts the roster without naming an Agent, so the prefs row is
# keyed by the reserved "default" alias — which is also the id a single-agent
# install gives its one Agent. The teammate is a second, named Agent.
OWNER = "default"
TEAMMATE = "teammate"


def _tool_names(agent):
    return sorted(t.name for t in getattr(agent, "tools", []) or [])


@pytest.fixture
def web_team(tmp_path, monkeypatch):
    """A web console with two Agents and a session prefs store of its own.

    The registry is pinned so the teammate below really exists: the shared
    conversation predicate resolves a roster id against the registry, and an id
    that resolves to nothing is a ghost rather than a teammate.
    """
    import bridge.bridge as bridge_module
    import channel.web.api.sessions as sessions_api
    import common.state_dir as state_dir
    from agent.registry import AgentProfile, AgentRegistry, set_agent_registry
    from agent.workspace import session_prefs

    owner_root = tmp_path / "owner"
    mate_root = tmp_path / "mate"
    owner_root.mkdir()
    mate_root.mkdir()

    registry = AgentRegistry(
        [
            AgentProfile(OWNER, "Owner", str(owner_root)),
            AgentProfile(TEAMMATE, "Teammate", str(mate_root)),
        ],
        default_agent_id=OWNER,
    )
    set_agent_registry(registry)

    # session_prefs resolves its store file at call time, so redirecting
    # shared_root keeps this test's roster out of the real one.
    monkeypatch.setattr(state_dir, "shared_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(sessions_api, "_require_auth", lambda: None)
    monkeypatch.setattr(sessions_api.web, "header", lambda *a, **k: None)

    # A real Bridge and a real AgentBridge: the runtime cache and the tool
    # assembly under test live in those two, and a stand-in would not
    # reproduce them.
    bridge = bridge_module.Bridge.new_instance()
    agent_bridge = bridge.get_agent_bridge()

    # The handler imports Bridge inside the function, so the name to patch is
    # the one in its own module, not a module-level attribute of sessions.py.
    monkeypatch.setattr(bridge_module, "Bridge", lambda: bridge)

    yield {
        "agent_bridge": agent_bridge,
        "registry": registry,
        "session_prefs": session_prefs,
        "owner": OWNER,
        "teammate": TEAMMATE,
    }

    set_agent_registry(None)


def _post_settings(payload):
    """The console's own call: POST to the session settings endpoint."""
    import channel.web.api.sessions as sessions_api

    body = json.dumps(payload).encode()
    original = sessions_api.web.data
    sessions_api.web.data = lambda: body
    try:
        return json.loads(sessions_api.SessionSettingsHandler().POST(SID))
    finally:
        sessions_api.web.data = original


def test_roster_added_to_a_live_session_grows_the_tool_list(web_team):
    """The diff arm: the roster arrives after the runtime, and the next turn
    must be able to reach the new teammate."""
    ab = web_team["agent_bridge"]
    prefs = web_team["session_prefs"]

    # The conversation starts as a solo chat: no roster yet.
    prefs.set_prefs(SID, web_team["owner"], members=None)

    # The first turn builds the runtime. It is cached per (agent_id, session_id),
    # and its tool list is fixed here.
    first = ab.get_agent(session_id=SID, agent_id=web_team["owner"])
    assert "agent_delegate" not in _tool_names(first), (
        "precondition: a solo conversation must not be offered the delegate tool"
    )

    # Now the user invites somebody into the *running* conversation.
    response = _post_settings({"members": [web_team["teammate"]]})
    assert response["status"] == "success"
    assert prefs.get_prefs(SID, web_team["owner"])["members"] == [
        web_team["teammate"]
    ], "the roster must actually be stored"

    # The next turn asks for the Agent again — the same path a real message
    # takes. Its tool list must now carry the delegate tool.
    second = ab.get_agent(session_id=SID, agent_id=web_team["owner"])
    assert "agent_delegate" in _tool_names(second), (
        "session %r / agent %r: a roster written while the conversation was "
        "already live must leave the next turn able to delegate"
        % (SID, web_team["owner"])
    )


def test_roster_set_before_the_runtime_grows_the_tool_list(web_team):
    """The control arm: the roster arrives first, which always worked.

    It must keep working — and it must reach the same tool list, or the diff
    arm would be measuring something the product never had.
    """
    ab = web_team["agent_bridge"]
    prefs = web_team["session_prefs"]

    prefs.set_prefs(SID, web_team["owner"], members=None)

    response = _post_settings({"members": [web_team["teammate"]]})
    assert response["status"] == "success"

    agent = ab.get_agent(session_id=SID, agent_id=web_team["owner"])
    assert "agent_delegate" in _tool_names(agent), (
        "session %r / agent %r: a roster set before the runtime is built must "
        "leave the conversation able to delegate"
        % (SID, web_team["owner"])
    )


def test_removing_the_last_teammate_takes_the_tool_away(web_team):
    """The symmetric arm: the roster goes away, so the tool must go too.

    The tool is assembled from the roster when the runtime is built, so a
    fix that only ever *adds* it would leave a removed teammate reachable.
    """
    ab = web_team["agent_bridge"]
    prefs = web_team["session_prefs"]

    # A team conversation to begin with.
    prefs.set_prefs(SID, web_team["owner"], members=[web_team["teammate"]])
    first = ab.get_agent(session_id=SID, agent_id=web_team["owner"])
    assert "agent_delegate" in _tool_names(first), (
        "precondition: a team conversation must be offered the delegate tool"
    )

    # The user takes the teammate back out, while the conversation is live.
    response = _post_settings({"members": None})
    assert response["status"] == "success"
    assert not prefs.get_prefs(SID, web_team["owner"]).get("members"), (
        "the roster must actually be cleared"
    )

    second = ab.get_agent(session_id=SID, agent_id=web_team["owner"])
    assert "agent_delegate" not in _tool_names(second), (
        "session %r / agent %r: a teammate removed from a live conversation must "
        "leave the next turn without the delegate tool"
        % (SID, web_team["owner"])
    )


def test_changing_only_the_model_does_not_retire_the_runtime(web_team):
    """A model or permission change is applied per turn; it must not cost a
    rebuild. Only a roster change changes what the tool list contains."""
    ab = web_team["agent_bridge"]
    prefs = web_team["session_prefs"]
    prefs.set_prefs(SID, web_team["owner"], members=None)

    first = ab.get_agent(session_id=SID, agent_id=web_team["owner"])

    response = _post_settings({"model": "some-model"})
    assert response["status"] == "success"

    second = ab.get_agent(session_id=SID, agent_id=web_team["owner"])
    assert second is first, (
        "session %r / agent %r: a model change must not rebuild the runtime"
        % (SID, web_team["owner"])
    )
