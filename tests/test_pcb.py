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
