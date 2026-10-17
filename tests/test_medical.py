import json
import re

import pytest

from cvhack import parts_graph
from cvhack.scrape import CACHE
from cvhack.scrape.medical import ISOLATION_CATEGORIES, RATINGS_PATH
from src.tools import parts as parts_tool

CATALOG = json.loads((parts_graph.ROOT / "data" / "parts_catalog.json").read_text())
PARTS = {p["mpn"]: p for p in CATALOG["parts"]}
RATINGS = {k: v for k, v in json.loads(RATINGS_PATH.read_text()).items() if not k.startswith("_")}
REQS = json.loads(parts_graph.MEDICAL_REQ_PATH.read_text())
GRAPH = parts_graph.load_graph()


def test_every_part_has_medical_evidence_sections():
    for p in CATALOG["parts"]:
        med = p["medical"]
        assert set(med) >= {"lifecycle", "traceability", "statements", "isolation", "leakage_current",
                            "biocompatibility", "datasheet_scan"}
        assert med["traceability"]["lot_date_code"] is None  # only knowable at goods receipt
        if p["category"] not in ISOLATION_CATEGORIES:
            assert med["isolation"] == {"applicable": False, "reason": med["isolation"]["reason"]}
        for st in med["statements"]:
            assert st["quote"] and st["source_id"].startswith("src:datasheet:")


def test_isolation_ratings_cover_the_isolation_parts():
    iso_parts = {m for m, p in PARTS.items() if p["category"] in ISOLATION_CATEGORIES}
    assert set(RATINGS) <= iso_parts
    not_done = json.loads(RATINGS_PATH.read_text())["_not_transcribed"]
    assert iso_parts == set(RATINGS) | set(not_done)
    for mpn in RATINGS:
        assert PARTS[mpn]["medical"]["isolation"]["status"] == "transcribed_from_datasheet"


def test_isolation_quotes_appear_in_the_datasheets():
    from cvhack.scrape.medical import datasheet_text
    if not (CACHE / "datasheet_text").exists():
        pytest.skip("datasheet cache not present; run python -m cvhack.scrape.medical")
    norm = lambda s: re.sub(r"[\s\x00-\x1f]+", "", s)  # noqa: E731  PDF text has stray control chars
    for mpn, r in RATINGS.items():
        text, _, _ = datasheet_text(PARTS[mpn]["datasheet_url"])
        for e in r["evidence"]:
            assert norm(e["quote"]) in norm(text), f"{mpn} {e['field']}: quote not in datasheet"


def test_mopp_claims_need_matching_package_spacing():
    two_mopp = next(r for r in REQS["insulation"] if r["id"] == "req:iec60601-1:mopp-2")
    for mpn, r in RATINGS.items():
        if (r.get("mopp") or {}).get("count") == 2:
            assert r["creepage_mm"] >= two_mopp["creepage_mm"] and r["clearance_mm"] >= two_mopp["clearance_mm"]


def test_screening_edges_match_the_numbers():
    reqs = {r["id"]: r for r in REQS["insulation"]}
    reached = {}
    for rel in GRAPH["relationships"]:
        if rel["relation"] != "rating_reaches":
            continue
        assert rel["attributes"]["status"] == "screening_only_not_certification"
        for check in rel["attributes"]["compared"].values():
            assert check["part"] >= check["required"]
        reached.setdefault(parts_graph.part_node(rel["source"])["label"], set()).add(rel["target"])
        assert rel["target"] in reqs
    assert "req:iec60601-1:mopp-2" in reached["ISOW7841DWER"]
    assert "req:iec60601-1:mopp-2" not in reached["ADUM4160BRWZ-RL"]  # 7.7 mm creepage in RW-16
    assert "req:iec60601-1:mopp-2" not in reached["SI8661BC-B-IS1"]  # narrow body


def test_reference_values():
    two = next(r for r in REQS["insulation"] if r["id"] == "req:iec60601-1:mopp-2")
    assert (two["test_voltage_vrms"], two["creepage_mm"], two["clearance_mm"]) == (4000, 8.0, 5.0)
    patient = next(r for r in REQS["leakage_current"] if r["id"] == "req:iec60601-1:patient-leakage")
    assert patient["limits_ua"]["CF"] == {"normal": 10, "single_fault": 50}


def test_lifecycle_is_sourced_or_unknown():
    for p in CATALOG["parts"]:
        life = p["medical"]["lifecycle"]
        if life["manufacturer_status"]:
            assert life["manufacturer_source_url"].startswith("https://www.ti.com/product/")
        else:
            assert life["status"] in ("unknown", "distributor_discontinued")
    statuses = [r for r in GRAPH["relationships"] if r["relation"] == "has_lifecycle_status"]
    assert len(statuses) == len(CATALOG["parts"])


def test_no_biocompatibility_claim_without_a_quote():
    for p in CATALOG["parts"]:
        bio = p["medical"]["biocompatibility"]
        assert (bio["datasheet_claim"] == "claim_found") == bool(bio["quotes"])


def test_medical_queries_and_tool():
    two = {n["label"] for n in parts_graph.search(mopp_at_least=2, limit=50)}
    assert "ISOW7841DWER" in two and "SI8661BC-B-IS1" not in two and "ADUM4160BRWZ-RL" not in two
    profile = json.loads(parts_tool.get_part_medical("ADUM4160BRWZ-RL"))
    assert profile["isolation"]["iec60601_rating"]["working_voltage_vrms"] == 125
    assert "not in the real-parts graph" in parts_tool.get_part_medical("NOPE-123")
