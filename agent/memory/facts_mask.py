"""Reading-view masking of the facts layer (P0-16 F-1).

The evidence room (``memory/facts/**.jsonl``) is APPEND-ONLY: its rows are
never rewritten, never deleted, and its row-level hash chain is never
broken. That is not negotiable (P0-1 §3, WR-14) — and it used to mean that
a released element's 感受 (the most private of the nine elements) stayed
readable through the evidence room, because "released" was only enforced on
the markdown layer.

SEED 拍板 (P0-16 F-1): the disk does not move, the READ VIEW masks.

  * a released element's feeling rows are replaced, in the view only, by a
    fixed placeholder line;
  * the placeholder leaks nothing: not the 原话, not its length, not a
    keyword, not a count of what was withheld;
  * the facts file's bytes, line count, ``seq`` and hash chain are
    untouched — the gate ② ("事实层不可改写") keeps holding.

The masking rule itself is NOT here — it is decided by
``agent.memory.visibility.decide_read`` (the one visibility judgement) and
travels to the read exits as the set of released fact element ids. This
module only knows how to RENDER that decision over a facts file.
"""

from __future__ import annotations

import json
from typing import Iterable, List, Optional, Set

#: The facts layer lives under this path prefix (workspace-relative).
FACTS_DIR = "memory/facts/"

#: The placeholder that replaces a released row in the reading view.
#:
#: It is FIXED (not derived from the original row) so it cannot leak the
#: 原话's length, wording or keywords, and it carries NO count of what was
#: withheld (a count is itself a leak — G4-d).
RELEASED_ROW_STATUS = "released"
RELEASED_ROW_NOTE = "该条已释放"


def is_facts_path(path: str) -> bool:
    """True when ``path`` is a file of the evidence room."""
    key = str(path or "").replace("\\", "/").lstrip("/")
    return key.startswith(FACTS_DIR)


def placeholder_line(element_id: str) -> str:
    """The one line that stands in for a released row.

    It carries the element id (which the caller already knows — it is the
    id of a released element, and knowing WHICH row was released is not
    the secret; the 原话 is) and nothing else.
    """
    return json.dumps(
        {
            "element_id": str(element_id),
            "status": RELEASED_ROW_STATUS,
            "note": RELEASED_ROW_NOTE,
        },
        ensure_ascii=False,
    )


def mask_facts_text(text: str, released_fact_ids: Optional[Iterable[str]],
                    *, element_id_of=None) -> str:
    """Return ``text`` with every released row replaced by a placeholder.

    ``text`` is the CONTENT OF A FACTS FILE as it would be served. Every
    line that parses as a JSON object and whose element id belongs to a
    released element is replaced by :func:`placeholder_line`.

    ``element_id_of`` is the callable that maps a parsed row to its element
    id. It defaults to the facts-layer rule (a feeling row's id comes from
    its ``run_id``, any other row's id is derived from its content — see
    ``agent.memory.facts_refs``).

    Lines that are not JSON objects are passed through UNCHANGED: this
    function masks content, it does not validate the evidence room (that is
    the inspector's job, and it reports on the disk, not on the view).
    """
    mask: Set[str] = set(released_fact_ids or ())
    if not mask:
        return text
    if element_id_of is None:
        from agent.memory.facts_refs import row_element_id

        element_id_of = row_element_id
    out: List[str] = []
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped:
            out.append(raw)
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            out.append(raw)
            continue
        if not isinstance(row, dict):
            out.append(raw)
            continue
        element_id = element_id_of(row)
        if element_id and element_id in mask:
            out.append(placeholder_line(element_id))
        else:
            out.append(raw)
    return "\n".join(out)
