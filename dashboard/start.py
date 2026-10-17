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
    _procs[run_id] = subprocess.Popen([sys.executable, "-m", "src.run_prompt", run_id, prompt], cwd=ROOT,
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
