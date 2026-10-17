"""Design -> KiCad schematic, KiCad netlist and SVG, from one shared layout.

The .kicad_sch is self-contained: every symbol is generated from Part.pins
and embedded in lib_symbols, so it opens and passes through kicad-cli with no
KiCad libraries configured. Pins connect through net labels at their ends, so
the picture is "boxes with named pins", which is what a netlist-first agent
design really is. Blocks are laid out as columns, one per subsystem.

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

G = 2.54                     # pin pitch and placement grid (mm)
PIN_LEN = 2.54
CHAR_W = 1.0                 # approx. width of one 1.27 mm label character
LEFT_TYPES = {"input", "power_in"}
RIGHT_TYPES = {"output", "power_out", "open_collector", "tri_state", "bidirectional"}
MARGIN = 20.32
PAPERS = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]


def _snap(v: float, g: float = G) -> float:
    return round(round(v / g) * g, 4)


def _up(v: float, g: float = G) -> float:
    return round(math.ceil(v / g - 1e-9) * g, 4)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

@dataclass
class PinPlace:
    pin: Pin
    side: str                 # "L" | "R"
    dx: float                 # endpoint offset from symbol origin, schematic coords (y down)
    dy: float
    net: str | None = None
    nc: bool = False


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

    def pin_xy(self, pp: PinPlace) -> tuple[float, float]:
        return round(self.x + pp.dx, 4), round(self.y + pp.dy, 4)


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


def layout(design: Design, parts: dict[str, Part]) -> Layout:
    nets_of: dict[tuple[str, str], str] = {}
    for name, nodes in design.merged_nets().items():
        for n in nodes:
            nets_of.setdefault((n.ref, n.pin), name)
    ncs = {(n.ref, n.pin) for n in design.no_connects}

    placed: list[PartPlace] = []
    titles: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    x_cursor = MARGIN
    max_bottom = MARGIN
    for block in design.blocks:
        insts = [i for i in block.parts if i.ref not in seen and i.mpn in parts]
        if not insts:
            continue
        col: list[tuple] = []
        lab_l = lab_r = 0.0
        body_w = 0.0
        for inst in insts:
            seen.add(inst.ref)
            p = parts[inst.mpn]
            left, right, half_w, top, bottom = _body(p)
            body_w = max(body_w, half_w)

            def labw(ps: list[Pin]) -> float:
                return max([len(nets_of.get((inst.ref, x.num), "")) for x in ps] or [0]) * CHAR_W + 3.0
            lab_l, lab_r = max(lab_l, labw(left)), max(lab_r, labw(right))
            col.append((inst, p, left, right, half_w, top, bottom))

        cx = _up(x_cursor + lab_l + PIN_LEN + body_w)
        titles.append((block.subsystem, cx - body_w - PIN_LEN - lab_l, MARGIN))
        y_cursor = MARGIN + 7.62
        for inst, p, left, right, half_w, top, bottom in col:
            cy = _up(y_cursor - top + 2.54)
            pp = PartPlace(ref=inst.ref, mpn=inst.mpn, part=p, subsystem=block.subsystem,
                           x=cx, y=cy, half_w=half_w, top=top, bottom=bottom)
            for side, plist in (("L", left), ("R", right)):
                top_pin = (max(len(left), len(right), 1) - 1) // 2 * G
                for k, pin in enumerate(plist):
                    dx = -(half_w + PIN_LEN) if side == "L" else half_w + PIN_LEN
                    dy = round(-top_pin + k * G, 4)
                    key = (inst.ref, pin.num)
                    pp.pins.append(PinPlace(pin=pin, side=side, dx=dx, dy=dy, net=nets_of.get(key),
                                            nc=(key in ncs or pin.type == "no_connect") and key not in nets_of))
            placed.append(pp)
            y_cursor = cy + bottom + 5.08 + 2.54   # room for ref/value text below
        max_bottom = max(max_bottom, y_cursor)
        x_cursor = cx + body_w + PIN_LEN + lab_r + 10.16

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


def _label(net: str, x: float, y: float, side: str) -> str:
    ang, just = (180, "right bottom") if side == "L" else (0, "left bottom")
    return (f"  (label {_q(net)} (at {_f(x)} {_f(y)} {ang}) (fields_autoplaced yes)\n"
            f"    (effects (font (size 1.27 1.27)) (justify {just})) (uuid {_q(_uid('label', net, _f(x), _f(y)))}))")


def to_kicad_sch(design: Design, parts: dict[str, Part], lay: Layout | None = None) -> str:
    lay = lay or layout(design, parts)
    root = _uid("sheet", design.name)
    project = design.name
    out = ["(kicad_sch (version 20231120) (generator \"cvhack\") (generator_version \"8.0\")",
           f"  (uuid {_q(root)})"]
    pname, pw, ph = lay.paper
    out.append(f"  (paper \"User\" {_f(pw)} {_f(ph)})" if pname == "User" else f"  (paper {_q(pname)})")
    out.append(f"  (title_block (title {_q(design.name)}) (company \"cvhack\") (comment 1 \"Generated by agents; ERC-gated\"))")

    libs: dict[str, str] = {}
    for pp in lay.parts:
        name = _sym_name(pp.mpn)
        if name not in libs:
            libs[name] = _lib_symbol(name, pp.part, re.sub(r"\d+$", "", pp.ref) or "U")
    out.append("  (lib_symbols")
    out.extend(libs.values())
    if lay.flags:
        out.append(_FLAG_SYM)
    out.append("  )")

    for text, x, y in lay.titles:
        out.append(f"  (text {_q(text.upper())} (exclude_from_sim no) (at {_f(x)} {_f(y)} 0)\n"
                   f"    (effects (font (size 2.54 2.54) (bold yes)) (justify left bottom)) (uuid {_q(_uid('title', text))}))")

    for pp in lay.parts:
        name = _sym_name(pp.mpn)
        pin_uuids = "\n".join(f"    (pin {_q(x.pin.num)} (uuid {_q(_uid('pin', pp.ref, x.pin.num))}))" for x in pp.pins)
        out.append(
            f"  (symbol (lib_id {_q('cvhack:' + name)}) (at {_f(pp.x)} {_f(pp.y)} 0) (unit 1)\n"
            f"    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (fields_autoplaced yes)\n"
            f"    (uuid {_q(_uid('sym', pp.ref))})\n"
            f"    (property \"Reference\" {_q(pp.ref)} (at {_f(pp.x)} {_f(pp.y + pp.bottom + 2.54)} 0) {FONT})\n"
            f"    (property \"Value\" {_q(pp.part.value or pp.mpn)} (at {_f(pp.x)} {_f(pp.y + pp.bottom + 5.08)} 0) {FONT})\n"
            f"    (property \"Footprint\" {_q(pp.part.footprint)} (at {_f(pp.x)} {_f(pp.y)} 0) {FONT_HIDE})\n"
            f"    (property \"Datasheet\" \"\" (at {_f(pp.x)} {_f(pp.y)} 0) {FONT_HIDE})\n"
            f"    (property \"MPN\" {_q(pp.mpn)} (at {_f(pp.x)} {_f(pp.y)} 0) {FONT_HIDE})\n"
            f"    (property \"Subsystem\" {_q(pp.subsystem)} (at {_f(pp.x)} {_f(pp.y)} 0) {FONT_HIDE})\n"
            f"{pin_uuids}\n"
            f"    (instances (project {_q(project)} (path {_q('/' + root)} (reference {_q(pp.ref)}) (unit 1)))))")
        for x in pp.pins:
            px, py = pp.pin_xy(x)
            if x.net:
                out.append(_label(x.net, px, py, x.side))
            elif x.nc:
                out.append(f"  (no_connect (at {_f(px)} {_f(py)}) (uuid {_q(_uid('nc', pp.ref, x.pin.num))}))")

    for fl in lay.flags:
        # Flag pin points down at (0,0); the label sits on the same point.
        out.append(
            f"  (symbol (lib_id \"cvhack:PWR_FLAG\") (at {_f(fl.x)} {_f(fl.y)} 0) (unit 1)\n"
            f"    (exclude_from_sim no) (in_bom no) (on_board no) (dnp no)\n"
            f"    (uuid {_q(_uid('flag', fl.net))})\n"
            f"    (property \"Reference\" {_q(fl.ref)} (at {_f(fl.x)} {_f(fl.y - 1.905)} 0) {FONT_HIDE})\n"
            f"    (property \"Value\" \"PWR_FLAG\" (at {_f(fl.x)} {_f(fl.y - 3.81)} 0) {FONT})\n"
            f"    (property \"Footprint\" \"\" (at {_f(fl.x)} {_f(fl.y)} 0) {FONT_HIDE})\n"
            f"    (property \"Datasheet\" \"\" (at {_f(fl.x)} {_f(fl.y)} 0) {FONT_HIDE})\n"
            f"    (pin \"1\" (uuid {_q(_uid('flagpin', fl.net))}))\n"
            f"    (instances (project {_q(project)} (path {_q('/' + root)} (reference {_q(fl.ref)}) (unit 1)))))")
        out.append(_label(fl.net, fl.x, fl.y, "R"))

    out.append("  (sheet_instances (path \"/\" (page \"1\")))")
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
# SVG (dashboard render; same layout as the .kicad_sch)
# ---------------------------------------------------------------------------

def _net_color(net: str) -> str:
    if net in ("GND", "VSS"):
        return "var(--sch-gnd, #6b7280)"
    h = int(uuid.uuid5(uuid.NAMESPACE_URL, net).hex[:4], 16) % 360
    return f"hsl({h} 65% 45%)"


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
        o.append(f'<rect class="body" x="{_f(pp.x - pp.half_w)}" y="{_f(pp.y + pp.top)}" '
                 f'width="{_f(2 * pp.half_w)}" height="{_f(pp.bottom - pp.top)}" rx=".4"/>')
        two_pin = len(pp.part.pins) <= 2
        for x in pp.pins:
            px, py = pp.pin_xy(x)
            bx = px + (PIN_LEN if x.side == "L" else -PIN_LEN)
            o.append(f'<line class="pin" x1="{_f(px)}" y1="{_f(py)}" x2="{_f(bx)}" y2="{_f(py)}"/>')
            if not two_pin:
                anchor, nx = ("start", bx + 0.6) if x.side == "L" else ("end", bx - 0.6)
                o.append(f'<text class="t" x="{_f(nx)}" y="{_f(py + 0.45)}" font-size="1.27" '
                         f'text-anchor="{anchor}">{escape(x.pin.name)}</text>')
            if x.net:
                c = "#dc2626" if x.net in hn else _net_color(x.net)
                anchor, lx = ("end", px - 0.6) if x.side == "L" else ("start", px + 0.6)
                o.append(f'<circle cx="{_f(px)}" cy="{_f(py)}" r=".35" fill="{c}"/>'
                         f'<text x="{_f(lx)}" y="{_f(py - 0.35)}" font-size="1.27" fill="{c}" '
                         f'text-anchor="{anchor}" data-net="{escape(x.net)}">{escape(x.net)}</text>')
            elif x.nc:
                o.append(f'<path d="M{_f(px - .6)} {_f(py - .6)}l1.2 1.2m0 -1.2l-1.2 1.2" stroke="#2563eb" stroke-width=".25"/>')
            else:
                o.append(f'<circle cx="{_f(px)}" cy="{_f(py)}" r=".6" fill="none" stroke="#dc2626" stroke-width=".25">'
                         f'<title>{escape(pp.ref)}.{escape(x.pin.num)} unconnected</title></circle>')
        o.append(f'<text class="t" x="{_f(pp.x)}" y="{_f(pp.y + pp.bottom + 2.2)}" font-size="1.5" '
                 f'text-anchor="middle" font-weight="700">{escape(pp.ref)}</text>'
                 f'<text class="t" x="{_f(pp.x)}" y="{_f(pp.y + pp.bottom + 4.2)}" font-size="1.1" '
                 f'text-anchor="middle" opacity=".7">{escape(pp.part.value or pp.mpn)}</text></g>')
    for fl in lay.flags:
        c = _net_color(fl.net)
        o.append(f'<path d="M{_f(fl.x)} {_f(fl.y)}v-1.27l-1 .63 1 .63 1-.63-1-.63" fill="none" stroke="{c}" stroke-width=".25"/>'
                 f'<text x="{_f(fl.x + .6)}" y="{_f(fl.y - .35)}" font-size="1.27" fill="{c}">{escape(fl.net)}</text>')
    o.append("</svg>")
    return "".join(o)
