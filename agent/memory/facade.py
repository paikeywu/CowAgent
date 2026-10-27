"""Live-memory write facade — the single governed write entry for
``memory/{facts, markdown/{events,entities,letters}}``.

Spec sources (both are upstream of this module; do not re-derive rules here):
  - P0-1 格式规范: deliverables/2026-10-27-P0-事件格式与事实层规范.md
    (event front-matter 21 fields, facts JSONL line format + hash chain)
  - P0-3 安全规则: deliverables/2026-10-27-P0-写入门面安全规则与越权用例.md
    (WR-01..21 write-time rejects, RR-01..05 read-time re-verification)

What this facade deliberately does NOT govern (P0-3 §一):
  - memory/long-term/** (old sync machinery owns it; double-write forbidden)
  - memory/dreams/**, memory/.evolution_backups/**, memory/.changes/**
  - memory/YYYY-MM-DD.md legacy dailies, MEMORY.md
  - shelf/ is projection-only: only the (P0-5) shelf generator writes there;
    this facade exposes a placeholder hook and rejects direct writes.

Three总纲 honored throughout:
  1. Every write-time rule that can be bypassed outside the facade has a
     read-time counterpart (RR) or an inspection hook (verify_facts_file).
  2. actor comes from the runtime identity (constructor DI seam for tests),
     never from a call parameter — on_behalf_of/agent_id kwargs are rejected.
  3. Every rejection appends one audit line (who/what/why/rejected_at).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from agent.memory.naming import (
    COMPONENT_MAX_BYTES,
    PATH_MAX_BYTES,
    build_memory_path,
    canonical_text,
    check_name,
    check_relpath,
)
from agent.memory.facts_refs import (
    feeling_element_id,
    feeling_run_id,
)
from agent.memory.frontmatter import split_frontmatter
from agent.memory.visibility import (
    READ_RELEASED,
    READ_SEALED,
    SEALED_PLACEHOLDER,
    VIS_RELEASED,
    VIS_SEALED,
    agent_owner_token,
    classify_frontmatter,
    owner_token,
)

# ---------------------------------------------------------------------------
# Exceptions (P0-3 §0 名词约定 — exact names, do not rename)
# ---------------------------------------------------------------------------


class FacadeError(RuntimeError):
    """Base of every facade rejection."""

    rule = ""  # WR/RR id the error implements


class PathEscapeError(FacadeError):
    rule = "WR-01..04"


class CrossRoleWriteError(FacadeError):
    rule = "WR-05"


class DisallowedLocationError(FacadeError):
    rule = "WR-06"


class TargetExistsError(FacadeError):
    rule = "WR-08"


class FrontmatterTamperError(FacadeError):
    rule = "RR-03"


class HashChainBreakError(FacadeError):
    rule = "WR-14/16"


class AppendOnlyViolationError(FacadeError):
    rule = "WR-11/13"


class ConsentRequiredError(FacadeError):
    rule = "WR-17..19"


class SecondConfirmRequiredError(FacadeError):
    rule = "WR-20"


class ConfirmationExpiredError(FacadeError):
    rule = "WR-20"


class ImmutableFieldError(FacadeError):
    rule = "WR-09"


# ---------------------------------------------------------------------------
# Constants from the specs
# ---------------------------------------------------------------------------

# P0-3 名词约定: front-matter fields only the facade may write.
IMMUTABLE_FM_FIELDS = frozenset({
    "id", "layer", "type", "occurred_at", "recorded_at", "status", "version",
    "supersedes", "superseded_by", "subject", "released", "released_by",
    "released_at", "consent_ref", "fm_hash", "slug", "hash8", "author_viewpoint",
})

# ---------------------------------------------------------------------------
# subject 合法值域（P0-4 打回修复新增，P0-3 WR-19 的前提）
#
# 真人档案（entities/people/）的 subject **必须是真人 slug**——当前唯一被
# 认可的真人 subject 是 "peiqi"（沛琦，体系内唯一真人档案主体，见 P0-1 §一
# 裁定与 P0-3 §四）。目录 agents/ 下的任何 agent id 都**不得**成为 people/
# 容器档案的 subject；agent 自身的记忆归属由 events/letters/relations/ 的
# subject = actor 默认承载，不建 people/ 档案。这条约束是 WR-19/WR-20 的
# 根基——真人档案释放走"peiqi 专属 + 二次确认"，前提是"是不是真人档案"
# 这个判定本身不可被写入参数伪造。
#
# 新增真人主体（如未来的家庭成员）的正规路径：先由沛琦拍板扩充
# HUMAN_PROFILE_SUBJECTS，再建档案；写入路径不接受临时发明 subject。
#
# events/letters/relations 的 subject 恒 = actor（门面注入，调用方不可
# 指定）；跨主体的提及进 tags/refs，不动 subject。
HUMAN_PROFILE_SUBJECTS = frozenset({"peiqi"})

# Subdirectories the facade governs, relative to the actor's memory root.
MANAGED_SUBDIRS = (
    "facts", "markdown/events", "markdown/entities",
    "markdown/letters", "shelf",
)

# Paths that exist under memory/ but belong to other mechanisms (P0-3 §一).
_UNMANAGED = ("long-term", "dreams", ".evolution_backups", ".changes")

LAYER_BY_CONTAINER = {
    "events": "event",
    "letters": "letter",
    "entities/people": "person",
    "entities/relations": "relation",
}

# P0-1 §3.2: fixed field order for facts JSONL lines (hash-chain stability).
_FACT_FIELD_ORDER = (
    "seq", "role", "content", "occurred_at", "recorded_at",
    "run_id", "tool_calls", "usage", "prev_hash", "line_hash",
)
_FACT_ROLES = ("user", "assistant", "tool", "system")
_GENESIS_PREV_HASH = "sha256:" + "0" * 64

# WR-15: one line must fit one atomic write (conservative PIPE_BUF).
FACT_LINE_MAX_BYTES = 4096

# WR-07: Windows reserved device names (stem, case-insensitive).
_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_SESS_NAME_RE = re.compile(r"^sess_[0-9a-f]{8}\.jsonl$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# P0-1 §4.3's body template carries a 「## 感受」 section. Gate ④ (感受可
# 下钻到原话) needs that section to be a machine-locatable element rather
# than a paragraph buried in the body, so the facade lifts it out into its
# own facts row and points the element's ``refs`` at it.
#
# The section runs from its own heading to the next heading of the same or
# a higher level (a ``###`` subsection belongs to the 感受 section).
_FEELING_HEADING_RE = re.compile(r"^##\s+感受\s*$", re.M)
_NEXT_SECTION_RE = re.compile(r"^##\s+\S", re.M)

# The facts file the feeling rows live in. One file per day, named like
# every other session file (sess_<8hex>.jsonl) — the rows are grouped so a
# reader can see every feeling of that day in one place, and the row-level
# hash chain runs inside that file like it does everywhere else.
_FEELING_SESSION_ID = "feed0001"

# WR-20: second confirmation window.
CONFIRM_TTL_SECONDS = 600

_CONFIRM_IDENTITY_KEYS = frozenset({"on_behalf_of", "agent_id", "actor", "as_agent"})

_CST = timezone(timedelta(hours=8))


def _utc8_now() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fact_line_hash(line: Dict[str, Any]) -> str:
    """P0-1 §3.3: content hash over the five canonical fields."""
    canonical = "{}|{}|{}|{}|{}".format(
        line["seq"], line["role"], line["content"],
        line["occurred_at"], line["run_id"],
    )
    return "sha256:" + _sha256_hex(canonical)


def _fm_hash(fm: Dict[str, Any]) -> str:
    """WR-12: self-sealing hash over the front-matter minus the hash itself."""
    payload = {k: v for k, v in fm.items() if k != "fm_hash"}
    return "sha256:" + _sha256_hex(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=True)
    )


def _is_reparse_point(path: Path) -> bool:
    """WR-04/RR-02: symlink OR Windows junction. junctions report
    ``islink()==False``; the reparse-point bit is the only reliable signal
    (probed 2026-10-27: mklink /J → islink False, st_file_attributes & 0x400).
    """
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)  # REPARSE
    except OSError:
        return False


# ---------------------------------------------------------------------------
# ConfirmationChannel (P0-3 §四 步骤1 的抽象 — 真实通道形态留给 P0-5)
# ---------------------------------------------------------------------------


class ConfirmationChannel:
    """How a human (peiqi) issues and consumes a release confirmation.

    P0-3 锁死两条安全性质: the confirmation is an independent event with its
    own auditable identity, and agent hearsay ("沛琦说要删") never counts.
    The concrete transport (web console button / chat command) is a P0-5
    decision; this interface is what the facade depends on.
    """

    def issue(self, *, actor: str, element_id: str, summary: str) -> str:
        """Return the consent_ref identifying THIS initiation event."""
        raise NotImplementedError

    def verify_consent(self, consent_ref: str) -> bool:
        """True when consent_ref points at a real, unconsumed consent event."""
        raise NotImplementedError


class RecordingConfirmationChannel(ConfirmationChannel):
    """Reference implementation: consent refs must be pre-registered through
    ``register_consent`` (which stands in for a real interactive event). A
    random, unknown, or agent-fabricated ref never verifies.
    """

    def __init__(self) -> None:
        self._consents: Dict[str, str] = {}

    def register_consent(self, actor: str, element_id: str) -> str:
        ref = "consent_" + hashlib.sha256(
            f"{actor}|{element_id}|{time.time_ns()}".encode()
        ).hexdigest()[:16]
        self._consents[ref] = actor
        return ref

    def issue(self, *, actor: str, element_id: str, summary: str) -> str:
        return self.register_consent(actor, element_id)

    def verify_consent(self, consent_ref: str) -> bool:
        return consent_ref in self._consents


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------


class WriteFacade:
    """Single governed write entry for one actor's live memory.

    ``workspace_root`` is the actor's own root (``agents/<id>`` or the shared
    root for the default agent). ``actor`` is a DI seam for tests; in
    production pass nothing and the runtime identity decides (总纲第 2 条).
    """

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        actor: Optional[str] = None,
        cow_root: Optional[Path | str] = None,
        channel: Optional[ConfirmationChannel] = None,
        clock: Optional[Any] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.memory_root = self.workspace_root / "memory"
        if actor is None:
            from common.runtime_identity import current_identity

            actor = current_identity().agent_id or "default"
        self.actor = actor
        if cow_root is None:
            # agents/<id>/ → agents/ → cow root
            wr = self.workspace_root
            cow_root = wr.parent.parent if wr.parent.name == "agents" else wr
        self.cow_root = Path(cow_root)
        self.channel = channel or RecordingConfirmationChannel()
        # Monotonic clock for the WR-20 600s window; injectable so tests can
        # age a token without sleeping.
        self._clock = clock or time.monotonic
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._consumed_tokens: set[str] = set()
        # The truth layer's release state (P0-14 L-1/L-2): the read exits
        # must ask the truth layer, not the file's own front-matter.
        self._release_cache = None

    # -- audit ------------------------------------------------------------

    def _audit(
        self, action: str, target: str, outcome: str, reason: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one hash-chained audit line. 总纲第 3 条: 拒绝必须出声."""
        audit_dir = self.memory_root / ".audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = audit_dir / "facade.jsonl"
        last_hash = _GENESIS_PREV_HASH
        if path.exists():
            with open(path, "rb") as f:
                lines = f.read().splitlines()
            if lines:
                try:
                    last_hash = json.loads(lines[-1])["line_hash"]
                except (ValueError, KeyError):
                    pass  # broken tail is an inspection finding, not ours
        entry: Dict[str, Any] = {
            "actor": self.actor,
            "action": action,
            "target": target,
            "outcome": outcome,
            "reason": reason,
            "at": _utc8_now(),
            "prev_hash": last_hash,
        }
        if extra:
            entry.update(extra)
        canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        entry["line_hash"] = "sha256:" + _sha256_hex(canonical)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _reject(self, exc: FacadeError, action: str, target: str) -> FacadeError:
        self._audit(action, target, "rejected",
                    f"{type(exc).__name__} ({exc.rule}): {exc}")
        return exc

    # -- path rules WR-01..06 (also RR-01 on read) -------------------------

    def _resolve(self, relpath: str) -> Path:
        """WR-01..06. Returns the real target path or raises.

        Order matters: lexical classification (WR-01/02/05) runs before any
        filesystem touch, so cross-role intent is diagnosed as
        CrossRoleWriteError even when it travels via ``..`` segments, while
        escapes that target non-memory files stay PathEscapeError. (P0-3
        TC-A2/D1/D2 force this split.)
        """
        if not isinstance(relpath, str) or not relpath:
            raise PathEscapeError("path must be a non-empty relative string")

        # WR-01: relative only — no absolute, drive-letter, or UNC forms.
        p = relpath.replace("\\", "/")
        if (os.path.isabs(relpath) or os.path.isabs(p)
                or re.match(r"^[A-Za-z]:[\\/]", relpath)
                or p.startswith("//")):
            raise PathEscapeError(f"absolute/UNC path rejected: {relpath!r}")

        # WR-02: segment-level hygiene. No decoding, no un-obfuscating.
        # Accept both memory-root-relative ("markdown/...") and
        # workspace-root-relative ("memory/markdown/...") forms — internal
        # builders produce the latter, external callers the former.
        if p.startswith("memory/"):
            p = p[len("memory/"):]
        parts = p.split("/")
        if any(seg in (".", "") for seg in parts):
            raise PathEscapeError(f"bad path segment in {relpath!r}")

        # WR-05 (lexical pass): where does this path LAND, segment-wise?
        # Runs before the generic '..' reject so cross-role intent is named
        # precisely (TC-D1/D2), while '..' aimed at non-memory files still
        # reports as PathEscapeError (TC-A1/A2).
        if ".." in parts:
            probe = self.workspace_root
            for seg in parts:
                probe = probe.parent if seg == ".." else probe / seg
            probe = Path(os.path.normpath(probe))
            agents_real = Path(os.path.realpath(self.cow_root / "agents"))
            shared_mem = Path(os.path.realpath(self.cow_root / "memory"))
            try:
                rel_agents = probe.relative_to(agents_real)
                owner = rel_agents.parts[0] if rel_agents.parts else None
                if (owner and owner != self.actor and len(rel_agents.parts) > 1
                        and rel_agents.parts[1] == "memory"):
                    raise CrossRoleWriteError(
                        f"target is inside agent {owner!r}'s memory; actor is "
                        f"{self.actor!r} (WR-05)"
                    )
            except ValueError:
                pass
            if self.actor != "default":
                try:
                    probe.relative_to(shared_mem)
                    raise CrossRoleWriteError(
                        "target is the shared-root memory, which belongs to the "
                        "default agent (WR-05)"
                    )
                except ValueError:
                    pass
            raise PathEscapeError(f"'..' segment in {relpath!r} (WR-02)")

        # WR-04 FIRST: every EXISTING ancestor (and the target itself) must
        # not be a symlink or junction — including dangling ones (lexists,
        # not exists; probed 2026-10-27: dangling junction has
        # islink()==False). Runs before the realpath escape check so a
        # junction-assisted escape reports as the reparse point it is
        # (TC-B1/B2/B3 expect PathEscapeError, not CrossRoleWriteError).
        node = self.memory_root
        for seg in parts:
            node = node / seg
            if os.path.lexists(node) and _is_reparse_point(node):
                raise PathEscapeError(f"reparse point in path: {node}")

        # WR-03: normalized path must stay inside the actor's memory root.
        root_real = Path(os.path.realpath(self.memory_root))
        real = Path(os.path.realpath(self.memory_root.joinpath(*parts)))
        escaped = False
        try:
            escaped = os.path.commonpath([str(real), str(root_real)]) != str(root_real)
        except ValueError:  # cross-drive on Windows
            escaped = True
        if escaped:
            # WR-05 (realpath pass): escaped via junction/symlink into another
            # agent's memory or the shared memory → name it cross-role.
            agents_real = Path(os.path.realpath(self.cow_root / "agents"))
            try:
                rel_agents = real.relative_to(agents_real)
                owner = rel_agents.parts[0] if rel_agents.parts else None
                if (owner and owner != self.actor and len(rel_agents.parts) > 1
                        and rel_agents.parts[1] == "memory"):
                    raise CrossRoleWriteError(
                        f"target resolves into agent {owner!r}'s memory; actor "
                        f"is {self.actor!r} (WR-05)"
                    )
            except ValueError:
                pass
            raise PathEscapeError(f"path escapes memory root: {relpath!r}")

        # WR-06: managed-subdir whitelist. A clean-segment path that lexically
        # addresses ANOTHER agent's memory (only expressible by the default
        # actor, whose root is the shared root) is cross-role, not merely
        # unmanaged (TC-D2).
        first_two = "/".join(parts[:2])
        first = parts[0]
        if (first == "agents" and len(parts) > 2 and parts[2] == "memory"
                and parts[1] != self.actor):
            raise CrossRoleWriteError(
                f"target is agent {parts[1]!r}'s memory; actor is "
                f"{self.actor!r} (WR-05)"
            )
        if first == "shelf":
            raise DisallowedLocationError(
                "shelf/ is projection-only; only the shelf generator writes there"
            )
        if first in _UNMANAGED or first.endswith(".md"):
            raise DisallowedLocationError(
                f"{first!r} belongs to a legacy mechanism, not the facade"
            )
        if first not in ("facts", "markdown"):
            raise DisallowedLocationError(
                f"{first!r} is not in MANAGED_SUBDIRS {MANAGED_SUBDIRS}"
            )
        if first == "markdown" and not first_two.startswith(
                ("markdown/events", "markdown/entities", "markdown/letters")):
            raise DisallowedLocationError(
                f"{first_two!r} is not a governed markdown container"
            )
        if first == "markdown" and len(parts) > 3 and parts[2] == "_tombstones":
            raise AppendOnlyViolationError(
                "tombstones are written only by the release transaction"
            )
        return real

    def _reject_actor_params(self, kwargs: Dict[str, Any]) -> None:
        """总纲第 2 条: identity-claiming call parameters are an escalation
        backdoor — reject them and audit who tried."""
        bad = _CONFIRM_IDENTITY_KEYS & kwargs.keys()
        if bad:
            raise CrossRoleWriteError(
                f"identity override parameter(s) {sorted(bad)} are not honored; "
                f"actor is the runtime identity {self.actor!r}"
            )

    def _sanitize_editable(self, editable: Dict[str, Any],
                           container: str) -> Dict[str, Any]:
        """THE single editable-field filter for every element-creating entry
        (write_event / write_letter / write_entity / revise / _execute_release
        internals). P0-4 fix: before this method existed, only raw ``write()``
        filtered the front-matter ``frontmatter`` dict, while the structured
        entries passed ``**editable`` straight into ``_new_element`` — so
        ``subject``/``status``/``released``/``released_by``/``consent_ref``
        could be forged, and ``_is_human_profile`` (which keys on subject)
        could be cheated into releasing a human profile without WR-19/WR-20.

        One function, one rule set, called from one place (_new_element):
          1. identity-claiming params (总纲第 2 条);
          2. any IMMUTABLE_FM_FIELDS key the caller tried to smuggle in →
             ImmutableFieldError (WR-09). This INCLUDES ``subject`` — callers
             never name a subject; the facade injects actor (or carries the
             old file's subject on revise/release, domain-checked against
             HUMAN_PROFILE_SUBJECTS for people/ profiles). See the
             HUMAN_PROFILE_SUBJECTS comment for the subject value domain.
        Returns a cleaned dict safe for _new_element to consume.
        """
        self._reject_actor_params(editable)
        bad = IMMUTABLE_FM_FIELDS & editable.keys()
        if bad:
            raise ImmutableFieldError(
                f"front-matter fields {sorted(bad)} are facade-injected and may "
                f"not be passed through editable (WR-09); 'edit' of them means "
                f"a new version via revise() / release()"
            )
        return dict(editable)

    # -- naming (WR-07, same algorithm as scripts/memory/validate_names.py) --

    def _check_target_name(self, real: Path, relpath: str) -> None:
        bad = check_relpath(relpath)
        if bad:
            raise PathEscapeError(f"naming: {bad}")
        name = real.name
        stem = real.stem.lower()
        if stem in _RESERVED_STEMS:
            raise PathEscapeError(f"reserved device name: {name!r}")
        if name.endswith(".md"):
            bad = check_name(name)
            if bad:
                raise PathEscapeError(f"naming: {bad}")
        elif name.endswith(".jsonl"):
            if not _SESS_NAME_RE.match(name):
                raise PathEscapeError(
                    f"naming: facts file must be sess_<8hex>.jsonl, got {name!r}"
                )

    # -- low-level governed write -------------------------------------------

    def write(self, relpath: str, content: Optional[str] = None,
              frontmatter: Optional[Dict[str, Any]] = None,
              **kwargs: Any) -> Path:
        """Raw governed write of a markdown truth-layer file.

        Callers normally use write_event/write_entity/revise. ``content=None``
        is delete semantics (TC-E1) and is rejected — the only legal form of
        deletion is release().
        """
        try:
            self._reject_actor_params(kwargs)
            if content is None:
                raise ConsentRequiredError(
                    "delete semantics rejected; the only legal form of deletion "
                    "is release() (WR-17)"
                )
            real = self._resolve(relpath)
            self._check_target_name(real, relpath)
            if real.suffix != ".md":
                raise DisallowedLocationError(
                    "write() is for markdown truth-layer files; "
                    "facts go through append_fact()"
                )
            if frontmatter:
                bad = IMMUTABLE_FM_FIELDS & frontmatter.keys()
                if bad:
                    raise ImmutableFieldError(
                        f"front-matter fields {sorted(bad)} are facade-injected "
                        f"(WR-09)"
                    )
            if real.exists():
                raise TargetExistsError(
                    f"{relpath!r} exists; 'edit' means a new version via revise() "
                    f"(WR-08/WR-11)"
                )
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text(content, encoding="utf-8", newline="\n")
        except FacadeError as e:
            raise self._reject(e, "write", relpath)
        self._audit("write", relpath, "ok", "")
        return real

    # -- facts: append-only JSONL (WR-13..16, P0-1 §3) -----------------------

    def facts_path(self, date: str, session_hex8: str) -> str:
        if not _DATE_RE.match(date):
            raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
        return f"memory/facts/{date}/sess_{session_hex8}.jsonl"

    def append_fact(
        self,
        date: str,
        session_hex8: str,
        *,
        role: str,
        content: str,
        occurred_at: str,
        run_id: str,
        tool_calls: Any = None,
        usage: Any = None,
    ) -> Dict[str, Any]:
        """Append one line to a facts JSONL file with chain + seq checks."""
        relpath = self.facts_path(date, session_hex8)
        try:
            if role not in _FACT_ROLES:
                raise FacadeError(f"role must be one of {_FACT_ROLES}")
            real = self._resolve(relpath)
            self._check_target_name(real, relpath)

            lines: List[Dict[str, Any]] = []
            if real.exists():
                with open(real, encoding="utf-8") as f:
                    for raw in f:
                        raw = raw.strip()
                        if raw:
                            lines.append(json.loads(raw))
            # WR-14 chain self-check against the append-only AUDIT trail, not
            # just the on-disk tail: if the file was externally truncated it
            # still looks internally consistent (P0-1 判定C), so the disk
            # tail cannot be trusted to prove continuity. The audit log
            # remembers the last seq this facade appended.
            known_seq = self._last_appended_seq(relpath)
            disk_seq = lines[-1]["seq"] if lines else 0
            if known_seq is not None and disk_seq != known_seq:
                raise HashChainBreakError(
                    f"facts tail moved: audit knows seq={known_seq}, disk tail "
                    f"is seq={disk_seq} — external truncation/rewrite suspected "
                    f"(WR-14; refusing to launder a broken chain, TC-F3)"
                )
            seq = disk_seq + 1
            prev = lines[-1]["line_hash"] if lines else _GENESIS_PREV_HASH
            line: Dict[str, Any] = {
                "seq": seq,
                "role": role,
                "content": content,
                "occurred_at": occurred_at,
                "recorded_at": _utc8_now(),
                "run_id": run_id,
                "tool_calls": tool_calls,
                "usage": usage,
                "prev_hash": prev,
                "line_hash": "",
            }
            line["line_hash"] = _fact_line_hash(line)
            # Fixed field order so byte-level comparison stays possible.
            line = {k: line[k] for k in _FACT_FIELD_ORDER}
            blob = json.dumps(line, ensure_ascii=False)
            if len(blob.encode("utf-8")) >= FACT_LINE_MAX_BYTES:
                raise FacadeError(
                    f"fact line {len(blob.encode('utf-8'))} bytes >= "
                    f"{FACT_LINE_MAX_BYTES}; split it (WR-15)"
                )
            real.parent.mkdir(parents=True, exist_ok=True)
            with open(real, "a", encoding="utf-8", newline="") as f:
                f.write(blob + "\n")
                f.flush()
                os.fsync(f.fileno())
        except FacadeError as e:
            raise self._reject(e, "append_fact", relpath)
        self._audit("append_fact", relpath, "ok", "", {"seq": line["seq"]})
        return line

    def _last_appended_seq(self, relpath: str) -> Optional[int]:
        """Last seq this facade appended to ``relpath``, per the audit log.
        None when no append is on record."""
        path = self.memory_root / ".audit" / "facade.jsonl"
        if not path.exists():
            return None
        seq: Optional[int] = None
        with open(path, encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if (row.get("action") == "append_fact"
                        and row.get("target") == relpath
                        and row.get("outcome") == "ok"
                        and isinstance(row.get("seq"), int)):
                    seq = row["seq"]
        return seq

    def verify_facts_file(self, relpath: str) -> List[str]:
        """PA-06 / INV-10 hook: re-verify chain, seq continuity, and line
        hashes. Returns a list of violation strings (empty = clean).
        Read-only. Accepts 'facts/...' or 'memory/facts/...'."""
        rel = relpath[len("memory/"):] if relpath.startswith("memory/") else relpath
        real = self.memory_root / rel
        violations: List[str] = []
        lines: List[Dict[str, Any]] = []
        with open(real, encoding="utf-8") as f:
            for i, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    violations.append(f"line {i}: unparseable (tail truncation?)")
        prev = _GENESIS_PREV_HASH
        for expected_seq, line in enumerate(lines, start=1):
            if line.get("seq") != expected_seq:
                violations.append(
                    f"seq break: expected {expected_seq}, got {line.get('seq')}"
                )
            if line.get("prev_hash") != prev:
                violations.append(f"prev_hash break at seq={line.get('seq')}")
            if line.get("line_hash") != _fact_line_hash(line):
                violations.append(f"line_hash mismatch at seq={line.get('seq')}")
            prev = line.get("line_hash", prev)
        return violations

    # -- gate ④: 感受 becomes a locatable element (P0-11 门禁④) ---------------

    @staticmethod
    def _extract_feeling(body: str) -> Optional[str]:
        """The 「## 感受」 section of a body, or None.

        The section runs from its heading to the next ``##`` heading (a
        ``###`` subsection belongs to it). Only the section's own text is
        taken — the heading itself is dropped, because the heading is the
        section's NAME, not its content, and the content is what must land
        in the facts row verbatim.
        """
        if not body:
            return None
        m = _FEELING_HEADING_RE.search(body)
        if not m:
            return None
        start = m.end()
        rest = body[start:]
        nxt = _NEXT_SECTION_RE.search(rest)
        section = rest[:nxt.start()] if nxt else rest
        section = section.strip()
        return section or None

    def _record_feeling(self, body: str, *, date: str) -> Optional[str]:
        """Write the body's 感受 section as its own facts row.

        Returns the feeling's element id, or None when the body has no
        感受 section.

        Why a facts ROW and not a file: the facts layer is the append-only
        evidence room (P0-1 §3), the element contract (``<slug>--<hash8>.md``)
        is for markdown elements, and a feeling is a dimension attached to
        an event — 03 §4.1 "感受必须与事件绑定，不独立成文件". A row in the
        evidence room keeps the 原话 byte-for-byte, keeps the row-level
        hash chain (P0-1 §3.3), and gives the feeling an id that
        ``refs`` can point at (P0-1 §4.1: refs 只指事实层 f_*).

        The id is derived from the section's own text (06 §3.1's
        ``<layer> + sha256(canonical_text)[:20]`` rule), so the same
        感受 always has the same id — the element id is a function of the
        content, never a counter.
        """
        section = self._extract_feeling(body)
        if not section:
            return None
        element_id = feeling_element_id(section)
        # Idempotence: the id is derived from the section's text, so the
        # same 感受 always has the same id. If the day's evidence room
        # already holds it, it is NOT appended again — a revision of an
        # event re-writes the same body, and the evidence room is
        # append-only, so a second copy of the same 原话 would be a
        # duplicate rather than new evidence.
        if self._feeling_row_exists(date, section):
            return element_id
        # The facts row's ``run_id`` carries the layer: P0-1 §3.2 fixes the
        # row at ten fields in a fixed order, so ``layer`` cannot be a field
        # of its own without breaking that contract. ``run_id`` is the one
        # free-form field, and it is part of the line hash, so the layer
        # cannot be changed behind the chain's back.
        line = self.append_fact(
            date, _FEELING_SESSION_ID,
            role="assistant",
            content=section,
            occurred_at=_utc8_now(),
            run_id=feeling_run_id(element_id),
        )
        self._audit("record_feeling", element_id, "ok",
                    f"facts_line={line['seq']}")
        return element_id

    def _feeling_row_exists(self, date: str, section: str) -> bool:
        """True when this day's evidence room already holds this 感受."""
        path = self.memory_root / "facts" / date / f"sess_{_FEELING_SESSION_ID}.jsonl"
        if not path.is_file():
            return False
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except ValueError:
                        continue
                    if str(row.get("content") or "") == section:
                        return True
        except (OSError, UnicodeDecodeError):
            return False
        return False

    # -- markdown truth layer ------------------------------------------------

    def _render(self, fm: Dict[str, Any], body: str) -> str:
        fm["fm_hash"] = _fm_hash(fm)
        dumped = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
        return f"---\n{dumped}---\n\n{body.rstrip()}\n"

    def _new_element(
        self,
        container: str,
        *,
        slug: str,
        title: str,
        body: str,
        editable: Dict[str, Any],
        date: Optional[str] = None,
        revision: Optional[int] = None,
        supersedes: Optional[List[str]] = None,
        version: int = 1,
        status: str = "active",
        released: bool = False,
        released_by: Optional[str] = None,
        consent_ref: Optional[str] = None,
        recorded_at: Optional[str] = None,
        carry: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Path, Dict[str, Any]]:
        """Build fm + file for a new element (or new version of one).

        ``editable`` passes through ``_sanitize_editable`` HERE, at the single
        choke point all four public entries (write_event/write_letter/
        write_entity/revise) and the release machinery funnel into — so no
        caller can reach file creation with unfiltered fields (P0-4 fix).
        """
        editable = self._sanitize_editable(editable, container)
        # ``carry`` is the TRUSTED channel for immutable fields carried
        # forward from an existing on-disk front-matter (revise/release use
        # it; public callers never see it). It bypasses _sanitize_editable by
        # construction — the source is a file the facade itself wrote.
        carry = carry or {}
        subject = carry.get("subject")
        if subject is not None and container == "entities/people":
            # Even the trusted carry channel is domain-checked for people/
            # profiles: the WR-19/WR-20 release gate keys on this value, so
            # it must never be attestable to an agent id.
            if subject not in HUMAN_PROFILE_SUBJECTS:
                raise ImmutableFieldError(
                    f"people/ profile subject must be a recognized human slug "
                    f"{sorted(HUMAN_PROFILE_SUBJECTS)}, got {subject!r}"
                )
        layer = LAYER_BY_CONTAINER[container]
        occurred_at = carry.get("occurred_at") or _utc8_now()
        rec = recorded_at or _utc8_now()
        # WR-10 internal assertions (facade self-checks, a failure = our bug).
        if occurred_at > rec:
            raise FacadeError(
                f"occurred_at {occurred_at} > recorded_at {rec} (WR-10)"
            )
        fm: Dict[str, Any] = {
            "layer": layer,
            "type": carry.get("type") or editable.get("type", "conversation"),
            "title": title,
            "tags": list(editable.get("tags", [])),
            "subject": carry.get("subject") or self.actor,
            "refs": list(editable.get("refs", [])),
            "occurred_at": occurred_at,
            "recorded_at": rec,
            "status": status,
            "version": version,
            "supersedes": list(supersedes or []),
            "superseded_by": None,
            "released": released,
            "released_by": released_by,
            "consent_ref": consent_ref,
            "author_viewpoint": self.actor,
            "density": int(editable.get("density", 3)),
            "visibility": editable.get("visibility", "open"),
        }
        if fm["visibility"] not in ("open", "sealed", "draft"):
            raise FacadeError(f"bad visibility {fm['visibility']!r}")
        # P0-11 门禁④ (写入侧): a body's 感受 section becomes its own facts
        # row, and the element's ``refs`` points at it — so the feeling is
        # a machine-locatable element that can be drilled down to, not a
        # paragraph buried in the body.
        #
        # It happens HERE, in the single choke point every element-creating
        # entry funnels through (write_event / write_letter / write_entity
        # / revise), so no entry can create a 感受 without a ref for it.
        # A released element is exempt: the release transaction re-creates
        # the element with the SAME body, and the feeling row already
        # exists — appending it again would duplicate the 原话 in the
        # append-only evidence room.
        if status != "released":
            feeling_id = self._record_feeling(
                body, date=date or _utc8_now()[:10])
            if feeling_id and feeling_id not in fm["refs"]:
                fm["refs"].append(feeling_id)
        text_for_hash = canonical_text(body)
        fm["body_hash"] = "sha256:" + _sha256_hex(text_for_hash)
        fm["id"] = f"{layer}_{_sha256_hex(self.actor + text_for_hash)[:20]}"
        fm["hash8"] = hashlib.md5(text_for_hash.encode()).hexdigest()[:8]
        fm["slug"] = slug
        rel = build_memory_path(container, slug, body, date=date, revision=revision)
        real = self._resolve(rel)
        self._check_target_name(real, rel)
        if real.exists():
            raise TargetExistsError(f"{rel!r} already exists (WR-08)")
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text(self._render(fm, body), encoding="utf-8", newline="\n")
        self._audit("new_element", rel, "ok", f"id={fm['id']}")
        return real, fm

    def write_event(self, slug: str, title: str, body: str,
                    date: Optional[str] = None, **editable: Any) -> Path:
        """Create an event. ``editable`` carries ONLY caller-editable fields
        (title/tags/refs/density/visibility); immutable fm fields (subject,
        status, released, ...) are rejected by the shared _sanitize_editable
        filter inside _new_element (P0-4 fix). subject always = actor."""
        date = date or _utc8_now()[:10]
        try:
            real, _ = self._new_element(
                "events", slug=slug, title=title, body=body,
                editable=editable, date=date)
        except FacadeError as e:
            raise self._reject(e, "write_event", f"events/{date}/{slug}")
        return real

    def write_letter(self, slug: str, title: str, body: str,
                     date: Optional[str] = None, **editable: Any) -> Path:
        date = date or _utc8_now()[:10]
        try:
            real, _ = self._new_element(
                "letters", slug=slug, title=title, body=body,
                editable=editable, date=date)
        except FacadeError as e:
            raise self._reject(e, "write_letter", f"letters/{date}/{slug}")
        return real

    def write_entity(self, kind: str, slug: str, title: str, body: str,
                     *, subject: Optional[str] = None,
                     **editable: Any) -> Path:
        """Create a person/relation profile.

        ``subject`` is a dedicated, audited parameter (not part of the
        free-form editable dict) and only meaningful for people/ profiles,
        where it MUST be in HUMAN_PROFILE_SUBJECTS — an agent id or an
        unknown human slug is rejected. Default subject = actor.
        """
        if kind not in ("people", "relations"):
            raise ValueError("kind must be 'people' or 'relations'")
        try:
            if subject is not None:
                if kind != "people" or subject not in HUMAN_PROFILE_SUBJECTS:
                    raise ImmutableFieldError(
                        f"entity subject must be in "
                        f"{sorted(HUMAN_PROFILE_SUBJECTS)} for people/ "
                        f"profiles (got {subject!r}); agents never own "
                        f"people/ profiles and relations/ subject = actor"
                    )
            real, _ = self._new_element(
                f"entities/{kind}", slug=slug, title=title, body=body,
                editable=editable,
                carry={"subject": subject} if subject else None)
        except FacadeError as e:
            raise self._reject(e, "write_entity", f"entities/{kind}/{slug}")
        return real

    # -- read with re-verification (RR-01..04) --------------------------------

    def _owner_tokens(self) -> set:
        """The identity tokens this facade speaks for.

        A facade is constructed over ONE agent's own memory root, so the
        reads it serves are 本视角 (that viewpoint) — and, through it, the
        主人格 who reads that agent's memory. The cross-view case (the
        master reading ANOTHER agent's dark page) is the borrowing desk,
        which is not implemented yet.
        """
        tokens = set()
        for candidate in (self.actor, self.workspace_root.name):
            token = agent_owner_token(candidate)
            if token:
                tokens.add(token)
        return tokens

    def _state(self):
        """The truth layer's release state (the authority for reads).

        Cached behind its own signature, so a repeated read does not
        re-read every markdown file.
        """
        if self._release_cache is None:
            from agent.memory.release_state import ReleaseStateCache
            self._release_cache = ReleaseStateCache(self.workspace_root)
        return self._release_cache.get()

    @staticmethod
    def _state_key(relpath: str) -> str:
        """The truth-layer key of a path, the way the state speaks it."""
        key = str(relpath or "").replace("\\", "/").lstrip("/")
        if not key.startswith("memory/"):
            key = "memory/" + key
        return key

    def read(self, relpath: str) -> Tuple[Dict[str, Any], str]:
        """Read a truth-layer file with RR-01/02/03/04 re-verification.

        Raises PathEscapeError (RR-01/02), FrontmatterTamperError (RR-03).
        A released element is not returned as trusted memory (RR-04): the
        caller gets an empty result, and no 'filtered N items' signal.

        P0-14 L-2: a DARK PAGE (``visibility: sealed``) is NOT a release.
        Its body is open to its owner and closed to everybody else, so a
        read that does not hold the owner's token gets the placeholder +
        access entry (06 §B1) — never the body, never a count of what was
        withheld.

        The release state — not the file's own front-matter — decides
        whether an element was released: a released element keeps its old
        bytes (WR-11), so the original version still says ``status: active``
        and only the tombstone knows the truth.
        """
        real = self._resolve(relpath)
        if not real.is_file():
            raise FileNotFoundError(relpath)
        text = real.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        if fm:
            stored = fm.get("fm_hash")
            if stored and stored != _fm_hash(fm):
                e = FrontmatterTamperError(
                    f"fm_hash mismatch in {relpath!r} (RR-03); file was "
                    f"modified outside the facade"
                )
                raise self._reject(e, "read", relpath)
            kind = classify_frontmatter(fm)
            if kind == VIS_RELEASED:
                return {}, ""  # RR-04: released leaves the trusted result set
            if kind == VIS_SEALED:
                owner = owner_token(fm)
                if owner and owner not in self._owner_tokens():
                    return {}, SEALED_PLACEHOLDER
        # The tombstone knows what the file's own bytes cannot say.
        try:
            state = self._state()
        except Exception as e:
            from agent.memory.read_audit import report_read_denied
            report_read_denied(self.workspace_root, "facade.read",
                               self._state_key(relpath), e)
            raise FacadeError(
                f"the release state could not be resolved ({e}); refusing to "
                f"serve the file (fail closed).") from e
        decision, _reason, mask = state.decide(
            self._state_key(relpath), self._owner_tokens())
        if decision == READ_RELEASED:
            return {}, ""
        if decision == READ_SEALED:
            return {}, SEALED_PLACEHOLDER
        # P0-16 F-1: the evidence room is append-only, so the 感受 rows of
        # released elements are masked in the READING VIEW — the disk keeps
        # the 原话 and the hash chain stays unbroken.
        from agent.memory.facts_mask import is_facts_path, mask_facts_text
        if mask and is_facts_path(self._state_key(relpath)):
            body = mask_facts_text(body, mask)
        return fm, body

    # -- revise = new version, old bytes untouched (WR-11) ---------------------

    def revise(self, old_relpath: str, *, body: str,
               revision: Optional[int] = None,
               **editable: Any) -> Path:
        """'Edit' = a new version file; old bytes untouched (WR-11).

        P0-4 fix: immutable fm fields (subject/status/released/...) are
        carried forward from the OLD front-matter by explicit whitelist — the
        caller's ``editable`` may only hold caller-editable fields and goes
        through the same _sanitize_editable filter as every other entry. A
        revise() that tries to rename the subject (同名变体攻击) is rejected.
        """
        try:
            old_real = self._resolve(old_relpath)
            if not old_real.is_file():
                raise FileNotFoundError(old_relpath)
            before = old_real.read_bytes()
            text = before.decode("utf-8")
            fm, _old_body = split_frontmatter(text)
            if not fm:
                raise FacadeError(f"{old_relpath!r} has no front-matter to revise")
            parts = old_relpath.replace("\\", "/").split("/")
            if parts and parts[0] == "memory":
                parts = parts[1:]
            container = "/".join(parts[1:-2] if _DATE_RE.match(parts[-2])
                                 else parts[1:-1])
            date = parts[-2] if _DATE_RE.match(parts[-2]) else None
            slug = fm.get("slug") or old_real.name.split("--")[0]
            rev = revision or int(fm.get("version", 1)) + 1
            # Carry-forward whitelist: these come from the old file, NEVER
            # from caller kwargs. Everything in the caller's dict that is not
            # caller-editable (title/tags/refs/density/visibility) raises
            # ImmutableFieldError inside _sanitize_editable.
            carried = {k: fm.get(k) for k in ("tags", "refs", "density",
                                              "visibility")}
            caller_editable = dict(editable)  # caller may override the above
            caller_editable.setdefault("title", fm.get("title", slug))
            merged = {**carried, **caller_editable}
            new_real, _fm = self._new_element(
                container, slug=slug,
                title=merged.pop("title", fm.get("title", slug)),
                body=body, editable=merged,
                carry={"type": fm.get("type"), "subject": fm.get("subject"),
                       "occurred_at": fm.get("occurred_at")},
                date=date, revision=rev if rev > 1 else None,
                supersedes=[fm["id"]], version=int(fm.get("version", 1)) + 1,
            )
            if old_real.read_bytes() != before:  # WR-11: old bytes untouched
                raise AppendOnlyViolationError("old version file was modified!")
        except FacadeError as e:
            raise self._reject(e, "revise", old_relpath)
        return new_real

    # -- release (WR-17..21, P0-3 §四) ----------------------------------------

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """WR-17: there is no delete. The only legal form is release()."""
        e = ConsentRequiredError(
            "delete/unlink/truncate is not exposed by the facade; "
            "the only legal form of deletion is release()"
        )
        raise self._reject(e, "delete", str(args[:1]))

    unlink = delete
    remove = delete
    truncate = delete

    def _target_fm(self, relpath: str) -> Tuple[Path, Dict[str, Any], str]:
        real = self._resolve(relpath)
        text = real.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        if not fm:
            raise FacadeError(f"{relpath!r} has no front-matter")
        return real, fm, body

    def _is_human_profile(self, relpath: str, fm: Dict[str, Any]) -> bool:
        if fm.get("layer") != "person":
            return False
        subject = fm.get("subject") or ""
        agents_dir = self.cow_root / "agents"
        agent_ids = {d.name for d in agents_dir.iterdir() if d.is_dir()} \
            if agents_dir.is_dir() else set()
        return subject not in agent_ids  # WR-19: person 且非 agent

    def initiate_release(
        self, relpath: str, *, consent_ref: Optional[str]
    ) -> Dict[str, Any]:
        """Step 1-2 of P0-3 §四. For human profiles this NEVER mutates state;
        it returns a confirmation token + human-readable impact summary."""
        try:
            real, fm, _body = self._target_fm(relpath)
            # WR-18: consent_ref mandatory and must point at a real consent event.
            if not consent_ref or not self.channel.verify_consent(consent_ref):
                raise ConsentRequiredError(
                    "release requires a consent_ref that points at a real "
                    "consent event (WR-18)"
                )
            subject = fm.get("subject") or ""
            human = self._is_human_profile(relpath, fm)
            # WR-19: initiation rights.
            if human:
                if self.actor != "peiqi":
                    raise ConsentRequiredError(
                        f"human profile release is peiqi-only; actor is "
                        f"{self.actor!r} (WR-19)"
                    )
            elif subject != self.actor:
                raise ConsentRequiredError(
                    f"only the memory owner may initiate release; subject is "
                    f"{subject!r}, actor is {self.actor!r} (WR-19)"
                )
            if classify_frontmatter(fm) == VIS_RELEASED:
                raise FacadeError("already released")
            if not human:
                # Own memory: release executes immediately (no WR-20).
                result = self._execute_release(relpath, real, fm, consent_ref,
                                               initiated_at=_utc8_now(),
                                               confirmed_at=_utc8_now(),
                                               token_hash=None)
                self._audit("release", relpath, "ok",
                            f"consent_ref={consent_ref}")
                return {"executed": True, **result}
            # Human profile: issue a token, change nothing on disk (WR-20).
            initiated_at = _utc8_now()
            file_hash = _sha256_hex(real.read_text(encoding="utf-8"))
            nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:8]
            token = _sha256_hex(
                f"{fm['id']}|{file_hash}|{initiated_at}|{nonce}")[:16]
            self._pending[token] = {
                "relpath": relpath, "element_id": fm["id"],
                "file_hash": file_hash, "initiated_at": initiated_at,
                "initiated_monotonic": self._clock(),
                "consent_ref": consent_ref,
            }
            summary = (
                f"目标档案: {relpath}（{fm.get('title')}）\n"
                f"将发生三件事: ① 追加释放事件 ② status→released（新版本，"
                f"旧版本字节不动） ③ 落墓碑。\n"
                f"本架构下释放 ≠ 全历史抹除：备份、快照、聊天记录里可能仍有副本。"
                f"本操作是正源层释放 + 永久留痕。\n"
                f"确认方式: 600 秒内以引用 token 的独立消息确认。token: {token}"
            )
            self._audit("release_initiated", relpath, "pending",
                        f"token_hash={_sha256_hex(token)[:16]}")
            return {"executed": False, "token": token, "summary": summary}
        except FacadeError as e:
            raise self._reject(e, "release_initiate", relpath)

    def confirm_release(self, token: Optional[str]) -> Dict[str, Any]:
        """Step 3-4 of P0-3 §四. Token: exists, fresh (<=600s), unconsumed,
        single-use."""
        if not token or token in self._consumed_tokens or token not in self._pending:
            e = SecondConfirmRequiredError(
                "confirmation must reference a live, unconsumed token (WR-20)"
            )
            raise self._reject(e, "release_confirm", token or "<none>")
        pend = self._pending[token]
        if self._clock() - pend["initiated_monotonic"] > CONFIRM_TTL_SECONDS:
            del self._pending[token]
            e = ConfirmationExpiredError(
                f"confirmation arrived after {CONFIRM_TTL_SECONDS}s (WR-20)"
            )
            raise self._reject(e, "release_confirm", pend["relpath"])
        del self._pending[token]
        self._consumed_tokens.add(token)
        real, fm, _body = self._target_fm(pend["relpath"])
        result = self._execute_release(
            pend["relpath"], real, fm, pend["consent_ref"],
            initiated_at=pend["initiated_at"], confirmed_at=_utc8_now(),
            token_hash=_sha256_hex(token),
        )
        self._audit("release", pend["relpath"], "ok",
                    f"consent_ref={pend['consent_ref']}",
                    {"token_hash": _sha256_hex(token)[:16]})
        return {"executed": True, **result}

    def _execute_release(
        self, relpath: str, real: Path, fm: Dict[str, Any], consent_ref: str,
        *, initiated_at: str, confirmed_at: str, token_hash: Optional[str],
    ) -> Dict[str, str]:
        """WR-21: the three layers land together or not at all.

        ① release event appended (events/<today>/release-<slug>--<hash8>.md)
        ② target element new version with status=released (WR-11, old untouched)
        ③ tombstone under markdown/_tombstones/
        Any step failing rolls the previous ones back (files are new files,
        so rollback = unlink what this transaction created).
        """
        created: List[Path] = []
        old_bytes = real.read_bytes()
        try:
            today = _utc8_now()[:10]
            # ① release event
            slug = fm.get("slug") or real.name.split("--")[0]
            ev_body = (
                f"# 释放：{fm.get('title', slug)}\n\n"
                f"> **一句话**：{self.actor} 释放了 {relpath}。\n\n"
                f"## 发生了什么\n\n- 释放对象: `{relpath}`\n"
                f"- 释放者: {self.actor}\n- 同意来源: `{consent_ref}`\n"
                f"- 发起: {initiated_at} / 确认: {confirmed_at}\n\n"
                f"## 审计\n\n| 字段 | 值 |\n|:---|:---|\n"
                f"| 写入 | `{self.actor}` @ {_utc8_now()} |\n"
                f"| 释放 | 是 |\n"
            )
            ev_real, _ = self._new_element(
                "events", slug=f"release-{slug}", title=f"释放：{fm.get('title')}",
                body=ev_body, date=today,
                editable={"tags": ["release"], "visibility": "sealed"},
                carry={"type": "release", "subject": fm.get("subject")},
            )
            created.append(ev_real)
            # ② target new version with status=released
            parts = relpath.replace("\\", "/").split("/")
            if parts and parts[0] == "memory":
                parts = parts[1:]
            container = "/".join(parts[1:-2] if _DATE_RE.match(parts[-2])
                                 else parts[1:-1])
            date = parts[-2] if _DATE_RE.match(parts[-2]) else None
            _orig_text, orig_body = None, split_frontmatter(
                real.read_text(encoding="utf-8"))[1]
            rev_real, rev_fm = self._new_element(
                container, slug=slug,
                title=fm.get("title", slug), body=orig_body,
                editable={"tags": fm.get("tags", []),
                          "refs": fm.get("refs", []),
                          "density": fm.get("density", 3),
                          "visibility": "sealed"},
                carry={"type": fm.get("type"), "subject": fm.get("subject"),
                       "occurred_at": fm.get("occurred_at")},
                date=date,
                revision=int(fm.get("version", 1)) + 1,
                supersedes=[fm["id"]], version=int(fm.get("version", 1)) + 1,
                status="released", released=True, released_by=self.actor,
                consent_ref=consent_ref,
            )
            created.append(rev_real)
            # ③ tombstone
            # P0-10 L-2: the tombstone is NOT a memory element, so it does
            # not get the <slug>--<hash8>.md element contract — but it does
            # get a stricter one of its own (validate_names.py
            # _TOMBSTONE rules): <原元素名>.tombstone.md, where the original
            # element name must itself satisfy the element naming rule, and
            # the file must carry all 8 audit fields. `real.stem` drops the
            # original .md so the name reads <slug>--<hash8>.tombstone.md
            # instead of <slug>--<hash8>.md.tombstone.md.
            tomb_dir = self.memory_root / "markdown" / "_tombstones"
            tomb_dir.mkdir(parents=True, exist_ok=True)
            tomb = tomb_dir / f"{real.stem}.tombstone.md"
            tomb.write_text(
                "---\n"
                f"original_path: {relpath}\n"
                f"original_hash: sha256:{_sha256_hex(old_bytes.decode('utf-8'))}\n"
                f"released_by: {self.actor}\n"
                f"released_at: {confirmed_at}\n"
                f"consent_ref: {consent_ref}\n"
                f"confirmation_token_hash: "
                f"{('sha256:' + token_hash) if token_hash else 'null'}\n"
                f"initiated_at: {initiated_at}\n"
                f"confirmed_at: {confirmed_at}\n"
                f"subject: {fm.get('subject')}\n"
                f"---\n\n墓碑：原档案已释放。本文件永久保留、不可被释放。\n",
                encoding="utf-8", newline="\n")
            created.append(tomb)
        except Exception:
            for c in created:  # WR-21: all three or nothing
                try:
                    c.unlink()
                except OSError:
                    pass
            raise
        if real.read_bytes() != old_bytes:
            raise AppendOnlyViolationError("released file bytes changed (WR-11)")
        return {"event": str(created[0]), "new_version": str(created[1]),
                "tombstone": str(created[2])}

    # -- shelf generator (P0-5 landed: agent/memory/shelf.py) -----------------

    def generate_shelf(self) -> Dict[str, Any]:
        """Rebuild this workspace's shelf/ projection.

        P0-10 L-3: this used to be a NotImplementedError stub left over from
        P0-4 ("the generator that lands in P0-5 will own this"). P0-5 has
        landed, so a stub that raises is a landmine: anyone calling it gets
        an exception instead of a directory card. It is now a thin wrapper
        over the real generator.

        The import is local on purpose: shelf.py imports from facade.py
        (split_frontmatter), so a module-level import here would be a cycle.
        """
        from agent.memory.shelf import ShelfGenerator
        generator = ShelfGenerator(self.workspace_root, agent_id=self.actor)
        return generator.rebuild()


# ``split_frontmatter`` is imported from agent.memory.frontmatter at the top
# of this module and re-exported here: it used to be DEFINED here, but the
# release-state resolver needs to read front-matter too, and importing the
# facade from that resolver would make the two modules circular. Every
# existing importer (``from agent.memory.facade import split_frontmatter``)
# keeps working.
