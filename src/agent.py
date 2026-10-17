"""Agent loop, delegate, and the two gated exits:

  sub-agent  -> submit_report : stage-1 gate (src/gate.py) validates every option against the parts DB;
                                failures go back to the agent as tool output, so it must revise.
  mother     -> finalize      : recomputes totals from the DB, checks the budget, then a critic on a
                                DIFFERENT model must be answered. Only an ACCEPTED finalize ends the run.

Neither exit is the model's own judgement: the model proposes, code accepts."""
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from cvhack.control import read_commands
from cvhack.parts import load_parts

from . import gate, llm, store, wiring
from .llm import chat
from .scheduler import SCHED
from .spec import BUDGET, DEVICE, DEVICE_CLASS, SUBSYSTEMS
from .tools import kg, parts, regs, sandbox, supply

MAX_SUBMITS = 3
MAX_CRITIC_ROUNDS = 2


@dataclass
class RunCtx:
    run: str
    reports: dict = field(default_factory=dict)  # subsystem -> gated report
    critic_rounds: int = 0
    delegations: dict = field(default_factory=dict)
    critic_failures: int = 0
    reviewed: bool = False
    objections: list = field(default_factory=list)
    result: dict | None = None
    agents_spawned: int = 0
    findings: list = field(default_factory=list)      # regulatory / sourcing findings that passed their gate
    fetched: dict = field(default_factory=dict)       # CFR section -> text actually returned by a tool this run
    wiring: dict = field(default_factory=dict)        # subsystem -> {insts, block, ncs, ok, errors} (src/wiring.py)
    ctrl_offset: int = 0                              # lines of runs/<run>/control.jsonl already applied
    device_class: str = DEVICE_CLASS
    lock: threading.Lock = field(default_factory=threading.Lock)


MAX_AGENTS = int(os.environ.get("MAX_AGENTS", "24"))  # total spawned per run (a different cap from concurrency)
STRATEGIES = {
    "lowest_cost": "Your bias: minimise BOM cost with parts that still satisfy every rule and rating.",
    "lowest_power": "Your bias: minimise power consumption (battery life) within the rules and ratings.",
    "robust_sourcing": "Your bias: prefer parts with deep stock and known alternates (use find_alternates); avoid "
                       "single-source parts.",
    "medical_conservative": "Your bias: a skin-contact Class II device. Prefer generous voltage/current margins, "
                            "ESD protection, and parts whose ratings are known; avoid unknown ratings.",
}


# ---------------------------------------------------------------- tool specs
_OPTION = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "parts": {"type": "array", "items": {"type": "object", "properties": {
            "mpn": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["mpn"]}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "rule_ids": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "sources: DB mpn, rule id, or 21 CFR section you relied on"},
    },
    "required": ["name", "parts"],
}
SUBMIT_SPEC = {"type": "function", "function": {
    "name": "submit_report",
    "description": "Submit your final report. A deterministic gate checks every option against the parts DB "
    "(part exists, category fits, voltage ratings cover the rail, stock). If NO option passes you get the "
    "errors back and must revise and resubmit. This is the ONLY way to finish.",
    "parameters": {"type": "object", "properties": {
        "subsystem": {"type": "string"},
        "options": {"type": "array", "items": _OPTION},
        "quantitative_constraints": {"type": "array", "items": {"type": "string"}},
        "qualitative_constraints": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "needs_from_others": {"type": "array", "items": {"type": "string"}},
    }, "required": ["subsystem", "options"]}}}

DELEGATE_SPEC = {"type": "function", "function": {
    "name": "delegate",
    "description": "Spawn a sub-agent. Types: 'specialist' (designs ONE subsystem and returns a gated report; "
    "give a `strategy` to run several explorers on the same subsystem), 'regulatory' (researches the FDA "
    "classification and applicable CFR sections, citing only sections it fetched), 'sourcing' (checks stock "
    "and alternates for parts you name). Several delegate calls in one turn run in parallel; a queue caps "
    "concurrent model calls, so over-delegating only waits.",
    "parameters": {"type": "object", "properties": {
        "agent_type": {"type": "string", "enum": ["specialist", "regulatory", "sourcing"]},
        "subsystem": {"type": "string", "enum": list(SUBSYSTEMS)},
        "strategy": {"type": "string", "enum": list(STRATEGIES)},
        "task": {"type": "string", "description": "scope, budget share, constraints, anything to avoid"},
    }, "required": ["task"]}}}

