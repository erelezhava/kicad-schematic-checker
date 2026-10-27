# 12. Check a recommended parts list

You have a list of parts someone recommends: a chip vendor's reference-design BOM, your
company's approved-parts list, a colleague's "use these" sheet. `parts-list` looks up every row
on DigiKey and tells you which parts exist, are Active and in stock. It can also compare the
list with your stock files and find alternatives for parts that are obsolete or not stocked.

[← All guides](../USER_GUIDE.md) · Command: `parts-list`

## Run it

The list is a CSV or XLSX with one exact part per row. It needs an MPN column and should have a
Manufacturer column. A per-board quantity column and an LCSC column are optional. Title rows
above the header are fine.

```csv
Ref Des,Manufacturer,Manufacturer Part Number,Qty per board,Description
U1,Texas Instruments,LP591233MDRVREP,1,3.3 V LDO
C3,Murata,GCM155R71C104KA55J,4,100nF X7R 0402
```

```sh
python3 -m kicad_checker parts-list recommended.csv --boards 5 --online --output runs/reflist-001

# also compare with your stock:
python3 -m kicad_checker parts-list recommended.csv --boards 5 --online \
  --stock JLC=stock/jlc-all-parts.csv --output runs/reflist-002
```

**Check the columns first** (no lookups, nothing written):

```sh
python3 -m kicad_checker parts-list recommended.csv --dry-run
```

It prints which header became which column, which headers are ignored, the first 10 rows as they will
be read (designators, MPN, manufacturer, per-board quantity, and "look up" / "skip: not fitted" /
"+1 alternate"), and how many DigiKey lookups a real run needs. If a column is wrong, fix it with
`--column mpn="Header"` and dry-run again. `--preview-rows 30` shows more rows.

Without `--online` it only uses DigiKey data already in the cache (from earlier runs of any
command). Rows with no cached data show `not checked`.

### Reference-design BOMs (TI, ADI, NXP, ST evaluation boards)

Download the eval board's BOM (CSV/XLSX from the product page) and pass it as-is:

- **Columns** such as `Designator`, `Quantity`, `PartNumber`, `PackageReference`, `Manufacturer 1`,
  `Manufacturer Part Number 1`, `Populate`/`Fitted` are recognized.
- **Designators** are kept and shown in the report.
- **Not fitted rows** are listed separately and never looked up. The answer depends on the column's
  header:

  | Column header | `Yes` / `1` / `x` means | `No` / `0` means |
  | --- | --- | --- |
  | `Fitted`, `Populate`, `Mount`… | fitted | not fitted |
  | `DNP`, `DNF`, `Do Not Populate`, `NoPop` | **not** fitted | fitted |

  The words "DNP" / "Not Fitted" always mean not fitted and "Fitted" always means fitted,
  under any header. Quantity 0 (TI's convention) and "DNP"/"Not Fitted" in Value or Description
  also count. TI's `!PCB1` row (the bare board) is recognized too. Check with `--dry-run`: each
  row shows "look up" or "skip: not fitted".
- **Quantity per board:** a quantity column wins. A blank cell uses the designator count
  (`C1-C3, C7` = 4), else `--quantity`, with a note. `100pcs`, `4 ea`, `12 units` are read; a cell
  that can't be read (`lots`, `see note`) makes the row *quantity unreadable* instead of guessing.
- **Vendor alternates**: `Alternate PartNumber`/`Alternate Manufacturer`, `Manufacturer 2`/
  `Manufacturer Part Number 2`, or two MPNs in one cell (`A / B`, `A; B`) are the reference design's
  own approved second sources. When the main part is obsolete, unstocked or not found, these are
  checked first. If one is available at DigiKey, or is in your stock, the row is marked ✔ and the
  tool's own alternative search is skipped.

## Read the result

`parts-list.md` (plus `parts-list.json` and `parts-list.csv` for spreadsheets):

| Status | Meaning | What to do |
| --- | --- | --- |
| available | Found, Active, and at least one DigiKey offer covers the quantity (cut tape counts) | Nothing |
| stock issues | Found and Active, but low stock, not normally stocking, MOQ too high, or stale data | Read the Notes column; check alternatives |
| not active | Obsolete, Last Time Buy, Not For New Designs, discontinued… | Pick an alternative |
| not found | DigiKey doesn't list this exact MPN | Check for a typo or a missing ordering suffix |
| manufacturer differs | DigiKey lists the MPN under another manufacturer | Correct the Manufacturer column |
| ambiguous | Same MPN under several manufacturers and the list gives none | Add a Manufacturer column |
| no MPN | The row has only a C-code | Add the MPN |
| not checked | No DigiKey data (offline without cache, or lookup budget reached) | Run with `--online` |
| not fitted / not a part | DNP row, or the bare PCB | Nothing (listed at the end of the report) |
| quantity unreadable | The quantity cell can't be read (`lots`, `see note`) | Fix the cell; availability isn't judged on a guess |
| ✔ vendor alternate … | The main part has a problem, but a vendor-listed alternate is available or in stock | Use the alternate (same reference design) |

Manufacturer spellings are compared after removing trailing words such as Inc, NV, Technologies
or Electronics, so "Murata" matches "Murata Electronics". The table also shows lifecycle,
DigiKey/LCSC/JLCPCB stock, your stock (with `--stock`), AEC-Q qualification and a datasheet link.
The CSV adds DigiKey part numbers.

**Problem parts and alternatives** (rows that are *not active* or have *stock issues*):

- For resistors, capacitors and inductors, the requirements are taken from the recommended
  part's own DigiKey record: value, package, tolerance, voltage, power, dielectric and AEC-Q200.
  Inductors also need saturation current (Isat), rated current and DC resistance (DCR) at least as good,
  and shielding at least as good (shielded → shielded; semi-shielded → shielded or semi-shielded).
  If DigiKey lists no current rating for the inductor, no search is made, because value and package
  alone could suggest an under-rated part.
- **In stock:** matching parts from your stock files, each checked against its own DigiKey record
  and marked `match` / `possible` / `rejected` (same meaning as in [6. Second sources](06-second-sources.md)).
- **DigiKey:** a search excluding the recommended MPN. It shows how many *other* manufacturers
  were found. A candidate from the same manufacturer is marked as not an independent source.
- Other parts (ICs, connectors, diodes…) are listed with "review manually". Use
  [parts-search](07-parts-search.md) with your own criteria for those.

## After copying: check your schematic against the reference BOM

Once the reference circuit is in your KiCad project, compare it with the reference BOM:

```sh
python3 -m kicad_checker parts-list eval-bom.csv --online \
  --against ~/projects/board/board.kicad_pro --output runs/reflist-check-001
```

`--against` takes a KiCad project, an exported netlist or a `circuit.json`. The expected positions of
each fitted reference row are its designators (or its per-board quantity). They are filled in this
order, each schematic part used once:

1. the declared designator, when it holds the same part or the same R/C/L value;
2. the reference MPN or a vendor-listed alternate anywhere in your schematic (renumbered parts);
3. an R/C/L part with the same value and package size;
4. last resort: a different part at the declared designator, labelled "designator only (could be
   unrelated)".

