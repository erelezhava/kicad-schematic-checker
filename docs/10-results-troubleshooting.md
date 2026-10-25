# 10. Results, exit codes, troubleshooting

[← All guides](../USER_GUIDE.md)

## Result values

| Where | Values | Meaning |
| --- | --- | --- |
| extract, digikey-check, review | `pass` · `fail` · `needs_review` · `not_checked` · `not_applicable` | `fail` = two declared facts contradict. `needs_review` = missing or unreadable. `not_checked` = rule still `draft` |
| inventory-check | in stock · split across sources · insufficient · not in stock · quantity unknown · no MPN/C-code | [guide 5](05-inventory-check.md#reading-the-bom-table) |
| Market check | `available` · `stock_issues` · `unknown` | Eligible somewhere · seen but stock/MOQ/lifecycle failed · no data |
| Alternatives | `match` · `possible` · `rejected` | [guide 6](06-second-sources.md#reading-the-result). Always a lead, never an approval |
| parts-search | `target_met` · `shortfall` | ≥ 3 distinct parts from ≥ 2 manufacturers, or not |
| jlcpcb_rules | error · warning · info | Beyond JLC limit · paid option or recommendation · FYI |

Missing data never becomes a pass or a fail.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Nothing open in what was checked (`extract`: extraction worked) |
| `1` | A `fail`: a contradiction |
| `2` | Something to review, fill or buy |
| `3` | Invalid input or tool error |

`0` never means "design approved". In CI, treat `1` and `3` as failures and `2` as a warning.

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
| `fill_fields.py` refuses to write | Close the schematic in KiCad (lock file) |
| Datasheet `needs_review`: two local files match | Remove or rename the stale PDF in `docs/` |
| DRC ignores the JLC rules | Reopen Board Setup after `install`; check `board.kicad_dru` sits next to the `.kicad_pro` |
| `preflight` says rules are stale | Stackup changed: run `install` again |
| Tests fail on your machine | Report it. Tests are offline and must never need keys or network |

## Caches

`cache/digikey/` (one JSON per MPN), `cache/sourcing/` (searches and stock), `cache/datasheets/`
(PDFs by hash). Offline runs reuse them. Online runs refresh, except a part record fetched in
the last 24 h is reused. Deleting `cache/` is safe; it only costs API calls.