BATCH_SPEC = {"type": "function", "function": {
    "name": "delegate_batch",
    "description": "Spawn many sub-agents in ONE call (max 12). Each item is a delegate request. They all run in "
    "parallel through the queue and you get every result back together. Prefer this to many separate delegate calls.",
    "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": DELEGATE_SPEC["function"]["parameters"]["properties"],
        "required": ["task"]}}}, "required": ["items"]}}}

_ITEM = {"type": "object", "properties": {
    "claim": {"type": "string"},
    "source": {"type": "string", "description": "regulatory: the CFR section id you fetched, e.g. 870.2700"},
    "quote": {"type": "string", "description": "regulatory: a verbatim sentence from that section"},
    "mpn": {"type": "string", "description": "sourcing: the part this finding is about"},
    "severity": {"type": "string", "enum": ["info", "risk"]},
}, "required": ["claim"]}
FINDINGS_SPEC = {"type": "function", "function": {
    "name": "submit_findings",
    "description": "Submit your findings. Regulatory items need `source` = a CFR section you fetched this run and "
    "ideally a verbatim `quote`; items citing sections you never fetched, or quotes not in the text, are "
    "rejected. Sourcing items must name an MPN that exists in the DB. This is the ONLY way to finish.",
    "parameters": {"type": "object", "properties": {
        "summary": {"type": "string"}, "items": {"type": "array", "items": _ITEM}},
        "required": ["summary", "items"]}}}

FINALIZE_SPEC = {"type": "function", "function": {
    "name": "finalize",
    "description": "Propose the final design: one gate-passing option per subsystem. Code recomputes cost and "
    "power from the DB and checks the budget; a critic then reviews it. If objections come back, call "
    "finalize again with `responses` to EVERY objection id (accept = you changed the selection, "
    "rebut = give evidence). This is the ONLY way to finish.",
    "parameters": {"type": "object", "properties": {
        "selections": {"type": "array", "items": {"type": "object", "properties": {
            "subsystem": {"type": "string"}, "option": {"type": "string"}}, "required": ["subsystem", "option"]}},
        "rationale": {"type": "string"},
        "responses": {"type": "array", "items": {"type": "object", "properties": {
            "objection_id": {"type": "string"}, "stance": {"type": "string", "enum": ["accept", "rebut"]},
            "evidence": {"type": "string"}}, "required": ["objection_id", "stance"]}},
    }, "required": ["selections", "rationale"]}}}

REG_PROMPT = """You are the regulatory researcher for a {device}.
1. classify_device to find the FDA class, product code and regulation number.
2. search_regulations / get_regulation_section to READ the sections that matter (at most 4 fetches).
3. submit_findings: 3 to 6 concrete design-relevant constraints. Each item must cite a section you fetched and
   quote a sentence from it verbatim. If you cannot support a claim, leave it out. Do not recite regulations you
   did not read. Be concise."""

SOURCING_PROMPT = """You are the sourcing agent for a {device}.
You are given parts to check. Call check_supply on them (and find_alternates for any single-source or low-stock
part), then submit_findings: one item per real supply risk (low stock, no alternates, price unknown), naming the
mpn. Report only what the tools returned. Be concise."""

SUB_PROMPT = """You are the {role} subsystem agent for a {device}. {strategy_line}
Work in this order:
1. get_subsystem_context: parts already placed, nets, and the design rules (by id) that apply. Respect them.
2. search_parts: BROWSE the DB by category with no text (e.g. category="mcu", limit 15) and choose from what it lists. Do NOT search for part names you remember: the DB is a fixed ~500-part catalog and most names are absent. Valid categories for {role}: {cats}. Use find_alternates after a stock-out.
3. run_python for any number (power, margins, cost). Each call is a fresh process; print() results.
4. regulation tools only if a rule or clause matters; at most 2 lookups. get_part_medical gives a part's lifecycle, datasheet medical-use statements and isolation (MOPP) evidence; use it for parts at a patient or USB barrier.
5. If your subsystem's key category has only ONE suitable part, build both options around it (they may differ in supporting parts and values). Do not hunt across unrelated categories.
6. submit_report with 2 options, within about 6 tool calls total. Cite evidence (mpn, rule id, CFR section) for every option.
A deterministic gate checks your options. If it rejects them, read the errors, fix the cause (do not argue),
and resubmit. Be concise; you have a small step budget."""

