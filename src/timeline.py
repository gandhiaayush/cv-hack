"""Reasoning timeline for a run: what was proposed, what the gate/critic pushed back on, what changed.

    python -m src.timeline            (reads runs/run.db)"""
import json
import sqlite3
import sys


def main(db="runs/run.db"):
    c = sqlite3.connect(db)
    rows = [(r[0], r[1], r[2], json.loads(r[3])) for r in
            c.execute("select ts, agent, kind, body from messages order by id")]
    t0 = rows[0][0]
    print(f"{'t':>6}  event")
    for ts, agent, kind, b in rows:
        t = f"{ts - t0:5.0f}s"
        who = agent.split(":")[0]
        if kind == "start" and b.get("depth") == 1:
            print(f"{t}  [{who}] spawned by mother")
        elif kind == "gate":
            ok = ", ".join(b["passed"]) or "none"
            print(f"{t}  [{who}] GATE attempt {b['attempt']}: passed [{ok}]")
            for name, errs in b["failed"].items():
                print(f"{'':>6}      rejected '{name}': {', '.join(errs)}")
        elif kind == "budget_check":
            print(f"{t}  [mother] budget recomputed from DB: ${b['cost_usd']}, {b['power_mw']} mW")
        elif kind == "tool_call" and b["tool"] == "finalize":
            sel = {s["subsystem"]: s["option"] for s in b["args"]["selections"]}
            print(f"{t}  [mother] FINALIZE proposes {sel}")
            print(f"{'':>6}      reasoning: {b['args']['rationale'][:300]}")
            for r in b["args"].get("responses", []):
                print(f"{'':>6}      answers {r['objection_id']}: {r['stance']} - {(r.get('evidence') or '')[:200]}")
        elif kind == "critique":
            print(f"{t}  [critic] round {b['round']}: {len(b['objections'])} objections")
            for o in b["objections"]:
                print(f"{'':>6}      {o['id']} {o.get('severity')}: {o.get('part')} - {o.get('issue', '')[:170]}")
        elif kind == "critique_failed":
            print(f"{t}  [critic] UNAVAILABLE (failure {b['failures']}); not counted as a review")
        elif kind == "final" and who == "mother" and isinstance(b, dict):
            print(f"{t}  [mother] ACCEPTED: {b['status']}, selections {b['selections']}")


if __name__ == "__main__":
    main(*sys.argv[1:])
