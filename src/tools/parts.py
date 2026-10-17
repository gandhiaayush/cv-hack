"""Real-parts lookup over the knowledge graph (data/parts_graph.json)."""
import json

from cvhack import parts_graph

SEARCH_SPEC = {
    "type": "function",
    "function": {
        "name": "search_parts",
        "description": "Search ~500 real, in-stock LCSC/JLCPCB parts. Filter by category (ldo, dcdc, charger, "
        "fuel_gauge, battery_protection, mcu, rf_module, afe, accelerometer, light_sensor, "
        "temp_humidity_sensor, temperature_sensor, pressure_sensor, opamp, adc, tvs, fuse, diode, mosfet, "
        "led, crystal, resistor, capacitor, inductor, ferrite, connector, ...), interface (i2c, spi, uart, "
        "usb, ble, wifi), free text, and numeric spec ranges in SI units, e.g. "
        '{"output_voltage": [3.2, 3.4]}. Returns MPN, price, stock, package, pin count and key specs.',
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "interface": {"type": "string"},
                "text": {"type": "string"},
                "spec_ranges": {"type": "object", "description": "spec name -> [min or null, max or null]"},
                "needs_pins": {"type": "boolean", "description": "only parts with a known KiCad pinout"},
                "limit": {"type": "integer"},
            },
        },
    },
}
ALTERNATES_SPEC = {
    "type": "function",
    "function": {
        "name": "find_alternates",
        "description": "In-stock alternate candidates for an MPN: same category, pinout, footprint and key "
        "value. Use after a stock-out. Candidates are unverified; re-run electrical checks after a swap.",
        "parameters": {
            "type": "object",
            "properties": {"mpn": {"type": "string"}},
            "required": ["mpn"],
        },
    },
}


def _row(n: dict) -> dict:
    a = n["attributes"]
    return {"mpn": a["mpn"], "manufacturer": a["manufacturer"], "category": a["category"],
            "package": a["package"], "unit_price_usd": a["unit_price_usd"], "stock": a["stock"],
            "stock_checked_at": a["stock_checked_at"], "pin_count": a["pin_count"],
            "specs": {k: v.get("value", [v.get("min"), v.get("max")]) for k, v in a["specs"].items()},
            "description": a["description"]}


def search_parts(category=None, interface=None, text=None, spec_ranges=None, needs_pins=False, limit=10) -> str:
    ranges = {k: tuple(v) for k, v in (spec_ranges or {}).items()}
    hits = parts_graph.search(category=category, interface=interface, text=text, spec_ranges=ranges,
                              needs_pins=needs_pins, limit=min(int(limit), 30))
    return json.dumps([_row(n) for n in hits]) if hits else "no matching parts"


def find_alternates(mpn: str) -> str:
    if parts_graph.part_node(mpn) is None:
        return f"{mpn} is not in the real-parts graph"
    alts = parts_graph.alternates(mpn)
    return json.dumps([_row(n) for n in alts]) if alts else "no alternate candidates in stock"
