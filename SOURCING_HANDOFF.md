# Handoff: component sourcing for the hardware design assistant

Updated: 2026-10-25 (Asia/Tbilisi).

## User's objective

Grow this KiCad checker into a hardware design assistant. The immediate problem is
BOM churn caused by unavailable/obsolete components. Support discovery across all
DigiKey categories; check stock separately at DigiKey, LCSC
and JLCPCB; find at least three normally stocked alternatives to explicit criteria
or an existing MPN. Keep the system understandable and usable by Claude, Codex,
local models or a person without relying on one AI provider.

## Read first

`AGENTS.md` → `README.md` → `SOURCING.md`. The separate schematic-review source
policy is in `AGENT_GUIDE.md`. Tests and examples are executable specifications.
The implementation is Python 3.10+ with **no new package dependencies**.

## Delivered in this iteration

- `parts-search REQUEST.json`: all-category search, built-in RLC/diode profiles, bounded DigiKey keyword
  discovery, exact local catalog screening, supplier stock enrichment, explicit
  target count/shortfall, original-MPN exclusion with `--replace`.
- `parts-stock MPN --manufacturer NAME`: exact identity stock/lifecycle report.
- `parts-bom CIRCUIT.json`: stock audit using existing checker extraction, grouped
  manufacturer/MPN, populated quantity × board count; keeps DNP, unknown identity
  and budget-exhausted rows visible.
- Official API adapters for DigiKey and LCSC, plus JLCPCB exact-C-code detail lookup.
  LCSC C-codes provide JLCPCB lookup leads; returned code and MPN must match.
- Offline caches, dated import/snapshot interchange, safe dry run, per-supplier
  request budgets, partial-failure diagnostics and immutable report directories.
- RLC constraints include nominal value, package, max tolerance, AEC-Q200, capacitor
  dielectric and explicit additional numeric/text parameters. All types can count
  as stocked catalog matches to supplied criteria; every type still requires
  manufacturer-backed engineering review before substitution.
- Arbitrary nonempty `kind` labels, explicit category names/IDs, optional generic
  packages, explicit generic qualification, dimensionless count constraints, signed
  scalar conversions and complete returned catalog descriptions/parameters.
  Generic searches without category are discovery-only and do not count toward
  the screened target. Numeric category filters reach the API and use isolated caches.
- Latest observations win for the same supplier/code; equal-time conflicts stay
  unresolved. Packaging and supplier stocks are never summed.
- `examples/sourcing/`: seven request examples and two honestly dated existing
  DigiKey response. Its date-only stock observation intentionally fails freshness.

## 2026-10-25 fixes (Claude review)

- Distinctness: same manufacturer + identical catalog specification (`spec_signature`) counts
  once (`variant_of`), and variants of an excluded original become `excluded_variant`. Before
  this, three reel/grade suffixes of one TI LDO reported `target_met 3/3`.
- Supply diversity: new `min_manufacturers` (default 2, capped by `min_alternatives`);
  reports add `distinct_manufacturers` and `manufacturer_shortfall`.
- Footprint: new `supplier_device_package`; for non-chip packages, a Supplier Device Package
  with extra outline information is unresolved unless requested. Pin-count/body-size
  differences are mismatches. Before this, a 3x3 part matched a 2x2 request through
  `Package / Case` alone. `examples/sourcing/regulator.json` now states `6-WSON (2x2)`.
- Tests: 155 → 159 (4 new; 2 existing tests updated to use distinct manufacturers and a
  consistent synthetic diode package).
- First live DigiKey run (runs/live-resistor-001): target_met, 33 counted / 10 manufacturers,
  16 variants collapsed. NRC04J103TRF is listed by DigiKey as ±1% but its NIC code suggests ±5%;
  catalog data errors are possible, so engineering review stays mandatory.
- Compact `sourcing.md` by default (366 KB → 22 KB on the live run); `--full-report` keeps the
  old full listing in `sourcing-full.md`. JSON is unchanged. Tests: 160.
- Server-side DigiKey filters, phase A (see SOURCING.md): category name→ID via cached
  `GET /products/v4/search/categories`, MinimumQuantityAvailable, InStock/NormallyStocking,
  ExcludeMarketPlace, sort by QuantityAvailable desc; HTTP 400 → one retry without the
  unverified options + warning. No StatusFilter (status IDs unknown; lifecycle is checked locally).
  `tests/test_search_filters.py` (6 tests, mocked). Tests: 166.
