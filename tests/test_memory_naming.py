"""Tests for agent.memory.naming (live-memory v2 naming contract)."""

import random
import string

import pytest

from agent.memory.naming import (
    PATH_MAX_BYTES,
    SLUG_MAX_BYTES,
    build_memory_path,
    canonical_text,
    check_name,
    check_relpath,
    content_hash8,
    normalize_slug,
)


def test_path_shape_and_determinism():
    p1 = build_memory_path("events", "My First Event!", "hello", date="2026-10-27")
    p2 = build_memory_path("events", "my first event", "hello", date="2026-10-27")
    assert p1 == p2  # slug normalization + content hash make it deterministic
    assert p1.startswith("memory/markdown/events/2026-10-27/")
    assert p1.endswith(f"--{content_hash8('hello')}.md")


def test_dated_vs_undated_containers():
    with pytest.raises(ValueError):
        build_memory_path("events", "x", "c")  # date required
    with pytest.raises(ValueError):
        build_memory_path("entities/people", "x", "c", date="2026-10-27")
    with pytest.raises(ValueError):
        build_memory_path("feelings", "x", "c")  # unknown container


def test_chinese_slug_survives():
    p = build_memory_path("events", "遗忘伦理确立", "内容", date="2026-10-27")
    assert "遗忘伦理确立" in p
    assert len(p.encode("utf-8")) <= PATH_MAX_BYTES


def test_no_ascii_slug_falls_back_to_hash():
    p = build_memory_path("letters", "！！！", "c", date="2026-10-27")
    name = p.rsplit("/", 1)[-1]
    assert name == f"{content_hash8('c')}--{content_hash8('c')}.md" or \
        name.startswith(content_hash8("c"))


def test_slug_cannot_escape_container():
    for evil in ("../../etc/passwd", "..\\..\\win", "a:b", "a/b"):
        p = build_memory_path("events", evil, "c", date="2026-10-27")
        parts = p.split("/")
        assert parts[:3] == ["memory", "markdown", "events"]
        assert ".." not in parts
        assert ":" not in p and "\\" not in p


def test_truncation_never_cuts_hash8():
    slug = "很长的标题" * 30  # way over budget
    p = build_memory_path("events", slug, "c", date="2026-10-27")
    assert len(p.encode("utf-8")) <= PATH_MAX_BYTES
    assert p.endswith(f"--{content_hash8('c')}.md")


def test_revision_suffix():
    p = build_memory_path("events", "x", "c", date="2026-10-27", revision=2)
    assert p.endswith("-2.md")
    assert check_name(p.rsplit("/", 1)[-1]) is None


def test_budget_property_1000_inputs():
    """02 §5.3 acceptance: 1000 random inputs, all outputs within budget,
    none escaping the container."""
    rng = random.Random(42)
    alphabet = string.ascii_letters + string.digits + " 一-龥遗忘伦理./\\:--"
    for i in range(1000):
        slug = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 120)))
        content = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 400)))
        container = rng.choice(["events", "entities/people"])
        date = "2026-10-27" if container == "events" else None
        p = build_memory_path(container, slug, content, date=date)
        assert len(p.encode("utf-8")) <= PATH_MAX_BYTES, p
        assert check_relpath(p) is None, p
        parts = p.split("/")
        assert ".." not in parts


def test_check_name_rules():
    assert check_name("遗忘伦理确立--b2c3d4e5.md") is None
    assert check_name("peiqi--agent-mvnerelm--f6a7b8c9.md") is None  # slug with --
    assert check_name("plain.md") is not None                        # no hash
    assert check_name("x--A1B2C3D4.md") is not None                  # uppercase hash
    assert check_name("a" * 41 + "--a1b2c3d4.md") is not None        # slug > 40 bytes
    assert check_name("a" * 40 + "--a1b2c3d4.md") is None


def test_check_relpath_budget():
    assert check_relpath("memory/shelf/index.md") is None
    assert check_relpath("memory/shelf/" + "记" * 30 + ".md") is not None


def test_canonical_text_normalization():
    assert canonical_text("a\r\nb\r\n") == canonical_text("a\nb")
    assert canonical_text("  a\n\n\n\nb  ") == "a\n\nb"
    assert content_hash8("a\r\nb") == content_hash8("a\nb")


def test_normalize_slug_byte_safe_on_cjk_boundary():
    # A truncation point inside a multi-byte char must not produce mojibake.
    slug = normalize_slug("记" * 20)  # 60 bytes -> truncated to <= 40
    assert len(slug.encode("utf-8")) <= SLUG_MAX_BYTES
    assert all(ch == "记" for ch in slug)


# -- P0-10 L-2: the tombstone rule set --------------------------------------
#
# A tombstone is not a memory element, so the element contract does not
# apply — but "not an element" is not an exemption. It gets its own rule
# set, stricter in the ways that matter: the name must point at a name
# that could exist, and the file must carry all 8 audit fields.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "memory"))
import validate_names  # noqa: E402


TOMBSTONE_DIR = "memory/markdown/_tombstones"

