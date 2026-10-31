"""P2-1 · L2 人格公共层的判据。

**这一组判据回答的是一个问题**：各角色的 `AGENT.md` 是不是**引用**了
人格公共层，而不是**抄**了一份。

**为什么它是判据而不是散文**：抄 N 份 = 迟早分叉 —— 这是本团队在
`RULE.md` 上已经吃过一次的亏（所以才有「改根文件 + 逐字节同步到所有
副本」的规矩）。一个人格公共层如果只是「多了一个目录」，它就会变成
第 16 个会分叉的地方。

**四条判据**：

| # | 名称 | 断言 |
|:--|:--|:--|
| P2-1a | L2 目录存在且三份文件非空 | 三份文件都在、有实质内容、有可机读的元数据 |
| P2-2a | 各 `AGENT.md` **引用**而不**复制** | 角色文件里**不再出现** persona 的特征句 |
| P2-2b | persona 指纹在所有引用方之间一致 | 每个引用方记录的指纹 == 实际指纹 |
| P2-3a | persona 层本身**可判定**（不是散文） | 元数据可被独立读出并复算 |

**NOTRUN 不是 GREEN**：没有 persona 目录 ⇒ 全部 NOTRUN。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: The persona layer's directory name, relative to the shared root.
PERSONA_DIRNAME = "persona"

#: The three files the layer is made of.
PERSONA_FILES = ("identity.md", "contract.md", "continuity.md")

#: The metadata file — the layer's own, machine-readable, version stamp.
PERSONA_META = "PERSONA.meta.json"

#: The marker the referencing files carry.  The line after it is the
#: fingerprint the referrer believes it is pointing at.
PERSONA_MARKER = "<!-- COW-PERSONA:"

#: The feature sentences used to detect a COPY are **derived from the
#: layer's own text**, not written here.
#:
#: **Why not hard-coded.**  A hard-coded list couples the criterion to
#: one version of one layer: when the layer's wording changes, the
#: sentences stop matching and the criterion reports NOTRUN — or, worse,
#: it keeps checking sentences the layer no longer contains and reports
#: PASS for a layer that was copied wholesale.  (That is the
#: "criterion and implementation share a source ⇒ always green" trap in
#: a new costume.)
#:
#: So the sentences come from the layer, and this module only decides
#: **how** to pick them: long enough to be distinctive, and drawn from
#: every file of the layer so that a copy of any one of them is caught.
_FEATURE_MIN_LEN = 14
_FEATURE_PER_FILE = 3


def _feature_sentences(layer_text: str) -> List[str]:
    """Distinctive sentences taken FROM the layer's own text."""
    out: List[str] = []
    for raw in layer_text.splitlines():
        s = raw.strip().lstrip("#").lstrip(">").strip()
        if not s:
            continue
        # 一句 = 以句号/问号/叹号结尾，或整行（列表项、表格行）
        for piece in re.split(r"(?<=[。！？])", s):
            piece = piece.strip()
            if len(piece) < _FEATURE_MIN_LEN:
                continue
            if piece in out:
                continue
            out.append(piece)
            if len(out) >= _FEATURE_PER_FILE:
                break
        if len(out) >= _FEATURE_PER_FILE:
            break
    return out


