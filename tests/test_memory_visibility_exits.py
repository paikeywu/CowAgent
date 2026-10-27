"""P0-14 · L-1 / L-2 / L-3: 读取出口统一、暗页语义、fail-closed。

P0-13 的复审留下三条遗留，SEED 裁决全部属实：

  L-1（高）``memory_get`` 是第二条读取出口，没过谓词 —— 释放后原档正文
        逐字返回。裁决：**所有读取出口必须共用同一个可见性判据**。
  L-2（中高）``visibility: sealed`` 被当成释放 —— 暗页对主人格也不可见，
        与 06 号 742 行冲突；三个面口径不一致。裁决：**sealed ≠ released**。
  L-3（中）投影刷新失败 → 谓词 fail-open（隐私控件故障时恰好放出私密内容，
        且静默降级）。裁决：**fail-closed + 出声**。

本文件的每条判据都配一条**反向用例**：一条抓不到坏事的断言是装饰。

身份模型（本文件用得到）：

  * ``anonymous``   —— 无任何身份（``user_id=None``，环境里也没有 agent id）
  * ``third-party`` —— 端用户 id 与作者不同
  * ``owner``       —— 环境身份 = 作者 agent（``identity_scope(agent_id=…)``）

暗页的归属用 ``author_viewpoint``（facade 写入的 actor）表达，归属令牌是
``agent--<actor>``（``agent.memory.visibility.agent_owner_token``）。
"""

import asyncio
import re
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
from agent.memory.manager import MemoryManager
from agent.memory.shelf import ShelfGenerator
from agent.memory.storage import MemoryStorage
from agent.memory.visibility import (
    SCOPE_SEALED,
    agent_owner_token,
    classify_frontmatter,
    owner_token,
)
from agent.tools.memory.memory_get import MemoryGetTool
from common.runtime_identity import identity_scope

ACTOR = "tester"
OTHER = "other-view"

RELEASED_TOKEN = "ZZRELEASED7788"
DARK_TOKEN = "ZZDARKPAGE5566"
OPEN_TOKEN = "ZZOPENPAGE3344"


# ---------------------------------------------------------------- helpers ---

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


def manager_for(ws):
    cfg = MemoryConfig(workspace_root=str(ws), sync_on_search=False)
    return MemoryManager(cfg, embedding_provider=None, reranker=None)


def close_manager(mgr):
    try:
        mgr.storage.close()
    except Exception:
        pass


def body_with(token, title="事件"):
    return (f"# {title}\n\n## 发生了什么\n\n"
            f"这段正文包含标记词{token}。\n")


def write_event(f, slug, token, title="事件", **editable):
    return f.write_event(slug, title, body_with(token, title),
                          date="2026-10-27", **editable)


def release(f, ws, path):
    """Release an element of this workspace (own memory → no 2nd confirm)."""
    rel = path.relative_to(ws).as_posix()
    ref = f.channel.register_consent(ACTOR, path.stem)
    result = f.initiate_release(rel, consent_ref=ref)
    assert result["executed"] is True
    return result


def hits(mgr, token, user_id=None):
    res = asyncio.run(mgr.search(token, user_id=user_id, max_results=10,
                                 min_score=0.0))
    return [r for r in res if token in r.snippet]


@pytest.fixture
def cow(tmp_path):
    return tmp_path


@pytest.fixture
def ws(cow):
    return cow / "agents" / ACTOR


# ------------------------------------------------------------------ L-1 -----
# 读取出口统一：释放后原档正文在**任何**读取路径、**任何**身份下都取不到。

def test_l1_memory_get_refuses_a_released_body(ws, cow):
    """L-1：释放后 ``memory_get`` 不得返回原档正文。"""
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        tool = MemoryGetTool(mgr)
        before = tool.execute({"path": rel})
        assert RELEASED_TOKEN in str(before.result), (
            "释放前就读不到 —— 测不出 memory_get 的漏洞")
        release(f, ws, p)
        after = tool.execute({"path": rel})
        assert RELEASED_TOKEN not in str(after.result), (
            "释放后 memory_get 仍返回原档正文（L-1）")
        assert after.status == "error"
    finally:
        close_manager(mgr)


