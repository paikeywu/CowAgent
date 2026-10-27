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
    TargetExistsError,
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


# ---------------------------------------------------------------------------
# P0-20 L-2：暗页的双轴语义在**真实样本**上成立
# ---------------------------------------------------------------------------
#
# 交办单指出的正是这一点：「所有暗页相关的规则、谓词、遮蔽逻辑，
# 全部是在 0 个真实样本上验证的。」
#
# 上面那批用例在临时目录里造了一个像样的工作区，但它们造的是**测试的
# 形状**：临时目录里没有共享知识库、没有真实的扫描根、没有真实的投影
# 刷新。下面的用例走**生产路径**在**真实工作区**上造暗页 —— 门面写
# front-matter、扫描按真源路径索引、投影从磁盘刷新 —— 然后验三面。
#
# 为什么不用 tmp_path：真实工作区的索引里同时有共享知识库和主人格身份
# 文件，白页的反向用例必须在一堆真实内容里成立，才说明谓词没有把正常
# 内容一起吃掉。

#: The REAL workspace. The acceptance criterion's fixed default, so the
#: probe and this test measure the same corpus.
REAL_WORKSPACE = r"C:\Users\User\cow"

#: This Agent's own id — the owner of the dark page it writes.
REAL_ACTOR = "agent-mvnewfyb"

REAL_DARK_TOKEN = "ZZDARKREAL9921"
REAL_OPEN_TOKEN = "ZZOPENREAL8834"


def _real_body(token, title):
    return ("# %s\n\n## 发生了什么\n\n这段正文包含标记词%s。\n\n"
            "## 感受\n\n这是暗页的三面验证样本。\n" % (title, token))


def _real_facade():
    return WriteFacade(REAL_WORKSPACE, actor=REAL_ACTOR,
                        cow_root=REAL_WORKSPACE,
                        channel=RecordingConfirmationChannel())


def _write_real_event(slug, title, token, **editable):
    """Write a real event in the real workspace, idempotently.

    WR-08 forbids overwriting an existing element, so a second run of this
    test must REUSE the file the first run wrote instead of failing on it.
    A test that only passes on a clean machine is a test that fails for a
    reason that has nothing to do with what it measures.
    """
    facade = _real_facade()
    body = _real_body(token, title)
    try:
        return facade.write_event(slug, title, body, date="2026-10-28",
                                  **editable)
    except TargetExistsError:
        from agent.memory.naming import build_memory_path

        rel = build_memory_path("events", slug, body, date="2026-10-28")
        existing = facade._resolve(rel)
        assert existing.exists(), (
            "写入报『已存在』但文件不在 —— 判据的前件不成立")
        return existing


def _real_manager():
    cfg = MemoryConfig(workspace_root=REAL_WORKSPACE, sync_on_search=False)
    return MemoryManager(cfg, embedding_provider=None, reranker=None)


def _real_db_path():
    return Path(REAL_WORKSPACE) / "memory" / "long-term" / "index.db"


@pytest.mark.skipif(not Path(REAL_WORKSPACE).is_dir(),
                    reason="真实工作区不在本机")
