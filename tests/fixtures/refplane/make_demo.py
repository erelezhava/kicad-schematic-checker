"""Regenerate demo_4L.kicad_pcb / .kicad_pro (synthetic 4-layer board, KiCad 10 token layout).

Stackup: F.Cu sig / In1.Cu GND plane / In2.Cu split +3V3 (left) + GND (right) / B.Cu sig.
Each trace is placed to exercise one case of the reference-plane check:

  /USB_DP    F.Cu, passes over the antipad of the /LED via         -> no_reference (void in GND)
  /USB_DN    F.Cu, crosses a fill fracture slit; runs 0.3 mm
             beside that antipad (In1 antipads are 0.8 mm radius)  -> reference_margin only
  /SPI_MOSI  F.Cu, hugs the In1 plane edge                         -> reference_margin
  /LED       F.Cu -> via -> B.Cu over +3V3, then across the split  -> reference_net_change,
                                                                      pwr_reference, no_reference
  /CLK       F.Cu -> via -> B.Cu, GND stitch via 0.9 mm away       -> clean (In1 GND -> In2 GND)

Run:  python3 tests/fixtures/refplane/make_demo.py
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def poly(pts):
    return "(pts " + " ".join(f"(xy {x:g} {y:g})" for x, y in pts) + ")"


def round_hole(cx, cy, r, n=32):
    """Round antipad (as KiCad fills it: via radius + zone clearance), entered from its bottom
    point by a zero-width slit, opposite winding to the outline. Returns the hole loop."""
    import math
    pts = [(round(cx + r * math.sin(2 * math.pi * k / n), 4), round(cy + r * math.cos(2 * math.pi * k / n), 4))
           for k in range(n)]
    return pts + [pts[0]]


def zone(net, name, layer, fill, outline, clearance=0.2):
    return f"""\t(zone (net {net}) (net_name "{name}") (layer "{layer}") (uuid "z-{name}-{layer}") (name "{name}_{layer}")
\t\t(hatch edge 0.5) (connect_pads (clearance {clearance})) (min_thickness 0.2) (filled_areas_thickness no)
\t\t(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
\t\t(polygon {poly(outline)})
\t\t(filled_polygon (layer "{layer}") {poly(fill)})
\t)
"""


def seg(a, b, layer, net, w=0.2):
    return (f'\t(segment (start {a[0]:g} {a[1]:g}) (end {b[0]:g} {b[1]:g}) (width {w}) (layer "{layer}") '
            f'(net {net}) (uuid "s-{a[0]:g}-{a[1]:g}-{b[0]:g}-{b[1]:g}-{layer}"))\n')


def via(at, net, uid):
    return f'\t(via (at {at[0]:g} {at[1]:g}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {net}) (uuid "{uid}"))\n'


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


LED_VIA, CLK_VIA, GND_STITCH = (10, 9.7), (30, 20), (30, 20.9)
VIA_R = 0.3
IN1_CLR, IN2_CLR = 0.5, 0.2     # zone clearances: In1 has large antipads (0.8 mm radius), In2 normal (0.5)

# In1.Cu: GND plane with antipads at the LED and CLK vias (fractured: holes entered from the bottom edge)
h_led, h_clk = round_hole(*LED_VIA, VIA_R + IN1_CLR), round_hole(*CLK_VIA, VIA_R + IN1_CLR)
in1 = ([(0.5, 0.5), (39.5, 0.5), (39.5, 29.5), (h_clk[0][0], 29.5)] + h_clk + [(h_clk[0][0], 29.5)]
       + [(h_led[0][0], 29.5)] + h_led + [(h_led[0][0], 29.5), (0.5, 29.5)])
in1_fill = in1
# In2.Cu: +3V3 left (antipad at LED via), GND right (antipad at CLK via), 0.5 mm split at x = 20..20.5
in1_holes = (h_led, h_clk)
h_led, h_clk = round_hole(*LED_VIA, VIA_R + IN2_CLR), round_hole(*CLK_VIA, VIA_R + IN2_CLR)
in2_pwr = [(0.5, 0.5), (20, 0.5), (20, 29.5), (h_led[0][0], 29.5)] + h_led + [(h_led[0][0], 29.5), (0.5, 29.5)]
in2_gnd = [(20.5, 0.5), (39.5, 0.5), (39.5, 29.5), (h_clk[0][0], 29.5)] + h_clk + [(h_clk[0][0], 29.5), (20.5, 29.5)]

HEAD = """(kicad_pcb
\t(version 20250926)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(4 "In1.Cu" power)
\t\t(6 "In2.Cu" power)
\t\t(2 "B.Cu" signal)
\t\t(25 "Edge.Cuts" user)
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4.4))
\t\t\t(layer "In1.Cu" (type "copper") (thickness 0.0175))
\t\t\t(layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4") (epsilon_r 4.6))
\t\t\t(layer "In2.Cu" (type "copper") (thickness 0.0175))
\t\t\t(layer "dielectric 3" (type "prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4.4))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
\t\t\t(copper_finish "ENIG")
\t\t)
\t)
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "+3V3")
\t(net 3 "/USB_DP")
\t(net 4 "/USB_DN")
\t(net 5 "/CLK")
\t(net 6 "/LED")
\t(net 7 "/SPI_MOSI")
\t(gr_rect (start 0 0) (end 40 30) (stroke (width 0.05) (type solid)) (fill no) (layer "Edge.Cuts"))
"""


def build() -> str:
    t = HEAD
    t += seg((2, 10.2), (20, 10.2), "F.Cu", 3)
    t += seg((5, 8), (5, 10.9), "F.Cu", 4)
    t += seg((5, 10.9), (15, 10.9), "F.Cu", 4)
    t += seg((2, 1.0), (38, 1.0), "F.Cu", 7)
    t += seg(LED_VIA, (10, 6), "F.Cu", 6)
    t += seg(LED_VIA, (10, 15), "B.Cu", 6)
    t += seg((10, 15), (30, 15), "B.Cu", 6)
    t += seg((5, 20), CLK_VIA, "F.Cu", 5)
    t += seg(CLK_VIA, (35, 25), "B.Cu", 5)
    t += via(LED_VIA, 6, "v-led")
    t += via(CLK_VIA, 5, "v-clk")
    t += via(GND_STITCH, 1, "v-gnd")
    t += zone(1, "GND", "In1.Cu", in1_fill, rect(0.5, 0.5, 39.5, 29.5), IN1_CLR)
    t += zone(2, "+3V3", "In2.Cu", in2_pwr, rect(0.5, 0.5, 20, 29.5), IN2_CLR)
    t += zone(1, "GND", "In2.Cu", in2_gnd, rect(20.5, 0.5, 39.5, 29.5), IN2_CLR)
    return t + ")\n"


PRO = {"meta": {"filename": "demo_4L.kicad_pro", "version": 3},
       "net_settings": {"meta": {"version": 4},
                        "classes": [{"name": "Default", "track_width": 0.2}, {"name": "USB", "track_width": 0.2},
                                    {"name": "Clock", "track_width": 0.2}],
                        "netclass_assignments": {"/CLK": ["Clock"]},
                        "netclass_patterns": [{"netclass": "USB", "pattern": "/USB_*"}]}}

if __name__ == "__main__":
    (HERE / "demo_4L.kicad_pcb").write_text(build())
    (HERE / "demo_4L.kicad_pro").write_text(json.dumps(PRO, indent=2) + "\n")
    print("wrote demo_4L.kicad_pcb, demo_4L.kicad_pro")
