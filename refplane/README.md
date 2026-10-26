# Reference-plane check for KiCad boards

Checks that routed nets run over unbroken reference copper on the next layer. It reports
traces that cross an antipad, a cutout, a split-plane gap or the edge of the plane, traces
whose only reference is a power plane, and plane edges or voids inside a margin band beside
the trace. At every layer change it checks that the return current can follow: a return via
when the reference moves to another layer, or a warning when it moves to another net.

- Python 3.10+ and the standard library only. The board file is never modified.
- It reads the **zone fills** in `.kicad_pcb`. KiCad stores antipads only as holes in those
  fills, so the fills are what get checked. By default the zones are refilled first with
  `kicad-cli pcb drc --refill-zones --save-board`, on a temporary copy.
- Net classes are read from `.kicad_pro`. You choose which classes to check.

## Quick start

```sh
python3 -m refplane info  ~/projects/board/board.kicad_pro      # layers, guessed roles, net classes
python3 -m refplane check ~/projects/board/board.kicad_pro      # asks roles, ref nets, classes, return-via rules
```

At the end, the questions run offers to save your answers to `<board>.refplane.json` next
to the board. After that, `check` asks nothing, until you pass `--ask`.

Non-interactive (CI, or through an AI assistant):

```sh
python3 -m refplane check board.kicad_pro -y \
    --layers sig,gnd,pwr,sig --gnd-nets 'GND' --pwr-nets '+3V3,+5V' --classes 'USB,Clock'
```

Every run prints a `re-run:` line with the exact flags it used.

## Settings files

Settings come from three places. Later ones win:

1. [`refplane/defaults.json`](defaults.json): the built-in defaults. Edit it to change them
   for every board.
2. `<board>.refplane.json` next to the `.kicad_pcb`. It takes any subset of the same keys,
   plus the saved answers: `layers`, `gnd_nets`, `pwr_nets`, `classes`, `nets`. `--config FILE`
   adds one more file on top.
3. Command-line flags.

Nested keys are merged, so a board file can change only
`stitch.profiles.low_speed.distance_mm`. Name patterns from a board file are tried before the
defaults' patterns. Keys starting with `_` are comments.

## Layer roles

| Role | Traces checked? | Counts as reference? |
| --- | --- | --- |
| `sig` | yes | no |
| `gnd` | no (tracks there are listed as cutting the plane) | yes: its GND/PWR net fills |
| `pwr` | no (same) | yes: its fills |
| `hybrid` | yes | yes, but only its GND/PWR net fills |

`--layers` accepts a full list from top to bottom (`sig,gnd,pwr,sig`) or overrides
(`In2.Cu=hybrid`, `L3=pwr`). Without it the roles are guessed from the KiCad layer type and how
much of the board the fills cover. Outer layers are always guessed as `sig`, because a top pour
is not trusted as a plane unless you say so.

A signal layer is checked against the adjacent copper layers only, the one above and the one
below. The plane two layers away doesn't count when a signal layer sits between. A stripline
passes wherever **either** neighbour has reference copper.

## Power rails are skipped

Routed supply traces (+3V3, VCC, …) are not signals, so their tracks are skipped by the trace
check and the layer-change check. A net counts as a power rail if either:

- its name matches `power_nets.patterns` in `defaults.json`: `+*`, `-*V*`, `VCC*`, `VDD*`,
  `VEE*`, `VBAT*`, `VBUS*`, `VSYS*`, `VIN`, `*_VIN`; or
- it has a zone fill anywhere on the board and is not a GND net (`power_nets.nets_with_zones`).

Skipped nets are listed in the report. `--include-power` checks them anyway, and so does naming
one with `--nets` (e.g. `--nets '+5V'`).

## Reference nets

- **GND nets** (default: names containing GND, VSS, EARTH, CHASSIS, or `0V`) are full
  references.
- **PWR nets** (default: every other net with a fill on a reference layer) also count, but
  each stretch of trace over PWR only is a **warning**. A power plane is an AC reference only
  when it's well decoupled, and the return current needs a cap where the signal changes layer.

## How the check works

For each segment of a selected net on a `sig`/`hybrid` layer, and for each adjacent reference
layer, the fill is sampled along five lines parallel to the trace:

```text
   margin line   ─ ─ ─ ─ ─ ─ ─ ─   +(w/2 + k·H)
   trace edge    ───────────────   +w/2
   centre line   ═══════════════    0
   trace edge    ───────────────   −w/2
   margin line   ─ ─ ─ ─ ─ ─ ─ ─   −(w/2 + k·H)
```

Each line is intersected exactly with the fill polygons, using the even-odd rule. That rule
handles KiCad's fractured fills, where holes are joined to the outline by zero-width slits. The
result is a set of intervals along the trace:

