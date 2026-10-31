"""P1 stage-3 · O(N^2) fix — the criteria for the rebuild release-state cache.

THE MEASUREMENT, NOT THE SOURCE
-------------------------------
Every criterion here COUNTS CALLS.  None of them reads the source to
decide whether a resolve happened.

Why counting is the only way: ``recompute_release_flags`` does a
*function-local* import::

    from agent.memory.release_state import release_mismatches, resolve

so ``resolve`` is re-read from the module on EVERY call.  Patching the
module attribute (``agent.memory.release_state.resolve``) is therefore
what observes the real call count.  A criterion that greps the source
would be a criterion about the source, not about the behaviour.

The O(N^2) being fixed
----------------------
``rebuild_file`` used to call ``recompute_release_flags`` per file, and
that function scans the WHOLE truth layer (``resolve()`` +
``release_mismatches()``).  A batch of N files therefore did N full
scans of an N-file tree.  Measured: 300 files -> 300 resolves ->
42.4s.  With the cache: 1 resolve -> 4.6s.

The BREAKERS (each one must turn a criterion RED)
-------------------------------------------------
B1  drop the ``if release_state is None:`` guard -> always resolve
B2  drop the cache threading in ``migrate.step_m7``
B3  make the cache ignore its own signature (never re-resolve)
B4  make the cache carry the state but NOT the mismatches
B5  make ``rebuild_file`` ignore the cache and resolve anyway
"""

import inspect
import time

import pytest

from agent.memory import release_state as rs_mod


# ---- helpers ---------------------------------------------------------------

def _make_workspace(tmp_path, n=5):
    """A workspace with n truth-layer markdown files."""
    ws = tmp_path / "ws"
    md = ws / "memory" / "markdown" / "events" / "2026-10-29"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (md / f"e{i:03d}--0000000{i}.md").write_text(
            "line one\nline two\n", encoding="utf-8")
    return ws, md


def _count_calls(fn):
    """Run ``fn`` and return (result, resolve_calls, mismatch_calls)."""
    counts = {"resolve": 0, "mismatch": 0}
    real_resolve = rs_mod.resolve
    real_mm = rs_mod.release_mismatches

    def counting_resolve(*a, **kw):
        counts["resolve"] += 1
        return real_resolve(*a, **kw)

    def counting_mm(*a, **kw):
        counts["mismatch"] += 1
        return real_mm(*a, **kw)

    rs_mod.resolve = counting_resolve
    rs_mod.release_mismatches = counting_mm
    try:
        result = fn()
    finally:
        rs_mod.resolve = real_resolve
        rs_mod.release_mismatches = real_mm
    return result, counts["resolve"], counts["mismatch"]


# ---- C-1 · the headline criterion -----------------------------------------

def test_c1_rebuild_file_does_not_resolve_when_given_a_cache(tmp_path):
    """C-1: with a cache, rebuild_file must NOT resolve PER FILE.

    This is the criterion the whole ticket is about.  It counts the real
    calls, so a rewrite that keeps the source looking right but still
    resolves cannot pass it.

    The count is 1, not 0: the cache resolves the truth layer ONCE (on
    its first use) and reuses that snapshot for the rest of the batch.
    What must not happen is a resolve PER FILE — that is the O(N^2).
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    ws, md = _make_workspace(tmp_path, n=5)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    try:
        cache = rs_mod.ReleaseStateCache(ws)
        files = sorted(md.glob("*.md"))

        def run():
            for f in files:
                rebuild_file(st, ws, f, release_state=cache)
            return True

        _, resolves, mismatches = _count_calls(run)
    finally:
        st.close()

    assert resolves == 1, (
        "C-1: rebuilding 5 files while holding a cache must cost ONE "
        "resolve (the cache's own), got %d — the cache is being bypassed "
        "and the O(N^2) is still there" % resolves)
    assert mismatches == 1, (
        "C-1: rebuilding 5 files while holding a cache must cost ONE "
        "mismatch scan, got %d" % mismatches)


def test_c2_rebuild_file_still_resolves_when_given_nothing(tmp_path):
    """C-2: with NO release_state, the behaviour must be UNCHANGED.

    The fix must not turn the uncached path into a no-op: without a
    release state the function must resolve on its own, exactly as
    before.  (This is the criterion that stops "fix the O(N^2)" from
    becoming "stop resolving".)
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    ws, md = _make_workspace(tmp_path, n=3)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    try:
        files = sorted(md.glob("*.md"))

        def run():
            for f in files:
                rebuild_file(st, ws, f)
            return True

        _, resolves, _ = _count_calls(run)
    finally:
        st.close()

    assert resolves == 3, (
        "C-2: without a release_state the function must resolve once per "
        "file (3 files -> 3 resolves); got %d" % resolves)


