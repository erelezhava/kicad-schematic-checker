# 10. Results, exit codes, troubleshooting

[← All guides](../USER_GUIDE.md)

## Result values

| Where | Values | Meaning |
| --- | --- | --- |
| extract, digikey-check, review | `pass` · `fail` · `needs_review` · `not_checked` · `not_applicable` | `fail` = two declared facts contradict. `needs_review` = missing or unreadable. `not_checked` = rule still `draft` |
| inventory-check | in stock · split across sources · insufficient · not in stock · quantity unknown · no MPN/C-code | [guide 5](05-inventory-check.md#reading-the-bom-table) |
| Market check | `available` · `stock_issues` · `unknown` | Eligible somewhere · seen but stock/MOQ/lifecycle failed · no data |
| Alternatives | `match` · `possible` · `rejected` · manufacturer differs | [guide 6](06-second-sources.md#reading-the-result). Always a lead, never an approval |
| parts-list rows | available · stock issues · not active · not found · manufacturer differs · ambiguous · no MPN · not checked · **quantity unreadable** · not fitted · not a part (PCB) · ✔ vendor alternate | [guide 12](12-parts-list.md#read-the-result). *Quantity unreadable*: the quantity cell can't be read, so availability isn't judged |
| parts-list `--against` | same part · vendor alternate · **same MPN, manufacturer not confirmed** · **same MPN, other manufacturer** · substituted (match / possible / rejected) · value differs · **positions missing** · not in your schematic | [guide 12](12-parts-list.md#after-copying-check-your-schematic-against-the-reference-bom). One result per position; the worst decides the row |
| parts-search | `target_met` · `shortfall` | ≥ 3 distinct parts from ≥ 2 manufacturers, or not |
| jlcpcb_rules | error · warning · info | Beyond JLC limit · paid option or recommendation · FYI |
| refplane | error · warning · info | [guide 11](11-reference-plane.md#reading-the-result) |

Missing data never becomes a pass or a fail.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Nothing open in what was checked (`extract`: extraction worked) |
| `1` | A `fail`: a contradiction |
| `2` | Something to review, fill or buy |
| `3` | Invalid input or tool error |

`0` never means "design approved". In CI, treat `1` and `3` as failures and `2` as a warning.
`python3 -m refplane check` uses only `0` (no errors; warnings allowed) and `1` (at least one
error finding, or bad input such as a missing board file).

`parts-list` returns `0` only when every row is available, not fitted or not a part **and**, with
`--against`, every row is *same part* or *vendor alternate*. Any substitution (even a catalog
`match`), missing position or unconfirmed manufacturer gives `2`, so a CI job flags a reference
design that wasn't copied exactly.

## Troubleshooting

| Message / symptom | Fix |
| --- | --- |
| `… already exists (reports are never overwritten)` | Use a new `--output` folder |
| `No live source hashes. Use --snapshot …` | `review` on an imported netlist: add `--snapshot` |
| `--boards is required when not running in a terminal` | Add `--boards N` |
| `kicad-cli not found` | Install KiCad 10 and put `kicad-cli` on PATH, or pass a netlist / `circuit.json` |
| `Project changed during export; save it and retry` | Save in KiCad, run again |
| `No header row with a quantity column …` | `--column quantity="<header>" --column mpn="<header>"` |
| `DigiKey credentials missing` | Fill `.env` ([Setup](00-setup.md#keys)). Results stay `unknown` |
| LCSC / JLCPCB always `? (unknown)` | No keys (expected), or your IP isn't whitelisted at LCSC |
| `DigiKey lists <MPN> under <X>, not <Y>` | Fix the Manufacturer spelling in KiCad |
| Search shows `shortfall` with few results | Raise `--max-pages`, check value/package in the request, read "Parametric value filter: not used — …" in that line's `sourcing.md` |
| `R27 matches no reference or MPN in the design` | Check the spelling in `--alternatives-for` (references or exact MPNs) |
| `R27 is not in the BOM (…)` | That part is DNP or excluded: pick a populated one |
| `Use either --alternatives-for … or --alternatives-even-if-available` | Choose one: named lines, or every R/C/L line |
| Alternatives section says "Not searched: not selected" | Expected: you didn't pick that line. Add it to `--alternatives-for` |
| `fill_fields.py` refuses to write | Close the schematic in KiCad (lock file) |
| parts-list reads the wrong column, counts DNP parts as fitted, or the quantity looks wrong | Run `parts-list LIST --dry-run` first: it shows which header became which column and how the first rows are read ("look up" / "skip: not fitted"), with no lookups. Fix with `--column fitted="DNP"` etc. |
| `No header row with an MPN or LCSC/JLCPCB part column was found` | Pass `--column mpn="<header>"` and check with `--dry-run` |
| parts-list row says *quantity unreadable* | Fix the cell (`4`, `4 pcs`); text like `lots` or `see note` is never guessed |
| `--against` row says *same MPN, manufacturer not confirmed* | Fill the Manufacturer field in KiCad (or in the BOM) |
| `--against` row says *designator only (could be unrelated)* | The reference part wasn't found; a different part sits at that designator, possibly your own `U1`. Check by hand |
| Inductor line: "Inductor current ratings unknown …" | Add `Isat` and `Irms` fields to the inductor symbol, or use an MPN DigiKey lists with current ratings |
| refplane `stale_fill` errors | Refill zones in KiCad (B), save, run again; or install `kicad-cli` so it refills a copy itself |
| `kicad-cli refill failed` | Run with `--refill no` to check the saved fills, or point `--kicad-cli` at your KiCad 10 binary |
| refplane asks nothing on a re-run | Answers are saved in `<board>.refplane.json`; pass `--ask` to change them |
| refplane checks too many / too few nets | `--classes`, `--nets`, `--include-power`; mounting-hole nets (`Net-(H*-Pad1)`) are checked but give no findings |
| Datasheet `needs_review`: two local files match | Remove or rename the stale PDF in `docs/` |
| DRC ignores the JLC rules | Reopen Board Setup after `install`; check `board.kicad_dru` sits next to the `.kicad_pro` |
| `preflight` says rules are stale | Stackup changed: run `install` again |
| Tests fail on your machine | Report it. Tests are offline and must never need keys or network |

## Caches

`cache/digikey/` (one JSON per MPN), `cache/sourcing/` (searches and stock), `cache/datasheets/`
(PDFs by hash). Offline runs reuse them. Online runs refresh, except a part record fetched in
the last 24 h is reused. Deleting `cache/` is safe; it only costs API calls.
