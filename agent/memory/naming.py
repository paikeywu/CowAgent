"""Live-memory naming: ``<slug>--<hash8>.md`` under a fixed container.

Single source of truth for how a live-memory markdown file gets its name.
Design: Design-Documents/live-memory/2026-10-26-生命态记忆体系-v2/
  - 02 §5.3 (slug + 8-char content hash, <=100 UTF-8 bytes total)
  - 06 §1.3.2 (unified form, byte budget, truncation order, validator regex)

Rules that matter and why:

- **Bytes, not characters.** NTFS/ext4 limit one path component to 255 bytes
  and a CJK char costs 3, so every budget here is UTF-8 bytes.
- **hash8 is never truncated.** The truncation ladder cuts the slug first;
  the hash is the human-visible fingerprint of the content (and matches the
  existing ``add_memory`` convention in manager.py).
- **The regex anchors the hash at the end.** Slugs may themselves contain
  ``--`` (relations do: ``peiqi--agent-mvnerelm``), so only the LAST
  ``--<8hex>`` before ``.md`` is the hash.
- **filenames are not identity.** The real id lives in front-matter
  (``element_id``); the name only has to be readable, unique, and safe.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Budgets (UTF-8 bytes)
# ---------------------------------------------------------------------------

PATH_MAX_BYTES = 100        # whole path relative to the workspace root
SLUG_MAX_BYTES = 40         # slug segment only
COMPONENT_MAX_BYTES = 255   # filesystem per-component hard limit

# Fixed containers (06 §1.3.2: enum, no invention). Dated containers get a
# YYYY-MM-DD segment; entity containers never do (they span time).
CONTAINERS_DATED = ("events", "letters")
CONTAINERS_UNDATED = ("entities/people", "entities/relations")
CONTAINERS = CONTAINERS_DATED + CONTAINERS_UNDATED

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Filename pattern for the truth layer (memory/markdown/**), keyed on the
# FILENAME only (no directory). The hash group must anchor at the end so a
# slug containing "--" (relations) is not split at the wrong place.
NAME_RE = re.compile(
    r"^(?P<slug>[\w一-龥][\w一-龥-]{0,200})--(?P<hash>[0-9a-f]{8})(?:-(?P<rev>\d+))?\.md$"
)

# What the slug normalizer keeps: ASCII word chars + dash, and CJK. Anything
# else becomes a dash. This also washes out "..", "/", "\\" and ":" (Windows
# drive syntax), so a slug can never escape its container.
_SLUG_KEEP_RE = re.compile(r"[^a-z0-9一-龥-]+")
_SLUG_DASHES_RE = re.compile(r"-{2,}")


def canonical_text(content: str) -> str:
    """Normalize content before hashing: LF endings, outer whitespace stripped,
    runs of blank lines folded to one. Same meaning must hash the same."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def content_hash8(content: str) -> str:
    """8-hex content fingerprint; md5 matches manager.add_memory's convention."""
    return hashlib.md5(canonical_text(content).encode("utf-8")).hexdigest()[:8]


def _truncate_bytes(text: str, max_bytes: int) -> str:
    """Cut to <= max_bytes UTF-8 bytes without splitting a multi-byte char."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes]
    # Back off while we land inside a UTF-8 continuation sequence.
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore")


def normalize_slug(slug: str, *, max_bytes: int = SLUG_MAX_BYTES) -> str:
    """Lowercase, wash unsafe chars to '-', collapse dashes, byte-truncate.

    Returns "" when nothing usable survives (e.g. a purely symbolic title) —
    the caller falls back to the hash as the slug.
    """
    safe = _SLUG_KEEP_RE.sub("-", slug.lower())
    safe = _SLUG_DASHES_RE.sub("-", safe).strip("-")
    safe = _truncate_bytes(safe, max_bytes).strip("-")
    return safe


def build_memory_path(
    container: str,
    slug: str,
    content: str,
    *,
    date: Optional[str] = None,
    revision: Optional[int] = None,
) -> str:
    """Deterministic workspace-relative path for a truth-layer memory file.

    ``memory/markdown/<container>/[<date>/]<slug>--<hash8>[-<rev>].md``

    - ``container`` must be one of CONTAINERS (enum, no invention).
    - ``date`` is required for events/letters, forbidden for entities —
      entities and relations span time, so they carry no date segment.
    - ``revision`` (2, 3, ...) disambiguates a same-slug different-content
      collision; the writer (P0-4) picks it, the validator accepts it.
    - Truncation ladder: slug first; if still over budget, degrade to the
      container's last segment as the slug. hash8 is never cut.

    Raises ValueError with the actual byte length when even the degraded
    form cannot fit — never silently truncates past the contract.
    """
    if container not in CONTAINERS:
        raise ValueError(
            f"unknown container {container!r}; must be one of {CONTAINERS}"
        )
    if container in CONTAINERS_DATED:
        if not date or not _DATE_RE.match(date):
            raise ValueError(
                f"container {container!r} requires date=YYYY-MM-DD, got {date!r}"
            )
    elif date is not None:
        raise ValueError(
            f"container {container!r} spans time and takes no date segment"
        )

    h = content_hash8(content)
    safe = normalize_slug(slug) or h  # no usable slug -> hash alone
    rev = f"-{revision}" if revision else ""

    def _assemble(slug_part: str) -> str:
        segments = ["memory", "markdown"] + container.split("/")
        if date:
            segments.append(date)
        segments.append(f"{slug_part}--{h}{rev}.md")
        return "/".join(segments)

    path = _assemble(safe)
    if len(path.encode("utf-8")) > PATH_MAX_BYTES:
        # Ladder step 2: degrade the slug to the container's last segment.
        path = _assemble(container.rsplit("/", 1)[-1])
    nbytes = len(path.encode("utf-8"))
    if nbytes > PATH_MAX_BYTES:
        raise ValueError(
            f"path still {nbytes} bytes after slug degradation (limit "
            f"{PATH_MAX_BYTES}): {path!r}"
        )
    return path


def check_name(name: str) -> Optional[str]:
    """Validate ONE filename (no directory) against the truth-layer contract.

    Returns None when valid, else a human-readable reason. Kept separate from
    the path-level checks so the validator can report precisely what failed.
    """
    nbytes = len(name.encode("utf-8"))
    if nbytes > COMPONENT_MAX_BYTES:
        return f"component {nbytes} bytes > {COMPONENT_MAX_BYTES}"
    m = NAME_RE.match(name)
    if not m:
        return "does not match <slug>--<hash8>.md"
    slug_bytes = len(m.group("slug").encode("utf-8"))
    if slug_bytes > SLUG_MAX_BYTES:
        return f"slug {slug_bytes} bytes > {SLUG_MAX_BYTES}"
    return None


def check_relpath(relpath: str) -> Optional[str]:
    """Validate a workspace-relative POSIX path: total byte budget plus the
    per-component filesystem limit. Layer/pattern rules are the caller's job.
    """
    nbytes = len(relpath.encode("utf-8"))
    if nbytes > PATH_MAX_BYTES:
        return f"path {nbytes} bytes > {PATH_MAX_BYTES}"
    for part in relpath.split("/"):
        pbytes = len(part.encode("utf-8"))
        if pbytes > COMPONENT_MAX_BYTES:
            return f"component {part[:32]!r}... {pbytes} bytes > {COMPONENT_MAX_BYTES}"
    return None
