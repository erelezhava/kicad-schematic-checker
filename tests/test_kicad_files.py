"""Reading KiCad files: net numbers of KiCad 9 and older boards, string escapes, damaged files."""

import tempfile
import unittest
from pathlib import Path

from jlcpcb_rules import board as jboard
from jlcpcb_rules import viapad
from jlcpcb_rules.profile import load_capabilities
from kicad_checker import schematic
from refplane import sexpr

# One 0.6 mm SMD pad on GND and a tented GND via whose ring is 0.07 mm from the pad edge.
BOARD = """(kicad_pcb (version {version}) (generator "pcbnew")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (setup (pad_to_mask_clearance 0.05))
 (net 0 "") (net 1 "GND") (net 2 "+3V3")
 (footprint "R:R0402" (layer "F.Cu") (at 10 10)
   (property "Reference" "R1")
   (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask") {pad_net}))
 (via (at 10.62 10) (size 0.5) (drill 0.2) (layers "F.Cu" "B.Cu") {via_net})
)"""


class NetNames(unittest.TestCase):
    def read(self, version, pad_net, via_net):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            path.write_text(BOARD.format(version=version, pad_net=pad_net, via_net=via_net), encoding="utf-8")
            return jboard.read_board(path)

    def test_kicad9_via_net_number_is_resolved_through_the_net_table(self):
        info = self.read(20240108, '(net 1 "GND")', "(net 1)")
        self.assertEqual(info.vias[0]["net"], "GND")
        self.assertEqual(info.pads[0]["net"], "GND")
        self.assertEqual(info.file_version, 20240108)

    def test_same_net_via_is_found_on_old_and_new_boards(self):
        caps = load_capabilities()
        old = viapad.check(self.read(20240108, '(net 1 "GND")', "(net 1)"), caps, 0.1)
        new = viapad.check(self.read(20260206, '(net "GND")', '(net "GND")'), caps, 0.1)
        self.assertEqual(len(old["same_tented"]), 1)
        self.assertEqual(len(new["same_tented"]), 1)
        self.assertEqual(old["same_tented"][0]["gap_mm"], new["same_tented"][0]["gap_mm"])

    def test_different_net_number_is_not_the_same_net(self):
        info = self.read(20240108, '(net 1 "GND")', "(net 2)")
        self.assertEqual(info.vias[0]["net"], "+3V3")
        self.assertEqual(viapad.check(info, load_capabilities(), 0.1)["same_tented"], [])

    def test_unconnected_stays_none(self):
        self.assertIsNone(self.read(20240108, '(net 1 "GND")', "(net 0)").vias[0]["net"])


class StringEscapes(unittest.TestCase):
    def test_non_ascii_text_with_an_escape_is_kept(self):
        for parse in (sexpr.parse_sexpr, jboard.parse_sexpr):
            self.assertEqual(parse('(net "µC_\\"RST\\"")'), [["net", 'µC_"RST"']])
            self.assertEqual(parse('(t "Ω \\\\ ±1%")'), [["t", "Ω \\ ±1%"]])
            self.assertEqual(parse('(t "a\\nb")'), [["t", "a\nb"]])

    def test_schematic_values_and_quote_round_trip(self):
        text = '(kicad_sch (symbol (property "Description" "2.2 µF \\"X7R\\"\\nline 2")))'
        prop = schematic.parse(text).children("symbol")[0].children("property")[0]
        value = prop.strings()[1][1]
        self.assertEqual(value, '2.2 µF "X7R"\nline 2')
        self.assertEqual(schematic.quote(value), '"2.2 µF \\"X7R\\"\\nline 2"')


class DamagedSchematic(unittest.TestCase):
    def test_cut_off_or_unbalanced_files_raise_value_error(self):
        for text in ('(kicad_sch (symbol (property "A" "b"', '(kicad_sch (symbol (property "A" "b', "(kicad_sch))",
                     "", "garbage", '"x"', "(kicad_pcb)"):
            with self.assertRaises(ValueError, msg=repr(text)):
                schematic.parse(text)

    def test_valid_file_still_parses(self):
        self.assertEqual(schematic.parse("(kicad_sch (version 1))").head, "kicad_sch")


if __name__ == "__main__":
    unittest.main()
