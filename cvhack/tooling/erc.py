"""Native electrical rules check on a Design.

Uses the same pin-type rules as KiCad's ERC matrix (simplified), so it runs
without KiCad installed and gives errors keyed to our refs and nets. When
kicad-cli is available, kicad_cli.run_erc() adds KiCad's own ERC on top.
"""

from __future__ import annotations

from collections import Counter

from cvhack.contracts import Design, Part, VerifierError

DRIVERS = {"output", "power_out", "bidirectional", "tri_state", "open_collector", "passive"}
STRONG_OUTPUTS = {"output", "power_out"}


def check(design: Design, parts: dict[str, Part]) -> list[VerifierError]:
    errs: list[VerifierError] = []

    # --- refs and part numbers -------------------------------------------------
    ref_mpn: dict[str, str] = {}
    for inst in design.all_parts():
        if inst.ref in ref_mpn and ref_mpn[inst.ref] != inst.mpn:
            errs.append(VerifierError(code="REF_CONFLICT", part=inst.ref,
                                      message=f"{inst.ref} is both {ref_mpn[inst.ref]} and {inst.mpn}"))
        ref_mpn.setdefault(inst.ref, inst.mpn)
        if inst.mpn not in parts:
            errs.append(VerifierError(code="PART_UNKNOWN", part=inst.ref,
                                      message=f"{inst.ref}: part {inst.mpn} is not in the parts DB"))

    def pin_type(ref: str, pin: str) -> str | None:
        p = parts.get(ref_mpn.get(ref, ""))
        if p is None:
            return None
        for pn in p.pins:
            if pn.num == pin:
                return pn.type
        return None

    nets = design.merged_nets()
    nc = {(n.ref, n.pin) for n in design.no_connects}
    connected: set[tuple[str, str]] = set()

    # --- per-net checks ----------------------------------------------------------
    for name, nodes in nets.items():
        uniq = list(dict.fromkeys((n.ref, n.pin) for n in nodes))
        types: list[str] = []
        for ref, pin in uniq:
            connected.add((ref, pin))
            if ref not in ref_mpn:
                errs.append(VerifierError(code="REF_UNKNOWN", part=ref, net=name,
                                          message=f"net {name} references {ref}, which is not placed"))
                continue
            t = pin_type(ref, pin)
            if t is None:
                if ref_mpn[ref] in parts:
                    errs.append(VerifierError(code="PIN_UNKNOWN", part=ref, net=name,
                                              message=f"{ref} ({ref_mpn[ref]}) has no pin {pin}"))
                continue
            if t == "no_connect" or (ref, pin) in nc:
                errs.append(VerifierError(code="ERC_NC_CONNECTED", part=ref, net=name,
                                          message=f"{ref}.{pin} is marked no-connect but is on net {name}"))
            types.append(t)

        if len(uniq) < 2:
            errs.append(VerifierError(code="ERC_SINGLE_PIN_NET", net=name,
                                      message=f"net {name} has only one pin; it goes nowhere"))
        counts = Counter(types)
        if counts["output"] + counts["power_out"] > 1 and counts["output"] > 0:
            errs.append(VerifierError(code="ERC_OUTPUT_CONFLICT", net=name,
                                      message=f"net {name} is driven by more than one output"))
        if counts["power_out"] > 1:
            errs.append(VerifierError(code="ERC_POWER_CONFLICT", net=name,
                                      message=f"net {name} has {counts['power_out']} power outputs shorted together"))
        if counts["power_in"] and not counts["power_out"] and name not in design.power_flags:
            errs.append(VerifierError(code="ERC_POWER_NOT_DRIVEN", net=name,
                                      message=f"net {name} feeds power inputs but nothing supplies it"))
        if counts["input"] and not any(counts[d] for d in DRIVERS) and name not in design.power_flags:
            errs.append(VerifierError(code="ERC_INPUT_NOT_DRIVEN", net=name,
                                      message=f"net {name} has inputs but no driver"))

    # --- every pin must be connected or explicitly no-connect ------------------------
    for ref, mpn in ref_mpn.items():
        p = parts.get(mpn)
        if p is None:
            continue
        for pn in p.pins:
            key = (ref, pn.num)
            if key in connected or key in nc or pn.type == "no_connect":
                continue
            errs.append(VerifierError(code="ERC_UNCONNECTED", part=ref,
                                      message=f"{ref} pin {pn.num} ({pn.name}) is not connected"))

    # --- block ports must exist in some other block --------------------------------
    owners: dict[str, set[str]] = {}
    for b in design.blocks:
        for n in b.nets:
            owners.setdefault(n.name, set()).add(b.subsystem)
    for b in design.blocks:
        for port in b.ports:
            if owners.get(port, set()) - {b.subsystem} == set():
                errs.append(VerifierError(code="BLOCK_PORT_DANGLING", net=port, severity="warning",
                                          message=f"{b.subsystem} exposes {port} but no other block uses it"))
    return errs
