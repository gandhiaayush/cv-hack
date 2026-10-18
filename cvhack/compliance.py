"""Rule evaluation and red-team checks for the PPG mock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cvhack.contracts import Design, Part, VerifierError, VerifierResult

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "data" / "rules.json"


def _load_rule_set(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path is not None else RULES_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def _net_contains_ref_pin(design: Design, net_name: str, ref: str, pin: str) -> bool:
    for block in design.blocks:
        for net in block.nets:
            if net.name != net_name:
                continue
            if any(node.ref == ref and node.pin == pin for node in net.nodes):
                return True
    return False


def _evaluate_check(
    check: dict[str, Any],
    design: Design,
    parts: dict[str, Part],
) -> tuple[bool, str | None, str | None, str | None]:
    op = check["operator"]
    if op == "all_of":
        details: list[str] = []
        last_part: str | None = None
        last_net: str | None = None
        for child in check["checks"]:
            ok, detail, part_ref, net_name = _evaluate_check(child, design, parts)
            if not ok:
                if detail:
                    details.append(detail)
                last_part = part_ref
                last_net = net_name
        if details:
            return False, "; ".join(details), last_part, last_net
        return True, None, None, None

    ref = check["ref"]
    if op == "instance_has_mpn":
        inst = next((i for block in design.blocks for i in block.parts if i.ref == ref), None)
        if inst is None:
            return False, f"reference {ref} is not in the design", ref, None
        return inst.mpn == check["mpn"], (
            None if inst.mpn == check["mpn"] else f"{ref} is {inst.mpn}, expected {check['mpn']}"
        ), ref, None

    if op == "part_value_is":
        inst = next((i for block in design.blocks for i in block.parts if i.ref == ref), None)
        if inst is None:
            return False, f"reference {ref} is not in the design", ref, None
        part = parts.get(inst.mpn)
        actual = part.value if part is not None else ""
        return actual == check["value"], (
            None if actual == check["value"] else f"{ref} value is {actual!r}, expected {check['value']!r}"
        ), ref, None

    if op == "pin_on_net":
        if not _net_contains_ref_pin(design, check["net"], ref, check["pin"]):
            return False, f"{ref}.{check['pin']} is not on {check['net']}", ref, check["net"]
        return True, None, ref, check["net"]

    raise ValueError(f"Unsupported rule operator: {op}")


def evaluate_rules(design: Design, parts: dict[str, Part], rule_set: dict[str, Any] | None = None) -> VerifierResult:
    rs = rule_set or _load_rule_set()
    errors: list[VerifierError] = []
    rules_total = 0
    rules_passed = 0

    for rule in rs.get("rules", []):
        rules_total += 1
        ok, detail, part_ref, net_name = _evaluate_check(rule["check"], design, parts)
        if ok:
            rules_passed += 1
            continue
        errors.append(
            VerifierError(
                code=rule.get("failure_code", "RULE_VIOLATION"),
                message=f"{rule['id']} failed: {detail or 'rule failed'}",
                part=part_ref,
                net=net_name,
                rule_id=rule["id"],
                severity=rule.get("severity", "error"),
            )
        )

    return VerifierResult(
        verifier="compliance",
        ok=not any(e.severity == "error" for e in errors),
        errors=errors,
        details={
            "rule_set_id": rs.get("rule_set_id"),
            "rules_total": rules_total,
            "rules_passed": rules_passed,
            "rules_failed": len(errors),
        },
    )


class ComplianceVerifier:
    name = "compliance"

    def verify(self, design: Design, parts: dict[str, Part]) -> VerifierResult:
        return evaluate_rules(design, parts)


class RedTeamVerifier:
    name = "red_team"

    def verify(self, design: Design, parts: dict[str, Part]) -> VerifierResult:
        errors: list[VerifierError] = []
        for block in design.blocks:
            for inst in block.parts:
                part = parts.get(inst.mpn)
                if part is None:
                    errors.append(
                        VerifierError(
                            code="REDTEAM_PART_UNKNOWN",
                            message=f"{inst.ref} references an unknown MPN {inst.mpn}",
                            part=inst.ref,
                            severity="error",
                        )
                    )
                    continue

                ratings = getattr(part, "ratings", {}) or {}
                if not ratings.get("sources"):
                    errors.append(
                        VerifierError(
                            code="REDTEAM_EVIDENCE_MISSING",
                            message=f"{inst.mpn} has no source-backed evidence trail in its part metadata",
                            part=inst.ref,
                            severity="warning",
                        )
                    )

                if not part.value:
                    errors.append(
                        VerifierError(
                            code="REDTEAM_VALUE_UNVERIFIED",
                            message=f"{inst.mpn} has no verified value string",
                            part=inst.ref,
                            severity="warning",
                        )
                    )

        return VerifierResult(
            verifier=self.name,
            ok=not any(e.severity == "error" for e in errors),
            errors=errors,
            details={"design_name": design.name, "evidence_gaps": len(errors)},
        )
