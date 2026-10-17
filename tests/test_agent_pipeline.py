"""The whole agent pipeline offline: a scripted model stands in for Crusoe, everything else is real
(gate, finalize, critic plumbing, wiring gate, ERC, requirements, control channel, bridge)."""
import json
import re
import shutil
import sqlite3
from collections import defaultdict

import pytest

from cvhack.bridge import Translator, read_rows
from cvhack.contracts import ControlCommand
from cvhack.control import send_command
from cvhack.events import RUNS_DIR
from cvhack.mocks import designs
from src import agent, gate
from src.tools import kg

MOCK = designs.wearable()
MOCK_BLOCKS = {b.subsystem: b for b in MOCK.blocks}


class Msg:
    """Just enough of an OpenAI chat message for run_agent."""

    def __init__(self, calls=None, content=None):
        self.content = content
        self.tool_calls = [type("TC", (), {"id": f"c{i}", "function": type("F", (), {
            "name": n, "arguments": json.dumps(a)})()})() for i, (n, a) in enumerate(calls or [])] or None

    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": t.id, "type": "function", "function": {
                "name": t.function.name, "arguments": t.function.arguments}} for t in self.tool_calls]
        return {k: v for k, v in d.items() if v is not None}


def _option(sub):
    qty = defaultdict(int)
    for p in MOCK_BLOCKS[sub].parts:  # first-appearance order, like assign_refs
        qty[p.mpn] += 1
    return {"name": "mock", "parts": [{"mpn": m, "qty": q} for m, q in qty.items()]}


def _wiring(sub, task):
    """Re-express the mock block's nets with the refs assign_refs gave this run."""
    new = defaultdict(list)
    for ref, mpn in re.findall(r"^(\S+) = (\S+) \(", task, re.M):
        new[mpn].append(ref)
    seen, mapping = defaultdict(int), {}
    for p in MOCK_BLOCKS[sub].parts:
        mapping[p.ref] = new[p.mpn][seen[p.mpn]]
        seen[p.mpn] += 1
    nets = [{"name": n.name, "nodes": [{"ref": mapping[x.ref], "pin": x.pin} for x in n.nodes]}
            for n in MOCK_BLOCKS[sub].nets]
    ncs = [{"ref": mapping[x.ref], "pin": x.pin} for x in MOCK.no_connects if x.ref in mapping]
    return {"nets": nets, "no_connects": ncs}


def fake_chat(messages, model, tools=None, **kw):
    system, task = messages[0]["content"], messages[1]["content"]
    done = any(m.get("role") == "tool" for m in messages)
    if "adversarial design reviewer" in system:
        return Msg(content='{"objections": []}')
    if "wiring engineer" in system:
        sub = re.search(r"You are the (\w+) wiring engineer", system).group(1)
        return Msg([("submit_wiring", _wiring(sub, task))])
    if "subsystem agent" in system:
        sub = re.search(r"You are the (\w+) subsystem agent", system).group(1)
        return Msg([("submit_report", {"subsystem": sub, "options": [_option(sub)]})])
    # the mother: list, delegate, then finalize (again after any constraint note)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if not done:
        return Msg([("list_subsystems", {})])
    if len(tool_msgs) == 1:
        return Msg([("delegate_batch", {"items": [
            {"agent_type": "specialist", "subsystem": s, "strategy": "lowest_cost", "task": f"design {s}"}
            for s in MOCK_BLOCKS]})])
    return Msg([("finalize", {"selections": [{"subsystem": s, "option": "[lowest_cost] mock"} for s in MOCK_BLOCKS],
                              "rationale": "mock parts"})])


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # runs/run.db and runs/<run>/kicad land in tmp
    monkeypatch.setattr(agent, "chat", fake_chat)
    monkeypatch.setattr("src.wiring.kicad_cli.available", lambda: False)
    monkeypatch.setenv("SUB_MODEL", "fake-sub")
    monkeypatch.setenv("CRITIC_MODEL", "fake-critic")
    gate.OUT_OF_STOCK.clear()
    kg._load.cache_clear()
    yield tmp_path
    gate.OUT_OF_STOCK.clear()


