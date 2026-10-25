"""Offline tests for jlcpcb_rules (KiCad 10 custom rules generated from JLCPCB capabilities)."""

import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

from jlcpcb_rules import Profile, load_capabilities, render, render_file
from jlcpcb_rules import preflight
from jlcpcb_rules.board import parse_sexpr, read_board, thickness_to_oz
from jlcpcb_rules.cli import main
from jlcpcb_rules.generator import BEGIN_MARK, END_MARK, read_profile_line, split_existing
from jlcpcb_rules.profile import profile_from_board

FIXTURE = Path(__file__).parent / "fixtures" / "jlcpcb" / "castellated_4L.kicad_pcb"

# Taken from the KiCad 10.0 sources (drc_rule_parser.cpp, property registrations,
# pcbexpr_functions.cpp). Generated rules may only use these.
KICAD10_CONSTRAINTS = {
    "assertion", "clearance", "creepage", "hole_clearance", "edge_clearance", "hole_size",
    "hole_to_hole", "courtyard_clearance", "silk_clearance", "text_height", "text_thickness",
    "track_width", "track_angle", "track_segment_length", "connection_width", "annular_width",
    "via_diameter", "via_dangling", "zone_connection", "thermal_relief_gap",
    "thermal_spoke_width", "min_resolved_spokes", "solder_mask_expansion", "solder_mask_sliver",
    "solder_paste_abs_margin", "solder_paste_rel_margin", "disallow", "length", "skew",
    "via_count", "diff_pair_gap", "diff_pair_uncoupled", "physical_clearance",
    "physical_hole_clearance", "bridged_mask",
}
KICAD10_DISALLOW = {"track", "via", "through_via", "blind_via", "buried_via", "micro_via", "pad",
                    "zone", "text", "graphic", "hole", "footprint"}
KICAD10_PROPERTIES = {
    "type", "layer", "netname", "netclass", "pad type", "pad shape", "fabrication property",
    "hole size x", "hole size y", "size x", "size y", "hole", "diameter", "via type",
    "filling", "capping", "front plugging", "back plugging", "backdrill mode",
    "auto thickness", "thickness", "height", "line width",
}
KICAD10_FUNCTIONS = {"isPlated", "isMicroVia", "isBlindVia", "isBuriedVia", "hasNetclass",
                     "intersectsArea", "existsOnLayer"}
ENUM_VALUES = {
    "Type": {"Pad", "Via", "Track", "Text", "Text Box", "Graphic", "Zone", "Footprint"},
    "Pad_Type": {"Through-hole", "SMD", "Edge connector", "NPTH, mechanical"},
    "Fabrication_Property": {"BGA pad", "Castellated pad", "Press-fit pad", "Heatsink pad"},
    "Pad_Shape": {"Custom", "Circle", "Rectangle", "Oval"},
    "Via_Type": {"Through", "Blind", "Buried", "Micro"},
    "Filling": {"Filled", "Not filled", "From board stackup"},
    "Capping": {"Capped", "Not capped", "From board stackup"},
    "Front_plugging": {"Plugged"},
    "Back_plugging": {"Plugged"},
    "Backdrill_Mode": {"No backdrill"},
    "Layer": {"F.Silkscreen", "B.Silkscreen"},
}

PROFILES = [
    Profile(layers=1),
    Profile(layers=2),
    Profile(layers=2, outer_oz=2, finish="HASL_LF"),
    Profile(layers=2, outer_oz=4.5, mask_color="white"),
    Profile(layers=4, finish="ENIG", thickness=0.8),
    Profile(layers=6, finish="ENIG", tier="standard", mask_color="black"),
    Profile(layers=8, tier="advanced", accept=["small_via"]),
    Profile(layers=12, thickness=2.5, finish="OSP"),
]


def rules_of(text):
    """Return each top-level (rule ...) S-expression as text."""
    out, i = [], 0
    while (j := text.find("(rule ", i)) >= 0:
        depth, k, inq = 0, j, False
        while True:
            ch = text[k]
            if ch == '"' and text[k - 1] != "\\":
                inq = not inq
            elif not inq and ch == "(":
                depth += 1
            elif not inq and ch == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(text[j:k + 1])
        i = k + 1
    return out


