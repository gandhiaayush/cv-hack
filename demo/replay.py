"""Replay a recorded run into a new run id with its original timing (stage fallback).

    python -m demo.replay runs/<good-run> [--run-id demo-x] [--speed 1.5]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from cvhack.contracts import AgentEvent
from cvhack.events import emit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--run-id", default=f"replay-{int(time.time())}")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--max-gap", type=float, default=3.0, help="cap idle gaps (seconds)")
    a = ap.parse_args()
    lines = [json.loads(l) for l in (Path(a.src) / "events.jsonl").read_text().splitlines() if l.strip()]
    print("run_id:", a.run_id, flush=True)
    prev = None
    for raw in lines:
        if prev is not None:
            time.sleep(min(raw["ts"] - prev, a.max_gap) / a.speed)
        prev = raw["ts"]
        emit(AgentEvent(**{**raw, "run_id": a.run_id, "ts": time.time()}))


if __name__ == "__main__":
    main()
