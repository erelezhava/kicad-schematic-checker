# JLCPCB design rules for KiCad 10

Turns JLCPCB's rigid FR-4 capability table into KiCad 10 custom design rules (`.kicad_dru`). It works like a fab-aware constraint manager: DRC fails on anything JLC can't build, and warns about anything that needs a paid option.

- **One source of truth:** [`capabilities.json`](capabilities.json) (JLC snapshot 2026-10-25). When JLC changes a number, edit it there and regenerate.
- **Stackup aware:** reads layer count, copper weights, thickness, finish and mask colour from the board's Physical Stackup. The limits depend on all five.
- **Tiered severities:** `error` means below JLC's process limit. `warning` means a paid option, a JLC recommendation, or silkscreen legibility.
- **Board-level preflight** covers what KiCad rules cannot express: board size, thickness, layer count, finish restrictions, castellated, HDI, backdrill and gold fingers.
- Python 3.10+ and the standard library only. Never edits the board; `install` writes only `<project>.kicad_dru`.

## Quick start

```sh
# 1. Set the stackup in KiCad first: Board Setup > Physical Stackup
#    (copper thickness per layer, board thickness, copper finish, mask colour).

# 2. Install the rules next to the board (reads the stackup)
python3 -m jlcpcb_rules install ~/projects/board/board.kicad_pro

# 3. Board-level checks DRC can't do
python3 -m jlcpcb_rules preflight ~/projects/board/board.kicad_pro

# 4. In KiCad: reopen Board Setup (rules reload), run DRC.
#    Or headless:
kicad-cli pcb drc --severity-all --format json -o drc.json ~/projects/board/board.kicad_pcb
```

Other commands:

```sh
python3 -m jlcpcb_rules generate --layers 4 --finish ENIG            # print rules for a profile
python3 -m jlcpcb_rules generate --board board.kicad_pcb -o x.kicad_dru
python3 -m jlcpcb_rules install board.kicad_pro --tier standard      # paid options become errors
python3 -m jlcpcb_rules install board.kicad_pro --accept small_via,via_in_pad
python3 -m jlcpcb_rules install board.kicad_pro --dry-run
python3 -m jlcpcb_rules preflight board.kicad_pro --json
python3 -m jlcpcb_rules presets                                      # rebuild presets/
```

You can override any detected value with `--layers --outer-oz --inner-oz --thickness --finish {HASL,HASL_LF,ENIG,OSP} --mask {green,...}`.

## Tiers

| Tier | Below process limit | Paid option / recommendation |
| --- | --- | --- |
| `tiered` (default) | error | warning |
| `standard` | error | error (budget build: nothing paid) |
| `advanced` | error | not reported |

Once you have decided to pay for an option, silence just that one with `--accept`. The keys are `small_via`, `hdi`, `via_in_pad` and `backdrill`.

## What is checked

| Area | DRC rule (KiCad) | Depends on |
| --- | --- | --- |
| Trace width / spacing | `track_width`; `physical_clearance` (different nets), outer and inner | layers, copper oz |
| SMD pad to SMD pad | `physical_clearance` 0.15 mm | |
| Drill sizes | `hole_size`: plated hole, via, NPTH 0.5, plated slot, NPTH slot 1.0, castellated 0.5, press-fit 0.55–2.0 | layers, thickness, finish |
| 0.10 mm drills | allowed only when board ≤ 1.0 mm and finish ENIG or OSP | |
| Via cost tiers | warnings: hole < 0.2, or 0.2–0.25 hole with diameter < 0.45 | |
| Annular ring | `annular_width`: via 0.05; PTH 0.18 (2L) / 0.15 (ML) / 0.254 (2 oz); recommended values as warnings | layers, oz |
| Hole spacing | `hole_to_hole`: 0.2 mm, pad holes 0.45, castellated 0.5 | |
| Hole to copper | `hole_clearance`: via 0.2, PTH 0.28 (outer) / 0.3 (inner); NPTH 0.2 | |
| Board edge | `edge_clearance` 0.2 on copper; castellated / edge-connector pads exempt | |
| HDI | blind/micro 0.075–0.15, buried 0.10–0.55; errors below 4 layers, otherwise paid | layers |
| Via-in-pad | via in an SMD pad (same net) must have Filling = Filled and Capping = Capped; filled holes 0.15–0.55 | layers (free from 6L) |
| Plugged vias | hole ≤ 0.5 mm | |
| Backdrill | 4+ layers, ≥ 0.8 mm, hole 0.2–0.5 | layers, thickness |
| BGA | pad ≥ 0.2; pads under 0.25 need ENIG | finish |
| Small SMD pads | warning when both sides < 0.25 mm | |
| Mask dam | `solder_mask_sliver` 0.10 / 0.13 (black, white) / 0.20 (2 oz) | colour, oz |
| Silkscreen | text height 1.0, stroke 0.15, stroke ≥ height/6, line width 0.15, 0.15 to pads (warnings) | |
| 1-layer boards | no vias, no plated holes | |
| Drill step | vias ≤ 0.6 mm: warning if the hole is off the 0.05 mm tool step; error if rounding up to the next step leaves a ring < 0.05 (e.g. 0.16/0.26 is drilled at 0.2 mm) | |

