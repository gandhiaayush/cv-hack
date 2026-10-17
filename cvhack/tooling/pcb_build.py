"""Build a .kicad_pcb from a job file. Runs under KiCad's own Python (needs pcbnew).

    <kicad python> pcb_build.py job.json out.kicad_pcb

Called by cvhack.tooling.pcb.build_board(); keep this file Python 3.9 compatible
and free of cvhack imports, since it runs outside our virtualenv.

Job: {"name", "footprint_dir", "gnd": "GND", "parts": [{"ref", "footprint", "value",
      "subsystem", "pads": {pad_number: net}}]}

Placement is a simple shelf pack per subsystem, subsystems side by side, with
the USB/battery connectors on the board edge. Ground is poured on both copper
layers; other nets are left unrouted (no autorouter here).
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict

import pcbnew

MM = pcbnew.FromMM
GAP = 1.2          # mm between footprints
GROUP_GAP = 3.0    # mm between subsystems
EDGE = 2.0         # mm from parts to board edge


def bbox_mm(fp):
    b = fp.GetBoundingBox(False)   # without text
    return pcbnew.ToMM(b.GetX()), pcbnew.ToMM(b.GetY()), pcbnew.ToMM(b.GetWidth()), pcbnew.ToMM(b.GetHeight())


def move_to(fp, x, y):
    """Move so the footprint's bounding box top-left lands at (x, y) mm."""
    bx, by, _, _ = bbox_mm(fp)
    pos = fp.GetPosition()
    fp.SetPosition(pcbnew.VECTOR2I(pos.x + MM(x - bx), pos.y + MM(y - by)))


def main(job_path, out_path):
    job = json.load(open(job_path))
    board = pcbnew.BOARD()
    nets = OrderedDict()
    for p in job["parts"]:
        for net in p["pads"].values():
            if net and net not in nets:
                ni = pcbnew.NETINFO_ITEM(board, net)
                board.Add(ni)
                nets[net] = ni

    groups = OrderedDict()
    fps = {}
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
        board.Add(fp)
        fps[p["ref"]] = fp
        groups.setdefault(p["subsystem"], []).append(fp)

    # 1) Shelf-pack each subsystem into a cluster, biggest parts first (positions relative to 0,0).
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
    # 2) Shelf-pack the clusters into a roughly square board.
    total = sum((cw + GROUP_GAP) * (ch + GROUP_GAP) for _, _, cw, ch in clusters)
    board_w = max(max(cw for _, _, cw, _ in clusters), total ** 0.5 * 1.15)
    x = y = row_h = 0.0
    w_used = 0.0
    for sub, rel, cw, ch in clusters:
        if x > 0 and x + cw > board_w:
            x, y, row_h = 0.0, y + row_h + GROUP_GAP, 0.0
        ox, oy = EDGE + 3.0 + x, EDGE + 3.0 + y
        txt = pcbnew.PCB_TEXT(board)
        txt.SetText(sub.upper())
        txt.SetLayer(pcbnew.F_SilkS)
        txt.SetTextSize(pcbnew.VECTOR2I(MM(1.0), MM(1.0)))
        txt.SetTextThickness(MM(0.15))
        txt.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
        txt.SetPosition(pcbnew.VECTOR2I(MM(ox), MM(oy + 0.6)))
        board.Add(txt)
        for fp, rx, ry in rel:
            move_to(fp, ox + rx, oy + 2.5 + ry)
        x += cw + GROUP_GAP
        w_used = max(w_used, x - GROUP_GAP)
        row_h = max(row_h, ch)
    w = w_used + 2 * (EDGE + 3.0)
    h = y + row_h + 2 * (EDGE + 3.0)

    # Mounting holes in the corners (the 3 mm margin above leaves room for them).
    hole = pcbnew.FootprintLoad(job["footprint_dir"] + "/MountingHole.pretty", "MountingHole_2.2mm_M2")
    if hole is not None:
        for i, (hx, hy) in enumerate(((2.5, 2.5), (w - 2.5, 2.5), (2.5, h - 2.5), (w - 2.5, h - 2.5))):
            mh = pcbnew.FOOTPRINT(hole)
            mh.SetReference(f"H{i + 1}")
            mh.Reference().SetVisible(False)
            mh.SetPosition(pcbnew.VECTOR2I(MM(hx), MM(hy)))
            board.Add(mh)

    rect = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECT)
    rect.SetStart(pcbnew.VECTOR2I(0, 0))
    rect.SetEnd(pcbnew.VECTOR2I(MM(w), MM(h)))
    rect.SetLayer(pcbnew.Edge_Cuts)
    rect.SetWidth(MM(0.1))
    board.Add(rect)

    title = pcbnew.PCB_TEXT(board)
    title.SetText(job["name"])
    title.SetLayer(pcbnew.B_SilkS)
    title.SetMirrored(True)
    title.SetTextSize(pcbnew.VECTOR2I(MM(1.5), MM(1.5)))
    title.SetTextThickness(MM(0.2))
    title.SetPosition(pcbnew.VECTOR2I(MM(w / 2), MM(h / 2)))
    board.Add(title)

    gnd = nets.get(job.get("gnd", "GND"))
    if gnd is not None:
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            z = pcbnew.ZONE(board)
            z.SetLayer(layer)
            z.SetNet(gnd)
            z.SetLocalClearance(MM(0.3))
            z.SetMinThickness(MM(0.2))
            z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
            ol = z.Outline()
            ol.NewOutline()
            for px, py in ((0.3, 0.3), (w - 0.3, 0.3), (w - 0.3, h - 0.3), (0.3, h - 0.3)):
                ol.Append(MM(px), MM(py))
            board.Add(z)
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    board.Save(out_path)
    placements = {}
    for ref, fp in fps.items():
        bx, by, bw, bh = bbox_mm(fp)
        placements[ref] = {"x": round(bx + bw / 2, 3), "y": round(by + bh / 2, 3), "w": round(bw, 3), "h": round(bh, 3),
                           "footprint": fp.GetFPIDAsString()}
    print(json.dumps({"width_mm": round(w, 2), "height_mm": round(h, 2), "footprints": len(fps),
                      "thickness_mm": pcbnew.ToMM(board.GetDesignSettings().GetBoardThickness()),
                      "placements": placements}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
