"""Evaluate data/spec.json requirements against a wired design. Deterministic: every status comes from the
design, the parts DB, the knowledge graph or a verifier result, and lists the refs that support it."""
from __future__ import annotations

import re

from cvhack import parts_graph
from cvhack.contracts import Design, Part, VerifierResult

from . import gate
from .spec import BUDGET, REQUIREMENTS

BLE_TEXT = re.compile(r"\bBLE\b|bluetooth|nRF5\d|ESP32", re.I)


def _by_sub(design: Design, parts: dict[str, Part]) -> dict[str, list[tuple[str, Part]]]:
    return {b.subsystem: [(i.ref, parts[i.mpn]) for i in b.parts if i.mpn in parts] for b in design.blocks}


def _has_ble(p: Part) -> bool:
    node = parts_graph.part_node(p.mpn)
    if node and any(n["id"] == "interface:ble" for _, n in parts_graph.neighbors(node["id"], "supports_interface")):
        return True
    return bool(BLE_TEXT.search(p.description or ""))


def _check(chk: dict, design: Design, parts: dict[str, Part], verifiers: dict[str, VerifierResult],
           totals: dict) -> tuple[bool, list[str], str]:
    by_sub = _by_sub(design, parts)
    t = chk["type"]
    if t == "has_category":
        refs = [r for r, p in by_sub.get(chk["subsystem"], []) if p.category in chk["categories"]]
        return bool(refs), refs, ""
    if t == "has_interface":
        refs = [r for r, p in by_sub.get(chk["subsystem"], []) if chk["interface"] == "ble" and _has_ble(p)]
        return bool(refs), refs, ""
    if t == "net_driven":
        nets = design.merged_nets().get(chk["net"], [])
        ref_part = {i.ref: parts.get(i.mpn) for i in design.all_parts()}
        drivers = [n.ref for n in nets if ref_part.get(n.ref) and any(
            pin.num == n.pin and pin.type == "power_out" for pin in ref_part[n.ref].pins)]
        return bool(drivers), drivers, "" if drivers else f"no power output drives {chk['net']}"
    if t == "budget":
        k = chk["key"]
        return totals[k] <= BUDGET[k], [], f"{totals[k]} / {BUDGET[k]}"
    if t == "verifier_ok":
        v = verifiers.get(chk["verifier"])
        return bool(v and v.ok), [], "" if v is None or v.ok else \
            f"{sum(e.severity == 'error' for e in v.errors)} error(s)"
    if t == "all_in_stock":
        bad = [i.ref for i in design.all_parts() if i.mpn in gate.OUT_OF_STOCK or not (parts[i.mpn].stock or 0)]
        return not bad, bad, ""
    if t == "no_statement":
        bad = sorted({i.ref for i in design.all_parts()
                      if chk["statement"] in ((parts_graph.medical_profile(i.mpn) or {}).get("statements") or [])})
        return not bad, bad, ""
    raise ValueError(f"unknown requirement check {t}")


def evaluate(design: Design, parts: dict[str, Part], verifiers: dict[str, VerifierResult], ctx=None) -> list[dict]:
    totals = {"power_mw": 0.0, "cost_usd": 0.0}
    for i in design.all_parts():
        p = parts[i.mpn]
        totals["power_mw"] += p.power_mw or 0.0
        totals["cost_usd"] += p.price_usd or 0.0
    totals = {k: round(v, 3) for k, v in totals.items()}
    out = []
    for req in REQUIREMENTS:
        ok, refs, note = _check(req["check"], design, parts, verifiers, totals)
        out.append({"req_id": req["id"], "text": req["text"], "status": "pass" if ok else "fail",
                    "parts": refs, "rule_ids": [], "advisory": bool(req.get("advisory")), "note": note})
    return out
