# KiCad schematic checker — v0.1

A local, AI-platform-independent foundation for reviewing KiCad schematics against explicit engineering requirements. Use it with Claude, Codex, another assistant, or directly from a terminal. The same JSON rules and evidence produce the same deterministic results regardless of model.

Requires Python 3.10+ and KiCad CLI for project extraction. No Python packages or model API keys are needed. Tested here with Python 3.14 and KiCad 10.0.6.

## What works

- Extract a saved root schematic/project using KiCad's own connectivity engine.
- Preserve component fields, part numbers, sheet paths, identifiers, pin nets, and DNP flags.
- Preserve full placed-symbol pin inventories from KiCad 10, including unconnected pins and multiple units.
- Save full ERC findings separately, including disabled checks.
- Audit component identification by default: manufacturer, MPN, supplier codes, conflicts, DNP parts, and documented exemptions.
- Optionally cross-check every MPN against cached DigiKey catalog data: existence, manufacturer, lifecycle, category, value, ratings and package.
- Audit component descriptions by default: every part needs a Description; for R/C/L it must state value and package and is cross-checked against Value, footprint and declared ratings. Owner to-dos are collected in `owner-actions.md`.
- Track every supplied requirement as pass, fail, needs_review, not_checked, or not_applicable.
- Check direct pin connectivity and component properties against reviewed evidence.
- Compare symbol pin counts (and optional pin-number sets) with reviewed package-specific datasheet requirements.
- Refuse silent nominal-capacitance substitution, wrong-part evidence, unreviewed facts, missing operating conditions, and stale live-project inputs.
- Generate readable Markdown and machine-readable JSON reports.

For an AI assistant, start with [AGENT_GUIDE.md](AGENT_GUIDE.md).

To describe the intended function and future review scope of each schematic sheet, use [SCHEMATIC_SHEET_INPUT.md](SCHEMATIC_SHEET_INPUT.md). It is a short Markdown form for design context; the checker still derives connectivity from KiCad.

For the symbol pin-count check, its source requirements, and a runnable example, see [SYMBOL_CHECKS.md](SYMBOL_CHECKS.md).

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

Evidence source preference: **DigiKey for RLC passive component catalog specifications; manufacturer datasheets and official product pages for active components.** Use manufacturer documents/curves for passive properties that need detail absent from DigiKey's listing. LCSC/JLCPCB codes remain procurement identifiers; their listings are not accepted as engineering evidence. Missing or conflicting preferred-source evidence stays unresolved. This policy guides the AI reviewer; automatic web lookup and publisher enforcement are not implemented. See [AGENT_GUIDE.md](AGENT_GUIDE.md#component-source-policy).

## Extract a real project

Run from this folder:

```sh
python3 -m kicad_checker extract "/path/to/project/board.kicad_pro" --output runs/board-001
```

The root schematic must share the project filename. Alternatively pass its `.kicad_sch` directly. Output must be a new directory outside the source project. The tool writes only review artifacts and does not intentionally alter the design. Unsaved edits are not included. Keep hierarchy dependencies available; this version supports the default assembly, not named variants.

Output includes `circuit.json`, the original XML netlist, full `erc.json`, `erc-summary.json`, `identification.json`/`identification.md`, `description.json`/`description.md` and `owner-actions.md`. Netlist imports also produce these reports. ERC findings describe the local library configuration and configured checks, not necessarily board defects.

## Default check: component identification completeness

Every exported, populated, purchasable component needs a declared **manufacturer** and **exact manufacturer part number (MPN)**. Extraction and import always produce an identification report; every engineering review includes a fresh identification audit without requiring additional rules or evidence. Missing identification stays `needs_review` (part selection incomplete), while other checks continue. The report starts with a grouped "What to fix" summary (for example, one line per MPN that lacks a Manufacturer field); grouping is only for readability, and every component keeps its own `needs_review` result. A metadata pass establishes that the fields are present and consistent; it does not verify a catalog match, ordering-code suffix, package, or electrical suitability.

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

## Default check: component description

Every exported, populated, purchasable component needs a filled **`Description`** field (alias `Desc`). Blank text and placeholders (`TBD`, `n/a`, …) count as empty. Exemptions are the same as for identification: DNP parts, KiCad power symbols, and documented `Checker_NonPurchasable` objects.

For resistors, capacitors and inductors (references `R<n>`, `C<n>`, `L<n>`), the Description must also state the **value** and, when the footprint name carries a size code, the **package**. It is then cross-checked against the declared fields:

| Check | Compared with | Mismatch |
| --- | --- | --- |
| Value | `Value` field, parsed in either notation (`10k` = `10 kOhms`, `2u2` = `2.2 µF`; ferrite beads by impedance) | `fail` |
| Package | Footprint size code (`C_0805_2012Metric` → 0805 / 2012 metric) | `fail` |
| Component type | Type words in the Description (capacitor/resistor/inductor) and the KiCad standard footprint library | `fail` |
| Ratings (optional) | `Voltage`, `Tolerance`, `Power`, `Dielectric` fields when the owner declared them and the Description states them | `fail` |

