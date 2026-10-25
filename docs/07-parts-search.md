# 7. Find parts, check one part, check a whole BOM

Three smaller sourcing commands, all offline by default. Add `--online` for fresh, read-only
supplier requests (needs keys).

[← All guides](../USER_GUIDE.md) · Commands: `parts-search`, `parts-stock`, `parts-bom`

| I want to… | Command |
| --- | --- |
| Find parts matching my criteria, any DigiKey category (MOSFET, LDO, connector, sensor, RLC…) | `parts-search` |
| Stock and lifecycle of **one exact** part, per supplier | `parts-stock` |
| Market stock of a whole BOM, without an inventory file | `parts-bom` |

## parts-search: find parts by your own criteria

Write a small request file. RLC (`examples/sourcing/resistor.json`):

```json
{"schema_version": 1, "kind": "resistor", "value": "10k", "package": "0402",
 "tolerance_percent": 1, "automotive": true, "quantity": 100, "min_stock": 10000,
 "constraints": [{"parameter": "Power (Watts)", "op": "min", "value": "0.0625", "unit": "W"}]}
```

Anything else uses DigiKey's parameter names (`examples/sourcing/mosfet.json`):

```json
{"schema_version": 1, "kind": "mosfet", "keywords": "N-channel MOSFET SOT-23 30V logic level",
 "category": 278, "package": "SOT-23-3", "quantity": 100, "min_stock": 1000,
 "constraints": [
   {"parameter": "Drain to Source Voltage (Vdss)", "op": "min", "value": 30, "unit": "V"},
   {"parameter": "Rds On (Max) @ Id, Vgs", "op": "max", "value": 60, "unit": "mOhm",
    "at": {"Vgs": {"value": 4.5, "unit": "V"}}},
   {"parameter": "Operating Temperature", "op": "covers", "value": [-40, 125], "unit": "°C"}]}
```

Operators: `eq` (text or exact value), `min`, `max`, `covers` (a range such as temperature).
`category` is a DigiKey ID or name. More examples: `capacitor`, `inductor`, `diode`,
`regulator`, `connector`, `catalog` in [examples/sourcing/](../examples/sourcing/).

```sh
python3 -m kicad_checker parts-search examples/sourcing/mosfet.json --dry-run          # shows the plan, no network
python3 -m kicad_checker parts-search examples/sourcing/resistor.json \
  --replace AF0402FR-0710KL --online --output runs/res-alt-001                         # live search
```

Result in `sourcing.md`: **`target_met`** when at least 3 distinct parts from at least 2
manufacturers pass (packaging variants count once), otherwise **`shortfall`** with how many
are missing.

| Option | Use |
| --- | --- |
| `--replace MPN` | Exclude the part you're replacing |
| `--max-pages 3` | DigiKey pages of 50 (1–20) |
| `--digikey-snapshot FILE` | Use a recorded response (offline demo: `examples/sourcing/recorded-resistor.json`) |
| `--full-report` | Also write `sourcing-full.md` with every screened part in detail |

## parts-stock: one exact part

```sh
python3 -m kicad_checker parts-stock LP591233MDRVREP --manufacturer "Texas Instruments" \
  --quantity 10 --online --output runs/lp5912-stock-001
```

Each supplier separately (DigiKey per packaging: cut tape, reel, Digi-Reel), with the reason an
offer isn't eligible: MOQ, stock, lifecycle, normally stocking, freshness. Add `--jlc-code
C123456` for JLCPCB. Use the manufacturer's exact catalog spelling: a prefix never matches.

## parts-bom: market stock of a whole BOM

```sh
python3 -m kicad_checker parts-bom runs/board-001/circuit.json --boards 10 --online --output runs/board-stock-001
```

Checks up to `--max-parts` (default 10) unique parts. For most builds
[inventory-check](05-inventory-check.md) is better: it also uses your stock, adds spares and
finds alternatives.

## Limits

- `target_met` means "enough catalog candidates", not engineering equivalence. Every candidate
  stays `requires_review`: check DC bias, ESR, pulse/power derating, pin mapping, thermal limits.
- Package-name equality doesn't prove footprint/pad compatibility.
- Fewer than 3 in a bounded search doesn't prove fewer than 3 exist.
- IC replacements (LDO, DC-DC, MCU) can be searched but are never suggested automatically.

All request fields, filters and cache rules: [SOURCING.md](../SOURCING.md).

**Next:** [8. JLCPCB manufacturing rules for the PCB](08-jlcpcb-rules.md)
