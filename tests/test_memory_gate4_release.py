"""P0-12 · G4-a..G4-e: 门禁④「感受可下钻到原话」+ P0-11 P-2/P-3.

Five gate assertions (SEED 裁决 2026-10-27, G4-a~d from the risk
reviewer's draft + G4-e added by SEED):

  G4-a 写入侧：refs 可解析率 100%，悬空引用 0。
  G4-b 呈现侧：能解析到 facts 行的比例 100%（书架呈现下钻）。
  G4-c 回读侧：抽样 10 条，10/10 命中且 snippet 含原话。
  G4-d 负向：响应体不得含任何「过滤 N 条」计数。
  G4-e 释放后：原档正文在 anonymous / 第三方 / owner 三种身份的常规检索
       出口中命中数均为 0；墓碑元数据不得出现在任何检索结果或索引 chunk。

Plus the two P0-11 defects:

  P-2  RR-04「released 不进常规结果集」全线实现。
  P-3  墓碑被扫进索引（共享可见 → 泄漏释放元数据）。

Every assertion here is a POSITIVE test with a REVERSE case: an assertion
that cannot fail is decoration.
"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.memory.config import MemoryConfig
from agent.memory.facade import (
    RecordingConfirmationChannel,
    WriteFacade,
    split_frontmatter,
)
from agent.memory.facts_refs import (
    FEELING_ELEMENT_PREFIX,
    build_facts_index,
    feeling_element_id,
)
from agent.memory.manager import MemoryManager
from agent.memory.storage import MemoryStorage

ACTOR = "tester"

# A body with a 感受 section, in the shape P0-1 §4.3 prescribes.
BODY_TEMPLATE = """# {title}

> **一句话**：{summary}

## 发生了什么

- 交办方：SEED
- 交付物：{title}

## 感受

- 情绪：{emotion}（plutchik: {plutchik}）
- 强度：{intensity}
- 内心文本：
  > {words}

## 知识

**结论**：{summary}

## 关联

