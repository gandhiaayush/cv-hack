"""Bridge A's SQLite run log (src/store.py) into the dashboard event stream.

    python -m cvhack.bridge                 # follow runs/run.db, emit into runs/<run>/events.jsonl
    python -m cvhack.bridge --once          # translate what's there and exit
    python -m cvhack.bridge --run ab12cd    # only one run

A's agents log rows (run, agent, parent, kind, body) with kind in
start | tool_call | tool_result | final. This turns them into AgentEvents:

    start (root)      -> agent_spawned(mother) + agent_status running
    start (child)     -> agent_spawned(subsystem) + running, and a `delegate` from the parent
    tool_call         -> step "tool(args)"            (delegate calls are skipped; the child's start covers them)
    tool_result       -> step "tool -> result"
    final (child)     -> `report` to the parent (parsed from the JSON report) + done
    final (root)      -> step with the decision text + done + run_done
    final "STEP LIMIT"-> agent_status failed

If A later calls cvhack.events.emit directly, stop running this.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cvhack.contracts import AgentEvent
from cvhack.events import RUNS_DIR, emit
from cvhack.mocks import designs
from cvhack.parts import load_parts
from cvhack.synth import apply_selection

DEFAULT_DB = RUNS_DIR / "run.db"


@dataclass
class Row:
    id: int
    ts: float
    run: str
    agent: str
    parent: str | None
    kind: str
    body: Any


def _short(x: Any, n: int = 140) -> str:
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_phrase(tool: str, args: dict) -> str:
    """One short line per tool call, e.g. 'searched parts: tvs' instead of the raw arguments."""
    a = args if isinstance(args, dict) else {}
    if tool == "search_parts":
        what = " ".join(str(x) for x in (a.get("category"), a.get("text"), a.get("interface")) if x)
        return f"searched parts: {what or 'all'}"
    if tool == "find_alternates":
        return f"looked for alternates to {a.get('mpn', '?')}"
    if tool == "run_python":
        first = next((l.strip("# ").strip() for l in str(a.get("code", "")).splitlines() if l.strip().startswith("#")), "")
        return "calculated" + (f": {first[:60]}" if first else "")
    if tool == "get_subsystem_context":
        return f"read {a.get('subsystem', '')} context"
    if tool == "list_subsystems":
        return "listed subsystems"
    if tool in ("search_regulations", "get_regulation_section", "classify_device"):
        return f"checked regulations: {a.get('query') or a.get('section') or a.get('device_name') or ''}".strip()
    if tool == "submit_report":
        n = a.get("options")
        n = len(n) if isinstance(n, list) else "?"
        return f"submitted {n} option(s)"
    return tool.replace("_", " ")


def parse_report(text: str) -> dict | None:
    """Pull the JSON object out of a sub-agent's final message (may be fenced or wrapped in prose)."""
    if not isinstance(text, str):
        return text if isinstance(text, dict) else None
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    cands = [m.group(1)] if m else []
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        cands.append(text[i : j + 1])
    for c in cands:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return None


def report_payload(rep: dict, to: str) -> dict:
    """A's sub-agent report schema -> the `report` event payload in docs/interfaces.md."""
    opts = []
    for o in rep.get("options") or []:
        if not isinstance(o, dict):
            opts.append({"name": str(o)})
            continue
        opt = {"name": o.get("name", "?")}
        totals = o.get("totals") or {}
        for k, src in (("cost", ("cost", "cost_usd")), ("power_mw", ("power_mw",))):
            val = next((x for x in (o.get(k2, totals.get(k2)) for k2 in src) if isinstance(x, (int, float))), None)
            if val is not None:
                opt[k] = round(val, 3)
        parts = ", ".join(p.get("mpn", "") for p in o.get("parts") or [] if isinstance(p, dict))
        gate = "" if o.get("gate_ok", True) else "GATE FAILED: " + ", ".join(o.get("gate_errors") or [])
        notes = "; ".join(filter(None, [gate, o.get("summary", ""), parts, *map(str, o.get("risks") or [])]))
        if notes:
            opt["notes"] = notes
        opts.append(opt)
    return {
        "to": to,
        "options": opts,
        "conflicts": [str(c) for c in rep.get("conflicts") or []],
        "cannot_meet": [str(c) for c in rep.get("cannot_meet") or []] +
                       (["no option passed the gate"] if rep.get("status") == "no_valid_option" else []),
        "needs": [str(c) for c in rep.get("needs_from_others") or rep.get("needs") or []],
        "quantitative": rep.get("quantitative_constraints") or [],
        "qualitative": rep.get("qualitative_constraints") or [],
    }


