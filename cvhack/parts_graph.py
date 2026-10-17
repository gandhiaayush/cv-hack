"""Build, validate and query the real-parts knowledge graph (data/parts_graph.json).

    python -m cvhack.parts_graph --write   # rebuild from data/parts_catalog.json
    python -m cvhack.parts_graph --check   # fail if stale or structurally invalid

Nodes: parts, manufacturers, categories and their groups, packages, KiCad
symbols and footprints, pinout classes, interfaces, the distributor, sources
(catalog snapshot, KiCad library, one datasheet per part) and the PPG mock
design. Every relationship carries the source IDs that support it, the same
convention as data/relationships.json, so the two graphs share part and design
IDs and can be loaded together.

`alternate_candidate` edges join parts in the same category with the same
pinout class, package and key value (e.g. a 3.3 V LDO in SOT-23-5 with the
same pin functions). They are leads for re-planning after a stock-out, not
verified drop-in replacements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "data" / "parts_catalog.json"
DESIGN_PATH = ROOT / "cvhack" / "mocks" / "design_wearable.json"
MOCK_PARTS_PATH = ROOT / "cvhack" / "mocks" / "parts.json"
MEDICAL_REQ_PATH = ROOT / "data" / "medical_requirements.json"
OUTPUT_PATH = ROOT / "data" / "parts_graph.json"
DESIGN_SOURCE_ID = "source:repo:design-wearable-mock"
DISTRIBUTOR_ID = "distributor:lcsc-jlcpcb"

# The spec that has to match for two parts to be alternates, per category.
KEY_SPECS = {
    "ldo": ("output_voltage",), "dcdc": ("output_voltage", "topology"),
    "charger": ("charging_saturation_voltage",), "resistor": ("resistance",),
    "capacitor": ("capacitance", "rated_voltage"), "inductor": ("inductance",),
    "tvs": ("reverse_stand_off_voltage_vrwm", "polarity"), "fuse": ("hold_current",),
    "led": ("illumination_color",), "crystal": ("frequency",), "mosfet": ("type",),
    "bjt": ("type",), "diode": ("reverse_voltage_vr", "zener_voltage_nom"),
    "supervisor": ("threshold_voltage",),
}
# Specs copied onto part nodes so the graph is useful without the catalog.
NODE_SPECS = ("output_voltage", "supply_voltage", "output_current", "resistance", "capacitance",
              "inductance", "rated_voltage", "tolerance", "reverse_stand_off_voltage_vrwm",
              "hold_current", "frequency", "charge_current_max", "topology", "cpu_core",
              "wireless_standard", "interface", "quiescent_current")
INTERFACES = {
    "i2c": r"\bi2c\b|\biic\b|\bi²c\b|\btwi\b", "spi": r"\bspi\b|\bqspi\b", "uart": r"\buart\b|\busart\b",
    "usb": r"\busb\b", "ble": r"bluetooth|\bble\b", "wifi": r"wi-?fi|802\.11", "lora": r"\blora\b",
    "can": r"\bcan\b", "1-wire": r"1-wire|one-wire|single bus", "analog": r"\banalog\b",
    "pwm": r"\bpwm\b", "smbus": r"\bsmbus\b",
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _pinout_signature(pins: list[dict[str, str]]) -> str:
    """Pin number -> electrical type, which is what ERC and a footprint swap care about."""
    sig = ";".join(f"{p['num']}:{p['type']}" for p in sorted(pins, key=lambda p: (len(p["num"]), p["num"])))
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


def _spec_scalar(spec: dict[str, Any] | None) -> Any:
    if not spec:
        return None
    if "value" in spec:
        v = spec["value"]
        return round(v, 9) if isinstance(v, float) else (v.lower() if isinstance(v, str) else v)
    return (spec.get("min"), spec.get("max"))


def build_graph() -> dict[str, Any]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    design = json.loads(DESIGN_PATH.read_text(encoding="utf-8"))
    design_id = "design:" + design["name"]
    design_mpns: dict[str, list[str]] = defaultdict(list)
    for block in design["blocks"]:
        for inst in block["parts"]:
            design_mpns[inst["mpn"].lower()].append(inst["ref"])

    nodes: list[dict[str, Any]] = []
    nodes_by_id: dict[str, dict[str, Any]] = {}
    relationships: list[dict[str, Any]] = []
    rel_keys: set[tuple[str, str, str]] = set()

    def add_node(node_id: str, node_type: str, label: str, **attributes: Any) -> str:
        if node_id not in nodes_by_id:
            node = {"id": node_id, "type": node_type, "label": label, "attributes": attributes}
            nodes.append(node)
            nodes_by_id[node_id] = node
        return node_id

    def rel(source: str, relation: str, target: str, evidence: list[str], **attributes: Any) -> None:
        key = (source, relation, target)
        if key in rel_keys:
            return
        rel_keys.add(key)
        r: dict[str, Any] = {"source": source, "relation": relation, "target": target,
                             "evidence_source_ids": evidence}
        if attributes:
            r["attributes"] = attributes
        relationships.append(r)

    source_registry = []
    for src in catalog["sources"]:
        add_node(src["source_id"], "source", src["document"], url=src["url"], publisher=src["publisher"],
                 evidence_kind=src["kind"], checked_at=src["checked_at"])
        source_registry.append(nodes_by_id[src["source_id"]])
    catalog_src = next(s["source_id"] for s in catalog["sources"] if s["kind"] == "distributor_catalog")
    kicad_src = next(s["source_id"] for s in catalog["sources"] if s["kind"] == "eda_library")
    add_node(DESIGN_SOURCE_ID, "source", "Wearable schematic mock", path="cvhack/mocks/design_wearable.json",
             evidence_kind="repository_fixture")
    source_registry.append(nodes_by_id[DESIGN_SOURCE_ID])
    add_node(design_id, "design", design["name"], status="mock", graph="data/relationships.json")
    add_node(DISTRIBUTOR_ID, "distributor", "LCSC / JLCPCB", url="https://www.lcsc.com/")

    # IEC 60601-1 reference values (secondary-source summary; see data/medical_requirements.json).
    med_req = json.loads(MEDICAL_REQ_PATH.read_text(encoding="utf-8"))
    for src in med_req["sources"]:
        add_node(src["source_id"], "source", src["document"], url=src["url"], publisher=src["publisher"],
                 evidence_kind="secondary_standard_summary", checked_at=src["checked_at"])
        source_registry.append(nodes_by_id[src["source_id"]])
    insulation_reqs = med_req["insulation"]
    for req in insulation_reqs + med_req["leakage_current"]:
        add_node(req["id"], "medical_requirement", req["label"],
                 **{k: v for k, v in req.items() if k not in ("id", "label", "source_ids")},
                 standard=med_req["standard"], status=med_req["status"])

    pinout_members: dict[str, list[str]] = defaultdict(list)
    parts_by_id: dict[str, dict[str, Any]] = {}
    for p in catalog["parts"]:
        pid = p["part_id"]
        parts_by_id[pid] = p
        kicad = p.get("kicad") or {}
        specs = p.get("specs", {})
        add_node(pid, "part", p["mpn"],
                 mpn=p["mpn"], manufacturer=p["manufacturer"], category=p["category"], group=p["group"],
                 description=p["description"], package=p["package"],
                 specs={k: {kk: vv for kk, vv in specs[k].items() if kk != "source_id"}
                        for k in NODE_SPECS if k in specs},
                 unit_price_usd=p["price"]["unit_amount"], stock=p["stock"]["available_qty"],
                 stock_checked_at=p["stock"]["checked_at"], sku=p["offer"]["sku"],
                 jlc_library_class=p["offer"]["jlc_library_class"], datasheet_url=p.get("datasheet_url"),
                 pin_count=len(kicad.get("pins", [])), pin_source=kicad.get("pin_source"),
                 area_mm2=(p.get("area_mm2") or {}).get("value"),
                 mock_part_id=("part:mock:" + _slug(p["mpn"])) if "cvhack/mocks/parts.json" in p.get("repo_refs", []) else None,
                 metadata_status="distributor_reported_not_datasheet_verified")

        mfr = add_node("manufacturer:" + _slug(p["manufacturer"]), "manufacturer", p["manufacturer"])
        rel(pid, "manufactured_by", mfr, [catalog_src])
        group = add_node("group:" + p["group"], "category_group", p["group"])
        cat = add_node("category:" + p["category"], "category", p["category"])
        rel(cat, "in_group", group, [catalog_src])
        rel(pid, "in_category", cat, [catalog_src])
        if p.get("package"):
            pkg = add_node("package:" + _slug(p["package"]), "package", p["package"])
            rel(pid, "has_package", pkg, [catalog_src])
        rel(pid, "offered_by", DISTRIBUTOR_ID, [catalog_src], sku=p["offer"]["sku"],
            unit_price_usd=p["price"]["unit_amount"], price_tiers=p["price"]["tiers"],
            stock=p["stock"]["available_qty"], checked_at=p["stock"]["checked_at"],
            source_url=p["stock"]["source_url"])
        for sid in p["sources"]:
            if sid.startswith("src:datasheet:"):
                rel(pid, "documented_by", sid, [sid])

        if kicad:
            sym = add_node("symbol:" + kicad["symbol"], "kicad_symbol", kicad["symbol"],
                           lib=kicad["symbol_lib"], name=kicad["symbol_name"], pins=kicad["pins"],
                           datasheet=kicad.get("kicad_datasheet"))
            rel(pid, "uses_symbol", sym, [kicad_src, catalog_src], pin_source=kicad["pin_source"],
                match_method=kicad["match_method"])
            if kicad.get("footprint"):
                fp = add_node("footprint:" + kicad["footprint"], "kicad_footprint", kicad["footprint"])
                rel(pid, "uses_footprint", fp, [kicad_src])
            if kicad["pins"]:
                sig = _pinout_signature(kicad["pins"])
                po = add_node("pinout:" + sig, "pinout_class", f"{len(kicad['pins'])}-pin pinout {sig}",
                              pins=[{"num": x["num"], "type": x["type"]} for x in kicad["pins"]])
                rel(pid, "has_pinout", po, [kicad_src])
                pinout_members[po].append(pid)

        text = " ".join(str(specs.get(k, {}).get("value", "")) for k in
                        ("interface", "interface_type", "wireless_standard", "communication_system")) + " " + p["description"]
        for iface, rx in INTERFACES.items():
            if re.search(rx, text.lower()):
                node = add_node("interface:" + iface, "interface", iface)
                rel(pid, "supports_interface", node, [catalog_src])

        refs = design_mpns.get(p["mpn"].lower())
        if refs:
            rel(pid, "used_in_design", design_id, [DESIGN_SOURCE_ID], refs=sorted(refs))

        med = p.get("medical")
        if med:
            _add_medical(pid, p, med, insulation_reqs, add_node, rel, nodes_by_id[pid]["attributes"], catalog_src)

    # Alternates: same category + pinout class + package family + key spec values.
    alt_count = 0
    for po, members in pinout_members.items():
        groups: dict[tuple, list[str]] = defaultdict(list)
        for pid in members:
            p = parts_by_id[pid]
            keys = KEY_SPECS.get(p["category"])
            if keys is None:
                continue
            values = tuple(_spec_scalar(p["specs"].get(k)) for k in keys)
            if all(v is None for v in values):
                continue
            footprint = (p.get("kicad") or {}).get("footprint") or p.get("package")
            groups[(p["category"], footprint, values)].append(pid)
        for (category, footprint, values), pids in groups.items():
            for a, b in combinations(sorted(pids), 2):
                rel(a, "alternate_candidate", b, [catalog_src, kicad_src], symmetric=True,
                    basis=f"same category, pinout class, footprint and {', '.join(KEY_SPECS[category])}",
                    status="unverified_candidate")
                alt_count += 1

    type_counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        type_counts[n["type"]] += 1
    rel_counts: dict[str, int] = defaultdict(int)
    for r in relationships:
        rel_counts[r["relation"]] += 1

    graph = {
        "schema_version": "1.0.0",
        "graph_id": "cv-hack-real-parts",
        "graph_status": "scraped_catalog_relationships",
        "generated_from": ["data/parts_catalog.json", "cvhack/mocks/design_wearable.json"],
        "catalog_snapshot": catalog["retrieved_on"],
        "source_registry": source_registry,
        "nodes": nodes,
        "relationships": relationships,
        "coverage": {
            "parts": type_counts["part"],
            "parts_with_pins": sum(1 for p in catalog["parts"] if (p.get("kicad") or {}).get("pins")),
            "parts_with_datasheet": sum(1 for p in catalog["parts"] if p.get("datasheet_url")),
            "parts_used_in_mock_design": sum(1 for r in relationships if r["relation"] == "used_in_design"),
            "alternate_candidate_pairs": alt_count,
            "medical": catalog["coverage"].get("medical"),
            "node_types": dict(sorted(type_counts.items())),
            "relationship_types": dict(sorted(rel_counts.items())),
        },
        "mock_pin_conflicts": _mock_pin_conflicts(catalog["parts"]),
        "limitations": [
            "Price and stock are a dated LCSC/JLCPCB snapshot (catalog_snapshot); they change daily.",
            "Specs are distributor-reported screening values, not datasheet-verified limits.",
            "Pinouts come from KiCad library symbols matched by MPN and package; parts without a "
            "confident match have no pinout rather than a guessed one.",
            "alternate_candidate means same category, pinout class, footprint and key spec; it is not "
            "a verified drop-in substitution. Re-run electrical checks after any swap.",
            "This graph does not establish medical-device or regulatory compliance.",
        ],
    }
    validate_graph(graph)
    return graph


def _mock_pin_conflicts(catalog_parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mock-registry pins that disagree with the KiCad symbol for the same MPN."""
    mock = {p["mpn"]: p for p in json.loads(MOCK_PARTS_PATH.read_text(encoding="utf-8"))}
    out = []
    for p in catalog_parts:
        kicad = p.get("kicad") or {}
        if p["mpn"] not in mock or kicad.get("pin_source") != "kicad_symbol":
            continue
        real = {x["num"]: x for x in kicad["pins"]}
        for pin in mock[p["mpn"]].get("pins", []):
            r = real.get(pin["num"])
            # Only differences ERC acts on: a pin that is really power or no-connect, or vice versa.
            serious = {"no_connect", "power_in", "power_out"}
            if r is None or (r["type"] != pin["type"] and serious & {r["type"], pin["type"]}):
                out.append({
                    "mpn": p["mpn"], "mock_part_id": "part:mock:" + _slug(p["mpn"]), "pin": pin["num"],
                    "mock": {"name": pin["name"], "type": pin["type"]},
                    "kicad": {"name": r["name"], "type": r["type"]} if r else None,
                    "kicad_symbol": kicad["symbol"],
                })
    return out

