"""Real KiCad library symbols for generated schematics.

    sym = load_symbol("Device", "R")          # None if the library or symbol is missing
    sym.pins                                  # [LibPin(num, name, type, x, y, angle, length, hidden)]
    sym.sexpr("Device:R")                     # flattened (symbol ...) block for lib_symbols

Libraries come from the KiCad install (KICAD_SYMBOL_DIR, or next to kicad-cli).
Only the symbol's own block is parsed, so the multi-MB library files stay
cheap. Derived symbols (extends) are flattened onto their base, which is how
KiCad stores them inside a .kicad_sch.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# S-expressions
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


class Q(str):
    """A string that was quoted in the source, so it is written back quoted."""


def parse(text: str):
    stack: list[list] = [[]]
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            node = stack.pop()
            stack[-1].append(node)
        elif t[0] == '"':
            stack[-1].append(Q(re.sub(r'\\(.)', r'\1', t[1:-1])))
        else:
            stack[-1].append(t)
    return stack[0][0] if stack[0] else None


def dump(node, indent: int = 0) -> str:
    if not isinstance(node, list):
        if isinstance(node, Q):
            return '"' + node.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return str(node)
    if not any(isinstance(x, list) for x in node):
        return "(" + " ".join(dump(x) for x in node) + ")"
    pad = "\t" * (indent + 1)
    head = [dump(x) for x in node if not isinstance(x, list)]
    kids = [pad + dump(x, indent + 1) for x in node if isinstance(x, list)]
    return "(" + " ".join(head) + "\n" + "\n".join(kids) + "\n" + "\t" * indent + ")"


def find(node: list, key: str) -> list | None:
    return next((x for x in node if isinstance(x, list) and x and x[0] == key), None)


def find_all(node: list, key: str) -> list[list]:
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


# ---------------------------------------------------------------------------
# Library lookup
# ---------------------------------------------------------------------------

def symbol_dir() -> Path | None:
    env = os.environ.get("KICAD_SYMBOL_DIR")
    cands = [Path(env)] if env else []
    from cvhack.tooling import kicad_cli
    cli = kicad_cli.cli_path()
    if cli:
        cands.append(Path(cli).resolve().parent.parent / "SharedSupport" / "symbols")   # macOS bundle
        cands.append(Path(cli).resolve().parent.parent / "share" / "kicad" / "symbols")  # Linux prefix
    cands += [Path("/usr/share/kicad/symbols"), Path("/usr/local/share/kicad/symbols")]
    return next((c for c in cands if c.is_dir()), None)


@lru_cache(maxsize=64)
def _lib_text(lib: str) -> str | None:
    d = symbol_dir()
    f = d / f"{lib}.kicad_sym" if d else None
    return f.read_text() if f and f.exists() else None


def _block(text: str, name: str) -> list | None:
    m = re.search(r'\n\t\(symbol "%s"\s*\n' % re.escape(name), text)
    if not m:
        return None
    start, depth, i, in_str = m.start() + 1, 0, m.start() + 1, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return parse(text[start : i + 1])
        i += 1
    return None


@dataclass
class LibPin:
    num: str
    name: str
    type: str
    x: float          # connection point, symbol coords (y up)
    y: float
    angle: int        # direction from the connection point into the body
    length: float
    hidden: bool
    unit: int


@dataclass
class LibSymbol:
    lib: str
    name: str
    node: list                       # flattened (symbol "Name" ...) node
    pins: list[LibPin] = field(default_factory=list)
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)   # xmin, ymin, xmax, ymax (y up)
    units: int = 1
    is_power: bool = False

    def sexpr(self, lib_id: str) -> str:
        node = [self.node[0], Q(lib_id), *self.node[2:]]
        return dump(node, 1)

    def pins_by_num(self) -> dict[str, list[LibPin]]:
        out: dict[str, list[LibPin]] = {}
        for p in self.pins:
            out.setdefault(p.num, []).append(p)
        return out


def _unit_of(sub_name: str) -> tuple[int, int]:
    m = re.search(r"_(\d+)_(\d+)$", sub_name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _points(node) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    if not isinstance(node, list) or not node:
        return pts
    tag = node[0]
    if tag in ("property", "pin", "text"):
        return pts
    if tag in ("xy", "start", "end", "mid", "center") and len(node) >= 3:
        try:
            x, y = float(node[1]), float(node[2])
        except ValueError:
            return pts
        pts.append((x, y))
    if tag == "circle":
        c, r = find(node, "center"), find(node, "radius")
        if c and r:
            cx, cy, rr = float(c[1]), float(c[2]), float(r[1])
            return [(cx - rr, cy - rr), (cx + rr, cy + rr)]
    for x in node[1:]:
        pts += _points(x)
    return pts


def _flatten(lib: str, name: str, text: str) -> list | None:
    node = _block(text, name)
    if node is None:
        return None
    ext = find(node, "extends")
    if not ext:
        return node
    base = _flatten(lib, str(ext[1]), text)
    if base is None:
        return None
    # Derived symbol: its own properties/flags over the base's graphics and pins.
    own_props = {str(p[1]): p for p in find_all(node, "property")}
    out = [base[0], Q(name)]
    for x in base[2:]:
        if isinstance(x, list) and x[0] == "property" and str(x[1]) in own_props:
            out.append(own_props.pop(str(x[1])))
        elif isinstance(x, list) and x[0] == "symbol":
            u, s = _unit_of(str(x[1]))
            out.append([x[0], Q(f"{name}_{u}_{s}"), *x[2:]])
        else:
            out.append(x)
    out.extend(own_props.values())
    return out


@lru_cache(maxsize=256)
def load_symbol(lib: str, name: str) -> LibSymbol | None:
    text = _lib_text(lib)
    if text is None or not name:
        return None
    node = _flatten(lib, name, text)
    if node is None:
        return None
    sym = LibSymbol(lib=lib, name=name, node=node, is_power=find(node, "power") is not None)
    pts: list[tuple[float, float]] = []
    units = set()
    for sub in find_all(node, "symbol"):
        unit, style = _unit_of(str(sub[1]))
        if style == 2:              # De Morgan alternate body; ignore
            continue
        if unit:
            units.add(unit)
        pts += _points(sub)
        for p in find_all(sub, "pin"):
            at, ln = find(p, "at"), find(p, "length")
            nm, nb = find(p, "name"), find(p, "number")
            x, y, a = float(at[1]), float(at[2]), int(float(at[3])) if len(at) > 3 else 0
            length = float(ln[1]) if ln else 2.54
            hidden = "hide" in p or any(isinstance(z, list) and z[:2] == ["hide", "yes"] for z in p)
            sym.pins.append(LibPin(num=str(nb[1]) if nb else "", name=str(nm[1]) if nm else "",
                                   type=str(p[1]), x=x, y=y, angle=a % 360, length=length,
                                   hidden=hidden, unit=unit))
            pts.append((x, y))
    sym.units = max(units) if units else 1
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        sym.bbox = (min(xs), min(ys), max(xs), max(ys))
    return sym
