import json
import shutil
import sqlite3

from cvhack.bridge import Translator, bridge, parse_report, read_rows
from cvhack.events import RUNS_DIR, read_events
from cvhack.state import fold

RUN = "_pytest_bridge"
REPORT = {"subsystem": "power", "options": [{"name": "LDO", "summary": "simple", "cost": 0.4, "power_mw": 2,
                                             "risks": ["dropout"]}],
          "quantitative_constraints": [], "qualitative_constraints": [],
          "conflicts": ["noisy rail"], "needs_from_others": ["peak current from mcu"]}


def make_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, ts REAL, run TEXT, agent TEXT,"
              " parent TEXT, kind TEXT, body TEXT)")
    rows = [
        ("mother:aa", None, "start", {"task": "Begin.", "depth": 0}),
        ("mother:aa", None, "tool_call", {"tool": "delegate", "args": {"subsystem": "power", "task": "t"}}),
        ("power:bb", "mother:aa", "start", {"task": "Power budget 30 mW", "depth": 1}),
        ("power:bb", "mother:aa", "tool_call", {"tool": "run_python", "args": {"code": "print(1)"}}),
        ("power:bb", "mother:aa", "tool_result", {"tool": "run_python", "result": "1"}),
        ("power:bb", "mother:aa", "final", "Here you go:\n```json\n" + json.dumps(REPORT) + "\n```"),
        ("mother:aa", None, "final", "Chose LDO for power."),
    ]
    for i, (agent, parent, kind, body) in enumerate(rows):
        c.execute("INSERT INTO messages(ts,run,agent,parent,kind,body) VALUES(?,?,?,?,?,?)",
                  (1000.0 + i, RUN, agent, parent, kind, json.dumps(body)))
    c.commit()
    c.close()


def test_bridge_builds_tree_and_negotiation(tmp_path):
    shutil.rmtree(RUNS_DIR / RUN, ignore_errors=True)
    db = tmp_path / "run.db"
    make_db(db)
    try:
        bridge(read_rows(db), Translator())
        s = fold(RUN, read_events(RUN)[0])
        agents = {a["id"]: a for a in s.agents.values()}
        assert agents["mother:aa"]["parent"] is None and agents["mother:aa"]["role"] == "mother"
        assert agents["power:bb"]["parent"] == "mother:aa" and agents["power:bb"]["status"] == "done"
        kinds = [n["kind"] for n in s.negotiation]
        assert kinds == ["delegate", "report"]
        rep = s.negotiation[1]
        assert rep["to"] == "mother:aa" and rep["needs"] == ["peak current from mcu"]
        assert rep["options"][0] == {"name": "LDO", "cost": 0.4, "power_mw": 2, "notes": "simple; dropout"}
        assert s.done["ok"] is True
    finally:
        shutil.rmtree(RUNS_DIR / RUN, ignore_errors=True)


def test_parse_report_variants():
    assert parse_report(json.dumps(REPORT))["subsystem"] == "power"
    assert parse_report("prose then " + json.dumps(REPORT) + " trailing")["conflicts"] == ["noisy rail"]
    assert parse_report("no json here") is None
