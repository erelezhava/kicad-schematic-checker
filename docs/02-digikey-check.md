# 2. Verify parts against DigiKey, fill empty Manufacturer fields

Checks that each MPN exists on DigiKey, is Active, and matches the value, ratings and package
you declared. Can then fill **empty** Manufacturer fields for you. Needs DigiKey keys
([Setup](00-setup.md#keys)).

[← All guides](../USER_GUIDE.md) · Commands: `digikey-fetch`, `digikey-check`, `fill-manufacturer`

## Run it

```sh
# 1. look up every unique MPN once (online; cached per MPN in cache/digikey/)
python3 -m kicad_checker digikey-fetch runs/board-001/circuit.json

# 2. compare the cached data with the schematic (offline)
python3 -m kicad_checker digikey-check runs/board-001/circuit.json --output runs/board-001-dk
```

Open `runs/board-001-dk/digikey.md`.

What `digikey-fetch` asks DigiKey again, without any option:

- answers older than 30 days (lifecycle and stock change); `--max-age DAYS` changes the limit,
  `--max-age 0` keeps any age. `digikey.md` states the age of the data it used.
- lookups that failed last time (HTTP 401, 429, 5xx). A failure is never kept as an answer.

`--refresh` asks again for every part. If a renewal fails, the older good answer is kept.

Rate limit (HTTP 429): the tool waits once when DigiKey asks for a short pause. If the limit
is still hit, typically the daily quota, it stops and reports how many parts were not asked.
Run it again later; what is already cached stays.

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
`fill-manufacturer` writes those suggestions into the schematic. It takes the newest
`<project>_report_R<n>` by itself (`run` prints the count and the command in `summary.md`):

```sh
python3 -m kicad_checker fill-manufacturer ~/projects/board            # preview
python3 -m kicad_checker fill-manufacturer ~/projects/board --apply    # write (close KiCad first)
```

| Option | Use |
| --- | --- |
| `--report R` | Use this report folder or `digikey.json` instead of the newest one |
| `--prefer "Murata Electronics"` | Pick a name when DigiKey lists several (repeatable) |
| `--backup` | Keep `*.kicad_sch.bak-<time>` copies |

When DigiKey lists several names for one MPN, `kicad_checker/manufacturer_aliases.json` decides:
`same_company` merges spellings of one company (`Yageo America` → `YAGEO`), and
`family_preference` picks by reference prefix (R/C/L/FB: `Murata Electronics` over
`Murata Power Solutions`). Anything else is skipped until you pass `--prefer`. Every chosen
name says why in the preview.

Safety: never overwrites a value; previews unless `--apply`; the symbol's MPN must still match
the MPN DigiKey was asked about (a renumbered or changed part is skipped); edits only the active
design (root sheet + its sub-sheets, never `.history/`); refuses while KiCad has the file open.
Every changed file is checked to still parse and then replaced in one step, so an interrupted
run cannot leave a half-written schematic; line endings stay as they were (also on Windows).
Then `git diff` in your design repo, **Update PCB from Schematic (F8)**, and run the checks again.

`tools/fill_fields.py DIGIKEY_JSON PROJECT [--apply]` still works and does the same.

## Gotchas

- `DigiKey credentials missing` — fill `.env`. Results stay `unknown`, never "pass".
- `DigiKey lists <MPN> under <X>, not <Y>` — fix the Manufacturer spelling in KiCad.
- The daily quota is per DigiKey app. The cache means each MPN costs one call per 30 days,
  across all projects.
- `DigiKey lookup failed (HTTP ...)` in the report: run `digikey-fetch` again, it retries those.

**Next:** [3. Collect datasheets](03-datasheets.md) or [5. Check stock](05-inventory-check.md)