def test_full_pipeline_produces_a_wired_verified_design(run_env):
    ctx = agent.RunCtx(run="_pytest_pipeline")
    try:
        agent.run_agent("mother", "Begin.", "fake-mother", agent.MOTHER_TOOLS, agent.MOTHER_PROMPT, ctx)
    finally:
        shutil.rmtree(RUNS_DIR / ctx.run, ignore_errors=True)  # the control poller creates the run dir
    res = ctx.result
    assert res and res["status"] == "accepted"
    w = res["wiring"]
    assert w["electrical_ok"], w["electrical_errors"]
    assert set(w["blocks"].values()) == {"ok"}
    reqs = w["requirements"]
    assert reqs["REQ-04"] == reqs["REQ-06"] == reqs["REQ-09"] == "pass"  # 3V3 driven, BLE, ERC clean

    tr, events = Translator(), []
    for row in read_rows(run_env / "runs" / "run.db"):
        events += tr.translate(row)
    kinds = {e.kind for e in events}
    assert {"design_updated", "requirement_status", "verifier_result", "run_done"} <= kinds
    shown = [e.payload for e in events if e.kind == "design_updated"]
    assert shown[0]["source"] == "reference" and shown[-1]["source"] == "wired"  # synth never overwrites the wired one
    design = shown[-1]["design"]
    assert {b["subsystem"] for b in design["blocks"]} == set(MOCK_BLOCKS)
    elec = [e for e in events if e.kind == "verifier_result" and e.payload.get("verifier") == "electrical"]
    assert elec and elec[-1].payload["ok"]


def test_wiring_gate_rejects_bad_blocks(run_env):
    from src import wiring
    insts = [{"ref": "U201", "mpn": "AP2112K-3.3TRG1"}, {"ref": "C201", "mpn": "GRM188R61A105KA61D"}]
    bad = {"nets": [{"name": "3V3", "nodes": [{"ref": "U201", "pin": "5"}, {"ref": "C201", "pin": "1"}]},
                    {"name": "GND", "nodes": [{"ref": "U201", "pin": "1"}, {"ref": "C201", "pin": "2"}]},
                    {"name": "VBAT", "nodes": [{"ref": "U201", "pin": "2"}, {"ref": "U201", "pin": "9"}]}]}
    block, ncs, _ = wiring.build_block("power", insts, bad)
    problems = " ".join(wiring.check_block("power", insts, block, ncs))
    assert "has no pin number '9'" in problems
    bad["nets"][2]["nodes"].pop()
    block, ncs, _ = wiring.build_block("power", insts, bad)
    problems = " ".join(wiring.check_block("power", insts, block, ncs))
    assert "GROUND_NOT_GROUNDED" in problems and "SUPPLY_GROUNDED" in problems  # GND on VBAT, VIN on GND
    assert "ERC_UNCONNECTED" in problems  # EN pin 3 left floating


def test_out_of_stock_command_regates_reports_and_tells_the_mother(run_env):
    ctx = agent.RunCtx(run="_pytest_ctrl")
    shutil.rmtree(RUNS_DIR / ctx.run, ignore_errors=True)
    try:
        ctx.reports["power"] = {"subsystem": "power", "status": "ok", "options": [
            {"name": "a", "parts": [{"mpn": "AP2112K-3.3TRG1"}, {"mpn": "MCP73831T-2ACI/OT"}],
             "gate": {"ok": True, "errors": [], "totals": {}}}]}
        send_command(ctx.run, ControlCommand(payload={"type": "out_of_stock", "mpn": "AP2112K-3.3TRG1"}))
        note = agent.apply_controls(ctx, "mother")
        assert "AP2112K-3.3TRG1 is now OUT OF STOCK" in note and "re-delegate" in note
        assert ctx.reports["power"]["status"] == "no_valid_option"
        assert agent.apply_controls(ctx, "mother") is None  # each command is applied once
        rows = sqlite3.connect(run_env / "runs" / "run.db").execute(
            "select kind from messages where run=?", (ctx.run,)).fetchall()
        assert ("constraint_change",) in rows
    finally:
        shutil.rmtree(RUNS_DIR / ctx.run, ignore_errors=True)
