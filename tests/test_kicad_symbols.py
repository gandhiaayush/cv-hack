import pytest

from cvhack.mocks import designs
from cvhack.parts import load_parts
from cvhack.tooling import kicad_cli, kicad_lib, kicad_sch
from cvhack.tooling.kicad_lib import find, find_all, parse

PARTS = load_parts()
needs_kicad = pytest.mark.skipif(not (kicad_cli.available() and kicad_lib.symbol_dir()),
                                 reason="KiCad with symbol libraries not installed")


def test_sexpr_roundtrip():
    src = '(a "x \\"y\\"" (b 1 2) (c (d "e")))'
    assert parse(kicad_lib.dump(parse(src))) == parse(src)


@needs_kicad
def test_library_symbols_used_only_when_pins_agree():
    rep = kicad_sch.symbol_report(designs.wearable(), PARTS)
    assert rep["RC0402FR-0710KL"] == "Device:R"
    assert rep["MCP73831T-2ACI/OT"] == "Battery_Management:MCP73831-2-OT"
    assert rep["AP2112K-3.3TRG1"] == "Regulator_Linear:AP2112K-3.3"         # derived symbol, flattened
    assert rep["MAX30102EFD+T"].startswith("generated (pin 1")               # mock pinout disagrees with KiCad


@needs_kicad
def test_kicad_netlist_matches_design(tmp_path):
    """The drawing must not change connectivity: KiCad's own netlist == our merged nets."""
    import subprocess
    d = designs.wearable()
    sch = kicad_cli.write_project(d, PARTS, tmp_path)
    assert kicad_cli.run_erc(sch) == []
    net = tmp_path / "k.net"
    subprocess.run([kicad_cli.cli_path(), "sch", "export", "netlist", "--format", "kicadsexpr",
                    "--output", str(net), str(sch)], capture_output=True, check=True)
    t = parse(net.read_text())
    kicad = {str(find(n, "name")[1]).lstrip("/"): {(str(find(x, "ref")[1]), str(find(x, "pin")[1]))
                                                    for x in find_all(n, "node")}
             for n in find_all(find(t, "nets"), "net")}
    modeled = {(i.ref, p.num) for i in d.all_parts() for p in PARTS[i.mpn].pins}
    for name, nodes in d.merged_nets().items():
        assert {x for x in kicad[name] if x in modeled} == {(x.ref, x.pin) for x in nodes}, name


@needs_kicad
def test_render_svg_with_highlight(tmp_path):
    svg = kicad_cli.render_svg(designs.wearable(), PARTS, tmp_path, highlight={"U5"})
    assert svg.startswith("<svg") and "U5: failing check" in svg
    assert kicad_cli.render_svg(designs.wearable(), PARTS, tmp_path) is not None   # cached path
