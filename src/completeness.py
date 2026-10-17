"""Completeness: what each subsystem must contain to be a buildable board, as plain code.

Taken from the design rules (USB-C CC termination, battery + charger rail, I2C pull-ups, charge LED resistor,
debug header) and from the gaps the critic raised in every earlier run. A subsystem option that lacks these is
refused at submit time, and the mother cannot finish without all four subsystems complete. This is what a
single-shot answer cannot be talked into: no argument satisfies a check that counts parts."""
from cvhack.parts import load_parts


def _n(items, pred):
    return sum(q for p, q in items if pred(p))


def _val(p):
    return (p.value or "").lower().replace(" ", "").replace("ohm", "")


def _desc(p):
    return (p.description or "").lower()


R = lambda *vals: (lambda p: p.category in {"resistor", "passive"} and _val(p) in vals)
CAP = lambda p: p.category in {"capacitor", "passive"} and "f" in _val(p)
IC = {"mcu", "afe", "ldo", "dcdc", "charger", "rf_module"}

# subsystem -> [(need, test(items) -> bool, hint for the agent)]
NEEDS = {
    "power": [
        ("Battery connector", lambda i: _n(i, lambda p: p.category == "connector" and ("battery" in _desc(p) or "jst" in _desc(p))) >= 1,
         "add a battery connector (category=connector, text='battery' or 'JST')"),
        ("Charger", lambda i: _n(i, lambda p: p.category == "charger") >= 1, "add a Li-ion charger (category=charger)"),
        ("Voltage regulator", lambda i: _n(i, lambda p: p.category in {"ldo", "dcdc"}) >= 1, "add a regulator (ldo or dcdc)"),
        ("Decoupling capacitors", lambda i: _n(i, CAP) >= max(2, _n(i, lambda p: p.category in {"ldo", "dcdc", "charger"})),
         "add at least one capacitor per regulator/charger (category=capacitor)"),
        ("Charge LED + series resistor", lambda i: _n(i, lambda p: p.category == "led") >= 1 and _n(i, lambda p: p.category in {"resistor", "passive"}) >= 1,
         "add a status LED (category=led) and a series resistor"),
    ],
    "protection": [
        ("USB input connector", lambda i: _n(i, lambda p: p.category == "connector" and "usb" in _desc(p)) >= 1,
         "add a USB-C receptacle (category=connector, text='USB')"),
        ("CC termination (2 x 5.1k)", lambda i: _n(i, R("5.1k")) >= 2,
         "add two 5.1k resistors (category=resistor, spec_ranges {\"resistance\": [5000, 5200]})"),
        ("TVS diode", lambda i: _n(i, lambda p: p.category == "tvs") >= 1, "add a TVS (category=tvs)"),
        ("Fuse", lambda i: _n(i, lambda p: p.category == "fuse") >= 1, "add a resettable fuse (category=fuse)"),
    ],
    "mcu_comms": [
        ("I2C pull-ups (2 x 4.7k)", lambda i: _n(i, R("4.7k")) >= 2,
         "add two 4.7k resistors (category=resistor, spec_ranges {\"resistance\": [4600, 4800]})"),
        ("Programming/debug header", lambda i: _n(i, lambda p: p.category == "connector" and ("tag" in _desc(p) or "swd" in _desc(p) or "tc2030" in p.mpn.lower())) >= 1,
         "add a 5-pin SWD / Tag-Connect header (category=connector, text='Tag')"),
        ("MCU bypass capacitor", lambda i: _n(i, CAP) >= 1, "add a bypass capacitor (category=capacitor)"),
    ],
    "analog_fe": [
        ("Sensor decoupling capacitors", lambda i: _n(i, CAP) >= 1, "add decoupling capacitors for the sensor (category=capacitor)"),
    ],
}


def _items(raw):
    parts = load_parts()
    return [(parts[x["mpn"]], int(x.get("qty", 1) or 1)) for x in raw if isinstance(x, dict) and x.get("mpn") in parts]


def missing(subsystem: str, raw: list[dict]) -> list[tuple[str, str]]:
    """[(need, hint)] the option lacks, for one subsystem."""
    items = _items(raw)
    return [(name, hint) for name, test, hint in NEEDS.get(subsystem, []) if not test(items)]


def score(bom: dict) -> dict:
    """Design-level completeness of {subsystem: [{"mpn","qty"}]}: passed / total checks and what is missing."""
    total = sum(len(v) for v in NEEDS.values())
    miss = [f"{s}: {n}" for s in NEEDS for n, _ in missing(s, bom.get(s, []))]
    return {"passed": total - len(miss), "total": total, "missing": miss}
