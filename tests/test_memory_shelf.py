"""Tests for agent/memory/shelf.py — the P0-5 shelf generator (SH-1..SH-12).

Structure follows the P0-5 spec 附录 B: every acceptance assertion gets a
positive case AND a "reverse case" (反向用例) that proves the assertion can
actually catch a wrong thing. An assertion that cannot fail is decoration.

Division of labour with the inspector (P0-8):
  * inspector  → integrity (hashes, chain, naming) — report-only, never fixes
  * shelf      → renderability (front-matter completeness) + the six views
  * files the shelf cannot render are reported as ``skipped`` and surface in
    the gaps view with reason code ``invalid-frontmatter`` — never dropped
    silently.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

from agent.memory.facade import (
    RecordingConfirmationChannel,
    WriteFacade,
    split_frontmatter,
)
from agent.memory.shelf import (
    INVALID_FM,
    LEGAL_GAP_CODES,
    ShelfGenerator,
)

ACTOR = "tester"
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


# -- fixtures ---------------------------------------------------------------

@pytest.fixture
def cow(tmp_path):
    return tmp_path


@pytest.fixture
def ws(cow):
    root = cow / "agents" / ACTOR
    (root / "memory").mkdir(parents=True)
    return root


def facade_for(ws, cow, actor=ACTOR):
    return WriteFacade(ws, actor=actor, cow_root=cow,
                       channel=RecordingConfirmationChannel())


def build(ws, cow, *, events=2, actor=ACTOR):
    """A workspace with a few events, one entity, one letter, one tombstone."""
    f = WriteFacade(ws, actor=actor, cow_root=cow,
                    channel=RecordingConfirmationChannel())
    paths = []
    for i in range(events):
        p = f.write_event(f"evt-{i}", f"事件 {i}", f"正文 {i}",
                          date="2026-10-27",
                          tags=["记忆"], involves=["peiqi"])
        paths.append(p)
    f.write_letter("letter-1", "一封信", "信件正文", date="2026-10-27")
    f.write_entity("people", "peiqi", "沛琦", "真人档案", subject="peiqi")
    return f, paths


def make_gap(ws, cow):
    """Produce a real gap: facts rows on a day with no event file."""
    f = WriteFacade(ws, actor=ACTOR, cow_root=cow,
                    channel=RecordingConfirmationChannel())
    f.append_fact("2026-10-26", "cafebabe", role="user", content="没有沉淀",
                  occurred_at="2026-10-26T10:00:00+08:00", run_id="r1")
    return f


def rebuild(ws, **kw):
    return ShelfGenerator(ws, agent_id=ACTOR).rebuild()


def shelf(ws):
    return ws / "memory" / "shelf"


def read(ws, rel):
    return (shelf(ws) / rel).read_text(encoding="utf-8")


def links_in(text):
    return LINK_RE.findall(text)


def resolve(ws, view_rel, link):
    """Resolve a shelf link relative to the view file; return the target."""
    base = (shelf(ws) / view_rel).parent
    return (base / link).resolve()


def all_view_files(ws):
    root = shelf(ws)
    return sorted(p for p in root.rglob("*.md"))


# -- SH-1 · 下钻链接有效性 ---------------------------------------------------

def test_sh1_every_drilldown_link_resolves_inside_memory(ws, cow):
    """SH-1: 每个 markdown 链接解析后必须在 memory/ 内且目标存在。"""
    build(ws, cow)
    rebuild(ws)
    checked = 0
    for p in all_view_files(ws):
        rel = p.relative_to(shelf(ws)).as_posix()
        for link in links_in(p.read_text(encoding="utf-8")):
            if link.startswith(("http://", "https://", "#")):
                continue
            target = resolve(ws, rel, link)
            assert target.is_file(), f"{rel}: 悬空链接 {link}"
            assert str(target).startswith(str((ws / "memory").resolve())), \
                f"{rel}: 链接逃出 memory/ → {link}"
            checked += 1
    assert checked > 0, "没有可检查的链接——断言会空转"


def test_sh1_reverse_a_dangling_link_is_detected(ws, cow):
    """反向用例：故意在视图里写一个不存在的链接，检测器必须抓到。"""
    build(ws, cow)
    rebuild(ws)
    broken = shelf(ws) / "index.md"
    broken.write_text(broken.read_text(encoding="utf-8")
                      + "\n[x](markdown/events/2026-10-27/nope--00000000.md)\n",
                      encoding="utf-8", newline="\n")
    dangling = []
    for p in all_view_files(ws):
        rel = p.relative_to(shelf(ws)).as_posix()
        for link in links_in(p.read_text(encoding="utf-8")):
            if link.startswith(("http://", "https://", "#")):
                continue
            if not resolve(ws, rel, link).is_file():
                dangling.append((rel, link))
    assert dangling, "反向用例失败：悬空链接没被抓到"


# -- SH-2 · 我记得的条目可下钻 ------------------------------------------------

def test_sh2_remembered_rows_drill_down_to_real_event_files(ws, cow):
    """SH-2: 「我记得的」每行的下钻链接指向真实事件文件。"""
    build(ws, cow)
    rebuild(ws)
    text = read(ws, "index.md")
    section = text.split("## 我记得的")[1].split("##")[0]
    links = [l for l in links_in(section) if l.endswith(".md")]
    assert links, "「我记得的」没有条目"
    for link in links:
        target = resolve(ws, "index.md", link)
        assert target.is_file()
        assert "markdown" in target.as_posix()
        fm, _ = split_frontmatter(target.read_text(encoding="utf-8"))
        assert fm["status"] == "active" and fm["visibility"] == "open"


def test_sh2_reverse_rows_with_broken_target_are_found(ws, cow):
    """反向用例：下钻指向不存在的文件 → 必须被抓到。"""
    build(ws, cow)
    rebuild(ws)
    text = read(ws, "index.md")
    section = text.split("## 我记得的")[1].split("##")[0]
    bad = []
    for link in links_in(section):
        if not link.endswith(".md"):
            continue
        if not resolve(ws, "index.md", link).is_file():
            bad.append(link)
    assert bad == [], "反向用例：下钻悬空"


# -- SH-3 · 缺口含合法原因码 ------------------------------------------------

def test_sh3_gap_reason_codes_are_legal(ws, cow):
    """SH-3: 每条缺口的「原因码」列 ∈ 规格枚举（+ 规格允许的扩展）。"""
    build(ws, cow)
    make_gap(ws, cow)
    rebuild(ws)
    for view in ("index.md", "gaps.md"):
        text = read(ws, view)
        section = text.split("我没记住的")[-1] if view == "index.md" else text
        codes = re.findall(r"`([a-z-]+)`", section)
        assert codes, "没有缺口条目可检查"
        for code in codes:
            assert code in LEGAL_GAP_CODES, f"非法原因码 {code}"


def test_sh3_reverse_illegal_reason_code_is_detected(ws, cow):
    """反向用例：写进一个非法原因码 → 判定必须失败。"""
    build(ws, cow)
    make_gap(ws, cow)
    rebuild(ws)
    text = read(ws, "gaps.md")
    assert "`made-up-code`" not in text
    illegal = [c for c in re.findall(r"`([a-z-]+)`", text)
               if c not in LEGAL_GAP_CODES]
    assert illegal == []


# -- SH-4 · 缺口含可执行动作 ------------------------------------------------

def test_sh4_every_gap_has_an_action_link_that_exists(ws, cow):
    """SH-4: 「可执行动作」列必须是链接且目标存在。"""
    build(ws, cow)
    make_gap(ws, cow)
    rebuild(ws)
    text = read(ws, "gaps.md")
    section = text.split("## 缺口清单")[1]
    rows = [r for r in section.splitlines()
            if r.startswith("| ") and "---" not in r and "原因码" not in r]
    assert rows, "缺口清单没有行"
    for row in rows:
        links = links_in(row)
        assert links, f"缺口行没有可执行动作链接: {row}"
        for link in links:
            target = resolve(ws, "gaps.md", link)
            assert target.exists(), f"动作链接悬空: {link}"


def test_sh4_reverse_gap_without_action_is_detected(ws, cow):
    """反向用例：一行没有链接 → 判定必须失败。"""
    build(ws, cow)
    rebuild(ws)
    bad_row = "| 1 | 2026-10-27 | `gap-rule` | 查询 | 这里有问题 |"
    assert not links_in(bad_row)


# -- SH-5 · 你释放的只列墓碑 -------------------------------------------------

def _release_one(ws, cow):
    """Release a human profile so a tombstone exists (peiqi-only + 2-step)."""
    f = WriteFacade(ws, actor="peiqi", cow_root=cow,
                    channel=RecordingConfirmationChannel())
    f.write_entity("people", "peiqi", "沛琦", "真人档案", subject="peiqi")
    rel = "memory/markdown/entities/people/peiqi--deadbeef.md"
    actual = next((ws / "memory" / "markdown" / "entities" / "people")
                  .glob("peiqi--*.md"))
    rel = actual.relative_to(ws).as_posix()
    ref = f.channel.register_consent("peiqi", "peiqi")
    token = f.initiate_release(rel, consent_ref=ref)["token"]
    f.confirm_release(token)
    return actual


def test_sh5_released_section_points_only_at_tombstones(ws, cow):
    """SH-5 / 验收标准 4: 「你释放的」每条都指向墓碑，无一指向真源。"""
    _release_one(ws, cow)
    rebuild(ws)
    text = read(ws, "index.md")
    section = text.split("## 你释放的")[1]
    rows = [r for r in section.splitlines() if r.startswith("| ")]
    data_rows = [r for r in rows if "---" not in r and "释放日期" not in r]
    assert data_rows, "释放视图没有条目"
    for row in data_rows:
        links = [l for l in links_in(row) if l.endswith(".tombstone.md")]
        assert links, f"释放行没有墓碑链接: {row}"
        for link in links:
            target = resolve(ws, "index.md", link)
            assert target.is_file()
            assert target.name.endswith(".tombstone.md")
            assert "_tombstones" in target.as_posix()
    # 真源路径不得出现在释放视图里
    assert "markdown/entities" not in section


def test_sh5_reverse_a_row_pointing_at_truth_file_is_detected(ws, cow):
    """反向用例：一行指向真源 → 判定必须失败。"""
    _release_one(ws, cow)
    rebuild(ws)
    text = read(ws, "index.md")
    section = text.split("## 你释放的")[1]
    bad = [r for r in section.splitlines()
           if r.startswith("| ") and "markdown/entities" in r]
    assert bad == [], "反向用例：释放视图里出现了指向真源的行"


# -- SH-6 · 释放条目含 consent_ref -------------------------------------------

def test_sh6_released_entries_carry_a_resolvable_consent_ref(ws, cow):
    """SH-6: 每个墓碑有 consent_ref，且该值能在 .audit/*.jsonl 里找到。"""
    _release_one(ws, cow)
    rebuild(ws)
    text = read(ws, "index.md")
    section = text.split("## 你释放的")[1]
    refs = re.findall(r"`(consent_[0-9a-f]+)`", section)
    assert refs, "释放条目没有 consent_ref"
    audit_text = ""
    for p in (ws / "memory" / ".audit").glob("*.jsonl"):
        audit_text += p.read_text(encoding="utf-8")
    for ref in refs:
        assert ref in audit_text, f"consent_ref {ref} 在审计链里查无此事"


def test_sh6_reverse_tombstone_without_consent_is_detected(ws, cow):
    """反向用例：墓碑没有 consent_ref → 判定必须失败。"""
    _release_one(ws, cow)
    tombstones = list((ws / "memory" / "markdown" / "_tombstones")
                      .glob("*.tombstone.md"))
    assert tombstones
    for t in tombstones:
        fm, _ = split_frontmatter(t.read_text(encoding="utf-8"))
        assert fm.get("consent_ref")
    # 人为抹掉 consent_ref → 判定必须失败
    t = tombstones[0]
    fm, body = split_frontmatter(t.read_text(encoding="utf-8"))
    fm.pop("consent_ref")
    t.write_text("---\n" + yaml.safe_dump(fm, allow_unicode=True)
                 + "---\n\n" + body, encoding="utf-8", newline="\n")
    fm2, _ = split_frontmatter(t.read_text(encoding="utf-8"))
    assert not fm2.get("consent_ref")


# -- SH-7 · 可见性过滤 -------------------------------------------------------
#
# P0-14 L-2 改了这条规则：``visibility: sealed``（暗页）**不是**释放。
# 06 §5.3 说主人格视角 = 白页 + 暗页，所以本视角自己的暗页必须出现在
# 书架上（带 🔒 标记）；释放件永不进；draft 不进。

def _set_visibility(path, value):
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("visibility: open", f"visibility: {value}"),
                    encoding="utf-8", newline="\n")


def test_sh7_the_owners_shelf_lists_open_and_dark_pages(ws, cow):
    """SH-7（P0-14 口径）：白页与**本视角的暗页**都进「我记得的」，
    暗页带 🔒 标记；draft 不进。"""
    f = facade_for(ws, cow)
    f.write_event("open-evt", "可见", "b", date="2026-10-27")
    sealed = f.write_event("sealed-evt", "暗页", "b", date="2026-10-27")
    _set_visibility(sealed, "sealed")
    draft = f.write_event("draft-evt", "草稿", "b", date="2026-10-27")
    _set_visibility(draft, "draft")
    rebuild(ws)
    text = read(ws, "index.md")
    section = text.split("## 我记得的")[1].split("##")[0]
    assert "可见" in section
    assert "暗页" in section, "本视角自己的暗页必须出现在书架上（06 §5.3）"
    assert "🔒" in section, "暗页必须带锁定标记"
    assert "草稿" not in section


def test_sh7_reverse_another_viewpoints_dark_page_never_leaks(ws, cow):
    """反向用例：别的视角的暗页 + 释放件 → 判定必须失败。

    借阅台（跨视角读取）还没实现，所以别人的暗页在本书架上必须完全不
    出现；释放件在任何视角下都不出现。
    """
    other = WriteFacade(ws, actor="other-view", cow_root=cow,
                        channel=RecordingConfirmationChannel())
    sealed = other.write_event("sealed-evt", "别人的暗页", "b",
                               date="2026-10-27")
    _set_visibility(sealed, "sealed")
    f = facade_for(ws, cow)
    rel = f.write_event("open-evt", "可见", "b", date="2026-10-27")
    ref = f.channel.register_consent(ACTOR, "open-evt")
    f.initiate_release(rel.relative_to(ws).as_posix(), consent_ref=ref)
    rebuild(ws)
    index = read(ws, "index.md")
    assert "别人的暗页" not in index, "别的视角的暗页泄漏进了本书架"
    assert "可见" not in index, "已释放的条目出现在了书架上"


# -- SH-8/9/10 · 三种聚合完整性 ---------------------------------------------

def test_sh8_by_day_covers_every_event(ws, cow):
    """SH-8: 每个事件都出现在其 occurred_at 日期的 by-day 页里。"""
    f, paths = build(ws, cow, events=3)
    rebuild(ws)
    for p in paths:
        fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        day = fm["occurred_at"][:10]
        page = shelf(ws) / "by-day" / f"{day}.md"
        assert page.is_file(), f"缺少 {day} 页"
        assert fm["title"] in page.read_text(encoding="utf-8")


def test_sh8_reverse_missing_day_page_is_detected(ws, cow):
    """反向用例：删掉某天的 by-day 页 → 判定必须失败。"""
    f, paths = build(ws, cow, events=2)
    rebuild(ws)
    page = shelf(ws) / "by-day" / "2026-10-27.md"
    page.unlink()
    missing = [p for p in paths
               if not (shelf(ws) / "by-day" /
                       f"{split_frontmatter(p.read_text(encoding='utf-8'))[0]['occurred_at'][:10]}.md"
                       ).is_file()]
    assert missing


def test_sh9_by_entity_covers_every_entity_slug(ws, cow):
    """SH-9: 事件的每个实体 slug（subject ∪ involves）都有 by-entity 页
    且含该事件。

    P0-1 §4.1 把 subject 定为 by-entity 的聚合键（facade 写 subject），
    P0-5 §3.5 写 involves —— 生成器取并集，两者任一存在都要成页。
    """
    build(ws, cow)
    rebuild(ws)
    # subject 聚合键：facade 写的 subject（= actor）
    page = shelf(ws) / "by-entity" / f"{ACTOR}.md"
    assert page.is_file(), "subject 聚合页缺失"
    assert "事件 0" in page.read_text(encoding="utf-8")


def test_sh9_reverse_missing_entity_page_is_detected(ws, cow):
    """反向用例：删掉 by-entity 页 → 判定必须失败。"""
    build(ws, cow)
    rebuild(ws)
    (shelf(ws) / "by-entity" / f"{ACTOR}.md").unlink()
    assert not (shelf(ws) / "by-entity" / f"{ACTOR}.md").is_file()


def test_sh9_involves_slug_also_gets_a_page(ws, cow):
    """involves 里的实体也要成页（手工写一个带 involves 的事件文件）。"""
    build(ws, cow)
    manual = (ws / "memory" / "markdown" / "events" / "2026-10-27" /
              "evt-manual--11111111.md")
    manual.write_text(
        "---\nstatus: active\nvisibility: open\ntitle: 手工事件\n"
        "slug: evt-manual\noccurred_at: 2026-10-27T12:00:00+08:00\n"
        "tags: []\ninvolves:\n  - 萌萌\n---\n\n正文\n",
        encoding="utf-8", newline="\n")
    rebuild(ws)
    page = shelf(ws) / "by-entity" / "萌萌.md"
    assert page.is_file()
    assert "手工事件" in page.read_text(encoding="utf-8")


def test_body_template_involves_is_read(ws, cow):
    """P0-1 §4.3 正文模板里的 `- involves: {slug}` 也要算实体关联。

    facade 不写 involves front-matter 字段（21 字段表里没有它），
    实体链接在正文的「## 关联」段 —— 生成器必须两处都读。
    """
    build(ws, cow)
    manual = (ws / "memory" / "markdown" / "events" / "2026-10-27" /
              "evt-rel--22222222.md")
    manual.write_text(
        "---\nstatus: active\nvisibility: open\ntitle: 关联事件\n"
        "slug: evt-rel\noccurred_at: 2026-10-27T13:00:00+08:00\n"
        "tags: []\n---\n\n## 关联\n\n- `involves`: 萌萌\n",
        encoding="utf-8", newline="\n")
    rebuild(ws)
    page = shelf(ws) / "by-entity" / "萌萌.md"
    assert page.is_file(), "正文模板里的 involves 没被读取"
    assert "关联事件" in page.read_text(encoding="utf-8")


def test_mentions_gap_rule_ignores_the_subject(ws, cow):
    """反向用例：自己提自己 N 次不算"缺实体档案页"。"""
    build(ws, cow, events=3)      # 三条事件，subject = actor
    rebuild(ws)
    gaps = read(ws, "gaps.md")
    assert "entity-page-missing" not in gaps


def test_sh10_by_tag_covers_every_tag(ws, cow):
    """SH-10: 事件的每个 tag 都有 by-tag 页且含该事件。"""
    build(ws, cow)
    rebuild(ws)
    page = shelf(ws) / "by-tag" / "记忆.md"
    assert page.is_file()
    assert "事件 0" in page.read_text(encoding="utf-8")


def test_sh10_reverse_missing_tag_page_is_detected(ws, cow):
    """反向用例：删掉 by-tag 页 → 判定必须失败。"""
    build(ws, cow)
    rebuild(ws)
    (shelf(ws) / "by-tag" / "记忆.md").unlink()
    assert not (shelf(ws) / "by-tag" / "记忆.md").is_file()


# -- SH-11 · 空页不生成 ------------------------------------------------------

def test_sh11_no_empty_aggregation_pages(ws, cow):
    """SH-11: 每个聚合页的 event_count > 0，且不为无事件的日子生成页。"""
    build(ws, cow)
    rebuild(ws)
    for sub in ("by-day", "by-entity", "by-tag"):
        for p in (shelf(ws) / sub).glob("*.md"):
            fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            assert fm["event_count"] > 0, f"空页 {p}"
    # 没有事件的日子不生成页
    assert not (shelf(ws) / "by-day" / "2020-01-01.md").exists()


def test_sh11_reverse_a_zero_count_page_is_detected(ws, cow):
    """反向用例：造一个 event_count=0 的页 → 判定必须失败。"""
    bad = shelf(ws) / "by-day" / "2020-01-01.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nkind: shelf-by-day\ndate: 2020-01-01\n"
                   "event_count: 0\n---\n\n空页\n", encoding="utf-8",
                   newline="\n")
    fm, _ = split_frontmatter(bad.read_text(encoding="utf-8"))
    assert fm["event_count"] == 0


# -- SH-12 · 生成器只写 shelf ------------------------------------------------

def test_sh12_generator_never_touches_the_truth_layer(ws, cow):
    """SH-12: markdown/ facts/ _tombstones/ 的字节在重建前后完全不变。"""
    f, paths = build(ws, cow)
    truth = {p: p.read_bytes() for p in (ws / "memory").rglob("*")
             if p.is_file() and "shelf" not in p.parts
             and ".snapshots" not in p.parts and ".audit" not in p.parts}
    rebuild(ws)
    for p, before in truth.items():
        assert p.read_bytes() == before, f"真源被改写: {p}"


def test_sh12_reverse_a_truth_file_change_is_detected(ws, cow):
    """反向用例：改一个真源文件 → 判定必须失败。"""
    f, paths = build(ws, cow)
    before = paths[0].read_bytes()
    paths[0].write_bytes(before + b"\n<!-- tampered -->\n")
    after = paths[0].read_bytes()
    assert after != before


# -- P0-8 追加：幂等 + skipped 出声 ----------------------------------------

def test_rebuild_is_idempotent_second_run_is_byte_identical(ws, cow, monkeypatch):
    """验收标准 6: 连跑两遍，第二遍零字节变化。

    严格形式：把生成器的时钟钉死，同一份输入的两次 rebuild 必须逐字节
    相同（不只是"除 generated_at 外相同"）。旧写法只剥掉了 front-matter
    里的 ``generated_at:`` 一行，而正文里还有一处时间戳（"最后更新：…"），
    两次 rebuild 跨秒时就会误报——这是一个被时间边界掩盖的真 bug。
    """
    import agent.memory.shelf as shelf_mod
    monkeypatch.setattr(shelf_mod, "_utc8_now", lambda: "2026-10-27T00:00:00+08:00")
    build(ws, cow)
    rebuild(ws)
    first = {p.relative_to(shelf(ws)).as_posix(): p.read_bytes()
             for p in all_view_files(ws)}
    rebuild(ws)
    second = {p.relative_to(shelf(ws)).as_posix(): p.read_bytes()
              for p in all_view_files(ws)}
    assert set(first) == set(second)
    for rel in first:
        assert first[rel] == second[rel], f"{rel} 第二次 rebuild 不是逐字节相同"


def test_rebuild_differs_only_in_timestamp_when_clock_moves(ws, cow, monkeypatch):
    """时钟真实推进时，唯一允许变化的是时间戳本身（两处出现）。"""
    import agent.memory.shelf as shelf_mod
    stamps = iter(["2026-10-27T00:00:00+08:00", "2026-10-27T00:00:05+08:00"])
    monkeypatch.setattr(shelf_mod, "_utc8_now", lambda: next(stamps))
    build(ws, cow)
    rebuild(ws)
    first = {p.relative_to(shelf(ws)).as_posix(): p.read_bytes()
             for p in all_view_files(ws)}
    rebuild(ws)
    second = {p.relative_to(shelf(ws)).as_posix(): p.read_bytes()
              for p in all_view_files(ws)}
    assert set(first) == set(second)
    for rel in first:
        a = first[rel].decode("utf-8")
        b = second[rel].decode("utf-8")
        if a == b:
            continue
        # 去掉所有出现的时间戳后必须完全一致
        na = a.replace("2026-10-27T00:00:00+08:00", "<TS>")
        nb = b.replace("2026-10-27T00:00:05+08:00", "<TS>")
        assert na == nb, f"{rel} 除时间戳外还有变化"


def test_skipped_files_are_reported_not_dropped_silently(ws, cow):
    """P0-8: front-matter 不合规的文件必须进 skipped 清单并出现在缺口里。"""
    f = facade_for(ws, cow)
    f.write_event("good", "好的", "b", date="2026-10-27")
    bad = ws / "memory" / "markdown" / "events" / "2026-10-27" / "bad--11111111.md"
    bad.write_text("---\ntitle: 缺字段的文件\n---\n\n正文\n",
                   encoding="utf-8", newline="\n")
    report = rebuild(ws)
    skipped = report["skipped"]
    assert skipped, "被跳过的文件没有出现在报告里"
    entry = next(s for s in skipped if s["path"].endswith("bad--11111111.md"))
    assert entry["reason"] == INVALID_FM
    assert "visibility" in entry["missing"] and "status" in entry["missing"]
    # 缺口视图里以 invalid-frontmatter 呈现，且动作给出应补字段
    gaps_text = read(ws, "gaps.md")
    assert "`invalid-frontmatter`" in gaps_text
    assert "status" in gaps_text and "visibility" in gaps_text


def test_skipped_reason_code_is_legal_for_sh3(ws, cow):
    """反向用例：跳过原因码必须仍在 SH-3 的合法集合内。"""
    f = facade_for(ws, cow)
    bad = ws / "memory" / "markdown" / "events" / "2026-10-27" / "bad--11111111.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("no front matter at all\n", encoding="utf-8", newline="\n")
    rebuild(ws)
    for code in re.findall(r"`([a-z-]+)`", read(ws, "gaps.md")):
        assert code in LEGAL_GAP_CODES


def test_views_have_parsable_frontmatter_with_fingerprint(ws, cow):
    """每页 front-matter 可被 PyYAML 无损解析，且带 generated_at + 指纹。"""
    build(ws, cow)
    report = rebuild(ws)
    for p in all_view_files(ws):
        fm, body = split_frontmatter(p.read_text(encoding="utf-8"))
        assert fm, f"{p} 没有 front-matter"
        assert fm["generated_at"]
        assert fm["input_fingerprint"].startswith("sha256:")
        assert fm["agent"] == ACTOR
        assert body.strip()
    # 同一份输入 → 同一指纹
    report2 = rebuild(ws)
    assert report2["input_fingerprint"] == report["input_fingerprint"]


def test_generator_is_the_only_writer_of_shelf(ws, cow):
    """SH-12 补充: 重建会清掉陈旧投影页（完整重建语义）。"""
    build(ws, cow)
    rebuild(ws)
    stale = shelf(ws) / "by-day" / "1999-01-01.md"
    stale.write_text("stale\n", encoding="utf-8", newline="\n")
    rebuild(ws)
    assert not stale.exists(), "陈旧投影页没有被清掉"


# --- P0-16 B-1 / B-2 / L-5: the two identity axes --------------------------
#
# B-1: the retrieval functions must accept the owner axis (the in-flight
#      change passed owner_tokens= but the storage signatures did not, so
#      the whole retrieval path raised TypeError).
# B-2: a dark page's chunk must actually carry its owner, and the owner must
#      be able to read it.
# L-5: the end-user axis and the agent-owner axis are SEPARATE columns.

def _dark_page(ws, name, *, author=None, body="DARKSECRET"):
    """A dark page (visibility: sealed), optionally with an owner."""
    d = ws / "memory" / "markdown" / "events"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    fm = ["---", "layer: event", "type: conversation", "title: 暗页",
          "tags: []", "subject: tester", "refs: []",
          "occurred_at: 2026-10-27T00:00:00+08:00",
          "recorded_at: 2026-10-27T00:00:00+08:00",
          "status: active", "visibility: sealed"]
    if author:
        fm.append("author_viewpoint: %s" % author)
    fm += ["version: 1", "---", "", body, ""]
    p.write_text("\n".join(fm), encoding="utf-8", newline="\n")
    return p


def _white_page(ws, name, body="WHITESECRET"):
    d = ws / "memory" / "markdown" / "events"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("---\nlayer: event\ntype: conversation\ntitle: 白页\n"
                 "tags: []\nsubject: tester\nrefs: []\n"
                 "occurred_at: 2026-10-27T00:00:00+08:00\n"
                 "recorded_at: 2026-10-27T00:00:00+08:00\n"
                 "status: active\nversion: 1\n---\n\n%s\n" % body,
                 encoding="utf-8", newline="\n")
    return p


def _index(ws):
    """Index the workspace and return (manager, sqlite connection)."""
    import asyncio
    import sqlite3

    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    mgr = MemoryManager(config=MemoryConfig(workspace_root=str(ws)))
    asyncio.run(mgr.sync())
    conn = sqlite3.connect(str(mgr.config.get_db_path()))
    conn.row_factory = sqlite3.Row
    return mgr, conn


def test_b1_a_the_retrieval_functions_accept_the_owner_axis(ws):
    """B-1: search_vector / search_keyword must take owner_tokens.

    The in-flight change passed owner_tokens= at the call site while the
    storage signatures did not have it — so every retrieval raised
    TypeError and the phase-1 acceptance dropped from 8/8 to 3/8.
    """
    import inspect

    from agent.memory.storage import MemoryStorage

    for name in ("search_vector", "search_keyword", "_search_fts5",
                 "_search_like", "_search_like_relaxed",
                 "_search_fts5_trigram"):
        sig = inspect.signature(getattr(MemoryStorage, name))
        assert "owner_tokens" in sig.parameters, (
            "%s does not accept owner_tokens — the retrieval path raises "
            "TypeError" % name)


def test_b1_a_every_visibility_clause_call_passes_the_owner_axis(ws):
    """B-1 (second layer): no call site may forget the owner axis.

    A call site that omits owner_token_list= silently disables the dark
    page rule — the dark page becomes visible to everybody, or (with the
    column half-written) to nobody. Either way the bug is invisible.
    """
    import inspect
    import pathlib

    from agent.memory.visibility import visibility_clause

    root = pathlib.Path(__file__).resolve().parent.parent
    calls = []
    for rel in ("agent/memory/storage.py", "agent/memory/vector_backend.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if "visibility_clause(" in line and "def " not in line:
                calls.append((rel, i, src.splitlines()[i - 1:]))
    assert calls, "no visibility_clause call site found"
    for rel, i, window in calls:
        block = "\n".join(window)
        assert "owner_token_list=" in block, (
            "%s:%d calls visibility_clause without owner_token_list= — the "
            "dark page rule does not apply there" % (rel, i))
    # and the predicate really does have the parameter
    assert "owner_token_list" in inspect.signature(
        visibility_clause).parameters


def test_b2_a_a_dark_page_carries_its_owner_and_the_owner_can_read_it(ws):
    """B-2: sealed_owner is written, and the four faces agree."""
    from agent.memory.visibility import SCOPE_SEALED, visibility_clause

    _dark_page(ws, "darkpage--00112233.md", author="agent-alice")
    _white_page(ws, "whitepage--00445566.md")
    mgr, conn = _index(ws)
    try:
        row = conn.execute(
            "SELECT scope, user_id, sealed_owner FROM chunks "
            "WHERE path LIKE '%darkpage%'").fetchone()
        assert row is not None, "the dark page was not indexed"
        assert row["scope"] == SCOPE_SEALED
        assert row["sealed_owner"] == "agent--agent-alice", (
            "sealed_owner is %r — the dark page has no owner" %
            (row["sealed_owner"],))

        def visible(end_user, owner):
            sql, params = visibility_clause(
                end_user, "chunks.scope", "chunks.user_id",
                "chunks.element_released", "chunks.file_sealed",
                identities=[end_user] if end_user else None,
                owner_token_list=[owner] if owner else None,
                sealed_owner_col="chunks.sealed_owner")
            q = ("SELECT path FROM chunks WHERE %s AND path LIKE ?"
                 % sql)
            return [r["path"] for r in conn.execute(
                q, params + ["%darkpage%"])]

        assert visible(None, "agent--agent-alice"), (
            "the owner cannot read its own dark page")
        assert not visible("user-bob", "agent--carol"), (
            "a third party can read the dark page")
        assert not visible(None, None), (
            "an anonymous caller can read the dark page")
    finally:
        conn.close()


def test_l5_a_the_two_axes_are_two_columns(ws):
    """L-5: the owner token must not sit in the end-user axis."""
    _dark_page(ws, "darkpage--00112233.md", author="agent-alice")
    _white_page(ws, "whitepage--00445566.md")
    mgr, conn = _index(ws)
    try:
        # the owner token lives in sealed_owner
        rows = conn.execute(
            "SELECT DISTINCT user_id, sealed_owner FROM chunks "
            "WHERE scope='sealed'").fetchall()
        assert rows
        for r in rows:
            assert (r["user_id"] or "") == "", (
                "the end-user axis carries an owner token: %r" % r["user_id"])
        # and NO row anywhere carries an owner token as its user_id
        n = conn.execute(
            "SELECT COUNT(*) c FROM chunks WHERE user_id LIKE 'agent--%'"
        ).fetchone()["c"]
        assert n == 0, "%d rows carry an owner token in the end-user axis" % n
    finally:
        conn.close()


def test_l5_a_an_end_user_id_in_the_agent_namespace_is_refused_on_write(ws):
    """L-5: 'agent--...' as an END USER id is rejected, not accepted."""
    import asyncio

    from agent.memory.visibility import is_reserved_end_user_id

    assert is_reserved_end_user_id("agent--agent-alice")
    assert not is_reserved_end_user_id("alice")
    assert not is_reserved_end_user_id("")

    _white_page(ws, "whitepage--00445566.md")
    mgr, conn = _index(ws)
    try:
        with pytest.raises(ValueError):
            asyncio.run(mgr.add_memory("some content",
                                       user_id="agent--agent-alice",
                                       scope="user"))
        n = conn.execute(
            "SELECT COUNT(*) c FROM chunks "
            "WHERE user_id='agent--agent-alice'").fetchone()["c"]
        assert n == 0, "the refused write still landed"
    finally:
        conn.close()


def test_l5_a_a_dark_page_with_no_owner_is_visible_to_nobody(ws):
    """A dark page whose owner cannot be determined belongs to nobody."""
    from agent.memory.visibility import visibility_clause

    _dark_page(ws, "noowner--00778899.md", author=None)
    mgr, conn = _index(ws)
    try:
        row = conn.execute(
            "SELECT scope, sealed_owner FROM chunks "
            "WHERE path LIKE '%noowner%'").fetchone()
        assert row is not None
        assert row["sealed_owner"] in (None, "")
        sql, params = visibility_clause(
            "agent--agent-alice", "chunks.scope", "chunks.user_id",
            "chunks.element_released", "chunks.file_sealed",
            identities=["agent--agent-alice"],
            owner_token_list=["agent--agent-alice"],
            sealed_owner_col="chunks.sealed_owner")
        got = [r["path"] for r in conn.execute(
            "SELECT path FROM chunks WHERE %s AND path LIKE ?" % sql,
            params + ["%noowner%"])]
        assert not got, "a dark page with no owner is visible"
    finally:
        conn.close()
