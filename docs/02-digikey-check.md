# 2. Verify parts against DigiKey, fill empty Manufacturer fields

Checks that each MPN exists on DigiKey, is Active, and matches the value, ratings and package
you declared. Can then fill **empty** Manufacturer fields for you. Needs DigiKey keys
([Setup](00-setup.md#keys)).

[← All guides](../USER_GUIDE.md) · Commands: `digikey-fetch`, `digikey-check`, `tools/fill_fields.py`

## Run it

```sh
# 1. look up every unique MPN once (online; cached per MPN in cache/digikey/)
python3 -m kicad_checker digikey-fetch runs/board-001/circuit.json

# 2. compare the cached data with the schematic (offline)
python3 -m kicad_checker digikey-check runs/board-001/circuit.json --output runs/board-001-dk
```

Open `runs/board-001-dk/digikey.md`.

`digikey-fetch --refresh` re-downloads parts already in the cache. Use it before a build
review: lifecycle and stock change.

## What it checks

| Item | Result when wrong |
| --- | --- |
| R/C/L value, voltage, tolerance, power, dielectric, package size contradict DigiKey | `fail` |
| MPN not found, or several ambiguous listings | `needs_review` |
| Manufacturer spelled differently from DigiKey | `needs_review` |
| Lifecycle not Active (NRND, Obsolete…) | `needs_review` |
| IC package / pin count from DigiKey's package name | reported only (names like `TO-39-3` can be wrong) |

DigiKey is distributor catalog data. It confirms identity and catalog ratings; it is not
manufacturer evidence for circuit-specific properties.

## Fill empty Manufacturer fields

`digikey-check` suggests a manufacturer for every part with an empty field.
`fill_fields.py` writes those suggestions into the schematic:

```sh
python3 tools/fill_fields.py runs/board-001-dk/digikey.json ~/projects/board            # preview
python3 tools/fill_fields.py runs/board-001-dk/digikey.json ~/projects/board --apply    # write
```

| Option | Use |
| --- | --- |
| `--prefer "Murata Electronics"` | Pick a name when DigiKey suggests several (repeatable) |
| `--field NAME` | Fill another field (default `Manufacturer`) |
| `--backup` | Keep `*.kicad_sch.bak-<time>` copies |

Safety: never overwrites a value, previews unless `--apply`, edits only the active design
(root sheet + its sub-sheets, never `.history/`), refuses while KiCad has the file open. Check
the result with `git diff` in your design repo, then extract again.

## Gotchas

- `DigiKey credentials missing` — fill `.env`. Results stay `unknown`, never "pass".
- `DigiKey lists <MPN> under <X>, not <Y>` — fix the Manufacturer spelling in KiCad.
- The daily quota is per DigiKey app. The cache means each MPN costs one call, across all
  projects.

**Next:** [3. Collect datasheets](03-datasheets.md) or [5. Check stock](05-inventory-check.md)