def test_l1_memory_get_refuses_a_released_body_without_any_sync(ws, cow):
    """L-1 的核心场景：释放后**一次 sync 都不跑**，memory_get 仍必须 0 命中。

    释放不改原档字节（WR-11），所以扫描看不见这次释放；读出口必须自己
    问真源，而不是问索引。
    """
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        release(f, ws, p)
        # 故意不 sync
        out = MemoryGetTool(mgr).execute({"path": rel})
        assert RELEASED_TOKEN not in str(out.result), (
            "释放后（未 sync）memory_get 仍返回原档正文（L-1）")
    finally:
        close_manager(mgr)


def test_l1_the_facade_refuses_the_released_original(ws, cow):
    """L-1：facade.read 也必须过判据 —— 包括「原档」（其 fm 仍写 active）。

    原档的字节没被改（WR-11），所以只看它自己的 front-matter 会放行；
    只有墓碑知道真相。
    """
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    rel = p.relative_to(ws).as_posix()
    release(f, ws, p)
    fm, body = f.read(rel)
    assert fm == {}, "facade.read 仍把已释放的原档当作可信记忆返回"
    assert RELEASED_TOKEN not in body


def test_l1_the_console_reader_refuses_a_released_file(ws, cow):
    """L-1：Web 控制台的读出口（MemoryService）同样必须过判据。"""
    from agent.memory.service import MemoryService

    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    release(f, ws, p)
    svc = MemoryService(str(ws))
    with pytest.raises(PermissionError):
        svc.get_content(p.relative_to(ws / "memory").as_posix())


def test_l1_the_read_exits_share_one_decision(ws, cow):
    """L-1 的结构判据：读取出口不再各自解释「释放」。

    判据扫的是**实现树**：每个读出口都必须走
    ``agent.memory.visibility`` 的那个判据，不许在出口里自己写一份
    「status == released」的判断（旧实现就是这么漏的）。
    """
    root = Path(__file__).resolve().parents[1]
    impl = root / "agent"
    offenders = []
    for path in impl.rglob("*.py"):
        if path.name == "visibility.py":
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        # The old inline shape: a read exit deciding "released" by itself.
        if re.search(r"""get\(\s*["']status["']\s*\)\s*==\s*["']released["']""",
                     src):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        f"这些地方自己判断了 released，必须改用统一判据: {offenders}")


def test_l1_reverse_the_body_is_readable_before_the_release(ws, cow):
    """反向用例：释放**之前**必须读得到 —— 否则上面的断言是「检索坏了」。"""
    f = facade_for(ws, cow)
    p = write_event(f, "open-evt", OPEN_TOKEN, "公开事件")
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        out = MemoryGetTool(mgr).execute({"path": rel})
        assert OPEN_TOKEN in str(out.result)
        fm, body = f.read(rel)
        assert OPEN_TOKEN in body
    finally:
        close_manager(mgr)


def test_l1_the_read_response_carries_no_count_of_withheld_content(ws, cow):
    """L-1 + G4-d：读取出口的响应体不得出现「过滤 N 条」这类计数。"""
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    rel = p.relative_to(ws).as_posix()
    release(f, ws, p)
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        out = MemoryGetTool(mgr).execute({"path": rel})
        body = str(out.result)
        for marker in ("过滤", "filtered", "hidden", "N 条", "excluded"):
            assert marker not in body, (
                f"响应体里出现了过滤计数类标记 {marker!r}: {body[:200]!r}")
    finally:
        close_manager(mgr)


# ------------------------------------------------------------------ L-2 -----
# sealed（暗页）≠ released（已释放）：三个面必须统一。

def test_l2_a_dark_page_is_visible_to_its_owner(ws, cow):
    """L-2：暗页对**本视角与主人格**可见（06 号 742 行）。"""
    f = facade_for(ws, cow)
    write_event(f, "dark-evt", DARK_TOKEN, "暗页", visibility="sealed")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        with identity_scope(agent_id=ACTOR):
            found = hits(mgr, DARK_TOKEN)
        assert found, "主人格检索不到自己的暗页（L-2：sealed 被当成释放了）"
    finally:
        close_manager(mgr)


