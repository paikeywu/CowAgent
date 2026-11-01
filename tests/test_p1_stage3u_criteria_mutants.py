#!/usr/bin/env python3
# encoding:utf-8
"""P1 阶段三 U · the S3-U criterion's MUTANT REPLAY + the Q5 baseline's
own guards.

MU-39   U-1's "the migration's claim vs the actual" check is removed ⇒
        U-1 must go RED on a PARTIAL migration (the migration reports
        success while only a fraction of the rows moved).
MU-40   U-1's "did the text actually leave the table" check is removed ⇒
        U-1 must go RED on a migration that emptied nothing.

The three-state discipline: "no pre-migration library to measure" must
be NOTRUN, never PASS — a fleet with no old-shaped library has NOT
measured whether the migration works.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from agent.memory.acceptance import Report, STATE_FAIL, STATE_NOTRUN, STATE_PASS  # noqa: E402
from agent.memory.acceptance import phase2_s3u  # noqa: E402


def _states(report: Report):
    return {number: state for number, _name, state in report.rows}


def _run(fn, *args):
    import contextlib
    import io

    report = Report()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(report, *args)
    report.printed = buf.getvalue()
    return report, _states(report)


def _pre_migration_library(tmp_path: Path, rows: int = 6,
                            agents=("", "")) -> Path:
    """A library in the PRE-migration shape.

    The shape is the one the criterion looks for MECHANICALLY: the
    ``messages`` table has NO ``content_state`` column and its
    ``content`` is ``NOT NULL``.
    """
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "agent_id TEXT NOT NULL DEFAULT '', "
            "session_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "role TEXT NOT NULL, content TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, "
            "extras TEXT NOT NULL DEFAULT '', "
            "run_id TEXT NOT NULL DEFAULT '', "
            "UNIQUE (agent_id, session_id, seq))")
        for i in range(rows):
            conn.execute(
                "INSERT INTO messages (agent_id, session_id, seq, role, "
                "content, created_at) VALUES (?,?,?,?,?,?)",
                (agents[i % len(agents)], "s1", i, "user",
                 "正文 %d" % i, 1700000000))
        conn.commit()
    finally:
        conn.close()
    return db


# ---------------------------------------------------------------------------
# MU-39 · the "claim vs actual" check is removed
# ---------------------------------------------------------------------------

def test_mu39_a_partial_migration_is_caught(tmp_path):
    """MU-39: 迁移只迁了一部分却报成功 ⇒ U-1 必须报红。

    The mutation is on the PRODUCT: the migration processes only the
    DEFAULT agent's rows (the ``externalise_all(conn, ws, "")`` scope)
    instead of every agent's.  That is a real, measured failure mode
    (measured 2026-11-01: 7,203 of 31,191 rows moved, and the tool
    reported success).

    The criterion must catch it: the rows that did NOT move are still
    holding their text in the table.
    """
    from agent.memory import msg_migrate

    db = _pre_migration_library(tmp_path, rows=6, agents=("", "agent-x"))
    old = os.environ.get(phase2_s3u.PRE_MIGRATION_ENV)
    os.environ[phase2_s3u.PRE_MIGRATION_ENV] = str(db)
    try:
        # The mutation: only the default agent is migrated.
        original = msg_migrate.externalise_every_agent

        def only_default(conn, workspace, **kw):
            return msg_migrate.externalise_all(
                conn, workspace, "", batch_date=kw.get("batch_date"))

        msg_migrate.externalise_every_agent = only_default
        try:
            report, states = _run(
                phase2_s3u.judge_u1_existing_library_can_be_migrated,
                tmp_path)
        finally:
            msg_migrate.externalise_every_agent = original

        assert states["U-1(REAL)"] == STATE_FAIL, (
            "部分迁移（自报成功但仍有行留在表里）必须报红 —— "
            "『迁完了吗』不能只看工具自己的自报")
        assert "没迁" in report.printed or "部分迁移" in report.printed, \
            report.printed
    finally:
        if old is None:
            os.environ.pop(phase2_s3u.PRE_MIGRATION_ENV, None)
        else:
            os.environ[phase2_s3u.PRE_MIGRATION_ENV] = old


def test_mu39b_a_complete_migration_passes(tmp_path):
    """U-1's GREEN case: the full migration really moves everything.

    Without this half, MU-39 could be satisfied by a criterion that is
    simply always red.
    """
    db = _pre_migration_library(tmp_path, rows=6, agents=("", "agent-x"))
    old = os.environ.get(phase2_s3u.PRE_MIGRATION_ENV)
    os.environ[phase2_s3u.PRE_MIGRATION_ENV] = str(db)
    try:
        report, states = _run(
            phase2_s3u.judge_u1_existing_library_can_be_migrated, tmp_path)
        assert states["U-1(REAL)"] == STATE_PASS, report.printed
        assert "PASS" in report.printed
    finally:
        if old is None:
            os.environ.pop(phase2_s3u.PRE_MIGRATION_ENV, None)
        else:
            os.environ[phase2_s3u.PRE_MIGRATION_ENV] = old


def test_mu40_the_truth_tree_check_is_not_optional(tmp_path):
    """MU-40: 正文离表却没落到真源树 ⇒ U-1 必须报红。

    **This is the "丢数据" case, and it is the one that must never be
    green.**  The rows say ``external`` and their text is gone from the
    database — but there is nothing to read back.  A criterion that
    only checked the state column would call that a success.
    """
    db = _pre_migration_library(tmp_path, rows=4)
    old = os.environ.get(phase2_s3u.PRE_MIGRATION_ENV)
    os.environ[phase2_s3u.PRE_MIGRATION_ENV] = str(db)
    try:
        # The mutation: the migration empties the rows and marks them
        # external, but writes nothing to the truth tree.
        from agent.memory import msg_migrate

        original = msg_migrate.externalise_every_agent

        def no_truth_tree(conn, workspace, **kw):
            rep = original(conn, workspace, **kw)
            # Delete every JSONL the migration wrote.
            root = Path(workspace) / "memory" / "messages_export"
            if root.is_dir():
                for p in list(root.rglob("*.jsonl")):
                    try:
                        p.unlink()
                    except OSError:
                        pass
            return rep

        msg_migrate.externalise_every_agent = no_truth_tree
        try:
            report, states = _run(
                phase2_s3u.judge_u1_existing_library_can_be_migrated,
                tmp_path)
        finally:
            msg_migrate.externalise_every_agent = original

        assert states["U-1(REAL)"] == STATE_FAIL, (
            "正文离表但真源树是空的 ⇒ 必须报红（这是丢数据，不是迁移）")
        assert "丢数据" in report.printed
    finally:
        if old is None:
            os.environ.pop(phase2_s3u.PRE_MIGRATION_ENV, None)
        else:
            os.environ[phase2_s3u.PRE_MIGRATION_ENV] = old


# ---------------------------------------------------------------------------
# The three-state discipline
# ---------------------------------------------------------------------------

def test_no_pre_migration_library_is_notrun(tmp_path):
    """没有既有库 ⇒ NOTRUN（0 个对象 ⇒ 测不了，不是『迁得动』）。"""
    ws = tmp_path / "ws"
    (ws / "tmp").mkdir(parents=True)
    report, states = _run(
        phase2_s3u.judge_u1_existing_library_can_be_migrated, ws)
    assert states["U-1(REAL)"] == STATE_NOTRUN, report.printed


def test_a_new_shape_library_is_not_pre_migration(tmp_path):
    """A library that is ALREADY in the new shape is not a pre-migration
    library — the criterion must say so instead of measuring it."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, "
            "agent_id TEXT, session_id TEXT, seq INTEGER, role TEXT, "
            "content TEXT, content_path TEXT, content_anchor TEXT, "
            "content_hash TEXT, content_state TEXT)")
        conn.commit()
    finally:
        conn.close()
    old = os.environ.get(phase2_s3u.PRE_MIGRATION_ENV)
    os.environ[phase2_s3u.PRE_MIGRATION_ENV] = str(db)
    try:
        report, states = _run(
            phase2_s3u.judge_u1_existing_library_can_be_migrated, tmp_path)
        assert states["U-1(REAL)"] == STATE_NOTRUN, report.printed
        assert "not in the pre-migration shape" in report.printed
    finally:
        if old is None:
            os.environ.pop(phase2_s3u.PRE_MIGRATION_ENV, None)
        else:
            os.environ[phase2_s3u.PRE_MIGRATION_ENV] = old


def test_the_module_declares_its_criteria():
    ids = [cid for cid, _fn in phase2_s3u.CRITERIA]
    assert ids == ["U-1"], ids


def test_the_module_has_no_machine_specific_paths():
    src = (REPO / "agent" / "memory" / "acceptance" / "phase2_s3u.py") \
        .read_text(encoding="utf-8")
    for needle in ("C:\\Users", "C:/Users", "\\\\Users\\"):
        assert needle not in src, "判据里出现了本机绝对路径: %r" % needle
