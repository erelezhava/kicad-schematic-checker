# KiCad Schematic Checker

Checks a saved KiCad design for incomplete or contradictory part data and for violations of datasheet requirements, then tells the design owner exactly which fields to fill or fix. The engine is plain Python with deterministic results: the same design, rules and evidence always give the same report, whichever AI assistant you use with it (Claude, ChatGPT/Codex, a local Ollama model) or none at all.

- Needs **Python 3.10+** and **KiCad 10** (`kicad-cli`). No Python packages.
- Optional: **`pdftotext`** (`sudo apt install poppler-utils`) for datasheet text search, and your own **DigiKey API keys** for catalog checks.
- Never edits your design. The one helper that can (`tools/fill_fields.py`) is opt-in and previews first.

## What it checks

| Check | Runs | What it looks at |
| --- | --- | --- |
| **Identification** | every extract | Manufacturer + exact MPN present and consistent; the Value names the same part as the MPN |
| **Description** | every extract | Description filled; for R/C/L the value, package and ratings agree with Value, footprint and rating fields |
| **DigiKey** | `digikey-fetch` + `digikey-check` | MPN exists, manufacturer, lifecycle (Active/Obsolete…), R/C/L value, voltage, tolerance, power, dielectric, package size |
| **Datasheets** | `datasheet-fetch` | finds each part's PDF: your `Docs/` folder first, then the cache, then download |
| **Rules** | `review` | your datasheet requirements: pin connections, component properties, symbol pin count |

