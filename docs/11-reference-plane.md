# 11. Check that nets run over an unbroken reference plane

Finds traces that cross a gap in the reference plane under them: via antipads, cutouts, gaps
between split planes, and the edge of the plane. It also warns where a trace is referenced only
to a power plane, and where a void or plane edge sits close beside a trace. At every layer
change it checks for a return via next to the signal via. Offline, no keys.
The board file is never changed.

[← All guides](../USER_GUIDE.md) · Commands: `python3 -m refplane info | check`

## Try it on the demo board (offline, 10 s)

```sh
python3 -m refplane check tests/fixtures/refplane/demo_4L.kicad_pro -y --refill no -o runs/refplane-demo-001
```

Expect 1 error (`/USB_DP` crosses a GND antipad on In1.Cu) and 5 warnings: the trace edges
just before and after the void (`reference_edge`), and `/USB_DN` plus both ends of `/USB_DP`
within 3H of it (`reference_margin`). Open `runs/refplane-demo-001/F_Cu.svg` in a browser to
see them.

## Run it

1. In KiCad, set the stackup (**Board Setup → Physical Stackup**) and save. The margin depends
   on the dielectric thickness H.
2. See what the tool detects:

   ```sh
   python3 -m refplane info ~/projects/board/board.kicad_pro
   ```

3. Run the check. It asks four things. Press Enter to keep the suggested answer:
   - the layer roles (`sig`, `gnd`, `pwr`, `hybrid`)
   - which nets are GND and which are PWR
   - which net classes to check
   - the return-via rule for each class (see below)

   At the end it offers to save your answers to `board.refplane.json`, next to the board.
   After that, runs ask nothing until you pass `--ask`.

   ```sh
   python3 -m refplane check ~/projects/board/board.kicad_pro
   ```

   If `kicad-cli` is installed, the zones are refilled first on a temporary copy, so stale
   fills can't hide or invent gaps.
4. Open `runs/refplane-<board>-<time>/report.md`. Then open the `<layer>.svg` files in a
   browser to see where each finding is.

To run it again without the questions, copy the `re-run:` line it prints, for example:

```sh
python3 -m refplane check board.kicad_pro -y --layers sig,gnd,pwr,sig \
    --gnd-nets 'GND' --pwr-nets '+3V3' --classes 'USB,Clock'
```

## Layer roles

| Stackup | `--layers` |
| --- | --- |
| 4L classic | `sig,gnd,pwr,sig` |
| 4L, two ground planes | `sig,gnd,gnd,sig` |
| 6L | `sig,gnd,sig,sig,gnd,sig` or `sig,gnd,sig,pwr,gnd,sig` |
| inner layer with a GND pour and some routing | that layer `hybrid` |

A signal layer is checked against the layers directly above and below it. A layer next to a PWR
plane is still checked: its traces get PWR-reference warnings instead of being skipped.

## Power rails

Routed supply traces (+3V3, +5V, VCC…, and any non-GND net with a copper pour) are skipped:
they are not signals. The report lists them. `--include-power`, or naming a rail in `--nets`,
checks them anyway.

## Return-via rules

When a net changes layer and its reference moves to another GND layer, a GND via is needed
nearby. How strict that is depends on the net:

| Profile | For | GND via within | Signal vias per GND via | If missing |
| --- | --- | --- | --- | --- |
| `high_speed` | clocks, USB, Ethernet, LVDS, RF, and anything not matched below | 1.0 mm | 2 (a pair) | error |
| `low_speed` | I2C, UART, SPI, CAN, SWD, GPIO, LEDs (matched by net name) | 3.0 mm | 4 | warning |

So I2C1_SCL, SPI2_MISO and UART4_TX can share one GND via. You only get a warning if more
than four signal vias lean on the same GND via, or if none is within 3 mm.

To change the rules:

- **All boards:** edit `refplane/defaults.json`. It holds the profiles and the name patterns.
- **This board:** put the same keys in `board.refplane.json`.
- **This run:** answer the question, or pass `--stitch-class 'USB=0.6,Default=low_speed'`.

A net class you created yourself, such as `HighSpeed`, wins over the name patterns.

