"""Dashboard backend.

    uvicorn dashboard.server:app --reload --port 8000

Reads runs/<run_id>/events.jsonl (written by cvhack.events.emit from any
process), streams it over SSE, and serves renders/exports of the latest design.
Serves the built frontend from dashboard/web/dist when it exists.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cvhack.contracts import ControlCommand
from cvhack.control import send_command
from cvhack.events import RUNS_DIR, latest_run_id, read_events
from cvhack.outputs import matrix_csv, matrix_rows, write_outputs
from cvhack.parts import load_parts
from cvhack.state import fold
from cvhack.tooling import budgets, kicad_sch
from cvhack.tooling.rules import RuleVerifier

app = FastAPI(title="cv-hack dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"


def _resolve(run_id: str) -> str:
    if run_id == "latest":
        rid = latest_run_id()
        if rid is None:
            raise HTTPException(404, "no runs yet")
        return rid
    if not (RUNS_DIR / run_id / "events.jsonl").exists():
        raise HTTPException(404, f"run {run_id} not found")
    return run_id


def _state(run_id: str):
    events, _ = read_events(run_id)
    return fold(run_id, events)


@app.get("/api/runs")
def runs():
    if not RUNS_DIR.exists():
        return []
    out = []
    for p in RUNS_DIR.iterdir():
        f = p / "events.jsonl"
        if f.exists():
            out.append({"run_id": p.name, "mtime": f.stat().st_mtime})
    return sorted(out, key=lambda r: -r["mtime"])


@app.get("/api/runs/{run_id}/state")
def state(run_id: str):
    rid = _resolve(run_id)
    s = _state(rid)
    data = s.to_json()
    d = s.design_obj()
    if d is not None:
        parts = load_parts()
        data["totals"] = budgets.totals(d, parts)
        data["budgets"] = d.budgets.model_dump()
        # Ground truth on every design, whether or not the agents ran the rule gate.
        data["rules"] = RuleVerifier().evaluate(d, parts)
    return data


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, request: Request):
    """SSE. Each message id is the byte offset, so reconnects resume via Last-Event-ID."""
    rid = _resolve(run_id)
    try:
        offset = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        offset = 0

    async def gen():
        nonlocal offset
        yield "retry: 1000\n\n"
        while True:
            if await request.is_disconnected():
                break
            evs, new_offset = read_events(rid, offset)
            for e in evs:
                yield f"id: {new_offset}\ndata: {e.model_dump_json()}\n\n"
            offset = new_offset
            if not evs:
                yield ": ping\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/schematic.svg")
def schematic(run_id: str):
    s = _state(_resolve(run_id))
    d = s.design_obj()
    if d is None:
        raise HTTPException(404, "no design yet")
    svg = kicad_sch.to_svg(d, load_parts(), highlight=s.failing_refs())
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.get("/api/runs/{run_id}/bom")
def bom(run_id: str):
    d = _state(_resolve(run_id)).design_obj()
    return budgets.bom(d, load_parts()) if d else []


@app.get("/api/runs/{run_id}/bom.csv")
def bom_csv(run_id: str):
    d = _state(_resolve(run_id)).design_obj()
    if d is None:
        raise HTTPException(404, "no design yet")
    return PlainTextResponse(budgets.bom_csv(d, load_parts()), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{d.name}-bom.csv"'})


@app.get("/api/runs/{run_id}/matrix.csv")
def matrix(run_id: str):
    s = _state(_resolve(run_id))
    return PlainTextResponse(matrix_csv(matrix_rows(s, s.design_obj(), load_parts())), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="traceability.csv"'})


class ConstraintIn(BaseModel):
    type: str                      # "out_of_stock" | "device_class" | anything A understands
    mpn: str | None = None
    value: str | None = None


@app.post("/api/runs/{run_id}/control")
def control(run_id: str, body: ConstraintIn):
    rid = _resolve(run_id)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    send_command(rid, ControlCommand(payload=payload))
    return {"ok": True, "run_id": rid, "payload": payload}


@app.post("/api/runs/{run_id}/export")
def export(run_id: str):
    rid = _resolve(run_id)
    try:
        out = write_outputs(rid)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "dir": str(out), "files": sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())}


@app.get("/api/runs/{run_id}/out/{path:path}")
def out_file(run_id: str, path: str):
    base = (RUNS_DIR / _resolve(run_id) / "out").resolve()
    f = (base / path).resolve()
    if base not in f.parents or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
else:
    @app.get("/")
    def index():
        return PlainTextResponse("Frontend not built. Run `npm run dev` in dashboard/web (port 5173) "
                                 "or `npm run build` to serve it here.\n" + json.dumps(runs(), indent=2))
