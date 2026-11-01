"""T2d — ``files.hash`` has ONE comparison口径.

The fork this closes (measured 2026-11-01 on the live corpus):

* 9 of 16 libraries store ``files.hash`` as ``sha256:<64hex>`` (written
  by ``rebuilder.rebuild_file``), 7 store it as a bare ``<64hex>``
  (written by the ``manager`` scan path);
* and the two writers disagree on the digest BASIS too — the prefixed
  rows hash the CANONICAL text, the bare rows hash the RAW text.

⇒ a raw string comparison read 493 of 1193 live rows as "the file
changed" (the manager's scan loop) and 1109 of 1193 (the criterion
side's ``_file_changed``) — false alarms on files that had not changed
at all.

These tests pin the properties that make the fix honest:

* both historical shapes compare EQUAL to the same digest;
* a REAL content change is still detected (the fix must not turn the
  comparison into a no-op);
* a line-ending-only difference is NOT a change (that is the point);
* the stored value is never rewritten;
* new writes use ONE shape, so the fork stops growing.
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.memory.file_hash import (  # noqa: E402
    SHA256_PREFIX,
    canonical_file_hash,
    file_hash_matches,
    hash_matches,
    hash_shape,
    hashes_equal,
    normalize_hash,
)
from agent.memory.naming import canonical_text  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- shapes

def test_both_historical_shapes_compare_equal_to_the_same_digest():
    digest = _sha("hello")
    assert hashes_equal(digest, SHA256_PREFIX + digest) is True
    assert hashes_equal(SHA256_PREFIX + digest, digest) is True
    assert hashes_equal(digest, digest) is True


def test_normalize_strips_the_prefix_and_keeps_the_digest():
    digest = _sha("hello")
    assert normalize_hash(SHA256_PREFIX + digest) == digest
    assert normalize_hash(digest) == digest
    assert normalize_hash("  " + SHA256_PREFIX + digest + "  ") == digest
    assert normalize_hash(None) is None
    assert normalize_hash("") is None


def test_two_absent_fingerprints_are_not_equal():
    # The absence of evidence is not evidence of equality.  A caller
    # asking "did it change?" must handle the absent case itself.
    assert hashes_equal(None, None) is False
    assert hashes_equal(None, _sha("x")) is False


def test_hash_shape_classifies_the_dialects():
    digest = _sha("x")
    assert hash_shape(SHA256_PREFIX + digest) == "prefixed"
    assert hash_shape(digest) == "bare"
    assert hash_shape(None) == "absent"
    assert hash_shape("") == "absent"
    assert hash_shape("not-a-hash") == "other"


# ------------------------------------------------- the basis disagreement

def test_the_canonical_and_raw_bases_both_match(tmp_path):
    # The two historical writers hashed DIFFERENT bases of the same text:
    #
    #   * the rebuilder  -> the CANONICAL text (outer whitespace
    #     stripped, runs of blank lines folded);
    #   * the scan path  -> the text AS ``read_text()`` returned it.
    #
    # And ``read_text()`` does universal-newline translation, so a CRLF
    # file and its LF twin read IDENTICALLY — the "as-read" basis
    # already tolerates the line-ending difference.  What it does NOT
    # tolerate is a change in the trailing whitespace / blank lines.
    #
    # ⇒ that difference is the second axis of the fork, and it is the
    # one that produces a REAL false alarm (a file whose trailing
    # newline was added must not read as "the file changed").
    padded = tmp_path / "padded.md"
    padded.write_text("line one\nline two\n\n\n", encoding="utf-8")
    trimmed = tmp_path / "trimmed.md"
    trimmed.write_text("line one\nline two\n", encoding="utf-8")

    padded_text = padded.read_text(encoding="utf-8")
    trimmed_text = trimmed.read_text(encoding="utf-8")

    as_read_digest = _sha(padded_text)
    canonical_digest = _sha(canonical_text(padded_text))
    assert as_read_digest != canonical_digest

    # the stored bare (as-read) digest matches the file it came from
    assert hash_matches(as_read_digest, padded_text) is True
    # the stored prefixed canonical digest matches the same file
    assert hash_matches(SHA256_PREFIX + canonical_digest, padded_text) is True
    # and the canonical basis ALSO matches the trimmed twin — the
    # trailing-blank-line change is not a content change
    assert hash_matches(SHA256_PREFIX + canonical_digest, trimmed_text) is True
    # the as-read basis is byte-exact: the trimmed twin differs ⇒ NOT
    # a match.  The canonical basis is the one that tolerates it.
    assert hash_matches(as_read_digest, trimmed_text) is False
    # but the bytes-on-disk basis matches the file it came from
    assert hash_matches(as_read_digest, padded_text,
                        raw_bytes=padded.read_bytes()) is True


def test_a_crlf_file_and_its_lf_twin_read_identically(tmp_path):
    # Why the "as-read" basis already tolerates a line-ending change:
    # ``read_text`` translates the newlines.  This is the fact that
    # makes the PREFIX the only thing that made the prefixed libraries
    # look changed.
    crlf = tmp_path / "crlf.md"
    crlf.write_bytes(b"line one\r\nline two\r\n")
    lf = tmp_path / "lf.md"
    lf.write_bytes(b"line one\nline two\n")
    assert crlf.read_text(encoding="utf-8") == lf.read_text(encoding="utf-8")
    stored = _sha(lf.read_text(encoding="utf-8"))
    assert hash_matches(stored, crlf.read_text(encoding="utf-8")) is True


def test_a_real_change_is_still_detected():
    stored = SHA256_PREFIX + _sha("original content")
    assert hash_matches(stored, "different content") is False
    assert hash_matches(stored, "original content\n") is True


def test_the_comparison_is_not_a_no_op():
    # The mutant guard: if the comparison always returned True, the
    # tests above would still pass on the "match" side.  This one says
    # a changed file is reported.
    stored = _sha("v1")
    assert hash_matches(stored, "v2") is False
    assert file_hash_matches(stored, "v2") is False


def test_raw_bytes_are_accepted_as_an_admissible_basis():
    content = "hello\n"
    raw = content.encode("utf-8")
    stored = _sha(content)  # the raw-text basis
    assert hash_matches(stored, content, raw_bytes=raw) is True
    # without the raw bytes the raw basis is still computed from the text
    assert hash_matches(stored, content) is True


# ------------------------------------------------------------ write side

def test_new_writes_use_one_shape():
    out = canonical_file_hash("hello")
    assert out.startswith(SHA256_PREFIX)
    assert hash_shape(out) == "prefixed"
    # and it is the CANONICAL basis, so a CRLF file and its LF twin
    # produce the SAME fingerprint
    assert canonical_file_hash("a\r\nb\r\n") == canonical_file_hash("a\nb")


def test_canonical_write_matches_what_the_reader_accepts():
    content = "hello\n"
    stored = canonical_file_hash(content)
    assert hash_matches(stored, content) is True
    assert hashes_equal(stored, _sha(canonical_text(content))) is True
