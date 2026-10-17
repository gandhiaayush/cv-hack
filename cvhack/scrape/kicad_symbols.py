"""KiCad's official symbol library (https://gitlab.com/kicad/libraries/kicad-symbols).

Gives catalog parts real pinouts: pin numbers, names and the ERC electrical
types the verifiers depend on, plus the library's default footprint and
datasheet link. A symbol is attached to an MPN only when the name matches and
the symbol's footprint (or pin count) agrees with the catalog package; a wrong
pinout is worse than none, so anything doubtful stays unmatched and the
electrical gate keeps reporting PIN_UNKNOWN for it.
"""

from __future__ import annotations

import io
import re
import tarfile
from dataclasses import dataclass, field

import requests

from cvhack.scrape import CACHE

ARCHIVE_URL = "https://gitlab.com/kicad/libraries/kicad-symbols/-/archive/master/kicad-symbols-master.tar.gz"
REPO_URL = "https://gitlab.com/kicad/libraries/kicad-symbols"
ARCHIVE = CACHE / "kicad" / "kicad-symbols-master.tar.gz"

# Libraries searched for MPN matches. Device and Connector_Generic are only used
# through the generic two-terminal / header symbols in build.py.
LIBRARIES = (
    "Amplifier_Current", "Amplifier_Instrumentation", "Amplifier_Operational", "Analog_ADC",
    "Battery_Management", "Comparator", "Converter_DCDC", "Diode", "Interface_USB", "Isolator",
    "Isolator_Analog", "LED",
    "MCU_Espressif", "MCU_Microchip_ATmega", "MCU_Microchip_ATtiny", "MCU_Microchip_SAMD",
    "MCU_Nordic", "MCU_RaspberryPi", "MCU_ST_STM32C0", "MCU_ST_STM32F0", "MCU_ST_STM32F1",
    "MCU_ST_STM32F4", "MCU_ST_STM32G0", "MCU_ST_STM32G4", "MCU_ST_STM32L0", "MCU_ST_STM32L4",
    "MCU_ST_STM32U0", "MCU_ST_STM32WB", "MCU_WCH_CH32V0", "MCU_WCH_CH32V2", "MCU_WCH_CH32V3",
    "Memory_EEPROM", "Memory_Flash", "Oscillator", "Power_Protection", "Power_Supervisor",
    "Power_Management", "Reference_Voltage", "Regulator_Linear", "Regulator_Switching",
    "RF_Bluetooth", "RF_Module", "Sensor", "Sensor_Humidity", "Sensor_Motion", "Sensor_Optical",
    "Sensor_Pressure", "Sensor_Temperature", "Timer_RTC", "Transistor_BJT", "Transistor_FET",
    "Device", "Connector_Generic",
)

PIN_TYPE_MAP = {"free": "unspecified", "open_emitter": "open_collector"}


@dataclass
class Symbol:
    lib: str
    name: str
    extends: str | None
    properties: dict[str, str]
    pins: list[dict[str, str]] = field(default_factory=list)

    @property
    def lib_id(self) -> str:
        return f"{self.lib}:{self.name}"

    @property
    def footprint(self) -> str:
        return self.properties.get("Footprint", "")

    @property
    def datasheet(self) -> str | None:
        ds = self.properties.get("Datasheet", "")
        return ds if ds.startswith("http") else None


# ---------------------------------------------------------------------------
# s-expression parsing (data only)
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def _parse(text: str) -> list:
    stack: list[list] = [[]]
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif t.startswith('"'):
            stack[-1].append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(t)
    return stack[0]


def _children(node: list, head: str) -> list[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == head]


def _symbol_from_sexpr(lib: str, node: list) -> Symbol:
    name = node[1]
    props = {c[1]: c[2] for c in _children(node, "property") if len(c) > 2}
    ext = _children(node, "extends")
    pins: dict[str, dict[str, str]] = {}
    for unit in _children(node, "symbol"):
        # Sub-symbols are NAME_<unit>_<style>; style 2 is the De Morgan alternate.
        if unit[1].rsplit("_", 1)[-1] not in ("0", "1"):
            continue
        for pin in _children(unit, "pin"):
            num = next((c[1] for c in _children(pin, "number")), None)
            pname = next((c[1] for c in _children(pin, "name")), "~")
            if num is None or num in pins:
                continue
            etype = PIN_TYPE_MAP.get(pin[1], pin[1])
            pins[num] = {"num": num, "name": pname, "type": etype}
    return Symbol(lib=lib, name=name, extends=ext[0][1] if ext else None,
                  properties=props, pins=list(pins.values()))


