"""A roster must only name teammates that can actually be reached.

``session_prefs.members`` is a promise: every id in it names a teammate the
conversation may hand work to. Two readers consult it, and they used to
disagree about what a name means:

  * the *assembly* side (``AgentInitializer._any_member_exists``) counts a
    name as a teammate as long as it resolves to a configured Agent --
    ``require_enabled=False`` -- so an **archived** Agent still counts;
  * the *hand-off* side (``resolve_teammate``) resolves with
    ``require_enabled=True``, so an archived Agent does **not**.

A roster made entirely of archived Agents therefore satisfied the first
reader and failed the second: the conversation was offered ``agent_delegate``
with **no target behind it**. The tool refuses every hand-off ("no teammate
to delegate to"), while the roster page still listed the name.

Two entrances reach that state, and both are measured here:

  * **D3 -- the web write side.** ``SessionSettingsHandler.POST`` stored
    whatever list it was given, so a roster of archived Agents could be
    written straight into the store.
  * **D5 -- archiving a teammate.** ``update_agent(enabled=False)`` took the
    Agent out of service but left its name in every roster that named it,
    and left the affected runtimes holding a tool list built under the old
    roster.

What is asserted here, on the product's own code path:

  * the **control** arm: a roster naming a live teammate keeps it, the tool
    is assembled and the teammate is reachable;
  * the **D3** arm: a roster naming only archived Agents does not end up
    storing them -- so the conversation is not a team with nobody in it;
  * the **D5** arm: archiving a teammate takes it out of **every** roster
    that named it -- including the rosters of *other* conversations, which
    the archiving Agent knows nothing about -- and the affected runtimes are
    retired, so the next turn is rebuilt under the new roster.

The expectation of "reachable" is computed **here**, independently: a local
Agent that is enabled, or a teammate another process hosts. It is not read
out of ``resolve_teammate`` or ``_clean_team_members``, because those are
the code under test -- an expectation taken from them would move with the
implementation and stay green forever.
"""

import json
import os

import pytest


# The conversation under test. Two hosts, so the D5 arm can prove the sweep
# reaches a roster that belongs to somebody else.
SID_OWN = "roster_reachability_own"
SID_OTHER = "roster_reachability_other"
OTHER_HOST = "other-host"

