"""Offline tests for the reference-plane checker (refplane/)."""

import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from refplane import cli  # noqa: E402
from refplane.check import Config, guess_ref_nets, guess_roles, parse_roles, run  # noqa: E402
from refplane.geometry import (Polygon, circle_interval, iv_complement, iv_intersect,  # noqa: E402
                               iv_subtract, iv_union)
from refplane.pcb import read_board  # noqa: E402
from refplane.project import load_netclasses, wildcard_match  # noqa: E402
from refplane.settings import (StitchRule, StitchRules, deep_merge, load_settings,  # noqa: E402
                               parse_class_overrides, stitch_rules)

FIX = ROOT / "tests" / "fixtures" / "refplane"
DEMO = FIX / "demo_4L.kicad_pcb"


def demo_config(board, **kw):
    roles = parse_roles("sig,gnd,pwr,sig", board)
    nets = {"/USB_DP", "/USB_DN", "/CLK", "/LED", "/SPI_MOSI"}
    nc = load_netclasses(DEMO.with_suffix(".kicad_pro"))
    return Config(roles=roles, gnd_nets={"GND"}, pwr_nets={"+3V3"}, nets=nets,
                  netclass_of={n: nc.classes_for(n) for n in board.nets}, **kw)


def rules_with(classes=None, nets=None):
    data, _ = load_settings()
    if classes:
        data["stitch"]["classes"].update(classes)
    if nets is not None:
        data["stitch"]["nets"] = nets
    return stitch_rules(data)


def kinds(res, net=None):
    return sorted((f.kind, f.net) for f in res.findings if net is None or f.net == net)


def poly_sexpr(pts):
    return "(pts " + " ".join(f"(xy {x} {y})" for x, y in pts) + ")"


def make_board(tmp: Path, body: str, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), h=(0.2, 1.0, 0.2)) -> Path:
    lay = "\n".join(f'({i} "{l}" signal)' for i, l in enumerate(layers))
    st = []
    for i, l in enumerate(layers):
        st.append(f'(layer "{l}" (type "copper") (thickness 0.035))')
        if i < len(h):
            st.append(f'(layer "dielectric {i + 1}" (type "prepreg") (thickness {h[i]}))')
    text = f"""(kicad_pcb (version 20250926) (generator "pcbnew")
      (layers {lay} (25 "Edge.Cuts" user))
      (setup (stackup {' '.join(st)}))
      (net 0 "") (net 1 "GND") (net 2 "SIG") (net 3 "+5V")
      (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))
      {body})"""
    path = tmp / "t.kicad_pcb"
    path.write_text(text)
    return path


def zone(net, name, layer, pts):
    return (f'(zone (net {net}) (net_name "{name}") (layer "{layer}") (connect_pads (clearance 0.2)) '
            f'(fill yes) (polygon {poly_sexpr(pts)}) (filled_polygon (layer "{layer}") {poly_sexpr(pts)}))')


SQUARE = [(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)]


class GeometryTests(unittest.TestCase):
    def test_intervals(self):
        self.assertEqual(iv_union([(0.5, 0.7), (0.1, 0.2), (0.15, 0.3)]), [(0.1, 0.3), (0.5, 0.7)])
        self.assertEqual(iv_intersect([(0, 0.5)], [(0.25, 1)]), [(0.25, 0.5)])
        self.assertEqual(iv_complement([(0.2, 0.4)]), [(0.0, 0.2), (0.4, 1.0)])
        self.assertEqual(iv_subtract([(0, 1)], [(0.2, 0.4)]), [(0.0, 0.2), (0.4, 1.0)])

    def test_fractured_hole(self):
        # 10x10 square with a 2x2 hole (opposite winding, as KiCad stores it) joined by a slit along y=5
        pts = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 5), (4, 5), (4, 6), (6, 6), (6, 4), (4, 4), (4, 5), (0, 5)]
        p = Polygon(pts)
        self.assertTrue(p.contains(2, 2))
        self.assertFalse(p.contains(5, 5.5))
        self.assertTrue(p.contains(2, 5.2))         # beside the slit is copper
        self.assertAlmostEqual(p.area, 96.0)
        # line across the slit: fully inside
        self.assertEqual(p.inside_intervals(2, 3, 2, 7), [(0.0, 1.0)])
        # line through the hole: inside, out (4..6), inside
        iv = p.inside_intervals(1, 5.5, 9, 5.5)
        self.assertEqual(len(iv), 2)
        self.assertAlmostEqual(iv[0][1] * 8 + 1, 4.0)
        self.assertAlmostEqual(iv[1][0] * 8 + 1, 6.0)
        # line starting outside the polygon
        iv = p.inside_intervals(-5, 2, 5, 2)
        self.assertAlmostEqual(iv[0][0], 0.5)

    def test_circle_interval(self):
        iv = circle_interval(0, 0, 10, 0, 5, 0, 1)
        self.assertAlmostEqual(iv[0][0], 0.4)
        self.assertAlmostEqual(iv[0][1], 0.6)
        self.assertEqual(circle_interval(0, 0, 10, 0, 5, 3, 1), [])