@dataclass
class Translator:
    """Stateful: remembers each agent's parent so every event carries its tree edge."""
    parents: dict[tuple[str, str], str | None] = field(default_factory=dict)
    specs: dict[str, dict] = field(default_factory=dict)  # run -> its brief (budget), so meters show THIS run's limits
    by_role: dict[tuple[str, str], str] = field(default_factory=dict)      # (run, subsystem) -> latest agent
    roots: dict[str, str] = field(default_factory=dict)                     # run -> mother agent
    options: dict[tuple[str, str], dict[str, list]] = field(default_factory=dict)  # (run, subsystem) -> option -> parts
    wired: set[str] = field(default_factory=set)                            # runs with a design from src/wiring.py

    def _design(self, r: Row, bom: list[dict] | None, source: str) -> list[AgentEvent]:
        """design_updated from the reference board, or from a BOM applied to it (see cvhack.synth)."""
        if bom is None:
            d, notes = designs.wearable(), {"kept": [], "removed": [], "added": [], "unwired": []}
        else:
            d, notes = apply_selection(bom, load_parts())
        sp = self.specs.get(r.run)
        if sp:  # the meters show THIS run's budget, not the reference board's
            d.budgets.cost_usd, d.budgets.power_mw = float(sp["cost_usd"]), float(sp["power_mw"])
        return [self._ev(r, self.roots.get(r.run, r.agent), "design_updated", design=d.model_dump(),
                         source=source, notes=notes)]

    def _ensure(self, r: Row, agent: str, role: str, label: str, parent: str | None) -> list[AgentEvent]:
        """Spawn agents A's log never `start`s (the critic, the gate)."""
        if (r.run, agent) in self.parents:
            return []
        self.parents[(r.run, agent)] = parent
        return [self._ev(r, agent, "agent_spawned", role=role, label=label)]

    def learn(self, r: Row) -> None:
        """Absorb a historical row (after a server restart) without emitting anything."""
        if r.kind == "spec" and isinstance(r.body, dict):
            self.specs[r.run] = r.body
        elif r.kind == "start":
            self.parents[(r.run, r.agent)] = r.parent
            self.by_role[(r.run, r.agent.split(":")[0])] = r.agent
            if not r.parent:
                self.roots[r.run] = r.agent
        elif r.kind == "final" and r.parent and isinstance(r.body, dict) and r.body.get("subsystem"):
            self.options[(r.run, r.body["subsystem"])] = {
                o.get("name"): [p for p in o.get("parts") or [] if isinstance(p, dict) and p.get("mpn")]
                for o in r.body.get("options") or [] if isinstance(o, dict)}

    def _ev(self, r: Row, agent: str, kind: str, **payload) -> AgentEvent:
        return AgentEvent(run_id=r.run, agent_id=agent, parent_id=self.parents.get((r.run, agent)),
                          kind=kind, payload=payload, ts=r.ts)

    def translate(self, r: Row) -> list[AgentEvent]:
        role = r.agent.split(":")[0]
        if r.kind == "constraint" and isinstance(r.body, dict):  # the run picked up a dashboard constraint change
            return [self._ev(r, r.agent, "constraint_change", **r.body)]
        if r.kind == "spec":  # shown in the chat column; not an agent
            if isinstance(r.body, dict):
                self.specs[r.run] = r.body
            return []
        if r.kind == "result":
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("selections"):
                return []  # the mother's own `final` row already reported the design and run_done
            return [self._ev(r, self.roots.get(r.run, r.agent), "run_done", ok=False,
                             summary=f"No accepted design ({b.get('status', 'unknown')})")]
        if r.kind == "board_check":
            b = r.body if isinstance(r.body, dict) else {}
            msg = ("Checked the board: " + ("it passed the design rules and electrical checks." if not b.get("problems") else
                   f"{len(b['problems'])} problem(s) found: " + _short("; ".join(b["problems"]), 160)))
            return [self._ev(r, r.agent, "step", message=msg)]
        if r.kind == "start":
            self.parents[(r.run, r.agent)] = r.parent
            self.by_role[(r.run, role)] = r.agent
            if not r.parent:
                self.roots[r.run] = r.agent
                out_ref = self._design(r, None, "reference")
            else:
                out_ref = []
            out = [self._ev(r, r.agent, "agent_spawned", role="subsystem" if r.parent else "mother",
                            label=role.replace("_", " "), strategy=(r.body or {}).get("strategy") if isinstance(r.body, dict) else None,
                            agent_type=(r.body or {}).get("agent_type") if isinstance(r.body, dict) else None),
                   self._ev(r, r.agent, "agent_status", status="running")] + out_ref
            if r.parent:
                task = (r.body or {}).get("task", "") if isinstance(r.body, dict) else str(r.body)
                out.append(self._ev(r, r.parent, "delegate", to=r.agent, subsystem=role, budget={},
                                    constraints=[_short(task, 400)] if task else []))
            return out
        if r.kind == "llm_call":
            b = r.body if isinstance(r.body, dict) else {}
            if not b:
                return []
            out = []
            if r.agent == "critic":
                out += self._ensure(r, "critic", "verifier", "Critic", self.roots.get(r.run))
            return out + [self._ev(r, r.agent, "inference", provider="Crusoe", **b)]
        if r.kind == "gate":
            b = r.body if isinstance(r.body, dict) else {}
            failed = b.get("failed") or {}
            errors = [{"code": c.split(":")[0], "message": f"{opt}: {c}", "part": (c.split(":", 1) + [""])[1] or None,
                       "severity": "error"} for opt, codes in failed.items() for c in codes]
            ok = bool(b.get("passed"))
            sub = b.get("subsystem", role)
            out = self._ensure(r, "gate", "verifier", "Deterministic gate", self.roots.get(r.run))
            out.append(self._ev(r, "gate", "verifier_result", verifier=f"gate:{sub}", ok=ok, errors=errors,
                                details={"attempt": b.get("attempt"), "passed": b.get("passed")}))
            out.append(self._ev(r, r.agent, "step", message=(f"gate attempt {b.get('attempt')}: passed {b.get('passed')}"
                                                            if ok else f"gate attempt {b.get('attempt')}: rejected {list(failed)}")))
            return out
        if r.kind == "critique":
            b = r.body if isinstance(r.body, dict) else {}
            objs = b.get("objections") or []
            errors = [{"code": "CRITIC_" + str(o.get("severity", "concern")).upper(),
                       "message": f"{o.get('id', '')} [{o.get('subsystem', '')}] {o.get('issue', '')}"
                                  + (f" — {o.get('evidence')}" if o.get("evidence") else ""),
                       "part": o.get("part") or None,
                       "severity": "error" if o.get("severity") == "blocker" else "warning"} for o in objs]
            out = self._ensure(r, "critic", "verifier", "Critic", self.roots.get(r.run))
            out.append(self._ev(r, "critic", "verifier_result", verifier="critic",
                                ok=not any(e["severity"] == "error" for e in errors), errors=errors,
                                details={"round": b.get("round")}))
            return out
        if r.kind == "critique_failed":
            return self._ensure(r, "critic", "verifier", "Critic", self.roots.get(r.run)) + \
                [self._ev(r, "critic", "step", message="review unavailable (not a verdict); retrying")]
        if r.kind == "budget_check":
            b = r.body if isinstance(r.body, dict) else {}
            bud = b.get("budget") or {}
            return [self._ev(r, r.agent, "step", message=f"budget check: ${b.get('cost_usd')} / ${bud.get('cost_usd')}, "
                                                        f"{b.get('power_mw')} / {bud.get('power_mw')} mW"
                                                        + (f", {len(b['unknown_price_parts'])} unpriced" if b.get("unknown_price_parts") else ""))]
        if r.kind == "queue":
            b = r.body if isinstance(r.body, dict) else {}
            return [self._ev(r, r.agent, "step", message=f"waited {b.get('waited_s')}s in the Crusoe request queue "
                                                        f"({b.get('running')}/{b.get('cap')} in flight)")]
        if r.kind == "findings_gate":
            b = r.body if isinstance(r.body, dict) else {}
            bad = b.get("rejected") or []
            out = self._ensure(r, "gate", "verifier", "Deterministic gate", self.roots.get(r.run))
            out.append(self._ev(r, "gate", "verifier_result", verifier=f"findings:{b.get('type', role)}", ok=not bad,
                                errors=[{"code": "FINDING_REJECTED", "message": str(x)[:300], "severity": "error"} for x in bad],
                                details={"accepted": b.get("accepted"), "attempt": b.get("attempt")}))
            return out
        if r.kind == "wiring_gate":
            b = r.body if isinstance(r.body, dict) else {}
            sub = b.get("subsystem", role)
            out = self._ensure(r, "gate", "verifier", "Deterministic gate", self.roots.get(r.run))
            out.append(self._ev(r, "gate", "verifier_result", verifier=f"wiring:{sub}", ok=bool(b.get("ok")),
                                errors=[{"code": str(x).split(":")[0], "message": str(x)[:300], "severity": "error"}
                                        for x in b.get("errors") or []],
                                details={"attempt": b.get("attempt"), "notes": b.get("notes")}))
            out.append(self._ev(r, r.agent, "step", message=f"wiring attempt {b.get('attempt')}: "
                                                            + ("accepted" if b.get("ok") else
                                                               f"{len(b.get('errors') or [])} problem(s)")))
            return out
        if r.kind == "design":
            self.wired.add(r.run)
            root = self.roots.get(r.run, r.agent)
            return [self._ev(r, root, "design_updated", design=r.body, source="wired")]
        if r.kind == "verifier":
            b = dict(r.body) if isinstance(r.body, dict) else {}
            b.pop("round", None)
            out = self._ensure(r, r.agent, "verifier", r.agent.replace("verifier.", "").title() + " verifier",
                               self.roots.get(r.run))
            return out + [self._ev(r, r.agent, "verifier_result", **b)]
        if r.kind == "requirement":
            return [self._ev(r, self.roots.get(r.run, r.agent), "requirement_status", **r.body)]
        if r.kind == "constraint_change":
            b = r.body if isinstance(r.body, dict) else {}
            root = self.roots.get(r.run, r.agent)
            what = b.get("mpn") or b.get("value") or ""
            return [self._ev(r, root, "constraint_change", **b),
                    self._ev(r, root, "replan", reason=f"operator: {b.get('type', 'change')} {what}".strip(),
                             changes=[json.dumps(b)])]
        if r.kind in ("retry", "llm_error"):
            b = r.body if isinstance(r.body, dict) else {}
            return [self._ev(r, r.agent, "step", message=f"{r.kind}: {b.get('reason') or b.get('error') or ''}")]
        if r.kind == "tool_call":
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tool") in ("delegate", "delegate_batch"):
                return []
            if b.get("tool") == "finalize":
                args = b.get("args") or {}
                sels = [s for s in args.get("selections") or [] if isinstance(s, dict)]
                out = [self._ev(r, r.agent, "decision", to=self.by_role.get((r.run, s.get("subsystem")), s.get("subsystem")),
                                chosen=s.get("option"), message=args.get("rationale", "")[:300])
                       for s in sels]
                bom = [{"subsystem": s.get("subsystem"), **p}
                       for s in sels for p in self.options.get((r.run, s.get("subsystem")), {}).get(s.get("option"), [])]
                if bom:
                    out += self._design(r, bom, "selection")
                return out + [self._ev(r, r.agent, "step", message="finalize: " + ", ".join(
                    f"{s.get('subsystem')}={s.get('option')}" for s in args.get("selections") or [] if isinstance(s, dict)))]
            return [self._ev(r, r.agent, "step", message=tool_phrase(b.get("tool", ""), b.get("args", {})))]
        if r.kind == "tool_result":
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tool") in ("delegate", "delegate_batch"):
                return []
            res = str(b.get("result", ""))
            # Only outcomes worth reading: verdicts and errors. Search results etc. are noise in the log.
            if b.get("tool") in ("submit_report", "finalize") or res.startswith(("ERROR", "REJECTED")) or res == "no matching parts":
                return [self._ev(r, r.agent, "step", message=f"{b.get('tool', '').replace('_', ' ')}: {_short(res, 110)}")]
            return []
        if r.kind == "final":
            text = r.body if isinstance(r.body, str) else json.dumps(r.body)
            if not r.parent and isinstance(r.body, dict) and "selections" in r.body:
                res = r.body
                status = str(res.get("status", ""))
                t = res.get("totals") or {}
                w = res.get("wiring") or {}
                failed_reqs = [k for k, v in (w.get("requirements") or {}).items() if v != "pass"]
                summary = (f"{status}: " + ", ".join(f"{k}={v}" for k, v in (res.get("selections") or {}).items())
                           + f" · ${t.get('cost_usd')} · {t.get('power_mw')} mW · critic rounds {res.get('critic_rounds')}"
                           + (f" · {len(res['unresolved'])} unresolved objection(s)" if res.get("unresolved") else "")
                           + (f" · contested blockers {res['contested_blockers']}" if res.get("contested_blockers") else "")
                           + (" · ERC " + ("pass" if w.get("electrical_ok") else "FAIL") if w else "")
                           + (f" · requirements not met: {failed_reqs}" if failed_reqs else ""))
                # Green only for a clean result: accepted outright, ERC clean, no non-advisory requirement failing.
                advisory = set(w.get("advisory_requirements") or [])
                ok = status == "accepted" and (not w or (bool(w.get("electrical_ok"))  # pre-wiring results: status only
                                                         and not [x for x in failed_reqs if x not in advisory]))
                # The wired design (a `design` row) is the real schematic; only fall back to the synthesized one without it.
                synth = self._design(r, res["bom"], "final") if res.get("bom") and r.run not in self.wired else []
                return [self._ev(r, r.agent, "step", message=summary)] + synth + [
                        self._ev(r, r.agent, "agent_status", status="done"),
                        self._ev(r, r.agent, "run_done", ok=ok, summary=summary[:600], result=res)]
            if text.strip() in ("STEP LIMIT", "NO_ACCEPTED_OUTPUT"):
                out = [self._ev(r, r.agent, "step", message="hit the step limit"),
                       self._ev(r, r.agent, "agent_status", status="failed")]
                if not r.parent:
                    out.append(self._ev(r, r.agent, "run_done", ok=False,
                                        summary="Mother agent finished without an accepted design"))
                return out
            out = []
            rep = parse_report(text) if r.parent else None
            if rep is not None and rep.get("subsystem"):
                self.options[(r.run, rep["subsystem"])] = {
                    o.get("name"): [p for p in o.get("parts") or [] if isinstance(p, dict) and p.get("mpn")]
                    for o in rep.get("options") or [] if isinstance(o, dict)}
            if rep is not None:
                out.append(self._ev(r, r.agent, "report", **report_payload(rep, r.parent)))
            else:
                out.append(self._ev(r, r.agent, "step", message=_short(text, 600)))
            out.append(self._ev(r, r.agent, "agent_status", status="done"))
            if not r.parent:
                out.append(self._ev(r, r.agent, "run_done", ok=True, summary=_short(text, 300)))
            return out
        return [self._ev(r, r.agent, "step", message=f"{r.kind}: {_short(r.body)}")]


