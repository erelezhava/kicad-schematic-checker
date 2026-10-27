# 9. Use it with an AI assistant

The tool is deterministic Python: Claude, ChatGPT/Codex or a local Ollama model all get the
same results. The assistant runs the commands, reads the reports and drafts rules; you decide.

[← All guides](../USER_GUIDE.md)

## Setup

Give the assistant access to this folder and your KiCad project. An assistant with file and
shell access runs the commands itself. In a chat-only tool, run the commands yourself and paste
or attach the reports.

## Ready-to-paste prompts

Replace `<project>` with your `.kicad_pro` path.

**Part data check** ([guide 1](01-schematic-check.md), [2](02-digikey-check.md))

> Read USER_GUIDE.md and docs/01-schematic-check.md. Run extract on `<project>` into a new
> runs/ folder, then digikey-fetch and digikey-check. Summarize owner-actions.md and every
> DigiKey `fail` and `needs_review`, grouped by what I must change in KiCad. Do not edit my
> schematic.

**Stock check** ([guide 5](05-inventory-check.md))

> Read docs/05-inventory-check.md. Run inventory-check on `<project>` with
> `--stock JLC=stock/<file>.csv --boards <N> --check-market --no-input` into a new runs/
> folder. Tell me what to buy, which parts are in stock under another C-code, and which
> alternatives need my review.

**Second sources** ([guide 6](06-second-sources.md))

> Read docs/06-second-sources.md. Run inventory-check on `<project>` with
> `--boards <N> --check-market --alternatives-for <R27,C2,…> --no-input`. For each chosen
> line list independent second sources (different manufacturer) with their verdict, and what
> I must still verify in the datasheet.

For the whole BOM, swap `--alternatives-for …` for `--alternatives-even-if-available`.

**Reference planes** ([guide 11](11-reference-plane.md))

> Read docs/11-reference-plane.md. Run `python3 -m refplane info` on `<project>`, show me the
> guessed layer roles, GND/PWR nets and net classes, and wait for my confirmation. Then run
> `python3 -m refplane check` with those answers as flags and `-y`. Group the findings by net
> and severity, give KiCad coordinates for each error, and say which `return_via_missing`
> findings involve my high-speed nets.

**Copy a reference design** ([guide 12](12-parts-list.md))

> Read docs/12-parts-list.md. Run `parts-list <bom.csv> --dry-run` and show me the column mapping
> and how the first rows are read; wait for my OK. Then run it with `--boards <N> --online` into a
> new runs/ folder, and once I've copied the circuit, again with `--against <project>`. List every
> row that is not *same part* / *vendor alternate*, per position, and what I must verify.

**PCB manufacturability** ([guide 8](08-jlcpcb-rules.md))

> Read docs/08-jlcpcb-rules.md. Run `python3 -m jlcpcb_rules install` and `preflight` on
> `<project>`, then `kicad-cli pcb drc --severity-all --format json`. Group DRC errors and
> warnings by rule, say which are paid options, and estimate which are worth fixing.

**Full datasheet review** ([guide 4](04-rules-review.md)): use the opening prompt in
[START_HERE.md](../START_HERE.md). Fill [SCHEMATIC_SHEET_INPUT.md](../SCHEMATIC_SHEET_INPUT.md)
for the sheets in scope first.

## Rules every assistant must follow

- Always pass flags (`--boards`, `--check-market`, `--find-alternatives` or
  `--alternatives-for`, `--no-input`; `-y` for refplane) instead of relying on prompts, and use
  a new `--output` folder each run.
- Never edit your schematic, never write `approved_by_human`, never print `.env`.
- Treat `heuristic`, `possible` and every alternative as a lead for you, not a verdict.
- Source policy: DigiKey for RLC specs, manufacturer datasheets for active parts, LCSC/JLCPCB
  only as procurement codes. Details: [AGENT_GUIDE.md](../AGENT_GUIDE.md).

For assistants **changing the code**: [AGENTS.md](../AGENTS.md) and
[SOURCING_HANDOFF.md](../SOURCING_HANDOFF.md).

**Next:** [10. Results, exit codes, troubleshooting](10-results-troubleshooting.md)
