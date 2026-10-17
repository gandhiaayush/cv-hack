"""kicad-cli wrapper: write the KiCad project, run KiCad's ERC, export SVG/netlist.

write_project() needs no KiCad install (see kicad_sch.py). Everything that
shells out checks available() first; set KICAD_CLI to point at a binary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from cvhack.contracts import Design, Part, VerifierError
from cvhack.tooling import kicad_sch

_CANDIDATES = [
    os.environ.get("KICAD_CLI", ""),
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    str(Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
    shutil.which("kicad-cli") or "",
]
TIMEOUT = 60

# Our symbols are embedded, not from a configured library; KiCad warns about that.
IGNORED_ERC = {"lib_symbol_issues", "lib_symbol_mismatch", "footprint_link_issues"}


def cli_path() -> str | None:
    for c in _CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


def available() -> bool:
    return cli_path() is not None


def default_out_dir(design: Design) -> Path:
    return Path(__file__).resolve().parents[2] / "runs" / "_scratch" / design.name


def write_project(design: Design, parts: dict[str, Part], out_dir: Path) -> Path:
    """Write <name>.kicad_pro, .kicad_sch and .net into out_dir; return the .kicad_sch path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lay = kicad_sch.layout(design, parts)
    sch = out_dir / f"{design.name}.kicad_sch"
    sch.write_text(kicad_sch.to_kicad_sch(design, parts, lay))
    (out_dir / f"{design.name}.kicad_pro").write_text(kicad_sch.to_kicad_pro(design))
    (out_dir / f"{design.name}.net").write_text(kicad_sch.to_kicad_netlist(design, parts))
    return sch


def _run(args: list[str]) -> subprocess.CompletedProcess:
    cli = cli_path()
    if cli is None:
        raise RuntimeError("kicad-cli not found (install KiCad or set KICAD_CLI)")
    return subprocess.run([cli, *args], capture_output=True, text=True, timeout=TIMEOUT)


_ITEM = re.compile(r"Symbol (?P<ref>\S+) \[[^\]]*\]|Symbol (?P<ref2>\S+)|Pin (?P<pin>\S+)")


def _ref_from(desc: str) -> str | None:
    m = re.search(r"Symbol (\S+)", desc)
    return m.group(1) if m else None


def _net_from(desc: str) -> str | None:
    m = re.search(r"Label '([^']+)'|net '?([^'\s]+)'?", desc)
    return (m.group(1) or m.group(2)) if m else None


def run_erc(sch: Path) -> list[VerifierError]:
    """Run KiCad's ERC on a schematic and map violations to VerifierErrors (code KICAD_<TYPE>)."""
    sch = Path(sch)
    report = sch.with_suffix(".erc.json")
    proc = _run(["sch", "erc", "--format", "json", "--severity-all", "--units", "mm",
                 "--output", str(report), str(sch)])
    if not report.exists():
        raise RuntimeError(f"kicad-cli erc failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    data = json.loads(report.read_text())
    errs: list[VerifierError] = []
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            vtype = v.get("type", "unknown")
            if vtype in IGNORED_ERC or v.get("excluded"):
                continue
            items = v.get("items", [])
            descs = [i.get("description", "") for i in items]
            ref = next((r for r in map(_ref_from, descs) if r and not r.startswith("#")), None)
            net = next((n for n in map(_net_from, descs) if n), None)
            msg = v.get("description", vtype)
            if descs:
                msg += ": " + "; ".join(descs)
            errs.append(VerifierError(code=f"KICAD_{vtype.upper()}", message=msg, part=ref, net=net,
                                      severity="error" if v.get("severity") == "error" else "warning"))
    return errs


def export_svg(sch: Path, out_dir: Path) -> Path | None:
    """KiCad's own schematic render. Returns the SVG path, or None if it failed."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(["sch", "export", "svg", "--no-background-color", "--exclude-drawing-sheet",
                 "--output", str(out_dir), str(sch)])
    svgs = sorted(out_dir.glob(f"{Path(sch).stem}*.svg"))
    return svgs[0] if proc.returncode == 0 and svgs else None


def export_netlist(sch: Path, out: Path) -> Path | None:
    proc = _run(["sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(out), str(sch)])
    return Path(out) if proc.returncode == 0 and Path(out).exists() else None


def export_pdf(sch: Path, out: Path) -> Path | None:
    proc = _run(["sch", "export", "pdf", "--output", str(out), str(sch)])
    return Path(out) if proc.returncode == 0 and Path(out).exists() else None
