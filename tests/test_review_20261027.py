"""Regressions for the 2026-10-27 Codex sourcing review (runs/codex-sourcing-review-20261027-001).
Each test is the review's reproduction with the assertion turned into the correct behaviour.
Synthetic records only; network and credentials are patched to fail."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kicad_checker.cli import main
from kicad_checker.core import parse_netlist
from kicad_checker.sourcing.inventory import _request, spec_from_product, verify_candidate
from kicad_checker.sourcing.partslist import compare_design
from kicad_checker.sourcing.providers import DigiKeyCatalog, utc_now
from kicad_checker.sourcing.requirements import validate_request
from tests.test_inventory import dk_inductor, netlist
from tests.test_partslist import product

CAP = {"Capacitance": "1 µF", "Tolerance": "±10%", "Voltage - Rated": "16V",
       "Temperature Coefficient": "X7R", "Package / Case": "0603 (1608 Metric)"}
CATALOG = {
    "REF-CAP": [product("REF-CAP", "Maker A", "capacitor", CAP)],
    "GOOD-CAP": [product("GOOD-CAP", "Maker B", "capacitor", CAP)],
    "BAD-CAP": [product("BAD-CAP", "Maker C", "capacitor", {**CAP, "Voltage - Rated": "6.3V"})],
    "SCARCE": [product("SCARCE", "Maker A", "capacitor", CAP, stock=3)],
}


class Records:
    def get(self, mpn):
        return {"products": CATALOG.get(mpn, []), "observed_at": utc_now()}


def row(refs, quantity=1, maker="Maker A", number=2):
    return {"row": number, "status": "available", "references": refs, "mpn": "REF-CAP",
            "manufacturer": maker, "vendor_alternates": [], "value": "1uF", "package": "0603",
            "need": {"per_board": quantity, "needed": quantity}, "_product": CATALOG["REF-CAP"][0],
            "catalog": {"manufacturer": maker}}


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for target in ("urllib.request.urlopen", "kicad_checker.digikey.load_credentials"):
            patcher = mock.patch(target, side_effect=ValueError("network/credentials prohibited in tests"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for mpn, products in CATALOG.items():
            client = mock.Mock(keyword=mock.Mock(return_value=(200, {"ExactMatches": products, "Products": []})))
            DigiKeyCatalog(self.dir / "cache" / "digikey", client).page(mpn, 0, limit=10, refresh=True, exact=True)

    def compare(self, rows, parts):
        xml = self.dir / "design.xml"
        xml.write_text(netlist([(ref, value, "Capacitor_SMD:C_0603_1608Metric",
                                 [("MPN", mpn)] + ([("Manufacturer", maker)] if maker else []))
                                for ref, value, mpn, maker in parts]), encoding="utf-8")
        return compare_design(rows, parse_netlist(xml), Records())

    def cli(self, name, csv_text, *extra):
        source = self.dir / f"{name}.csv"
        source.write_text(csv_text, encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            code = main(["parts-list", str(source), "--cache", str(self.dir / "cache"), "--no-find-alternatives",
                         "--output", str(self.dir / name), *extra])
        return code, json.loads((self.dir / name / "parts-list.json").read_text(encoding="utf-8"))

    # F1: DNP-style headers have negative polarity
    def test_f1_dnp_column_polarity(self):
        _, report = self.cli("dnp", "MPN,Manufacturer,DNP\nREF-CAP,Maker A,Yes\nGOOD-CAP,Maker B,No\n")
        self.assertEqual([r["status"] for r in report["rows"]], ["not_fitted", "available"])
        _, report = self.cli("pop", "MPN,Manufacturer,Populate\nREF-CAP,Maker A,Yes\nGOOD-CAP,Maker B,No\n")
        self.assertEqual([r["status"] for r in report["rows"]], ["available", "not_fitted"])
        _, report = self.cli("text", "MPN,Manufacturer,DNP\nREF-CAP,Maker A,DNP\nGOOD-CAP,Maker B,Fitted\n")
        self.assertEqual([r["status"] for r in report["rows"]], ["not_fitted", "available"])

    # F2: every position of a grouped row is screened; the worst one decides
    def test_f2_every_substitute_is_screened(self):
        result = self.compare([row("C1, C2", 2)], [("C1", "1u", "GOOD-CAP", "Maker B"),
                                                   ("C2", "1u", "BAD-CAP", "Maker C")])["results"][0]
        verdicts = {p["ref"]: p["verdict"]["status"] for p in result["positions"]}
        self.assertEqual(verdicts, {"C1": "match", "C2": "rejected"})
        self.assertEqual(result["verdict"]["status"], "rejected")
        # An exact part at one position cannot hide a bad part at the other.
        result = self.compare([row("C1, C2", 2)], [("C1", "1u", "REF-CAP", "Maker A"),
                                                   ("C2", "1u", "BAD-CAP", "Maker C")])["results"][0]
        self.assertEqual([p["status"] for p in result["positions"]], ["same_part", "substituted"])
        self.assertEqual((result["status"], result["verdict"]["status"]), ("substituted", "rejected"))

    # F3: manufacturer is part of the identity
    def test_f3_manufacturer_conflict_is_not_same_part(self):
        compared = self.compare([row("C1")], [("C1", "1u", "REF-CAP", "Other Manufacturer")])
        self.assertEqual(compared["results"][0]["status"], "manufacturer_differs")
        self.assertEqual(compared["open"], 1)
        compared = self.compare([row("C1")], [("C1", "1u", "REF-CAP", None)])
        self.assertEqual(compared["results"][0]["status"], "unconfirmed")      # missing evidence → review
        self.assertEqual(compared["open"], 1)
        compared = self.compare([row("C1")], [("C1", "1u", "REF-CAP", "Maker A Inc.")])
        self.assertEqual(compared["results"][0]["status"], "same_part")       # suffix-normalized names match

    # F4: one design part satisfies at most one BOM position
    def test_f4_positions_are_allocated_once(self):
        compared = self.compare([row("C1"), row("C2", number=3)], [("C1", "1u", "REF-CAP", "Maker A")])
        statuses = [r["status"] for r in compared["results"]]
        self.assertEqual(statuses, ["same_part", "not_in_design"])
        self.assertEqual(compared["open"], 1)
        compared = self.compare([row("C1, C2", 2)], [("C1", "1u", "REF-CAP", "Maker A")])
        self.assertEqual(compared["results"][0]["status"], "incomplete")
        self.assertIn("1 of 2 positions found", " ".join(compared["results"][0]["notes"]))

    # F5: semi-shielded baseline survives into DigiKey screening
    def test_f5_semi_shielding_retained(self):
        spec = spec_from_product(dk_inductor("REF-IND", "Maker A", shielding="Semi-Shielded"))
        request = _request(spec, {"needed": 3}, spec["tolerance_percent"])
        self.assertIn({"parameter": "Shielding", "op": "in", "value": ["Shielded", "Semi-Shielded"]}, request["constraints"])
        validate_request(request)
        declared = {"tolerance": True, "power": True, "current": True}

        def verdict(shielding):
            return verify_candidate({"mpn": "X", "manufacturer": "Maker B", "checks": []},
                                    [dk_inductor("X", "Maker B", shielding=shielding)], utc_now(), request, declared)["spec_status"]
        self.assertEqual(verdict("Unshielded"), "rejected")
        self.assertEqual(verdict("Semi-Shielded"), "match")
        self.assertEqual(verdict("Shielded"), "match")

    # F6: an unreadable explicit quantity is unknown demand, never a guess
    def test_f6_unreadable_quantity_stays_unresolved(self):
        code, report = self.cli("lots", "MPN,Manufacturer,Qty\nSCARCE,Maker A,lots\n")
        self.assertEqual((code, report["rows"][0]["status"]), (2, "demand_unknown"))
        self.assertIn("unreadable", report["rows"][0]["quantity_note"])
        code, report = self.cli("pcs", "MPN,Manufacturer,Qty\nSCARCE,Maker A,100pcs\n")   # '100pcs' is 100
        self.assertEqual(report["rows"][0]["need"]["per_board"], 100)
        self.assertEqual((code, report["rows"][0]["status"]), (2, "stock_issues"))      # only 3 in stock

    # F7: the requested schematic comparison takes part in the exit code
    def test_f7_against_results_drive_exit_code(self):
        bad = self.dir / "bad.xml"
        bad.write_text(netlist([("C1", "1u", "Capacitor_SMD:C_0603_1608Metric",
                                 [("MPN", "BAD-CAP"), ("Manufacturer", "Maker C")])]), encoding="utf-8")
        code, report = self.cli("exit-bad", "MPN,Manufacturer,Designator\nREF-CAP,Maker A,C1\n", "--against", str(bad))
        self.assertEqual(report["against"]["results"][0]["verdict"]["status"], "rejected")
        self.assertEqual(code, 2)
        good = self.dir / "good.xml"
        good.write_text(netlist([("C1", "1u", "Capacitor_SMD:C_0603_1608Metric",
                                  [("MPN", "REF-CAP"), ("Manufacturer", "Maker A")])]), encoding="utf-8")
        code, report = self.cli("exit-good", "MPN,Manufacturer,Designator\nREF-CAP,Maker A,C1\n", "--against", str(good))
        self.assertEqual((code, report["against"]["open"]), (0, 0))
        missing = self.dir / "empty.xml"
        missing.write_text(netlist([("R9", "1k", "Resistor_SMD:R_0603_1608Metric", [("MPN", "X"), ("Manufacturer", "Y")])]),
                           encoding="utf-8")
        code, _ = self.cli("exit-missing", "MPN,Manufacturer,Designator\nREF-CAP,Maker A,C1\n", "--against", str(missing))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
