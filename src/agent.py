"""Generic agent loop + the mother's delegate tool.

run_agent(): model <-> tools loop with a step cap, every message logged to SQLite.
delegate: a tool the mother (or any agent below max depth) calls to spawn a sub-agent."""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import store
from .llm import chat
from .tools import kg, parts, regs, sandbox

TOOL_FUNCS = {
    "run_python": lambda a: sandbox.run_python(**a),
    "search_regulations": lambda a: regs.search_regulations(**a),
    "get_regulation_section": lambda a: regs.get_regulation_section(**a),
    "classify_device": lambda a: regs.classify_device(**a),
    "get_subsystem_context": lambda a: kg.get_subsystem_context(**a),
    "list_subsystems": lambda a: kg.list_subsystems(),
    "search_parts": lambda a: parts.search_parts(**a),
    "find_alternates": lambda a: parts.find_alternates(**a),
}
LEAF_TOOLS = [kg.SPEC, sandbox.SPEC, regs.SEARCH_SPEC, regs.SECTION_SPEC, regs.CLASSIFY_SPEC,
              parts.SEARCH_SPEC, parts.ALTERNATES_SPEC]

DELEGATE_SPEC = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": "Spawn a sub-agent to investigate ONE subsystem and report back. Several delegate "
        "calls in one turn run in parallel.",
        "parameters": {
            "type": "object",
            "properties": {
                "subsystem": {"type": "string"},
                "task": {"type": "string", "description": "scope, budget share, constraints to check"},
            },
            "required": ["subsystem", "task"],
        },
    },
}

SUB_PROMPT = """You are the {role} subsystem agent in a medical-device PCB design team.
Investigate the constraints that touch your subsystem BEFORE proposing anything:
- FIRST call get_subsystem_context for your subsystem: it returns the parts already placed, the nets, and the design rules (by id) that apply. Your options must respect those rules; cite rule ids.
- Quantitative: use run_python to compute power, voltage margins, cost, thermal numbers. Never do arithmetic in your head.
- Qualitative/regulatory: use classify_device / search_regulations / get_regulation_section and cite section ids.
- Parts: use search_parts / find_alternates to pick real, in-stock MPNs; quote their price and stock date.
Then reply with a JSON report only:
{{"subsystem": str, "options": [{{"name": str, "summary": str, "cost": num, "power_mw": num, "risks": [str]}}],
 "quantitative_constraints": [{{"constraint": str, "value": str, "how": str}}],
 "qualitative_constraints": [{{"constraint": str, "source": str}}],
 "conflicts": [str], "needs_from_others": [str]}}"""


def run_agent(role, task, model, tools, system, run, parent=None, depth=0, max_steps=10):
    name = f"{role}:{uuid.uuid4().hex[:4]}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    store.log(run, name, parent, "start", {"task": task, "depth": depth})
    nudges = 0
    for step in range(max_steps):
        if step == max_steps - 1:  # out of budget: force a final answer from what it has
            msgs.append({"role": "user", "content": "Step budget exhausted. Reply NOW with your final answer "
                         "(JSON report) from what you have learned. Make no more tool calls."})
        m = chat(msgs, model, tools=None if step == max_steps - 1 else tools)
        msgs.append(m.model_dump(exclude_none=True))
        if not m.tool_calls:
            text = (m.content or "").strip()
            bad = not text or "<tool_call>" in text or "<function=" in text
            if bad and nudges < 2 and step < max_steps - 1:
                # model leaked a text-format tool call or answered with nothing: ask again
                nudges += 1
                store.log(run, name, parent, "retry", {"reason": "empty or text-format tool call"})
                msgs.pop()
                msgs.append({"role": "user", "content": "Your last reply was empty or malformed. Use the "
                             "function-calling interface for tools, or reply with your final answer now."})
                continue
            store.log(run, name, parent, "final", m.content)
            return m.content or ""
        calls = m.tool_calls

        def exec_call(tc):
            args = json.loads(tc.function.arguments or "{}")
            store.log(run, name, parent, "tool_call", {"tool": tc.function.name, "args": args})
            if tc.function.name == "delegate":
                out = run_agent(
                    args["subsystem"], args["task"], os.environ["SUB_MODEL"], LEAF_TOOLS,
                    SUB_PROMPT.format(role=args["subsystem"]), run, parent=name, depth=depth + 1,
                )
            else:
                try:
                    out = TOOL_FUNCS[tc.function.name](args)
                except Exception as e:  # tool errors go back to the model, not up the stack
                    out = f"ERROR: {e}"
            store.log(run, name, parent, "tool_result", {"tool": tc.function.name, "result": str(out)[:1500]})
            return {"role": "tool", "tool_call_id": tc.id, "content": str(out)}

        with ThreadPoolExecutor(max_workers=max(1, len(calls))) as ex:
            msgs.extend(ex.map(exec_call, calls))
    store.log(run, name, parent, "final", "STEP LIMIT")
    return "STEP LIMIT"