MOTHER_PROMPT = f"""You are the mother agent designing a {DEVICE}. Budget: {BUDGET['power_mw']:.0f} mW, ${BUDGET['cost_usd']:.0f} BOM.
1. Call list_subsystems. Then make ONE delegate_batch call with EXACTLY these 9 items (a queue runs at most {{conc}}
   model calls at once, so do not hold back):
   - agent_type=regulatory (once, no subsystem)
   - agent_type=specialist, strategy=lowest_cost, for each of: power, analog_fe, mcu_comms, protection   (4 calls)
   - agent_type=specialist, strategy=robust_sourcing, for each of the same four subsystems               (4 calls)
   Give each a short task naming the device and its budget share. Do not delegate anything else before the batch returns.
2. Read the gated reports and the regulatory findings. Explorer options are prefixed with their strategy: pick the
   best per subsystem, mixing strategies where it helps. Optionally delegate a 'sourcing' agent on your shortlist. Options marked gate-failed are invalid. Look for cross-subsystem conflicts
   (budget, voltage rails, a part that makes another subsystem's rule fail) and resolve them by choosing
   options or re-delegating a subsystem with new constraints.
3. Call finalize. Treat your own choice as probably wrong until the checks and the critic say otherwise:
   answer every objection with a change or with evidence. You cannot finish any other way."""
MOTHER_PROMPT = MOTHER_PROMPT.replace("{conc}", str(SCHED.n))

CRITIC_PROMPT = f"""You are an adversarial design reviewer for a {DEVICE}. Assume the design has a mistake and
find it: wrong part for the function, voltage/current margin, missing protection, budget games, ignored rule,
regulatory gap, supply risk. Use only the facts given. Return JSON only:
{{"objections":[{{"id":"O1","severity":"blocker|concern","subsystem":str,"part":str,"issue":str,"evidence":str}}]}}
Return an empty list only if you genuinely find nothing. Do not praise."""


# ------------------------------------------------------------------ handlers
def _gate_report(args: dict) -> dict:
    sub = args["subsystem"]
    for o in args["options"]:
        res = gate.check_option(sub, [{"mpn": p["mpn"], "qty": p.get("qty", 1)} for p in o.get("parts", [])])
        o["gate"] = res
    return args


def _submit_report(args, ctx: RunCtx, a: dict):
    sub = a["subsystem"]  # the delegated role, not whatever the model typed
    args["subsystem"] = sub
    a["submits"] += 1
    if a.get("strategy"):  # explorers share a subsystem: keep option names unique and attributable
        for o in args["options"]:
            o["name"] = f"[{a['strategy']}] {o['name']}"
    rep = _gate_report(args)
    passing = [o for o in rep["options"] if o["gate"]["ok"]]
    store.log(ctx.run, a["name"], a["parent"], "gate", {"subsystem": sub, "attempt": a["submits"],
              "passed": [o["name"] for o in passing],
              "failed": {o["name"]: [e["code"] + ":" + e.get("part", "") for e in o["gate"]["errors"]
                                     if e["severity"] == "error"] for o in rep["options"] if not o["gate"]["ok"]}})
    if passing or a["submits"] >= MAX_SUBMITS:
        rep["status"] = "ok" if passing else "no_valid_option"
        with ctx.lock:
            prev = ctx.reports.get(sub)
            ctx.reports[sub] = rep if not prev else {
                **rep, "options": prev["options"] + rep["options"],
                "status": "ok" if "ok" in (prev["status"], rep["status"]) else "no_valid_option"}
        a["done"] = json.dumps(_brief(rep))
        return "ACCEPTED" if passing else "ACCEPTED (no option passed the gate; reported as a failure)"
    lines = [f"- option '{o['name']}': " + "; ".join(e["message"] for e in o["gate"]["errors"]
             if e["severity"] == "error") for o in rep["options"]]
    return ("REJECTED by the deterministic gate. No option passed:\n" + "\n".join(lines) +
            "\nFix the causes (use search_parts) and resubmit.")


