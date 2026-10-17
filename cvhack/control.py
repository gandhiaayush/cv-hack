"""Control channel: dashboard -> mother agent.

The dashboard appends ControlCommands to runs/<run_id>/control.jsonl.
The mother agent calls read_commands() between steps with the offset it
got last time, applies new commands, and emits a `constraint_change`
event so the dashboard knows it was picked up.
"""

from __future__ import annotations

from cvhack.contracts import ControlCommand
from cvhack.events import run_dir


def send_command(run_id: str, cmd: ControlCommand) -> None:
    with open(run_dir(run_id) / "control.jsonl", "a") as f:
        f.write(cmd.model_dump_json() + "\n")


def read_commands(run_id: str, offset: int = 0) -> tuple[list[ControlCommand], int]:
    """Return commands after line `offset` and the new offset (line count)."""
    path = run_dir(run_id) / "control.jsonl"
    if not path.exists():
        return [], offset
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    return [ControlCommand.model_validate_json(l) for l in lines[offset:]], len(lines)
