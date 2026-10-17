"""Build and route a .kicad_pcb. Runs under KiCad's own Python (needs pcbnew).

    <kicad python> pcb_build.py build job.json out.kicad_pcb
    <kicad python> pcb_build.py route in.kicad_pcb out.kicad_pcb <java> <freerouting.jar> [passes]

Called by cvhack.tooling.pcb; keep this file Python 3.9 compatible and free of
cvhack imports, since it runs outside our virtualenv.

Job: {"name", "footprint_dir", "gnd": "GND", "power_nets": [..],
      "parts": [{"ref", "footprint", "value", "subsystem", "pads": {pad_number: net}}],
      "previous": {"width_mm", "height_mm", "placements": {ref: {"x", "y", "rot", "footprint"}}} | null}

build: parts that were on the previous board (same ref and footprint) keep their
position, so a design edit only moves what changed. New parts go to the nearest
free spot by their subsystem. Without a previous board, parts are shelf-packed
per subsystem and the clusters packed into a roughly square board. GND is poured
on both layers; net classes give power nets 0.4 mm tracks.

route: Specctra DSN -> Freerouting -> SES -> zones refilled.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import OrderedDict

import pcbnew

MM = pcbnew.FromMM
GAP = 2.0          # mm between footprints (routing channels)
GROUP_GAP = 3.0    # mm between subsystems
EDGE = 2.0         # mm from parts to board edge
MARGIN = EDGE + 3.0
GRID = 0.5         # mm, free-spot search grid


def bbox_mm(fp):
    b = fp.GetBoundingBox(False)   # without text
    return pcbnew.ToMM(b.GetX()), pcbnew.ToMM(b.GetY()), pcbnew.ToMM(b.GetWidth()), pcbnew.ToMM(b.GetHeight())


def center_mm(fp):
    x, y, w, h = bbox_mm(fp)
    return x + w / 2, y + h / 2


def move_center(fp, cx, cy):
    x, y = center_mm(fp)
    pos = fp.GetPosition()
    fp.SetPosition(pcbnew.VECTOR2I(pos.x + MM(cx - x), pos.y + MM(cy - y)))


def move_to(fp, x, y):
    """Move so the footprint's bounding box top-left lands at (x, y) mm."""
    bx, by, _, _ = bbox_mm(fp)
    pos = fp.GetPosition()
    fp.SetPosition(pcbnew.VECTOR2I(pos.x + MM(x - bx), pos.y + MM(y - by)))


def propagate_pad_nets(fp):
    """Pads that overlap a netted pad (USB-C's paired A9/B4 etc.) are the same conductor."""
    pads = list(fp.Pads())
    changed = True
    while changed:
        changed = False
        for a in pads:
            if a.GetNetCode() <= 0:
                continue
            for b in pads:
                shared = any(a.IsOnLayer(L) and b.IsOnLayer(L) for L in (pcbnew.F_Cu, pcbnew.B_Cu))
                if b is a or b.GetNetCode() > 0 or not shared:
                    continue
                if a.GetBoundingBox().Intersects(b.GetBoundingBox()):
                    b.SetNet(a.GetNet())
                    changed = True


def silk_text(board, text, x, y, size=1.0, layer=None, mirrored=False, left=True):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(text)
    t.SetLayer(layer if layer is not None else pcbnew.F_SilkS)
    t.SetMirrored(mirrored)
    t.SetTextSize(pcbnew.VECTOR2I(MM(size), MM(size)))
    t.SetTextThickness(MM(0.15 * size))
    if left:
        t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
    t.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
    board.Add(t)


def setup_rules(board, power_nets):
    ds = board.GetDesignSettings()
    ds.m_TrackMinWidth = MM(0.15)
    ds.m_HoleClearance = MM(0.15)     # library USB-C footprint sits 0.19 mm from its own NPTHs
    ds.m_MinClearance = MM(0.15)
    ns = ds.m_NetSettings
    dflt = ns.GetDefaultNetclass()
    dflt.SetTrackWidth(MM(0.2))
    dflt.SetClearance(MM(0.2))
    dflt.SetViaDiameter(MM(0.6))
    dflt.SetViaDrill(MM(0.3))
    pwr = pcbnew.NETCLASS("Power")
    pwr.SetTrackWidth(MM(0.4))
    pwr.SetClearance(MM(0.2))
    pwr.SetViaDiameter(MM(0.7))
    pwr.SetViaDrill(MM(0.35))
    ns.SetNetclass("Power", pwr)
    for n in power_nets:
        ns.SetNetclassPatternAssignment(n, "Power")
    ns.RecomputeEffectiveNetclasses()