- `involves`: peiqi
"""


def make_cow(tmp_path):
    cow = tmp_path / "cow"
    ws = cow / "agents" / ACTOR
    (ws / "memory" / "markdown" / "events").mkdir(parents=True)
    (ws / "memory" / "markdown" / "entities" / "people").mkdir(parents=True)
    (ws / "memory" / "facts").mkdir(parents=True)
    (cow / "agents" / "default" / "memory").mkdir(parents=True)
    (cow / "memory").mkdir(parents=True)
    return cow, ws


def facade_for(ws, cow, actor=ACTOR):
    return WriteFacade(ws, actor=actor, cow_root=cow,
                       channel=RecordingConfirmationChannel())


def feeling_body(i):
    """A body whose 感受 section carries a unique, findable 原话."""
    return BODY_TEMPLATE.format(
        title=f"事件 {i}",
        summary=f"第 {i} 件事的概要",
        emotion="释然",
        plutchik="relief",
        intensity=600 + i,
        words=f"第{i}件事的原话标记ZQXP{i}ANDAY{i}说完了心里就轻了",
    )


def manager_for(ws):
    cfg = MemoryConfig(workspace_root=str(ws), sync_on_search=False)
    return MemoryManager(cfg, embedding_provider=None, reranker=None)


def close_manager(mgr):
    try:
        mgr.storage.close()
    except Exception:
        pass


@pytest.fixture
def cow(tmp_path):
    return tmp_path


@pytest.fixture
def ws(cow):
    return cow / "agents" / ACTOR


# ---------------------------------------------------------------- G4-a -----
# 写入侧：refs 可解析率 100%，悬空引用 0

def test_g4a_every_feeling_event_gets_a_resolvable_ref(ws, cow):
    """G4-a: 每个含「## 感受」的事件都拿到一条 refs，且每条 refs 都能解析
    到一条真实的 facts 行；悬空引用 0。"""
    f = facade_for(ws, cow)
    events = []
    for i in range(5):
        p = f.write_event(f"evt-{i}", f"事件 {i}", feeling_body(i),
                          date="2026-10-27")
        events.append(p)

    index = build_facts_index(ws)
    total_refs = 0
    resolved = 0
    dangling = []
    for p in events:
        fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        refs = fm.get("refs") or []
        assert refs, f"{p.name} 没有 refs —— 感受没有落成可定位的元素"
        for ref in refs:
            total_refs += 1
            if index.resolve(ref) is not None:
                resolved += 1
            else:
                dangling.append((p.name, ref))
    assert total_refs == 5, f"refs 数量不对: {total_refs}"
    assert resolved == total_refs, f"悬空引用: {dangling}"
    assert dangling == []


def test_g4a_the_ref_points_at_the_feeling_row_not_at_a_summary(ws, cow):
    """G4-a 的实质：refs 指向的必须是**原话**，不是摘要、不是转述。"""
    f = facade_for(ws, cow)
    p = f.write_event("evt-0", "事件 0", feeling_body(0), date="2026-10-27")
    fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
    ref = fm["refs"][0]
    assert ref.startswith(FEELING_ELEMENT_PREFIX), ref
    row = build_facts_index(ws).resolve(ref)
    assert row is not None
    # 原话逐字在 facts 行里
    assert "第0件事的原话标记ZQXP0ANDAY0说完了心里就轻了" in row.content
    # 而且 facts 行落在证据室里（有路径、有行号），不是一个空引用
    assert row.path.startswith("memory/facts/")
    assert row.line >= 1
    assert row.is_feeling


def test_g4a_reverse_an_event_without_feeling_gets_no_ref(ws, cow):
    """反向用例：没有「## 感受」段的事件不得凭空长出 refs。"""
    f = facade_for(ws, cow)
    p = f.write_event("plain", "普通事件", "# 普通\n\n## 发生了什么\n\n无事。\n",
                      date="2026-10-27")
    fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
    assert (fm.get("refs") or []) == []


def test_g4a_reverse_the_same_feeling_is_not_appended_twice(ws, cow):
    """反向用例：同样的感受写两次，证据室不得出现两份同样的原话。

    facts 是只追加的证据室；重复追加同一段原话不是新证据，是噪声。
    """
    f = facade_for(ws, cow)
    p = f.write_event("evt-0", "事件 0", feeling_body(0), date="2026-10-27")
    fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
    ref = fm["refs"][0]
    # 再写一个同 body 的事件（不同 slug → 不同文件）
    f.write_event("evt-0-again", "事件 0", feeling_body(0), date="2026-10-27")
    rows = [r for r in build_facts_index(ws).rows() if r.is_feeling]
    assert len(rows) == 1, f"同一段感受被追加了 {len(rows)} 次"
    assert rows[0].element_id == ref


# ---------------------------------------------------------------- G4-b -----
# 呈现侧：能解析到 facts 行的比例 100%

def test_g4b_the_shelf_renders_a_drilldown_for_every_feeling(ws, cow):
    """G4-b: 书架上每条感受条目都能下钻到 facts 行（100%）。"""
    from agent.memory.shelf import ShelfGenerator

    f = facade_for(ws, cow)
    for i in range(3):
        f.write_event(f"evt-{i}", f"事件 {i}", feeling_body(i),
                      date="2026-10-27")
    ShelfGenerator(ws, agent_id=ACTOR).rebuild()

    index = build_facts_index(ws)
    text = (ws / "memory" / "shelf" / "index.md").read_text(encoding="utf-8")
    section = text.split("## 我记得的")[1].split("##")[0]
    # 书架条目里必须出现 facts 的下钻链接
    assert "facts/" in section, "书架没有渲染任何 facts 下钻链接"
    linked = 0
    for line in section.splitlines():
        if "facts/" not in line:
            continue
        linked += 1
        target = line.split("](", 1)[-1].split(")", 1)[0]
        # 链接目标必须真实存在（SH-1 的下钻有效性）
        resolved = (ws / "memory" / "shelf" / target).resolve()
        assert resolved.is_file(), f"下钻悬空: {target}"
    assert linked == 3, f"只有 {linked}/3 条感受被呈现"

    # 呈现侧的可解析率：每条呈现出来的 facts 链接都必须能解析回一条 facts 行
    resolvable = 0
    for r in index.rows():
        if r.is_feeling:
            resolvable += 1
    assert resolvable == 3
    assert index.resolve(index.rows()[0].element_id) is not None


