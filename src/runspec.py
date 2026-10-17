"""Turn the user's free-text request into a run spec: device, budget, and whether it is in scope.

The model only extracts; numbers are validated in code and fall back to defaults, and the scope flag is
shown to the user rather than silently trusted."""
import json
import os
import re

from .llm import chat
from .spec import BUDGET, DEVICE

SYSTEM = """Extract a design brief from the user's request. Return JSON only:
{"device": "<short noun phrase describing the product, include battery/wireless/class if stated>",
 "cost_usd": <BOM budget in USD or null>, "power_mw": <average power budget in mW or null>,
 "in_scope": <true only if it is a small battery-powered wearable or biosensor board (MCU + sensor + power
 + protection); false for anything else>, "scope_note": "<one sentence if not in scope, else empty>"}
Use only what the request says; use null for numbers it does not give."""

SCOPE_NOTE = ("This system's baseline design, rules and parts graph cover a PPG pulse-oximeter wearable. "
              "Other products use that board only as a starting point.")


def _num(x, default, lo, hi):
    try:
        v = float(x)
        return v if lo <= v <= hi else default
    except (TypeError, ValueError):
        return default


def extract(prompt: str) -> dict:
    spec = {"device": DEVICE, "cost_usd": BUDGET["cost_usd"], "power_mw": BUDGET["power_mw"],
            "in_scope": True, "scope_note": "", "request": prompt.strip()[:1000]}
    try:
        m = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt[:1000]}],
                 os.environ.get("SUB_MODEL"), max_tokens=300, temperature=0,
                 extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        t = (m.content or "")
        j = json.loads(re.search(r"\{.*\}", t, re.S).group(0))
    except Exception:
        return spec  # extraction is best-effort; the run still happens with defaults
    dev = str(j.get("device") or "").strip()[:120]
    if dev:
        spec["device"] = dev
    spec["cost_usd"] = _num(j.get("cost_usd"), spec["cost_usd"], 1, 10000)
    spec["power_mw"] = _num(j.get("power_mw"), spec["power_mw"], 0.1, 100000)
    spec["in_scope"] = bool(j.get("in_scope", True))
    if not spec["in_scope"]:
        spec["scope_note"] = str(j.get("scope_note") or "").strip()[:300] or SCOPE_NOTE
    return spec
