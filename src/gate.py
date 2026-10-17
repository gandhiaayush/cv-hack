"""Stage-1 gate: cheap deterministic checks on a proposed option, run BEFORE the mother sees it.

All facts come from the parts DB (cvhack.parts), never from model memory. Errors block the option;
warnings are surfaced to the mother (e.g. unknown price) but do not."""
from __future__ import annotations

from cvhack.contracts import VerifierError
from cvhack.parts import load_parts

from . import completeness, design_apply
from .spec import SUBSYSTEMS

# parts whose voltage figure is not a limit the rail must stay under (TVS stand-off, diode Vf, ...)
NO_RAIL_CHECK = {"tvs", "diode", "fuse", "led", "crystal", "oscillator", "inductor", "ferrite", "resistor"}
OUT_OF_STOCK: set[str] = set()  # set by the demo's constraint change; never touches data/parts.json


def _nums(x):
    if isinstance(x, (int, float)):
        yield float(x)
    elif isinstance(x, dict):
        for k in ("max", "value"):
            if isinstance(x.get(k), (int, float)):
                yield float(x[k])


def rated_max_v(part) -> float | None:
    """Lowest voltage LIMIT the DB knows for this part, or None.

    Only unambiguous fields count: scalar vin_max / v_rated, an explicit {"max": ...}, or fields that are
    literally maximums (supply_voltage_max, rated_voltage). A bare {"value": ...} under supply_voltage is
    ambiguous (nominal? minimum? maximum?) in the distributor data, so it is NOT treated as a limit."""
    r = part.ratings or {}
    vals = [float(r[k]) for k in ("vin_max", "v_rated") if isinstance(r.get(k), (int, float))]
    specs = r.get("specs") or {}
    for k in ("supply_voltage", "input_voltage", "voltage_rating"):
        v = specs.get(k)
        if isinstance(v, dict) and isinstance(v.get("max"), (int, float)):
            vals.append(float(v["max"]))
    for k in ("supply_voltage_max", "rated_voltage"):
        vals += list(_nums(specs.get(k)))
    return min(vals) if vals else None


def check_option(subsystem: str, items: list[dict], oos: set | None = None) -> dict:
    """items: [{"mpn": str, "qty": int}]. Returns {ok, errors:[VerifierError], totals}."""
    sub = SUBSYSTEMS[subsystem]
    stockout = set(oos or ()) | OUT_OF_STOCK
    parts = load_parts()
    errs: list[VerifierError] = []
    cost, power, unknown_price, cats = 0.0, 0.0, [], set()
    for it in items:
        mpn, qty = it["mpn"], int(it.get("qty", 1))
        p = parts.get(mpn)
        if p is None:
            errs.append(VerifierError(code="PART_UNKNOWN", part=mpn, message=
                f"{mpn} is not in the parts DB. Use search_parts and choose a listed part."))
            continue
        cats.add(p.category)
        if p.category not in sub["allowed"]:
            errs.append(VerifierError(code="CATEGORY_MISMATCH", part=mpn, message=
                f"{mpn} is category '{p.category}', not allowed in {subsystem} "
                f"(allowed: {sorted(sub['allowed'])})."))
        if p.category in {"mcu", "afe", "ldo", "charger", "rf_module"}:  # the board must be able to draw it
            ok, why = design_apply.wireable(subsystem, mpn, parts)
            if not ok:
                alts = design_apply.drawable_alternatives(subsystem, mpn, parts)
                errs.append(VerifierError(code="NOT_DRAWABLE", part=mpn, message=
                    f"{mpn} cannot be drawn on this board: {why}." + (f" Drawable {p.category} choices: {', '.join(alts)}." if alts else "")))
        if mpn in stockout or p.stock == 0:
            errs.append(VerifierError(code="OUT_OF_STOCK", part=mpn, message=f"{mpn} is out of stock."))
        elif p.stock is None:
            errs.append(VerifierError(code="STOCK_UNKNOWN", part=mpn, severity="warning",
                                      message=f"{mpn} stock is unknown."))
        limit = None if p.category in NO_RAIL_CHECK else rated_max_v(p)
        if p.category in NO_RAIL_CHECK:
            pass
        elif limit is None:
            if p.category in {"ldo", "dcdc", "mcu", "rf_module", "power", "charger", "afe", "analog", "protection"}:
                errs.append(VerifierError(code="RATING_UNKNOWN", part=mpn, severity="warning", message=
                    f"{mpn} has no voltage rating in the DB; cannot confirm it covers {sub['max_rail_v']} V."))
        elif limit < sub["max_rail_v"]:
            errs.append(VerifierError(code="RATING_EXCEEDED", part=mpn, message=
                f"{mpn} is rated {limit} V max but the {subsystem} rail can reach {sub['max_rail_v']} V."))
        if p.price_usd is None:
            unknown_price.append(mpn)
            errs.append(VerifierError(code="PRICE_UNKNOWN", part=mpn, severity="warning",
                                      message=f"{mpn} price is unknown; cost is a lower bound."))
        else:
            cost += p.price_usd * qty
        power += (p.power_mw or 0.0) * qty
    if not (sub["required_any"] & cats):
        errs.append(VerifierError(code="ROLE_MISSING", message=
            f"{subsystem} needs at least one part from categories {sorted(sub['required_any'])}."))
    for need, hint in completeness.missing(subsystem, items):
        errs.append(VerifierError(code="INCOMPLETE", message=f"{subsystem} is missing: {need}. To fix: {hint}."))
    return {
        "ok": not any(e.severity == "error" for e in errs),
        "errors": [e.model_dump(exclude_none=True) for e in errs],
        "totals": {"cost_usd": round(cost, 3), "power_mw": round(power, 3),
                   "cost_is_lower_bound": bool(unknown_price)},
    }