def test_l2_a_real_dark_page_is_sealed_only_for_its_owner():
    """L-2（真实样本）：生产路径造的暗页，三面可见性必须一致。

    ① 主人格可见 ② 第三方不可见 ③ 匿名不可见
    ④ 索引里 scope='sealed' 且 sealed_owner 非空
    ⑤ 端用户轴不承载归属令牌（P0-16 L-5 的两轴分离）
    """
    import sqlite3

    dark = _write_real_event(
        "p020-regression-dark", "P0-20 暗页回归", REAL_DARK_TOKEN,
        visibility="sealed")
    rel = dark.relative_to(REAL_WORKSPACE).as_posix()
    mgr = _real_manager()
    try:
        asyncio.run(mgr.sync())

        with identity_scope(agent_id=REAL_ACTOR):
            owner_found = hits(mgr, REAL_DARK_TOKEN)
        assert owner_found, "主人格检索不到自己的暗页（真实工作区上 L-2 失效）"
        assert not hits(mgr, REAL_DARK_TOKEN, user_id="someone-else"), (
            "第三方检索到了别人的暗页（真实工作区上 L-2 失效）")
        assert not hits(mgr, REAL_DARK_TOKEN, user_id=None), (
            "匿名检索到了暗页（真实工作区上 L-2 失效）")

        conn = sqlite3.connect("file:%s?mode=ro" % _real_db_path(), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT DISTINCT scope, user_id, sealed_owner FROM chunks "
                "WHERE path = ?", (rel,)).fetchall()
        finally:
            conn.close()
        assert rows, "暗页根本没进真实索引"
        for row in rows:
            assert row["scope"] == SCOPE_SEALED, (
                f"暗页的 scope 不是 {SCOPE_SEALED}: {row['scope']!r}")
            assert row["sealed_owner"] == agent_owner_token(REAL_ACTOR), (
                f"暗页的归属令牌不对: {row['sealed_owner']!r}")
            assert not (row["user_id"] or "").startswith("agent--"), (
                f"端用户轴里混进了归属令牌: {row['user_id']!r}")
    finally:
        close_manager(mgr)


@pytest.mark.skipif(not Path(REAL_WORKSPACE).is_dir(),
                    reason="真实工作区不在本机")
def test_l2_reverse_a_real_open_page_is_not_mistaken_for_a_dark_page():
    """L-2 反向用例（真实样本）：白页不得被误判成暗页。

    没有这条，上面那条可能只是「全都坏了」—— 白页和暗页一起消失，
    三面断言里的「主人格可见」之外的每一条都会照样成立。
    """
    import sqlite3

    opened = _write_real_event(
        "p020-regression-open", "P0-20 白页回归", REAL_OPEN_TOKEN,
        visibility="open")
    rel = opened.relative_to(REAL_WORKSPACE).as_posix()
    mgr = _real_manager()
    try:
        asyncio.run(mgr.sync())
        with identity_scope(agent_id=REAL_ACTOR):
            assert hits(mgr, REAL_OPEN_TOKEN), "白页对主人格不可见"
        assert hits(mgr, REAL_OPEN_TOKEN, user_id="someone-else"), (
            "白页对第三方不可见 —— 谓词把正常内容也吃了")
        assert hits(mgr, REAL_OPEN_TOKEN, user_id=None), (
            "白页对匿名不可见 —— 谓词把正常内容也吃了")

        conn = sqlite3.connect("file:%s?mode=ro" % _real_db_path(), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT DISTINCT scope, sealed_owner FROM chunks "
                "WHERE path = ?", (rel,)).fetchall()
        finally:
            conn.close()
        assert rows, "白页根本没进真实索引"
        for row in rows:
            assert row["scope"] != SCOPE_SEALED, (
                "白页被标成了暗页 —— 整库都会变暗页")
            assert not row["sealed_owner"], (
                f"白页带上了归属令牌: {row['sealed_owner']!r}")
    finally:
        close_manager(mgr)


# ---------------------------------------------------------------------------
# P0-21 · 写侧守卫：暗页不能被写成公开行 —— 哪怕写它的是不认识 visibility 的旧版
# ---------------------------------------------------------------------------
#
# 实测（P0-21）：2026-10-13 打包的桌面版**还在跑**，而且和源码版**共用同一个
# index.db**。它的 sync 不认识 `visibility: sealed`，把 memory/ 下的每个 md
# 都写成 `scope='shared'` + `user_id=NULL` —— 正是匿名可读的形状。
# 在隔离工作区用旧版代码实测：它自己的匿名检索返回了暗页的 front-matter 和正文。
#
# 旧版是已打包的 exe，改不了它。所以不变量只能在**数据库的写入边界**上强制：
# 一个触发器，谁写都得过。守卫只会让行**更不可见**，从不放宽访问。