def _norm_section(x: str) -> str:
    return re.sub(r"^(21\s*CFR|§)\s*", "", x.replace("§", "").strip(), flags=re.I).strip()


def _squash(t: str) -> str:
    return re.sub(r"\s+", " ", t).lower()


def _submit_findings(args, ctx: RunCtx, a: dict):
    a["submits"] += 1
    kind, good, bad = a["agent_type"], [], []
    for it in args.get("items", []):
        if kind == "regulatory":
            src = _norm_section(it.get("source", ""))
            text = ctx.fetched.get(src)
            if text is None:
                bad.append(f"'{it.get('claim', '')[:60]}': cited section '{it.get('source')}' was never fetched this run "
                           f"(fetched: {sorted(ctx.fetched) or 'none'})")
            elif it.get("quote") and _squash(it["quote"]) not in _squash(text):
                bad.append(f"'{it.get('claim', '')[:60]}': quote not found in section {src}")
            else:
                good.append({**it, "source": src, "verified": "quote" if it.get("quote") else "section_fetched"})
        else:
            if it.get("mpn") and it["mpn"] not in load_parts():
                bad.append(f"'{it.get('claim', '')[:60]}': {it['mpn']} is not in the parts DB")
            else:
                good.append(it)
    store.log(ctx.run, a["name"], a["parent"], "findings_gate",
              {"type": kind, "attempt": a["submits"], "accepted": len(good), "rejected": bad})
    if bad and a["submits"] < 2:
        return "REJECTED " + str(len(bad)) + " item(s); fix or drop them and resubmit:\n" + "\n".join(bad)
    rec = {"type": kind, "summary": args.get("summary", ""), "items": good, "dropped": len(bad)}
    with ctx.lock:
        ctx.findings.append(rec)
    a["done"] = json.dumps(rec)
    return "ACCEPTED" + (f" ({len(bad)} unsupported item(s) dropped)" if bad else "")


def _brief(rep: dict) -> dict:
    return {"subsystem": rep["subsystem"], "status": rep["status"],
            "options": [{"name": o["name"], "summary": o.get("summary", ""),
                         "parts": o["parts"], "risks": o.get("risks", []), "rule_ids": o.get("rule_ids", []),
                         "gate_ok": o["gate"]["ok"], "totals": o["gate"]["totals"],
                         "gate_errors": [f"{e['code']} {e.get('part', '')}" for e in o["gate"]["errors"]]}
                        for o in rep["options"]],
            "conflicts": rep.get("conflicts", []), "needs_from_others": rep.get("needs_from_others", [])}