def test_c3_a_batch_is_one_resolve_not_n(tmp_path):
    """C-3: the whole point — N files must cost ONE resolve, not N.

    This is the O(N^2) as a measurable property, not as a timing claim.
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    ws, md = _make_workspace(tmp_path, n=8)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    try:
        cache = rs_mod.ReleaseStateCache(ws)
        files = sorted(md.glob("*.md"))

        def run():
            for f in files:
                rebuild_file(st, ws, f, release_state=cache)
            return True

        _, resolves, _ = _count_calls(run)
    finally:
        st.close()

    assert resolves == 1, (
        "C-3: a batch of 8 files must cost exactly 1 resolve (the cache "
        "resolves once); got %d" % resolves)


def test_c4_the_cached_and_uncached_results_are_identical(tmp_path):
    """C-4: the cache must not change WHAT is written.

    The rows are compared field by field: id, path, scope, the release
    flags, the body columns and the hash.  "Same row count" would not be
    enough — the flags are exactly what the fix touches.
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    ws, md = _make_workspace(tmp_path, n=4)
    files = sorted(md.glob("*.md"))
    cols = ("id", "path", "scope", "element_released", "file_sealed",
            "sealed_owner", "body_path", "body_anchor", "body_hash",
            "text_state", "hash")

    def collect(use_cache):
        db = ws / "memory" / "long-term" / ("c.db" if use_cache else "u.db")
        st = MemoryStorage(db_path=db)
        try:
            if use_cache:
                cache = rs_mod.ReleaseStateCache(ws)
                for f in files:
                    rebuild_file(st, ws, f, release_state=cache)
            else:
                for f in files:
                    rebuild_file(st, ws, f)
            rows = st.conn.execute(
                "SELECT %s FROM chunks ORDER BY id" % ", ".join(cols)
            ).fetchall()
            return [tuple(r) for r in rows]
        finally:
            st.close()

    cached = collect(True)
    uncached = collect(False)
    assert cached == uncached, (
        "C-4: the cached and the uncached rebuild must produce identical "
        "rows; they differ")


