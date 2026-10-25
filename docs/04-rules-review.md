# 4. Check datasheet requirements (rules review)

Turns datasheet requirements ("LDO output needs X5R/X7R, 1–10 µF", "EN connects to VIN",
"symbol has all 21 pins") into JSON rules and checks them against the schematic. Offline.
This is the expert workflow: usually an AI assistant drafts the rules and evidence, and you
review them.

[← All guides](../USER_GUIDE.md) · Command: `review`

## The three files

| File | Who writes it | Content |
| --- | --- | --- |
| `circuit.json` | `extract` ([guide 1](01-schematic-check.md)) | What the schematic says |
| **rules** JSON | you / an AI assistant | Requirements, each with its source (datasheet page, table) |
| **evidence** JSON | you / an AI assistant | Which reference each rule applies to (`C5` is "the LDO output cap"), plus exact-part facts with sources |

Rules check **logical targets** (`output_capacitor`); the evidence file binds them to real
references. The same rule pack can then be reused on another board.

## Run it

```sh
python3 -m kicad_checker review runs/board-001/circuit.json \
  --rules rules/my-board.json --evidence rules/my-board-evidence.json \
  --output runs/board-001-review
```

Open `runs/board-001-review/report.md`. Try the synthetic demo first:
[Setup → Try it without a board](00-setup.md#try-it-without-a-board-2-minutes-offline).

| Option | Use |
| --- | --- |
| `--snapshot` | Required when `circuit.json` came from `import-netlist`, or the project is not on this machine. Skips the live "project unchanged" check |
| `--docs-root DIR` | Resolve relative `source.path` values against this folder |
| `--digikey-cache cache/digikey` | Also cross-check against cached DigiKey data |

## Rule kinds

| Kind | Checks | Example |
| --- | --- | --- |
| `connected` | Two pins are on the same net | cap pin 1 ↔ regulator OUT (`pin`, `other_target`, `other_pin`) |
| `component_property` | A property is `range` (`min`/`max`/`unit`) or `one_of` (`allowed`) | dielectric one of X5R, X7R |
| `symbol_pin_count` | Placed symbol has the datasheet's pin count | see [SYMBOL_CHECKS.md](../SYMBOL_CHECKS.md) |
| `manual` | Stays on the list for a human | "good GND return: inspect PCB" |
| `heuristic` | A lead, always `needs_review` | decoupling/pull-up detection |

Each `component_property` rule says where its fact comes from:

- `evidence_basis: "schematic"` + `schematic_field: "Value"` — the declared value. Enough for
  "pull-up is 10 k".
- `evidence_basis: "manufacturer"` — an exact-part fact in the evidence file. Needed for
  dielectric, ESR, effective capacitance, tolerance of a sense resistor…

Units convert exactly (`10 uF` = `10000 nF`); `NP0` = `C0G`.

## Status and approval

- New rules are `"status": "draft"` → result `not_checked`. Change to `approved` once you've
  checked the interpretation against the datasheet.
- A pass counts as human-approved only when **you** add `approved_by_human`. AI assistants
  never write that field; their passes show as "agent review only".
- `source.path` (relative) + `source.sha256` pin the datasheet file. If it changes or
  disappears, the result becomes `needs_review`.

## Describe what each sheet should do

Connectivity can't tell the tool what a block is *for*. Copy the form in
[SCHEMATIC_SHEET_INPUT.md](../SCHEMATIC_SHEET_INPUT.md) to your project's `docs/` and fill in
intent, operating conditions and scope per sheet before asking an assistant to draft rules.

## Limits

- No automatic rule generation from datasheets: rules are written and reviewed.
- Connectivity = same net only. Nothing conducts through resistors, filters or ICs, and nets
  are not merged across 0 Ω resistors.
- The engine checks supplied bounds; it doesn't derive DC-bias or parallel-capacitor effects.
  Keep those as `manual` rules.

Full agent workflow and source policy: [AGENT_GUIDE.md](../AGENT_GUIDE.md).
Reusable pack: [rules/lp5912-output-capacitor.json](../rules/lp5912-output-capacitor.json).

**Next:** [5. Can I build N boards from our stock?](05-inventory-check.md)
