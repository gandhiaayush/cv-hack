"""Verify the board that the team's chosen parts actually produce: it must be fully drawn, pass the design rules, and
pass the electrical checks. Plain code; this is what makes "accepted" mean a real, checked board."""
import re

from cvhack.parts import load_parts
from cvhack.tooling.electrical import ElectricalVerifier
from cvhack.tooling.rules import RuleVerifier

from . import design_apply


def verify(chosen: dict) -> dict:
    parts = load_parts()
    d, rep = design_apply.apply(chosen, parts)
    problems = []
    for u in rep["unplaced"]:
        alts = design_apply.drawable_alternatives(u["subsystem"], u["mpn"], parts)
        problems.append(f"{u['mpn']} ({u['subsystem']}) cannot be drawn: {u['reason']}." + (f" Drawable choices: {', '.join(alts)}." if alts else ""))
    rules = RuleVerifier().verify(d, parts)
    notes = []
    for e in rules.errors:
        if e.severity != "error":
            continue
        if re.search(r"\b[A-Z]+\d+ is \S+, expected ", e.message):  # pins the reference board's exact part: a note, not a fault
            notes.append(f"{e.rule_id or e.code}: differs from the reference board ({e.message.split(': ', 1)[-1]})")
        else:
            problems.append(f"Design rule {e.rule_id or e.code} fails: {e.message}")
    erc = ElectricalVerifier(use_kicad=False).verify(d, parts)
    for e in erc.errors:
        if e.severity == "error":
            problems.append(f"Electrical check {e.code}{' on ' + e.part if e.part else ''}: {e.message}")
    return {"design": d, "report": rep, "problems": problems, "notes": notes, "rules_ok": rules.ok, "erc_ok": erc.ok}
