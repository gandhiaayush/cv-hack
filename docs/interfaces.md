# Interfaces

These are the seams between A (agents), B (data and rules) and C (tooling and demo). The source of truth is [`cvhack/contracts.py`](../cvhack/contracts.py), where every model is Pydantic. To change a contract, message C (who owns the file) rather than editing it directly.

## Who calls what

| Caller | Call | Provided by |
|---|---|---|
| A | `cvhack.events.emit(run_id=..., agent_id=..., parent_id=..., kind=..., payload=...)` | C |
| A | `ElectricalVerifier().verify(design, parts) -> VerifierResult` | C |
| A | `cvhack.control.read_commands(run_id, offset) -> (cmds, new_offset)`, polled between steps | C |
| A | `cvhack.outputs.write_outputs(run_id)` on `run_done` (optional; the dashboard has an Export button too) | C |
| A, C | `cvhack.parts.load_parts() -> dict[mpn, Part]` | B's DB through the hook in `parts.py` (mock until then) |
| A | `ComplianceVerifier().verify(design, parts)`, `RedTeamVerifier().verify(design, parts)` | B |

Every verifier follows the same `Verifier` protocol (a `name` attribute plus `verify(design, parts) -> VerifierResult`), so A can run them all in one loop.

**Where parts come from.** `load_parts()` merges three sources by MPN:

1. `cvhack/mocks/parts.json`: C's mock catalogue, with full electrical data.
2. `data/parts.json`: B's sourced dataset, in B's schema (`part_id`, `specs`, `price.unit_amount`, `stock.status`). `cvhack.parts.from_b_record` maps it onto `Part`. B's description, specs, price and stock override the mock, but the mock's pins and symbol are kept.
3. `cvhack/part_overlay.json`: C's KiCad symbol, footprint, pins, `power_mw` and `area_mm2` for B's parts. A B part with no overlay entry has no pins, so the electrical gate reports `PIN_UNKNOWN` instead of guessing a pinout. The `_missing` list in that file shows which parts still need one.

`PARTS_SOURCE=path/to/parts.json` (a plain list of `Part` records) replaces all three. A `cvhack/data/parts_db.py` with `all_parts()` replaces source 2.

## Part (B)

```json
{"mpn": "AP2112K-3.3TRG1", "category": "ldo", "description": "600mA LDO 3.3V SOT-23-5",
 "value": "", "footprint": "Package_TO_SOT_SMD:SOT-23-5",
 "symbol_lib": "Regulator_Linear", "symbol_name": "AP2112K-3.3",
 "pins": [{"num": "1", "name": "VIN", "type": "power_in"}, {"num": "5", "name": "VOUT", "type": "power_out"}],
 "price_usd": 0.21, "stock": 60000, "power_mw": 0.3, "area_mm2": 9,
 "ratings": {"vin_max": 6.0, "vout": 3.3}}
```

`price_usd` and `stock` may be `null`, which means **unknown**. `stock: 0` means out of stock. Unknown values produce `BOM_PRICE_UNKNOWN` and `BOM_STOCK_UNKNOWN` warnings, and an unknown price counts as $0 in the cost total.

Pin `type` is one of the KiCad electrical types: `input, output, bidirectional, passive, power_in, power_out, open_collector, tri_state, no_connect, unspecified`. ERC depends on these types, so get them right, especially `power_in` and `power_out`. Every pin a design uses must appear in `pins`.

## Rule (B)

```json
{"id": "R-EMC-003", "clause": "IEC 61000-4-2", "applies_to": "connector",
 "check": "has_tvs_on_external_power", "description": "ESD protection on external connectors"}
```

## Design / SchematicBlock (A produces; C verifies and renders)

```json
{"name": "ppg-wearable-class2",
 "budgets": {"power_mw": 30, "cost_usd": 15, "area_mm2": 700},
 "power_flags": ["VBUS", "GND"],
 "no_connects": [{"ref": "U2", "pin": "4"}],
 "blocks": [{"subsystem": "power",
             "parts": [{"ref": "U2", "mpn": "AP2112K-3.3TRG1"}],
             "nets": [{"name": "3V3", "nodes": [{"ref": "U2", "pin": "5"}, {"ref": "C4", "pin": "1"}]}],
             "ports": ["3V3", "GND"]}]}
```

