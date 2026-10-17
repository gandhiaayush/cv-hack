"""Regulation lookup: eCFR (21 CFR text) and openFDA (device classification)."""
import re
import requests

ECFR = "https://www.ecfr.gov/api"
OPENFDA = "https://api.fda.gov/device/classification.json"

SEARCH_SPEC = {
    "type": "function",
    "function": {
        "name": "search_regulations",
        "description": "Search 21 CFR (FDA regulations) by keyword. Returns section ids and headings. "
        "Follow up with get_regulation_section to read one.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}
SECTION_SPEC = {
    "type": "function",
    "function": {
        "name": "get_regulation_section",
        "description": "Get the text of one 21 CFR section, e.g. part='820', section='820.30'.",
        "parameters": {
            "type": "object",
            "properties": {"part": {"type": "string"}, "section": {"type": "string"}},
            "required": ["part", "section"],
        },
    },
}
CLASSIFY_SPEC = {
    "type": "function",
    "function": {
        "name": "classify_device",
        "description": "Look up FDA device classification (class I/II/III, product code, regulation number) "
        "by device name, e.g. 'pulse oximeter'.",
        "parameters": {
            "type": "object",
            "properties": {"device_name": {"type": "string"}},
            "required": ["device_name"],
        },
    },
}


def search_regulations(query: str) -> str:
    r = requests.get(
        f"{ECFR}/search/v1/results",
        params={"query": query, "hierarchy[title]": "21", "per_page": 8},
        timeout=20,
    )
    r.raise_for_status()
    rows = []
    for x in r.json().get("results", []):
        h = x["hierarchy"]
        rows.append(f"{h.get('section') or h.get('part')}: {x['hierarchy_headings'].get('section') or ''}"
                    f" | part {h.get('part')}")
    return "\n".join(rows) or "no results"


def get_regulation_section(part: str, section: str) -> str:
    # models pass "2720", "870.2720" or "§ 870.2720"; normalise to part + "part.section"
    section = section.replace("§", "").strip()
    if "." not in section:
        section = f"{part}.{section}"
    part = section.split(".")[0]
    date = requests.get(f"{ECFR}/versioner/v1/titles.json", timeout=20).json()
    d = next(t["up_to_date_as_of"] for t in date["titles"] if t["number"] == 21)
    r = requests.get(
        f"{ECFR}/versioner/v1/full/{d}/title-21.xml",
        params={"part": part, "section": section},
        headers={"Accept-Encoding": "gzip"},
        timeout=30,
    )
    if r.status_code in (404, 406):
        return (f"ERROR: section {section} not found (it may have been removed or renumbered). "
                "Use search_regulations to find a current section id.")
    r.raise_for_status()
    r.encoding = "utf-8"
    text = re.sub(r"<[^>]+>", " ", r.text)
    return re.sub(r"\s+", " ", text).strip()[:6000]


def classify_device(device_name: str) -> str:
    r = requests.get(
        OPENFDA, params={"search": f'device_name:"{device_name}"', "limit": 5}, timeout=20
    )
    if r.status_code != 200:
        return "no results"
    out = []
    for x in r.json().get("results", []):
        out.append(
            f"{x.get('device_name')} | class {x.get('device_class')} | product code {x.get('product_code')}"
            f" | reg {x.get('regulation_number')} | {x.get('medical_specialty_description')}"
        )
    return "\n".join(out) or "no results"
