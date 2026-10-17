"""Deterministic evaluator for B's design rule set (data/rules.json).

    from cvhack.tooling.rules import RuleVerifier
    result = RuleVerifier().verify(design, parts)

Only the operators in the rule set's operator_catalog are interpreted; check
strings are never eval'd. Following B's execution policy, a missing ref, an
unknown MPN or an unsupported operator is "indeterminate", which fails the
rule. It is never an implicit pass. Rules whose applies_to names a different
design are reported as not_applicable and don't affect `ok`.

These are project-defined prototype checks, not regulatory clauses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cvhack.contracts import Design, Part, VerifierError, VerifierResult

RULES_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "rules.json"


class Indeterminate(Exception):
    pass


def load_rule_set(path: Path | str = RULES_PATH) -> dict[str, Any]:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {"rules": [], "operator_catalog": {}}


class _Ctx:
    def __init__(self, design: Design, parts: dict[str, Part]):
        self.mpn = {i.ref: i.mpn for i in design.all_parts()}
        self.nets = {name: {(n.ref, n.pin) for n in nodes} for name, nodes in design.merged_nets().items()}
        self.parts = parts

    def need_ref(self, ref: str) -> str:
        if ref not in self.mpn:
            raise Indeterminate(f"{ref} is not placed in the design")
        return self.mpn[ref]


def _eval(check: dict[str, Any], ctx: _Ctx, allowed: set[str]) -> list[str]:
    """Return a list of failure messages (empty = pass). Raises Indeterminate."""
    op = check.get("operator")
    if op not in allowed:
        raise Indeterminate(f"operator {op!r} is not in the operator catalog")
    if op == "all_of":
        fails: list[str] = []
        for c in check.get("checks", []):
            fails += _eval(c, ctx, allowed)
        return fails
    if op == "instance_has_mpn":
        got = ctx.need_ref(check["ref"])
        return [] if got == check["mpn"] else [f"{check['ref']} is {got}, expected {check['mpn']}"]
    if op == "pin_on_net":
        ctx.need_ref(check["ref"])
        net = check["net"]
        if (check["ref"], check["pin"]) in ctx.nets.get(net, set()):
            return []
        where = [n for n, nodes in ctx.nets.items() if (check["ref"], check["pin"]) in nodes]
        return [f"{check['ref']}.{check['pin']} not on {net}" + (f" (on {where[0]})" if where else " (unconnected)")]
    if op == "part_value_is":
        mpn = ctx.need_ref(check["ref"])
        part = ctx.parts.get(mpn)
        if part is None:
            raise Indeterminate(f"{check['ref']}: part {mpn} not in parts DB")
        return [] if part.value == check["value"] else [
            f"{check['ref']} value is {part.value or '(none)'}, expected {check['value']}"]
    raise Indeterminate(f"operator {op!r} has no evaluator")


def _refs(rule: dict[str, Any]) -> list[str]:
    return [t["ref"] for t in rule.get("targets", []) if t.get("type") == "part_instance"]


class RuleVerifier:
    name = "design_rules"

    def __init__(self, rule_set: dict[str, Any] | None = None):
        self.rule_set = rule_set if rule_set is not None else load_rule_set()

    def evaluate(self, design: Design, parts: dict[str, Part]) -> list[dict[str, Any]]:
        """One row per rule: {rule_id, title, status, refs, messages, severity, failure_code}."""
        allowed = set(self.rule_set.get("operator_catalog", {}))
        ctx = _Ctx(design, parts)
        design_id = f"design:{design.name}"
        rows = []
        for rule in self.rule_set.get("rules", []):
            row = {"rule_id": rule["id"], "title": rule.get("title", rule["id"]), "refs": _refs(rule),
                   "severity": rule.get("severity", "error"), "failure_code": rule.get("failure_code", "RULE_FAILED"),
                   "category": rule.get("category", ""), "messages": []}
            applies = rule.get("applies_to", "design")
            if applies.startswith("design:") and applies != design_id:
                row["status"] = "not_applicable"
            else:
                try:
                    row["messages"] = _eval(rule["check"], ctx, allowed)
                    row["status"] = "fail" if row["messages"] else "pass"
                except Indeterminate as exc:
                    row["status"], row["messages"] = "indeterminate", [str(exc)]
            rows.append(row)
        return rows

    def verify(self, design: Design, parts: dict[str, Part]) -> VerifierResult:
        rows = self.evaluate(design, parts)
        errors = []
        for r in rows:
            if r["status"] in ("fail", "indeterminate"):
                code = r["failure_code"] if r["status"] == "fail" else "RULE_INDETERMINATE"
                errors.append(VerifierError(
                    code=code, rule_id=r["rule_id"], part=r["refs"][0] if r["refs"] else None,
                    severity="error" if r["status"] == "indeterminate" else r["severity"],
                    message=f"{r['rule_id']} {r['title']}: {'; '.join(r['messages'][:3])}"))
        if rows and all(r["status"] == "not_applicable" for r in rows):
            errors.append(VerifierError(code="RULES_NOT_APPLICABLE", severity="warning",
                                        message=f"no rule in {self.rule_set.get('rule_set_id')} applies to design:{design.name}"))
        ok = not any(e.severity == "error" for e in errors)
        counts = {s: sum(r["status"] == s for r in rows) for s in ("pass", "fail", "indeterminate", "not_applicable")}
        return VerifierResult(verifier=self.name, ok=ok, errors=errors,
                              details={"rule_set_id": self.rule_set.get("rule_set_id"), "counts": counts, "rules": rows})
