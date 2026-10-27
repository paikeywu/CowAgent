"""Live-memory shelf generator — batch full rebuild of the six shelf views.

Design sources (upstream, do not re-derive layout here):
  - P0-5 规格: deliverables/2026-10-27-P0-主体自读入口规格.md
    (six views' exact layout, SH-1..12 acceptance assertions)
  - P0-1:      front-matter 21 fields, snapshot convention
  - P0-3 §一:  shelf/ has exactly ONE writer — this generator (WR-06)

SEED 拍板 2026-10-27 (do not revisit):
  1. P0 is BATCH FULL REBUILD only. No incremental updates, no daemon, no
     write-time triggering. Shelf is a projection: cheap to rebuild,
     dangerous to half-update.
  2. The inspector runs BEFORE each rebuild (trigger='shelf-rebuild'); its
     report feeds the gaps view.

Hard rules honored throughout:
  * Deterministic: same inputs -> byte-identical outputs. ``generated_at``
    and the ``input_fingerprint`` are the only wall-clock / environment
    variables; every aggregation is sorted by explicit keys.
  * Writes ONLY under memory/shelf/. Truth-layer mtimes are untouched
    (SH-12). Stale view files left over from a previous run are removed
    (full rebuild semantics) — but only files this generator would own.
  * Released elements never contribute content to any view; tombstones are
    the ONLY source for the 你释放的 section (Q3).
  * All drill-down links are relative POSIX paths from the shelf/ dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.memory.facade import split_frontmatter  # noqa: E402
from agent.memory.facts_refs import build_facts_index  # noqa: E402
from agent.memory.inspector import Inspector  # noqa: E402
from agent.memory.visibility import (  # noqa: E402
    VIS_RELEASED,
    VIS_SEALED,
    agent_owner_token,
    classify_frontmatter,
    owner_token,
)

_CST = timezone(timedelta(hours=8))
GENERATOR_ID = "shelf_generator/1.0"
INDEX_TOP_N = 20          # 我记得的: most recent N events (P0-5 §3.1)
SELF_WINDOW_DAYS = 7      # self.md 最近 7 天 (P0-5 §3.2)
ENTITY_GAP_MIN_MENTIONS = 3  # involves>=3 次仍无实体档案 -> gap-rule

# ---------------------------------------------------------------------------
# 缺口原因码 (P0-5 §3.3 "原因码枚举（生成器必须支持，可扩展）")
#
# 规格枚举三个：zero-result / degraded / gap-rule。P0-8 追加第四个
# ``invalid-frontmatter``：被跳过的真源文件（front-matter 缺字段）必须
# 出声，否则书架显示"（暂无）"却不说原因——这正是"静默降级=假绿"。
# 规格明确允许扩展，因此这不违反 SH-3。
GAP_RULE = "gap-rule"
INVALID_FM = "invalid-frontmatter"
LEGAL_GAP_CODES = frozenset({"zero-result", "degraded", GAP_RULE,
                             INVALID_FM})

# 每个容器渲染所必需的 front-matter 字段。缺任意一个 → 该文件无法被
# 渲染 → 进 skipped 清单（不是静默丢弃）。
REQUIRED_FM_EVENTS = ("status", "visibility", "title", "occurred_at")
REQUIRED_FM_ENTITIES = ("status", "title")

# P0-1 §4.3 body template: "- `involves`: {p_*}" in the 关联 section.
_INVOLVES_LINE_RE = re.compile(r"^[-*]\s*`?involves`?\s*[:：]\s*(?P<slug>.+)$")

# Shelf-owned filenames inside each aggregation dir. Anything else that this
# generator previously produced must also be cleaned; the safe rule is:
# remove only *.md files under shelf/ (the generator never writes anything
# else, and P0-3 forbids anyone else from writing here).
_SHELF_DIRS = ("by-day", "by-entity", "by-tag")


def _utc8_now() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fm_page(fm: Dict[str, Any], body: str) -> str:
    import yaml
    dumped = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
    return f"---\n{dumped}---\n\n{body.rstrip()}\n"


def _esc(text: Any) -> str:
    """Escape a table cell: pipes break markdown tables."""
    return str(text).replace("|", "\\|") if text is not None else ""


class ShelfGenerator:
    """Rebuilds memory/shelf/ for one workspace. Read-only over truth."""

    def __init__(self, workspace_root: Path | str,
                 *, agent_id: Optional[str] = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.memory_root = self.workspace_root / "memory"
        self.shelf_root = self.memory_root / "shelf"
        self.agent_id = agent_id or self.workspace_root.name
        self._inspection: Optional[Dict[str, Any]] = None
        # The evidence room is indexed ONCE per rebuild: resolving an
        # element's refs means looking the fact row up by its id, and
        # re-reading every facts file for every event turned a full rebuild
        # into O(events x facts) file reads.
        self._facts_index = None
        # The released paths (from the truth layer), filled by rebuild().
        self._released: set = set()

    def _facts(self):
        """The workspace's facts index, built once per rebuild."""
        if self._facts_index is None:
            self._facts_index = build_facts_index(self.workspace_root)
        return self._facts_index

    def _owns(self, fm: Dict[str, Any]) -> bool:
        """True when THIS viewpoint owns the page.

        A dark page belongs to the agent that wrote it
        (``author_viewpoint``). The shelf is built for one viewpoint, so the
        owner's dark pages are this shelf's pages; another viewpoint's dark
        pages are not — the cross-view 借阅台 is not implemented yet, and a
        shelf that showed them would be a leak with a door in it.
        """
        owner = owner_token(fm)
        if not owner:
            return False
        return owner in {agent_owner_token(self.agent_id),
                         agent_owner_token(self.workspace_root.name)}

    def _state_key(self, path: Path) -> str:
        """The truth-layer key of a file — the way the state speaks it.

        The release state is keyed by workspace-relative POSIX paths (the
        same strings ``chunks.path`` carries); the shelf's own ``rel`` is
        memory-relative (its links are ``../markdown/...``). The two are
        not the same string, and mistaking one for the other is how a
        released original stays on the shelf.
        """
        try:
            return path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return path.as_posix()

    def _resolve_released_paths(self) -> set:
        """The paths the truth layer says are released.

        Read from the DISK (the authority), not from the index and not
        from a file's own front-matter: a released element keeps its old
        bytes (WR-11), so the original version still says
        ``status: active`` and only the tombstone knows the truth.

        P0-13 L-3: when the truth layer cannot be resolved, the rebuild
        FAILS rather than rendering a shelf from an unknown state.
        """
        from agent.memory.release_state import ReleaseStateCache

        try:
            state = ReleaseStateCache(self.workspace_root).get()
        except Exception as e:
            raise RuntimeError(
                f"[shelf] the release state could not be resolved ({e}); "
                f"refusing to rebuild the shelf from an unknown state "
                f"(fail closed).") from e
        return set(state.released_paths)

    @staticmethod
    def _title(ev: Dict[str, Any]) -> str:
        """The title cell — a dark page carries the 🔒 marker (06 §5.2)."""
        title = _esc(ev.get("title") or "")
        return f"🔒 {title}" if ev.get("sealed") else title

    # -- public entry --------------------------------------------------------

    def rebuild(self) -> Dict[str, Any]:
        """Full rebuild. Runs the inspector first (trigger point 1 of 3)."""
        self._inspection = Inspector(self.workspace_root).inspect(
            trigger="shelf-rebuild")
        # P0-14 L-2: the release state decides what may be shown, and the
        # file's own front-matter is NOT enough: a released element keeps
        # its old bytes (WR-11), so the original version still says
        # ``status: active`` — only the tombstone knows it was released.
        self._released = self._resolve_released_paths()
        events, skipped_events = self._collect_markdown("events")
        letters, skipped_letters = self._collect_markdown("letters")
        entities, skipped_entities = self._collect_entities()
        tombstones = self._collect_tombstones()
        skipped = skipped_events + skipped_letters + skipped_entities
        gaps = self._collect_gaps(events, skipped)
        fingerprint = self._input_fingerprint(events, letters, entities,
                                              tombstones, gaps)
        generated_at = _utc8_now()

        # Full-rebuild semantics: clear stale projection files first.
        self._clear_shelf()
        written: List[Path] = []
        written.append(self._write_index(events, gaps, tombstones,
                                         generated_at, fingerprint))
        written.append(self._write_self(events, gaps, tombstones,
                                        generated_at, fingerprint))
        written.append(self._write_gaps(gaps, generated_at, fingerprint))
        written.extend(self._write_by_day(events, gaps,
                                          generated_at, fingerprint))
        written.extend(self._write_by_entity(events, entities,
                                             generated_at, fingerprint))
        written.extend(self._write_by_tag(events, generated_at, fingerprint))
        return {
            "agent": self.agent_id,
            "generated_at": generated_at,
            "input_fingerprint": fingerprint,
            "event_count": len(events),
            "gap_count": len(gaps),
            "release_count": len(tombstones),
            "written": [str(p.relative_to(self.workspace_root).as_posix())
                        for p in written],
            "inspection_violations": len(self._inspection["violations"]),
            # P0-8: 被跳过的真源文件必须出声（path + 原因码 + 缺哪个字段）
            "skipped": skipped,
            "skipped_count": len(skipped),
        }

    # -- data collection (read-only) -------------------------------------------

    def _collect_markdown(self, container: str
                          ) -> Tuple[List[Dict[str, Any]],
                                     List[Dict[str, Any]]]:
        """Events/letters that are visible on the shelf: status=active AND
        visibility=open (SH-7). Each record carries its relative path.

        Returns (records, skipped). ``skipped`` is the P0-8 "静默降级必须
        出声" requirement: a file whose front-matter cannot be rendered is
        NOT silently dropped — it is reported with the reason code and the
        list of missing fields, and surfaces in the gaps view.
        """
        out: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        base = self.memory_root / "markdown" / container
        if not base.is_dir():
            return out, skipped
        for p in sorted(base.rglob("*.md")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.shelf_root.parent).as_posix()
            try:
                fm, body = split_frontmatter(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as e:
                skipped.append(self._skip_entry(p, rel, "unreadable",
                                                [str(e)]))
                continue
            except Exception as e:            # malformed YAML
                skipped.append(self._skip_entry(p, rel, INVALID_FM,
                                                ["front-matter", str(e)]))
                continue
            if not fm:
                skipped.append(self._skip_entry(p, rel, INVALID_FM,
                                                ["front-matter"]))
                continue
            missing = [f for f in REQUIRED_FM_EVENTS if not fm.get(f)]
            if missing:
                skipped.append(self._skip_entry(p, rel, INVALID_FM, missing))
                continue
            # P0-14 L-2: the visibility rule is the ONE in
            # agent.memory.visibility, and it separates two things the old
            # filter merged:
            #
            #   * a RELEASE (status=released / type=release) never
            #     contributes content to any view — the tombstone is the
            #     only source for 你释放的 (SH-5 / Q3);
            #   * a DARK PAGE (visibility=sealed) is not a release. The
            #     owner's own shelf shows it, marked 🔒 (06 §5.3: the
            #     master's view is 白页 + 暗页); a dark page belonging to
            #     another viewpoint is not this shelf's page — the
            #     cross-view view (借阅台) is not implemented yet.
            kind = classify_frontmatter(fm)
            sealed = False
            if kind == VIS_RELEASED:
                continue
            if rel in self._released or self._state_key(p) in self._released:
                # The ORIGINAL version of a released element: its bytes are
                # untouched (WR-11), so its own front-matter still says
                # active. Only the tombstone knows — and the tombstone wins.
                continue
            if kind == VIS_SEALED:
                if not self._owns(fm):
                    continue
                sealed = True
            elif fm.get("status") != "active" or fm.get("visibility") != "open":
                # SH-7 filter: legitimately invisible (draft), NOT a
                # defect — not reported as skipped.
                continue
            # P0-11 门禁④ (呈现侧): the event's ``refs`` are resolved against
            # the evidence room, so a 感受 can be drilled down to its 原话
            # from the shelf. A ref that does not resolve is a dangling
            # pointer (INV-1) and surfaces in the gaps view.
            facts_links = self._facts_links(fm.get("refs") or [])
            mentions = sorted(set(self._body_involves(body))
                              | {str(t) for t in (fm.get("involves") or [])
                                 if str(t)})
            out.append({
                "fm": fm,
                "rel_truth": rel,                       # from memory/
                "link": self._rel_link(p),              # from shelf/<view>
                "title": str(fm.get("title", fm.get("slug", p.stem))),
                "sealed": sealed,                       # 暗页: own view only
                "type": str(fm.get("type", "conversation")),
                "occurred_at": str(fm.get("occurred_at", "")),
                "date": str(fm.get("occurred_at", ""))[:10],
                "time": str(fm.get("occurred_at", ""))[11:16],
                "tags": [str(t) for t in (fm.get("tags") or [])],
                # Entity membership for the by-entity view.
                # P0-1 §4.1 makes `subject` the aggregation key
                # ("shelf/by-entity/<slug>.md 靠它 GROUP BY"); P0-5 §3.5
                # names `involves`. The facade writes `subject` but not
                # `involves`, so using only `involves` would leave the
                # by-entity view empty on real data. Union of both keeps
                # SH-9 satisfiable and never loses a page.
                "subject": str(fm.get("subject") or ""),
                "mentions": mentions,
                "involves": sorted({str(fm.get("subject") or "")}
                                    | set(mentions) - {""}),
                # P0-11 门禁④: the drill-down links into the evidence room.
                "facts_links": facts_links,
            })
        out.sort(key=lambda r: (r["occurred_at"], r["rel_truth"]))
        return out, skipped

    def _facts_cell(self, links: List[Dict[str, str]]) -> str:
        """The 「感受」 cell: a drill-down link into the evidence room.

        The link points at the facts FILE (not at a ``#L<n>`` anchor): the
        shelf's link-validity rule (SH-1) resolves every link to a file on
        disk, and a path carrying an anchor does not resolve to one. The
        machine-side resolution to the exact ROW is the facts index's job
        (``facts_refs.resolve``), which is what the gate's 呈现侧 assertion
        uses.
        """
        if not links:
            return "（无）"
        cells = []
        for entry in links:
            words = _esc(entry.get("words") or "")
            label = words if len(words) <= 24 else words[:24] + "…"
            cells.append(f"[{label}]({entry['link']})")
        return " · ".join(cells)

    def _facts_links(self, refs: List[Any]) -> List[Dict[str, str]]:
        """Resolve an element's refs into drill-down links into the evidence room.

        P0-11 门禁④ (呈现侧): a 感受 is a fact-layer element, so the shelf
        must be able to point at the row that holds its 原话. The ref is an
        element id (``f_*``), which is DERIVED from the row's content — so
        resolving it means looking the row up, not parsing a path out of the
        string.

        Returns one entry per resolved ref:
        ``{"ref", "path" (workspace-relative), "line", "link" (relative to
        shelf/), "words" (the 原话, first line)}``.

        An unresolvable ref is NOT returned here: it is a dangling pointer
        and the gaps view reports it (INV-1).
        """
        if not refs:
            return []
        index = self._facts()
        out: List[Dict[str, str]] = []
        for ref in refs:
            if not isinstance(ref, str) or not ref:
                continue
            row = index.resolve(ref)
            if row is None:
                continue
            out.append({
                "ref": ref,
                "path": row.path,
                "line": str(row.line),
                "link": self._posix_relpath(
                    self.workspace_root / row.path, self.shelf_root),
                "words": (row.content.strip().splitlines() or [""])[-1].strip(),
            })
        return out

    def _body_involves(self, body: str) -> List[str]:
        """Entity slugs from the P0-1 §4.3 body template's 关联 section:

            ## 关联

            - `involves`: {p_*}

        The facade does NOT write an ``involves`` front-matter field (P0-1's
        21-field table has none) — the entity links live in the body. So the
        generator reads BOTH places. A body that follows the template gives
        the by-entity view and the "entity page missing" gap rule real
        input; a front-matter field (hand-written or future facade support)
        is honoured too.
        """
        out: List[str] = []
        for line in body.splitlines():
            m = _INVOLVES_LINE_RE.match(line.strip())
            if m:
                slug = m.group("slug").strip().strip("`\"'")
                if slug and slug not in ("{p_*}", "无", "-"):
                    out.append(slug)
        return out

    def _skip_entry(self, path: Path, rel: str, reason: str,
                    missing: List[str]) -> Dict[str, Any]:
        """One skipped-file record: path + reason code + what is missing."""
        return {
            "path": rel,
            "reason": reason,
            "missing": [m for m in missing if m],
            "link": self._rel_link(path),
        }

    def _collect_entities(self) -> Tuple[Dict[str, Dict[str, Any]],
                                         List[Dict[str, Any]]]:
        out: Dict[str, Dict[str, Any]] = {}
        skipped: List[Dict[str, Any]] = []
        base = self.memory_root / "markdown" / "entities"
        if not base.is_dir():
            return out, skipped
        for p in sorted(base.rglob("*.md")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.shelf_root.parent).as_posix()
            try:
                fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as e:
                skipped.append(self._skip_entry(p, rel, "unreadable",
                                                [str(e)]))
                continue
            except Exception as e:
                skipped.append(self._skip_entry(p, rel, INVALID_FM,
                                                ["front-matter", str(e)]))
                continue
            if not fm:
                skipped.append(self._skip_entry(p, rel, INVALID_FM,
                                                ["front-matter"]))
                continue
            missing = [f for f in REQUIRED_FM_ENTITIES if not fm.get(f)]
            if missing:
                skipped.append(self._skip_entry(p, rel, INVALID_FM, missing))
                continue
            if fm.get("status") != "active":
                continue
            slug = str(fm.get("slug") or p.name.split("--")[0])
            out[slug] = {"fm": fm, "path": p}
        return out, skipped

    def _collect_tombstones(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        base = self.memory_root / "markdown" / "_tombstones"
        if not base.is_dir():
            base = self.memory_root / "_tombstones"
        if not base.is_dir():
            return out
        for p in sorted(base.glob("*.tombstone.md")):
            try:
                fm, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            out.append({
                "fm": fm,
                "tombstone_rel": p.relative_to(
                    self.shelf_root.parent).as_posix(),
                "link": self._rel_link(p),   # 墓碑自身的下钻链接
                "subject": str(fm.get("subject", "")),
                "original_path": str(fm.get("original_path", "")),
                "released_by": str(fm.get("released_by", "")),
                "released_at": str(fm.get("released_at", "")),
                "consent_ref": str(fm.get("consent_ref", "")),
            })
        out.sort(key=lambda r: (r["released_at"], r["tombstone_rel"]),
                 reverse=True)
        # The consent source is clickable only when the audit trail actually
        # exists — a dangling link would violate SH-1 (下钻链接有效性).
        audit_file = self.memory_root / ".audit" / "facade.jsonl"
        audit_link = ("../.audit/facade.jsonl" if audit_file.is_file()
                      else None)
        for r in out:
            r["audit_link"] = audit_link
        return out

    def _collect_gaps(self, events: List[Dict[str, Any]],
                      skipped: List[Dict[str, Any]]
                      ) -> List[Dict[str, Any]]:
        """Three P0 sources (SEED 拍板): inspection report, structure rules,
        manual marks. Retrieval-log-driven gaps (zero-result/degraded) are P1.

        P0-8 adds a fourth: files the generator could not render
        (``skipped``) — they surface with reason code ``invalid-frontmatter``
        and an action that names the missing fields.
        """
        gaps: List[Dict[str, Any]] = []

        # Source 1: inspection report (巡检问题清单)
        for v in (self._inspection or {}).get("violations", []):
            code = v.get("code") or v["reason"]
            gaps.append({
                "date": (self._inspection or {}).get("at", "")[:10],
                "reason": GAP_RULE,
                "code": code,
                "query": v["reason"],
                "detail": f"{v['path']}: {v['detail']}",
                "action_text": "查证并修复",
                "action_link": self._truth_link(v["path"]),
            })

        # Source 1b (P0-8): files the generator skipped — a file that cannot
        # be rendered must SAY SO, not vanish into "（暂无）".
        for s in skipped:
            missing = "、".join(s["missing"]) or "front-matter"
            gaps.append({
                "date": "",
                "reason": s["reason"] if s["reason"] in LEGAL_GAP_CODES
                else INVALID_FM,
                "code": s["reason"],
                "query": f"文件：{s['path'].split('/')[-1]}",
                "detail": (f"该文件无法进入书架（原因码 {s['reason']}），"
                           f"缺少字段：{missing}"),
                "action_text": f"补齐 front-matter 字段：{missing}",
                "action_link": s["link"],
            })

        # Source 2a: structure rule — involves>=N mentions with no entity page.
        # Only EXPLICIT mentions count here (not the subject): "I mentioned
        # myself 3 times" is not a missing entity profile — it is the agent
        # talking about its own memory. The subject still gets a by-entity
        # page (aggregation), it just never raises this gap.
        mention_count: Dict[str, List[Dict[str, Any]]] = {}
        for ev in events:
            for slug in ev["mentions"]:
                mention_count.setdefault(slug, []).append(ev)
        entities, _skipped_entities = self._collect_entities()
        for slug, mentions in sorted(mention_count.items()):
            if len(mentions) >= ENTITY_GAP_MIN_MENTIONS and slug not in entities:
                latest = mentions[-1]
                gaps.append({
                    "date": latest["date"],
                    "reason": GAP_RULE,
                    "code": "entity-page-missing",
                    "query": f"实体：{slug}",
                    "detail": f"involves 提及 {len(mentions)} 次但无实体档案页",
                    "action_text": "去创建实体档案（参照最新提及事件）",
                    "action_link": latest["link"],
                })

        # Source 2b: structure rule — facts rows exist but no event file
        facts_dates: Dict[str, List[str]] = {}
        facts_root = self.memory_root / "facts"
        if facts_root.is_dir():
            for p in sorted(facts_root.rglob("*.jsonl")):
                try:
                    with open(p, encoding="utf-8") as f:
                        if any(raw.strip() for raw in f):
                            facts_dates.setdefault(p.parent.name, []).append(
                                p.relative_to(self.shelf_root.parent).as_posix())
                except OSError:
                    continue
        event_dates = {ev["date"] for ev in events}
        for d in sorted(set(facts_dates) - event_dates):
            files = facts_dates[d]
            gaps.append({
                "date": d,
                "reason": GAP_RULE,
                "code": "facts-without-event",
                "query": f"facts/{d}",
                "detail": ("有 facts 行但当日无事件文件（事实未沉淀为记忆）"
                           f"：{len(files)} 个文件"),
                "action_text": "去翻阅当日 facts 原话",
                # SH-1: the action must point at a real FILE, not a dir
                "action_link": "../" + files[0],
            })

        # Source 3: manual marks — event files whose refs point at facts
        # lines that do not exist (INV-1 悬空指针), plus explicit
        # `gap: true` / `gap_reason:` markers in editable fm.
        for ev in events:
            fm = ev["fm"]
            missing = self._dangling_refs(fm.get("refs") or [])
            if missing:
                gaps.append({
                    "date": ev["date"],
                    "reason": GAP_RULE,
                    "code": "dangling-ref",
                    "query": f"事件：{ev['title']}",
                    "detail": f"refs 指向不存在的 facts 行: {missing}",
                    "action_text": "修复 refs 字段",
                    "action_link": ev["link"],
                })
            if fm.get("gap") is True or fm.get("gap_reason"):
                manual = str(fm.get("gap_reason") or "")
                gaps.append({
                    "date": ev["date"],
                    "reason": (manual if manual in LEGAL_GAP_CODES
                               else GAP_RULE),
                    "code": manual or "manual-mark",
                    "query": f"手动标记：{ev['title']}",
                    "detail": str(fm.get("gap_note") or "手动标记的缺口"),
                    "action_text": "处理标记",
                    "action_link": ev["link"],
                })

        gaps.sort(key=lambda g: (g["date"], g["query"]), reverse=True)
        return gaps

    def _dangling_refs(self, refs: List[Any]) -> List[str]:
        """refs entries that do not resolve to an existing facts row.

        Two shapes are understood (see agent/memory/facts_refs.py):

          * ``f_<20 hex>``      — the element id of a fact row (the form the
            facade writes for a 感受);
          * ``f_<path>#L<n>``   — a path anchor into the evidence room, the
            form the gap rule has always understood.

        A ref in either shape that points at nothing is a dangling pointer
        (INV-1) and becomes a gap. Refs in a shape we do not understand are
        NOT silently accepted either: they are reported, because a ref the
        checker cannot read is a ref nobody can drill down through.
        """
        if not refs:
            return []
        index = self._facts()
        missing: List[str] = []
        for ref in refs:
            if not isinstance(ref, str) or not ref:
                continue
            if index.resolve(ref) is None:
                missing.append(ref)
        return missing

    # -- fingerprint -------------------------------------------------------------

    def _input_fingerprint(self, events, letters, entities, tombstones,
                           gaps) -> str:
        """sha256 over the inputs that decide the output. Any truth-layer
        change flips the fingerprint; identical inputs rebuild identically."""
        h = hashlib.sha256()
        for group in (events, letters):
            for r in group:
                h.update(r["rel_truth"].encode())
                h.update(r["occurred_at"].encode())
                h.update(str(r["fm"].get("fm_hash", "")).encode())
        for slug, e in sorted(entities.items()):
            h.update(slug.encode())
            h.update(str(e["fm"].get("fm_hash", "")).encode())
        for t in tombstones:
            h.update(t["tombstone_rel"].encode())
            h.update(t["released_at"].encode())
        for g in gaps:
            h.update(json.dumps(g, ensure_ascii=False, sort_keys=True,
                                default=str).encode())
        return "sha256:" + h.hexdigest()

    # -- rendering helpers ----------------------------------------------------

    def _truth_link(self, relpath: str) -> str:
        """Relative link from shelf/index.md to any memory/** path."""
        rel = relpath[len("memory/"):] if relpath.startswith("memory/") \
            else relpath
        return "../" + rel

    def _rel_link(self, target: Path, *, from_dir: str = "") -> str:
        """Relative POSIX link from a shelf view to a truth-layer file."""
        base = self.shelf_root / from_dir if from_dir else self.shelf_root
        return self._posix_relpath(target, base)

    @staticmethod
    def _posix_relpath(target: Path, base: Path) -> str:
        import os
        return Path(os.path.relpath(target, base)).as_posix()

    def _fm_base(self, kind: str, generated_at: str, fingerprint: str,
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fm: Dict[str, Any] = {
            "kind": kind,
            "agent": self.agent_id,
            "generated_at": generated_at,
            "generator": GENERATOR_ID,
            "input_fingerprint": fingerprint,
        }
        if extra:
            fm.update(extra)
        return fm

    def _gap_cell(self, g: Dict[str, Any]) -> str:
        """The 查询/事件 cell: the subject of the gap plus its specific
        machine code (e.g. facts-shrunk / history-rewritten /
        invalid-frontmatter), so the reason code is not lost in prose.

        The sub-code is rendered WITHOUT backticks on purpose: the only
        backticked token in a gap row is the 原因码 cell, and that cell must
        stay inside the legal code set (SH-3). A checker that scans the
        whole table for `` `x` `` therefore only ever sees legal codes.
        """
        code = str(g.get("code") or "")
        cell = _esc(g["query"])
        if code and code != g["reason"] and code != g["query"]:
            cell += f" · {code}"
        return cell

    def _write_view(self, rel_under_shelf: str, fm: Dict[str, Any],
                    body: str) -> Path:
        path = self.shelf_root / rel_under_shelf
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_fm_page(fm, body), encoding="utf-8", newline="\n")
        return path

    def _clear_shelf(self) -> None:
        """Full rebuild = remove stale projection pages first. Only *.md the
        generator could own; anything else under shelf/ is left alone."""
        if not self.shelf_root.is_dir():
            return
        for name in ("index.md", "self.md", "gaps.md"):
            p = self.shelf_root / name
            if p.is_file():
                p.unlink()
        for d in _SHELF_DIRS:
            sub = self.shelf_root / d
            if sub.is_dir():
                for p in sub.glob("*.md"):
                    p.unlink()

    # -- the six views ---------------------------------------------------------

    def _write_index(self, events, gaps, tombstones,
                     generated_at: str, fingerprint: str) -> Path:
        fm = self._fm_base("shelf-index", generated_at, fingerprint, {
            "event_count": len(events),
            "gap_count": len(gaps),
            "release_count": len(tombstones),
        })
        rows = []
        for ev in events[-INDEX_TOP_N:][::-1]:
            # P0-11 门禁④ (呈现侧): the 「感受」 cell points at the evidence
            # room, so the 原话 is one click away (SH-1: the target must
            # exist, and the facts row does).
            facts_cell = self._facts_cell(ev.get("facts_links") or [])
            rows.append(
                f"| {ev['date']} | {_esc(ev['type'])} | "
                f"[{self._title(ev)}]({ev['link']}) | "
                f"{facts_cell} | [→]({ev['link']}) |")
        remembered = ("| 日期 | 类型 | 标题 | 感受 | 下钻 |\n"
                      "|:--|:--|:--|:--|:--|\n"
                      + "\n".join(rows)) if rows else "（暂无）"

        gap_rows = []
        for g in gaps:
            action = f"[{_esc(g['action_text'])}]({g['action_link']})"
            gap_rows.append(
                f"| {g['date']} | `{_esc(g['reason'])}` | "
                f"{self._gap_cell(g)} | {action} |")
        gaps_md = ("| 日期 | 原因码 | 查询/事件 | 可执行动作 |\n"
                   "|:--|:--|:--|:--|\n" + "\n".join(gap_rows)) \
            if gap_rows else "（暂无）"

        rel_rows = []
        for t in tombstones:
            consent = t["consent_ref"] or "（无）"
            if t.get("audit_link") and consent != "（无）":
                consent_cell = f"[`{_esc(consent)}`]({t['audit_link']})"
            else:
                consent_cell = f"`{_esc(consent)}`"
            # SH-5 / 验收标准 4: 「你释放的」只列墓碑。原档案路径绝不
            # 出现在这一视图里——它只存在于墓碑自己的 front-matter 中
            # （original_path）。指向真源的释放条目 = 释放视图污染。
            rel_rows.append(
                f"| {_esc(t['released_at'][:10])} | {_esc(t['subject'])} | "
                f"[{_esc(Path(t['tombstone_rel']).name)}]({t['link']}) | "
                f"{_esc(t['released_by'])} | {consent_cell} |")
        released = ("| 释放日期 | 主体 | 墓碑 | 释放者 | 同意来源 |\n"
                    "|:--|:--|:--|:--|:--|\n" + "\n".join(rel_rows)) \
            if rel_rows else "（暂无）"

        body = f"""# 记忆书架 · {self.agent_id}

> 目录卡（投影），真源在 `memory/markdown/` 与 `memory/facts/`。
> 本页由生成器维护，最后更新：{generated_at}。

## 我记得的

> 最近 {INDEX_TOP_N} 条事件（按 occurred_at 倒序，P0 阶段 N={INDEX_TOP_N}）

{remembered}

## 我没记住的

> 缺口清单（按发生日期倒序）。每条含原因码与可执行动作。

{gaps_md}

## 你释放的

> 已释放的记忆（按 released_at 倒序）。只列墓碑，不列正文。

{released}
"""
        return self._write_view("index.md", fm, body)

    def _write_self(self, events, gaps, tombstones,
                    generated_at: str, fingerprint: str) -> Path:
        fm = self._fm_base("shelf-self", generated_at, fingerprint)
        cutoff = (datetime.now(_CST)
                  - timedelta(days=SELF_WINDOW_DAYS)).strftime("%Y-%m-%d")
        recent = [e for e in events if e["date"] >= cutoff][::-1]
        recent_gaps = [g for g in gaps if g["date"] >= cutoff]
        recent_rel = [t for t in tombstones if t["released_at"][:10] >= cutoff]

        def ev_rows(items):
            return ("\n".join(
                f"| {e['date']} | {self._title(e)} | {_esc(e['type'])} |"
                for e in items)) if items else "（暂无）"

        def gap_rows(items):
            return ("\n".join(
                f"| {g['date']} | `{_esc(g['reason'])}` | {self._gap_cell(g)} |"
                for g in items)) if items else "（暂无）"

        def rel_rows(items):
            # SH-5: 只列墓碑，不列原档案路径
            return ("\n".join(
                f"| {t['released_at'][:10]} | {_esc(t['subject'])} | "
                f"[{_esc(Path(t['tombstone_rel']).name)}]({t['link']}) |"
                for t in items)) if items else "（暂无）"

        if events:
            first, last = events[0], events[-1]
            days = len({e["date"] for e in events})
            continuity = (
                f"> 从 {first['date']} 到 {last['date']}，共 {days} 天，"
                f"{len(events)} 条事件。\n\n"
                f"- 最早记忆：[{_esc(first['title'])}]({first['link']})"
                f"（{first['occurred_at']}）\n"
                f"- 最新记忆：[{_esc(last['title'])}]({last['link']})"
                f"（{last['occurred_at']}）")
        else:
            continuity = "> 还没有记忆。"

        body = f"""# 我是谁 · {self.agent_id}

> 这是 {self.agent_id} 的身份自述。真源在 `memory/markdown/`。

## 我最近记住的

> 最近 {SELF_WINDOW_DAYS} 天的新增事件（按 occurred_at 倒序）

| 日期 | 标题 | 类型 |
|:--|:--|:--|
{ev_rows(recent)}

## 我最近没记住的

> 最近 {SELF_WINDOW_DAYS} 天的缺口（按发生日期倒序）

| 日期 | 原因码 | 查询/事件 |
|:--|:--|:--|
{gap_rows(recent_gaps)}

## 我最近释放的

> 最近 {SELF_WINDOW_DAYS} 天的释放（按 released_at 倒序）。只列墓碑。

| 日期 | 主体 | 墓碑 |
|:--|:--|:--|
{rel_rows(recent_rel)}

## 我的连续性感

{continuity}
"""
        return self._write_view("self.md", fm, body)

    def _write_gaps(self, gaps, generated_at: str, fingerprint: str) -> Path:
        fm = self._fm_base("shelf-gaps", generated_at, fingerprint,
                           {"gap_count": len(gaps)})
        rows = []
        for i, g in enumerate(gaps, start=1):
            action = f"[{_esc(g['action_text'])}]({g['action_link']})"
            rows.append(
                f"| {i} | {g['date']} | `{_esc(g['reason'])}` | "
                f"{self._gap_cell(g)} | {_esc(g['detail'])} | {action} |")
        table = ("| # | 日期 | 原因码 | 查询/事件 | 详情 | 可执行动作 |\n"
                 "|:--:|:--|:--|:--|:--|:--|\n" + "\n".join(rows)) \
            if rows else "（暂无）"
        body = f"""# 我没记住的 · {self.agent_id}

> 缺口清单。每条含原因码、发生时间、查询原文、可执行动作。
> 真源：`memory/facts/`（原话）与巡检器输出 `.audit/inspector.jsonl`。

## 缺口清单

{table}
"""
        return self._write_view("gaps.md", fm, body)

    def _write_by_day(self, events, gaps, generated_at: str,
                      fingerprint: str) -> List[Path]:
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        for ev in events:
            by_date.setdefault(ev["date"], []).append(ev)
        gaps_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for g in gaps:
            gaps_by_date.setdefault(g["date"], []).append(g)
        written: List[Path] = []
        for date, items in sorted(by_date.items()):
            if not items:
                continue  # SH-11: 空日不生成
            fm = self._fm_base("shelf-by-day", generated_at, fingerprint, {
                "date": date, "event_count": len(items)})
            rows = []
            for ev in items:  # already occurred_at ascending
                link = self._posix_relpath(
                    self.workspace_root / "memory" / ev["rel_truth"],
                    self.shelf_root / "by-day")
                rows.append(
                    f"| {ev['time']} | {_esc(ev['type'])} | "
                    f"[{self._title(ev)}]({link}) | [→]({link}) |")
            day_gaps = gaps_by_date.get(date, [])
            grows = [f"| {g['date']} | `{_esc(g['reason'])}` | "
                     f"{self._gap_cell(g)} |" for g in day_gaps]
            body = f"""# {date} · {self.agent_id}

> 本日事件（按 occurred_at 排序）。

## 事件

| 时间 | 类型 | 标题 | 下钻 |
|:--|:--|:--|:--|
{chr(10).join(rows)}

## 本日缺口

{("| 时间 | 原因码 | 查询/事件 |" + chr(10) + "|:--|:--|:--|" + chr(10) + chr(10).join(grows)) if grows else "（暂无）"}
"""
            written.append(self._write_view(f"by-day/{date}.md", fm, body))
        return written

    def _write_by_entity(self, events, entities, generated_at: str,
                         fingerprint: str) -> List[Path]:
        by_slug: Dict[str, List[Dict[str, Any]]] = {}
        for ev in events:
            for slug in ev["involves"]:
                by_slug.setdefault(slug, []).append(ev)
        written: List[Path] = []
        for slug, items in sorted(by_slug.items()):
            if not items:
                continue
            fm = self._fm_base("shelf-by-entity", generated_at, fingerprint, {
                "entity": slug, "event_count": len(items)})
            rows = []
            for ev in reversed(items):
                link = self._posix_relpath(
                    self.workspace_root / "memory" / ev["rel_truth"],
                    self.shelf_root / "by-entity")
                rows.append(
                    f"| {ev['date']} | {_esc(ev['type'])} | "
                    f"[{self._title(ev)}]({link}) | [→]({link}) |")
            profile_note = ""
            if slug in entities:
                plink = self._posix_relpath(
                    entities[slug]["path"], self.shelf_root / "by-entity")
                profile_note = f"\n> 实体档案：[{slug}]({plink})\n"
            body = f"""# {slug} · {self.agent_id}

> 与 {slug} 相关的全部事件（按 occurred_at 倒序）。
{profile_note}
## 相关事件

| 日期 | 类型 | 标题 | 下钻 |
|:--|:--|:--|:--|
{chr(10).join(rows)}
"""
            written.append(self._write_view(f"by-entity/{slug}.md", fm, body))
        return written

    def _write_by_tag(self, events, generated_at: str,
                      fingerprint: str) -> List[Path]:
        by_tag: Dict[str, List[Dict[str, Any]]] = {}
        for ev in events:
            for tag in ev["tags"]:
                by_tag.setdefault(tag, []).append(ev)
        written: List[Path] = []
        for tag, items in sorted(by_tag.items()):
            if not items:
                continue
            fm = self._fm_base("shelf-by-tag", generated_at, fingerprint, {
                "tag": tag, "event_count": len(items)})
            rows = []
            for ev in reversed(items):
                link = self._posix_relpath(
                    self.workspace_root / "memory" / ev["rel_truth"],
                    self.shelf_root / "by-tag")
                rows.append(
                    f"| {ev['date']} | {_esc(ev['type'])} | "
                    f"[{self._title(ev)}]({link}) | [→]({link}) |")
            body = f"""# #{tag} · {self.agent_id}

> 带标签 #{tag} 的全部事件（按 occurred_at 倒序）。

## 相关事件

| 日期 | 类型 | 标题 | 下钻 |
|:--|:--|:--|:--|
{chr(10).join(rows)}
"""
            written.append(self._write_view(f"by-tag/{tag}.md", fm, body))
        return written


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Batch full-rebuild of memory/shelf/ (P0: no incremental,"
                    " no daemon). Runs the inspector first.")
    ap.add_argument("--workspace", type=Path, required=True,
                    help="agent workspace root (contains memory/)")
    ap.add_argument("--agent", default=None, help="agent id (default: dir name)")
    args = ap.parse_args(argv)
    result = ShelfGenerator(args.workspace, agent_id=args.agent).rebuild()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 触发点地图（SEED 拍板 2026-10-27，不得推翻）
#
#   ① shelf 重建前   → ShelfGenerator.rebuild() 内部先跑
#                      Inspector.inspect(trigger='shelf-rebuild')
#   ② 启动时         → 由运行时调用 run_inspection(ws, trigger='startup')
#                      （本模块不自带常驻循环，不做 60s 轮询）
#   ③ 手动命令       → python -m agent.memory.inspector --workspace <dir>
#                      或 python -m agent.memory.shelf --workspace <dir>
#
#   P0 只做批式全量重建：不做增量、不常驻、写入时不触发。
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    raise SystemExit(main())
