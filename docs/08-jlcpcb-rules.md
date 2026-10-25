# 8. Check the PCB against JLCPCB manufacturing limits

Generates KiCad 10 custom DRC rules from JLCPCB's rigid FR-4 capability table, matched to your
board's stackup. DRC then fails on anything JLC can't build and warns about anything that costs
extra. A `preflight` covers what DRC can't express. Offline, no keys.

[← All guides](../USER_GUIDE.md) · Commands: `python3 -m jlcpcb_rules install | preflight | generate | presets`

## Run it

1. In KiCad set the stackup: **Board Setup → Physical Stackup** (copper per layer, board
   thickness, copper finish, mask colour). The limits depend on all of them.
2. Install the rules next to the board:

   ```sh
   python3 -m jlcpcb_rules install ~/projects/board/board.kicad_pro     # writes board.kicad_dru
   ```

3. Board-level checks:

   ```sh
   python3 -m jlcpcb_rules preflight ~/projects/board/board.kicad_pro
   ```

4. In KiCad reopen Board Setup (rules reload) and run DRC. Headless:
   `kicad-cli pcb drc --severity-all --format json -o drc.json board.kicad_pcb`
5. Copy the minimums listed in the `.kicad_dru` header into **Board Setup → Constraints** so the
   router uses them too.

Re-run `install` after any stackup change. `preflight` tells you when the rules are stale.

## Reading DRC results

| Severity | Meaning |
| --- | --- |
| error | Beyond JLC's process limit: they can't build it |
| warning | Paid option, JLC recommendation, or silkscreen legibility |

Choose how strict with `--tier`:

| `--tier` | Beyond limit | Paid option |
| --- | --- | --- |
| `tiered` (default) | error | warning |
| `standard` | error | error (budget build: nothing paid) |
| `advanced` | error | not reported |

Decided to pay for something? Silence just that: `--accept small_via,via_in_pad` (keys:
`small_via`, `hdi`, `via_in_pad`, `backdrill`).

## What preflight adds

Board size, thickness and layer count; finish restrictions; castellated holes, HDI, backdrill,
gold fingers; drill-step rounding (a 0.16 mm hole is drilled at 0.2 mm — is the ring still OK?);
and per-BGA fanout feasibility (pad gap, ENIG need, tracks between balls, cheapest fanout).
`--json` for scripts.

## Your own rules

Put them **below the `END` marker** in the `.kicad_dru`. `install` keeps them on regeneration,
and because they come later they override JLC's. `--merge` keeps existing non-JLC rules when
installing over an old file; `--force` replaces them (backup kept). Your netclass clearances
(e.g. an HV class) still apply.

## Other commands

```sh
python3 -m jlcpcb_rules install board.kicad_pro --dry-run                 # show, don't write
python3 -m jlcpcb_rules generate --layers 4 --finish ENIG                 # rules for a profile, no board
python3 -m jlcpcb_rules generate --board board.kicad_pcb -o x.kicad_dru
python3 -m jlcpcb_rules presets                                           # rebuild jlcpcb_rules/presets/
```

Ready-made files for common stackups are in [jlcpcb_rules/presets/](../jlcpcb_rules/presets/).
Override any detected value with `--layers --outer-oz --inner-oz --thickness --finish --mask`.

## Limits

- V-cut copper clearance, same-net spacing, impedance and hatched-pour grid are not checked.
- FR-4 only. No aluminium, Rogers/PTFE or flex.
- BGA analysis is fanout feasibility only, not JLC assembly (PCBA) limits.
- Capability snapshot: 2026-10-25. When JLC changes a number, edit
  `jlcpcb_rules/capabilities.json` and re-run `install`.
- The KiCad 10-only rules (mask dam, via fill/cap, backdrill, BGA, press-fit) still need
  confirming on a real KiCad 10 DRC run.

Rule-engine details, the full table of checks and known JLC contradictions:
[jlcpcb_rules/README.md](../jlcpcb_rules/README.md).

**Next:** [9. Use it with an AI assistant](09-ai-assistants.md)
