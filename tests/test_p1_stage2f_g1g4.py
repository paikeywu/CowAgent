"""P1-阶段二-F · G1~G4 门禁漏洞修复的反向用例。

  G-1: 生产巡检必须全量（300 行坏 1 行 → 必报红 + 定位到那一行）。
  G-2: .audit/ 失败标记必须有读者（重建失败 → 巡检报红）。
  G-3: 重建器与写入器必须产出同一套 chunk 身份（真实语料）。
  G-4: D-A / D-C 的真实状态登记（未修就如实登记，不许声称已修）。

G-5（判据可移植性）：语料来源必须是**显式输入**，不是某台机器的绝对路径。

语料来源，三层兜底（按优先级）：
  ① 环境变量 ``COW_TEST_CORPUS`` —— 指向一个含真实 .md 的目录；
  ② 仓库内 fixture ``tests/fixtures/corpus/`` —— CI / 干净环境的兜底；
  ③ 都没有 → ``pytest.skip``，并说明原因（不许假绿、不许假红）。

**不许**把语料降级成「简单样本」：简单样本在两套分块器下恰好切得一样，
分叉被样本掩盖 —— 那正是 G-3 漏掉的原因。仓库 fixture 里的三份文件
（文档型 / 文章型 / 超长行型）都是**实测能暴露分叉**的（见
``test_g5_fixture_corpus_exposes_the_fork``）。
"""
import ast
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: G-5: the corpus directory. NEVER a machine-specific absolute path.
#: The old version of this file pointed the corpus at one Windows
#: machine's home directory, which made this criterion true on that box
#: and permanently RED on CI (``ubuntu-latest``). The exact path it used
#: is recorded in the G-5 delivery note (the grep-based portability check
#: must find NOTHING here, including in the prose).
CORPUS_ENV_VAR = "COW_TEST_CORPUS"
FIXTURE_CORPUS = REPO / "tests" / "fixtures" / "corpus"


#: Corpus file extensions. ``.md`` files exercise the markdown-aware
#: branch of the chunking decision; ``.txt`` files exercise the PLAIN
#: branch — which is the only path that produces the ``part`` ordinal, and
#: therefore the only path on which the id rule ``md5(path:start:end[:part])``
#: can be observed at all. A corpus of ``.md`` files alone cannot expose
#: a defect in the ``part`` rule.
CORPUS_SUFFIXES = (".md", ".txt")


def _corpus_root():
    """The corpus root, resolved from an explicit input (G-5).

    Resolution order:
      ① ``$COW_TEST_CORPUS`` — an explicit, environment-owned input;
      ② ``tests/fixtures/corpus/`` — the in-repo fallback;
      ③ neither → ``pytest.skip`` (never a silent PASS, never a FAIL).
    """
    env = os.environ.get(CORPUS_ENV_VAR)
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_dir():
            return candidate, f"${CORPUS_ENV_VAR}={candidate}"
        pytest.skip(
            f"G-3: ${CORPUS_ENV_VAR} is set to {env!r} but that is not a "
            "directory; the real corpus is unavailable — this check did "
            "NOT run (no silent PASS).")
    if FIXTURE_CORPUS.is_dir():
        return FIXTURE_CORPUS, f"in-repo fixture {FIXTURE_CORPUS}"
    pytest.skip(
        f"G-3: no real corpus available — ${CORPUS_ENV_VAR} is unset "
        f"and the in-repo fixture {FIXTURE_CORPUS} does not exist; "
        "this check did NOT run (no silent PASS).")


def _corpus_files(limit=None, suffixes=CORPUS_SUFFIXES):
    """The REAL corpus for the G-3 checks, wherever it lives."""
    root, source = _corpus_root()
    files = sorted(p for p in root.rglob("*")
                   if p.is_file() and p.suffix.lower() in suffixes)
    if not files:
        pytest.skip(
            f"G-3: the corpus at {source} contains no "
            f"{'/'.join(suffixes)} files; the real corpus is unavailable — "
            "this check did NOT run.")
    if limit is not None:
        files = files[:limit]
    return files


def _corpus_source() -> str:
    """Which corpus layer answered — reported in the assertion messages."""
    env = os.environ.get(CORPUS_ENV_VAR)
    if env and Path(env).expanduser().is_dir():
        return f"${CORPUS_ENV_VAR}={env}"
    return f"in-repo fixture {FIXTURE_CORPUS}"


def _expected_chunk_id(path: str, start_line: int, end_line: int,
                       part: int = 0) -> str:
    """The chunk id rule, spelled out HERE — not imported.

    ``md5("path:start:end[:part]")``. It is written out in the test on
    purpose: an expectation computed by the very function it is supposed
    to check is a tautology — change the rule and both sides of the
    comparison move together, and the check stays green while the ids
    are wrong. That is not a hypothetical: the stage-2-F G-3 check
    compared ``rebuilder._chunk_id`` against ``rebuilder._chunk_id``.
    """
    import hashlib
    content = f"{path}:{start_line}:{end_line}"
    if part:
        content += f":{part}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


