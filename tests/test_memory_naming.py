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
