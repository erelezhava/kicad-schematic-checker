# 5. Can I build N boards from our stock?

Compares the board's BOM with your stock file (JLCPCB parts inventory, or any company list),
works out quantities with spares, and tells you what to buy. Optionally checks market stock and
finds R/C/L alternatives for missing parts.

[← All guides](../USER_GUIDE.md) · Command: `inventory-check`

## Run it

Export your stock to `stock/` (git-ignored), then:

```sh
python3 -m kicad_checker inventory-check ~/projects/board/board.kicad_pro \
  --stock JLC=stock/jlc-all-parts.csv --output runs/board-inv-001
```

In a terminal it asks three questions:

1. **How many boards?**
2. **Some parts are missing or short. Check market stock?** (DigiKey, LCSC, JLCPCB; read-only)
3. **Which lines should get alternatives?** It lists the numbered lines that may need one:

   ```text
   Lines that may need alternatives (3):
     1. R27 — RC0402FR-0710KL (not in stock)
     2. C2, C7 — GRM155R71C104KA88D (insufficient)
     3. L1 — LQM2HPN2R2MG0L (not in stock, available on the market)
   Search DigiKey for alternatives? Enter = no · a = all listed · numbers or references/MPNs (e.g. 1,3 or R27,C2) · e = every R/C/L line:
   ```

   | Answer | Searches |
   | --- | --- |
   | Enter | nothing (no API calls) |
   | `a` | every listed line |
   | `1,3` or `R27,C2` or an MPN | only those, also lines that aren't listed (already in stock) |
   | `e` | every R/C/L line in the BOM (second sources, [guide 6](06-second-sources.md)) |

Same thing without prompts (scripts, CI, AI assistants):

```sh
python3 -m kicad_checker inventory-check ~/projects/board/board.kicad_pro \
  --stock JLC=stock/jlc-all-parts.csv --boards 5 --check-market --find-alternatives \
  --no-input --output runs/board-inv-001
```

Only some lines, without prompts: `--alternatives-for R27,C2` (references or MPNs).

The design can be a `.kicad_pro`, a root `.kicad_sch`, the project folder, an exported XML
netlist, or a `circuit.json` from `extract`.

## The stock file

CSV or XLSX, one row per part. Columns are found by header name: MPN, C-code (LCSC part),
quantity, and optionally manufacturer, description, value, package, category.
`Available Qty` wins over `Total Qty`. Unusual headers:

```sh
--column quantity="Pieces" --column mpn="Article"
```

Several stock sources: repeat `--stock Office=stock/office.xlsx`. Each is counted separately;
add `--combine-sources` when they are one physical pool.

## How it decides

| Step | Rule |
| --- | --- |
| BOM lines | Grouped by MPN (or C-code). DNP, power symbols and *Exclude from BOM* listed separately |
| Quantity | references × boards + spare. Spare = 5 % or 2 parts per line, whichever is more |
| Match | Same MPN counts. A C-code whose stock row has a **different** MPN never counts. Same MPN under another C-code counts, and the report tells you which C-code to put in the schematic (JLCPCB uses consigned stock by C-code) |
| In-stock alternatives | For missing R/C/L: other stock rows with the same value and package, equal-or-better tolerance, voltage/power and dielectric (inductors: see below), then checked on DigiKey under the stock row's own manufacturer (JLC never states grade). If DigiKey lists that MPN only under another maker, it's "manufacturer differs", never a match |
| Original part baseline | Your MPN's DigiKey record fills in ratings the schematic doesn't state. Your schematic values always win |
| Market check | Stock at DigiKey / LCSC / JLCPCB for the shortfall quantity |
| DigiKey alternatives | For lines not available anywhere, or the lines you picked: a search excluding your MPN, by category, stock and value. Lines you didn't pick show "Not searched: not selected" |

## Inductors: current ratings decide

In a DC-DC stage the wrong inductor saturates or overheats, so an alternative must be at least
as good as the original on:

