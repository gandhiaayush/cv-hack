import json

from cvhack import parts_graph
from src.tools import parts as parts_tool


def test_graph_is_current_and_valid():
    graph = parts_graph.build_graph()
    assert parts_graph.OUTPUT_PATH.read_text(encoding="utf-8") == json.dumps(graph, indent=1, ensure_ascii=False) + "\n"
    assert graph["coverage"]["parts"] >= 500


def test_alternates_share_pinout_and_value():
    src = parts_graph.part_node("AP2112K-3.3TRG1")
    alts = parts_graph.alternates("AP2112K-3.3TRG1")
    assert alts, "a 3.3 V SOT-23-5 LDO should have in-stock alternates"
    pinout = {n["id"] for _, n in parts_graph.neighbors(src["id"], "has_pinout")}
    for alt in alts:
        assert alt["attributes"]["category"] == "ldo" and alt["attributes"]["stock"] > 0
        assert alt["attributes"]["specs"]["output_voltage"]["value"] == 3.3
        assert {n["id"] for _, n in parts_graph.neighbors(alt["id"], "has_pinout")} == pinout


def test_search_by_spec_and_interface():
    ldos = parts_graph.search(category="ldo", spec_ranges={"output_voltage": (1.7, 1.9)})
    assert ldos and all(1.7 <= n["attributes"]["specs"]["output_voltage"]["value"] <= 1.9 for n in ldos)
    i2c = {n["attributes"]["mpn"] for n in parts_graph.search(interface="i2c", limit=200)}
    assert "LIS3DHTR" in i2c


def test_mock_design_parts_link_to_real_records():
    used = [r for r in parts_graph.load_graph()["relationships"] if r["relation"] == "used_in_design"]
    assert {"AP2112K-3.3TRG1", "MAX30102EFD+T"} <= {parts_graph.part_node(r["source"])["label"] for r in used}


def test_mock_pin_conflicts_are_reported():
    conflicts = parts_graph.load_graph()["mock_pin_conflicts"]
    assert any(c["mpn"] == "MAX30102EFD+T" and c["pin"] == "4" for c in conflicts)


def test_agent_tools_return_json():
    rows = json.loads(parts_tool.search_parts(category="tvs", limit=3))
    assert len(rows) == 3 and all(r["stock"] > 0 for r in rows)
    assert json.loads(parts_tool.find_alternates("AP2112K-3.3TRG1"))
    assert "not in the real-parts graph" in parts_tool.find_alternates("NOPE-123")
