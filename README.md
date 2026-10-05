# KiCad schematic checker — v0.1

A local, AI-platform-independent foundation for reviewing KiCad schematics against explicit engineering requirements. Use it with Claude, Codex, another assistant, or directly from a terminal. The same JSON rules and evidence produce the same deterministic results regardless of model.

Requires Python 3.10+ and KiCad CLI for project extraction. No Python packages or model API keys are needed. Tested here with Python 3.14 and KiCad 10.0.6.

## What works

- Extract a saved root schematic/project using KiCad's own connectivity engine.
- Preserve component fields, part numbers, sheet paths, identifiers, pin nets, and DNP flags.
- Save full ERC findings separately, including disabled checks.
- Audit component identification by default: manufacturer, MPN, supplier codes, conflicts, DNP parts, and documented exemptions.
- Track every supplied requirement as pass, fail, needs_review, not_checked, or not_applicable.
- Check direct pin connectivity and component properties against reviewed evidence.
- Refuse silent nominal-capacitance substitution, wrong-part evidence, unreviewed facts, missing operating conditions, and stale live-project inputs.
- Generate readable Markdown and machine-readable JSON reports.

For an AI assistant, start with [AGENT_GUIDE.md](AGENT_GUIDE.md).

To describe the intended function and future review scope of each schematic sheet, use [SCHEMATIC_SHEET_INPUT.md](SCHEMATIC_SHEET_INPUT.md). It is a short Markdown form for design context; the checker still derives connectivity from KiCad.

## Project documents

Keep a `docs/` folder alongside the KiCad project being reviewed (`Docs/` is also supported by the agent instructions):

```text
my-board/
  board.kicad_pro
  board.kicad_sch
  docs/
    lp5912-ep.pdf
    lp5912-ep.md
    reference-circuit.pdf
```

Original PDFs remain the authoritative documents; Markdown copies are search/read aids. Use matching filename stems for each pair. The assistant should discover these files, record their hashes/revisions with the review, and verify critical tables, graphs, and circuit drawings against PDF pages. Reference schematics do not need Markdown to be useful. You may provide a different document folder explicitly.

This discovery and document verification is part of the agent workflow in AGENT_GUIDE.md; the v0.1 command-line engine does not itself ingest the documents. Rules and evidence must be supplied to `review` after reading them.

## Extract a real project

Run from this folder:

```sh
python3 -m kicad_checker extract "/path/to/project/board.kicad_pro" --output runs/board-001
```

The root schematic must share the project filename. Alternatively pass its `.kicad_sch` directly. Output must be a new directory outside the source project. The tool writes only review artifacts and does not intentionally alter the design. Unsaved edits are not included. Keep hierarchy dependencies available; this version supports the default assembly, not named variants.

Output includes `circuit.json`, the original XML netlist, full `erc.json`, `erc-summary.json`, and `identification.json`/`identification.md`. Netlist imports also produce identification reports. ERC findings describe the local library configuration and configured checks, not necessarily board defects.

## Default check: component identification completeness

Every exported, populated, purchasable component needs a declared **manufacturer** and **exact manufacturer part number (MPN)**. Extraction and import always produce an identification report; every engineering review includes a fresh identification audit without requiring additional rules or evidence. Missing identification stays `needs_review` (part selection incomplete), while other checks continue. A metadata pass establishes that the fields are present and consistent; it does not verify a catalog match, ordering-code suffix, package, or electrical suitability.

Recommended KiCad fields:

| Field | Meaning |
| --- | --- |
| `Manufacturer` | Required manufacturer name |
| `MPN` | Required manufacturer's ordering code |
| `LCSC` | Optional supplier catalog code |
| `JLCPCB` | Optional supplier catalog code |

The reader also accepts `Manufacturer Name`, `Mfr`, `Mfr Name`, and `MFG`; MPN aliases include `Manufacturer_Part_Number`, `Manufacturer Part Number`, `Manufacturer Part #`, `Mfr. Part #`, `Mfr Part Number`, `MFR.PN`, and legacy `PRT.NUM`. Supplier aliases include `LCSC Part #`, `LCSC Part Number`, `JLCPCB Part #`, and `JLCPCB Part Number`. Matching ignores case, spaces, and punctuation. Multiple non-empty aliases that disagree are reported as conflicts. Blank strings and placeholders such as `TBD`, `unknown`, `?`, and `N/A` do not satisfy the requirement. The schematic `Value` is not used as an MPN.

An LCSC/JLCPCB code alone does not satisfy the manufacturer or MPN requirement. An MPN resembling an LCSC `C`-plus-digits code is a lead for manual confirmation and stays unresolved; this pattern is not proof of an incorrect part number. This check performs no web lookup or schematic edits. Requiring an MPN does not require a separate datasheet review for every ordinary resistor.