def _finalize(args, ctx: RunCtx, a: dict):
    sel = {s["subsystem"]: s["option"] for s in args["selections"]}
    problems, chosen = [], {}
    for sub in SUBSYSTEMS:
        rep = ctx.reports.get(sub)
        if not rep:
            problems.append(f"{sub}: no report yet (delegate it)")
            continue
        opt = next((o for o in rep["options"] if o["name"] == sel.get(sub)), None)
        if opt is None:
            problems.append(f"{sub}: option '{sel.get(sub)}' not found; choose from "
                            f"{[o['name'] for o in rep['options']]}")
        elif not opt["gate"]["ok"]:
            problems.append(f"{sub}: option '{opt['name']}' failed the gate")
        else:
            # re-check now: stock or constraints may have changed since the report was gated
            regate = gate.check_option(sub, [{"mpn": p["mpn"], "qty": p.get("qty", 1)} for p in opt["parts"]])
            if not regate["ok"]:
                opt["gate"] = regate
                problems.append(f"{sub}: option '{opt['name']}' no longer passes the gate: " + "; ".join(
                    e["message"] for e in regate["errors"] if e["severity"] == "error"))
            else:
                chosen[sub] = opt
    if problems:
        return "REJECTED: " + " | ".join(problems)
    parts = load_parts()
    items = [(sub, p) for sub, o in chosen.items() for p in o["parts"]]
    cost = sum((parts[p["mpn"]].price_usd or 0.0) * p.get("qty", 1) for _, p in items)
    power = sum((parts[p["mpn"]].power_mw or 0.0) * p.get("qty", 1) for _, p in items)
    unknown = [p["mpn"] for _, p in items if parts[p["mpn"]].price_usd is None]
    totals = {"cost_usd": round(cost, 3), "power_mw": round(power, 3), "unknown_price_parts": unknown,
              "budget": BUDGET}
    store.log(ctx.run, a["name"], a["parent"], "budget_check", totals)
    if cost > BUDGET["cost_usd"] or power > BUDGET["power_mw"]:
        return f"REJECTED: over budget (DB-computed) {totals}. Choose cheaper/lower-power options."

    responses = {r["objection_id"]: r for r in args.get("responses", [])}
    open_objs = [o for o in ctx.objections if o["id"] not in responses]
    if open_objs and ctx.critic_rounds < MAX_CRITIC_ROUNDS:
        return ("REJECTED: answer EVERY open objection id via `responses` before finalizing: "
                + ", ".join(o["id"] for o in open_objs))
    if ctx.critic_rounds < MAX_CRITIC_ROUNDS:  # first review, or re-review after the mother answered all
        objs = _critic(ctx, chosen, totals, args["rationale"])
        if objs is None:
            ctx.critic_failures += 1
            store.log(ctx.run, "critic", a["name"], "critique_failed", {"failures": ctx.critic_failures})
            if ctx.critic_failures < 3:
                return "REJECTED: the reviewer was unavailable (not a verdict). Call finalize again."
        else:
            ctx.reviewed = True
            ctx.critic_rounds += 1
            ctx.objections = objs
        store.log(ctx.run, "critic", a["name"], "critique",
                  {"round": ctx.critic_rounds, "objections": ctx.objections})
        if objs and ctx.objections:
            return ("REJECTED by the critic (round %d). Answer EVERY objection id via `responses` "
                    "(accept = change your selection, rebut = cite evidence) and call finalize again:\n%s"
                    % (ctx.critic_rounds, json.dumps(ctx.objections, indent=1)))
    unresolved = [o for o in ctx.objections if o["id"] not in responses]
    # "answered" is not "resolved": a rebuttal is free text nobody has verified, so contested blockers are reported
    contested = [o["id"] for o in ctx.objections if o.get("severity") == "blocker"
                 and responses.get(o["id"], {}).get("stance") == "rebut"]
    ctx.result = {"status": ("accepted_UNREVIEWED_critic_failed" if not ctx.reviewed else
                          "accepted_with_unresolved_objections" if unresolved else
                          "accepted_with_contested_blockers" if contested else "accepted"),
                  "contested_blockers": contested,
                  "selections": {s: o["name"] for s, o in chosen.items()},
                  "bom": [{"subsystem": s, **p} for s, p in items], "totals": totals,
                  "critic_rounds": ctx.critic_rounds, "objections": ctx.objections,
                  "responses": list(responses.values()), "unresolved": unresolved,
                  "rationale": args["rationale"], "device_class": ctx.device_class}
    try:
        ctx.result["wiring"] = wiring.wire(ctx, chosen, parent=a["name"])
    except Exception as e:  # a wiring crash must not lose the accepted selection
        store.log(ctx.run, a["name"], a["parent"], "llm_error", {"error": f"wiring: {type(e).__name__}: {e}"})
        ctx.result["wiring"] = {"electrical_ok": False, "error": f"{type(e).__name__}: {e}"}
    a["done"] = json.dumps(ctx.result)
    return "ACCEPTED"


