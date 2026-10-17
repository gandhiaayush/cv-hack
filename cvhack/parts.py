"""Single entry point for the parts DB.

Sources, merged by MPN (later wins field by field):
  1. cvhack/mocks/parts.json        C's mock catalogue, full electrical data
  2. data/parts.json                B's sourced seed dataset (no pins, price/stock often unknown)
  3. data/parts_catalog.json        ~500 real LCSC/JLCPCB parts with KiCad pinouts
                                    (python -m cvhack.scrape.build); fills gaps, see below
  4. cvhack/part_overlay.json       C's electrical overlay for B's parts (pins, symbol, footprint)
PARTS_SOURCE=path.json replaces all of this with one list of Part records.
A cvhack/data/parts_db.py with all_parts() is still picked up if B adds one.

B's sourced fields (description, specs, price, stock) win over the mock. A
null price or stock in B's data means "unknown" and stays None here, never 0.

The real catalog adds parts nobody else defines and fills fields that are
still unknown (B's null prices, missing pins), but leaves the mock's demo
values alone so scripted runs stay reproducible. PARTS_PRICES=catalog makes
the catalog's dated price and stock win everywhere.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from cvhack.contracts import Part

ROOT = Path(__file__).resolve().parent.parent
MOCK_PATH = ROOT / "cvhack" / "mocks" / "parts.json"
B_PATH = ROOT / "data" / "parts.json"
CATALOG_PATH = ROOT / "data" / "parts_catalog.json"
OVERLAY_PATH = ROOT / "cvhack" / "part_overlay.json"

# Fields only C's overlay or the mock know about; B's records never set them.
ELECTRICAL = ("footprint", "symbol_lib", "symbol_name", "pins", "power_mw", "area_mm2")


_SCALE = {"pF": 1e-12, "nF": 1e-9, "uF": 1e-6, "mF": 1e-3, "F": 1.0,
          "ohm": 1.0, "kohm": 1e3, "Mohm": 1e6}


def _eng(x: float, prefixes: list[tuple[float, str]]) -> str:
    for scale, sym in prefixes:
        if abs(x) >= scale * 0.999:
            v = round(x / scale, 3)
            return f"{int(v) if float(v).is_integer() else v}{sym}"
    return f"{x:g}"


def _value_from_specs(specs: dict[str, Any]) -> str:
    """KiCad-style value string, e.g. 10000 ohm -> "10k", 0.1 uF -> "100nF"."""
    c, r = specs.get("capacitance"), specs.get("resistance")
    if isinstance(c, dict) and c.get("value") is not None and c.get("unit") in _SCALE:
        return _eng(c["value"] * _SCALE[c["unit"]], [(1e-6, "uF"), (1e-9, "nF"), (1e-12, "pF")])
    if isinstance(r, dict) and r.get("value") is not None and r.get("unit") in _SCALE:
        return _eng(r["value"] * _SCALE[r["unit"]], [(1e6, "M"), (1e3, "k"), (1.0, "")])
    return ""


def from_b_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Map one record of B's schema (README "Part records") onto Part fields."""
    price = (rec.get("price") or {}).get("unit_amount")
    st = rec.get("stock") or {}
    if st.get("status") == "out_of_stock":
        stock = 0
    else:
        stock = st.get("available_qty")  # None when unknown
    specs = rec.get("specs") or {}
    return {
        "mpn": rec["mpn"],
        "category": rec.get("category", ""),
        "description": rec.get("description", ""),
        "value": _value_from_specs(specs),
        "price_usd": price,
        "stock": stock,
        "ratings": {
            "part_id": rec.get("part_id"), "manufacturer": rec.get("manufacturer"),
            "package": rec.get("package"), "specs": specs, "sources": rec.get("sources", []),
            "stock_status": st.get("status", "unknown"),
        },
    }


