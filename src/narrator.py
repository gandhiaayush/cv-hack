"""Live plain-English commentary on a run, for the demo.

Reads the agents' SQLite log (runs/run.db), turns new events into short factual statements, and has a
fast model rewrite them as 1-3 friendly sentences. The model sees ONLY those facts (so it has nothing to
invent from), and if the API is slow or down the plain facts are shown instead, so the feed never stalls.
Run standalone:  python -m src.narrator"""
import json
import os
import sqlite3
import threading
import time

from .llm import chat

DB = "runs/run.db"
WHY = {
    "PART_UNKNOWN": "that part is not in the parts database",
    "CATEGORY_MISMATCH": "that is the wrong kind of part for this subsystem",
    "RATING_EXCEEDED": "its voltage rating is too low for the rail",
    "ROLE_MISSING": "a required kind of part is missing",
    "OUT_OF_STOCK": "it is out of stock",
}
PRETTY = {"analog_fe": "analog front-end", "mcu_comms": "microcontroller and radio", "power": "power",
          "protection": "protection"}
SYSTEM = ("You are the live commentator for a demo of an AI team designing a medical wearable's circuit board. "
          "Turn the events below into 1 to 3 short, plain-English sentences for a non-expert audience, in the "
          "present tense. Summarise what the team is doing and why it matters instead of repeating the facts "
          "word for word. Use ONLY the facts given: do not add numbers, part names, reasons or claims that "
          "are not in them. Refer to agents as 'the lead agent', 'the power agent' etc. and use 'it' if a pronoun is needed; never 'he', 'she', 'him' or 'her'. If earlier commentary is given, do NOT repeat it: say only what is new. Describe actions and results only: never judge or promise quality (no 'ensures', "
          "'guarantees', 'correctly', 'perfect', 'safe'). No markdown, no lists.")
BANNED = ("ensure", "guarantee", "correct", "perfect", "safe", "flawless", "reliab", "optimal")


def _grounded(out: str, src: str) -> bool:
    """Code check on the narrator itself: no numbers that are not in the facts, no judgement words."""
    import re
    nums = set(re.findall(r"\d+(?:\.\d+)?", out)) - set(re.findall(r"\d+(?:\.\d+)?", src))
    gendered = re.search(r"\b(he|she|his|hers?|him|himself|herself)\b", out, re.I)
    return not nums and not gendered and not any(w in out.lower() for w in BANNED)


def _collapse(facts):
    """Merge repeated statements into one with a count, keeping order."""
    out, seen = [], {}
    for text, tone in facts:
        if text in seen:
            seen[text][1] += 1
        else:
            seen[text] = [text, 1, tone]
            out.append(seen[text])
    return [(t if n == 1 else f"{t[:-1]} ({n} times).", tone) for t, n, tone in out]


def _role(agent: str) -> str:
    r = agent.split(":")[0]
    return PRETTY.get(r, r.replace("_", " "))