class SymbolLibrary:
    def __init__(self, refresh: bool = False):
        if refresh or not ARCHIVE.exists():
            ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(ARCHIVE_URL, timeout=600)
            r.raise_for_status()
            ARCHIVE.write_bytes(r.content)
        self.symbols: dict[str, Symbol] = {}       # lib_id -> Symbol
        self._by_lib: dict[str, dict[str, Symbol]] = {}
        wanted = set(LIBRARIES)
        with tarfile.open(fileobj=io.BytesIO(ARCHIVE.read_bytes()), mode="r:gz") as tar:
            for member in tar:
                parts = member.name.split("/")
                if not member.isfile() or len(parts) != 3 or not parts[2].endswith(".kicad_sym"):
                    continue
                lib = parts[1].removesuffix(".kicad_symdir")
                if lib not in wanted:
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                for node in _parse(f.read().decode("utf-8"))[0][1:]:
                    if isinstance(node, list) and node and node[0] == "symbol":
                        sym = _symbol_from_sexpr(lib, node)
                        self.symbols[sym.lib_id] = sym
                        self._by_lib.setdefault(lib, {})[sym.name] = sym
        for sym in self.symbols.values():
            self._resolve(sym)
        # Symbols bucketed by the first two characters of their name, so matching
        # an MPN only tries the symbols that could possibly be its prefix.
        self._patterns: dict[str, list[tuple[Symbol, re.Pattern, str]]] = {}
        for sym in self.symbols.values():
            if sym.lib in ("Device", "Connector_Generic") or not sym.pins:
                continue
            # "TLV70018_SOT23-5" names part "TLV70018" in one package; the package
            # half is checked against the catalog package by package_compatible().
            for name in {sym.name, sym.name.split("_")[0]}:
                plain = _norm(name)
                if len(plain) < 4:
                    continue
                key = name[:2] if "x" not in name[:2] else "*"
                self._patterns.setdefault(_norm(key), []).append((sym, _name_regex(name), plain))

    def _resolve(self, sym: Symbol, depth: int = 0) -> None:
        """Derived symbols inherit pins (and unset properties) from their parent."""
        if not sym.extends or depth > 8:
            return
        parent = self._by_lib.get(sym.lib, {}).get(sym.extends)
        if parent is None:
            return
        self._resolve(parent, depth + 1)
        if not sym.pins:
            sym.pins = [dict(p) for p in parent.pins]
        for k, v in parent.properties.items():
            sym.properties.setdefault(k, v)

    def get(self, lib_id: str) -> Symbol:
        return self.symbols[lib_id]

    def match(self, mpn: str, package: str | None) -> tuple[Symbol, str] | None:
        """Best symbol for an orderable MPN, or None. Returns (symbol, match_method)."""
        target = _norm(mpn)
        best: tuple[int, Symbol, str] | None = None
        candidates = self._patterns.get(target[:2], []) + self._patterns.get("*", [])
        for sym, rx, plain in candidates:
            m = rx.match(target)
            if not m:
                continue
            rest = target[m.end():]
            # The remainder may only be an ordering suffix (packaging, temperature, reel).
            if len(rest) > 6:
                continue
            method = "exact_name" if not rest else "name_prefix"
            if "x" in sym.name:
                method = "wildcard_name" if not rest else "wildcard_prefix"
            ok, why = package_compatible(sym, package)
            if not ok:
                continue
            score = m.end() * 10 - len(rest) + (5 if why == "footprint" else 0)
            if best is None or score > best[0]:
                best = (score, sym, f"{method}+{why}")
        return (best[1], best[2]) if best else None


def _norm(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]", "", s).upper()


def _name_regex(name: str) -> re.Pattern:
    # KiCad writes wildcard characters in symbol names as lowercase 'x'.
    out = []
    for ch in re.sub(r"[^A-Za-z0-9.]", "", name):
        out.append("[A-Z0-9]" if ch == "x" else re.escape(ch.upper()))
    return re.compile("".join(out))


