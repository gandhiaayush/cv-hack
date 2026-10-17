"""Design -> KiCad schematic, KiCad netlist and SVG, from one shared layout.

Symbols come from the installed KiCad libraries when the part's
symbol_lib:symbol_name exists there and its pin numbers and names agree with
Part.pins (see lib_symbol_for). Anything else gets a generated box symbol
built from Part.pins. Either way the symbol is embedded in lib_symbols, so
the file opens and passes through kicad-cli with no libraries configured.

Pins connect through net labels at their ends; power nets on top/bottom pins
get real KiCad power symbols (GND, +3V3, ...), whose Value is the net name.
Two-pin parts sitting between two power nets (decoupling caps) stand
vertically between their power symbols, the way a hand-drawn schematic would.
Blocks are laid out as columns, one per subsystem.

    lay = layout(design, parts)
    Path("x.kicad_sch").write_text(to_kicad_sch(design, parts, lay))
    Path("x.svg").write_text(to_svg(design, parts, lay, highlight={"U5"}))
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from html import escape

from cvhack.contracts import Design, Part, Pin
from cvhack.tooling import kicad_lib

G = 2.54                     # pin pitch and placement grid (mm)
PIN_LEN = 2.54
CHAR_W = 1.0                 # approx. width of one 1.27 mm label character
LEFT_TYPES = {"input", "power_in"}
RIGHT_TYPES = {"output", "power_out", "open_collector", "tri_state", "bidirectional"}
MARGIN = 20.32
PAPERS = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]
POWER_SYM_H = 5.08           # room a power symbol plus its value text takes
STUB = 2.54                  # wire between a pin end and its label
MAX_COL_H = 170.0            # wrap a subsystem column taller than this
GND_NAMES = {"GND", "VSS", "GNDA", "GNDD", "AGND", "DGND", "PGND"}
RAIL_GLYPH = {"3V3": "+3V3", "+3V3": "+3V3", "1V8": "+1V8", "+1V8": "+1V8", "5V": "+5V", "+5V": "+5V",
              "VBUS": "VBUS", "VBAT": "+BATT", "+BATT": "+BATT", "VCC": "VCC", "VDD": "VDD"}

# Parts whose catalogue symbol name differs from KiCad's library name.
SYMBOL_ALIASES = {("Sensor_Optical", "MAX30102"): ("Sensor", "MAX30102"),
                  ("RF_Module", "MDBT42Q-512KV2"): ("RF_Module", "MDBT42Q-512K")}


def _snap(v: float, g: float = G) -> float:
    return round(round(v / g) * g, 4)


def _up(v: float, g: float = G) -> float:
    return round(math.ceil(v / g - 1e-9) * g, 4)


# ---------------------------------------------------------------------------
# Library symbols
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[~{}_\s]", "", name).upper()


def _generic(n: str) -> bool:
    return n in ("", "~") or bool(re.fullmatch(r"(PIN|A|K)\d*", n))


def pin_mismatches(part: Part, sym: kicad_lib.LibSymbol) -> list[str]:
    """Why this library symbol can't stand in for part (empty = usable)."""
    if sym.units > 1:
        return [f"{sym.lib}:{sym.name} has {sym.units} units"]
    by_num = sym.pins_by_num()
    out = []
    for p in part.pins:
        lps = by_num.get(p.num)
        if not lps:
            out.append(f"pin {p.num} ({p.name}) not in {sym.lib}:{sym.name}")
            continue
        if len(part.pins) <= 2:
            continue
        on, ln = _norm(p.name), _norm(lps[0].name)
        if _generic(on) or _generic(ln) or on == ln or set(on.split("/")) & set(ln.split("/")) \
                or (len(ln) > 1 and ln in on) or (len(on) > 1 and on in ln):
            continue
        out.append(f"pin {p.num}: catalogue says {p.name}, {sym.lib}:{sym.name} says {lps[0].name}")
    return out


def lib_symbol_for(part: Part) -> kicad_lib.LibSymbol | None:
    lib, name = SYMBOL_ALIASES.get((part.symbol_lib, part.symbol_name), (part.symbol_lib, part.symbol_name))
    sym = kicad_lib.load_symbol(lib, name) if part.pins else None
    return sym if sym is not None and not pin_mismatches(part, sym) else None


def symbol_report(design: Design, parts: dict[str, Part]) -> dict[str, str]:
    """mpn -> which symbol the schematic uses, or why it fell back to a box."""
    out = {}
    for inst in design.all_parts():
        p = parts.get(inst.mpn)
        if p is None or p.mpn in out:
            continue
        lib, name = SYMBOL_ALIASES.get((p.symbol_lib, p.symbol_name), (p.symbol_lib, p.symbol_name))
        sym = kicad_lib.load_symbol(lib, name)
        if sym is None:
            out[p.mpn] = f"generated (no {lib}:{name} in the KiCad libraries)"
        else:
            why = pin_mismatches(p, sym)
            out[p.mpn] = f"{lib}:{name}" if not why else "generated (" + "; ".join(why[:3]) + ")"
    return out


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