def write_project(pcb_path, power_nets):
    """The .kicad_pro carries the design rules kicad-cli's DRC applies (the board file doesn't)."""
    pro = {
        "meta": {"filename": pcb_path.rsplit("/", 1)[-1].replace(".kicad_pcb", ".kicad_pro"), "version": 3},
        "board": {"design_settings": {"rules": {
            "min_clearance": 0.15, "min_hole_clearance": 0.15, "min_track_width": 0.15,
            "min_via_diameter": 0.45, "min_through_hole_diameter": 0.2, "min_copper_edge_clearance": 0.3}}},
        "net_settings": {
            "classes": [
                {"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3,
                 "priority": 2147483647},
                {"name": "Power", "clearance": 0.2, "track_width": 0.4, "via_diameter": 0.7, "via_drill": 0.35,
                 "priority": 0}],
            "netclass_patterns": [{"netclass": "Power", "pattern": n} for n in power_nets],
            "meta": {"version": 4}},
    }
    with open(pcb_path[: -len(".kicad_pcb")] + ".kicad_pro", "w") as f:
        json.dump(pro, f, indent=2)


def stitch_ground(board, spacing=6.0, margin=0.6):
    """Ground vias inside every top-layer GND fill island (and on a grid in big ones), wherever the
    bottom GND fill also covers the spot, so routing can't leave orphaned copper."""
    zones = {z.GetLayer(): z for z in board.Zones() if z.GetNetname() == "GND"}
    if pcbnew.F_Cu not in zones or pcbnew.B_Cu not in zones:
        return 0
    gnd = zones[pcbnew.F_Cu].GetNet()

    def shrunk(layer, by=margin):
        ps = pcbnew.SHAPE_POLY_SET(zones[layer].GetFilledPolysList(layer))
        ps.Deflate(MM(by), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MM(0.01))
        return ps
    top, bot = shrunk(pcbnew.F_Cu), shrunk(pcbnew.B_Cu)
    existing = [t.GetPosition() for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == "GND"]
    placed = list(existing)

    def ok(pt):
        if not (top.Contains(pt) and bot.Contains(pt)):
            return False
        return all((pt.x - q.x) ** 2 + (pt.y - q.y) ** 2 > MM(2.0) ** 2 for q in placed)

    candidates = []
    for i in range(top.OutlineCount()):
        ol = top.Outline(i)
        candidates += [ol.CPoint(j) for j in range(0, ol.PointCount(), max(1, ol.PointCount() // 4))]
    bb = board.GetBoardEdgesBoundingBox()
    step = MM(spacing)
    y = bb.GetY() + step // 2
    while y < bb.GetBottom():
        x = bb.GetX() + step // 2
        while x < bb.GetRight():
            candidates.append(pcbnew.VECTOR2I(x, y))
            x += step
        y += step
    def add(pt):
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pt)
        v.SetWidth(MM(0.6))
        v.SetDrill(MM(0.3))
        v.SetNet(gnd)
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        board.Add(v)
        placed.append(pt)

    for pt in candidates:
        pt = pcbnew.VECTOR2I(pt.x, pt.y)
        if ok(pt):
            add(pt)
    # Second pass for narrow islands: a via needs only 0.2 mm to other nets and the pour already
    # keeps 0.3 mm, so a 0.35 mm inset still clears. Any island that has no via yet gets one.
    full = pcbnew.SHAPE_POLY_SET(zones[pcbnew.F_Cu].GetFilledPolysList(pcbnew.F_Cu))
    top, bot = shrunk(pcbnew.F_Cu, 0.35), shrunk(pcbnew.B_Cu, 0.35)
    for i in range(full.OutlineCount()):
        island = pcbnew.SHAPE_POLY_SET()
        island.AddOutline(full.Outline(i))
        if any(island.Contains(q) for q in placed):
            continue
        # Fine 0.5 mm search inside this island for a spot both pours cover.
        bb = island.BBox()
        step = MM(0.5)
        hit = None
        yy = bb.GetY()
        while hit is None and yy <= bb.GetBottom():
            xx = bb.GetX()
            while xx <= bb.GetRight():
                pt = pcbnew.VECTOR2I(xx, yy)
                if island.Contains(pt) and top.Contains(pt) and bot.Contains(pt):
                    hit = pt
                    break
                xx += step
            yy += step
        if hit is not None:
            add(hit)
    return len(placed) - len(existing)


def via_in_pad_orphans(board):
    """GND islands too small for a stitching via (a pad boxed in by traces) get a small via in
    the pad itself, if the bottom GND fill is under it."""
    zones = {z.GetLayer(): z for z in board.Zones() if z.GetNetname() == "GND"}
    if pcbnew.F_Cu not in zones or pcbnew.B_Cu not in zones:
        return 0
    gnd = zones[pcbnew.F_Cu].GetNet()
    vias = [t.GetPosition() for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == "GND"]
    top = zones[pcbnew.F_Cu].GetFilledPolysList(pcbnew.F_Cu)
    bot = pcbnew.SHAPE_POLY_SET(zones[pcbnew.B_Cu].GetFilledPolysList(pcbnew.B_Cu))
    bot.Deflate(MM(0.25), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, MM(0.01))
    pads = [p for p in board.GetPads() if p.GetNetname() == "GND" and p.IsOnLayer(pcbnew.F_Cu)
            and p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
    n = 0
    for i in range(top.OutlineCount()):
        isl = pcbnew.SHAPE_POLY_SET()
        isl.AddOutline(top.Outline(i))
        if any(isl.Contains(v) for v in vias):
            continue
        for pad in pads:
            c = pad.GetPosition()
            size = pad.GetSize(pcbnew.F_Cu)
            if isl.Contains(c) and bot.Contains(c) and min(size.x, size.y) >= MM(0.45):
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(c)
                v.SetWidth(MM(0.45))
                v.SetDrill(MM(0.2))
                v.SetNet(gnd)
                v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
                board.Add(v)
                vias.append(c)
                n += 1
                break
    return n


def overlaps(r, rects):
    x, y, w, h = r
    for (ax, ay, aw, ah) in rects:
        if x < ax + aw + GAP and ax < x + w + GAP and y < ay + ah + GAP and ay < y + h + GAP:
            return True
    return False


def find_spot(w, h, near, rects, bw, bh):
    """Nearest free top-left position for a w x h box to `near` (centre), inside the board."""
    best = None
    nx, ny = near
    steps_x, steps_y = int((bw - 2 * MARGIN) / GRID) + 1, int((bh - 2 * MARGIN) / GRID) + 1
    for i in range(steps_x):
        x = MARGIN + i * GRID
        if x + w > bw - MARGIN:
            break
        for j in range(steps_y):
            y = MARGIN + j * GRID
            if y + h > bh - MARGIN:
                break
            d = (x + w / 2 - nx) ** 2 + (y + h / 2 - ny) ** 2
            if best is not None and d >= best[0]:
                continue
            if not overlaps((x, y, w, h), rects):
                best = (d, x, y)
    return None if best is None else (best[1], best[2])


def pack_fresh(board, groups):
    """No previous board: clusters per subsystem, clusters packed roughly square."""
    clusters = []
    for sub, items in groups.items():
        items.sort(key=lambda f: -(bbox_mm(f)[2] * bbox_mm(f)[3]))
        area = sum((bbox_mm(f)[2] + GAP) * (bbox_mm(f)[3] + GAP) for f in items)
        width = max(max(bbox_mm(f)[2] for f in items), (area * 1.2) ** 0.5)
        cx, cy, row_h, used_w, rel = 0.0, 0.0, 0.0, 0.0, []
        for fp in items:
            _, _, w, h = bbox_mm(fp)
            if cx > 0 and cx + w > width:
                cx, cy, row_h = 0.0, cy + row_h + GAP, 0.0
            rel.append((fp, cx, cy))
            used_w = max(used_w, cx + w)
            cx += w + GAP
            row_h = max(row_h, h)
        clusters.append((sub, rel, used_w, cy + row_h + 2.5))   # 2.5 mm for the silkscreen title
    total = sum((cw + GROUP_GAP) * (ch + GROUP_GAP) for _, _, cw, ch in clusters)
    board_w = max(max(cw for _, _, cw, _ in clusters), total ** 0.5 * 1.15)
    x = y = row_h = 0.0
    w_used = 0.0
    titles = []
    for sub, rel, cw, ch in clusters:
        if x > 0 and x + cw > board_w:
            x, y, row_h = 0.0, y + row_h + GROUP_GAP, 0.0
        ox, oy = MARGIN + x, MARGIN + y
        titles.append((sub, ox, oy + 0.6))
        for fp, rx, ry in rel:
            move_to(fp, ox + rx, oy + 2.5 + ry)
        x += cw + GROUP_GAP
        w_used = max(w_used, x - GROUP_GAP)
        row_h = max(row_h, ch)
    return w_used + 2 * MARGIN, y + row_h + 2 * MARGIN, titles


def place_incremental(fps, subs, prev, groups):
    """Keep previous positions; drop new parts into the nearest free spot by their subsystem."""
    bw, bh = prev["width_mm"], prev["height_mm"]
    kept, new = [], []
    for ref, fp in fps.items():
        p = prev["placements"].get(ref)
        if p and p.get("footprint") == fp.GetFPIDAsString():
            fp.SetOrientationDegrees(p.get("rot", 0))
            move_center(fp, p["x"], p["y"])
            kept.append(ref)
        else:
            new.append(ref)
    rects = [bbox_mm(fps[r]) for r in kept]
    for ref in sorted(new, key=lambda r: -(bbox_mm(fps[r])[2] * bbox_mm(fps[r])[3])):
        fp = fps[ref]
        _, _, w, h = bbox_mm(fp)
        mates = [center_mm(fps[r]) for r in kept if subs[r] == subs[ref]]
        old = prev["placements"].get(ref)    # same ref, new footprint: start where it was
        if old:
            near = (old["x"], old["y"])
        elif mates:
            near = (sum(m[0] for m in mates) / len(mates), sum(m[1] for m in mates) / len(mates))
        else:
            near = (bw / 2, bh / 2)
        spot = find_spot(w, h, near, rects, bw, bh)
        while spot is None:            # board full: grow the shorter side, so it stays roughly square
            if bw <= bh:
                bw += max(w + GAP, 5.0)
            else:
                bh += max(h + GAP, 5.0)
            spot = find_spot(w, h, near, rects, bw, bh)
        move_to(fp, spot[0], spot[1])
        rects.append(bbox_mm(fp))
        kept.append(ref)
    titles = []
    for sub, items in groups.items():
        xs = [bbox_mm(f) for f in items]
        titles.append((sub, min(x for x, _, _, _ in xs), max(min(y for _, y, _, _ in xs) - 1.6, 1.0)))
    return bw, bh, titles, new


def _outline_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("outline", os.path.join(os.path.dirname(os.path.abspath(__file__)), "outline.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def add_outline_holes_title(board, job, w, h):
    o = job.get("outline")
    if o and o.get("kind", "rect") != "rect":
        pts = _outline_module().points(o, w, h)  # custom shape: closed polygon on Edge.Cuts enclosing the parts
        for i, (x0, y0) in enumerate(pts):
            x1, y1 = pts[(i + 1) % len(pts)]
            seg = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
            seg.SetStart(pcbnew.VECTOR2I(MM(x0), MM(y0)))
            seg.SetEnd(pcbnew.VECTOR2I(MM(x1), MM(y1)))
            seg.SetLayer(pcbnew.Edge_Cuts)
            seg.SetWidth(MM(0.1))
            board.Add(seg)
    else:
        rect = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECT)
        rect.SetStart(pcbnew.VECTOR2I(0, 0))
        rect.SetEnd(pcbnew.VECTOR2I(MM(w), MM(h)))
        rect.SetLayer(pcbnew.Edge_Cuts)
        rect.SetWidth(MM(0.1))
        board.Add(rect)
    hole = pcbnew.FootprintLoad(job["footprint_dir"] + "/MountingHole.pretty", "MountingHole_2.2mm_M2")
    if hole is not None:
        for i, (hx, hy) in enumerate(((2.5, 2.5), (w - 2.5, 2.5), (2.5, h - 2.5), (w - 2.5, h - 2.5))):
            mh = pcbnew.FOOTPRINT(hole)
            mh.SetReference("H%d" % (i + 1))
            mh.Reference().SetVisible(False)
            mh.SetPosition(pcbnew.VECTOR2I(MM(hx), MM(hy)))
            board.Add(mh)
    silk_text(board, job["name"], w / 2, h / 2, size=1.5, layer=pcbnew.B_SilkS, mirrored=True, left=False)


def add_gnd_pours(board, gnd, w, h):
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
        z = pcbnew.ZONE(board)
        z.SetLayer(layer)
        z.SetNet(gnd)
        z.SetLocalClearance(MM(0.3))
        z.SetMinThickness(MM(0.2))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
        ol = z.Outline()
        ol.NewOutline()
        for px, py in ((0.3, 0.3), (w - 0.3, 0.3), (w - 0.3, h - 0.3), (0.3, h - 0.3)):
            ol.Append(MM(px), MM(py))
        board.Add(z)


def build(job_path, out_path):
    job = json.load(open(job_path))
    board = pcbnew.BOARD()
    setup_rules(board, job.get("power_nets", []))
    nets = OrderedDict()
    for p in job["parts"]:
        for net in p["pads"].values():
            if net and net not in nets:
                ni = pcbnew.NETINFO_ITEM(board, net)
                board.Add(ni)
                nets[net] = ni

    groups, fps, subs = OrderedDict(), OrderedDict(), {}
    for p in job["parts"]:
        lib, _, name = p["footprint"].partition(":")
        fp = pcbnew.FootprintLoad(job["footprint_dir"] + "/" + lib + ".pretty", name)
        if fp is None:
            print("missing footprint", p["footprint"], file=sys.stderr)
            continue
        fp.SetReference(p["ref"])
        fp.SetValue(p["value"])
        for pad in fp.Pads():
            net = p["pads"].get(pad.GetNumber())
            if net:
                pad.SetNet(nets[net])
        propagate_pad_nets(fp)
        board.Add(fp)
        fps[p["ref"]] = fp
        subs[p["ref"]] = p["subsystem"]
        groups.setdefault(p["subsystem"], []).append(fp)

    prev = job.get("previous")
    if prev and prev.get("placements"):
        w, h, titles, new = place_incremental(fps, subs, prev, groups)
    else:
        w, h, titles = pack_fresh(board, groups)
        new = list(fps)
    drawn = []
    for sub, x, y in titles:   # skip a subsystem title that would sit on top of another
        w_txt = len(sub) * 1.0
        if any(abs(y - py) < 1.6 and x < px + pw and px < x + w_txt for px, py, pw in drawn):
            continue
        silk_text(board, sub.upper(), x, y)
        drawn.append((x, y, w_txt))
    add_outline_holes_title(board, job, w, h)
    gnd = nets.get(job.get("gnd", "GND"))
    if gnd is not None:
        add_gnd_pours(board, gnd, w, h)
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(out_path)
    write_project(out_path, job.get("power_nets", []))

    placements = {}
    for ref, fp in fps.items():
        _, _, bw, bh = bbox_mm(fp)
        cx, cy = center_mm(fp)
        placements[ref] = {"x": round(cx, 3), "y": round(cy, 3), "w": round(bw, 3), "h": round(bh, 3),
                           "rot": fp.GetOrientationDegrees(), "footprint": fp.GetFPIDAsString()}
    print(json.dumps({"width_mm": round(w, 2), "height_mm": round(h, 2), "footprints": len(fps),
                      "thickness_mm": pcbnew.ToMM(board.GetDesignSettings().GetBoardThickness()),
                      "placed_new": new, "placements": placements}))


def route(in_path, out_path, java, jar, passes="30"):
    board = pcbnew.LoadBoard(in_path)
    dsn, ses = out_path + ".dsn", out_path + ".ses"
    if not pcbnew.ExportSpecctraDSN(board, dsn):
        raise SystemExit("DSN export failed")
    proc = subprocess.run([java, "-Djava.awt.headless=true", "-jar", jar, "-de", dsn, "-do", ses,
                           "-mp", str(passes), "--gui.enabled=false"], capture_output=True, text=True, timeout=600)
    unrouted = []
    for line in proc.stdout.splitlines() + proc.stderr.splitlines():
        s = line.strip()
        if s.startswith("- ") and "->" in s:
            unrouted.append(s[2:])
    if not pcbnew.ImportSpecctraSES(board, ses):
        raise SystemExit("SES import failed: " + proc.stdout[-500:])
    # Fill without island removal first: the bottom pour has no pads, so with removal on it would
    # vanish before the stitching vias that connect it exist.
    for z in board.Zones():
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_NEVER)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    stitched = stitch_ground(board)
    for z in board.Zones():
        z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    stitched += stitch_ground(board)          # again, now that the pours have settled
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    stitched += via_in_pad_orphans(board)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.Save(out_path)
    src_pro = in_path[: -len(".kicad_pcb")] + ".kicad_pro"
    try:
        shutil.copyfile(src_pro, out_path[: -len(".kicad_pcb")] + ".kicad_pro")
    except OSError:
        pass
    tracks = list(board.GetTracks())
    vias = sum(1 for t in tracks if t.GetClass() == "PCB_VIA")
    length = sum(pcbnew.ToMM(t.GetLength()) for t in tracks if t.GetClass() == "PCB_TRACK")
    print(json.dumps({"tracks": len(tracks) - vias, "vias": vias, "stitching_vias": stitched,
                      "track_length_mm": round(length, 1), "unrouted": unrouted}))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build":
        build(sys.argv[2], sys.argv[3])
    elif cmd == "route":
        route(*sys.argv[2:])
    else:
        raise SystemExit("usage: pcb_build.py build|route ...")
