"""Run one design from a prompt:  python -m src.run_prompt <run_id> "<what to design>"
Logs to runs/run.db (the bridge and the dashboard follow it), then distills lessons from the debate and
writes a result row."""
import os
import sys

import threading

from . import baseline, lessons, store
from .agent import MOTHER_TOOLS, RunCtx, _custom_block, _lessons_block, mother_prompt, run_agent
from .runspec import extract


def main(run_id: str, prompt: str) -> None:
    spec = extract(prompt)
    store.log(run_id, "mother", None, "spec", spec)
    threading.Thread(target=baseline.run, args=(run_id, spec), daemon=True).start()  # the skeptic's comparison
    ctx = RunCtx(run=run_id, device=spec["device"], request=spec["request"],
                 budget={"power_mw": spec["power_mw"], "cost_usd": spec["cost_usd"]})
    try:
        run_agent("mother", "Begin.", os.environ["MOTHER_MODEL"], MOTHER_TOOLS,
                  mother_prompt(ctx) + _lessons_block(ctx, None) + _custom_block(), ctx, max_steps=18)
    except Exception as e:  # never leave the UI waiting on a crashed run
        store.log(run_id, "mother", None, "result", {"status": "crashed", "error": f"{type(e).__name__}: {e}"})
        raise
    learned = {}
    try:  # the recursive step: what did this run's debate teach us?
        learned = lessons.distill(run_id)
    except Exception as e:
        learned = {"error": f"{type(e).__name__}: {e}"}
    res = dict(ctx.result or {"status": "no_accepted_design"})
    res.update(lessons_used=ctx.lessons_used, blockers_first_round=ctx.first_round_blockers, learned=learned)
    store.log(run_id, "mother", None, "result", res)


if __name__ == "__main__":
    main(sys.argv[1], " ".join(sys.argv[2:]))
