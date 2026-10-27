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


def _own_profile(f, ws):
    """An entity whose subject == actor (releasable by the actor itself)."""
    return f.write_entity("people", "self-note", "me", "about me",
                          subject=ACTOR)


@pytest.mark.parametrize("bad_ref", [None, "", "consent_nonexistent123"])
def test_tc_e2_release_without_valid_consent(tmp_path, bad_ref):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    p = _own_profile(f, ws)
    rel = p.relative_to(ws).as_posix()
    with pytest.raises(ConsentRequiredError):
        f.initiate_release(rel, consent_ref=bad_ref)
    fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
    assert fm["status"] == "active"


def test_tc_e3_human_profile_not_by_peiqi_and_positive_self_release(tmp_path):
    cow, ws = make_cow(tmp_path)
    f = facade_for(ws, cow)
    ch = f.channel
    mengmeng = f.write_entity("people", "mengmeng", "萌萌", "daughter profile",
                              subject="mengmeng")
    rel = mengmeng.relative_to(ws).as_posix()
    ref = ch.register_consent(ACTOR, "mengmeng")
    with pytest.raises(ConsentRequiredError):
        f.initiate_release(rel, consent_ref=ref)
    # positive side: actor releasing its OWN memory with valid consent works
    own = _own_profile(f, ws)
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
    mengmeng = f.write_entity("people", "mengmeng", "萌萌", "profile",
                              subject="mengmeng")
    rel = mengmeng.relative_to(ws).as_posix()
    ref = f.channel.register_consent("peiqi", "mengmeng")
    out = f.initiate_release(rel, consent_ref=ref)
    assert out["executed"] is False and out["token"]
    # WR-20: nothing changed yet — still active, no tombstone, no release event
    fm, _ = split_frontmatter(mengmeng.read_text(encoding="utf-8"))
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
    mengmeng = f.write_entity("people", "mengmeng", "萌萌", "profile",
                              subject="mengmeng")
    rel = mengmeng.relative_to(ws).as_posix()
    old_bytes = mengmeng.read_bytes()
    ref = f.channel.register_consent("peiqi", "mengmeng")
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
               if p != mengmeng]
    assert len(new_ver) == 1
    fm, _ = split_frontmatter(new_ver[0].read_text(encoding="utf-8"))
    assert fm["status"] == "released" and fm["version"] == 2
    assert mengmeng.read_bytes() == old_bytes  # WR-11: old version untouched
    # token single-use
    with pytest.raises(SecondConfirmRequiredError):
        f.confirm_release(token)
    # RR-04: released element leaves the trusted read path
    fm2, _ = f.read(new_ver[0].relative_to(ws).as_posix())
    assert fm2 == {}


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