class ParseTests(unittest.TestCase):
    def test_read_demo(self):
        b = read_board(DEMO)
        self.assertEqual(b.copper_layers, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertAlmostEqual(b.h_between("F.Cu", "In1.Cu"), 0.2)
        self.assertAlmostEqual(b.h_between("In2.Cu", "In1.Cu"), 1.065)
        self.assertEqual(len(b.tracks), 9)
        self.assertEqual({z.net for z in b.zones}, {"GND", "+3V3"})
        self.assertEqual(b.tracks[0].net, "/USB_DP")

    def test_roles(self):
        b = read_board(DEMO)
        self.assertEqual(parse_roles("sig,gnd,pwr,sig", b)["In2.Cu"], "pwr")
        self.assertEqual(parse_roles("In2.Cu=mixed", b), {"In2.Cu": "hybrid"})
        self.assertEqual(parse_roles("L3=power", b), {"In2.Cu": "pwr"})
        with self.assertRaises(ValueError):
            parse_roles("sig,gnd", b)
        with self.assertRaises(ValueError):
            parse_roles("In9.Cu=gnd", b)
        roles, _ = guess_roles(b)
        self.assertEqual(roles["F.Cu"], "sig")
        self.assertEqual(roles["In1.Cu"], "gnd")
        self.assertEqual(guess_ref_nets(b, roles), ({"GND"}, {"+3V3"}))

    def test_netclasses(self):
        nc = load_netclasses(DEMO.with_suffix(".kicad_pro"))
        self.assertEqual(nc.classes_for("/USB_DP"), ["USB"])
        self.assertEqual(nc.classes_for("/CLK"), ["Clock"])
        self.assertEqual(nc.classes_for("/LED"), ["Default"])
        self.assertTrue(wildcard_match("/USB_*", "/USB_D+"))
        self.assertFalse(wildcard_match("USB_[DP]", "USB_D"))
        self.assertEqual(load_netclasses(None).classes, ["Default"])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.board = read_board(DEMO)
        self.res = run(self.board, demo_config(self.board))

    def finding(self, kind, net):
        found = [f for f in self.res.findings if f.kind == kind and f.net == net]
        self.assertEqual(len(found), 1, kinds(self.res))
        return found[0]

    def test_antipad_void_is_error(self):
        f = self.finding("no_reference", "/USB_DP")
        self.assertEqual(f.severity, "error")
        # round antipad r = 0.8 (32-gon), centre line 0.5 mm from the via: chord 2*sqrt(0.8^2-0.5^2) ~ 1.24
        self.assertAlmostEqual(f.length, 1.239, places=2)
        self.assertIn("void in GND", f.message)
        self.assertEqual(f.netclass, "USB")

    def test_split_crossing_and_pwr_reference(self):
        f = self.finding("no_reference", "/LED")
        self.assertAlmostEqual(f.length, 0.5, places=3)
        self.assertIn("split between +3V3 and GND", f.message)
        w = self.finding("pwr_reference", "/LED")
        self.assertEqual(w.severity, "warning")
        # 15.3 mm: the LED via's own antipad on In2 sits inside +3V3, so it counts as PWR-referenced
        self.assertAlmostEqual(w.length, 15.3, places=3)

    def test_slit_and_own_via_are_clean(self):
        self.assertEqual(kinds(self.res, "/CLK"), [])
        self.assertEqual(kinds(self.res, "/USB_DN"), [("reference_margin", "/USB_DN")])

    def test_margin_warnings(self):
        f = self.finding("reference_margin", "/SPI_MOSI")
        self.assertAlmostEqual(f.length, 36.0, places=3)
        tight = run(self.board, demo_config(self.board, margin_h=1.0))
        # a 0.2 mm band no longer reaches the plane edge (SPI_MOSI) or the antipad (USB_DN)
        self.assertFalse([x for x in tight.findings if x.kind == "reference_margin" and x.net != "/USB_DP"])

    def test_stats_and_no_stale(self):
        st = self.res.net_stats["/LED"]
        self.assertEqual(st["length"], 29.0)
        self.assertEqual(st["unreferenced"], 0.5)
        self.assertEqual(st["pwr_only"], 15.3)
        self.assertEqual(set(st["references"]), {"In1.Cu/GND", "In2.Cu/+3V3", "In2.Cu/GND"})
        self.assertEqual((st["transitions"], st["transitions_ok"]), (1, 0))
        clk = self.res.net_stats["/CLK"]
        self.assertEqual((clk["transitions"], clk["transitions_ok"]), (1, 1))
        self.assertFalse([f for f in self.res.findings if f.kind == "stale_fill"])

    def test_stale_fill_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = DEMO.read_text().rstrip().rstrip(")")
            text += '\t(via (at 15 25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 7))\n)\n'
            p = Path(tmp) / "stale.kicad_pcb"
            p.write_text(text)
            b = read_board(p)
            res = run(b, demo_config(b))
            stale = [f for f in res.findings if f.kind == "stale_fill"]
            self.assertEqual({f.layer for f in stale}, {"In1.Cu", "In2.Cu"})

    def test_no_adjacent_plane(self):
        cfg = demo_config(self.board)
        cfg.roles = parse_roles("sig,sig,pwr,sig", self.board)
        res = run(self.board, cfg)
        f = [x for x in res.findings if x.kind == "no_adjacent_plane"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].layer, "F.Cu")


