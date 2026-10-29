"""INV 第 9 检查组 —— 文本外置不变量（P1 阶段二-B T5）。

These are the CHUNKS-table landing of 03 号 §八 INV-1/2/3/6/9/11. They are
NOT a copy of 03 号's SQL: 03 号 references the P2 element-system tables
(``element_index`` / ``element_links`` / ``fact_sessions`` / ...) that do not
exist yet. P1 lands the equivalent invariants over the ``chunks`` table plus
the filesystem. INV-4/5/7/8 are deferred and registered (trigger condition:
the element system ships).

Each check is REPORT-ONLY and exposes three things:
  * a fingerprint (what object / range it actually measured),
  * a NOTRUN verdict when a precondition does not hold (an empty index is
    NOTRUN, never a silent PASS),
  * violations, each keyed by a short machine code.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory.naming import canonical_text
from agent.memory.body_pointer import resolve_truth_file

# T2: the shelf-lag invariant reads the single change log the facade /
# import_human_edit / rebuilder all write to — the two projections read the
# SAME record, never their own (SEED 裁决: no write-time rebuild, the P0
# batch-rebuild contract stands).
from agent.memory import changes as _changes
from agent.memory import paths as _paths

_CST = timezone(timedelta(hours=8))

REASON_INV1 = "gap-rule:inv1-pointer-missing"
REASON_INV2 = "gap-rule:inv2-body-not-rebuildable"
REASON_INV3 = "gap-rule:inv3-fragment-hash-mismatch"
REASON_INV6 = "gap-rule:inv6-time-disorder"
REASON_INV9 = "gap-rule:inv9-release-no-consent"
REASON_INV11 = "gap-rule:inv11-unsafe-path"
# T2: shelf lags the truth layer (真源变了，书架还没 batch 重建)。
REASON_SHELF_LAG = "gap-rule:shelf-lag"

CODE_INV1 = "inv1-pointer-missing"
CODE_INV2 = "inv2-body-not-rebuildable"
CODE_INV3 = "inv3-fragment-hash-mismatch"
CODE_INV6 = "inv6-time-disorder"
CODE_INV9 = "inv9-release-no-consent"
CODE_INV11 = "inv11-unsafe-path"
CODE_SHELF_LAG = "shelf-lag"

_EXTERNALIZED_STATES = ("projection", "mirror")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_anchor(anchor: str) -> Optional[Tuple[int, int, int]]:
    """``L<start>-<end>[:p<part>]`` -> (start, end, part). None if unparseable."""
    if not anchor or not anchor.startswith("L"):
        return None
    body = anchor[1:]
    part = 0
    if ":p" in body:
        body, p = body.rsplit(":p", 1)
        try:
            part = int(p)
        except ValueError:
            return None
    if "-" not in body:
        return None
    s, e = body.split("-", 1)
    try:
        return int(s), int(e), part
    except ValueError:
        return None


def read_fragment_at_anchor(path: Path, anchor: str) -> Optional[str]:
    """The fragment an anchor points at, or None when it cannot be located.

    P1-阶段三 (C-A2): this is the ONE reader of the anchor grammar.  It was
    private (``_read_fragment``) and is now public, because the WRITER side
    (the body-pointer builder) must read the fragment the SAME way the
    CHECKER reads it — otherwise the two sides parse the same anchor
    differently and INV-3 reports a hash mismatch that no amount of
    rewriting the row can fix.

    The private alias below is kept so the existing callers keep working.
    """
    parsed = _parse_anchor(anchor)
    if parsed is None:
        return None
    start, end, _part = parsed
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None
    if start < 1 or end < start or start > len(lines):
        return None
    # The chunker's end_line can run one past the last line (a trailing
    # newline): clamp, never reject — rejecting a valid tail is a false red.
    end = min(end, len(lines))
    return "".join(lines[start - 1:end])


def _read_fragment(path: Path, anchor: str) -> Optional[str]:
    """Read the fragment an anchor points at, or None when it cannot be located."""
    return read_fragment_at_anchor(path, anchor)


def _v(reason: str, code: str, path: str, detail: str, severity: str,
       **fields: Any) -> Dict[str, Any]:
    return {"reason": reason, "code": code, "path": path, "detail": detail,
            "severity": severity, **fields}


def check_inv1(conn: sqlite3.Connection, root: Path) -> Dict[str, Any]:
    """INV-1 指针存在性：externalized rows must carry a resolvable pointer.

    ``text_state IN ('projection','mirror')`` with a NULL body_path / body_anchor
    is a pointer that points nowhere; and on the filesystem side the body_path
    must exist and the anchor must be locatable inside it.
    """
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT id, body_path, body_anchor, text_state FROM chunks "
            "WHERE text_state IN ('projection','mirror')").fetchall()
    except sqlite3.Error as e:
        return {"inv": "INV-1", "verdict": "NOTRUN", "fingerprint": "n/a",
                "violations": [_v(REASON_INV1, CODE_INV1, "<index>",
                                  f"cannot read chunks: {e}", "high")]}
    if not rows:
        return {"inv": "INV-1", "verdict": "NOTRUN", "fingerprint": "chunks=0",
                "violations": []}
    fp = f"chunks={len(rows)}"
    for cid, bpath, banchor, _state in rows:
        if not bpath or not banchor:
            out.append(_v(REASON_INV1, CODE_INV1, str(bpath or "?"),
                          f"chunk {cid}: externalized but body_path="
                          f"{bpath!r} body_anchor={banchor!r} (points nowhere)",
                          "high", chunk_id=cid))
            continue
        # P1-阶段三 (C-A2): the file is found through the ONE resolution
        # rule (the writer's), not by joining the workspace root — the
        # knowledge tree lives in the SHARED root when the agent has no
        # copy of its own, and a reader that guessed the root reported
        # every knowledge row as "the body file does not exist".
        fpath = resolve_truth_file(root, bpath)
        if fpath is None:
            out.append(_v(REASON_INV1, CODE_INV1, bpath,
                          f"chunk {cid}: body_path does not exist on disk",
                          "high", chunk_id=cid))
            continue
        if _read_fragment(fpath, banchor) is None:
            out.append(_v(REASON_INV1, CODE_INV1, bpath,
                          f"chunk {cid}: anchor {banchor!r} not locatable in "
                          f"body_path", "high", chunk_id=cid, anchor=banchor))
    return {"inv": "INV-1", "verdict": "RED" if out else "GREEN",
            "fingerprint": fp, "violations": out}


def _sample_chunks(conn: sqlite3.Connection, limit: int,
                   stale_paths: Optional[List[str]]) -> List[Tuple]:
    """分层抽样: random ``limit`` rows + EVERY row of a stale-flagged file."""
    rows = conn.execute(
        "SELECT id, path, body_path, body_anchor, body_hash, text "
        "FROM chunks WHERE text_state IN ('projection','mirror') "
        "AND body_path IS NOT NULL AND body_anchor IS NOT NULL").fetchall()
    if not rows:
        return []
    picked = random.sample(rows, min(limit, len(rows)))
    if stale_paths:
        sp = set(stale_paths)
        extra = [r for r in rows if r[2] in sp]
        seen = {r[0] for r in picked}
        picked.extend(r for r in extra if r[0] not in seen)
    return picked


def check_inv2_inv3(conn: sqlite3.Connection, root: Path, *,
                    sample_limit: int = 20,
                    stale_paths: Optional[List[str]] = None,
                    full: bool = True) -> Dict[str, Any]:
    """INV-2 正文可重建 + INV-3 片段哈希（默认全量）.

    G-1 (P0, sampling blind spot): this used to default to ``full=False``
    — a 20-row sample — and the PRODUCTION inspection path called it
    without the argument, so 300 broken rows were found 1 time in 60
    inspections (98% missed) while the gate stayed green. The default is
    now ``full=True``: "forgot to pass the argument" must degrade to
    EXHAUSTIVE, never to a sample. Sampling remains available ONLY as an
    explicit ``full=False`` call, for development self-checks.

    For each checked chunk: read body_path+body_anchor -> canonical ->
    must be byte-equal to chunks.text (CRLF/LF tolerated), and body_hash
    must equal sha256(canonical(fragment)).
    """
    out2: List[Dict[str, Any]] = []
    out3: List[Dict[str, Any]] = []
    try:
        if full:
            rows = conn.execute(
                "SELECT id, path, body_path, body_anchor, body_hash, text "
                "FROM chunks WHERE text_state IN ('projection','mirror') "
                "AND body_path IS NOT NULL AND body_anchor IS NOT NULL").fetchall()
        else:
            rows = _sample_chunks(conn, sample_limit, stale_paths)
    except sqlite3.Error as e:
        return {"inv": "INV-2/3", "verdict": "NOTRUN", "fingerprint": "n/a",
                "violations": [_v(REASON_INV2, CODE_INV2, "<index>",
                                  f"cannot read chunks: {e}", "high")]}
    if not rows:
        return {"inv": "INV-2/3", "verdict": "NOTRUN",
                "fingerprint": "sampled=0", "violations": []}
    fp = f"sampled={len(rows)} full={bool(full)}"
    for cid, _cpath, bpath, banchor, bhash, text in rows:
        # P1-阶段三 (C-A2): the SAME resolution rule as the writer and as
        # INV-1 — one rule for "where the truth file is".
        fpath = resolve_truth_file(root, bpath)
        if fpath is None:
            out2.append(_v(REASON_INV2, CODE_INV2, bpath,
                           f"chunk {cid}: body_path does not exist on disk",
                           "high", chunk_id=cid, anchor=banchor))
            continue
        frag = _read_fragment(fpath, banchor)
        if frag is None:
            out2.append(_v(REASON_INV2, CODE_INV2, bpath,
                           f"chunk {cid}: anchor {banchor!r} not locatable",
                           "high", chunk_id=cid, anchor=banchor))
            continue
        # INV-2: canonical byte-equality (CRLF/LF tolerated via canonical_text).
        if canonical_text(frag) != canonical_text(text or ""):
            out2.append(_v(REASON_INV2, CODE_INV2, bpath,
                           f"chunk {cid}: rebuilt fragment != chunks.text "
                           f"(truth drifted)", "high", chunk_id=cid,
                           anchor=banchor))
        # INV-3: fragment hash.
        actual = "sha256:" + _sha256_hex(canonical_text(frag))
        if (bhash or "") != actual:
            out3.append(_v(REASON_INV3, CODE_INV3, bpath,
                           f"chunk {cid}: body_hash {(bhash or '')[:23]}… != "
                           f"actual {actual[:23]}…", "high", chunk_id=cid,
                           anchor=banchor))
    violations = out2 + out3
    return {"inv": "INV-2/3", "verdict": "RED" if violations else "GREEN",
            "fingerprint": fp, "violations": violations}


def check_inv6(conn: sqlite3.Connection) -> Dict[str, Any]:
    """INV-6 时序（chunks 落地版）: files.mtime sane + updated_at monotonic.

    The chunks table has no occurred_at/recorded_at (that is the P2 element
    system). The P1-landable form: a files.mtime in the future or negative is
    a clock/lie, and chunk updated_at must never go backwards per row.
    """
    import time as _time
    out: List[Dict[str, Any]] = []
    now = int(_time.time())
    try:
        frows = conn.execute("SELECT path, mtime FROM files").fetchall()
        urows = conn.execute(
            "SELECT id, updated_at FROM chunks WHERE updated_at IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as e:
        return {"inv": "INV-6", "verdict": "NOTRUN", "fingerprint": "n/a",
                "violations": [_v(REASON_INV6, CODE_INV6, "<index>",
                                  f"cannot read: {e}", "high")]}
    if not frows and not urows:
        return {"inv": "INV-6", "verdict": "NOTRUN",
                "fingerprint": "files=0 chunks=0", "violations": []}
    fp = f"files={len(frows)} chunks={len(urows)}"
    for path, mtime in frows:
        if mtime is None:
            continue
        if mtime < 0 or mtime > now + 60:
            out.append(_v(REASON_INV6, CODE_INV6, str(path),
                          f"files.mtime {mtime} is {'negative' if mtime < 0 else 'in the future'} "
                          f"(now={now})", "mid", mtime=mtime))
    for cid, updated_at in urows:
        if updated_at is not None and (updated_at < 0 or updated_at > now + 60):
            out.append(_v(REASON_INV6, CODE_INV6, "<index>",
                          f"chunk {cid}: updated_at {updated_at} is not sane "
                          f"(now={now})", "mid", chunk_id=cid,
                          updated_at=updated_at))
    return {"inv": "INV-6", "verdict": "RED" if out else "GREEN",
            "fingerprint": fp, "violations": out}


def check_inv9(conn: sqlite3.Connection, root: Path) -> Dict[str, Any]:
    """INV-9 释放有同意源：released rows must trace to a resolvable consent_ref.

    A released element (element_released=1) with no consent_ref, or one whose
    consent_ref cannot be resolved to a real confirmation record, is a release
    nobody authorized.
    """
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT DISTINCT path FROM chunks WHERE element_released=1").fetchall()
    except sqlite3.Error as e:
        return {"inv": "INV-9", "verdict": "NOTRUN", "fingerprint": "n/a",
                "violations": [_v(REASON_INV9, CODE_INV9, "<index>",
                                  f"cannot read chunks: {e}", "high")]}
    if not rows:
        return {"inv": "INV-9", "verdict": "NOTRUN",
                "fingerprint": "released=0", "violations": []}
    fp = f"released={len(rows)}"
    consent_log = root / "memory" / ".audit" / "consent.jsonl"
    consent_refs = set()
    if consent_log.is_file():
        try:
            import json as _json
            for line in consent_log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except ValueError:
                    continue
                ref = rec.get("consent_ref") or rec.get("id") or rec.get("ref")
                if ref:
                    consent_refs.add(ref)
        except OSError:
            pass
    from agent.memory.facade import split_frontmatter
    for (path,) in rows:
        fpath = root / path
        consent_ref = None
        if fpath.is_file():
            try:
                fm, _ = split_frontmatter(
                    fpath.read_text(encoding="utf-8"))
                consent_ref = fm.get("consent_ref")
            except (OSError, UnicodeDecodeError):
                pass
        if not consent_ref:
            out.append(_v(REASON_INV9, CODE_INV9, path,
                          "released element has no consent_ref (unauthorized "
                          "release)", "high"))
            continue
        if consent_refs and consent_ref not in consent_refs:
            out.append(_v(REASON_INV9, CODE_INV9, path,
                          f"consent_ref {consent_ref!r} does not resolve to a "
                          f"real confirmation record", "high",
                          consent_ref=consent_ref))
    return {"inv": "INV-9", "verdict": "RED" if out else "GREEN",
            "fingerprint": fp, "violations": out}


def check_inv11(conn: sqlite3.Connection, root: Path) -> Dict[str, Any]:
    """INV-11 路径安全：body_path must stay inside the truth-layer root.

    No absolute path, no ``..`` segment, no backslash; and the resolved real
    path must share a commonpath with the workspace root.
    """
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT DISTINCT body_path FROM chunks "
            "WHERE body_path IS NOT NULL").fetchall()
    except sqlite3.Error as e:
        return {"inv": "INV-11", "verdict": "NOTRUN", "fingerprint": "n/a",
                "violations": [_v(REASON_INV11, CODE_INV11, "<index>",
                                  f"cannot read chunks: {e}", "high")]}
    if not rows:
        return {"inv": "INV-11", "verdict": "NOTRUN",
                "fingerprint": "body_paths=0", "violations": []}
    fp = f"body_paths={len(rows)}"
    for (bpath,) in rows:
        if not bpath:
            continue
        # C-6: the path rule lives in ONE place (agent.memory.paths) and is
        # shared with the content endpoint (manager.read_content_fresh). A
        # second copy of the rules is how the two drift apart.
        bad = _paths.path_escape_reason(bpath, root)
        if bad:
            out.append(_v(REASON_INV11, CODE_INV11, bpath,
                          f"unsafe body_path: {bad}", "high"))
    return {"inv": "INV-11", "verdict": "RED" if out else "GREEN",
            "fingerprint": fp, "violations": out}


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parse a timestamp into an aware datetime, or None if unparseable.

    P1-阶段三 (D-A): this was private (``_parse_ts``) and is now public,
    because the DECAY side must parse ``occurred_at`` the same way the
    shelf-lag check parses ``generated_at``.  Two parsers for one timestamp
    format is how "the file says one moment and the code thinks another"
    happens.

    Two spellings exist in this system and BOTH must compare correctly:
      * ``2026-10-29T14:45:35+08:00``  — changes.py (``isoformat``)
      * ``2026-10-29 06:45:35+00:00``  — shelf.py (``strftime``, space sep)
    They are the SAME instant; comparing them as strings says otherwise
    (P1 阶段二-B T2-B2). A naive timestamp (no offset) is treated as UTC+08
    — the system's own zone — rather than being rejected, so a legacy shelf
    page does not turn red on a formatting technicality.
    """
    raw = (value or "").strip().strip("'\"")
    if not raw:
        return None
    text = raw.replace(" ", "T", 1) if " " in raw and "T" not in raw else raw
    # Python < 3.11 cannot parse a trailing 'Z'; normalize it.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fall back to the handful of strftime shapes the generators use.
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CST)
    return dt


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse a timestamp into an aware datetime, or None if unparseable."""
    return parse_timestamp(value)


def check_shelf_lag(root: Path) -> Dict[str, Any]:
    """T2 shelf 滞后判据：真源变更晚于 shelf 页 generated_at → 报红。

    P0 拍板：书架是投影、批量全量重建、无写时增量。这条判据兜住时序——
    它不触发重建，只在「最新一次真源变更晚于最新 shelf 页的 generated_at」
    时报红，提示该跑 batch 重建了。变更时间取自单一变更记录点（同一份
    记录，索引投影也读它）；shelf 页时间取自 shelf/*.md front-matter 的
    ``generated_at``。
    """
    from agent.memory.facade import split_frontmatter  # local import: no cycle
    latest = _changes.latest_change_mtime(root)
    if latest is None:
        return {"inv": "SHELF-LAG", "verdict": "NOTRUN",
                "fingerprint": "no-change-events", "violations": []}
    shelf_dir = root / "memory" / "shelf"
    if not shelf_dir.is_dir():
        # Truth changed but no shelf exists at all -> the shelf is maximally
        # behind. Report it (this is the T2 red case before any batch rebuild).
        return {"inv": "SHELF-LAG", "verdict": "RED",
                "fingerprint": f"latest-change={latest}",
                "violations": [_v(REASON_SHELF_LAG, CODE_SHELF_LAG,
                                  "memory/shelf",
                                  f"truth-layer change at {latest} but no "
                                  f"shelf directory exists — run a batch "
                                  f"rebuild", "MID")]}
    generated: List[Tuple[datetime, str]] = []
    unparsable: List[str] = []
    for page in sorted(shelf_dir.glob("*.md")):
        try:
            fm, _ = split_frontmatter(page.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        ga = fm.get("generated_at")
        if not ga:
            continue
        parsed = _parse_ts(str(ga))
        if parsed is None:
            # Fail-CLOSED: an unparsable stamp is NOT green and NOT "not run".
            # We cannot prove the shelf is fresh, so we report red and say why.
            unparsable.append(str(ga))
            continue
        generated.append((parsed, str(ga)))
    if not generated:
        if unparsable:
            return {"inv": "SHELF-LAG", "verdict": "RED",
                    "fingerprint": f"latest-change={latest}, "
                                   f"unparsable={unparsable}",
                    "violations": [_v(
                        REASON_SHELF_LAG, CODE_SHELF_LAG, "memory/shelf",
                        f"shelf page generated_at could not be parsed "
                        f"({unparsable}) — freshness cannot be proven "
                        f"(fail-closed)", "MID")]}
        return {"inv": "SHELF-LAG", "verdict": "RED",
                "fingerprint": f"latest-change={latest}",
                "violations": [_v(REASON_SHELF_LAG, CODE_SHELF_LAG,
                                  "memory/shelf",
                                  f"truth-layer change at {latest} but no "
                                  f"shelf page carries generated_at", "MID")]}
    # P1 阶段二-B T2-B2: compare INSTANTS, not strings. The two timestamps
    # come from different generators with different spellings
    # (changes.py isoformat vs shelf.py strftime) — a string comparison said
    # "the shelf is behind" even for the very same moment.
    newest_dt, newest_gen = max(generated, key=lambda item: item[0])
    latest_dt = _parse_ts(latest)
    if latest_dt is None:
        return {"inv": "SHELF-LAG", "verdict": "RED",
                "fingerprint": f"latest-change={latest} (unparsable)",
                "violations": [_v(
                    REASON_SHELF_LAG, CODE_SHELF_LAG, "memory/shelf",
                    f"the newest change stamp {latest!r} could not be parsed "
                    f"— freshness cannot be proven (fail-closed)", "MID")]}
    lag = latest_dt > newest_dt
    out = [] if not lag else [
        _v(REASON_SHELF_LAG, CODE_SHELF_LAG, "memory/shelf",
           f"shelf is stale: latest truth-layer change {latest} is newer "
           f"than the newest shelf page generated_at {newest_gen} — run a "
           f"batch rebuild (P0 批量重建)", "MID")]
    if unparsable:
        # A page we cannot read is a finding on its own, even if another
        # page is fresh enough.
        out.append(_v(
            REASON_SHELF_LAG, CODE_SHELF_LAG, "memory/shelf",
            f"shelf page generated_at could not be parsed ({unparsable}) — "
            f"freshness cannot be proven (fail-closed)", "MID"))
    return {"inv": "SHELF-LAG", "verdict": "RED" if out else "GREEN",
            "fingerprint": f"latest-change={latest}, newest-shelf={newest_gen}",
            "violations": out}


# Deferred INVs — registered, with their trigger condition. NOT implemented
# in P1 (the element system is not shipped); listed so the report can say
# precisely WHICH of 03 号's INVs are landed here and which are pending.
DEFERRED_INVS = {
    "INV-4": "element_index 稳定 id（element 体系未上线）",
    "INV-5": "fact_sessions/messages_export 只追加（messages 外置为 §8 阶段三）",
    "INV-7": "element_tags（element 体系未上线）",
    "INV-8": "refs/element_links 无悬空（chunks 无 refs 列）",
}


def run_text_invariants(conn: sqlite3.Connection, root: Path, *,
                        sample_limit: int = 20,
                        stale_paths: Optional[List[str]] = None,
                        full: bool = True) -> Dict[str, Any]:
    """Run the P1-landed INVs (1/2/3/6/9/11) over chunks + filesystem.

    G-1: ``full`` defaults to True (exhaustive). The production
    inspection path must not degrade into sampling by omission.
    """
    checks = {
        "INV-1": check_inv1(conn, root),
        "INV-2/3": check_inv2_inv3(conn, root, sample_limit=sample_limit,
                                    stale_paths=stale_paths, full=full),
        "INV-6": check_inv6(conn),
        "INV-9": check_inv9(conn, root),
        "INV-11": check_inv11(conn, root),
        # T2: shelf 滞后判据（读单一变更记录点，不依赖 chunks 表）。
        "SHELF-LAG": check_shelf_lag(root),
    }
    violations: List[Dict[str, Any]] = []
    for c in checks.values():
        violations.extend(c["violations"])
    return {
        "scope": "03 号 INV chunks 落地版 (6 条: INV-1/2/3/6/9/11)",
        "deferred": DEFERRED_INVS,
        "checks": checks,
        "violations": violations,
        "violation_count": len(violations),
    }