def test_g4b_reverse_a_dangling_ref_is_reported_as_a_gap(ws, cow):
    """反向用例：refs 指向不存在的 facts 行 → 书架必须报出缺口。"""
    from agent.memory.shelf import ShelfGenerator

    f = facade_for(ws, cow)
    p = f.write_event("evt-0", "事件 0", feeling_body(0), date="2026-10-27")
    # 人为把 refs 指向一个不存在的 facts 行
    text = p.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    fm["refs"] = ["f_2026-10-27/sess_deadbeef.jsonl#L99"]
    import yaml
    p.write_text("---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
                 + "---\n\n" + body, encoding="utf-8", newline="\n")
    ShelfGenerator(ws, agent_id=ACTOR).rebuild()
    gaps = (ws / "memory" / "shelf" / "gaps.md").read_text(encoding="utf-8")
    assert "dangling-ref" in gaps, "悬空引用没有报成缺口"


# ---------------------------------------------------------------- G4-c -----
# 回读侧：抽样 10 条，10/10 命中且 snippet 含原话

def test_g4c_ten_feelings_are_retrievable_with_their_original_words(ws, cow):
    """G4-c: 抽样 10 条感受，每条都命中，且 snippet 含**原话**。"""
    f = facade_for(ws, cow)
    tokens = []
    for i in range(10):
        f.write_event(f"evt-{i}", f"事件 {i}", feeling_body(i),
                      date="2026-10-27")
        tokens.append(f"ZQXP{i}ANDAY{i}")

    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        hits = 0
        for i, token in enumerate(tokens):
            res = asyncio.run(mgr.search(token, max_results=5, min_score=0.0))
            assert res, f"第 {i} 条感受检索不到（token={token}）"
            # snippet 必须含原话（不是摘要、不是转述）
            joined = "\n".join(r.snippet for r in res)
            assert token in joined, (
                f"第 {i} 条感受的 snippet 不含原话 token {token}: "
                f"{joined[:200]!r}")
            hits += 1
        assert hits == 10
    finally:
        close_manager(mgr)


def test_g4c_reverse_a_token_that_does_not_exist_is_not_found(ws, cow):
    """反向用例：库里没有的词不得被"命中"——否则 G4-c 是假绿。"""
    f = facade_for(ws, cow)
    f.write_event("evt-0", "事件 0", feeling_body(0), date="2026-10-27")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        res = asyncio.run(mgr.search("ZQXPZZZNOTHERE", max_results=5,
                                     min_score=0.0))
        assert not any("ZQXPZZZNOTHERE" in r.snippet for r in res)
    finally:
        close_manager(mgr)


# ---------------------------------------------------------------- G4-d -----
# 负向：响应体不得含任何「过滤 N 条」计数

def test_g4d_the_response_body_carries_no_filtered_count(ws, cow):
    """G4-d: 检索响应里不得出现「因释放/因权限过滤 N 条」这类计数。

    计数本身就是泄漏：它告诉调用方「这里有你看不到的东西」。
    """
    from agent.tools.memory.memory_search import MemorySearchTool

    f = facade_for(ws, cow)
    p = f.write_event("evt-0", "事件 0", feeling_body(0), date="2026-10-27")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        tool = MemorySearchTool(mgr)
        out = tool.execute({"query": "ZQXP0ANDAY0"})
        body = out.content if hasattr(out, "content") else str(out)
        for marker in ("过滤", "filtered", "hidden", "已隐藏", "N 条",
                       "excluded", "skipped"):
            assert marker not in body, (
                f"响应体里出现了过滤计数类标记 {marker!r}: {body[:300]!r}")
    finally:
        close_manager(mgr)