| Finding | Severity | Condition |
| --- | --- | --- |
| `no_reference` | error | trace **centre line** not over GND/PWR copper on any adjacent reference layer |
| `reference_edge` | warning | centre line referenced, but a trace edge is over a void; the message gives how far the void reaches into the trace (5 % steps of the width) |
| `pwr_reference` | warning | trace body over PWR copper only |
| `reference_margin` | warning | body referenced, but a margin line leaves the copper: void or plane edge within k·H |
| `no_adjacent_plane` | error | a selected net is routed on a layer with no adjacent reference layer |
| `stale_fill` | error | a via or through pad sits inside another net's fill: the fill is out of date |
| `empty_plane`, `unfilled_zone` | error / warning | plane layer with no reference fill; reference zone with no fill |
| `hatched_reference` | warning | reference zone is hatched: not a solid plane |
| `tracks_on_plane` | info | signal nets routed on a `gnd`/`pwr` layer cut the plane |
| `return_via_missing` | profile: error (`high_speed`) / warning (`low_speed`) | at a layer change the reference moves to the same net on another layer, with no via of that net within the profile distance |
| `return_via_shared` | warning | one return via serves more signal vias than the profile's `max_share` |
| `reference_net_change` | warning | at a layer change the reference moves to a different net, e.g. GND → +3V3 |

**Error messages explain what is under the trace.** The copper just before and just after the
run is looked up:

- `void in GND` (antipad, cutout or slot)
- `crosses split between +3V3 and GND`
- `runs off the edge of GND`
- `no reference copper`

**Some voids are expected and are not reported: only the antipad of a via or through-hole
pad the net lands on.** All three conditions must hold:

- the net's routing on that layer ends on the via or pad (KiCad stores each straight piece
  as its own segment, so this is judged per net and layer);
- the via or pad passes through both the trace's layer and the reference layer being checked.
  A blind via that stops above the plane waives nothing on it;
- the point is inside the antipad disc on that layer: copper radius + the larger of the zone
  clearance and the netclass clearance + 0.05 mm. Only zones and reference nets filled on
  **that** layer count: a 2 mm clearance on a +HV plane on In2 does not widen the GND
  antipad on In1. Two reference nets sharing one layer still use the larger of the two.

The disc is filled in line by line: centre, edges and margin lines. Every other void is
still reported, including a neighbouring via's antipad right next to it.

**Coverage does not depend on direction.** Each sampled line is split at every edge crossing,
including crossings exactly at its ends, and each piece is classified by its midpoint. A trace
that starts on the plane edge and leaves it gives the same result in both directions.

### Layer changes

At each via, and each through-hole pad, of a selected net that has tracks on two or more
layers, the reference is looked up on each of those layers. The lookup point is on the
track, just past the antipad: via or pad radius + zone clearance + 0.25 mm from the centre.

| Reference on the two sides | Result |
| --- | --- |
| a shared (layer, net), e.g. F.Cu and In2.Cu (sig) both over In1 GND | OK: the shared plane carries the return |
| same net, different layers, e.g. GND In1 → GND In4 | needs a **return via**: a via or through pad of that net within the profile distance |
| different nets, e.g. GND → +3V3 | warning `reference_net_change`: no conductive return path |
| one side unreferenced | nothing extra (the trace check already reports it) |

A return via only counts if it does all three:

- lies within the net's profile distance, centre to centre;
- reaches both reference layers (blind and buried spans are checked);
- is tied into the fill on both layers, by solid connection or thermal spokes.

When none qualifies, the message names the nearest one that would have qualified if it were
closer, and any nearer via that fails the span or connection test.

#### Return-via profiles

Not every net needs its own return via. Slow buses can share one or two GND vias between
several signal vias. Each net gets a profile:

| Profile (defaults.json) | Distance | `max_share` | Missing return via |
| --- | --- | --- | --- |
| `high_speed` (default) | 1.0 mm | 2 (a differential pair shares one) | error |
| `low_speed` | 3.0 mm | 4 | warning |

A net's profile is chosen in this order:

1. `stitch.classes`, for any of its net classes except Default. If it has several, the
   strictest wins.
2. The first matching `stitch.nets` name pattern. The defaults cover `*I2C*`, `*SCL*`,
   `*SDA*`, `*UART*`, `*SPI*`, `*MISO*`, `*MOSI*`, `*CAN*`, `*SWDIO*`,
   `*NRST*`, `*GPIO*` and `*LED*`. Patterns match case-insensitively on the last part of the
   net name, so `/MCU/I2C1_SCL` matches.
3. `stitch.classes.Default`.
4. `stitch.default_profile`.

So I2C1_SCL, SPI2_MISO and UART4_TX in the Default class are `low_speed` without any setup.
Putting a net into a deliberate net class, such as `HighSpeed`, overrides its name pattern.

**Explicit mode: name the high-speed nets, and the rest are low speed.** Set
`stitch.high_speed` to a list of net classes and/or net patterns, e.g.
`["USB", "Clock", "*ETH_*", "/LVDS_*"]`. You can also pass `--high-speed 'USB,Clock,*ETH_*'`, or
answer the first return-via question. Then:

1. A net in that list gets `high_speed_profile` (default `high_speed`).
2. Every other net gets `low_speed_profile` (default `low_speed`).
3. Name patterns are not used.
4. Explicit `stitch.classes` rules still win, e.g. `USB=0.6`.