@dataclass
class PinPlace:
    pin: Pin
    side: str                 # where the endpoint sits relative to the body: "L" | "R" | "U" | "D"
    dx: float                 # endpoint offset from symbol origin, schematic coords (y down)
    dy: float
    net: str | None = None
    nc: bool = False
    length: float = PIN_LEN
    power: str | None = None  # power symbol lib name to attach (e.g. "GND"), else a label
    glabel: bool = False      # power net on a side pin: global label (same net as the power symbols)
    extra: bool = False       # library pin the catalogue doesn't model (gets a no-connect flag)
    hidden: bool = False


@dataclass
class PartPlace:
    ref: str
    mpn: str
    part: Part
    subsystem: str
    x: float
    y: float
    half_w: float             # body half width
    top: float                # body top/bottom offsets from origin (y down, top < 0)
    bottom: float
    pins: list[PinPlace] = field(default_factory=list)
    lib: kicad_lib.LibSymbol | None = None
    rot: int = 0
    x0: float = 0.0           # body extents (schematic coords, relative to origin)
    x1: float = 0.0
    text_right: bool = False  # ref/value beside the body (vertical 2-pin parts) instead of above

    def pin_xy(self, pp: PinPlace) -> tuple[float, float]:
        return round(self.x + pp.dx, 4), round(self.y + pp.dy, 4)

    @property
    def lib_id(self) -> str:
        return f"{self.lib.lib}:{self.lib.name}" if self.lib else "cvhack:" + _sym_name(self.mpn)


@dataclass
class Flag:
    ref: str
    net: str
    x: float                  # pin endpoint
    y: float


@dataclass
class Layout:
    parts: list[PartPlace]
    flags: list[Flag]
    titles: list[tuple[str, float, float]]   # (text, x, y)
    width: float
    height: float
    paper: tuple[str, float, float]


def _sides(pins: list[Pin]) -> tuple[list[Pin], list[Pin]]:
    if len(pins) <= 2:
        return pins[:1], pins[1:]
    left = [p for p in pins if p.type in LEFT_TYPES]
    right = [p for p in pins if p.type in RIGHT_TYPES]
    for p in pins:
        if p not in left and p not in right:
            (left if len(left) <= len(right) else right).append(p)
    return left, right


