# 6. Second sources for flight hardware

Finds alternative R/C/L parts for **every** line, or just the lines you name, including the
ones you already have, so each part has an independent second source before a build. Needs
DigiKey keys.

[← All guides](../USER_GUIDE.md) · Command: `inventory-check --alternatives-even-if-available` or `--alternatives-for`

## Run it

```sh
python3 -m kicad_checker inventory-check ~/projects/board/board.kicad_pro \
  --stock JLC=stock/jlc-all-parts.csv --boards 5 \
  --check-market --alternatives-even-if-available --no-input \
  --output runs/board-inv-002
```

Every R/C/L line gets a DigiKey search and an in-stock check. This spends the search budget
fast: raise `--max-alternative-searches` (default 10 lines) for bigger boards.

## Only the parts that matter

Usually only a few parts are critical (sense resistors, the LDO output cap, a single-source
inductor). Name them instead of searching the whole BOM:

```sh
python3 -m kicad_checker inventory-check ~/projects/board/board.kicad_pro \
  --stock JLC=stock/jlc-all-parts.csv --boards 5 \
  --check-market --alternatives-for R27,C2,LQM2HPN2R2MG0L --no-input \
  --output runs/board-inv-003
```

References or MPNs, comma-separated, repeatable, case-insensitive. Those lines are searched
even when in stock; the report calls them `second_source` (in stock) or `replacement`
(missing). In a terminal you can answer the pick-list question with the same list
([guide 5](05-inventory-check.md)).

The run stops before any API call if a name matches nothing, points at a DNP/excluded part, or
is combined with `--alternatives-even-if-available` or `--no-find-alternatives`. A chosen
diode, transistor or IC is reported as "R/C/L only".

## Reading the result

For each line `inventory.md` shows:

- **`requires …`** — the requirements used, and which ones came from the original part on
  DigiKey, e.g. `requires ≤1% · ≥62.5 mW · AEC-Q200 (from original part on DigiKey: power)`.
- **counted / other manufacturers** — how many candidates passed, and how many are from a
  **different** manufacturer. Only those are independent second sources. A part from your
  original manufacturer is marked "same manufacturer (not an independent source)".
- **In-stock candidates** with a DigiKey verdict:

| Verdict | Meaning |
| --- | --- |
| `match` | Every check passed on DigiKey data and the part is Active |
| `possible` | Something unconfirmed: AEC-Q200 not listed, not Active, a rating not declared, MPN not on DigiKey |
| `rejected` | DigiKey contradicts a requirement |
| manufacturer differs | DigiKey lists the stock row's MPN only under another manufacturer: not verified, never a `match` |

Every verdict is an engineering-review lead, never an approval.

## Make the requirements yours

Add `Tolerance` and `Power` fields (or state them in the Description) for resistors in critical
circuits, and `Voltage`/`Dielectric` for capacitors. Then the search uses your design intent
instead of copying the original part's ratings.

## Inductors

Inductor alternatives must match or beat the original's saturation current, rated current, DCR
and shielding (`requires ≤20% · Isat ≥2.1 A · Irated ≥1.6 A · DCR ≤110 mOhm · shielded`).
If no current rating is known, from the schematic or the original part on DigiKey, the line is
not searched. Add `Isat` and `Irms` fields to the inductor symbol. Details:
[guide 5](05-inventory-check.md#inductors-current-ratings-decide).

For DC-DC inductors copied from a reference design, a catalog `match` still needs the converter's
datasheet check (ripple current, peak current at your load, core losses).

## Not covered automatically

- 0 Ω jumpers (catalogs rate them by max resistance/current, not tolerance).
- Diodes, transistors, ICs: write a request file ([guide 7](07-parts-search.md)).

## What `match` does not tell you

Catalog data doesn't cover DC-bias derating, ESR, pulse handling, thermal behaviour or your
circuit's real stress. AEC-Q200 is a sensible proxy for CubeSat COTS parts but it is **not** a
space-grade or radiation qualification. Check the manufacturer datasheet before you swap.

**Next:** [7. Find parts by your own criteria](07-parts-search.md)