GOOD_TOMBSTONE = """---
original_path: memory/markdown/entities/people/peiqi--a1b2c3d4.md
original_hash: sha256:abc
released_by: peiqi
released_at: 2026-10-27T10:00:00+08:00
consent_ref: consent_123
confirmation_token_hash: sha256:def
initiated_at: 2026-10-27T09:59:00+08:00
confirmed_at: 2026-10-27T10:00:00+08:00
---

墓碑：原档案已释放。
"""


def test_valid_tombstone_passes():
    problems = validate_names.validate_file(
        f"{TOMBSTONE_DIR}/peiqi--a1b2c3d4.tombstone.md", GOOD_TOMBSTONE)
    assert problems == [], problems


def test_tombstone_missing_audit_field_is_violation():
    """缺任一审计字段即违规（逐个字段都试一遍）。"""
    for field in validate_names.TOMBSTONE_REQUIRED_FIELDS:
        broken = "\n".join(
            line for line in GOOD_TOMBSTONE.splitlines()
            if not line.startswith(field + ":"))
        problems = validate_names.validate_file(
            f"{TOMBSTONE_DIR}/peiqi--a1b2c3d4.tombstone.md", broken)
        assert problems, f"缺 {field} 未被抓出"
        assert any("fields" in p for p in problems)


def test_tombstone_name_must_point_at_a_valid_element_name():
    """墓碑名里的原元素名本身必须满足元素命名规则。"""
    for bad_stem in ("plain", "x--A1B2C3D4", "a" * 41 + "--a1b2c3d4"):
        problems = validate_names.validate_file(
            f"{TOMBSTONE_DIR}/{bad_stem}.tombstone.md", GOOD_TOMBSTONE)
        assert problems, f"{bad_stem} 未被抓出"
        assert any("name" in p for p in problems)


def test_tombstone_name_must_end_with_the_suffix():
    """不是 .tombstone.md 结尾即违规。"""
    for bad_name in ("peiqi--a1b2c3d4.md", "peiqi--a1b2c3d4",
                     "peiqi--a1b2c3d4.tombstone.txt"):
        problems = validate_names.validate_file(
            f"{TOMBSTONE_DIR}/{bad_name}", GOOD_TOMBSTONE)
        assert problems, f"{bad_name} 未被抓出"
        assert any("name" in p for p in problems)


def test_old_double_md_tombstone_name_is_violation():
    """旧实现写的 <原名>.md.tombstone.md 必须被判违规（这正是 L-2 的形态）。"""
    problems = validate_names.validate_file(
        f"{TOMBSTONE_DIR}/peiqi--a1b2c3d4.md.tombstone.md", GOOD_TOMBSTONE)
    assert problems
    assert any("name" in p for p in problems)


def test_tombstone_still_obeys_the_byte_budget():
    """墓碑不是豁免：字节预算照常适用。"""
    long_stem = "记" * 30 + "--a1b2c3d4"
    problems = validate_names.validate_file(
        f"{TOMBSTONE_DIR}/{long_stem}.tombstone.md", GOOD_TOMBSTONE)
    assert any("budget" in p for p in problems)


def test_tombstone_container_is_not_reported_as_unknown():
    """_tombstones 是合法容器（专属规则），不是 'unknown container'。"""
    problems = validate_names.validate_file(
        f"{TOMBSTONE_DIR}/peiqi--a1b2c3d4.tombstone.md", GOOD_TOMBSTONE)
    assert not any("unknown markdown container" in p for p in problems)


# --- P0-16: the naming contract must not drift with the two-axis split ---
#
# P0-16 L-5 split the identity axes: the end-user id and the agent owner
# token are now different namespaces. The owner token is NOT a user id, so
# it must not be spelled like one — and the naming rules must not start
# accepting a file whose identity claims the agent namespace.

def test_an_agent_owner_token_is_not_a_user_id():
    """'agent--x' is the AGENT namespace; 'user--x' is the end-user one.

    The two are separate namespaces and separate columns (P0-16 L-5). A
    rule that treats one as the other is how an end-user id shaped like an
    owner token could read a dark page.
    """
    from agent.memory.visibility import (
        AGENT_OWNER_PREFIX,
        agent_owner_token,
        is_reserved_end_user_id,
    )
    from agent.memory.identity_ns import AGENT_PREFIX, USER_PREFIX

    assert AGENT_OWNER_PREFIX == AGENT_PREFIX == "agent--"
    assert USER_PREFIX == "user--"
    assert agent_owner_token("alice") == "agent--alice"
    assert is_reserved_end_user_id("agent--alice")
    assert not is_reserved_end_user_id("user--alice")
    assert not is_reserved_end_user_id("alice")


def test_the_owner_token_does_not_change_the_memory_path_contract():
    """The path contract is unchanged: a dark page is still an element."""
    from agent.memory.naming import check_name, content_hash8

    # a normal element FILE name is still valid
    assert not check_name("记一件事--%s.md" % content_hash8("body"))
    # and the naming rules are not loosened by the axis split
    assert check_name("bad/name.md")
    assert check_name("no-hash.md")
    assert check_name("记一件事.md")
