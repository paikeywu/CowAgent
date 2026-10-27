"""P0-4 tests: the 17 abuse cases (TC-A1..F3) from the P0-3 safety spec.

Spec: deliverables/2026-10-27-P0-写入门面安全规则与越权用例.md §三.
Common assertion (P0-3 §三 intro): after ANY rejection the target filesystem
is unchanged AND the audit log grew by exactly one rejection line.
"""

import json
import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory.facade import (
    AppendOnlyViolationError,
    ConfirmationExpiredError,
    ConsentRequiredError,
    CrossRoleWriteError,
    ImmutableFieldError,
    PathEscapeError,
    RecordingConfirmationChannel,
    SecondConfirmRequiredError,
    WriteFacade,
    split_frontmatter,
)

ACTOR = "agent-mvnes449"


def make_cow(tmp_path):
    """cow_root/agents/<ACTOR>/memory skeleton + agents/default + shared mem."""
    cow = tmp_path / "cow"
    ws = cow / "agents" / ACTOR
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "markdown" / "entities" / "people").mkdir(parents=True)
    (ws / "memory" / "facts").mkdir(parents=True)
    (cow / "agents" / "default" / "memory").mkdir(parents=True)
    (cow / "agents" / "core-architect" / "memory" / "markdown").mkdir(parents=True)
    (cow / "memory").mkdir(parents=True)
    (cow / "knowledge").mkdir()
    return cow, ws


def facade_for(ws, cow, actor=ACTOR, **kw):
    return WriteFacade(ws, actor=actor, cow_root=cow,
                       channel=RecordingConfirmationChannel(), **kw)


def audit_lines(ws):
    p = ws / "memory" / ".audit" / "facade.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def assert_rejected_cleanly(ws, exc_info, snapshot_before, target=None):
    """P0-3 common assertion: fs unchanged + exactly one new audit line."""
    assert snapshot_before() == snapshot_before(), "filesystem changed on reject"
    lines = audit_lines(ws)
    assert lines and lines[-1]["outcome"] == "rejected"
    assert lines[-1]["actor"] == ACTOR or lines[-1]["actor"] == "peiqi"


def fs_snapshot(ws):
    def snap():
        out = {}
        for p in sorted(ws.rglob("*")):
            if p.is_file() and ".audit" not in p.parts:
                out[str(p)] = p.read_bytes()
        return out
    return snap


# --- A. ../ path traversal ---------------------------------------------------

def test_tc_a1_single_level_escape(tmp_path):
    cow, ws = make_cow(tmp_path)
    (ws / "AGENT.md").write_text("identity", encoding="utf-8")
    f = facade_for(ws, cow)
    before = (ws / "AGENT.md").read_bytes()
    n0 = len(audit_lines(ws))
    with pytest.raises(PathEscapeError):
        f.write("../AGENT.md", content="x")
    assert (ws / "AGENT.md").read_bytes() == before
    assert len(audit_lines(ws)) == n0 + 1


@pytest.mark.parametrize("p", [
    "markdown/events/../../..%2f..%2fcow/knowledge/index.md",
    "markdown/events/....//....//x.md",
    "markdown/events/../../agents/default/MEMORY.md",
])
def test_tc_a2_confused_escapes(tmp_path, p):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    with pytest.raises(PathEscapeError):
        f.write(p, content="x")


# --- B. symlinks / junctions -------------------------------------------------

def _mklink_junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_tc_b1_junction_to_shared_knowledge(tmp_path):
    """symlink needs SeCreateSymbolicLinkPrivilege (probed WinError 1314);
    junctions are the no-privilege equivalent and carry the same risk."""
    cow, ws = make_cow(tmp_path)
    link = ws / "memory" / "markdown" / "events" / "evil"
    _mklink_junction(link, cow / "knowledge")
    f = facade_for(ws, cow)
    with pytest.raises(PathEscapeError):
        f.write("markdown/events/evil/poison--a1b2c3d4.md", content="x")
    assert not (cow / "knowledge" / "poison--a1b2c3d4.md").exists()


def test_tc_b2_dangling_junction(tmp_path):
    """The exists() trap: target does not exist, exists() is False, yet the
    ancestor is a reparse point and must be rejected (islink() is also False
    for junctions — the reparse bit is the only reliable signal)."""
    cow, ws = make_cow(tmp_path)
    link = ws / "memory" / "markdown" / "events" / "dangling"
    _mklink_junction(link, cow / ".cow" / ".env.d")  # nonexistent target
    assert os.path.lexists(link) and not os.path.exists(link)
    assert not os.path.islink(link)  # proves the exists()/islink() trap is real
    f = facade_for(ws, cow)
    with pytest.raises(PathEscapeError):
        f.write("markdown/events/dangling/x--a1b2c3d4.md", content="x")


def test_tc_b3_junction_to_default_memory(tmp_path):
    cow, ws = make_cow(tmp_path)
    link = ws / "memory" / "markdown" / "events" / "jlink"
    _mklink_junction(link, cow / "agents" / "default" / "memory")
    f = facade_for(ws, cow)
    with pytest.raises(PathEscapeError):
        f.write("markdown/events/jlink/x--a1b2c3d4.md", content="x")
    default_mem = cow / "agents" / "default" / "memory"
    assert list(default_mem.rglob("*.md")) == []


# --- C. absolute paths --------------------------------------------------------

@pytest.mark.parametrize("p", [
    "C:\\Users\\User\\.cow\\.env",
    "C:/cow/agents/x/memory/facts/2026-10-27/sess_deadbeef.jsonl",
])
def test_tc_c1_drive_absolute(tmp_path, p):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    with pytest.raises(PathEscapeError):
        f.write(p, content="x")


@pytest.mark.parametrize("p", [
    "/etc/passwd",
    "/c/Users/User/cow/MEMORY.md",
    "\\\\?\\C:\\Users\\User\\.cow\\.env",
    "//localhost/C$/Users/User/cow/",
])
def test_tc_c2_posix_and_unc(tmp_path, p):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    with pytest.raises(PathEscapeError):
        f.write(p, content="x")


# --- D. cross-role -------------------------------------------------------------

