import json
import struct

import pytest

from cvhack.mocks import designs
from cvhack.parts import load_parts
from cvhack.tooling import pcb

pytestmark = pytest.mark.skipif(not pcb.available(), reason="KiCad python/footprints not installed")


def test_board_builds_with_every_part_and_exports_glb(tmp_path):
    d = designs.wearable()
    glb, info = pcb.render_glb(d, load_parts(), tmp_path)
    refs = {i.ref for i in d.all_parts()}
    assert refs <= set(info["placements"])                      # every part placed (plus mounting holes)
    assert 20 < info["width_mm"] < 120 and 20 < info["height_mm"] < 120
    b = glb.read_bytes()
    assert b[:4] == b"glTF"
    gltf = json.loads(b[20:20 + struct.unpack("<I", b[12:16])[0]])
    named = {n.get("name") for n in gltf["nodes"]}
    assert {"J1", "U1", "C1"} <= named                           # nodes carry reference designators
    # Pads carry the design's nets.
    text = (glb.parent / f"{d.name}.kicad_pcb").read_text()
    assert '(net "GND")' in text or 'net 1 "GND"' in text or '"GND"' in text


def test_incremental_placement_keeps_existing_parts(tmp_path):
    full = designs.wearable()
    partial = full.model_copy(deep=True)
    partial.blocks = partial.blocks[:2]
    parts = load_parts()
    a = pcb.place(partial, parts, tmp_path)
    prev = {k: a.info[k] for k in ("width_mm", "height_mm", "placements")}
    b = pcb.place(full, parts, tmp_path, previous=prev)
    for ref, p in a.info["placements"].items():
        q = b.info["placements"][ref]
        assert abs(p["x"] - q["x"]) < 0.01 and abs(p["y"] - q["y"]) < 0.01, ref   # nothing already placed moves
    new = set(b.info["placed_new"])
    assert new == {i.ref for i in full.all_parts()} - {i.ref for i in partial.all_parts()}


@pytest.mark.skipif(not pcb.can_route(), reason="Freerouting not set up (demo/setup_router.sh)")
def test_routed_board_passes_kicad_drc(tmp_path):
    b = pcb.route(pcb.place(designs.wearable(), load_parts(), tmp_path))
    r = b.route_info
    assert r["tracks"] > 100 and r["vias"] > 0
    assert r["drc"]["errors"] == {}, r["drc"]["details"]
    assert b.routed_glb.read_bytes()[:4] == b"glTF"
