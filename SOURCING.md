# Component sourcing — all-category catalog search

Search any component category exposed by DigiKey's API, check exact identities at
suppliers, and report whether at least three distinct MPNs meet your supplied catalog
criteria and stock policy. ICs, MOSFETs, connectors, sensors, switches, relays, fuses,
crystals, RLC and other items use the same workflow.
The engine uses standard-library Python and no paid AI service. API accounts,
provider approval, limits and terms still apply. It is a foundation for the future
hardware design assistant; it currently exposes command-line tools and JSON/Markdown
reports, rather than a chat interface.

## Try it

From this checker folder:

```sh
# No credentials, network, cache changes or reports: inspect the planned request.
python3 -m kicad_checker parts-search examples/sourcing/resistor.json --dry-run

# Broad discovery, or explicit generic catalog criteria for an IC/connector.
python3 -m kicad_checker parts-search examples/sourcing/catalog.json --dry-run
python3 -m kicad_checker parts-search examples/sourcing/regulator.json --dry-run
python3 -m kicad_checker parts-search examples/sourcing/connector.json --dry-run

# Offline demonstration using the existing recorded DigiKey response.
# Its stock is old: expect a shortfall, not a claim of current availability.
python3 -m kicad_checker parts-search examples/sourcing/resistor.json \
  --digikey-snapshot examples/sourcing/recorded-resistor.json --output runs/sourcing-demo-001

# Same offline workflow for a recorded IC; old stock remains unresolved.
python3 -m kicad_checker parts-search examples/sourcing/regulator.json \
  --digikey-snapshot examples/sourcing/recorded-regulator.json --output runs/regulator-demo-001

# Fresh search and supplier lookups; uses your own configured API accounts.
python3 -m kicad_checker parts-search examples/sourcing/resistor.json \
  --replace AF0402FR-0710KL --online --output runs/resistor-alternatives-001

# Exact stock/lifecycle view, including parts that are obsolete or out of stock.
# Use the manufacturer's exact catalog spelling. A prefix is never a match.
python3 -m kicad_checker parts-stock AF0402FR-0710KL --manufacturer YAGEO \
  --quantity 100 --min-stock 10000 --online --output runs/resistor-stock-001

# BOM stock from a previously extracted circuit; default budget is 10 unique parts.
python3 -m kicad_checker parts-bom runs/board-001/circuit.json --boards 10 \
  --online --output runs/board-stock-001
```

Omit `--online` to reuse sourcing caches and the old exact-MPN checker cache
(for `parts-stock`/`parts-bom`). Offline calls never load credentials or send HTTP.
Online calls refresh instead of treating the cache as current. Failed requests
retain prior cached observations when available, with the original timestamp and
an explicit warning. A cache read never renews the stock timestamp.

### DigiKey-side narrowing (phase A)

`parts-search` asks DigiKey to do the coarse filtering before paging, so the page budget
is spent on relevant parts. The local screening is unchanged and still checks every
returned part; filters only shrink the result set.

- Category: an explicit numeric `category` is sent as-is. A category name is resolved
  through `<cache>/digikey/categories.json` (fetched with `--online` when missing or
  older than 30 days; offline reruns reuse it). Built-in profiles infer a category
  only when unambiguous: chip-size resistors → Chip Resistor - Surface Mount, inductors
  → Fixed Inductors, capacitors with a `dielectric` → Ceramic Capacitors.
- Stock: `MinimumQuantityAvailable` = max(quantity, min_stock), `InStock`,
  `NormallyStocking` (when required), `ExcludeMarketPlace`.
- Sort: QuantityAvailable descending instead of text relevance.
- If DigiKey answers HTTP 400, the search retries once without SearchOptions,
  MarketPlaceFilter and SortOptions and says so in the warnings. These enum spellings
  were not yet confirmed against a live response.
- The report's `search` object and the "DigiKey filters requested" line show exactly
  what was asked. Parts below the stock threshold are no longer returned, so they
  no longer appear under "Not counted". Search caches made before this change do not
  match the new request keys; rerun with `--online`.

### Parametric value filter (phase B, R/C/L)

