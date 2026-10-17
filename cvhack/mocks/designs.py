"""Hand-written mock designs standing in for A's subsystem agents.

`python -m cvhack.mocks.designs` rewrites the JSON files next to this module,
so teammates can see exactly what a Design looks like on the wire.
"""

from __future__ import annotations

import json
from pathlib import Path

from cvhack.contracts import Budgets, Design, Net, Node, PartInstance, SchematicBlock

HERE = Path(__file__).resolve().parent


def _nodes(spec: str) -> list[Node]:
    """'U1.3 C2.1' -> [Node(ref='U1', pin='3'), Node(ref='C2', pin='1')]"""
    out = []
    for tok in spec.split():
        ref, pin = tok.split(".", 1)
        out.append(Node(ref=ref, pin=pin))
    return out


def _block(subsystem: str, parts: dict[str, str], nets: dict[str, str], ports: list[str]) -> SchematicBlock:
    return SchematicBlock(
        subsystem=subsystem,
        parts=[PartInstance(ref=r, mpn=m) for r, m in parts.items()],
        nets=[Net(name=n, nodes=_nodes(s)) for n, s in nets.items()],
        ports=ports,
    )


def wearable() -> Design:
    """Battery-powered Class II PPG wearable: USB-C charging, LiPo, BLE, MAX30102."""
    protection = _block(
        "protection",
        {"J1": "USB4105-GF-A", "F1": "0ZCJ0050FF2E", "D1": "SMF5.0A",
         "R1": "RC0402FR-075K1L", "R2": "RC0402FR-075K1L"},
        {"VBUS_RAW": "J1.A9 F1.1",
         "VBUS": "F1.2 D1.1",
         "CC1": "J1.A5 R1.1",
         "CC2": "J1.B5 R2.1",
         "GND": "J1.A12 D1.2 R1.2 R2.2"},
        ports=["VBUS", "GND"],
    )
    power = _block(
        "power",
        {"U1": "MCP73831T-2ACI/OT", "C1": "GRM188R61A475KE15D", "C2": "GRM188R61A475KE15D",
         "R3": "RC0402FR-072KL", "BT1": "BM02B-PASS-TBT",
         "U2": "AP2112K-3.3TRG1", "C3": "GRM188R61A105KA61D", "C4": "GRM188R61A105KA61D",
         "U3": "TLV70018DDCR", "C5": "GRM188R61A105KA61D", "C6": "GRM188R61A105KA61D",
         "R4": "RC0402FR-071KL", "D2": "LTST-C191KGKT"},
        {"VBUS": "U1.4 C1.1",
         "VBAT": "U1.3 C2.1 BT1.1 U2.1 U2.3 C3.1",
         "PROG": "U1.5 R3.1",
         "3V3": "U2.5 C4.1 U3.1 U3.3 C5.1 R4.1",
         "1V8": "U3.5 C6.1",
         "CHG_STAT": "U1.1 D2.1",
         "LED_A": "D2.2 R4.2",
         "GND": "U1.2 C1.2 C2.2 R3.2 BT1.2 U2.2 C3.2 C4.2 U3.2 C5.2 C6.2"},
        ports=["VBUS", "VBAT", "3V3", "1V8", "CHG_STAT", "GND"],
    )
    analog_fe = _block(
        "analog_fe",
        {"U4": "MAX30102EFD+T", "C7": "GRM155R71C104KA88D", "C8": "GRM188R61A475KE15D"},
        {"1V8": "U4.11 C7.1",
         "VBAT": "U4.1 C8.1",
         "I2C_SDA": "U4.4",
         "I2C_SCL": "U4.5",
         "PPG_INT": "U4.13",
         "GND": "U4.12 C7.2 C8.2"},
        ports=["1V8", "VBAT", "I2C_SDA", "I2C_SCL", "PPG_INT", "GND"],
    )
    mcu = _block(
        "mcu_comms",
        {"U5": "MDBT42Q-512KV2", "C9": "GRM155R71C104KA88D", "C10": "GRM188R61A106KE69D",
         "R5": "RC0402FR-074K7L", "R6": "RC0402FR-074K7L", "R7": "RC0402FR-0710KL",
         "R8": "RC0402FR-0710KL", "J2": "TC2030-IDC-NL"},
        {"3V3": "U5.1 C9.1 C10.1 R5.1 R6.1 R7.1 R8.1 J2.1",
         "I2C_SDA": "U5.10 R5.2",
         "I2C_SCL": "U5.11 R6.2",
         "PPG_INT": "U5.20 R7.2",
         "CHG_STAT": "U5.21",
         "SWDIO": "U5.30 J2.2",
         "SWDCLK": "U5.31 J2.4",
         "nRESET": "U5.32 R8.2 J2.3",
         "GND": "U5.2 C9.2 C10.2 J2.5"},
        ports=["3V3", "I2C_SDA", "I2C_SCL", "PPG_INT", "CHG_STAT", "GND"],
    )
    return Design(
        name="ppg-wearable-class2",
        blocks=[protection, power, analog_fe, mcu],
        budgets=Budgets(power_mw=30.0, cost_usd=15.0, area_mm2=700.0),
        no_connects=_nodes("U2.4 U3.4"),
        power_flags=["VBUS", "GND"],
    )


