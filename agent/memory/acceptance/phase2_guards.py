#!/usr/bin/env python3
# encoding:utf-8
"""P1-阶段三 · S3-E · the criteria that GUARD the criteria.

A criterion is a claim about the world.  A criterion about criteria is a
claim about the CLAIMS.  Without the second kind, the first kind decays
silently: the three-state harness gets replaced by a two-state one, the
live-library enumeration quietly goes back to measuring only the ROOT,
and every report still says PASS.

The mutant list in the design spec has two entries of this kind:

  MU-12  the three-state mechanism is hollowed out — ``judge()`` accepts a
          ``notrun`` argument but every caller passes ``ok=False``, so
          every NOTRUN becomes a FAIL and the harness reports the third
          state as if it were never used.
  MU-14  the coverage is over-reported — the criteria file claims to
          cover the §11 total table (F/Q/M/D/S) but only some of the
          entries are actually implemented, and the report still says
          "covered".

Both are checked HERE, statically, by scanning the source.  A static
check is the only kind that works for this: the thing being guarded is
the SOURCE of the criteria, not their runtime behaviour.

The mutant replays themselves live in
``tests/test_p1_stage3e_criteria_mutants.py`` — the same mutations, made
for real, and the criteria are asked whether they went red.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

#: The §11 total table's entries.  Each one must be covered by a
#: criterion in one of the criterion files, or be listed in
#: :data:`OUT_OF_SCOPE` with a reason.
SPEC_ENTRIES: Tuple[str, ...] = (
    "F1", "F2", "F3", "F4",
    "Q1", "Q2", "Q3", "Q4", "Q5", "Q6",
    "M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7",
    "D6", "D-A", "D-C",
    "S1", "S2", "S3", "S4", "S5", "S6",
)

#: Entries this round does NOT cover, with the reason.  Every entry here
#: is a DECISION, not an oversight: the reason says why it cannot be
#: judged yet, and (where it applies) which criterion will pick it up.
OUT_OF_SCOPE: Dict[str, str] = {
    "F1": "外置写入端到端（写入→落盘→读回）由阶段二 phase1.py 的 T1b/T3 覆盖；"
          "本轮只加四列维度",
    "F2": "外置读路径端到端由 phase1.py 的 T3(REAL)/T6(REAL) 覆盖",
    "F3": "外置检索排序一致性由 phase1.py T6(REAL) 覆盖",
    "F4": "外置结果不泄漏正文由 phase1.py T1a/T1b 覆盖",
    "Q3": "指纹一致性由 C-A1.2~C-A1.4(unit) + M1(REAL) 覆盖",
    "Q4": "指纹稳定性（不随内容变）由 C-A1.2~C-A1.4(unit) 的反向用例覆盖",
    "Q6": "性能/时延由后台工程师的压测负责，不在判据线",
    "M0": "备份与恢复演练由 migrate.step_m0 的报告负责；"
          "判据侧只验『备份文件存在且能被打开』（M0 是动作不是结果）",
    "M2": "schema 幂等由 migrate.step_m1 的报告负责；判据侧用 M1(REAL) 验指纹一致",
    "M3": "锚点恢复由 migrate.step_m4 的报告负责；判据侧用 M4(REAL) 验四列齐备",
    "M5": "指针处正文与 chunks.text 相等 —— 与 Q2 同尺（canonical），"
          "字节计数一并打印不藏；由 Q2(REAL) 覆盖",
    "M7": "重建与真源层一致由 migrate.step_m7 的报告负责；"
          "判据侧用 Q1/Q2 验重建后的库仍满足不变量",
    "D6": "D6 已随阶段一落地（9a0b6533）并有单测覆盖",
    "D-A": "D-A 已落地（2d952121），衰减改读真源层 occurred_at；"
           "本轮不重复判据，避免两处口径打架",
    "D-C": "D-C 已落地（2d952121），融合分不再被 rerank 分替换；"
           "本轮不重复判据",
    "S2": "路径安全（INV-11）由 text_inv.check_inv11 负责，"
          "Q1(REAL) 跑全套 INV 时一并覆盖",
    "S3": "释放同意源（INV-9）由 text_inv.check_inv9 负责；"
          "本轮 Q1/Q2 只判 INV-1/2/3，INV-9 仍在 NOTRUN（元素体系未上线）",
    "S4": "暗页不泄漏由 phase1.py 的可见性谓词判据覆盖",
    "S5": "FTS 同步由 migrate.step_m7 的报告负责",
}

#: The criterion files.
CRITERION_FILES = ("phase2.py", "phase2_s3e.py", "live_libs.py")


def _criterion_dir() -> Path:
    return Path(__file__).resolve().parent


def _read(name: str) -> str:
    p = _criterion_dir() / name
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


def judge_mu12_three_states_are_real(report, workspace: Path = None):
    """MU-12 (unit): the three-state mechanism is not hollowed out.

    The mutation: ``judge()`` keeps its ``notrun`` parameter, but every
    caller passes ``ok=False`` instead of ``notrun=True`` — so every
    NOTRUN degrades into a FAIL, and the report's third state is a
    column of zeroes that nobody reads.

    The check has two halves, because either alone is insufficient:

      1. the HARNESS really has the third state — ``judge_notrun``
         exists, ``rows`` carries a state STRING (not a bool), and
         ``summary()`` counts NOTRUN separately;
      2. the CRITERIA really USE it — at least one criterion calls
         ``judge_notrun`` or passes ``notrun=True``.

    Half 1 alone passes on a harness nobody uses; half 2 alone passes on
    a harness that cannot express the state.
    """
    detail = []
    ok = True

    # --- half 1: the harness has the state ---
    harness = _read("__init__.py")
    checks = {
        "judge_notrun 方法存在": "def judge_notrun" in harness,
        "judge 接受 notrun 参数": "notrun=False" in harness,
        "rows 存的是状态字符串": "STATE_PASS if ok else STATE_FAIL" in harness,
        "summary 单独统计 NOTRUN": "STATE_NOTRUN" in harness and "notrun = sum(" in harness,
        "NOTRUN 不算通过": "return passed == total" in harness,
    }
    for k, v in checks.items():
        detail.append("  harness: %-28s %s" % (k, v))
        if not v:
            ok = False

    # --- half 2: the criteria use it ---
    used = []
    for name in CRITERION_FILES:
        src = _read(name)
        if not src:
            continue
        n = len(re.findall(r"judge_notrun\(", src))
        m = len(re.findall(r"notrun=True", src))
        if n or m:
            used.append("%s: judge_notrun×%d notrun=True×%d" % (name, n, m))
    detail.append("  criteria 使用第三态: %s" % (", ".join(used) if used else "无"))
    if not used:
        ok = False
        detail.append("  —— 没有任何判据用到第三态：机制存在但被架空")

    if ok:
        detail.append("三态机制既在 harness 里实现，又被真实判据使用 —— 不是装饰")
    return report.judge(
        "MU-12(unit)", "三态机制未被架空：harness 有 NOTRUN 且判据真的用它",
        ok, "\n".join(detail))


def judge_mu14_coverage_is_not_overreported(report, workspace: Path = None):
    """MU-14 (unit): the §11 coverage claim is true.

    The mutation: the criteria file says it covers the §11 total table,
    but half the entries are missing and the report still says "covered".

    The check: every entry of :data:`SPEC_ENTRIES` must be EITHER
      * covered — a criterion number that starts with the entry's
        prefix appears among the criteria, OR
      * explicitly out of scope — it is in :data:`OUT_OF_SCOPE` with a
        reason that is not empty.

    An entry that is in NEITHER is a silent gap: the coverage claim is
    a lie.  The criterion prints the gap by name.
    """
    detail = []
    src = ""
    for name in CRITERION_FILES:
        src += _read(name)

    # The criterion numbers that exist.  They look like ``Q1(REAL)``,
    # ``C-A2.1/C-A2.2(unit)``, ``M4(REAL)``, ``S-1(unit)`` — so the
    # prefix is the part BEFORE the parenthesis, and the parenthesis
    # part is the ``(REAL)`` / ``(unit)`` qualifier.
    numbers = set(re.findall(
        r'"([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?:\.\d+)?'
        r'(?:\s*/\s*[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*(?:\.\d+)?)*)'
        r'(?:\([^)"]*\))?"', src))
    flat = set()
    for n in numbers:
        for part in re.split(r"\s*/\s*", n):
            part = part.strip()
            if part:
                flat.add(part)
                # The §11 table writes ``S1``/``S6``; the criteria write
                # ``S-1(unit)``/``S-6(unit)``.  Both spellings name the
                # same entry, so the hyphen is normalised away for the
                # comparison — otherwise a covered entry looks like a gap.
                flat.add(part.replace("-", "", 1)
                         if re.match(r"^[A-Z]-\d", part) else part)

    covered = []
    gaps = []
    for entry in SPEC_ENTRIES:
        hit = sorted(x for x in flat
                     if x == entry or x.startswith(entry + ".")
                     or x.startswith(entry + "-") or x.startswith(entry + "~"))
        if hit:
            covered.append("%s(%s)" % (entry, ",".join(hit[:3])))
        elif entry in OUT_OF_SCOPE and OUT_OF_SCOPE[entry].strip():
            covered.append("%s(不适用: %s)" % (entry, OUT_OF_SCOPE[entry][:40]))
        else:
            gaps.append(entry)

    detail.append("§11 总表条目数: %d" % len(SPEC_ENTRIES))
    detail.append("已覆盖或明确不适用: %d" % (len(SPEC_ENTRIES) - len(gaps)))
    detail.append("未覆盖（既无判据也未说明不适用理由）: %d" % len(gaps))
    for g in gaps:
        detail.append("  ! %s" % g)
    detail.append("")
    for c in covered:
        detail.append("  OK  %s" % c)

    ok = not gaps
    if gaps:
        detail.append("  —— 覆盖矩阵漏了 %s：报告说『已覆盖』而实际没有，"
                      "这就是 MU-14（覆盖范围被谎报）" % ", ".join(gaps))
    return report.judge(
        "MU-14(unit)", "§11 覆盖矩阵无谎报：每个条目都有判据或明确的不适用理由",
        ok, "\n".join(detail))


def judge_mutation_replay_is_registered(report, workspace: Path = None):
    """MU-12/MU-14's replay: the mutations are REAL, in the test suite.

    A static self-check proves the criteria FILE is honest.  It does not
    prove the criterion CATCHES the mutation.  The proof of that is the
    mutant replay: the same mutation, applied for real, and the criteria
    asked whether they went red.

    So this criterion checks that the replay exists, is in the
    repository, and names the mutations it replays — a replay that is
    not in version control is a replay nobody else can run.
    """
    detail = []
    repo = Path(__file__).resolve().parents[3]
    candidates = list(repo.glob("tests/test_p1_stage3e*.py"))
    if not candidates:
        detail.append("找不到 mutant 重放测试（tests/test_p1_stage3e*.py）")
        detail.append("没有重放 = 没有证据『改坏必红』；自检不等于反例")
        return report.judge(
            "MU-REPLAY(unit)", "mutant 重放已入库并指名要重放的变异",
            False, "\n".join(detail))
    ok = True
    for path in candidates:
        src = path.read_text(encoding="utf-8")
        tracked = False
        try:
            import subprocess
            out = subprocess.run(
                ["git", "-C", str(repo), "ls-files", "--error-unmatch",
                 str(path.relative_to(repo)).replace("\\", "/")],
                capture_output=True, text=True, timeout=10)
            tracked = out.returncode == 0
        except Exception as e:
            detail.append("git ls-files 失败: %s" % e)
        names = sorted(set(re.findall(r"MU-(\d+)", src)))
        detail.append("  %s  入库=%s  重放的变异=MU-%s"
                      % (path.name, tracked, ", ".join(names) if names else "无"))
        if not tracked:
            ok = False
            detail.append("  —— 重放测试不在版本库内：换台机器跑不了")
        if not names:
            ok = False
            detail.append("  —— 重放测试没有指名任何变异")
    return report.judge(
        "MU-REPLAY(unit)", "mutant 重放已入库并指名要重放的变异",
        ok, "\n".join(detail))
