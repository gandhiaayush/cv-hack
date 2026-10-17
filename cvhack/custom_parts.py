"""User-defined parts (custom MCUs and chips).

A custom part is a normal Part record stored in data/custom_parts.json and merged into load_parts(), so the
gate, BOM, schematic, 3D board and agents all treat it like any other part. Its pinout can be declared as a
Verilog module header (ports become pins) or as a pin table. Everything the user did not supply stays unknown
instead of being invented: no price means unknown price, no voltage rating means the gate warns."""
from __future__ import annotations

import json
import re
from pathlib import Path

from cvhack.contracts import Part, Pin

PATH = Path(__file__).resolve().parent.parent / "data" / "custom_parts.json"
POWER_IN = re.compile(r"^(VDD|VCC|VIN|VBAT|VBUS|VSS|GND|AGND|DGND|PGND|VDDIO|AVDD|DVDD|3V3|1V8|VREF)\w*$", re.I)
POWER_OUT = re.compile(r"^(VOUT|VO|VREG|VDDOUT)\w*$", re.I)
# KiCad footprints by pin count: the nearest size at or above, so the 3D board has something real to place.
FOOTPRINTS = [(5, "Package_TO_SOT_SMD:SOT-23-5"), (6, "Package_TO_SOT_SMD:SOT-23-6"),
              (8, "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"), (14, "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm"),
              (16, "Package_SO:TSSOP-16_4.4x5mm_P0.65mm"), (20, "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm"),
              (32, "Package_QFP:TQFP-32_7x7mm_P0.8mm"), (48, "Package_QFP:LQFP-48_7x7mm_P0.5mm"),
              (64, "Package_QFP:LQFP-64_10x10mm_P0.5mm"), (100, "Package_QFP:LQFP-100_14x14mm_P0.5mm")]
CATEGORIES = ["mcu", "rf_module", "afe", "analog", "adc", "opamp", "ldo", "dcdc", "charger", "tvs", "fuse",
              "connector", "flash", "eeprom", "light_sensor", "accelerometer", "protection", "other"]


CATEGORY_LABELS = {"mcu": "Microcontroller", "rf_module": "Radio Module", "afe": "Sensor Front-End", "analog": "Analog Chip",
                   "adc": "Analog-to-Digital Converter", "opamp": "Op-Amp", "ldo": "Voltage Regulator", "dcdc": "DC-DC Converter",
                   "charger": "Battery Charger", "tvs": "Surge Protector", "fuse": "Fuse", "connector": "Connector",
                   "flash": "Flash Memory", "eeprom": "EEPROM", "light_sensor": "Light Sensor", "accelerometer": "Accelerometer",
                   "protection": "Protection", "resistor": "Resistor", "capacitor": "Capacitor", "inductor": "Inductor",
                   "led": "LED", "diode": "Diode", "mosfet": "Transistor", "crystal": "Crystal", "other": "Other"}


class CustomPartError(ValueError):
    pass


def parse_verilog(text: str) -> dict:
    """Pin list from a Verilog module header. Supports ANSI ports (`input [3:0] A, output TX`) and the older
    `module m(a, b); input a; output b;` style. Vectors expand to NAME0..NAMEn."""
    t = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
    m = re.search(r"\bmodule\s+(\w+)\s*(?:#\s*\(.*?\)\s*)?\((.*?)\)\s*;", t, re.S)
    if not m:
        raise CustomPartError("No `module name ( ... );` header found.")
    name, body = m.group(1), m.group(2)
    ports: list[tuple[str, str]] = []

    def expand(direction: str, rng: str | None, ident: str):
        if rng:
            hi, lo = (int(x) for x in re.findall(r"\d+", rng)[:2])
            for i in range(min(hi, lo), max(hi, lo) + 1):
                ports.append((f"{ident}{i}", direction))
        else:
            ports.append((ident, direction))

    if re.search(r"\b(input|output|inout)\b", body):
        direction = "input"
        for item in body.split(","):
            item = item.strip()
            dm = re.match(r"(input|output|inout)\b", item)
            if dm:
                direction = dm.group(1)
            rm = re.search(r"\[\s*\d+\s*:\s*\d+\s*\]", item)
            idm = re.findall(r"[A-Za-z_]\w*", re.sub(r"\[.*?\]", "", re.sub(r"\b(input|output|inout|wire|reg|logic|signed)\b", "", item)))
            if idm:
                expand(direction, rm.group(0) if rm else None, idm[-1])
    else:  # non-ANSI: names in the header, directions declared after
        names = [n.strip() for n in body.split(",") if n.strip()]
        decl = {}
        for dm in re.finditer(r"\b(input|output|inout)\b\s*(?:wire|reg|logic)?\s*(\[\s*\d+\s*:\s*\d+\s*\])?\s*([\w\s,]+?);", t):
            for ident in re.findall(r"[A-Za-z_]\w*", dm.group(3)):
                decl[ident] = (dm.group(1), dm.group(2))
        for n in names:
            d, r = decl.get(n, ("inout", None))
            expand(d, r, n)
    if not ports:
        raise CustomPartError("The module has no ports.")
    return {"name": name, "ports": ports}


