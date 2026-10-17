import shutil

import pytest
from fastapi.testclient import TestClient

from cvhack.events import RUNS_DIR
from cvhack.mocks.fake_run import FakeRun
from dashboard.server import app

RID = "_pytest_fake"


@pytest.fixture(scope="module")
def client():
    shutil.rmtree(RUNS_DIR / RID, ignore_errors=True)
    FakeRun(RID, speed=200).run(linger=0)
    yield TestClient(app)
    shutil.rmtree(RUNS_DIR / RID, ignore_errors=True)


def test_fake_run_ends_green(client):
    s = client.get(f"/api/runs/{RID}/state").json()
    assert s["done"]["ok"]
    assert all(r["status"] == "pass" for r in s["requirements"])
    assert any(a["parent"] == "power" for a in s["agents"])          # recursive tree
    assert s["replans"] and s["negotiation"]
    assert s["totals"]["power_mw"] <= s["budgets"]["power_mw"]


def test_renders_and_exports(client):
    r = client.get(f"/api/runs/{RID}/schematic.svg")
    assert r.status_code == 200 and r.text.startswith("<svg")
    assert "MAX30102" in client.get(f"/api/runs/{RID}/bom.csv").text
    assert "REQ-01" in client.get(f"/api/runs/{RID}/matrix.csv").text
    files = client.post(f"/api/runs/{RID}/export").json()["files"]
    assert {"bom.csv", "traceability.csv", "schematic.svg"} <= set(files)
    assert any(f.endswith(".kicad_sch") for f in files)


def test_control_roundtrip(client):
    r = client.post(f"/api/runs/{RID}/control", json={"type": "device_class", "value": "III"}).json()
    assert r["ok"] and r["payload"] == {"type": "device_class", "value": "III"}
    cmds = client.get(f"/api/runs/{RID}/state").json()["commands"]
    assert cmds[-1]["type"] == "device_class" and cmds[-1]["applied"] is False  # fake run already exited


def test_state_has_rules_and_negotiation(client):
    s = client.get(f"/api/runs/{RID}/state").json()
    assert s["rules"] and all(r["status"] == "pass" for r in s["rules"])
    assert {"delegate", "report", "decision"} <= {n["kind"] for n in s["negotiation"]}


def test_unknown_run_404(client):
    assert client.get("/api/runs/nope/state").status_code == 404
