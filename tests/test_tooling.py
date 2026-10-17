import shutil

import pytest

from cvhack.contracts import ControlCommand
from cvhack.control import read_commands, send_command
from cvhack.events import RUNS_DIR, emit, read_events
from cvhack.mocks import designs
from cvhack.parts import load_parts
from cvhack.state import fold
from cvhack.tooling import budgets, erc, kicad_cli
from cvhack.tooling.electrical import ElectricalVerifier

PARTS = load_parts()


@pytest.fixture
def run_id():
    rid = "_pytest"
    shutil.rmtree(RUNS_DIR / rid, ignore_errors=True)
    yield rid
    shutil.rmtree(RUNS_DIR / rid, ignore_errors=True)


def codes(errs):
    return {e.code for e in errs}


def test_wearable_passes_native_checks():
    d = designs.wearable()
    assert erc.check(d, PARTS) == []
    errs, details = budgets.check(d, PARTS)
    assert errs == []
    assert details["totals"]["power_mw"] <= d.budgets.power_mw


def test_broken_design_is_caught():
    d = designs.broken()
    found = codes(erc.check(d, PARTS)) | codes(budgets.check(d, PARTS)[0])
    assert {"ERC_UNCONNECTED", "PIN_UNKNOWN", "BUDGET_POWER"} <= found


def test_esp32_draft_fails_only_on_power():
    d = designs.wearable()
    d.blocks = [b for b in d.blocks if b.subsystem != "mcu_comms"] + [designs.esp32_mcu_block()]
    d.no_connects += designs._nodes("U5.26 U5.27")
    res = ElectricalVerifier(use_kicad=False).verify(d, PARTS)
    assert not res.ok and codes(res.errors) == {"BUDGET_POWER"}


def test_out_of_stock_and_swap():
    parts = {k: v.model_copy() for k, v in PARTS.items()}
    parts["AP2112K-3.3TRG1"].stock = 0
    v = ElectricalVerifier(use_kicad=False)
    assert "BOM_OUT_OF_STOCK" in codes(v.verify(designs.wearable(), parts).errors)
    swapped = designs.swap_mpn(designs.wearable(), "AP2112K-3.3TRG1", "TPS7A0233PDBVR")
    assert v.verify(swapped, parts).ok


def test_power_flag_on_driven_net_conflicts():
    d = designs.wearable()
    d.power_flags.append("VBAT")
    assert "ERC_POWER_CONFLICT" in codes(erc.check(d, PARTS))


def test_bom_totals_match():
    d = designs.wearable()
    rows = budgets.bom(d, PARTS)
    assert sum(r["qty"] for r in rows) == len({p.ref for p in d.all_parts()})
    assert round(sum(r["ext_usd"] for r in rows), 3) == pytest.approx(budgets.totals(d, PARTS)["cost_usd"], abs=0.01)


@pytest.mark.skipif(not kicad_cli.available(), reason="kicad-cli not installed")
def test_kicad_erc(tmp_path):
    d = designs.wearable()
    res = ElectricalVerifier(out_dir=tmp_path).verify(d, PARTS)
    assert res.ok, [e.message for e in res.errors]
    assert res.details["kicad"]["erc_violations"] == 0

    # Same fault KiCad must see independently: R8 dropped from 3V3.
    for b in d.blocks:
        for n in b.nets:
            if n.name == "3V3":
                n.nodes = [x for x in n.nodes if x.ref != "R8"]
    sch = kicad_cli.write_project(d, PARTS, tmp_path / "bad")
    assert any(e.code == "KICAD_PIN_NOT_CONNECTED" and e.part == "R8" for e in kicad_cli.run_erc(sch))


def test_events_control_and_fold(run_id):
    emit(run_id=run_id, agent_id="mother", kind="agent_spawned", payload={"role": "mother", "label": "M"})
    emit(run_id=run_id, agent_id="power", parent_id="mother", kind="agent_spawned", payload={"role": "subsystem"})
    emit(run_id=run_id, agent_id="power", parent_id="mother", kind="report", payload={"to": "mother", "options": []})
    emit(run_id=run_id, agent_id="mother", kind="design_updated", payload={"design": designs.wearable().model_dump()})
    evs, off = read_events(run_id)
    assert len(evs) == 4 and read_events(run_id, off)[0] == []
    s = fold(run_id, evs)
    assert s.agents["power"]["parent"] == "mother" and s.design_obj().name == "ppg-wearable-class2"
    assert s.negotiation[0]["kind"] == "report"

    send_command(run_id, ControlCommand(payload={"type": "out_of_stock", "mpn": "X"}))
    cmds, n = read_commands(run_id)
    assert n == 1 and cmds[0].payload["mpn"] == "X"
    assert read_commands(run_id, n)[0] == []