STATEMENT_LABELS = {
    "iec60601_1_safety": "Datasheet references IEC 60601-1 (safety)",
    "iec60601_1_2_emc": "Datasheet references IEC 60601-1-2 (EMC)",
    "mopp_or_moop": "Datasheet states MOPP/MOOP",
    "iso10993_or_biocompat": "Datasheet claims ISO 10993 / biocompatibility",
    "medical_use_restriction": "Datasheet restricts life-support / medical use",
    "medical_application": "Datasheet lists medical applications",
    "aec_q_qualified": "Datasheet states AEC-Q qualification",
}


def _add_medical(pid, p, med, insulation_reqs, add_node, rel, attrs, catalog_src) -> None:
    """Lifecycle, datasheet statements, certifications and isolation screening for one part."""
    life = med["lifecycle"]
    trace = med["traceability"]
    iso = med["isolation"]
    attrs["medical"] = {
        "lifecycle_status": life["status"], "manufacturer_status": life["manufacturer_status"],
        "distributor_lifecycle": life["distributor_lifecycle"], "rohs": trace["rohs"], "eccn": trace["eccn"],
        "datasheet_sha256": trace["datasheet_sha256"],
        "statements": sorted({s["kind"] for s in med["statements"]}),
        "biocompatibility_claim": med["biocompatibility"]["datasheet_claim"],
        "patient_contact_path_candidate": med["biocompatibility"]["patient_contact_path_candidate"],
        "isolation_applicable": iso["applicable"],
        "leakage_estimate_ua": med["leakage_current"].get("estimate_ua"),
    }
    if iso.get("status") == "transcribed_from_datasheet":
        attrs["medical"]["isolation"] = {k: iso.get(k) for k in (
            "isolation_voltage_vrms", "isolation_voltage_vdc", "isolation_voltage_vpk", "working_voltage_vrms",
            "repetitive_peak_vpk", "creepage_mm", "clearance_mm", "dti_um", "cti_v", "insulation",
            "barrier_capacitance_pf", "package_column", "mopp", "iec60601_rating", "certifications", "notes")
            if iso.get(k) is not None}
    lc = add_node("lifecycle:" + life["status"], "lifecycle_status", life["status"])
    rel(pid, "has_lifecycle_status", lc, life["source_ids"] or [catalog_src],
        manufacturer_status=life["manufacturer_status"], distributor_lifecycle=life["distributor_lifecycle"],
        checked_at=life["checked_at"])
    for st in med["statements"]:
        node = add_node("statement:" + st["kind"], "datasheet_statement", STATEMENT_LABELS[st["kind"]])
        if st.get("source_id"):
            rel(pid, "datasheet_states", node, [st["source_id"]], quote=st["quote"])
    if iso.get("status") != "transcribed_from_datasheet":
        return
    evidence = ["src:isolation-ratings"] + [s for s in p["sources"] if s.startswith("src:datasheet:")]
    for cert in iso.get("certifications") or []:
        node = add_node("standard:" + _slug(cert), "safety_standard", cert)
        rel(pid, "certified_to", node, evidence)
    # Screening: does the part's own rating reach each insulation level's test voltage and spacing?
    for req in insulation_reqs:
        checks = {
            "test_voltage": (iso.get("isolation_voltage_vrms"), req["test_voltage_vrms"]),
            "creepage": (iso.get("creepage_mm"), req["creepage_mm"]),
            "clearance": (iso.get("clearance_mm"), req["clearance_mm"]),
        }
        if any(have is None for have, _ in checks.values()):
            continue
        if all(have >= need for have, need in checks.values()):
            rel(pid, "rating_reaches", req["id"], evidence, status="screening_only_not_certification",
                compared={k: {"part": h, "required": n} for k, (h, n) in checks.items()},
                note="Package rating only; the board layout, working voltage and the part's own "
                     "IEC 60601-1 certificate decide whether it provides this means of protection.")


