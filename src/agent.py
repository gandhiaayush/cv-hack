"""Generic agent loop + the mother's delegate tool.

run_agent(): model <-> tools loop with a step cap, every message logged to SQLite.
delegate: a tool the mother (or any agent below max depth) calls to spawn a sub-agent."""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import store
from .llm import chat
from .tools import regs, sandbox

TOOL_FUNCS = {
    "run_python": lambda a: sandbox.run_python(**a),
    "search_regulations": lambda a: regs.search_regulations(**a),
    "get_regulation_section": lambda a: regs.get_regulation_section(**a),
    "classify_device": lambda a: regs.classify_device(**a),
}
LEAF_TOOLS = [sandbox.SPEC, regs.SEARCH_SPEC, regs.SECTION_SPEC, regs.CLASSIFY_SPEC]

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
- Quantitative: use run_python to compute power, voltage margins, cost, thermal numbers. Never do arithmetic in your head.
- Qualitative/regulatory: use classify_device / search_regulations / get_regulation_section and cite section ids.
Then reply with a JSON report only:
{{"subsystem": str, "options": [{{"name": str, "summary": str, "cost": num, "power_mw": num, "risks": [str]}}],
 "quantitative_constraints": [{{"constraint": str, "value": str, "how": str}}],
 "qualitative_constraints": [{{"constraint": str, "source": str}}],
 "conflicts": [str], "needs_from_others": [str]}}"""


def run_agent(role, task, model, tools, system, run, parent=None, depth=0, max_steps=10):
    name = f"{role}:{uuid.uuid4().hex[:4]}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    store.log(run, name, parent, "start", {"task": task, "depth": depth})
    for step in range(max_steps):
        if step == max_steps - 1:  # out of budget: force a final answer from what it has
            msgs.append({"role": "user", "content": "Step budget exhausted. Reply NOW with your final answer "
                         "(JSON report) from what you have learned. Make no more tool calls."})
        m = chat(msgs, model, tools=None if step == max_steps - 1 else tools)
        msgs.append(m.model_dump(exclude_none=True))
        if not m.tool_calls:
            store.log(run, name, parent, "final", m.content)
            return m.content
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
