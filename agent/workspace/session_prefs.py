"""Per-session runtime preferences: which model answers, what it may change.

Sibling of :mod:`agent.workspace.project_store`, same shape and same key scheme.
A conversation can pin its own model and its own permission mode; anything not
pinned follows the global settings, so an untouched session keeps behaving the
way the instance is configured.

Why a JSON file rather than the sessions table: the web UI mints a session id in
the browser and a row only appears once the first message is persisted. A user
who picks a model before typing must have somewhere to put it, and this store
has no foreign key to satisfy.

Stored under ``<shared_root>/session_prefs.json``::

    {
      "sessions": {
        "default::session_abc": {
          "provider": "claudeAPI",
          "model": "claude-sonnet-5",
          "permission": "workspace-write",
          "ts": 1731999999.0
        }
      }
    }

Every field is optional. Absent means "follow the global setting".
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, Iterable, List, Optional

from common.atomic_write import write_json_atomic
from common.log import logger

# ``members`` is the roster of a team conversation: the Agent ids the session's
# owner may hand work to. Here rather than in a table of its own for the same
# reason the model override is — it can be set before the conversation has a
# single message, so there is no row to hang it off yet. An empty roster is
# stored as absent, which is exactly "not a team conversation".
_FIELDS = ("provider", "model", "permission", "members")

_lock = threading.Lock()


def _store_file() -> str:
    from common.state_dir import shared_root

    return str(shared_root() / "session_prefs.json")


def _load() -> Dict:
    path = _store_file()
    if not os.path.isfile(path):
        return {"sessions": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:
        logger.warning(f"[SessionPrefs] Could not read {path}: {e}")
        return {"sessions": {}}
    if not isinstance(data, dict):
        return {"sessions": {}}
    if not isinstance(data.get("sessions"), dict):
        data["sessions"] = {}
    return data


def _save(data: Dict) -> None:
    path = _store_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_atomic(path, data, indent=2)
    except Exception as e:
        logger.warning(f"[SessionPrefs] Could not write {path}: {e}")
        raise


def _session_key(session_id: str, agent_id: Optional[str]) -> str:
    """Namespace by agent so identical session ids across Agents don't collide."""
    return f"{agent_id or 'default'}::{session_id}"


def get_prefs(session_id: str, agent_id: Optional[str] = None) -> Dict:
    """Overrides explicitly set for a session (missing keys = follow global)."""
    if not session_id:
        return {}
    with _lock:
        data = _load()
        entry = data["sessions"].get(_session_key(session_id, agent_id))
    if not isinstance(entry, dict):
        return {}
    return {k: entry[k] for k in _FIELDS if entry.get(k)}


def members_index() -> Dict[tuple, list]:
    """Every team conversation's roster, keyed by ``(agent_id, session_id)``.

    Listing conversations needs the roster for a whole page at once, and asking
    per row would re-read and re-parse the file once per conversation. Only
    team conversations appear here, so an empty result means nobody has ever
    invited anybody.
    """
    with _lock:
        data = _load()
    index: Dict[tuple, list] = {}
    for key, entry in data["sessions"].items():
        if not isinstance(entry, dict) or not entry.get("members"):
            continue
        agent_id, _, session_id = str(key).partition("::")
        if session_id:
            index[(agent_id, session_id)] = list(entry["members"])
    return index


def set_prefs(
    session_id: str,
    agent_id: Optional[str] = None,
    **updates,
) -> Dict:
    """Merge overrides for a session; pass ``None`` for a field to clear it.

    Returns the resulting override dict (empty when the session now follows the
    global settings entirely).
    """
    if not session_id:
        raise ValueError("session_id is required")

    key = _session_key(session_id, agent_id)
    with _lock:
        data = _load()
        entry = data["sessions"].get(key)
        entry = dict(entry) if isinstance(entry, dict) else {}

        for field in _FIELDS:
            if field not in updates:
                continue
            value = updates[field]
            if value is None or (isinstance(value, str) and not value.strip()):
                entry.pop(field, None)
            else:
                entry[field] = value.strip() if isinstance(value, str) else value

        result = {k: entry[k] for k in _FIELDS if entry.get(k)}
        if result:
            data["sessions"][key] = {**result, "ts": time.time()}
        else:
            # Nothing pinned any more: drop the row instead of leaving a husk.
            data["sessions"].pop(key, None)
        _save(data)
    return result


def forget_session(session_id: str, agent_id: Optional[str] = None) -> None:
    """Drop a session's overrides (called when the conversation is deleted)."""
    if not session_id:
        return
    key = _session_key(session_id, agent_id)
    with _lock:
        data = _load()
        if data["sessions"].pop(key, None) is not None:
            _save(data)