# ---- G-1 · sampling blind spot -----------------------------------------

def _build_300_row_index(tmp_path, break_one=False):
    """A 300-row projection with ONE corrupted row."""
    from agent.memory import index_guard
    from agent.memory.storage import MemoryStorage

    ws = tmp_path / "ws"
    md = ws / "memory" / "markdown" / "events" / "2026-10-29"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    body = "line one\nline two\n"
    for i in range(300):
        (md / f"e{i:03d}--0000000{i%10}.md").write_text(body, encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    from agent.memory.rebuilder import rebuild_file
    for f in sorted(md.glob("*.md")):
        rebuild_file(st, ws, f)
    if break_one:
        # corrupt exactly ONE row's text (the truth file stays correct)
        target = st.conn.execute(
            "SELECT id FROM chunks ORDER BY id LIMIT 1").fetchone()[0]
        st.conn.execute("UPDATE chunks SET text='CORRUPTED' WHERE id=?", (target,))
        st.conn.commit()
        return ws, st, target
    return ws, st, None


def test_g1_one_bad_row_in_300_is_found(tmp_path):
    """G-1: 300 rows, 1 broken -> the check must be RED (no sampling)."""
    from agent.memory import text_inv
    ws, st, target = _build_300_row_index(tmp_path, break_one=True)
    try:
        res = text_inv.check_inv2_inv3(st.conn, ws)
        assert res["verdict"] == "RED", (
            "G-1: a corrupted row must turn the check red, got "
            f"{res['verdict']!r} (fingerprint={res.get('fingerprint')!r})")
        assert res["violations"], "RED but no violations were reported"
        chunk_ids = {v.get("chunk_id") for v in res["violations"]}
        assert target in chunk_ids, (
            f"G-1: the violation must name the broken chunk id {target!r}; "
            f"got {chunk_ids!r}")
    finally:
        st.close()


def test_g1_default_is_full_not_sampled(tmp_path):
    """G-1: the DEFAULT must be exhaustive — omission must not degrade."""
    from agent.memory import text_inv
    import inspect
    sig = inspect.signature(text_inv.check_inv2_inv3)
    assert sig.parameters["full"].default is True, (
        "G-1: check_inv2_inv3 must default to full=True")
    sig2 = inspect.signature(text_inv.run_text_invariants)
    assert sig2.parameters["full"].default is True, (
        "G-1: run_text_invariants must default to full=True")


def test_g1_production_inspection_path_is_full(tmp_path):
    """G-1: the inspector's call site must pass full=True EXPLICITLY."""
    src = (REPO / "agent" / "memory" / "inspector.py").read_text(
        encoding="utf-8")
    assert "run_text_invariants(\n                conn, self.workspace_root, full=True)" \
        in src, (
            "G-1: the production inspection path must pass full=True "
            "explicitly")


def test_g1_the_production_inspection_path_finds_the_bad_row(tmp_path):
    """G-1: the PRODUCTION path (Inspector.inspect) must be exhaustive.

    This is the criterion the mutant replay needs: the first version of
    the G-1 check called ``check_inv2_inv3`` DIRECTLY, so it kept the
    default (full) and stayed green even when the production call site
    was mutated back to sampling — a blind spot in the criterion, not a
    property of the code. This one goes through the real entry point.
    """
    from agent.memory.inspector import Inspector
    ws, st, target = _build_300_row_index(tmp_path, break_one=True)
    try:
        insp = Inspector(workspace_root=ws)
        report = insp.inspect(trigger="manual", persist_snapshot=False)
        text_inv_report = report.get("text_inv") or {}
        check = (text_inv_report.get("checks") or {}).get("INV-2/3") or {}
        assert check.get("verdict") == "RED", (
            "G-1: the production inspection must find the corrupted row; "
            f"verdict={check.get('verdict')!r} "
            f"fingerprint={check.get('fingerprint')!r}")
        chunk_ids = {v.get("chunk_id") for v in check.get("violations", [])}
        assert target in chunk_ids, (
            f"G-1: the violation must name the broken chunk id {target!r}; "
            f"got {chunk_ids!r}")
    finally:
        st.close()


# ---- G-2 · the marker must have a reader --------------------------------

def test_g2_rebuild_failure_makes_the_inspection_red(tmp_path):
    """G-2: a failed rebuild -> the inspection must report it."""
    from agent.memory.inspector import Inspector
    from agent.memory import index_guard
    from agent.memory.storage import MemoryStorage
    from agent.memory.rebuilder import rebuild_file, RebuildError

    ws = tmp_path / "ws"
    md = ws / "memory" / "markdown" / "events" / "2026-10-29"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    f = md / "note--aaaaaaaa.md"
    f.write_text("hello\n", encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuild_file(st, ws, f)

    # force a rebuild failure
    original = st.replace_file_chunks

    def boom(*a, **k):
        raise RuntimeError("forced rebuild failure")

    st.replace_file_chunks = boom
    try:
        with pytest.raises(RebuildError):
            rebuild_file(st, ws, f)
    finally:
        st.replace_file_chunks = original

    marker = ws / "memory" / ".audit" / "rebuild_failures.jsonl"
    assert marker.is_file(), (
        "G-2: the failure marker must be written under memory/.audit/, "
        f"not {ws / '.audit'} — found nothing at {marker}")
    assert "forced rebuild failure" in marker.read_text(encoding="utf-8")

    # and the inspector must READ it
    insp = Inspector(workspace_root=ws)
    report = insp.inspect(trigger="manual", persist_snapshot=False)
    codes = [v.get("code") for v in report.get("violations", [])]
    assert "rebuild-failed" in codes, (
        "G-2: the inspection must report the failed rebuild; "
        f"got codes={codes!r}")
    st.close()


def test_g2_every_audit_marker_has_a_reader_or_is_registered():
    """G-2: every .audit/ marker must have a reader, or be registered."""
    from agent.memory import rebuilder
    # rebuild_failures.jsonl -> read by the inspector (G-2)
    assert hasattr(rebuilder, "read_rebuild_failures")
    # release_mismatch.jsonl -> read by the inspector (G-2)
    assert hasattr(rebuilder, "read_release_mismatches")
    src = (REPO / "agent" / "memory" / "inspector.py").read_text(
        encoding="utf-8")
    assert "read_rebuild_failures" in src, (
        "G-2: the inspector must call the reader")
    assert "read_release_mismatches" in src, (
        "G-2: the inspector must read the release-mismatch marker too")


def test_g2_release_mismatch_makes_the_inspection_red(tmp_path):
    """G-2: a front-matter/tombstone disagreement -> the inspection reports it."""
    from agent.memory.inspector import Inspector
    from agent.memory.rebuilder import _audit_mismatch

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    _audit_mismatch(ws, "memory/markdown/events/x.md", ["frontmatter_only"])
    insp = Inspector(workspace_root=ws)
    report = insp.inspect(trigger="manual", persist_snapshot=False)
    codes = [v.get("code") for v in report.get("violations", [])]
    assert "release-mismatch" in codes, (
        "G-2: the inspection must report the release mismatch; "
        f"got codes={codes!r}")


# ---- G-3 · one chunking decision ----------------------------------------

def _real_corpus_files(limit=3):
    """REAL corpus: the knowledge pages this team actually wrote."""
    return _corpus_files(limit=limit)


def test_g3_real_corpus_ids_are_identical_after_rebuild(tmp_path):
    """G-3: write -> rebuild must yield the SAME chunk ids (real corpus).

    G-5: the EXPECTED ids are computed with the id rule spelled out in
    the TEST (``_expected_chunk_id``), NOT with either implementation's
    own helper. The rule has two independent implementations
    (``manager._generate_chunk_id`` and ``rebuilder._chunk_id``) and
    comparing the rebuilder against itself is a tautology: mutate the
    rebuilder's rule and both sides of the comparison move together, so
    the check stays green while the ids are wrong. The point of the check
    is that the written ids match the RULE.
    """
    from agent.memory import index_guard
    from agent.memory.chunker import chunk_for_file
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    files = _real_corpus_files()
    ws = tmp_path / "ws"
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)

    for src in files:
        raw = src.read_text(encoding="utf-8")
        rel = f"memory/markdown/events/2026-10-29/{src.name}"
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(raw, encoding="utf-8")

        # what the WRITER would produce (the single decision point)
        writer_ids = {
            _expected_chunk_id(rel, c.start_line, c.end_line, c.part)
            for c in chunk_for_file(raw, rel)}

        rebuild_file(st, ws, dst)
        rows = st.conn.execute(
            "SELECT id FROM chunks WHERE path=?", (rel,)).fetchall()
        rebuild_ids = {r[0] for r in rows}

        assert writer_ids == rebuild_ids, (
            "G-3: writer and rebuilder must agree on chunk identity for "
            f"{src.name} (corpus: {_corpus_source()}): "
            f"writer={len(writer_ids)} rebuild={len(rebuild_ids)} "
            f"only_writer={sorted(writer_ids - rebuild_ids)[:3]} "
            f"only_rebuild={sorted(rebuild_ids - writer_ids)[:3]}")
    st.close()


def test_g3_hit_count_survives_the_rebuild(tmp_path):
    """G-3: hit_count / body_hash / body_anchor must survive a rebuild."""
    from agent.memory import index_guard
    from agent.memory.rebuilder import rebuild_file
    from agent.memory.storage import MemoryStorage

    src = _real_corpus_files(1)[0]
    ws = tmp_path / "ws"
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    raw = src.read_text(encoding="utf-8")
    rel = "memory/markdown/events/2026-10-29/x.md"
    dst = ws / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(raw, encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuild_file(st, ws, dst)
    st.conn.execute("UPDATE chunks SET hit_count=7 WHERE path=?", (rel,))
    st.conn.commit()
    before = st.conn.execute(
        "SELECT id, hit_count, body_anchor, body_hash FROM chunks "
        "WHERE path=? ORDER BY id", (rel,)).fetchall()

    rebuild_file(st, ws, dst)
    after = st.conn.execute(
        "SELECT id, hit_count, body_anchor, body_hash FROM chunks "
        "WHERE path=? ORDER BY id", (rel,)).fetchall()
    assert [r[0] for r in before] == [r[0] for r in after], (
        "G-3: the chunk id set changed across a rebuild")
    assert [r[2] for r in before] == [r[2] for r in after], (
        "G-3: body_anchor changed across a rebuild")
    assert [r[3] for r in before] == [r[3] for r in after], (
        "G-3: body_hash changed across a rebuild")
    # G-5: the expected total is DERIVED from the number of chunks the
    # rebuild actually produced. The old hard-coded ``== 14`` (2 chunks x 7)
    # silently bound the criterion to one specific corpus file — on any
    # other corpus (or any other chunk count) it was a FALSE RED, and it
    # said nothing about the corpus it was supposedly proving anything on.
    n_chunks = len(after)
    assert n_chunks > 0, (
        f"G-3: the rebuild produced no chunks for {src.name}; the criterion "
        "would be vacuous")
    assert sum(r[1] for r in after) == sum(r[1] for r in before) == n_chunks * 7, (
        "G-3: hit_count was lost across a rebuild: "
        f"before={[r[1] for r in before]} after={[r[1] for r in after]} "
        f"(expected {n_chunks} chunks x 7 = {n_chunks * 7})")
    st.close()


def test_g3_the_decision_has_one_source():
    """G-3: the chunking decision must live in ONE function."""
    from agent.memory import chunker
    assert hasattr(chunker, "chunk_for_file")
    # the rebuilder must not decide for itself any more
    src = (REPO / "agent" / "memory" / "rebuilder.py").read_text(
        encoding="utf-8")
    assert "chunk_for_file(raw, rel)" in src, (
        "G-3: the rebuilder must call the shared decision function")
    assert "chunker.chunk_markdown(raw)" not in src, (
        "G-3: the rebuilder still decides for itsel")


# ---- G-5 · the criterion must be portable --------------------------------

def test_g5_no_machine_specific_absolute_path_in_this_file():
    """G-5: this file must not hard-code one machine's paths.

    The old version of this file pointed the corpus at one Windows
    machine's home directory, which made the two G-3 checks true on ONE
    box and permanently RED on CI (``runs-on: ubuntu-latest``). A
    criterion that only holds on the machine that wrote it is not a
    criterion — it is a local assertion.

    The whole file is checked, prose included: a machine-specific path
    written into a comment still tells the next reader "this only works
    here", and the portability review greps this file for such paths.
    So the needles are built at RUN time from parts — the file itself
    then contains no machine-specific path for that grep to find, which
    is the same thing this criterion is asserting.
    """
    src = (REPO / "tests" / "test_p1_stage2f_g1g4.py").read_text(
        encoding="utf-8")
    # The AST gives the string LITERALS; docstrings are excluded because
    # they are statements, not assignments — but the RAW text is checked
    # too, so the prose cannot smuggle a path in either.
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and id(n) not in docstrings]

    drive = "C:" if os.name == "nt" else "C:"
    needles = (
        drive + chr(92) + "Users",
        drive + "/Users",
        "/home/" + "paikey" + "wu",
        "/Users" + "/",
    )
    for needle in needles:
        offenders = [lit for lit in literals if needle in lit]
        assert not offenders, (
            "G-5: this test file hard-codes the machine-specific path "
            f"{needle!r} (in {offenders!r}); the corpus must come from "
            f"${CORPUS_ENV_VAR} or the in-repo fixture")
        assert needle not in src, (
            f"G-5: the machine-specific path {needle!r} appears in the raw "
            "text of this file — a portability review greps it and finds "
            "it. Keep machine-specific paths out of the file entirely.")


def test_g5_the_corpus_comes_from_an_explicit_input():
    """G-5: the corpus root must be resolved, not hard-coded.

    Resolution order: ``$COW_TEST_CORPUS`` -> in-repo fixture -> skip.
    The point of the check is that BOTH sources are reachable without
    editing the file — which is what makes the criterion portable.
    """
    env = os.environ.get(CORPUS_ENV_VAR)
    if env:
        root = Path(env).expanduser()
        assert root.is_dir(), (
            f"G-5: ${CORPUS_ENV_VAR}={env!r} is not a directory")
    else:
        assert FIXTURE_CORPUS.is_dir(), (
            f"G-5: neither ${CORPUS_ENV_VAR} nor the in-repo fixture "
            f"{FIXTURE_CORPUS} is available")
    files = _corpus_files()
    assert files, (
        f"G-5: the in-repo fixture corpus {FIXTURE_CORPUS} holds no .md "
        "files — the criterion would have nothing to run on")


def test_g5_fixture_corpus_exposes_the_fork():
    """G-5: the fixture corpus must be COMPLEX enough to expose a fork.

    This is the check that keeps the fixture from being a decoration.
    The two chunking decisions are ``chunk_text`` (the old writer's) and
    ``chunk_markdown`` (the old rebuilder's). On a SIMPLE sample they
    cut IDENTICALLY — that is exactly why the original "idempotence"
    check was green while 56 of 86 real files diverged.

    So the fixture must make the two decisions produce DIFFERENT chunk
    ids on at least one file. Compared as ID SETS, not chunk counts:
    two chunkers can agree on the count and still cut different lines.

    The id rule is spelled out HERE, in the test, rather than imported
    from the module under test: an expectation computed with the very
    implementation it is supposed to check is a tautology — mutate the
    rule and both sides move together.
    """
    import hashlib

    def expected_id(path, start, end, part=0):
        content = f"{path}:{start}:{end}"
        if part:
            content += f":{part}"
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    from agent.memory.chunker import TextChunker

    files = _corpus_files()
    if not files:
        pytest.skip(
            f"G-5: the in-repo fixture corpus {FIXTURE_CORPUS} holds no .md "
            "files — this check did NOT run.")

    rel = "memory/markdown/events/2026-10-29/x.md"
    tc = TextChunker()
    forked = []
    for p in files:
        raw = p.read_text(encoding="utf-8")
        ids_text = {
            expected_id(rel, c.start_line, c.end_line, c.part)
            for c in tc.chunk_text(raw)}
        ids_md = {
            expected_id(rel, c.start_line, c.end_line, c.part)
            for c in tc.chunk_markdown(raw)}
        if ids_text != ids_md:
            forked.append(p.name)

    assert forked, (
        "G-5: the fixture corpus does NOT expose the chunking fork — on "
        "every fixture file the two decisions cut identically, so the G-3 "
        "checks would pass no matter how wrong the decision function is. "
        f"Files: {[p.name for p in files]}. The corpus must be complex "
        "enough (multi-level headings, lists, code blocks, tables, blank "
        "lines, indentation, over-long lines) to make the two decisions "
        "diverge.")


def test_g5_fixture_corpus_forces_the_decision_to_split():
    """G-5: the corpus must push the decision function into its SPLITTING
    path — not just its "one chunk for the whole file" shortcut.

    A corpus of short files would satisfy every other G-5 check (the
    structures are there, the fork is exposed) and still leave the
    decision function's main branch untested: below the size target it
    returns ONE chunk and the boundaries never have to be decided.

    So the corpus must contain at least one file that is cut into SEVERAL
    chunks — and at least one that is cut at a boundary other than the
    file's first line. Without that, a decision function that never
    splits at all would pass everything.
    """
    from agent.memory.chunker import chunk_for_file

    files = _corpus_files()
    if not files:
        pytest.skip(
            f"G-5: the in-repo fixture corpus {FIXTURE_CORPUS} holds no .md "
            "files — this check did NOT run.")

    rel = "memory/markdown/events/2026-10-29/x.md"
    multi = []
    interior = []
    for p in files:
        raw = p.read_text(encoding="utf-8")
        chunks = chunk_for_file(raw, rel)
        if len(chunks) > 1:
            multi.append(p.name)
        if any(c.start_line not in (1, len(chunks)) for c in chunks) and \
                any(c.start_line != 1 for c in chunks):
            interior.append(p.name)

    assert multi, (
        "G-5: no fixture file is cut into more than one chunk — the corpus "
        "is too small to exercise the decision function's splitting path "
        f"(files: {[p.name for p in files]}).")
    assert interior, (
        "G-5: no fixture file is cut at a boundary other than the file's "
        "first line — the corpus never forces the decision function to "
        f"decide WHERE to cut (files: {[p.name for p in files]}).")


def test_g5_fixture_corpus_is_structurally_complex():
    """G-5: the fixture files must really carry the structures claimed.

    A file can be long and still be one flat paragraph — and then both
    decisions cut it identically. This check asserts the STRUCTURES are
    present (headings at several levels, lists, a fenced code block, a
    table, blank-line separated blocks, indented continuation lines) so
    that "complex enough" is a verified property, not a claim.

    The requirement is on the CORPUS, not on every single file: real
    knowledge pages do not all carry a table either, and demanding one of
    each structure per file would be a rule no real corpus satisfies.
    What must hold is that the corpus AS A WHOLE exercises every
    structure — otherwise one structural blind spot survives.
    """
    files = _corpus_files()
    if not files:
        pytest.skip(
            f"G-5: the in-repo fixture corpus {FIXTURE_CORPUS} holds no .md "
            "files — this check did NOT run.")

    required = {
        "h1": lambda t: any(l.startswith("# ") for l in t.splitlines()),
        "h2": lambda t: any(l.startswith("## ") for l in t.splitlines()),
        "h3": lambda t: any(l.startswith("### ") for l in t.splitlines()),
        "bullet list": lambda t: any(
            l.startswith("- ") for l in t.splitlines()),
        "ordered list": lambda t: any(
            l.strip() and l.strip()[0].isdigit() and ". " in l
            for l in t.splitlines()),
        "fenced code block": lambda t: "```" in t,
        "table": lambda t: "|:--|" in t or "|:--" in t
        or "| --- |" in t,
        "indented continuation": lambda t: any(
            l.startswith("  ") and l.strip() for l in t.splitlines()),
        "blank-line separated blocks": lambda t: "\n\n" in t,
        "over-long line (>2000 chars, the part-ordinal path)": lambda t:
            any(len(l) > 2000 for l in t.splitlines()),
    }

    corpus = "\n".join(p.read_text(encoding="utf-8") for p in files)
    missing = [name for name, probe in required.items() if not probe(corpus)]
    assert not missing, (
        "G-5: the fixture corpus as a whole is missing required "
        f"structures {missing} (files: {[p.name for p in files]}). The "
        "corpus must be complex enough to exercise the chunking "
        "decision.")


def test_g5_the_corpus_covers_the_part_ordinal_path():
    """G-5: the corpus must exercise the ``part`` ordinal of the id rule.

    The id rule is ``md5(path:start:end[:part])``. The ``part`` segment
    exists for the pieces of ONE over-long line, and the ONLY code path
    that produces a ``part`` is the plain (non-markdown) branch of the
    chunking decision.

    So a corpus of ``.md`` files alone CANNOT observe that part of the
    rule — a defect that drops the ordinal (making the pieces collide,
    so the UPSERT overwrites one and its text never reaches the index)
    is invisible on it. The corpus therefore carries a non-markdown file
    with a line over the hard-split threshold, and this check asserts
    that the path really is reached.
    """
    from agent.memory.chunker import chunk_for_file

    files = [p for p in _corpus_files() if p.suffix.lower() != ".md"]
    if not files:
        pytest.skip(
            "G-5: the corpus carries no non-markdown file, so the `part` "
            "ordinal path of the id rule cannot be observed — this check "
            "did NOT run.")

    rel = "memory/markdown/events/2026-10-29/x.txt"
    with_parts = []
    for p in files:
        raw = p.read_text(encoding="utf-8")
        chunks = chunk_for_file(raw, rel)
        if any(c.part for c in chunks):
            with_parts.append((p.name, [(c.start_line, c.end_line, c.part)
                                        for c in chunks]))

    assert with_parts, (
        "G-5: no non-markdown fixture file produced a chunk with a `part` "
        f"ordinal (files: {[p.name for p in files]}). The corpus must "
        "contain a line longer than the hard-split threshold "
        "(2000 chars) so that the `part` segment of the id rule "
        "md5(path:start:end[:part]) is actually exercised.")

    # The pieces of one over-long line share (start_line, end_line) and
    # differ ONLY in `part` — that is what makes the ordinal necessary.
    for name, ranges in with_parts:
        by_line = {}
        for start, end, part in ranges:
            by_line.setdefault((start, end), []).append(part)
        for (start, end), parts in by_line.items():
            if len(parts) > 1:
                assert len(set(parts)) == len(parts), (
                    f"G-5: the pieces of the over-long line at {name}:{start} "
                    f"share the ordinal {parts} — their chunk ids would "
                    "collide and the UPSERT would drop all but one.")


def test_g5_the_chunk_decision_is_checked_independently_of_itself():
    """G-5: the G-3 check must be able to FAIL when the decision is wrong.

    The stage-2-F version compared ``chunk_for_file(...)`` against
    ``rebuild_file(...)`` — but after stage-2-F BOTH sides call
    ``chunk_for_file``, so the comparison was a TAUTOLOGY: mutate the
    decision function and the check still passed.

    This criterion holds the decision function to a PROPERTY, derived
    here from the fixture text alone (not from the function under test):
    a MARKDOWN chunk must start at line 1 or at a HEADING line. The
    plain line splitter does not honour that, so a decision function that
    falls back to it goes RED on the fixture — which is exactly the
    regression the G-3 defect was.

    Only the ``.md`` files are subject to this rule: the plain branch
    (``chunk_text``, used for non-markdown files) cuts at arbitrary lines
    by design, so a boundary that is not a heading is CORRECT there.

    The property is checked against the TEXT, so nothing here depends on
    the function producing the expectation.
    """
    from agent.memory.chunker import chunk_for_file

    files = [p for p in _corpus_files() if p.suffix.lower() == ".md"]

    rel = "memory/markdown/events/2026-10-29/x.md"
    checked = 0
    violations = []
    for p in files:
        raw = p.read_text(encoding="utf-8")
        lines = raw.split("\n")
        chunks = chunk_for_file(raw, rel)
        if not chunks:
            continue
        checked += 1
        for c in chunks:
            if c.start_line == 1:
                continue
            if c.start_line - 1 >= len(lines):
                violations.append(
                    f"{p.name}: chunk starts at line {c.start_line}, past "
                    "the end of the file")
                continue
            if not lines[c.start_line - 1].lstrip().startswith("#"):
                violations.append(
                    f"{p.name}: chunk starts at line {c.start_line}, which is "
                    f"not a heading line ({lines[c.start_line - 1]!r})")

    assert checked, (
        "G-5: no markdown fixture file produced a chunk to check — the "
        "criterion would be vacuous")
    assert not violations, (
        "G-5: the chunking decision does not honour the markdown heading "
        f"boundary rule ({len(violations)} violation(s)): "
        f"{violations[:5]}. A markdown chunk must start at line 1 or at a "
        "heading — otherwise the decision function has fallen back to the "
        "plain line splitter, and the two sides of G-3 would be compared "
        "against different boundaries.")


# ---- G-4 · the real D-A / D-C status ------------------------------------
#
# P1-阶段三 (S3-B): D-A and D-C are NOW FIXED, so these criteria FLIP.
#
# They used to assert that the "STILL UNFIXED" registration was PRESENT —
# a criterion that was about a defect being honestly reported.  The defect
# is fixed now, so the honest report is "FIXED", and a criterion that still
# demanded the "STILL UNFIXED" wording would FAIL the code for being
# correct — which is the same failure the G-4 criterion was written to
# prevent, pointing the other way.
#
# What the flipped criteria assert:
#   * the "STILL UNFIXED" registration is GONE (the defect is fixed, and
#     the code must not keep claiming it is open);
#   * a FIXED-status registration is present (the fix must be SAY SO in
#     the code, not just be present in it — an unregistered fix is a fix
#     nobody can audit);
#   * the fix is REAL: D-A consults the truth layer's ``occurred_at``, and
#     D-C fuses the rerank score and applies the decay exactly once.
#
# The last pair are the behavioural criteria; the registration checks are
# the documentation ones.  Both are needed: a docstring that says "FIXED"
# without the behaviour is a false claim, and the behaviour without the
# docstring is an undeclared change.


def _g4_registration(source: str, marker: str) -> bool:
    """Is the UNFIXED-status registration for ``marker`` present?

    The check is deliberately LOCAL: the registration must live inside the
    docstring of the function that carries the defect. A global
    ``"D-A" in source`` test would pass on a mention ANYWHERE in the
    file — the mutant that deletes the registration line but leaves a
    passing mention of "D-A" elsewhere would slip through (which is
    exactly what the first version of this criterion did: it was
    caught by the mutant replay, not by reasoning).
    """
    for block in source.split("\n    "):
        if marker in block and "STILL UNFIXED" in block:
            return True
    return False


def _g4_fixed_registration(source: str, marker: str) -> bool:
    """Is the FIXED-status registration for ``marker`` present?"""
    for block in source.split("\n    "):
        if marker in block and "FIXED" in block:
            return True
    return False


def test_g4_da_is_fixed_and_says_so():
    """G-4 / P1-阶段三 S3-B: D-A (decay from occurred_at) IS fixed now."""
    src = (REPO / "agent" / "memory" / "manager.py").read_text(
        encoding="utf-8")
    assert not _g4_registration(src, "D-A"), (
        "G-4: D-A is fixed (P1-阶段三 S3-B) — the 'STILL UNFIXED' "
        "registration must be REMOVED; a code that keeps claiming the "
        "defect is open is misreporting its own state")
    assert _g4_fixed_registration(src, "D-A"), (
        "G-4: D-A's fix must be REGISTERED in "
        "_compute_temporal_decay's docstring as FIXED (an unregistered fix "
        "is a fix nobody can audit)")


def test_g4_dc_is_fixed_and_says_so():
    """G-4 / P1-阶段三 S3-B: D-C (reranker fusion / double decay) IS fixed."""
    src = (REPO / "agent" / "memory" / "manager.py").read_text(
        encoding="utf-8")
    assert not _g4_registration(src, "D-C"), (
        "G-4: D-C is fixed (P1-阶段三 S3-B) — the 'STILL UNFIXED' "
        "registration must be REMOVED")
    assert _g4_fixed_registration(src, "D-C"), (
        "G-4: D-C's fix must be REGISTERED in _rerank's docstring as FIXED")


def test_g4_da_really_reads_the_truth_layers_occurred_at():
    """The D-A fix is REAL: the decay consults the truth layer.

    A docstring that says "FIXED" is a claim; this is the evidence.  The
    criterion is BEHAVIOURAL: an element file whose front-matter declares
    an old ``occurred_at`` must decay, and the same file without the
    front-matter must not.
    """
    from agent.memory.manager import MemoryManager

    # 100 days ago, in the v2 element structure (a path the old regex
    # never matched).
    import datetime

    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=100)
    stamp = old.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    path = "memory/markdown/events/2026-05-01/some-event--deadbeef.md"

    decayed = MemoryManager._compute_temporal_decay(path, occurred_at=stamp)
    assert decayed < 1.0, (
        "D-A is NOT fixed: an element whose front-matter declares a "
        f"100-day-old occurred_at still scores {decayed} — the decay never "
        "consulted the truth layer")
    # Without the front-matter, the legacy path rule does not match this
    # path either, so it is evergreen.
    assert MemoryManager._compute_temporal_decay(path) == 1.0