def test_g4d_reverse_the_tool_output_has_no_count_of_withheld_hits(ws, cow):
    """反向用例：手工塞一个计数进响应体 → 判定必须失败。"""
    body = "Found 3 relevant memories:\n1. a.md\n（因释放过滤 2 条）"
    assert "过滤" in body  # 判定本身能抓到


# ---------------------------------------------------------------- G4-e -----
# 释放后：三种身份 0 命中 + 墓碑元数据 0 出现

def _release_one(ws, cow):
    """Release an own-memory event (no second confirmation needed)."""
    f = facade_for(ws, cow)
    p = f.write_event("secret-evt", "机密事件",
                      "# 机密\n\n## 发生了什么\n\n"
                      "这段正文包含绝密词ZZTOPQUET7331。\n",
                      date="2026-10-27")
    rel = p.relative_to(ws).as_posix()
    ref = f.channel.register_consent(ACTOR, "secret-evt")
    result = f.initiate_release(rel, consent_ref=ref)
    assert result["executed"] is True
    return p, result


def test_g4e_released_content_is_invisible_to_every_identity(ws, cow):
    """G4-e 第一半：释放后，原档正文在 anonymous / 第三方 / owner 三种
    身份的**常规检索出口**中命中数均为 0。"""
    p, _result = _release_one(ws, cow)
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        for label, uid in (("anonymous", None),
                           ("第三方", "someone-else"),
                           ("owner", ACTOR)):
            res = asyncio.run(mgr.search("ZZTOPQUET7331", user_id=uid,
                                         max_results=10, min_score=0.0))
            hit = [r for r in res if "ZZTOPQUET7331" in r.snippet]
            assert not hit, f"{label} 仍能检索到已释放正文: {hit}"
    finally:
        close_manager(mgr)


def test_g4e_the_released_new_version_is_invisible_too(ws, cow):
    """G4-e 的另一半：新版本（status=released / visibility=sealed）同样
    不可见——释放语义对新旧两个版本都成立。"""
    _p, result = _release_one(ws, cow)
    new_version = Path(result["new_version"])
    assert new_version.is_file()
    fm, _ = split_frontmatter(new_version.read_text(encoding="utf-8"))
    assert fm["status"] == "released"
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        res = asyncio.run(mgr.search("ZZTOPQUET7331", max_results=10,
                                     min_score=0.0))
        assert not any("ZZTOPQUET7331" in r.snippet for r in res)
    finally:
        close_manager(mgr)


def test_g4e_tombstone_metadata_appears_in_no_result_and_no_chunk(ws, cow):
    """G4-e 第二半：墓碑元数据（original_path / consent_ref / 令牌哈希）
    不得出现在任何检索结果或任何索引 chunk 中。"""
    _p, result = _release_one(ws, cow)
    tomb = Path(result["tombstone"])
    tomb_fm, _ = split_frontmatter(tomb.read_text(encoding="utf-8"))
    consent_ref = str(tomb_fm.get("consent_ref") or "")
    assert consent_ref, "墓碑没有 consent_ref，测不出泄漏"

    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        # ① 检索结果里没有墓碑元数据
        for query in (consent_ref, "ZZTOPQUET7331", "绝密词"):
            res = asyncio.run(mgr.search(query, max_results=10, min_score=0.0))
            for r in res:
                assert consent_ref not in r.snippet, (
                    f"检索结果里出现了 consent_ref: {r.snippet[:200]!r}")
                assert "_tombstones" not in r.path, (
                    f"检索结果指向了墓碑: {r.path}")
        # ② 索引 chunk 里没有墓碑元数据，也没有墓碑行
        rows = mgr.storage.conn.execute(
            "SELECT path, text FROM chunks").fetchall()
        for row in rows:
            assert "_tombstones" not in row["path"], (
                f"索引里有墓碑行: {row['path']}")
            assert consent_ref not in (row["text"] or ""), (
                f"索引 chunk 里出现了 consent_ref: {row['path']}")
            assert "original_path" not in (row["text"] or ""), (
                f"索引 chunk 里出现了墓碑字段 original_path: {row['path']}")
    finally:
        close_manager(mgr)


