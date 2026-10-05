# Input for a schematic-sheet review

Use this Markdown form to tell a reviewing agent what each KiCad schematic sheet is **intended** to do. Put a copy in the project's `Docs/` or `docs/` folder (for example, `Docs/schematic-review-input.md`). A sheet may contain several functional blocks; describe each block separately when their requirements differ. This is a human-readable input, not a file parsed by the v0.1 checker.

The agent extracts references, values, pins, and nets from KiCad. You do **not** need to transcribe the schematic. This form supplies intent, operating conditions, review scope, and known exceptions that connectivity alone cannot reveal.

Use the extracted `sheet_path` as the sheet identifier (`/` for the root sheet). A displayed page number or title may change and is only a reading aid. If a sheet is reused in a hierarchy, identify the particular instance path. If you do not know the path yet, use its title and filename; the reviewer should resolve the path after extraction.

## Project information (once per project)

| Field | What to provide |
| --- | --- |
| Project and revision | KiCad project path/name, design revision or commit, and date if known. |
| Assembly variant | Which population/variant is being checked. Write `default` if there is only one; write `unknown` if undecided. |
| Operating envelope | Input supply ranges, ambient/board temperature range, and operating modes that apply to the whole design. Unknown is acceptable. |
| Review scope | Which sheets or blocks are `review_now`, `later`, or `out_of_scope`, with a short reason for exclusions. |
| Documents | Paths to relevant files in `Docs/` or `docs/`; give document revision when known. The reviewer will verify the exact PDF pages and record hashes. |

## For each sheet or functional block

| Field | What to provide |
| --- | --- |
| Identity | `sheet_path`, visible sheet title, and block name if needed. |
| Scope | `review_now`, `later`, or `out_of_scope`. `later` means keep it on the review list, not assume it passes. |
| Intended function | One or two sentences describing what this part of the circuit must do. |
| Boundaries | External connectors, incoming supplies/signals, outgoing supplies/signals, and the other sheets or devices they connect to. Use net names or references when helpful. |
| Conditions | Relevant voltage/current/load ranges, temperature, startup/shutdown states, modes, and fault cases. State `unknown` where not yet specified. |
| Requirements | Concrete checks and acceptance limits, with a source document/page/section when available. Say whether a requirement is a firm limit, recommendation, or design goal. |
| Special parts | Parts whose **specific properties** matter: e.g. LDO output capacitor effective capacitance/ESR, a current-sense resistor tolerance/power, a precision reference, or protection components. Ordinary resistors need no individual datasheet unless a property beyond the schematic value matters. |
| Intentional exceptions | DNP parts, optional circuits, allowed open pins, special net ties, or a documented deviation from a reference design. Give the reason; an exception is not an automatic pass. |
| Unknowns | Questions the reviewer should leave unresolved and ask about. Never fill them by guessing. |

Use the following copyable entry. Keep only relevant lines, but do not hide unknown operating conditions.

```md
## Sheet: <title> — block: <name or whole sheet>

- Sheet path: <from circuit.json; root is />
- Scope: review_now | later | out_of_scope
- Intended function: <what should happen>
- Boundaries: <inputs/sources and outputs/loads; connector or net names if known>
- Conditions: <supply, load, temperature, mode, startup/fault cases; unknown if absent>
- Requirements:
  - <required behavior or limit> — <PDF filename, revision, page/section; or "user design requirement">
- Special parts: <reference, exact ordering code if known, property that matters>
- Intentional exceptions: <or none known>
- Unknowns: <or none known>
```

For example, a power-supply block on the root sheet `/` could say “produce the required regulated supply,” identify its regulator and capacitors, cite the selected regulator's PDF for capacitor requirements, and leave unspecified load, temperature, and effective-capacitance evidence as `unknown` until supplied. The reviewer should derive actual pins and nets from KiCad rather than accept this description as connectivity proof.

The agent should turn each stated requirement into a traceable rule or manual review item, verify source-backed limits against the original document, and report missing inputs as `needs_review`. A heuristic observation can prompt a question but cannot by itself close a requirement.