GUARD_DARK_TEXT = (
    "---\n"
    "element_id: guard-dark--00000000\n"
    "type: event\n"
    "status: active\n"
    "visibility: sealed\n"
    "author_viewpoint: tester\n"
    "---\n\n"
    "# 暗页\n\n正文标记 ZZGUARDDARK9911。\n"
)
GUARD_OPEN_TEXT = (
    "---\n"
    "element_id: guard-open--00000000\n"
    "type: event\n"
    "status: active\n"
    "visibility: open\n"
    "---\n\n"
    "# 白页\n\n正文标记 ZZGUARDOPEN9911。\n"
)
GUARD_DARK_PATH = "memory/markdown/events/2026-10-27/guard-dark--00000000.md"
GUARD_OPEN_PATH = "memory/markdown/events/2026-10-27/guard-open--00000000.md"


def _guard_storage(ws):
    """A storage whose index directory exists.

    The ``ws`` fixture creates the truth-layer directories but not the
    index directory, and SQLite refuses to create a database in a
    directory that is not there.
    """
    db = ws / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return MemoryStorage(db_path=str(db))


def _old_build_insert(storage, path, text, chunk_id="old-c1"):
    """Exactly what the 2026-10-13 desktop build does.

    It names no visibility at all and puts the row in the shared scope —
    the shape an anonymous caller reads. It writes through raw SQL, so
    nothing in this build's own write path can stand in its way.
    """
    storage.conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
    storage.conn.execute(
        "INSERT INTO chunks (id,user_id,scope,source,path,start_line,end_line,"
        "text,embedding,hash,metadata,updated_at) "
        "VALUES (?,NULL,'shared','memory',?,1,1,?,NULL,'h',NULL,strftime('%s','now'))",
        (chunk_id, path, text))
    storage.conn.commit()


def _row_for(storage, path):
    return storage.conn.execute(
        "SELECT scope, user_id, file_sealed, element_released, sealed_owner "
        "FROM chunks WHERE path = ?", (path,)).fetchone()


def test_p021_the_guard_seals_a_dark_page_written_by_the_old_build(ws):
    """P0-21：旧版写的暗页不能是公开行。

    这条是本轮的核心：旧版的 sync 会把暗页写成 ``scope='shared'`` +
    ``user_id=NULL``，而那正是匿名可读的形状。守卫把它挡住。
    """
    st = _guard_storage(ws)
    try:
        _old_build_insert(st, GUARD_DARK_PATH, GUARD_DARK_TEXT)
        row = _row_for(st, GUARD_DARK_PATH)
        assert row is not None, "旧版写入的行不见了 —— 测法有问题"
        assert row["scope"] != "shared", (
            f"暗页被写成了公开行: scope={row['scope']!r} —— 这就是泄漏")
        assert row["scope"] == SCOPE_SEALED, (
            f"暗页没有落到自己的 scope: {row['scope']!r}")
        assert row["user_id"] is None, (
            f"暗页的端用户轴不该有值: {row['user_id']!r}")
        assert row["file_sealed"], "暗页没有打上 sealed 标志"

        # 匿名检索读不到它 —— 这才是判据
        hits_anon = st.search_keyword(query="ZZGUARDDARK9911", user_id=None,
                                      scopes=["shared"], limit=20)
        assert not any("ZZGUARDDARK9911" in (h.snippet or "") for h in hits_anon), (
            "匿名检索拿到了暗页正文 —— 泄漏")
    finally:
        st.close()


def test_p021_reverse_the_guard_leaves_an_open_page_public(ws):
    """P0-21 反向用例：白页不得被守卫误伤。

    没有这条，上一条可能只是「全都变暗了」—— 一个把整库封死的守卫同样
    能让上一条变绿，而那不是修复。
    """
    st = _guard_storage(ws)
    try:
        _old_build_insert(st, GUARD_OPEN_PATH, GUARD_OPEN_TEXT,
                          chunk_id="old-c2")
        row = _row_for(st, GUARD_OPEN_PATH)
        assert row["scope"] == "shared", (
            f"白页被守卫封了: scope={row['scope']!r} —— 守卫把整库变暗页了")
        assert not row["file_sealed"], "白页被打上了 sealed 标志"
        hits_anon = st.search_keyword(query="ZZGUARDOPEN9911", user_id=None,
                                      scopes=["shared"], limit=20)
        assert any("ZZGUARDOPEN9911" in (h.snippet or "") for h in hits_anon), (
            "白页对匿名不可见 —— 守卫把正常内容也吃了")
    finally:
        st.close()


