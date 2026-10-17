"""check_supply: stock, price and alternate depth for a list of MPNs (used by the sourcing agent)."""
import json

from cvhack import parts_graph
from cvhack.parts import load_parts

from ..gate import OUT_OF_STOCK

SPEC = {"type": "function", "function": {
    "name": "check_supply",
    "description": "For each MPN: stock, unit price, snapshot date, and how many in-stock pin-compatible "
    "alternate candidates exist (0 means single-source risk).",
    "parameters": {"type": "object", "properties": {
        "mpns": {"type": "array", "items": {"type": "string"}}}, "required": ["mpns"]}}}


def check_supply(mpns: list) -> str:
    parts, out = load_parts(), []
    for m in mpns:
        p = parts.get(m)
        if p is None:
            out.append({"mpn": m, "error": "not in the parts DB"})
            continue
        try:
            alts = len(parts_graph.alternates(m)) if parts_graph.part_node(m) else None
        except Exception:
            alts = None
        out.append({"mpn": m, "stock": 0 if m in OUT_OF_STOCK else p.stock, "price_usd": p.price_usd,
                    "alternates_in_stock": alts, "category": p.category})
    return json.dumps(out)
