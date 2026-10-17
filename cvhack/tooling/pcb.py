"""Design -> placed KiCad board -> routed board -> GLB for the 3D viewer.

    placed = place(design, parts, cache_dir, previous)   # fast: footprints, nets, pours (BoardBuild)
    routed = route(placed)                               # slow: Freerouting, zones refilled, DRC
    placed.glb / routed.glb                              # kicad-cli pcb export glb

Both stages are cached by content. `previous` is the last board's placement
for the same run, so an edit only moves the parts that changed.

Routing uses Freerouting (needs Java 25+). Both are looked up in runs/_tools
(see demo/setup_router.sh) or via FREEROUTING_JAR / JAVA. 3D models come from
KiCad's official kicad-packages3D repo and are downloaded on first use into
runs/_3dmodels (KICAD_3DMODEL_DIR overrides).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from cvhack.contracts import Design, Part
from cvhack.tooling import kicad_cli

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = Path(__file__).resolve().parent / "pcb_build.py"
TOOLS = ROOT / "runs" / "_tools"
MODELS_URL = "https://gitlab.com/kicad/libraries/kicad-packages3D/-/raw/master/"
_VERSION = "14"

# DRC findings that come from KiCad's own library footprints or silkscreen
# cosmetics, not from what the agents designed.
DRC_COSMETIC = {"silk_overlap", "silk_over_copper", "silk_edge_clearance", "text_height", "text_thickness",
                "lib_footprint_issues", "lib_footprint_mismatch", "courtyards_overlap"}


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def kicad_python() -> str | None:
    env = os.environ.get("KICAD_PYTHON")
    if env and Path(env).exists():
        return env
    cli = kicad_cli.cli_path()
    if cli:
        contents = Path(cli).resolve().parent.parent
        hits = sorted(glob.glob(str(contents / "Frameworks/Python.framework/Versions/*/bin/python3")))
        if hits:
            return hits[-1]
    for c in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if Path(c).exists() and subprocess.run([c, "-c", "import pcbnew"], capture_output=True).returncode == 0:
            return c
    return None


def footprint_dir() -> Path | None:
    env = os.environ.get("KICAD_FOOTPRINT_DIR")
    if env:
        return Path(env)
    from cvhack.tooling import kicad_lib
    sym = kicad_lib.symbol_dir()
    return sym.parent / "footprints" if sym and (sym.parent / "footprints").is_dir() else None


def models_dir() -> Path:
    return Path(os.environ.get("KICAD_3DMODEL_DIR", ROOT / "runs" / "_3dmodels"))


def java() -> str | None:
    env = os.environ.get("JAVA")
    cands = [env] if env else []
    cands += [str(TOOLS / "jre/Contents/Home/bin/java"), str(TOOLS / "jre/bin/java")]
    for c in cands:
        if c and Path(c).exists():
            return c
    return None


def freerouting_jar() -> str | None:
    env = os.environ.get("FREEROUTING_JAR")
    if env and Path(env).exists():
        return env
    jars = sorted(glob.glob(str(TOOLS / "freerouting-*.jar")))
    return jars[-1] if jars else None


def available() -> bool:
    return bool(kicad_cli.available() and kicad_python() and footprint_dir())


def can_route() -> bool:
    return available() and bool(java() and freerouting_jar())


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

@dataclass
class BoardBuild:
    dir: Path
    pcb: Path
    info: dict = field(default_factory=dict)

    @property
    def glb(self) -> Path:
        return self.dir / "placed.glb"

    @property
    def routed_pcb(self) -> Path:
        return self.dir / "routed.kicad_pcb"

    @property
    def routed_glb(self) -> Path:
        return self.dir / "routed.glb"

    @property
    def route_info(self) -> dict | None:
        f = self.dir / "route.json"
        return json.loads(f.read_text()) if f.exists() else None


def _pad_nets(design: Design, parts: dict[str, Part]) -> dict[str, dict[str, str]]:
    """ref -> {pad number: net}, including pins that share a name in KiCad's symbol (USB-C's
    second VBUS/GND pins), so the footprint's duplicate pads land on the same net."""
    from cvhack.tooling import kicad_sch
    out: dict[str, dict[str, str]] = {}
    for name, nodes in design.merged_nets().items():
        for n in nodes:
            out.setdefault(n.ref, {})[n.pin] = name
    mpn = {i.ref: i.mpn for i in design.all_parts()}
    for ref, pads in out.items():
        p = parts.get(mpn.get(ref, ""))
        sym = kicad_sch.lib_symbol_for(p) if p else None
        if sym is None:
            continue
        by_name: dict[str, list[str]] = {}
        for lp in sym.pins:
            by_name.setdefault(lp.name, []).append(lp.num)
        for num, net in list(pads.items()):
            lp = next((x for x in sym.pins if x.num == num), None)
            for twin in by_name.get(lp.name, []) if lp and lp.name not in ("", "~", "NC") else []:
                pads.setdefault(twin, net)
    return out


