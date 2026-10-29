"""P1-阶段二-E · C-8 + C-10 反向用例。

**C-8（P1 · 审计链可污染）**：``old_fm.get(key)`` 把「字段缺失」和
「字段存在但值为 null」混为一谈。生产 ``write_event`` 产出的文件里
``consent_ref`` / ``released_at`` / ``released_by`` **正是 null** → 这三项
永远不被比较 → 伪造 ``consent_ref``（WR-18 的释放凭据）或把
``released_by`` 栽给谁都���合法导入。

**C-10（P3 · 假绿的新形态）**：C-7 的两条规则互为冗余 ——
``..`` 被拒 → 走不到 realpath 边界；realpath 越界 → 必然含 ``..``。
于是任一条单独坏掉，判据仍全绿（被兄弟规则遮蔽）。

本文件给每条规则配一条**能单独暴露它**的判据：

  * ``..`` 规则的专属判据：**含 ``..`` 段、但 realpath 仍在 root 内**
    的路径（``a/../b.md``）。删掉 ``..`` 规则 → realpath 判定放行它 →
    判据转红；删掉 realpath 规则 → ``..`` 规则仍拦 → 判据**不**转红。
  * realpath 规则的专属判据：**symlink 越界**（root 内 junction 指向
    root 外）。不含 ``..``、非绝对路径 → 只有 realpath 规则能拦。
    删掉 realpath 规则 → 放行 → 判据转红；删掉 ``..`` 规则 → realpath
    规则仍拦 → 判据**不**转红。

**判据一律经模块属性调用**（``paths.path_escape_reason(...)``），
不直接 import 函数名 —— 否则 monkeypatch 换掉模块属性时，判据仍在跑旧
函数引用，破坏验证就成了自说自话。
"""
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from agent.memory import paths
from agent.memory.facade import (
    IMMUTABLE_FM_FIELDS,
    tampered_immutable_fields,
)


# ===========================================================================
# C-8 · immutable 字段的存在性判断
# ===========================================================================

def _fm(**over):
    """A front-matter shaped like a production ``write_event`` file."""
    base = {
        "id": "e1", "layer": "events", "type": "conversation",
        "occurred_at": "2026-10-29T10:00:00+08:00",
        "recorded_at": "2026-10-29T10:00:01+08:00",
        "status": "active", "version": 1, "supersedes": [],
        "superseded_by": None, "subject": "primus", "released": False,
        "released_by": None, "released_at": None, "consent_ref": None,
        "fm_hash": "sha256:x", "slug": "note", "hash8": "aaaaaaaa",
        "author_viewpoint": "primus",
    }
    base.update(over)
    return base


def test_c8_null_valued_fields_are_compared():
    """C-8 判据 2: a null ``consent_ref`` forged into a value MUST be caught."""
    out = tampered_immutable_fields(_fm(), _fm(consent_ref="forged-consent-ref"))
    assert any(e.startswith("consent_ref:") for e in out), \
        f"C-8: forging consent_ref was NOT detected: {out}"


def test_c8_released_by_forgery_is_caught():
    """C-8 判据 3: ``released_by: attacker`` MUST be caught."""
    out = tampered_immutable_fields(_fm(), _fm(released_by="attacker"))
    assert any(e.startswith("released_by:") for e in out), \
        f"C-8: pinning released_by on somebody else was NOT detected: {out}"


def test_c8_released_at_forgery_is_caught():
    """C-8 判据 4: a forged ``released_at`` MUST be caught."""
    out = tampered_immutable_fields(
        _fm(), _fm(released_at="2026-10-29T00:00:00+08:00"))
    assert any(e.startswith("released_at:") for e in out), \
        f"C-8: forging released_at was NOT detected: {out}"


def test_c8_null_to_null_is_not_tampering():
    """C-8: null -> null is EQUAL (a legal edit must not be punished)."""
    out = tampered_immutable_fields(_fm(), _fm())
    assert out == [], f"C-8: an unchanged front-matter was reported as tampered: {out}"