def test_l2_a_dark_page_is_invisible_to_a_third_party(ws, cow):
    """L-2：暗页对第三方不可见。"""
    f = facade_for(ws, cow)
    write_event(f, "dark-evt", DARK_TOKEN, "暗页", visibility="sealed")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        assert not hits(mgr, DARK_TOKEN, user_id="someone-else"), (
            "第三方检索到了别人的暗页（L-2）")
    finally:
        close_manager(mgr)


def test_l2_a_dark_page_is_invisible_anonymously(ws, cow):
    """L-2：暗页对匿名不可见。"""
    f = facade_for(ws, cow)
    write_event(f, "dark-evt", DARK_TOKEN, "暗页", visibility="sealed")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        assert not hits(mgr, DARK_TOKEN, user_id=None), (
            "匿名检索到了暗页（L-2）")
    finally:
        close_manager(mgr)


def test_l2_a_released_element_is_invisible_to_every_identity(ws, cow):
    """L-2：已释放对**所有**身份不可见 —— 包括主人格本人。

    主人行使的是「遗忘」权：遗忘的内容就该从他的常规视野里消失；
    真源与墓碑保留，审计可查。
    """
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    release(f, ws, p)
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        for label, uid in (("anonymous", None),
                           ("第三方", "someone-else"),
                           ("owner", ACTOR)):
            found = hits(mgr, RELEASED_TOKEN, user_id=uid)
            assert not found, f"{label} 仍能检索到已释放正文（L-2）"
    finally:
        close_manager(mgr)


def test_l2_released_content_is_invisible_after_a_release_without_a_sync(ws, cow):
    """SEED 验收第 5 条：释放后**不跑 sync**，检索与 memory_get 两种调用，
    三种身份各 0 命中。

    检索自己会刷一次投影（那是设计里的写），但**不跑显式 sync** ——
    释放不改原档字节，扫描看不见这次释放，所以判据必须自己问真源。
    """
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())          # 先建索引
        release(f, ws, p)                 # 再释放
        # 故意不跑 sync
        for label, uid in (("anonymous", None),
                           ("第三方", "someone-else"),
                           ("owner", ACTOR)):
            found = hits(mgr, RELEASED_TOKEN, user_id=uid)
            assert not found, f"{label} 仍能检索到已释放正文（L-2）"
            out = MemoryGetTool(mgr).execute({"path": rel})
            assert RELEASED_TOKEN not in str(out.result), (
                f"{label} 通过 memory_get 读到了已释放正文（L-1）")
    finally:
        close_manager(mgr)


def test_l2_the_three_surfaces_agree_on_a_dark_page(ws, cow):
    """L-2 的核心：三面（facade.read / shelf / 索引谓词）口径必须一致。

    P0-13 实测过它们互相矛盾：facade.read 开放 sealed、shelf 跳过
    sealed、索引对所有人隐藏 sealed。三面现在都问同一个判据。
    """
    f = facade_for(ws, cow)
    p = write_event(f, "dark-evt", DARK_TOKEN, "暗页", visibility="sealed")
    rel = p.relative_to(ws).as_posix()
    # ① facade.read（本视角 → 可见）
    fm, body = f.read(rel)
    assert body and DARK_TOKEN in body, "facade.read 对本视角的暗页不可见"
    # ② shelf（本视角 → 出现，带 🔒）
    ShelfGenerator(ws, agent_id=ACTOR).rebuild()
    index = (ws / "memory" / "shelf" / "index.md").read_text(encoding="utf-8")
    assert "暗页" in index, "shelf 上没有本视角的暗页"
    assert "🔒" in index
    # ③ 索引谓词（本视角 → 可见）
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        with identity_scope(agent_id=ACTOR):
            found = hits(mgr, DARK_TOKEN)
        assert found, "索引谓词对本视角的暗页不可见"
    finally:
        close_manager(mgr)


def test_l2_the_three_surfaces_agree_on_a_released_element(ws, cow):
    """L-2：已释放在三面上都不可见。"""
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    rel = p.relative_to(ws).as_posix()
    release(f, ws, p)
    fm, body = f.read(rel)
    assert fm == {} and RELEASED_TOKEN not in body
    ShelfGenerator(ws, agent_id=ACTOR).rebuild()
    index = (ws / "memory" / "shelf" / "index.md").read_text(encoding="utf-8")
    assert "机密事件" not in index, "已释放的条目出现在书架上"
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        assert not hits(mgr, RELEASED_TOKEN)
    finally:
        close_manager(mgr)


