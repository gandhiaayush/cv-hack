"""Power, cost, area and stock checks, plus the BOM they're computed from."""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from cvhack.contracts import Design, Part, VerifierError


def bom(design: Design, parts: dict[str, Part]) -> list[dict]:
    """One row per MPN, grouped refs, extended price."""
    groups: dict[str, list[str]] = defaultdict(list)
    for inst in design.all_parts():
        if inst.ref not in groups[inst.mpn]:
            groups[inst.mpn].append(inst.ref)
    rows = []
    for mpn, refs in groups.items():
        p = parts.get(mpn)
        rows.append({
            "refs": " ".join(sorted(refs, key=_ref_key)),
            "qty": len(refs),
            "mpn": mpn,
            "description": p.description if p else "UNKNOWN PART",
            "value": p.value if p else "",
            "footprint": p.footprint if p else "",
            "unit_usd": p.price_usd if p else None,
            "ext_usd": round(p.price_usd * len(refs), 4) if p and p.price_usd is not None else None,
            "stock": p.stock if p else None,
        })
    return sorted(rows, key=lambda r: _ref_key(r["refs"].split()[0]))


def bom_csv(design: Design, parts: dict[str, Part]) -> str:
    rows = bom(design, parts)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["refs"])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def totals(design: Design, parts: dict[str, Part]) -> dict[str, float]:
    t = {"power_mw": 0.0, "cost_usd": 0.0, "area_mm2": 0.0}
    seen = set()
    for inst in design.all_parts():
        if inst.ref in seen or inst.mpn not in parts:
            continue
        seen.add(inst.ref)
        p = parts[inst.mpn]
        t["power_mw"] += p.power_mw
        t["cost_usd"] += p.price_usd or 0.0
        t["area_mm2"] += p.area_mm2
    return {k: round(v, 3) for k, v in t.items()}


def check(design: Design, parts: dict[str, Part]) -> tuple[list[VerifierError], dict]:
    t = totals(design, parts)
    b = design.budgets
    errs = []
    for key, code, unit in [("power_mw", "BUDGET_POWER", "mW"), ("cost_usd", "BUDGET_COST", "USD"),
                            ("area_mm2", "BUDGET_AREA", "mm²")]:
        limit = getattr(b, key)
        if t[key] > limit:
            errs.append(VerifierError(code=code, message=f"{key} {t[key]:g} {unit} exceeds budget {limit:g} {unit}"))
    for row in bom(design, parts):
        if row["mpn"] not in parts:
            continue  # PART_UNKNOWN comes from erc.check
        if row["stock"] is None:
            errs.append(VerifierError(code="BOM_STOCK_UNKNOWN", part=row["mpn"], severity="warning",
                                      message=f"{row['mpn']} ({row['refs']}): stock not checked"))
        elif row["stock"] < row["qty"]:
            errs.append(VerifierError(code="BOM_OUT_OF_STOCK", part=row["mpn"],
                                      message=f"{row['mpn']} ({row['refs']}): stock {row['stock']} < qty {row['qty']}"))
        if row["unit_usd"] is None:
            errs.append(VerifierError(code="BOM_PRICE_UNKNOWN", part=row["mpn"], severity="warning",
                                      message=f"{row['mpn']} ({row['refs']}): no price; cost total excludes it"))
    details = {"totals": t, "budgets": b.model_dump(),
               "utilization": {k: round(t[k] / getattr(b, k), 3) if getattr(b, k) else None for k in t}}
    return errs, details


def _ref_key(ref: str) -> tuple[str, int]:
    prefix = ref.rstrip("0123456789")
    num = ref[len(prefix):]
    return prefix, int(num) if num.isdigit() else 0