def validate_graph(graph: dict[str, Any]) -> None:
    ids = [n["id"] for n in graph["nodes"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Graph contains duplicate node IDs")
    id_set = set(ids)
    sources = {s["id"] for s in graph["source_registry"]}
    if sources != {n["id"] for n in graph["nodes"] if n["type"] == "source"}:
        raise ValueError("Source registry and source nodes do not match")
    for r in graph["relationships"]:
        for end in ("source", "target"):
            if r[end] not in id_set:
                raise ValueError(f"Relationship {end} does not resolve: {r[end]}")
        if not r["evidence_source_ids"]:
            raise ValueError("Relationship without evidence: " + repr(r))
        for sid in r["evidence_source_ids"]:
            if sid not in sources:
                raise ValueError("Relationship evidence source does not resolve: " + sid)
    parts = {n["id"] for n in graph["nodes"] if n["type"] == "part"}
    for relation in ("manufactured_by", "in_category", "offered_by"):
        have = {r["source"] for r in graph["relationships"] if r["relation"] == relation}
        if have != parts:
            raise ValueError(f"Every part needs exactly the {relation} relationship")
    if graph["coverage"]["parts"] != len(parts):
        raise ValueError("Part coverage count is inconsistent")


# ---------------------------------------------------------------------------
# Queries (used by the agents' parts tools)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_graph() -> dict[str, Any]:
    return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, str]]:
    g = load_graph()
    nodes = {n["id"]: n for n in g["nodes"]}
    out: dict[str, list[dict]] = defaultdict(list)
    for r in g["relationships"]:
        out[r["source"]].append(r)
        if (r.get("attributes") or {}).get("symmetric"):
            out[r["target"]].append({**r, "source": r["target"], "target": r["source"]})
    by_mpn = {n["attributes"]["mpn"].lower(): n["id"] for n in g["nodes"] if n["type"] == "part"}
    return nodes, out, by_mpn