def test_g4_dc_applies_the_decay_exactly_once():
    """The D-C fix is REAL: the decay is applied once, not twice.

    The old code multiplied the decay in BOTH ``_merge_results`` and
    ``_rerank``.  The criterion is BEHAVIOURAL: with a dated path, the
    reranked score must be the FUSED score times the decay ONCE — not
    times the decay squared.
    """
    import math

    from agent.memory.manager import MemoryManager
    from agent.memory.reranker import Reranker
    from agent.memory.storage import SearchResult

    class _Fixed(Reranker):
        def rerank(self, query, documents):
            return [1.0 for _ in documents]

    manager = MemoryManager.__new__(MemoryManager)
    manager.reranker = _Fixed()
    manager.config = type("C", (), {"rerank_weight": 0.5})()

    # 100 days ago with a 30-day half-life -> exp(-ln2/30 * 100).
    expected_decay = math.exp(-math.log(2) / 30.0 * 100)

    result = SearchResult(
        path="memory/2026-01-01.md", start_line=1, end_line=1, score=0.8,
        snippet="s", source="memory", user_id=None,
        decay=expected_decay,
    )
    reranked = manager._rerank("q", [result])
    got = reranked[0].score

    # The rerank side is rank-normalized to 1.0 for a single candidate, so
    # the fused score is (0.8 + 1.0) / 2 = 0.9.
    assert abs(got - 0.9 * expected_decay) < 1e-9, (
        f"D-C is NOT fixed: the decay was applied more than once "
        f"(got {got}, expected {0.9 * expected_decay})")
    assert abs(got - 0.9 * expected_decay * expected_decay) > 1e-9, (
        "the score is the decay applied TWICE — D-C is not fixed")