def test_l2_reverse_an_open_page_stays_visible_to_everyone(ws, cow):
    """反向用例：白页必须对所有人可见 —— 否则上面的断言是「全都坏了」。"""
    f = facade_for(ws, cow)
    write_event(f, "open-evt", OPEN_TOKEN, "公开事件")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        for uid in (None, "someone-else", ACTOR):
            assert hits(mgr, OPEN_TOKEN, user_id=uid), (
                f"白页对身份 {uid!r} 不可见 —— 谓词把正常内容也吃了")
    finally:
        close_manager(mgr)


def test_l2_the_dark_page_is_indexed_under_its_own_scope(ws, cow):
    """L-2：暗页在索引里带自己的 scope 与归属令牌（不是共享白页）。

    P0-16 L-5：归属令牌住在**自己的列** ``sealed_owner``，不再和端用户
    共用 ``user_id``。P0-14 L-2 当初把归属令牌写进了 ``user_id``，那是
    两条轴混在一列 —— 端用户 id 只要撞上 ``agent--<id>`` 的形状就能读到
    暗页。语义没变（暗页带自己的 scope 和归属），变的是归属住在哪一列。
    """
    f = facade_for(ws, cow)
    p = write_event(f, "dark-evt", DARK_TOKEN, "暗页", visibility="sealed")
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        rows = mgr.storage.conn.execute(
            "SELECT DISTINCT scope, user_id, sealed_owner FROM chunks "
            "WHERE path = ?", (rel,)).fetchall()
        assert rows, "暗页根本没进索引"
        for row in rows:
            assert row["scope"] == SCOPE_SEALED, (
                f"暗页的 scope 不是 {SCOPE_SEALED}: {row['scope']!r}")
            assert row["sealed_owner"] == agent_owner_token(ACTOR), (
                f"暗页的归属令牌不对: {row['sealed_owner']!r}")
            # 端用户轴不承载归属令牌（P0-16 L-5）
            assert not (row["user_id"] or "").startswith("agent--"), (
                f"端用户轴里混进了归属令牌: {row['user_id']!r}")
    finally:
        close_manager(mgr)


def test_l2_a_dark_page_without_an_owner_is_invisible_to_everyone(ws, cow):
    """L-2 的 fail-closed 边界：无法确定归属的暗页 → 谁都看不见。"""
    f = facade_for(ws, cow)
    p = write_event(f, "dark-evt", DARK_TOKEN, "暗页", visibility="sealed")
    # 抹掉归属（front-matter 被手改）
    text = p.read_text(encoding="utf-8")
    p.write_text(re.sub(r"^author_viewpoint:.*$", "author_viewpoint:",
                         text, flags=re.M), encoding="utf-8", newline="\n")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        with identity_scope(agent_id=ACTOR):
            assert not hits(mgr, DARK_TOKEN), (
                "没有归属的暗页对主人格可见了 —— fail-closed 失效")
    finally:
        close_manager(mgr)


# ------------------------------------------------------------------ L-3 -----
# 投影刷新失败 → fail-closed（报错，不是静默放行）

def test_l3_a_failed_projection_refresh_makes_the_search_fail(ws, cow):
    """L-3：投影刷新失败 → 检索**报错**，不得返回内容。

    旧实现：吞掉异常、继续用陈旧标记 → 释放后的私密正文重新可见
    （隐私控件故障时恰好把私密内容放出去，且静默降级）。
    """
    f = facade_for(ws, cow)
    p = write_event(f, "secret-evt", RELEASED_TOKEN, "机密事件")
    release(f, ws, p)
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        # 注入真源解析失败
        def boom():
            raise RuntimeError("injected truth-layer read failure")

        mgr._release_state.get = boom
        with pytest.raises(Exception):
            asyncio.run(mgr.search(RELEASED_TOKEN, max_results=5, min_score=0.0))
    finally:
        close_manager(mgr)


