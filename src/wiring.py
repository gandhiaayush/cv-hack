"""Wiring phase: turn the mother's accepted parts selection into a wired, ERC-checked design.

After finalize passes the budget and the critic, every selected part gets a reference designator and one
wiring agent per subsystem connects its parts' real pins (from KiCad symbols) into nets, using the shared
interface nets in data/spec.json. As everywhere else, the model proposes and code accepts:

  per block   submit_wiring -> structural + ERC + grounding checks; errors go back to the agent
  whole board ElectricalVerifier (native ERC, budgets, stock, and KiCad's own ERC when installed); errors
              that cross blocks get one repair round with the subsystems they touch

The result is logged as `design`, `verifier` and `requirement` rows, which the bridge turns into the
dashboard's design_updated, verifier_result and requirement_status events."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cvhack.contracts import Design, Net, Node, PartInstance, SchematicBlock
from cvhack.parts import load_parts
from cvhack.tooling import erc, kicad_cli
from cvhack.tooling.electrical import ElectricalVerifier

from . import requirements, store
from .spec import BUDGET, DESIGN_NAME, DEVICE, INTERFACE_NETS, POWER_FLAGS, SUBSYSTEMS

MAX_WIRE_SUBMITS = 4
REPAIR_ROUNDS = 1
PREFIX = {"resistor": "R", "capacitor": "C", "inductor": "L", "ferrite": "FB", "led": "D", "diode": "D",
          "tvs": "D", "fuse": "F", "connector": "J", "crystal": "Y", "oscillator": "Y", "switch": "SW",
          "mosfet": "Q", "bjt": "Q"}
GROUND_NAME = re.compile(r"^(~\{)?(GND|VSS|PGND|AGND|DGND|GNDA|GNDD|SGND|EPAD|EP|PAD|THERMAL|VEE|0V)\b", re.I)
GPIO_NAME = re.compile(r"^(P[A-K]\d{1,2}|P\d\.\d{1,2}|GPIO\d+|IO\d+|D\d+|A\d+|NC|DNC|N\.C\.)\b", re.I)
# Interface-net problems that are only real once every block is merged.
CROSS_BLOCK_CODES = {"ERC_SINGLE_PIN_NET", "ERC_POWER_NOT_DRIVEN", "ERC_INPUT_NOT_DRIVEN", "ERC_POWER_CONFLICT",
                     "BLOCK_PORT_DANGLING"}

WIRE_SPEC = {"type": "function", "function": {
    "name": "submit_wiring",
    "description": "Submit the nets for your block. Every pin must be on a net or listed in no_connects. Use the "
    "shared interface net names exactly; any other net name is private to your block. A deterministic gate "
    "runs ERC and grounding checks; if it rejects the block you get the errors and must resubmit. "
    "This is the ONLY way to finish.",
    "parameters": {"type": "object", "properties": {
        "nets": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "nodes": {"type": "array", "items": {"type": "object", "properties": {
                "ref": {"type": "string"}, "pin": {"type": "string", "description": "pin NUMBER"}},
                "required": ["ref", "pin"]}}}, "required": ["name", "nodes"]}},
        "no_connects": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "string"}, "pin": {"type": "string"}}, "required": ["ref", "pin"]}},
        "auto_nc_unused_gpio": {"type": "boolean", "description": "mark unused GPIO-named pins (PA3, P0.12, IO5, "
                                "GPIO7...) as no-connect for you"},
        "notes": {"type": "string"},
    }, "required": ["nets"]}}}

WIRE_PROMPT = """You are the {sub} wiring engineer for a {device}.
Connect the parts below into nets. Use these SHARED interface nets by exact name (they join your block to the
others): {iface}. Your subsystem drives {drives} and uses {uses}. Any other net name you invent is private to
your block. Rules:
- Pins are given as number:name:electrical_type. Refer to pins by NUMBER.
- Every ground pin (GND, VSS, PGND, EP...) goes on GND. Supply pins (power_in) go on a supply net, never GND.
- Decoupling capacitors: one side on the supply net they decouple, the other on GND.
- Every pin must be on a net or listed in no_connects. auto_nc_unused_gpio=true can NC unused GPIO pins for you.
- A net needs at least two pins unless it is a shared interface net.
Call submit_wiring once with the whole block; if the gate rejects it, fix exactly what it says and resubmit."""


# ---------------------------------------------------------------------------- refs
def assign_refs(chosen: dict[str, dict]) -> dict[str, list[dict]]:
    """{subsystem: option} -> {subsystem: [{ref, mpn}]}, refs numbered 101.., 201.. per subsystem."""
    parts = load_parts()
    out: dict[str, list[dict]] = {}
    for i, sub in enumerate(SUBSYSTEMS, start=1):
        if sub not in chosen:
            continue
        count: dict[str, int] = {}
        insts = []
        for item in chosen[sub]["parts"]:
            p = parts[item["mpn"]]
            prefix = PREFIX.get(p.category, "U")
            for _ in range(int(item.get("qty", 1))):
                count[prefix] = count.get(prefix, 0) + 1
                insts.append({"ref": f"{prefix}{i * 100 + count[prefix]}", "mpn": item["mpn"]})
        out[sub] = insts
    return out


def _pin_table(insts: list[dict]) -> str:
    parts = load_parts()
    lines = []
    for inst in insts:
        p = parts[inst["mpn"]]
        pins = ", ".join(f"{x.num}:{x.name}:{x.type}" for x in p.pins)
        lines.append(f"{inst['ref']} = {inst['mpn']} ({p.category}; {p.description[:70]}) pins [{pins}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- block gate
def build_block(sub: str, insts: list[dict], args: dict) -> tuple[SchematicBlock, list[Node], list[str]]:
    """Normalize a submission into a block: private nets get a subsystem prefix so they can't short
    across blocks; optional auto no-connect of unused GPIO pins."""
    parts = load_parts()
    refs = {i["ref"]: i["mpn"] for i in insts}
    nets, notes = [], []
    for n in args.get("nets") or []:
        name = str(n.get("name", "")).strip()
        if not name:
            continue
        if name not in INTERFACE_NETS:
            name = f"{sub}.{name}"
        nodes = [Node(ref=str(x["ref"]), pin=str(x["pin"])) for x in n.get("nodes") or [] if "ref" in x and "pin" in x]
        nets.append(Net(name=name, nodes=nodes))
    ncs = [Node(ref=str(x["ref"]), pin=str(x["pin"])) for x in args.get("no_connects") or [] if "ref" in x and "pin" in x]
    if args.get("auto_nc_unused_gpio"):
        used = {(x.ref, x.pin) for n in nets for x in n.nodes} | {(x.ref, x.pin) for x in ncs}
        added = 0
        for ref, mpn in refs.items():
            for pin in parts[mpn].pins:
                if (ref, pin.num) not in used and GPIO_NAME.match(pin.name) and not pin.type.startswith("power"):
                    ncs.append(Node(ref=ref, pin=pin.num))
                    added += 1
        if added:
            notes.append(f"auto no-connect: {added} unused GPIO pin(s)")
    ports = sorted({n.name for n in nets if n.name in INTERFACE_NETS})
    block = SchematicBlock(subsystem=sub, parts=[PartInstance(**i) for i in insts], nets=nets, ports=ports)
    return block, ncs, notes


def check_block(sub: str, insts: list[dict], block: SchematicBlock, ncs: list[Node]) -> list[str]:
    """Errors that make this block unacceptable on its own (interface-net driver checks wait for the merge)."""
    parts = load_parts()
    refs = {i["ref"]: i["mpn"] for i in insts}
    problems = []
    for n in block.nets:
        for x in n.nodes:
            if x.ref not in refs:
                problems.append(f"net {n.name}: {x.ref} is not one of your parts ({', '.join(refs)})")
            elif x.pin not in {p.num for p in parts[refs[x.ref]].pins}:
                problems.append(f"net {n.name}: {x.ref} has no pin number '{x.pin}'")
    if problems:
        return problems
    design = Design(name="block-check", blocks=[block], budgets=BUDGET, no_connects=ncs,
                    power_flags=sorted(set(block.ports) | set(POWER_FLAGS)))
    for e in erc.check(design, parts):
        if e.severity != "error" or (e.code in CROSS_BLOCK_CODES and (e.net in INTERFACE_NETS or e.net is None)):
            continue
        problems.append(f"{e.code}: {e.message}")
    pin_net = {(x.ref, x.pin): n.name for n in block.nets for x in n.nodes}
    for ref, mpn in refs.items():
        for pin in parts[mpn].pins:
            net = pin_net.get((ref, pin.num))
            if net is None:
                continue
            if GROUND_NAME.match(pin.name) and pin.type in ("power_in", "passive") and net != "GND":
                problems.append(f"GROUND_NOT_GROUNDED: {ref} pin {pin.num} ({pin.name}) is on {net}, not GND")
            if pin.type == "power_in" and not GROUND_NAME.match(pin.name) and net == "GND":
                problems.append(f"SUPPLY_GROUNDED: {ref} supply pin {pin.num} ({pin.name}) is tied to GND")
    return problems


def submit_wiring(args: dict, ctx, a: dict) -> str:
    sub = a["subsystem"].removeprefix("wiring_")
    st = ctx.wiring[sub]
    a["submits"] += 1
    block, ncs, notes = build_block(sub, st["insts"], args)
    problems = check_block(sub, st["insts"], block, ncs)
    store.log(ctx.run, a["name"], a["parent"], "wiring_gate",
              {"subsystem": sub, "attempt": a["submits"], "ok": not problems, "errors": problems[:20], "notes": notes})
    if problems and a["submits"] < MAX_WIRE_SUBMITS:
        return "REJECTED by the wiring gate:\n- " + "\n- ".join(problems[:25]) + "\nFix these and resubmit."
    st.update(block=block, ncs=ncs, ok=not problems, errors=problems)
    a["done"] = json.dumps({"subsystem": sub, "status": "ok" if not problems else "wiring_failed",
                            "nets": len(block.nets), "errors": problems[:10]})
    return "ACCEPTED" if not problems else "ACCEPTED (still failing the gate; reported as a failure)"


# ---------------------------------------------------------------------------- orchestration
def _wire_subsystem(ctx, sub: str, parent: str, extra: str = "") -> None:
    from .agent import run_agent  # late import: agent imports this module
    import os
    st = ctx.wiring[sub]
    s = SUBSYSTEMS[sub]
    system = WIRE_PROMPT.format(sub=sub, device=DEVICE, iface=", ".join(INTERFACE_NETS),
                                drives=", ".join(s["drives"]) or "none", uses=", ".join(s["uses"]) or "none")
    task = "Parts in your block:\n" + _pin_table(st["insts"]) + (("\n\n" + extra) if extra else "")
    run_agent(f"wiring_{sub}", task, os.environ["SUB_MODEL"], [WIRE_SPEC], system, ctx, parent=parent, depth=1,
              agent_type="wiring", max_steps=6)
    if st.get("block") is None:  # the agent never submitted: record an empty, failing block
        st.update(block=SchematicBlock(subsystem=sub, parts=[PartInstance(**i) for i in st["insts"]], nets=[]),
                  ncs=[], ok=False, errors=["no wiring submitted"])


def merge(ctx) -> Design:
    blocks = [ctx.wiring[s]["block"] for s in SUBSYSTEMS if s in ctx.wiring]
    ncs = [n for s in ctx.wiring.values() for n in s["ncs"]]
    return Design(name=f"{DESIGN_NAME}-{ctx.run}", blocks=blocks, budgets=BUDGET, no_connects=ncs,
                  power_flags=list(POWER_FLAGS))


def _subsystems_for(errors, design: Design) -> dict[str, list[str]]:
    ref_sub = {p.ref: b.subsystem for b in design.blocks for p in b.parts}
    net_subs: dict[str, set[str]] = {}
    for b in design.blocks:
        for n in b.nets:
            net_subs.setdefault(n.name, set()).add(b.subsystem)
    out: dict[str, list[str]] = {}
    for e in errors:
        if e.severity != "error":
            continue
        subs = {ref_sub[e.part]} if e.part in ref_sub else net_subs.get(e.net or "", set())
        if not subs and e.net in INTERFACE_NETS:  # a shared net nobody drives: tell the subsystems that should
            subs = {s for s, spec in SUBSYSTEMS.items() if e.net in spec["drives"] and s in ref_sub.values()}
        for s in subs:
            out.setdefault(s, []).append(f"{e.code}: {e.message}")
    return out


def wire(ctx, chosen: dict[str, dict], parent: str) -> dict:
    """Wire the accepted selection, verify it, evaluate requirements. Returns a summary for the run result."""
    ctx.wiring = {sub: {"insts": insts, "block": None, "ncs": [], "ok": False, "errors": []}
                  for sub, insts in assign_refs(chosen).items()}
    with ThreadPoolExecutor(max_workers=len(ctx.wiring) or 1) as ex:
        list(ex.map(lambda s: _wire_subsystem(ctx, s, parent), ctx.wiring))
    parts = load_parts()
    out_dir = Path("runs") / ctx.run / "kicad"
    verifier = ElectricalVerifier(out_dir=out_dir, use_kicad=kicad_cli.available())
    design = merge(ctx)
    result = verifier.verify(design, parts)
    for rnd in range(REPAIR_ROUNDS):
        if result.ok:
            break
        todo = _subsystems_for(result.errors, design)
        if not todo:
            break
        store.log(ctx.run, "verifier.electrical", parent, "verifier", {**result.model_dump(), "round": rnd})
        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            list(ex.map(lambda s: _wire_subsystem(ctx, s, parent, extra=(
                "Your previous block (keep what is right):\n" + json.dumps(
                    {"nets": [n.model_dump() for n in ctx.wiring[s]["block"].nets],
                     "no_connects": [n.model_dump() for n in ctx.wiring[s]["ncs"]]}) +
                "\n\nThe whole-board check found these problems involving your block:\n- " +
                "\n- ".join(todo[s][:15]))), todo))
        design = merge(ctx)
        result = verifier.verify(design, parts)
    store.log(ctx.run, "wiring", parent, "design", design.model_dump())
    store.log(ctx.run, "verifier.electrical", parent, "verifier", result.model_dump())
    reqs = requirements.evaluate(design, parts, {"electrical": result}, ctx)
    for r in reqs:
        store.log(ctx.run, "requirements", parent, "requirement", r)
    return {"design_name": design.name, "electrical_ok": result.ok,
            "electrical_errors": [f"{e.code} {e.part or e.net or ''}".strip() for e in result.errors
                                  if e.severity == "error"][:20],
            "blocks": {s: ("ok" if w["ok"] else "wiring_failed") for s, w in ctx.wiring.items()},
            "requirements": {r["req_id"]: r["status"] for r in reqs},
            "advisory_requirements": [r["req_id"] for r in reqs if r["advisory"]],
            "kicad": result.details.get("kicad")}
