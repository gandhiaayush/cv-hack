"""Apply the agents' chosen parts to the baseline board so KiCad can draw what they decided.

The baseline is the wired PPG wearable (cvhack/mocks/design_wearable.json). A chosen part is swapped into a
baseline instance only when it can be WIRED by pin function (same category, every connected pin has a
counterpart by name/synonym), and the nets are rewritten to the new pin numbers. Parts that cannot be wired
are not drawn: they are returned in `unplaced` with the reason, because guessing a pinout would produce a
schematic that looks finished but is wrong."""
import json
import re
from pathlib import Path

from cvhack.contracts import Design

BASE = Path(__file__).resolve().parent.parent / "cvhack" / "mocks" / "design_wearable.json"
SYN = [{"VIN", "IN", "VI", "VBUS", "VCC_IN", "VIN1"}, {"VOUT", "OUT", "VO", "VOUT1"}, {"GND", "VSS", "AGND", "PGND", "EP", "GND_EP"},
       {"EN", "CE", "ENABLE", "SHDN", "~SHDN"}, {"VDD", "VCC", "V+", "VDDIO", "VDD_IO"}, {"SDA"}, {"SCL", "SCK"}]


def _n(name: str) -> str:
    return re.sub(r"[^A-Z0-9+~]", "", name.upper())


def _canon(name: str) -> str:
    n = _n(name)
    for group in SYN:
        if n in {_n(g) for g in group}:
            return sorted(group)[0]
    return n


def _pin_map(old, new, connected: set[str]) -> dict[str, str] | None:
    """old pin num -> new pin num for every CONNECTED old pin, or None if any cannot be matched."""
    by_name: dict[str, list[str]] = {}
    for p in new.pins:
        by_name.setdefault(_canon(p.name), []).append(p.num)
    used, out = set(), {}
    for p in old.pins:
        if p.num not in connected:
            continue
        cands = [n for n in by_name.get(_canon(p.name), []) if n not in used]
        if not cands:
            return None
        out[p.num] = cands[0]
        used.add(cands[0])
    return out


def apply(chosen: dict[str, list[dict]], parts: dict) -> tuple[Design, dict]:
    """chosen: {subsystem: [{"mpn", "qty"}]}.  Returns (design, report)."""
    d = Design.model_validate(json.loads(BASE.read_text()))
    report = {"swapped": [], "kept": [], "unplaced": []}
    connected: dict[str, set[str]] = {}
    for b in d.blocks:
        for n in b.nets:
            for nd in n.nodes:
                connected.setdefault(nd.ref, set()).add(nd.pin)
    taken: set[str] = set()
    for sub, items in chosen.items():
        block = next((b for b in d.blocks if b.subsystem == sub), None)
        if block is None:
            continue
        want = [(i["mpn"], int(i.get("qty", 1))) for i in items]
        have = {pi.mpn for pi in block.parts}
        for mpn, _ in want:
            if mpn in have:
                report["kept"].append(mpn)
        for mpn, _ in want:
            if mpn in have:
                continue
            new = parts.get(mpn)
            if new is None or not new.pins:
                report["unplaced"].append({"mpn": mpn, "subsystem": sub,
                                           "reason": "no pinout in the catalog" if new else "not in the parts DB"})
                continue
            done = False
            for pi in block.parts:
                old = parts.get(pi.mpn)
                if pi.ref in taken or old is None or old.category != new.category or pi.mpn in {m for m, _ in want}:
                    continue
                mp = _pin_map(old, new, connected.get(pi.ref, set()))
                if mp is None:
                    continue
                for b in d.blocks:
                    for n in b.nets:
                        for nd in n.nodes:
                            if nd.ref == pi.ref and nd.pin in mp:
                                nd.pin = mp[nd.pin]
                report["swapped"].append({"ref": pi.ref, "from": pi.mpn, "to": mpn, "subsystem": sub})
                pi.mpn = mpn
                taken.add(pi.ref)
                done = True
                break
            if not done:
                cat_has = any(parts.get(pi.mpn) and parts[pi.mpn].category == new.category for pi in block.parts)
                report["unplaced"].append({"mpn": mpn, "subsystem": sub, "reason":
                    "pins could not be matched to the baseline part by function" if cat_has else
                    "no baseline part of this category to replace; adding it needs new nets"})
    return d, report