# Standard KiCad footprints for catalogue parts that only give a package name.
PACKAGE_FOOTPRINTS = {
    "SOIC-8": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SOP-8": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
    "SOIC-8-150MIL": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SOIC-14": "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm",
    "SOT-23-5": "Package_TO_SOT_SMD:SOT-23-5", "SOT-23-6": "Package_TO_SOT_SMD:SOT-23-6",
    "TSSOP-8": "Package_SO:TSSOP-8_4.4x3mm_P0.65mm", "TSSOP-14": "Package_SO:TSSOP-14_4.4x5mm_P0.65mm",
    "TSSOP-24": "Package_SO:TSSOP-24_4.4x7.8mm_P0.65mm", "SOD-323F": "Diode_SMD:D_SOD-323F",
    "TO-252": "Package_TO_SOT_SMD:TO-252-2",
}


def footprint_for(p: Part) -> str:
    if ":" in (p.footprint or ""):
        return p.footprint
    return PACKAGE_FOOTPRINTS.get(str(p.ratings.get("package") or "").strip().upper(), "")


def _job(design: Design, parts: dict[str, Part], previous: dict | None) -> dict:
    from cvhack.tooling import kicad_sch
    pads = _pad_nets(design, parts)
    job_parts, seen = [], set()
    for block in design.blocks:
        for inst in block.parts:
            p = parts.get(inst.mpn)
            fp = footprint_for(p) if p else ""
            if inst.ref in seen or p is None or not fp:
                continue
            seen.add(inst.ref)
            job_parts.append({"ref": inst.ref, "footprint": fp, "value": p.value or p.mpn,
                              "subsystem": block.subsystem, "pads": pads.get(inst.ref, {})})
    power = sorted(n for n in kicad_sch.power_nets(design, parts) if n.upper() not in kicad_sch.GND_NAMES)
    return {"name": design.name, "footprint_dir": str(footprint_dir()), "gnd": "GND", "power_nets": power,
            "parts": job_parts, "previous": previous, "outline": design.outline, "board": design.board}


def _kpy(*args: str, timeout: int = 600) -> dict:
    py = kicad_python()
    if py is None:
        raise RuntimeError("KiCad's Python (pcbnew) not found")
    proc = subprocess.run([py, str(BUILD_SCRIPT), *args], capture_output=True, text=True, timeout=timeout)
    lines = [l for l in proc.stdout.strip().splitlines() if l.startswith("{")]
    if proc.returncode != 0 or not lines:
        raise RuntimeError(f"pcb_build {args[0]} failed: {(proc.stderr or proc.stdout).strip()[-800:]}")
    return json.loads(lines[-1])


def place(design: Design, parts: dict[str, Part], cache_dir: Path, previous: dict | None = None) -> BoardBuild:
    if footprint_dir() is None:
        raise RuntimeError("KiCad footprint libraries not found")
    job = _job(design, parts, previous)
    key = hashlib.sha1(json.dumps([_VERSION, job], sort_keys=True).encode()).hexdigest()[:16]
    d = Path(cache_dir) / key
    pcb = d / f"{design.name}.kicad_pcb"
    meta = d / "board.json"
    if not (meta.exists() and pcb.exists()):
        d.mkdir(parents=True, exist_ok=True)
        (d / "board_job.json").write_text(json.dumps(job, indent=1))
        info = _kpy("build", str(d / "board_job.json"), str(pcb))
        info["models_missing"] = fetch_models(pcb)
        meta.write_text(json.dumps(info))
    b = BoardBuild(dir=d, pcb=pcb, info=json.loads(meta.read_text()))
    if not b.glb.exists():
        export_glb(b.pcb, b.glb)
    return b