| Rating | Rule | Read from (first found wins) |
| --- | --- | --- |
| Saturation current (Isat) | ≥ original | schematic field `Isat` / `Saturation Current`, else the original's DigiKey record |
| Rated current | ≥ original | `Irms` / `Rated Current` / `Current Rating` / `Current` / `Irated`, else DigiKey |
| DC resistance (DCR) | ≤ original | `DCR` / `DC Resistance`, else DigiKey |
| Shielding | shielded → shielded; semi-shielded → shielded or semi | `Shielding`, else DigiKey |

When DigiKey lists several values, the worst one is used. `Typ` values are not guaranteed
limits and count as unknown.

**No current rating known → no DigiKey search** for that inductor; the report says why. An
in-stock inductor can then never be a full `match`. LCSC stock descriptions like
`1.4A 110mΩ 2.1A` don't say which current is which, so they are never guessed. Fix: add `Isat`
and `Irms` fields to the symbol, or use an MPN that DigiKey knows.

## Output files

| File | Content |
| --- | --- |
| `inventory.md` | Summary, BOM vs stock, alternatives, market stock, next steps |
| `shortfall.csv` | What to buy: MPN, stock C-code, needed, shortfall |
| `inventory.json` | Everything, for scripts and AI assistants |
| `market/line-NNN/`, `alternatives/line-NNN/` | Detail per BOM line |

## Reading the BOM table

| Status | Meaning |
| --- | --- |
| in stock | One source has enough, spare included |
| split across sources | Only the sources together have enough (`--combine-sources` if they can be pooled) |
| insufficient | Matched but short; shortfall given |
| not in stock | No matching row |
| quantity unknown | Matched row has a blank or unreadable quantity (never counted as stock) |
| no MPN/C-code | Can't be matched; fill its fields ([guide 1](01-schematic-check.md)) |
| ⚠ | Counted, but read the note (C-code differs, C-code-only match, spelling…) |
| `(stock: C123456)` | Schematic has no LCSC field but your stock has this C-code: add it |

Market check: `available` (eligible at ≥ 1 supplier) · `stock_issues` (seen, but stock, MOQ or
lifecycle failed) · `unknown` (no data, e.g. no keys).

## Options

| Option | Use |
| --- | --- |
| `--boards N` | Skip question 1 (required when not in a terminal) |
| `--check-market` / `--no-check-market` | Answer question 2 |
| `--find-alternatives` / `--no-find-alternatives` | Answer question 3 with "all listed" / "no" |
| `--alternatives-for R27,C2` | Answer question 3 with these lines only (references or MPNs, repeatable). Searched even when in stock |
| `--alternatives-even-if-available` | Every R/C/L line ([guide 6](06-second-sources.md)). Can't be combined with `--alternatives-for` |
| `--spare-percent 5`, `--spare-min 2` | Attrition spare per line |
| `--sheet NAME` | XLSX sheet (default: the one with the best header) |
| `--offline` | Caches only, no network |
| `--no-input` | Never prompt (automatic when not in a terminal) |
| `--verify-stock-alternatives` / `--no-verify-stock-alternatives` | DigiKey check of in-stock candidates (default: online when market/alternatives are on) |
| `--max-market-parts 20`, `--max-alternative-searches 10`, `--max-verify-lookups 40`, `--max-pages 2` | API budgets |
| `--max-age-hours H` | Market stock freshness limit |

Exit code `2` is normal here: it means something is short or needs review.

## Limits

- The stock file's date is not checked. Export it fresh before a build decision.
- Stock is a snapshot; "normally stocking" is a DigiKey flag, not a guarantee.
- Automatic alternatives: R/C/L only. Diodes, transistors and ICs: [guide 7](07-parts-search.md).
- LCSC / JLCPCB live lookups are not yet validated.

**Next:** [6. Second sources for flight hardware](06-second-sources.md)