def test_p021_the_guard_keeps_the_owner_the_truth_layer_recorded(ws):
    """P0-21：真源层登记了归属，守卫就要把它带上。

    守卫的 front-matter 兜底**故意不猜**归属（猜错就是隐私事故），所以
    没有真源层登记时暗页对谁都不可见 —— 安全但不可用。真源层登记之后，
    归属必须回到行上，否则主人格永远看不见自己的暗页。
    """
    from agent.memory.index_guard import CLASS_SEALED, record_paths

    st = _guard_storage(ws)
    try:
        record_paths(st.conn, [(GUARD_DARK_PATH, CLASS_SEALED,
                                "agent--tester")])
        _old_build_insert(st, GUARD_DARK_PATH, GUARD_DARK_TEXT,
                          chunk_id="old-c3")
        row = _row_for(st, GUARD_DARK_PATH)
        assert row["sealed_owner"] == "agent--tester", (
            f"暗页丢了归属: {row['sealed_owner']!r} —— 主人格看不见自己的暗页")
        assert row["scope"] == SCOPE_SEALED
    finally:
        st.close()


def test_p021_the_guard_covers_a_chunk_without_frontmatter(ws):
    """P0-21：一个文件切成多块时，只有第一块带 front-matter。

    旧版把第一块封住之后，剩下几块如果没有登记就会落回公开形状 ——
    而它们和第一块是同一个文件。
    """
    from agent.memory.index_guard import CLASS_SEALED, record_paths

    st = _guard_storage(ws)
    try:
        record_paths(st.conn, [(GUARD_DARK_PATH, CLASS_SEALED, None)])
        _old_build_insert(st, GUARD_DARK_PATH,
                          "# 第二块，没有 front-matter\n\nZZGUARDDARK9911 还在。\n",
                          chunk_id="old-c4")
        row = _row_for(st, GUARD_DARK_PATH)
        assert row["scope"] != "shared", (
            f"没有 front-matter 的块落成了公开行: {row['scope']!r}")
        hits_anon = st.search_keyword(query="ZZGUARDDARK9911", user_id=None,
                                      scopes=["shared"], limit=20)
        assert not any("ZZGUARDDARK9911" in (h.snippet or "") for h in hits_anon)
    finally:
        st.close()


def test_p021_the_guard_covers_the_excluded_layers(ws):
    """P0-21：排除层（书架/事实层/墓碑）同样不能被写成公开行。

    书架里是「我记得的 / 我没记住的 / 你释放的」—— 索引它等于把记忆的
    目录公开。实测旧版会把 57 个书架文件全部索引成 shared。
    """
    st = _guard_storage(ws)
    try:
        _old_build_insert(st, "memory/shelf/index.md",
                          "# 书架\n\nZZGUARDOPEN9911\n", chunk_id="old-c5")
        row = _row_for(st, "memory/shelf/index.md")
        assert row is not None
        assert row["scope"] != "shared", (
            f"排除层被写成了公开行: {row['scope']!r}")
        hits_anon = st.search_keyword(query="ZZGUARDOPEN9911", user_id=None,
                                      scopes=["shared"], limit=20)
        assert not any("ZZGUARDOPEN9911" in (h.snippet or "") for h in hits_anon)
    finally:
        st.close()


def test_p021_the_guard_survives_the_upsert_path(ws):
    """P0-21：旧版的 save_chunk 走 UPSERT（ON CONFLICT DO UPDATE）。

    只测 INSERT 是不够的：UPSERT 走的是 UPDATE 分支，守卫必须也在那里。
    """
    st = _guard_storage(ws)
    try:
        st.conn.execute(
            "INSERT INTO chunks (id,user_id,scope,source,path,start_line,end_line,"
            "text,embedding,hash,metadata,updated_at) "
            "VALUES ('old-c6',NULL,'shared','memory',?,1,1,?,NULL,'h',NULL,"
            "strftime('%s','now')) ON CONFLICT(id) DO UPDATE SET "
            "scope=excluded.scope, text=excluded.text",
            (GUARD_DARK_PATH, GUARD_DARK_TEXT))
        st.conn.commit()
        row = _row_for(st, GUARD_DARK_PATH)
        assert row["scope"] != "shared", (
            f"UPSERT 路径绕过了守卫: {row['scope']!r}")
    finally:
        st.close()


