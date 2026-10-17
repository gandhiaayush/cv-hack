"""Query Vishnu's knowledge graph (data/relationships.json + data/rules.json) for a subsystem.

Read-only. The graph describes the checked-in PPG wearable mock plus project-defined (non-regulatory)
design rules; every answer says so, so agents don't present it as FDA compliance."""
import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent.parent / "data"

SPEC = {
    "type": "function",
    "function": {
        "name": "get_subsystem_context",
        "description": "Knowledge graph lookup for one subsystem of the PPG wearable: its placed parts "
        "(ref, MPN, mock ratings), the nets they touch, the design rules that apply (id, rationale, what is "
        "checked), and known evidence gaps. Call this BEFORE proposing parts. Subsystem names: use list_subsystems.",
        "parameters": {
            "type": "object",
            "properties": {"subsystem": {"type": "string"}},
            "required": ["subsystem"],
        },
    },
}
LIST_SPEC = {
    "type": "function",
    "function": {
        "name": "list_subsystems",
        "description": "List the subsystem names in the knowledge graph and how many parts/rules each has.",
        "parameters": {"type": "object", "properties": {}},
    },
}


@lru_cache
def _load():
    g = json.loads((DATA / "relationships.json").read_text())
    rules = json.loads((DATA / "rules.json").read_text())
    nodes = {n["id"]: n for n in g["nodes"]}
    return g, nodes, rules


def _subsystems():
    g, nodes, _ = _load()
    return sorted(n["label"] for n in nodes.values() if n["type"] == "subsystem")


def _instances(sub):
    _, nodes, _ = _load()
    return {n["label"]: n for n in nodes.values()
            if n["type"] == "part_instance" and n["attributes"].get("subsystem") == sub}


def _norm(name):
    names = _subsystems()
    n = name.lower().replace(" ", "_").replace("-", "_")
    for s in names:
        if s.lower() == n or n in s.lower() or s.lower() in n:
            return s
    return None


def _brief_check(check):
    ops = []

    def walk(c):
        if "checks" in c:
            for x in c["checks"]:
                walk(x)
        else:
            ops.append({k: v for k, v in c.items()})
    walk(check)
    return ops[:12]


def list_subsystems() -> str:
    rules = _load()[2]["rules"]
    out = []
    for s in _subsystems():
        inst = _instances(s)
        nr = sum(1 for r in rules if any(t.get("ref") in inst for t in r["targets"]))
        out.append(f"{s}: {len(inst)} parts, {nr} rules")
    return "\n".join(out)


def get_subsystem_context(subsystem: str) -> str:
    g, nodes, rule_set = _load()
    sub = _norm(subsystem)
    if not sub:
        return f"ERROR: unknown subsystem '{subsystem}'. Known: {', '.join(_subsystems())}"
    inst = _instances(sub)
    inst_ids = {n["id"]: ref for ref, n in inst.items()}
    part_of = {r["source"]: r["target"] for r in g["relationships"] if r["relation"] == "instance_of"}
    pin_net = {r["source"]: nodes[r["target"]]["label"] for r in g["relationships"]
               if r["relation"] == "connected_to_net"}
    parts, nets = [], set()
    for iid, ref in inst_ids.items():
        p = nodes[part_of[iid]]["attributes"] if iid in part_of else {}
        parts.append({"ref": ref, "mpn": p.get("mpn"), "category": p.get("category"),
                      "description": p.get("description"), "mock_ratings": p.get("mock_ratings")})
        nets |= {n for pin, n in pin_net.items() if pin.startswith(iid + ":pin:")}
    rules = [{"id": r["id"], "title": r["title"], "category": r["category"], "severity": r.get("severity"),
              "rationale": r.get("rationale"), "checks": _brief_check(r["check"])}
             for r in rule_set["rules"] if any(t.get("ref") in inst for t in r["targets"])]
    gaps = [{"mpn": e.get("mpn"), "missing": e.get("missing")} for e in g["evidence_gaps"]
            if e.get("entity_id") in {part_of.get(i) for i in inst_ids}]
    return json.dumps({
        "subsystem": sub, "parts": parts, "nets": sorted(nets), "applicable_rules": rules,
        "evidence_gaps": gaps,
        "caveat": "Mock design + project-defined prototype rules. NOT FDA/IEC compliance; mock ratings unverified.",
    }, indent=1)[:7000]
