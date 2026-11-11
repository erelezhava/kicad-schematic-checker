# 13. Run every check in one go, results inside the project

One command runs the schematic, DigiKey, JLCPCB, reference-plane, MCU-pin and datasheet checks and puts all
results into a numbered report folder next to your `.kicad_pro`, with a full log.

[← All guides](../USER_GUIDE.md) · Command: `python3 -m kicad_checker run`

## Run it

Save in KiCad first, then from the checker folder:

```sh
python3 -m kicad_checker run ~/projects/board
```

Pass the `.kicad_pro`, its `.kicad_sch`/`.kicad_pcb`, or a folder that contains exactly one
project (KiCad's `.history/` and old report folders are ignored).

```text
board/
├── board.kicad_pro
├── ...
└── board_report_R0/                  ← first run; next run makes _R1, then _R2 ...
    ├── summary.md                    ← start here: one line per check
    ├── run.log                       ← everything: commands, kicad-cli calls, versions, timings, errors
    ├── run.json                      ← same, machine-readable (+ SHA-256 of every design file)
    ├── 01_schematic/                 owner-actions.md, identification.md, description.md, ERC
    ├── 02_digikey/                   digikey.md (summary.md shows how many empty Manufacturer fields fill-manufacturer can fill)
    ├── 03_jlcpcb/                    <board>.kicad_dru, preflight.txt, drc.md, drc.json
    ├── 04_refplane/                  report.md, <layer>.svg
    ├── 05_mcu_pins/                  mcu-pins.md: CubeMX .ioc vs MCU nets (when the repo has one .ioc)
    └── 06_datasheets/                datasheets.md: PDF per active part (docs/ up to the repo root, cache, DigiKey link)
```

A report folder is never reused or overwritten. Delete old ones yourself when you no longer need
them.

## Options

| Option | Effect |
| --- | --- |
| `--checks schematic,digikey` | Run only these (default: all six: schematic, digikey, jlcpcb, refplane, mcu_pins, datasheets) |
| `--ioc FILE` | CubeMX file for `mcu_pins` (default: the one `.ioc` in the project/repo) |
| `--offline` | No DigiKey requests; use the cache only |
| `-y` | Ask nothing: refplane uses `<board>.refplane.json` if saved, otherwise its guesses (listed as "assumed" in the summary) |
| `--tier`, `--accept` | Same as in [8. JLCPCB rules](08-jlcpcb-rules.md) |
| `--refplane-args ...` | Extra `refplane check` flags, must be last: `--refplane-args --layers sig,gnd,gnd,sig --classes USB` |

## What it does to your project

- Reads the design files; never edits them. It checks their SHA-256 before and after and warns
  if you saved during the run.
- Writes only the new `<project>_report_R<n>/` folder.
- JLCPCB: rules are generated **into the report** and DRC runs on a temporary copy of the board
  with those rules. Your own `<board>.kicad_dru` is not created or changed. Install the rules into
  KiCad with `python3 -m jlcpcb_rules install` when you want them there.
- refplane: if you answer its questions interactively it may offer to save them to
  `<board>.refplane.json` next to the board, as it does when run on its own.

In a git-tracked design, add `*_report_R*/` to the project's `.gitignore` unless you want to
commit the reports.

## Log and secrets

`run.log` has one timestamped line per printed line, tagged with the step
(`[schematic]`, `[digikey]`, `[jlcpcb]`, `[refplane]`), plus every `kicad-cli` call with its
exit code, duration and output, and the Python traceback of any step that crashed. Values of all
keys named in `.env.example` are replaced with `***`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Nothing open in what was checked (not "design approved") |
| 1 | At least one check failed (e.g. DRC error, refplane error, contradictory fields) |
| 2 | Open items: owner actions, needs_review, warnings, DRC not run, design changed during the run |
| 3 | A step could not run (e.g. kicad-cli missing for the schematic step) |

One failing or blocked step never stops the others; DigiKey is marked "not run" when the
schematic step produced no `circuit.json`.

## Needs

`kicad-cli` (KiCad 10) on PATH for the schematic step and DRC. Without it, JLCPCB preflight and
refplane still run (refplane then uses the zone fills saved in the board).
