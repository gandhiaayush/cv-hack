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


def test_gate_critic_inference_and_finalize_map_to_dashboard_events():
    from cvhack.bridge import Row, Translator
    from cvhack.state import fold
    tr = Translator()
    rows = [
        Row(1, 1.0, "r", "mother:m", None, "start", {"task": "Begin."}),
        Row(2, 2.0, "r", "mother:m", None, "llm_call", {"model": "nvidia/Nemotron-X", "latency_s": 2.0,
                                                       "prompt_tokens": 100, "completion_tokens": 50}),
        Row(3, 3.0, "r", "power:p", "mother:m", "start", {"task": "power"}),
        Row(4, 4.0, "r", "power:p", "mother:m", "gate", {"subsystem": "power", "attempt": 1, "passed": [],
                                                        "failed": {"A": ["RATING_EXCEEDED:J1"]}}),
        Row(5, 5.0, "r", "power:p", "mother:m", "gate", {"subsystem": "power", "attempt": 2, "passed": ["A2"], "failed": {}}),
        Row(6, 6.0, "r", "power:p", "mother:m", "final", {"subsystem": "power", "status": "ok", "options": [
            {"name": "A2", "parts": [{"mpn": "X1"}], "gate_ok": True, "totals": {"cost_usd": 1.5, "power_mw": 3}}]}),
        Row(7, 7.0, "r", "mother:m", None, "tool_call", {"tool": "finalize", "args": {
            "selections": [{"subsystem": "power", "option": "A2"}], "rationale": "cheapest"}}),
        Row(8, 8.0, "r", "critic", None, "llm_call", {"model": "zai-org/GLM-5.3", "latency_s": 1.0,
                                                     "prompt_tokens": 10, "completion_tokens": 5}),
        Row(9, 9.0, "r", "critic", "mother:m", "critique", {"round": 1, "objections": [
            {"id": "R0-O1", "severity": "blocker", "subsystem": "power", "part": "X1", "issue": "too hot"}]}),
        Row(10, 10.0, "r", "mother:m", None, "final", {"status": "accepted", "selections": {"power": "A2"},
                                                     "totals": {"cost_usd": 1.5, "power_mw": 3}, "critic_rounds": 1}),
    ]
    evs = [e for r in rows for e in tr.translate(r)]
    s = fold("r", evs)
    assert s.verifiers["gate:power"]["ok"] is True                      # latest gate attempt wins
    assert s.verifiers["critic"]["ok"] is False and s.verifiers["critic"]["errors"][0]["code"] == "CRITIC_BLOCKER"
    rep = next(n for n in s.negotiation if n["kind"] == "report")
    assert rep["options"][0]["cost"] == 1.5 and "X1" in rep["options"][0]["notes"]
    dec = next(n for n in s.negotiation if n["kind"] == "decision")
    assert dec["to"] == "power:p" and dec["chosen"] == "A2"
    models = {m["model"]: m for m in s.to_json()["inference"]}
    assert models["nvidia/Nemotron-X"]["calls"] == 1 and models["zai-org/GLM-5.3"]["prompt_tokens"] == 10
    assert s.agents["critic"]["role"] == "verifier" and s.agents["critic"]["parent"] == "mother:m"
    assert s.done["ok"] is True and "accepted" in s.done["summary"]


def test_run_done_is_green_only_for_a_clean_wired_result():
    from cvhack.bridge import Row, Translator
    base = {"status": "accepted", "selections": {"power": "A"}, "totals": {}, "critic_rounds": 1,
            "wiring": {"electrical_ok": True, "requirements": {"REQ-01": "pass", "REQ-11": "fail"},
                       "advisory_requirements": ["REQ-11"]}}

    def done(res):
        tr = Translator()
        tr.translate(Row(1, 1.0, "r", "mother:m", None, "start", {"task": "Begin."}))
        return next(e for e in tr.translate(Row(2, 2.0, "r", "mother:m", None, "final", res))
                    if e.kind == "run_done").payload

    assert done(base)["ok"] is True                                   # an advisory failure stays green
    assert done({**base, "status": "accepted_with_contested_blockers", "contested_blockers": ["R1-O1"]})["ok"] is False
    assert done({**base, "wiring": {**base["wiring"], "electrical_ok": False}})["ok"] is False
    bad = {**base, "wiring": {**base["wiring"], "requirements": {"REQ-01": "fail"}}}
    assert done(bad)["ok"] is False and "REQ-01" in done(bad)["summary"]

