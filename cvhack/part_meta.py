"""Human-readable part cards: name, manufacturer, buy/datasheet links, price breaks, stock provenance.

Pulled from the sourced catalog (data/parts_catalog.json). Parts that exist only in the mock catalogue get a
card with no links rather than invented ones."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CATALOG = Path(__file__).resolve().parent.parent / "data" / "parts_catalog.json"


@lru_cache(maxsize=1)
def _catalog() -> dict:
    if not CATALOG.exists():
        return {}
    d = json.loads(CATALOG.read_text())
    return {r["mpn"]: r for r in (d["parts"] if isinstance(d, dict) else d)}


def _kind(r: dict) -> str:
    cat = (r.get("offer") or {}).get("catalog_category") or r.get("category", "")
    return cat.split(" / ")[-1].replace(",", "").strip()


def card(mpn: str, mock_description: str = "") -> dict:
    r = _catalog().get(mpn)
    if not r:
        return {"mpn": mpn, "name": mpn, "subtitle": mock_description, "manufacturer": None, "links": {},
                "tiers": [], "source": "mock catalogue (no distributor data)"}
    price, stock, offer = r.get("price") or {}, r.get("stock") or {}, r.get("offer") or {}
    links = {"LCSC": price.get("source_url") or stock.get("source_url"), "JLCPCB": offer.get("jlc_url"),
             "Datasheet": r.get("datasheet_url") or (r.get("kicad") or {}).get("kicad_datasheet")}
    return {
        "mpn": mpn, "name": f"{r['manufacturer']} {mpn}", "manufacturer": r["manufacturer"],
        "subtitle": f"{_kind(r)} · {r.get('package', '')}".strip(" ·"), "category": r.get("category"),
        "links": {k: v for k, v in links.items() if v},
        "tiers": price.get("tiers") or [], "sku": offer.get("sku"),
        "stock_qty": stock.get("available_qty"), "checked_at": stock.get("checked_at") or price.get("checked_at"),
        "source": "LCSC/JLCPCB catalog snapshot " + str(stock.get("checked_at") or price.get("checked_at") or "?"),
    }


def unit_price_at(tiers: list, qty: int, fallback: float | None) -> float | None:
    """Unit price for a total purchase quantity, from the distributor's price breaks."""
    for t in tiers:
        if qty >= t["min_qty"] and (t["max_qty"] is None or qty <= t["max_qty"]):
            return t["unit_amount"]
    return fallback