def _critic(ctx: RunCtx, chosen: dict, totals: dict, rationale: str) -> list:
    rnd = ctx.critic_rounds
    facts = []
    for sub, o in chosen.items():
        for p in o["parts"]:
            pt = load_parts()[p["mpn"]]
            facts.append({"subsystem": sub, "mpn": p["mpn"], "qty": p.get("qty", 1), "category": pt.category,
                          "description": pt.description, "price_usd": pt.price_usd, "stock": pt.stock,
                          "power_mw": pt.power_mw, "max_voltage_v": gate.rated_max_v(pt)})
    rules = {s: kg.rules_for(s) for s in chosen}
    msg = json.dumps({"device_class": ctx.device_class, "bom": facts, "totals": totals, "mother_rationale": rationale,
                      "options_chosen": {s: {"name": o["name"], "risks": o.get("risks", []),
                                             "warnings": [e["message"] for e in o["gate"]["errors"]
                                                          if e["severity"] == "warning"]}
                                         for s, o in chosen.items()},
                      "design_rules_in_scope": rules,
                      "regulatory_and_sourcing_findings": ctx.findings})
    try:
        with SCHED.slot(ctx.run, "critic"):
            m = chat([{"role": "system", "content": CRITIC_PROMPT}, {"role": "user", "content": msg}],
                     os.environ.get("CRITIC_MODEL") or os.environ["SUB_MODEL"])
            store.log(ctx.run, "critic", None, "llm_call", llm.last_call())
        text = re.sub(r"^```(?:json)?|```$", "", (m.content or "").strip(), flags=re.M).strip()
        objs = json.loads(text[text.index("{"):text.rindex("}") + 1])["objections"]
    except Exception:  # None means "no review happened", which must never read as "no objections"
        return None
    return [{**o, "id": f"R{rnd}-O{i + 1}"} for i, o in enumerate(objs)]


# ------------------------------------------------------------------ the loop
LEAF_TOOLS = [kg.SPEC, parts.SEARCH_SPEC, parts.ALTERNATES_SPEC, parts.MEDICAL_SPEC, sandbox.SPEC, regs.SEARCH_SPEC,
              regs.SECTION_SPEC, regs.CLASSIFY_SPEC, SUBMIT_SPEC]
REG_TOOLS = [regs.CLASSIFY_SPEC, regs.SEARCH_SPEC, regs.SECTION_SPEC, parts.MEDICAL_SPEC, FINDINGS_SPEC]
SOURCING_TOOLS = [supply.SPEC, parts.ALTERNATES_SPEC, FINDINGS_SPEC]
MOTHER_TOOLS = [DELEGATE_SPEC, BATCH_SPEC, kg.LIST_SPEC, FINALIZE_SPEC]
FINISHERS = ("submit_report", "submit_findings", "finalize", "submit_wiring")

def _search_parts(a: dict) -> str:
    """Models send spec_ranges as a JSON string and numbers as strings; coerce instead of crashing."""
    a = dict(a)
    if isinstance(a.get("spec_ranges"), str):
        try:
            a["spec_ranges"] = json.loads(a["spec_ranges"])
        except ValueError:
            a.pop("spec_ranges")
    if isinstance(a.get("limit"), str):
        a["limit"] = int(a["limit"]) if a["limit"].isdigit() else 10
    if isinstance(a.get("needs_pins"), str):
        a["needs_pins"] = a["needs_pins"].lower() == "true"
    if isinstance(a.get("mopp_at_least"), str):
        a["mopp_at_least"] = int(a["mopp_at_least"]) if a["mopp_at_least"].isdigit() else None
    return parts.search_parts(**a)


PLAIN = {
    "run_python": lambda a: sandbox.run_python(**a),
    "search_regulations": lambda a: regs.search_regulations(**a),
    "classify_device": lambda a: regs.classify_device(**a),
    "get_subsystem_context": lambda a: kg.get_subsystem_context(**a),
    "list_subsystems": lambda a: kg.list_subsystems(),
    "search_parts": _search_parts,
    "find_alternates": lambda a: parts.find_alternates(**a),
    "check_supply": lambda a: supply.check_supply(**a),
    "get_part_medical": lambda a: parts.get_part_medical(**a),
}