- Live 2026-10-25 (runs/live-resistor-002, live-regulator-001): DigiKey accepted InStock,
  NormallyStocking, ExcludeMarketPlace and the QuantityAvailable sort (no HTTP 400). Resistor
  rejects fell 80 → 47 without the category filter. Category resolution failed because the live
  tree nests under `Children` (product records use `ChildCategories`); fixed, and verified offline
  against the real cached tree: Chip Resistor - Surface Mount = 52, LDO = 699, Fixed Inductors = 71,
  Ceramic Capacitors = 60, Headers, Male Pins = 314. Unknown names now suggest close matches
  (e.g. MOSFET: "Single FETs, MOSFETs" (278)). The category filter itself still needs one live run.
- 2026-10-25 scope (user): realistic substitution focus is R/C/L, diodes and transistors;
  IC/regulator/MCU search stays discovery-only.
- Discretes: `evaluate_numeric` handles test conditions (`at`), ranges (`op: covers`) and
  Ta/Tc suffixes (`qualifier`, else worst case); package matching accepts any comma-separated
  DigiKey name. `examples/sourcing/mosfet.json` (category 278), `diode.json` extended.
  `tests/test_discretes.py` (synthetic strings in DigiKey format). Tests: 173. Not yet live.
- Next, in order: (1) live check of the phase A category filter (resistor-003, regulator-002)
  plus a live MOSFET and diode run; (2) built-in mosfet/bjt/zener/TVS profiles (category
  inference + polarity/type checks); (3) baseline-MPN → draft requirements for RLC and
  discretes; later: phase B parametric value-ID filters, manufacturer aliases before
  LCSC/JLCPCB live tests.

## 2026-10-25: inventory-check (Claude)

User request: compare a KiCad project's parts with company stock (JLCPCB inventory export or
any CSV), ask the number of boards, then optionally check market stock for missing/short
parts and find alternatives (R/C/L first, as before).

- New files: `kicad_checker/sourcing/stockfile.py` (CSV/XLSX reader, header/column detection,
  stdlib-only XLSX), `inventory.py` (pure BOM grouping, matching, spares, R/C/L spec parsing,
  in-stock alternatives, request drafting), `inventory_cli.py` (prompts/flags, market check via
  `parts-stock`, DigiKey alternatives via `parts-search`, reports). `cli.py` registers
  `inventory-check`. `tests/test_inventory.py`: 21 offline tests (synthetic JLC-format CSV,
  synthetic XLSX, mocked supplier runs, simulated prompts). Tests: 173 → 194.
- Real-data check (offline, not committed): user's JLCPCB "All_Parts" sheet (113 rows; columns
  MFR Part #, JLCPCB Part #, Total Qty + three pool columns, Value, Footprint, Description; no
  manufacturer column) against runs/earth-sensor-extract-v06/circuit.json, 5 boards:
  15 lines, 10 in stock, 1 insufficient (MLX90640: 0 in stock), 4 not in stock; ERJ-2RKF6200X
  matched ERJ2RKF6200X; H1–H4 excluded via exclude_from_bom; C2/C37 GCM21BR71E225KA73L found
  CL21B225KAFVPNE in stock as a `possible` alternative (AEC-Q200 not stated). 11 lines have no
  LCSC field but match stock C-codes (reported as `(stock: C…)`).
- Decisions (user): spare = configurable % + minimum (default 5%, ≥2 per line); prompts in a
  terminal with the same answers as flags for AI agents. Total Qty is used by default for the
  JLC sheet (JLCPCB + Global Sourcing + Consigned pools); `Available Qty` would win if present.
- Market availability here accepts any supplier (DigiKey, LCSC or JLCPCB eligible), unlike
  `parts-bom` where DigiKey decides the row status (open item 6 above, addressed for this tool).
- **Not run live.** Market checks and alternative searches were exercised offline/mocked.
  First live run should be the user's: `--check-market --find-alternatives` on a real project.