def _body(part: Part) -> tuple[list[Pin], list[Pin], float, float, float]:
    left, right = _sides(part.pins)
    n = max(len(left), len(right), 1)
    two_pin = len(part.pins) <= 2
    name_w = 0.0 if two_pin else max([len(p.name) for p in part.pins] or [0]) * CHAR_W
    half_w = _up(max(2.54 if two_pin else 5.08, name_w + 1.27 if two_pin else name_w + 2.0))
    top_pin = (n - 1) // 2 * G
    top = -(top_pin + G / 2 + (0 if two_pin else G / 2))
    bottom = (n - 1 - (n - 1) // 2) * G + G / 2 + (0 if two_pin else G / 2)
    return left, right, half_w, round(top, 4), round(bottom, 4)


def _rot(x: float, y_up: float, r: int) -> tuple[float, float]:
    """Library point (y up) -> schematic offset (y down) for a symbol rotated r degrees CCW."""
    x, y = x, -y_up
    if r == 90:
        x, y = y, -x
    elif r == 180:
        x, y = -x, -y
    elif r == 270:
        x, y = -y, x
    return round(x, 4), round(y, 4)


_SIDE = {0: "L", 90: "D", 180: "R", 270: "U"}


def power_nets(design: Design, parts: dict[str, Part]) -> set[str]:
    """Nets drawn with power symbols: grounds, flagged supplies, and nets touching power pins."""
    out = {n for n in design.merged_nets() if n.upper() in GND_NAMES} | set(design.power_flags)
    ref_mpn = {i.ref: i.mpn for i in design.all_parts()}
    for name, nodes in design.merged_nets().items():
        for n in nodes:
            p = parts.get(ref_mpn.get(n.ref, ""))
            pin = next((x for x in (p.pins if p else []) if x.num == n.pin), None)
            if pin and pin.type in ("power_in", "power_out"):
                out.add(name)
    return out


def power_glyph(net: str) -> str:
    if net.upper() in GND_NAMES:
        return "GND"
    return RAIL_GLYPH.get(net, RAIL_GLYPH.get(net.upper(), "VCC"))


def _power_rot(glyph: str, side: str) -> int:
    """Rotation so the glyph points away from the body."""
    down = glyph == "GND"
    if side == "U":
        return 180 if down else 0
    if side == "D":
        return 0 if down else 180
    if side == "L":
        return 270 if down else 90
    return 90 if down else 270


def _place_lib(inst, p: Part, sym: kicad_lib.LibSymbol, nets_of, ncs, pnets) -> PartPlace:
    model = {x.num: x for x in p.pins}
    rot = 0
    if len(p.pins) == 2:
        a, b = (nets_of.get((inst.ref, x.num)) for x in p.pins)
        lp = {x.num: x for x in sym.pins}
        side0 = {num: _SIDE[lp[num].angle] for num in model}
        opposite = set(side0.values()) in ({"U", "D"}, {"L", "R"})   # not e.g. a 1x02 header
        vertical = opposite and bool(a and b and a in pnets and b in pnets)
        is_vertical = all(s in "UD" for s in side0.values())
        if opposite and vertical != is_vertical:
            rot = 90
        if vertical:
            # Ground at the bottom, the rail on top.
            top_num = next(n for n in model if _SIDE[(lp[n].angle + rot) % 360] == "U")
            if (nets_of.get((inst.ref, top_num)) or "").upper() in GND_NAMES:
                rot = (rot + 180) % 360
    pins = []
    for lp in sym.pins:
        dx, dy = _rot(lp.x, lp.y, rot)
        side = _SIDE[(lp.angle + rot) % 360]
        key = (inst.ref, lp.num)
        pin = model.get(lp.num) or Pin(num=lp.num, name=lp.name, type="no_connect")
        net = nets_of.get(key) if lp.num in model else None
        pins.append(PinPlace(pin=pin, side=side, dx=dx, dy=dy, net=net, length=lp.length, hidden=lp.hidden,
                             extra=lp.num not in model,
                             nc=lp.num in model and (key in ncs or pin.type == "no_connect") and key not in nets_of,
                             power=power_glyph(net) if net and net in pnets and side in "UD" else None,
                             glabel=bool(net and net in pnets and side in "LR")))
    xs, ys = [], []
    for cx, cy in [(sym.bbox[0], sym.bbox[1]), (sym.bbox[2], sym.bbox[3]), (sym.bbox[0], sym.bbox[3]), (sym.bbox[2], sym.bbox[1])]:
        x, y = _rot(cx, cy, rot)
        xs.append(x)
        ys.append(y)
    pp = PartPlace(ref=inst.ref, mpn=inst.mpn, part=p, subsystem="", x=0, y=0,
                   half_w=max(-min(xs), max(xs)), top=min(ys), bottom=max(ys), pins=pins, lib=sym, rot=rot,
                   x0=min(xs), x1=max(xs))
    pp.text_right = len(p.pins) == 2 and all(x.side in "UD" for x in pins if not x.extra)
    return pp


def _place_box(inst, p: Part, nets_of, ncs, pnets) -> PartPlace:
    left, right, half_w, top, bottom = _body(p)
    pp = PartPlace(ref=inst.ref, mpn=inst.mpn, part=p, subsystem="", x=0, y=0,
                   half_w=half_w, top=top, bottom=bottom, x0=-half_w, x1=half_w)
    top_pin = (max(len(left), len(right), 1) - 1) // 2 * G
    for side, plist in (("L", left), ("R", right)):
        for k, pin in enumerate(plist):
            dx = -(half_w + PIN_LEN) if side == "L" else half_w + PIN_LEN
            dy = round(-top_pin + k * G, 4)
            key = (inst.ref, pin.num)
            net = nets_of.get(key)
            pp.pins.append(PinPlace(pin=pin, side=side, dx=dx, dy=dy, net=net,
                                    nc=(key in ncs or pin.type == "no_connect") and key not in nets_of,
                                    glabel=bool(net and net in pnets and kicad_lib.symbol_dir())))
    pp.x0, pp.x1 = -(half_w + PIN_LEN), half_w + PIN_LEN
    return pp


def _ext(pp: PartPlace) -> dict[str, float]:
    """Room needed beyond the body on each side for labels, power symbols and text."""
    e = {"L": 0.0, "R": 0.0, "U": 0.0, "D": 0.0}
    for x in pp.pins:
        if x.hidden and not x.net:
            continue
        need = POWER_SYM_H if x.power else \
            (len(x.net) * CHAR_W + 3.0 + STUB + (2.0 if x.glabel else 0) if x.net else 1.27)
        e[x.side] = max(e[x.side], need)
    if pp.text_right:
        e["R"] += max(len(pp.ref), len(pp.part.value or pp.mpn)) * CHAR_W + 2.54
    else:
        e["U"] += 5.08
    return e


def layout(design: Design, parts: dict[str, Part]) -> Layout:
    nets_of: dict[tuple[str, str], str] = {}
    for name, nodes in design.merged_nets().items():
        for n in nodes:
            nets_of.setdefault((n.ref, n.pin), name)
    ncs = {(n.ref, n.pin) for n in design.no_connects}
    pnets = power_nets(design, parts)

    placed: list[PartPlace] = []
    titles: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    x_cursor = MARGIN
    max_bottom = MARGIN
    for block in design.blocks:
        insts = [i for i in block.parts if i.ref not in seen and i.mpn in parts]
        if not insts:
            continue
        col: list[tuple[PartPlace, dict]] = []
        lab_l = lab_r = 0.0
        for inst in insts:
            seen.add(inst.ref)
            p = parts[inst.mpn]
            sym = lib_symbol_for(p)
            pp = _place_lib(inst, p, sym, nets_of, ncs, pnets) if sym else _place_box(inst, p, nets_of, ncs, pnets)
            pp.subsystem = block.subsystem
            e = _ext(pp)
            lab_l, lab_r = max(lab_l, -pp.x0 + e["L"]), max(lab_r, pp.x1 + e["R"])
            col.append((pp, e))

        cx = _up(x_cursor + lab_l)
        titles.append((block.subsystem, cx - lab_l, MARGIN))
        y0 = MARGIN + 7.62
        y_cursor = y0
        for pp, e in col:
            h = -pp.top + e["U"] + 2.54 + pp.bottom + e["D"] + 2.54
            if y_cursor > y0 and y_cursor + h - y0 > MAX_COL_H:   # wrap into a new column
                max_bottom = max(max_bottom, y_cursor)
                cx = _up(cx + lab_r + 7.62 + lab_l)
                y_cursor = y0
            cy = _up(y_cursor - pp.top + e["U"] + 2.54)
            pp.x, pp.y = cx, cy
            placed.append(pp)
            y_cursor = cy + pp.bottom + e["D"] + 2.54
        max_bottom = max(max_bottom, y_cursor)
        x_cursor = cx + lab_r + 10.16

    flags: list[Flag] = []
    fx = MARGIN + 12.7
    fy = _up(max_bottom + 7.62)
    for i, net in enumerate(design.power_flags):
        flags.append(Flag(ref=f"#FLG{i + 1:02d}", net=net, x=_snap(fx), y=fy))
        fx += max(len(net) * CHAR_W + 15.24, 25.4)
    width = max(x_cursor, fx) + MARGIN
    height = fy + (12.7 if flags else 0) + MARGIN
    paper = next(((n, w, h) for n, w, h in PAPERS if w >= width and h >= height), ("User", width, height))
    if paper[0] == "User":
        paper = ("User", _up(width), _up(height))
    return Layout(parts=placed, flags=flags, titles=titles, width=width, height=height, paper=paper)


# ---------------------------------------------------------------------------
# .kicad_sch
# ---------------------------------------------------------------------------

def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "cvhack/" + "/".join(parts)))


