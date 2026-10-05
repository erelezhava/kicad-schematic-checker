# KiCad Schematic Checker — independent review (Claude, 2026-10-05)

Reviewed: all docs, `kicad_checker/` (cli.py, core.py), tests, `examples/`, `cases/earth-sensor-ldo-v02`, and the earth-sensor runs.
Executed: test suite (29/29 pass, Python 3.13), demo pipeline (exit 1 as designed), earth-sensor case rerun, and targeted edge-case probes.
Note: README/AGENT_GUIDE/SYMBOL_CHECKS were being modified while this review ran; findings reflect the snapshot taken at that time.

---

## 1. Fresh description

**KiCad Schematic Checker** is an AI-agnostic, evidence-based schematic review tool for KiCad projects.

It splits the work into two halves:

- **Deterministic engine (Python, no dependencies):** extracts connectivity, pin inventories, fields and ERC via `kicad-cli`, then evaluates JSON rules against JSON evidence. It produces the same verdict regardless of which AI or human prepared the inputs.
- **AI reviewer (any assistant: Claude, ChatGPT/Codex, local Ollama models):** reads datasheets in `docs/`, turns requirements into traceable rules, binds them to real components, and collects exact-part evidence with source page, revision and SHA-256.

Every requirement ends as `pass`, `fail`, `needs_review`, `not_checked` or `not_applicable`. Missing evidence never becomes a pass. The tool never edits the design.

Current state (v0.1): component identification audit (Manufacturer + MPN), pin-to-pin connectivity checks, component property checks (range / one_of), symbol pin-count checks, manual/heuristic tracking, and source-freshness pinning. One real case so far: the LP5912-EP LDO block on Earth Sensor 18x40.

---

## 2. Verdict

**The foundation is solid but the engine does too little, and the AI is asked to do too much by hand.**

The anti-false-pass discipline is excellent: drafts stay visible, evidence has to be tied to the exact part, sources are hash-pinned, and the tests cover the edge cases. That is the hard part to retrofit, and it's done.

The problem is the ratio of effort to value. After 9 iterations, one LDO block has 27 hand-written rules. The real engineering outcome is 7 connectivity passes, 2 nominal-value passes, and 18 unresolved items. Scaled to a full CubeSat board, that's hundreds of per-refdes JSON entries per project, written and "approved" by the same AI. That won't scale, and it won't run on a local Ollama model at all.

---

## 3. What's good (keep it)

1. **Clean split between engine and AI.** The verdict is deterministic and reproducible, which is the right architecture.
2. **KiCad's own connectivity engine via `kicad-cli`.** No home-made S-expression parsing, so nets are what KiCad says they are.
3. **Fail-closed semantics.** Draft, unverified, wrong-part, unit-mismatch, stale-source and DNP cases all land in `needs_review` or `fail`, never in `pass`.
4. **Evidence-basis split (`schematic` vs `manufacturer`).** It correctly separates "declared 2.2 µF" from "effective capacitance under bias".
5. **Source pinning by SHA-256.** If a datasheet changes, its checks go stale.
6. **Live-project freshness check and `--snapshot` mode.** The CLI refuses to review a stale extraction unless told to.
7. **Exit codes suitable for CI** (0/1/2/3).
8. **Good test coverage** for v0.1: 29 tests, focused on false-pass prevention.
9. **The LDO review content is engineering-correct.** It distinguishes the 0.7 µF stability minimum from the 1–10 µF recommendation, and it flags the 18 µF nominal total on +3V3 against the 10 µF maximum as a question rather than a verdict. A senior engineer would raise the same points.

---

## 4. Bugs and trust holes (verified by running code)

| # | Issue | Where | Evidence | Severity |
|---|---|---|---|---|
| B1 | **The `schematic_value` class is not checked against the schematic.** The engine trusts the AI-typed number. A fact saying C1 = 4.7 µF passes even though the schematic says 2.2 µF. | core.py `review()` property branch (~L395–434) | Probe: the fact is accepted and the status is `pass` | **High.** It defeats the purpose of a deterministic engine. |
| B2 | **`one_of` matching is case-sensitive and gives a false `fail`.** `"x7r"` vs `["X7R"]` produces `fail`, not `needs_review`. | core.py L424 | Probe: status `fail` | Medium |
| B3 | **Absolute paths make cases non-portable.** Rules pin `/home/erekle/GitLab/adcs/...`. On any other machine or CI runner, all 27 checks become `needs_review` ("source missing or changed"). | `cases/*/rules.json`, `evidence.json` | Rerun here: 0 pass / 27 needs_review | **High.** It contradicts "platform independent". |
| B4 | **The case's evidence points into the git-ignored `runs/` folder.** `evidence.json` cites `runs/earth-sensor-extract-v02/circuit.json`, but `.gitignore` excludes `runs/`. A fresh clone cannot reproduce the case. | `.gitignore`, `cases/earth-sensor-ldo-v02/evidence.json` | Read both files | Medium |
| B5 | **The AI approves its own rules.** `status: approved` plus `reviewed_by: "Codex"` is self-attestation. The report does not distinguish agent-reviewed rules from human-approved ones. | rules schema | Rules note says "Approved means … reviewed by Codex, not user approval" | Medium. It's honest, but the gate is meaningless. |
| B6 | **Mounting holes are flagged as missing MPN.** H1–H4 get `needs_review`, and 28 of 32 parts are flagged because they have an MPN but no `Manufacturer` field. The identification report is mostly noise on a real board. | `identification()` | Rerun output | Low–Medium (usability) |
| B7 | **No unit normalisation.** `uF` vs `µF` vs `u` vs `nF` all count as mismatches. Conservative, but it creates friction. | core.py L426 | Code | Low |

---

## 5. Architectural gaps (what limits real value)