Distributor text works as-is, e.g. `2.2 µF ±10% 25V Ceramic Capacitor X7R 0805 (2012 Metric)` or `CAP CER 2.2UF 25V X7R 0805`. Generic library text such as `Unpolarized capacitor` has no value, so it stays `needs_review`. Anything the parser cannot read stays `needs_review`; it never becomes a `fail`. For example, all-caps `MOHM` could mean milliohm or megaohm. Other component classes only need a filled Description.

Extraction, import and review write `description.json`/`description.md` and **`owner-actions.md`**. That file is one list per component of the fields the owner must fill or correct (identification and description). The checker never edits the schematic: the owner fills the fields in KiCad, saves, and re-runs extraction, and the cross-check then runs on the new text.

## Credentials

External sources need **your own** keys. They live only on your machine:

1. `cp .env.example .env` in the checker folder.
2. Fill in your keys. `.env` is git-ignored; `.env.example` is the shared, empty template.
3. Alternatively use `~/.config/kicad_checker/credentials.env` for every checkout on the machine, or environment variables (these win; use them in CI as masked variables).

Each engineer creates their own DigiKey app instead of sharing one: keys are personal, and the daily API quota is per app. The tool never prints or stores credentials; error messages name the files it looked in, not values. If a key was ever committed or pasted somewhere public, revoke it in the DigiKey developer portal and create a new one.

## Optional check: DigiKey cross-check

Looks up every declared MPN on DigiKey (Product Information API v4, exact-MPN keyword search) and compares the catalog data with the saved schematic. Fetching and checking are separate steps, so reviews run offline from a cache and give the same result every time.

```sh
# once: your own free app at developer.digikey.com with "Product Information V4" enabled,
# then: cp .env.example .env  and fill in DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET (see "Credentials")
python3 -m kicad_checker digikey-fetch runs/board-001/circuit.json          # network; one call per unique MPN
python3 -m kicad_checker digikey-check runs/board-001/circuit.json --output runs/board-001-digikey   # offline
# or include it in a review:
python3 -m kicad_checker review ... --digikey-cache cache/digikey
```

The cache defaults to `cache/digikey` inside the checker folder (git-ignored), one JSON file per MPN, shared by every project you check. `--cache DIR` changes it, and `--refresh` queries the API again. Cached entries keep exact matches only, with photos and account identifiers removed, plus the fetch date. Credentials are never written by the tool.