- **Nets merge across blocks by name.** `3V3` in the power block and `3V3` in the MCU block are the same net.
- **Refs must be globally unique.** Pick a prefix range per subsystem to avoid collisions.
- **`power_flags`** lists nets powered from off the board (USB VBUS, GND). It works like KiCad's PWR_FLAG. Never flag a net that a `power_out` pin already drives: that's an `ERC_POWER_CONFLICT`.
- **`no_connects`** lists pins left open on purpose. Any other pin that isn't on a net is an `ERC_UNCONNECTED` error.
- Full examples: [`design_wearable.json`](../cvhack/mocks/design_wearable.json) (passes) and [`design_broken.json`](../cvhack/mocks/design_broken.json) (fails).

## VerifierResult (all three)

```json
{"verifier": "electrical", "ok": false,
 "errors": [{"code": "BUDGET_POWER", "message": "power_mw 92.4 mW exceeds budget 30 mW",
             "part": null, "net": null, "rule_id": null, "severity": "error"}],
 "details": {"totals": {...}, "kicad": {"erc_violations": 0}}}
```

`ok` is false only when there is an error. Warnings don't fail the gate.

Electrical error codes:

| Group | Codes |
|---|---|
| Native ERC | `ERC_UNCONNECTED`, `ERC_SINGLE_PIN_NET`, `ERC_POWER_NOT_DRIVEN`, `ERC_POWER_CONFLICT`, `ERC_OUTPUT_CONFLICT`, `ERC_INPUT_NOT_DRIVEN`, `ERC_NC_CONNECTED`, `BLOCK_PORT_DANGLING` (warning) |
| Structural | `PART_UNKNOWN`, `PIN_UNKNOWN`, `REF_UNKNOWN`, `REF_CONFLICT` |
| Budget / BOM | `BUDGET_POWER`, `BUDGET_COST`, `BUDGET_AREA`, `BOM_OUT_OF_STOCK`, `BOM_STOCK_UNKNOWN` (warning), `BOM_PRICE_UNKNOWN` (warning) |
| KiCad's own ERC | `KICAD_<TYPE>`, e.g. `KICAD_PIN_NOT_CONNECTED`, `KICAD_POWER_PIN_NOT_DRIVEN` |

For B's verifiers, set `rule_id` and put the clause in `message`, so the matrix can cite it.

## AgentEvent (A emits; C renders)

```json
{"run_id": "r-42", "agent_id": "power.charger", "parent_id": "power", "kind": "step",
 "payload": {"message": "Picked MCP73831"}, "ts": 1792265466.5}
```

The tree is built from `parent_id`; only the root `mother` has `null`. Payloads by kind:

| kind | payload |
|---|---|
| `agent_spawned` | `{role: "mother"\|"subsystem"\|"verifier", label}` |
| `agent_status` | `{status: "running"\|"waiting"\|"done"\|"failed"}` |
| `step` | `{message}` |
| `delegate` | `{to, subsystem, budget: {power_mw?, cost_usd?, area_mm2?}, constraints: [str]}` |
| `report` | `{to, options: [{name, cost?, power_mw?, notes?}], conflicts: [str], cannot_meet: [str], needs: [str]}` |
| `decision` | `{to, chosen?, budget_changes?: {}, extra_constraints?: [str], spawn?: [agent_id], message?}` |
| `verifier_result` | the `VerifierResult` fields |
| `replan` | `{reason, changes: [str]}` |
| `requirement_status` | `{req_id, text, status: "pending"\|"pass"\|"fail", parts: [ref], rule_ids: [id]}` |
| `design_updated` | `{design: Design}`, the whole current design, every time it changes |
| `constraint_change` | echo of the ControlCommand payload, emitted when A applies it |
| `run_done` | `{ok, summary}` |

Events are appended to `runs/<run_id>/events.jsonl`, so the agents and the dashboard can run in different processes.

## ControlCommand (dashboard → mother agent)

The dashboard appends to `runs/<run_id>/control.jsonl`:

```json
{"kind": "constraint_change", "payload": {"type": "out_of_stock", "mpn": "AP2112K-3.3TRG1"}}
{"kind": "constraint_change", "payload": {"type": "device_class", "value": "III"}}
```

The mother agent polls `read_commands(run_id, offset)` between steps, emits `constraint_change` once it picks a command up, and then replans. For a working reference loop, see `FakeRun.poll_control` and `FakeRun.handle` in [`cvhack/mocks/fake_run.py`](../cvhack/mocks/fake_run.py).