class SyntheticTests(unittest.TestCase):
    def test_stripline_either_plane_is_enough(self):
        # In1 signal between F.Cu (GND left half) and In2 (GND right half): together they cover it.
        body = (zone(1, "GND", "F.Cu", [(0.5, 0.5), (10.5, 0.5), (10.5, 19.5), (0.5, 19.5)])
                + zone(1, "GND", "In2.Cu", [(9.5, 0.5), (19.5, 0.5), (19.5, 19.5), (9.5, 19.5)])
                + '(segment (start 2 10) (end 18 10) (width 0.2) (layer "In1.Cu") (net 2))')
        with tempfile.TemporaryDirectory() as tmp:
            b = read_board(make_board(Path(tmp), body))
            roles = parse_roles("hybrid,sig,gnd,sig", b)
            res = run(b, Config(roles=roles, gnd_nets={"GND"}, pwr_nets=set(), nets={"SIG"}))
            self.assertFalse([f for f in res.findings if f.kind == "no_reference"], kinds(res))
            plan = [p for p in res.plans if p.layer == "In1.Cu"][0]
            self.assertEqual([r for r, _, _ in plan.refs], ["F.Cu", "In2.Cu"])

    def test_arc_track_and_rotated_through_pad(self):
        # GND plane with an antipad around a SIG through pad at (15,10) from a rotated footprint
        import math
        ring = [(round(15 + 0.9 * math.sin(2 * math.pi * k / 32), 4), round(10 + 0.9 * math.cos(2 * math.pi * k / 32), 4))
                for k in range(32)]                      # round antipad r 0.9, entered from its bottom point
        hole = [(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (15, 19.5)] + ring + [ring[0], (15, 19.5), (0.5, 19.5)]
        body = (zone(1, "GND", "In1.Cu", hole)
                + '(footprint "X" (at 10 10 270) (pad "1" thru_hole circle (at 0 -5) (size 1 1) (drill 0.5) '
                  '(layers "*.Cu") (net 2 "SIG")))'
                + '(arc (start 3 10) (mid 6 7) (end 9 10) (width 0.2) (layer "F.Cu") (net 2))'
                + '(segment (start 9 10) (end 15 10) (width 0.2) (layer "F.Cu") (net 2))')
        with tempfile.TemporaryDirectory() as tmp:
            b = read_board(make_board(Path(tmp), body))
            pad = b.pads[0]
            self.assertAlmostEqual(pad.at[0], 15.0)   # (0,-5) rotated 270 deg (CCW on screen) about (10,10)
            self.assertAlmostEqual(pad.at[1], 10.0)
            self.assertGreater(max(len(t.points) for t in b.tracks), 3)
            res = run(b, Config(roles=parse_roles("sig,gnd,sig,sig", b), gnd_nets={"GND"}, pwr_nets=set(),
                                nets={"SIG"}))
            self.assertEqual(kinds(res), [], [f.message for f in res.findings])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_writes_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r"
            code, text, err = self.run_cli("check", str(DEMO), "--layers", "sig,gnd,pwr,sig", "--classes", "USB",
                                           "-y", "--refill", "no", "-o", str(out))
            self.assertEqual(code, 1)
            self.assertIn("/USB_DP", text)
            self.assertNotIn("/LED", text)
            data = json.loads((out / "report.json").read_text())
            self.assertEqual(data["setup"]["classes"], ["USB"])
            self.assertEqual(data["summary"]["error"], 1)
            self.assertTrue((out / "F_Cu.svg").exists())
            self.assertIn("--classes 'USB'", (out / "report.md").read_text() + data["setup"]["rerun"])

    def test_default_selection_skips_default_class(self):
        code, text, err = self.run_cli("check", str(DEMO), "-y", "--refill", "no", "--no-files", "--json")
        data = json.loads(text)
        self.assertEqual(set(data["setup"]["nets"]), {"/USB_DP", "/USB_DN", "/CLK"})
        self.assertIn("net classes", err)

    def test_info_json(self):
        code, text, _ = self.run_cli("info", str(DEMO), "--json")
        data = json.loads(text)
        self.assertEqual(data["layers"][1]["guess"], "gnd")
        self.assertEqual(data["netclasses"]["USB"]["nets"], 2)

    @unittest.skipIf(os.name == "nt", "shell script stub")
    def test_refill_uses_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            proj = tmp / "proj"
            proj.mkdir()
            board = proj / "demo_4L.kicad_pcb"
            board.write_text(DEMO.read_text())
            original = board.read_text()
            stub = tmp / "kicad-cli"
            stub.write_text(textwrap.dedent(f"""\
                #!{sys.executable}
                import sys, pathlib, time
                time.sleep(0.01)
                p = pathlib.Path(sys.argv[-1]); p.write_text(p.read_text())
                """))
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            code, text, _ = self.run_cli("check", str(board), "--layers", "sig,gnd,pwr,sig", "--classes", "all",
                                         "-y", "--refill", "yes", "--kicad-cli", str(stub), "--no-files", "--json")
            data = json.loads(text)
            self.assertIn("refilled", data["setup"]["fills"])
            self.assertEqual(board.read_text(), original)
            self.assertEqual(data["board"], str(board))


class TransitionTests(unittest.TestCase):
    """Reference continuity at layer changes (refplane/transitions.py)."""

    def board_from(self, tmp, text):
        p = Path(tmp) / "t.kicad_pcb"
        p.write_text(text)
        return read_board(p)

    def demo_text(self, drop=(), add=""):
        lines = [l for l in DEMO.read_text().splitlines(True) if not any(d in l for d in drop)]
        text = "".join(lines).rstrip().rstrip(")")
        return text + add + ")\n"

    def clk_findings(self, res):
        return [f for f in res.findings if f.net == "/CLK"]

    def test_gnd_to_gnd_with_stitch_via_is_clean(self):
        b = read_board(DEMO)
        self.assertEqual(self.clk_findings(run(b, demo_config(b))), [])

    def test_gnd_to_gnd_without_stitch_via(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.board_from(tmp, self.demo_text(drop=('"v-gnd"',)))
            f = self.clk_findings(run(b, demo_config(b)))
            self.assertEqual([x.kind for x in f], ["return_via_missing"])
            self.assertEqual(f[0].severity, "error")
            self.assertEqual(f[0].via_layers, ["F.Cu", "B.Cu"])
            self.assertIn("GND on In1.Cu -> GND on In2.Cu", f[0].message)
            self.assertIn("none within 5 mm", f[0].message)

    def test_stitch_via_too_far_reports_nearest(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.board_from(tmp, self.demo_text(
                drop=('"v-gnd"',),
                add='\t(via (at 31.5 20) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'))
            f = self.clk_findings(run(b, demo_config(b)))
            self.assertEqual(len(f), 1)
            self.assertIn("nearest connected one 1.50 mm", f[0].message)
            # a looser class rule accepts it
            res = run(b, demo_config(b, stitch=rules_with({"Clock": 2.0})))
            self.assertEqual(self.clk_findings(res), [])

    def test_per_class_distance_is_tighter(self):
        b = read_board(DEMO)
        res = run(b, demo_config(b, stitch=rules_with({"Clock": 0.5})))
        self.assertEqual([x.kind for x in self.clk_findings(res)], ["return_via_missing"])

    def test_blind_gnd_via_does_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = self.board_from(tmp, self.demo_text(
                drop=('"v-gnd"',),
                add='\t(via blind (at 30 20.9) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1))\n'))
            f = self.clk_findings(run(b, demo_config(b)))
            self.assertEqual(len(f), 1)
            self.assertIn("does not reach or is not tied into both", f[0].message)

    def test_gnd_to_pwr_is_net_change_warning(self):
        b = read_board(DEMO)
        res = run(b, demo_config(b))
        f = [x for x in res.findings if x.kind == "reference_net_change"]
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0].net, f[0].severity), ("/LED", "warning"))
        self.assertIn("GND on In1.Cu to +3V3 on In2.Cu", f[0].message)

    def test_shared_plane_needs_no_via(self):
        # F.Cu -> In2.Cu (sig): both reference In1 GND, so no return via is needed.
        body = (zone(1, "GND", "In1.Cu", SQUARE)
                + zone(1, "GND", "B.Cu", SQUARE)
                + '(segment (start 2 10) (end 10 10) (width 0.2) (layer "F.Cu") (net 2))'
                + '(segment (start 10 10) (end 18 10) (width 0.2) (layer "In2.Cu") (net 2))'
                + '(via (at 10 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))')
        with tempfile.TemporaryDirectory() as tmp:
            b = read_board(make_board(Path(tmp), body))
            res = run(b, Config(roles=parse_roles("sig,gnd,sig,gnd", b), gnd_nets={"GND"}, pwr_nets=set(),
                                nets={"SIG"}))
            self.assertFalse([f for f in res.findings if f.via_layers], [f.message for f in res.findings])
            self.assertEqual(res.net_stats["SIG"]["transitions_ok"], 1)
            # In2 is a stripline between In1 and B.Cu: both counted as references
            self.assertEqual(set(res.net_stats["SIG"]["references"]), {"In1.Cu/GND", "B.Cu/GND"})

    def test_single_layer_via_is_not_a_transition(self):
        b = read_board(DEMO)
        res = run(b, demo_config(b))
        self.assertEqual(res.net_stats["/USB_DP"]["transitions"], 0)

    def test_cli_stitch_flags(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli.main(["check", str(DEMO), "--layers", "sig,gnd,pwr,sig", "--classes", "all", "-y",
                      "--refill", "no", "--no-files", "--json", "--stitch-class", "Clock=0.5"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["setup"]["stitch_class"], {"Clock": 0.5})
        self.assertEqual(data["setup"]["stitch_profiles"]["low_speed"]["max_share"], 4)
        self.assertIn("--stitch-class 'Clock=0.5'", data["setup"]["rerun"])
        self.assertTrue(any(f["kind"] == "return_via_missing" for f in data["findings"]))


class SettingsTests(unittest.TestCase):
    def test_defaults_profiles_and_patterns(self):
        r = rules_with()
        self.assertEqual(r.rule_for("/I2C1_SCL", ["Default"]).name, "low_speed")
        self.assertEqual(r.rule_for("/MCU/SPI2_MISO", ["Default"]).name, "low_speed")
        self.assertEqual(r.rule_for("UART4_TX", ["Default"]).name, "low_speed")
        self.assertEqual(r.rule_for("/ETH_TX", ["Default"]).name, "high_speed")     # no generic *_TX / *_RX
        self.assertEqual(r.rule_for("/SERDES_RX", ["Default"]).name, "high_speed")
        self.assertEqual(r.rule_for("/CLK", ["Clock"]).name, "high_speed")
        self.assertEqual(r.rule_for("/USB_DP", ["USB"]).name, "high_speed")
        self.assertEqual(r.rule_for("/USB_DP", ["USB"]).severity, "error")
        self.assertEqual(r.rule_for("/I2C1_SCL", ["Default"]).severity, "warning")

    def test_class_beats_pattern_beats_default_class(self):
        r = rules_with({"Fast": "high_speed", "Default": 2.5})
        self.assertEqual(r.rule_for("/I2C_FAST", ["Fast"]).name, "high_speed")   # explicit class wins
        self.assertEqual(r.rule_for("/I2C_X", ["Default"]).name, "low_speed")     # pattern beats Default
        d = r.rule_for("/FOO", ["Default"])                                        # then Default class
        self.assertEqual((d.distance_mm, d.max_share, d.severity), (2.5, 2, "error"))

    def test_strictest_of_several_classes(self):
        r = rules_with({"A": "low_speed", "B": 0.6})
        self.assertEqual(r.rule_for("/X", ["A", "B"]).distance_mm, 0.6)

    def test_bad_values(self):
        with self.assertRaises(ValueError):
            rules_with({"USB": "warp_speed"})
        data, _ = load_settings()
        data["stitch"]["profiles"]["low_speed"]["severity"] = "fatal"
        with self.assertRaises(ValueError):
            stitch_rules(data)
        self.assertEqual(parse_class_overrides("USB=0.6, Default=low_speed"), {"USB": 0.6, "Default": "low_speed"})
        with self.assertRaises(ValueError):
            parse_class_overrides("USB")

    def test_board_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "x.kicad_pcb"
            board.write_text("(kicad_pcb)")
            (Path(tmp) / "x.refplane.json").write_text(json.dumps({
                "stitch": {"profiles": {"low_speed": {"distance_mm": 2.0}}, "nets": {"*DBG*": "low_speed"}},
                "margin_h": 2}))
            data, sources = load_settings(board)
            self.assertEqual(len(sources), 2)
            self.assertEqual(data["margin_h"], 2)
            low = data["stitch"]["profiles"]["low_speed"]
            self.assertEqual((low["distance_mm"], low["max_share"]), (2.0, 4))      # merged, not replaced
            self.assertEqual(list(data["stitch"]["nets"])[0], "*DBG*")             # user patterns first
            self.assertIn("*I2C*", data["stitch"]["nets"])

    def test_deep_merge_ignores_comments(self):
        self.assertEqual(deep_merge({"a": 1}, {"_about": "x", "a": 2}), {"a": 2})


def shared_board(tmp, xs, names, gnd_vias):
    nets = "".join(f'(net {10 + i} "{n}")' for i, n in enumerate(names))
    body = nets + zone(1, "GND", "In1.Cu", SQUARE) + zone(1, "GND", "In2.Cu", SQUARE)
    for i, (x, n) in enumerate(zip(xs, names)):
        body += (f'(segment (start {x} 10) (end {x} 4) (width 0.2) (layer "F.Cu") (net {10 + i}))'
                 f'(segment (start {x} 10) (end {x} 16) (width 0.2) (layer "B.Cu") (net {10 + i}))'
                 f'(via (at {x} 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {10 + i}))')
    for gx, gy in gnd_vias:
        body += f'(via (at {gx} {gy}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
    return read_board(make_board(Path(tmp), body))


class SharedReturnViaTests(unittest.TestCase):
    def run_shared(self, xs, names, gnd_vias, stitch=None):
        with tempfile.TemporaryDirectory() as tmp:
            b = shared_board(tmp, xs, names, gnd_vias)
            res = run(b, Config(roles=parse_roles("sig,gnd,gnd,sig", b), gnd_nets={"GND"}, pwr_nets=set(),
                                nets=set(names), stitch=stitch))
            return [f for f in res.findings if f.kind in ("return_via_missing", "return_via_shared")], res

    def test_low_speed_nets_share_one_gnd_via(self):
        f, res = self.run_shared([4, 5.5, 7], ["I2C1_SCL", "SPI2_MISO", "UART4_TX"], [(4.75, 10)])
        self.assertEqual(f, [])
        self.assertEqual(res.net_stats["UART4_TX"]["stitch_profile"], "low_speed")
        self.assertEqual(res.net_stats["UART4_TX"]["transitions_ok"], 1)

    def test_too_many_on_one_via_is_warning(self):
        rule = StitchRules(profiles={"p": StitchRule("p", 5.0, 4, "warning")}, default_profile="p")
        names = ["N1", "N2", "N3", "N4", "N5"]
        f, _ = self.run_shared([4, 5.5, 7, 8.5, 10], names, [(7.75, 10)], stitch=rule)
        self.assertEqual([x.kind for x in f], ["return_via_shared"])
        self.assertEqual(f[0].severity, "warning")
        self.assertIn("return via for 5 signal vias", f[0].message)
        self.assertIn("allows 4", f[0].message)

    def test_load_is_spread_over_available_vias(self):
        rule = StitchRules(profiles={"p": StitchRule("p", 5.0, 2, "error")}, default_profile="p")
        names = ["N1", "N2", "N3", "N4"]
        f, _ = self.run_shared([4, 5.5, 7, 8.5], names, [(4.75, 10), (7.75, 10)], stitch=rule)
        self.assertEqual(f, [])
        f, _ = self.run_shared([4, 5.5, 7, 8.5], names, [(4.75, 10)], stitch=rule)
        self.assertEqual([x.kind for x in f], ["return_via_shared"])

    def test_high_speed_default_pair_ok_third_missing(self):
        f, _ = self.run_shared([4, 5.5, 7], ["HS_P", "HS_N", "HS_CLK"], [(4.75, 10)])
        self.assertEqual([(x.kind, x.net, x.severity) for x in f], [("return_via_missing", "HS_CLK", "error")])

    def test_low_speed_missing_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "".join(l for l in DEMO.read_text().splitlines(True) if '"v-gnd"' not in l)
            p = Path(tmp) / "t.kicad_pcb"
            p.write_text(text)
            b = read_board(p)
            res = run(b, demo_config(b, stitch=rules_with({"Clock": "low_speed"})))
            f = [x for x in res.findings if x.net == "/CLK"]
            self.assertEqual([(x.kind, x.severity) for x in f], [("return_via_missing", "warning")])
            self.assertIn("(low_speed)", f[0].message)


class SavedConfigTests(unittest.TestCase):
    def copy_demo(self, tmp):
        for suffix in (".kicad_pcb", ".kicad_pro"):
            (Path(tmp) / f"demo_4L{suffix}").write_text(DEMO.with_suffix(suffix).read_text())
        return Path(tmp) / "demo_4L.kicad_pcb"

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_save_then_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = self.copy_demo(tmp)
            self.cli("check", str(board), "--layers", "sig,gnd,pwr,sig", "--classes", "Clock", "-y",
                     "--refill", "no", "--no-files", "--stitch-class", "Clock=low_speed", "--save-config")
            saved = json.loads((Path(tmp) / "demo_4L.refplane.json").read_text())
            self.assertEqual(saved["classes"], ["Clock"])
            self.assertEqual(saved["stitch"]["classes"], {"Clock": "low_speed"})
            code, text, err = self.cli("check", str(board), "-y", "--refill", "no", "--no-files", "--json")
            data = json.loads(text)
            self.assertEqual(data["setup"]["nets"], ["/CLK"])
            self.assertEqual(data["setup"]["roles"], "F.Cu=sig,In1.Cu=gnd,In2.Cu=pwr,B.Cu=sig")
            self.assertEqual(data["nets"]["/CLK"]["stitch_profile"], "low_speed")
            self.assertNotIn("layer roles guessed", err)
            self.assertTrue(any(s.endswith("demo_4L.refplane.json") for s in data["setup"]["settings_files"]))

    def test_interactive_prompts_and_save(self):
        import builtins
        answers = iter(["sig,gnd,pwr,sig", "", "", "3", "", "Clock=0.5", "y"])
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        with tempfile.TemporaryDirectory() as tmp:
            board = self.copy_demo(tmp)
            orig_input, orig_tty = builtins.input, sys.stdin.isatty
            builtins.input, sys.stdin.isatty = fake_input, (lambda: True)
            try:
                code, text, _ = self.cli("check", str(board), "--refill", "no", "--no-files")
                # second run: saved answers, no questions
                prompts.clear()
                code2, text2, _ = self.cli("check", str(board), "--refill", "no", "--no-files")
            finally:
                builtins.input, sys.stdin.isatty = orig_input, orig_tty
            self.assertIn("Return-via rules", text)
            self.assertIn("Clock              1 net(s) -> high_speed (default)", text)
            self.assertEqual(prompts, [])
            self.assertIn("using saved answers", text2)
            saved = json.loads((Path(tmp) / "demo_4L.refplane.json").read_text())
            self.assertEqual(saved["classes"], ["Clock"])
            self.assertEqual(saved["stitch"]["classes"], {"Clock": 0.5})
            self.assertIn("return_via_missing", text2)   # 0.9 mm stitch via > 0.5 mm rule


class HighSpeedListTests(unittest.TestCase):
    """Explicit mode: the user names the high-speed nets, everything else is low speed."""

    def test_rules(self):
        data, _ = load_settings()
        data["stitch"]["high_speed"] = ["USB", "*CLK*"]
        r = stitch_rules(data)
        self.assertTrue(r.explicit)
        self.assertEqual(r.rule_for("/USB_DP", ["USB"]).name, "high_speed")       # by class
        self.assertEqual(r.rule_for("/MCU/CLK", ["Default"]).name, "high_speed")  # by net pattern
        self.assertEqual(r.rule_for("/ETH_TX", ["Default"]).name, "low_speed")    # not listed -> low
        self.assertEqual(r.rule_for("/I2C1_SCL", ["Default"]).name, "low_speed")
        self.assertEqual(r.source_for("/ETH_TX", ["Default"]), "not listed")
        data["stitch"]["classes"] = {"USB": 0.6}
        r = stitch_rules(data)
        self.assertEqual(r.rule_for("/USB_DP", ["USB"]).distance_mm, 0.6)         # class rule still wins

    def test_unknown_profile_name(self):
        data, _ = load_settings()
        data["stitch"]["high_speed"] = ["USB"]
        data["stitch"]["low_speed_profile"] = "slow"
        with self.assertRaises(ValueError):
            stitch_rules(data)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_flag_and_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "demo_4L.kicad_pcb"
            board.write_text("".join(l for l in DEMO.read_text().splitlines(True) if '"v-gnd"' not in l))
            (Path(tmp) / "demo_4L.kicad_pro").write_text(DEMO.with_suffix(".kicad_pro").read_text())
            base = ["check", str(board), "--layers", "sig,gnd,pwr,sig", "--classes", "all", "-y",
                    "--refill", "no", "--no-files", "--json"]
            # pattern mode: /CLK is high speed -> missing return via is an error
            data = json.loads(self.cli(*base)[1])
            clk = [f for f in data["findings"] if f["net"] == "/CLK"]
            self.assertEqual(clk[0]["severity"], "error")
            # explicit mode, only USB listed: /CLK becomes low speed -> warning
            code, text, _ = self.cli(*base, "--high-speed", "USB", "--save-config")
            data = json.loads(text.split("saved:")[0]) if text.startswith("{") else json.loads(text[text.index("{"):])
            clk = [f for f in data["findings"] if f["net"] == "/CLK"]
            self.assertEqual((clk[0]["kind"], clk[0]["severity"]), ("return_via_missing", "warning"))
            self.assertEqual(data["setup"]["stitch_mode"], "explicit high-speed list")
            self.assertEqual(data["nets"]["/USB_DP"]["stitch_profile"], "high_speed")
            self.assertEqual(data["nets"]["/LED"]["stitch_profile"], "low_speed")
            self.assertIn("--high-speed 'USB'", data["setup"]["rerun"])
            saved = json.loads((Path(tmp) / "demo_4L.refplane.json").read_text())
            self.assertEqual(saved["stitch"]["high_speed"], ["USB"])
            # '-' switches back to name patterns
            data = json.loads(self.cli(*base, "--high-speed", "-")[1])
            self.assertEqual(data["setup"]["stitch_mode"], "name patterns")

    def test_interactive_answer(self):
        import builtins
        answers = iter(["sig,gnd,pwr,sig", "", "", "all", "USB,*CLK*", "", "y"])
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in (".kicad_pcb", ".kicad_pro"):
                (Path(tmp) / f"demo_4L{suffix}").write_text(DEMO.with_suffix(suffix).read_text())
            board = Path(tmp) / "demo_4L.kicad_pcb"
            orig_input, orig_tty = builtins.input, sys.stdin.isatty
            builtins.input, sys.stdin.isatty = (lambda prompt="": next(answers)), (lambda: True)
            try:
                code, text, _ = self.cli("check", str(board), "--refill", "no", "--no-files")
            finally:
                builtins.input, sys.stdin.isatty = orig_input, orig_tty
            self.assertIn("low_speed (not listed)", text)
            self.assertIn("high_speed (listed high speed)", text)
            saved = json.loads((Path(tmp) / "demo_4L.refplane.json").read_text())
            self.assertEqual(saved["stitch"]["high_speed"], ["USB", "*CLK*"])


class RealBoardFixTests(unittest.TestCase):
    """Fixes found by running on a real KiCad 10 board (earth sensor, 2026-10-26)."""

    def test_edge_only_void_is_warning_with_depth(self):
        # Void reaches 0.05 mm into a 0.2 mm trace from its lower edge; centre line still over GND.
        hole = [(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (10, 19.5), (10, 10.05), (11, 10.05), (11, 9.5),
                (9, 9.5), (9, 10.05), (10, 10.05), (10, 19.5), (0.5, 19.5)]
        body = zone(1, "GND", "In1.Cu", hole) + '(segment (start 2 10.1) (end 18 10.1) (width 0.2) (layer "F.Cu") (net 2))'
        with tempfile.TemporaryDirectory() as tmp:
            b = read_board(make_board(Path(tmp), body))
            res = run(b, Config(roles=parse_roles("sig,gnd,sig,sig", b), gnd_nets={"GND"}, pwr_nets=set(),
                                nets={"SIG"}, margin_h=0.1))
            k = [(f.kind, f.severity) for f in res.findings]
            self.assertNotIn(("no_reference", "error"), k)
            f = [x for x in res.findings if x.kind == "reference_edge"]
            self.assertEqual(len(f), 1)
            self.assertEqual(f[0].severity, "warning")
            self.assertAlmostEqual(f[0].length, 2.0, places=3)
            self.assertRegex(f[0].message, r"about (5\d|6\d) um into the 0.20 mm trace")
            self.assertEqual(res.net_stats["SIG"]["edge_only"], 2.0)

    def test_centre_over_void_is_still_error(self):
        b = read_board(DEMO)
        res = run(b, demo_config(b))
        self.assertEqual([f.severity for f in res.findings if f.net == "/USB_DP" and f.kind == "no_reference"],
                         ["error"])

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def power_board(self, tmp):
        body = (zone(1, "GND", "In1.Cu", SQUARE) + zone(1, "GND", "In2.Cu", SQUARE)
                + '(net 4 "/VCC_IO") (net 5 "/I2C_SDA")'
                + '(segment (start 2 5) (end 18 5) (width 0.5) (layer "F.Cu") (net 3))'      # +5V
                + '(segment (start 2 8) (end 18 8) (width 0.5) (layer "F.Cu") (net 4))'      # /VCC_IO
                + '(segment (start 2 11) (end 18 11) (width 0.2) (layer "F.Cu") (net 5))')   # signal
        return make_board(Path(tmp), body)

    def test_power_rails_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = self.power_board(tmp)
            base = ["check", str(board), "--layers", "sig,gnd,gnd,sig", "--classes", "all", "-y", "--refill", "no",
                    "--no-files", "--json"]
            code, text, err = self.cli(*base)
            data = json.loads(text)
            self.assertEqual(data["setup"]["nets"], ["/I2C_SDA"])
            self.assertEqual(data["setup"]["skipped_power"], ["+5V", "/VCC_IO"])
            self.assertIn("power nets skipped", err)
            data = json.loads(self.cli(*base, "--include-power")[1])
            self.assertEqual(set(data["setup"]["nets"]), {"+5V", "/VCC_IO", "/I2C_SDA"})
            data = json.loads(self.cli(*base, "--nets", "+5V")[1])        # naming a rail checks it
            self.assertIn("+5V", data["setup"]["nets"])
            self.assertEqual(data["setup"]["skipped_power"], ["/VCC_IO"])

    def test_svg_draws_vias_pads_and_legend(self):
        from refplane import report
        b = read_board(DEMO)
        res = run(b, demo_config(b))
        svg = report.to_svg(res, b, "F.Cu", ["In1.Cu"], {"/CLK"}, {"GND"}, {"+3V3"})
        self.assertIn("F.Cu over In1.Cu", svg)
        self.assertEqual(svg.count("<title>via "), 3)                 # LED, CLK, GND stitch via
        self.assertIn('stroke="#2e7d32"', svg)                         # GND via outlined green
        self.assertIn("trace edge over a void (warning)", svg)
        vb = [float(x) for x in svg.split('viewBox="')[1].split('"')[0].split()]
        self.assertGreaterEqual(vb[2], 40.0)                           # wide enough for the board


class ReviewFindingTests(unittest.TestCase):
    """Codex review 2026-10-26: F1 direction dependence, F2 unrelated via waiving a gap."""

    SQ = [(0, 0), (10, 0), (10, 10), (0, 10)]
    # 10x10 with a 2x2 hole at 4..6, entered by a slit along y = 5 from x = 0
    HOLED = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 5), (4, 5), (4, 6), (6, 6), (6, 4), (4, 4), (4, 5), (0, 5)]

    def both_ways(self, poly, a, b):
        p = Polygon(poly)
        fwd = p.inside_intervals(*a, *b)
        rev = [(1 - e, 1 - s) for s, e in reversed(p.inside_intervals(*b, *a))]
        self.assertEqual([(round(x, 9), round(y, 9)) for x, y in fwd],
                         [(round(x, 9), round(y, 9)) for x, y in rev], (a, b))
        return fwd

    def test_outer_boundary_start_and_end(self):
        self.assertEqual(self.both_ways(self.SQ, (0, 5), (-5, 5)), [])          # leaves the plane
        self.assertEqual(self.both_ways(self.SQ, (0, 5), (5, 5)), [(0.0, 1.0)])  # enters the plane
        self.assertEqual(self.both_ways(self.SQ, (10, 5), (15, 5)), [])
        self.assertEqual(self.both_ways(self.SQ, (0, 0), (-3, -3)), [])         # from a corner, outwards
        self.assertEqual(self.both_ways(self.SQ, (0, 0), (3, 3)), [(0.0, 1.0)])  # from a corner, inwards
        iv = self.both_ways(self.SQ, (-5, 5), (15, 5))                           # through, both edges
        self.assertAlmostEqual(iv[0][0], 0.25)
        self.assertAlmostEqual(iv[0][1], 0.75)

    def test_hole_boundary_start_and_end(self):
        self.assertEqual(self.both_ways(self.HOLED, (4, 5.5), (5, 5.5)), [])           # into the hole
        self.assertEqual(self.both_ways(self.HOLED, (4, 5.5), (2, 5.5)), [(0.0, 1.0)])  # out into copper
        self.assertEqual(self.both_ways(self.HOLED, (6, 4.5), (8, 4.5)), [(0.0, 1.0)])
        self.assertEqual(self.both_ways(self.HOLED, (6, 4.5), (4, 4.5)), [])           # across the hole

    def run_board(self, body, roles="sig,gnd,sig,sig"):
        with tempfile.TemporaryDirectory() as tmp:
            b = read_board(make_board(Path(tmp), body))
            res = run(b, Config(roles=parse_roles(roles, b), gnd_nets={"GND"}, pwr_nets=set(), nets={"SIG"}))
            return res, res.net_stats.get("SIG", {})

    def test_trace_leaving_plane_edge_same_both_directions(self):
        plane = zone(1, "GND", "In1.Cu", self.SQ)
        out = '(segment (start 0 5) (end -5 5) (width 0.2) (layer "F.Cu") (net 2))'
        back = '(segment (start -5 5) (end 0 5) (width 0.2) (layer "F.Cu") (net 2))'
        r1, s1 = self.run_board(plane + out)
        r2, s2 = self.run_board(plane + back)
        self.assertEqual(s1["unreferenced"], 5.0)
        self.assertEqual(s2["unreferenced"], 5.0)
        self.assertEqual([f.kind for f in r1.findings if f.net == "SIG"], ["no_reference"])

    VOID = [(0, 0), (10, 0), (10, 10), (5, 10), (5, 5.3), (5.3, 5.3), (5.3, 4.7), (4.7, 4.7), (4.7, 5.3),
            (5, 5.3), (5, 10), (0, 10)]
    TRACE = '(segment (start 2 5) (end 8 5) (width 0.2) (layer "F.Cu") (net 2))'

    def test_unrelated_blind_via_does_not_waive_gap(self):
        base = zone(1, "GND", "In1.Cu", self.VOID) + self.TRACE
        _, st = self.run_board(base)
        self.assertEqual(st["unreferenced"], 0.6)
        _, st = self.run_board(base + '(via blind (at 5 5) (size 0.3) (drill 0.15) (layers "In2.Cu" "B.Cu") (net 2))')
        self.assertEqual(st["unreferenced"], 0.6)

    def test_same_net_via_the_trace_does_not_end_on_does_not_waive_gap(self):
        base = zone(1, "GND", "In1.Cu", self.VOID) + self.TRACE
        _, st = self.run_board(base + '(via (at 5 5) (size 0.3) (drill 0.15) (layers "F.Cu" "B.Cu") (net 2))')
        self.assertEqual(st["unreferenced"], 0.6)

    def test_connected_via_waives_only_its_own_antipad(self):
        # Trace ends on a through via inside the void: that antipad is expected.
        body = (zone(1, "GND", "In1.Cu", self.VOID)
                + '(segment (start 2 5) (end 5 5) (width 0.2) (layer "F.Cu") (net 2))'
                + '(via (at 5 5) (size 0.3) (drill 0.15) (layers "F.Cu" "B.Cu") (net 2))')
        _, st = self.run_board(body)
        self.assertEqual(st["unreferenced"], 0.0)

    def test_connected_via_not_reaching_the_reference_layer_waives_nothing(self):
        # In1 signal referenced by In2 only; blind via F.Cu-In1 at the trace end stops above In2.
        hole2 = [(0, 0), (10, 0), (10, 10), (5, 10), (5, 5.3), (5.3, 5.3), (5.3, 4.7), (4.7, 4.7), (4.7, 5.3),
                 (5, 5.3), (5, 10), (0, 10)]
        body = (zone(1, "GND", "In2.Cu", hole2)
                + '(segment (start 2 5) (end 5 5) (width 0.2) (layer "In1.Cu") (net 2))'
                + '(via blind (at 5 5) (size 0.3) (drill 0.15) (layers "F.Cu" "In1.Cu") (net 2))')
        _, st = self.run_board(body, roles="sig,sig,gnd,sig")
        self.assertAlmostEqual(st["unreferenced"], 0.3, places=3)
        # the same via as a through via reaches In2: then its antipad is expected
        _, st = self.run_board(body.replace('via blind', 'via').replace('"F.Cu" "In1.Cu"', '"F.Cu" "B.Cu"'),
                               roles="sig,sig,gnd,sig")
        self.assertEqual(st["unreferenced"], 0.0)

    def test_other_plane_clearance_does_not_widen_exemption(self):
        # Codex follow-up 2026-10-26: a 2 mm netclass clearance on +HV (In2, not adjacent to F.Cu)
        # must not widen the GND antipad exemption on In1 and swallow the unrelated gap at x = 6..7.
        from refplane.pcb import Board, Track, Via, Zone

        def holed(holes):
            pts = [(0, 0), (20, 0), (20, 20), (0, 20)]
            for x0, y0, x1, y1 in holes:
                pts += [(0, y0), (x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0), (0, y0)]
            return pts

        board = Board(Path("synthetic.kicad_pcb"), copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                      nets=["SIG", "GND", "+HV"],
                      tracks=[Track("SIG", "F.Cu", 0.2, [(5, 5), (9, 5)])],
                      vias=[Via("SIG", (5, 5), 0.6, ("F.Cu", "B.Cu"))],
                      zones=[Zone("GND", ["In1.Cu"], {"In1.Cu": [holed([(4.5, 4.5, 5.5, 5.5), (6, 4.5, 7, 5.5)])]},
                                  clearance=0.2),
                             Zone("+HV", ["In2.Cu"], {"In2.Cu": [holed([(2.7, 2.7, 7.3, 7.3)])]}, clearance=0.2)])
        for hv in (0.2, 2.0):
            cfg = Config(roles={"F.Cu": "sig", "In1.Cu": "gnd", "In2.Cu": "pwr", "B.Cu": "sig"},
                         gnd_nets={"GND"}, pwr_nets={"+HV"}, nets={"SIG"},
                         net_clearance={"GND": 0.2, "SIG": 0.2, "+HV": hv})
            res = run(board, cfg)
            self.assertAlmostEqual(res.net_stats["SIG"]["unreferenced"], 1.0, places=6, msg=f"+HV clearance {hv}")
            self.assertIn("no_reference", [f.kind for f in res.findings])


if __name__ == "__main__":
    unittest.main()
