"""Via beside SMD pad checks (jlcpcb_rules.viapad) and the board-parser fields they use."""

import math
import unittest

from jlcpcb_rules import viapad
from jlcpcb_rules.board import BoardInfo, _net, _tenting, parse_sexpr
from jlcpcb_rules.profile import load_capabilities

CAPS = load_capabilities()


def pad(xy, size=(1.0, 0.5), shape="rect", angle=0.0, net="GND", side="F", ref="C1", num="1", margin=None, rr=0.25):
    return {"ref": ref, "number": num, "type": "smd", "shape": shape, "xy": xy, "angle": angle, "size": size,
            "net": net, "layers": [f"{side}.Cu", f"{side}.Mask", f"{side}.Paste"], "rratio": rr,
            "mask_margin": margin}


def via(at, net="GND", d=0.4, hole=0.2, tenting=None, kind="through", layers=("F.Cu", "B.Cu")):
    return {"type": kind, "at": at, "diameter": d, "hole": hole, "net": net, "layers": list(layers),
            "tenting": tenting}


def board(vias, pads, tent=True, expansion=0.0):
    b = BoardInfo(path=None)
    b.vias, b.pads = vias, pads
    b.default_tenting = {"front": tent, "back": tent}
    b.mask_expansion = expansion
    return b


class Distance(unittest.TestCase):
    def test_rect_edge_and_inside(self):
        p = pad((0, 0), (1.0, 0.5))
        self.assertAlmostEqual(viapad.pad_signed_distance(0, 0.75, p), 0.5)
        self.assertAlmostEqual(viapad.pad_signed_distance(1.0, 0, p), 0.5)
        self.assertLess(viapad.pad_signed_distance(0.1, 0.1, p), 0)

    def test_rotation_90(self):
        p = pad((0, 0), (1.0, 0.5), angle=90)
        # long side now vertical: 0.5 above centre is the pad end, 0.5 right is 0.25 outside
        self.assertAlmostEqual(viapad.pad_signed_distance(0, -0.75, p), 0.25)
        self.assertAlmostEqual(viapad.pad_signed_distance(0.5, 0, p), 0.25)

    def test_roundrect_corner_and_circle(self):
        p = pad((0, 0), (1.0, 1.0), shape="roundrect", rr=0.25)  # corner radius 0.25
        d = viapad.pad_signed_distance(1.0, 1.0, p)
        self.assertAlmostEqual(d, math.hypot(0.75, 0.75) - 0.25)
        c = pad((0, 0), (0.6, 0.6), shape="circle")
        self.assertAlmostEqual(viapad.pad_signed_distance(1.0, 0, c), 0.7)


class Checks(unittest.TestCase):
    def test_same_net_tented_below_min_warns(self):
        # pad top edge at y=0.25, via radius 0.2 at y=0.5 -> gap 0.05
        found = viapad.check(board([via((0, 0.5))], [pad((0, 0))]), CAPS, 0.1)
        self.assertEqual(len(found["same_tented"]), 1)
        self.assertAlmostEqual(found["same_tented"][0]["gap_mm"], 0.05)

    def test_same_net_tented_at_min_is_ok(self):
        found = viapad.check(board([via((0, 0.55))], [pad((0, 0))]), CAPS, 0.1)
        self.assertFalse(any(found.values()))

    def test_same_net_untented_needs_dam_plus_both_expansions(self):
        # gap 0.15, expansions 0.05 each -> strip 0.05 < 0.1
        b = board([via((0, 0.6))], [pad((0, 0))], tent=False, expansion=0.05)
        found = viapad.check(b, CAPS, 0.1)
        self.assertEqual(len(found["same_untented"]), 1)
        self.assertAlmostEqual(found["same_untented"][0]["mask_web_mm"], 0.05)
        ok = viapad.check(board([via((0, 0.65))], [pad((0, 0))], tent=False, expansion=0.05), CAPS, 0.1)
        self.assertFalse(any(ok.values()))  # gap 0.2 = 0.1 + 0.05 + 0.05

    def test_per_via_tenting_overrides_board(self):
        b = board([via((0, 0.5), tenting={"front": False, "back": None})], [pad((0, 0))], tent=True)
        self.assertEqual(len(viapad.check(b, CAPS, 0.1)["same_untented"]), 1)

    def test_pad_mask_margin_counts(self):
        # gap 0.2, board expansion 0, pad margin 0.15 -> strip 0.05 < 0.1
        b = board([via((0, 0.65))], [pad((0, 0), margin=0.15)], tent=False, expansion=0.0)
        rows = viapad.check(b, CAPS, 0.1)["same_untented"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["mask_web_mm"], 0.05)
        # strip exactly at the dam is OK
        b = board([via((0, 0.65))], [pad((0, 0), margin=0.1)], tent=False, expansion=0.0)
        self.assertFalse(any(viapad.check(b, CAPS, 0.1).values()))

    def test_other_net_untented_bridge(self):
        b = board([via((0, 0.52), net="+3V3")], [pad((0, 0))], tent=False)  # gap 0.07
        found = viapad.check(b, CAPS, 0.1)
        self.assertEqual(len(found["diff_untented"]), 1)
        tented = viapad.check(board([via((0, 0.52), net="+3V3")], [pad((0, 0))], tent=True), CAPS, 0.1)
        self.assertFalse(any(tented.values()), "tented different-net vias are left to the DRC")

    def test_via_in_pad_and_wrong_side_are_skipped(self):
        self.assertFalse(any(viapad.check(board([via((0.1, 0))], [pad((0, 0))]), CAPS, 0.1).values()))
        blind = via((0, 0.5), kind="blind", layers=("B.Cu", "In2.Cu"))
        self.assertFalse(any(viapad.check(board([blind], [pad((0, 0))]), CAPS, 0.1).values()))


class Parsing(unittest.TestCase):
    def node(self, text):
        return parse_sexpr(text)[0]

    def test_tenting_forms(self):
        self.assertEqual(_tenting(self.node("(tenting (front yes) (back no))")), {"front": True, "back": False})
        self.assertEqual(_tenting(self.node("(tenting front back)")), {"front": True, "back": True})
        self.assertEqual(_tenting(self.node("(tenting front)")), {"front": True, "back": False})
        self.assertEqual(_tenting(self.node("(tenting none)")), {"front": False, "back": False})
        self.assertIsNone(_tenting(None))

    def test_net_forms(self):
        self.assertEqual(_net(self.node('(via (net "GND"))')), "GND")
        self.assertEqual(_net(self.node('(via (net 3 "GND"))')), "GND")
        self.assertIsNone(_net(self.node('(via (net 0 ""))')))
        self.assertIsNone(_net(self.node("(via (net 0))")))


if __name__ == "__main__":
    unittest.main()