def _delegate(args, ctx: RunCtx, parent: str, depth: int) -> str:
    typ, sub, strat = args.get("agent_type", "specialist"), args.get("subsystem"), args.get("strategy")
    if typ == "specialist" and sub not in SUBSYSTEMS:
        return f"ERROR: specialist needs a subsystem from {list(SUBSYSTEMS)}"
    key = (typ, sub, strat)
    with ctx.lock:
        if ctx.delegations.get(key, 0) >= (1 if typ == "regulatory" else 2) or ctx.agents_spawned >= MAX_AGENTS:
            note = {"status": "not_spawned", "reason": f"cap reached (max {MAX_AGENTS} agents per run, 2 per "
                                                       "type/subsystem/strategy)"}
            if typ == "specialist" and sub in ctx.reports:
                note["existing_report"] = _brief(ctx.reports[sub])
            return json.dumps(note)
        ctx.delegations[key] = ctx.delegations.get(key, 0) + 1
        ctx.agents_spawned += 1
    model = os.environ["SUB_MODEL"]
    if typ == "regulatory":
        role, tools, system = "regulatory", REG_TOOLS, REG_PROMPT.format(device=DEVICE)
    elif typ == "sourcing":
        role, tools, system = "sourcing", SOURCING_TOOLS, SOURCING_PROMPT.format(device=DEVICE)
    else:
        role, tools = sub, LEAF_TOOLS
        system = SUB_PROMPT.format(role=sub, device=DEVICE, strategy_line=STRATEGIES.get(strat, ""),
                                   cats=", ".join(sorted(SUBSYSTEMS[sub]["allowed"])))
    return run_agent(role, args["task"], model, tools, system, ctx, parent=parent, depth=depth + 1,
                     agent_type=typ, strategy=strat)


def run_agent(role, task, model, tools, system, ctx: RunCtx, parent=None, depth=0, max_steps=9,
              agent_type="specialist", strategy=None):
    name = f"{role}:{uuid.uuid4().hex[:4]}"
    a = {"name": name, "parent": parent, "subsystem": role, "submits": 0, "done": None,
         "agent_type": agent_type, "strategy": strategy}
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    store.log(ctx.run, name, parent, "start", {"task": task, "depth": depth, "agent_type": agent_type,
                                               "strategy": strategy})
    nudges = 0
    for step in range(max_steps):
        if depth == 0:  # the mother picks up dashboard constraint changes between steps
            note = apply_controls(ctx, name)
            if note:
                msgs.append({"role": "user", "content": note})
        last = step == max_steps - 1
        step_tools = tools
        finish = [t for t in tools if t["function"]["name"] in FINISHERS]
        if step == max_steps - 4:  # wrap-up window: only the finishing tool is offered
            msgs.append({"role": "user", "content": "Research budget used up. Call the finishing tool "
                         f"({finish[0]['function']['name']}) NOW with what you have."})
            step_tools = finish or tools
        elif step > max_steps - 4:
            step_tools = finish or tools
        m = None
        for attempt in range(3):  # transient API failures (timeouts, 429s) must not kill the run
            try:
                kw = {}
                if step >= max_steps - 2 and len(step_tools) == 1:  # force the exit tool instead of hoping
                    kw["tool_choice"] = {"type": "function", "function": {"name": step_tools[0]["function"]["name"]}}
                with SCHED.slot(ctx.run, name):  # the queue: at most MAX_CONCURRENT model calls in flight
                    try:
                        m = chat(msgs, model, tools=step_tools, **kw)
                    except Exception:
                        if not kw:
                            raise
                        m = chat(msgs, model, tools=step_tools)
                store.log(ctx.run, name, parent, "llm_call", llm.last_call())
                break
            except Exception as e:
                store.log(ctx.run, name, parent, "llm_error", {"attempt": attempt + 1, "error": type(e).__name__})
                time.sleep(5 * (attempt + 1))
        if m is None:
            break
        msgs.append(m.model_dump(exclude_none=True))
        if not m.tool_calls:
            if nudges < 3 and not last:
                nudges += 1  # text answer / leaked text-format tool call / empty: only the exit tool ends us
                store.log(ctx.run, name, parent, "retry", {"reason": "no tool call"})
                msgs.pop()
                msgs.append({"role": "user", "content": "Use the function-calling interface. Finish only by "
                             "calling the finishing tool."})
                continue
            break

        def exec_call(tc):
            args = json.loads(tc.function.arguments or "{}")
            fn = tc.function.name
            store.log(ctx.run, name, parent, "tool_call", {"tool": fn, "args": args})
            try:
                if fn == "delegate":
                    out = _delegate(args, ctx, name, depth)
                elif fn == "delegate_batch":
                    items = args.get("items", [])[:12]
                    with ThreadPoolExecutor(max_workers=max(1, len(items))) as bex:
                        out = json.dumps(list(bex.map(lambda it: _delegate(it, ctx, name, depth), items)))
                elif fn == "submit_report":
                    out = _submit_report(args, ctx, a)
                elif fn == "submit_findings":
                    out = _submit_findings(args, ctx, a)
                elif fn == "finalize":
                    out = _finalize(args, ctx, a)
                elif fn == "submit_wiring":
                    out = wiring.submit_wiring(args, ctx, a)
                elif fn == "get_regulation_section":
                    out = regs.get_regulation_section(**args)
                    if not str(out).startswith("ERROR"):  # provenance: what this run has really read
                        sec = _norm_section(args.get("section", ""))
                        sec = sec if "." in sec else f"{args.get('part', '')}.{sec}"
                        ctx.fetched[sec] = out
                else:
                    out = PLAIN[fn](args)
            except Exception as e:  # tool errors go back to the model, not up the stack
                out = f"ERROR: {type(e).__name__}: {e}"
            store.log(ctx.run, name, parent, "tool_result", {"tool": fn, "result": str(out)[:1500]})
            return {"role": "tool", "tool_call_id": tc.id, "content": str(out)}

        with ThreadPoolExecutor(max_workers=max(1, len(m.tool_calls))) as ex:
            msgs.extend(ex.map(exec_call, m.tool_calls))
        if a["done"]:
            store.log(ctx.run, name, parent, "final", json.loads(a["done"]))
            return a["done"]
    store.log(ctx.run, name, parent, "final", "NO_ACCEPTED_OUTPUT")
    return json.dumps({"subsystem": role, "status": "no_accepted_output"})