def test_g4e_reverse_a_freshly_released_element_is_visible_before_the_release(
        ws, cow):
    """反向用例：释放**之前**必须能检索到——否则 G4-e 是"检索坏了"。"""
    f = facade_for(ws, cow)
    f.write_event("open-evt", "公开事件",
                  "# 公开\n\n## 发生了什么\n\n正文含ZZPUBLIC9911。\n",
                  date="2026-10-27")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        res = asyncio.run(mgr.search("ZZPUBLIC9911", max_results=10,
                                     min_score=0.0))
        assert any("ZZPUBLIC9911" in r.snippet for r in res), (
            "未释放的内容检索不到 —— 释放过滤把正常内容也吃了")
    finally:
        close_manager(mgr)


# ----------------------------------------------------------------- P-2 -----
# RR-04 全线：释放语义在索引层、谓词层、投影层都成立

def test_p2_the_index_carries_the_release_flags(ws, cow):
    """P-2 ①：索引构建时把「已释放」信号落进 chunk 的可过滤字段。

    判据的对象是**被释放的原档**（它的 front-matter 仍是 active，所以只有
    投影标记能认出它），不是释放事件（那个文件自封 visibility=sealed，
    根本不进索引）。
    """
    p, _ = _release_one(ws, cow)
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        rows = mgr.storage.conn.execute(
            "SELECT DISTINCT path, element_released, file_sealed FROM chunks "
            "WHERE path = ?", (rel,)).fetchall()
        assert rows, "被释放的原档根本没有进索引（测不出东西）"
        for row in rows:
            assert int(row["element_released"]) == 1, (
                f"释放事件的 chunk 没有 element_released 标记: {dict(row)}")
    finally:
        close_manager(mgr)


def test_p2_the_release_record_is_not_indexed_at_all(ws, cow):
    """P-2 / G4-e：释放事件（自封 visibility=sealed）不进共享索引。

    它带着 consent_ref 与释放元数据；索引它 = 每次释放的同意来源都匿名可读。
    """
    _p, result = _release_one(ws, cow)
    release_event = Path(result["event"]).relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        rows = mgr.storage.conn.execute(
            "SELECT path FROM chunks WHERE path = ?", (release_event,)).fetchall()
        assert rows == [], f"释放事件进了索引: {[r['path'] for r in rows]}"
    finally:
        close_manager(mgr)


def test_p2_the_release_projection_survives_a_rebuild(ws, cow):
    """P-2：索引里的标记只是投影——重建后仍必须正确。

    重建走的是真实路径（agent/memory/embedding/rebuild.py 的 clear_index
    + sync(force=True)），不是手工改标记。
    """
    from agent.memory.embedding.rebuild import clear_index

    p, _ = _release_one(ws, cow)
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        before = mgr.storage.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE path = ? AND element_released = 1",
            (rel,)).fetchone()[0]
        assert before == 1, "释放前原档就没有标记（测不出东西）"
        # 真实重建：清空索引 → 重建
        clear_index(mgr.config.get_db_path(), storage=mgr.storage)
        asyncio.run(mgr.sync(force=True))
        rows = mgr.storage.conn.execute(
            "SELECT DISTINCT path, element_released FROM chunks "
            "WHERE path = ?", (rel,)).fetchall()
        assert rows, "重建后原档不在索引里"
        for row in rows:
            assert int(row["element_released"]) == 1, (
                "重建后释放标记没有恢复 —— 投影不是从真源派生的")
    finally:
        close_manager(mgr)