def test_p021_the_guard_leaves_the_fts_index_intact(ws):
    """P0-21：守卫不能把 FTS5 索引搞坏。

    实测过一个更朴素的写法（AFTER 触发器里 UPDATE chunks）：FTS5 的同步
    触发器会去删一条还不存在的 FTS 行，结果是 ``database disk image is
    malformed``。守卫改成 BEFORE 触发器 + RAISE(IGNORE)，就不碰那个顺序。
    """
    st = _guard_storage(ws)
    try:
        _old_build_insert(st, GUARD_DARK_PATH, GUARD_DARK_TEXT)
        _old_build_insert(st, GUARD_OPEN_PATH, GUARD_OPEN_TEXT,
                          chunk_id="old-c7")
        integrity = st.conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"FTS5 索引被守卫搞坏了: {integrity}"
        n = st.conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'ZZGUARDOPEN9911'"
        ).fetchone()[0]
        assert n == 1, f"FTS5 索引里的白页不见了（{n} 行）"
    finally:
        st.close()


def test_p021_the_guard_does_not_depend_on_the_trigger_order():
    """P0-21：守卫与 FTS5 触发器的先后顺序无关。

    实测：AFTER 触发器里改行，只有在守卫比 FTS 触发器**旧**的时候才安全
    —— 而 FTS 触发器会被重建，重建后它就更新了，守卫随即失效并损坏索引。
    BEFORE 触发器不依赖顺序，所以这条把它钉死。
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("""CREATE TABLE chunks (
            id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, source TEXT,
            path TEXT, start_line INT, end_line INT, text TEXT,
            embedding BLOB, hash TEXT, metadata TEXT, updated_at INT,
            file_sealed INT DEFAULT 0, element_released INT DEFAULT 0,
            sealed_owner TEXT)""")
        conn.execute("""CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text, id, user_id, path, source, scope,
            content='chunks', content_rowid='rowid')""")
        # FTS 触发器先建（守卫后建）—— 也就是「最坏的那种顺序」
        conn.execute("""CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
            INSERT INTO chunks_fts(rowid, text, id, user_id, path, source, scope)
            VALUES (new.rowid, new.text, new.id, new.user_id, new.path,
                    new.source, new.scope);
        END""")
        conn.execute("""CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, id, user_id, path, source, scope)
            VALUES ('delete', old.rowid, old.text, old.id, old.user_id, old.path, old.source, old.scope);
            INSERT INTO chunks_fts(rowid, text, id, user_id, path, source, scope)
            VALUES (new.rowid, new.text, new.id, new.user_id, new.path, new.source, new.scope);
        END""")
        from agent.memory.index_guard import install_guard
        install_guard(conn)
        conn.execute(
            "INSERT INTO chunks (id,user_id,scope,source,path,start_line,end_line,"
            "text,embedding,hash,metadata,updated_at) "
            "VALUES ('x',NULL,'shared','memory','memory/markdown/events/d.md',1,1,"
            "?,NULL,'h',NULL,strftime('%s','now'))", (GUARD_DARK_TEXT,))
        conn.commit()
        row = conn.execute("SELECT scope FROM chunks WHERE id='x'").fetchone()
        assert row[0] != "shared", "守卫在「后建」顺序下没生效"
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"FTS5 索引被搞坏了: {integrity}"
    finally:
        conn.close()


def test_p021_the_guard_is_installed_on_every_index(tmp_path):
    """P0-21：守卫要装到**每一个**工作区的索引上。

    桌面版用的是实例根（``~/cow``）那个库 —— 源码版一个都没寻址过的工作区，
    它的库就停在旧 schema。守卫同理：只装到被打开过的库上，等于没装。
    """
    import sqlite3

    from agent.memory.index_guard import is_installed
    from agent.memory.schema_migrate import install_guard_all

    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        db = root / "memory" / "long-term" / "index.db"
        db.parent.mkdir(parents=True)
        st = MemoryStorage(db_path=str(db))
        st.close()
        roots.append(str(root))
    # A third index that does not exist yet must not break the sweep.
    roots.append(str(tmp_path / "missing"))

    result = install_guard_all(roots=roots)
    assert set(result) == {roots[0], roots[1]}, result
    assert all(result.values()), f"有索引没装上守卫: {result}"
    for root in roots[:2]:
        conn = sqlite3.connect(
            str(tmp_path / root / "memory" / "long-term" / "index.db"))
        try:
            assert is_installed(conn), f"{root} 的索引没有守卫"
        finally:
            conn.close()


# -----------------------------------------------------------------------------
# P0-22：守卫的丢弃必须可见、可查；守卫必须由机制保证
# -----------------------------------------------------------------------------
#
# 风控对 P0-21 的结论：「静默丢弃零审计痕迹 —— guard_paths.updated_at 重复
# 丢弃时不动、无日志、巡检第 9 项按构造看不见。」这是把旧的『静默失败』换
# 了个地方继续存在：RAISE(IGNORE) 是一次无声的操作 —— 写者没收到异常，返回
# 值正常，而行被悄悄改掉了。
#
# 所以本节钉死三件事：
#   ① 每次丢弃都留下痕迹（计数 + 时间戳 + 事件行）；
#   ② 运维能问出「这次挡了多少、挡了哪些」；
#   ③ 守卫是机制不是快照 —— 触发器被删了，下一次 sync 会装回来。

def test_p022_the_guard_records_every_discard(ws):
    """P0-22：一次丢弃 = 一条事件 + 计数前进 + 时间戳前进。"""
    from agent.memory.index_guard import (
        discard_count,
        last_discard_at,
        recent_discards,
    )

    st = _guard_storage(ws)
    try:
        assert discard_count(st.conn) == 0
        assert last_discard_at(st.conn) is None

        _old_build_insert(st, GUARD_DARK_PATH, GUARD_DARK_TEXT)

        assert discard_count(st.conn) == 1, "丢弃没有计数"
        assert last_discard_at(st.conn), "丢弃没有留下时间戳"
        events = recent_discards(st.conn, minutes=60)
        assert len(events) == 1, f"丢弃没有留下事件行: {events}"
        assert events[0]["path"] == GUARD_DARK_PATH
        assert events[0]["class"] == "sealed"
        assert events[0]["op"] == "insert"

        # A second discard moves the counter again — it does not collapse
        # into "the same thing happened once".
        _old_build_insert(st, "memory/shelf/index.md", "# 书架\n\nZZ\n",
                          chunk_id="old-c22")
        assert discard_count(st.conn) == 2
        assert len(recent_discards(st.conn, minutes=60)) == 2
    finally:
        st.close()


def test_p022_the_guard_says_what_it_dropped(ws):
    """P0-22：「这次 sync 挡了多少行、挡了哪些？」要能被回答。"""
    from agent.memory.index_guard import guard_report

    st = _guard_storage(ws)
    try:
        _old_build_insert(st, GUARD_DARK_PATH, GUARD_DARK_TEXT)
        _old_build_insert(st, "memory/shelf/index.md", "# 书架\n\nZZ\n",
                          chunk_id="old-c23")
        rep = guard_report(st.conn, minutes=60)
        assert rep["blocked_count"] == 2, rep
        assert rep["blocked_recent"] == 2, rep
        assert rep["last_at"], rep
        paths = {p["path"] for p in rep["paths"]}
        assert GUARD_DARK_PATH in paths, rep
        assert "memory/shelf/index.md" in paths, rep
    finally:
        st.close()


def test_p022_the_manager_reports_the_discards_it_saw(ws):
    """P0-22：应用层必须把丢弃变成可见事件（不能只靠触发器悄悄做）。

    交办单原话：「不得只靠 SQLite 触发器悄悄做 —— 若必须用，则必须在应用
    层捕获并转成可见事件。」所以这一条断言的是**管理器**看得见，并且
    ``guard_report()`` 能回答运维的问题。
    """
    mgr = manager_for(ws)
    try:
        assert mgr.guard_report()["blocked_count"] == 0
        # The old build's write goes straight through the guard.
        _old_build_insert(mgr.storage, GUARD_DARK_PATH, GUARD_DARK_TEXT)
        rep = mgr.guard_report()
        assert rep["blocked_count"] == 1, rep
        assert GUARD_DARK_PATH in {p["path"] for p in rep["paths"]}, rep
    finally:
        close_manager(mgr)


def test_p022_a_deleted_guard_is_reinstalled_by_the_mechanism(tmp_path):
    """P0-22：守卫被删掉 → 机制必须把它装回来（自愈）。

    ``CREATE TRIGGER IF NOT EXISTS`` 永不抛异常，所以一个被删了触发器的库
    会**静默裸奔**：没有任何检测器会说话，直到有人发现泄漏。
    """
    import sqlite3

    from agent.memory.index_guard import GUARD_TRIGGERS, is_installed
    from agent.memory.schema_migrate import ensure_guard_all

    root = tmp_path / "ws"
    db = root / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True)
    st = MemoryStorage(db_path=str(db))
    st.close()

    conn = sqlite3.connect(str(db))
    try:
        for name in GUARD_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.commit()
        assert not is_installed(conn), "触发器没删掉 —— 测法有问题"
    finally:
        conn.close()

    states = ensure_guard_all(roots=[str(root)])
    assert states[str(root)]["status"] in ("installed", "repaired"), states

    conn = sqlite3.connect(str(db))
    try:
        assert is_installed(conn), "机制没有把守卫装回来"
    finally:
        conn.close()


def test_p022_the_guard_sweep_finds_an_index_the_roster_does_not_name(tmp_path,
                                                                       monkeypatch):
    """P0-22：花名册漏一个 = 守卫漏一个 —— 所以机制必须看磁盘。

    ``agents/default`` 不在花名册里，这就是「手工补装的一次性快照」的
    由来。所以发现索引的依据必须是**磁盘上真实存在的库**，而不只是花名册。
    """
    import sqlite3

    from agent.memory.index_guard import (
        discover_index_roots,
        is_installed,
    )
    from agent.memory.schema_migrate import ensure_guard_all

    shared = tmp_path / "cow"
    stray = shared / "agents" / "default"
    db = stray / "memory" / "long-term" / "index.db"
    db.parent.mkdir(parents=True)
    st = MemoryStorage(db_path=str(db))
    st.close()

    # The roster knows nothing about this workspace.
    monkeypatch.setattr("agent.registry.get_agent_registry", lambda: None)
    monkeypatch.setattr("common.state_dir.shared_root", lambda: shared)

    roots = discover_index_roots()
    assert str(stray) in roots, (
        f"磁盘上存在的索引没有被发现: {roots}")

    states = ensure_guard_all(roots=roots)
    assert str(stray) in states, states
    conn = sqlite3.connect(str(db))
    try:
        assert is_installed(conn), "花名册之外的索引没有装上守卫"
    finally:
        conn.close()


def test_p022_ensure_guard_tells_installed_from_failed(tmp_path):
    """P0-22：「已装」和「装不上」必须是两个答案。"""
    import sqlite3

    from agent.memory.index_guard import ensure_guard

    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        # A database with no chunks table: the guard cannot be installed.
        state = ensure_guard(conn)
        assert state["status"] == "failed", state
        assert state["reason"], "装不上必须给出原因"

        conn.execute("""CREATE TABLE chunks (
            id TEXT PRIMARY KEY, user_id TEXT, scope TEXT, source TEXT,
            path TEXT, start_line INT, end_line INT, text TEXT,
            embedding BLOB, hash TEXT, metadata TEXT, updated_at INT,
            file_sealed INT DEFAULT 0, element_released INT DEFAULT 0,
            sealed_owner TEXT)""")
        conn.commit()
        first = ensure_guard(conn)
        assert first["status"] == "installed", first
        second = ensure_guard(conn)
        assert second["status"] == "present", (
            f"已经装好的守卫必须报 present: {second}")
    finally:
        conn.close()