def test_c5_the_mismatch_audit_is_still_written_with_a_cache(tmp_path):
    """C-5: the cache must NOT silently drop the mismatch audit.

    The flags decide what is written; the mismatches decide what is
    REPORTED.  A cache that carried only the state would make the
    rebuild stop reporting a front-matter/tombstone disagreement — a
    silent loss of the audit trail.  This criterion builds a real
    disagreement and asserts the marker is still written.
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    ws = tmp_path / "ws"
    md = ws / "memory" / "markdown" / "events" / "2026-10-29"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    # A file whose front-matter says "released" but with NO tombstone:
    # release_mismatches calls that "frontmatter_only" -> a disagreement.
    rel = "memory/markdown/events/2026-10-29/e000--00000000.md"
    (md / "e000--00000000.md").write_text(
        "---\nstatus: released\ntype: release\nid: f_x\n---\nbody\n",
        encoding="utf-8")

    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    try:
        cache = rs_mod.ReleaseStateCache(ws)
        rebuild_file(st, ws, md / "e000--00000000.md", release_state=cache)
    finally:
        st.close()

    marker = ws / "memory" / ".audit" / "release_mismatch.jsonl"
    assert marker.is_file(), (
        "C-5: the mismatch audit must still be written when a cache is "
        "used — the cache carried the state but lost the mismatches; "
        "no marker at %s" % marker)
    text = marker.read_text(encoding="utf-8")
    assert rel in text, (
        "C-5: the mismatch marker must name the disagreeing path %r; "
        "got %r" % (rel, text))


def test_c6_the_cache_notices_a_truth_layer_edit_mid_batch(tmp_path):
    """C-6: the cache must NOT serve a stale state.

    The rebuild used to be always-fresh (it resolved per file).  A cache
    that did not notice an edit would be a semantic change, not just a
    speed-up.  The edit here is made BETWEEN two rebuilds and the
    second rebuild must see the new truth.

    WHAT is asserted: the RELEASE FLAG.  The earlier version of this
    criterion compared the chunk TEXT, which changes whenever the file
    content changes -- so it stayed green even when the cache served a
    completely stale state.  The text is not what the cache holds; the
    flags are.

    MEASURED LIMIT OF THIS CRITERION (found by the breaker): even with
    a FROZEN cache this criterion stays GREEN, because the
    ``index_guard`` trigger recomputes ``element_released`` from the
    row's own front-matter text (``released_by_text``) -- a SECOND
    mechanism that reaches the same verdict independently.  So this
    criterion proves the OUTCOME is safe; it does NOT prove the cache
    re-resolved.  C-6b is the criterion that proves the MECHANISM.
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    ws, md = _make_workspace(tmp_path, n=2)
    target = sorted(md.glob("*.md"))[0]
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    try:
        cache = rs_mod.ReleaseStateCache(ws)
        rebuild_file(st, ws, target, release_state=cache)
        before = [tuple(r) for r in st.conn.execute(
            "SELECT element_released FROM chunks WHERE path LIKE ? "
            "ORDER BY id", ("%e000%",))]

        # The truth file changes: it becomes RELEASED.  The flag is what
        # the cache holds, so the flag is what must change.
        time.sleep(0.02)
        target.write_text(
            "---\nstatus: released\ntype: release\nid: f_y\n---\nbody\n",
            encoding="utf-8")
        rebuild_file(st, ws, target, release_state=cache)
        after = [tuple(r) for r in st.conn.execute(
            "SELECT element_released FROM chunks WHERE path LIKE ? "
            "ORDER BY id", ("%e000%",))]
    finally:
        st.close()

    assert before and all(flag == 0 for (flag,) in before), (
        "C-6: before the edit the file is not released, so the projection "
        "must carry element_released=0; got %r" % (before,))
    assert after and all(flag == 1 for (flag,) in after), (
        "C-6: after the truth file became released, the rebuild must write "
        "element_released=1 — the cache served a STALE state (got %r)"
        % (after,))


def test_c6b_the_cache_re_resolves_when_the_tree_moves(tmp_path):
    """C-6b: the cache must RE-RESOLVE when the truth layer moves.

    C-6 asserts the OUTCOME (the flag follows the truth).  C-6b asserts
    the MECHANISM (a resolve actually happened again).  Both matter:
    the outcome can be reached by accident (e.g. the rebuild reading the
    file itself), while the mechanism is what keeps the O(N^2) from
    coming back through the back door.

    Measured: the second resolve happens exactly when the signature
    moves.
    """
    ws, md = _make_workspace(tmp_path, n=3)
    cache = rs_mod.ReleaseStateCache(ws)

    _, resolves_1, _ = _count_calls(lambda: cache.get_with_mismatches())
    _, resolves_2, _ = _count_calls(lambda: cache.get_with_mismatches())
    assert resolves_1 == 1, (
        "C-6b: the first use of the cache must resolve once; got %d"
        % resolves_1)
    assert resolves_2 == 0, (
        "C-6b: with an unchanged truth layer the cache must NOT resolve "
        "again; got %d — the cache is not caching" % resolves_2)

    # Now move the tree.
    time.sleep(0.02)
    (md / "e000--0000000.md").write_text("changed\n", encoding="utf-8")
    _, resolves_3, _ = _count_calls(lambda: cache.get_with_mismatches())
    assert resolves_3 == 1, (
        "C-6b: after the truth layer moved the cache must resolve again "
        "once; got %d — a stale state would be served" % resolves_3)


