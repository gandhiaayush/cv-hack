"""Board-shape agent: turns "round 35 mm", "capsule wristband pod", "rounded corners 4 mm" into a validated outline.

The model only reads the request; code validates the kind and numbers, and after the board is built reports whether
the requested size actually fits the parts (it never shrinks the board below what the parts need)."""
import json
import os
import re

from cvhack.tooling import outline

from .llm import chat

SYSTEM = """You are the board-shape agent for a PCB design tool. Read the request and return JSON only:
{"kind": "rect|rounded_rect|circle|ellipse|capsule|octagon|hexagon",
 "corner_radius_mm": <number or null, only for rounded_rect>,
 "max_width_mm": <number or null>, "max_height_mm": <number or null>,
 "note": "<one short sentence on what you chose>"}
'round'/'circular' -> circle; 'oval' -> ellipse; 'pill'/'capsule'/'stadium'/'wristband strip' -> capsule;
'rounded corners' -> rounded_rect; otherwise rect. Use null for any size not stated."""


def _clamp(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def extract(text: str) -> dict:
    spec = {"kind": "rect", "corner_radius_mm": None, "max_width_mm": None, "max_height_mm": None,
            "note": "No shape recognised; using a rectangle.", "request": text.strip()[:300]}
    try:
        m = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": text[:300]}],
                 os.environ["SUB_MODEL"], max_tokens=250, temperature=0,
                 extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        j = json.loads(re.search(r"\{.*\}", m.content or "", re.S).group(0))
    except Exception:
        return spec
    kind = j.get("kind")
    if kind in outline.KINDS:
        spec["kind"] = kind
        spec["note"] = str(j.get("note") or "")[:200]
    spec["corner_radius_mm"] = _clamp(j.get("corner_radius_mm"), 0.5, 20) if spec["kind"] == "rounded_rect" else None
    if spec["kind"] == "rounded_rect" and spec["corner_radius_mm"] is None:
        spec["corner_radius_mm"] = 3.0
    spec["max_width_mm"] = _clamp(j.get("max_width_mm"), 5, 500)
    spec["max_height_mm"] = _clamp(j.get("max_height_mm"), 5, 500)
    return spec


def fit_report(spec: dict, w: float, h: float) -> dict:
    """Compare the requested limits with what the parts need (placement w x h, outline bbox)."""
    box = outline.bbox(outline.points(spec, w, h))
    problems = []
    if spec.get("max_width_mm") and box["width_mm"] > spec["max_width_mm"]:
        problems.append(f"needs {box['width_mm']} mm wide, you asked for at most {spec['max_width_mm']:g} mm")
    if spec.get("max_height_mm") and box["height_mm"] > spec["max_height_mm"]:
        problems.append(f"needs {box['height_mm']} mm tall, you asked for at most {spec['max_height_mm']:g} mm")
    return {**box, "fits": not problems, "problems": problems}