**Or turn it around:** name only your fast nets, and everything else becomes `low_speed`.
The name patterns are then ignored:

```sh
python3 -m refplane check board.kicad_pro --high-speed 'USB,Clock,*ETH_*,/LVDS_*'
```

That also works as the first answer to the return-via question, or as
`"high_speed": [...]` under `stitch` in a settings file. `--high-speed -` goes back to name
patterns.

## Reading the result

| Severity | Finding | What to do |
| --- | --- | --- |
| error | `no_reference`: void in GND | The trace centre is over an antipad or cutout: move the trace, or reduce the antipad |
| warning | `reference_edge` | Only the trace edge clips a void. The message says how deep (e.g. "about 13 um into the 0.25 mm trace"). A few µm is harmless; a third of the width is worth fixing on fast nets |
| error | `no_reference`: crosses split | Re-route over one net, or add a stitching cap across the split |
| error | `no_reference`: runs off the edge | Pull the trace in, or extend the plane |
| error | `no_adjacent_plane` | The layer has no plane next to it: change the stackup or the routing layer |
| error | `stale_fill` | Refill zones (B) in KiCad, save, run again |
| warning | `pwr_reference` | OK if decoupled: put a cap between PWR and GND near the layer change |
| warning | `reference_margin` | A void or edge is within 3H beside the trace: fine for slow nets, review for fast ones |
| info | `tracks_on_plane` | Signal routing on a plane layer cuts the plane |
| error / warning | `return_via_missing` | The signal via moves the reference to another GND layer: add a GND via within the ring (dashed ring in the SVG). Error for `high_speed`, warning for `low_speed` |
| warning | `return_via_shared` | Too many signal vias share one GND via: add one, or spread the signal vias |
| warning | `reference_net_change` | The reference changes from GND to a power net at this via: keep the net on layers that share a GND reference |

Coordinates are KiCad board millimetres, so you can go straight to them in KiCad.

Exit code: `1` if there is at least one error, otherwise `0` (warnings don't fail a run).

## Options

| Option | Use |
| --- | --- |
| `-y` | No questions: flags, saved answers and guesses (each guess printed as "assumed") |
| `--layers`, `--gnd-nets`, `--pwr-nets`, `--classes` | Answer the questions up front (`--pwr-nets ''` = none, `--classes all`) |
| `--nets '/DDR_*'` | Add nets to the class selection (wildcards) |
| `--include-power` | Also check power-rail nets |
| `--high-speed 'USB,Clock'` | Explicit mode: only these are `high_speed` (`-` = back to name patterns) |
| `--stitch-class 'USB=0.6,Default=low_speed'` | Return-via rule per class: distance in mm or a profile name |
| `--stitch-mm 1.0` | Distance of the default return-via profile |
| `--margin-h 3` | Margin band each side, in multiples of H |
| `--min-length 0.02` | Ignore runs shorter than this (mm) |
| `--refill auto\|yes\|no` | Refill zones on a temporary copy first (`auto`: if `kicad-cli` is found) |
| `--kicad-cli PATH` | Use this `kicad-cli` |
| `--config FILE` | Extra settings on top of `defaults.json` and `board.refplane.json` |
| `--save-config` / `--ask` | Save the answers without asking / ask again despite saved answers |
| `-o DIR` | Report folder (must not exist; default `runs/refplane-<board>-<time>`) |
| `--json`, `--no-svg`, `--no-files` | JSON to stdout · skip the SVGs · print only, no report folder |

Algorithm, settings merge order and the full findings list:
[refplane/README.md](../refplane/README.md).

## Limits

- Checks tracks, not pads or signal-net pours. Deliberate voids, such as under RF pads, can't
  be waived yet.
- A return via is judged by distance, connection and sharing. Loop inductance is not computed,
  and decoupling caps don't count as a return path.
- Net-name patterns are heuristics (`*CAN*` also matches `SCAN`). A net class or
  `--high-speed` always wins.
- Validated on one real KiCad 10 board (4-layer, sig/GND/GND/sig) so far. Please report any
  parse problem with another board.

**Next:** [9. Use it with an AI assistant](09-ai-assistants.md)