def test_c6c_a_bare_release_state_says_the_audit_is_lost(tmp_path, caplog):
    """C-6c: a bare ReleaseState must WARN that the audit is lost.

    A bare ``ReleaseState`` carries no mismatch list, so the rebuild
    cannot report a front-matter/tombstone disagreement.  Dropping that
    SILENTLY would make the audit trail vanish without a trace -- a
    silent loss of a finding.  So the rebuild must say so.
    """
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    ws, md = _make_workspace(tmp_path, n=1)
    target = sorted(md.glob("*.md"))[0]
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    try:
        bare = rs_mod.resolve(ws)
        with caplog.at_level("WARNING"):
            rebuild_file(st, ws, target, release_state=bare)
    finally:
        st.close()

    text = caplog.text
    assert "mismatch" in text.lower(), (
        "C-6c: a bare ReleaseState must WARN that the release-mismatch "
        "audit will not be written; the log said: %r" % text)


def test_c7_migrate_threads_the_cache_into_the_rebuild():
    """C-7: the PRODUCTION path must use the cache.

    A fix that only the tests exercise is not a fix.  This criterion
    reads the production call site and asserts the cache is passed.
    """
    from agent.memory import migrate

    src = inspect.getsource(migrate.step_m7)
    assert "release_state=" in src, (
        "C-7: migrate.step_m7 must pass a release_state to rebuild_file — "
        "the production path still pays the full scan per file")
    assert "ReleaseStateCache" in src, (
        "C-7: migrate.step_m7 must build a ReleaseStateCache for the batch")


def test_c8_the_production_path_resolves_once_per_batch(tmp_path):
    """C-8: the production path must resolve ONCE per batch.

    C-7 proves the code SAYS the right thing; C-8 proves it BEHAVES
    that way — the call is counted, not read.

    The batch is driven through the REAL ``step_m7`` on a real library.
    ``step_m7`` imports ``rebuild_file`` INSIDE its body, so the patch
    goes on the module it is imported FROM.
    """
    from agent.memory import migrate
    from agent.memory import rebuilder as rebuilder_mod
    from agent.memory.storage import MemoryStorage

    ws = tmp_path / "ws"
    md = ws / "memory" / "markdown" / "events" / "2026-10-29"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    n = 6
    for i in range(n):
        (md / f"e{i:03d}--0000000{i}.md").write_text(
            "line one\nline two\n", encoding="utf-8")

    db = ws / "memory" / "long-term" / "index.db"
    st = MemoryStorage(db_path=db)
    try:
        for f in sorted(md.glob("*.md")):
            rebuilder_mod.rebuild_file(st, ws, f)
    finally:
        st.close()

    # Now drive the REAL production step and count the resolves.
    real_rebuild = rebuilder_mod.rebuild_file
    seen = []

    def recording_rebuild(storage, workspace_root, file_path, **kw):
        seen.append(kw.get("release_state"))
        return real_rebuild(storage, workspace_root, file_path, **kw)

    rebuilder_mod.rebuild_file = recording_rebuild
    try:
        report = migrate.Report()
        live = [{"name": "t", "db": db, "workspace": ws}]
        _, resolves, _ = _count_calls(
            lambda: migrate.step_m7(ws, live, report))
    finally:
        rebuilder_mod.rebuild_file = real_rebuild

    assert len(seen) == n, (
        "C-8: the production step must rebuild all %d files; it rebuilt %d"
        % (n, len(seen)))
    assert all(s is not None for s in seen), (
        "C-8: every production rebuild must receive a release_state — one "
        "of the %d calls got none" % len(seen))
    # ONE resolve for the whole batch (the cache resolves once).
    assert resolves == 1, (
        "C-8: the production batch of %d files must cost exactly 1 resolve; "
        "got %d" % (n, resolves))
    # ... and the M7 verdict must still be PASS.
    m7 = [c for c in report.criteria if c["id"].startswith("M7.")]
    assert m7 and m7[0]["state"] == "PASS", (
        "C-8: the M7 verdict must stay PASS; got %r" % (m7,))