DNP components remain visible as `not_applicable` for the default assembly, with their missing/conflicting fields retained. Exported KiCad `power` symbols with `#PWR`/`#FLG` references are exempt. Other non-purchasable objects, such as bare PCB test pads, may declare `Checker_NonPurchasable: true` plus a meaningful `Checker_ExemptionReason`; an exemption without a reason stays unresolved. Connectors and physical test points are not automatically exempt. Excluding a component from a BOM is not an identification exemption. Symbols omitted from KiCad's exported netlist are outside this audit.

## Run the synthetic example

```sh
python3 -m kicad_checker import-netlist examples/demo/netlist.xml --output runs/demo-circuit
python3 -m kicad_checker review runs/demo-circuit/circuit.json --snapshot --rules examples/demo/rules.json --evidence examples/demo/evidence.json --output runs/demo-report
```

The synthetic example intentionally contains an ESR violation, missing effective-capacitance evidence, and a manual PCB check. It should return exit code 1, not an all-clear. The evidence is fictitious and must never be used for real engineering.

## Start your own review

1. Copy the draft rule pack in `rules/` and `examples/evidence-template.json` into a new case folder.
2. Verify the source passage and applicability. Change a rule to `approved` only after review. Bind each target to an actual reference and document why it applies.
3. Add reviewed facts from actual component documents. Leave unknown properties absent: the report will say exactly what is missing.
4. Run:

```sh
python3 -m kicad_checker review runs/board-001/circuit.json --rules cases/board/rules.json --evidence cases/board/evidence.json --output runs/board-review-001
```

Each property fact needs `basis`, `status: verified`, `reviewed_by`, `source.locator`, `conditions`, and `conditions_match: true`. Manufacturer facts also need the exact schematic `part_number`. Numeric facts use a `min`/`max` interval and matching units. Review attribution is auditable text, not a cryptographic attestation; the engine cannot prove supplied evidence is true. It checks the declared evidence consistently.

Each component-property rule also declares `evidence_basis`: `schematic` for a check of a saved schematic value, or `manufacturer` for a property that must come from the exact component specification. Each fact declares the matching `basis`. A schematic-based fact does not require an MPN; a manufacturer-based fact does. Missing facts remain `needs_review` with the required basis named. For example, a 10-kOhm pull-up value may pass a schematic-value rule without an individual resistor datasheet. A current-sense tolerance rule cannot pass from its schematic value; it needs the selected resistor's specification. The reviewer must decide which properties matter to the circuit and record separate rules for power, temperature, or fault conditions when applicable.

Add `source.path` and `source.sha256` whenever a rule or fact cites a local document. If that file changes or disappears, the affected check becomes `needs_review`. Rules without a local source path retain their text citation but cannot be checked for document freshness. See [KICAD_HAPPY_NOTES.md](KICAD_HAPPY_NOTES.md) for the upstream idea that prompted this safeguard and other possible additions.

Supported check kinds:

| Kind | Meaning |
| --- | --- |
| `connected` | Two explicit component pins occupy the same net |
| `component_property` / `range` | Entire supplied evidence interval lies within inclusive rule bounds |
| `component_property` / `one_of` | Supplied value is one of the allowed values |
| `heuristic` | Records a candidate and method; always `needs_review` |
| `manual` | Always remains needs_review in v0.1 |

Each result has `observation_class`: `deterministic_connectivity`, `schematic_value`, `manufacturer_specification`, `heuristic_candidate`, or `manual_review`. Class labels describe where a conclusion would come from; **status** still determines whether the check passed. A `manufacturer_specification` result with missing evidence is unresolved, not manufacturer verified. Heuristic findings can guide a follow-up check but cannot create an automatic pass or fail.

Do not mark a rule inapplicable simply because evidence is missing. A `not_applicable` target binding requires a reviewer and rationale and applies to every rule using that target. Create distinct targets where applicability differs.

Review exit codes: 0 = identification is complete or explicitly exempt, and every supplied rule passes or is explicitly inapplicable; 1 = an engineering rule failure exists; 2 = identification/engineering review is incomplete or no engineering requirements were supplied; 3 = invalid input or tool failure. Successful extraction/import returns 0 even if its identification report is incomplete. Code 0 is not a complete design approval. Draft rules are not_checked and count as incomplete. Engineering coverage counts apply to supplied rules; identification has separate coverage counts. The tool cannot detect engineering requirements omitted from documents.

## Current boundaries

No automatic PDF ingestion, AI API integration, simulation, PCB layout checks, general circuit solving, parallel-capacitor equivalent calculations, or automatic datasheet rule generation. AI-assisted evidence collection follows AGENT_GUIDE.md. Logical nets are not merged across zero-ohm resistors. Source freshness covers project-folder schematics/project files, not external libraries or schematic files outside that folder; pin a full project snapshot for reproducible reviews involving external dependencies.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Next useful iteration: review a real document together, turn its requirements into an approved rule pack, and populate real evidence for a selected circuit block.