An empty list, or `--high-speed -`, switches back to name-pattern mode. The list is saved in
`<board>.refplane.json` along with the other answers.

A class rule or pattern value can be a profile name, or a number. A number keeps the
profile's other values and changes only the distance.

**Sharing.** Signal vias are assigned to return vias so the load is spread: the most
constrained signal via is placed first, and a return via that still has room is preferred.
A return via that ends up serving more signal vias than a profile's `max_share` gives one
`return_via_shared` warning, listing the nets. Fix it by adding a GND via, or by spreading
out the signal vias.

When the check asks its questions, it shows each checked class with the profile it got,
and whether the profile came from the class, a name pattern or the default. You can change
any class with `CLASS=MM` or `CLASS=PROFILE`, for example `USB=0.6,Default=low_speed`.

The per-net table also lists each net's **reference profile**: how many mm of its centre
line run over each (layer, net), e.g. `In1.Cu/GND 24.6, In2.Cu/GND 6.5`. It also shows how
many of its layer changes passed.

### Margin

H is the dielectric thickness between the two copper layers, taken from **Board Setup →
Physical Stackup**. The margin is `--margin-h` (default 3) × H. About 80 % of the return current
in a microstrip flows within ±3H. With no stackup, H = 0.2 mm is used and a note says so.

## Output

The report goes into a new directory (default `runs/refplane-<board>-<time>/`):

| File | Content |
| --- | --- |
| `report.md` | Setup, layer table (H, margin), per-net coverage table, findings |
| `report.json` | Same, machine readable. Coordinates are KiCad board mm (Y down) |
| `<layer>.svg` | Each checked layer over its reference fills, with its pads and vias (GND vias outlined green). Red = no reference under the centre, magenta = edge over a void, orange = PWR only, yellow = margin, dashed rings = layer-change findings. Hover any mark, track, pad or via for details |

Exit code `1` if there is any error, otherwise `0`. `--json` prints JSON to stdout, and
`--no-files` skips the directory.

## Options

| Flag | Default | |
| --- | --- | --- |
| `--layers` | ask, or guess | roles top → bottom or overrides |
| `--gnd-nets`, `--pwr-nets` | ask, or guess | comma lists, `*`/`?` wildcards; `--pwr-nets ''` for none |
| `--classes` | ask, or all non-Default classes | comma list or `all` |
| `--nets` | — | extra nets (wildcards), added to the class selection |
| `--margin-h` | settings file (3) | margin band in multiples of H |
| `--stitch-class` | settings file | per class `CLASS=MM` or `CLASS=PROFILE`, e.g. `USB=0.6,Default=low_speed` |
| `--high-speed` | settings file (empty) | explicit mode: these classes/nets (comma, wildcards) are high speed, all others low speed; `-` = name patterns |
| `--stitch-mm` | settings file (1.0) | distance of the default profile |
| `--config` | — | extra settings JSON on top of defaults.json and `<board>.refplane.json` |
| `--save-config` | — | write the answers to `<board>.refplane.json` (the questions run offers this too) |
| `--ask` | — | ask the questions again even when `<board>.refplane.json` has answers |
| `--min-length` | settings file (0.02 mm) | shorter runs are ignored as numeric noise |
| `--refill` | `auto` | `auto`: refill if kicad-cli is found; `yes`: must refill; `no`: use saved fills |
| `--kicad-cli` | PATH, standard install locations | |
| `--include-power` | — | also check power-rail nets |
| `-y` | — | no questions: flags plus guesses, each guess listed as "assumed" |

## Limits and next steps

- **Tracks only.** SMD pads over a void (e.g. a deliberate void under an RF pad) are not
  checked yet. Neither are copper pours of signal nets, or the trace's own pads.
- **No exceptions file yet.** Deliberate voids will be reported until there is a waiver list,
  planned as net + area.
- **Return vias are judged by distance and sharing only.** Loop inductance, and the via's
  own return-path impedance, are not modelled. Decoupling caps are not treated as a return
  path.
- **Name patterns are heuristics.** Generic suffixes like `*_TX` / `*_RX` are deliberately not in
  the defaults (they can be SerDes, Ethernet or LVDS). `*CAN*` also matches e.g. `SCAN`. Fast
  nets in a net class always win over patterns; edit the list for your naming.
- **Zones inside footprints are ignored.**
- **One real KiCad 10 board so far.** `Earth_Sensor_18x40` (file format 20260206, inline
  `(net "GND")` names, one GND zone on In1 + In2) parsed in 0.2 s, 2026-10-26. Other boards are
  untested. `pcb.py` reads `(net N "name")`, `(net N)` with a net table, `(net "name")` and
  `net_name`.
- **Mounting-hole nets** (`Net-(H*-Pad1)`) are still treated as signals. They give no findings,
  but could be skipped.
- **Speed:** about 7 s for 4,000 segments over a plane with 1,200 antipads (40k edges), in
  pure Python.
