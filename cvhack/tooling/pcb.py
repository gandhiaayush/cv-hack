"""Design -> placed KiCad board -> GLB for the 3D viewer.

    pcb_path = build_board(design, parts, out_dir)      # .kicad_pcb via KiCad's own pcbnew
    glb_path = export_glb(pcb_path, out_dir / "board.glb")
    glb = render_glb(design, parts, cache_dir)          # both, cached by content

The board is placed (per-subsystem clusters, ground poured on both layers)
but not routed. 3D models come from KiCad's official kicad-packages3D repo
and are downloaded on first use into runs/_3dmodels (KICAD_3DMODEL_DIR
overrides). Without them the GLB still has the board, pads and silkscreen.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from cvhack.contracts import Design, Part
from cvhack.tooling import kicad_cli

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = Path(__file__).resolve().parent / "pcb_build.py"
MODELS_URL = "https://gitlab.com/kicad/libraries/kicad-packages3D/-/raw/master/"
_VERSION = "3"


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


def available() -> bool:
    return bool(kicad_cli.available() and kicad_python() and footprint_dir())


def _job(design: Design, parts: dict[str, Part]) -> dict:
    nets_of = {}
    for name, nodes in design.merged_nets().items():
        for n in nodes:
            nets_of.setdefault(n.ref, {})[n.pin] = name
    job_parts, seen = [], set()
    for block in design.blocks:
        for inst in block.parts:
            p = parts.get(inst.mpn)
            if inst.ref in seen or p is None or ":" not in p.footprint:
                continue
            seen.add(inst.ref)
            job_parts.append({"ref": inst.ref, "footprint": p.footprint, "value": p.value or p.mpn,
                              "subsystem": block.subsystem, "pads": nets_of.get(inst.ref, {})})
    return {"name": design.name, "footprint_dir": str(footprint_dir()), "gnd": "GND", "parts": job_parts}


def build_board(design: Design, parts: dict[str, Part], out_dir: Path) -> tuple[Path, dict]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    py = kicad_python()
    if py is None or footprint_dir() is None:
        raise RuntimeError("KiCad's Python (pcbnew) or footprint libraries not found")
    job = out_dir / "board_job.json"
    job.write_text(json.dumps(_job(design, parts), indent=1))
    pcb = out_dir / f"{design.name}.kicad_pcb"
    proc = subprocess.run([py, str(BUILD_SCRIPT), str(job), str(pcb)], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not pcb.exists():
        raise RuntimeError(f"board build failed: {proc.stderr.strip()[-800:]}")
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    return pcb, info


def fetch_models(pcb: Path) -> list[str]:
    """Download the 3D models the board's footprints reference (STEP), if missing. Returns failures."""
    text = Path(pcb).read_text()
    failed = []
    for ref in sorted(set(re.findall(r'\(model "\$\{KICAD\d*_3DMODEL_DIR\}/([^"]+)"', text))):
        rel = re.sub(r"\.wrl$", ".step", ref)
        dest = models_dir() / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(MODELS_URL + urllib.request.quote(rel), timeout=30) as r:
                dest.write_bytes(r.read())
        except Exception:
            failed.append(rel)
    return failed


def export_glb(pcb: Path, out: Path) -> Path | None:
    mdir = models_dir()
    var_names = set(re.findall(r"\$\{(KICAD\d*_3DMODEL_DIR)\}", Path(pcb).read_text())) or {"KICAD10_3DMODEL_DIR"}
    defines = [a for v in sorted(var_names) for a in ("-D", f"{v}={mdir}")]
    proc = kicad_cli._run(["pcb", "export", "glb", "--force", "--subst-models", "--include-tracks",
                           "--include-pads", "--include-zones", "--include-silkscreen", "--include-soldermask",
                           *defines, "--output", str(out), str(pcb)])
    return Path(out) if proc.returncode == 0 and Path(out).exists() else None


def render_glb(design: Design, parts: dict[str, Part], cache_dir: Path) -> tuple[Path | None, dict]:
    """Board + GLB for a design, cached by content. Returns (glb path or None, info)."""
    used = sorted({i.mpn for i in design.all_parts()})
    key = hashlib.sha1(json.dumps([_VERSION, design.model_dump(), [parts[m].footprint for m in used if m in parts]],
                                  sort_keys=True).encode()).hexdigest()[:16]
    d = Path(cache_dir) / key
    glb, meta = d / "board.glb", d / "board.json"
    if glb.exists() and meta.exists():
        return glb, json.loads(meta.read_text())
    pcb, info = build_board(design, parts, d)
    info["models_missing"] = fetch_models(pcb)
    out = export_glb(pcb, glb)
    info["pcb"] = str(pcb)
    meta.write_text(json.dumps(info))
    return out, info
