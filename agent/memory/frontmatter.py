"""Front-matter splitting — one implementation, no cycles.

``split_frontmatter`` used to live in ``agent/memory/facade.py``, but the
release-state resolver needs to read front-matter too, and the facade needs
the release-state resolver. Importing the facade from the resolver would
make the two modules circular, so the parsing primitive moved here: this
module depends on nothing but PyYAML, and both sides import it.

A front-matter block is ``---\\n<yaml>\\n---\\n``. Anything else (no
opening fence, no closing fence) is treated as "no front-matter" and the
whole text is the body — a truth file without front-matter is not an error
here, it is simply not a sealed/released element.

Malformed YAML RAISES, exactly as it did in the facade: the shelf catches it
and reports the file as ``invalid-frontmatter`` (P0-8: a file that cannot be
rendered must say so, not vanish). Readers that must not crash — the
release-state resolver — catch it themselves.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import yaml


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split ``---\\n<yaml>\\n---\\n<body>``.

    Returns ``({}, text)`` when the file carries no front-matter.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm = yaml.safe_load(text[4:end]) or {}
    return fm, text[end + 5:]
