"""Design intent for a run, loaded from data/spec.json (SPEC_PATH=... to use another spec).

SUBSYSTEMS[name]: max_rail_v (highest voltage any part can see), allowed part categories, required_any
(at least one part from this set), and the interface nets the subsystem drives / uses when wired.
REQUIREMENTS: stable IDs with a machine check each, evaluated after the design is wired (src/requirements.py)."""
import json
import os
from pathlib import Path

SPEC_PATH = Path(os.environ.get("SPEC_PATH") or Path(__file__).resolve().parent.parent / "data" / "spec.json")
SPEC = json.loads(SPEC_PATH.read_text())

DEVICE = SPEC["device"]
DEVICE_CLASS = SPEC.get("device_class", "II")
DESIGN_NAME = SPEC.get("design_name", "design")
BUDGET = {k: float(v) for k, v in SPEC["budget"].items() if not k.startswith("_")}
SUBSYSTEMS = {name: {**{k: v for k, v in s.items() if not k.startswith("_")},
                     "allowed": set(s["allowed"]), "required_any": set(s["required_any"]),
                     "drives": list(s.get("drives", [])), "uses": list(s.get("uses", []))}
              for name, s in SPEC["subsystems"].items()}
INTERFACE_NETS: dict[str, str] = SPEC.get("interface_nets", {})
POWER_FLAGS: list[str] = SPEC.get("power_flags", ["GND"])
REQUIREMENTS: list[dict] = SPEC.get("requirements", [])
