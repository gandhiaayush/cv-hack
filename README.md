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

- The regulation set is a small, hand-written demonstration rule set based on public FDA/IEC concepts. It is **not** legal or regulatory advice.
- Target is schematic + netlist + ERC. Board layout only if time allows.

## Team split (3 people)

| Owner | Area | Deliverables |
|-------|------|--------------|
| **A: Agents** | Orchestration | Mother agent, subsystem agents, re-plan/recovery loop, Crusoe/Nemotron client, run log and step counters |
| **B: Data and rules** | Knowledge | Parts DB (schema, seed data, stock/price fields), FDA rule set as machine-checkable rules with clause IDs, compliance agent and red-team agent, traceability matrix |
| **C: Tooling and demo** | Ground truth and UI | KiCad/SKiDL generation, `kicad-cli` ERC/DRC wrapper, budget checks (power/cost/size), live dashboard (agent tree, requirement matrix, schematic render), scripted demo and constraint-change trigger |

Interfaces to agree in the first 30 minutes (write them in `docs/interfaces.md`):

- Part record schema (B publishes, A and C consume).
- Rule record schema: `id`, `clause`, `applies_to`, `check` (B publishes, A consumes).
- Verifier result format: `{ok, errors:[{code, message, part/net, rule_id}]}` (all three).
- Agent event stream format for the dashboard (A emits, C renders).

## Setup

Requires KiCad (`kicad-cli`), Python 3.11+, and Crusoe Managed Inference credentials in `.env`.