def strip_comments(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class SyntaxTests(unittest.TestCase):
    def test_every_profile_renders_valid_kicad10_rules(self):
        for prof in PROFILES:
            with self.subTest(profile=prof.key()):
                text = render(prof, today="2026-01-01")
                body = strip_comments(text)
                tree = parse_sexpr(body)
                self.assertEqual(tree[0], ["version", "1"], "version clause must come first")
                names = [r[1] for r in tree[1:]]
                self.assertEqual(len(names), len(set(names)), "rule names must be unique")
                for rule in tree[1:]:
                    self.assertEqual(rule[0], "rule")
                    for clause in rule[2:]:
                        self.assertIn(clause[0], {"constraint", "condition", "layer", "severity"})
                        if clause[0] == "severity":
                            self.assertIn(clause[1], {"error", "warning", "ignore", "exclusion"})
                        if clause[0] == "constraint":
                            self.assertIn(clause[1], KICAD10_CONSTRAINTS)
                            if clause[1] == "disallow":
                                self.assertTrue(set(clause[2:]) <= KICAD10_DISALLOW)
                        if clause[0] == "layer":
                            self.assertRegex(clause[1], r"^(outer|inner|\*\.Cu|\?\.Silkscreen)$")
                self._check_expressions(body)

    def _check_expressions(self, body):
        exprs = re.findall(r'\(condition "([^"]*)"\)', body)
        exprs += re.findall(r'\(constraint assertion "([^"]*)"\)', body)
        self.assertTrue(exprs)
        for e in exprs:
            self.assertEqual(e.count("("), e.count(")"), e)
            bare = re.sub(r"'[^']*'", "''", e)
            for prop in re.findall(r"\b[AB]\.([A-Za-z_]+)\b(?!\()", bare):
                self.assertIn(prop.replace("_", " ").lower(), KICAD10_PROPERTIES, prop)
            for fn in re.findall(r"\b[AB]\.([A-Za-z_]+)\(", e):
                self.assertIn(fn, KICAD10_FUNCTIONS, fn)
            for prop, val in re.findall(r"\b[AB]\.([A-Za-z_]+) [!=]= '([^']*)'", e):
                self.assertIn(prop, ENUM_VALUES, prop)
                self.assertIn(val, ENUM_VALUES[prop], f"{prop} == '{val}'")

    def test_no_rule_without_severity_except_permissive(self):
        for prof in PROFILES:
            for r in rules_of(render(prof)):
                if "(severity" not in r:
                    self.assertIn("min -1mm", r)

    def test_assertions_do_not_rely_on_layer_clause(self):
        # KiCad 10 ignores (layer) for assertions; the layer test must be in the condition.
        for prof in PROFILES:
            for r in rules_of(render(prof)):
                if "constraint assertion" in r:
                    self.assertNotIn("(layer", r)


class ValueTests(unittest.TestCase):
    def rules(self, **kw):
        return render(Profile(**kw))

    def test_trace_space_by_stackup(self):
        self.assertIn("track_width (min 0.1mm)", self.rules(layers=2))
        self.assertIn("track_width (min 0.16mm)", self.rules(layers=2, outer_oz=2))
        self.assertIn("track_width (min 0.3mm)", self.rules(layers=2, outer_oz=4.5))
        four = self.rules(layers=4)
        self.assertIn("track_width (min 0.09mm)", four)
        self.assertIn("(layer inner)", four)
        self.assertIn("track_width (min 0.15mm)", self.rules(layers=4, inner_oz=2))

    def test_clearance_is_physical_so_netclasses_survive(self):
        text = self.rules(layers=4)
        self.assertIn("physical_clearance (min 0.09mm)", text)
        self.assertNotRegex(text, r"constraint clearance ")
        self.assertIn("A.NetName != B.NetName", text)

    def test_unsupported_stackups_raise(self):
        for kw in (dict(layers=3), dict(layers=4, outer_oz=2.5), dict(layers=4, inner_oz=3)):
            with self.subTest(**kw), self.assertRaises(ValueError):
                render(Profile(**kw))

    def test_micro_drill_needs_thin_enig_or_osp(self):
        self.assertIn("hole_size (min 0.1mm)", self.rules(layers=4, thickness=0.8, finish="ENIG"))
        self.assertIn("via_diameter (min 0.2mm)", self.rules(layers=4, thickness=1.0, finish="OSP"))
        for kw in (dict(thickness=1.2, finish="ENIG"), dict(thickness=0.8, finish="HASL")):
            text = self.rules(layers=4, **kw)
            self.assertIn('"JLC via: hole min 0.15mm"', text)
            self.assertIn("via_diameter (min 0.25mm)", text)

    def test_tiers(self):
        tiered = self.rules(layers=2)
        self.assertRegex(tiered, r'JLC paid: via hole under 0.2mm"\n    \(severity warning\)')
        standard = self.rules(layers=2, tier="standard")
        self.assertRegex(standard, r'JLC paid: via hole under 0.2mm"\n    \(severity error\)')
        advanced = self.rules(layers=2, tier="advanced")
        self.assertNotIn("JLC paid", advanced)
        self.assertNotIn("JLC recommended", advanced)
        self.assertIn("JLC via: hole min", advanced)

    def test_accept_silences_one_paid_option(self):
        text = self.rules(layers=4, accept=["small_via"])
        self.assertNotIn("JLC paid: via hole under", text)
        self.assertIn("JLC paid: HDI blind/buried/micro via", text)

    def test_mask_dam_by_color_and_copper(self):
        self.assertIn("solder_mask_sliver (min 0.1mm)", self.rules(layers=2))
        self.assertIn("solder_mask_sliver (min 0.13mm)", self.rules(layers=2, mask_color="black"))
        self.assertIn("solder_mask_sliver (min 0.2mm)", self.rules(layers=2, outer_oz=2, mask_color="white"))

    def test_bga_enig_rule_only_without_enig(self):
        self.assertIn("need ENIG", self.rules(layers=4, finish="HASL"))
        self.assertIn("need ENIG", self.rules(layers=4))  # unknown finish is conservative
        self.assertNotIn("need ENIG", self.rules(layers=4, finish="ENIG"))

    def test_annular_ring_by_stackup(self):
        self.assertIn("annular_width (min 0.18mm)", self.rules(layers=2))
        self.assertIn("annular_width (min 0.15mm)", self.rules(layers=4))
        self.assertIn("annular_width (min 0.254mm)", self.rules(layers=2, outer_oz=2))

    def test_single_layer(self):
        text = self.rules(layers=1)
        self.assertIn("disallow via", text)
        self.assertIn('assertion "!A.isPlated()"', text)
        self.assertIn("hole_size (min 0.3mm)", text)

    def test_hdi_and_backdrill_need_four_layers(self):
        two = self.rules(layers=2)
        self.assertRegex(two, r'need 4\+ layers"\n    \(severity error\)\n    \(constraint disallow blind_via')
        self.assertIn("JLC backdrill: needs 4+ layers", two)
        four = self.rules(layers=4, thickness=1.6)
        self.assertIn("JLC backdrill: through hole 0.2mm to 0.5mm", four)

    def test_via_in_pad_respects_board_default_fill(self):
        default = self.rules(layers=4)
        self.assertIn("A.Filling == 'From board stackup'", default)
        filled = self.rules(layers=4, via_fill_default=True, via_cap_default=True)
        self.assertNotIn("A.Filling == 'From board stackup'", filled)
        six = self.rules(layers=6)
        self.assertIn("default 6+ layers", six)

    def test_rules_ordered_generic_before_specific(self):
        text = self.rules(layers=4)
        order = [m.group(1) for m in re.finditer(r'\(rule "([^"]+)"', text)]

        def pos(prefix):
            return next(i for i, n in enumerate(order) if n.startswith(prefix))

        self.assertLess(pos("JLC drill: plated hole min"), pos("JLC drill: castellated hole"))
        self.assertLess(pos("JLC via: hole min"), pos("JLC HDI: laser blind"))
        self.assertLess(pos("JLC drill: hole to hole"), pos("JLC drill: pad hole to pad hole"))
        self.assertLess(pos("JLC clearance: outer copper"), pos("JLC clearance: SMD pad"))
        self.assertLess(pos("JLC edge: copper to routed edge"), pos("JLC edge: castellated"))


class BoardTests(unittest.TestCase):
    def test_reads_stackup_and_outline(self):
        info = read_board(FIXTURE)
        self.assertEqual(info.layer_count, 4)
        self.assertEqual(info.thickness, 0.8)
        self.assertEqual(info.outer_oz, 1.0)
        self.assertEqual(info.inner_oz, 0.5)
        self.assertEqual(info.finish, "ENIG")
        self.assertEqual(info.mask_color, "black")
        w, h = info.size
        self.assertAlmostEqual(w, 33.0, places=1)  # arc bulges 3 mm past the rectangle
        self.assertAlmostEqual(h, 20.0, places=1)
        self.assertEqual(len(info.vias), 3)
        self.assertEqual(sum(p["fab_property"] == "pad_prop_castellated" for p in info.pads), 2)

    def test_profile_from_board_and_overrides(self):
        info = read_board(FIXTURE)
        p = profile_from_board(info)
        self.assertEqual((p.layers, p.finish, p.mask_color, p.thickness), (4, "ENIG", "black", 0.8))
        self.assertTrue(p.micro_drill_ok)
        p2 = profile_from_board(info, finish="HASL", tier="standard")
        self.assertEqual((p2.finish, p2.tier), ("HASL", "standard"))
        self.assertFalse(p2.micro_drill_ok)

    def test_thickness_to_oz(self):
        self.assertEqual(thickness_to_oz(0.035), 1.0)
        self.assertEqual(thickness_to_oz(0.018), 0.5)
        self.assertEqual(thickness_to_oz(0.07), 2.0)
        self.assertIsNone(thickness_to_oz(0.05))

    def test_parser_handles_escaped_quotes(self):
        tree = parse_sexpr('(a "x \\"y\\"" (b 1))')
        self.assertEqual(tree[0][1], 'x "y"')


class PreflightTests(unittest.TestCase):
    def run_pf(self, **over):
        info = read_board(FIXTURE)
        p = profile_from_board(info, **over)
        return {(f.severity, f.code): f.message for f in preflight.run(info, p)}, \
            preflight.run(info, p)

    def test_fixture_findings(self):
        _, findings = self.run_pf()
        msgs = "\n".join(f"{f.severity} {f.code} {f.message}" for f in findings)
        self.assertIn("castellated pads", msgs)
        self.assertIn("edge-connector pads", msgs)
        self.assertIn("backdrilled", msgs)
        self.assertIn("countersunk", msgs)
        self.assertIn("HDI", msgs)
        self.assertNotIn("error", [f.severity for f in findings])

    def test_hasl_on_thin_board_and_thickness(self):
        _, findings = self.run_pf(finish="HASL", thickness=0.4)
        errors = [f.message for f in findings if f.severity == "error"]
        self.assertTrue(any("HASL" in m for m in errors))
        _, findings = self.run_pf(thickness=2.5)
        self.assertTrue(any("12+ layers" in f.message for f in findings if f.severity == "error"))

    def test_castellated_needs_size_and_thickness(self):
        info = read_board(FIXTURE)
        info.outline_bbox = (0, 0, 8, 8)
        p = profile_from_board(info, thickness=0.4)
        errs = [f.message for f in preflight.run(info, p) if f.severity == "error"]
        self.assertTrue(any("castellated boards must be >= 10" in m for m in errs))
        self.assertTrue(any("0.6 mm thick" in m for m in errs))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.board = self.dir / "demo.kicad_pcb"
        self.board.write_text(FIXTURE.read_text())
        self.dru = self.dir / "demo.kicad_dru"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_install_fresh_then_regenerate_keeps_user_rules(self):
        self.assertEqual(self.cli("install", str(self.board))[0], 0)
        text = self.dru.read_text()
        self.assertTrue(text.startswith(BEGIN_MARK))
        self.dru.write_text(text + '(rule "mine"\n    (constraint clearance (min 1mm)))\n')
        self.assertEqual(self.cli("install", str(self.board), "--tier", "standard")[0], 0)
        text = self.dru.read_text()
        self.assertIn('(rule "mine"', text)
        self.assertEqual(text.count("(version 1)"), 1)
        self.assertEqual(read_profile_line(text).tier, "standard")
        self.assertGreater(text.index('(rule "mine"'), text.index(END_MARK))

    def test_foreign_file_needs_merge_or_force(self):
        self.dru.write_text('(version 1)\n(rule "old"\n    (constraint clearance (min 1mm)))\n')
        code, _ = self.cli("install", str(self.board))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.cli("install", str(self.board), "--merge")[0], 0)
        text = self.dru.read_text()
        self.assertIn('(rule "old"', text)
        self.assertEqual(text.count("(version 1)"), 1)
        self.assertTrue(list(self.dir.glob("demo.kicad_dru.bak-*")))

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.cli("install", str(self.board), "--dry-run")[0], 0)
        self.assertFalse(self.dru.exists())

    def test_preflight_detects_stale_rules(self):
        self.cli("install", str(self.board), "--finish", "HASL")
        code, out = self.cli("preflight", str(self.board))
        self.assertEqual(code, 1)
        self.assertIn("do not match the board stackup", out)

    def test_split_existing_roundtrip(self):
        text = render_file(Profile(layers=2), user_rules='(rule "x" (constraint clearance (min 1mm)))')
        generated, user = split_existing(text)
        self.assertTrue(generated)
        self.assertIn('(rule "x"', user)

    def test_capabilities_are_source_of_truth(self):
        caps = load_capabilities()
        caps["trace_space"]["two_layer_outer"]["1"] = 0.123
        self.assertIn("track_width (min 0.123mm)", render(Profile(layers=2), caps=caps))