- **First live run (user, runs/inv-earth-live-001, DigiKey only; LCSC/JLCPCB keys not configured):**
  GCM21BR71E225KA73L, LTC4331HUFD#PBF, LP591233MDRVREP, SBSPP1000472MXT → DigiKey available
  (cut tape eligible; T&R offers correctly fail on MOQ). MLX90640ESF-BAA-000-TU → silently
  `unknown`: schematic "Melexis" ≠ DigiKey "Melexis Technologies NV" (open item 5). **Fixed:**
  `providers.manufacturer_key()` removes trailing legal/descriptive words (Inc, NV, Technologies,
  Electronics, …) plus a small reviewed alias table (TI, ST, ADI, onsemi, Vishay brands);
  `identity()` uses it everywhere (search grouping, LCSC, stock). The MPN stays exact.
  `stock_report` now warns when DigiKey lists the MPN under another manufacturer instead of
  returning a silent unknown. Verified against the recorded MLX90640 DigiKey fixture (1235 pcs).
  No alternative search ran: the only R/C/L open line was available on the market (as designed);
  the report now lists such lines as "Not searched (…): available on the market".
  The market check now uses the matching stock C-code as the JLCPCB lookup lead when the schematic
  has no LCSC field. Tests: 196.
- `--alternatives-even-if-available` (user request, second sources for flight hardware): searches
  all R/C/L lines (purpose `second_source` for lines in stock or on the market, `replacement` for
  open lines); in-stock alternatives listed for every R/C/L line; DigiKey results report
  `other_manufacturers` and mark same-manufacturer candidates. 0 Ω jumpers skipped with a reason.
  Offline run on earth sensor: 9 searches drafted (7 second-source, 1 replacement, 0 Ω and ICs
  skipped). Not run live yet. Tests: 198.
- **Live second-source run (runs/inv-earth-live-003):** capacitors work: C1 4.7 µF 27 counted / 5 other
  manufacturers, C2 2.2 µF 17/6, C5 1 µF 24/5, C98 10 µF 8/3 (TDK CGA, KEMET …AUTO, Samsung, Taiyo
  Yuden; same-manufacturer Murata variants marked). Category filter 60/52 confirmed accepted live.
  **Failures:** every resistor search found 0 parts and C44 100 nF found 1. Cause: keyword value
  spelling. DigiKey writes "10k"/"620" and "0.1 µF"; the drafted requests said "10kOhm" and "100nF".
  **Fixed:** `inventory.catalog_values()` produces DigiKey spellings (R: 10k/620/2.7k/100m; C: pF
  below 10 nF, µF from 10 nF; L: nH/µH/mH), all parsing to the same exact value; one retry with
  the alternate spelling on a shortfall, keeping the better result (`value_spellings_tried`,
  `discovered` in the report). Tests: 200. Needs one live rerun to confirm resistors.
- Live run inv-earth-live-004 confirmed the spelling fix: R1 620 Ω 6 counted / 3 other mfrs,
  R4 2.7k 12/6, R6 10k 37/7, C44 0.1 µF 33/7; R27 110 Ω 1/1 (keyword "110" is fuzzy: 100 hits,
  most other values; honest shortfall).
