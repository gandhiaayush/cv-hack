"""Agent loop, delegate, and the two gated exits:

  sub-agent  -> submit_report : stage-1 gate (src/gate.py) validates every option against the parts DB;
                                failures go back to the agent as tool output, so it must revise.
  mother     -> finalize      : recomputes totals from the DB, checks the budget, then a critic on a
                                DIFFERENT model must be answered. Only an ACCEPTED finalize ends the run.

Neither exit is the model's own judgement: the model proposes, code accepts."""
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from cvhack.parts import load_parts

from . import gate, store
from .llm import chat
from .spec import BUDGET, DEVICE, SUBSYSTEMS
from .tools import kg, parts, regs, sandbox

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
    "description": "Spawn a sub-agent to investigate ONE subsystem and return its gated report. Several "
    "delegate calls in one turn run in parallel.",
    "parameters": {"type": "object", "properties": {
        "subsystem": {"type": "string", "enum": list(SUBSYSTEMS)},
        "task": {"type": "string", "description": "scope, budget share, constraints, anything to avoid"},
    }, "required": ["subsystem", "task"]}}}

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

SUB_PROMPT = """You are the {role} subsystem agent for a {device}.
Work in this order:
1. get_subsystem_context: parts already placed, nets, and the design rules (by id) that apply. Respect them.
2. search_parts / find_alternates: you may ONLY use parts that exist in the DB (real, in-stock LCSC parts). Never propose a part from memory.
3. run_python for any number (power, margins, cost). Each call is a fresh process; print() results.
4. regulation tools only if a rule or clause matters; at most 2 lookups.
5. submit_report with 2 options, within about 6 tool calls total. Cite evidence (mpn, rule id, CFR section) for every option.
A deterministic gate checks your options. If it rejects them, read the errors, fix the cause (do not argue),
and resubmit. Be concise; you have a small step budget."""

MOTHER_PROMPT = f"""You are the mother agent designing a {DEVICE}. Budget: {BUDGET['power_mw']:.0f} mW, ${BUDGET['cost_usd']:.0f} BOM.
1. list_subsystems, then delegate ALL subsystems in ONE turn (parallel).
2. Read the gated reports. Options marked gate-failed are invalid. Look for cross-subsystem conflicts
   (budget, voltage rails, a part that makes another subsystem's rule fail) and resolve them by choosing
   options or re-delegating a subsystem with new constraints.
3. Call finalize. Treat your own choice as probably wrong until the checks and the critic say otherwise:
   answer every objection with a change or with evidence. You cannot finish any other way."""

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
    rep = _gate_report(args)
    passing = [o for o in rep["options"] if o["gate"]["ok"]]
    store.log(ctx.run, a["name"], a["parent"], "gate", {"subsystem": sub, "attempt": a["submits"],
              "passed": [o["name"] for o in passing],
              "failed": {o["name"]: [e["code"] + ":" + e.get("part", "") for e in o["gate"]["errors"]
                                     if e["severity"] == "error"] for o in rep["options"] if not o["gate"]["ok"]}})
    if passing or a["submits"] >= MAX_SUBMITS:
        rep["status"] = "ok" if passing else "no_valid_option"
        ctx.reports[sub] = rep
        a["done"] = json.dumps(_brief(rep))
        return "ACCEPTED" if passing else "ACCEPTED (no option passed the gate; reported as a failure)"
    lines = [f"- option '{o['name']}': " + "; ".join(e["message"] for e in o["gate"]["errors"]
             if e["severity"] == "error") for o in rep["options"]]
    return ("REJECTED by the deterministic gate. No option passed:\n" + "\n".join(lines) +
            "\nFix the causes (use search_parts) and resubmit.")


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
    ctx.result = {"status": ("accepted_UNREVIEWED_critic_failed" if not ctx.reviewed else
                          "accepted" if not unresolved else "accepted_with_unresolved_objections"),
                  "selections": {s: o["name"] for s, o in chosen.items()},
                  "bom": [{"subsystem": s, **p} for s, p in items], "totals": totals,
                  "critic_rounds": ctx.critic_rounds, "objections": ctx.objections,
                  "responses": list(responses.values()), "unresolved": unresolved,
                  "rationale": args["rationale"]}
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
    msg = json.dumps({"bom": facts, "totals": totals, "mother_rationale": rationale,
                      "options_chosen": {s: {"name": o["name"], "risks": o.get("risks", []),
                                             "warnings": [e["message"] for e in o["gate"]["errors"]
                                                          if e["severity"] == "warning"]}
                                         for s, o in chosen.items()},
                      "design_rules_in_scope": rules})
    try:
        m = chat([{"role": "system", "content": CRITIC_PROMPT}, {"role": "user", "content": msg}],
                 os.environ.get("CRITIC_MODEL") or os.environ["SUB_MODEL"])
        text = re.sub(r"^```(?:json)?|```$", "", (m.content or "").strip(), flags=re.M).strip()
        objs = json.loads(text[text.index("{"):text.rindex("}") + 1])["objections"]
    except Exception:  # None means "no review happened", which must never read as "no objections"
        return None
    return [{**o, "id": f"R{rnd}-O{i + 1}"} for i, o in enumerate(objs)]