**G1: No net-level queries.** The engine can only ask "are pin A and pin B on the same net?" and "is a property of part X in range?". Most real schematic review questions are about nets, for example:

- Which capacitors sit between net N and GND, and what is their nominal sum?
- Does every IC power-input pin have at least one decoupling cap on its net?
- Does every open-drain or I²C net have exactly one pull-up?
- Are there single-pin nets, floating inputs, or NC pins wired to something?
- Do any power nets have no source?

Today each of these has to be hand-written per refdes, or tagged "heuristic" and skipped.

**G2: Rules are bound by refdes, not by role.** `output_capacitor → C2` is written by hand for each project. Rules should be bound by pin function and topology, for example "the cap(s) between `U[LP5912].OUT`'s net and GND". Then an MPN rule pack works on any board that uses that part.

**G3: No generic rule pack.** There is nothing that runs with zero per-design input. A board-agnostic "lint" layer would give value on day one, on every project, with no AI involved.

**G4: The engine doesn't parse values.** The KiCad `Value` ("2.2u", "4k7", "10 µF/25V") is never parsed, which is the root cause of B1.

**G5: ERC is extracted but not reviewed.** `erc-summary.json` sits beside the report rather than inside it. ERC findings should become classified results (library/setup vs circuit) in the same report.

**G6: The docs are too long and too hedged.** README + AGENT_GUIDE are about 26 KB, and many caveats are repeated three or four times. Strong models will work through it. A local Ollama model (7–32B) will lose the thread, and so will a mid-size cloud model on a big board. The guide should be a short operating checklist, with policy in one place.

**G7: No access layer for AI tools.** "Any AI" currently means "an AI that can run shell commands and hand-write large JSON files". Exposing the engine as an MCP server (`extract`, `query_net`, `list_caps_on_net`, `run_rules`, `propose_rule`) would make Claude, Codex and Ollama clients all first-class, with small tool calls instead of hand-authored files.

---

## 6. Recommended roadmap (in this order)

**Phase 1: Close the trust holes (small, do first)**

1. Add an SI value parser (`2u2`, `4k7`, `10µF`, `100n`, `0R`). It derives `schematic` facts **automatically from `circuit.json`**, and agent-supplied schematic facts are rejected or cross-checked (fixes B1, G4).
2. Make `one_of` matching case- and whitespace-insensitive, and send unknown enum values to `needs_review` instead of `fail` (B2).
3. Make `source.path` relative to the project's `docs/` root, with the hash kept. Add `--docs-root` to the CLI (B3).
4. Move case circuit snapshots out of `runs/`, or commit a pinned `cases/<case>/circuit.json` (B4).
5. Split the approval fields: `proposed_by` (agent) and `approved_by` (human, optional). The report shows "agent-reviewed only" counts separately (B5).
6. Auto-exempt `MountingHole*` / `TestPoint*` library symbols that have no footprint pads or nets. Report "MPN present, manufacturer missing" as one grouped finding (B6).

**Phase 2: Net-level engine and generic lint (biggest value jump)**

7. Add query primitives: `components_on_net(net, class=C|R|L)`, `between(netA, netB)`, `pins_of_type(power_in|open_collector|...)`, `net_has_driver`.
8. Add new check kinds: `net_sum` (e.g. total C between +3V3 and GND, nominal), `exists_between` (pull-up present), `per_pin` (every `power_in` pin has a cap to GND on its net), `single_pin_net`, `nc_pin_connected`.
9. Write a **generic board lint pack** that runs on every project with no AI: decoupling coverage, I²C/open-drain pull-ups, floating inputs, undriven power nets, DNP straps that could short supplies, NC misuse, and duplicate or missing MPNs.

**Phase 3: Reusable part packs (where CubeSat reuse pays off)**

10. Write MPN/family rule packs (`rules/parts/LP5912.json`) bound via **pin function names**, not refdes. Build each pack once, then reuse it across every board that uses the part.
11. Add a capacitor DC-bias evidence adapter. For Murata GCM parts (all of C1/C2/C5/C37/C44/C98), the manufacturer publishes DC-bias and ESR curves. That data closes LDO-COUT-EFFECTIVE and LDO-COUT-ESR properly.

**Phase 4: Automation**

12. Run it in GitLab CI on every push: extract, generic lint and part packs, with the report as an artifact and exit code 1 failing the pipeline.
13. Expose the engine as an MCP server (see G7), so Claude, Codex and Ollama clients all use the same tools.
14. Cut AGENT_GUIDE to about 150 lines: a workflow checklist, plus one policy section, plus links.

---

## 7. Suggested division of work

- **Engine (Python) changes:** whichever agent you prefer. They are small, well-tested-area changes, so ask for a test per bug (B1–B7) first.
- **Rule packs and datasheet extraction:** good to cross-check between models. One model writes the pack, the other reviews it against the PDF pages. That is a real second reviewer, which fixes B5 in practice.
- **Engineering judgement** (does the 10 µF LP5912 limit apply to the whole +3V3 network? is PG intentionally unused?): you.

## 8. Open engineering items from the Earth Sensor LDO case (unchanged, still yours)

- C2/C37 GCM21BR71E225KA73L: effective capacitance at 3.3 V / 5 V bias and ESR. Needs Murata data.
- +3V3 output network is 18 µF nominal (C1, C2, C5, C44, C98) against the LP5912 table maximum of 10 µF. Decide whether that limit covers distributed load decoupling.
- U2 symbol: GND/EPAD typed as power output, which causes an ERC false error. Fix it in the library.
- U2 PG has no receiver. Intentional?
- EN driven from J2.1 over a ~300 mm cable: levels and sequencing undefined.
- Input-cap note (≥10 µF with long supply leads): depends on the U3 filter and cable impedance.
