"""Release state — resolved from the DISK, never from the index.

P0-11 P-2 (high): after a release, the released content was still returned by
the regular retrieval exit, anonymously. The root cause was that nothing knew
what "released" meant at retrieval time: the storage layer only had
scope + user_id, the tool layer had no filter at all, and ``chunks.metadata``
was always None.

The rules this module implements (SEED 裁决 2026-10-27, P0-14 L-2/L-3):

  * The TRUTH layer keeps the content — release is not deletion (遗忘伦理).
    The PROJECTION layer must hide it.
  * The authority is the DISK: ``memory/markdown/_tombstones/*.tombstone.md``
    plus the front-matter of the released file's new version
    (``status=released`` / ``type=release``). Whatever the index says is
    only a projection, and it has to be correct again after a rebuild.
  * TWO DISTINCT exclusions, because they are two different facts:
      1. the ELEMENT is released → the whole element leaves every read
         exit, for every identity, including the owner's;
      2. the FILE is a DARK PAGE (``visibility: sealed`` and NOT a
         release) → it stays in the index under its own scope, and only
         its owner may read it.
  * A file whose front-matter cannot be read at all is treated as
    RELEASED (hidden). We cannot know whether it was released, and
    "cannot tell" must not mean "serve it" (P0-13 L-3: fail closed).

P0-13 L-3: this module FAILS LOUDLY. A truth-layer file that cannot be read
raises — the caller (the manager) then fails the retrieval instead of
falling back to a stale projection. A privacy control that breaks open is
worse than one that breaks shut.

Everything here is read-only. It reads files; it never writes, and it never
edits the index (that is the manager's job, and the inspector's is only to
report).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from agent.memory.frontmatter import split_frontmatter
from agent.memory.visibility import (
    READ_ALLOW,
    READ_RELEASED,
    READ_SEALED,
    VIS_OPEN,
    VIS_RELEASED,
    VIS_SEALED,
    classify_frontmatter,
    owner_token,
)

# The truth layer's markdown root, relative to the memory root.
_MARKDOWN_DIR = "markdown"
_TOMBSTONE_DIR = "_tombstones"


class ReleaseStateError(RuntimeError):
    """The truth layer could not be resolved.

    Raised instead of a silent "nothing is released" — the caller decides
    what to do, and the only safe thing to do with an unknown release
    state is to serve nothing.
    """


@dataclass(frozen=True)
class ReleaseState:
    """Which index paths must not be returned by the regular retrieval exit.

    ``released_paths`` — the element behind this path was released.
                    Invisible to EVERY identity, on every read exit.
    ``sealed_paths``  — this path is a DARK PAGE (暗页). It stays in the
                    index; only the owner may read it.
    ``sealed_owner``  — path → owner token for the dark pages.
    ``released_fact_ids`` — the fact-layer element ids (the 感受 rows of
                    released elements) that must be MASKED in the reading
                    view (P0-16 F-1). The rows themselves stay on disk —
                    the evidence room is append-only — but no read exit
                    may hand out their 原话.

    The path sets hold INDEX KEYS: workspace-relative POSIX paths, exactly
    the strings ``chunks.path`` carries. The fact ids are ELEMENT ids
    (``f_*``), the same strings a released element's ``refs`` carries.
    """

    released_paths: FrozenSet[str] = frozenset()
    sealed_paths: FrozenSet[str] = frozenset()
    sealed_owner: Mapping[str, str] = field(default_factory=dict)
    released_fact_ids: FrozenSet[str] = frozenset()
    signature: Tuple = ()

    def __hash__(self) -> int:
        # The owner map is a dict (unhashable), so the hash is taken over
        # the sets only — the state is used as a cache value, never as a
        # dict key, but a frozen dataclass must still be hashable to be
        # usable in one.
        return hash((self.released_paths, self.sealed_paths, self.signature))

    def element_released(self, path: str) -> bool:
        return path in self.released_paths

    def file_sealed(self, path: str) -> bool:
        return path in self.sealed_paths

    def owner_for(self, path: str) -> Optional[str]:
        return (self.sealed_owner or {}).get(path)

    def flags_for(self, path: str) -> Tuple[int, int]:
        """``(element_released, file_sealed)`` as 0/1 for the chunk columns."""
        return (1 if self.element_released(path) else 0,
                1 if self.file_sealed(path) else 0)

    def decide(self, path: str, tokens) -> Tuple[str, str, FrozenSet[str]]:
        """``(decision, reason, released_fact_ids)`` for a read exit.

        One derivation, no drift. The third element is the set of fact
        rows the reading view must mask (P0-16 F-1) — it travels WITH the
        decision so a read exit cannot apply a rule of its own.
        """
        from agent.memory.visibility import decide_read

        return decide_read(
            path,
            released_paths=self.released_paths,
            sealed_paths=self.sealed_paths,
            sealed_owner=self.sealed_owner,
            tokens=tokens,
            released_fact_ids=self.released_fact_ids,
        )


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


def release_mismatches(workspace_root: Path | str) -> Dict[str, List[str]]:
    """Detect front-matter / tombstone DISAGREEMENT about what is released.

    The release record is written in TWO places: the tombstone under
    ``_tombstones/`` (the authoritative release record — WR-11 says the
    original file's front-matter is never rewritten) and the new version's
    front-matter (``status=released``). ``resolve()`` merges both into
    ``released_paths`` and never says whether they agreed.

    But the front-matter is editable (风控 attack surface): someone can strip
    ``status=released`` off a file to make it resurface, and the merge alone
    would quietly accept that as long as the tombstone still hides it — a
    silent disagreement the projection then faithfully reproduces. This
    function names the disagreement so the rebuilder / inspector can report
    it RED instead of papering over it.

    Returns a dict with two keys:
      ``tombstone_only`` — paths a tombstone releases but whose current
          front-matter does NOT say released (suspect: front-matter stripped).
      ``frontmatter_only`` — paths whose front-matter says released but for
          which no tombstone exists (suspect: release without a record).

    Both are reported; neither is silently trusted. Read-only, never writes.
    """
    workspace = Path(workspace_root)
    memory_root = _memory_root(workspace)

    fm_released: Set[str] = set()
    for p in _iter_markdown(memory_root):
        key = _norm_key(p.relative_to(workspace).as_posix())
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable is resolve()'s fail-closed job, not ours
        try:
            fm, _body = split_frontmatter(text)
        except Exception:
            continue
        if fm and classify_frontmatter(fm) == VIS_RELEASED:
            fm_released.add(key)

    tomb_released: Set[str] = set()
    for p in _iter_tombstones(memory_root):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            fm, _body = split_frontmatter(text)
        except Exception:
            continue
        if not fm:
            continue
        original = str(fm.get("original_path") or "")
        if not original:
            continue
        key = _norm_key(original)
        if not key.startswith("memory/"):
            key = _norm_key("memory/" + key)
        tomb_released.add(key)

    return {
        "tombstone_only": sorted(tomb_released - fm_released),
        "frontmatter_only": sorted(fm_released - tomb_released),
    }


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

    Fail-closed (P0-13 L-3): a truth-layer file that cannot be READ
    raises. The caller must fail the retrieval rather than serve content
    under a stale "nothing is released" marker.
    """
    workspace = Path(workspace_root)
    memory_root = _memory_root(workspace)

    id_to_key: Dict[str, str] = {}
    supersedes_of: Dict[str, List[str]] = {}
    directly_released: Set[str] = set()
    sealed: Set[str] = set()
    sealed_owner: Dict[str, str] = {}
    status_released: Set[str] = set()
    # P0-16 F-1: the fact-layer element ids each truth file points at.
    # A released element's 感受 rows live in the evidence room, and the
    # element's own ``refs`` are the only pointer to them.
    refs_of: Dict[str, List[str]] = {}

    for p in _iter_markdown(memory_root):
        key = _norm_key(p.relative_to(workspace).as_posix())
        # A file we cannot read is a file whose visibility we cannot know.
        # Treating it as released is the only safe reading (fail closed).
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ReleaseStateError(
                f"cannot read the truth-layer file {key!r}: {e}") from e
        try:
            fm, _body = split_frontmatter(text)
        except Exception:
            # Malformed YAML: we cannot read its visibility either, so it
            # is treated as released (hidden) — the inspector reports the
            # malformed front-matter separately.
            fm = None
        if not fm:
            if text.startswith("---\n"):
                # The file CLAIMS to have front-matter we cannot parse.
                status_released.add(key)
            continue
        eid = str(fm.get("id") or "")
        if eid:
            id_to_key[eid] = key
            sups = fm.get("supersedes")
            if isinstance(sups, list):
                supersedes_of[eid] = [str(s) for s in sups if s]
        refs = fm.get("refs")
        if isinstance(refs, list):
            refs_of[key] = [str(r) for r in refs if r]
        kind = classify_frontmatter(fm)
        if kind == VIS_RELEASED:
            if eid:
                directly_released.add(eid)
            status_released.add(key)
        elif kind == VIS_SEALED:
            sealed.add(key)
            owner = owner_token(fm)
            if owner:
                sealed_owner[key] = owner

    # The tombstone names the ORIGINAL path of the released element. That
    # file's own front-matter still says active (WR-11), so this is one of
    # the two places the original is known to be released.
    tombstoned: Set[str] = set()
    for p in _iter_tombstones(memory_root):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ReleaseStateError(
                f"cannot read the tombstone {p.name!r}: {e}") from e
        try:
            fm, _body = split_frontmatter(text)
        except Exception:
            continue
        if not fm:
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

    # A released file is never a dark page: the release wins. (P0-13 L-2:
    # treating every sealed file as a release is what hid dark pages from
    # their own owner.)
    sealed_paths = {p for p in sealed if p not in released_paths}
    sealed_owner = {k: v for k, v in sealed_owner.items()
                    if k in sealed_paths}

    # P0-16 F-1: the fact rows of released elements. The released element's
    # own front-matter names them (``refs`` → ``f_*`` ids, P0-1 §4.1); the
    # rows themselves stay on disk (the evidence room is append-only), but
    # a read exit must mask them in the view.
    released_fact_ids: Set[str] = set()
    for path in released_paths:
        for ref in refs_of.get(path, ()):
            released_fact_ids.add(ref)

    return ReleaseState(
        released_paths=frozenset(released_paths),
        sealed_paths=frozenset(sealed_paths),
        sealed_owner=sealed_owner,
        released_fact_ids=frozenset(released_fact_ids),
        signature=tree_signature(workspace),
    )


class ReleaseStateError(RuntimeError):
    """The truth layer could not be resolved.

    Raised instead of a silent "nothing is released" — the caller decides
    what to do, and the only safe thing to do with an unknown release
    state is to serve nothing.
    """


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
                sealed_owner=state.sealed_owner,
                released_fact_ids=state.released_fact_ids,
                signature=signature,
            )
        return self._state
