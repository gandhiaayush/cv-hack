"""Shared data contracts. Every team member imports from here.

Change a model only after telling the team: these are the seams between
A (agents), B (data and rules) and C (tooling and demo).
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Parts and rules (B publishes, A and C consume)
# ---------------------------------------------------------------------------

PinType = Literal[
    "input", "output", "bidirectional", "passive", "power_in", "power_out",
    "open_collector", "tri_state", "no_connect", "unspecified",
]


class Pin(BaseModel):
    num: str
    name: str
    type: PinType = "passive"


class Part(BaseModel):
    mpn: str                       # manufacturer part number, the primary key
    category: str                  # e.g. "ldo", "mcu", "resistor", "tvs"
    description: str = ""
    value: str = ""                # e.g. "10k", "4.7uF"; empty for ICs
    footprint: str = ""            # KiCad footprint, e.g. "Package_TO_SOT_SMD:SOT-23-5"
    symbol_lib: str = "Device"     # KiCad symbol library
    symbol_name: str = ""          # KiCad symbol name
    pins: list[Pin] = Field(default_factory=list)
    price_usd: float | None = None  # None = unknown (B never invents a price)
    stock: int | None = None       # None = unknown; 0 = out of stock
    power_mw: float = 0.0          # typical active consumption
    area_mm2: float = 0.0          # footprint area incl. courtyard
    ratings: dict[str, Any] = Field(default_factory=dict)  # e.g. {"vin_max": 6.0, "iso_10993": true}


class Rule(BaseModel):
    id: str                        # e.g. "R-ELEC-001"
    clause: str                    # e.g. "IEC 60601-1 8.7.3"
    applies_to: str                # part category, subsystem, or "design"
    check: str                     # machine-checkable expression or check name
    description: str = ""


# ---------------------------------------------------------------------------
# Design (A produces, C turns into KiCad)
# ---------------------------------------------------------------------------

class PartInstance(BaseModel):
    ref: str                       # reference designator, e.g. "U1", "R3"
    mpn: str


class Node(BaseModel):
    ref: str
    pin: str                       # pin number (Pin.num)


class Net(BaseModel):
    name: str                      # global across blocks: same name == same net
    nodes: list[Node]


class SchematicBlock(BaseModel):
    subsystem: str                 # "power" | "analog_fe" | "mcu_comms" | "protection" | ...
    parts: list[PartInstance]
    nets: list[Net]
    ports: list[str] = Field(default_factory=list)  # net names shared with other blocks


class Budgets(BaseModel):
    power_mw: float
    cost_usd: float
    area_mm2: float


class Design(BaseModel):
    name: str
    blocks: list[SchematicBlock]
    budgets: Budgets
    no_connects: list[Node] = Field(default_factory=list)  # pins intentionally left open
    # Nets powered from off-board (USB VBUS, battery, GND), like KiCad's PWR_FLAG.
    power_flags: list[str] = Field(default_factory=list)
    board: dict[str, Any] | None = None    # custom size/options: {"width_mm", "height_mm", "holes": true}
    outline: dict[str, Any] | None = None  # custom board shape: {"kind": "capsule", "corner_radius_mm": 3}

    def all_parts(self) -> list[PartInstance]:
        return [p for b in self.blocks for p in b.parts]

    def merged_nets(self) -> dict[str, list[Node]]:
        """Nets merged across blocks by name, which is how the netlist sees them."""
        nets: dict[str, list[Node]] = {}
        for b in self.blocks:
            for n in b.nets:
                nets.setdefault(n.name, []).extend(n.nodes)
        return nets


# ---------------------------------------------------------------------------
# Verifiers (all three teams)
# ---------------------------------------------------------------------------

class VerifierError(BaseModel):
    code: str                      # e.g. "ERC_UNCONNECTED", "BUDGET_POWER", "RULE_VIOLATION"
    message: str
    part: str | None = None        # ref designator or mpn
    net: str | None = None
    rule_id: str | None = None
    severity: Literal["error", "warning"] = "error"


class VerifierResult(BaseModel):
    verifier: str                  # "electrical" | "compliance" | "red_team"
    ok: bool
    errors: list[VerifierError] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)  # free-form, e.g. budget totals


class Verifier(Protocol):
    name: str

    def verify(self, design: Design, parts: dict[str, Part]) -> VerifierResult: ...


# ---------------------------------------------------------------------------
# Agent events (A emits, C renders) and control (C sends, A obeys)
# ---------------------------------------------------------------------------

EventKind = Literal[
    "agent_spawned",       # payload: {role, label}
    "agent_status",        # payload: {status: "running"|"waiting"|"done"|"failed"}
    "step",                # payload: {message, step_no?, tokens?}
    # Negotiation loop (see README). agent_id is the sender; `to` is the receiver.
    "delegate",            # mother/parent -> child. payload: {to, subsystem, budget:{...}, constraints:[str]}
    "report",              # child -> parent. payload: {to, options:[{name, cost?, power_mw?, notes?}],
                           #   conflicts:[str], cannot_meet:[str], needs:[str]}
    "decision",            # parent -> child. payload: {to, chosen?, budget_changes?:{...},
                           #   extra_constraints?:[str], spawn?:[str], message?}
    "verifier_result",     # payload: VerifierResult.model_dump() + {target_subsystem?}
    "replan",              # payload: {reason, changes:[str]}
    "requirement_status",  # payload: {req_id, text, status: "pending"|"pass"|"fail", parts:[ref], rule_ids:[id]}
    "design_updated",      # payload: {design: Design.model_dump()}
    "constraint_change",   # payload: ControlCommand.payload (echoed by the mother agent when applied)
    "run_done",            # payload: {ok, summary}
]


class AgentEvent(BaseModel):
    run_id: str
    agent_id: str                  # e.g. "mother", "power", "verifier.electrical"
    parent_id: str | None = None   # tree edge; None only for the root agent
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class ControlCommand(BaseModel):
    kind: Literal["constraint_change"] = "constraint_change"
    # e.g. {"type": "out_of_stock", "mpn": "TPS7A02"} or {"type": "device_class", "value": "III"}
    payload: dict[str, Any]
    ts: float = Field(default_factory=time.time)
