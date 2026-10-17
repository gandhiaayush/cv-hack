"""Agent event stream. A calls emit(); the dashboard tails the JSONL file.

The file is the source of truth, so the dashboard can run in a separate
process and late joiners can replay the whole run.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterator

from cvhack.contracts import AgentEvent

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
_lock = threading.Lock()


def run_dir(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def emit(event: AgentEvent | None = None, **kwargs: Any) -> AgentEvent:
    """emit(AgentEvent(...)) or emit(run_id=..., agent_id=..., kind=..., payload=...)."""
    if event is None:
        event = AgentEvent(**kwargs)
    line = event.model_dump_json()
    with _lock, open(run_dir(event.run_id) / "events.jsonl", "a") as f:
        f.write(line + "\n")
    return event


def read_events(run_id: str, offset: int = 0) -> tuple[list[AgentEvent], int]:
    """Return events written after byte `offset`, plus the new offset."""
    path = run_dir(run_id) / "events.jsonl"
    if not path.exists():
        return [], offset
    with open(path) as f:
        f.seek(offset)
        chunk = f.read()
    # Only consume complete lines; a writer may be mid-line.
    end = chunk.rfind("\n") + 1
    events = [AgentEvent.model_validate_json(l) for l in chunk[:end].splitlines() if l.strip()]
    return events, offset + len(chunk[:end].encode())


def latest_run_id() -> str | None:
    if not RUNS_DIR.exists():
        return None
    runs = [p for p in RUNS_DIR.iterdir() if (p / "events.jsonl").exists()]
    return max(runs, key=lambda p: (p / "events.jsonl").stat().st_mtime).name if runs else None


def iter_lines(run_id: str) -> Iterator[dict]:
    path = run_dir(run_id) / "events.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                yield json.loads(line)
