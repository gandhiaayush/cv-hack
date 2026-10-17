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
        for k in ("cost", "power_mw"):
            if isinstance(o.get(k), (int, float)):
                opt[k] = o[k]
        notes = "; ".join(filter(None, [o.get("summary", ""), *map(str, o.get("risks") or [])]))
        if notes:
            opt["notes"] = notes
        opts.append(opt)
    return {
        "to": to,
        "options": opts,
        "conflicts": [str(c) for c in rep.get("conflicts") or []],
        "cannot_meet": [str(c) for c in rep.get("cannot_meet") or []],
        "needs": [str(c) for c in rep.get("needs_from_others") or rep.get("needs") or []],
        "quantitative": rep.get("quantitative_constraints") or [],
        "qualitative": rep.get("qualitative_constraints") or [],
    }


@dataclass
class Translator:
    """Stateful: remembers each agent's parent so every event carries its tree edge."""
    parents: dict[tuple[str, str], str | None] = field(default_factory=dict)
    options: dict[tuple[str, str], tuple[str, list]] = field(default_factory=dict)  # (run, option) -> (subsystem, parts)

    def _ev(self, r: Row, agent: str, kind: str, **payload) -> AgentEvent:
        return AgentEvent(run_id=r.run, agent_id=agent, parent_id=self.parents.get((r.run, agent)),
                          kind=kind, payload=payload, ts=r.ts)

    def _design_events(self, r: Row, chosen: dict, label: str) -> list[AgentEvent]:
        """Apply the agents' chosen parts to the baseline board and tell the UI (schematic, 3D, BOM, ERC)."""
        try:
            from cvhack.parts import load_parts
            from cvhack.tooling.electrical import ElectricalVerifier
            from src.design_apply import apply
            parts = load_parts()
            d, rep = apply(chosen, parts)
            res = ElectricalVerifier(use_kicad=False).verify(d, parts)
            msg = (f"{label}: {len(rep['swapped'])} part(s) swapped into the board, {len(rep['kept'])} already there, "
                   f"{len(rep['unplaced'])} not drawn (" + "; ".join(f"{u['mpn']}: {u['reason']}" for u in rep["unplaced"][:3]) + ")")
            return [self._ev(r, r.agent, "design_updated", design=d.model_dump()),
                    self._ev(r, r.agent, "verifier_result", **res.model_dump(), target_subsystem=None),
                    self._ev(r, r.agent, "step", message=msg)]
        except Exception as exc:  # the bridge must never stop because a design could not be built
            return [self._ev(r, r.agent, "step", message=f"{label}: could not build a board ({type(exc).__name__}: {exc})")]

    def translate(self, r: Row) -> list[AgentEvent]:
        role = r.agent.split(":")[0]
        if r.kind == "final" and r.parent and isinstance(r.body, dict) and r.body.get("options"):
            for o in r.body["options"]:  # remember each explorer option's parts for when the mother selects it
                self.options[(r.run, o.get("name"))] = (r.body.get("subsystem") or role, o.get("parts") or [])
        if r.kind == "tool_call" and isinstance(r.body, dict) and r.body.get("tool") == "finalize":
            chosen: dict[str, list] = {}
            for sel in (r.body.get("args") or {}).get("selections") or []:
                ent = self.options.get((r.run, sel.get("option"))) if isinstance(sel, dict) else None
                if ent:
                    chosen.setdefault(ent[0], []).extend(ent[1])
            base = [self._ev(r, r.agent, "step", message=f"finalize({_short(r.body.get('args', {}), 100)})")]
            return base + (self._design_events(r, chosen, "Proposed design") if chosen else [])
        if r.kind == "spec":  # shown in the chat column; not an agent
            return []
        if r.kind == "result":
            b = r.body if isinstance(r.body, dict) else {}
            ok = bool(b.get("selections"))
            chosen: dict[str, list] = {}
            for x in b.get("bom") or []:
                chosen.setdefault(x["subsystem"], []).append({"mpn": x["mpn"], "qty": x.get("qty", 1)})
            extra = self._design_events(r, chosen, "Final design") if chosen else []
            summary = (f"Design {b.get('status', '')}: ${(b.get('totals') or {}).get('cost_usd', '?')}" if ok
                       else f"No accepted design ({b.get('status', 'unknown')})")
            return extra + [self._ev(r, r.agent, "run_done", ok=ok, summary=summary)]
        if r.kind == "start":
            self.parents[(r.run, r.agent)] = r.parent
            out = [self._ev(r, r.agent, "agent_spawned", role="subsystem" if r.parent else "mother",
                            label=role.replace("_", " ")),
                   self._ev(r, r.agent, "agent_status", status="running")]
            if r.parent:
                task = (r.body or {}).get("task", "") if isinstance(r.body, dict) else str(r.body)
                out.append(self._ev(r, r.parent, "delegate", to=r.agent, subsystem=role, budget={},
                                    constraints=[_short(task, 400)] if task else []))
            return out
        if r.kind == "tool_call":
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tool") == "delegate":
                return []
            return [self._ev(r, r.agent, "step", message=f"{b.get('tool')}({_short(b.get('args', {}), 100)})")]
        if r.kind == "tool_result":
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tool") == "delegate":
                return []
            return [self._ev(r, r.agent, "step", message=f"{b.get('tool')} → {_short(b.get('result', ''))}")]
        if r.kind == "final":
            text = r.body if isinstance(r.body, str) else json.dumps(r.body)
            if text.strip() == "STEP LIMIT":
                out = [self._ev(r, r.agent, "step", message="hit the step limit"),
                       self._ev(r, r.agent, "agent_status", status="failed")]
                if not r.parent:
                    out.append(self._ev(r, r.agent, "run_done", ok=False, summary="Mother agent hit the step limit"))
                return out
            out = []
            rep = parse_report(text) if r.parent else None
            if rep is not None:
                out.append(self._ev(r, r.agent, "report", **report_payload(rep, r.parent)))
            else:
                out.append(self._ev(r, r.agent, "step", message=_short(text, 600)))
            out.append(self._ev(r, r.agent, "agent_status", status="done"))
            if not r.parent:
                out.append(self._ev(r, r.agent, "run_done", ok=True, summary=_short(text, 300)))
            return out
        if r.kind == "gate" and isinstance(r.body, dict):
            errs = [{"code": e.split(":")[0], "message": f"{n}: {e}", "part": e.split(":", 1)[1] or None}
                    for n, es in (r.body.get("failed") or {}).items() for e in es]
            return [self._ev(r, r.agent, "verifier_result", verifier="gate", ok=bool(r.body.get("passed")),
                             errors=errs, details={"attempt": r.body.get("attempt")}, target_subsystem=role)]
        if r.kind == "critique" and isinstance(r.body, dict):
            objs = r.body.get("objections") or []
            errs = [{"code": "CRITIC_" + (o.get("severity") or "concern").upper(), "message": (o.get("issue") or "")[:240],
                     "part": None, "severity": "error" if o.get("severity") == "blocker" else "warning"} for o in objs]
            return [self._ev(r, "critic", "verifier_result", verifier="critic",
                             ok=not any(o.get("severity") == "blocker" for o in objs), errors=errs,
                             details={"round": r.body.get("round")})]
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
