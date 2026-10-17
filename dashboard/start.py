"""Start a design run from a prompt.   POST /api/start {"prompt": "..."} -> {"run_id": "..."}

Spawns `python -m src.run_prompt` as a subprocess and runs the bridge (agent log -> dashboard events) in a
thread of this server, so a run needs nothing else started by hand."""
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cvhack import bridge
from cvhack.events import RUNS_DIR, run_dir

ROOT = Path(__file__).resolve().parent.parent
router = APIRouter()
_procs: dict[str, subprocess.Popen] = {}
MAX_ACTIVE = 2  # each run is ~10 agents; the queue caps calls, this caps how many runs pile up


class StartIn(BaseModel):
    prompt: str
    instructions: str = ""


@router.post("/api/start")
def start(body: StartIn):
    prompt = body.prompt.strip()
    if len(prompt) < 5:
        raise HTTPException(400, "Describe what you want to design.")
    if len(prompt) > 1000:
        raise HTTPException(400, "Keep the request under 1000 characters.")
    active = [r for r, p in _procs.items() if p.poll() is None]
    if len(active) >= MAX_ACTIVE:
        raise HTTPException(429, f"{len(active)} runs are already in progress; wait for one to finish.")
    run_id = uuid.uuid4().hex[:6]
    (run_dir(run_id) / "events.jsonl").touch()  # so the UI can open the run's stream before the first event
    log = open(run_dir(run_id) / "agent.log", "w")
    env = {**__import__("os").environ, "TEAM_INSTRUCTIONS": body.instructions.strip()[:800]}
    _procs[run_id] = subprocess.Popen([sys.executable, "-m", "src.run_prompt", run_id, prompt], cwd=ROOT, env=env,
                                      stdout=log, stderr=subprocess.STDOUT)
    return {"run_id": run_id}


@router.get("/api/runs/{run_id}/active")
def active(run_id: str):
    p = _procs.get(run_id)
    return {"active": bool(p and p.poll() is None)}