Keyword search matches text, so an odd value like 110 Ω also returns 1.1 kΩ and 110 kΩ parts and
can run out of page budget before finding enough 110 Ω parts. When an R/C/L search with a resolved
category falls short, the response's parametric filter list (`FilterOptions.ParametricFilters`,
stored in the cache entry as `parametric_filters`) is searched for the exact value (and the
imperial package), and the search is repeated with `ParameterFilterRequest` pinning them. Results
are added to the keyword results and screened locally as before, so a wrong filter can only lose
candidates. If DigiKey did not offer the value in the first response, or rejects the filter
(HTTP 400), keyword results stand and the report says why (`search.parametric`). Confirmed live
2026-10-25: a 110 Ω 0402 search went from 1 to 8 counted parts (4 manufacturers).

`parts-search` writes `sourcing.json` (complete data) and a compact `sourcing.md`:
summary, a table of counted alternatives, short details for the top 10, one line per
not-counted candidate with its reasons, the variant list, and rejected parts grouped
by failed check. `--full-report` also writes `sourcing-full.md` with every row in
detail (the live 10k 0402 run: 366 KB full vs 22 KB compact). Read the JSON or the
full report when you need every parameter. `parts-stock` writes the
same files with `mode: stock`. `parts-bom` writes `bom-stock.json`, `bom-stock.md`
and a separate report folder for each checked unique manufacturer/MPN. BOM demand
is populated reference count × boards. DNP/exempt references are listed separately;
missing/conflicting identity stays unknown. Assembly wastage is not included. Input
extraction hash is retained; the command does not assert current schematic freshness.
Existing output folders are refused. Exit 0 means the DigiKey target/availability
criterion was met, 2 means shortfall/unknown/review, and 3 means invalid input/tool
failure. Warnings can remain with exit 0: other suppliers may still be unknown.

## Inventory check: BOM vs your stock

`inventory-check` answers "can I build N boards from what we already have?" It reads the
design, groups it into BOM lines, compares them with one or more stock files, and then
(only if you say yes) checks market stock and searches for R/C/L alternatives.

```sh
# Put stock exports in stock/ (git-ignored: company inventory stays local).
# In a terminal it asks: number of boards, check market?, which lines get alternatives?
python3 -m kicad_checker inventory-check ~/projects/board/board.kicad_pro \
  --stock JLC=stock/jlc-all-parts.csv --output runs/board-inventory-001

# Same answers as flags (for Codex/Claude/Ollama, CI or scripts; never prompts):
python3 -m kicad_checker inventory-check runs/board-001/circuit.json \
  --stock JLC=stock/jlc-all-parts.csv --stock Office=stock/office.xlsx \
  --boards 10 --check-market --find-alternatives --output runs/board-inventory-002
```

**Design input:** `.kicad_pro`, root `.kicad_sch` or the project folder (netlist exported
with `kicad-cli`), an exported KiCad XML netlist, or an earlier `circuit.json`. DNP parts,
power symbols, documented exemptions and parts marked *Exclude from BOM/board* are listed
as excluded.

**Stock files:** `.csv` (comma, semicolon or tab) or `.xlsx`, one row per part. The header
row is found automatically, also below title rows. Needed: an MPN and/or LCSC/JLCPCB C-code
column, plus a quantity column. Recognized names include `MFR Part #`, `MPN`,
`Manufacturer Part Number`; `JLCPCB Part #`, `LCSC Part #`, `LCSC`; `Available Qty`
(preferred) > `Total Qty` > `Stock`/`Inventory` > `Qty`/`Quantity`. Optional: `Manufacturer`,
`Description`, `Value`, `Footprint`/`Package`, `Component Type`. Other `…Qty` columns
(JLCPCB, Global Sourcing, Consigned pools) are kept in the JSON. Anything else:
`--column quantity="Pieces" --column mpn="Article"`. For an `.xlsx` with several sheets the
best-matching sheet is used, or `--sheet NAME`. Each `--stock` is a separate source; name it
with `LABEL=FILE`.

**Quantity needed per line:** populated references × boards + spare, where spare =
max(ceil(net × `--spare-percent`), `--spare-min`); defaults 5% and 2.

**Matching rules**

| Situation | Result |
| --- | --- |
| Same MPN (ignoring case, spaces, `- # / .`) | counts |
| Same MPN, different C-code | counts, flagged: JLCPCB assembly uses stock by C-code, so the report says which C-code to put in the LCSC field |
| Same C-code, no MPN on one side | counts, flagged as C-code-only match |
| Same C-code, **different** MPN | conflict, never counts |
| Manufacturer present on both sides and different (after removing Inc/Ltd/…) | counts, flagged |
| Stock row quantity blank/unreadable | `quantity unknown`, never treated as stock |