- **DigiKey verification of in-stock candidates** (user: "JLC never mentions grade; DigiKey has the
  best system"): `inventory.verification_request()` + `verify_candidate()` screen the exact DigiKey
  record of each in-stock candidate with `requirements.screen()`; undeclared tolerance = placeholder
  limit, reported as not compared, never a full match; lifecycle must be Active for `match`;
  DigiKey-rejected candidates sort last and re-open the alternative search. CLI
  `--verify-stock-alternatives` (auto-online with other online flags), `--max-verify-lookups` 30.
  Tests hermetic now (CACHE_ROOT and credentials patched in the inventory tests). Tests: 203.
  Live (runs/inv-earth-live-005): 5 lookups; CL21B225KAFVPNE (Samsung, AEC-Q200), GCM188R71C105KA64D,
  GCM155R71H104KE02J (same manufacturer, marked), CRCW040210K0FKED (AEC-Q200) → match;
  GRM21BZ71E106KE15L → possible (commercial GRM, no AEC-Q200). Follow-up fix: undeclared resistor
  power is now reported (with DigiKey's rating) and blocks a full match, like undeclared tolerance.
  Next idea: use the original part's DigiKey record as the no-downgrade baseline when the schematic
  omits a rating (handoff item 3). Tests: 204.
- **Built (user request): original-part baseline.** `inventory.baseline_from_digikey()` reads the line's
  own MPN record (manufacturer must match; value conflict → not used, reported); `schematic_spec()`
  fills only undeclared tolerance/voltage/resistor power/dielectric/AEC-Q200 and records
  `provenance`. Used by stock screening, DigiKey verification of in-stock candidates and the drafted
  parts-search request. CLI `DigiKeyRecords`: one budget (`--max-verify-lookups` 40), memo per run,
  cache reuse within 24 h. Report: `requires …` per line with sources; ⚠ when no baseline.
  Also fixed Decimal exponent formatting ("1E+1%"). Tests: 207. Live (inv-earth-live-006): all 10
  R/C/L lines got baselines; R6 power from original → CRCW040210K0FKED match.
- **Phase B parametric value filter (user: fix R27 110 Ω shortfall).** `providers.parametric_filters()`
  stores `FilterOptions.ParametricFilters` per cached page (lenient field names: ParameterId/Id,
  ParameterName, FilterValues[ValueId/Id, ValueName, ProductCount]); `requirements.parametric_request()`
  picks the exact value (+ imperial package); `cli.parametric_pass()` reruns a short keyword
  ("resistor 0402") with `ParameterFilterRequest` only when the keyword pass falls short; results are
  merged and screened locally; 400/absent value → keyword results + reason. Request shape confirmed
  from a DigiKey forum post; response field names confirmed live (below). Old search caches have no `parametric_filters` → rerun with --online.
  `tests/test_parametric.py` (4). Tests: 211.
  **Confirmed live (runs/inv-earth-live-007):** response field names are right (ParameterId,
  ParameterName, FilterValues[ValueId, ValueName]); R27 110 Ω went from 1 counted / 1 manufacturer to
  8 counted / 4 manufacturers (KOA RK73H/RK73G, Vishay Dale CRCW…HP/RCS/TNPW, YAGEO AC…-7W, Panasonic
  ERJ-U02/PA2) after "+39 records" from the parametric pass. Other lines unchanged (keyword pass met
  the target, no extra calls).
- Next: (1) live run with LCSC/JLCPCB credentials; (2) read the raw JLCPCB
  `Parts Inventory on JLCPCB.xlsx` directly (XLSX reader exists; the raw export's column
  names are unverified); (3) optional per-package attrition table instead of one spare rule;
  (4) diode/transistor request drafting once the discrete profiles exist; (5) a helper to
  write the suggested C-codes into empty LCSC fields (same opt-in preview/apply model as
  `tools/fill_fields.py`).

## File map

| File | Responsibility |
| --- | --- |
| `kicad_checker/sourcing/requirements.py` | Strict request validation, units, package comparison, catalog constraint checks |
| `kicad_checker/sourcing/providers.py` | DigiKey bounded search/cache, normalized offers, stock imports, JLCPCB stock transport |
| `kicad_checker/sourcing/lcsc.py` | Official LCSC request signing and exact manufacturer/MPN stock lookup |
| `kicad_checker/sourcing/jlc_auth.py` | Unchanged signer copied from the supplied old agent |
| `kicad_checker/sourcing/service.py` | Pure candidate grouping, stock freshness/eligibility, distinct target count, report generation |
| `kicad_checker/sourcing/cli.py` | Search/stock CLI wiring, bounded cross-supplier lookups, cache orchestration |
| `kicad_checker/sourcing/bom.py` | Circuit-to-BOM grouping and per-part stock reports |
| `kicad_checker/sourcing/stockfile.py` | Stock CSV/XLSX reading, header and column detection |
| `kicad_checker/sourcing/inventory.py` | BOM lines, stock matching, spares, R/C/L spec parsing, in-stock alternatives, request drafts |
| `kicad_checker/sourcing/inventory_cli.py` | `inventory-check` prompts/flags, market check, DigiKey alternatives, reports |
| `tests/test_inventory.py` | 21 offline tests for the inventory check |
| `tests/test_sourcing.py` | 34 offline tests covering screening, identity, inventory, adapters, CLI and BOM integration |
| `tests/test_catalog_search.py` | 12 offline tests covering generic categories, real IC/connector fixtures, signed scalar/count criteria, API filter cache isolation and generic CLI |

Existing files changed narrowly: `cli.py` registers commands; `digikey.py` adds
keyword pagination/filter arguments while retaining old defaults and retry behavior;
README and `.env.example` document the additions. Existing checker evidence caches
and review rules are not rewritten.

## Older projects supplied by the user

Read as reference material, rather than as new user requests:

- `/home/erekle/Downloads/Comp agent-20261024T133901Z-1-001/Comp agent/HANDOFF_TO_CHATGPT.txt`
- The sibling `component agent/` source/tests and `review-agent/SKILL.md`.

The JLCPCB signer was reused unchanged and its published test vector is covered by
the new offline tests. The old JLCPCB detail endpoint/field contract was adapted
into a standard-library transport. The old one-page substring catalog search was
not ported: it could miss a valid part and incorrectly imply no match. Original
files were left untouched. The old project's full test suite was not executed:
its separate third-party dependencies are not installed in this checker environment.
The review-agent file is a review reference, not an installed skill or a spawned agent.

## Verification completed

```sh
python3 -m unittest discover -s tests
# 155 tests run: 154 passed, 1 existing skip.
python3 -m unittest discover -s tests -p test_sourcing.py
# 34 passed, no credentials/network.
python3 -m unittest discover -s tests -p test_catalog_search.py
# 12 passed, no credentials/network.
python3 -m compileall -q kicad_checker
git diff --check
```

The existing skip is a missing-credentials test that deliberately skips when a
local `.env` exists. No `.env` contents were displayed or changed.

CLI dry run and real generated offline reports were checked:

- `runs/sourcing-demo-20261024/sourcing.md`: one catalog-matching resistor,
  **zero current qualified candidates**, shortfall three due to old stock evidence.
- `runs/sourcing-stock-20261024/sourcing.md`: exact-part report using the existing
  checker cache; freshness issues remain visible.
- `runs/sourcing-regulator-20261024/sourcing.md`: a real recorded LDO matches
  explicit category/package/output/current/Iq criteria. Old stock stays unresolved;
  full parameter listings and the datasheet link are included.

**No authenticated supplier API call was made in this iteration.** DigiKey fixtures
are recorded older responses; new HTTP behavior uses mocks. LCSC signing/schema
was checked against current official docs. JLCPCB endpoint compatibility is inherited
from the old implementation, with mock/vector checks only. Do not describe all three
as successfully validated live.

## Known limits and next milestones

1. Validate read-only live calls with the user's supplier accounts, starting with
   the resistor example and a known LCSC/JLCPCB C-code. Verify LCSC key names match
   their portal; verify JLCPCB endpoint response schema, manufacturer identity, MOQ,
   assembly availability and Basic/Preferred enum semantics. Never print secrets or
   raw error bodies. Prefer sanitized real response fixtures for any repairs.
2. Improve search recall with DigiKey-discovered category/parameter filter IDs.
   Explicit numeric category IDs now reach the API. Further parametric filtering
   and automatic filter discovery remain future work. Search can report
   a shortfall because of its page budget, not because no suitable part exists.
3. Add baseline-MPN-to-requirements extraction for RLC with provenance and a review
   step. Currently `--replace MPN` excludes an original; the user/assistant still
   supplies explicit engineering requirements. An MPN alone cannot establish all
   circuit requirements. Do not silently downgrade voltage, power or qualification.
4. Add a reviewed manufacturer alias map for cross-supplier naming differences.
   Current matching is exact except case/whitespace; unmatched names remain unknown.
5. Add persistent stock history/observations to distinguish repeatedly stocked
   parts from one-time inventory. Currently “normally stocked” means DigiKey's
   explicit flag; it is not a forecast. Per-run reports preserve snapshots, caches
   retain only the latest result. No scheduled monitoring has been requested.
6. Integrate per-BOM-row alternative requirements and approved alternatives lists,
   then a friendly UI or natural-language request translation. BOM stock is implemented;
   automatic engineering requirements inference and BOM substitution are not.
7. Extend engineering validation for diodes, MOSFETs, ICs and connectors using
   manufacturer documents and pin/package mapping. Their catalog search is now
   supported; similar headlines alone still do not establish interchangeability.

## Catalog-count semantics after the all-category extension

The user explicitly requested support for all other DigiKey items. `kind` is now an
open label; built-in RLC/diode profiles remain special conveniences. Generic type
labels are not automatic classification rules. Explicit categories and named
parameters are the criteria. RLC category guards cannot be overridden by a generic
category filter. `value` and `tolerance_percent` shorthand remain RLC-only and are
rejected elsewhere rather than silently ignored.

`qualified_candidates` is retained for compatibility and counts catalog/stock
matches, with `target_basis: supplied_catalog_criteria_and_digikey_stock`. Diode
matches now count toward that catalog target too; this intentionally supersedes
the first iteration's blanket zero count. `engineering_status` remains
`requires_review` for every item, including RLC. Reports do not approve substitutions.

Credentials remain local through the existing loader. Only blank environment names
were added to `.env.example`. No order/purchase endpoint or design editing is exposed.
An existing untracked `Claude outputs/` folder was present before this work and was
not changed. No commit was created.
