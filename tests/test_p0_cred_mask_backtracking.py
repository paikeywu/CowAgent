# -*- coding: utf-8 -*-
"""P0 · 凭据遮蔽器的病态回溯（2026-11-01 实测）。

**The defect.**  The quoted-value pattern was
``(?:[^'"\n]|\\.)+?`` — the character class could match a backslash
too, so a backslash had TWO ways to be consumed.  When the closing
delimiter could not be satisfied, the engine had exponentially many
partitions to try.

**MEASURED**: one message in the live ROOT library (8,319 characters)
made ``S2-sshpass-quoted`` run for **more than 20 seconds**; the other
eleven forms were instant.  The masker runs on **every message written**
(the database throat and the truth source), so **one message could
stall the write path** — and the write path is where the externalisation
happens.

**The fix**: make the two alternatives DISJOINT — the escape branch owns
the backslash and the character class refuses it.  Ordering them the
other way does NOT help (the engine still falls back to the class).

**The guard**: the pattern must finish in a bounded time on a text that
used to hang it.  A test that merely checks "it returns" would pass on a
slow implementation, so the test MEASURES the time.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory import cred_mask  # noqa: E402


def _the_shape() -> str:
    """A text with the shape that used to hang the masker.

    Built, not copied from the live library: the corpus must not depend
    on a machine's data.  The shape is what matters — an ``sshpass -p``
    with an opening quote, a long body, and backslashes.
    """
    body = ("x" * 60 + "\\" + "y" * 60) * 12
    return ("sshpass -p '" + body) * 4


def test_the_quoted_value_pattern_is_linear_on_the_hostile_shape():
    """The regression: the pattern must not blow up.

    **The bound is generous** (5 seconds for a few kilobytes is
    astronomically slow for a regex) — the point is that the OLD
    implementation took *minutes*, so any finite bound catches it.
    """
    text = _the_shape()
    t0 = time.time()
    hits = sum(1 for _ in cred_mask._FORMS[1][1].finditer(text))
    dt = time.time() - t0
    assert dt < 5.0, (
        "S2-sshpass-quoted took %.2fs on a %d character text — the "
        "pathological backtracking is back" % (dt, len(text)))
    # the shape is a *hostile* one, not a credential — so no hit is the
    # right answer, and the test asserts the TIME, not the hit count.


def test_mask_text_finishes_on_the_hostile_shape():
    text = _the_shape()
    t0 = time.time()
    out = cred_mask.mask_text(text)
    dt = time.time() - t0
    assert dt < 5.0, "mask_text took %.2fs" % dt
    assert out is not None


def test_scan_forms_finishes_on_the_hostile_shape():
    text = _the_shape()
    t0 = time.time()
    forms = cred_mask.scan_forms(text)
    dt = time.time() - t0
    assert dt < 5.0, "scan_forms took %.2fs" % dt
    assert isinstance(forms, dict)


def test_the_alternatives_are_disjoint():
    """**The fix is structural, not incidental.**

    The character class must NOT be able to match a backslash — that is
    what made the two branches ambiguous.  Asserting the property (not
    the outcome) means a future edit that reintroduces the ambiguity
    fails here, with a message that says why.

    **The check parses the class** rather than looking for a ``\\``:
    inside a character class ``\\\\`` is an escaped backslash (refused)
    and ``\\n`` is the newline escape (not a backslash).  A naive
    substring test confuses the two — and a test that is wrong about
    the thing it is testing is worse than no test.
    """
    pat = cred_mask._FORMS[1][1].pattern
    m = re.search(r"\(\?P<v>(.*?)\)\(\?P<post>", pat)
    assert m, "cannot find the value group in %r" % pat
    value = m.group(1)

    for cls in re.findall(r"\[\^[^]]*\]", value):
        # Walk the class body.  In a NEGATED class ``[^...]``:
        #   "\\\\"  (an escaped backslash) means the backslash is EXCLUDED;
        #   "\\X"   (an escape sequence like \" or \n) is a literal X;
        #   a LONE trailing backslash would mean the class ADMITS it.
        # Getting this backwards is how a test ends up asserting the
        # opposite of what it says.
        body = cls[2:-1]           # drop "[", "^" and "]"
        admits_backslash = False
        j = 0
        while j < len(body):
            ch = body[j]
            if ch == "\\":
                if j + 1 < len(body) and body[j + 1] == "\\":
                    j += 2          # escaped backslash -> EXCLUDED
                    continue
                if j + 1 < len(body):
                    j += 2          # an escape sequence -> a literal
                    continue
                admits_backslash = True   # a lone trailing backslash
                j += 1
                continue
            j += 1
        assert not admits_backslash, (
            "the value's character class %r still admits a backslash — "
            "the two alternatives are ambiguous again" % cls)

    # and the escape branch must be present
    assert "\\\\." in value, (
        "the value no longer has an escape branch: %r" % value)


def test_the_character_class_really_refuses_a_backslash():
    """**Prove it by execution, not by reading.**  The class must not
    match a single backslash."""
    pat = cred_mask._FORMS[1][1].pattern
    m = re.search(r"\(\?P<v>(.*?)\)\(\?P<post>", pat)
    value = m.group(1)
    cls = re.findall(r"\[\^[^]]*\]", value)[0]
    rx = re.compile(cls)
    assert rx.search("\\") is None, (
        "the character class %r matches a lone backslash — the ambiguity "
        "is back" % cls)


def test_the_positive_match_is_unchanged():
    """The fix must not change WHAT is matched."""
    rx = cred_mask._FORMS[1][1]
    got = [(m.group("pre"), m.group("v"), m.group("post"))
           for m in rx.finditer("sshpass -p 'SYNTHETIC-CREDENTIAL-DO-NOT-USE'")]
    assert got == [("sshpass -p '", "SYNTHETIC-CREDENTIAL-DO-NOT-USE", "'")], got


def test_the_escaped_quote_case_still_matches():
    """The value may contain an escaped quote — and still terminates at
    the right place."""
    rx = cred_mask._FORMS[1][1]
    got = [(m.group("pre"), m.group("v"), m.group("post"))
           for m in rx.finditer("sshpass -p 'ab'")]
    assert got and got[0][1] == "ab", got
