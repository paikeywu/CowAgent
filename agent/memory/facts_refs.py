"""Facts-layer element ids and the resolver that turns a ref into 原话.

P0-1 §4.1 defines ``refs`` as a list of fact-layer element ids (``f_*``),
and 06 §3.1 defines an element id as ``<layer前缀> + sha256(canonical_text)[:20]``
— i.e. **derived from the element's own text**, not stored in a column. A
fact row therefore has no id field (P0-1 §3.2 fixes the row to ten fields in
a fixed order), and the id is recomputed from the row's ``content``.

That is what this module does:

  * ``fact_element_id(content)`` — the id of a fact row, derived from its
    content exactly the way 06 §3.1 prescribes;
  * ``FactsIndex`` — a scan of ``memory/facts/**/*.jsonl`` that maps every
    id to the row behind it (file, line number, content);
  * ``resolve_ref`` — one ref -> one row, or None.

Two shapes of ref are understood, because the ecosystem has produced both:

  * ``f_<20 hex>``            — the element id (P0-1's form);
  * ``f_<path>#L<n>``         — a path anchor into the evidence room, the
    form the P0-5 shelf generator's gap rule already understood.

A ref that resolves to neither is DANGLING, and the shelf reports it as a
gap (INV-1). Nothing here guesses: the id must match a row that exists.

TWO ID SPACES, ONE DIGEST — read this before you "fix" an id (P0-14 附带):

``fact_element_id(c)`` and ``feeling_element_id(c)`` hash the SAME content
with the SAME sha256 and take the SAME first 20 hex digits. They differ
only in the prefix::

    fact    = f_909a65ef96c3c090a8ab
    feeling = f_feeling_909a65ef96c3c090a8ab

So a feeling row and a plain fact row carrying the SAME text are
**twins at the digest level**. The two id spaces cannot collide as ids
(``f_feeling_`` is not a legal ``f_`` id — ``g``/``l``/``i``/``n`` are not
hex characters, so ``f_<20 hex>`` never matches a feeling id), and
``FactsIndex`` keys on the full id, so the rows never overwrite each
other. But if anyone ever strips the prefix to match ids, a feeling row and
its twin fact row WILL look like the same element. Strip prefixes only
when you also compare the prefix namespace.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from agent.memory.facts_mask import RELEASED_ROW_NOTE
from agent.memory.naming import canonical_text

FACT_ID_PREFIX = "f_"
#: ``run_id`` of a fact row that carries a FEELING element. The facts row
#: format is fixed at ten fields by P0-1 §3.2, so the layer of a row cannot
#: be a field of its own without breaking that contract — it rides on
#: ``run_id``, which is the one field that is free-form and part of the
#: line hash.
FEELING_RUN_PREFIX = "feeling:"
#: The element id of a feeling element is derived from its own text, the
#: same rule every other layer uses (06 §3.1).
FEELING_ELEMENT_PREFIX = "f_feeling_"


def fact_element_id(content: str) -> str:
    """The element id of a fact row, derived from its content."""
    digest = hashlib.sha256(canonical_text(content).encode("utf-8")).hexdigest()
    return FACT_ID_PREFIX + digest[:20]


def feeling_element_id(content: str) -> str:
    """The element id of a feeling element (a distinct id space)."""
    digest = hashlib.sha256(canonical_text(content).encode("utf-8")).hexdigest()
    return FEELING_ELEMENT_PREFIX + digest[:20]


def is_feeling_run(run_id: str) -> bool:
    return str(run_id or "").startswith(FEELING_RUN_PREFIX)


def feeling_run_id(element_id: str) -> str:
    """The ``run_id`` a feeling fact row carries."""
    return FEELING_RUN_PREFIX + element_id


def row_element_id(row: dict) -> str:
    """The element id of one parsed fact row.

    Same rule as :func:`build_facts_index`: a feeling row's id comes from
    its content (like every other layer's), and a plain fact row's id is
    the sha256 of its own content. One definition — the index and the
    reading view must never disagree about which row is which.
    """
    content = str(row.get("content") or "")
    if is_feeling_run(str(row.get("run_id") or "")):
        return feeling_element_id(content)
    return fact_element_id(content)


def row_element_ids(row: dict) -> List[str]:
    """Every id that identifies this row — the derived one, plus the id the
    row's own ``run_id`` claims when it is a feeling row.

    The two agree for an untampered row (the run id carries the same
    digest). When they disagree the row was rewritten, and the reading
    view masks it if EITHER id says "released" — the safe direction.
    """
    out: List[str] = []
    derived = row_element_id(row)
    if derived:
        out.append(derived)
    run_id = str(row.get("run_id") or "")
    if is_feeling_run(run_id):
        claimed = run_id[len(FEELING_RUN_PREFIX):].strip()
        if claimed and claimed not in out:
            out.append(claimed)
    return out


@dataclass(frozen=True)
class FactRow:
    """One fact row, with where it lives and what it says."""

    element_id: str
    path: str          # workspace-relative POSIX path
    seq: int
    line: int          # 1-based physical line number in the file
    content: str
    role: str
    run_id: str
    layer: str         # "fact" | "feeling"

    @property
    def is_feeling(self) -> bool:
        return self.layer == "feeling"


class FactsIndex:
    """Every fact row of one workspace, indexed by element id."""

    def __init__(self) -> None:
        self._by_id: Dict[str, FactRow] = {}
        self._by_path: Dict[str, List[FactRow]] = {}

    def __len__(self) -> int:
        return len(self._by_id)

    def rows(self) -> List[FactRow]:
        return list(self._by_id.values())

    def get(self, element_id: str) -> Optional[FactRow]:
        return self._by_id.get(element_id)

    def rows_in(self, path: str) -> List[FactRow]:
        return self._by_path.get(path, [])

    def add(self, row: FactRow) -> None:
        self._by_id.setdefault(row.element_id, row)
        self._by_path.setdefault(row.path, []).append(row)

    def resolve(self, ref: str) -> Optional[FactRow]:
        """One ref -> the row behind it, or None (dangling)."""
        if not isinstance(ref, str) or not ref:
            return None
        ref = ref.strip()
        # ① the element-id form
        row = self._by_id.get(ref)
        if row is not None:
            return row
        # ② the path-anchor form: f_<path>#L<n>
        target = ref[2:] if ref.startswith(FACT_ID_PREFIX) else ref
        path_part, _, anchor = target.partition("#")
        if not path_part:
            return None
        candidates = self.rows_in(path_part)
        if not candidates:
            return None
        if anchor.upper().startswith("L"):
            try:
                want = int(anchor[1:])
            except ValueError:
                return None
            for row in candidates:
                if row.seq == want or row.line == want:
                    return row
            return None
        return candidates[0] if len(candidates) == 1 else None


def build_facts_index(workspace_root: Path | str,
                      mask_ids: Optional[Iterable[str]] = None) -> FactsIndex:
    """Scan ``memory/facts/**/*.jsonl`` and index every row by element id.

    Read-only, and tolerant: an unparseable line is skipped (it is the
    inspector's violation to report, not the reader's to crash on).

    ``mask_ids`` (P0-16 F-1) are the element ids of rows that belong to
    RELEASED elements. Their CONTENT is replaced by a placeholder in this
    index — the disk is untouched; only what a reader sees changes. A
    caller that only needs to know WHICH rows exist (the inspector, the
    chain check) passes nothing and sees the real content.
    """
    workspace = Path(workspace_root)
    facts_root = workspace / "memory" / "facts"
    index = FactsIndex()
    if not facts_root.is_dir():
        return index
    mask = set(mask_ids or ())
    for path in sorted(facts_root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, raw in enumerate(text.splitlines(), start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            content = str(row.get("content") or "")
            run_id = str(row.get("run_id") or "")
            feeling = is_feeling_run(run_id)
            element_id = (feeling_element_id(content) if feeling
                          else fact_element_id(content))
            if mask and any(i in mask for i in row_element_ids(row)):
                # The row is released: the reader sees the placeholder, not
                # the 原话. The id is kept — which row was released is not
                # the secret; what it said is.
                content = RELEASED_ROW_NOTE
            index.add(FactRow(
                element_id=element_id,
                path=rel,
                seq=int(row.get("seq") or 0),
                line=line_no,
                content=content,
                role=str(row.get("role") or ""),
                run_id=run_id,
                layer="feeling" if feeling else "fact",
            ))
    return index
