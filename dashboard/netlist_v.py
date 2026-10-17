"""Verilog-style structural netlist of the current design.   GET /api/runs/{id}/netlist.v

Reads like an HDL netlist: one wire per net, one instance per part with named port connections taken from the
part's pinout. Unconnected pins are shown as empty connections with a comment, so open pins are visible."""
import re
import time

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from cvhack.parts import load_parts


def _id(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return ("n_" + s) if re.match(r"\d", s) else s


def to_verilog(design, parts: dict, run_id: str = "", version: int = 0) -> str:
    nets = design.merged_nets()
    pin_net: dict[tuple[str, str], str] = {}
    for name, nodes in nets.items():
        for nd in nodes:
            pin_net[(nd.ref, nd.pin)] = name
    ncs = {(n.ref, n.pin) for n in design.no_connects}
    out = [f"// {design.name}  (run {run_id}, design v{version})",
           "// Structural netlist generated from the agents' design. Not synthesizable HDL: parts are black boxes.",
           f"// budgets: {design.budgets.power_mw:g} mW, ${design.budgets.cost_usd:g}, {design.budgets.area_mm2:g} mm2",
           "", f"module {_id(design.name)} ();", ""]
    out.append("  // ---- nets ----")
    for n in sorted(nets):
        out.append(f"  wire {_id(n)};")
    for blk in design.blocks:
        out += ["", f"  // ---- subsystem: {blk.subsystem} ----"]
        for inst in blk.parts:
            p = parts.get(inst.mpn)
            if p is None:
                out.append(f"  /* {inst.ref}: {inst.mpn} not in the parts DB */")
                continue
            price = f"${p.price_usd:g}" if p.price_usd is not None else "price unknown"
            tag = " (custom)" if p.ratings.get("custom") else ""
            conns, used = [], {}
            for pin in p.pins:
                net = pin_net.get((inst.ref, pin.num))
                nm = _id(pin.name) if pin.name else ""
                if not nm.strip("_") or nm in used:  # '+', '-', '~' or repeated names: keep ports unique
                    nm = f"{nm.strip('_') or 'P'}{pin.num}" if not nm.strip("_") else f"{nm}_{pin.num}"
                used[nm] = 1
                if net:
                    conns.append(f"    .{nm}({_id(net)})")
                else:
                    why = "no-connect" if (inst.ref, pin.num) in ncs else "UNCONNECTED"
                    conns.append(f"    .{nm}()  /* {why} pin {pin.num} */")
            out.append(f"  // {inst.ref}: {p.description[:60]} | {price}{tag}")
            if conns:
                out.append(f"  {_id(inst.mpn)} {inst.ref} (")
                out.append(",\n".join(conns).replace("),\n    .", "),\n    .") + "\n  );")
            else:
                out.append(f"  {_id(inst.mpn)} {inst.ref} ();  // no pinout")
    out += ["", "endmodule", ""]
    return "\n".join(out)


def install(app, resolve, state):
    router = APIRouter()

    @router.get("/api/runs/{run_id}/netlist.v", response_class=PlainTextResponse)
    def netlist(run_id: str):
        rid = resolve(run_id)
        s = state(rid)
        d = s.design_obj()
        if d is None:
            return PlainTextResponse("// waiting for the first design…\n")
        return PlainTextResponse(to_verilog(d, load_parts(), rid, s.design_version))

    app.include_router(router)
