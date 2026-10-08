# What we can use from kicad-happy

Reviewed [aklofas/kicad-happy](https://github.com/aklofas/kicad-happy) on 2026-09-24. Its [workflow explanation](https://github.com/aklofas/kicad-happy/blob/main/how-it-works.md) describes deterministic KiCad parsing, circuit detectors, and datasheet cross-checks. Its [datasheet extraction guide](https://github.com/aklofas/kicad-happy/blob/main/datasheet-extraction.md) describes page-anchored facts with confidence levels and source-PDF hash checks. The project is [MIT licensed](https://github.com/aklofas/kicad-happy/blob/main/LICENSE).

## Useful ideas

1. Keep circuit extraction separate from engineering conclusions. This checker already follows that pattern using KiCad's XML netlist and explicit rule/evidence files.
2. Track every fact's exact document revision, page, conditions, confidence, and freshness. We added optional `source.path` plus `source.sha256` checks to rules and facts. A changed or missing local source now makes the affected result `needs_review`.
3. Enrich the circuit inventory with pattern detectors such as power trees, I2C pull-ups, termination, and capacitor networks. These are useful candidates for a later version, provided each detector reports assumptions and can be checked against KiCad's exported nets.
4. Distinguish deterministic connectivity observations from heuristics and manufacturer-backed specifications. A heuristic finding should be a lead for review, not an automatic pass/fail verdict.

## How the projects fit together

`kicad-happy` could serve as an optional second analyzer when installed locally. Its output would supply candidate facts to our rule/evidence review, alongside KiCad CLI extraction. We have not installed or run its scripts and have not copied its code. Before importing its findings, test its pin/net mapping and DNP handling on this earth-sensor project and compare them with the KiCad XML netlist. An upstream `datasheet-backed` label alone would not meet our evidence gate unless it includes the exact part, source page, conditions, and current source file.

Our immediate priority remains the LDO capacitor question. Circuit pattern detection cannot establish C2's dielectric, effective capacitance, or ESR without capacitor manufacturer data and operating conditions. Those checks remain unresolved.
