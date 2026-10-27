"""Release state — resolved from the DISK, never from the index.

P0-11 P-2 (high): after a release, the released content was still returned by
the regular retrieval exit, anonymously. The root cause was that nothing knew
what "released" meant at retrieval time: the storage layer only had
scope + user_id, the tool layer had no filter at all, and ``chunks.metadata``
was always None.

The rules this module implements (SEED 裁决 2026-10-27):

  * The TRUTH layer keeps the content — release is not deletion (遗忘伦理).
    The PROJECTION layer must hide it.
  * The authority is the DISK: ``memory/markdown/_tombstones/*.tombstone.md``
    plus the front-matter of the released file's new version
    (``status=released`` / ``visibility=sealed``). Whatever the index says
    is only a projection, and it has to be correct again after a rebuild.
  * Two DISTINCT exclusions, because they are two different facts:
      1. the ELEMENT is released → the whole element leaves the result set,
         including the version whose own front-matter still says ``active``
         (WR-11 keeps the old bytes untouched, so the old file cannot know
         by itself that it was released);
      2. the FILE seals itself → ``visibility=sealed`` / ``status=released``
         (the release event, the released version).

Why the element case needs the supersedes chain: releasing element E
produces ``E-v2`` with ``supersedes: [E-v1]`` and a tombstone naming
``E-v1``. A later revision of ``E-v2`` carries the same body and does NOT
carry ``status=released`` — the facade creates revisions with the default
status. So released-ness has to travel along the chain, otherwise a revised
released element walks straight back into the index.

Everything here is read-only. It reads files; it never writes, and it never
edits the index (that is the manager's job, and the inspector's is only to
report).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Set, Tuple

from agent.memory.frontmatter import split_frontmatter

# The truth layer's markdown root, relative to the memory root.
_MARKDOWN_DIR = "markdown"
_TOMBSTONE_DIR = "_tombstones"


@dataclass(frozen=True)
class ReleaseState:
    """Which index paths must not be returned by the regular retrieval exit.

    ``released_paths`` — the element behind this path was released.
    ``sealed_paths``  — this path seals itself (front-matter).

    Both sets hold INDEX KEYS: workspace-relative POSIX paths, exactly the
    strings ``chunks.path`` carries.
    """

    released_paths: FrozenSet[str] = frozenset()
    sealed_paths: FrozenSet[str] = frozenset()
    signature: Tuple = ()

    def element_released(self, path: str) -> bool:
        return path in self.released_paths

    def file_sealed(self, path: str) -> bool:
        return path in self.sealed_paths

    def flags_for(self, path: str) -> Tuple[int, int]:
        """``(element_released, file_sealed)`` as 0/1 for the chunk columns."""
        return (1 if self.element_released(path) else 0,
                1 if self.file_sealed(path) else 0)


def _norm_key(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _memory_root(workspace_root: Path) -> Path:
    return Path(workspace_root) / "memory"


def _iter_markdown(memory_root: Path) -> List[Path]:
    base = memory_root / _MARKDOWN_DIR
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.rglob("*.md")):
        if not p.is_file():
            continue
        if _TOMBSTONE_DIR in p.relative_to(base).parts:
            continue  # tombstones are read separately, and never indexed
        out.append(p)
    return out


def _iter_tombstones(memory_root: Path) -> List[Path]:
    base = memory_root / _MARKDOWN_DIR / _TOMBSTONE_DIR
    if not base.is_dir():
        return []
    return [p for p in sorted(base.glob("*.tombstone.md")) if p.is_file()]


def tree_signature(workspace_root: Path | str) -> Tuple:
    """A cheap fingerprint of the truth layer.

    Two calls with the same signature see the same release state, so the
    caller can cache the (more expensive) resolution and re-resolve only
    when the truth layer actually moved. The tombstone directory counts
    separately: a release adds a tombstone AND a new version, and both
    move the signature — but a tombstone alone (the release record) must
    also be enough to trigger a re-resolve.
    """
    memory_root = _memory_root(Path(workspace_root))
    md = memory_root / _MARKDOWN_DIR
    count = 0
    newest = 0
    if md.is_dir():
        for dirpath, dirnames, filenames in os.walk(md):
            dirnames[:] = [d for d in dirnames if d != _TOMBSTONE_DIR]
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                count += 1
                try:
                    mtime = os.stat(os.path.join(dirpath, fn)).st_mtime_ns
                except OSError:
                    continue
                if mtime > newest:
                    newest = mtime
    tombs = 0
    tomb_newest = 0
    tdir = md / _TOMBSTONE_DIR
    if tdir.is_dir():
        for p in tdir.glob("*.tombstone.md"):
            tombs += 1
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                continue
            if mtime > tomb_newest:
                tomb_newest = mtime
    return (count, newest, tombs, tomb_newest)


def resolve(workspace_root: Path | str) -> ReleaseState:
    """Read the truth layer and decide what is released / sealed.

    Paths are returned as workspace-relative POSIX keys — the same strings
    ``chunks.path`` carries, so the caller can filter the index directly.
    """
    workspace = Path(workspace_root)
    memory_root = _memory_root(workspace)

    id_to_key: Dict[str, str] = {}
    supersedes_of: Dict[str, List[str]] = {}
    directly_released: Set[str] = set()
    sealed: Set[str] = set()
    status_released: Set[str] = set()

    for p in _iter_markdown(memory_root):
        key = _norm_key(p.relative_to(workspace).as_posix())
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            fm, _body = split_frontmatter(text)
        except Exception:
            continue  # malformed YAML: the inspector reports it, we skip it
        if not fm:
            continue
        eid = str(fm.get("id") or "")
        if eid:
            id_to_key[eid] = key
            sups = fm.get("supersedes")
            if isinstance(sups, list):
                supersedes_of[eid] = [str(s) for s in sups if s]
        if fm.get("status") == "released" or fm.get("released") is True:
            directly_released.add(eid)
            status_released.add(key)
        if fm.get("visibility") == "sealed":
            sealed.add(key)

    # The tombstone names the ORIGINAL path of the released element. That
    # file's own front-matter still says active (WR-11), so this is one of
    # the two places the original is known to be released.
    tombstoned: Set[str] = set()
    for p in _iter_tombstones(memory_root):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            fm, _body = split_frontmatter(text)
        except Exception:
            continue
        original = str(fm.get("original_path") or "")
        if not original:
            continue
        key = _norm_key(original)
        if not key.startswith("memory/"):
            # Tolerate a path relative to the memory root.
            key = _norm_key("memory/" + key)
        tombstoned.add(key)

    # Travel the supersedes chain: a released element takes its predecessors
    # with it (same content, earlier version), and any later revision of a
    # released element stays released (the facade does not carry `status`
    # forward on revise).
    released_ids = set(directly_released)
    changed = True
    while changed:
        changed = False
        for eid, sups in supersedes_of.items():
            if eid in released_ids:
                for s in sups:
                    if s and s not in released_ids:
                        released_ids.add(s)
                        changed = True
            elif any(s in released_ids for s in sups):
                released_ids.add(eid)
                changed = True

    released_paths = set(tombstoned) | set(status_released)
    for eid in released_ids:
        key = id_to_key.get(eid)
        if key:
            released_paths.add(key)

    # A released file seals itself too: the rule says a file whose
    # front-matter says status=released OR visibility=sealed leaves the
    # result set.
    sealed_paths = set(sealed) | set(status_released)

    return ReleaseState(
        released_paths=frozenset(released_paths),
        sealed_paths=frozenset(sealed_paths),
        signature=tree_signature(workspace),
    )


class ReleaseStateCache:
    """Resolve once, re-resolve only when the truth layer moved.

    The retrieval exit asks for the state on every query; re-reading and
    re-parsing every markdown file on every query would be a per-search
    cost, so the state is cached behind a cheap signature (file count +
    newest mtime of the truth layer and of the tombstone directory).
    """

    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root)
        self._state: ReleaseState = ReleaseState()

    def get(self) -> ReleaseState:
        signature = tree_signature(self.workspace_root)
        if signature != self._state.signature:
            state = resolve(self.workspace_root)
            # resolve() computed its own signature; keep the one we compared
            # against so the next call does not re-resolve on a stale value.
            self._state = ReleaseState(
                released_paths=state.released_paths,
                sealed_paths=state.sealed_paths,
                signature=signature,
            )
        return self._state