#: The files that may legitimately mention D-A / D-C, and WHY.
#:
#: The guard is about COLLISION: a test that uses "D-C" to mean something
#: else (the foundation test once used it for "the four columns landed").
#: Two files talk about the REAL defects and are therefore allowed:
#:
#:   * this one -- it OWNS the status criteria for the two defects;
#:   * test_memory_rerank.py -- it carries the new behavioural criterion
#:     for the D-C fusion fix.
#:
#: An explicit list of files (each with its reason) is the honest shape
#: here.  The earlier shape -- a keyword list of "legitimate contexts" --
#: had to be extended with every new legitimate mention, which is exactly
#: how a guard like this gets quietly disabled.
G4_FILES_ALLOWED_TO_MENTION = {
    "test_p1_stage2f_g1g4.py":
        "this file owns the D-A / D-C status criteria",
    "test_memory_rerank.py":
        "carries the behavioural criterion for the D-C fusion fix",
    "test_p1_stage3a_holes.py":
        "carries the behavioural criteria for both fixes (P1-阶段三 S3-A/S3-B)",
}


def test_g4_no_number_collision_left_in_the_tests():
    """G-4: no test may use D-A/D-C to mean something else."""
    for p in (REPO / "tests").glob("test_*.py"):
        if p.name in G4_FILES_ALLOWED_TO_MENTION:
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        # A mention is legitimate only inside a collision notice: the block
        # that EXPLAINS the collision and points at the real status.
        for block in t.split("\n\n"):
            if "D-A" not in block and "D-C" not in block:
                continue
            legitimate = any(k in block for k in (
                "COLLISION", "collision", "G-4", "STILL UNFIXED",
                "registered", "NUMBER COLLISION",
            ))
            if legitimate:
                continue
            for line in block.splitlines():
                if "D-A" in line or "D-C" in line:
                    raise AssertionError(
                        f"G-4: {p.name} still uses D-A/D-C for something "
                        f"else: {line.strip()[:100]}")
