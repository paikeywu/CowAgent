#!/usr/bin/env python3
# encoding:utf-8
"""Phase-one acceptance criteria for the live-memory system (P0-18).

This file is the criterion, not a scratch script: it is in the repository
so that ``git log`` is the history of what was checked and when. The old
``tmp/probe_phase1_accept.py`` forwards here.

The six changes that define this version (P0-18):

  1. every criterion prints the FINGERPRINT of the index it measured —
     path, row count, mtime. A PASS without a fingerprint is not a PASS;
  2. the integration criteria assert the corpus is NON-EMPTY before they
     report a retrieval failure;
  3. unit criteria and integration criteria are named apart — ``T1b(unit)``
     builds its own index and tests the RULE, ``T3(REAL)`` measures a
     real workspace and tests the CORPUS;
  4. the default workspace is FIXED — the registry and the environment
     are not consulted;
  5. a failing criterion says whether the thing it measures exists;
  6. the criteria are version-controlled.

Usage:
    python -m agent.memory.acceptance.phase1 --workspace "C:\\Users\\User\\cow"
    python -m agent.memory.acceptance.phase1 --baseline
    python agent/memory/acceptance/phase1.py --workspace DIR
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

parser = argparse.ArgumentParser()
parser.add_argument("--baseline", action="store_true",
                    help="run against tmp/_baseline (the pre-fix code)")
parser.add_argument("--code-root", default=None,
                    help="explicit code root (e.g. a git worktree of HEAD)")
parser.add_argument("--workspace", default=None,
                    help="the workspace to measure (default: the fixed one)")
parser.add_argument("--no-sync", action="store_true")
ARGS, _ = parser.parse_known_args()

if ARGS.code_root:
    CODE_ROOT = Path(ARGS.code_root).resolve()
elif ARGS.baseline:
    CODE_ROOT = REPO / "tmp" / "_baseline"
else:
    CODE_ROOT = REPO
sys.path.insert(0, str(CODE_ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from agent.memory.acceptance import (  # noqa: E402
    DEFAULT_WORKSPACE,
    Report,
    SourceTreeWorkspaceError,
    assert_not_source_tree,
    fingerprint,
    resolve_workspace,
)

TWO_CHAR_WORDS = ["事件", "实体", "知识", "关系", "标签", "时序", "人物", "事实"]


def wait_maintenance(db_path, timeout=15.0):
    """Wait for MemoryStorage's background maintenance thread to finish.

    It opens its own connection to run the repair/integrity scan and
    closes it in a finally block. A tempdir cleanup has to wait for that,
    otherwise Windows raises WinError 32. This is the probe's obligation,
    not a product bug.
    """
    import agent.memory.storage as st

    key = str(db_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with st._maintenance_lock:
            if key not in st._maintenance_running:
                return True
        time.sleep(0.1)
    return False


def temp_fingerprint(ws):
    """The fingerprint of the temporary index a unit criterion built."""
    return fingerprint(str(ws))


# ---------------------------------------------------------------------------
# T1-a 谓词收敛（静态）
# ---------------------------------------------------------------------------
def judge_t1a_single_predicate(report):
    """The old literal must be gone; the predicate is defined in ONE place.

    The scan covers the IMPLEMENTATION tree only (``agent/``). The
    criterion's own file contains the old literal as a PATTERN — a
    checker that scans itself finds itself, and the criterion then fails
    for the wrong reason. This is the same trap the acceptance skill
    records: "the checker scans itself".
    """
    import re

    # The patterns are assembled from fragments, not written out. A
    # criterion that spells the old literal out finds ITSELF: the file
    # that holds the pattern contains the pattern, so the count is 2
    # (the real definition and the criterion) instead of 1, and the
    # criterion fails for a reason that has nothing to do with the code.
    # The fragments are split so no line of this file contains the
    # searched text.
    old_pats = [
        r"\(" + r"scope = 'shared' OR user_id",
        r"\(" + r"chunks\.scope = 'shared' OR chunks\.user_id",
    ]
    def_marker = "def " + "visibility_clause"
    detail, ok = [], True
    old_total = 0
    helper_defs = 0
    helper_uses = []
    # The implementation tree only. ``tests/`` and this package are not
    # implementation, and this package is where the patterns live.
    scan_roots = [p for p in (CODE_ROOT / "agent",) if p.is_dir()]
    if not scan_roots:
        scan_roots = [CODE_ROOT]
    for scan_root in scan_roots:
        for dirpath, dirs, files in os.walk(scan_root):
            dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', '.pytest_cache',
                                                    'tmp', 'node_modules', 'build', 'venv')]
            for fn in files:
                if not fn.endswith('.py'):
                    continue
                p = Path(dirpath) / fn
                try:
                    src = p.read_text(encoding='utf-8')
                except Exception:
                    continue
                for i, line in enumerate(src.split('\n'), 1):
                    for pat in old_pats:
                        if re.search(pat, line):
                            old_total += 1
                            detail.append(f"  残留: {p.relative_to(CODE_ROOT)}:{i} {line.strip()[:80]}")
                    if def_marker in line:
                        helper_defs += 1
                    if 'visibility_clause(' in line and 'def ' not in line:
                        helper_uses.append(f"{p.relative_to(CODE_ROOT)}:{i}")
    detail.append(f"扫描范围: {', '.join(str(p.relative_to(CODE_ROOT)) for p in scan_roots)}（实现树）")
    detail.append(f"旧谓词字面量残留: {old_total} 处（要求 0）")
    detail.append(f"helper 定义: {helper_defs} 处（要求 1）")
    detail.append(f"helper 调用点: {len(helper_uses)} 处 -> {helper_uses}")
    ok = (old_total == 0 and helper_defs == 1 and len(helper_uses) >= 5)
    return report.judge("T1a(unit)", "可见性谓词全仓单点（旧字面量 0 残留，helper 唯一且 5 处调用）",
                        ok, "\n".join(detail))


# ---------------------------------------------------------------------------
# T1-b A/B 行为（单元：自建临时库）
# ---------------------------------------------------------------------------
def judge_t1b_ab_visibility(report):
    """A/B: own private + shared; cross-lookups miss; anonymous sees only
    unowned shared; a mislabelled row (scope=shared but with an owner) is
    invisible to everybody else (fail-closed)."""
    detail = []
    try:
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager
        from agent.memory.storage import MemoryChunk
    except ImportError as e:
        return report.judge("T1b(unit)", "A/B 私有记忆隔离", False, f"导入失败: {e}")

    with tempfile.TemporaryDirectory() as ws:
        cfg = MemoryConfig(workspace_root=ws, sync_on_search=False)
        mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)

        def put(cid, user_id, scope, marker):
            mgr.storage.save_chunk(MemoryChunk(
                id=cid, user_id=user_id, scope=scope, source="memory",
                path=f"memory/t/{cid}.md", start_line=1, end_line=1,
                text=f"记忆内容 {marker}", embedding=None,
                hash=mgr.storage.compute_hash(marker),
            ))
        mgr.storage.conn.commit()

        put("c-a-priv", "user-a", "user", "MKPRIVA")
        put("c-b-priv", "user-b", "user", "MKPRIVB")
        put("c-shared", None, "shared", "MKSHARED")
        # Mislabelled: claims shared but names an owner -> invisible to
        # others (fail-closed). The owner still sees it: the predicate's
        # intent is "the caller's own rows are always visible".
        put("c-mislabeled", "user-a", "shared", "MKMISLABEL")

        def see(user_id, marker):
            res = asyncio.run(mgr.search(marker, user_id=user_id,
                                         max_results=5, min_score=0.0))
            return any(marker in r.snippet for r in res)

        a_priv = see("user-a", "MKPRIVA")
        a_shared = see("user-a", "MKSHARED")
        b_priv = see("user-b", "MKPRIVB")
        b_shared = see("user-b", "MKSHARED")
        b_sees_a = see("user-b", "MKPRIVA")
        anon_shared = see(None, "MKSHARED")
        anon_priv = see(None, "MKPRIVA")
        a_mislabel = see("user-a", "MKMISLABEL")
        b_mislabel = see("user-b", "MKMISLABEL")
        anon_mislabel = see(None, "MKMISLABEL")

        detail += [
            f"A 见自己私有: {a_priv}（要 True）",
            f"A 见共享    : {a_shared}（要 True）",
            f"B 见自己私有: {b_priv}（要 True）",
            f"B 见共享    : {b_shared}（要 True）",
            f"B 见 A 私有 : {b_sees_a}（要 False，越权即 FAIL）",
            f"匿名见共享  : {anon_shared}（要 True）",
            f"匿名见私有  : {anon_priv}（要 False）",
            f"错标签行 属主A可见 : {a_mislabel}（要 True，自己的行恒可见）",
            f"错标签行 B 可见   : {b_mislabel}（要 False，fail-closed 的核心）",
            f"错标签行 匿名可见 : {anon_mislabel}（要 False，fail-closed 的核心）",
        ]
        ok = (a_priv and a_shared and b_priv and b_shared
              and not b_sees_a and anon_shared and not anon_priv
              and a_mislabel and not b_mislabel and not anon_mislabel)
        fp = temp_fingerprint(ws)
        mgr.storage.close()
        wait_maintenance(cfg.get_db_path())
    return report.judge("T1b(unit)", "A/B 私有记忆隔离 + 匿名只见无归属 shared + 错标签 fail-closed",
                        ok, "\n".join(detail), fingerprint=fp)


# ---------------------------------------------------------------------------
# T1-c 检索入口身份：环境身份 + status 报告
# ---------------------------------------------------------------------------
def judge_t1c_identity(report):
    detail = []
    try:
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager
        from agent.memory.storage import MemoryChunk
        from common.runtime_identity import use_identity, RuntimeIdentity
    except ImportError as e:
        return report.judge("T1c(unit)", "检索入口知道我是谁（环境身份）", False, f"导入失败: {e}")

    with tempfile.TemporaryDirectory() as ws:
        cfg = MemoryConfig(workspace_root=ws, sync_on_search=False)
        mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
        mgr.storage.save_chunk(MemoryChunk(
            id="c-ambient", user_id="user-env", scope="user", source="memory",
            path="memory/t/ambient.md", start_line=1, end_line=1,
            text="只有环境身份能看到的记忆 MKAMBIENT", embedding=None,
            hash=mgr.storage.compute_hash("MKAMBIENT"),
        ))
        mgr.storage.conn.commit()

        # No user_id, but the ambient identity is user-env -> must hit.
        with use_identity(RuntimeIdentity(agent_id="agent-x", user_id="user-env")):
            res = asyncio.run(mgr.search("MKAMBIENT", max_results=5, min_score=0.0))
            ambient_hit = any("MKAMBIENT" in r.snippet for r in res)
            st = mgr.get_status()
            ident_label = st.get("identity")
        detail.append(f"环境身份 user-env 检索命中私有: {ambient_hit}（要 True）")
        detail.append(f"status.identity（带身份）: {ident_label!r}")

        # No identity: invisible + status reports anonymous.
        res2 = asyncio.run(mgr.search("MKAMBIENT", max_results=5, min_score=0.0))
        anon_hit = any("MKAMBIENT" in r.snippet for r in res2)
        st2 = mgr.get_status()
        detail.append(f"无身份检索命中私有: {anon_hit}（要 False）")
        detail.append(f"status.identity（匿名）: {st2.get('identity')!r}（要 'anonymous'）")
        detail.append(f"status.knowledge_root: {st2.get('knowledge_root')!r}")

        ok = (ambient_hit and not anon_hit
              and ident_label and "user-env" in str(ident_label)
              and st2.get("identity") == "anonymous")
        fp = temp_fingerprint(ws)
        mgr.storage.close()
        wait_maintenance(cfg.get_db_path())
    return report.judge("T1c(unit)", "检索入口从运行时上下文取身份；无身份=anonymous 且只见 shared",
                        ok, "\n".join(detail), fingerprint=fp)


# ---------------------------------------------------------------------------
# T2 降级自曝
# ---------------------------------------------------------------------------
def judge_t2_degradation_status(report):
    detail = []
    try:
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager
    except ImportError as e:
        return report.judge("T2a(unit)", "降级自曝状态字段", False, f"导入失败: {e}")

    with tempfile.TemporaryDirectory() as ws:
        # One file, so chunks>0 (the degraded criterion requires it).
        Path(ws, "memory").mkdir(parents=True, exist_ok=True)
        Path(ws, "memory", "2026-01-01.md").write_text("# t\n\n内容内容内容\n", encoding="utf-8")
        cfg = MemoryConfig(workspace_root=ws, sync_on_search=False)
        mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
        asyncio.run(mgr.sync())
        st = mgr.get_status()
        for k in ("embedding_enabled", "search_mode", "degraded",
                  "degradation_reason", "identity", "rerank_status", "knowledge_root"):
            detail.append(f"status[{k}] = {st.get(k)!r}")
        ok = (st.get("embedding_enabled") is False
              and "keyword" in str(st.get("search_mode")).lower()
              and st.get("degraded") is True
              and bool(st.get("degradation_reason"))
              and st.get("rerank_status") == "off")
        fp = temp_fingerprint(ws)
        mgr.storage.close()
        wait_maintenance(cfg.get_db_path())
    return report.judge("T2a(unit)", "status 暴露降级字段（embedding/search_mode/degraded/identity/rerank/knowledge_root）",
                        ok, "\n".join(detail), fingerprint=fp)


def judge_t2b_reranker_down(report):
    """Point the reranker at a dead port: the search must not crash, must
    log a WARN, and status must say unreachable."""
    detail = []
    try:
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager
        from agent.memory.storage import MemoryChunk
        from agent.memory.reranker import HttpReranker
    except ImportError as e:
        return report.judge("T2b(unit)", "reranker 不可达不静默", False, f"导入失败: {e}")

    with tempfile.TemporaryDirectory() as ws:
        cfg = MemoryConfig(workspace_root=ws, sync_on_search=False)
        dead = HttpReranker("test-model", base_url="http://127.0.0.1:9", timeout=2)
        mgr = MemoryManager(cfg, embedding_provider=None, reranker=dead)
        mgr.storage.save_chunk(MemoryChunk(
            id="c-down", user_id=None, scope="shared", source="memory",
            path="memory/t/down.md", start_line=1, end_line=1,
            text="一些可供检索的内容 MKDOWNTEST", embedding=None,
            hash=mgr.storage.compute_hash("MKDOWNTEST"),
        ))
        mgr.storage.conn.commit()

        # Capture WARN logs. common.log's logger is named "log" and has
        # propagate=False -- attaching to root catches nothing.
        records = []

        class Cap(logging.Handler):
            def emit(self, r):
                records.append(r)

        cap = Cap(level=logging.WARNING)
        captured_logger = logging.getLogger("log")
        captured_logger.addHandler(cap)
        try:
            res = asyncio.run(mgr.search("MKDOWNTEST", max_results=5, min_score=0.0))
            crashed = False
        except Exception as e:
            res, crashed = [], e
        finally:
            captured_logger.removeHandler(cap)

        warns = [r for r in records if r.levelno >= logging.WARNING
                 and "rerank" in (r.getMessage().lower())]
        detail.append(f"检索是否崩溃: {crashed!r}（要 False）")
        detail.append(f"检索是否仍返回结果: {len(res)} 条（要 >0）")
        detail.append(f"捕获 rerank WARN 数: {len(warns)}（要 >=1）")
        for w in warns[:2]:
            detail.append(f"  WARN: {w.getMessage()[:100]}")
        st = mgr.get_status()
        detail.append(f"status.rerank_status: {st.get('rerank_status')!r}（要含 unreachable）")
        ok = (not crashed and len(res) > 0 and len(warns) >= 1
              and "unreachable" in str(st.get("rerank_status")))
        fp = temp_fingerprint(ws)
        mgr.storage.close()
        wait_maintenance(cfg.get_db_path())
    return report.judge("T2b(unit)", "reranker 不可达：不崩 + WARN 日志 + status=unreachable",
                        ok, "\n".join(detail), fingerprint=fp)


# ---------------------------------------------------------------------------
# T3 中文召回（真实工作区）
# ---------------------------------------------------------------------------
def judge_t3_chinese_recall(manager, report, fp, corpus_fp=None):
    """Retrieval quality against a REAL corpus.

    P0-18: the corpus is asserted to be NON-EMPTY first. A zero-hit result
    on an empty index is CORRECT behaviour, and reporting it as a
    retrieval failure is the wrong verdict for the right number.
    """
    detail, ok = [], True

    if fp is None or not fp.exists:
        detail.append("被测库不存在 —— 前置条件不成立，无法判定召回质量")
        return report.judge("T3(REAL)", "中文召回（真实工作区）",
                            False, "\n".join(detail), fingerprint=fp)
    # P0-21 F3: the emptiness verdict is about the corpus as the
    # workspace HANDED IT OVER, so it is taken from the pre-sync
    # fingerprint. With the default run (sync ON) the sync fills an empty
    # workspace, and a verdict taken afterwards would no longer be about
    # emptiness: it would drift into "the top hit is not USER.md", which
    # is exactly the reason this verdict exists.
    corpus = corpus_fp if corpus_fp is not None else fp
    if corpus.empty:
        detail.append("被测库是空的（0 行）—— 语料不存在，0 命中是正确行为")
        detail.append("这不是召回失败。请指向一个有语料的工作区，或先同步语料。")
        if fp.rows != corpus.rows:
            detail.append(f"（本次同步后被测库有 {fp.rows} 行，但同步前是 0 行 —— "
                          f"空库判定以同步前为准）")
        return report.judge("T3(REAL)", "中文召回（真实工作区）",
                            False, "\n".join(detail), fingerprint=fp)

    hits_total = 0
    for word in TWO_CHAR_WORDS:
        results = asyncio.run(manager.search(word, max_results=3, min_score=0.0))
        hits_total += bool(results)
        detail.append(f"{word}: {len(results)} hits"
                      + (f"  top1={results[0].path}" if results else ""))
        if not results:
            ok = False
    detail.append(f"2 字词命中合计: {hits_total}/8")

    wang = asyncio.run(manager.search("王静静是谁", max_results=5, min_score=0.0))
    wang_top1 = wang[0].path if wang else None
    detail.append(f"'王静静是谁': {len(wang)} hits  top1={wang_top1}")
    # Hits are not enough: the old behaviour treated the whole sentence as
    # one word, so a page that merely QUOTES the query matched. The top1
    # must be the USER.md that actually answers the question.
    if wang_top1 != "USER.md":
        ok = False
        detail.append("  top1 不是 USER.md —— 命中了但没有答到问题（旧 bug 形态）")

    for q in ("我上次生气是因为什么",):
        results = asyncio.run(manager.search(q, max_results=5, min_score=0.0))
        detail.append(f"{q!r}: {len(results)} hits"
                      + (f"  top1={results[0].path}" if results else ""))
        if not results:
            ok = False

    # English regression: an ASCII word that IS in the corpus must still hit.
    ascii_results = asyncio.run(manager.search("FTS5", max_results=3, min_score=0.0))
    detail.append(f"'FTS5' (ASCII 回归): {len(ascii_results)} hits")
    if not ascii_results:
        ok = False
    return report.judge("T3(REAL)", "中文召回：8/8 双字词 + 自然语言查询命中 + ASCII 回归不破",
                        ok, "\n".join(detail), fingerprint=fp)


# ---------------------------------------------------------------------------
# T4 user_id 命名空间
# ---------------------------------------------------------------------------
def judge_t4_namespace(report):
    detail, ok = [], True
    try:
        from agent.memory.identity_ns import (
            UnsafeIdentifierError, agent_dir_name, is_fs_safe,
            parse_user_dir_name, user_dir_name, validate_fs_identifier,
        )
    except ImportError as e:
        return report.judge("T4(unit)", "user_id 命名空间", False,
                            f"identity_ns 不存在: {e}（修复前即此 FAIL）")

    with tempfile.TemporaryDirectory() as ws:
        # 1. The new prefixed directory can be created on Windows.
        d_user = user_dir_name("alice", workspace=ws)
        d_agent = agent_dir_name("agent-mvnewfyb", workspace=ws)
        detail.append(f"user_dir_name('alice') = {d_user!r}")
        detail.append(f"agent_dir_name('agent-mvnewfyb') = {d_agent!r}")
        if not d_user.startswith("user--") or not d_agent.startswith("agent--"):
            ok = False
            detail.append("  缺少 user--/agent-- 前缀")
        try:
            os.makedirs(Path(ws) / "memory" / "users" / d_user, exist_ok=False)
            os.makedirs(Path(ws) / "agents" / d_agent, exist_ok=False)
            detail.append("os.makedirs 含新前缀目录: 成功")
        except Exception as e:
            ok = False
            detail.append(f"os.makedirs 失败: {e}")

        # 2. The round-trip is character-for-character equal (safe id AND
        #    an id with a colon).
        for logical in ("alice", "u:a", "王静静"):
            d = user_dir_name(logical, workspace=ws)
            back = parse_user_dir_name(d, workspace=ws)
            same = (back == logical)
            detail.append(f"round-trip {logical!r} -> {d!r} -> {back!r}  逐字符相等: {same}")
            if not same:
                ok = False

        # 3. The whitelist refuses (no silence).
        for bad in ("a:b", "..", "x/y", "", ".hidden"):
            try:
                validate_fs_identifier(bad)
                ok = False
                detail.append(f"validate({bad!r}) 未拒绝 —— FAIL")
            except UnsafeIdentifierError:
                detail.append(f"validate({bad!r}) -> UnsafeIdentifierError ✓")

        # 4. The users.json mapping exists.
        reg = Path(ws) / "memory" / "long-term" / "users.json"
        detail.append(f"users.json 存在: {reg.exists()}")
        if not reg.exists():
            ok = False
        else:
            import json
            data = json.loads(reg.read_text(encoding="utf-8"))
            detail.append(f"users.json users 表: {data.get('users')}")

        # 5. Legacy directory names (no prefix) still work.
        legacy = parse_user_dir_name("alice", workspace=ws)
        detail.append(f"legacy 'alice' -> {legacy!r}（要 'alice'）")
        if legacy != "alice":
            ok = False

        # 6. e2e: the path add_memory writes carries no colon, and sync
        #    resolves it back to the logical id.
        try:
            from agent.memory.config import MemoryConfig
            from agent.memory.manager import MemoryManager
            cfg = MemoryConfig(workspace_root=ws, sync_on_search=False)
            mgr = MemoryManager(cfg, embedding_provider=None, reranker=None)
            asyncio.run(mgr.add_memory("私有记忆内容 MKNSE2E", user_id="u:a", scope="user"))
            row = mgr.storage.conn.execute(
                "SELECT path, user_id FROM chunks WHERE user_id IS NOT NULL LIMIT 5"
            ).fetchall()
            for pth, uid in row:
                detail.append(f"DB: path={pth!r} user_id={uid!r}")
                if ":" in pth.replace("memory/", "", 1):
                    ok = False
                    detail.append("  路径里仍有冒号 —— FAIL")
            asyncio.run(mgr.sync())
            fp = temp_fingerprint(ws)
            mgr.storage.close()
            wait_maintenance(cfg.get_db_path())
        except Exception as e:
            ok = False
            detail.append(f"e2e add_memory 异常: {type(e).__name__}: {e}")
            fp = temp_fingerprint(ws)
    return report.judge("T4(unit)", "user_id 命名空间：新前缀可建目录 / 反解逐字符相等 / 白名单报错 / users.json 映射",
                        ok, "\n".join(detail), fingerprint=fp)


# ---------------------------------------------------------------------------
# T6 reranker 端到端
# ---------------------------------------------------------------------------
def judge_t6_reranker_e2e(manager, report, fp):
    detail = []
    try:
        from agent.memory.reranker import create_reranker
        reranker = create_reranker("http")
    except Exception as e:
        return report.judge("T6(REAL)", "reranker 端到端", False, f"创建失败: {e}",
                            fingerprint=fp)
    if reranker is None:
        return report.judge("T6(REAL)", "reranker 端到端", False,
                            "config 未启用 rerank_provider=http", fingerprint=fp)

    # P0-18: an empty corpus cannot be re-ranked. Say so instead of
    # reporting "the ordering did not change".
    if fp is None or not fp.exists or fp.empty:
        detail.append("被测库不存在或是空的 —— 无候选可重排，前置条件不成立")
        return report.judge("T6(REAL)", "reranker 端到端", False,
                            "\n".join(detail), fingerprint=fp)

    query = "记忆 检索 召回"
    off = asyncio.run(manager.search(query, max_results=5, min_score=0.0))
    manager.reranker = reranker
    try:
        on = asyncio.run(manager.search(query, max_results=5, min_score=0.0))
    finally:
        manager.reranker = None
    off_paths = [r.path for r in off]
    on_paths = [r.path for r in on]
    detail.append(f"OFF: {off_paths}")
    detail.append(f"     scores={[round(r.score, 4) for r in off]}")
    detail.append(f"ON : {on_paths}")
    detail.append(f"     scores={[round(r.score, 4) for r in on]}")
    same_count = len(off_paths) == len(on_paths)
    order_changed = off_paths != on_paths
    detail.append(f"候选数量一致: {same_count}；顺序不同: {order_changed}")
    manager.reranker = reranker
    st = manager.get_status().get("rerank_status")
    manager.reranker = None
    detail.append(f"status.rerank_status（服务在跑）: {st!r}")
    ok = same_count and order_changed and "reachable" in str(st)
    return report.judge("T6(REAL)", "reranker 端到端：ON/OFF 排序确实不同（数量一致），status=reachable",
                        ok, "\n".join(detail), fingerprint=fp)


# ---------------------------------------------------------------------------
# S-1 判据不得修改它要验收的对象
# ---------------------------------------------------------------------------
def judge_s1_index_untouched(report, workspace, fp_before):
    """Did the acceptance run modify the index it measured?

    P0-20 S-1 (风控): "会修改它要验收的对象——phase1.py 判 T3 前 sync，
    仓库根库 102→106 行。"  A criterion that changes the thing it measures
    is measuring a state it manufactured itself.

    The comparison is on the DIGEST, not the row count: a search rewrites
    ``hit_count`` on the rows it matched, which changes the bytes and
    leaves the row count alone. The digest is what sees that.

    What is NOT a violation: the explicit ``sync`` step. Syncing the
    corpus before measuring it is the point of an integration criterion —
    it is the corpus being brought up to date, not the measurement
    mutating it. So the fingerprint compared here is the one taken AFTER
    the sync and BEFORE the real-corpus criteria, and only what the
    criteria themselves did is on trial.

    A violation is reported even when every other criterion is green —
    that is the whole point: a green run that moved the index cannot
    read as a clean pass.
    """
    fp_after = fingerprint(workspace)
    detail = []
    detail.append(f"判据执行前: {fp_before.render()}")
    detail.append(f"判据执行后: {fp_after.render()}")
    changed = fp_after.differs_from(fp_before)
    if not changed:
        detail.append("被测库字节级未变 —— 判据没有修改它要验收的对象")
        return report.judge("S1(unit)", "判据未修改被测库（前后指纹一致）",
                            True, "\n".join(detail), fingerprint=fp_after)
    detail.append(f"被测库发生变化: {', '.join(changed)}")
    detail.append("  —— 判据修改了它要验收的对象；即使其它判据全绿，本次结果不可作为"
                  "『系统行为正确』的证据")
    # Say WHICH field moved and what it means, so the reader does not have
    # to guess. rows moving is a corpus change (the sync re-indexed);
    # sha256 moving alone is a bookkeeping write (hit_count).
    if "rows" in changed:
        detail.append("  rows 变化 = 语料行数变了（同步重建了索引）")
    elif "sha256" in changed:
        detail.append("  仅 sha256 变化 = 行数未变但字节变了（典型的 hit_count 回写）")
    return report.judge("S1(unit)", "判据未修改被测库（前后指纹一致）",
                        False, "\n".join(detail), fingerprint=fp_after)


def main() -> int:
    workspace, how = resolve_workspace(ARGS.workspace)

    # P0-20 L-1: the source tree is not a workspace. Refuse BEFORE anything
    # touches an index — a refusal that happens after the sync has already
    # written the runtime state is a refusal that came too late.
    try:
        assert_not_source_tree(workspace, REPO)
    except SourceTreeWorkspaceError as e:
        print("=" * 72)
        print("阶段一验收 · 拒绝执行")
        print("=" * 72)
        print(str(e))
        return 2

    fp = fingerprint(workspace)

    print("=" * 72)
    print("记忆体系升级 · 阶段一验收（T1/T2/T3/T4/T6）")
    print("=" * 72)
    print(f"code root : {CODE_ROOT}{'  ← baseline（修复前）' if ARGS.baseline else ''}")
    print(f"workspace : {workspace}  （{how}）")
    print(f"被测库    : {fp.render()}")
    if ARGS.baseline or ARGS.code_root:
        print("注意：本次跑的是基线代码，判据按『修复前』口径计数。")
    print()

    report = Report()

    def guarded(fn, *args, **kwargs):
        """A criterion that raises IS a failure — on the baseline a missing
        function or a missing parameter is exactly the evidence of
        "fails before the fix"."""
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            import traceback
            return report.judge(
                getattr(fn, '__name__', '?'), "判据执行异常（按 FAIL 计）", False,
                f"{type(e).__name__}: {e}\n" + "\n".join(
                    traceback.format_exc().splitlines()[-4:]))

    guarded(judge_t1a_single_predicate, report)
    guarded(judge_t1b_ab_visibility, report)
    guarded(judge_t1c_identity, report)
    guarded(judge_t2_degradation_status, report)
    guarded(judge_t2b_reranker_down, report)
    guarded(judge_t4_namespace, report)

    # T3/T6 need a real corpus and a real reranker service.
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    # P0-21 F1/F3: the pre-sync fingerprint is taken BEFORE the manager is
    # built, because the manager's construction can fail and the failure
    # path needs a fingerprint to print. (It used to reference ``fp``,
    # which is only assigned further down — a NameError on the error path
    # is a bad way to report a different error.)
    fp_pre_sync = fingerprint(workspace)
    try:
        cfg = MemoryConfig(workspace_root=workspace, sync_on_search=False)
        manager = MemoryManager(cfg, embedding_provider=None, reranker=None)
    except Exception as e:
        report.judge("T3(REAL)", "中文召回", False,
                     f"管理器构造失败: {type(e).__name__}: {e}",
                     fingerprint=fp_pre_sync)
        report.judge("T6(REAL)", "reranker 端到端", False, "管理器构造失败，跳过",
                     fingerprint=fp_pre_sync)
        report.summary()
        return 1

    # P0-21 F1: the baseline is taken AFTER the sync, which is where the
    # code's own docstring said it was taken. It was taken BEFORE, so the
    # comparison blamed the sync (the corpus being brought up to date)
    # for a change the criteria did not make — the code and the document
    # said different things about the same baseline.
    #
    # It is also taken BEFORE the sync for a second reason (F3): whether
    # the corpus EXISTS is a fact about the workspace as it was handed
    # over, and the sync is what fills an empty one. An emptiness verdict
    # taken after the sync cannot be about emptiness.
    try:
        if not ARGS.no_sync:
            print("[sync] 同步真实工作区索引 …")
            t0 = time.time()
            asyncio.run(manager.sync())
            print(f"[sync] 用时 {time.time() - t0:.1f}s")
            # The sync may have changed the corpus, so the fingerprint is
            # re-read: the criteria report the index they MEASURED, not
            # the one they started from.
            fp = fingerprint(workspace)
            print(f"[sync] 同步后被测库: {fp.render()}\n")
        else:
            fp = fp_pre_sync
        # P0-21 F1: the S-1 baseline is the index as the criteria will
        # MEASURE it — that is, after the sync. Taken before, the
        # comparison blames the sync for a change the criteria did not
        # make, and the code's own docstring already said the baseline
        # was taken after the sync.
        fp_before = fingerprint(workspace)
        # P0-21 F3: the emptiness verdict comes from the PRE-sync
        # fingerprint. With the default run (sync ON) an empty workspace
        # is filled by the sync, so a verdict taken afterwards would no
        # longer be about emptiness — it would drift into "the top hit is
        # not USER.md", which is the reason this verdict exists to
        # eliminate.
        guarded(judge_t3_chinese_recall, manager, report, fp,
                corpus_fp=fp_pre_sync)
        guarded(judge_t6_reranker_e2e, manager, report, fp)
    finally:
        manager.storage.close()

    # S-1: compare the index AFTER the criteria with the one BEFORE them.
    # This is reported as a criterion of its own so a green run that
    # modified the object it measured cannot read as a clean 8/8.
    guarded(judge_s1_index_untouched, report, workspace, fp_before)

    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
