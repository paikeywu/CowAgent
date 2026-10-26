"""Identity namespaces for on-disk memory paths (T4).

A logical identity (``user_id`` / ``agent_id``) and the directory it maps to
are deliberately DECOUPLED: the directory name is a filesystem-safe slug,
the mapping lives in ``users.json`` at the memory index root, and the
logical id stays verbatim in the database.

Why the decoupling exists: the earlier scheme prefixed ids as ``a:<id>`` /
``u:<id>``. On Windows ``:`` is drive-letter syntax, so ``a:foo`` is not a
directory name — ``os.makedirs`` fails or, worse, a path parser strips
``a:`` as a drive and the id degrades to ``'a'`` for EVERY agent, merging
all of them into one namespace (a silent cross-identity leak). The
prefixes here are ``agent--<slug>`` / ``user--<slug>``: no colon anywhere.

Rules:

* Any identifier that is about to become a directory name must pass the
  whitelist — letters, digits, ``-``, ``_``, ``.``, not starting with a
  dot, no separators. Failure RAISES; a memory write that cannot name its
  directory must never silently land somewhere shared.
* A safe id maps to itself (``user--alice``). An unsafe one maps to a
  content-hashed slug (``user--x<sha16>``) and the mapping is REGISTERED
  in ``users.json`` so the on-disk path can be resolved back to the
  logical id, character for character.
* Legacy directories without a prefix (``memory/users/alice/``) parse as
  the bare id, so pre-T4 data keeps resolving.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional

USER_PREFIX = "user--"
AGENT_PREFIX = "agent--"

# Filesystem-safe whitelist: no ':' (Windows drive syntax), no separators,
# no leading dot (hidden), no '..' (traversal), bounded length.
_SAFE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62})?$")

_registry_lock = threading.Lock()


class UnsafeIdentifierError(ValueError):
    """An identifier that can never become a directory name (empty, or
    nothing usable left after slugging). Raised, never swallowed."""


def validate_fs_identifier(name: str) -> str:
    """Return ``name`` unchanged if it may become a directory name.

    This is the filesystem-safety gate for ANY identifier about to touch
    disk. It rejects path separators, drive-letter syntax, traversal and
    hidden/empty segments. Rejection is an exception — never a silent
    rewrite, because a rewrite nobody knows about IS the leak.
    """
    if not isinstance(name, str) or not name:
        raise UnsafeIdentifierError(f"identifier is empty or not a string: {name!r}")
    if not _SAFE_RE.match(name):
        raise UnsafeIdentifierError(
            f"identifier {name!r} is not filesystem-safe "
            f"(whitelist: ^[A-Za-z0-9][A-Za-z0-9._-]{{0,62}}$)"
        )
    return name


def is_fs_safe(name: str) -> bool:
    return isinstance(name, str) and bool(name) and bool(_SAFE_RE.match(name))


def _registry_path(workspace) -> Path:
    """users.json lives at the memory INDEX root (memory/long-term/), beside
    index.db: it is part of the index's identity bookkeeping, not content."""
    return Path(workspace) / "memory" / "long-term" / "users.json"


def _load_registry(workspace) -> Dict[str, Dict[str, str]]:
    path = _registry_path(workspace)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                "users": dict(data.get("users") or {}),
                "agents": dict(data.get("agents") or {}),
            }
    except FileNotFoundError:
        pass
    except Exception:
        # A corrupt registry must not silently empty the mapping — that
        # would make every hashed directory unresolvable. Fail loudly.
        raise
    return {"users": {}, "agents": {}}


def _register(workspace, kind: str, slug: str, logical_id: str) -> None:
    """Record slug -> logical id. Idempotent; a conflicting existing entry
    is a bug (two logical ids colliding on one slug) and raises."""
    path = _registry_path(workspace)
    with _registry_lock:
        data = _load_registry(workspace)
        table = data[kind]
        existing = table.get(slug)
        if existing is not None:
            if existing != logical_id:
                raise UnsafeIdentifierError(
                    f"{kind} slug {slug!r} is already registered to "
                    f"{existing!r}; cannot re-register to {logical_id!r}"
                )
            return
        table[slug] = logical_id
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def _slug_for(logical_id: str) -> str:
    """Filesystem-safe slug for one logical id.

    Safe ids keep their readable form. Unsafe ids (CJK names, embedded
    colons, separators) collapse onto a content hash — readable was already
    impossible, so the hash loses nothing and stays stable across runs.
    """
    if is_fs_safe(logical_id):
        return logical_id
    digest = hashlib.sha256(logical_id.encode("utf-8")).hexdigest()[:16]
    slug = f"x{digest}"
    # The digest slug is safe by construction; validate anyway so the gate
    # is never bypassed, even by this module.
    return validate_fs_identifier(slug)


def user_dir_name(user_id: str, workspace=None) -> str:
    """Directory name for one user's memory area: ``user--<slug>``.

    With ``workspace`` given, an unsafe id is registered in users.json so
    the path can be resolved back to the logical id. Safe ids need no
    registration (the slug IS the id) but are registered anyway when a
    workspace is available, so the registry is a complete directory of
    who has memory here.
    """
    if not isinstance(user_id, str) or not user_id:
        raise UnsafeIdentifierError(f"user_id is empty or not a string: {user_id!r}")
    slug = _slug_for(user_id)
    if workspace is not None:
        _register(workspace, "users", slug, user_id)
    return USER_PREFIX + slug


def agent_dir_name(agent_id: str, workspace=None) -> str:
    """Directory name for one agent's area: ``agent--<slug>``."""
    if not isinstance(agent_id, str) or not agent_id:
        raise UnsafeIdentifierError(f"agent_id is empty or not a string: {agent_id!r}")
    slug = _slug_for(agent_id)
    if workspace is not None:
        _register(workspace, "agents", slug, agent_id)
    return AGENT_PREFIX + slug


def parse_user_dir_name(segment: str, workspace=None) -> Optional[str]:
    """Resolve one path segment back to the logical user id.

    ``user--<slug>`` -> registry lookup (hashed ids) or the slug itself
    (safe ids, where slug == id). A bare legacy segment (pre-T4, no
    prefix) resolves to itself. Returns None for segments this function
    does not own (e.g. an ``agent--`` directory).
    """
    if not isinstance(segment, str) or not segment:
        return None
    if segment.startswith(AGENT_PREFIX):
        return None
    if not segment.startswith(USER_PREFIX):
        # Legacy pre-T4 layout: the directory name WAS the id.
        return segment
    slug = segment[len(USER_PREFIX):]
    if not slug:
        return None
    if workspace is not None:
        registered = _load_registry(workspace)["users"].get(slug)
        if registered is not None:
            return registered
    # A safe slug is its own logical id by construction (user_dir_name
    # keeps safe ids verbatim), so this is a resolution, not a guess. A
    # slug that fails the whitelist is a corrupted segment — loud, because
    # returning it would mint a phantom identity.
    if not is_fs_safe(slug):
        raise UnsafeIdentifierError(
            f"user directory {segment!r} does not resolve: the slug is not "
            f"filesystem-safe and there is no users.json entry for it"
        )
    return slug