Rows of the same part within one source are summed (one stock). Sources are not added
together unless `--combine-sources`; when only the sum covers the need the line is
`split across sources`. When the schematic has no LCSC field, the matching stock C-code is
shown as `(stock: C…)` because a JLCPCB assembly BOM needs it.

**Line statuses:** `in stock`, `split across sources`, `insufficient` (shortfall given),
`not in stock`, `quantity unknown`, `no MPN/C-code`. ⚠ marks a counted line with a flag.

**Alternatives already in stock (always, offline):** for open R/C/L lines, other stock rows
are screened by value, package, tolerance (≤ required), voltage/power (≥ required) and
dielectric (C0G replaces any class II; class II needs an equal-or-wider temperature range
and equal-or-smaller ΔC, so X7R→X5R is rejected), parsed from the stock row's Value,
Footprint and Description. `match` means every check passed; `possible` means something
could not be read (often tolerance not declared in the schematic, or AEC-Q200 not stated).
DC-bias derating, ESR and qualification are not compared. They are review leads.

**Original part as baseline (no downgrade):** before alternatives are screened, the exact
DigiKey record of each R/C/L line's own MPN is read. Ratings the schematic does not declare are
taken from it: tolerance, rated voltage, resistor power (the smaller of e.g. "0.063W, 1/16W"),
dielectric, and AEC-Q200 when the original is qualified. Declared schematic values always win.
Every report line shows `requires …` and names what came from the original part. The baseline
is not used when DigiKey's value for the MPN contradicts the schematic Value, or when the MPN
is listed under another manufacturer (shown with ⚠). It feeds the stock screening, the DigiKey
check of in-stock candidates and the DigiKey alternative search alike.

**DigiKey check of in-stock candidates (`--verify-stock-alternatives`):** stock files from
JLC/LCSC do not state qualification (AEC-Q200) and often omit ratings. Each in-stock candidate's
exact MPN is looked up on DigiKey and screened with the same rules as `parts-search` (value,
package, tolerance, voltage/power, dielectric, AEC-Q200 when the original's Description states
it, lifecycle). Result per candidate: `match` (all checks pass on DigiKey data and Active),
`possible` (something unconfirmed, e.g. tolerance not declared in the schematic, AEC-Q200 not
listed, not Active, MPN not on DigiKey, resistor power not declared in the schematic), `rejected` (DigiKey contradicts a requirement; sorted
last and no longer suppresses the DigiKey alternative search). The manufacturer comes from
DigiKey, so same-manufacturer candidates are marked. Online by default whenever the run already
goes online (`--check-market`, `--find-alternatives`, `--alternatives-even-if-available`), else
cache only; `--no-verify-stock-alternatives` turns baselines and this check off; one budget
`--max-verify-lookups` (40) for both. Shares the exact-lookup cache with the market check; a
record fetched in the last 24 h is reused.

