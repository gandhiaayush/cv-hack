"""python smoke_test.py            -> list models + one plain call + one tool call
   python smoke_test.py mother    -> tiny end-to-end mother -> sub-agent negotiation"""
import os
import sys
import uuid

from src import llm
from src.agent import DELEGATE_SPEC, run_agent
from src.tools import kg, sandbox

if len(sys.argv) == 1:
    print("models:", llm.list_models())
    m = os.environ["SUB_MODEL"]
    print(llm.chat([{"role": "user", "content": "Say hi in 3 words."}], m).content)
    r = llm.chat(
        [{"role": "user", "content": "Use the tool to compute 3.3*0.12*5."}], m, tools=[sandbox.SPEC]
    )
    print("tool call:", r.tool_calls)
else:
    MOTHER = (
        "You are the mother agent for a battery-powered Class II pulse-oximeter PCB. Call list_subsystems, then delegate power, "
        "analog_fe, mcu_comms and protection to sub-agents (delegate calls in ONE turn, in parallel). Read their "
        "reports, find cross-subsystem conflicts, and give a final decision: chosen option per subsystem "
        "and any budget changes. Total budget: 150 mW, $12 BOM."
    )
    out = run_agent("mother", "Begin.", os.environ["MOTHER_MODEL"], [DELEGATE_SPEC, kg.LIST_SPEC], MOTHER,
                    run=uuid.uuid4().hex[:6])
    print(out)
