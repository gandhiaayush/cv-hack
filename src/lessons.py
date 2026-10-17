"""Recursive self-improvement, v1: lessons learned from the debate persist and shape future runs.

After a run, distill() turns the critic's objections into short reusable design lessons, each with its source
run, objection and evidence. Tiers:
  auto      the mother ACCEPTED the objection (the design changed because of it)  -> injected as advice
  approved  a human approved it                                                   -> injected as advice
  candidate a blocker the mother rebutted, or a one-off                           -> shown, NOT injected
  rejected  a human rejected it                                                   -> ignored
Lessons are advice for prompts (unverified), never silent changes to the checks. History records how many
reviewer blockers each run drew, so the effect of the lessons can be measured."""
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from .llm import chat

PATH = Path(__file__).resolve().parent.parent / "data" / "lessons.json"
_lock = threading.Lock()
SUBS = {"power", "analog_fe", "mcu_comms", "protection"}
GEN = """Rewrite this design-review objection as ONE reusable lesson for future designs of battery-powered
medical wearables. Imperative, general, at most 30 words, and name a checkable comparison if there is one
(e.g. 'compare fuse hold current with the charger's input current'). No part numbers unless essential.
Return JSON only: {"lesson": "...", "applies_to": "power|analog_fe|mcu_comms|protection|system"}"""


def load() -> dict:
    if PATH.exists():
        return json.loads(PATH.read_text())
    return {"lessons": [], "history": []}


def _save(d: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, indent=1))


def _key(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()[:90]


def active(subsystem: str | None = None, limit: int = 6) -> list[str]:
    """Lesson texts to inject into prompts: tier auto/approved, for this subsystem or the whole system."""
    out = [l for l in load()["lessons"] if l["status"] in ("auto", "approved")
           and (subsystem is None or l["applies_to"] in (subsystem, "system"))]
    out.sort(key=lambda l: (l["status"] != "approved", -l.get("seen", 1)))
    return [l["text"] for l in out[:limit]]


def _generalise(o: dict) -> dict | None:
    msg = json.dumps({"objection": o.get("issue", "")[:600], "evidence": o.get("evidence", "")[:400],
                      "subsystem": o.get("subsystem"), "part": o.get("part")})
    try:
        m = chat([{"role": "system", "content": GEN}, {"role": "user", "content": msg}],
                 os.environ["SUB_MODEL"], max_tokens=300, temperature=0,
                 extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        j = json.loads(re.search(r"\{.*\}", m.content or "", re.S).group(0))
        text = str(j["lesson"]).strip()
        return {"text": text[:300], "applies_to": j.get("applies_to") if j.get("applies_to") in SUBS else "system"}
    except Exception:
        return None


def distill(run_id: str, db: str = "runs/run.db") -> dict:
    """Read the finished run's debate from the log and update lessons.json. Returns a small summary."""
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute("select kind, agent, body from messages where run=? order by id", (run_id,)).fetchall()
    c.close()
    objections, stances = {}, {}
    for kind, agent, body in rows:
        b = json.loads(body)
        if kind == "critique":
            for o in b["objections"]:
                objections[o["id"]] = o
        elif kind == "tool_call" and b.get("tool") == "finalize":
            for r in b["args"].get("responses", []) or []:
                if isinstance(r, dict):
                    stances[r.get("objection_id")] = r.get("stance")
    blockers = [o for o in objections.values() if o.get("severity") == "blocker"]
    first_round = [o for o in blockers if o["id"].startswith("R0-")]
    added = 0
    with _lock:
        d = load()
        for o in objections.values():
            stance = stances.get(o["id"])
            if o.get("severity") != "blocker" and stance != "accept":
                continue
            g = _generalise(o)
            if not g:
                continue
            tier = "auto" if stance == "accept" else "candidate"
            hit = next((l for l in d["lessons"] if _key(l["text"]) == _key(g["text"])), None)
            if hit:
                hit["seen"] = hit.get("seen", 1) + (run_id not in hit["runs"])
                if run_id not in hit["runs"]:
                    hit["runs"].append(run_id)
                if tier == "auto" and hit["status"] == "candidate":
                    hit["status"] = "auto"
                continue
            d["lessons"].append({"id": f"L{len(d['lessons']) + 1}", **g, "status": tier, "seen": 1,
                                 "runs": [run_id], "source": {"run": run_id, "objection": o["id"],
                                 "issue": o.get("issue", "")[:400], "evidence": o.get("evidence", "")[:300],
                                 "stance": stance}, "ts": time.time()})
            added += 1
        d["history"].append({"run": run_id, "ts": time.time(), "blockers_total": len(blockers),
                             "blockers_first_round": len(first_round), "lessons_added": added})
        _save(d)
    return {"added": added, "blockers_total": len(blockers), "blockers_first_round": len(first_round)}


def set_status(lesson_id: str, status: str) -> bool:
    with _lock:
        d = load()
        for l in d["lessons"]:
            if l["id"] == lesson_id:
                l["status"] = status
                _save(d)
                return True
    return False
