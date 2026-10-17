# cv-hack

Built for the Cerebral Valley x Crusoe **Recursive Agents Hackathon** (Oct 17, 2026, San Francisco).

Theme: *build a team of agents you can walk away from.*

## What we're building

A long-horizon PCB-design system. Nemotron (served on Crusoe Managed Inference) takes a product spec, a parts database and an FDA regulation list, and produces a compliant schematic, a BOM and a traceability matrix, with no human in the loop.

A **mother agent** decomposes the spec into subsystems and owns the plan, budgets and run log. **Subsystem agents** pick parts and draft schematic blocks. **Verifier agents** gate every step. Failures return to the mother agent as structured errors, and it re-plans, swaps parts or reallocates budget.

## Architecture

```
spec ──► Mother agent (Nemotron)
           │  decompose, budget, re-plan, run log
           ├─► Power agent      ┐
           ├─► Analog FE agent  │ pick parts from DB, draft schematic block
           ├─► MCU/comms agent  │ (SKiDL / KiCad netlist)
           └─► Protection agent ┘
                    │
                    ▼
           Verifiers (the gate)
             ├─ Compliance agent : parts + design vs. reg rules, cites clause
             ├─ Electrical agent : kicad-cli ERC/DRC, power/cost/size budgets
             └─ Red-team agent   : hunts violations the others missed
                    │ pass → merge into design   fail → structured error → mother agent
                    ▼
           Output: KiCad project, BOM, requirement-to-part traceability matrix
```

Ground truth is real tooling (`kicad-cli` ERC/DRC and rule checks), not a model scoring a model.

## Negotiation loop (mother ↔ subsystem agents)

Subsystem agents are not just executors. Each one first **investigates** the constraints that touch its subsystem (applicable reg clauses, parts availability, power/cost/size share) and reports back to the mother agent before committing to a design.

```
mother ──delegate(subsystem, budget, constraints)──► subsystem agent
subsystem agent ──report──► mother
    {feasible options, conflicts found, constraints it cannot meet,
     what it needs from other subsystems, cost of each option}
mother ──decision──► subsystem agent
    {chosen option, budget changes, extra constraints, or spawn sub-subsystem}
subsystem agent ──► may spawn child agents (e.g. power → battery, regulator, charger)
                    and repeat the same report/decide cycle
```

