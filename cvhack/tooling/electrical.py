"""ElectricalVerifier: the ground-truth gate A calls after every design change.

    from cvhack.tooling.electrical import ElectricalVerifier
    result = ElectricalVerifier().verify(design, parts)

Stages: native ERC -> budgets/stock -> (if kicad-cli is installed) generate
a KiCad schematic and run KiCad's own ERC. Warnings don't fail the gate.
"""

from __future__ import annotations

from pathlib import Path

from cvhack.contracts import Design, Part, VerifierResult
from cvhack.tooling import budgets, erc, kicad_cli


class ElectricalVerifier:
    name = "electrical"

    def __init__(self, out_dir: Path | str | None = None, use_kicad: bool = True):
        self.out_dir = Path(out_dir) if out_dir else None
        self.use_kicad = use_kicad

    def verify(self, design: Design, parts: dict[str, Part]) -> VerifierResult:
        errors = erc.check(design, parts)
        budget_errs, details = budgets.check(design, parts)
        errors += budget_errs

        details["kicad"] = "skipped"
        # Only hand KiCad designs whose parts/pins resolve; otherwise the
        # native errors above already say what's wrong.
        structural = {"PART_UNKNOWN", "PIN_UNKNOWN", "REF_UNKNOWN", "REF_CONFLICT"}
        if self.use_kicad and kicad_cli.available() and not any(e.code in structural for e in errors):
            out = self.out_dir or kicad_cli.default_out_dir(design)
            try:
                sch = kicad_cli.write_project(design, parts, out)
                kerrs = kicad_cli.run_erc(sch)
                errors += kerrs
                details["kicad"] = {"schematic": str(sch), "erc_violations": len(kerrs)}
            except Exception as exc:  # KiCad trouble must never crash the agent loop
                details["kicad"] = f"error: {exc}"

        ok = not any(e.severity == "error" for e in errors)
        return VerifierResult(verifier=self.name, ok=ok, errors=errors, details=details)