def fact(kind: str, agent: str, b) -> tuple[str, str] | None:
    """One event -> (plain statement, tone) or None if it is not worth saying."""
    who = _role(agent)
    if kind == "start":
        if b.get("depth", 0) == 0:
            return "The lead agent starts planning the design.", "info"
        return f"The lead agent hands the {who} subsystem to a specialist agent.", "info"
    if kind == "gate":
        bits = []
        if b["passed"]:
            bits.append(f"{len(b['passed'])} option(s) passed the automatic checks")
        for name, errs in b["failed"].items():
            why = "; ".join(sorted({WHY.get(e.split(':')[0], e.split(':')[0]) for e in errs}))
            bits.append(f"option '{name}' was rejected because {why}")
        tone = "warn" if b["failed"] else "good"
        return f"The {who} agent's proposal is checked by code against the parts database: " + "; ".join(bits) + ".", tone
    if kind == "budget_check":
        bud = b["budget"]
        return (f"Totals are recomputed from the parts database, not from the AI's word: ${b['cost_usd']} and "
                f"{b['power_mw']} mW against a budget of ${bud['cost_usd']:.0f} and {bud['power_mw']:.0f} mW."), "info"
    if kind == "critique":
        n = len(b["objections"])
        if not n:
            return "An independent reviewer model checks the design and finds no objections.", "good"
        blockers = [o for o in b["objections"] if o.get("severity") == "blocker"]
        top = (blockers or b["objections"])[0]
        return (f"An independent reviewer model, a different AI from the designer, raises {n} objections "
                f"({len(blockers)} serious). For example: {top.get('issue', '')[:220]}"), "warn"
    if kind == "critique_failed":
        return "The reviewer was unavailable, so this does not count as a review.", "warn"
    if kind in ("retry", "llm_error"):
        return f"The {who} agent hit a hiccup and is trying again.", "warn"
    if kind == "tool_call":
        t, a = b.get("tool"), b.get("args", {})
        if t == "finalize":
            sel = "; ".join(f"{_role(s['subsystem'])}: {s['option']}" for s in a.get("selections", []))
            resp = len(a.get("responses", []))
            extra = f" It is answering {resp} reviewer objection(s)." if resp else ""
            return f"The lead agent proposes a final design ({sel}).{extra}", "info"
        if t == "search_parts":
            return f"The {who} agent browses the parts catalog.", "info"
        if t == "get_regulation_section":
            return f"The {who} agent reads FDA regulation section {a.get('section', '')}.", "info"
        if t == "classify_device":
            return f"The {who} agent looks up how the FDA classifies this device.", "info"
        if t == "run_python":
            return f"The {who} agent runs a calculation.", "info"
        if t == "get_subsystem_context":
            return f"The {who} agent reads the knowledge graph of rules for its subsystem.", "info"
        return None
    if kind == "final" and isinstance(b, dict):
        if "selections" in b:
            return f"Design {b['status'].replace('_', ' ')}: " + ", ".join(
                f"{_role(s)}: {o}" for s, o in b["selections"].items()) + ".", "good"
        if b.get("subsystem"):
            return f"The {who} agent submits its report.", "good"
    return None


class Narrator:
    def __init__(self, db: str = DB):
        self.db, self.last, self.lines, self.lock = db, 0, [], threading.Lock()
        self.run = None

    def poll(self) -> list[tuple[str, str]]:
        if not os.path.exists(self.db):
            return []
        c = sqlite3.connect(self.db)
        try:
            cur = c.execute("select run from messages order by id desc limit 1").fetchone()
            top = c.execute("select coalesce(max(id),0) from messages").fetchone()[0]
            if (cur and cur[0] != self.run) or top < self.last:  # a new run started: commentary follows it
                self.run = cur[0] if cur else None
                self.last = 0
                with self.lock:
                    self.lines.clear()
            rows = c.execute("select id, agent, kind, body from messages where id > ? and run = ? order by id",
                             (self.last, self.run)).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            c.close()
        out = []
        for id_, agent, kind, body in rows:
            self.last = id_
            f = fact(kind, agent, json.loads(body))
            if f:
                out.append(f)
        return out

    def say(self, facts: list[tuple[str, str]]) -> str:
        facts = _collapse(facts[-30:])
        text = " ".join(f for f, _ in facts)
        with self.lock:
            earlier = " ".join(l["text"] for l in self.lines[-2:])
        try:
            m = chat([{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": (f"Earlier commentary (do not repeat): {earlier}\n\n" if earlier else "") + "New events: " + text}],
                     os.environ.get("NARRATOR_MODEL") or os.environ["SUB_MODEL"], max_tokens=300, temperature=0.3,
                     extra_body={"chat_template_kwargs": {"enable_thinking": False}})
            out = (m.content or "").strip()
            return out if out and _grounded(out, text) else text
        except Exception:
            return text  # API slow or down: plain facts, never a stalled feed

    def step(self) -> None:
        facts = self.poll()
        if not facts:
            return
        tone = "warn" if any(t == "warn" for _, t in facts) else "good" if any(t == "good" for _, t in facts) else "info"
        line = {"ts": time.time(), "tone": tone, "text": self.say(facts)}
        with self.lock:
            line["id"] = len(self.lines)
            self.lines.append(line)

    def run_forever(self, every: float = 3.0) -> None:
        while True:
            try:
                self.step()
            except Exception:
                pass
            time.sleep(every)


if __name__ == "__main__":
    n = Narrator()
    while True:
        n.step()
        if n.lines:
            print(n.lines[-1]["text"], flush=True)
            n.lines.clear()
        time.sleep(3)