# ------------------------------------------------------------------ the loop
LEAF_TOOLS = [kg.SPEC, parts.SEARCH_SPEC, parts.ALTERNATES_SPEC, sandbox.SPEC, regs.SEARCH_SPEC, regs.SECTION_SPEC, regs.CLASSIFY_SPEC,
              SUBMIT_SPEC]
MOTHER_TOOLS = [DELEGATE_SPEC, kg.LIST_SPEC, FINALIZE_SPEC]

PLAIN = {
    "run_python": lambda a: sandbox.run_python(**a),
    "search_regulations": lambda a: regs.search_regulations(**a),
    "get_regulation_section": lambda a: regs.get_regulation_section(**a),
    "classify_device": lambda a: regs.classify_device(**a),
    "get_subsystem_context": lambda a: kg.get_subsystem_context(**a),
    "list_subsystems": lambda a: kg.list_subsystems(),
    "search_parts": lambda a: parts.search_parts(**a),
    "find_alternates": lambda a: parts.find_alternates(**a),
}

def run_agent(role, task, model, tools, system, ctx: RunCtx, parent=None, depth=0, max_steps=9):
    name = f"{role}:{uuid.uuid4().hex[:4]}"
    a = {"name": name, "parent": parent, "subsystem": role, "submits": 0, "done": None}
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    store.log(ctx.run, name, parent, "start", {"task": task, "depth": depth})
    nudges = 0
    for step in range(max_steps):
        last = step == max_steps - 1
        step_tools = tools
        if step == max_steps - 4:  # wrap-up window: only the finishing tool is offered
            finish = [t for t in tools if t["function"]["name"] in ("submit_report", "finalize")]
            msgs.append({"role": "user", "content": "Research budget used up. Call the finishing tool "
                         f"({finish[0]['function']['name']}) NOW with what you have."})
            step_tools = finish or tools
        elif step > max_steps - 4:
            step_tools = [t for t in tools if t["function"]["name"] in ("submit_report", "finalize")] or tools
        m = chat(msgs, model, tools=step_tools)
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
                if fn == "delegate" and ctx.delegations.get(args["subsystem"], 0) >= 2:
                    out = json.dumps(_brief(ctx.reports[args["subsystem"]])) if args["subsystem"] in ctx.reports \
                        else json.dumps({"subsystem": args["subsystem"], "status": "no_accepted_output"})
                elif fn == "delegate":
                    ctx.delegations[args["subsystem"]] = ctx.delegations.get(args["subsystem"], 0) + 1
                    out = run_agent(args["subsystem"], args["task"], os.environ["SUB_MODEL"], LEAF_TOOLS,
                                    SUB_PROMPT.format(role=args["subsystem"], device=DEVICE), ctx,
                                    parent=name, depth=depth + 1)
                elif fn == "submit_report":
                    out = _submit_report(args, ctx, a)
                elif fn == "finalize":
                    out = _finalize(args, ctx, a)
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
