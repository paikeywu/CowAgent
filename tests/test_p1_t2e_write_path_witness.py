"""T2e — the write-path witness hook: self-proof.

**What this file proves, on a SANDBOX (never the live workspace):**

  1. An external edit to a truth-layer file, followed by a write-path
     landing, produces a WITNESS that reproduces the current content.
  2. The witness is recorded by the WRITE PATH — no sync is needed.
  3. Editing the SAME file again produces a NEW witness (the hole does
     not close after the first time).
  4. Re-reading does NOT refresh the witness and does NOT advance
     ``latest_change_mtime`` (A3 — the "every read refreshes" trap).
  5. The write path and the reconciliation do NOT fight each other:
     after the write path witnessed the file, the reconciliation adds
     NOTHING for it (no duplicate, no log growth).
  6. ``write_decide`` (which judges ``body_origin``) and the witness
     mechanism (which records provenance) do not interfere: writing
     through the memory facade still decides the origin.
  7. A NON-truth-layer file (a scratch file) is NOT witnessed.
  8. An unreadable file yields NO witness (never a fabricated one).
  9. The witness is computed from CANONICAL content, so a CRLF/LF
     flip is not read as a change.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.memory import changes, external_edit  # noqa: E402
from agent.memory.witness_write import witness_write  # noqa: E402

WITNESS = external_edit.WITNESS_KEY


def _make_workspace(tmp: Path) -> Path:
    """A minimal workspace shaped like ours: a change log + truth files."""
    ws = tmp / "cow"
    (ws / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
    (ws / "memory").mkdir(parents=True, exist_ok=True)
    (ws / "AGENT.md").write_text("# agent\n\noriginal\n", encoding="utf-8")
    (ws / "MEMORY.md").write_text("# memory\n\noriginal\n", encoding="utf-8")
    (ws / "memory" / "2026-11-01.md").write_text("# day\n\noriginal\n",
                                                encoding="utf-8")
    (ws / "memory" / "evolution").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "evolution" / "2026-11-01.md").write_text(
        "# evolution\n\noriginal\n", encoding="utf-8")
    return ws


def _last_event(ws: Path, rel: str):
    last = None
    for e in changes.read_changes(ws):
        if e.get("path") == rel:
            last = e
    return last


def _canon(path: Path) -> str:
    import hashlib
    from agent.memory.naming import canonical_text
    return hashlib.sha256(
        canonical_text(path.read_text(encoding="utf-8")).encode("utf-8")
    ).hexdigest()


class TestWritePathWitness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="t2e-witness-")
        self.ws = _make_workspace(Path(self._tmp))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- 1/2: the write path witnesses without a sync --------------------
    def test_write_path_records_a_reproducible_witness_without_a_sync(self):
        target = self.ws / "memory" / "2026-11-01.md"
        target.write_text("# day\n\nEDITED BY SOMEONE\n", encoding="utf-8")

        ev = witness_write(target, actor="test")
        self.assertIsNotNone(ev, "the write path must record a witness")

        last = _last_event(self.ws, "memory/2026-11-01.md")
        self.assertIsNotNone(last)
        self.assertEqual(last[WITNESS], _canon(target),
                         "the witness must reproduce the current content")
        self.assertEqual(last.get("source"), external_edit.SOURCE_EXTERNAL)

    # -- 3: the hole does not close after the first time -----------------
    def test_editing_again_produces_a_new_witness(self):
        target = self.ws / "memory" / "2026-11-01.md"
        target.write_text("# day\n\nv1\n", encoding="utf-8")
        witness_write(target, actor="test")
        first = _last_event(self.ws, "memory/2026-11-01.md")[WITNESS]

        target.write_text("# day\n\nv2 CHANGED\n", encoding="utf-8")
        witness_write(target, actor="test")
        second = _last_event(self.ws, "memory/2026-11-01.md")[WITNESS]

        self.assertNotEqual(first, second,
                            "a second edit must produce a NEW witness")
        self.assertEqual(second, _canon(target))

    # -- 4: re-reading must not refresh (A3) -----------------------------
    def test_rereading_does_not_refresh_the_witness(self):
        target = self.ws / "memory" / "2026-11-01.md"
        target.write_text("# day\n\nv1\n", encoding="utf-8")
        witness_write(target, actor="test")

        before_events = len(changes.read_changes(self.ws))
        before_mtime = changes.latest_change_mtime(self.ws)

        # A second witness of the SAME content adds nothing.
        again = witness_write(target, actor="test")
        self.assertIsNone(again, "the same content must not be recorded twice")
        self.assertEqual(len(changes.read_changes(self.ws)), before_events)
        self.assertEqual(changes.latest_change_mtime(self.ws), before_mtime)

    # -- 5: the write path and the reconciliation do not fight -----------
    def test_reconciliation_adds_nothing_after_the_write_path_witnessed(self):
        """The two mechanisms are COMPLEMENTARY, not competing.

        The write path witnesses the file it just wrote; the
        reconciliation witnesses the files nobody wrote through a path
        we know about.  So after the write path has witnessed a file,
        the reconciliation must NOT record that file again — but it
        still owes a witness for every OTHER truth file.
        """
        target = self.ws / "memory" / "2026-11-01.md"
        target.write_text("# day\n\nv1\n", encoding="utf-8")
        witness_write(target, actor="test")

        rep = external_edit.reconcile_external_edits(
            self.ws, external_edit.truth_files(self.ws))
        recorded = [e["path"] for e in rep["recorded"]]

        self.assertNotIn(
            "memory/2026-11-01.md", recorded,
            "the reconciliation must not duplicate the write path's witness")
        # ... and it DID close the ones nobody wrote through our path.
        self.assertIn("AGENT.md", recorded)
        self.assertIn("MEMORY.md", recorded)
        self.assertIn(
            "memory/evolution/2026-11-01.md", recorded)

        # A SECOND reconciliation pass must find nothing left: the two
        # mechanisms together are idempotent.
        rep2 = external_edit.reconcile_external_edits(
            self.ws, external_edit.truth_files(self.ws))
        self.assertEqual(
            [e["path"] for e in rep2["recorded"]], [],
            "the second pass must record nothing (A3)")

    # -- 6: the two mechanisms do not interfere --------------------------
    def test_write_decide_and_the_witness_mechanism_do_not_interfere(self):
        """``write_decide`` judges body_origin; the witness records
        provenance.  They must not step on each other: the witness
        writes to the CHANGE LOG, ``write_decide`` writes to the
        INDEX.  A file that both mechanisms see must get both."""
        from agent.memory import decide_origin
        target = self.ws / "memory" / "2026-11-01.md"
        target.write_text("# day\n\nsome real content here\n", encoding="utf-8")

        verdict = decide_origin.decide_row(
            str(self.ws),
            {"path": "memory/2026-11-01.md",
             "text": target.read_text(encoding="utf-8")},
        )
        self.assertIsNotNone(verdict)
        self.assertNotEqual(
            verdict.get("origin"), "unresolved",
            "the origin decision must still be made")

        ev = witness_write(target, actor="test")
        self.assertIsNotNone(ev, "the witness must still be recorded")
        self.assertEqual(ev[WITNESS], _canon(target))

    # -- 7: a non-truth-layer file is not witnessed -----------------------
    def test_a_non_truth_layer_file_is_not_witnessed(self):
        scratch = self.ws / "scratch.txt"
        scratch.write_text("not a truth file\n", encoding="utf-8")
        self.assertIsNone(witness_write(scratch, actor="test"))
        self.assertIsNone(_last_event(self.ws, "scratch.txt"))

    # -- 8: an unreadable file yields no witness ------------------------
    def test_an_unreadable_file_yields_no_witness(self):
        # A file that does not exist cannot be witnessed.
        missing = self.ws / "memory" / "nope.md"
        self.assertIsNone(witness_write(missing, actor="test"))

    # -- 9: canonical, so a CRLF flip is not a change --------------------
    def test_a_crlf_flip_is_not_read_as_a_change(self):
        target = self.ws / "memory" / "2026-11-01.md"
        target.write_bytes(b"# day\r\n\r\nv1\r\n")
        witness_write(target, actor="test")
        first = _last_event(self.ws, "memory/2026-11-01.md")[WITNESS]

        target.write_bytes(b"# day\n\nv1\n")   # same content, LF endings
        again = witness_write(target, actor="test")
        self.assertIsNone(again,
                          "a CRLF/LF flip must not be read as a change")
        self.assertEqual(_last_event(self.ws, "memory/2026-11-01.md")[WITNESS],
                         first)

    # -- the agent workspace gets ITS OWN log, not the root's ------------
    def test_an_agent_workspace_uses_its_own_log(self):
        agent_ws = self.ws / "agents" / "agent-x"
        (agent_ws / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
        (agent_ws / "memory").mkdir(parents=True, exist_ok=True)
        (agent_ws / "memory" / "2026-11-01.md").write_text("# a\n\nv1\n",
                                                           encoding="utf-8")
        ev = witness_write(agent_ws / "memory" / "2026-11-01.md", actor="test")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["path"], "memory/2026-11-01.md")
        # The ROOT log must not have received it.
        self.assertIsNone(_last_event(self.ws, "memory/2026-11-01.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
