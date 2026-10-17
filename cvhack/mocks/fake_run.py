"""Scripted stand-in for A's agent system, so the dashboard and demo work
before the real agents exist. Also a reference for A: this is exactly how to
emit events and poll the control channel.

    python -m cvhack.mocks.fake_run [--run-id demo] [--speed 2] [--linger 600]

The electrical verifier here is the real one, so failures, replans and
passes come from actual checks, not canned results.
"""

from __future__ import annotations

import argparse
import time
import uuid

from cvhack.contracts import Design, Part, VerifierResult
from cvhack.control import read_commands
from cvhack.events import emit
from cvhack.mocks import designs
from cvhack.parts import load_parts
from cvhack.tooling.electrical import ElectricalVerifier

REQUIREMENTS = [
    ("REQ-01", "USB-C 5V charging input with overcurrent protection", ["J1", "F1"], ["R-ELEC-004"]),
    ("REQ-02", "LiPo charging at <= 500 mA", ["U1", "R3"], ["R-BATT-001"]),
    ("REQ-03", "Regulated 3.3 V and 1.8 V rails", ["U2", "U3"], []),
    ("REQ-04", "SpO2 / heart-rate optical sensing", ["U4"], ["R-BIO-002"]),
    ("REQ-05", "BLE connectivity with certified module", ["U5"], ["R-RF-001"]),
    ("REQ-06", "Average power <= 30 mW", [], []),
    ("REQ-07", "BOM cost <= $15", [], []),
    ("REQ-08", "ESD protection on external connector (IEC 61000-4-2)", ["D1"], ["R-EMC-003"]),
    ("REQ-09", "Schematic passes ERC", [], []),
]