# ---------------------------------------------------------------------------
# Package agreement
# ---------------------------------------------------------------------------

_FAMILIES = [
    (r"SOT-?223", "SOT-223"), (r"SOT-?89", "SOT-89"), (r"SC-?70|SOT-?323|SOT-?363", "SC-70"),
    (r"SOT-?23|TSOT|SOT-?563|SOT-?353", "SOT-23"), (r"TO-?252|DPAK", "TO-252"),
    (r"TO-?263|D2PAK", "TO-263"), (r"TO-?220", "TO-220"), (r"TO-?92", "TO-92"),
    (r"MSOP|VSSOP|UMAX|MICRO8", "MSOP"), (r"TSSOP|HTSSOP", "TSSOP"), (r"SSOP", "SSOP"),
    (r"SOIC|E?SOP|SO-\d", "SOIC"), (r"LQFP|TQFP|QFP", "QFP"),
    (r"QFN|DFN|SON|LGA|UQFN|WQFN|VQFN|WSON|VSON|USON|X2SON", "NOLEAD"), (r"BGA|WLCSP|CSP|DSBGA", "BGA"),
    (r"SOD-?123F", "SOD-123F"), (r"SOD-?123", "SOD-123"), (r"SOD-?323", "SOD-323"),
    (r"SOD-?523", "SOD-523"), (r"SMA|DO-?214AC", "SMA"), (r"SMB|DO-?214AA", "SMB"),
    (r"SMC|DO-?214AB", "SMC"), (r"DIP", "DIP"),
]


def package_family(text: str | None) -> tuple[str | None, int | None]:
    """('SOT-23', 5) for 'SOT-23-5', ('QFP', 48) for 'LQFP-48(7x7)'."""
    if not text:
        return None, None
    t = text.upper().split(":")[-1]
    # Vendor aliases: SOT-25/SOT-753 are SOT-23-5, SOT-26/SOT-457 are SOT-23-6, OESIP is an optical LGA.
    t = re.sub(r"SOT-?(25|753)(-5)?(?!\d)", "SOT-23-5", t)
    t = re.sub(r"SOT-?(26|457)(-6)?(?!\d)", "SOT-23-6", t)
    t = t.replace("OESIP", "OLGA")
    fam = next((f for rx, f in _FAMILIES if re.search(rx, t)), None)
    n = None
    m = re.search(r"(?:SOT-?23|SC-?70|SOT-?323|SOT-?363)[-_](\d+)", t)
    if m:
        n = int(m.group(1))
    elif fam == "SOT-23":
        n = 3
    elif fam in ("SOT-223", "SOT-89", "TO-92"):
        n = 3 if fam != "SOT-223" else 4
    elif fam not in ("SOD-123", "SOD-123F", "SOD-323", "SOD-523", "SMA", "SMB", "SMC"):
        # The pin count is the first "-<n>" outside any "(LxW)" body-size group.
        m = re.search(r"-(\d{1,3})(?![\d.])", re.sub(r"\([^)]*\)", "", t))
        if m:
            n = int(m.group(1))
    return fam, n


def package_compatible(sym: Symbol, package: str | None) -> tuple[bool, str]:
    """Does the symbol's pinout plausibly belong to this catalog package?"""
    fam, n = package_family(package)
    pin_nums = [p["num"] for p in sym.pins]
    numeric = [int(p) for p in pin_nums if p.isdigit()]
    fp = sym.footprint
    if fp:
        sfam, sn = package_family(fp)
        if fam is None or sfam is None or sfam != fam:
            return False, "package_mismatch"
        if n is not None and sn is not None and n != sn and abs(n - sn) != 1:  # +-1 for exposed pad
            return False, "pin_count_mismatch"
        return True, "footprint"
    filters = sym.properties.get("ki_fp_filters", "")
    if filters and package and fam is not None:
        # KiCad lists only footprints that share the symbol's pinout in ki_fp_filters.
        fams = {package_family(re.sub(r"[*?]", "-", f))[0] for f in filters.split()}
        if fam not in fams:
            return False, "fp_filter_mismatch"
        if n is None or not numeric or max(numeric) > n + 1:
            return False, "pin_count_mismatch"
        return True, "fp_filter"
    return False, "no_package_evidence"
