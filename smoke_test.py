"""python smoke_test.py          -> list models + one plain call + one tool call
   python smoke_test.py mother   -> full gated run: sub-agents -> gate -> mother -> critic -> wiring -> ERC

A real run uses the catalog's dated LCSC prices (PARTS_PRICES=catalog unless set). LINGER=<seconds> keeps the
run listening for dashboard constraint changes after the design is accepted, and re-plans when one arrives."""
import json
import os
import sys
import time
import uuid

os.environ.setdefault("PARTS_PRICES", "catalog")  # before anything loads the parts DB

from src import llm  # noqa: E402
from src.agent import MOTHER_PROMPT, MOTHER_TOOLS, RunCtx, apply_controls, run_agent  # noqa: E402
from src.tools import sandbox  # noqa: E402


def replan_task(note: str, previous: dict | None) -> str:
    prev = json.dumps({k: previous.get(k) for k in ("status", "selections", "totals")}) if previous else "none"
    return (f"{note}\n\nThe previously accepted design was: {prev}\nThe subsystem reports from before are still "
            "available to finalize (re-gated against the change). Re-delegate only what the change broke, then "
            "call finalize.")


if len(sys.argv) == 1:
    print("models:", llm.list_models())
    m = os.environ["SUB_MODEL"]
    print(llm.chat([{"role": "user", "content": "Say hi in 3 words."}], m).content)
    r = llm.chat([{"role": "user", "content": "Use the tool to compute 3.3*0.12*5."}], m, tools=[sandbox.SPEC])
    print("tool call:", r.tool_calls)
else:
    ctx = RunCtx(run=os.environ.get("RUN_ID") or uuid.uuid4().hex[:6])
    print("run id:", ctx.run, flush=True)
    run_agent("mother", "Begin.", os.environ["MOTHER_MODEL"], MOTHER_TOOLS, MOTHER_PROMPT, ctx)
    print(json.dumps(ctx.result, indent=1) if ctx.result else "NO ACCEPTED DESIGN", flush=True)
    linger = float(os.environ.get("LINGER", "0"))
    deadline = time.time() + linger
    while time.time() < deadline:
        previous = ctx.result
        note = apply_controls(ctx, "mother")
        if note:
            run_agent("mother", replan_task(note, previous), os.environ["MOTHER_MODEL"], MOTHER_TOOLS,
                      MOTHER_PROMPT, ctx)
            print(json.dumps(ctx.result, indent=1) if ctx.result else "NO ACCEPTED DESIGN AFTER RE-PLAN", flush=True)
            deadline = time.time() + linger
        time.sleep(2)
