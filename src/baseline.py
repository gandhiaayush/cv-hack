"""The skeptic's baseline: the same model, one shot, with the WHOLE parts catalog in its context.

This is the strongest version of "just ask ChatGPT with the right integrations": no agents, no debate, no
gate, but full catalog access. Its answer is then graded by the SAME deterministic checks the system uses, so
the comparison is about what the architecture adds, not about data access."""
import json
import os
import re

from cvhack.parts import load_parts

from . import gate, store
from .llm import chat
from .scheduler import SCHED
from .spec import SUBSYSTEMS

SYSTEM = """You are an expert hardware engineer. Answer in ONE reply, no tools, no follow-ups.
Design the board for the request below and list the bill of materials per subsystem
(power, analog_fe, mcu_comms, protection). Use ONLY parts from the catalog provided (mpn column).
Return JSON only: {"power":[{"mpn":"...","qty":1}], "analog_fe":[...], "mcu_comms":[...], "protection":[...]}"""


def _catalog() -> str:
    rows = []
    for p in load_parts().values():
        v = gate.rated_max_v(p)
        rows.append(f"{p.mpn}|{p.category}|{'' if p.price_usd is None else p.price_usd}|{'' if p.stock is None else p.stock}|{'' if v is None else v}")
    return "mpn|category|price_usd|stock|max_voltage_v\n" + "\n".join(rows)


def grade(bom: dict, budget: dict) -> dict:
    parts = load_parts()
    codes: dict[str, int] = {}
    failing, examples, n_parts, unknown = [], [], 0, []
    cost = power = 0.0
    for sub in SUBSYSTEMS:
        items = [{"mpn": str(i.get("mpn", "")), "qty": int(i.get("qty", 1) or 1)} for i in bom.get(sub, []) if isinstance(i, dict)]
        n_parts += len(items)
        res = gate.check_option(sub, items)
        errs = [e for e in res["errors"] if e["severity"] == "error"]
        if errs:
            failing.append(sub)
        for e in errs:
            codes[e["code"]] = codes.get(e["code"], 0) + 1
            if len(examples) < 6:
                examples.append(f"{sub}: {e['message']}")
        cost += res["totals"]["cost_usd"]
        power += res["totals"]["power_mw"]
        unknown += [i["mpn"] for i in items if i["mpn"] not in parts]
    over = []
    if cost > budget["cost_usd"]:
        over.append(f"cost ${cost:.2f} > ${budget['cost_usd']:.0f}")
    if power > budget["power_mw"]:
        over.append(f"power {power:.0f} mW > {budget['power_mw']:.0f} mW")
    return {"parts": n_parts, "unknown_parts": len(unknown), "violations": sum(codes.values()), "by_code": codes,
            "subsystems_failing": failing, "examples": examples, "cost_usd": round(cost, 2),
            "power_mw": round(power, 2), "over_budget": over, "completeness": completeness(bom)}


def run(run_id: str, spec: dict) -> None:
    import time
    t0 = time.time()
    try:
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Request: {spec['request']}\nBudget: ${spec['cost_usd']:.0f}, "
                 f"{spec['power_mw']:.0f} mW\n\nCatalog:\n{_catalog()}"}]
        m = None
        for attempt in range(5):  # shares the request queue with the agents; back off on 429s
            try:
                with SCHED.slot(run_id, "baseline"):
                    m = chat(msgs, os.environ["MOTHER_MODEL"], max_tokens=1800, temperature=0.2,
                             extra_body={"chat_template_kwargs": {"enable_thinking": False}})
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(6 * (attempt + 1))
        j = json.loads(re.search(r"\{.*\}", m.content or "", re.S).group(0))
        store.log(run_id, "baseline", None, "baseline", {**grade(j, {"cost_usd": spec["cost_usd"], "power_mw": spec["power_mw"]}),
                                                         "setup": "same model, one shot, full catalog in context",
                                                         "seconds": round(time.time() - t0, 1)})
    except Exception as e:
        store.log(run_id, "baseline", None, "baseline", {"error": f"{type(e).__name__}: {e}"})


def completeness(bom: dict) -> dict:
    from . import completeness as C
    return C.score(bom)
