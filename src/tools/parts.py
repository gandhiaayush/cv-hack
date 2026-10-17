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
                "mopp_at_least": {"type": "integer", "description": "isolation parts whose datasheet states at "
                                  "least this many MOPP (IEC 60601-1) for their package"},
                "lifecycle": {"type": "string", "description": "e.g. 'active' (manufacturer status where known)"},
                "limit": {"type": "integer"},
            },
        },
    },
}
MEDICAL_SPEC = {
    "type": "function",
    "function": {
        "name": "get_part_medical",
        "description": "Medical-device evidence for one MPN: lifecycle status, RoHS/ECCN traceability, what its "
        "datasheet says about IEC 60601-1, MOPP, ISO 10993 and life-support use (with quotes), isolation "
        "ratings (creepage, clearance, withstand voltage, certifications) and estimated barrier leakage. "
        "Evidence only: device-level IEC 60601-1 conformity and biocompatibility are decided on the device.",
        "parameters": {"type": "object", "properties": {"mpn": {"type": "string"}}, "required": ["mpn"]},
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
            "lifecycle": (a.get("medical") or {}).get("lifecycle_status"),
            "mopp": (((a.get("medical") or {}).get("isolation") or {}).get("mopp") or {}).get("count"),
            "description": a["description"]}


def search_parts(category=None, interface=None, text=None, spec_ranges=None, needs_pins=False, limit=10,
                 mopp_at_least=None, lifecycle=None) -> str:
    ranges = {k: tuple(v) for k, v in (spec_ranges or {}).items()}
    hits = parts_graph.search(category=category, interface=interface, text=text, spec_ranges=ranges,
                              needs_pins=needs_pins, limit=min(int(limit), 30),
                              mopp_at_least=mopp_at_least, lifecycle=lifecycle)
    return json.dumps([_row(n) for n in hits]) if hits else "no matching parts"


def find_alternates(mpn: str) -> str:
    if parts_graph.part_node(mpn) is None:
        return f"{mpn} is not in the real-parts graph"
    alts = parts_graph.alternates(mpn)
    return json.dumps([_row(n) for n in alts]) if alts else "no alternate candidates in stock"


def get_part_medical(mpn: str) -> str:
    profile = parts_graph.medical_profile(mpn)
    return json.dumps(profile) if profile else f"{mpn} is not in the real-parts graph"
