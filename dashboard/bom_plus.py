"""Rich BOM + cost spec + annotations. Mounted from server.py:  bom_plus.install(app, resolve, state)

GET    /api/runs/{id}/bom2            rows with names, links, price breaks, stock provenance + cost spec
GET    /api/runs/{id}/annotations     [{id, target, text, ts}]
POST   /api/runs/{id}/annotations     {target, text}       target = ref (U1), MPN, or "design"
DELETE /api/runs/{id}/annotations/{n}"""
import json
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cvhack.events import RUNS_DIR
from cvhack.parts import load_parts
from cvhack.part_meta import card, unit_price_at
from cvhack.tooling import budgets

VOLUMES = (1, 100, 1000)  # boards


class NoteIn(BaseModel):
    target: str
    text: str


def install(app, resolve, state):
    router = APIRouter()

    def _notes(rid):
        f = RUNS_DIR / rid / "annotations.json"
        return f, (json.loads(f.read_text()) if f.exists() else [])

    @router.get("/api/runs/{run_id}/bom2")
    def bom2(run_id: str):
        rid = resolve(run_id)
        d = state(rid).design_obj()
        if d is None:
            raise HTTPException(404, "no design yet")
        parts = load_parts()
        sub_of = {p.ref: b.subsystem for b in d.blocks for p in b.parts}
        rows = []
        for r in budgets.bom(d, parts):
            c = card(r["mpn"], r["description"])
            refs = r["refs"].split()
            row = {**r, **c, "subsystem": sub_of.get(refs[0], ""), "price_known": r["unit_usd"] is not None,
                   "at_volume": {str(v): unit_price_at(c["tiers"], r["qty"] * v, r["unit_usd"]) for v in VOLUMES}}
            row.pop("description", None)
            rows.append(row)
        spec = {"boards": {}, "by_subsystem": {}, "budget_usd": d.budgets.cost_usd,
                "unpriced_lines": sum(1 for x in rows if not x["price_known"])}
        for v in VOLUMES:
            spec["boards"][str(v)] = round(sum((x["at_volume"][str(v)] or 0) * x["qty"] for x in rows), 4)
        for x in rows:  # distributor price at qty 1, so the bar adds up to the first card
            spec["by_subsystem"][x["subsystem"]] = round(
                spec["by_subsystem"].get(x["subsystem"], 0) + (x["at_volume"]["1"] or 0) * x["qty"], 4)
        # what the agents' DB used (demo-catalogue values win over the catalog unless PARTS_PRICES=catalog)
        spec["agent_priced_total"] = round(sum((x["ext_usd"] or 0) for x in rows), 4)
        return {"rows": rows, "cost": spec,
                "note": "Unit prices are per-board at the quantity shown; volume columns apply distributor price "
                        "breaks to total purchased quantity. Snapshot data, not a live quote."}

    @router.get("/api/runs/{run_id}/annotations")
    def get_notes(run_id: str):
        return _notes(resolve(run_id))[1]

    @router.post("/api/runs/{run_id}/annotations")
    def add_note(run_id: str, body: NoteIn):
        rid = resolve(run_id)
        f, notes = _notes(rid)
        n = {"id": (max((x["id"] for x in notes), default=0) + 1), "target": body.target.strip()[:40],
             "text": body.text.strip()[:500], "ts": time.time()}
        if not n["text"]:
            raise HTTPException(400, "empty note")
        f.write_text(json.dumps(notes + [n]))
        return n

    @router.delete("/api/runs/{run_id}/annotations/{n}")
    def del_note(run_id: str, n: int):
        rid = resolve(run_id)
        f, notes = _notes(rid)
        f.write_text(json.dumps([x for x in notes if x["id"] != n]))
        return {"ok": True}

    app.include_router(router)