def _row(run_id: str, kind: str):
    import json
    import sqlite3
    if not bridge.DEFAULT_DB.exists():
        return None
    c = sqlite3.connect(f"file:{bridge.DEFAULT_DB}?mode=ro", uri=True)
    try:
        r = c.execute("select body from messages where run=? and kind=? order by id desc limit 1",
                      (run_id, kind)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()
    return json.loads(r[0]) if r else None


@router.get("/api/runs/{run_id}/spec")
def spec(run_id: str):
    return _row(run_id, "spec")


@router.get("/api/runs/{run_id}/result")
def result(run_id: str):
    return _row(run_id, "result")


class CustomIn(BaseModel):
    mpn: str = ""
    category: str = "mcu"
    description: str = ""
    price_usd: float | None = None
    stock: int | None = None
    power_mw: float | None = None
    max_voltage_v: float | None = None
    footprint: str = ""
    verilog: str = ""
    pins: str = ""


def _cp(fn, *a):
    from cvhack import custom_parts
    try:
        return fn(*a)
    except custom_parts.CustomPartError as e:
        raise HTTPException(400, str(e))


@router.get("/api/custom_parts")
def list_custom():
    from cvhack import custom_parts
    return {"categories": [{"value": c, "label": custom_parts.CATEGORY_LABELS.get(c, c.title())} for c in custom_parts.CATEGORIES],
            "parts": [{"mpn": r["mpn"], "category": r["category"], "pins": len(r["pins"]),
                       "description": r["description"], "price_usd": r["price_usd"],
                       "footprint": r["footprint"], "footprint_assumed": r["ratings"].get("footprint_assumed")}
                      for r in custom_parts.load_raw()]}


@router.post("/api/custom_parts/preview")
def preview_custom(body: CustomIn):
    from cvhack import custom_parts
    rec = _cp(custom_parts.build, {**body.model_dump(), "mpn": body.mpn or "PREVIEW"})
    return {"pins": rec["pins"], "footprint": rec["footprint"], "footprint_assumed": rec["ratings"]["footprint_assumed"]}


@router.post("/api/custom_parts")
def add_custom(body: CustomIn):
    from cvhack import custom_parts
    rec = _cp(custom_parts.add, body.model_dump())
    return {"mpn": rec["mpn"], "pins": len(rec["pins"])}


@router.delete("/api/custom_parts/{mpn:path}")
def del_custom(mpn: str):
    from cvhack import custom_parts
    if not custom_parts.delete(mpn):
        raise HTTPException(404, "no such custom part")
    return {"ok": True}


class ShapeIn(BaseModel):
    text: str


@router.post("/api/runs/{run_id}/shape")
def set_shape(run_id: str, body: ShapeIn):
    """The board-shape agent: read a request like 'round 35 mm', validate it, save it for this run's board."""
    import json
    from cvhack.contracts import AgentEvent
    from cvhack.events import emit
    from src import shape
    if len(body.text.strip()) < 3:
        raise HTTPException(400, "Describe the shape, e.g. 'round, 38 mm' or 'capsule wristband pod'.")
    rd = run_dir(run_id)
    aid = "board_shape:" + uuid.uuid4().hex[:4]
    emit(AgentEvent(run_id=run_id, agent_id=aid, parent_id=None, kind="agent_spawned", payload={"role": "subsystem", "label": "board shape"}))
    spec = shape.extract(body.text)
    (rd / "outline.json").write_text(json.dumps(spec))
    emit(AgentEvent(run_id=run_id, agent_id=aid, parent_id=None, kind="step", payload={"message": f"outline → {spec['kind']}: {spec['note']}"}))
    emit(AgentEvent(run_id=run_id, agent_id=aid, parent_id=None, kind="agent_status", payload={"status": "done"}))
    emit(AgentEvent(run_id=run_id, agent_id="mother", parent_id=None, kind="design_updated", payload={"design": _latest_design(run_id)}) ) if _latest_design(run_id) else None
    return spec


def _latest_design(run_id: str):
    from cvhack.events import read_events
    from cvhack.state import fold
    evs, _ = read_events(run_id)
    return fold(run_id, evs).design


@router.get("/api/real_runs")
def real_runs():
    """Runs started from a prompt (they have a spec row), newest first. Scripted samples never appear here."""
    import sqlite3
    if not bridge.DEFAULT_DB.exists():
        return []
    c = sqlite3.connect(f"file:{bridge.DEFAULT_DB}?mode=ro", uri=True)
    try:
        rows = c.execute("select run, max(ts) from messages where run in (select run from messages where kind='spec') "
                         "group by run order by max(ts) desc").fetchall()
        return [{"run_id": r, "ts": t} for r, t in rows if (RUNS_DIR / r / "events.jsonl").exists()]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


@router.get("/api/parts")
def parts_table():
    """The whole parts database as rows for the Parts Database tab (catalog snapshot + your own parts)."""
    from cvhack.custom_parts import CATEGORY_LABELS
    from cvhack.parts import load_parts
    from cvhack.part_meta import card
    rows = []
    for p in load_parts().values():
        c = card(p.mpn, p.description)
        rows.append({"mpn": p.mpn, "name": c.get("name", p.mpn).replace(" (custom)", ""), "manufacturer": c.get("manufacturer"),
                     "category": CATEGORY_LABELS.get(p.category, p.category.replace("_", " ").title()),
                     "category_key": p.category, "price_usd": p.price_usd, "stock": p.stock, "pins": len(p.pins),
                     "package": (c.get("subtitle") or "").split(" · ")[-1], "links": c.get("links", {}),
                     "custom": bool(p.ratings.get("custom")), "source": c.get("source")})
    rows.sort(key=lambda r: (not r["custom"], r["category"], r["mpn"]))
    return rows


@router.get("/api/learning")
def learning():
    """Recursive improvement over time: every real run's results, plus the lessons carried between runs."""
    from src import lessons
    data = lessons.load()
    runs = []
    for r in reversed(real_runs()):  # oldest first
        try:
            L = ledger(r["run_id"])
        except Exception:
            continue
        if not L or not L.get("system", {}).get("done"):
            continue
        sy = L["system"]
        runs.append({"run_id": r["run_id"], "ts": r["ts"], "device": (L.get("spec") or {}).get("device"),
                     "seconds": sy["seconds"], "objections": sy["objections"], "blockers": sy["blockers"], "fixed": sy["fixed"],
                     "rebutted": sy["rebutted"], "refused": L["counts"]["options_refused"], "lessons_used": sy["lessons_used"],
                     "status": sy["status"], "cost_usd": (sy.get("totals") or {}).get("cost_usd"),
                     "complete": (sy.get("completeness") or {}).get("passed"), "of": (sy.get("completeness") or {}).get("total")})
    return {"runs": runs, "lessons": data["lessons"]}


class StatusIn(BaseModel):
    status: str


@router.get("/api/lessons")
def get_lessons():
    from src import lessons
    return lessons.load()


@router.post("/api/lessons/{lesson_id}/status")
def set_lesson(lesson_id: str, body: StatusIn):
    from src import lessons
    if body.status not in ("approved", "rejected", "candidate", "auto"):
        raise HTTPException(400, "bad status")
    if not lessons.set_status(lesson_id, body.status):
        raise HTTPException(404, "no such lesson")
    return {"ok": True}


@router.get("/api/runs/{run_id}/ledger")
def ledger(run_id: str):
    """What the run did and what the checks caught, computed from the log. Also the one-shot baseline."""
    import json
    import sqlite3
    from src import completeness
    if not bridge.DEFAULT_DB.exists():
        return None
    c = sqlite3.connect(f"file:{bridge.DEFAULT_DB}?mode=ro", uri=True)
    try:
        rows = [(k, a, json.loads(b), ts) for k, a, b, ts in
                c.execute("select kind, agent, body, ts from messages where run=? order by id", (run_id,))]
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()
    if not rows:
        return None
    caught, objections, answered = [], [], {}
    n = {"agents": 0, "tool_calls": 0, "gate_checks": 0, "options_refused": 0, "findings_dropped": 0}
    base, result, spec = None, None, None
    for k, a, b, ts in rows:
        if k == "start":
            n["agents"] += 1
        elif k == "tool_call":
            n["tool_calls"] += 1
            if b.get("tool") == "finalize":
                for r in b["args"].get("responses", []) or []:
                    if isinstance(r, dict):
                        answered[r.get("objection_id")] = r.get("stance")
        elif k == "gate":
            n["gate_checks"] += 1
            for name, errs in (b.get("failed") or {}).items():
                n["options_refused"] += 1
                if len(caught) < 12:
                    caught.append({"agent": a.split(":")[0], "option": name, "codes": sorted({e.split(":")[0] for e in errs}),
                                   "parts": sorted({e.split(":", 1)[1] for e in errs if ":" in e and e.split(":", 1)[1]})})
        elif k == "findings_gate":
            n["findings_dropped"] += len(b.get("rejected") or [])
        elif k == "critique":
            objections += b.get("objections") or []
        elif k == "baseline":
            base = b
        elif k == "result":
            result = b
        elif k == "spec":
            spec = b
    sysc = None
    if result and result.get("bom"):
        bom: dict = {}
        for x in result["bom"]:
            bom.setdefault(x["subsystem"], []).append({"mpn": x["mpn"], "qty": x.get("qty", 1)})
        sysc = completeness.score(bom)
    blockers = [o for o in objections if o.get("severity") == "blocker"]
    return {"counts": n, "caught": caught, "baseline": base, "system": {
        "completeness": sysc, "seconds": round(rows[-1][3] - rows[0][3], 1), "done": bool(result),
        "status": (result or {}).get("status"), "totals": (result or {}).get("totals"),
        "objections": len(objections), "blockers": len(blockers),
        "fixed": sum(1 for o in objections if answered.get(o["id"]) == "accept"),
        "rebutted": sum(1 for o in objections if answered.get(o["id"]) == "rebut"),
        "lessons_used": len((result or {}).get("lessons_used") or [])}, "spec": spec}


def _bridge_loop():
    """Follow runs/run.db, translating rows into dashboard events. The cursor is saved, so a server restart
    resumes where it stopped (rows before it only teach the translator parents and options)."""
    db, tr = bridge.DEFAULT_DB, bridge.Translator()
    cur_file = RUNS_DIR / ".bridge_cursor"
    cursor = None
    while True:
        try:
            rows = bridge.read_rows(db, 0 if cursor is None else cursor)
            if cursor is None:  # first pass: history only teaches state; emit nothing older than the saved cursor
                saved = int(cur_file.read_text()) if cur_file.exists() else (rows[-1].id if rows else 0)
                for r in rows:
                    if r.id <= saved:
                        if r.kind == "spec" and isinstance(r.body, dict):
                            tr.specs[r.run] = r.body
                        if r.kind == "start":
                            tr.parents[(r.run, r.agent)] = r.parent
                        if r.kind == "final" and r.parent and isinstance(r.body, dict) and r.body.get("options"):
                            for o in r.body["options"]:
                                tr.options[(r.run, o.get("name"))] = (r.body.get("subsystem"), o.get("parts") or [])
                cursor = saved
                continue
            if rows:
                bridge.bridge(rows, tr)
                cursor = rows[-1].id
                cur_file.write_text(str(cursor))
        except Exception:
            pass
        time.sleep(0.5)


def install(app):
    app.include_router(router)
    threading.Thread(target=_bridge_loop, daemon=True).start()