## Preflight extras

- **Drill step:** checks every via and PTH pad hole. A hole is rounded **up** to the next 0.05 mm tool, and the ring is checked against the JLC minimum at the drilled size. The step and rounding come from fab practice: the JLC page doesn't state them. Change `drill_step` in `capabilities.json` if JLC confirms different values.
- **BGA compatibility:** BGA footprints are found by the pad Fabrication Property "BGA pad" or a `BGA` library name. For each one, preflight measures the pitch and pad size, then works out with this stackup's JLC limits:
  - whether the pad gap meets SMD spacing and the mask dam
  - whether the pad needs ENIG
  - how many tracks fit between balls (including JLC's 3 mil BGA-fanout trace)
  - the cheapest fanout that fits: dog-bone with free vias, dog-bone with paid small vias, via-in-pad (filled + capped), or HDI microvias
  
  Each result is printed next to JLC's pitch guide (0.8 / 0.65 / 0.5 / 0.4 mm).

## How it fits KiCad's rule engine (read this before editing)

- **Your netclass clearances still apply.** JLC copper spacing uses `physical_clearance` with `A.NetName != B.NetName`, a separate check. A custom `clearance` rule would silently replace your netclass clearances, for example an HV class.
- **Board Setup minimums are replaced** for the items these rules match. These *are* the fab limits. Copy the values listed in the file header into Board Setup > Constraints so the router uses them.
- **For one constraint type, the last matching rule wins.** Inside the JLC block, rules go from generic to specific. Your own rules belong below the `END` marker. `install` keeps them when it regenerates, and because they come later they override JLC's.
- **Assertions all run** and don't override anything. That is why cost and recommendation checks are assertions.
- **KiCad 10 ignores the `(layer ...)` clause on assertions,** so assertion rules put the layer test in the condition.

## Known limits

- KiCad can't tell a V-cut edge from a routed one, so the 0.4 mm copper to V-cut rule is not checked.
- Same-net track spacing (0.25 mm), hatched-pour grid, trace coils and impedance are not checked.
- BGA analysis covers fanout feasibility only. It does not cover layer count for escaping inner rows, or JLC *assembly* (PCBA) BGA limits.
- `physical_clearance` flags the intentional short inside net-tie footprints. Exclude those markers in DRC.
- The PTH annular *recommendation* assumes a centred hole and skips custom-shape pads. The hard minimum uses KiCad's exact `annular_width`.
- The JLC page contradicts itself in a few places: max thickness 4.5 vs 3.2 mm, heavy copper 4.5 vs 3 oz, multilayer plated slot 0.35 vs 0.5 mm, impedance ±10 vs ±5 %. The generator uses the permissive value as the hard limit and warns about the conservative one. Confirm with JLC before relying on it.
- FR-4 only. Aluminium, copper-core, Rogers/PTFE and flex are not modelled.
- Verified so far:
  - Syntax checked against KiCad 10.0 source (keywords, property names, enum strings).
  - The rules that KiCad 7 can parse were run through KiCad 7's real DRC engine on a test board. Every planted violation was caught, with no false hits.
  - Still to confirm on a real KiCad 10 install: the KiCad 10-only rules (mask dam, via fill/cap/plug, backdrill, BGA, press-fit).