| Check | Compared | Result |
| --- | --- | --- |
| Found | exact MPN on DigiKey | not found → `needs_review` (DigiKey doesn't list every part) |
| Manufacturer | declared vs DigiKey, with legal suffixes and common aliases ignored (TI, ADI/Linear, ST…) | differs → `needs_review`; empty → owner suggestion with DigiKey's name |
| Match | several listings of one MPN (e.g. Murata Electronics / Murata Power Solutions) | identical data → checked once; different data → `needs_review` until the Manufacturer field picks one |
| Lifecycle | `ProductStatus` | anything but Active, or discontinued/end-of-life flags → `needs_review` |
| Category | R/C/L reference vs DigiKey category | contradiction → `fail` |
| Value | R/C/L Value vs DigiKey capacitance/resistance/inductance | contradiction → `fail` |
| Ratings | voltage, tolerance, power, dielectric vs Description text and rating fields | contradiction → `fail` |
| Package | footprint size code vs DigiKey `Package / Case` | contradiction → `fail`; IC packages are reported, not judged |
| Pin-count hint | leading count in DigiKey package name (e.g. `6-WDFN Exposed Pad`) vs placed symbol pins | report only; package names can be wrong (e.g. `TO-39-3`), so use a datasheet `symbol_pin_count` rule for a verdict |

DigiKey data is distributor catalog data (`observation_class: distributor_catalog`), not manufacturer-authored evidence; a pass does not establish suitability. Owner suggestions and conflicts are added to `owner-actions.md`. `digikey-check` exit codes: 0 all pass, 1 a conflict, 2 something to review. `digikey-fetch` returns 3 if any lookup failed.

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

Each property fact needs `basis`, `status: verified`, `reviewed_by`, `source.locator`, `conditions`, and `conditions_match: true`. Manufacturer facts also need the exact schematic `part_number`. Numeric facts use a `min`/`max` interval. Recognized units of the same quantity are converted exactly (F, Ohm/Ω, H, V, A, W, Hz with p/n/u/µ/m/k/M/G prefixes, plus %, ppm, ppm/°C and °C): `10 uF` equals `10000 nF`. A verified value outside the rule bounds after conversion still fails. Unrecognized units, or units of a different quantity, stay `needs_review`; identical unit strings still compare directly. Review attribution is auditable text, not a cryptographic attestation; the engine cannot prove supplied evidence is true. It checks the declared evidence consistently.

Each component-property rule also declares `evidence_basis`: `schematic` for a check of a saved schematic value, or `manufacturer` for a property that must come from the exact component specification. Each fact declares the matching `basis`. A schematic-based fact does not require an MPN; a manufacturer-based fact does. For a schematic basis the **saved schematic is the source of truth**: the rule (`schematic_field`) or the fact (`field`) must name the KiCad field to read, usually `Value`. The engine parses that field (`2.2uF`, `2u2`, `100n`, `4k7`, `10k 1%`, `2.2uF 25V X7R`) and judges it. A supplied schematic fact must agree with the parsed field, otherwise the check is `needs_review`; an empty, unparseable or ambiguous field is also `needs_review`. With `schematic_field` on the rule, no hand-typed schematic fact is needed. Missing facts remain `needs_review` with the required basis named. For example, a 10-kOhm pull-up value may pass a schematic-value rule without an individual resistor datasheet. A current-sense tolerance rule cannot pass from its schematic value; it needs the selected resistor's specification. The reviewer must decide which properties matter to the circuit and record separate rules for power, temperature, or fault conditions when applicable.

Add `source.path` and `source.sha256` whenever a rule or fact cites a local document. Prefer a **relative** `source.path` (for example `docs/lp5912-ep.pdf`): it resolves against the folder of the rules or evidence file that contains it, or against `review --docs-root DIR` when given. Absolute paths still work but tie a case to one machine. If the resolved file changes or disappears, the affected check becomes `needs_review`, and the reason names the path that was checked. Rules without a local source path retain their text citation but cannot be checked for document freshness. See [KICAD_HAPPY_NOTES.md](KICAD_HAPPY_NOTES.md) for the upstream idea that prompted this safeguard and other possible additions.

Supported check kinds:

| Kind | Meaning |
| --- | --- |
| `connected` | Two explicit component pins occupy the same net |
| `symbol_pin_count` | Full placed-symbol terminal count, and optional pin-number set, match the reviewed datasheet for the selected manufacturer/MPN/package |
| `component_property` / `range` | Entire evidence interval (after exact unit conversion), or the parsed schematic field for a schematic basis, lies within inclusive rule bounds |
| `component_property` / `one_of` | Value is one of the allowed values, ignoring case and spacing. Dielectric properties (`property` ending in `dielectric`, or `value_type: dielectric`) compare EIA codes: `x7r` = `X7R`, `NP0`/`NPO`/`COG` = `C0G`. A recognized code that is not allowed fails; an unrecognized one stays `needs_review` |
| `heuristic` | Records a candidate and method; always `needs_review` |
| `manual` | Always remains needs_review in v0.1 |

Each result has `observation_class`: `deterministic_connectivity`, `schematic_value`, `manufacturer_specification`, `heuristic_candidate`, or `manual_review`. Class labels describe where a conclusion would come from; **status** still determines whether the check passed. A `manufacturer_specification` result with missing evidence is unresolved, not manufacturer verified. Heuristic findings can guide a follow-up check but cannot create an automatic pass or fail.

Do not mark a rule inapplicable simply because evidence is missing. A `not_applicable` target binding requires a reviewer and rationale and applies to every rule using that target. Create distinct targets where applicability differs.

Review exit codes: 0 = identification and descriptions are complete (or explicitly exempt), and every supplied rule passes or is explicitly inapplicable; 1 = an engineering rule failure or a description conflict exists; 2 = identification/description/engineering review is incomplete or no engineering requirements were supplied; 3 = invalid input or tool failure. Successful extraction/import returns 0 even if its identification report is incomplete. Code 0 is not a complete design approval. Draft rules are not_checked and count as incomplete. Reports also count passes by approval: a pass is shown as human-approved only when the rule, its target binding(s) and any fact used all carry `approved_by_human`; otherwise it is "agent review only". This is reporting, not a gate, and statuses and exit codes are unchanged. Only the user writes `approved_by_human`. Engineering coverage counts apply to supplied rules; identification has separate coverage counts. The tool cannot detect engineering requirements omitted from documents.

## Current boundaries

No automatic PDF ingestion, AI API integration, simulation, PCB layout checks, general circuit solving, parallel-capacitor equivalent calculations, or automatic datasheet rule generation. Symbol count checks do not verify pin names/functions, footprint mapping, or infer package selection. AI-assisted evidence collection follows AGENT_GUIDE.md. Logical nets are not merged across zero-ohm resistors. Source freshness covers the active design: the root schematic, every sub-sheet it references (wherever it lives) and the `.kicad_pro`. KiCad's `.history/` copies and unrelated files are ignored. External symbol/footprint libraries are not covered; pin a full project snapshot for reproducible reviews involving external dependencies.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Next useful iteration: review a real document together, turn its requirements into an approved rule pack, and populate real evidence for a selected circuit block.