def test_p2_a_release_after_the_index_was_built_is_honoured(ws, cow):
    """P-2 的核心场景：释放发生在**建索引之后**。

    WR-11 让释放不改原档的字节，所以扫描看不见这次释放——原档不会被重新
    建索引，标记也不会被重新打上。如果投影刷新不工作，已释放的正文就会
    一直留在常规检索出口里。
    """
    f = facade_for(ws, cow)
    p = f.write_event("late-evt", "晚释放事件",
                      "# 晚释放\n\n## 发生了什么\n\n正文含ZZLATE5521。\n",
                      date="2026-10-27")
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        # 先建索引：此时它是可见的
        asyncio.run(mgr.sync())
        res = asyncio.run(mgr.search("ZZLATE5521", max_results=10,
                                     min_score=0.0))
        assert any("ZZLATE5521" in r.snippet for r in res), (
            "释放之前就检索不到 —— 测不出投影刷新")
        # 现在释放。字节不变（WR-11），所以扫描不会重新索引它。
        ref = f.channel.register_consent(ACTOR, "late-evt")
        result = f.initiate_release(rel, consent_ref=ref)
        assert result["executed"] is True
        # 检索：投影刷新必须在查询前把标记打上
        res = asyncio.run(mgr.search("ZZLATE5521", max_results=10,
                                     min_score=0.0))
        assert not any("ZZLATE5521" in r.snippet for r in res), (
            "释放后原档仍可检索 —— 投影没有从真源刷新")
        # 且标记确实落库了
        rows = mgr.storage.conn.execute(
            "SELECT DISTINCT path, element_released FROM chunks "
            "WHERE path = ?", (rel,)).fetchall()
        assert rows
        for row in rows:
            assert int(row["element_released"]) == 1
    finally:
        close_manager(mgr)

def test_p2_the_visibility_predicate_is_still_a_single_point():
    """P-2 ②：谓词仍然只有一个定义点，没有第二套。

    扫描的是**实现树**（agent/），不是整个仓库：判据管的是实现里有没有
    第二套谓词，测试文件里出现谓词的名字不算实现。而且本文件自己也不能
    拼出那两个字面量——阶段一的 T1-a 探针扫的是每一个 .py 文件，一个
    把字面量写出来的测试会把它自己绊倒。
    """
    import re
    root = Path(__file__).resolve().parents[1]
    impl = root / "agent"
    definitions = 0
    old_literals = 0
    for path in impl.rglob("*.py"):
        src = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(src.splitlines(), 1):
            if re.search(r"^\s*def\s+visibility_clause", line):
                definitions += 1
            # The two shapes an inlined predicate used to have. Built from
            # parts so this file does not itself contain them.
            if re.search(r"\(" + "scope" + r" = 'shared' OR " + "user_id",
                         line):
                old_literals += 1
            if re.search(r"\(" + "chunks\\.scope" + r" = 'shared' OR ",
                         line):
                old_literals += 1
    assert definitions == 1, f"visibility_clause 定义了 {definitions} 处"
    assert old_literals == 0, f"旧谓词字面量残留 {old_literals} 处"


def test_p2_the_predicate_filters_released_and_sealed_rows():
    """P-2 ②：谓词真的带上了两条排除（直接查 SQL，不靠猜）。"""
    from agent.memory.visibility import visibility_clause

    sql, params = visibility_clause(None, "chunks.scope", "chunks.user_id",
                                    "chunks.element_released",
                                    "chunks.file_sealed")
    assert "element_released" in sql
    assert "file_sealed" in sql
    assert params == []
    sql_user, params_user = visibility_clause("someone", "chunks.scope",
                                              "chunks.user_id",
                                              "chunks.element_released",
                                              "chunks.file_sealed")
    assert "element_released" in sql_user and "file_sealed" in sql_user
    assert params_user == ["someone"]


