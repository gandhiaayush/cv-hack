"""Medical-device fields for catalog parts, from evidence that can be traced.

    python -m cvhack.scrape.medical     # enrich data/parts_catalog.json in place (cached fetches)

Adds a `medical` block to every part:

  lifecycle         manufacturer status where the maker publishes it machine-readably
                    (TI product pages), plus LCSC's distributor lifecycle flag. Anything
                    else stays "unknown"; a distributor listing is not a manufacturer status.
  traceability      identity and compliance documents the supply chain provides (RoHS
                    certificate, ECCN, HTS code, packaging, datasheet hash). Lot and
                    date-code traceability only exists at goods receipt, so it is null.
  statements        what the part's own datasheet says that matters for medical use:
                    IEC 60601-1 certification, MOPP/MOOP, ISO 10993, life-support
                    disclaimers, medical applications. Every entry carries a quote.
  isolation         isolation-barrier ratings (withstand and working voltage, creepage,
                    clearance, DTI, CTI, barrier capacitance, certifications) for parts
                    that are an isolation barrier, from data/isolation_ratings.json, which
                    is transcribed from the datasheets' insulation tables. Other parts
                    are marked not_applicable with the reason.
  leakage_current   for isolation barriers with a known barrier capacitance, the leakage
                    it alone would pass at 250 Vrms 60 Hz (I = 2*pi*f*C*V). A screening
                    estimate, not a measurement; system leakage is tested on the device.
  biocompatibility  ISO 10993 applies to the finished device's patient-contacting materials,
                    so components carry only what their datasheet claims (almost always
                    nothing) and whether their category often sits in a patient-contact path.

Downloads are cached under .cache/scrape/ and parsed as data only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from cvhack.scrape import CACHE, ROOT

CATALOG_PATH = ROOT / "data" / "parts_catalog.json"
RATINGS_PATH = ROOT / "data" / "isolation_ratings.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"}
LCSC_API = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={sku}"
TI_PRODUCT = "https://www.ti.com/product/{gpn}"
LCSC_SOURCE_ID = "src:lcsc:product-detail"
TI_SOURCE_ID = "src:ti:product-status"
ISOLATION_CATEGORIES = {"isolator", "isolation_amp", "isolated_dcdc", "optocoupler"}
# Categories that commonly sit behind a window, electrode or skin-facing surface in wearables.
PATIENT_PATH_CATEGORIES = {"afe", "light_sensor", "temperature_sensor", "temp_humidity_sensor", "led"}
MAX_PAGES = 40


# ---------------------------------------------------------------------------
# Cached fetches
# ---------------------------------------------------------------------------

def _cached(kind: str, key: str, url: str, binary: bool = False, delay: float = 0.2) -> bytes | None:
    path = CACHE / kind / (re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:120])
    if path.exists():
        data = path.read_bytes()
        return data or None  # an empty file records a failed fetch
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                data = r.content
                break
            if r.status_code in (403, 404, 410):
                break
        except requests.RequestException:
            pass
        time.sleep(1 + attempt * 2)
    time.sleep(delay)
    path.write_bytes(data)
    return data or None


def lcsc_detail(sku: str) -> dict[str, Any] | None:
    raw = _cached("lcsc", sku + ".json", LCSC_API.format(sku=sku))
    if not raw:
        return None
    try:
        return (json.loads(raw) or {}).get("result")
    except json.JSONDecodeError:
        return None


def ti_status(gpn: str) -> str | None:
    raw = _cached("ti", gpn + ".html", TI_PRODUCT.format(gpn=gpn), delay=0.5)
    if not raw:
        return None
    m = re.search(rb'"marketingStatusDescription"\s*:\s*"([A-Z ]+)"', raw)
    return m.group(1).decode() if m else None


def datasheet_text(url: str) -> tuple[str | None, str | None, int]:
    """(text, sha256, pages) for a datasheet PDF, cached as extracted text."""
    key = hashlib.sha1(url.encode()).hexdigest()
    txt_path = CACHE / "datasheet_text" / (key + ".json")
    if txt_path.exists():
        d = json.loads(txt_path.read_text())
        return d["text"], d["sha256"], d["pages"]
    raw = _cached("datasheets", key + ".pdf", url, binary=True)
    text, sha, pages = None, None, 0
    if raw and raw[:5] == b"%PDF-":
        sha = hashlib.sha256(raw).hexdigest()
        try:
            import logging

            import pypdf
            logging.getLogger("pypdf").setLevel(logging.ERROR)
            reader = pypdf.PdfReader(io.BytesIO(raw))
            pages = len(reader.pages)
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:MAX_PAGES])
        except Exception:  # malformed PDFs are common; record the failure, don't guess
            text = None
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(json.dumps({"text": text, "sha256": sha, "pages": pages}))
    return text, sha, pages


# ---------------------------------------------------------------------------
# Datasheet statements
# ---------------------------------------------------------------------------

STATEMENTS = {
    # IEC 60601-1 is the safety standard; 60601-1-2 is EMC and is reported separately.
    "iec60601_1_safety": r"\b60601-1(?!-\d)",
    "iec60601_1_2_emc": r"60601-1-2",
    "mopp_or_moop": r"\b\d\s*(?:x\s*)?MO[OP]P\b|\bMO[OP]Ps?\b|means of (?:patient|operator) protection",
    "iso10993_or_biocompat": r"ISO\s*10993|biocompatib",
    "medical_use_restriction": r"life[- ]support|life[- ]sustaining|"
                               r"not (?:designed|intended|authori[sz]ed|warranted|recommended)[^.]{0,100}"
                               r"(?:medical|surgical|implant)",
    "medical_application": r"\bmedical\b|\bpatient monitor|\bhealthcare\b",
    "aec_q_qualified": r"AEC-Q\d{3}",
}
# A "medical" mention inside a disclaimer or exclusion list is not an application claim.
DISCLAIMER_CONTEXT = re.compile(r"life[- ]support|not (?:be )?(?:designed|intended|authori|warrant|recommend)|"
                                r"jeopardi|nuclear|military|aerospace|aircraft|failure|liabilit|"
                                r"prior (?:written )?(?:consent|approval)|special (?:quality|application)", re.I)


def _quote(text: str, m: re.Match, width: int = 110) -> str:
    s = text[max(0, m.start() - width): m.end() + width]
    return re.sub(r"\s+", " ", s).strip()


def scan_statements(text: str) -> list[dict[str, str]]:
    out = []
    for kind, rx in STATEMENTS.items():
        for m in re.finditer(rx, text, re.I):
            quote = _quote(text, m)
            if kind == "medical_application" and DISCLAIMER_CONTEXT.search(quote):
                continue
            out.append({"kind": kind, "quote": quote})
            break
    return out


# ---------------------------------------------------------------------------
# Field builders
# ---------------------------------------------------------------------------

def _ti_gpn_candidates(part: dict[str, Any]) -> list[str]:
    ds = part.get("datasheet_url") or ""
    m = re.search(r"ti\.com(?:\.cn)?/(?:cn/)?lit/(?:ds/symlink|gpn)/([a-z0-9-]+?)(?:\.pdf)?$", ds, re.I)
    if m:
        return [m.group(1).upper()]
    mpn = re.sub(r"[^A-Z0-9-]", "", part["mpn"].upper())
    # Orderable TI numbers are the generic part number plus package/reel suffixes.
    return [mpn[:n] for n in range(len(mpn), 3, -1)][:6]


def lifecycle(part: dict[str, Any], detail: dict | None, snapshot: str) -> dict[str, Any]:
    mfr_status, mfr_source = None, None
    if (part.get("manufacturer") or "").lower().startswith("texas instruments"):
        for gpn in _ti_gpn_candidates(part):
            s = ti_status(gpn)
            if s:
                mfr_status, mfr_source = s, TI_PRODUCT.format(gpn=gpn)
                break
    normalized = {"ACTIVE": "active", "PREVIEW": "preview", "NRND": "nrnd", "LAST TIME BUY": "last_time_buy",
                  "OBSOLETE": "obsolete", "DISCONTINUED": "obsolete"}.get(mfr_status or "", "unknown")
    if normalized == "unknown" and (detail or {}).get("productCycle") == "stop_product":
        normalized = "distributor_discontinued"  # LCSC has stopped the product; the maker may not have
    return {
        "status": normalized,
        "manufacturer_status": mfr_status,
        "manufacturer_source_url": mfr_source,
        "distributor_lifecycle": (detail or {}).get("productCycle"),
        "distributor_listing": "listed_in_stock" if (part.get("stock") or {}).get("available_qty") else "unknown",
        "checked_at": snapshot,
        "source_ids": [s for s in (TI_SOURCE_ID if mfr_status else None, LCSC_SOURCE_ID if detail else None) if s],
        "note": None if mfr_status else "Manufacturer lifecycle not published machine-readably for this maker; "
                                         "check the manufacturer page or a lifecycle service before design freeze.",
    }


def traceability(part: dict[str, Any], detail: dict | None, sha: str | None, pages: int) -> dict[str, Any]:
    d = detail or {}
    return {
        "manufacturer": part["manufacturer"],
        "mpn": part["mpn"],
        "distributor_sku": (part.get("offer") or {}).get("sku"),
        "datasheet_url": part.get("datasheet_url"),
        "datasheet_sha256": sha,
        "datasheet_pages": pages or None,
        "rohs": d.get("rohsCertType") or ("claimed_in_listing" if "rohs" in part["description"].lower() else None),
        "rohs_certificates": [c.get("url") for c in (d.get("rohsCertList") or []) if c.get("url")][:3],
        "eccn": d.get("eccn"),
        "hts_us": (d.get("htsMap") or {}).get("US"),
        "packaging": d.get("productArrange"),
        "lot_date_code": None,
        "lot_date_code_note": "Recorded per reel at goods receipt (supplier CoC / label); not available from a catalog.",
        "source_ids": [LCSC_SOURCE_ID] if detail else [],
    }


def isolation(part: dict[str, Any], ratings: dict[str, Any]) -> dict[str, Any]:
    if part["category"] not in ISOLATION_CATEGORIES:
        return {"applicable": False,
                "reason": "Not an isolation-barrier component; creepage, clearance and MOPP/MOOP apply to the "
                          "board layout and to barrier parts, not to this part."}
    r = ratings.get(part["mpn"])
    if r is None:
        return {"applicable": True, "status": "not_transcribed",
                "isolation_voltage_vrms_distributor": (part["specs"].get("isolation_voltage_vrms")
                                                       or part["specs"].get("isolation_voltage") or {}).get("value")}
    return {"applicable": True, "status": "transcribed_from_datasheet", **r}


def leakage(part: dict[str, Any], iso: dict[str, Any]) -> dict[str, Any]:
    if not iso.get("applicable"):
        return {"applicable": False, "reason": "Leakage limits (IEC 60601-1) apply to the device and its "
                                               "isolation barriers; this part is not a barrier."}
    c_pf = iso.get("barrier_capacitance_pf")
    if c_pf is None:
        return {"applicable": True, "estimate_ua": None, "reason": "Barrier capacitance not stated in datasheet."}
    i_ua = 2 * math.pi * 60 * c_pf * 1e-12 * 250 * 1e6
    return {"applicable": True, "estimate_ua": round(i_ua, 3),
            "basis": f"I = 2*pi*60 Hz*{c_pf} pF*250 Vrms through the barrier capacitance alone",
            "compare_to": "req:iec60601-1:patient-leakage"}


def biocompat(part: dict[str, Any], statements: list[dict[str, str]], scanned: bool) -> dict[str, Any]:
    claims = [s for s in statements if s["kind"] == "iso10993_or_biocompat"]
    return {
        "datasheet_claim": "claim_found" if claims else ("no_claim_found" if scanned else "datasheet_not_scanned"),
        "quotes": [c["quote"] for c in claims],
        "patient_contact_path_candidate": part["category"] in PATIENT_PATH_CATEGORIES,
        "note": "ISO 10993-1 evaluates the finished device's patient-contacting materials (enclosure, window, "
                "electrodes, adhesives). A PCB component is assessed only if it touches the patient.",
    }


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _enrich_one(part: dict[str, Any], snapshot: str, ratings: dict[str, Any]) -> dict[str, Any]:
    sku = (part.get("offer") or {}).get("sku")
    detail = lcsc_detail(sku) if sku else None
    text, sha, pages = datasheet_text(part["datasheet_url"]) if part.get("datasheet_url") else (None, None, 0)
    statements = scan_statements(text) if text else []
    ds_source = next((s for s in part["sources"] if s.startswith("src:datasheet:")), None)
    for s in statements:
        s["source_id"] = ds_source
    iso = isolation(part, ratings)
    return {
        "lifecycle": lifecycle(part, detail, snapshot),
        "traceability": traceability(part, detail, sha, pages),
        "statements": statements,
        "isolation": iso,
        "leakage_current": leakage(part, iso),
        "biocompatibility": biocompat(part, statements, text is not None),
        "datasheet_scan": {"status": "scanned" if text else ("unreadable" if sha else "unavailable"),
                           "pages_scanned": min(pages, MAX_PAGES) if text else 0, "source_id": ds_source},
    }


def enrich(catalog: dict[str, Any], workers: int = 6) -> dict[str, Any]:
    snapshot = catalog["retrieved_on"]
    ratings = {k: v for k, v in json.loads(RATINGS_PATH.read_text()).items() if not k.startswith("_")} \
        if RATINGS_PATH.exists() else {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        blocks = list(ex.map(lambda p: _enrich_one(p, snapshot, ratings), catalog["parts"]))
    for part, block in zip(catalog["parts"], blocks):
        part["medical"] = block
    srcs = {s["source_id"] for s in catalog["sources"]}
    for sid, doc, url in [(LCSC_SOURCE_ID, "LCSC product detail (RoHS, ECCN, HTS, lifecycle flag)",
                           "https://www.lcsc.com/"),
                          (TI_SOURCE_ID, "Texas Instruments product pages (marketing status)", "https://www.ti.com/")]:
        if sid not in srcs:
            catalog["sources"].append({"source_id": sid, "publisher": doc.split(" ")[0], "document": doc,
                                       "url": url, "kind": "distributor_record" if "lcsc" in sid else
                                       "manufacturer_record", "checked_at": snapshot})
    if RATINGS_PATH.exists() and "src:isolation-ratings" not in srcs:
        catalog["sources"].append({"source_id": "src:isolation-ratings", "publisher": "cv-hack",
                                   "document": "Isolation ratings transcribed from manufacturer datasheets",
                                   "url": "data/isolation_ratings.json", "kind": "transcription",
                                   "checked_at": snapshot})
    parts = catalog["parts"]
    catalog["coverage"]["medical"] = {
        "datasheets_scanned": sum(p["medical"]["datasheet_scan"]["status"] == "scanned" for p in parts),
        "manufacturer_lifecycle_known": sum(bool(p["medical"]["lifecycle"]["manufacturer_status"]) for p in parts),
        "rohs_certificate": sum(bool(p["medical"]["traceability"]["rohs_certificates"]) for p in parts),
        "distributor_discontinued": sum(p["medical"]["lifecycle"]["status"] == "distributor_discontinued"
                                        for p in parts),
        "isolation_parts": sum(p["medical"]["isolation"]["applicable"] for p in parts),
        "isolation_transcribed": sum(p["medical"]["isolation"].get("status") == "transcribed_from_datasheet"
                                     for p in parts),
        "iec60601_1_safety_mentioned": sum(any(s["kind"] == "iec60601_1_safety" for s in p["medical"]["statements"])
                                  for p in parts),
        "medical_use_restriction": sum(any(s["kind"] == "medical_use_restriction"
                                           for s in p["medical"]["statements"]) for p in parts),
        "iso10993_claims": sum(p["medical"]["biocompatibility"]["datasheet_claim"] == "claim_found" for p in parts),
    }
    catalog["data_policy"]["medical"] = (
        "medical.* fields are evidence only: datasheet quotes, transcribed insulation tables, manufacturer "
        "or distributor records, each with a source. They are not a suitability or compliance finding; "
        "IEC 60601-1 conformity and ISO 10993 evaluation are properties of the finished device.")
    return catalog


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    enrich(catalog, workers=args.workers)
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Enriched", CATALOG_PATH.relative_to(ROOT), catalog["coverage"]["medical"])


if __name__ == "__main__":
    main()
