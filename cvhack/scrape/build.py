"""Select ~500 real parts from the JLCPCB/LCSC catalog and write data/parts_catalog.json.

    python -m cvhack.scrape.build [--refresh] [--target 500] [--no-medical]

Selection is deterministic for a given catalog snapshot: every MPN already
referenced by the repo (mock design, B's seed) that the catalog stocks is kept,
then each category in PLAN is filled by rank: KiCad pinout available, JLC
Basic/Preferred library class, then stock. Records use B's schema (README
"Part records") plus a `kicad` block with pins, and an `offer` block with the
distributor SKU and price tiers. cvhack.scrape.medical then adds the `medical`
evidence block (lifecycle, traceability, datasheet statements, isolation).
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from cvhack.scrape import ROOT
from cvhack.scrape.jlcparts import SOURCE_PAGE, Catalog, Component
from cvhack.scrape.kicad_symbols import REPO_URL, SymbolLibrary, Symbol

OUT_PATH = ROOT / "data" / "parts_catalog.json"
MOCK_PATH = ROOT / "cvhack" / "mocks" / "parts.json"
B_PATH = ROOT / "data" / "parts.json"
KICAD_SOURCE_ID = "src:kicad:symbols"


@dataclass
class Bucket:
    category: str              # fine-grained Part.category, matching the mock vocabulary
    group: str                 # coarse group (README controlled vocabulary)
    subcategories: tuple[int, ...]
    quota: int
    accept: Callable[[Component], bool] = lambda c: True
    # Two candidates with the same key are near-duplicates; only the best is kept.
    dedupe: Callable[[Component], Any] | None = None
    per_manufacturer: int | None = None
    needs_pins: bool = False   # rank parts with a KiCad pinout first
    prefer: Callable[[Component], float] | None = None  # extra rank key, after pins


def _t(c: Component, name: str) -> str:
    return (c.attr_text(name) or "").lower()


def _vout(c: Component) -> float | None:
    return c.attr_num("Output Voltage")


def _single(c: Component, name: str) -> bool:
    return _t(c, name) in ("1", "nan", "")


TWO_TERMINAL_PKGS = re.compile(r"^(0201|0402|0603|0805|1206|1210|1812|2010|2512|SOD-?123F?L?|SOD-?323F?|"
                               r"SOD-?523|SMA(\(DO-214AC\))?|SMB(\(DO-214AA\))?|SMAF|SMBF|DO-214AC|DO-214AA)$", re.I)


def _two_terminal(c: Component) -> bool:
    return bool(c.package and TWO_TERMINAL_PKGS.match(c.package.replace(" ", "")))


def _isolation_kv(c: Component) -> float:
    v = c.attr_num("Isolation Voltage(VRMS)") or c.attr_num("Isolation Voltage")
    return round(v / 1000, 1) if v else 0.0


# Isolation parts commonly used at a patient or USB barrier; kept whenever stocked.
MEDICAL_MPNS = ("ISOW7841DWER", "ADUM3160BRWZ", "ADUM4160BRWZ-RL", "ISO1540DR", "SI8641BD-B-IS",
                "AMC1301DWVR", "ADUM5000ARWZ-RL", "B0505S-1WR3")


PLAN: list[Bucket] = [
    # --- power ---------------------------------------------------------------
    Bucket("ldo", "power", (455, 457), 40, needs_pins=True, per_manufacturer=8,
           accept=lambda c: _vout(c) is not None and 0.8 <= _vout(c) <= 5.0,
           dedupe=lambda c: (c.mpn.split("-")[0][:7], _vout(c))),
    Bucket("dcdc", "power", (441,), 28, needs_pins=True, per_manufacturer=8,
           accept=lambda c: any(t in _t(c, "Topology") for t in ("buck", "boost"))
           and (c.attr_num("Supply Voltage") or 99) <= 40,
           dedupe=lambda c: c.mpn[:7]),
    Bucket("charger", "power", (438,), 16, needs_pins=True, per_manufacturer=5,
           accept=lambda c: "charging" in _t(c, "The Chip Type") and "lithium" in _t(c, "Type of Battery"),
           dedupe=lambda c: c.mpn[:6]),
    Bucket("battery_protection", "power", (438,), 8, needs_pins=True, per_manufacturer=4,
           accept=lambda c: _t(c, "The Chip Type").startswith("protection") and "lithium" in _t(c, "Type of Battery"),
           dedupe=lambda c: c.mpn[:6]),
    Bucket("fuel_gauge", "power", (438,), 6, needs_pins=True,
           accept=lambda c: "monitoring" in _t(c, "The Chip Type")),
    Bucket("supervisor", "power", (450,), 6, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    Bucket("load_switch", "power", (447,), 6, needs_pins=True,
           accept=lambda c: _t(c, "Type") in ("load switch", "high side switch"), dedupe=lambda c: c.mpn[:6]),
    # --- MCU and comms --------------------------------------------------------
    Bucket("mcu", "mcu", (216,), 32, needs_pins=True, per_manufacturer=10, dedupe=lambda c: c.mpn[:9]),
    Bucket("rf_module", "mcu", (320,), 12, per_manufacturer=4),
    Bucket("rf_module", "mcu", (331,), 10, per_manufacturer=4, dedupe=lambda c: c.mpn[:12]),
    Bucket("rf_module", "mcu", (325, 324), 6, per_manufacturer=3),
    Bucket("eeprom", "memory", (381,), 4, needs_pins=True, dedupe=lambda c: c.attr_num("Memory Size")),
    Bucket("flash", "memory", (389,), 4, needs_pins=True, dedupe=lambda c: c.attr_num("Memory Size")),
    Bucket("rtc", "timing", (67,), 4, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    # --- sensing and analog front end ----------------------------------------
    Bucket("afe", "analog", (521,), 2),
    Bucket("accelerometer", "analog", (511, 513), 10, needs_pins=True, dedupe=lambda c: c.mpn[:7]),
    Bucket("light_sensor", "analog", (512,), 6, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    Bucket("temp_humidity_sensor", "analog", (534,), 8, needs_pins=True, dedupe=lambda c: c.mpn[:5]),
    Bucket("temperature_sensor", "analog", (535,), 8, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    Bucket("pressure_sensor", "analog", (530,), 5, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    Bucket("opamp", "analog", (10, 18), 16, needs_pins=True, per_manufacturer=6, dedupe=lambda c: c.mpn[:6]),
    Bucket("inst_amp", "analog", (16,), 4, needs_pins=True),
    Bucket("current_sense", "analog", (13,), 5, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    Bucket("comparator", "analog", (7,), 4, needs_pins=True, dedupe=lambda c: c.mpn[:5]),
    Bucket("adc", "analog", (2,), 5, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    # --- protection -------------------------------------------------------------
    Bucket("tvs", "protection", (50,), 24, needs_pins=True, per_manufacturer=8,
           dedupe=lambda c: (c.attr_num("Reverse Stand-Off Voltage (VRWM)"), _t(c, "Polarity"),
                             c.package, _t(c, "Number of Channels"))),
    Bucket("fuse", "protection", (55,), 12, per_manufacturer=5,
           accept=_two_terminal, dedupe=lambda c: (c.attr_num("Hold Current"), c.package)),
    # --- discretes ---------------------------------------------------------------
    Bucket("diode", "discrete", (192,), 8, accept=lambda c: _single(c, "Diode Configuration"),
           dedupe=lambda c: (c.attr_num("Reverse Voltage (Vr)"), c.attr_num("Rectified Current"), c.package)),
    Bucket("diode", "discrete", (199,), 6, accept=_two_terminal,
           dedupe=lambda c: (c.attr_num("Zener Voltage (Nom)"), c.package)),
    Bucket("diode", "discrete", (196,), 4, dedupe=lambda c: c.mpn[:5]),
    Bucket("mosfet", "discrete", (592,), 12, needs_pins=True, per_manufacturer=4,
           accept=lambda c: _t(c, "Number") == "1", dedupe=lambda c: c.mpn[:6]),
    Bucket("bjt", "discrete", (587,), 6, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    Bucket("led", "passive", (414,), 12, accept=_two_terminal,
           dedupe=lambda c: (_t(c, "Illumination Color"), c.package)),
    Bucket("led", "passive", (421,), 3, needs_pins=True, dedupe=lambda c: c.mpn[:6]),
    Bucket("crystal", "timing", (156,), 10, dedupe=lambda c: (c.attr_num("Frequency"), c.package)),
    Bucket("oscillator", "timing", (161,), 3, dedupe=lambda c: c.attr_num("Frequency")),
    # --- passives ---------------------------------------------------------------
    Bucket("resistor", "passive", (501,), 50,
           accept=lambda c: c.package in ("0402", "0603", "0805") and (c.is_basic or c.is_preferred),
           dedupe=lambda c: (c.attr_num("Resistance"), c.package)),
    Bucket("capacitor", "passive", (39,), 40,
           accept=lambda c: c.package in ("0402", "0603", "0805", "1206") and (c.is_basic or c.is_preferred),
           dedupe=lambda c: (c.attr_num("Capacitance"), c.package)),
    Bucket("inductor", "passive", (264,), 12, per_manufacturer=5,
           dedupe=lambda c: (c.attr_num("Inductance"), c.package)),
    Bucket("inductor", "passive", (263,), 4, dedupe=lambda c: (c.attr_num("Inductance"), c.package)),
    Bucket("ferrite", "passive", (29,), 6, dedupe=lambda c: (_t(c, "Impedance @ Frequency"), c.package)),
    # --- connectors and UI ------------------------------------------------------
    Bucket("connector", "connector", (147,), 10, per_manufacturer=4,
           accept=lambda c: _t(c, "Connector Type") in ("type-c", "micro-b"),
           dedupe=lambda c: (c.manufacturer, _t(c, "Number of Contacts"))),
    Bucket("connector", "connector", (149,), 10, per_manufacturer=6,
           accept=lambda c: _t(c, "Number of Rows") in ("1", "") and (c.attr_num("Number of Pins") or 99) <= 6,
           dedupe=lambda c: (_t(c, "Reference Series"), c.attr_num("Number of Pins"), _t(c, "Mounting Type"))),
    Bucket("connector", "connector", (132,), 4,
           accept=lambda c: _t(c, "Number of Rows") == "1",
           dedupe=lambda c: (c.attr_num("Number of Pins"), c.attr_num("Pitch"))),
    Bucket("connector", "connector", (96,), 2),
    Bucket("switch", "connector", (340,), 4, dedupe=lambda c: c.package),
    # --- isolation barriers (where IEC 60601-1 MOOP/MOPP ratings apply) -----------
    Bucket("isolator", "isolation", (540, 541, 542, 552), 12, needs_pins=True, per_manufacturer=4,
           prefer=_isolation_kv, dedupe=lambda c: c.mpn[:8]),
    Bucket("isolation_amp", "isolation", (553,), 3, needs_pins=True, prefer=_isolation_kv,
           dedupe=lambda c: c.mpn[:6]),
    Bucket("isolated_dcdc", "isolation", (466, 442), 5, per_manufacturer=2, prefer=_isolation_kv,
           dedupe=lambda c: (c.manufacturer, _isolation_kv(c))),
    Bucket("optocoupler", "isolation", (428,), 3, needs_pins=True, dedupe=lambda c: c.mpn[:5]),
]

SKIP_ATTRS = {"Basic/Extended", "Package", "Status", "Manufacturer", "Category", "Description",
              "Is Pre Sale", "Minimum Order Quantity", "Mpn", "Order Multiple", "Packaging",
              "Packaging Quantity", "Packaging_unit", "Rohs"}
UNITS = {"voltage": "V", "current": "A", "resistance": "ohm", "capacitance": "F", "inductance": "H",
         "frequency": "Hz", "power": "W", "temperature": "degC", "time": "s", "percentage": "%",
         "data_size": "bit", "charge": "C"}
SIZE_CODE_METRIC = {"0201": "0603Metric", "0402": "1005Metric", "0603": "1608Metric", "0805": "2012Metric",
                    "1206": "3216Metric", "1210": "3225Metric", "1812": "4532Metric", "2010": "5025Metric",
                    "2512": "6332Metric"}
SIZE_CODE_MM = {"0201": (0.6, 0.3), "0402": (1.0, 0.5), "0603": (1.6, 0.8), "0805": (2.0, 1.25),
                "1206": (3.2, 1.6), "1210": (3.2, 2.5), "1812": (4.5, 3.2), "2010": (5.0, 2.5),
                "2512": (6.3, 3.2)}
# Nominal outline incl. leads (JEDEC/IPC typical), for packages with no size in the name.
OUTLINE_MM = {"SOT-23": (2.9, 2.8), "SOT-23-3": (2.9, 2.8), "SOT-23-3L": (2.9, 2.8), "SOT-23-5": (2.9, 2.8),
              "SOT-23-6": (2.9, 2.8), "SOT-223": (6.5, 7.0), "SOT-89": (4.5, 4.1), "SOT-89-3": (4.5, 4.1),
              "SOIC-8": (4.9, 6.0), "SOP-8": (4.9, 6.0), "MSOP-8": (3.0, 4.9), "SOD-123": (3.7, 1.6),
              "SOD-123F": (3.7, 1.6), "SOD-123FL": (3.7, 1.6), "SOD-323": (2.5, 1.3), "SOD-523": (1.6, 0.8),
              "SMA": (5.2, 2.6), "SMB": (5.4, 3.6), "SC-70-5": (2.1, 2.1), "SOT-363": (2.1, 2.1),
              "SOT-563": (1.6, 1.6), "TO-252": (6.6, 10.0)}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def part_id(manufacturer: str | None, mpn: str) -> str:
    return f"part:{slug(manufacturer or 'unknown')}:{slug(mpn)}"


def _spec_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def specs_of(c: Component, source_id: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, a in c.attributes.items():
        if name in SKIP_ATTRS:
            continue
        vals = a["values"]
        numeric = {k: v for k, v in vals.items()
                   if isinstance(v[0], (int, float)) and not isinstance(v[0], bool)
                   and not (isinstance(v[0], float) and math.isnan(v[0]))}
        spec: dict[str, Any] = {}
        if numeric:
            mins = [k for k in numeric if k.endswith(" min")]
            maxs = [k for k in numeric if k.endswith(" max")]
            kind = next(iter(numeric.values()))[1]
            if mins or maxs:
                if mins:
                    spec["min"] = numeric[mins[0]][0]
                if maxs:
                    spec["max"] = numeric[maxs[0]][0]
            else:
                key = a["primary"] if a["primary"] in numeric else next(iter(numeric))
                spec["value"] = numeric[key][0]
                kind = numeric[key][1]
            if kind in UNITS:
                spec["unit"] = UNITS[kind]
        else:
            text = a["text"].strip()
            if not text or text.lower() in ("nan", "-", "unspecified"):
                continue
            spec["value"] = text
        if a["text"] and len(numeric) > 1:
            spec["text"] = a["text"]
        spec["source_id"] = source_id
        out[_spec_key(name)] = spec
    return out


def outline_area(package: str | None) -> float | None:
    if not package:
        return None
    pkg = package.strip()
    if pkg in SIZE_CODE_MM:
        w, h = SIZE_CODE_MM[pkg]
        return round(w * h, 2)
    m = re.search(r"\((\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", pkg) or re.search(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm", pkg)
    if m:
        return round(float(m.group(1)) * float(m.group(2)), 2)
    if pkg in OUTLINE_MM:
        w, h = OUTLINE_MM[pkg]
        return round(w * h, 2)
    return None


# ---------------------------------------------------------------------------
# KiCad pins
# ---------------------------------------------------------------------------

def _generic(lib: SymbolLibrary, c: Component, category: str) -> tuple[Symbol, str] | None:
    """Generic KiCad symbols for parts whose pinout is fixed by convention."""
    pkg = (c.package or "").replace(" ", "")
    if category in ("resistor", "capacitor", "inductor", "ferrite", "fuse"):
        name = {"resistor": "R", "capacitor": "C", "inductor": "L", "ferrite": "FerriteBead",
                "fuse": "Polyfuse"}[category]
        return lib.get("Device:" + name), "generic_two_terminal"
    if category == "led" and _two_terminal(c):
        return lib.get("Device:LED"), "generic_two_terminal"
    if category == "diode" and _two_terminal(c):
        zener = c.subcategory_id == 199
        return lib.get("Device:D_Zener" if zener else "Device:D_Schottky" if c.subcategory_id == 192
                       else "Device:D"), "generic_two_terminal"
    if category == "tvs" and _two_terminal(c) and _single(c, "Number of Channels"):
        bidir = "bidirectional" in _t(c, "Polarity")
        return lib.get("Device:D_TVS" if bidir else "Device:D_Zener"), "generic_two_terminal"
    if category == "crystal" and re.search(r"-2P$|^HC-49", pkg, re.I):
        return lib.get("Device:Crystal"), "generic_two_terminal"
    if category == "connector" and c.subcategory_id in (132, 149):
        n = c.attr_num("Number of Pins")
        if n and _t(c, "Number of Rows") in ("1", "") and 1 <= n <= 40:
            return lib.get(f"Connector_Generic:Conn_01x{int(n):02d}"), "generic_single_row_header"
    return None


def _footprint(c: Component, category: str, sym: Symbol | None, method: str) -> str:
    pkg = (c.package or "").strip()
    if pkg in SIZE_CODE_METRIC:
        prefix = {"resistor": ("Resistor_SMD", "R"), "capacitor": ("Capacitor_SMD", "C"),
                  "inductor": ("Inductor_SMD", "L"), "ferrite": ("Inductor_SMD", "L"),
                  "led": ("LED_SMD", "LED"), "fuse": ("Fuse", "Fuse"),
                  "diode": ("Diode_SMD", "D"), "tvs": ("Diode_SMD", "D")}.get(category)
        if prefix:
            return f"{prefix[0]}:{prefix[1]}_{pkg}_{SIZE_CODE_METRIC[pkg]}"
    diode_fp = {"SOD-123": "D_SOD-123", "SOD-123F": "D_SOD-123F", "SOD-123FL": "D_SOD-123F",
                "SOD-323": "D_SOD-323", "SOD-523": "D_SOD-523", "SMA": "D_SMA", "SMA(DO-214AC)": "D_SMA",
                "SMB": "D_SMB", "SMB(DO-214AA)": "D_SMB"}
    if category in ("diode", "tvs") and pkg in diode_fp:
        return "Diode_SMD:" + diode_fp[pkg]
    if sym is not None and not method.startswith("generic") and sym.footprint:
        return sym.footprint
    return ""


def kicad_block(lib: SymbolLibrary, c: Component, category: str) -> dict[str, Any] | None:
    hit = lib.match(c.mpn, c.package)
    if hit is None:
        hit = _generic(lib, c, category)
    if hit is None:
        return None
    sym, method = hit
    return {
        "symbol": sym.lib_id, "symbol_lib": sym.lib, "symbol_name": sym.name,
        "footprint": _footprint(c, category, sym, method),
        "pins": sym.pins,
        "pin_source": "kicad_generic" if method.startswith("generic") else "kicad_symbol",
        "match_method": method,
        "kicad_datasheet": sym.datasheet,
        "source_id": KICAD_SOURCE_ID,
    }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _repo_mpns() -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for p in json.loads(MOCK_PATH.read_text()):
        refs.setdefault(p["mpn"], []).append("cvhack/mocks/parts.json")
    for p in json.loads(B_PATH.read_text())["parts"]:
        refs.setdefault(p["mpn"], []).append("data/parts.json")
    for mpn in MEDICAL_MPNS:
        refs.setdefault(mpn, [])
    return refs


def _rank(c: Component, has_pins: bool, b: Bucket) -> tuple:
    return ((has_pins if b.needs_pins else 0), b.prefer(c) if b.prefer else 0,
            c.is_basic or c.is_preferred, c.is_basic, math.log10(c.stock + 1), c.lcsc)


def select(cat: Catalog, lib: SymbolLibrary, target: int) -> tuple[list[tuple[Bucket, Component, dict | None]], list[str]]:
    repo = _repo_mpns()
    by_sub: dict[int, list[Component]] = {}
    for b in PLAN:
        for sid in b.subcategories:
            if sid not in by_sub:
                by_sub[sid] = [c for c in cat.components(sid) if (c.status or "active").lower() == "active"]

    chosen: list[tuple[Bucket, Component, dict | None]] = []
    used_mpns: set[str] = set()
    counts: dict[int, int] = {}

    # 1. Repo MPNs the catalog stocks, in whichever bucket covers their subcategory.
    missing: list[str] = []
    for mpn in repo:
        best = None
        for i, b in enumerate(PLAN):
            for sid in b.subcategories:
                for c in by_sub[sid]:
                    if c.mpn.lower() == mpn.lower() and (best is None or c.stock > best[1].stock):
                        best = (i, c)
        if best is None:
            missing.append(mpn)
            continue
        i, c = best
        chosen.append((PLAN[i], c, kicad_block(lib, c, PLAN[i].category)))
        used_mpns.add(c.mpn.lower())
        counts[i] = counts.get(i, 0) + 1

    # 2. Fill each bucket to quota.
    for i, b in enumerate(PLAN):
        pool = []
        seen_keys = {b.dedupe(c) for bb, c, _ in chosen if bb is b} if b.dedupe else set()
        for sid in b.subcategories:
            for c in by_sub[sid]:
                if c.mpn.lower() in used_mpns or not c.manufacturer or not b.accept(c):
                    continue
                kb = kicad_block(lib, c, b.category) if b.needs_pins else None
                pool.append((c, kb))
        pool.sort(key=lambda x: _rank(x[0], x[1] is not None, b), reverse=True)
        per_mfr: dict[str, int] = {}
        for bb, c, _ in chosen:
            if bb is b:
                per_mfr[c.manufacturer or ""] = per_mfr.get(c.manufacturer or "", 0) + 1
        for c, kb in pool:
            if counts.get(i, 0) >= b.quota:
                break
            if c.mpn.lower() in used_mpns:
                continue
            key = b.dedupe(c) if b.dedupe else None
            if key is not None and key in seen_keys:
                continue
            m = c.manufacturer or ""
            if b.per_manufacturer and per_mfr.get(m, 0) >= b.per_manufacturer:
                continue
            chosen.append((b, c, kb if b.needs_pins else kicad_block(lib, c, b.category)))
            used_mpns.add(c.mpn.lower())
            seen_keys.add(key)
            per_mfr[m] = per_mfr.get(m, 0) + 1
            counts[i] = counts.get(i, 0) + 1

    if len(chosen) > target:
        # Trim the largest passive buckets first; they are the most interchangeable.
        protected = {mpn.lower() for mpn in repo}
        for cat_name in ("resistor", "capacitor", "tvs", "ldo"):
            while len(chosen) > target:
                idx = next((j for j in range(len(chosen) - 1, -1, -1)
                            if chosen[j][0].category == cat_name and chosen[j][1].mpn.lower() not in protected), None)
                if idx is None:
                    break
                chosen.pop(idx)
    return chosen, missing


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _datasheet_publisher(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if host.endswith("jlcpcb.com") or host.endswith("lcsc.com"):
        return "JLCPCB/LCSC-hosted manufacturer datasheet"
    return host


def record(b: Bucket, c: Component, kb: dict | None, snapshot: str, catalog_source: str,
           repo_refs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    src_ids = [catalog_source]
    ds_id = None
    if c.datasheet:
        ds_id = f"src:datasheet:{c.lcsc.lower()}"
        sources.append({"source_id": ds_id, "publisher": _datasheet_publisher(c.datasheet),
                        "document": f"{c.manufacturer} {c.mpn} datasheet", "url": c.datasheet,
                        "kind": "datasheet", "checked_at": None})
        src_ids.append(ds_id)
    if kb:
        src_ids.append(KICAD_SOURCE_ID)
    tiers = [{"min_qty": t.get("qFrom"), "max_qty": t.get("qTo"), "unit_amount": t.get("price")}
             for t in c.price_tiers]
    unit = tiers[0]["unit_amount"] if tiers else None
    lcsc_url = f"https://www.lcsc.com/product-detail/{c.lcsc}.html"
    area = outline_area(c.package)
    rec: dict[str, Any] = {
        "part_id": part_id(c.manufacturer, c.mpn),
        "manufacturer": c.manufacturer,
        "mpn": c.mpn,
        "category": b.category,
        "group": b.group,
        "description": c.description,
        "package": c.package,
        "specs": specs_of(c, catalog_source),
        "price": {"unit_amount": unit, "currency": "USD", "quantity": tiers[0]["min_qty"] if tiers else None,
                  "tiers": tiers, "checked_at": snapshot, "source_url": lcsc_url, "source_id": catalog_source},
        "stock": {"available_qty": c.stock, "status": "in_stock" if c.stock > 0 else "out_of_stock",
                  "checked_at": snapshot, "source_url": lcsc_url, "source_id": catalog_source},
        "offer": {"distributor": "LCSC / JLCPCB", "sku": c.lcsc,
                  "jlc_library_class": c.attr_text("Basic/Extended"),
                  "jlc_assembly_joints": c.joints,
                  "jlc_url": f"https://jlcpcb.com/partdetail/{c.lcsc}",
                  "catalog_category": f"{c.category} / {c.subcategory}"},
        "datasheet_url": c.datasheet,
        "kicad": kb,
        "area_mm2": {"value": area, "basis": "nominal package outline from package name or JEDEC/IPC size"}
        if area is not None else None,
        "sources": src_ids,
    }
    if repo_refs:
        rec["repo_refs"] = repo_refs
    return rec, sources


def build(refresh: bool = False, target: int = 500) -> dict[str, Any]:
    cat = Catalog(refresh=refresh)
    lib = SymbolLibrary(refresh=refresh)
    snapshot = cat.created[:10]
    catalog_source = f"src:jlcparts:catalog-{snapshot}"
    chosen, missing = select(cat, lib, target)
    repo = _repo_mpns()
    repo_lower = {k.lower(): v for k, v in repo.items()}

    parts, ds_sources = [], []
    for b, c, kb in chosen:
        rec, srcs = record(b, c, kb, snapshot, catalog_source, repo_lower.get(c.mpn.lower(), []))
        parts.append(rec)
        ds_sources.extend(srcs)
    parts.sort(key=lambda r: (r["group"], r["category"], r["mpn"].lower()))
    ids = [p["part_id"] for p in parts]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate part_id in selection")

    with_pins = sum(1 for p in parts if p["kicad"] and p["kicad"]["pins"])
    return {
        "schema_version": "1.0.0",
        "dataset_id": "cv-hack-real-parts-catalog",
        "dataset_status": "scraped",
        "retrieved_on": snapshot,
        "scope": {
            "selection": "Real, orderable parts stocked by LCSC/JLCPCB, chosen per category by KiCad pinout "
                         "availability, JLC Basic/Preferred class, and stock. Every MPN the repo already "
                         "references is included when the catalog stocks it.",
            "not_a_design_approval": "Catalog membership is not proof of suitability, regulatory compliance, "
                                     "or a complete design. Validate every selected component against its "
                                     "datasheet, schematic, layout, and operating conditions.",
        },
        "data_policy": {
            "price_stock": "Price tiers (USD) and stock are LCSC/JLCPCB values from the jlcparts snapshot dated "
                           "checked_at. They change daily; rerun the scraper before relying on them.",
            "specs": "Parametric values are distributor-reported (JLCPCB/LCSC catalog), normalized to SI units. "
                     "They are screening data, not datasheet-verified limits.",
            "pins": "pin_source 'kicad_symbol' means the MPN matched a KiCad library symbol whose footprint or "
                    "footprint filter agrees with the catalog package. 'kicad_generic' means a conventional "
                    "two-terminal or single-row-header symbol. kicad=null means no pinout is known; the "
                    "electrical verifier reports PIN_UNKNOWN rather than guessing.",
            "power": "Typical power draw is not in the catalog; parts here carry no power_mw.",
            "area": "area_mm2 is a nominal package outline, not a courtyard.",
        },
        "sources": [
            {"source_id": catalog_source, "publisher": "JLCPCB / LCSC via jlcparts (yaqwsx)",
             "document": "JLCPCB assembly parts catalog snapshot", "url": SOURCE_PAGE,
             "kind": "distributor_catalog", "checked_at": snapshot},
            {"source_id": KICAD_SOURCE_ID, "publisher": "KiCad Library Team",
             "document": "KiCad official symbol library (master)", "url": REPO_URL,
             "kind": "eda_library", "checked_at": snapshot},
            *ds_sources,
        ],
        "coverage": {
            "parts": len(parts),
            "with_pins": with_pins,
            "with_kicad_symbol_match": sum(1 for p in parts if p["kicad"] and p["kicad"]["pin_source"] == "kicad_symbol"),
            "with_datasheet": sum(1 for p in parts if p["datasheet_url"]),
            "repo_mpns_found": sum(1 for p in parts if p.get("repo_refs")),
            "repo_mpns_not_stocked": sorted(missing),
            "by_category": {k: sum(1 for p in parts if p["category"] == k)
                            for k in sorted({p["category"] for p in parts})},
        },
        "parts": parts,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="re-download the catalog and KiCad library")
    ap.add_argument("--target", type=int, default=500)
    ap.add_argument("--no-medical", action="store_true", help="skip the medical-evidence enrichment")
    args = ap.parse_args()
    data = build(refresh=args.refresh, target=args.target)
    if not args.no_medical:
        from cvhack.scrape import medical
        medical.enrich(data)
    OUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    cov = data["coverage"]
    print(f"Wrote {OUT_PATH.relative_to(ROOT)}: {cov['parts']} parts, {cov['with_pins']} with pins "
          f"({cov['with_kicad_symbol_match']} matched KiCad symbols), {cov['repo_mpns_found']} repo MPNs found")
    print("by category:", cov["by_category"])


if __name__ == "__main__":
    main()