class FakeRun:
    def __init__(self, run_id: str, speed: float):
        self.run_id = run_id
        self.speed = speed
        self.parts: dict[str, Part] = {k: v.model_copy() for k, v in load_parts().items()}
        self.verifier = ElectricalVerifier()
        self.ctrl_offset = 0
        self.design: Design | None = None

    # -- helpers ----------------------------------------------------------------
    def ev(self, agent_id: str, kind: str, parent: str | None = "mother", **payload):
        emit(run_id=self.run_id, agent_id=agent_id, parent_id=parent, kind=kind, payload=payload)

    def wait(self, s: float):
        end = time.time() + s / self.speed
        while time.time() < end:
            self.poll_control()
            time.sleep(0.1)

    def spawn(self, agent_id: str, role: str, label: str, parent: str | None = "mother"):
        self.ev(agent_id, "agent_spawned", parent, role=role, label=label)
        self.ev(agent_id, "agent_status", parent, status="running")

    def status(self, agent_id: str, status: str, parent: str | None = "mother"):
        self.ev(agent_id, "agent_status", parent, status=status)

    def req(self, req_id: str, status: str):
        r = next(r for r in REQUIREMENTS if r[0] == req_id)
        self.ev("mother", "requirement_status", None, req_id=r[0], text=r[1], status=status,
                parts=r[2], rule_ids=r[3])

    def publish(self, design: Design):
        self.design = design
        self.ev("mother", "design_updated", None, design=design.model_dump())

    def electrical(self, design: Design) -> VerifierResult:
        self.status("verifier.electrical", "running")
        res = self.verifier.verify(design, self.parts)
        self.ev("verifier.electrical", "verifier_result", "mother", **res.model_dump())
        self.status("verifier.electrical", "done" if res.ok else "failed")
        return res

    def compliance_ok(self, extra: str = ""):
        for vid in ("verifier.compliance", "verifier.red_team"):
            self.status(vid, "running")
            self.wait(0.6)
            self.ev(vid, "verifier_result", "mother", verifier=vid.split(".")[1], ok=True, errors=[],
                    details={"note": extra or "all applicable rules satisfied"})
            self.status(vid, "done")

    # -- control channel (A: copy this loop into the mother agent) ----------------
    def poll_control(self):
        cmds, self.ctrl_offset = read_commands(self.run_id, self.ctrl_offset)
        for cmd in cmds:
            self.ev("mother", "constraint_change", None, **cmd.payload)
            self.pending.append(cmd.payload)

    # -- script ------------------------------------------------------------------
    def run(self, linger: float):
        self.pending: list[dict] = []
        self.spawn("mother", "mother", "Mother agent", parent=None)
        self.ev("mother", "step", None, message="Parsing spec: battery-powered Class II PPG wearable")
        for r in REQUIREMENTS:
            self.req(r[0], "pending")
        self.wait(1.5)
        self.ev("mother", "step", None, message="Decomposed into 4 subsystems; budgets 30 mW / $15 / 700 mm²")

        w = designs.wearable()
        blocks = {b.subsystem: b for b in w.blocks}
        agents = [("protection", "Protection agent"), ("power", "Power agent"),
                  ("analog_fe", "Analog FE agent"), ("mcu_comms", "MCU/comms agent")]
        for aid, label in agents:
            self.spawn(aid, "subsystem", label)
            self.wait(0.4)
        for vid, label in [("verifier.electrical", "Electrical verifier"),
                           ("verifier.compliance", "Compliance verifier"),
                           ("verifier.red_team", "Red-team verifier")]:
            self.ev(vid, "agent_spawned", "mother", role="verifier", label=label)
            self.status(vid, "waiting")

        drafted: list = []
        msgs = {
            "protection": "Picked USB-C receptacle, 500 mA PTC, SMF5.0A TVS",
            "power": "Picked MCP73831 charger, AP2112K 3.3 V LDO, TLV70018 1.8 V LDO",
            "analog_fe": "Picked MAX30102 PPG sensor on I2C",
            "mcu_comms": "Picked ESP32-C3-MINI-1 (WiFi+BLE)",
        }
        for aid, _ in agents:
            self.ev(aid, "step", "mother", message=msgs[aid])
            self.wait(1.2)
            drafted.append(designs.esp32_mcu_block() if aid == "mcu_comms" else blocks[aid])
            self.ev(aid, "step", "mother", message="Drafted schematic block")
            self.status(aid, "waiting")
            self.publish(Design(name=w.name, blocks=list(drafted), budgets=w.budgets,
                                no_connects=w.no_connects + designs._nodes("U5.26 U5.27"),
                                power_flags=w.power_flags))

        self.ev("mother", "step", None, message="All blocks drafted; running verifiers")
        res = self.electrical(self.design)
        self.wait(1)
        if not res.ok:
            codes = sorted({e.code for e in res.errors})
            self.ev("mother", "replan", None, reason=f"electrical gate failed: {', '.join(codes)}",
                    changes=["MCU/comms: ESP32-C3 draws ~85 mW; swap to nRF52832 module (MDBT42Q)"])
            self.status("mcu_comms", "running")
            self.ev("mcu_comms", "step", "mother", message="Swapped to MDBT42Q-512KV2 (nRF52832, ~9 mW)")
            self.wait(1.5)
            self.publish(w)
            self.status("mcu_comms", "waiting")
            res = self.electrical(self.design)
        self.compliance_ok()
        if res.ok:
            for r in REQUIREMENTS:
                self.req(r[0], "pass")
                self.wait(0.3)
            for aid, _ in agents:
                self.status(aid, "done")
            self.ev("mother", "run_done", None, ok=True, summary="Design verified; outputs written")
            self.status("mother", "done", parent=None)

        # Stay alive to react to constraint changes from the dashboard.
        end = time.time() + linger
        while time.time() < end:
            self.wait(0.5)
            while self.pending:
                self.handle(self.pending.pop(0))

    def handle(self, change: dict):
        self.status("mother", "running", parent=None)
        if change.get("type") == "out_of_stock":
            mpn = change.get("mpn", "AP2112K-3.3TRG1")
            if mpn in self.parts:
                self.parts[mpn].stock = 0
            res = self.electrical(self.design)
            self.req("REQ-03", "fail")
            alt = {"AP2112K-3.3TRG1": "TPS7A0233PDBVR"}.get(mpn)
            if not res.ok and alt:
                self.ev("mother", "replan", None, reason=f"{mpn} out of stock",
                        changes=[f"Power: swap {mpn} -> {alt} (same SOT-23-5 pinout, lower Iq)"])
                self.status("power", "running")
                self.wait(1.2)
                self.ev("power", "step", "mother", message=f"Swapped {mpn} -> {alt}")
                self.publish(designs.swap_mpn(self.design, mpn, alt))
                self.status("power", "done")
                res = self.electrical(self.design)
                if res.ok:
                    self.req("REQ-03", "pass")
        elif change.get("type") == "device_class":
            self.req("REQ-08", "fail")
            self.ev("mother", "replan", None, reason=f"device class raised to {change.get('value')}",
                    changes=["Re-run compliance with stricter rule set", "Protection: confirm TVS rating"])
            self.compliance_ok(extra=f"class {change.get('value')} rules satisfied")
            self.req("REQ-08", "pass")
        self.ev("mother", "run_done", None, ok=True, summary="Re-plan complete")
        self.status("mother", "done", parent=None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--linger", type=float, default=600, help="seconds to keep listening for constraint changes")
    a = ap.parse_args()
    run_id = a.run_id or f"fake-{uuid.uuid4().hex[:6]}"
    print("run_id:", run_id, flush=True)
    FakeRun(run_id, a.speed).run(a.linger)


if __name__ == "__main__":
    main()
