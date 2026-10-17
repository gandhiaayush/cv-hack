"""Turn the agents' part selection into a drawable Design.

A's agents choose parts (a BOM per subsystem) around the reference board in
cvhack/mocks/design_wearable.json; they don't produce nets. This applies their
BOM to that reference so the schematic and 3D board can show the real run:

  - a reference part whose MPN the agents kept keeps its reference wiring
  - a reference part the agents left out is removed, with its connections
  - a part the agents added goes into its subsystem block; power, ground and I2C
    pins are connected by pin name, extra decoupling caps go 3V3-GND, and every
    other pin is left open and reported in `unwired`

    design, notes = apply_selection(bom, parts)
    notes == {"kept": [...], "removed": [...], "added": [...], "unwired": ["U6.3 (IN+)", ...]}
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from cvhack.contracts import Design, Net, Node, Part, PartInstance, SchematicBlock
from cvhack.mocks import designs

GND = {"GND", "VSS", "AGND", "DGND", "PGND", "GNDA", "EP", "V-", "VEE", "PAD"}
SUPPLY = {"VDD", "VCC", "V+", "VDDIO", "AVDD", "DVDD", "VIO", "VS"}
I2C = {"SDA": "I2C_SDA", "SCL": "I2C_SCL"}
PREFIX = {"resistor": "R", "capacitor": "C", "inductor": "L", "led": "D", "diode": "D", "tvs": "D",
          "connector": "J", "fuse": "F", "crystal": "Y", "battery": "BT"}


def _norm(name: str) -> str:
    return re.sub(r"[~{}_\s]", "", name).upper()


def _net_for(pin_name: str, subsystem: str) -> str | None:
    n = _norm(pin_name)
    for tok in re.split(r"[/,]", n):
        if tok in GND:
            return "GND"
        if tok in SUPPLY:
            return "3V3"
        if tok in I2C:
            return I2C[tok]
    return None


def apply_selection(bom: list[dict], parts: dict[str, Part], base: Design | None = None) -> tuple[Design, dict]:
    """bom: [{"subsystem", "mpn", "qty"}] as in the mother's accepted result."""
    base = (base or designs.wearable()).model_copy(deep=True)
    want: Counter = Counter()
    sub_of: dict[str, str] = {}
    for item in bom:
        want[item["mpn"]] += int(item.get("qty", 1) or 1)
        sub_of.setdefault(item["mpn"], item.get("subsystem", "misc"))

    original_refs = {i.ref for i in base.all_parts()}   # new parts never reuse a removed part's designator
    # 1) keep / remove reference instances, per MPN up to the wanted quantity
    have: Counter = Counter()
    removed: list[str] = []
    for blk in base.blocks:
        keep = []
        for inst in blk.parts:
            if have[inst.mpn] < want[inst.mpn]:
                have[inst.mpn] += 1
                keep.append(inst)
            else:
                removed.append(inst.ref)
        blk.parts = keep
    gone = set(removed)
    for blk in base.blocks:
        for net in blk.nets:
            net.nodes = [n for n in net.nodes if n.ref not in gone]
        blk.nets = [n for n in blk.nets if n.nodes]
    base.no_connects = [n for n in base.no_connects if n.ref not in gone]

    # 2) add the rest, auto-wiring power / ground / I2C by pin name
    used = {i.ref for i in base.all_parts()}
    counters: dict[str, int] = defaultdict(int)
    for ref in used | original_refs:
        m = re.match(r"([A-Z]+)(\d+)$", ref)
        if m:
            counters[m.group(1)] = max(counters[m.group(1)], int(m.group(2)))
    blocks = {b.subsystem: b for b in base.blocks}
    added, unwired = [], []
    for mpn, qty in want.items():
        p = parts.get(mpn)
        for _ in range(qty - have[mpn]):
            if p is None or not p.pins:
                unwired.append(f"{mpn} (no pin data; not drawn)")
                break
            pre = PREFIX.get(p.category, "U")
            counters[pre] += 1
            ref = f"{pre}{counters[pre]}"
            sub = sub_of.get(mpn, "misc")
            blk = blocks.get(sub)
            if blk is None:
                blk = SchematicBlock(subsystem=sub, parts=[], nets=[])
                base.blocks.append(blk)
                blocks[sub] = blk
            blk.parts.append(PartInstance(ref=ref, mpn=mpn))
            added.append(ref)
            two = len(p.pins) == 2
            for i, pin in enumerate(p.pins):
                net = _net_for(pin.name, sub)
                if two and p.category == "capacitor":
                    net = "3V3" if i == 0 else "GND"   # an added cap is decoupling
                if net is None:
                    unwired.append(f"{ref}.{pin.num} ({pin.name or 'pin ' + pin.num})")
                    continue
                existing = next((n for n in blk.nets if n.name == net), None)
                if existing is None:
                    blk.nets.append(Net(name=net, nodes=[Node(ref=ref, pin=pin.num)]))
                else:
                    existing.nodes.append(Node(ref=ref, pin=pin.num))
                if net not in blk.ports:
                    blk.ports.append(net)
    base.blocks = [b for b in base.blocks if b.parts]
    kept = sorted(used - set(added))
    return base, {"kept": kept, "removed": sorted(removed), "added": added, "unwired": unwired}
