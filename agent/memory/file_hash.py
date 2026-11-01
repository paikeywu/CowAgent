"""``files.hash`` — ONE comparison口径 for the file fingerprint.

T2d (2026-11-01) · the reading fork
====================================

**The measured fork.**  On the live corpus (2026-11-01, 16 libraries,
1193 ``files`` rows) the ``files.hash`` column is stored in **two
shapes**:

============  ======  ==========================  =====================
shape         rows   written by                   digest of
============  ======  ==========================  =====================
``sha256:`` + 509     ``rebuilder.rebuild_file``   canonical text
bare hex      684     ``manager`` scan path        **raw text**
============  ======  ==========================  =====================

**and the fork is on TWO axes, not one** — this is the part that
matters, because it means "strip the prefix" is NOT a sufficient fix:

* **axis 1 — the prefix**: ``sha256:<64hex>`` vs ``<64hex>``;
* **axis 2 — the digest basis**: the prefixed rows hash the
  **canonical** text (``canonical_text``), the bare rows hash the
  **raw** text (no CRLF folding, no blank-line folding, no strip).

⇒ **Stripping the prefix alone still yields a false "the file
changed"** for every row whose file carries CRLF endings or trailing
whitespace.  Measured on the live corpus: 68 of the 684 bare-hex rows
match only the RAW digest, not the canonical one.

**Why a comparison fork is a LIE, not an inconvenience.**  The ANCHOR
``~N``口径 (架构师 T1b, 定案) uses ``files.hash`` as the **baseline
fingerprint**: ``files.hash == the file's current hash`` ⇒ unchanged ⇒
compare as before; different ⇒ the file changed ⇒ **NOTRUN** (not
green — green would be lying; not FAIL either — the真因 is a stale
projection, which is DR-6's business, and pointing the fix elsewhere
is how you fix the wrong thing).

A baseline comparison is only sound if BOTH sides speak the same
dialect.  A reader that compares a ``sha256:``-prefixed stored value
against a bare computed digest reads **every prefixed row as
"changed"** — a false alarm on 509 of 1193 rows (42.6%).  That is the
"假的「文件已变」" the architect found.

**Why we do NOT wash the truth.**  Rewriting the stored values would
change the hash chain that every evidence row points at (see
``knowledge/concepts/washing-a-truth-source-rewrites-its-hash-chain.md``)
⇒ it would destroy the existing evidence.  So:

* **read side**: every comparison of a ``files.hash`` goes through
  :func:`hashes_equal` (or :func:`normalize_hash` + :func:`hash_matches`),
  which understands BOTH shapes and compares the **digest**, never the
  string;
* **write side**: new writes use ONE shape (:func:`canonical_file_hash`
  — prefixed, canonical basis), so the fork stops growing;
* **existing rows**: **left exactly as they are.**  The read side
  carries them.

**One module, both sides.**  This lives in its own module — NOT inside
``storage.py`` — because the **criterion side** has to import it too
(``acceptance/phase2_s3k.py`` compares a stored hash against a freshly
computed one), and because we have already paid the "private copy"
lesson once: two spellings of one rule is how the two writers drifted
apart in the first place (that is literally what happened here).
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from agent.memory.naming import canonical_text

#: The algorithm prefix used by the canonical form.
SHA256_PREFIX = "sha256:"

#: A bare sha256 digest (64 lowercase hex).
_BARE_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_file_hash(content: str) -> str:
    """The ONE shape new ``files.hash`` writes use.

    ``sha256:`` + sha256 of the CANONICAL text.  Canonical because a
    CRLF/LF conversion or a trailing-newline change must NOT read as
    "the file changed" — the memory system hashes canonical text
    everywhere else (``body_pointer.body_hash``, ``naming.content_hash8``),
    and a file-bookkeeping row that uses a different basis than the
    rest of the system is a second dialect.
    """
    return SHA256_PREFIX + hashlib.sha256(
        canonical_text(content).encode("utf-8")).hexdigest()


def normalize_hash(value: Optional[str]) -> Optional[str]:
    """Reduce a hash string to its DIGEST, dropping any algorithm prefix.

    ``None``/empty in, ``None`` out (an absent fingerprint is not the
    same as an empty one — it means "not measured").

    The prefix is dropped for COMPARISON only; the stored value is
    never rewritten.  Any ``<algo>:<hex>`` shape is accepted, because
    the rule that matters is "the same digest", not "the same string".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        algo, _, digest = text.partition(":")
        if algo and digest:
            return digest.strip().lower()
    return text.lower()


def hashes_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Are these two fingerprints the same digest?

    **This is the comparison entry point.**  It answers "are these the
    same?" — never "are these the same STRING?" — so a ``sha256:``
    prefix on one side and a bare digest on the other compares EQUAL
    when the digest is equal.

    Both ``None`` are **not** equal to each other: two absent
    fingerprints are the absence of evidence, not evidence of
    equality.  (A caller asking "did it change?" must handle the
    absent case itself — see :func:`hash_matches`.)
    """
    na, nb = normalize_hash(a), normalize_hash(b)
    if na is None or nb is None:
        return False
    return na == nb


def hash_matches(stored: Optional[str], content: Optional[str],
                 *, raw_bytes: Optional[bytes] = None) -> bool:
    """Does the stored ``files.hash`` match this file's CURRENT content?

    This is the **baseline fingerprint** comparison the ANCHOR ``~N``
    口径 needs, and it is tolerant of BOTH historical shapes:

    * the stored value may be ``sha256:``-prefixed or bare;
    * the stored value may hash the canonical text or the raw text.

    ⇒ the answer is "yes, it matches" if ANY of the admissible digest
    bases equals the stored digest.  A file that only differs from its
    stored fingerprint by a line-ending convention is **not** reported
    as changed.

    ``raw_bytes`` lets the caller pass the bytes it actually read, so
    the raw-basis variant can be computed without a second read.
    """
    want = normalize_hash(stored)
    if want is None or content is None:
        return False

    candidates = [hashlib.sha256(canonical_text(content).encode("utf-8")).hexdigest()]
    candidates.append(hashlib.sha256(content.encode("utf-8")).hexdigest())
    if raw_bytes is not None:
        candidates.append(hashlib.sha256(raw_bytes).hexdigest())
    return want in {c for c in candidates}


def hash_shape(value: Optional[str]) -> str:
    """Classify a stored hash: 'prefixed' | 'bare' | 'other' | 'absent'.

    Purely diagnostic — it exists so a report can say which dialect a
    row is in without guessing.
    """
    if value is None:
        return "absent"
    text = str(value).strip()
    if not text:
        return "absent"
    if text.startswith(SHA256_PREFIX):
        return "prefixed"
    if _BARE_RE.match(text):
        return "bare"
    return "other"


#: The name the PRODUCT code uses at the call site.  ``hash_matches``
#: is the descriptive name; this one says what it answers at the call
#: site ("does the stored fingerprint still match this file?").
file_hash_matches = hash_matches