def measure_p2_2a_referring_not_copying(root: Path) -> Dict[str, Any]:
    """P2-2a: the role files REFERENCE the layer; they do not COPY it.

    **The feature sentences are taken from the layer's own text** — so
    this criterion cannot drift from the layer, and a copy that slips
    back in is caught by a sentence that actually exists.
    """
    pdir = persona_dir(root)
    out: Dict[str, Any] = {
        "criterion": "P2-2a",
        "name": "各 AGENT.md 引用人格公共层而不复制它",
        "verdict": "NOTRUN",
        "detail": "",
        "offenders": {},
        "checked": 0,
    }
    if not pdir.is_dir():
        out["detail"] = "the persona layer does not exist"
        return out
    features: List[str] = []
    for n in PERSONA_FILES:
        text = _read(pdir / n)
        if not text:
            continue
        got = _feature_sentences(text)
        features.extend(got[:_FEATURE_PER_FILE])
        if len(features) >= _FEATURE_PER_FILE * len(PERSONA_FILES):
            break
    if not features:
        out["detail"] = ("no feature sentence could be derived from the "
                         "layer's own text — the layer may be too thin to "
                         "distinguish a copy of it")
        return out
    offenders: Dict[str, List[str]] = {}
    checked = 0
    for f in _referrers(root):
        checked += 1
        text = _read(f)
        hits = [s for s in features if s in text]
        if hits:
            offenders[f.name] = hits
    out["offenders"] = offenders
    out["checked"] = checked
    if offenders:
        out["verdict"] = "FAIL"
        out["detail"] = ("%d of %d file(s) still COPY the layer's text "
                         "instead of referencing it: %s"
                         % (len(offenders), checked,
                            ", ".join("%s(%d 句)" % (k, len(v))
                                      for k, v in offenders.items())))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("%d 份 AGENT.md 均未复制公共层的正文（特征句 %d 条，"
                     "取自层自身）" % (checked, len(features)))
    return out


def shared_root() -> Path:
    """The shared root — where the persona layer lives.

    ``COW_SHARED_ROOT`` wins when it is set; otherwise the module's
    own ``~/cow`` neighbourhood is used.  **The criterion must not
    hard-code one machine's path** (it would be permanently red
    everywhere else), so the environment variable is the first
    choice and the fallback is derived from this file's location.
    """
    env = os.environ.get("COW_SHARED_ROOT")
    if env:
        return Path(env)
    # <root>/cow/<something>/<this file> -> <root>/cow
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "cow":
            return parent
    return Path.home() / "cow"


def persona_dir(root: Optional[Path] = None) -> Path:
    return (root or shared_root()) / PERSONA_DIRNAME


def persona_fingerprint(root: Optional[Path] = None) -> Tuple[str, Dict[str, str]]:
    """The layer's fingerprint and its per-file hashes, recomputed.

    **Recomputed, never read from the metadata file** — the metadata
    file is a CLAIM, this is the truth.  (Same discipline as the
    chain's hashes: the file's self-reported hash is a claim.)
    """
    pdir = persona_dir(root)
    hashes: Dict[str, str] = {}
    for name in PERSONA_FILES:
        f = pdir / name
        if not f.is_file():
            continue
        hashes[name] = hashlib.sha256(f.read_bytes()).hexdigest()
    if not hashes:
        return "", hashes
    fp = hashlib.sha256(
        "".join("%s:%s\n" % (k, hashes[k]) for k in sorted(hashes)).encode("utf-8")
    ).hexdigest()
    return fp, hashes


def _referrers(root: Path) -> List[Path]:
    """Every ``AGENT.md`` that is expected to REFERENCE the layer."""
    out: List[Path] = []
    agents = root / "agents"
    if agents.is_dir():
        for d in sorted(agents.iterdir()):
            f = d / "AGENT.md"
            if f.is_file():
                out.append(f)
    root_agent = root / "AGENT.md"
    if root_agent.is_file():
        out.append(root_agent)
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def measure_p2_1a_the_layer_exists(root: Path) -> Dict[str, Any]:
    """P2-1a: the L2 directory exists and its three files are real.

    NOTRUN (not PASS) when there is no persona directory: a layer that
    was never built is not a layer that passed.
    """
    pdir = persona_dir(root)
    out: Dict[str, Any] = {
        "criterion": "P2-1a",
        "name": "L2 人格公共层存在且三份文件非空",
        "verdict": "NOTRUN",
        "detail": "",
        "files": {},
        "root": str(pdir),
    }
    if not pdir.is_dir():
        out["detail"] = ("the persona layer %s does not exist — this "
                         "workspace has no L2" % pdir)
        return out
    missing = []
    thin = []
    for name in PERSONA_FILES:
        f = pdir / name
        if not f.is_file():
            missing.append(name)
            continue
        text = _read(f)
        n = len(text.strip())
        out["files"][name] = n
        if n < 200:
            thin.append("%s(%d 字符)" % (name, n))
    if missing:
        out["verdict"] = "FAIL"
        out["detail"] = "missing file(s): %s" % ", ".join(missing)
        return out
    if thin:
        out["verdict"] = "FAIL"
        out["detail"] = "too thin to be a layer: %s" % ", ".join(thin)
        return out
    out["verdict"] = "PASS"
    out["detail"] = "三份文件齐备：%s" % ", ".join(
        "%s=%d 字符" % (k, v) for k, v in out["files"].items())
    return out