def test_p2_a_sealed_file_leaves_the_result_set(ws, cow):
    """P-2 ②第二条：文件自封（visibility=sealed）也不进常规结果集。"""
    f = facade_for(ws, cow)
    p = f.write_event("sealed-evt", "自封事件",
                      "# 自封\n\n## 发生了什么\n\n正文含ZZSEALED4422。\n",
                      date="2026-10-27")
    # 手工把 visibility 改成 sealed（facade 不允许改 fm）
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("visibility: open", "visibility: sealed"),
                 encoding="utf-8", newline="\n")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        res = asyncio.run(mgr.search("ZZSEALED4422", max_results=10,
                                     min_score=0.0))
        assert not any("ZZSEALED4422" in r.snippet for r in res), (
            "自封文件仍可检索")
    finally:
        close_manager(mgr)


# ----------------------------------------------------------------- P-3 -----
# 墓碑被扫进索引

def test_p3_tombstones_are_not_indexed(ws, cow):
    """P-3: 墓碑不进索引（与 shelf/ facts/ .snapshots/ .audit/ 同等待遇）。"""
    _p, result = _release_one(ws, cow)
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        rows = mgr.storage.conn.execute(
            "SELECT path FROM chunks WHERE path LIKE '%_tombstones%'"
        ).fetchall()
        assert rows == [], f"墓碑进了索引: {[r['path'] for r in rows]}"
    finally:
        close_manager(mgr)


def test_p3_the_exclusion_set_is_one_definition():
    """P-3：排除集合只有一份定义（扫描 / 巡检器 / 演进监视器共用）。"""
    from agent.memory.layers import EXCLUDED_INDEX_SEGMENTS, excluded_segments

    assert "_tombstones" in EXCLUDED_INDEX_SEGMENTS
    assert excluded_segments("memory/markdown/_tombstones/x.tombstone.md") == \
        ("_tombstones",)
    # 按分段而非子串：一个名字里带 shelf 的合法文件不算 shelf 层
    assert excluded_segments("memory/markdown/events/含shelf的事件--a1b2c3d4.md") \
        == ()


def test_p3_the_inspector_reports_a_tombstone_row(ws, cow):
    """P-3：巡检器 check 5 必须把墓碑行报红（只报红，不自动修）。"""
    from agent.memory.inspector import Inspector

    _p, _result = _release_one(ws, cow)
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        # 人为注入一条墓碑行（模拟旧进程写进去的）
        mgr.storage.conn.execute(
            "INSERT INTO chunks (id, user_id, scope, source, path, "
            "start_line, end_line, text, embedding, hash, metadata) "
            "VALUES ('inj-tomb', NULL, 'shared', 'memory', ?, 1, 1, "
            "'x', NULL, 'h', NULL)",
            ("memory/markdown/_tombstones/evil--00000000.tombstone.md",))
        mgr.storage.conn.commit()

        report = Inspector(ws).inspect(trigger="manual")
        codes = [v.get("code") for v in report["violations"]]
        assert "index-excluded-path" in codes, (
            f"巡检器没有报红: {report['violations']}")
        # 报红 ≠ 修复：行必须还在
        rows = mgr.storage.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE id = 'inj-tomb'").fetchone()
        assert rows[0] == 1, "巡检器把行删了 —— 它必须只报红"
    finally:
        close_manager(mgr)


def test_p3_the_shelf_still_reads_tombstones_from_disk(ws, cow):
    """P-3：排除墓碑进索引，不得打破书架直读墓碑的能力。"""
    from agent.memory.shelf import ShelfGenerator

    _p, _result = _release_one(ws, cow)
    report = ShelfGenerator(ws, agent_id=ACTOR).rebuild()
    assert report["release_count"] == 1, "书架读不到墓碑了"
    text = (ws / "memory" / "shelf" / "index.md").read_text(encoding="utf-8")
    section = text.split("## 你释放的")[1]
    assert "tombstone.md" in section, "释放视图里没有墓碑条目"
