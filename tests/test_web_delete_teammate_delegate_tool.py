"""Deleting a teammate must take the delegate tool away.

Same root cause as the roster-late case, one step further down the same
family: a runtime fixes its tool list when it is built, and the delegate
tool is only assembled for a conversation that has teammates. Every writer
of a roster therefore has to retire the session's runtimes, or the tool
list goes stale.

The web roster path retires (channel/web/api/sessions.py, the ``members``
branch). The bridge's channel path retires (AgentBridge._retire_session_
runtimes). The **sweep that runs when an Agent is deleted** does not:
``session_prefs.forget_agent`` prunes the deleted Agent's id out of every
roster that names it -- a roster write, on the *host's* session -- and the
host keeps a runtime that still offers ``agent_delegate`` for a teammate
that no longer exists.

What is measured here, on the product's own code path:

  * the roster really is pruned (the sweep ran);
  * the host's next-turn tool list must no longer carry the delegate tool;
  * the same call must not disturb a runtime whose roster did not change.

The sweep is driven through the real ``AgentAdminService.delete_agent``,
and the tool list through the real ``AgentBridge.get_agent`` -- a fake
bridge would not reproduce the mechanism under test.
"""

import os
import shutil
import tempfile

import pytest


SID = "delete_teammate_delegate_tool_probe"
HOST = "host-agent"
MATE = "mate-agent"


def _tool_names(agent):
    return sorted(t.name for t in getattr(agent, "tools", []) or [])


@pytest.fixture
def team_install(tmp_path, monkeypatch):
    """A two-Agent install whose session-prefs store is the test's own.

    The teammate has to exist for real: the shared-conversation predicate
    resolves a roster id against the registry, and an id that resolves to
    nothing is a ghost rather than a teammate.
    """
    import pathlib

    import common.state_dir as state_dir
    from agent.registry import AgentProfile, AgentRegistry, set_agent_registry
    from agent.workspace import session_prefs

    host_root = tmp_path / "host"
    mate_root = tmp_path / "mate"
    host_root.mkdir()
    mate_root.mkdir()

    registry = AgentRegistry(
        [
            AgentProfile(HOST, "Host", str(host_root)),
            AgentProfile(MATE, "Mate", str(mate_root)),
        ],
        default_agent_id=HOST,
    )
    set_agent_registry(registry)

    # session_prefs resolves its store file at call time, so redirecting
    # shared_root keeps this test's roster out of the real one.
    monkeypatch.setattr(state_dir, "shared_root", lambda *a, **k: pathlib.Path(tmp_path))

    import bridge.bridge as bridge_module
    from bridge.bridge import Bridge

    bridge = Bridge.new_instance()
    agent_bridge = bridge.get_agent_bridge()

    # The deletion path reaches the bridge through ``Bridge()`` -- the
    # process-wide singleton, which is what every other caller uses, so the
    # runtime it retires is the one that answers this conversation. Point the
    # module attribute at this test's instance so the two are one bridge.
    monkeypatch.setattr(bridge_module, "Bridge", lambda: bridge)

    yield {
        "agent_bridge": agent_bridge,
        "bridge": bridge,
        "session_prefs": session_prefs,
        "host": HOST,
        "mate": MATE,
        "host_root": host_root,
    }

    set_agent_registry(None)


def _delete_teammate(team, extra_agents=()):
    """The real deletion path: delete the teammate, sweep the rosters.

    ``AgentAdminService`` takes its settings directly, so the roster under
    test is the service's own -- no file is written and nothing outside the
    test's tree is touched.
    """
    from agent.admin import AgentAdminService

    root = team["host_root"].parent
    settings = {
        "agent_workspace": str(root),
        "agents": [
            {"id": team["host"], "enabled": True},
            {"id": team["mate"], "enabled": True},
        ] + [{"id": aid, "enabled": True} for aid in extra_agents],
        "default_agent_id": team["host"],
        "channel_instances": [],
    }
    service = AgentAdminService(os.path.join(str(root), "config.json"), settings=settings)
    return service.delete_agent(team["mate"])


def test_deleting_a_teammate_takes_the_delegate_tool_away(team_install):
    """The diff arm: the roster loses its only teammate, so the next turn
    must not be able to delegate."""
    ab = team_install["agent_bridge"]
    prefs = team_install["session_prefs"]

    # A team conversation to begin with.
    prefs.set_prefs(SID, team_install["host"], members=[team_install["mate"]])
    first = ab.get_agent(session_id=SID, agent_id=team_install["host"])
    assert "agent_delegate" in _tool_names(first), (
        "precondition: a team conversation must be offered the delegate tool"
    )

    # The user deletes that teammate. The sweep prunes the roster.
    _delete_teammate(team_install)

    assert not prefs.get_prefs(SID, team_install["host"]).get("members"), (
        "precondition: the deletion sweep must have pruned the roster"
    )

    second = ab.get_agent(session_id=SID, agent_id=team_install["host"])
    assert "agent_delegate" not in _tool_names(second), (
        "session %r / agent %r: a teammate deleted from a live conversation must "
        "leave the next turn without the delegate tool"
        % (SID, team_install["host"])
    )


def test_deleting_an_unrelated_agent_leaves_the_roster_alone(team_install):
    """The control arm: a deletion that touches no roster must not cost a
    rebuild, and the conversation must keep its delegate tool."""
    ab = team_install["agent_bridge"]
    prefs = team_install["session_prefs"]

    prefs.set_prefs(SID, team_install["host"], members=[team_install["mate"]])
    first = ab.get_agent(session_id=SID, agent_id=team_install["host"])
    assert "agent_delegate" in _tool_names(first), (
        "precondition: a team conversation must be offered the delegate tool"
    )

    # A third Agent is deleted: it is in no roster, so nothing changes.
    from agent.admin import AgentAdminService

    root = team_install["host_root"].parent
    settings = {
        "agent_workspace": str(root),
        "agents": [
            {"id": team_install["host"], "enabled": True},
            {"id": team_install["mate"], "enabled": True},
            {"id": "unrelated-agent", "enabled": True},
        ],
        "default_agent_id": team_install["host"],
        "channel_instances": [],
    }
    service = AgentAdminService(os.path.join(str(root), "config.json"), settings=settings)
    service.delete_agent("unrelated-agent")

    assert prefs.get_prefs(SID, team_install["host"]).get("members") == [
        team_install["mate"]
    ], "precondition: an unrelated deletion must not touch this roster"

    second = ab.get_agent(session_id=SID, agent_id=team_install["host"])
    assert second is first, (
        "session %r / agent %r: a deletion that changed no roster must not "
        "rebuild the runtime" % (SID, team_install["host"])
    )
    assert "agent_delegate" in _tool_names(second), (
        "session %r / agent %r: the conversation is still a team and must "
        "keep the delegate tool" % (SID, team_install["host"])
    )
