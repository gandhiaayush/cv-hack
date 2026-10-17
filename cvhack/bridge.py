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
    by_role: dict[tuple[str, str], str] = field(default_factory=dict)      # (run, subsystem) -> latest agent
    roots: dict[str, str] = field(default_factory=dict)                     # run -> mother agent

    def _ensure(self, r: Row, agent: str, role: str, label: str, parent: str | None) -> list[AgentEvent]:
        """Spawn agents A's log never `start`s (the critic, the gate)."""
        if (r.run, agent) in self.parents:
            return []
        self.parents[(r.run, agent)] = parent
        return [self._ev(r, agent, "agent_spawned", role=role, label=label)]

    def _ev(self, r: Row, agent: str, kind: str, **payload) -> AgentEvent:
        return AgentEvent(run_id=r.run, agent_id=agent, parent_id=self.parents.get((r.run, agent)),
                          kind=kind, payload=payload, ts=r.ts)

    def translate(self, r: Row) -> list[AgentEvent]:
        role = r.agent.split(":")[0]
        if r.kind == "start":
            self.parents[(r.run, r.agent)] = r.parent
            self.by_role[(r.run, role)] = r.agent
            if not r.parent:
                self.roots[r.run] = r.agent
            out = [self._ev(r, r.agent, "agent_spawned", role="subsystem" if r.parent else "mother",
                            label=role.replace("_", " ")),
                   self._ev(r, r.agent, "agent_status", status="running")]
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
        if r.kind in ("retry", "llm_error"):
            b = r.body if isinstance(r.body, dict) else {}
            return [self._ev(r, r.agent, "step", message=f"{r.kind}: {b.get('reason') or b.get('error') or ''}")]
        if r.kind == "tool_call":
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tool") in ("delegate", "delegate_batch"):
                return []
            if b.get("tool") == "finalize":
                args = b.get("args") or {}
                out = [self._ev(r, r.agent, "decision", to=self.by_role.get((r.run, s.get("subsystem")), s.get("subsystem")),
                                chosen=s.get("option"), message=args.get("rationale", "")[:300])
                       for s in args.get("selections") or [] if isinstance(s, dict)]
                return out + [self._ev(r, r.agent, "step", message="finalize: " + ", ".join(
                    f"{s.get('subsystem')}={s.get('option')}" for s in args.get("selections") or [] if isinstance(s, dict)))]
            return [self._ev(r, r.agent, "step", message=f"{b.get('tool')}({_short(b.get('args', {}), 100)})")]
        if r.kind == "tool_result":
            b = r.body if isinstance(r.body, dict) else {}
            if b.get("tool") in ("delegate", "delegate_batch"):
                return []
            return [self._ev(r, r.agent, "step", message=f"{b.get('tool')} → {_short(b.get('result', ''))}")]
        if r.kind == "final":
            text = r.body if isinstance(r.body, str) else json.dumps(r.body)
            if not r.parent and isinstance(r.body, dict) and "selections" in r.body:
                res = r.body
                status = str(res.get("status", ""))
                t = res.get("totals") or {}
                summary = (f"{status}: " + ", ".join(f"{k}={v}" for k, v in (res.get("selections") or {}).items())
                           + f" · ${t.get('cost_usd')} · {t.get('power_mw')} mW · critic rounds {res.get('critic_rounds')}"
                           + (f" · {len(res['unresolved'])} unresolved objection(s)" if res.get("unresolved") else ""))
                return [self._ev(r, r.agent, "step", message=summary),
                        self._ev(r, r.agent, "agent_status", status="done"),
                        self._ev(r, r.agent, "run_done", ok=status.startswith("accepted"), summary=summary[:400],
                                 result=res)]
            if text.strip() in ("STEP LIMIT", "NO_ACCEPTED_OUTPUT"):
                out = [self._ev(r, r.agent, "step", message="hit the step limit"),
                       self._ev(r, r.agent, "agent_status", status="failed")]
                if not r.parent:
                    out.append(self._ev(r, r.agent, "run_done", ok=False,
                                        summary="Mother agent finished without an accepted design"))
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
