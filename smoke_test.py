"""python smoke_test.py          -> list models + one plain call + one tool call
   python smoke_test.py mother   -> full gated negotiation (sub-agents -> gate -> mother -> critic)"""
import json
import os
import sys
import uuid

from src import llm
from src.agent import MOTHER_PROMPT, MOTHER_TOOLS, RunCtx, run_agent
from src.tools import sandbox

if len(sys.argv) == 1:
    print("models:", llm.list_models())
    m = os.environ["SUB_MODEL"]
    print(llm.chat([{"role": "user", "content": "Say hi in 3 words."}], m).content)
    r = llm.chat([{"role": "user", "content": "Use the tool to compute 3.3*0.12*5."}], m, tools=[sandbox.SPEC])
    print("tool call:", r.tool_calls)
else:
    ctx = RunCtx(run=uuid.uuid4().hex[:6])
    run_agent("mother", "Begin.", os.environ["MOTHER_MODEL"], MOTHER_TOOLS, MOTHER_PROMPT, ctx)
    print(json.dumps(ctx.result, indent=1) if ctx.result else "NO ACCEPTED DESIGN")