def forget_session_under_any(
    session_id: str, agent_ids: Iterable[Optional[str]]
) -> None:
    """Drop a session's overrides under every id in *agent_ids*.

    A session's row is keyed by the owning Agent's id, and the id the row
    was written under and the id it is read back under have to be the same.
    The write side resolves the reserved "default" alias to the real id of
    the Agent behind it, so a row written today lives under the real id --
    but a row written before that still lives under the alias.

    A delete that only knows one of the two keys leaves the other behind: the
    conversation is gone and its overrides are not. So the caller passes
    every id the row could be under, and this drops whichever of them exist.
    Duplicates are harmless.

    The store is written once, not once per id.
    """
    if not session_id:
        return
    keys = []
    for agent_id in agent_ids or ():
        key = _session_key(session_id, agent_id)
        if key not in keys:
            keys.append(key)
    if not keys:
        return
    with _lock:
        data = _load()
        changed = False
        for key in keys:
            if data["sessions"].pop(key, None) is not None:
                changed = True
        if changed:
            _save(data)


def forget_agent(agent_id: str) -> List[str]:
    """Erase every trace of a deleted Agent from the prefs store.

    Deleting an Agent leaves two kinds of dangling references here that would
    otherwise linger forever:

    - Its own sessions' overrides, keyed ``{agent_id}::*`` — orphaned, because
      the conversations they belonged to were removed with the Agent's
      workspace.
    - Its id sitting in *other* Agents' team ``members`` rosters — a ghost
      teammate the composer/settings would keep offering as ``available:false``.

    Both are pruned in one pass; a row left with nothing pinned is dropped
    rather than kept as an empty husk.

    Returns the ids of the sessions whose roster changed, so a caller that
    keeps live runtimes can retire them. A runtime fixes its tool list when
    it is built, and the delegate tool is only assembled for a conversation
    that has teammates: a roster that loses a member while the conversation
    is already live has to retire what was built under the old roster, or the
    conversation keeps offering a tool that points at an Agent which no
    longer exists.
    """
    if not agent_id:
        return []
    owner_prefix = f"{agent_id}::"
    touched: List[str] = []
    with _lock:
        data = _load()
        sessions = data["sessions"]
        changed = False

        for key in [k for k in sessions if str(k).startswith(owner_prefix)]:
            sessions.pop(key, None)
            changed = True
            _, _, session_id = str(key).partition("::")
            if session_id:
                touched.append(session_id)

        # The roster sweep itself is shared with the archive path; see
        # ``forget_agent_from_rosters``.
        roster_changed, roster_touched = _forget_agent_from_rosters(sessions, agent_id)
        changed = changed or roster_changed
        touched.extend(roster_touched)

        if changed:
            _save(data)
    return touched


def _forget_agent_from_rosters(sessions: Dict, agent_id: str) -> tuple:
    """Prune *agent_id* out of every team roster in *sessions*, in place.

    Only the rosters are touched: an Agent's own ``{agent_id}::*`` rows stay
    untouched here, because an archived Agent keeps its workspace and its
    history -- only a deleted one loses them.

    Returns ``(changed, touched)``: whether any roster was rewritten, and the
    ids of the sessions whose roster changed, so a caller that keeps live
    runtimes can retire them. The two are separate because a roster can
    change without yielding a session id, and the store still has to be
    written in that case.

    The caller holds the store's lock and saves the file.
    """
    changed = False
    touched: List[str] = []
    for key, entry in list(sessions.items()):
        if not isinstance(entry, dict) or not entry.get("members"):
            continue
        members = [m for m in entry["members"] if m != agent_id]
        if len(members) == len(entry["members"]):
            continue
        changed = True
        if members:
            entry["members"] = members
        else:
            entry.pop("members", None)
            if not any(entry.get(f) for f in _FIELDS):
                sessions.pop(key, None)
        _, _, session_id = str(key).partition("::")
        if session_id:
            touched.append(session_id)
    return changed, touched


def forget_agent_from_rosters(agent_id: str) -> List[str]:
    """Prune an archived Agent's id out of every team roster, and report which.

    Archiving an Agent takes it out of service without deleting it: its
    workspace and its history stay. What must not stay is its name sitting in
    other conversations' team rosters, because a roster entry names a
    teammate that can actually be reached -- an archived one cannot, and a
    hand-off to it is refused. A conversation whose roster still lists it
    would keep offering a delegate tool with nobody behind it while the
    roster page still showed the name.

    Returns the ids of the sessions whose roster changed, so a caller that
    keeps live runtimes can retire them: a runtime fixes its tool list when
    it is built, so a roster that changed under a live conversation has to
    retire what was built under the old one.

    Deliberately does *not* touch the Agent's own ``{agent_id}::*`` rows --
    those belong to an Agent that is still here.
    """
    if not agent_id:
        return []
    with _lock:
        data = _load()
        sessions = data["sessions"]
        changed, touched = _forget_agent_from_rosters(sessions, agent_id)
        if changed:
            _save(data)
    return touched


def resolve_permission(session_id: str, agent_id: Optional[str] = None) -> str:
    """The permission mode in force for a session: its own, else the global one."""
    from agent.permission import global_mode, normalize_mode

    prefs = get_prefs(session_id, agent_id)
    if prefs.get("permission"):
        return normalize_mode(prefs["permission"], global_mode())
    return global_mode()
