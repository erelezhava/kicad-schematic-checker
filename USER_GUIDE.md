# User guide: KiCad schematic checker

Reviews a saved KiCad design, helps you source its parts, and checks the PCB against JLCPCB's
limits. Plain Python commands; every run writes reports into a new folder. Same results
whether you run it yourself or through Claude, ChatGPT/Codex or a local Ollama model.

**It never** edits your schematic (except the opt-in `fill_fields.py`), places orders, approves
a substitution, or turns missing data into a pass.

## Pick your task

| I want to… | Guide | Network |
| --- | --- | --- |
| Install it, add my keys, try the demo | [0. Setup](docs/00-setup.md) | — |
| Find missing or contradictory Manufacturer / MPN / Description | [1. Schematic check](docs/01-schematic-check.md) | no |
| Check each MPN exists, is Active, matches value and package; fill empty Manufacturer fields | [2. DigiKey check](docs/02-digikey-check.md) | yes |
| Collect datasheet PDFs for every part | [3. Datasheets](docs/03-datasheets.md) | optional |
| Check datasheet requirements (pin connections, cap values, pin counts) | [4. Rules review](docs/04-rules-review.md) | no |
| **Know whether I can build N boards from our stock** | [5. Inventory check](docs/05-inventory-check.md) | optional |
| **Find second sources for flight hardware** (whole BOM or the parts I name) | [6. Second sources](docs/06-second-sources.md) | yes |
| Find parts by my own criteria, check one part, check a BOM's market stock | [7. Parts search](docs/07-parts-search.md) | optional |
| **Check a recommended parts list** (vendor reference BOM, approved parts) on DigiKey | [12. Parts list](docs/12-parts-list.md) | optional |
| **Copy a reference design**: check its BOM first, then compare my schematic with it (`--against`) | [12. Parts list](docs/12-parts-list.md#after-copying-check-your-schematic-against-the-reference-bom) | optional |
| **Check the PCB against JLCPCB manufacturing limits** | [8. JLCPCB rules](docs/08-jlcpcb-rules.md) | no |
| **Check every trace runs over an unbroken reference plane** | [11. Reference plane](docs/11-reference-plane.md) | no |
| Run any of this through an AI assistant | [9. AI assistants](docs/09-ai-assistants.md) | — |
| Understand a result, an exit code or an error | [10. Results & troubleshooting](docs/10-results-troubleshooting.md) | — |

## Typical flow for a new board

```text
 schematic                                     PCB
 ─────────                                     ───
 1 extract ──► fix fields in KiCad ──┐         8 jlcpcb_rules install
   ▲                                 │           │
   └────────── re-extract ◄──────────┘           ▼
 2 digikey-fetch / digikey-check               KiCad DRC + preflight
 5 inventory-check  (6 for flight builds)      11 refplane check
 4 review           (when datasheet rules are needed)
```

## Five rules that apply everywhere

1. Save in KiCad before running anything: only saved files are read.
2. Run every command from this folder as `python3 -m kicad_checker <command>` (or
   `python3 -m jlcpcb_rules …`, `python3 -m refplane …`). `--help` lists all options.
3. Every report needs a **new** `--output runs/<name>` folder; nothing is overwritten.
4. Keys live in `.env`, stock in `stock/`, reports in `runs/`. None of them go into git.
5. Exit code `0` means "nothing open in what was checked", never "design approved".

## Reference

| File | For |
| --- | --- |
| [README.md](README.md) | Command reference, how each check works |
| [SOURCING.md](SOURCING.md) | Sourcing internals: request schema, filters, matching rules, caches |
| [jlcpcb_rules/README.md](jlcpcb_rules/README.md) | JLCPCB rule engine details and known limits |
| [refplane/README.md](refplane/README.md) | Reference-plane check: algorithm, findings, limits |
| [AGENT_GUIDE.md](AGENT_GUIDE.md), [START_HERE.md](START_HERE.md) | AI-assisted schematic review workflow and source policy |
| [SYMBOL_CHECKS.md](SYMBOL_CHECKS.md), [SCHEMATIC_SHEET_INPUT.md](SCHEMATIC_SHEET_INPUT.md) | Pin-count rules; describing sheet intent |
| [SOURCING_HANDOFF.md](SOURCING_HANDOFF.md), [AGENTS.md](AGENTS.md) | Development status; rules for assistants changing the code |