def read_rows(db: Path, after: int = 0, run: str | None = None) -> list[Row]:
    if not db.exists():
        return []
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        q = "SELECT id, ts, run, agent, parent, kind, body FROM messages WHERE id > ?"
        args: list[Any] = [after]
        if run:
            q += " AND run = ?"
            args.append(run)
        rows = c.execute(q + " ORDER BY id", args).fetchall()
    except sqlite3.OperationalError:  # table not created yet
        return []
    finally:
        c.close()
    out = []
    for rid, ts, rn, agent, parent, kind, body in rows:
        try:
            body = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            pass
        out.append(Row(rid, ts, rn, agent, parent, kind, body))
    return out


def bridge(rows: Iterable[Row], tr: Translator) -> int:
    n = 0
    for r in rows:
        for e in tr.translate(r):
            emit(e)
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--run", default=None, help="only bridge this run id")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--from-start", action="store_true",
                    help="re-emit rows already bridged (default: start at the end of the DB)")
    a = ap.parse_args()
    db = Path(a.db)
    tr = Translator()
    last = 0
    if not a.from_start and not a.once:
        # Skip history, but learn parents so later events still attach to the right node.
        old = read_rows(db, 0, a.run)
        for r in old:
            if r.kind == "start":
                tr.parents[(r.run, r.agent)] = r.parent
        last = old[-1].id if old else 0
    print(f"bridging {db} from row {last}", flush=True)
    while True:
        rows = read_rows(db, last, a.run)
        if rows:
            n = bridge(rows, tr)
            last = rows[-1].id
            print(f"rows ≤{last}: {n} events", flush=True)
        if a.once:
            break
        time.sleep(0.5)


if __name__ == "__main__":
    main()