**Market check (`--check-market` or "y"):** for missing/short lines with an exact MPN and a
known manufacturer, runs the `parts-stock` lookup for the shortfall quantity (DigiKey,
LCSC, JLCPCB with the line's C-code; each supplier separate). Line result: `available`
(at least one supplier eligible), `stock_issues` (seen but stock, MOQ, freshness or
lifecycle failed; see the per-line report) or `unknown`. `--offline` uses caches only. Manufacturer names
are compared after removing trailing words such as Inc, NV, Technologies, Electronics ("Melexis" =
"Melexis Technologies NV"); if DigiKey lists the MPN under a different manufacturer the report says so.
When the schematic has no LCSC field, the matching stock C-code is used for the JLCPCB lookup.

**Market alternatives (`--find-alternatives` or "a"):** for lines not available on the market
and not covered by a full in-stock `match`, drafts a `parts-search` request from declared
schematic data only (value, footprint size, tolerance, dielectric, voltage/power fields or
Description) and searches DigiKey, excluding the original MPN. A missing tolerance is never
assumed: the line is skipped with the reason. Scope: R/C/L; diodes, transistors and ICs
are listed with "write a request JSON" (see the selection request below).

In a terminal the question is a numbered pick list of those default lines: Enter = none (no
API calls), `a`/`y` = all listed, `e` = every R/C/L line, or numbers and/or references/MPNs
(any BOM line, also lines already in stock). Invalid input is asked again.

**Chosen lines (`--alternatives-for R27,C2`):** references or MPNs, comma-separated,
repeatable, case-insensitive (MPNs compared as in matching). Only those lines are searched,
also when in stock or available on the market; `market_alternatives.purpose` is
`second_source` when in stock, else `replacement`. They also get original-part baselines,
in-stock alternatives and the DigiKey check. Implies `--find-alternatives`. Open lines not
chosen get `market_alternatives.status = not_selected`. The run stops before any API call on an
unknown selector, a DNP/excluded reference, or a combination with
`--alternatives-even-if-available` or `--no-find-alternatives`. A chosen non-R/C/L line is
reported as "R/C/L only".

**Second sources (`--alternatives-even-if-available`):** for flight hardware, searches every
R/C/L line, including lines already in stock or available on the market (implies
`--find-alternatives`). In-stock alternatives are listed for those lines too. Each DigiKey
result says how many *other* manufacturers were found; a candidate from the original part's
manufacturer is marked "same manufacturer (not an independent source)". Uses the search
budget (`--max-alternative-searches`, default 10 lines × `--max-pages` 2). 0 Ω jumpers are
skipped (tolerance does not apply; write a request with max resistance/current). ICs and
other parts stay manual.

**Outputs:** `inventory.md` (summary, BOM vs stock table, alternatives, market results,
next steps), `inventory.json` (all data), `shortfall.csv` (what to buy, with stock
C-codes), plus `market/line-NNN/` and `alternatives/line-NNN/` reports. Exit code 0 only
when every line is in stock without flags; otherwise 2.

Limits: stock file dates are not verified (export fresh before an order); no attrition table
per package (one spare rule for all lines); the tool never edits the schematic or orders.

## Selection request, schema 1

See `examples/sourcing/` for RLC, diode, generic discovery, regulator and connector requests.
Unknown field names are errors, so misspelled constraints cannot disappear silently.

| Field | Meaning |
| --- | --- |
| `kind` | Any nonempty label, e.g. `mosfet`, `microcontroller`, `connector`, `sensor`, `component`; `resistor/capacitor/inductor/diode` retain built-in profiles |
| `category` | Optional exact category name or positive numeric DigiKey CategoryId; matches a node in the returned category path. Required to count generic catalog matches. Numeric IDs are sent to DigiKey directly; names are resolved to an ID through a cached DigiKey category list (unknown or ambiguous names are screened locally only, with a warning) |
| `value` | Required for RLC; `10k`, `100nF`, `10uH`; exact nominal equality after unit conversion |
| `package` | Required for RLC/diode profiles, optional for other items; when supplied it is always checked. Numeric chip sizes are imperial (`0402` = 1005 metric); other names compare exactly except case/whitespace, and any comma-separated name counts (`TO-236-3, SC-59, SOT-23-3` matches `SOT-23-3`; `SOT-23` without pin count is unresolved). For non-chip packages, a `Supplier Device Package` that adds outline information (e.g. `6-WSON (2x2)`) leaves the footprint unresolved unless `supplier_device_package` is given |
| `supplier_device_package` | Optional DigiKey Supplier Device Package, e.g. `6-WSON (2x2)`. Same name → match; different pin count or body size → mismatch; same outline under another name → unresolved (check the land pattern) |
| `tolerance_percent` | Required for RLC; maximum allowed percentage, so 0.5% satisfies a 1% requirement |
| `automotive` | Default false; built-in automotive defaults are AEC-Q200 for RLC and AEC-Q101 for diodes. Other kinds require an explicit `qualification`; the word Automotive alone is insufficient |
| `qualification` | Explicit required catalog qualification text, e.g. `AEC-Q100`; no automatic guess based on an arbitrary component label |
| `dielectric` | Optional exact capacitor EIA code; NP0/NPO normalize to C0G; X5R does not substitute for X7R |
| `constraints` | Extra catalog parameters, each with `parameter`, `op`, `value`, optional `unit`; names must match DigiKey's parameter labels |
| `quantity` | Requested order quantity; default 100 |
| `min_stock` | Minimum acceptable reported stock; default 1000; must also cover `quantity` |
| `min_alternatives` | Default 3 distinct alternatives. MPNs from the same manufacturer with an identical catalog specification (reel/tape suffixes) count once and are marked `variant_of`; variants of an excluded original are `excluded_variant` |
| `min_manufacturers` | Default 2 (1 if `min_alternatives` is 1); `target_met` also needs this many different manufacturers among counted alternatives. Set 1 explicitly for single-source parts |
| `normally_stocking` | Default true; requires DigiKey's explicit `NormallyStocking=true` |
| `max_age_hours` | Default 24; stale, future, invalid and timezone-less timestamps are unresolved |
| `exclude_mpns` | Exact original MPNs to exclude from the alternative count; `--replace MPN` appends one |
| `keywords` | Optional discovery query override; it does not relax any constraint |
| `supplier_codes` | Optional explicit `{"manufacturer":"YAGEO","mpn":"EXACT","jlcpcb":"C123"}` mappings |

Numeric constraints use `op: eq/min/max`, a finite `value`, and a supported unit
(e.g. W, V, A, Ohm, Hz, °C). Use `unit: ""` for a dimensionless numeric count such
as `Number of Positions`; omit `unit` for text `eq` constraints. Scalar comparisons
preserve signs and convert units (`500mA` = `0.5A`). Every constraint
appears in the report with its actual value and match/mismatch/unknown result.
Catalog values with test conditions, ranges or suffixes are compared only when the
request states what it needs; the condition is never discarded to manufacture a match:

- **Test conditions** (`28mOhm @ 5.8A, 10V` under `Rds On (Max) @ Id, Vgs`): add
  `"at": {"Vgs": {"value": 4.5, "unit": "V"}}`. Condition names come from the label after
  `@`. Every listed condition must equal the tested one (units convert), otherwise the
  check is unknown with the reason (`tested at Vgs=10V, required at 4.5V`). Unlisted
  conditions (Id here) are shown but not compared. Without `at`, the check is unknown.
- **Ranges** (`-55°C ~ 150°C (TJ)`): `"op": "covers", "value": [-40, 125], "unit": "°C"`
  passes when the catalog range contains the requested one. Optional `"qualifier": "TJ"`
  requires that suffix. eq/min/max on a range stay unknown.
- **Suffixes** (`5.8A (Ta), 30A (Tc)`): `"qualifier": "Ta"` picks one value; without it
  the worst case is used (smallest for min, largest for max).

See `examples/sourcing/mosfet.json` and `diode.json`. Power fields with both rounded decimals
and fractions use the smaller stated value for minimum comparisons.

The built-in RLC profiles still require supported leaf categories: single fixed chip
or through-hole resistors, selected capacitor types, and fixed inductors. Networks,
arrays and ferrite beads need their own generic requests with explicit categories
and constraints. An explicit category cannot bypass a built-in RLC type check.

For every other type, `kind` is a human label used in discovery keywords, not an
inferred electrical classification. The explicit `category` and named `constraints`
define the checks. Package is optional because many connectors, tools and other
items have no Package / Case parameter. If you supply it and the listing omits it,
that check stays unresolved.

For discovery alone, `{ "kind": "component", "keywords": "temperature sensor I2C" }`
is sufficient. Results include descriptions, the category tree, all returned catalog
parameters and datasheet URLs. Add an exact category and the parameters that matter
to turn that discovery into a screened request. A generic search without a category
shows candidates and stock but does not count them toward the target.

The report's legacy `qualified_candidates` field means **stocked catalog matches to
supplied criteria**, not approved electrical substitutes. `target_basis` makes that
scope explicit, and `discovered_candidates` reports the returned unique identities.
Diodes now follow this same catalog-counting policy: catalog matches can count,
while every diode and every other component retains `engineering_status: requires_review`.
Existing RLC request files remain supported under schema 1.

## What counts as a stocked candidate

All supplied electrical/package/qualification catalog constraints must match.
Then at least one **DigiKey packaging offer** must have:

- A fresh timestamp and stock ≥ both order demand and `min_stock`.
- A known MOQ no greater than the requested order; known distribution limits must permit the order.
- Active lifecycle, no discontinued/end-of-life flag, and confirmed normally stocked status when required.
- Explicit non-marketplace seller status.

Three packaging options for one MPN count as one candidate. Quantities are never
added across packages or suppliers. The newest observation for a supplier/code wins;
conflicting observations at the same timestamp stay unresolved. Excluded originals,
stale stock, missing data and contradictions remain visible. A shortfall explicitly
reports how many candidates are missing. Finding fewer than three within a bounded
search does not prove that fewer than three exist in the supplier's whole catalog.

All candidates retain `engineering_status: requires_review`, even when the catalog
target is met. Review resistor temperature coefficient, working voltage, pulse and
power derating; capacitor DC bias, ESR, temperature, dimensions and effective C;
inductor saturation, DCR, SRF, shielding and rated-current conditions; diode technology,
polarity/pin mapping, thermal limits, forward/reverse behavior and switching conditions
as applicable. Package-name equality does not prove footprint/pad compatibility.

## Suppliers and access

Credentials use the existing loader: environment variables, then this repo's `.env`,
then `~/.config/kicad_checker/credentials.env`. Keep values local; `.env.example`
contains the names only. No existing `.env` has been changed by this implementation.

| Supplier | Implementation | Credentials |
| --- | --- | --- |
| DigiKey | Product Information v4 keyword search, paginated; exact-MPN stock lookup; local spec screening | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` |
| LCSC | Official signed GET keyword lookup per MPN, followed by exact manufacturer/MPN comparison | `LCSC_APP_KEY`, `LCSC_APP_SECRET` |
| JLCPCB | Official signed detail lookup by C-number and exact returned MPN; inherited endpoint contract from the older agent | `JLCPCB_APP_ID`, `JLCPCB_ACCESS_KEY`, `JLCPCB_SECRET_KEY` |

DigiKey uses US/en/USD, matching the existing checker. Source/stock timestamps and
provider diagnostics are retained. LCSC and JLCPCB inventory remain separate: an
LCSC catalog code alone does not establish JLCPCB assembly availability.

LCSC results can supply C-number lookup leads for JLCPCB. Explicit mappings and
saved schematic LCSC/JLCPCB fields are also supported. JLCPCB must return the same
code and exact MPN. Its inherited detail schema does not confirm manufacturer or
MOQ; these stay flagged for review, so its reported stock is not promoted to fully
verified procurement availability. `libraryType` is preserved without guessing
Basic/Preferred/Extended enum semantics. No first-page JLCPCB catalog search is used.

DigiKey defaults to at most 3 pages × 50 records (`--max-pages`, 1–20). Exact LCSC
lookup checks at most 100 records, reporting pagination limits. Each supplier gets
at most 10 part lookups (`--max-stock-lookups`, 1–50). BOM lookup budget is
`--max-parts` (default 10). Budgets and incomplete coverage are visible. Supplier
failures leave other results intact. LCSC/JLCPCB use one bounded retry for transient
transport errors, HTTP 429 and 5xx; errors redact server bodies. No ordering endpoint
or internal webshop/scraping fallback is exposed.

Official references checked 2026-10-24:

- [DigiKey keyword search](https://developer.digikey.com/products/product-information-v4/productsearch/keywordsearch)
- [LCSC API access](https://www.lcsc.com/agent/), [authentication](https://www.lcsc.com/agent/authentication?urlKey=authentication), [keyword response schema](https://www.lcsc.com/agent/docs?id=keywordSearch)
- [JLCPCB API access](https://jlcpcb.com/help/article/jlcpcb-online-api-available-now)

These references establish documented contracts, not successful live testing.
The new integrations have only been exercised with offline fixtures/mocks so far.

## Interchange and caches

A DigiKey snapshot is `{"schema_version":1,"records":[{"observed_at":"2026-10-07T00:00:00+00:00","product":{...}}]}`.
`product` is a DigiKey Product Information v4 product object. The included
`recorded-resistor.json` is derived from the existing test response captured on
2026-10-07; only its capture date is known, so the example retains a date-only value
that cannot pass freshness validation. `--digikey-snapshot` never refreshes
its timestamp. Snapshots and manual imports are user-supplied observations; the tool
validates structure, not publisher authenticity.

`--stock-import FILE` accepts `{"schema_version":1,"offers":[...]}`. Each offer
needs `supplier` (`digikey/lcsc/jlcpcb`), `manufacturer`, exact `mpn`, `url` and
`observed_at` with timezone. Useful optional fields are `code`, `stock`, `moq`,
`order_multiple`, `normally_stocking`, `lifecycle`, `discontinued`, `end_of_life`,
`identity_review`, `marketplace`, `max_order`, `library_type`, `origin`.
Unknown values are null or absent, never fabricated zeroes. Manufacturer spelling
must agree with DigiKey to join automatically; supplier naming differences stay
unresolved. Import files are not engineering evidence.

`cache/sourcing/digikey/` stores bounded search snapshots separately from the
existing `cache/digikey/` evidence cache. `cache/sourcing/lcsc/` and `jlcpcb/`
store allowlisted normalized stock observations. Reports are immutable per output
folder; caches retain the latest lookup, not a historical stock database. There is
no forecast of long-term stocking reliability in this version.
