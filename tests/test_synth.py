from cvhack.mocks import designs
from cvhack.parts import load_parts
from cvhack.synth import apply_selection

PARTS = load_parts()


def test_full_reference_bom_round_trips():
    ref = designs.wearable()
    bom = [{"subsystem": b.subsystem, "mpn": i.mpn, "qty": 1} for b in ref.blocks for i in b.parts]
    d, notes = apply_selection(bom, PARTS)
    assert not notes["removed"] and not notes["added"] and not notes["unwired"]
    assert d.merged_nets().keys() == ref.merged_nets().keys()


def test_selection_removes_adds_and_wires_by_pin_name():
    ref = designs.wearable()
    keep = [{"subsystem": b.subsystem, "mpn": i.mpn, "qty": 1} for b in ref.blocks for i in b.parts if i.ref != "U4"]
    d, notes = apply_selection(keep + [{"subsystem": "analog_fe", "mpn": "MCP3421A0T-E/CH", "qty": 1}], PARTS)
    assert "U4" in notes["removed"] and not any(n.ref == "U4" for ns in d.merged_nets().values() for n in ns)
    new = notes["added"][0]
    assert new not in {i.ref for i in ref.all_parts()}            # never reuses a removed designator
    nets = {n.pin: name for name, ns in d.merged_nets().items() for n in ns if n.ref == new}
    assert {"GND", "3V3", "I2C_SDA", "I2C_SCL"} <= set(nets.values())
    assert any(u.startswith(new + ".") for u in notes["unwired"])   # the analog inputs stay open, and are reported
