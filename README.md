# cv-hack

Built for the Cerebral Valley x Crusoe **Recursive Agents Hackathon** (Oct 17, 2026, San Francisco).

Theme: *build a team of agents you can walk away from.*

## What we're building

A long-horizon PCB-design system. Nemotron (served on Crusoe Managed Inference) takes a product spec
([`data/spec.json`](./data/spec.json)), a 500-part real catalog and FDA regulations, and produces a wired,
ERC-checked schematic, a BOM and a requirement traceability matrix, with no human in the loop. Every exit is
gated by code: the models propose, deterministic checks accept.

## How a real run works

```
data/spec.json ──► Mother agent (Nemotron Super)
                     │ one delegate_batch: 1 regulatory + 4 subsystems × 2 strategies (lowest_cost, robust_sourcing)
                     ├─► specialist agents (Nemotron Lightning) ── search_parts / find_alternates / get_part_medical
                     │        └─ submit_report ──► stage-1 gate (src/gate.py): part exists, category, voltage
                     │                              rating vs rail, stock, pinout known, price/power known
                     ├─► regulatory agent ── classify_device / CFR sections ── findings gate (quotes must be fetched)
                     ├─► sourcing agent (optional) ── check_supply / alternates
                     │
                     └─ finalize ──► budget check (DB prices) ──► critic on a different model (GLM) ──► answer
                                     every objection ──► wiring phase (src/wiring.py):
                                       refs assigned, one wiring agent per subsystem wires real KiCad pins
                                       ──► wiring gate (ERC, grounding) per block
                                       ──► merged design ──► ElectricalVerifier (native ERC, budgets, stock,
                                           KiCad ERC when installed), one repair round for cross-block errors
                                       ──► requirements (src/requirements.py) evaluated from data/spec.json
```

The agents log to `runs/run.db`; `python -m cvhack.bridge` turns that into the dashboard's event stream
(agent tree, negotiation, gate and critic results, design, ERC, requirements). The dashboard's **Mark out of
stock** and **Raise to Class III** buttons write `runs/<run>/control.jsonl`; the mother picks them up between
steps (or, after the design is accepted, during `LINGER`), re-gates every option that used the part, and
re-plans.