def test_l3_the_failure_is_loud(ws, cow):
    """L-3：失败必须**出声** —— 日志 + 标记文件（巡检可发现）。"""
    f = facade_for(ws, cow)
    mgr = manager_for(ws)
    try:
        def boom():
            raise RuntimeError("injected truth-layer read failure")

        mgr._release_state.get = boom
        with pytest.raises(Exception):
            asyncio.run(mgr.search("ZZANYTOKEN", max_results=5, min_score=0.0))
        marker = ws / "memory" / ".audit" / "release_projection.json"
        assert marker.is_file(), (
            "投影刷新失败没有留下标记 —— 巡检发现不了这次降级")
        import json
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload.get("error")
        assert payload.get("stage") == "resolve"
    finally:
        close_manager(mgr)


def test_l3_the_inspector_reports_the_degraded_projection(ws, cow):
    """L-3：降级状态必须能被巡检发现（不是只躺在日志里）。"""
    from agent.memory.inspector import Inspector

    f = facade_for(ws, cow)
    mgr = manager_for(ws)
    try:
        def boom():
            raise RuntimeError("injected truth-layer read failure")

        mgr._release_state.get = boom
        with pytest.raises(Exception):
            asyncio.run(mgr.search("ZZANYTOKEN", max_results=5, min_score=0.0))
        report = Inspector(ws).inspect(trigger="manual")
        codes = [v.get("code") for v in report["violations"]]
        assert "release-projection-degraded" in codes, (
            f"巡检器没有报出降级状态: {report['violations']}")
    finally:
        close_manager(mgr)


def test_l3_the_search_works_again_after_the_failure_is_gone(ws, cow):
    """反向用例：故障恢复后检索必须恢复正常 —— 否则 fail-closed 只是把
    检索关死了。"""
    f = facade_for(ws, cow)
    write_event(f, "open-evt", OPEN_TOKEN, "公开事件")
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())
        original = mgr._release_state.get

        def boom():
            raise RuntimeError("injected truth-layer read failure")

        mgr._release_state.get = boom
        with pytest.raises(Exception):
            asyncio.run(mgr.search(OPEN_TOKEN, max_results=5, min_score=0.0))
        mgr._release_state.get = original
        assert hits(mgr, OPEN_TOKEN), "恢复之后检索仍然失败"
        assert not (ws / "memory" / ".audit" / "release_projection.json").exists(), (
            "恢复之后降级标记没有被清掉")
    finally:
        close_manager(mgr)


def test_l3_a_read_exit_also_fails_closed(ws, cow):
    """L-3：读取出口同样 fail-closed —— 状态不明就不给内容。"""
    f = facade_for(ws, cow)
    p = write_event(f, "open-evt", OPEN_TOKEN, "公开事件")
    rel = p.relative_to(ws).as_posix()
    mgr = manager_for(ws)
    try:
        asyncio.run(mgr.sync())

        def boom():
            raise RuntimeError("injected truth-layer read failure")

        mgr._release_state.get = boom
        out = MemoryGetTool(mgr).execute({"path": rel})
        assert OPEN_TOKEN not in str(out.result), (
            "真源状态不明时，读取出口仍然返回了正文（L-3）")
        assert out.status == "error"
    finally:
        close_manager(mgr)


# --------------------------------------------------- the classification ------

def test_the_visibility_classification_is_one_definition():
    """L-2：分类只有一处定义，且 sealed 与 released 互不相认。"""
    from agent.memory.visibility import VIS_OPEN, VIS_RELEASED, VIS_SEALED

    assert classify_frontmatter({"status": "released"}) == VIS_RELEASED
    assert classify_frontmatter({"type": "release"}) == VIS_RELEASED
    assert classify_frontmatter({"visibility": "sealed"}) == VIS_SEALED
    assert classify_frontmatter({"visibility": "sealed",
                                  "status": "released"}) == VIS_RELEASED
    assert classify_frontmatter({"visibility": "open"}) == VIS_OPEN
    assert classify_frontmatter({}) == VIS_OPEN


def test_the_owner_token_is_the_author_viewpoint():
    """L-2：暗页的归属 = 写它的人（author_viewpoint）。"""
    assert owner_token({"author_viewpoint": ACTOR}) == agent_owner_token(ACTOR)
    assert owner_token({}) is None
    assert agent_owner_token(ACTOR) != agent_owner_token(OTHER)
