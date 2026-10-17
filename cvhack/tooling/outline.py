from __future__ import annotations

"""Board outline polygons (pure Python, no KiCad imports: pcb_build loads this file by path).

points(spec, w, h) returns the outline in mm around the placement rectangle (0,0)-(w,h). Every shape ENCLOSES that
rectangle, so no placed part is cut off; the board simply gets bigger for round shapes (reported by bbox())."""
import math

KINDS = ("rect", "rounded_rect", "circle", "ellipse", "capsule", "octagon", "hexagon")


def _ellipse(cx, cy, a, b, n=72, rot=0.0):
    return [(cx + a * math.cos(rot + 2 * math.pi * i / n), cy + b * math.sin(rot + 2 * math.pi * i / n)) for i in range(n)]


def points(spec: dict | None, w: float, h: float, m: float = 1.0) -> list[tuple[float, float]]:
    kind = (spec or {}).get("kind", "rect")
    if kind == "rect" or kind not in KINDS:
        return [(0, 0), (w, 0), (w, h), (0, h)]
    cx, cy = w / 2, h / 2
    if kind == "rounded_rect":
        r = max(0.5, min(float((spec or {}).get("corner_radius_mm") or 3), min(w, h) / 2))
        e = 0.3 * r                                   # grow so the parts' corners land on the arc, not outside it
        x0, y0, x1, y1 = -e, -e, w + e, h + e
        out = []
        for (px, py, a0) in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
            for k in range(9):
                t = math.radians(a0 + 90 * k / 8)
                out.append((px + r * math.cos(t), py + r * math.sin(t)))
        return out
    if kind == "circle":
        rad = math.hypot(w, h) / 2 + m
        return _ellipse(cx, cy, rad, rad)
    if kind == "ellipse":
        return _ellipse(cx, cy, w / math.sqrt(2) + m, h / math.sqrt(2) + m)
    if kind == "capsule":  # stadium along the LONGER side, end arcs just enclosing the rectangle's corners
        L, S = (w, h) if w >= h else (h, w)        # long and short sides of the placement
        R = S / 2 * 1.25 + m / 2                   # cap radius: corners sit on the arc, so nothing is cut off
        c = max(math.sqrt(max(R * R - (S / 2) ** 2, 0.0)) - 0.3, 0.0)  # inward: corners end up just inside the arc
        out = [(c, S / 2 - R), (L - c, S / 2 - R)]
        out += [(L - c + R * math.sin(math.radians(a)), S / 2 - R * math.cos(math.radians(a))) for a in range(5, 180, 10)]
        out += [(L - c, S / 2 + R), (c, S / 2 + R)]
        out += [(c - R * math.sin(math.radians(a)), S / 2 + R * math.cos(math.radians(a))) for a in range(5, 180, 10)]
        return out if w >= h else [(y, x) for x, y in out]
    n = 8 if kind == "octagon" else 6
    a, b = w / math.sqrt(2) + m, h / math.sqrt(2) + m
    k = 1 / math.cos(math.pi / n)
    return _ellipse(cx, cy, a * k, b * k, n=n, rot=math.pi / n)


def bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"x0": min(xs), "y0": min(ys), "width_mm": round(max(xs) - min(xs), 2), "height_mm": round(max(ys) - min(ys), 2)}
