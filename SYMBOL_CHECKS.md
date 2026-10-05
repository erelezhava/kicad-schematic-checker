# Symbol checks: pin count

The first symbol check compares a component's **placed symbol pin count** against a reviewed datasheet requirement for the selected manufacturer, exact MPN, and package. A count match checks one property of the symbol; pin names, functions, and footprint pad mapping still need separate review.

## How pins are counted

The reader uses KiCad 10 XML netlist `components/comp/units/unit/pins`, rather than counting connected net nodes. This includes unconnected and hidden pins exported with the placed units. All placed units of one component are combined. Shared or stacked pins with the same number count as one terminal. Alphanumeric pin numbers are supported.

The library pin list is also retained for context, but it cannot prove that every unit was placed. An old netlist or saved circuit file without a complete placed-unit pin inventory stays unresolved; re-extract it with KiCad 10. An inconsistency between connected pins and the exported pin inventory also stays unresolved.

Use the selected package's pin table or drawing, including NC pins, supply pins, and numbered exposed pads. A package described as “8-pin” may have an additional numbered exposed pad; explicitly record the counting convention. Do not count graphical lines, only connected pins, or pins from one unit of a multi-unit symbol.

## Input requirement

Add a `symbol_pin_count` rule to the normal rules file. Read the original package pin diagram/table first, record its document revision and page, and pin the source file's hash when available. Keep the rule `draft` until its applicability and interpretation are reviewed.

Follow the component source policy in AGENT_GUIDE.md: manufacturer datasheets/official product pages for active parts; DigiKey's exact-part specifications for RLC passives when they explicitly establish the needed count/package, with manufacturer documents for further detail. LCSC/JLCPCB listings do not establish a pin-count requirement. A generic two-terminal assumption without supporting evidence remains a review lead.

Required fields beyond the normal rule fields:

| Field | Meaning |
| --- | --- |
| `manufacturer` | Manufacturer name; must match the declared schematic manufacturer (case-insensitive). |
| `part_number` | Exact MPN; must match the schematic's resolved MPN without conflicting declarations. |
| `package` | Selected manufacturer package, confirmed by the target binding. |
| `expected_count` | Positive integer: number of unique numbered terminals expected across placed units. |
| `pin_count_convention` | State whether numbered exposed pads are included and how NC/shared pins are counted. |
| `expected_pin_numbers` | Optional list of unique strings, with exactly `expected_count` entries. Enables an exact pin-number set comparison too. |

The target's normal evidence binding must include matching `package` (compared ignoring case and spacing, so `soic-8` matches `SOIC-8`; a different package such as `SOIC-14` stays `needs_review`), plus the existing `reference`, `applicability`, `rationale`, and `reviewed_by`. Package correspondence to the MPN is a reviewed declaration; this version does not infer it from a footprint name or independently verify the package ordering code.

Results:

| Situation | Result |
| --- | --- |
| Reviewed requirement says 8; complete symbol inventory has 8 | `pass` for the count check |
| Reviewed requirement says 8; complete symbol inventory has 7 or 9 | `fail` |
| Count matches but an optional expected pin-number set differs | `fail`, showing missing and unexpected numbers |
| Manufacturer/MPN/package binding differs or is missing | `needs_review` |
| Source file changed/disappeared, or full symbol inventory is missing | `needs_review` |
| Rule has not been reviewed | `not_checked` (draft) |

Extraction gathers pin inventories automatically. Datasheet comparisons run for the supplied `symbol_pin_count` rules; the tool does not automatically read pin diagrams or discover omitted rules. If the needed source is absent, retain a `manual` requirement to obtain and verify the package pin table rather than inventing an expected count. This does not require individual manufacturer documents for ordinary passive components unless their package/pin definition needs verification.

## Runnable synthetic example

`examples/symbol-pins` contains a fictional eight-pin component split across two units. Only two pins appear in nets; all eight appear in the symbol inventory. This example is not manufacturer evidence for a real component.

```sh
python3 -m kicad_checker import-netlist examples/symbol-pins/netlist.xml --output runs/symbol-pins-circuit-001
python3 -m kicad_checker review runs/symbol-pins-circuit-001/circuit.json --snapshot --rules examples/symbol-pins/rules.json --evidence examples/symbol-pins/evidence.json --output runs/symbol-pins-review-001
```

See its `rules.json` and `evidence.json` for the complete input format. Use new output directories for reruns.
