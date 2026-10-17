"""Fold an event stream into the current run state (agent tree, requirements,
latest design, verifier results). Shared by the dashboard and outputs.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cvhack.contracts import AgentEvent, Design, VerifierResult


@dataclass
class RunState:
    run_id: str
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    requirements: dict[str, dict[str, Any]] = field(default_factory=dict)
    verifiers: dict[str, dict[str, Any]] = field(default_factory=dict)  # latest result per verifier
    replans: list[dict[str, Any]] = field(default_factory=list)
    negotiation: list[dict[str, Any]] = field(default_factory=list)
    constraint_changes: list[dict[str, Any]] = field(default_factory=list)
    design: dict[str, Any] | None = None
    design_version: int = 0
    done: dict[str, Any] | None = None
    started: float | None = None
    updated: float | None = None
    n_events: int = 0
    inference: dict[str, dict[str, Any]] = field(default_factory=dict)   # per model: calls, tokens, latency
    design_source: str | None = None     # "reference" | "selection" | "final" (real runs, via cvhack.synth)
    design_notes: dict[str, Any] | None = None

    def apply(self, e: AgentEvent) -> None:
        self.n_events += 1
        self.started = self.started or e.ts
        self.updated = e.ts
        a = self.agents.setdefault(e.agent_id, {"id": e.agent_id, "parent": e.parent_id, "role": "",
                                                 "label": e.agent_id, "status": "waiting",
                                                 "last": "", "steps": 0})
        if e.parent_id and not a["parent"]:
            a["parent"] = e.parent_id
        p = e.payload
        if e.kind == "agent_spawned":
            a.update(role=p.get("role", ""), label=p.get("label", e.agent_id))
        elif e.kind == "agent_status":
            a["status"] = p.get("status", a["status"])
        elif e.kind == "step":
            a["last"] = p.get("message", "")
            a["steps"] += 1
        elif e.kind in ("delegate", "report", "decision"):
            self.negotiation.append({"kind": e.kind, "from": e.agent_id, "to": p.get("to"), **p, "ts": e.ts})
            a["last"] = {"delegate": "delegated to ", "report": "reported to ",
                         "decision": "decided for "}[e.kind] + str(p.get("to", ""))
        elif e.kind == "verifier_result":
            self.verifiers[p.get("verifier", e.agent_id)] = {**p, "ts": e.ts}
            a["last"] = "pass" if p.get("ok") else f"{len(p.get('errors', []))} error(s)"
        elif e.kind == "requirement_status":
            self.requirements[p["req_id"]] = {**self.requirements.get(p["req_id"], {}), **p, "ts": e.ts}
        elif e.kind == "design_updated":
            self.design = p.get("design")
            self.design_version += 1
            self.design_source = p.get("source")
            self.design_notes = p.get("notes")
        elif e.kind == "replan":
            self.replans.append({**p, "ts": e.ts})
            self.done = None
        elif e.kind == "constraint_change":
            self.constraint_changes.append({**p, "ts": e.ts})
            self.done = None
        elif e.kind == "run_done":
            self.done = {**p, "ts": e.ts}
        elif e.kind == "inference":
            m = self.inference.setdefault(p.get("model", "?"), {"model": p.get("model", "?"), "provider": p.get("provider", ""),
                                                                 "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                                                 "latency_s": 0.0, "agents": []})
            m["calls"] += 1
            m["prompt_tokens"] += p.get("prompt_tokens") or 0
            m["completion_tokens"] += p.get("completion_tokens") or 0
            m["latency_s"] = round(m["latency_s"] + (p.get("latency_s") or 0.0), 3)
            role = e.agent_id.split(":")[0]
            if role not in m["agents"]:
                m["agents"].append(role)
            a["calls"] = a.get("calls", 0) + 1
            a["model"] = p.get("model")

    def design_obj(self) -> Design | None:
        return Design.model_validate(self.design) if self.design else None

    def failing_refs(self) -> set[str]:
        """Refs named in the latest failing verifier results (for highlighting)."""
        refs: set[str] = set()
        for v in self.verifiers.values():
            if not v.get("ok"):
                refs.update(e["part"] for e in v.get("errors", []) if e.get("part"))
        return refs

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "agents": list(self.agents.values()),
            "requirements": sorted(self.requirements.values(), key=lambda r: r["req_id"]),
            "verifiers": self.verifiers, "replans": self.replans, "negotiation": self.negotiation[-50:],
            "constraint_changes": self.constraint_changes, "design": self.design,
            "design_version": self.design_version, "done": self.done,
            "started": self.started, "updated": self.updated, "n_events": self.n_events,
            "inference": sorted(self.inference.values(), key=lambda m: -m["calls"]),
            "design_source": self.design_source, "design_notes": self.design_notes,
        }


def fold(run_id: str, events: list[AgentEvent]) -> RunState:
    s = RunState(run_id)
    for e in events:
        s.apply(e)
    return s


def latest_result(state: RunState, verifier: str) -> VerifierResult | None:
    v = state.verifiers.get(verifier)
    return VerifierResult.model_validate({k: x for k, x in v.items() if k != "ts"}) if v else None