# ------------------------------------------------------------------ operator control channel
def apply_controls(ctx: RunCtx, agent_name: str) -> str | None:
    """Apply dashboard commands (runs/<run>/control.jsonl) and return a note for the mother, or None.

    out_of_stock: the part fails the gate from now on; every report option using it is re-gated.
    device_class: recorded on the run (the critic sees it); the mother is told to re-explore conservatively."""
    cmds, ctx.ctrl_offset = read_commands(ctx.run, ctx.ctrl_offset)
    notes = []
    for c in cmds:
        p = dict(c.payload)
        typ = p.get("type")
        if typ == "out_of_stock" and p.get("mpn"):
            mpn = p["mpn"]
            gate.OUT_OF_STOCK.add(mpn)
            hit, dead = [], []
            with ctx.lock:
                for sub, rep in ctx.reports.items():
                    for o in rep["options"]:
                        if any(x.get("mpn") == mpn for x in o.get("parts", [])):
                            o["gate"] = gate.check_option(sub, [{"mpn": x["mpn"], "qty": x.get("qty", 1)}
                                                                for x in o["parts"]])
                            hit.append(o["name"])
                    if not any(o["gate"]["ok"] for o in rep["options"]):
                        rep["status"] = "no_valid_option"
                        dead.append(sub)
            notes.append(f"{mpn} is now OUT OF STOCK. Options that used it now fail the gate: {hit or 'none'}. "
                         + (f"Subsystems left with no passing option: {dead}; re-delegate each one (agent_type="
                            f"specialist, strategy=robust_sourcing) telling it {mpn} is unavailable. " if dead else "")
                         + "Then call finalize again with passing options.")
        elif typ == "device_class" and p.get("value"):
            ctx.device_class = str(p["value"])
            notes.append(f"Device class raised to {ctx.device_class}: treat this as a higher-risk device. Re-delegate "
                         "the analog_fe and protection specialists with strategy=medical_conservative (ask them to "
                         "check get_part_medical for isolation and medical-use restrictions), then call finalize "
                         "again. The critic will review against the new class.")
        else:
            notes.append(f"Constraint change: {json.dumps(p)}")
        store.log(ctx.run, agent_name, None, "constraint_change", p)
    if not notes:
        return None
    with ctx.lock:
        ctx.result, ctx.critic_rounds, ctx.objections, ctx.reviewed = None, 0, [], False
    return "CONSTRAINT CHANGE from the operator:\n- " + "\n- ".join(notes)