def test_c8_absent_key_is_skipped_not_rejected():
    """C-8 判据 6: a key the file NEVER had is skipped, not judged tampered.

    Deleting the comparison wholesale would reject a legal edit on a file
    that simply lacks the key — the other half of the same bug.
    """
    old = _fm()
    del old["consent_ref"]      # the file never had this key
    new = _fm(consent_ref="forged")
    out = tampered_immutable_fields(old, new)
    assert out == [], \
        f"C-8: a key absent at birth was judged tampered: {out}"


@pytest.mark.parametrize("field", sorted(IMMUTABLE_FM_FIELDS))
def test_c8_every_immutable_field_is_individually_guarded(field):
    """C-8 判据 1: all 18 immutable fields are guarded, ONE BY ONE.

    This is the table the delivery document shows: every field, not a
    sample. The field is changed in the OLD front-matter (so the
    comparison definitely has two different values to look at) and the
    change must be reported.
    """
    old = _fm()
    new = _fm()
    old[field] = "OLD-VALUE"
    new[field] = "NEW-VALUE"
    out = tampered_immutable_fields(old, new)
    assert any(e.startswith(f"{field}:") for e in out), \
        f"C-8: field {field!r} is NOT guarded: {out}"


@pytest.mark.parametrize("field", ["consent_ref", "released_at", "released_by"])
def test_c8_null_birth_fields_are_guarded(field):
    """C-8: the three null-valued production fields are guarded TOO.

    The parametrized sweep above changes both sides to non-null values.
    This one keeps the OLD value null (as a production file has it) and
    forges a value on the NEW side — the exact shape of the defect.
    """
    old = _fm()
    assert old[field] is None, f"fixture wrong: {field} is not null at birth"
    new = _fm(**{field: "forged-value"})
    out = tampered_immutable_fields(old, new)
    assert any(e.startswith(f"{field}:") for e in out), \
        f"C-8: field {field!r} (null at birth) is NOT guarded: {out}"


def test_c8_mutation_back_to_is_none_makes_the_criterion_fail():
    """C-8 mutant 重放: ``old_val is None`` → the criterion MUST go red.

    The mutation is the C-8 bug, reintroduced. If the criterion still
    passed after the mutation, the criterion was measuring nothing.
    """
    def _mutant(old_fm, new_fm, fields=IMMUTABLE_FM_FIELDS):
        out = []
        for key in sorted(fields):
            if key not in old_fm:
                continue
            old_val = old_fm.get(key)
            new_val = new_fm.get(key)
            if old_val is None:          # ← the C-8 bug
                continue
            if str(old_val) != str(new_val):
                out.append(f"{key}: {old_val!r} -> {new_val!r}")
        return out

    # The criterion BEFORE the mutation — it must be green.
    assert any(e.startswith("consent_ref:")
               for e in tampered_immutable_fields(_fm(), _fm(
                   consent_ref="forged-consent-ref")))

    # Under the mutation the criterion MUST go red.
    with pytest.raises(AssertionError):
        out = _mutant(_fm(), _fm(consent_ref="forged-consent-ref"))
        assert any(e.startswith("consent_ref:") for e in out), (
            "C-8 破坏验证失败：把 'key in old_fm' 改回 'old_val is None' 后，"
            "伪造 consent_ref 仍然被检出 —— 说明这条判据没有真正依赖"
            "那条规则")

    # And the mutant really is the C-8 bug: the forgery slips through.
    assert _mutant(_fm(), _fm(consent_ref="forged-consent-ref")) == []


def test_c8_the_presence_rule_lives_in_one_place():
    """C-8: the presence decision is decided in ONE function."""
    import agent.memory.facade as facade_mod
    src = inspect.getsource(facade_mod)
    # The only place that may ask "is this key present?" for the immutable
    # comparison is tampered_immutable_fields.
    body = inspect.getsource(tampered_immutable_fields)
    assert "if key not in old_fm:" in body, \
        "C-8: the presence rule must be decided on the KEY, not on the value"
    # The caller must not re-implement the comparison.
    caller = inspect.getsource(facade_mod.WriteFacade.import_human_edit)
    assert "old_val is None" not in caller, \
        "C-8: import_human_edit still contains its own copy of the " \
        "null-vs-absent decision — the rule must live in ONE place"
    assert "tampered_immutable_fields(old_fm, new_fm)" in caller


