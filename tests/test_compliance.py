from cvhack.compliance import ComplianceVerifier, RedTeamVerifier, evaluate_rules
from cvhack.mocks import designs
from cvhack.parts import load_parts

PARTS = load_parts()


def test_compliance_verifier_passes_current_mock():
    res = ComplianceVerifier().verify(designs.wearable(), PARTS)
    assert res.ok, [e.model_dump() for e in res.errors]


def test_compliance_verifier_detects_violations():
    d = designs.wearable()
    for block in d.blocks:
        for net in block.nets:
            if net.name == "CC1":
                net.nodes = [node for node in net.nodes if not (node.ref == "R1" and node.pin == "1")]
                break
    res = evaluate_rules(d, PARTS)
    assert not res.ok
    assert any(e.rule_id == "DR-DEMO-USB-001" for e in res.errors)


def test_red_team_flags_missing_evidence():
    d = designs.wearable()
    res = RedTeamVerifier().verify(d, PARTS)
    assert any(e.code == "REDTEAM_EVIDENCE_MISSING" for e in res.errors)