Run modes: `demo/run_demo.sh` (scripted fake run, no LLM), `--replay` (recorded run, stage fallback) and
`--real` (the pipeline above, at the catalog's dated LCSC prices).

## Scope and caveats

- The current rule set contains project-defined prototype wiring checks, not FDA/IEC regulatory clauses or a regulatory assessment. It is **not** legal or regulatory advice.
- Target is schematic + netlist + ERC. Wiring is checked electrically (ERC, grounding, drivers), not
  functionally; a passing run is a reviewed starting point, not a validated design.

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
nets, and all 85 mock symbol pins (83 connected and 2 intentionally
no-connect). The graph also includes B's project-defined demo rules and their
links to affected mock parts/nets. The mock fixtures are not manufacturer
datasheets: official manufacturer evidence is still missing for those 20 PPG
MPNs. Formal requirement links remain pending A's authoritative requirement
IDs, and per-run verifier-result links require actual verifier outputs. Do not
interpret structural connectivity as a requirement pass or regulatory finding.

### Real parts catalog and knowledge graph

[`data/parts_catalog.json`](./data/parts_catalog.json) holds 500 real, orderable
parts in B's record schema, scraped from two public sources:

- **LCSC/JLCPCB catalog**, via the daily [jlcparts](https://yaqwsx.github.io/jlcparts/)
  mirror: MPN, manufacturer, package, datasheet link, USD price tiers, stock and
  parametric specs (normalized to SI units). Price and stock carry the snapshot date.
- **KiCad's official symbol library**: real pinouts with ERC pin types and default
  footprints. A symbol is attached only when the MPN matches and the symbol's
  footprint agrees with the catalog package (404 of 500 parts have pins; the rest
  have none rather than a guessed pinout).

Parts are chosen per category for this project's subsystems (power, MCU/RF,
sensing/analog, protection, passives, connectors), ranked by KiCad pinout
availability, JLC Basic/Preferred class and stock. Every repo MPN that LCSC
stocks is included (21; the other 17 are listed under `coverage`).

[`data/parts_graph.json`](./data/parts_graph.json) is the knowledge graph built
from it: parts linked to manufacturers, categories, packages, KiCad symbols and
footprints, pinout classes, interfaces (I2C, SPI, BLE, ...), the distributor
offer and one datasheet source each, with evidence source IDs on every edge
(same conventions and IDs as `relationships.json`). `alternate_candidate` edges
join parts with the same category, pinout class, footprint and key value; they
are leads for stock-out re-planning, not verified substitutions.
`mock_pin_conflicts` lists mock-registry pins that disagree with the KiCad
symbol for the same MPN.

```
python -m cvhack.scrape.build          # re-scrape (cached in .cache/scrape/), ~20 s after download
python -m cvhack.parts_graph --write   # rebuild the graph; --check validates it is current
```

`load_parts()` includes the catalog: it adds parts nobody else defines and fills
fields that are still unknown (B's null prices, missing pins), but keeps the
mock's demo values. Set `PARTS_PRICES=catalog` to make the dated catalog price
and stock win everywhere. Sub-agents can query the graph with the
`search_parts` and `find_alternates` tools. Catalog parts carry no `power_mw`
(the distributor data has no typical power), so the power budget counts them as 0.

#### Medical evidence fields

Every catalog part has a `medical` block (`python -m cvhack.scrape.medical`;
`cvhack.scrape.build` runs it too). It records evidence, not a compliance finding:

| Field | Source | Coverage |
|---|---|---|
| `lifecycle` | TI product pages (manufacturer status); LCSC `productCycle` (distributor) | 58 manufacturer statuses (all TI); 3 parts LCSC has discontinued; the rest `unknown` (ST, ADI, Microchip and onsemi block automated lookups) |
| `traceability` | LCSC product record and the datasheet itself | RoHS certificate for 360 parts, ECCN, HTS, packaging, datasheet SHA-256; lot/date code is `null` because it only exists at goods receipt |
| `statements` | Text scan of each datasheet (457 of 500 readable), with the quote | IEC 60601-1 (8 parts), 60601-1-2 EMC, MOPP/MOOP, ISO 10993 (none), life-support/medical-use restrictions (62) |
| `isolation` | Insulation tables of the 23 isolation-barrier parts, transcribed into [`data/isolation_ratings.json`](./data/isolation_ratings.json) with verbatim quotes (tests check each quote is in the datasheet) | 20 transcribed: withstand/working voltage, creepage, clearance, DTI, CTI, barrier capacitance, certifications, MOPP where stated for that package; other parts are `not_applicable` with the reason |
| `leakage_current` | Barrier capacitance x 250 Vrms at 60 Hz | Isolation parts only; a screening estimate, not a measurement |
| `biocompatibility` | Datasheet claims only; ISO 10993 is assessed on the finished device's patient-contacting materials | No part claims it; categories often in a skin-facing path are flagged |

[`data/medical_requirements.json`](./data/medical_requirements.json) holds IEC
60601-1 reference values (MOOP/MOPP test voltage, creepage and clearance at
mains working voltage; patient, touch and earth leakage limits), transcribed
from manufacturer application notes rather than the standard itself; confirm
them against the standard before a design decision. In the graph,
`rating_reaches` edges compare an isolation part's own ratings with those
levels (screening only: the board layout, working voltage and the part's
certificate decide the real means of protection), and `certified_to`,
`datasheet_states` and `has_lifecycle_status` edges carry the evidence.
`search_parts` accepts `mopp_at_least` and `lifecycle`, and the
`get_part_medical` tool returns a part's full medical profile.

Things the transcription turned up: the ADuM4160's IEC 60601-1 250 Vrms
rating is for the RI-16 package (the stocked RW-16 part is 125 Vrms, 7.7 mm
creepage); LCSC lists isolated DC-DC modules' VDC ratings as Vrms and the
AMC1200B's 4250 V peak as 4.25 kV rms; and the H0505S module's "60601"
references are to the EMC standard 60601-1-2, not safety isolation.

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

Python 3.11+ with `pip install -r requirements.txt` (the scripts use `./.venv` or `../.venv`), Crusoe Managed
Inference credentials in `.env` (copy `.env.example`), and optionally KiCad (`brew install --cask kicad`) for
KiCad's own ERC and PDF export, plus `demo/setup_router.sh` for the routed 3D board.

## Running the demo

```
demo/preflight.sh                 # checklist: tests, build, KiCad, fake run, credentials
demo/run_demo.sh                  # dashboard + scripted fake run (no LLM needed)
demo/run_demo.sh --real           # dashboard + the real agents on Crusoe, bridged from runs/run.db
demo/run_demo.sh --replay         # dashboard + recorded run (stage fallback)
```

The dashboard opens at http://localhost:8000. Use **Mark out of stock** or **Raise to Class III** mid-run to trigger a re-plan, and open `/#negotiation` to show the mother/subsystem negotiation.

## Desktop app

Oak runs as a local desktop app (a native window around the local server; your data stays on your machine).

```
python app.py                 # opens the window
open "Run Oak.command" # double-click version (runs in Terminal, which already has access to this folder)
bash bin/make_app.sh          # builds "Oak.app"
```

macOS note: this folder is under Desktop, which macOS protects. A Finder-launched `Oak.app` needs Full Disk Access
(System Settings > Privacy & Security > Full Disk Access > add "Oak.app"), or keep the project outside Desktop.
Routing needs Java 25 and Freerouting: `bash demo/setup_router.sh` (downloads into `runs/_tools`, no system install).