if __name__ == "__main__":
    unittest.main()


def _bga_board(pitch, pad, layers=4, finish="ENIG", vias=()):
    cu = '(layer "F.Cu" (type "copper") (thickness 0.035))(layer "B.Cu" (type "copper") (thickness 0.035))'
    names = ['(0 "F.Cu" signal)', '(2 "B.Cu" signal)'] + [f'({4 + 2 * i} "In{i + 1}.Cu" signal)'
                                                            for i in range(layers - 2)]
    pads = "".join(f'(pad "{r}{c}" smd circle (at {c * pitch:g} {r * pitch:g}) (size {pad:g} {pad:g}) '
                   f'(layers "F.Cu" "F.Mask") (property pad_prop_bga))'
                   for r in range(4) for c in range(4))
    via_txt = "".join(f'(via (at {5 + i} 15) (size {d:g}) (drill {h:g}) (layers "F.Cu" "B.Cu") (net 0))'
                      for i, (h, d) in enumerate(vias))
    return (f'(kicad_pcb (version 20250926) (general (thickness 1.6)) (layers {" ".join(names)}) '
            f'(setup (stackup {cu} (copper_finish "{finish}")))'
            f'(footprint "Package_BGA:Test_BGA" (layer "F.Cu") (at 10 10) '
            f'(property "Reference" "U1" (at 0 0 0) (layer "F.SilkS")) {pads})'
            f'(gr_rect (start 0 0) (end 30 30) (layer "Edge.Cuts")) {via_txt})')


