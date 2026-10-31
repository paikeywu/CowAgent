"""Credential masking for the messages truth source (F-5 · C-1).

WHY THIS MODULE EXISTS
-----------------------
On 2026-10-31 the risk survey measured a real leak: the SSH password of
``192.168.0.114`` was found in **109 files** — session exports, the live
WAL, backup databases, temporary scripts and deliverables.  The root
cause is NOT that the export layer "wrote credentials into the export".
The export layer was a pure courier.  The credential was in the message
from the start, because ``sshpass -p '<v>'`` puts the password into
**argv**, argv is recorded in the bash tool's ``tool_use.input.command``,
and the tool call is stored as conversation content.

So there are two layers to fix, and this module is the ONE implementation
both of them use:

* ``build_msg_line`` (the export layer's throat) — the JSONL chain;
* ``ConversationStore.add_messages`` (the database layer's throat) — the
  ``messages.content`` projection.

Two implementations would be two normalisers, and two normalisers are how
the same text hashes two different ways — the exact failure
``agent/memory/msg_chain.py`` warns about in its own header.

THE FORMS ARE ENUMERATED, NOT GUESSED
-------------------------------------
The survey's own method note is the reason this module looks the way it
does: the first scan used a value class that EXCLUDED the quote
characters, so on the machine's actual shape —

    sshpass -p '<v>' ssh ...

— the regex stopped at the quote and the whole credential was skipped.
The measured result was "0 hits" on one scan and "109 files" on another.
The difference was not the data; it was the DELIMITER FORM.

So every shape below is enumerated **with its delimiters**, and the value
class of each rule deliberately INCLUDES the quote so that the quoted and
unquoted shapes are both caught.  See
``knowledge/concepts/credential-delimiter-forms-are-a-morphology.md``.

The rule set covers:

======  ==================================================================
 S-1    ``sshpass -p <v>``            (unquoted)
 S-2    ``sshpass -p '<v>'`` / ``"<v>"``   (quoted — this machine's shape)
 S-3    ``curl``/``wget`` ``-u <v>`` / ``--user <v>``
 S-4    ``password=<v>`` / ``passwd=`` / ``pwd=``   (kv, no space)
 S-5    ``password="<v>"`` / ``password='<v>'``     (kv, quoted)
 S-6    ``"<password>": "<v>"``       (JSON field, incl. the escaped form)
 S-7    ``scheme://user:<v>@host``    (URL userinfo)
 S-8    ``SSHPASS=<v>`` / ``<PREFIX>_PASSWORD=<v>``
 S-9    ``export VAR=<v>``
 S-10   ``PASS = "<v>"``              (bare identifier assignment)
======  ==================================================================

**S-10 is not in the survey's table, and it is the shape this machine
actually uses** — ``cow/tmp/remote_exec.py:11``, ``sync_identity.py:21``
and ``sync_team.py:26`` all carry ``PASS = "<v>"``.  A rule set copied
from the survey would have missed every one of them.

THE PLACEHOLDER RULE
--------------------
A masker that rewrites ``password="<your-password>"`` corrupts ordinary
documentation and changes every hash that depends on it.  Placeholders
are therefore NOT credentials and are left alone: ``<...>``, ``***``,
``REDACTED``, ``your_password``, ``example``, ``placeholder``, ``${VAR}``
and ``$VAR``.

THE NORMALISER QUESTION (answered by measurement, not by assumption)
-------------------------------------------------------------------
The survey requires the masker to be "同源" with ``canonical_text``,
because a masker that rewrites the text would otherwise introduce a
SECOND normaliser.  Measured on 2026-10-31: ``canonical_text`` is
imported by ``msg_chain.py`` but **never called** — the import carries a
``# noqa: F401 (imported, per C-2)`` marker.  The messages chain hashes
with ``_canonical_json`` (JSON canonicalisation), not with
``canonical_text``.  So ``canonical_text`` is not on this path at all, and
the masker does not have to agree with it.

What the masker DOES have to guarantee is that the hash and the stored
text describe the same bytes.  That is why masking happens BEFORE
``msg_line_hash`` — see ``build_msg_line``.  Masking after the hash would
make every read raise ``ContentDriftError``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

#: The single replacement marker.  It is deliberately angle-bracketed so
#: that the placeholder rule exempts it: masking is IDEMPOTENT.
REDACTED = "<REDACTED>"

#: The minimum length of a value that may be a credential.  Shorter values
#: are far more likely to be ordinary words ("pass", "none", "true").
MIN_VALUE_LEN = 4


# ---------------------------------------------------------------------------
# The value classes
# ---------------------------------------------------------------------------

#: A quoted value: single- or double-quoted, no quote of the same kind
#: inside.  Used by the quoted shapes.
_Q = r"'[^'\n]*'|\"[^\"\n]*\""

#: A bare value: no whitespace, no quote, no shell separator.
#:
#: **The leading backslash is excluded** because a value that STARTS with
#: an escape is not a bare value — it is an escaped one, and the quoted
#: rules own that case.  Measured 2026-10-31: without this, the
#: unquoted rule matched the escaped backslash run that precedes a
#: JSON-escaped quoted value and stopped there, so the credential
#: BEHIND it was never seen.
_BARE = r"(?<!\\)[^\s'\";|&`\\]+"

#: A value inside a JSON string: any character that is not a quote or a
#: backslash, OR an escaped pair.  Used by the JSON-field shape.
_JSON_VAL = r"(?:[^\"\\]|\\.){4,}?"

#: **The escaped-delimiter problem** (found by measurement, 2026-10-31).
#:
#: A message's ``content`` is JSON TEXT — the database stored
#: ``json.dumps(content)`` — so a credential that was written with double
#: quotes arrives with ESCAPED quotes:
#:
#:     sshpass -p \"SYNTHETIC-CREDENTIAL-DO-NOT-USE\" ssh host
#:
#: A rule that looks for a plain ``"`` therefore MISSES it: the
#: character after the value is ``\`` and then ``"``.  This is the same
#: class of bug as the survey's own "the value class excluded the
#: quote" — one delimiter layer deeper, and found only by running the
#: masker over the REAL corpus instead of a hand-written sample.
#:
#: **And the escape DEPTH is not fixed.**  Every JSON round-trip adds a
#: layer: the tool call's text is dumped once into the message, and the
#: message's content is dumped again into the JSONL line.  A rule that
#: accepts exactly one backslash catches the first layer and misses the
#: second — so the delimiter classes accept a RUN of backslashes, not a
#: fixed count.
_ESC = r"(?:\\)*"

#: A quoted value: the opening delimiter may be preceded by any number of
#: backslashes (the escape depth), and the closing delimiter must match
#: the opening one.
_QOPEN = _ESC + r"(?P<q>['\"])"
#: **The two alternatives are DISJOINT, and that is load-bearing.**
#:
#: The original was ``(?:[^'"\n]|\\.)+?`` — the character class could match a
#: backslash too, so a backslash could be consumed EITHER by the class OR
#: by the escape branch.  Two ways to match the same character ⇒ when the
#: closing delimiter could not be satisfied, the engine had exponentially
#: many partitions to try.
#:
#: MEASURED (2026-11-01): one message in the live ROOT library (8,319
#: characters) made ``S2-sshpass-quoted`` run for **more than 20
#: seconds**; the other eleven forms were instant.  The message contained
#: an ``sshpass -p '`` whose closing quote was not where the pattern
#: needed it.  So a single message could hang the credential masker —
#: and the masker runs on **every message written** (the database throat
#: and the truth source), so one message could stall the write path.
#:
#: The fix keeps the language and removes the ambiguity: the escape
#: branch owns the backslash, and the character class refuses it.
#:
#:   ``(?:\\.|[^'"\\\n])+``
#:
#: Ordering the branches the other way (``(?:\\.|[^'"\n])+?``) does
#: **NOT** fix it — the engine still falls back to the class when the
#: escape branch fails, so the ambiguity (and the blow-up) remains.
#: MEASURED: that variant still timed out.
#:
#: The positive match is unchanged: on the F-5 synthetic corpus it still
#: yields ``('sshpass -p ', 'SYNTHETIC-CREDENTIAL-DO-NOT-USE', "'")``.
#:
#: One semantic note: the quantifier is now greedy rather than lazy.
#: That is safe here because the character class refuses the quote
#: character, so the value always ends at the first unescaped quote —
#: the shortest and the longest value coincide.  (The escape branch can
#: span a quote, but then the closing delimiter that follows it is the
#: one that terminates the value, so the two lengths still agree.)
_QVAL = r"(?:\\.|[^'\"\\\n])+"
_QCLOSE = _ESC + r"(?P=q)"


#: The credential word, as the TAIL of an identifier.
#:
#: The word must be the identifier's tail, not merely inside it: that is
#: what separates ``web_password`` (a credential) from ``token_budget``
#: (a number) and ``bypass`` (a word).  The tail must not be followed by
#: another word character, so ``passwords`` is not a tail-match for
#: ``password`` alone — but ``passwd`` and ``password`` are separate
#: alternatives and both are covered.
_CRED_TAIL = (r"(?:pass|password|passwd|pwd|secret|token|api_?key|apikey"
              r"|credential|cred)(?![A-Za-z0-9_])")


def _kv_name() -> str:
    """The credential-ish identifier fragment.

    Matches a name whose TAIL is one of the credential words, so
    ``password``, ``PASS``, ``web_password``, ``API_KEY`` and
    ``my_token`` are all covered — but ``monkey``, ``token_budget`` and
    ``bypass`` are not.
    """
    return _CRED_TAIL


def _cred_ident() -> str:
    """An identifier whose tail is a credential word.

    The lookahead anchors the credential word to the identifier's END,
    which is what keeps ``token_budget`` out.
    """
    return (r"(?=[A-Za-z0-9_]*" + _CRED_TAIL + r"\s*=)"
            r"[A-Za-z_][A-Za-z0-9_]*")


#: (logical id, compiled pattern).  The pattern has the named groups
#: ``pre`` (everything up to and including the opening delimiter),
#: ``v`` (the value) and ``post`` (the closing delimiter, possibly empty).
_FORMS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # ---- S-1 / S-2 : the argv shape -------------------------------------
    ("S1-sshpass-unquoted",
     re.compile(r"(?P<pre>sshpass\s+-p\s+)(?P<v>" + _BARE + r")")),
    ("S2-sshpass-quoted",
     re.compile(r"(?P<pre>sshpass\s+-p\s+" + _QOPEN + r")"
                r"(?P<v>" + _QVAL + r")(?P<post>" + _QCLOSE + r")")),

    # ---- S-3 : curl / wget -----------------------------------------------
    ("S3-curl-user-unquoted",
     re.compile(r"(?P<pre>(?:curl|wget)\b[^\n]{0,300}?(?<![A-Za-z0-9-])"
                r"(?:-u|--user)\s+)(?P<v>" + _BARE + r")")),
    ("S3-curl-user-quoted",
     re.compile(r"(?P<pre>(?:curl|wget)\b[^\n]{0,300}?(?<![A-Za-z0-9-])"
                r"(?:-u|--user)\s+" + _QOPEN + r")"
                r"(?P<v>" + _QVAL + r")(?P<post>" + _QCLOSE + r")")),

    # ---- S-4 / S-5 : the key=value shapes --------------------------------
    # The identifier may carry a prefix (``web_password``); the
    # credential word must be its TAIL, which keeps ``token_budget`` out.
    ("S4-kv-unquoted",
     re.compile(r"(?i)(?P<pre>\b" + _cred_ident() + r"\s*=\s*)"
                r"(?P<v>[^\s\"',;(){}]{4,})")),
    ("S5-kv-quoted",
     re.compile(r"(?i)(?P<pre>\b" + _cred_ident() + r"\s*=\s*"
                + _QOPEN + r")(?P<v>" + _QVAL + r")(?P<post>" + _QCLOSE + r")")),

    # ---- S-6 : the JSON field --------------------------------------------
    # Two variants: the plain JSON text (``"password": "v"``) and the
    # ESCAPED form that appears when the message content was dumped into
    # a JSON string (``\"password\": \"v\"``).  The escaped variant is the
    # one a naive rule silently misses.
    #
    # The escaped variant accepts a RUN of backslashes, because the
    # escape depth grows with every JSON round-trip (measured: the real
    # corpus carries the credential at depth 3).
    ("S6-json-field",
     re.compile(r"(?i)(?P<pre>\"(?:password|passwd|pwd|secret|token|api_?key"
                r"|apikey)\"\s*:\s*\")(?P<v>[^\"\\]{4,})(?P<post>\")")),
    ("S6-json-field-escaped",
     re.compile(r"(?i)(?P<pre>" + _ESC + r"\"(?:password|passwd|pwd|secret|token"
                r"|api_?key|apikey)" + _ESC + r"\"\s*:\s*" + _ESC + r"\")"
                r"(?P<v>" + _JSON_VAL + r")(?P<post>" + _ESC + r"\")")),

    # ---- S-7 : URL userinfo ----------------------------------------------
    ("S7-url-userinfo",
     re.compile(r"(?P<pre>[A-Za-z][A-Za-z0-9+.-]{2,9}://[^\s:/@\"']+:)"
                r"(?P<v>[^\s\"'@/]+)(?P<post>@)")),

    # ---- S-8 : the environment-variable shape ----------------------------
    ("S8-env-assign",
     re.compile(r"(?i)(?P<pre>\b" + _cred_ident() + r"\s*=\s*)"
                r"(?P<v>[^\s\"',;(){}]{4,})")),

    # ---- S-9 : the export shape ------------------------------------------
    ("S9-export",
     re.compile(r"(?i)(?P<pre>\bexport\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*)"
                r"(?P<v>[^\s\"',;(){}]{4,})")),

    # ---- S-10 : the bare identifier assignment --------------------------
    # ``PASS = "<v>"`` — the shape this machine's scripts actually use.
    ("S10-assignment",
     re.compile(r"(?i)(?P<pre>\b" + _cred_ident() + r"\s*=\s*)"
                r"(?P<v>[^\s\"',;(){}]{4,})")),
)


#: The FORM IDs, in order.  The criterion asserts that every one of them
#: is reachable — a rule silently deleted from this table must not be
#: allowed to make the criterion quietly weaker.
FORM_IDS: Tuple[str, ...] = tuple(fid for fid, _rx in _FORMS)


# ---------------------------------------------------------------------------
# The placeholder rule — what is NOT a credential
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"<[^>]*>"                       # <REDACTED>, <your-password>, ...
    r"|\*\*+"
    r"|REDACTED"
    r"|(?:your|my|the)[_-]?(?:password|passwd|pwd|secret|token|key)"
    r"|(?:example|placeholder|dummy|changeme|your[_-]?password)"
    r"|x{3,}"
    r"|\.\.\."
    r"|\$\{[^}]*\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|[Nn]/[Aa]"
    r"|[Tt][Rr][Uu][Ee]"
    r"|[Nn][Uu][Ll][Ll]"
    r"|[Nn][Oo][Nn][Ee]"
    r"|[Nn][Uu][Ll][Ll]"
    r")$"
)

#: A value that looks like a dotted Python/JS attribute path
#: (``os.environ``, ``self.token``, ``args.password``) is CODE, not a
#: credential.  Real passwords rarely look like ``a.b.c``.
_DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*"
                        r"(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")

#: The cheap pre-filter: if the text cannot contain any credential shape,
#: do not run the ten rules over it.  A 200 KB message costs one regex
#: instead of ten.
_MAYBE_RE = re.compile(
    r"(?i)(?:sshpass|pass|pwd|secret|token|key|cred|://)")


def is_placeholder(value: str) -> bool:
    """Is this value a PLACEHOLDER rather than a credential?

    Exposed on purpose: the criterion needs to assert that placeholders
    survive, and a rule that cannot be asked cannot be tested.

    **The trailing backslash is stripped first.**  Measured 2026-10-31:
    a JSONL line's ``content`` is JSON TEXT, so the value that arrives
    here can end with an escape (``<REDACTED>\\``).  Without the strip,
    the placeholder ``<REDACTED>`` reads as a credential and the scanner
    reports a leak that is not there — a criterion that cries wolf is a
    criterion nobody trusts.
    """
    v = value.strip().rstrip("\\").strip()
    if not v:
        return True
    if _PLACEHOLDER_RE.match(v):
        return True
    if _DOTTED_RE.match(v):
        return True
    return False


def _looks_like_credential(value: str) -> bool:
    """The value test: long enough, not a placeholder, not code."""
    v = value.strip()
    if len(v) < MIN_VALUE_LEN:
        return False
    return not is_placeholder(v)


def _mask_once(text: str) -> str:
    """Apply every rule once, in order."""
    if not text or not _MAYBE_RE.search(text):
        return text
    out = text
    for _fid, rx in _FORMS:
        def _sub(m: "re.Match[str]") -> str:
            v = m.group("v")
            if not _looks_like_credential(v):
                return m.group(0)
            pre = m.group("pre")
            post = m.groupdict().get("post") or ""
            return pre + REDACTED + post

        out = rx.sub(_sub, out)
    return out


def mask_text(text: str) -> str:
    """Mask the credential shapes in one piece of text.

    IDEMPOTENT: ``mask_text(mask_text(x)) == mask_text(x)``.  The marker
    is ``<REDACTED>``, which the placeholder rule exempts, and the
    marker contains none of the shapes' own trigger words in a way that
    would re-match.

    The text may be PLAIN TEXT or JSON TEXT (a message's ``content`` is
    a JSON string, so a credential inside it appears with escaped
    quotes).  Both are handled by the same rule set.
    """
    if not isinstance(text, str):
        return text
    return _mask_once(text)


def mask_content(obj: Any) -> Any:
    """Mask every string leaf of a JSON-serialisable structure.

    Provided for callers that hold the STRUCTURED content rather than
    the dumped JSON text.  The two paths produce the same result for the
    same logical content, because the rules are delimiter-aware.
    """
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, list):
        return [mask_content(x) for x in obj]
    if isinstance(obj, dict):
        return {k: mask_content(v) for k, v in obj.items()}
    return obj


def scan_forms(text: str) -> Dict[str, int]:
    """Count the credential shapes in ``text``, per FORM ID.

    This is the MEASUREMENT the criterion uses — it asks the masker what
    it would find, rather than asking the source what it contains.
    """
    out: Dict[str, int] = {}
    if not isinstance(text, str) or not text:
        return out
    for fid, rx in _FORMS:
        n = 0
        for m in rx.finditer(text):
            if _looks_like_credential(m.group("v")):
                n += 1
        if n:
            out[fid] = n
    return out


def has_credential(text: str) -> bool:
    """Is there at least one credential shape in ``text``?"""
    return bool(scan_forms(text))


# ---------------------------------------------------------------------------
# File-level measurement
# ---------------------------------------------------------------------------

def scan_jsonl(path: Any) -> Dict[str, int]:
    """Count the credential shapes in a JSONL file, per FORM ID.

    **The file is PARSED, not pattern-matched as raw bytes.**  Measured
    2026-10-31: a JSONL line's ``content`` is JSON TEXT, so on the raw
    bytes a double-quoted credential appears as ``\\"SYNTHETIC-CREDENTIAL-DO-NOT-USE\\"``.  A
    raw-bytes scanner has two failure modes there, and BOTH are wrong in
    the direction that matters:

    * the UNQUOTED rule matches the escaped backslash run that precedes
      the value, and stops there — so the credential behind it is never
      seen (a MISS);
    * the QUOTED rule then matches the already-masked ``<REDACTED>`` and
      reports a leak that is not there (a FALSE POSITIVE).

    Parsing first and scanning the DECODED content removes both: the
    decoded content carries the real delimiters, which is exactly the
    layer the rules are written for.
    """
    import json

    out: Dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    # An unparseable line is still scanned as text — it
                    # may BE the leak.
                    for fid, n in scan_forms(raw).items():
                        out[fid] = out.get(fid, 0) + n
                    continue
                if not isinstance(obj, dict):
                    continue
                content = obj.get("content")
                if isinstance(content, str):
                    for fid, n in scan_forms(content).items():
                        out[fid] = out.get(fid, 0) + n
                else:
                    for fid, n in scan_forms(json.dumps(content,
                                                        ensure_ascii=False)).items():
                        out[fid] = out.get(fid, 0) + n
    except OSError:
        return out
    return out


def scan_tree(root: Any, suffixes: Tuple[str, ...] = (".jsonl",)
              ) -> Dict[str, int]:
    """Count the credential shapes in every matching file under ``root``."""
    import os

    out: Dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            if suffixes and not fn.endswith(suffixes):
                continue
            p = os.path.join(dirpath, fn)
            for fid, n in scan_jsonl(p).items():
                out[fid] = out.get(fid, 0) + n
    return out
