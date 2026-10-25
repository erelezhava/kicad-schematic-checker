# 1. Check the part data in a schematic

Finds missing or contradictory Manufacturer, MPN and Description fields, and gives the design
owner one list of fields to fix. Offline, no keys. This is the first step for every board.

[← All guides](../USER_GUIDE.md) · Commands: `extract`, `import-netlist`

## Run it

Save the project in KiCad first: only saved files are read.

```sh
python3 -m kicad_checker extract ~/projects/board/board.kicad_pro --output runs/board-001
```

Then open **`runs/board-001/owner-actions.md`**. Fix the listed fields in KiCad, save, and
extract again into a new folder (`runs/board-002`). Repeat until the list is empty.

No KiCad on this machine? Export the netlist elsewhere (KiCad 10, XML) and run:

```sh
python3 -m kicad_checker import-netlist board.xml --output runs/board-001
```

This skips ERC and the "is the project saved and unchanged" check.

## What it checks

**Identification** — every populated, purchasable part needs a Manufacturer and an exact MPN.

- Accepted field names: `Manufacturer`, `Manufacturer_Name`, `Mfr`, `MFG` and `MPN`,
  `Manufacturer_Part_Number`, `Mfr. Part #`, `PRT.NUM` (case, spaces and punctuation ignored).
- Empty values and placeholders (`TBD`, `?`, `N/A`) don't count. Two aliases with different
  values are a conflict.
- An LCSC/JLCPCB C-code alone is **not** an MPN.
- For non-R/C/L parts the Value is compared with the MPN. `LP5912` for `LP591233MDRVREP`
  passes (family prefix). `…-SP` vs `…-TU`, an unrelated number, or an MPN shorter than the
  Value is `needs_review`.

**Description** — every part needs one. For R/C/L (`R1`, `C2`, `L3`…) it must state the value
and, when the footprint has a size code, the package. Both are compared with the Value, the
footprint (`C_0805_2012Metric` → 0805) and any `Voltage`/`Tolerance`/`Power`/`Dielectric`
fields. Distributor text works as-is (`CAP CER 2.2UF 25V X7R 0805`). Generic library text
(`Unpolarized capacitor`) stays `needs_review`.

## Exemptions

Exempt automatically: DNP parts and KiCad power symbols.

Exempt on request: add `Checker_NonPurchasable = true` **and** a `Checker_ExemptionReason`
field to the symbol. Mounting holes, connectors and test points are **not** exempt by default:
if you buy them, they need an MPN.

## Output files

| File | Read it for |
| --- | --- |
| `owner-actions.md` | **Start here.** Per component: what to fill or correct |
| `identification.md` | Manufacturer/MPN audit, with a grouped "What to fix" summary |
| `description.md` | Description audit and cross-checks |
| `circuit.json` | Input for every other command |
| `netlist.xml`, `erc.json` | Raw KiCad export and ERC (extract only) |

Statuses: `pass`, `fail` (two declared facts contradict), `needs_review` (missing or
unreadable), `not_applicable`. `extract` exits `0` when the extraction itself worked, even if
owner actions are open.

## Gotchas

- `Project changed during export; save it and retry` — you edited in KiCad during the export.
- Each run needs a **new** `--output` folder. Reports are never overwritten.
- Only the default assembly variant is checked.

**Next:** [2. Verify parts against DigiKey](02-digikey-check.md)