def esp32_mcu_block() -> SchematicBlock:
    """A plausible first draft that wires up cleanly but blows the power budget."""
    return _block(
        "mcu_comms",
        {"U5": "ESP32-C3-MINI-1-N4", "C9": "GRM155R71C104KA88D", "C10": "GRM188R61A106KE69D",
         "R5": "RC0402FR-074K7L", "R6": "RC0402FR-074K7L", "R7": "RC0402FR-0710KL",
         "R8": "RC0402FR-0710KL"},
        {"3V3": "U5.3 C9.1 C10.1 R5.1 R6.1 R7.1 R8.1",
         "I2C_SDA": "U5.15 R5.2",
         "I2C_SCL": "U5.16 R6.2",
         "PPG_INT": "U5.6 R7.2",
         "CHG_STAT": "U5.7",
         "nEN": "U5.8 R8.2",
         "GND": "U5.1 C9.2 C10.2"},
        ports=["3V3", "I2C_SDA", "I2C_SCL", "PPG_INT", "CHG_STAT", "GND"],
    )


def swap_mpn(design: Design, old: str, new: str) -> Design:
    """Drop-in replacement (same pinout), e.g. when a part goes out of stock."""
    d = design.model_copy(deep=True)
    for p in d.all_parts():
        if p.mpn == old:
            p.mpn = new
    return d


def broken() -> Design:
    """Same design with faults the electrical verifier must catch:
    - MCU swapped for the ESP32-C3 (blows the power budget; its pins don't match the nets)
    - R8 reset pull-up dropped from the 3V3 net (unconnected pin)
    - 1V8 LDO EN tied to nothing (unconnected input)
    """
    d = wearable()
    for b in d.blocks:
        if b.subsystem == "mcu_comms":
            for p in b.parts:
                if p.ref == "U5":
                    p.mpn = "ESP32-C3-MINI-1-N4"
            for n in b.nets:
                if n.name == "3V3":
                    n.nodes = [x for x in n.nodes if x.ref != "R8"]
        if b.subsystem == "power":
            for n in b.nets:
                if n.name == "3V3":
                    n.nodes = [x for x in n.nodes if not (x.ref == "U3" and x.pin == "3")]
    d.name = "ppg-wearable-class2-broken"
    return d


if __name__ == "__main__":
    for name, fn in [("design_wearable", wearable), ("design_broken", broken)]:
        (HERE / f"{name}.json").write_text(json.dumps(fn().model_dump(), indent=2) + "\n")
        print("wrote", HERE / f"{name}.json")
