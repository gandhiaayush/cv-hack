"""Dashboard backend.

    uvicorn dashboard.server:app --reload --port 8000

Reads runs/<run_id>/events.jsonl (written by cvhack.events.emit from any
process), streams it over SSE, and serves renders/exports of the latest design.
Serves the built frontend from dashboard/web/dist when it exists.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cvhack.contracts import ControlCommand
from cvhack.control import read_commands, send_command
from cvhack.events import RUNS_DIR, latest_run_id, read_events
from cvhack.outputs import matrix_csv, matrix_rows, write_outputs
from cvhack.parts import load_parts
from cvhack.state import fold
from cvhack.tooling import budgets, kicad_cli, kicad_sch, pcb
from cvhack.tooling.rules import RuleVerifier

app = FastAPI(title="cv-hack dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from dashboard.narration import install as install_narration  # noqa: E402

install_narration(app)

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


from dashboard import bom_plus  # noqa: E402

bom_plus.install(app, _resolve, _state)

from dashboard import start as _start  # noqa: E402

_start.install(app)

from dashboard import netlist_v as _netlist  # noqa: E402

_netlist.install(app, _resolve, _state)


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
    # Commands sent from the dashboard, in order; the first N are applied once the
    # agents have echoed N constraint_change events.
    cmds, _ = read_commands(rid)
    n_applied = len(s.constraint_changes)
    data["failing_refs"] = sorted(s.failing_refs())
    data["commands"] = [{**c.payload, "ts": c.ts, "applied": i < n_applied} for i, c in enumerate(cmds)]
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
    parts = load_parts()
    svg = kicad_cli.render_svg(d, parts, RUNS_DIR / "_render", highlight=s.failing_refs(), crop=True)
    renderer = "kicad"
    if svg is None:   # no kicad-cli: our own approximation of the same layout
        svg, renderer = kicad_sch.to_svg(d, parts, highlight=s.failing_refs()), "cvhack"
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store", "X-Renderer": renderer})


_board_lock = threading.Lock()                 # one KiCad placement at a time
_route_pool = ThreadPoolExecutor(max_workers=1)  # routing runs in the background
_route_jobs: dict[str, object] = {}              # build dir -> Future
_route_latest: dict[str, str] = {}               # run -> build dir it should route next


def _route_bg(run_id: str, b: pcb.BoardBuild):
    key = str(b.dir)
    _route_latest[run_id] = key
    if key in _route_jobs or not pcb.can_route() or b.route_info is not None:
        return

    def work():
        if _route_latest.get(run_id) != key:      # superseded by a newer edit before it started
            _route_jobs.pop(key, None)
            return
        try:
            pcb.route(b)
        except Exception as exc:                  # surfaced through board.json
            (b.dir / "route_error.txt").write_text(str(exc)[:2000])
    _route_jobs[key] = _route_pool.submit(work)


def _changes(prev: dict | None, design) -> dict:
    now = {i.ref: i.mpn for i in design.all_parts()}
    before = (prev or {}).get("parts", {})
    if not before:
        return {"added": [], "removed": [], "swapped": [], "first": True}
    return {"added": sorted(r for r in now if r not in before),
            "removed": sorted(r for r in before if r not in now),
            "swapped": [{"ref": r, "from": before[r], "to": now[r]} for r in sorted(now)
                        if r in before and before[r] != now[r]],
            "first": False}


def _place(run_id: str):
    rid = _resolve(run_id)
    d = _state(rid).design_obj()
    if d is None:
        raise HTTPException(404, "no design yet")
    if not pcb.available():
        raise HTTPException(503, "KiCad (kicad-cli, its Python and footprint libraries) is needed for the 3D board")
    of = RUNS_DIR / rid / "outline.json"  # custom board shape chosen by the board-shape agent
    if of.exists():
        d.outline = json.loads(of.read_text())
    state_f = RUNS_DIR / rid / "board_state.json"
    with _board_lock:
        prev = json.loads(state_f.read_text()) if state_f.exists() else None
        same = prev and prev.get("design") == d.model_dump()
        try:
            b = pcb.place(d, load_parts(), RUNS_DIR / "_board", None if not prev else prev.get("previous_of_this")
                          if same else {k: prev[k] for k in ("width_mm", "height_mm", "placements")})
        except RuntimeError as exc:
            raise HTTPException(500, str(exc))
        ch_f = b.dir / f"changes-{rid}.json"
        if not same:
            ch = _changes(prev, d)
            ch_f.write_text(json.dumps(ch))
            state_f.write_text(json.dumps({
                "design": d.model_dump(), "parts": {i.ref: i.mpn for i in d.all_parts()},
                "width_mm": b.info["width_mm"], "height_mm": b.info["height_mm"], "placements": b.info["placements"],
                "previous_of_this": {k: prev[k] for k in ("width_mm", "height_mm", "placements")} if prev else None}))
        changes = json.loads(ch_f.read_text()) if ch_f.exists() else {"added": [], "removed": [], "swapped": []}
    _route_bg(rid, b)
    return rid, d, b, changes


@app.get("/api/runs/{run_id}/board.json")
def board_info(run_id: str):
    rid, d, b, changes = _place(run_id)
    parts = load_parts()
    nets_of: dict[str, dict[str, str]] = {}
    for name, nodes in d.merged_nets().items():
        for n in nodes:
            nets_of.setdefault(n.ref, {})[n.pin] = name
    sub = {i.ref: blk.subsystem for blk in d.blocks for i in blk.parts}
    mpn = {i.ref: i.mpn for i in d.all_parts()}
    info = json.loads(json.dumps(b.info))
    for ref, pl in info.get("placements", {}).items():
        p = parts.get(mpn.get(ref, ""))
        pl.update(mpn=mpn.get(ref), value=(p.value or p.mpn) if p else "", description=p.description if p else "",
                  subsystem=sub.get(ref), nets=nets_of.get(ref, {}))
    err = b.dir / "route_error.txt"
    if b.route_info is not None and b.routed_glb.exists():
        route = {"status": "routed", **b.route_info}
        glb = "routed"
    elif err.exists():
        route, glb = {"status": "failed", "error": err.read_text()}, "placed"
    elif not pcb.can_route():
        route, glb = {"status": "unavailable", "error": "Freerouting not set up (demo/setup_router.sh)"}, "placed"
    else:
        route, glb = {"status": "routing"}, "placed"
    outline_info = None
    of = RUNS_DIR / rid / "outline.json"
    if of.exists():
        from src import shape
        spec = json.loads(of.read_text())
        outline_info = {"spec": spec, **shape.fit_report(spec, info["width_mm"], info["height_mm"])}
    return {**info, "outline": outline_info, "changes": changes, "route": route, "glb": glb, "build": b.dir.name,
            "glb_url": f"/api/runs/{rid}/board.glb?build={b.dir.name}&kind={glb}"}


@app.get("/api/runs/{run_id}/board.glb")
def board_glb(run_id: str, build: str | None = None, kind: str = "placed"):
    if build:
        d = (RUNS_DIR / "_board" / build).resolve()
        if d.parent != (RUNS_DIR / "_board").resolve():
            raise HTTPException(404)
        f = d / ("routed.glb" if kind == "routed" else "placed.glb")
    else:
        _, _, b, _ = _place(run_id)
        f = b.routed_glb if (kind == "routed" and b.routed_glb.exists()) else b.glb
    if not f.exists():
        raise HTTPException(404, "board not built yet")
    return FileResponse(f, media_type="model/gltf-binary", headers={"Cache-Control": "max-age=3600"})


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