"Same part" means manufacturer **and** MPN. The same MPN with a different or missing manufacturer is
reported separately, so confirm it.

| Result | Meaning |
| --- | --- |
| same part | Your schematic uses the reference manufacturer + MPN at every expected position |
| same MPN, manufacturer not confirmed | The MPN matches but the manufacturer is missing on one side: fill it in |
| same MPN, other manufacturer | The MPN matches but the manufacturers differ (after Inc/Ltd/… are removed): confirm it's the same part |
| positions missing | Some positions use the reference part, others weren't found (`2 of 3 positions found`) |
| vendor alternate | You used one of the reference BOM's listed alternates |
| substituted · match / possible / rejected | A different MPN at that position. For R/C/L it is screened against the reference part's DigiKey ratings (e.g. X5R where the reference uses X7R → rejected). Other parts say "compare datasheets and pinout" |
| value differs | Same designator, different value: usually a copy mistake |
| not in your schematic | Not found: not copied yet, or copied with a different part and designator |

Each position is checked separately, and the report lists them. One schematic part is never
counted for two BOM positions, and the worst position decides the row's result. Substitutes are
screened once per distinct manufacturer + MPN. The "designator only" fallback is the last step
because a pasted circuit can collide with your own `C1`/`U1`.

Every substitution is marked ⚠: the reference design was validated with its parts, so even a
catalog `match` needs re-verification in the circuit (stability, DC bias, current, timing). Parts in
your schematic that aren't in the reference BOM (the rest of your design) are counted, not compared.

## Exit code

| Code | When |
| --- | --- |
| `0` | Every row is *available*, *not fitted* or *not a part*, **and** with `--against` every row is *same part* or *vendor alternate* |
| `2` | Anything else: a stock or lifecycle problem, an unreadable quantity, or any `--against` row that is substituted (even `match`), missing, manufacturer-unconfirmed or different |
| `3` | Invalid input or tool error (missing or unreadable file, no MPN column found) |

## Options

| Option | Use |
| --- | --- |
| `--boards 5` | Quantity = per-board quantity × boards + spare (5 %, at least 2) |
| `--spare-percent 5`, `--spare-min 2` | Spare per row |
| `--quantity 2` | Per-board quantity for rows without a quantity or designators (default 1) |
| `--min-stock 1000` | Require more supplier stock than you need, for a healthier choice (default: just what you need) |
| `--stock LABEL=FILE` | Compare with stock files (repeatable). `--combine-sources` pools them |
| `--no-find-alternatives` | Report only, no DigiKey alternative searches |
| `--column mpn="Part No."` | Unusual column names (roles: mpn, lcsc, manufacturer, quantity, description, value, package, category, references, fitted) |
| `--sheet NAME` | Pick an XLSX sheet |
| `--against DESIGN` | Compare your schematic (project, netlist or circuit.json) with the reference BOM |
| `--dry-run`, `--preview-rows 30` | Show the column mapping and the first rows; no lookups, no report (`--output` not needed) |
| `--max-lookups`, `--max-alternative-searches`, `--max-pages` | API budgets (defaults 60, 10, 2) |

## Limits

- `available` is a catalog and stock snapshot, not a recommendation to design in. Check the
  datasheet, qualification and your circuit's stress yourself.
- Rows are matched by exact MPN. A family name (e.g. `LP5912` without the ordering suffix) shows
  `not found`.
- LCSC and JLCPCB columns stay `unknown` until those API keys are configured.
- Automatic alternatives cover R/C/L only and are always engineering-review leads.
- Open issue (Codex recheck 2026-10-27, R1): when one substitute MPN fills positions of two
  reference rows with different requirements (e.g. 16 V and 50 V), both rows can get the same
  verdict. Check substitutions that appear in more than one row by hand.

**Next:** [5. Inventory check](05-inventory-check.md), once the parts are in a schematic.