class BgaAndDrillStepTests(unittest.TestCase):
    def findings(self, text, **over):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            path.write_text(text)
            info = read_board(path)
            return preflight.run(info, profile_from_board(info, **over))

    def bga(self, findings):
        return next(f for f in findings if f.code == "bga")

    def test_08_pitch_dogbone_with_free_vias(self):
        f = self.bga(self.findings(_bga_board(0.8, 0.4)))
        self.assertEqual(f.severity, "info")
        self.assertIn("pitch 0.8", f.message)
        self.assertIn("dog-bone fanout OK with free", f.message)

    def test_05_pitch_needs_via_in_pad(self):
        f = self.bga(self.findings(_bga_board(0.5, 0.25)))
        self.assertEqual(f.severity, "warning")
        self.assertIn("via-in-pad", f.message)
        self.assertIn("0 track(s)", f.message)   # 0.09 + 0.09 + 0.09 > 0.25 mm gap

    def test_04_pitch_without_enig(self):
        f = self.bga(self.findings(_bga_board(0.4, 0.22, finish="HAL lead-free")))
        self.assertEqual(f.severity, "error")
        self.assertIn("need ENIG", f.message)

    def test_drill_step_rounding_breaks_ring(self):
        fs = self.findings(_bga_board(0.8, 0.4, vias=[(0.16, 0.26), (0.3, 0.4), (0.22, 0.45)]))
        errors = [f.message for f in fs if f.code == "drill_step" and f.severity == "error"]
        warns = [f.message for f in fs if f.code == "drill_step" and f.severity == "warning"]
        self.assertEqual(len(errors), 1)
        self.assertIn("drilled 0.2", errors[0])
        self.assertNotIn("0.22/0.45", errors[0])        # 0.25 drill still leaves 0.1 ring
        self.assertIn("2 holes are not on the 0.05 mm drill step", warns[0])

    def test_drc_rules_contain_step_checks(self):
        text = render(Profile(layers=4))
        self.assertIn("(A.Hole > 0.1995mm && A.Hole < 0.2005mm)", text)
        self.assertIn("A.Diameter - 0.2mm < 0.1mm", text)


class OperatorPrecedenceTests(unittest.TestCase):
    """KiCad's expression grammar binds && LOOSER than || (grammar.lemon declares
    G_BOOL_AND before G_BOOL_OR). Mixing them without parentheses would be misread."""

    def test_no_unparenthesised_mix(self):
        for prof in PROFILES:
            body = strip_comments(render(prof))
            exprs = re.findall(r'\(condition "([^"]*)"\)', body)
            exprs += re.findall(r'\(constraint assertion "([^"]*)"\)', body)
            for e in exprs:
                e = re.sub(r"'[^']*'", "''", e)
                e = re.sub(r"\b\w+\(\)", "f", e)
                stack = [[]]
                for tok in re.findall(r"\(|\)|&&|\|\|", e):
                    if tok == "(":
                        stack.append([])
                    elif tok == ")":
                        ops = stack.pop()
                        self.assertFalse("&&" in ops and "||" in ops, e)
                    else:
                        stack[-1].append(tok)
                self.assertFalse("&&" in stack[0] and "||" in stack[0], e)