def measure_p2_3a_the_layer_is_judgeable(root: Path) -> Dict[str, Any]:
    """P2-3a: the layer carries machine-readable metadata, and the
    fingerprint it claims is the one you get by recomputing.

    **The claim is checked against the truth**, not accepted: the
    metadata file's fingerprint must equal the recomputed one.
    """
    pdir = persona_dir(root)
    out: Dict[str, Any] = {
        "criterion": "P2-3a",
        "name": "人格公共层可判定（自带版本与指纹，且指纹复算一致）",
        "verdict": "NOTRUN",
        "detail": "",
        "claimed": "",
        "actual": "",
    }
    meta_path = pdir / PERSONA_META
    if not pdir.is_dir():
        out["detail"] = "the persona layer does not exist"
        return out
    if not meta_path.is_file():
        out["detail"] = ("the layer has no %s — a layer whose version cannot "
                         "be read cannot be referenced *at a version*"
                         % PERSONA_META)
        return out
    try:
        meta = json.loads(_read(meta_path))
    except Exception as e:
        out["detail"] = "%s is not readable JSON: %s" % (PERSONA_META, e)
        return out
    claimed = str(meta.get("fingerprint") or "")
    actual, _hashes = persona_fingerprint(root)
    out["claimed"] = claimed
    out["actual"] = actual
    if not claimed:
        out["detail"] = "%s carries no fingerprint" % PERSONA_META
        return out
    if not actual:
        out["detail"] = "the layer's files do not hash to anything (empty?)"
        return out
    if claimed != actual:
        out["verdict"] = "FAIL"
        out["detail"] = ("the metadata claims fingerprint %s but the files "
                         "hash to %s — the metadata is stale, or a file was "
                         "edited without updating it" % (claimed[:16], actual[:16]))
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("version=%s, fingerprint=%s（复算一致）"
                     % (meta.get("version"), actual[:16]))
    return out


def measure_p2_2b_the_fingerprint_agrees_everywhere(root: Path) -> Dict[str, Any]:
    """P2-2b: every referrer records the SAME fingerprint as the layer.

    **The fingerprint a referrer records is a CLAIM**; it is compared
    with the recomputed one.  A referrer that points at a stale layer
    is exactly the drift this layer exists to prevent.
    """
    pdir = persona_dir(root)
    out: Dict[str, Any] = {
        "criterion": "P2-2b",
        "name": "persona 指纹在所有引用方之间一致",
        "verdict": "NOTRUN",
        "detail": "",
        "actual": "",
        "mismatched": {},
        "missing": [],
        "checked": 0,
    }
    if not pdir.is_dir():
        out["detail"] = "the persona layer does not exist"
        return out
    actual, _hashes = persona_fingerprint(root)
    if not actual:
        out["detail"] = "the layer's files do not hash to anything"
        return out
    out["actual"] = actual
    mismatched: Dict[str, str] = {}
    missing = []
    checked = 0
    for f in _referrers(root):
        checked += 1
        text = _read(f)
        m = re.search(re.escape(PERSONA_MARKER) + r"[^\n]*?([0-9a-f]{64})", text)
        if not m:
            missing.append(f.name)
            continue
        if m.group(1) != actual:
            mismatched[f.name] = m.group(1)
    out["mismatched"] = mismatched
    out["missing"] = missing
    out["checked"] = checked
    if mismatched or missing:
        out["verdict"] = "FAIL"
        parts = []
        if mismatched:
            parts.append("%d 份记的指纹与实际不符" % len(mismatched))
        if missing:
            parts.append("%d 份没有记录指纹（没引用这一层）" % len(missing))
        out["detail"] = ("；".join(parts) + "。实际指纹 %s"
                         % actual[:16])
        return out
    out["verdict"] = "PASS"
    out["detail"] = ("%d 份引用方记录的指纹一致（%s）"
                     % (checked, actual[:16]))
    return out