Results are `pass`, `fail` (two declared facts contradict each other), `needs_review` (something is missing or can't be read), `not_checked` or `not_applicable`. Anything the tool can't read is never turned into a pass or a fail.

## Quick start

```sh
git clone <this repo> && cd kicad-schematic-checker
python3 -m unittest discover -s tests            # all OK

# 1. read the design (saved project only)
python3 -m kicad_checker extract ~/projects/board/board.kicad_pro --output runs/board-001
# 2. open runs/board-001/owner-actions.md: the list of fields to fill or fix in KiCad
```

With DigiKey keys (see [Credentials](#credentials-and-caches)):

```sh
python3 -m kicad_checker digikey-fetch runs/board-001/circuit.json                         # online, cached
python3 -m kicad_checker digikey-check runs/board-001/circuit.json --output runs/board-001-dk
python3 tools/fill_fields.py runs/board-001-dk/digikey.json ~/projects/board               # preview
python3 tools/fill_fields.py runs/board-001-dk/digikey.json ~/projects/board --apply       # fill empty Manufacturer fields
python3 -m kicad_checker datasheet-fetch runs/board-001/circuit.json --output runs/board-001-ds
```

After changing the design in KiCad, save and extract again into a new run folder.

## Commands

| Command | Network | Purpose |
| --- | --- | --- |
| `extract PROJECT --output DIR` | no | Netlist, pins, fields and ERC via `kicad-cli`; identification and description audits |
| `import-netlist NETLIST.xml --output DIR` | no | Same audits from an exported netlist (no ERC, no freshness check) |
| `digikey-fetch CIRCUIT [--refresh]` | yes | Look up every unique MPN once and cache it |
| `digikey-check CIRCUIT --output DIR` | no | Compare cached DigiKey data with the schematic |
| `datasheet-fetch CIRCUIT --output DIR [--docs DIR] [--offline] [--all]` | optional | Locate a datasheet PDF per active part (`--all` adds R/C/L) |
| `review CIRCUIT --rules R --evidence E --output DIR` | no | Evaluate rules; options `--snapshot`, `--docs-root`, `--digikey-cache cache/digikey` |
| `tools/fill_fields.py DIGIKEY_JSON PROJECT [--prefer NAME] [--apply]` | no | Fill **empty** Manufacturer fields from DigiKey suggestions |

## Outputs

Each run writes into its own new folder under `runs/` (git-ignored):

| File | Content |
| --- | --- |
| `owner-actions.md` | **Start here.** Per component: what the owner must fill or correct |
| `identification.md` | Manufacturer/MPN audit with a grouped "What to fix" summary |
| `description.md` | Description audit and cross-checks |
| `digikey.md` | DigiKey comparison per component |
| `datasheets.md` | Which PDF was used for each MPN, and where it came from |
| `report.md` | Rule results from `review` |
| `circuit.json`, `netlist.xml`, `erc.json` | Raw extraction data |

## Exit codes

`0` everything complete and passing · `1` a `fail` (a contradiction) · `2` something to review or fill · `3` invalid input or tool error. `extract` returns 0 when extraction itself worked. Code 0 means "no open items in what was checked", not a full design approval.

## Credentials and caches

- **Keys:** `cp .env.example .env` and fill in **your own** DigiKey app keys (developer.digikey.com → Production App → *Product Information V4*). `.env` is git-ignored; `.env.example` stays empty. Environment variables override it (use them in CI). Don't share keys: they are personal and the daily quota is per app.
- **Caches:** everything downloaded stays in `cache/` in this folder (git-ignored): `cache/digikey/` (one JSON per MPN) and `cache/datasheets/` (PDFs stored by hash, plus extracted text). Each MPN is fetched once and reused by every project.

## Folder layout

```text
kicad_checker/   the engine (CLI, parsers, audits, DigiKey, datasheets)
tools/           opt-in helpers: fill_fields.py, digikey_capture.py
tests/           unit tests (offline; real DigiKey responses as fixtures)
examples/        synthetic demo netlists, rules and evidence
rules/           reusable rule packs (drafts until reviewed)
runs/, cache/    your outputs and downloads (git-ignored)
```

## How the checks work

### Identification

Every populated, purchasable part needs a **Manufacturer** and an exact **MPN**. Accepted field names include `Manufacturer`, `Manufacturer_Name`, `Mfr`, `MFG` and `MPN`, `Manufacturer_Part_Number`, `Mfr. Part #`, `PRT.NUM` (case, spaces and punctuation ignored). Empty values and placeholders (`TBD`, `?`, `N/A`) don't count, aliases that disagree are conflicts, and an LCSC/JLCPCB code alone is not an MPN.

For non-R/C/L parts the **Value** is compared with the MPN: equal, or a family prefix (`LP5912` for `LP591233MDRVREP`), passes. A different variant (`…-SP` vs `…-TU`, often only packaging), an unrelated part number, or an MPN shorter than the Value (probably only a family name) is `needs_review`. Generic library values (`Conn_01x06`, `MountingHole_Pad`) are not compared.

Exempt: DNP parts, KiCad power symbols, and parts with `Checker_NonPurchasable = true` **plus** a `Checker_ExemptionReason`. Mounting holes, connectors and test points are not exempt automatically.

### Description

Every part needs a `Description`. For R/C/L (references `R1`, `C2`, `L3`…) it must state the **value** and, when the footprint has a size code, the **package**. It is then compared with the Value, the footprint size (`C_0805_2012Metric` → 0805), the part type, and any `Voltage`/`Tolerance`/`Power`/`Dielectric` fields. Distributor text works as-is: `2.2 µF ±10% 25V Ceramic Capacitor X7R 0805 (2012 Metric)` or `CAP CER 2.2UF 25V X7R 0805`. Generic text like `Unpolarized capacitor` stays `needs_review`.

### DigiKey

Exact-MPN keyword search (API v4). A contradiction in value, category, ratings or package size is a `fail`. Not found, ambiguous listings, a different manufacturer name, or a non-Active lifecycle are `needs_review`. An empty Manufacturer gets a suggestion that `fill_fields.py` can apply. IC packages and pin counts from package names are reported only, because names like `TO-39-3` can be wrong. DigiKey data is distributor catalog data, not manufacturer evidence.

### Datasheets

Search order per MPN: PDFs in the project's `Docs/`, `docs/`, `datasheets/`… (subfolders included, hidden folders like `.history` skipped). A file matches when its name contains the MPN, its name equals DigiKey's datasheet file name, or its text lists the MPN. Then the cache, then a download from DigiKey's link (https first, 60 s limit, real PDFs only). Two matching local files → `needs_review`. A failed download tells you the URL to fetch by hand.

### fill_fields.py

Fills only **empty** Manufacturer fields, reusing an existing empty field such as `Manufacturer_Name`. It edits only the active design (root sheet and its sub-sheets, never `.history`), refuses while KiCad has the file open, and previews unless `--apply` is given. Check the result with `git diff` in your design repo.

### Rules and evidence (`review`)

Requirements from datasheets are written as JSON rules (`connected`, `component_property`, `symbol_pin_count`, `manual`, `heuristic`) with sources (relative `source.path` + `sha256`; a changed file makes the check `needs_review`). Facts come from the saved schematic (`schematic_field`, e.g. `Value`) or from the exact part's specification. Units are converted exactly (`10 uF` = `10000 nF`), and dielectric codes are normalized (`NP0` = `C0G`). Rules stay `draft` until reviewed. Reports count passes as human-approved only when a person added `approved_by_human`; agents never write that field. Details: [SYMBOL_CHECKS.md](SYMBOL_CHECKS.md) for pin counts, [SCHEMATIC_SHEET_INPUT.md](SCHEMATIC_SHEET_INPUT.md) for describing each sheet's intent, `examples/` for runnable inputs.

## Limits

No simulation, PCB layout, footprint pad-mapping or pin-name checks, and no automatic rule generation from datasheets (yet). Nets are not merged across zero-ohm resistors. Named assembly variants are not supported (default assembly only). The freshness check covers the root schematic, its sub-sheets and the `.kicad_pro`, not external libraries.

## For AI assistants

Start with [AGENT_GUIDE.md](AGENT_GUIDE.md): the review workflow, the component source policy (DigiKey for passives, manufacturer documents for active parts), and what an agent must never do (edit the design, approve its own rules, invent evidence).
