"""Build and validate the relationship graph for the checked-in PPG mock."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DESIGN_PATH = ROOT / "cvhack" / "mocks" / "design_wearable.json"
PARTS_PATH = ROOT / "cvhack" / "mocks" / "parts.json"
RULES_PATH = ROOT / "data" / "rules.json"
OUTPUT_PATH = ROOT / "data" / "relationships.json"
DESIGN_SOURCE_ID = "source:repo:design-wearable-mock"
PARTS_SOURCE_ID = "source:repo:parts-mock"
RULES_SOURCE_ID = "source:repo:demo-rules"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _part_id(mpn: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", mpn.lower()).strip("-")
    return "part:mock:" + normalized


def _instance_id(design_id: str, ref: str) -> str:
    return "instance:" + design_id.removeprefix("design:") + ":" + ref


def _net_id(design_id: str, name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return "net:" + design_id.removeprefix("design:") + ":" + normalized


def _pin_id(instance_id: str, pin: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", pin.lower()).strip("-")
    return instance_id + ":pin:" + normalized


def build_graph() -> dict[str, Any]:
    design = _load_json(DESIGN_PATH)
    parts = _load_json(PARTS_PATH)
    rule_set = _load_json(RULES_PATH)
    design_id = "design:" + design["name"]
    if rule_set["scope"]["design_id"] != design_id:
        raise ValueError("Rule set design_id does not match the graph design")
    parts_by_mpn = {part["mpn"]: part for part in parts}
    if len(parts_by_mpn) != len(parts):
        raise ValueError("Mock parts contain duplicate MPNs")

    nodes: list[dict[str, Any]] = [
        {
            "id": DESIGN_SOURCE_ID,
            "type": "source",
            "label": "Wearable schematic mock",
            "attributes": {
                "path": "cvhack/mocks/design_wearable.json",
                "evidence_kind": "repository_fixture",
                "scope": "Design instances, subsystem assignment, nets, and pin connections only",
            },
        },
        {
            "id": PARTS_SOURCE_ID,
            "type": "source",
            "label": "Mock parts registry",
            "attributes": {
                "path": "cvhack/mocks/parts.json",
                "evidence_kind": "repository_fixture",
                "scope": "Mock MPN descriptions, symbol pins, footprints, and placeholder ratings",
            },
        },
        {
            "id": RULES_SOURCE_ID,
            "type": "source",
            "label": "Project-defined demo rule catalog",
            "attributes": {
                "path": "data/rules.json",
                "evidence_kind": "repository_fixture",
                "scope": "Prototype design consistency checks; not regulatory clauses",
            },
        },
        {
            "id": design_id,
            "type": "design",
            "label": design["name"],
            "attributes": {
                "status": "mock",
                "requirements_status": "pending_authoritative_requirement_ids",
            },
        },
    ]
    relationships: list[dict[str, Any]] = []
    relationship_keys: set[tuple[str, str, str, str]] = set()
    nodes_by_id = {node["id"]: node for node in nodes}

    def add_node(node: dict[str, Any]) -> None:
        if node["id"] in nodes_by_id:
            if nodes_by_id[node["id"]] != node:
                raise ValueError("Conflicting graph nodes normalize to the same ID: " + node["id"])
            return
        nodes.append(node)
        nodes_by_id[node["id"]] = node

    def add_relationship(
        source: str,
        relation: str,
        target: str,
        evidence: list[str],
        **attributes: Any,
    ) -> None:
        relationship = {
            "source": source,
            "relation": relation,
            "target": target,
            "evidence_source_ids": evidence,
            **({"attributes": attributes} if attributes else {}),
        }
        key = (source, relation, target, json.dumps(attributes, sort_keys=True))
        if key not in relationship_keys:
            relationships.append(relationship)
            relationship_keys.add(key)

    add_relationship(design_id, "documented_by", DESIGN_SOURCE_ID, [DESIGN_SOURCE_ID])
    add_relationship(design_id, "checked_by_rule_set", RULES_SOURCE_ID, [RULES_SOURCE_ID])

    used_mpns: set[str] = set()
    subsystem_ids: set[str] = set()
    instance_refs: dict[str, str] = {}
    net_names: set[str] = set()
    pin_numbers_by_ref: dict[str, set[str]] = {}
    connected_pin_ids: set[str] = set()
    intentionally_open: set[tuple[str, str]] = {
        (node["ref"], node["pin"]) for node in design.get("no_connects", [])
    }
    all_pin_ids: set[str] = set()

    for block in design["blocks"]:
        subsystem_id = "subsystem:" + block["subsystem"]
        subsystem_ids.add(subsystem_id)
        add_node(
            {
                "id": subsystem_id,
                "type": "subsystem",
                "label": block["subsystem"],
                "attributes": {},
            }
        )
        add_relationship(design_id, "contains_subsystem", subsystem_id, [DESIGN_SOURCE_ID])

        for instance in block["parts"]:
            ref = instance["ref"]
            mpn = instance["mpn"]
            if ref in instance_refs:
                raise ValueError("Duplicate component reference designator: " + ref)
            if mpn not in parts_by_mpn:
                raise ValueError("No mock part metadata for design MPN: " + mpn)
            instance_refs[ref] = mpn
            pin_numbers_by_ref[ref] = {
                pin["num"] for pin in parts_by_mpn[mpn].get("pins", [])
            }
            used_mpns.add(mpn)

            instance_id = _instance_id(design_id, ref)
            part_id = _part_id(mpn)
            part = parts_by_mpn[mpn]
            add_node(
                {
                    "id": part_id,
                    "type": "part",
                    "label": mpn,
                    "attributes": {
                        "mpn": mpn,
                        "category": part["category"],
                        "description": part.get("description", ""),
                        "footprint": part.get("footprint", ""),
                        "mock_ratings": part.get("ratings", {}),
                        "metadata_status": "mock_only_not_manufacturer_verified",
                    },
                }
            )
            add_node(
                {
                    "id": instance_id,
                    "type": "part_instance",
                    "label": ref,
                    "attributes": {"reference_designator": ref, "subsystem": block["subsystem"]},
                }
            )
            add_relationship(subsystem_id, "contains_instance", instance_id, [DESIGN_SOURCE_ID])
            add_relationship(instance_id, "instance_of", part_id, [DESIGN_SOURCE_ID, PARTS_SOURCE_ID])
            add_relationship(part_id, "described_in_mock_registry", PARTS_SOURCE_ID, [PARTS_SOURCE_ID])

            for pin in part.get("pins", []):
                pin_id = _pin_id(instance_id, pin["num"])
                all_pin_ids.add(pin_id)
                add_node(
                    {
                        "id": pin_id,
                        "type": "pin",
                        "label": pin["name"],
                        "attributes": {
                            "number": pin["num"],
                            "name": pin["name"],
                            "electrical_type": pin.get("type", "passive"),
                            "pin_scope": "mock_registry_symbol_interface_not_verified_complete_package_pinout",
                        },
                    }
                )
                add_relationship(instance_id, "has_pin", pin_id, [PARTS_SOURCE_ID])

    for block in design["blocks"]:
        for net in block["nets"]:
            name = net["name"]
            net_names.add(name)
            net_id = _net_id(design_id, name)
            add_node(
                {
                    "id": net_id,
                    "type": "net",
                    "label": name,
                    "attributes": {"name": name},
                }
            )
            for node in net["nodes"]:
                if node["ref"] not in instance_refs:
                    raise ValueError("Net references unknown component: " + node["ref"])
                instance_id = _instance_id(design_id, node["ref"])
                pin_id = _pin_id(instance_id, node["pin"])
                if pin_id not in all_pin_ids:
                    raise ValueError(
                        "Net references pin absent from mock pin registry: "
                        + node["ref"]
                        + "."
                        + node["pin"]
                    )
                connected_pin_ids.add(pin_id)
                add_relationship(
                    pin_id,
                    "connected_to_net",
                    net_id,
                    [DESIGN_SOURCE_ID],
                    pin_number=node["pin"],
                )

    open_pin_ids: set[str] = set()
    for ref, pin in sorted(intentionally_open):
        if ref not in instance_refs:
            raise ValueError("No-connect references unknown component: " + ref)
        pin_id = _pin_id(_instance_id(design_id, ref), pin)
        if pin_id not in all_pin_ids:
            raise ValueError("No-connect pin absent from mock pin registry: " + ref + "." + pin)
        open_pin_ids.add(pin_id)
        add_relationship(
            pin_id,
            "intentionally_no_connect",
            design_id,
            [DESIGN_SOURCE_ID],
        )

    rule_ids: set[str] = set()
    rule_relationship_count = 0
    for rule in rule_set["rules"]:
        rule_id = rule["id"]
        if rule_id in rule_ids:
            raise ValueError("Duplicate rule ID: " + rule_id)
        rule_ids.add(rule_id)
        if not rule.get("evidence_source_ids"):
            raise ValueError("Rule has no evidence source IDs: " + rule_id)
        if not rule.get("targets"):
            raise ValueError("Rule has no graph targets: " + rule_id)
        if not rule.get("failure_code"):
            raise ValueError("Rule has no failure_code: " + rule_id)
        for source_id in rule["evidence_source_ids"]:
            if source_id not in {DESIGN_SOURCE_ID, PARTS_SOURCE_ID}:
                raise ValueError("Rule references unknown evidence source: " + source_id)
        _validate_check(
            rule["check"],
            rule_set["operator_catalog"],
            instance_refs,
            parts_by_mpn,
            pin_numbers_by_ref,
            net_names,
        )
        if rule["applies_to"] != design_id:
            raise ValueError("Rule applies_to does not match graph design: " + rule_id)
        declared_targets = {
            (target["type"], target.get("ref", target.get("name")))
            for target in rule["targets"]
        }
        check_targets = _check_targets(rule["check"])
        if not check_targets <= declared_targets:
            missing_targets = sorted(check_targets - declared_targets)
            raise ValueError(
                "Rule targets omit entities referenced by checks: "
                + rule_id
                + " "
                + repr(missing_targets)
            )
        rule_node_id = "rule:" + rule_id.lower()
        add_node(
            {
                "id": rule_node_id,
                "type": "rule",
                "label": rule["title"],
                "attributes": {
                    "rule_id": rule_id,
                    "clause": rule["clause"],
                    "category": rule["category"],
                    "severity": rule["severity"],
                    "check": rule["check"],
                    "rationale": rule["rationale"],
                    "rule_set_id": rule_set["rule_set_id"],
                    "scope": "project_defined_non_regulatory_demo_check",
                },
            }
        )
        add_relationship(
            rule_node_id,
            "defined_in_rule_set",
            RULES_SOURCE_ID,
            [RULES_SOURCE_ID],
        )
        for target in rule["targets"]:
            if target["type"] == "part_instance":
                target_id = _instance_id(design_id, target["ref"])
            elif target["type"] == "net":
                target_id = _net_id(design_id, target["name"])
            elif target["type"] == "subsystem":
                target_id = "subsystem:" + target["name"]
            else:
                raise ValueError("Unsupported rule target type: " + target["type"])
            if target_id not in nodes_by_id:
                raise ValueError(
                    "Rule target does not resolve: " + rule_id + " -> " + target_id
                )
            add_relationship(
                rule_node_id,
                "applies_to",
                target_id,
                rule["evidence_source_ids"],
            )
            rule_relationship_count += 1

    connected_or_intentionally_open = connected_pin_ids | open_pin_ids
    unconnected_pin_ids = all_pin_ids - connected_or_intentionally_open
    evidence_gaps = [
        {
            "entity_id": _part_id(mpn),
            "mpn": mpn,
            "missing": "manufacturer datasheet or official product documentation",
            "note": "The linked mock registry is an implementation fixture, not manufacturer evidence.",
        }
        for mpn in sorted(used_mpns)
    ]

    graph = {
        "schema_version": "1.0.0",
        "graph_id": "cv-hack-ppg-wearable-mock",
        "graph_status": "schematic_relationships_only",
        "generated_from": [
            "cvhack/mocks/design_wearable.json",
            "cvhack/mocks/parts.json",
        ],
        "source_registry": [
            nodes_by_id[DESIGN_SOURCE_ID],
            nodes_by_id[PARTS_SOURCE_ID],
            nodes_by_id[RULES_SOURCE_ID],
        ],
        "nodes": nodes,
        "relationships": relationships,
        "coverage": {
            "design_part_instances": len(instance_refs),
            "unique_design_parts": len(used_mpns),
            "all_design_parts_have_mock_metadata": True,
            "subsystems": len(subsystem_ids),
            "nets": len(net_names),
            "mock_symbol_pins": len(all_pin_ids),
            "connected_pins": len(connected_pin_ids),
            "intentionally_unconnected_pins": len(open_pin_ids),
            "unconnected_mock_symbol_pins": len(unconnected_pin_ids),
            "requirement_nodes": 0,
            "requirement_edges": 0,
            "rule_nodes": len(rule_ids),
            "rule_relationships": rule_relationship_count,
            "verifier_result_nodes": 0,
            "manufacturer_evidence_gaps": len(evidence_gaps),
        },
        "pending_relationships": [
            {
                "relation": "requirement_allocated_to_subsystem",
                "blocked_by": "A must publish authoritative requirement IDs and subsystem assignments.",
            },
            {
                "relation": "part_candidate_for_or_selected_for_requirement",
                "blocked_by": "Requirements and acceptance criteria are not represented in the current mock.",
            },
            {
                "relation": "check_result_verifies_requirement_part_or_rule",
                "blocked_by": "Verifier results are produced per run and are not available as static design facts.",
            },
        ],
        "evidence_gaps": evidence_gaps,
        "limitations": [
            "Connectivity and subsystem placement reflect the checked-in mock design, not a validated hardware design.",
            "Mock part ratings and pin interfaces are not manufacturer-verified or necessarily complete.",
            "The separate sourced data/parts.json seed currently has a different environmental-wearable profile and does not establish suitability for this PPG mock.",
            "This graph does not establish medical-device or regulatory compliance.",
        ],
    }
    validate_graph(graph)
    return graph


def _validate_check(
    check: dict[str, Any],
    operator_catalog: dict[str, Any],
    instance_refs: dict[str, str],
    parts_by_mpn: dict[str, Any],
    pin_numbers_by_ref: dict[str, set[str]],
    net_names: set[str],
) -> None:
    operator = check.get("operator")
    if operator not in operator_catalog:
        raise ValueError("Unsupported rule check operator: " + str(operator))
    operator_definition = operator_catalog[operator]
    if set(check) - set(operator_definition["allowed_fields"]):
        raise ValueError("Rule check contains unsupported fields for operator: " + operator)
    for field in operator_definition["required_fields"]:
        if field not in check:
            raise ValueError("Rule check is missing required field: " + field)
    if operator == "all_of":
        if not isinstance(check["checks"], list) or not check["checks"]:
            raise ValueError("all_of requires a non-empty checks list")
        for child in check["checks"]:
            _validate_check(
                child,
                operator_catalog,
                instance_refs,
                parts_by_mpn,
                pin_numbers_by_ref,
                net_names,
            )
        return

    ref = check["ref"]
    if ref not in instance_refs:
        raise ValueError("Rule check references unknown component: " + ref)
    if operator == "instance_has_mpn" and check["mpn"] not in parts_by_mpn:
        raise ValueError("Rule check references unknown MPN: " + check["mpn"])
    if operator == "pin_on_net":
        if check["pin"] not in pin_numbers_by_ref[ref]:
            raise ValueError("Rule check references unknown pin: " + ref + "." + check["pin"])
        if check["net"] not in net_names:
            raise ValueError("Rule check references unknown net: " + check["net"])
    if operator == "part_value_is" and instance_refs[ref] not in parts_by_mpn:
        raise ValueError("Rule check has no part metadata for component: " + ref)


def _check_targets(check: dict[str, Any]) -> set[tuple[str, str]]:
    operator = check["operator"]
    if operator == "all_of":
        return set().union(*(_check_targets(child) for child in check["checks"]))
    targets = {("part_instance", check["ref"])}
    if operator == "pin_on_net":
        targets.add(("net", check["net"]))
    return targets


def validate_graph(graph: dict[str, Any]) -> None:
    node_ids = [node["id"] for node in graph["nodes"]]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("Graph contains duplicate node IDs")
    node_id_set = set(node_ids)
    source_ids = {source["id"] for source in graph["source_registry"]}
    if source_ids != {
        node["id"] for node in graph["nodes"] if node["type"] == "source"
    }:
        raise ValueError("Source registry and source nodes do not match")

    relationship_keys: set[tuple[str, str, str, str | None]] = set()
    for relationship in graph["relationships"]:
        if relationship["source"] not in node_id_set:
            raise ValueError("Relationship source does not resolve: " + relationship["source"])
        if relationship["target"] not in node_id_set:
            raise ValueError("Relationship target does not resolve: " + relationship["target"])
        for source_id in relationship["evidence_source_ids"]:
            if source_id not in source_ids:
                raise ValueError("Relationship evidence source does not resolve: " + source_id)
        attributes = relationship.get("attributes", {})
        key = (
            relationship["source"],
            relationship["relation"],
            relationship["target"],
            attributes.get("pin_number"),
        )
        if key in relationship_keys:
            raise ValueError("Duplicate relationship: " + repr(key))
        relationship_keys.add(key)

    instances = {node["id"] for node in graph["nodes"] if node["type"] == "part_instance"}
    parts = {node["id"] for node in graph["nodes"] if node["type"] == "part"}
    pins = {node["id"] for node in graph["nodes"] if node["type"] == "pin"}
    nets = {node["id"] for node in graph["nodes"] if node["type"] == "net"}
    instance_of = {
        relationship["source"]
        for relationship in graph["relationships"]
        if relationship["relation"] == "instance_of"
        and relationship["target"] in parts
    }
    contained_instances = {
        relationship["target"]
        for relationship in graph["relationships"]
        if relationship["relation"] == "contains_instance"
    }
    if instance_of != instances or contained_instances != instances:
        raise ValueError("Every instance must resolve to a part and exactly one subsystem")

    pin_owners = {
        relationship["target"]
        for relationship in graph["relationships"]
        if relationship["relation"] == "has_pin"
    }
    if pin_owners != pins:
        raise ValueError("Every mock pin must resolve to an instance")

    connected = {
        relationship["source"]
        for relationship in graph["relationships"]
        if relationship["relation"] == "connected_to_net"
        and relationship["target"] in nets
    }
    intentionally_open = {
        relationship["source"]
        for relationship in graph["relationships"]
        if relationship["relation"] == "intentionally_no_connect"
    }
    if connected & intentionally_open:
        raise ValueError("A pin cannot be both connected and intentionally unconnected")
    if connected | intentionally_open != pins:
        raise ValueError("Every mock pin must be connected or explicitly no-connect")

    coverage = graph["coverage"]
    if coverage["unconnected_mock_symbol_pins"] != len(pins) - len(connected) - len(intentionally_open):
        raise ValueError("Pin coverage counts are inconsistent")
    if coverage["design_part_instances"] != len(instances):
        raise ValueError("Design part instance coverage count is inconsistent")
    if coverage["unique_design_parts"] != len(parts):
        raise ValueError("Unique design part coverage count is inconsistent")
    if coverage["nets"] != len(nets):
        raise ValueError("Net coverage count is inconsistent")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Regenerate data/relationships.json")
    mode.add_argument("--check", action="store_true", help="Validate generated data is current")
    args = parser.parse_args()

    graph = build_graph()
    serialized = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    if args.write:
        OUTPUT_PATH.write_text(serialized, encoding="utf-8")
        print("Wrote", OUTPUT_PATH.relative_to(ROOT))
    else:
        if not OUTPUT_PATH.exists():
            raise SystemExit("Missing generated graph: " + str(OUTPUT_PATH))
        if OUTPUT_PATH.read_text(encoding="utf-8") != serialized:
            raise SystemExit("Graph is stale; run python -m cvhack.knowledge_graph --write")
        print("Graph is current and valid:", graph["coverage"])


if __name__ == "__main__":
    main()
