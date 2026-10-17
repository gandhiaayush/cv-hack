"""Apply the agents' chosen parts to the baseline board so KiCad can draw what they decided.

The baseline is the wired PPG wearable (cvhack/mocks/design_wearable.json). A chosen part is swapped into a
baseline instance only when it can be WIRED by pin function (same category, every connected pin has a
counterpart by name/synonym), and the nets are rewritten to the new pin numbers. Parts that cannot be wired
are not drawn: they are returned in `unplaced` with the reason, because guessing a pinout would produce a
schematic that looks finished but is wrong."""
import json
import re
from pathlib import Path

from cvhack.contracts import Design, Node

BASE = Path(__file__).resolve().parent.parent / "cvhack" / "mocks" / "design_wearable.json"
SYN = [{"RESET", "NRST", "RST", "NRESET", "~RESET", "RESETN"}, {"VIN", "IN", "VI", "VBUS", "VCC_IN", "VIN1"}, {"VOUT", "OUT", "VO", "VOUT1"}, {"GND", "VSS", "AGND", "PGND", "EP", "GND_EP"},
       {"EN", "CE", "ENABLE", "SHDN", "~SHDN"}, {"VDD", "VCC", "V+", "VDDIO", "VDD_IO"}, {"SDA"}, {"SCL", "SCK"}]


def _n(name: str) -> str:
    return re.sub(r"[^A-Z0-9+~]", "", name.upper())


def _canon(name: str) -> str:
    n = _n(name)
    for group in SYN:
        if n in {_n(g) for g in group}:
            return sorted(group)[0]
    return n


def _tokens(name: str) -> set[str]:
    """Function words in a pin or net name: 'P0.26/SDA' -> {SDA}, 'I2C_SDA' -> {I2C, SDA}. Generic port prefixes
    and bare numbers are dropped, synonyms collapsed (NRST/RESET/nRESET)."""
    toks = {t for t in re.split(r"[^A-Za-z0-9]+", name.upper()) if t and not t.isdigit()}
    toks = {t for t in toks if not re.fullmatch(r"P\d+|PA\d*|PB\d*|PC\d*|GPIO\d*|IO\d*|PIN\d*", t)}
    return {_canon(t) for t in toks if len(t) >= 2}


def _pin_map(old, new, connected: dict[str, str]) -> dict[str, str] | None:
    """old pin num -> new pin num for every CONNECTED old pin (pin num -> net name), or None if any cannot be
    matched. Matching order: same canonical name, then shared function token with the old pin name or its net."""
    new_tok = {p.num: (_canon(p.name), _tokens(p.name)) for p in new.pins}
    used, out = set(), {}
    for p in old.pins:
        if p.num not in connected:
            continue
        net = connected[p.num]
        want = {_canon(p.name)} | _tokens(p.name) | _tokens(net)
        best, score = None, 0
        for num, (canon, toks) in new_tok.items():
            if num in used:
                continue
            sc = (3 if canon == _canon(p.name) else 0) + len(toks & want)
            if sc > score:
                best, score = num, sc
        if best is None:
            return None
        out[p.num] = best
        used.add(best)
    return out


_BASE_CACHE: dict = {}


def _base() -> Design:
    if "d" not in _BASE_CACHE:
        _BASE_CACHE["d"] = Design.model_validate(json.loads(BASE.read_text()))
    return _BASE_CACHE["d"]


def _connected_map(d: Design) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for b in d.blocks:
        for n in b.nets:
            for nd in n.nodes:
                out.setdefault(nd.ref, {})[nd.pin] = n.name
    return out


def wireable(subsystem: str, mpn: str, parts: dict) -> tuple[bool, str]:
    """Can this part be drawn on the board in this subsystem? (It must be in the slot already, or have a pinout whose
    connected pins match a same-category slot by function.)"""
    d = _base()
    block = next((b for b in d.blocks if b.subsystem == subsystem), None)
    if block is None:
        return False, "unknown subsystem"
    if mpn in {pi.mpn for pi in block.parts}:
        return True, ""
    new = parts.get(mpn)
    if new is None or not new.pins:
        return False, "no pinout in the catalog"
    conn = _connected_map(d)
    same = False
    for pi in block.parts:
        old = parts.get(pi.mpn)
        if old is None or old.category != new.category:
            continue
        same = True
        if _pin_map(old, new, conn.get(pi.ref, {})) is not None:
            return True, ""
    return False, ("its pins cannot be matched to the board slot by function" if same else "this board has no slot for this kind of part")


def drawable_alternatives(subsystem: str, mpn: str, parts: dict, n: int = 3) -> list[str]:
    new = parts.get(mpn)
    if new is None:
        return []
    cands = [p for p in parts.values() if p.category == new.category and p.mpn != mpn and p.stock != 0 and p.pins]
    cands.sort(key=lambda p: (p.price_usd is None, p.price_usd or 0))
    out = []
    for p in cands:
        if wireable(subsystem, p.mpn, parts)[0]:
            out.append(p.mpn)
            if len(out) == n:
                break
    return out


def apply(chosen: dict[str, list[dict]], parts: dict) -> tuple[Design, dict]:
    """chosen: {subsystem: [{"mpn", "qty"}]}.  Returns (design, report)."""
    d = Design.model_validate(json.loads(BASE.read_text()))
    report = {"swapped": [], "kept": [], "unplaced": []}
    connected: dict[str, dict[str, str]] = {}  # ref -> {pin num: net name}
    for b in d.blocks:
        for n in b.nets:
            for nd in n.nodes:
                connected.setdefault(nd.ref, {})[nd.pin] = n.name
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
                mp = _pin_map(old, new, connected.get(pi.ref, {}))
                if mp is None:
                    continue
                for b in d.blocks:
                    for n in b.nets:
                        for nd in n.nodes:
                            if nd.ref == pi.ref and nd.pin in mp:
                                nd.pin = mp[nd.pin]
                spare = [p.num for p in new.pins if p.num not in mp.values() and p.type != "power_in"]
                for num in spare:  # unused GPIO/outputs on the new part are intentionally left open
                    d.no_connects.append(Node(ref=pi.ref, pin=num))
                report["swapped"].append({"ref": pi.ref, "from": pi.mpn, "to": mpn, "subsystem": sub,
                                          "no_connect_pins": len(spare)})
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
