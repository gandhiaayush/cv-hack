from cvhack.mocks import designs
from cvhack.parts import load_parts
from cvhack.tooling.rules import RuleVerifier

PARTS = load_parts()


def statuses(rows):
    return {r["rule_id"]: r["status"] for r in rows}


def test_wearable_passes_all_b_rules():
    res = RuleVerifier().verify(designs.wearable(), PARTS)
    assert res.ok and res.details["counts"]["pass"] == len(res.details["rules"]) > 0


def test_esp32_draft_fails_rules_and_missing_ref_is_indeterminate():
    d = designs.wearable()
    d.blocks = [b for b in d.blocks if b.subsystem != "mcu_comms"] + [designs.esp32_mcu_block()]
    res = RuleVerifier().verify(d, PARTS)
    st = statuses(res.details["rules"])
    assert not res.ok
    assert st["DR-DEMO-PWR-003"] == "fail" and st["DR-DEMO-MCU-001"] == "indeterminate"
    assert any(e.code == "RULE_INDETERMINATE" and e.rule_id == "DR-DEMO-MCU-001" for e in res.errors)


def test_wrong_value_is_caught():
    d = designs.swap_mpn(designs.wearable(), "RC0402FR-074K7L", "RC0402FR-0710KL")  # I2C pull-ups -> 10k
    st = statuses(RuleVerifier().evaluate(d, PARTS))
    assert st["DR-DEMO-I2C-001"] == "fail"


def test_unknown_operator_never_passes():
    rs = {"rule_set_id": "t", "operator_catalog": {"pin_on_net": {}},
          "rules": [{"id": "X", "applies_to": "design", "check": {"operator": "eval", "expr": "True"}}]}
    res = RuleVerifier(rs).verify(designs.wearable(), PARTS)
    assert not res.ok and res.details["rules"][0]["status"] == "indeterminate"


def test_other_design_is_not_applicable_with_warning():
    res = RuleVerifier().verify(designs.broken(), PARTS)
    assert res.ok and {e.code for e in res.errors} == {"RULES_NOT_APPLICABLE"}
