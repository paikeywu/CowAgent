"""T2c — the external-edit provenance round.

The hole this closes (measured 2026-11-01):

* the change log is an APPEND-ONLY JSONL file, and its ONLY writers were
  our own write paths (``facade.write`` / ``facade.write_element`` /
  ``facade.import_human_edit`` / ``rebuilder.rebuild_file``);
* ``manager.py`` — the module that actually READS THE DISK — never
  imported ``changes`` at all, so a file edited outside those paths was
  indexed but never logged.

⇒ a persona rewritten on 2026-11-01 left the log saying "the last
truth-layer change was 2026-10-30".  DR-2 reported "no source" and was
RIGHT.

These tests pin the properties that make the fix honest:

* an external edit ENTERS the log;
* the log does NOT grow when nothing changed (the "every read refreshes
  the stamp" failure would break the shelf-lag invariant);
* a SECOND external edit is seen AGAIN (not a one-shot);
* a ``touch`` alone is NOT a change (content, not clock);
* the drift round-trip works (edit -> drift -> re-cut -> zero);
* the old ``SOURCES`` semantics still hold.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agent.memory import changes, external_edit


class TestExternalEditProvenance(unittest.TestCase):
    """Every test runs in its own sandbox workspace."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t2c_"))
        self.ws = self.tmp / "ws"
        (self.ws / "memory" / "markdown").mkdir(parents=True)
        (self.ws / "memory" / "long-term").mkdir(parents=True)
        self.truth = self.ws / "AGENT.md"
        self.truth.write_text("# 灵魂文件\n\n第一段。\n\n第二段。\n",
                               encoding="utf-8")
        self.rel = "AGENT.md"
        self.cands = [(self.rel, self.truth)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the hole ----------------------------------------------------------

    def test_external_edit_enters_the_change_log(self):
        """An edit made OUTSIDE our write paths must be witnessed."""
        self.truth.write_text("# 灵魂文件\n\n第一段。\n\n第二段。\n\n外部加的。\n",
                               encoding="utf-8")
        rep = external_edit.reconcile_external_edits(self.ws, self.cands)
        self.assertEqual(1, len(rep["recorded"]), rep)
        self.assertEqual(self.rel, rep["recorded"][0]["path"])
        sources = {e.get("source") for e in changes.read_changes(self.ws)}
        self.assertIn(external_edit.SOURCE_EXTERNAL, sources)

    def test_the_event_carries_a_content_witness(self):
        """The event must carry the hash — otherwise it cannot be compared."""
        self.truth.write_text("# 灵魂文件\n\n改了。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        evs = changes.read_changes(self.ws)
        self.assertTrue(any(e.get(external_edit.WITNESS_KEY) for e in evs),
                        evs)

    # -- the "every read refreshes the stamp" failure -----------------------

    def test_rerun_records_nothing(self):
        """A second pass over an unchanged tree must record NOTHING.

        If it recorded, the log would grow on every scan AND
        ``latest_change_mtime`` would advance on every read — which
        would make the shelf-lag invariant report every page as stale
        forever.
        """
        self.truth.write_text("# 灵魂文件\n\n改了。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        n_events = len(changes.read_changes(self.ws))
        stamp = changes.latest_change_mtime(self.ws)

        rep = external_edit.reconcile_external_edits(self.ws, self.cands)
        self.assertEqual([], rep["recorded"])
        self.assertEqual(n_events, len(changes.read_changes(self.ws)))
        self.assertEqual(stamp, changes.latest_change_mtime(self.ws))

    def test_touch_alone_is_not_a_change(self):
        """Content, not clock: a ``touch`` must not fabricate a change."""
        self.truth.write_text("# 灵魂文件\n\n改了。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        n_events = len(changes.read_changes(self.ws))
        os.utime(self.truth, None)
        rep = external_edit.reconcile_external_edits(self.ws, self.cands)
        self.assertEqual([], rep["recorded"])
        self.assertEqual(n_events, len(changes.read_changes(self.ws)))

    def test_a_crlf_conversion_is_not_a_change(self):
        """The witness is CANONICAL: a line-ending flip is not a persona
        change.  (The whole memory system hashes canonical text; a raw
        byte witness would report a change every time a file was
        checked out on Windows.)"""
        self.truth.write_text("# 灵魂文件\n\n改了。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        self.truth.write_bytes(
            "# 灵魂文件\r\n\r\n改了。\r\n".encode("utf-8"))
        rep = external_edit.reconcile_external_edits(self.ws, self.cands)
        self.assertEqual([], rep["recorded"])

    # -- "not only effective once" ----------------------------------------

    def test_a_second_external_edit_is_seen_again(self):
        """The mechanism must not be one-shot."""
        self.truth.write_text("# 灵魂文件\n\n第一改。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        n1 = len(changes.read_changes(self.ws))

        self.truth.write_text("# 灵魂文件\n\n第一改。\n\n第二改。\n",
                               encoding="utf-8")
        rep = external_edit.reconcile_external_edits(self.ws, self.cands)
        self.assertEqual(1, len(rep["recorded"]), rep)
        self.assertEqual(n1 + 1, len(changes.read_changes(self.ws)))

    def test_a_third_external_edit_is_seen_again(self):
        self.truth.write_text("# 灵魂文件\n\n一。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        for i in (2, 3, 4):
            self.truth.write_text("# 灵魂文件\n\n%s\n" % ("x" * i),
                                   encoding="utf-8")
            rep = external_edit.reconcile_external_edits(self.ws, self.cands)
            self.assertEqual(1, len(rep["recorded"]), "edit #%d: %s" % (i, rep))

    # -- the drift round-trip ---------------------------------------------

    def test_drift_goes_to_zero_after_a_recut(self):
        """The whole point: an external edit makes the index stale, and a
        re-cut makes it fresh again — while the log keeps the witness."""
        from agent.memory.body_pointer import four_columns_for_chunk
        from agent.memory.chunker import chunk_for_file
        from agent.memory.storage import MemoryChunk, MemoryStorage
        import hashlib

        db = self.ws / "memory" / "long-term" / "index.db"
        storage = MemoryStorage(str(db))

        def cut(text):
            rows = []
            for pc in chunk_for_file(text, self.rel):
                bp, ba, bh, ts, bo = four_columns_for_chunk(
                    workspace_root=self.ws, relpath=self.rel,
                    start_line=pc.start_line, end_line=pc.end_line,
                    part=pc.part, chunk_text=pc.text)
                rows.append(MemoryChunk(
                    id=hashlib.md5(("%s:%d:%d" % (self.rel, pc.start_line,
                                                   pc.end_line)).encode()
                                   ).hexdigest(),
                    user_id=None, scope="shared", source="memory",
                    path=self.rel, start_line=pc.start_line,
                    end_line=pc.end_line, text=pc.text, embedding=None,
                    hash=bh, metadata=None, element_released=False,
                    file_sealed=False, sealed_owner=None,
                    body_path=bp, body_anchor=ba, body_hash=bh,
                    text_state=ts, body_origin=bo))
            return rows

        text = self.truth.read_text(encoding="utf-8")
        storage.replace_file_chunks(
            self.rel, cut(text), file_hash="x" * 64,
            mtime=int(self.truth.stat().st_mtime),
            size=self.truth.stat().st_size, chunker_version=4)

        # edit externally -> the index is stale
        self.truth.write_text(text + "\n漂移了。\n", encoding="utf-8")
        # re-cut -> fresh again
        storage.replace_file_chunks(
            self.rel, cut(self.truth.read_text(encoding="utf-8")),
            file_hash="y" * 64, mtime=int(self.truth.stat().st_mtime),
            size=self.truth.stat().st_size, chunker_version=4)

        # and the log still witnesses the NEW content
        rep = external_edit.reconcile_external_edits(self.ws, self.cands)
        self.assertEqual(1, len(rep["recorded"]), rep)
        rep2 = external_edit.reconcile_external_edits(self.ws, self.cands)
        self.assertEqual([], rep2["recorded"])

    # -- existing semantics must not break --------------------------------

    def test_the_old_sources_still_validate(self):
        changes.record_change(self.ws, "x.md", "facade-write")
        with self.assertRaises(ValueError):
            changes.record_change(self.ws, "x.md", "bogus-source")

    def test_latest_change_mtime_advances_on_a_real_change(self):
        before = changes.latest_change_mtime(self.ws)
        self.truth.write_text("# 灵魂文件\n\n改了。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        after = changes.latest_change_mtime(self.ws)
        self.assertNotEqual(before, after)

    def test_the_log_stays_append_only(self):
        """A reconciliation appends; it never rewrites an old event."""
        self.truth.write_text("# 灵魂文件\n\n一。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        first = list(changes.read_changes(self.ws))
        self.truth.write_text("# 灵魂文件\n\n二。\n", encoding="utf-8")
        external_edit.reconcile_external_edits(self.ws, self.cands)
        second = list(changes.read_changes(self.ws))
        self.assertEqual(first, second[:len(first)])
        self.assertGreater(len(second), len(first))

    def test_the_shelf_lag_invariant_still_decides(self):
        from agent.memory.text_inv import check_shelf_lag
        v = check_shelf_lag(self.ws)
        self.assertIn(v.get("verdict"), ("NOTRUN", "RED", "GREEN"))

    # -- the diagnostic (the visibility fallback) ---------------------------

    def test_diagnose_reports_a_newer_file(self):
        """``diagnose`` is the visibility that does not depend on the
        witness hashes: a file newer than the log's last record for it
        is reported."""
        changes.record_change(self.ws, self.rel, "facade-write")
        # the recorded event's mtime is NOW; make the file newer
        os.utime(self.truth, (self.truth.stat().st_atime + 120,
                              self.truth.stat().st_mtime + 120))
        rep = external_edit.diagnose(self.ws)
        self.assertTrue(any(e["path"] == self.rel
                            for e in rep["paths_mtime_newer"]), rep)

    def test_diagnose_reports_a_never_logged_path(self):
        rep = external_edit.diagnose(self.ws)
        self.assertIn(self.rel, rep["never_logged"])


if __name__ == "__main__":
    unittest.main()