def from_catalog_record(rec: dict[str, Any]) -> dict[str, Any]:
    """A data/parts_catalog.json record: B's schema plus a `kicad` block and an `offer`."""
    p = from_b_record(rec)
    kicad = rec.get("kicad") or {}
    if kicad:
        p.update(footprint=kicad.get("footprint", ""), symbol_lib=kicad.get("symbol_lib", "Device"),
                 symbol_name=kicad.get("symbol_name", ""), pins=kicad.get("pins", []))
    if rec.get("area_mm2"):
        p["area_mm2"] = rec["area_mm2"]["value"]
    offer = rec.get("offer") or {}
    p["ratings"].update({
        "group": rec.get("group"), "datasheet_url": rec.get("datasheet_url"),
        "pin_source": kicad.get("pin_source"), "power_mw_known": False,
        "medical": rec.get("medical"),
        "catalog": {"sku": offer.get("sku"), "distributor": offer.get("distributor"),
                    "price_usd": p["price_usd"], "stock": p["stock"],
                    "checked_at": (rec.get("price") or {}).get("checked_at")},
    })
    return p


def _catalog_records() -> list[dict[str, Any]]:
    if not CATALOG_PATH.exists():
        return []
    return [from_catalog_record(r) for r in json.loads(CATALOG_PATH.read_text())["parts"]]


def _b_records() -> list[dict[str, Any]]:
    try:
        from cvhack.data import parts_db  # type: ignore  # optional module from B
    except ImportError:
        parts_db = None
    if parts_db is not None:
        return [p.model_dump() if isinstance(p, Part) else p for p in parts_db.all_parts()]
    if not B_PATH.exists():
        return []
    data = json.loads(B_PATH.read_text())
    recs = data["parts"] if isinstance(data, dict) else data
    # Accept either B's schema or records already in Part shape.
    return [from_b_record(r) if "part_id" in r or isinstance(r.get("stock"), dict) else r for r in recs]


def merge_sources(mock: list[dict], b: list[dict], overlay: dict[str, dict],
                  catalog: list[dict] | None = None, catalog_prices: bool = False) -> dict[str, Part]:
    merged: dict[str, dict[str, Any]] = {p["mpn"]: dict(p) for p in mock}
    for rec in b:
        base = merged.get(rec["mpn"], {})
        merged[rec["mpn"]] = {**base, **rec,
                              **{k: base[k] for k in ELECTRICAL if k in base},
                              "ratings": {**base.get("ratings", {}), **rec.get("ratings", {})}}
    for rec in catalog or []:
        base = merged.get(rec["mpn"])
        if base is None:
            merged[rec["mpn"]] = rec
            continue
        # Fill only what is still unknown; existing values keep precedence.
        for k in (*ELECTRICAL, "price_usd", "stock", "value"):
            if not base.get(k) and base.get(k) != 0 and rec.get(k) not in (None, "", []):
                base[k] = rec[k]
        if catalog_prices:
            base["price_usd"], base["stock"] = rec["price_usd"], rec["stock"]
        base["ratings"] = {**rec["ratings"], **base.get("ratings", {}), "catalog": rec["ratings"]["catalog"]}
    for mpn, extra in overlay.items():
        if mpn.startswith("_"):
            continue
        if mpn in merged:
            merged[mpn].update({k: v for k, v in extra.items() if k in ELECTRICAL})
    return {mpn: Part.model_validate(p) for mpn, p in merged.items()}


@lru_cache(maxsize=1)
def load_parts() -> dict[str, Part]:
    src = os.environ.get("PARTS_SOURCE")
    if src:
        return {p.mpn: p for p in (Part.model_validate(x) for x in json.loads(Path(src).read_text()))}
    overlay = json.loads(OVERLAY_PATH.read_text()) if OVERLAY_PATH.exists() else {}
    return merge_sources(json.loads(MOCK_PATH.read_text()), _b_records(), overlay, _catalog_records(),
                         catalog_prices=os.environ.get("PARTS_PRICES") == "catalog")