def parse_pin_table(text: str) -> list[tuple[str, str]]:
    """`name[,direction]` per line (direction: input/output/inout/power). Blank lines and # comments ignored."""
    out = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = [x.strip() for x in re.split(r"[,\t]", line) if x.strip()]
        out.append((parts[0], parts[1].lower() if len(parts) > 1 else "inout"))
    return out


def _pin_type(name: str, direction: str) -> str:
    if direction == "power" or POWER_IN.match(name):
        return "power_in"
    if POWER_OUT.match(name):
        return "power_out"
    return {"input": "input", "output": "output", "inout": "bidirectional"}.get(direction, "passive")


def build(spec: dict) -> dict:
    """Validate a user spec and return a Part record (as a dict)."""
    mpn = (spec.get("mpn") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-/#]{1,39}", mpn):
        raise CustomPartError("Part number: 2-40 characters (letters, digits, . _ + - /).")
    category = (spec.get("category") or "other").strip().lower()
    ports: list[tuple[str, str]] = []
    if spec.get("verilog", "").strip():
        v = parse_verilog(spec["verilog"])
        ports = v["ports"]
    elif spec.get("pins", "").strip():
        ports = parse_pin_table(spec["pins"])
    if len(ports) > 200:
        raise CustomPartError("At most 200 pins.")
    seen, pins = {}, []
    for i, (name, direction) in enumerate(ports, start=1):
        base = name
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            name = f"{base}_{seen[base]}"
        pins.append(Pin(num=str(i), name=name, type=_pin_type(name, direction)))

    def num(key, lo, hi):
        v = spec.get(key)
        if v in (None, ""):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise CustomPartError(f"{key} must be a number.")
        if not lo <= f <= hi:
            raise CustomPartError(f"{key} must be between {lo} and {hi}.")
        return f

    footprint = (spec.get("footprint") or "").strip()
    if not footprint and pins:
        footprint = next((fp for n, fp in FOOTPRINTS if len(pins) <= n), FOOTPRINTS[-1][1])
    ratings = {"custom": True, "source": "user-defined", "footprint_assumed": not spec.get("footprint")}
    vmax = num("max_voltage_v", 0.1, 1000)
    if vmax is not None:
        ratings["vin_max"] = vmax
    return Part(mpn=mpn, category=category, description=(spec.get("description") or "Custom part")[:200],
                footprint=footprint, symbol_lib="Custom", symbol_name=mpn, pins=pins,
                price_usd=num("price_usd", 0, 100000), stock=int(num("stock", 0, 1e9)) if num("stock", 0, 1e9) is not None else None,
                power_mw=num("power_mw", 0, 1e6) or 0.0, area_mm2=num("area_mm2", 0, 1e6) or 0.0,
                ratings=ratings).model_dump()


def load_raw() -> list[dict]:
    return json.loads(PATH.read_text()) if PATH.exists() else []


def load() -> dict[str, Part]:
    return {r["mpn"]: Part.model_validate(r) for r in load_raw()}


def add(spec: dict) -> dict:
    rec = build(spec)
    from cvhack import parts as _parts
    mine = set(load())
    if rec["mpn"] in set(_parts.load_parts()) - mine:
        raise CustomPartError(f"{rec['mpn']} already exists in the catalog; choose another part number.")
    rows = [r for r in load_raw() if r["mpn"] != rec["mpn"]] + [rec]
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(rows, indent=1))
    _parts.load_parts.cache_clear()
    return rec


def delete(mpn: str) -> bool:
    rows = load_raw()
    keep = [r for r in rows if r["mpn"] != mpn]
    if len(keep) == len(rows):
        return False
    PATH.write_text(json.dumps(keep, indent=1))
    from cvhack import parts as _parts
    _parts.load_parts.cache_clear()
    return True


def search(category: str | None = None, text: str | None = None) -> list[dict]:
    """Rows in the shape of the catalog search, flagged custom, for the agents' search_parts tool."""
    out = []
    for p in load().values():
        if category and p.category != category:
            continue
        if text and text.lower() not in f"{p.mpn} {p.description} {p.category}".lower():
            continue
        out.append({"mpn": p.mpn, "manufacturer": "Customer-supplied", "category": p.category, "package": p.footprint,
                    "unit_price_usd": p.price_usd, "stock": p.stock, "pin_count": len(p.pins), "custom": True,
                    "specs": {"max_voltage_v": p.ratings.get("vin_max")}, "description": p.description})
    return out
