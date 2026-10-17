"""Final deliverables for a run: KiCad project, BOM, traceability matrix, renders.

    from cvhack.outputs import write_outputs
    out_dir = write_outputs(run_id)          # A can call this on run_done

Everything lands in runs/<run_id>/out/.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from cvhack.contracts import Design, Part
from cvhack.events import read_events, run_dir
from cvhack.parts import load_parts
from cvhack.state import RunState, fold
from cvhack.tooling import budgets, kicad_cli, kicad_sch


def matrix_rows(state: RunState, design: Design | None, parts: dict[str, Part]) -> list[dict]:
    """Requirement -> parts (ref + MPN) -> rule IDs -> status."""
    ref_mpn = {p.ref: p.mpn for p in design.all_parts()} if design else {}
    rows = []
    for r in sorted(state.requirements.values(), key=lambda r: r["req_id"]):
        refs = r.get("parts", [])
        rows.append({
            "req_id": r["req_id"], "requirement": r.get("text", ""), "status": r.get("status", "pending"),
            "parts": " ".join(f"{x}:{ref_mpn.get(x, '?')}" for x in refs),
            "rule_ids": " ".join(r.get("rule_ids", [])),
        })
    return rows


def matrix_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["req_id", "requirement", "status", "parts", "rule_ids"])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def write_outputs(run_id: str, state: RunState | None = None) -> Path:
    if state is None:
        events, _ = read_events(run_id)
        state = fold(run_id, events)
    design = state.design_obj()
    if design is None:
        raise ValueError(f"run {run_id} has no design yet")
    parts = load_parts()
    out = run_dir(run_id) / "out"
    out.mkdir(parents=True, exist_ok=True)

    sch = kicad_cli.write_project(design, parts, out / "kicad")
    (out / "schematic.svg").write_text(kicad_sch.to_svg(design, parts))
    (out / "bom.csv").write_text(budgets.bom_csv(design, parts))
    (out / "traceability.csv").write_text(matrix_csv(matrix_rows(state, design, parts)))
    (out / "design.json").write_text(json.dumps(design.model_dump(), indent=2))
    (out / "verifiers.json").write_text(json.dumps(state.verifiers, indent=2))
    if kicad_cli.available():
        kicad_cli.export_pdf(sch, out / "schematic.pdf")
        kicad_cli.export_svg(sch, out / "kicad_svg")
    return out