HOST = "default"
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
    for name in (HOST, MATE, ARCHIVED, OTHER_HOST):
        root = tmp_path / name
        root.mkdir()
        roots[name] = root

    registry = AgentRegistry(
        [
            AgentProfile(HOST, "Host", str(roots[HOST])),
            AgentProfile(OTHER_HOST, "Other Host", str(roots[OTHER_HOST])),
            AgentProfile(MATE, "Mate", str(roots[MATE])),
            AgentProfile(ARCHIVED, "Archived", str(roots[ARCHIVED]), enabled=False),
        ],
        default_agent_id=HOST,
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

    yield {
        "agent_bridge": agent_bridge,
        "bridge": bridge,
        "registry": registry,
        "session_prefs": session_prefs,
        "roots": roots,
    }

    set_agent_registry(None)


def _post_settings(payload, session_id=SID_OWN):
    """The console's own call: POST to the session settings endpoint."""
    import channel.web.api.sessions as sessions_api

    body = json.dumps(payload).encode()
    original = sessions_api.web.data
    sessions_api.web.data = lambda: body
    try:
        return json.loads(sessions_api.SessionSettingsHandler().POST(session_id))
    finally:
        sessions_api.web.data = original


def _archive(install, mate=MATE):
    """Archive the teammate the way the console does.

    ``AgentAdminService`` takes its settings directly, so the roster under
    test is the service's own -- no file outside the test's tree is written.

    The pinned registry is put in step with the archive, which is what the
    console's ``_reload_agent_runtime`` does in production: it replaces the
    process-wide registry with one that knows the Agent is out of service.
    """
    from agent.admin import AgentAdminService

    root = install["roots"][HOST].parent
    settings = {
        "agent_workspace": str(root),
        "agents": [
            {"id": HOST, "enabled": True},
            {"id": OTHER_HOST, "enabled": True},
            {"id": MATE, "enabled": True},
            {"id": ARCHIVED, "enabled": False},
        ],
        "default_agent_id": HOST,
        "channel_instances": [],
    }
    service = AgentAdminService(os.path.join(str(root), "config.json"), settings=settings)
    result = service.update_agent(mate, enabled=False)
    # Keep the pinned registry in step with the archive.
    try:
        install["registry"].set_enabled(mate, False)
    except Exception:
        pass
    return result


def test_control_a_live_teammate_is_kept_and_reachable(install):
    """The control arm: a roster naming a live teammate keeps it.

    The conversation is a team, the delegate tool is assembled, and the
    teammate is reachable -- the state every other arm is measured against.
    """
    ab = install["agent_bridge"]
    prefs = install["session_prefs"]

    prefs.set_prefs(SID_OWN, HOST, members=None)

    response = _post_settings({"members": [MATE]})
    assert response["status"] == "success"

    stored = prefs.get_prefs(SID_OWN, HOST).get("members")
    assert stored == [MATE], (
        "session %r / agent %r: a live teammate must be stored as asked, got %r"
        % (SID_OWN, HOST, stored)
    )

    agent = ab.get_agent(session_id=SID_OWN, agent_id=HOST)
    assert "agent_delegate" in _tool_names(agent), (
        "session %r / agent %r: a team conversation must be offered the "
        "delegate tool" % (SID_OWN, HOST)
    )
    assert _reachable_ids(stored) == [MATE], (
        "session %r / agent %r: the stored roster must name a reachable "
        "teammate, got %r" % (SID_OWN, HOST, stored)
    )


def test_d3_a_roster_of_archived_teammates_is_not_stored(install):
    """The D3 arm: the web write side must not store unreachable names.

    The measured outcome is stated here: the roster that lands in the store
    must not name an Agent that is out of service. The consequence -- the
    conversation is not a team with nobody in it -- is asserted right after,
    through the product's own tool assembly.
    """
    ab = install["agent_bridge"]
    prefs = install["session_prefs"]

    prefs.set_prefs(SID_OWN, HOST, members=None)

    response = _post_settings({"members": [ARCHIVED]})
    assert response["status"] == "success"

    stored = prefs.get_prefs(SID_OWN, HOST).get("members") or []
    assert ARCHIVED not in stored, (
        "session %r / agent %r: the web write side must not store %r, which "
        "is out of service and cannot be reached; stored roster was %r"
        % (SID_OWN, HOST, ARCHIVED, stored)
    )

    # The consequence, measured through the product: no teammate reachable
    # means no delegate tool offering a target nobody can be.
    agent = ab.get_agent(session_id=SID_OWN, agent_id=HOST)
    names = _tool_names(agent)
    assert not (("agent_delegate" in names) and not _reachable_ids(stored)), (
        "session %r / agent %r: the conversation offers the delegate tool "
        "but no teammate is reachable -- a zombie tool; stored roster was %r"
        % (SID_OWN, HOST, stored)
    )


def test_d5_archiving_a_teammate_clears_every_roster_and_retires(install):
    """The D5 arm: archiving sweeps every roster, including other people's.

    Two conversations name the teammate: the one under test and one owned by
    somebody else. The archiving Agent knows nothing about the second -- the
    sweep has to find it by itself -- and both must come out clean.
    """
    ab = install["agent_bridge"]
    prefs = install["session_prefs"]

    # A teammate in a roster on this conversation and on somebody else's.
    prefs.set_prefs(SID_OWN, HOST, members=[MATE])
    prefs.set_prefs(SID_OTHER, OTHER_HOST, members=[MATE])

    # Both conversations are running, so both hold a runtime built under the
    # old roster.
    own_agent = ab.get_agent(session_id=SID_OWN, agent_id=HOST)
    other_agent = ab.get_agent(session_id=SID_OTHER, agent_id=OTHER_HOST)
    assert "agent_delegate" in _tool_names(own_agent), (
        "precondition: session %r / agent %r must be a team conversation"
        % (SID_OWN, HOST)
    )
    assert "agent_delegate" in _tool_names(other_agent), (
        "precondition: session %r / agent %r must be a team conversation"
        % (SID_OTHER, OTHER_HOST)
    )

    # The teammate goes out of service.
    _archive(install, MATE)

    for sid, owner in ((SID_OWN, HOST), (SID_OTHER, OTHER_HOST)):
        stored = prefs.get_prefs(sid, owner).get("members") or []
        assert MATE not in stored, (
            "session %r / agent %r: archiving %r must take it out of this "
            "roster too; stored roster was %r"
            % (sid, owner, MATE, stored)
        )

    # The roster changed under a live conversation, so the runtime built
    # under the old one has to be retired: the next turn must be rebuilt.
    rebuilt = ab.get_agent(session_id=SID_OWN, agent_id=HOST)
    assert rebuilt is not own_agent, (
        "session %r / agent %r: archiving a teammate must retire the "
        "runtime built under the old roster" % (SID_OWN, HOST)
    )
    assert "agent_delegate" not in _tool_names(rebuilt), (
        "session %r / agent %r: the rebuilt runtime must not offer a "
        "delegate tool whose only target went out of service"
        % (SID_OWN, HOST)
    )

    rebuilt_other = ab.get_agent(session_id=SID_OTHER, agent_id=OTHER_HOST)
    assert rebuilt_other is not other_agent, (
        "session %r / agent %r: archiving a teammate must retire the "
        "runtimes of every conversation it was in" % (SID_OTHER, OTHER_HOST)
    )


def test_d3_the_owner_cannot_be_invited_as_its_own_teammate(install):
    """The owner is cleaned against its *resolved* id, not the alias.

    The console posts the roster without naming an Agent, so the host is
    addressed by the reserved "default" alias -- but the Agent behind that
    alias has a real id of its own, and the roster is stored and read back
    under the real id. Cleaning against the alias would compare the alias
    against member ids that are real ids, and the owner would survive as its
    own teammate: the conversation would offer a delegate tool whose only
    "teammate" is the host itself, which the tool rejects.
    """
    from agent.registry import AgentProfile, AgentRegistry, set_agent_registry

    ab = install["agent_bridge"]
    prefs = install["session_prefs"]

    # An install whose default Agent has a real id that is not the alias.
    real_id = "primary-agent"
    registry = AgentRegistry(
        [
            AgentProfile(real_id, "Primary", str(install["roots"][HOST])),
            AgentProfile(MATE, "Mate", str(install["roots"][MATE])),
        ],
        default_agent_id=real_id,
    )
    set_agent_registry(registry)
    ab.agent_registry = registry

    try:
        prefs.set_prefs(SID_OWN, None, members=None)

        # The console names nobody: the host is the reserved alias.
        response = _post_settings({"members": [real_id, MATE]})
        assert response["status"] == "success"

        stored = prefs.get_prefs(SID_OWN, real_id).get("members") or []
        assert real_id not in stored, (
            "session %r / owner %r: the host %r must not be stored as its own "
            "teammate; the reserved alias resolves to %r, and the roster is "
            "read back under the resolved id. Stored roster was %r"
            % (SID_OWN, None, real_id, real_id, stored)
        )
        assert MATE in stored, (
            "session %r / owner %r: the real teammate must survive the "
            "cleaning; stored roster was %r" % (SID_OWN, None, stored)
        )
    finally:
        set_agent_registry(None)


def test_d5_archiving_keeps_the_agents_own_sessions(install):
    """Archiving is not deleting: the Agent keeps its own conversations.

    The delete path erases the Agent's own ``{id}::*`` rows; the archive path
    must not, because the workspace and the history are still there.
    """
    prefs = install["session_prefs"]

    # The archived Agent's own session, pinned to a model of its own.
    prefs.set_prefs("archived-mate-own-session", ARCHIVED, model="some-model")
    before = prefs.get_prefs("archived-mate-own-session", ARCHIVED)
    assert before.get("model") == "some-model", "precondition: the pin must be there"

    _archive(install, MATE)

    after = prefs.get_prefs("archived-mate-own-session", ARCHIVED)
    assert after.get("model") == "some-model", (
        "archiving must not erase the Agent's own session overrides -- "
        "that is the delete path's job"
    )
