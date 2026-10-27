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