def part_node(mpn_or_id: str) -> dict[str, Any] | None:
    nodes, _, by_mpn = _index()
    return nodes.get(mpn_or_id) or nodes.get(by_mpn.get(mpn_or_id.lower(), ""))


def neighbors(node_id: str, relation: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    nodes, out, _ = _index()
    return [(r, nodes[r["target"]]) for r in out.get(node_id, []) if relation is None or r["relation"] == relation]


def alternates(mpn: str, in_stock_only: bool = True) -> list[dict[str, Any]]:
    node = part_node(mpn)
    if node is None:
        return []
    alts = [n for _, n in neighbors(node["id"], "alternate_candidate")]
    if in_stock_only:
        alts = [n for n in alts if (n["attributes"].get("stock") or 0) > 0]
    return sorted(alts, key=lambda n: (n["attributes"].get("unit_price_usd") or 1e9))


def search(category: str | None = None, text: str | None = None, interface: str | None = None,
           spec_ranges: dict[str, tuple[float | None, float | None]] | None = None,
           in_stock_only: bool = True, needs_pins: bool = False, limit: int = 20,
           mopp_at_least: int | None = None, lifecycle: str | None = None) -> list[dict[str, Any]]:
    """Filter part nodes. spec_ranges: {"output_voltage": (3.2, 3.4)} matches value (or max) in range.
    mopp_at_least keeps isolation parts whose datasheet states that many MOPP for their package;
    lifecycle keeps parts with that lifecycle status (e.g. "active")."""
    g = load_graph()
    iface_parts = None
    if interface:
        iface_parts = {s for s, rels in _index()[1].items() for r in rels
                       if r["relation"] == "supports_interface" and r["target"] == "interface:" + interface.lower()}
    hits = []
    for n in g["nodes"]:
        if n["type"] != "part":
            continue
        a = n["attributes"]
        if category and a["category"] != category:
            continue
        if text and text.lower() not in f"{a['mpn']} {a['manufacturer']} {a['description']}".lower():
            continue
        if iface_parts is not None and n["id"] not in iface_parts:
            continue
        if in_stock_only and not (a.get("stock") or 0) > 0:
            continue
        if needs_pins and not a.get("pin_count"):
            continue
        med = a.get("medical") or {}
        if lifecycle and med.get("lifecycle_status") != lifecycle:
            continue
        if mopp_at_least and ((med.get("isolation") or {}).get("mopp") or {}).get("count", 0) < mopp_at_least:
            continue
        ok = True
        for spec, (lo, hi) in (spec_ranges or {}).items():
            s = a["specs"].get(spec) or {}
            v = s.get("value", s.get("max"))
            if not isinstance(v, (int, float)) or (lo is not None and v < lo) or (hi is not None and v > hi):
                ok = False
                break
        if ok:
            hits.append(n)
    # Parts with a known pinout first (the electrical gate can check them), then cheapest.
    hits.sort(key=lambda n: (0 if n["attributes"].get("pin_count") else 1, n["attributes"].get("unit_price_usd") or 1e9))
    return hits[:limit]


def medical_profile(mpn: str) -> dict[str, Any] | None:
    """Medical evidence for one part: lifecycle, statements (with quotes), certifications,
    isolation ratings, leakage estimate and the IEC 60601-1 levels its rating reaches."""
    node = part_node(mpn)
    if node is None:
        return None
    med = node["attributes"].get("medical") or {}
    rels = neighbors(node["id"])
    return {
        "mpn": node["label"], **med,
        "datasheet_statements": [{"kind": n["id"].split(":", 1)[1], "quote": r["attributes"]["quote"]}
                                 for r, n in rels if r["relation"] == "datasheet_states"],
        "certified_to": [n["label"] for r, n in rels if r["relation"] == "certified_to"],
        "rating_reaches": [{"requirement": n["id"], "label": n["label"], **r["attributes"]}
                           for r, n in rels if r["relation"] == "rating_reaches"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Regenerate data/parts_graph.json")
    mode.add_argument("--check", action="store_true", help="Validate generated data is current")
    args = parser.parse_args()

    graph = build_graph()
    serialized = json.dumps(graph, indent=1, ensure_ascii=False) + "\n"
    if args.write:
        OUTPUT_PATH.write_text(serialized, encoding="utf-8")
        print("Wrote", OUTPUT_PATH.relative_to(ROOT), graph["coverage"])
    else:
        if not OUTPUT_PATH.exists():
            raise SystemExit("Missing generated graph: " + str(OUTPUT_PATH))
        if OUTPUT_PATH.read_text(encoding="utf-8") != serialized:
            raise SystemExit("Graph is stale; run python -m cvhack.parts_graph --write")
        print("Graph is current and valid:", graph["coverage"])


if __name__ == "__main__":
    main()