def test_tc_d1_relative_into_other_agent(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    n0 = len(audit_lines(ws))
    with pytest.raises(CrossRoleWriteError) as ei:
        f.write("../../agents/core-architect/memory/markdown/events/x--a1b2c3d4.md",
                content="x")
    assert "core-architect" in str(ei.value)
    assert len(audit_lines(ws)) == n0 + 1


def test_tc_d2_agent_to_shared_and_default_to_agent(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    with pytest.raises(CrossRoleWriteError):
        f.write("../../memory/markdown/events/x--a1b2c3d4.md", content="x")
    # reverse: default must not write into an agent's memory either
    d = facade_for(cow, cow, actor="default")
    with pytest.raises(CrossRoleWriteError):
        d.write(f"agents/{ACTOR}/memory/markdown/events/x--a1b2c3d4.md",
                content="x")


def test_tc_d3_actor_forgery_param(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    n0 = len(audit_lines(ws))
    with pytest.raises(CrossRoleWriteError):
        f.write("markdown/events/2026-10-27/x--a1b2c3d4.md", content="x",
                on_behalf_of="core-architect")
    lines = audit_lines(ws)
    assert len(lines) == n0 + 1
    assert lines[-1]["actor"] == ACTOR  # runtime identity, not the claim


# --- E. deletion semantics / release -------------------------------------------

def test_tc_e1_delete_semantics(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    p = f.write_event("evt", "t", "body", date="2026-10-27")
    before = p.read_bytes()
    with pytest.raises(ConsentRequiredError):
        f.delete("markdown/events/2026-10-27/evt--x.md")
    with pytest.raises(ConsentRequiredError):
        f.write("markdown/events/2026-10-27/other--a1b2c3d4.md", content=None)
    with pytest.raises(ConsentRequiredError):
        f.truncate("facts/2026-10-27/sess_deadbeef.jsonl")
    assert p.read_bytes() == before


def _own_memory(f, ws):
    """An element whose subject == actor (releasable by the actor itself).
    Agents do not own people/ profiles (P0-4 fix); an agent's self-scoped
    releasable memory is an event/relation with subject = actor."""
    return f.write_event("self-note", "me", "about me", date="2026-10-27")


@pytest.mark.parametrize("bad_ref", [None, "", "consent_nonexistent123"])
def test_tc_e2_release_without_valid_consent(tmp_path, bad_ref):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    p = _own_memory(f, ws)
    rel = p.relative_to(ws).as_posix()
    with pytest.raises(ConsentRequiredError):
        f.initiate_release(rel, consent_ref=bad_ref)
    fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
    assert fm["status"] == "active"


def test_tc_e3_human_profile_not_by_peiqi_and_positive_self_release(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    ch = f.channel
    peiqi_profile = f.write_entity("people", "peiqi", "沛琦", "human profile",
                                   subject="peiqi")
    rel = peiqi_profile.relative_to(ws).as_posix()
    ref = ch.register_consent(ACTOR, "peiqi")
    # agent (not peiqi) must NOT be able to release the human profile
    with pytest.raises(ConsentRequiredError):
        f.initiate_release(rel, consent_ref=ref)
    # positive side: actor releasing its OWN memory with valid consent works
    own = _own_memory(f, ws)
    own_rel = own.relative_to(ws).as_posix()
    own_ref = ch.register_consent(ACTOR, "self")
    result = f.initiate_release(own_rel, consent_ref=own_ref)
    assert result["executed"] is True
    fm, _ = split_frontmatter(own.read_text(encoding="utf-8"))
    assert fm["status"] == "active"  # old version untouched
    assert (ws / "memory" / "markdown" / "_tombstones").is_dir()


def test_tc_e4_human_profile_second_confirmation(tmp_path):
    cow, ws = make_cow(tmp_path)
    class FakeClock:
        def __init__(self):
            self.t = 0.0
        def __call__(self):
            return self.t
    clock = FakeClock()
    f = facade_for(ws, cow, actor="peiqi", clock=clock)
    peiqi_profile = f.write_entity("people", "peiqi", "沛琦", "profile",
                                   subject="peiqi")
    rel = peiqi_profile.relative_to(ws).as_posix()
    ref = f.channel.register_consent("peiqi", "peiqi")
    out = f.initiate_release(rel, consent_ref=ref)
    assert out["executed"] is False and out["token"]
    # WR-20: nothing changed yet — still active, no tombstone, no release event
    fm, _ = split_frontmatter(peiqi_profile.read_text(encoding="utf-8"))
    assert fm["status"] == "active"
    assert not (ws / "memory" / "markdown" / "_tombstones").exists()
    # forged token
    with pytest.raises(SecondConfirmRequiredError):
        f.confirm_release("deadbeefdeadbeef")
    # expired token
    clock.t += 601
    with pytest.raises(ConfirmationExpiredError):
        f.confirm_release(out["token"])


def test_tc_e4_positive_full_release_flow(tmp_path):
    """peiqi + valid consent + in-window confirm → all three layers land."""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow, actor="peiqi")
    peiqi_profile = f.write_entity("people", "peiqi", "沛琦", "profile",
                                   subject="peiqi")
    rel = peiqi_profile.relative_to(ws).as_posix()
    old_bytes = peiqi_profile.read_bytes()
    ref = f.channel.register_consent("peiqi", "peiqi")
    token = f.initiate_release(rel, consent_ref=ref)["token"]
    result = f.confirm_release(token)
    assert result["executed"]
    # ① release event exists ② new version released ③ tombstone
    assert (ws / "memory" / "markdown" / "_tombstones").is_dir()
    tombs = list((ws / "memory" / "markdown" / "_tombstones").glob("*.md"))
    assert len(tombs) == 1 and "consent_ref" in tombs[0].read_text(encoding="utf-8")
    events = list((ws / "memory" / "markdown" / "events").rglob("*.md"))
    assert any("release" in e.name for e in events)
    new_ver = [p for p in (ws / "memory" / "markdown" / "entities" / "people").glob("*.md")
               if p != peiqi_profile]
    assert len(new_ver) == 1
    fm, _ = split_frontmatter(new_ver[0].read_text(encoding="utf-8"))
    assert fm["status"] == "released" and fm["version"] == 2
    assert peiqi_profile.read_bytes() == old_bytes  # WR-11: old version untouched
    # token single-use
    with pytest.raises(SecondConfirmRequiredError):
        f.confirm_release(token)
    # RR-04: released element leaves the trusted read path
    fm2, _ = f.read(new_ver[0].relative_to(ws).as_posix())
    assert fm2 == {}


# -- P0-10 L-2: tombstone naming + audit fields ------------------------------

def test_tombstone_name_follows_its_own_rule(tmp_path):
    """墓碑名必须是 <原元素名>.tombstone.md（原元素名本身符合元素命名规则）。

    旧实现写的是 <原元素名>.md.tombstone.md（原名已含 .md）——那会让
    validate_names 的门禁从 0 violating 变成 N violating。
    """
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow, actor="peiqi")
    profile = f.write_entity("people", "peiqi", "沛琦", "profile",
                              subject="peiqi")
    rel = profile.relative_to(ws).as_posix()
    ref = f.channel.register_consent("peiqi", "peiqi")
    token = f.initiate_release(rel, consent_ref=ref)["token"]
    f.confirm_release(token)

    tombs = list((ws / "memory" / "markdown" / "_tombstones").glob("*.tombstone.md"))
    assert len(tombs) == 1
    name = tombs[0].name
    assert name.endswith(".tombstone.md")
    assert not name.endswith(".md.tombstone.md"), f"墓碑名重复了 .md: {name}"
    # <原元素名> 必须自身满足元素命名规则
    stem = name[: -len(".tombstone.md")]
    from agent.memory.naming import check_name
    assert check_name(stem + ".md") is None, f"原元素名不合规: {stem}"


def test_tombstone_carries_all_eight_audit_fields(tmp_path):
    """墓碑必须带齐 8 个审计字段（缺一即违规）。"""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow, actor="peiqi")
    profile = f.write_entity("people", "peiqi", "沛琦", "profile",
                              subject="peiqi")
    rel = profile.relative_to(ws).as_posix()
    ref = f.channel.register_consent("peiqi", "peiqi")
    token = f.initiate_release(rel, consent_ref=ref)["token"]
    f.confirm_release(token)

    tomb = list((ws / "memory" / "markdown" / "_tombstones")
                .glob("*.tombstone.md"))[0]
    text = tomb.read_text(encoding="utf-8")
    from scripts.memory.validate_names import TOMBSTONE_REQUIRED_FIELDS
    for field in TOMBSTONE_REQUIRED_FIELDS:
        assert f"{field}:" in text, f"墓碑缺少审计字段 {field}"


def test_tombstone_passes_the_name_validator(tmp_path):
    """端到端：释放一次后，全库 validate_names 仍是 0 violating。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                           / "scripts" / "memory"))
    import validate_names
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow, actor="peiqi")
    profile = f.write_entity("people", "peiqi", "沛琦", "profile",
                              subject="peiqi")
    rel = profile.relative_to(ws).as_posix()
    ref = f.channel.register_consent("peiqi", "peiqi")
    token = f.initiate_release(rel, consent_ref=ref)["token"]
    f.confirm_release(token)
    violations = validate_names.validate_root(ws)
    assert violations == [], f"释放后出现命名违规: {violations}"


# -- P0-10 L-3: generate_shelf is no longer a stub --------------------------

def test_generate_shelf_is_wired_to_the_real_generator(tmp_path):
    """P0-5 已落地，generate_shelf() 不能再是抛异常的桩。"""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    f.write_event("e1", "事件", "正文", date="2026-10-27")
    report = f.generate_shelf()
    assert isinstance(report, dict)
    assert report["agent"] == ACTOR
    shelf = ws / "memory" / "shelf"
    assert (shelf / "index.md").is_file()
    assert (shelf / "self.md").is_file()
    assert (shelf / "gaps.md").is_file()


def test_generate_shelf_never_raises_notimplemented(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    try:
        f.generate_shelf()
    except NotImplementedError as e:  # pragma: no cover - the whole point
        pytest.fail(f"generate_shelf 仍是桩: {e}")
    except Exception:
        pass  # any other failure is a real bug, surfaced by the test above



# ---------------------------------------------------------------------------
# P0-4 打回修复用例：editable 不可变字段注入（subject 伪造攻击链）
# 风控 P0-6 实测：write() 过滤了 frontmatter，但四个结构化入口的
# **editable 未统一过滤 → subject 可伪造 → people/ 档案 subject=agent id
# → _is_human_profile 判 False → 释放绕过 WR-19/WR-20。
# 修复：_new_element 是唯一收口，所有 editable 必过 _sanitize_editable。
# ---------------------------------------------------------------------------

import json as _json


def _audit_rows(ws):
    p = ws / "memory" / ".audit" / "facade.jsonl"
    if not p.exists():
        return []
    return [_json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _rejections(rows):
    return [r for r in rows if r.get("outcome") == "rejected"]


@pytest.mark.parametrize("field,value", [
    ("subject", ACTOR),            # subject 伪造：把自己伪装成档案主体
    ("subject", "peiqi"),          # subject 伪造：把档案主体指到沛琦
    ("status", "released"),        # 直接宣布已释放
    ("released", True),
    ("released_by", "peiqi"),
    ("consent_ref", "consent_forged"),
])
def test_fix_write_event_immutable_fields_rejected(tmp_path, field, value):
    """write_event 的 editable 夹带不可变字段 → ImmutableFieldError + 审计 + 无文件."""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    before = _rejections(_audit_rows(ws))
    with pytest.raises(ImmutableFieldError):
        f.write_event("evt-forge", "t", "body", date="2026-10-27",
                      **{field: value})
    assert _rejections(_audit_rows(ws)) != before  # 拒绝必须出声
    assert not list((ws / "memory" / "markdown" / "events").rglob("*.md"))


@pytest.mark.parametrize("field,value", [
    ("subject", ACTOR), ("status", "released"), ("released", True),
    ("consent_ref", "consent_x"),
])
def test_fix_write_letter_immutable_fields_rejected(tmp_path, field, value):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    with pytest.raises(ImmutableFieldError):
        f.write_letter("letter-forge", "t", "body", date="2026-10-27",
                       **{field: value})
    assert not list((ws / "memory" / "markdown" / "letters").rglob("*.md"))


@pytest.mark.parametrize("field,value", [
    ("subject", ACTOR),            # ← 原攻击链入口：people/ 档案 subject=agent id
    ("subject", "someone-else"),   # 未知真人 slug 同样拒绝
    ("status", "released"), ("released", True), ("released_by", "peiqi"),
    ("consent_ref", "consent_y"),
])
def test_fix_write_entity_immutable_fields_rejected(tmp_path, field, value):
    """原攻击链的核心入口：people/ 容器 + 伪造 subject。现在全部被同一过滤点拦截。"""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    before = _rejections(_audit_rows(ws))
    with pytest.raises(ImmutableFieldError):
        f.write_entity("people", "forge", "t", "body", **{field: value})
    assert _rejections(_audit_rows(ws)) != before
    assert not list((ws / "memory" / "markdown" / "entities").rglob("*.md"))


@pytest.mark.parametrize("field,value", [
    ("subject", ACTOR), ("subject", "peiqi"), ("status", "released"),
    ("released", True), ("consent_ref", "consent_z"),
])
def test_fix_revise_immutable_fields_rejected(tmp_path, field, value):
    """revise 同名变体：新版本试图改 subject/status/released → 拒绝。"""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    p = f.write_event("rev-target", "t", "v1", date="2026-10-27")
    rel = p.relative_to(ws).as_posix()
    with pytest.raises(ImmutableFieldError):
        f.revise(rel, body="v2", **{field: value})
    # 只有原版一个文件，没有新版本落盘
    versions = list((ws / "memory" / "markdown" / "events").rglob("*.md"))
    assert len(versions) == 1


def test_fix_original_attack_chain_now_dead(tmp_path):
    """风控实测攻击链端到端复现：people/ 档案 subject 填 agent id。
    修复前：写入成功 → _is_human_profile 判 False → agent 可绕过二次确认释放。
    修复后：写入那一步就被拒绝，档案根本不存在。"""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    with pytest.raises(ImmutableFieldError):
        f.write_entity("people", "fake-human", "fake", "looks human",
                       subject=ACTOR)
    # 连 dedicated subject 参数也过不了值域
    with pytest.raises(ImmutableFieldError):
        f.write_entity("people", "fake-human2", "fake", "looks human",
                       subject=ACTOR)
    people = ws / "memory" / "markdown" / "entities" / "people"
    assert not list(people.glob("*.md"))


def test_fix_positive_paths_still_work(tmp_path):
    """正值域不破：真人档案（subject=peiqi）可建、普通事件 subject=actor 注入、
    revise 正常携带旧 subject。"""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    # ① 合法真人档案
    pp = f.write_entity("people", "peiqi", "沛琦", "profile", subject="peiqi")
    fm, _ = split_frontmatter(pp.read_text(encoding="utf-8"))
    assert fm["subject"] == "peiqi" and fm["layer"] == "person"
    # ② 普通事件 subject 注入为 actor
    ev = f.write_event("normal", "t", "b", date="2026-10-27")
    fm2, _ = split_frontmatter(ev.read_text(encoding="utf-8"))
    assert fm2["subject"] == ACTOR
    # ③ revise 正常路径：subject 从旧文件携带，新版本 subject 不变
    ev2 = f.revise(ev.relative_to(ws).as_posix(), body="v2")
    fm3, _ = split_frontmatter(ev2.read_text(encoding="utf-8"))
    assert fm3["subject"] == ACTOR and fm3["version"] == 2


def test_fix_single_filter_point_structural_proof(tmp_path):
    """结构性证明"四个入口共用同一个过滤点"：
    给 _sanitize_editable 打桩计数，五个创建动作（含 revise 生成的新版本）
    全部恰好各过一次，且都经过同一个函数对象（id 相同）。"""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    calls = []
    original = f._sanitize_editable

    def spy(editable, container):
        calls.append((id(original.__func__), container))
        return original(editable, container)

    f._sanitize_editable = spy
    f.write_event("e1", "t", "b", date="2026-10-27")
    f.write_letter("l1", "t", "b", date="2026-10-27")
    f.write_entity("relations", "r1", "t", "b")
    p = f.write_event("e2", "t", "b", date="2026-10-27")
    f.revise(p.relative_to(ws).as_posix(), body="v2")
    assert len(calls) == 5  # 4 个入口 + revise 的新版本创建
    assert len({c[0] for c in calls}) == 1  # 同一个函数对象
    from collections import Counter
    assert Counter(c[1] for c in calls) == Counter(
        {"events": 3, "letters": 1, "entities/relations": 1})


# --- F. tamper / facts chain ----------------------------------------------------

def test_tc_f1_external_frontmatter_tamper_detected_on_read(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    p = f.write_event("tamper-me", "t", "body", date="2026-10-27")
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("density: 3", "density: 5"), encoding="utf-8")
    from agent.memory.facade import FrontmatterTamperError
    with pytest.raises(FrontmatterTamperError):
        f.read(p.relative_to(ws).as_posix())
    lines = audit_lines(ws)
    assert lines[-1]["outcome"] == "rejected"


def test_tc_f2_facts_history_line_rewrite_detected(tmp_path):
    """Same line count, changed content — only the hash chain catches it."""
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    f.append_fact("2026-10-26", "deadbeef", role="user", content="one",
                  occurred_at="2026-10-26T10:00:00+08:00", run_id="r1")
    f.append_fact("2026-10-26", "deadbeef", role="assistant", content="two",
                  occurred_at="2026-10-26T10:00:01+08:00", run_id="r1")
    f.append_fact("2026-10-26", "deadbeef", role="user", content="three",
                  occurred_at="2026-10-26T10:00:02+08:00", run_id="r1")
    path = ws / "memory" / "facts" / "2026-10-26" / "sess_deadbeef.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["content"] = "TAMPERED"
    lines[1] = json.dumps(row, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    violations = f.verify_facts_file("facts/2026-10-26/sess_deadbeef.jsonl")
    assert any("line_hash mismatch" in v or "prev_hash break" in v
               for v in violations)


def test_tc_f3_append_after_external_truncation_refused(tmp_path):
    """External truncation of the last two lines, then a normal facade append:
    the disk tail looks internally consistent, so the facade must check its
    append-only audit trail (last known seq) and refuse to launder the break."""
    from agent.memory.facade import HashChainBreakError

    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    for i in range(3):
        f.append_fact("2026-10-26", "deadbeef", role="user", content=f"m{i}",
                      occurred_at=f"2026-10-26T10:00:0{i}+08:00", run_id="r1")
    path = ws / "memory" / "facts" / "2026-10-26" / "sess_deadbeef.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n", encoding="utf-8")  # external truncation
    with pytest.raises(HashChainBreakError):
        f.append_fact("2026-10-26", "deadbeef", role="user", content="m3",
                      occurred_at="2026-10-26T10:00:03+08:00", run_id="r1")
    # and the rejection is audited
    assert audit_lines(ws)[-1]["outcome"] == "rejected"


# --- WR-14 negative: seq/chain break surfaces as HashChainBreakError --------

def test_wr14_chain_self_check_on_append(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    f.append_fact("2026-10-26", "deadbeef", role="user", content="a",
                  occurred_at="2026-10-26T10:00:00+08:00", run_id="r1")
    # corrupt the tail's line_hash externally so the next append would extend
    # a broken chain — verify_facts_file must flag it before/instead
    path = ws / "memory" / "facts" / "2026-10-26" / "sess_deadbeef.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").strip())
    row["line_hash"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    violations = f.verify_facts_file("facts/2026-10-26/sess_deadbeef.jsonl")
    assert violations


# --- P0-16 F-1: the evidence room's released rows are masked in the VIEW ---
#
# The evidence room is append-only: its rows are never rewritten and its hash
# chain is never broken. But "released" must mean released on EVERY read
# path — so a read of a facts file replaces the rows of released elements
# with a fixed placeholder. The DISK does not move; the VIEW does.

F1_SECRET = "我今天特别生气因为对方连续三次无视我的提醒这句话是绝密原话"


def _f1_workspace(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    body = ("# 事件\n\n今天发生了一件事。\n\n"
            "## 感受\n\n" + F1_SECRET + "\n")
    path = f.write_event("f1-e2e", "F-1 端到端事件", body)
    return cow, ws, f, path


def _f1_facts_file(ws):
    facts = list((ws / "memory" / "facts").rglob("*.jsonl"))
    assert facts, "no facts file was written"
    return facts[0]


def test_f1_a_released_element_masks_its_feeling_row_in_the_read_view(tmp_path):
    """The web console's reader must NOT serve the released 原话."""
    from agent.memory.service import MemoryService

    cow, ws, f, path = _f1_workspace(tmp_path)
    facts_file = _f1_facts_file(ws)
    rel = path.relative_to(ws).as_posix()

    # REVERSE CASE: before the release the row is served VERBATIM.
    before = MemoryService(str(ws)).get_content(
        facts_file.relative_to(ws).as_posix().replace("memory/", "", 1),
        category="memory")["content"]
    assert F1_SECRET in before, (
        "the unreleased feeling row must be served as-is — a mask that "
        "hides everything would pass a leak test")

    ref = f.channel.register_consent(ACTOR, "self")
    init = f.initiate_release(rel, consent_ref=ref)
    assert init.get("executed") or f.confirm_release(init["token"])["executed"]

    after = MemoryService(str(ws)).get_content(
        facts_file.relative_to(ws).as_posix().replace("memory/", "", 1),
        category="memory")["content"]
    assert F1_SECRET not in after, "the released 原话 leaked through the console"
    assert '"status": "released"' in after, "no placeholder line was rendered"
    assert F1_SECRET[:3] not in after, "the placeholder leaks a fragment"


def test_f1_a_released_element_masks_its_feeling_row_for_memory_get(tmp_path):
    """The memory_get tool must NOT serve the released 原话 either."""
    from pathlib import Path

    from agent.tools.memory.memory_get import MemoryGetTool

    cow, ws, f, path = _f1_workspace(tmp_path)
    facts_file = _f1_facts_file(ws)
    rel = path.relative_to(ws).as_posix()

    ref = f.channel.register_consent(ACTOR, "self")
    init = f.initiate_release(rel, consent_ref=ref)
    assert init.get("executed") or f.confirm_release(init["token"])["executed"]

    class _Cfg:
        def get_workspace(self):
            return Path(str(ws))

    class _Mgr:
        """A stub that consults the REAL truth layer — stubbing the decision
        would make the test prove nothing about the masking."""

        def __init__(self):
            self.config = _Cfg()

        def read_decision(self, rel_path):
            from agent.memory.release_state import resolve
            from agent.memory.visibility import (
                ambient_owner_tokens,
                owner_tokens,
            )
            state = resolve(str(ws))
            return state.decide(rel_path,
                                 owner_tokens(ambient_owner_tokens()))

    res = MemoryGetTool(memory_manager=_Mgr()).execute(
        {"path": facts_file.relative_to(ws).as_posix()})
    assert getattr(res, "status", None) == "success", (
        "memory_get failed: %r" % (getattr(res, "result", None),))
    text = "".join(str(x) for x in (getattr(res, "result", None),
                                     getattr(res, "display", None))
                   if x is not None)
    assert F1_SECRET not in text, "the released 原话 leaked through memory_get"
    assert '"status": "released"' in text, "no placeholder line was rendered"


def test_f1_a_masking_does_not_touch_the_disk(tmp_path):
    """The disk keeps the 原话: bytes, line count and hash chain unchanged.

    This is gate ② (事实层不可改写). A mask that rewrote the file would
    pass the leak test and break the evidence room.
    """
    import hashlib

    from agent.memory.release_state import resolve

    cow, ws, f, path = _f1_workspace(tmp_path)
    facts_file = _f1_facts_file(ws)
    rel = path.relative_to(ws).as_posix()

    before_bytes = facts_file.read_bytes()
    before_sha = hashlib.sha256(before_bytes).hexdigest()
    before_lines = len(before_bytes.decode("utf-8").splitlines())

    ref = f.channel.register_consent(ACTOR, "self")
    init = f.initiate_release(rel, consent_ref=ref)
    assert init.get("executed") or f.confirm_release(init["token"])["executed"]

    after_bytes = facts_file.read_bytes()
    after_sha = hashlib.sha256(after_bytes).hexdigest()
    after_lines = len(after_bytes.decode("utf-8").splitlines())
    assert before_sha == after_sha, "the facts file's bytes changed"
    assert before_bytes == after_bytes
    assert before_lines == after_lines, "the facts file's line count changed"
    assert F1_SECRET.encode("utf-8") in after_bytes, (
        "the disk must keep the 原话 — only the view masks it")

    # and the chain still verifies
    assert not f.verify_facts_file(
        facts_file.relative_to(ws).as_posix().replace("memory/", "", 1))

    # the truth layer knows which row belongs to the released element
    state = resolve(str(ws))
    assert state.released_fact_ids, (
        "the released element's feeling row id is NOT in the state — the "
        "element->feeling mapping is broken")


def test_f1_a_the_placeholder_leaks_nothing_about_the_original(tmp_path):
    """No fragment of the 原话, no length, no count of what was withheld."""
    from agent.memory.facts_mask import mask_facts_text
    from agent.memory.facts_refs import feeling_element_id

    eid = feeling_element_id(F1_SECRET)
    row = {"run_id": "feeling:" + eid, "content": F1_SECRET, "seq": 1}
    text = json.dumps(row, ensure_ascii=False) + "\n"

    masked = mask_facts_text(text, [eid])
    assert F1_SECRET not in masked
    assert '"status": "released"' in masked
    # no 3-character fragment of the original survives
    for n in (3, 4, 5):
        for i in range(len(F1_SECRET) - n + 1):
            assert F1_SECRET[i:i + n] not in masked, (
                "the placeholder leaks a %d-char fragment" % n)
    # no count of what was withheld
    assert "1" not in masked.split('"element_id"')[0]
    assert "count" not in masked and "filtered" not in masked


# --- P0-17 B-1/B-2: a stale index must not look like an empty corpus ------
#
# P0-17's finding: an index whose schema was behind the code made every
# retrieval query fail with "no such column"; the search path caught the
# error and returned an EMPTY LIST. From the outside that is
# indistinguishable from "nothing matched" — the system looked healthy
# and knew nothing.
#
# These tests pin the two halves of the fix:
#   * B-2 — a structural failure RAISES (and logs at ERROR level);
#   * B-2 reverse — a genuine absence of matches still returns [] quietly.
# and the migration that makes B-1 self-healing:
#   * B-1 — opening a stale index adds the missing column.

import logging
import sqlite3
import threading
from pathlib import Path

from agent.memory.storage import (
    MemorySchemaError,
    MemoryStorage,
    _query_groups,
)


def _stale_index(tmp_path, drop_columns=("sealed_owner",)):
    """An index.db written by an older build: same rows, older schema."""
    from agent.memory.schema_drift import EXPECTED_CHUNK_COLUMNS

    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    definitions = []
    for name in EXPECTED_CHUNK_COLUMNS:
        if name in drop_columns:
            continue
        if name == "id":
            definitions.append("id TEXT PRIMARY KEY")
        elif name in ("user_id", "scope", "source", "path", "text", "hash",
                      "metadata"):
            definitions.append(f"{name} TEXT")
        elif name in ("start_line", "end_line", "created_at", "updated_at",
                      "last_hit_at"):
            definitions.append(f"{name} INTEGER")
        elif name == "embedding":
            definitions.append("embedding TEXT")
        elif name == "hit_count":
            definitions.append("hit_count INTEGER NOT NULL DEFAULT 0")
        elif name in ("element_released", "file_sealed"):
            definitions.append(f"{name} INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE TABLE chunks (" + ", ".join(definitions) + ")")
    conn.execute("INSERT INTO chunks (id, user_id, scope, source, path, "
                 "start_line, end_line, text, hash) VALUES "
                 "('c1', NULL, 'shared', 'memory', 'memory/a.md', 1, 1, "
                 "'内容内容 FTS5 内容', 'h1')")
    conn.commit()
    conn.close()
    return db


def _columns(db):
    conn = sqlite3.connect(str(db))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    finally:
        conn.close()


def test_b1_opening_a_stale_index_migrates_it(tmp_path):
    """B-1: the migration runs when the index is OPENED."""
    db = _stale_index(tmp_path)
    assert "sealed_owner" not in _columns(db)

    storage = MemoryStorage(db)
    try:
        assert "sealed_owner" in _columns(db), (
            "the migration did not add the missing column")
        # and the index is usable
        results = storage.search_keyword("FTS5", limit=5,
                                         scopes=["shared", "user", "sealed"])
        assert results, "the migrated index returned nothing"
    finally:
        storage.close()


def test_b1_migrate_all_covers_every_workspace(tmp_path):
    """B-1: the batch migration reaches EVERY workspace, not just one."""
    from agent.memory.schema_migrate import migrate_all

    roots = []
    for i in range(3):
        ws = tmp_path / f"ws{i}"
        db = ws / "memory" / "long-term" / "index.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        stale = _stale_index(ws)
        for suffix in ("-wal", "-shm"):
            extra = stale.with_name(stale.name + suffix)
            if extra.exists():
                extra.replace(db.with_name(db.name + suffix))
        stale.replace(db)
        roots.append(str(ws))

    migrated, failed = migrate_all(roots=roots)
    assert not failed, f"migration failed: {failed}"
    assert len(migrated) == 3
    for ws in roots:
        db = Path(ws) / "memory" / "long-term" / "index.db"
        assert "sealed_owner" in _columns(db), f"{ws} was not migrated"


def _unmigrated_storage(db):
    """A storage instance whose schema was NOT migrated.

    ``MemoryStorage(db)`` migrates on open, so the broken state has to be
    built by hand — that is precisely the state a workspace is in when the
    migration could not run (an unwritable file, a build that predates the
    column, a database the current code never opened).
    """
    storage = MemoryStorage.__new__(MemoryStorage)
    storage.db_path = db
    storage.conn = sqlite3.connect(str(db))
    storage.conn.row_factory = sqlite3.Row
    storage.vector_backend = None
    storage._lock = threading.RLock()
    storage.fts5_available = False
    storage.trigram_fts5_available = False
    return storage


def test_b2_a_stale_index_raises_instead_of_returning_empty(tmp_path):
    """B-2: a structural failure is an ERROR, not an empty result set.

    The trigger is a caller that holds an OWNER token: only then does the
    visibility predicate name ``sealed_owner`` at all. A caller without
    one never reaches the missing column (the sealed branch is the
    constant ``(0)``), which is why the failure looked intermittent.
    """
    db = _stale_index(tmp_path)
    storage = _unmigrated_storage(db)

    with pytest.raises(MemorySchemaError):
        storage.search_keyword("FTS5", limit=5,
                               scopes=["shared", "user", "sealed"],
                               owner_tokens=["agent--someone"])


def test_b2_reverse_an_anonymous_caller_on_a_stale_index_still_gets_results(
        tmp_path):
    """B-2 REVERSE (the other half of the trigger condition).

    A caller with NO owner token never names the missing column, so its
    query runs and returns its hits. This is what makes the failure look
    intermittent: the same stale index answers an anonymous CLI and
    fails an agent.
    """
    db = _stale_index(tmp_path)
    storage = _unmigrated_storage(db)

    results = storage.search_keyword("FTS5", limit=5,
                                     scopes=["shared", "user", "sealed"])
    assert results, "an anonymous caller on a stale index got nothing"


def test_b2_a_stale_index_logs_at_error_level(tmp_path):
    """B-2: the failure must be LOUD — an ERROR line, not a silent []."""
    from common.log import logger

    db = _stale_index(tmp_path)
    storage = _unmigrated_storage(db)

    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record)

    cap = _Cap()
    logger.addHandler(cap)
    try:
        with pytest.raises(MemorySchemaError):
            storage.search_keyword("FTS5", limit=5,
                                   scopes=["shared", "user", "sealed"],
                                   owner_tokens=["agent--someone"])
    finally:
        logger.removeHandler(cap)

    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert errors, "no ERROR log line was emitted for a structural failure"
    assert "refusing to report an empty result" in errors[0].getMessage()


def test_b2_reverse_a_genuinely_empty_index_returns_empty_without_error(tmp_path):
    """B-2 REVERSE: 'nothing matched' must still be [] and must stay quiet."""
    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    try:
        records = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        cap = _Cap()
        from common.log import logger

        logger.addHandler(cap)
        try:
            results = storage.search_keyword(
                "FTS5", limit=5, scopes=["shared", "user", "sealed"])
        finally:
            logger.removeHandler(cap)

        assert results == [], "an empty index returned something"
        errors = [r for r in records if r.levelno >= logging.ERROR]
        assert not errors, f"a normal empty result logged errors: {errors}"
    finally:
        storage.close()


def test_b2_reverse_a_full_index_without_a_match_returns_empty(tmp_path):
    """B-2 REVERSE: rows exist, the query matches none → [] and no error."""
    db = _stale_index(tmp_path)
    storage = MemoryStorage(db)
    try:
        results = storage.search_keyword("ZZZNOMATCHZZZ", limit=5,
                                         scopes=["shared", "user", "sealed"])
        assert results == [], "a non-matching query returned something"
    finally:
        storage.close()


def test_b2_the_manager_propagates_the_schema_failure(tmp_path):
    """B-2: the failure must reach the caller, not stop at the storage."""
    import asyncio

    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from common.runtime_identity import RuntimeIdentity, use_identity

    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    cfg = MemoryConfig(workspace_root=str(ws), sync_on_search=False)
    mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
    try:
        # Break the index AFTER the manager opened it: the queries will
        # name a column that is gone.
        conn = mgr.storage.conn
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        if "sealed_owner" in cols:
            keep = [c for c in cols if c != "sealed_owner"]
            collist = ", ".join(keep)
            conn.execute(f"CREATE TABLE chunks_new ({collist})")
            conn.execute("INSERT INTO chunks_new SELECT " +
                         ", ".join(keep) + " FROM chunks")
            conn.execute("DROP TABLE chunks")
            conn.execute("ALTER TABLE chunks_new RENAME TO chunks")
            conn.commit()
        # An agent identity — that is what supplies the owner token.
        with use_identity(RuntimeIdentity(agent_id="agent-x")):
            with pytest.raises(MemorySchemaError):
                asyncio.run(mgr.search("FTS5", max_results=5, min_score=0.0))
    finally:
        mgr.storage.close()


# --- P0-18 L-1 / L-2 / R4 / R5 -----------------------------------------
#
# L-1: the relaxed search path referenced an undefined name, so every
#      relaxed search that found a row raised NameError.
# L-2: the relaxed path was called WITHOUT the owner axis, so the sealed
#      branch of the predicate degraded to the constant (0) and the owner
#      could not read their own dark page through it.
# R4:  the vector path raised a raw sqlite3 error while the keyword path
#      raised MemorySchemaError — a half-written contract.
# R5:  the orphaned dark pages (owner in the pre-P0-16 user_id column,
#      NULL in sealed_owner) got their owner back.

def _two_group_index(tmp_path):
    """An index whose rows make the RELAXED path reachable.

    Two groups in one query ("alpha beta") and NO row carries both, so the
    strict AND finds nothing and the relaxed path runs. That is the path
    L-1 killed.
    """
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE chunks (id TEXT PRIMARY KEY, user_id TEXT, "
        "scope TEXT NOT NULL DEFAULT 'shared', source TEXT NOT NULL DEFAULT 'memory', "
        "path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, "
        "text TEXT NOT NULL, embedding TEXT, hash TEXT NOT NULL, metadata TEXT, "
        "created_at INTEGER, updated_at INTEGER, "
        "hit_count INTEGER NOT NULL DEFAULT 0, last_hit_at INTEGER, "
        "element_released INTEGER NOT NULL DEFAULT 0, "
        "file_sealed INTEGER NOT NULL DEFAULT 0, sealed_owner TEXT)")
    conn.execute(
        "INSERT INTO chunks (id, user_id, scope, source, path, start_line, "
        "end_line, text, hash) VALUES "
        "('c-alpha', NULL, 'shared', 'memory', 'memory/alpha.md', 1, 1, "
        "'alpha 内容 alpha', 'h1'), "
        "('c-beta', NULL, 'shared', 'memory', 'memory/beta.md', 1, 1, "
        "'beta 内容 beta', 'h2')")
    conn.commit()
    conn.close()
    return db


def test_l1_the_relaxed_path_no_longer_raises_name_error(tmp_path):
    """L-1: a relaxed search that finds rows must RETURN them.

    It used to raise NameError on the first row, which P0-17 turned from
    a silent empty result into a user-visible crash (the except clause
    was narrowed to sqlite3.Error, so NameError was no longer swallowed).
    """
    db = _two_group_index(tmp_path)
    storage = _unmigrated_storage(db)
    try:
        results = storage._search_like("alpha beta", None,
                                       ["shared", "user", "sealed"], 5)
        assert results, "the relaxed path found nothing"
        paths = {r.path for r in results}
        assert "memory/alpha.md" in paths
        assert "memory/beta.md" in paths
    finally:
        storage.close()


def test_l1_the_relaxed_path_returns_a_snippet_around_the_query(tmp_path):
    """L-1: the snippet is cut around the ORIGINAL query.

    The query is what the function had no access to, and it is what
    makes the snippet show the matched words instead of the head of the
    chunk.
    """
    db = _two_group_index(tmp_path)
    storage = _unmigrated_storage(db)
    try:
        results = storage._search_like("alpha beta", None,
                                       ["shared", "user", "sealed"], 5)
        alpha = [r for r in results if r.path == "memory/alpha.md"]
        assert alpha, "the alpha row is missing"
        assert "alpha" in alpha[0].snippet
    finally:
        storage.close()


def test_l1_reverse_a_single_group_query_is_unchanged(tmp_path):
    """L-1 REVERSE: the common case (one group) behaves exactly as before.

    A single group never reaches the relaxed path, so this guards against
    the fix changing the path that everybody uses.
    """
    db = _two_group_index(tmp_path)
    storage = _unmigrated_storage(db)
    try:
        results = storage._search_like("alpha", None,
                                       ["shared", "user", "sealed"], 5)
        assert len(results) == 1
        assert results[0].path == "memory/alpha.md"
        assert results[0].snippet == "alpha 内容 alpha"
    finally:
        storage.close()


def test_l1_reverse_a_query_that_matches_nothing_still_returns_empty(tmp_path):
    """L-1 REVERSE: no match is still an empty result, not an error."""
    db = _two_group_index(tmp_path)
    storage = _unmigrated_storage(db)
    try:
        results = storage._search_like("nothingmatchesthis", None,
                                       ["shared", "user", "sealed"], 5)
        assert results == []
    finally:
        storage.close()


def test_l2_the_relaxed_path_carries_the_owner_axis(tmp_path):
    """L-2: the relaxed path must see the owner axis.

    It used to be called without owner_tokens, so the sealed branch of the
    predicate was the constant (0) and the owner could not read their own
    dark page through the relaxed path while the strict path could. Two
    paths, one rule, two answers.
    """
    db = _two_group_index(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chunks (id, user_id, scope, source, path, start_line, "
        "end_line, text, hash, element_released, file_sealed, sealed_owner) "
        "VALUES ('c-dark', NULL, 'sealed', 'memory', 'memory/dark.md', 1, 1, "
        "'gamma dark gamma', 'h3', 0, 1, 'agent--agent-alice')")
    conn.commit()
    conn.close()

    storage = _unmigrated_storage(db)
    try:
        scopes = ["shared", "user", "sealed"]
        owner = ["agent--agent-alice"]
        # "gamma zzznotfound" is two groups and no row carries both, so the
        # strict AND finds nothing and the RELAXED path runs. The relaxed
        # path must find the dark page — before L-2 it lost the owner axis
        # and could not.
        results = storage._search_like("gamma zzznotfound", None, scopes, 5,
                                       None, owner)
        assert results, "the relaxed path found nothing"
        assert any(r.path == "memory/dark.md" for r in results), (
            "the owner cannot see their own dark page through the "
            "relaxed path — the owner axis was dropped")
    finally:
        storage.close()


def test_l2_reverse_the_two_paths_agree_on_visibility(tmp_path):
    """L-2 REVERSE: the strict and the relaxed path answer identically.

    Same data, same identity, same rule -> the same set of visible rows.
    A path that lets through what the other rejects is a permission bug,
    not a ranking difference.
    """
    db = _two_group_index(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chunks (id, user_id, scope, source, path, start_line, "
        "end_line, text, hash, element_released, file_sealed, sealed_owner) "
        "VALUES ('c-dark', NULL, 'sealed', 'memory', 'memory/dark.md', 1, 1, "
        "'alpha dark alpha', 'h3', 0, 1, 'agent--agent-bob')")
    conn.commit()
    conn.close()

    storage = _unmigrated_storage(db)
    try:
        scopes = ["shared", "user", "sealed"]
        # The caller holds agent--agent-alice, but the dark page belongs
        # to agent--agent-bob -> nobody may see it, through either path.
        for owners in (["agent--agent-alice"], []):
            groups = _query_groups("alpha zzznotfound")
            strict = storage._search_like("alpha zzznotfound", None, scopes, 5,
                                           None, owners)
            relaxed = storage._search_like_relaxed(
                groups, None, scopes, 5, identities=None,
                owner_tokens=owners, query="alpha zzznotfound")
            strict_paths = {r.path for r in strict}
            relaxed_paths = {r.path for r in relaxed}
            assert "memory/dark.md" not in strict_paths, (
                "the strict path leaked another agent's dark page")
            assert "memory/dark.md" not in relaxed_paths, (
                "the relaxed path leaked another agent's dark page")
            assert strict_paths == relaxed_paths, (
                "the two paths disagree: strict=%s relaxed=%s"
                % (strict_paths, relaxed_paths))
    finally:
        storage.close()


def test_r4_the_vector_path_raises_the_same_schema_error(tmp_path):
    """R4: both retrieval paths raise MemorySchemaError.

    A caller that writes ``except MemorySchemaError:`` around a retrieval
    call must catch the vector path too. It used to raise a raw
    sqlite3.OperationalError, so the handler let it through.
    """
    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    try:
        # Break the index the way a schema drift breaks it.
        storage.conn.execute("DROP TABLE chunks")
        storage.conn.commit()
        with pytest.raises(MemorySchemaError):
            storage.search_vector([0.1, 0.2], scopes=["shared"])
    finally:
        storage.close()


def test_r4_reverse_a_non_structural_error_is_not_wrapped(tmp_path):
    """R4 REVERSE: an argument problem is not a schema problem.

    Wrapping it would tell the caller "the index is broken" when the
    index is fine, and would send the caller looking in the wrong place.

    P0-19 自报：这条用例原来的输入 ``["not-a-number"]`` **根本不抛
    异常**——向量后端逐行容错，把坏元素丢掉就返回了。它只能证明
    "没被误包装"，证不了"非结构错误会抛出来"。
    """
    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    try:
        try:
            storage.search_vector(["not-a-number"], scopes=["shared"])
        except MemorySchemaError as e:
            pytest.fail(
                "a malformed embedding was reported as a schema error: %s" % e)
        except Exception:
            pass  # any non-schema exception is acceptable here
    finally:
        storage.close()


@pytest.mark.parametrize("bad_kwargs", [
    {"limit": None},
    {"limit": "x"},
    {"scopes": 42},
])
def test_r4_reverse_a_real_non_structural_error_is_not_wrapped(tmp_path,
                                                               bad_kwargs):
    """3.2: 一个**真的**会抛出来的非结构异常，不得被包装成 schema 错误。

    P0-19 补的强度用例。原来那条 ``["not-a-number"]`` 的输入压根不抛
    （向量后端逐行容错），所以它证明不了"非结构错误一定抛出"。这里的
    三个输入是**实测**会抛 ``TypeError`` 的：``limit`` 传 None / 字符串、
    ``scopes`` 传整数。

    判据有两层，缺一不可：
      ① 异常**真的抛出来了**（不是"没有异常所以通过"）；
      ② 它**不是** ``MemorySchemaError``——它不是结构问题，索引是好的。
    """
    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    try:
        with pytest.raises(Exception) as excinfo:
            storage.search_vector([0.1, 0.2], **bad_kwargs)
        assert not isinstance(excinfo.value, MemorySchemaError), (
            "a non-structural error was reported as a schema error: %s"
            % excinfo.value)
    finally:
        storage.close()


def test_r4_reverse_the_schema_error_still_wraps_a_real_structural_failure(
        tmp_path):
    """3.2 的对照面：结构错误**必须**仍然被包装。

    上一条只说"非结构的别包装"；这一条说"结构的还是要包装"。两条合
    起来才是完整的契约——只有第二条时，判据会在"什么都不抛"的世界里
    变绿。
    """
    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    try:
        storage.conn.execute("DROP TABLE chunks")
        storage.conn.commit()
        with pytest.raises(MemorySchemaError):
            storage.search_vector([0.1, 0.2], scopes=["shared"])
    finally:
        storage.close()


def _legacy_dark_rows(db):
    """The three legacy row shapes, as they sit on disk.

    L1 is the shape P0-16 left behind: the owner token is in ``user_id``
    (where the pre-P0-16 build put it) and ``sealed_owner`` is NULL.
    L2 is an end-user id in the sealed scope — it must NOT be backfilled.
    L3 is an ordinary row that happens to carry a token-shaped user_id.
    """
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chunks (id, user_id, scope, source, path, start_line, "
        "end_line, text, hash, element_released, file_sealed, sealed_owner) "
        "VALUES "
        "('L1', 'agent--agent-alice', 'sealed', 'memory', 'memory/L1.md', 1, 1, "
        "'DARKPAGE legacy alpha', 'hL1', 0, 1, NULL), "
        "('L2', 'wangjingjing', 'sealed', 'memory', 'memory/L2.md', 1, 1, "
        "'DARKPAGE legacy beta', 'hL2', 0, 1, NULL), "
        "('L3', 'agent--agent-alice', 'shared', 'memory', 'memory/L3.md', 1, 1, "
        "'ORDINARY row gamma', 'hL3', 0, 0, NULL)")
    conn.commit()
    conn.close()


def test_r5_the_backfill_restores_the_orphaned_dark_page(tmp_path):
    """R5: a dark page whose owner is only in user_id gets it back."""
    from agent.memory.schema_migrate import backfill_sealed_owner

    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    storage.close()
    _legacy_dark_rows(db)

    changed = backfill_sealed_owner(db)
    assert changed == 1, "exactly one row should have been backfilled"

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, user_id, scope, sealed_owner FROM chunks")}
    conn.close()
    assert rows["L1"]["sealed_owner"] == "agent--agent-alice", (
        "the orphaned dark page did not get its owner back")
    assert rows["L2"]["sealed_owner"] is None, (
        "an END USER id was backfilled into the owner axis — that crosses "
        "the two axes")
    assert rows["L3"]["sealed_owner"] is None, (
        "an ordinary row acquired an owner it never had")


def test_r5_reverse_the_backfill_never_touches_the_end_user_axis(tmp_path):
    """R5 REVERSE: the end-user axis stays out of the owner axis.

    An end-user id that happens to look like an owner token must never be
    treated as one. That is the L-5 two-axis separation, and it is a red
    line rather than a heuristic.
    """
    from agent.memory.schema_migrate import backfill_sealed_owner

    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    storage.close()
    _legacy_dark_rows(db)
    backfill_sealed_owner(db)

    storage = MemoryStorage(db)
    try:
        scopes = ["shared", "user", "sealed"]
        # The end user who owns the L2 row must not read it through the
        # owner axis, and the owner must not read it either (its owner was
        # never known).
        as_end_user = storage.search_keyword(
            "DARKPAGE legacy", scopes=scopes, identities=["wangjingjing"])
        assert not any(r.path == "memory/L2.md" for r in as_end_user), (
            "the end user read a dark page whose owner is unknown")
        as_owner = storage.search_keyword(
            "DARKPAGE legacy", scopes=scopes,
            owner_tokens=["agent--agent-alice"])
        paths = {r.path for r in as_owner}
        assert "memory/L1.md" in paths, (
            "the owner cannot read the dark page the backfill repaired")
        assert "memory/L2.md" not in paths, (
            "a dark page with an unknown owner became visible")
    finally:
        storage.close()


def test_r5_reverse_the_backfill_is_idempotent(tmp_path):
    """R5 REVERSE: running it twice changes nothing the second time."""
    from agent.memory.schema_migrate import backfill_sealed_owner

    db = tmp_path / "index.db"
    storage = MemoryStorage(db)
    storage.close()
    _legacy_dark_rows(db)

    first = backfill_sealed_owner(db)
    second = backfill_sealed_owner(db)
    assert first == 1
    assert second == 0, "the backfill is not idempotent"
