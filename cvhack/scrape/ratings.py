"""Electrical limits for catalog parts that the distributor data leaves ambiguous.

    python -m cvhack.scrape.ratings     # enrich data/parts_catalog.json in place (uses the datasheet cache)

vin_abs_max   the supply-pin absolute maximum from the datasheet's "Absolute Maximum Ratings" table, with the
              line it came from. The gate uses it as the part's voltage limit. Lines that are relative to a
              supply ("VDD + 0.3"), or about signal pins, are skipped; when no supply line is found the value
              stays unknown rather than guessed.
power_mw_est  quiescent / supply / receive current from the distributor's own parametric data x 3.3 V, so the
              power budget counts something for parts that draw current. A screening estimate, not a measured
              active power; parts without a current figure stay unknown.
"""

from __future__ import annotations

import json
import re
from typing import Any

from cvhack.scrape import ROOT
from cvhack.scrape.medical import datasheet_text

CATALOG_PATH = ROOT / "data" / "parts_catalog.json"
# Categories whose parts have a supply pin with an absolute maximum worth extracting.
SUPPLY_CATEGORIES = {"ldo", "dcdc", "charger", "battery_protection", "fuel_gauge", "supervisor", "load_switch",
                     "mcu", "rf_module", "afe", "accelerometer", "light_sensor", "temp_humidity_sensor",
                     "temperature_sensor", "pressure_sensor", "opamp", "inst_amp", "current_sense", "comparator",
                     "adc", "eeprom", "flash", "rtc", "isolator", "isolation_amp"}
ABS_HEADING = re.compile(r"absolute\s+maximum|绝对最大|极限参数|最大额定值", re.I)
SUPPLY_LINE = re.compile(r"^\s*(?:[\w/ ,()-]{0,30}?)(\bV\s?IN\b|\bVIN\b|\bV\s?DD\w{0,4}\b|\bV\s?CC\w{0,3}\b|\bVBAT\b|"
                         r"\bVBUS\b|\bV\s?S\b|\bV\+|supply\s+voltage|input\s+voltage|power\s+supply|"
                         r"输入电压|电源电压|供电电压|IN\s+to\s+GND|VDD\s+to\s+GND)", re.I)
RELATIVE = re.compile(r"(V\w{0,4}|IN)\s*\+\s*0?\.\d|\+\s*0\.[0-9]\s*V", re.I)
SIGNAL = re.compile(r"\b(pin|I/O|SDA|SCL|EN\b|enable|input\s+pin|digital|analog\s+input|output\s+voltage|differential|"
                    r"VOUT|FB\b|SW\b|LX\b)", re.I)
RANGE = re.compile(r"[-−–]\s?\d+(?:\.\d+)?\s*(?:V\s*)?(?:to|~|～|…|-|–|\s)\s*\+?\s?(\d+(?:\.\d+)?)\s*V", re.I)
NUMBER_V = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d+)?)\s*V\b")
CURRENT_SPECS = ("quiescent_current", "quiescent_current_iq", "supply_current_iq", "supply_current",
                 "operating_current", "quiescent_supply_current", "current_consumption", "receive_current")


# Words that end the absolute-maximum table: the next section has operating or test conditions.
TABLE_END = re.compile(r"recommended\s+operating|electrical\s+characteristics|thermal\s+(information|resistance)|"
                       r"esd\s+ratings|推荐工作|电气特性|电特性", re.I)
CONDITION = re.compile(r"=\s*[-+]?\d|unless|otherwise|typical|current|\bI\s?DD\b|\bI\s?Q\b|[μu]A\b|\bmA\b|"
                       r"high-level|low-level|V\s?IH|V\s?IL|threshold", re.I)


def _heading_positions(text: str):
    """Absolute-maximum headings that start a line (not 'beyond those listed under Absolute Maximum...')."""
    for m in ABS_HEADING.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[line_start:m.start()].strip()
        if re.fullmatch(r"[\d.]*\s*", prefix):
            yield m.end()


def extract_vin_abs_max(text: str) -> tuple[float | None, str | None]:
    for start in _heading_positions(text):
        for line in text[start: start + 2500].splitlines()[:40]:
            if TABLE_END.search(line):
                break
            if not SUPPLY_LINE.match(line) or RELATIVE.search(line) or CONDITION.search(line) \
                    or SIGNAL.search(line[:40]):
                continue
            vals = [float(x) for x in RANGE.findall(line)] or [float(x) for x in NUMBER_V.findall(line)]
            vals = [v for v in vals if 1.5 <= v <= 100]
            if vals:
                return max(vals), re.sub(r"\s+", " ", line).strip()[:200]
    return None, None


def power_estimate(specs: dict[str, Any]) -> tuple[float | None, str | None]:
    for key in CURRENT_SPECS:
        s = specs.get(key) or {}
        v = s.get("value", s.get("max"))
        if isinstance(v, (int, float)) and s.get("unit") == "A" and 0 < v < 1:
            return round(v * 3.3 * 1000, 3), f"{key} {v * 1000:g} mA x 3.3 V (distributor parametric data)"
    return None, None


def enrich(catalog: dict[str, Any]) -> dict[str, Any]:
    stats = {"vin_abs_max": 0, "power_estimated": 0, "vin_agrees_with_distributor": 0, "vin_compared": 0}
    for p in catalog["parts"]:
        elec: dict[str, Any] = {}
        ds = next((s for s in p["sources"] if s.startswith("src:datasheet:")), None)
        if p["category"] in SUPPLY_CATEGORIES and p.get("datasheet_url"):
            text, _, _ = datasheet_text(p["datasheet_url"])
            v, line = extract_vin_abs_max(text) if text else (None, None)
            if v is not None:
                dist = (p["specs"].get("supply_voltage") or {})
                dv = dist.get("max", dist.get("value"))
                agrees = None
                if isinstance(dv, (int, float)):
                    agrees = dv * 0.9 <= v <= dv * 3
                    stats["vin_compared"] += 1
                    stats["vin_agrees_with_distributor"] += bool(agrees)
                # A value well below the distributor's supply figure is more likely a misread line than a
                # tighter limit, so it is kept for review but not used as the part's rating.
                elec["vin_abs_max"] = {"value": v, "unit": "V", "quote": line, "source_id": ds,
                                       "distributor_supply_voltage": dv, "agrees_with_distributor": agrees,
                                       "used": agrees is not False}
                stats["vin_abs_max"] += 1
        mw, basis = power_estimate(p["specs"])
        if mw is not None:
            elec["power_mw_est"] = {"value": mw, "basis": basis}
            stats["power_estimated"] += 1
        p["electrical"] = elec or None
    catalog["coverage"]["electrical"] = stats
    catalog["data_policy"]["electrical"] = (
        "electrical.vin_abs_max is the supply-pin absolute maximum quoted from the datasheet; "
        "electrical.power_mw_est is a distributor current figure x 3.3 V. Both are screening values.")
    return catalog


def main() -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    enrich(catalog)
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Enriched", CATALOG_PATH.relative_to(ROOT), catalog["coverage"]["electrical"])


if __name__ == "__main__":
    main()