CRITERION_IDS = ("P2-1a", "P2-2a", "P2-2b", "P2-3a")


def judge_p2_family(root: Optional[Path] = None) -> Dict[str, Any]:
    """All four, over one workspace."""
    root = root or shared_root()
    reports = []
    verdict = "PASS"
    for fn in (measure_p2_1a_the_layer_exists,
               measure_p2_2a_referring_not_copying,
               measure_p2_2b_the_fingerprint_agrees_everywhere,
               measure_p2_3a_the_layer_is_judgeable):
        rep = fn(root)
        reports.append(rep)
        if rep["verdict"] == "FAIL":
            verdict = "FAIL"
        elif rep["verdict"] == "NOTRUN" and verdict == "PASS":
            verdict = "NOTRUN"
    return {
        "group": "P2-1",
        "title": "L2 人格公共层",
        "root": str(root),
        "verdict": verdict,
        "reports": reports,
    }


# ---------------------------------------------------------------------------
# The harness contract: judge_*(report, workspace) -> report.judge(...)
#
# **The logic lives in ``measure_*``** (above); these only book the
# verdict.  Keeping them apart is what lets the tests and the mutant
# replay call the measurement directly, without inventing a Report.
# ---------------------------------------------------------------------------

def _book(report, m: Dict[str, Any]):
    """Record one measurement in the report."""
    name = m.get("name") or m.get("criterion") or "?"
    detail = m.get("detail") or ""
    if m.get("verdict") == "NOTRUN":
        return report.judge_notrun(m.get("criterion", "?"), name, detail)
    return report.judge(m.get("criterion", "?"), name,
                        m.get("verdict") == "PASS", detail)


def judge_p2_1a_the_layer_exists(report, workspace) -> bool:
    """P2-1a: the L2 directory exists and its three files are real."""
    return _book(report, measure_p2_1a_the_layer_exists(Path(workspace)))


def judge_p2_2a_referring_not_copying(report, workspace) -> bool:
    """P2-2a: the role files REFERENCE the layer; they do not COPY it."""
    return _book(report, measure_p2_2a_referring_not_copying(Path(workspace)))


def judge_p2_2b_the_fingerprint_agrees_everywhere(report, workspace) -> bool:
    """P2-2b: every referrer records the SAME fingerprint as the layer."""
    return _book(report,
                 measure_p2_2b_the_fingerprint_agrees_everywhere(Path(workspace)))


def judge_p2_3a_the_layer_is_judgeable(report, workspace) -> bool:
    """P2-3a: the layer carries metadata, and its fingerprint recomputes."""
    return _book(report, measure_p2_3a_the_layer_is_judgeable(Path(workspace)))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json as _json

    ap = argparse.ArgumentParser(
        description="Judge the P2-1 L2 persona layer of a workspace.")
    ap.add_argument("--root", default=None,
                    help="the shared root (default: $COW_SHARED_ROOT or ~/cow)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else shared_root()
    rep = judge_p2_family(root)
    if args.json:
        print(_json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("P2-1 · L2 人格公共层  —— %s" % rep["root"])
        for r in rep["reports"]:
            print("  [%-7s] %-12s %s" % (r["verdict"], r["criterion"], r["name"]))
            print("           %s" % r["detail"])
        print("VERDICT: %s" % rep["verdict"])
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