def _sym_name(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", mpn)


def _f(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") if v else "0"


FONT = "(effects (font (size 1.27 1.27)))"
FONT_HIDE = "(effects (font (size 1.27 1.27)) (hide yes))"


def _lib_symbol(name: str, part: Part, ref_prefix: str) -> str:
    left, right, half_w, top, bottom = _body(part)
    n = max(len(left), len(right), 1)
    top_pin = (n - 1) // 2 * G
    pins = []
    for side, plist in (("L", left), ("R", right)):
        for k, pin in enumerate(plist):
            # Symbol coords are y-up; a pin's (at) is its connection point and its
            # angle points from there into the body.
            x = -(half_w + PIN_LEN) if side == "L" else half_w + PIN_LEN
            y = top_pin - k * G
            ang = 0 if side == "L" else 180
            pins.append(
                f"      (pin {pin.type} line (at {_f(x)} {_f(y)} {ang}) (length {_f(PIN_LEN)})\n"
                f"        (name {_q(pin.name)} {FONT}) (number {_q(pin.num)} {FONT}))")
    hide_names = " (hide yes)" if len(part.pins) <= 2 else ""
    return (
        f"    (symbol {_q('cvhack:' + name)} (pin_names (offset 1.016){hide_names}) (exclude_from_sim no) (in_bom yes) (on_board yes)\n"
        f"      (property \"Reference\" {_q(ref_prefix)} (at 0 {_f(-top + 1.27)} 0) {FONT})\n"
        f"      (property \"Value\" {_q(part.value or part.mpn)} (at 0 {_f(-bottom - 1.27)} 0) {FONT})\n"
        f"      (property \"Footprint\" {_q(part.footprint)} (at 0 0 0) {FONT_HIDE})\n"
        f"      (property \"Datasheet\" \"\" (at 0 0 0) {FONT_HIDE})\n"
        f"      (property \"Description\" {_q(part.description)} (at 0 0 0) {FONT_HIDE})\n"
        f"      (symbol {_q(name + '_0_1')}\n"
        f"        (rectangle (start {_f(-half_w)} {_f(-top)}) (end {_f(half_w)} {_f(-bottom)})\n"
        f"          (stroke (width 0.254) (type default)) (fill (type background))))\n"
        f"      (symbol {_q(name + '_1_1')}\n" + "\n".join(pins) + "))")


_FLAG_SYM = (
    "    (symbol \"cvhack:PWR_FLAG\" (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes)) (exclude_from_sim no) (in_bom no) (on_board no)\n"
    f"      (property \"Reference\" \"#FLG\" (at 0 1.905 0) {FONT_HIDE})\n"
    f"      (property \"Value\" \"PWR_FLAG\" (at 0 3.81 0) {FONT})\n"
    f"      (property \"Footprint\" \"\" (at 0 0 0) {FONT_HIDE})\n"
    f"      (property \"Datasheet\" \"\" (at 0 0 0) {FONT_HIDE})\n"
    "      (symbol \"PWR_FLAG_0_0\" (pin power_out line (at 0 0 90) (length 0)\n"
    f"        (name \"pwr\" {FONT}) (number \"1\" {FONT})))\n"
    "      (symbol \"PWR_FLAG_0_1\" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27))\n"
    "        (stroke (width 0) (type default)) (fill (type none)))))")

_LABEL_ANGLE = {"L": (180, "right bottom"), "R": (0, "left bottom"), "U": (90, "left bottom"), "D": (270, "right bottom")}


def _label(net: str, x: float, y: float, side: str) -> str:
    ang, just = _LABEL_ANGLE[side]
    return (f"  (label {_q(net)} (at {_f(x)} {_f(y)} {ang}) (fields_autoplaced yes)\n"
            f"    (effects (font (size 1.27 1.27)) (justify {just})) (uuid {_q(_uid('label', net, _f(x), _f(y)))}))")


_OUT = {"L": (-1, 0), "R": (1, 0), "U": (0, -1), "D": (0, 1)}


def _stubbed(net: str, x: float, y: float, side: str, glabel: bool) -> str:
    """A short wire out of the pin, then a (global) label at its end."""
    ux, uy = _OUT[side]
    ex, ey = round(x + ux * STUB, 4), round(y + uy * STUB, 4)
    wire = (f"  (wire (pts (xy {_f(x)} {_f(y)}) (xy {_f(ex)} {_f(ey)})) (stroke (width 0) (type default))"
            f" (uuid {_q(_uid('wire', net, _f(x), _f(y)))}))")
    if not glabel:
        return wire + "\n" + _label(net, ex, ey, side)
    ang, just = _LABEL_ANGLE[side]
    just = just.split()[0]
    return (wire + "\n" +
            f"  (global_label {_q(net)} (shape passive) (at {_f(ex)} {_f(ey)} {ang}) (fields_autoplaced yes)\n"
            f"    (effects (font (size 1.27 1.27)) (justify {just})) (uuid {_q(_uid('glabel', net, _f(ex), _f(ey)))})\n"
            f"    (property \"Intersheetrefs\" \"${{INTERSHEET_REFS}}\" (at {_f(ex)} {_f(ey)} 0)"
            f" (effects (font (size 1.27 1.27)) (justify {just}) (hide yes))))")


def _instance(lib_id: str, ref: str, value: str, x: float, y: float, rot: int, uid: str, project: str, root: str,
              pin_nums: list[str], ref_at: tuple[float, float, str], val_at: tuple[float, float, str],
              extra_props: list[tuple[str, str]] = (), hide_ref: bool = False, in_bom: bool = True,
              footprint: str = "") -> str:
    fa = 90 if rot in (90, 270) else 0   # KiCad draws field angle + symbol rotation; keep text horizontal

    def prop(name, val, at, hide=False, just="center"):
        j = "" if just == "center" else f" (justify {just})"
        h = " (hide yes)" if hide else ""
        return f"    (property {_q(name)} {_q(val)} (at {_f(at[0])} {_f(at[1])} {fa}) (effects (font (size 1.27 1.27)){j}){h})\n"
    pins = "".join(f"    (pin {_q(n)} (uuid {_q(_uid('pin', uid, n))}))\n" for n in pin_nums)
    yn = "yes" if in_bom else "no"
    return (f"  (symbol (lib_id {_q(lib_id)}) (at {_f(x)} {_f(y)} {rot}) (unit 1)\n"
            f"    (exclude_from_sim no) (in_bom {yn}) (on_board {yn}) (dnp no)\n"
            f"    (uuid {_q(_uid('sym', uid))})\n"
            + prop("Reference", ref, ref_at[:2], hide_ref, ref_at[2])
            + prop("Value", value, val_at[:2], False, val_at[2])
            + prop("Footprint", footprint, (x, y), True)
            + prop("Datasheet", "", (x, y), True)
            + "".join(prop(k, v, (x, y), True) for k, v in extra_props)
            + pins
            + f"    (instances (project {_q(project)} (path {_q('/' + root)} (reference {_q(ref)}) (unit 1)))))")


def to_kicad_sch(design: Design, parts: dict[str, Part], lay: Layout | None = None) -> str:
    lay = lay or layout(design, parts)
    root = _uid("sheet", design.name)
    project = design.name
    out = ["(kicad_sch (version 20250114) (generator \"cvhack\") (generator_version \"9.0\")",
           f"  (uuid {_q(root)})"]
    pname, pw, ph = lay.paper
    out.append(f"  (paper \"User\" {_f(pw)} {_f(ph)})" if pname == "User" else f"  (paper {_q(pname)})")
    out.append(f"  (title_block (title {_q(design.name)}) (company \"cvhack\") (comment 1 \"Generated by agents; ERC-gated\"))")

    flag_sym = kicad_lib.load_symbol("power", "PWR_FLAG")
    libs: dict[str, str] = {}
    for pp in lay.parts:
        if pp.lib_id not in libs:
            libs[pp.lib_id] = pp.lib.sexpr(pp.lib_id) if pp.lib else \
                _lib_symbol(_sym_name(pp.mpn), pp.part, re.sub(r"\d+$", "", pp.ref) or "U")
        for x in pp.pins:
            if x.power and f"power:{x.power}" not in libs:
                ps = kicad_lib.load_symbol("power", x.power)
                if ps:
                    libs[f"power:{x.power}"] = ps.sexpr(f"power:{x.power}")
                else:
                    x.power = None
    out.append("  (lib_symbols")
    out.extend(libs.values())
    if lay.flags:
        out.append(flag_sym.sexpr("power:PWR_FLAG") if flag_sym else _FLAG_SYM)
    out.append("  )")

    for text, x, y in lay.titles:
        out.append(f"  (text {_q(text.upper())} (exclude_from_sim no) (at {_f(x)} {_f(y)} 0)\n"
                   f"    (effects (font (size 2.54 2.54) (bold yes)) (justify left bottom)) (uuid {_q(_uid('title', text))}))")

    n_pwr = 0
    for pp in lay.parts:
        value = pp.part.value or pp.mpn
        if pp.text_right:
            tx = pp.x + pp.x1 + 1.27
            ref_at, val_at = (tx, pp.y - 1.27, "left"), (tx, pp.y + 1.27, "left")
        else:
            ty = pp.y + pp.top - (max([POWER_SYM_H for x in pp.pins if x.power and x.side == "U"] or [0]))
            ref_at, val_at = (pp.x, ty - 3.81, "center"), (pp.x, ty - 1.27, "center")
        out.append(_instance(pp.lib_id, pp.ref, value, pp.x, pp.y, pp.rot, pp.ref, project, root,
                             [x.pin.num for x in pp.pins], ref_at, val_at,
                             [("MPN", pp.mpn), ("Subsystem", pp.subsystem)], footprint=pp.part.footprint))
        for x in pp.pins:
            px, py = pp.pin_xy(x)
            if x.net and x.power:
                n_pwr += 1
                r = _power_rot(x.power, x.side)
                vy = {0: -3.81, 180: 3.81}.get(r, 0) if x.power != "GND" else {0: 3.81, 180: -3.81}.get(r, 0)
                vx = {90: -3.81, 270: 3.81}.get(r, 0) if x.power != "GND" else {90: 3.81, 270: -3.81}.get(r, 0)
                out.append(_instance(f"power:{x.power}", f"#PWR{n_pwr:03d}", x.net, px, py, r,
                                     f"pwr/{pp.ref}/{x.pin.num}", project, root, ["1"],
                                     (px, py, "center"), (px + vx, py + vy, "center"), hide_ref=True, in_bom=False))
            elif x.net:
                out.append(_stubbed(x.net, px, py, x.side, x.glabel))
            elif (x.nc or x.extra) and not x.hidden:
                out.append(f"  (no_connect (at {_f(px)} {_f(py)}) (uuid {_q(_uid('nc', pp.ref, x.pin.num))}))")

    for fl in lay.flags:
        # Flag pin points down at (0,0); the label sits on the same point.
        out.append(_instance("power:PWR_FLAG" if flag_sym else "cvhack:PWR_FLAG", fl.ref, "PWR_FLAG",
                             fl.x, fl.y, 0, f"flag/{fl.net}", project, root, ["1"],
                             (fl.x, fl.y - 1.905, "center"), (fl.x, fl.y - 3.81, "center"),
                             hide_ref=True, in_bom=False))
        glyph = power_glyph(fl.net)
        if flag_sym and f"power:{glyph}" in libs:
            # A power symbol, not a label: KiCad keeps labels and power nets of the same name apart.
            n_pwr += 1
            r = 0 if glyph == "GND" else 180
            out.append(_instance(f"power:{glyph}", f"#PWR{n_pwr:03d}", fl.net, fl.x, fl.y, r, f"flagpwr/{fl.net}",
                                 project, root, ["1"], (fl.x, fl.y, "center"), (fl.x + 3.81, fl.y + 3.81, "left"),
                                 hide_ref=True, in_bom=False))
        else:
            out.append(_label(fl.net, fl.x, fl.y, "R"))

    out.append("  (sheet_instances (path \"/\" (page \"1\")))")
    out.append("  (embedded_fonts no)")
    out.append(")")
    return "\n".join(out) + "\n"


def to_kicad_pro(design: Design) -> str:
    import json
    return json.dumps({"meta": {"filename": f"{design.name}.kicad_pro", "version": 1},
                       "sheets": [[_uid("sheet", design.name), "Root"]]}, indent=2) + "\n"


# ---------------------------------------------------------------------------
# KiCad netlist (.net, "E" format): importable by pcbnew without kicad-cli
# ---------------------------------------------------------------------------

def to_kicad_netlist(design: Design, parts: dict[str, Part]) -> str:
    comps, seen = [], set()
    for inst in design.all_parts():
        if inst.ref in seen or inst.mpn not in parts:
            continue
        seen.add(inst.ref)
        p = parts[inst.mpn]
        comps.append(f"    (comp (ref {_q(inst.ref)}) (value {_q(p.value or p.mpn)}) (footprint {_q(p.footprint)})\n"
                     f"      (fields (field (name \"MPN\") {_q(p.mpn)}))\n"
                     f"      (libsource (lib \"cvhack\") (part {_q(_sym_name(p.mpn))}) (description {_q(p.description)})))")
    ref_mpn = {i.ref: i.mpn for i in design.all_parts()}
    nets = []
    for code, (name, nodes) in enumerate(sorted(design.merged_nets().items()), 1):
        ns = []
        for ref, pin in dict.fromkeys((n.ref, n.pin) for n in nodes):
            p = parts.get(ref_mpn.get(ref, ""))
            pn = next((x for x in (p.pins if p else []) if x.num == pin), None)
            extra = f" (pinfunction {_q(pn.name)}) (pintype {_q(pn.type)})" if pn else ""
            ns.append(f"      (node (ref {_q(ref)}) (pin {_q(pin)}){extra})")
        nets.append(f"    (net (code {_q(str(code))}) (name {_q(name)})\n" + "\n".join(ns) + ")")
    return ("(export (version \"E\")\n"
            f"  (design (source {_q(design.name + '.kicad_sch')}) (tool \"cvhack\"))\n"
            "  (components\n" + "\n".join(comps) + ")\n"
            "  (nets\n" + "\n".join(nets) + "))\n")




# ---------------------------------------------------------------------------
# SVG (fallback render when kicad-cli isn't installed; same layout as the .kicad_sch)
# ---------------------------------------------------------------------------

def _net_color(net: str) -> str:
    if net in ("GND", "VSS"):
        return "var(--sch-gnd, #6b7280)"
    h = int(uuid.uuid5(uuid.NAMESPACE_URL, net).hex[:4], 16) % 360
    return f"hsl({h} 65% 45%)"


_INWARD = {"L": (1, 0), "R": (-1, 0), "U": (0, 1), "D": (0, -1)}


def to_svg(design: Design, parts: dict[str, Part], lay: Layout | None = None,
           highlight: set[str] | None = None, highlight_nets: set[str] | None = None) -> str:
    """highlight: refs to outline in red (e.g. parts named in verifier errors)."""
    lay = lay or layout(design, parts)
    hi, hn = highlight or set(), highlight_nets or set()
    w, h = lay.width, lay.height
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(w)} {_f(h)}" '
         f'font-family="ui-monospace, Menlo, monospace" class="schematic">',
         '<style>.body{fill:var(--sch-body,#fffbe6);stroke:var(--sch-line,#7a1f1f);stroke-width:.254}'
         '.pin{stroke:var(--sch-line,#7a1f1f);stroke-width:.2}.t{fill:var(--sch-text,#1f2937)}'
         '.bad .body{stroke:#dc2626;stroke-width:.6;fill:var(--sch-bad,#fee2e2)}'
         '.title{fill:var(--sch-title,#374151);font-weight:700}</style>']
    for text, x, y in lay.titles:
        o.append(f'<text class="title" x="{_f(x)}" y="{_f(y)}" font-size="3">{escape(text.upper())}</text>')
    for pp in lay.parts:
        cls = "part bad" if pp.ref in hi else "part"
        o.append(f'<g class="{cls}" data-ref="{escape(pp.ref)}" data-mpn="{escape(pp.mpn)}">'
                 f'<title>{escape(pp.ref)} {escape(pp.mpn)}: {escape(pp.part.description)}</title>')
        # Body = extents minus pin length on each side that has pins.
        sides = {x.side for x in pp.pins if not x.hidden}
        bx0 = pp.x0 + (PIN_LEN if "L" in sides else 0)
        bx1 = pp.x1 - (PIN_LEN if "R" in sides else 0)
        by0 = pp.top + (PIN_LEN if "U" in sides else 0)
        by1 = pp.bottom - (PIN_LEN if "D" in sides else 0)
        if bx1 - bx0 < 1:
            bx0, bx1 = -1, 1
        if by1 - by0 < 1:
            by0, by1 = -1, 1
        o.append(f'<rect class="body" x="{_f(pp.x + bx0)}" y="{_f(pp.y + by0)}" '
                 f'width="{_f(bx1 - bx0)}" height="{_f(by1 - by0)}" rx=".4"/>')
        two_pin = len(pp.part.pins) <= 2
        for x in pp.pins:
            if x.hidden and not x.net:
                continue
            px, py = pp.pin_xy(x)
            ux, uy = _INWARD[x.side]
            ex, ey = px + ux * x.length, py + uy * x.length
            o.append(f'<line class="pin" x1="{_f(px)}" y1="{_f(py)}" x2="{_f(ex)}" y2="{_f(ey)}"/>')
            if not two_pin and x.side in "LR":
                anchor, nx = ("start", ex + 0.6) if x.side == "L" else ("end", ex - 0.6)
                o.append(f'<text class="t" x="{_f(nx)}" y="{_f(py + 0.45)}" font-size="1.27" '
                         f'text-anchor="{anchor}">{escape(x.pin.name)}</text>')
            if x.net:
                c = "#dc2626" if x.net in hn else _net_color(x.net)
                if x.side in "LR":
                    anchor, lx, ly = ("end", px - 0.6, py - 0.35) if x.side == "L" else ("start", px + 0.6, py - 0.35)
                else:
                    anchor, lx, ly = "middle", px, (py - 0.8 if x.side == "U" else py + 1.8)
                o.append(f'<circle cx="{_f(px)}" cy="{_f(py)}" r=".35" fill="{c}"/>'
                         f'<text x="{_f(lx)}" y="{_f(ly)}" font-size="1.27" fill="{c}" '
                         f'text-anchor="{anchor}" data-net="{escape(x.net)}">{escape(x.net)}</text>')
            elif x.nc or x.extra:
                o.append(f'<path d="M{_f(px - .6)} {_f(py - .6)}l1.2 1.2m0 -1.2l-1.2 1.2" stroke="#2563eb" stroke-width=".25"/>')
            else:
                o.append(f'<circle cx="{_f(px)}" cy="{_f(py)}" r=".6" fill="none" stroke="#dc2626" stroke-width=".25">'
                         f'<title>{escape(pp.ref)}.{escape(x.pin.num)} unconnected</title></circle>')
        tx, ty = (pp.x + pp.x1 + 1.5, pp.y) if pp.text_right else (pp.x, pp.y + pp.bottom + 2.2)
        anchor = "start" if pp.text_right else "middle"
        o.append(f'<text class="t" x="{_f(tx)}" y="{_f(ty)}" font-size="1.5" '
                 f'text-anchor="{anchor}" font-weight="700">{escape(pp.ref)}</text>'
                 f'<text class="t" x="{_f(tx)}" y="{_f(ty + 2)}" font-size="1.1" '
                 f'text-anchor="{anchor}" opacity=".7">{escape(pp.part.value or pp.mpn)}</text></g>')
    for fl in lay.flags:
        c = _net_color(fl.net)
        o.append(f'<path d="M{_f(fl.x)} {_f(fl.y)}v-1.27l-1 .63 1 .63 1-.63-1-.63" fill="none" stroke="{c}" stroke-width=".25"/>'
                 f'<text x="{_f(fl.x + .6)}" y="{_f(fl.y - .35)}" font-size="1.27" fill="{c}">{escape(fl.net)}</text>')
    o.append("</svg>")
    return "".join(o)