def route(b: BoardBuild, passes: int = 30) -> BoardBuild:
    """Autoroute a placed board (cached next to it). Raises if Freerouting isn't set up."""
    if b.route_info is not None and b.routed_glb.exists():
        return b
    j, jar = java(), freerouting_jar()
    if not (j and jar):
        raise RuntimeError("Freerouting not set up (run demo/setup_router.sh)")
    info = _kpy("route", str(b.pcb), str(b.routed_pcb), j, jar, str(passes))
    if info["unrouted"]:
        # Freerouting isn't deterministic; one longer retry, keep whichever leaves fewer gaps.
        alt = b.dir / "routed_retry.kicad_pcb"
        retry = _kpy("route", str(b.pcb), str(alt), j, jar, str(passes * 2))
        if len(retry["unrouted"]) < len(info["unrouted"]):
            shutil.move(alt, b.routed_pcb)
            shutil.move(alt.with_suffix(".kicad_pro"), b.routed_pcb.with_suffix(".kicad_pro"))
            info = {**retry, "retried": True}
    info["drc"] = drc(b.routed_pcb)
    export_glb(b.routed_pcb, b.routed_glb)
    (b.dir / "route.json").write_text(json.dumps(info))
    return b


def drc(pcb: Path) -> dict:
    """KiCad DRC summary: real errors vs. cosmetic/library findings, plus unconnected count."""
    out = Path(pcb).with_suffix(".drc.json")
    kicad_cli._run(["pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(pcb)])
    if not out.exists():
        return {"error": "drc failed"}
    data = json.loads(out.read_text())
    real, cosmetic, library = {}, {}, {}
    for v in data.get("violations", []):
        items = [i.get("description", "") for i in v.get("items", [])]
        m = re.search(r"keepout area of (\S+)\)", v.get("description", ""))
        if m and all(f" of {m.group(1)} " in i for i in items):
            bucket = library            # a KiCad library footprint conflicting with its own keepout
        elif v.get("type") in DRC_COSMETIC or v.get("severity") != "error":
            bucket = cosmetic
        else:
            bucket = real
        bucket[v["type"]] = bucket.get(v["type"], 0) + 1
    return {"errors": real, "cosmetic": cosmetic, "library": library,
            "unconnected": len(data.get("unconnected_items", [])),
            "details": [f"{v['type']}: {v['description']} [" + "; ".join(i['description'] for i in v.get('items', []))[:160] + "]"
                        for v in data.get("violations", []) if v.get("type") in real][:20]}


def fetch_models(pcb: Path) -> list[str]:
    """Download the 3D models the board's footprints reference (STEP), if missing. Returns failures."""
    text = Path(pcb).read_text()
    failed = []
    for ref in sorted(set(re.findall(r'\(model "\$\{KICAD\d*_3DMODEL_DIR\}/([^"]+)"', text))):
        rel = re.sub(r"\.wrl$", ".step", ref)
        dest = models_dir() / rel
        miss = dest.with_suffix(".missing")
        if dest.exists() or miss.exists():
            if miss.exists():
                failed.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(MODELS_URL + urllib.request.quote(rel), timeout=30) as r:
                dest.write_bytes(r.read())
        except Exception:
            miss.write_text("not in kicad-packages3D")
            failed.append(rel)
    return failed


def export_glb(pcb: Path, out: Path) -> Path | None:
    mdir = models_dir()
    var_names = set(re.findall(r"\$\{(KICAD\d*_3DMODEL_DIR)\}", Path(pcb).read_text())) or {"KICAD10_3DMODEL_DIR"}
    defines = [a for v in sorted(var_names) for a in ("-D", f"{v}={mdir}")]
    tmp = Path(str(out) + ".tmp.glb")
    proc = kicad_cli._run(["pcb", "export", "glb", "--force", "--subst-models", "--include-tracks",
                           "--include-pads", "--include-zones", "--include-silkscreen", "--include-soldermask",
                           *defines, "--output", str(tmp), str(pcb)])
    if proc.returncode == 0 and tmp.exists():
        shutil.move(tmp, out)      # atomic for readers
        return Path(out)
    return None


def render_glb(design: Design, parts: dict[str, Part], cache_dir: Path, routed: bool = False) -> tuple[Path | None, dict]:
    """One-shot helper (outputs, tests): placed or routed GLB plus info."""
    b = place(design, parts, cache_dir)
    if routed and can_route():
        route(b)
        return b.routed_glb, {**b.info, "route": b.route_info, "pcb": str(b.routed_pcb)}
    return (b.glb if b.glb.exists() else None), {**b.info, "pcb": str(b.pcb)}