- The mother resolves cross-subsystem conflicts (for example, the analog front end wants a low-noise supply that blows the power agent's cost budget) by reallocating budgets or choosing between options.
- Delegation is recursive with a depth and step budget, so the tree can grow as deep as the design needs.
- Every report and decision is an event in the run log, so the dashboard can show the negotiation, not just the result.

## Demo

1. Hand the system a spec (e.g. battery-powered Class II wearable) and walk away.
2. A live agent tree and a requirements matrix turning green.
3. Mid-run we change a constraint (a part goes out of stock, or the device class goes up) and the system re-plans on its own.
4. Final output: schematic rendered from KiCad, BOM, compliance matrix.

## Scope and caveats

- The current rule set contains project-defined prototype wiring checks, not FDA/IEC regulatory clauses or a regulatory assessment. It is **not** legal or regulatory advice.
- Target is schematic + netlist + ERC. Board layout only if time allows.

## Team split (3 people)

| Owner | Area | Deliverables |
|-------|------|--------------|
| **A: Agents** | Orchestration | Mother agent, subsystem agents, re-plan/recovery loop, Crusoe/Nemotron client, run log and step counters |
| **B: Data and rules** | Knowledge | Parts DB (schema, seed data, stock/price fields), FDA rule set as machine-checkable rules with clause IDs, compliance agent and red-team agent, traceability matrix |
| **C: Tooling and demo** | Ground truth and UI | KiCad/SKiDL generation, `kicad-cli` ERC/DRC wrapper, budget checks (power/cost/size), live dashboard (agent tree, requirement matrix, schematic render), scripted demo and constraint-change trigger |

## Shared interfaces and Part B data plan

Use these contracts as the initial integration target. Keep IDs stable across the
parts database, agent messages, verifier results, BOM, and traceability matrix.
Agree on any changes with the affected owners before implementation.

### Part records (B publishes; A and C consume)

Store one record per orderable component. Use a stable `part_id` such as
`part:<manufacturer-slug>:<normalized-mpn>`; do not use a display name as an ID.
Normalize by lowercasing and replacing each run of non-alphanumeric characters
with a single hyphen, then trim hyphens at the ends.
Keep specification values machine-readable and include units. A minimal record:

```json
{
  "part_id": "part:<manufacturer-slug>:<normalized-mpn>",
  "manufacturer": "<manufacturer>",
  "mpn": "<manufacturer-part-number>",
  "category": "<controlled-category>",
  "description": "<short description>",
  "package": "<package or null>",
  "specs": {
    "<spec-name>": {"value": 0, "unit": "<unit>"},
    "<range-name>": {"min": 0, "max": 0, "unit": "<unit>"}
  },
  "price": {"unit_amount": null, "currency": "USD", "checked_at": null, "source_url": null},
  "stock": {"available_qty": null, "status": "unknown", "checked_at": null, "source_url": null},
  "sources": ["<source-id>"]
}
```

`category` comes from a small controlled vocabulary (for example `power`,
`mcu`, `analog`, `protection`, `passive`, `connector`). Use explicit units in
`specs`; do not encode a number and its unit in a string. `stock.status` is
`in_stock`, `out_of_stock`, or `unknown`. Use `unknown` rather than inventing
availability; record the source and date for mutable stock and price data.
Where a value is not known, use `null` or omit it rather than guessing. Keep
source records in a shared registry and attach stable source IDs to parts and
specification fields so claims can be traced to evidence.

Represent requirement-to-part suitability as explicit links, not as an
unsupported claim buried in a description:

```json
{"requirement_id": "<stable-requirement-id>", "part_id": "<part-id>", "role": "<design role>"}
```

The mother agent (A) owns requirement IDs and subsystem assignments; B should
not create competing IDs. A and B should align the controlled category/role
names with C's KiCad/SKiDL and budget checks.

### Seed dataset (B)

The initial 17-record seed is in [`data/parts.json`](./data/parts.json). It
covers candidate BLE modules, battery converters, environmental sensors,
fuel gauges, ESD protection, and common passives/connectors, with source IDs
and screening notes. This catalog currently assumes a non-medical
environmental wearable and **does not match the checked-in Class II PPG mock
BOM** in `cvhack/mocks/design_wearable.json`. Do not use it as that mock's
approved parts list. Functional groups indicate broad purpose only—not
drop-in substitutions or verified requirement suitability.

Live distributor prices and stock were not captured in this seed: these values
are `null`/`unknown`. Keep them that way until a named distributor, quantity,
currency, and timestamp are recorded. The stock-out demo should apply an
explicit, labeled runtime scenario override rather than changing sourced facts
or implying inventory was checked. An example synthetic override is in
[`data/demo_scenarios.json`](./data/demo_scenarios.json). Do not mark a part
suitable until its values meet the actual requirement; keep suitability links
separate and verifiable.

### Relationship graph for the PPG mock

[`data/relationships.json`](./data/relationships.json) is generated from the
PPG mock design and its runtime mock parts registry. It links every placed
reference designator to its part identity and subsystem, every mock symbol pin
to its net or explicit no-connect, and each relationship to the repository
fixture that supports that structural claim. Run
`python -m cvhack.knowledge_graph --write` after changing either mock input or
[`data/rules.json`](./data/rules.json), or `python -m cvhack.knowledge_graph
--check` to validate that the checked-in graph is current and has no dangling
nodes, pins, rule targets, or evidence references.

The graph covers all 29 placed instances, 20 distinct MPNs, 4 subsystems, 17
nets, and all 83 mock symbol pins (81 connected and 2 intentionally
no-connect). The graph also includes B's project-defined demo rules and their
links to affected mock parts/nets. The mock fixtures are not manufacturer
datasheets: official manufacturer evidence is still missing for those 20 PPG
MPNs. Formal requirement links remain pending A's authoritative requirement
IDs, and per-run verifier-result links require actual verifier outputs. Do not
interpret structural connectivity as a requirement pass or regulatory finding.

### Rules, verifier results, and events

- B's initial rules are in [`data/rules.json`](./data/rules.json). They check
  selected wiring and mock-BOM assumptions for the PPG demo using the
  allow-listed structured operators in `operator_catalog` (no executable
  expressions). Every rule has a stable ID, design scope, affected graph
  targets, evidence-source IDs, rationale, severity, and failure code.
  `clause` values are project-defined demo identifiers, **not** citations to
  FDA/IEC regulatory clauses. These checks are not a compliance or safety
  assessment, and their fixture-based assumptions need review against the
  finalized requirements and manufacturer documents.
- Treat missing inputs and unknown values as explicit indeterminate/failure
  results, never as a pass. Implementing and running the rule evaluator is a
  separate verifier task.
- All verifiers return
  `{ "ok": true, "errors": [{ "code": "...", "message": "...", "part_id": "...", "net": "...", "rule_id": "..." }] }`.
  Use `errors: []` on success. Include the applicable identifiers; omit fields
  that do not apply. A failed check must not be reported as success.
- A owns and emits the agent event stream; C consumes it for the dashboard.
  Agree its event fields before wiring the UI.
- The traceability matrix is built from stable requirement, part, rule, and
  verifier-result IDs, so every coverage or failure entry can be traced to its
  evidence.

## Setup

Requires KiCad (`kicad-cli`), Python 3.11+, and Crusoe Managed Inference credentials in `.env`.

## Running the demo

```
demo/preflight.sh                 # checklist: tests, build, KiCad, fake run, credentials
demo/run_demo.sh                  # dashboard + scripted fake run (no LLM needed)
demo/run_demo.sh --real           # dashboard + A's agents, bridged from runs/run.db
demo/run_demo.sh --replay         # dashboard + recorded run (stage fallback)
```

The dashboard opens at http://localhost:8000. Use **Mark out of stock** or **Raise to Class III** mid-run to trigger a re-plan, and open `/#negotiation` to show the mother/subsystem negotiation.