# ===========================================================================
# C-10 · 两条路径规则互不遮蔽
# ===========================================================================

def _make_junction(root: Path, target: Path, name: str) -> Path:
    """A directory junction (Windows) or symlink (POSIX) inside ``root``.

    A junction needs no special privilege on Windows (``symlink`` does),
    and ``Path.resolve()`` follows both.
    """
    link = root / name
    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"cannot create a junction here: {r.stderr.strip()}")
    else:
        link.symlink_to(target, target_is_directory=True)
    return link


@pytest.fixture
def escape_root(tmp_path):
    """A root containing a junction that points OUTSIDE the root."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    (root / "b.md").write_text("b\n", encoding="utf-8")
    (root / "a").mkdir()
    _make_junction(root, outside, "link")
    return root, outside


def test_c10_dotdot_rule_has_a_criterion_of_its_own(escape_root):
    """C-10: the '..' rule is guarded by a criterion that ONLY it can fail.

    The path contains a '..' segment but resolves INSIDE the root — so the
    realpath rule cannot be the one that catches it. If the '..' rule is
    deleted, this path is allowed through and the criterion turns red.
    """
    root, _outside = escape_root
    reason = paths.path_escape_reason("a/../b.md", root)
    assert reason == "contains a '..' segment", \
        f"C-10: expected the '..' rule to fire, got {reason!r}"


def test_c10_realpath_rule_has_a_criterion_of_its_own(escape_root):
    """C-10: the realpath rule is guarded by a criterion that ONLY it can fail.

    The path has no '..' segment and is not absolute — the only rule that
    can catch it is the realpath boundary rule (the junction points out
    of the root).
    """
    root, _outside = escape_root
    reason = paths.path_escape_reason("link/secret.txt", root)
    assert reason == "resolves outside the workspace root", \
        f"C-10: expected the realpath rule to fire, got {reason!r}"


def _mutant_no_dotdot(relpath, root):
    """paths.path_escape_reason WITHOUT the '..' rule (the C-10 mutant M3)."""
    if relpath is None:
        return "empty path"
    raw = str(relpath)
    if not raw.strip():
        return "empty path"
    if "\\" in raw:
        return "contains a backslash"
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return "absolute path"
    norm = paths.normalize_relpath(raw)
    if not norm:
        return "empty path"
    try:
        root_res = Path(root).resolve()
        real = (root_res / norm).resolve()
        if os.path.commonpath([str(root_res), str(real)]) != str(root_res):
            return "resolves outside the workspace root"
    except (OSError, ValueError):
        return "cannot be resolved within the root"
    return None


def _mutant_no_realpath(relpath, root):
    """paths.path_escape_reason WITHOUT the realpath rule (mutant M4)."""
    if relpath is None:
        return "empty path"
    raw = str(relpath)
    if not raw.strip():
        return "empty path"
    if "\\" in raw:
        return "contains a backslash"
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return "absolute path"
    norm = paths.normalize_relpath(raw)
    if not norm:
        return "empty path"
    if ".." in norm.split("/"):
        return "contains a '..' segment"
    return None


def test_c10_mutants_are_not_shadowed_by_each_other(escape_root, monkeypatch):
    """C-10 mutant 重放: M3 and M4 each turn EXACTLY ONE criterion red.

    This is the proof that the two rules are not redundant. The old
    criterion set had no way to tell: M3 and M4 both left everything
    green, because each rule's failure was covered by its sibling.
    """
    root, _outside = escape_root

    def _criterion_dotdot():
        reason = paths.path_escape_reason("a/../b.md", root)
        assert reason == "contains a '..' segment", \
            f"C-10: the '..' rule did not fire: {reason!r}"

    def _criterion_realpath():
        reason = paths.path_escape_reason("link/secret.txt", root)
        assert reason == "resolves outside the workspace root", \
            f"C-10: the realpath rule did not fire: {reason!r}"

    # Baseline: both green.
    _criterion_dotdot()
    _criterion_realpath()

    # M3: the '..' rule is broken -> ONLY the '..' criterion turns red.
    monkeypatch.setattr(paths, "path_escape_reason", _mutant_no_dotdot)
    with pytest.raises(AssertionError):
        _criterion_dotdot()
    _criterion_realpath()      # ← the sibling criterion stays GREEN

    monkeypatch.undo()

    # M4: the realpath rule is broken -> ONLY the realpath criterion turns red.
    monkeypatch.setattr(paths, "path_escape_reason", _mutant_no_realpath)
    with pytest.raises(AssertionError):
        _criterion_realpath()
    _criterion_dotdot()        # ← the sibling criterion stays GREEN

    monkeypatch.undo()

    # Restored: both green again.
    _criterion_dotdot()
    _criterion_realpath()


def test_c10_the_mutants_really_are_different(escape_root):
    """C-10: the two mutants behave differently on the two criteria.

    Without this, the mutant replay above would be self-referential: a
    mutant that happened to fail BOTH criteria would still "pass" the
    non-shadowing assertion for the wrong reason.
    """
    root, _outside = escape_root
    # M3 (no '..' rule) lets the in-root '..' path through.
    assert _mutant_no_dotdot("a/../b.md", root) is None
    # …but M3 still catches the junction escape (via the realpath rule).
    assert _mutant_no_dotdot("link/secret.txt", root) is not None
    # M4 (no realpath rule) lets the junction escape through.
    assert _mutant_no_realpath("link/secret.txt", root) is None
    # …but M4 still catches the '..' path (via the '..' rule).
    assert _mutant_no_realpath("a/../b.md", root) is not None


def test_c10_the_two_criteria_really_need_the_two_rules(escape_root):
    """C-10: neither criterion can be satisfied by the other rule.

    Static proof of the non-shadowing: the '..' criterion's path resolves
    INSIDE the root (so the realpath rule cannot fire on it), and the
    realpath criterion's path has no '..' segment (so the '..' rule
    cannot fire on it).
    """
    root, _outside = escape_root
    root_res = root.resolve()
    # The '..' path resolves inside the root -> only the '..' rule can fire.
    assert (root_res / "a" / ".." / "b.md").resolve() == \
        (root_res / "b.md").resolve()
    # The junction path resolves OUTSIDE the root -> only realpath can fire.
    assert not str((root_res / "link" / "secret.txt").resolve()).startswith(
        str(root_res))
    # …and it carries no '..' segment.
    assert ".." not in "link/secret.txt".split("/")


def test_c10_paths_is_still_the_only_rule_source():
    """C-10: the path rules stay in paths.py (one source)."""
    import agent.memory
    root = Path(agent.memory.__file__).parent
    offenders = []
    for mod in root.glob("*.py"):
        if mod.name == "paths.py":
            continue
        text = mod.read_text(encoding="utf-8")
        if "commonpath" in text and "path_escape_reason" not in text:
            offenders.append(mod.name)
    assert not offenders, \
        f"C-10: these modules re-implement the path boundary rule: {offenders}"


# ===========================================================================
# C-8 · 端到端：import_human_edit 必须真的拒绝伪造
# ===========================================================================

def _import_setup(tmp_path, extra_fm=""):
    """A truth file shaped like a production one (null consent_ref etc.)."""
    from agent.memory import index_guard, rebuilder
    from agent.memory.facade import WriteFacade
    from agent.memory.storage import MemoryStorage
    ws = tmp_path
    md = ws / "memory" / "markdown" / "events"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "markdown" / "_tombstones").mkdir(parents=True, exist_ok=True)
    f = md / "2026-10-29-note--aaaaaaaa.md"
    fm = ("id: e1\nhash8: aaaaaaaa\noccurred_at: 2026-10-29T10:00:00+08:00\n"
          "author_viewpoint: agent--primus\nstatus: active\n"
          "consent_ref: null\nreleased_by: null\nreleased_at: null\n"
          + extra_fm)
    f.write_text(f"---\n{fm}---\n\noriginal body\n", encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuilder.rebuild_file(st, ws, f)
    facade = WriteFacade(ws, actor="primus", cow_root=ws)
    rel = "memory/markdown/events/2026-10-29-note--aaaaaaaa.md"
    return ws, f, st, facade, rel


@pytest.mark.parametrize("forged", [
    "consent_ref: forged-consent-ref",
    "released_by: attacker",
    "released_at: 2026-01-01T00:00:00+08:00",
])
def test_c8_end_to_end_forgery_is_rejected(tmp_path, forged):
    """C-8 end-to-end: the forgery must be refused at the import channel.

    The unit test above exercises the comparison function; this one
    exercises the CHANNEL — the thing a human editor actually hits.
    """
    from agent.memory.facade import FrontmatterTamperError
    ws, f, st, facade, rel = _import_setup(tmp_path)
    key, value = forged.split(": ", 1)
    f.write_text(
        f"---\nid: e1\nhash8: aaaaaaaa\n"
        f"occurred_at: 2026-10-29T10:00:00+08:00\n"
        f"author_viewpoint: agent--primus\nstatus: active\n"
        f"consent_ref: null\nreleased_by: null\nreleased_at: null\n"
        f"{key}: {value}\n---\n\noriginal body\n",
        encoding="utf-8")
    with pytest.raises(FrontmatterTamperError):
        facade.import_human_edit(rel, storage=st)


def test_c8_end_to_end_legal_edit_still_imports(tmp_path):
    """C-8: a legal edit (body appended, no immutable field touched) imports."""
    ws, f, st, facade, rel = _import_setup(tmp_path)
    f.write_text(
        "---\nid: e1\nhash8: aaaaaaaa\n"
        "occurred_at: 2026-10-29T10:00:00+08:00\n"
        "author_viewpoint: agent--primus\nstatus: active\n"
        "consent_ref: null\nreleased_by: null\nreleased_at: null\n"
        "---\n\noriginal body\n\nnew section appended\n",
        encoding="utf-8")
    out = facade.import_human_edit(rel, storage=st)
    assert out is not None


def test_c8_end_to_end_edit_without_the_keys_is_not_punished(tmp_path):
    """C-8 判据 6 end-to-end: a file that NEVER had the key is not punished."""
    from agent.memory import index_guard, rebuilder
    from agent.memory.facade import WriteFacade
    from agent.memory.storage import MemoryStorage
    ws = tmp_path
    md = ws / "memory" / "markdown" / "events"
    md.mkdir(parents=True)
    (ws / "memory" / "long-term").mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "markdown" / "_tombstones").mkdir(parents=True, exist_ok=True)
    f = md / "2026-10-29-note--bbbbbbbb.md"
    f.write_text(
        "---\nid: e1\nhash8: bbbbbbbb\n"
        "occurred_at: 2026-10-29T10:00:00+08:00\n"
        "author_viewpoint: agent--primus\n---\n\noriginal body\n",
        encoding="utf-8")
    st = MemoryStorage(db_path=ws / "memory" / "long-term" / "index.db")
    index_guard.ensure_guard(st.conn)
    rebuilder.rebuild_file(st, ws, f)
    facade = WriteFacade(ws, actor="primus", cow_root=ws)
    rel = "memory/markdown/events/2026-10-29-note--bbbbbbbb.md"
    # The human appends a section; the file still has no consent_ref key.
    f.write_text(
        "---\nid: e1\nhash8: bbbbbbbb\n"
        "occurred_at: 2026-10-29T10:00:00+08:00\n"
        "author_viewpoint: agent--primus\n---\n\n"
        "original body\n\nnew section appended\n",
        encoding="utf-8")
    out = facade.import_human_edit(rel, storage=st)
    assert out is not None, \
        "C-8: a legal edit on a file that lacks the key was rejected"
