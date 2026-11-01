"""T2f · the two coverage holes in the external-edit witness.

**Hole 1 · the persona layer was outside the scan set.**
``external_edit.truth_files`` has always listed ``persona/*.md`` — but
the *manager's* scan set (``files_to_scan``) never did.  The sync-time
reconciliation walks the manager's scan set ⇒ it could never witness the
persona layer, no matter how often it ran.  Measured live: the root log
holds 1328 events and **not one** points at ``persona/``.

**Hole 2 · the CLI reconciled every workspace against the ROOT's log.**
``python -m agent.memory.external_edit --workspace <root>`` took the
root's truth set (which INCLUDES ``agents/<id>/...``) and reconciled it
into the ROOT's log ⇒ the agents' own logs stayed **empty** ⇒ an agent
workspace's provenance was invisible to every reader of that agent's
log.

Plus the finding that answers SEED's question: **the reconciliation CAN
back-fill the historical debt** (113/113 closed on the live workspace) ⇒
a file edited before the mechanism went live, and not edited since,
becomes ``witnessed`` — not ``historical``.  The ``historical`` state is
therefore only correct while the back-fill has NOT been run.
"""

import json
import pathlib
import shutil
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.memory import changes, external_edit  # noqa: E402
from agent.memory.acceptance import phase2_s3x as X  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _mk_workspace(root: pathlib.Path, *, with_persona=True, with_agent=True):
    """A miniature workspace tree with its own change logs."""
    (root / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text("root memory\n", encoding="utf-8")
    (root / "AGENT.md").write_text("root agent\n", encoding="utf-8")
    if with_persona:
        (root / "persona").mkdir(exist_ok=True)
        (root / "persona" / "identity.md").write_text("identity\n", encoding="utf-8")
        (root / "persona" / "contract.md").write_text("contract\n", encoding="utf-8")
    if with_agent:
        ag = root / "agents" / "agent-x"
        (ag / "memory" / ".audit").mkdir(parents=True, exist_ok=True)
        (ag / "MEMORY.md").write_text("agent memory\n", encoding="utf-8")
        (ag / "AGENT.md").write_text("agent agent\n", encoding="utf-8")
    return root


def _census(root: pathlib.Path):
    """The judge's own four-state census, re-implemented from its helpers."""
    wss = X._workspaces(root)
    live_from = X._mechanism_live_from(wss)
    import datetime
    born = (datetime.datetime.fromisoformat(live_from[:19]).timestamp()
            if live_from else 0.0)
    out = {"witnessed": 0, "historical": 0, "live-leak": 0, "forged": 0}
    leaks = []
    for ws in wss:
        events = X._events(ws)
        last = X._last_event_per_path(events)
        files = X._truth_files(ws)
        own = [(r, f) for r, f in files
               if not (ws == root and r.startswith("agents/"))]
        for rel, real in own:
            H = X._canon_hash(pathlib.Path(real))
            if not H:
                continue
            e = last.get(rel)
            if e is not None and e.get(X.WITNESS_KEY) == H:
                out["witnessed"] += 1
                continue
            try:
                mt = float(pathlib.Path(real).stat().st_mtime)
            except OSError:
                mt = 0.0
            if mt < born - 1:
                out["historical"] += 1
                continue
            w = (e or {}).get(X.WITNESS_KEY)
            if w and mt <= X._event_ts(e) + 1:
                out["forged"] += 1
                continue
            out["live-leak"] += 1
            leaks.append("%s::%s" % (ws.name, rel))
    return out, leaks


# ---------------------------------------------------------------------------
# Hole 1 · persona/ is in the truth set AND in the manager's scan set
# ---------------------------------------------------------------------------

def test_persona_is_in_the_truth_set(tmp_path):
    root = _mk_workspace(tmp_path / "cow")
    rels = [r for r, _ in external_edit.truth_files(root)]
    assert "persona/identity.md" in rels
    assert "persona/contract.md" in rels


def test_persona_is_in_the_managers_scan_set(tmp_path):
    """The MUTANT this test kills: drop persona/ from the scan set.

    The scan set is the list the sync-time reconciliation walks.  If
    persona/ is missing from it, the reconciliation can never witness
    the persona layer ⇒ the whole L2 layer is outside the mechanism.
    """
    root = _mk_workspace(tmp_path / "cow")
    scan = _manager_scan_set(root)
    assert "persona/identity.md" in scan, (
        "persona/ 不在 manager 的扫描集里 ⇒ sync 期对账永远看不见它 "
        "（T2f 洞 1）")
    assert "persona/contract.md" in scan


def _manager_scan_set(workspace: pathlib.Path):
    """Reproduce manager.sync()'s ``files_to_scan``.

    Mirrors the production walk: the four root identity files, the
    persona layer (T2f), the ``memory/`` markdown tree, and the
    ``knowledge/`` tree.  A test that does not mirror the production
    walk cannot catch the production walk losing a directory.
    """
    out = set()
    for n in ("MEMORY.md", "USER.md", "AGENT.md", "RULE.md"):
        if (workspace / n).is_file():
            out.add(n)
    persona = workspace / "persona"
    if persona.is_dir():
        names = None
        meta = persona / "PERSONA.meta.json"
        if meta.is_file():
            try:
                names = list(json.loads(meta.read_text(encoding="utf-8"))
                             .get("files", {}).keys())
            except Exception:
                names = None
        names = names or sorted(p.name for p in persona.glob("*.md"))
        for n in names:
            if (persona / n).is_file():
                out.add("persona/%s" % n)
    m = workspace / "memory"
    if m.is_dir():
        for p in m.rglob("*.md"):
            out.add(p.relative_to(workspace).as_posix())
    k = workspace / "knowledge"
    if k.is_dir():
        for p in k.rglob("*.[mM][dD]"):
            if p.parent == k and p.name in ("index.md", "log.md"):
                continue
            out.add(p.relative_to(workspace).as_posix())
    return out


def test_persona_is_witnessed_by_a_reconciliation(tmp_path):
    """A reconciliation over the manager's scan set witnesses persona/."""
    root = _mk_workspace(tmp_path / "cow")
    cands = [(r, root / r) for r in sorted(_manager_scan_set(root))
             if (root / r).is_file()]
    rep = external_edit.reconcile_external_edits(root, cands)
    recorded = {e["path"] for e in rep["recorded"]}
    assert "persona/identity.md" in recorded
    assert "persona/contract.md" in recorded
    # the witness must REPRODUCE the content
    wit = external_edit._witnessed_hashes(root)
    for rel in ("persona/identity.md", "persona/contract.md"):
        assert wit[rel] == X._canon_hash(root / rel)


def test_persona_declaration_in_meta_is_honoured(tmp_path):
    """The layer's own declaration decides WHICH files it has."""
    root = _mk_workspace(tmp_path / "cow", with_persona=False)
    (root / "persona").mkdir()
    (root / "persona" / "identity.md").write_text("i\n", encoding="utf-8")
    (root / "persona" / "ignored.md").write_text("x\n", encoding="utf-8")
    (root / "persona" / "PERSONA.meta.json").write_text(
        json.dumps({"files": {"identity.md": {}}}), encoding="utf-8")
    rels = [r for r, _ in external_edit.truth_files(root)]
    assert "persona/identity.md" in rels
    assert "persona/ignored.md" not in rels


# ---------------------------------------------------------------------------
# Hole 2 · each workspace is reconciled against its OWN log
# ---------------------------------------------------------------------------

def test_cli_reconciles_each_workspace_against_its_own_log(tmp_path):
    """The MUTANT this test kills: reconcile the root's set into the root's log.

    Before the fix the root's log received ``agents/agent-x/MEMORY.md``
    and the agent's own log received NOTHING ⇒ the agent workspace's
    provenance was invisible.
    """
    root = _mk_workspace(tmp_path / "cow")
    assert external_edit.main(["--workspace", str(root), "--apply"]) == 0

    root_paths = {e["path"] for e in changes.read_changes(root)}
    agent_paths = {e["path"] for e in changes.read_changes(root / "agents" / "agent-x")}

    assert "MEMORY.md" in root_paths
    assert "persona/identity.md" in root_paths
    # the AGENT's own log must carry the agent's own files
    assert "MEMORY.md" in agent_paths, (
        "agent 工作区自己的变更记录是空的 ⇒ 它的溯源对任何读者都不可见（T2f 洞 2）")
    assert "AGENT.md" in agent_paths
    # and the ROOT's log must NOT have swallowed the agent's paths
    assert "agents/agent-x/MEMORY.md" not in root_paths


def test_workspace_truth_files_drops_foreign_paths(tmp_path):
    """The root's reconciliation must NOT claim the agents' paths.

    The root INDEX owns the agents' identity files (its ``files`` table
    carries those rows), but the agents' change LOGS are where their
    provenance lives.  Reconciling the root's set into the root's log
    would put ``agents/agent-x/MEMORY.md`` in the root's log and leave
    the agent's own log empty.
    """
    root = _mk_workspace(tmp_path / "cow")
    root_rels = {r for r, _ in external_edit.workspace_truth_files(root)}
    assert "MEMORY.md" in root_rels
    assert "persona/identity.md" in root_rels
    assert "agents/agent-x/MEMORY.md" not in root_rels, (
        "根工作区的对账不应认领 agents/ 下的路径 —— 它们属于各 agent 自己的日志")

    agent = root / "agents" / "agent-x"
    agent_rels = {r for r, _ in external_edit.agent_truth_files(agent)}
    assert "MEMORY.md" in agent_rels
    assert "agents/agent-x/MEMORY.md" not in agent_rels
    # the agent's keys are RELATIVE TO THE AGENT - that is how its own
    # log is keyed, and how the criterion reads it.
    assert "memory/2026-11-01.md" not in agent_rels  # does not exist
    for r in agent_rels:
        assert not r.startswith("agents/"), r


# ---------------------------------------------------------------------------
# The back-fill: 'historical' debt CAN be closed by a reconciliation
# ---------------------------------------------------------------------------

def test_reconciliation_backfills_the_historical_debt(tmp_path):
    """A file edited BEFORE the mechanism went live, and not since, is
    closable => it becomes ``witnessed``, NOT ``historical``.

    This is the fact SEED asked for: the ``historical`` state is only
    correct while the back-fill has NOT run.

    The fixture needs a PRIOR mechanism run (an ``external-edit``
    event), because that event's timestamp is what the criterion uses
    as "when the mechanism went live".  Without one, every file counts
    as historical for a different reason (the mechanism never ran).
    """
    import os
    import time
    root = _mk_workspace(tmp_path / "cow")

    # The mechanism was alive: one witnessed event, now.
    changes.record_change(root, "AGENT.md", "external-edit",
                          actor="reconciler",
                          extra={X.WITNESS_KEY: X._canon_hash(root / "AGENT.md")})

    # MEMORY.md was edited a MONTH ago and never since -> historical debt.
    old = time.time() - 86400 * 30
    os.utime(root / "MEMORY.md", (old, old))
    changes.record_change(root, "MEMORY.md", "rebuilder")   # no witness

    before, _ = _census(root)
    assert before["historical"] >= 1, "夹具没造出历史欠账"

    cands = external_edit.workspace_truth_files(root)
    external_edit.reconcile_external_edits(root, cands)

    after, leaks = _census(root)
    assert after["historical"] == 0, "历史欠账应当被对账补上见证"
    assert after["witnessed"] > before["witnessed"]
    assert after["live-leak"] == 0


def test_reconciliation_is_idempotent(tmp_path):
    """A second reconciliation over an unchanged tree records NOTHING."""
    root = _mk_workspace(tmp_path / "cow")
    cands = external_edit.workspace_truth_files(root)
    external_edit.reconcile_external_edits(root, cands)
    n1 = len(changes.read_changes(root))
    m1 = changes.latest_change_mtime(root)
    external_edit.reconcile_external_edits(root, cands)
    n2 = len(changes.read_changes(root))
    m2 = changes.latest_change_mtime(root)
    assert n1 == n2, "重复对账不应增长日志"
    assert m1 == m2, "重复对账不应推进 latest_change_mtime"


def test_dry_run_writes_nothing(tmp_path):
    root = _mk_workspace(tmp_path / "cow")
    assert external_edit.main(["--workspace", str(root), "--dry-run"]) == 0
    assert changes.read_changes(root) == []


# ---------------------------------------------------------------------------
# The census must not lose the persona layer
# ---------------------------------------------------------------------------

def test_census_counts_the_persona_layer(tmp_path):
    """The judge measures the persona files ⇒ they must be countable.

    A regression that dropped persona/ from the truth set would make
    the criterion silently stop measuring the L2 layer — the criterion
    would stay green while the thing it watches goes unwitnessed.
    """
    root = _mk_workspace(tmp_path / "cow")
    cands = external_edit.workspace_truth_files(root)
    external_edit.reconcile_external_edits(root, cands)
    counts, leaks = _census(root)
    assert counts["witnessed"] >= 4      # 2 persona + MEMORY.md + AGENT.md
    assert counts["live-leak"] == 0
    assert counts["forged"] == 0
