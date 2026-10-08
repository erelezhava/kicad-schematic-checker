import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kicad_checker.core import identification, parse_netlist, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]


class IdentificationTests(unittest.TestCase):
    def setUp(self):
        self.circuit = parse_netlist(ROOT / "examples/demo/netlist.xml")

    def result(self, ref="C1"):
        return next(r for r in identification(self.circuit)["results"] if r["reference"] == ref)

    def test_missing_manufacturer_and_supplier_only_are_incomplete(self):
        item = self.result()
        self.assertEqual(item["status"], "needs_review")
        self.assertEqual(item["missing_fields"], ["manufacturer"])
        component = self.circuit["components"]["C1"]
        component["part_number"] = None
        component["fields"] = {"Manufacturer": "Example", "LCSC Part #": "C12345"}
        item = self.result()
        self.assertEqual(item["status"], "needs_review")
        self.assertEqual(item["missing_fields"], ["part_number"])
        self.assertEqual(item["supplier_part_numbers"]["lcsc"], ["C12345"])

    def test_aliases_are_extracted_without_using_value_as_mpn(self):
        xml = '<export><components><comp ref="R1"><value>10k</value><fields><field name="Manufacturer">Example</field><field name="Mfr. Part #">RES-10K-1</field><field name="JLCPCB Part Number">C123</field></fields></comp><comp ref="R2"><value>RES-10K-1</value></comp></components><nets/></export>'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "netlist.xml"
            path.write_text(xml)
            self.circuit = parse_netlist(path)
        item = self.result("R1")
        self.assertEqual(item["status"], "pass")
        self.assertEqual(self.circuit["components"]["R1"]["part_number"], "RES-10K-1")
        self.assertEqual(item["supplier_part_numbers"]["jlcpcb"], ["C123"])
        self.assertEqual(self.result("R2")["missing_fields"], ["manufacturer", "part_number"])

    def test_placeholders_conflicts_and_supplier_looking_mpn_cannot_pass(self):
        component = self.circuit["components"]["C1"]
        component["part_number"] = None
        for placeholder in (" ", "TBD", "unknown", "?", "N/A", True):
            with self.subTest(placeholder=placeholder):
                component["fields"] = {"Manufacturer": "Example", "MPN": placeholder}
                self.assertEqual(self.result()["missing_fields"], ["part_number"])
        component["fields"] = {"Manufacturer": "Example", "MPN": "CAP-A", "PRT.NUM": "CAP-B"}
        item = self.result()
        self.assertEqual(item["status"], "needs_review")
        self.assertIsNone(item["part_number"])
        self.assertEqual(item["conflicting_fields"]["part_number"], {"MPN": "CAP-A", "PRT.NUM": "CAP-B"})
        component["fields"] = {"Manufacturer": "Example", "MPN": "C12345"}
        self.assertEqual(self.result()["status"], "needs_review")
        self.assertIn("LCSC", self.result()["reason"])
        component["fields"] = {"Manufacturer": "Example", "MPN": "CAP-A", "LCSC": "C1", "LCSC Part #": "C2"}
        self.assertIn("lcsc", self.result()["conflicting_fields"])

    def test_dnp_power_symbols_and_documented_exemptions(self):
        component = self.circuit["components"]["C1"]
        component["dnp"] = True
        self.assertEqual(self.result()["category"], "dnp")
        self.assertEqual(self.result()["status"], "not_applicable")
        self.assertEqual(self.result()["missing_fields"], ["manufacturer"])
        self.circuit["components"]["#PWR01"] = {"symbol_library": "power"}
        self.assertEqual(self.result("#PWR01")["status"], "not_applicable")
        # A physical test point or a component in a custom 'power' library is not automatically exempt.
        self.circuit["components"]["TP1"] = {"fields": {}, "symbol_library": "power"}
        self.assertEqual(self.result("TP1")["status"], "needs_review")
        self.circuit["components"]["TP1"]["fields"] = {"Checker_NonPurchasable": "true"}
        self.assertEqual(self.result("TP1")["status"], "needs_review")
        self.circuit["components"]["TP1"]["fields"]["Checker_ExemptionReason"] = "Bare PCB test pad; no fitted component"
        self.assertEqual(self.result("TP1")["status"], "not_applicable")

    def test_default_import_artifacts_and_review_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            proc = subprocess.run([sys.executable, "-m", "kicad_checker", "import-netlist", str(ROOT / "examples/demo/netlist.xml"), "--output", str(base / "import")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(read_json(base / "import/identification.json")["coverage"]["needs_review"], 2)
            self.assertIn("Missing manufacturer", (base / "import/identification.md").read_text())

            rules = read_json(ROOT / "examples/demo/rules.json")
            rules["rules"] = [r for r in rules["rules"] if r["id"] == "LDO-COUT-NOMINAL"]
            write_json(base / "rules.json", rules)
            command = [sys.executable, "-m", "kicad_checker", "review", str(base / "import/circuit.json"), "--snapshot", "--rules", str(base / "rules.json"), "--evidence", str(ROOT / "examples/demo/evidence.json")]
            proc = subprocess.run(command + ["--output", str(base / "incomplete")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            report = read_json(base / "incomplete/report.json")
            self.assertEqual(report["coverage"]["pass"], 1)
            self.assertEqual(report["identification"]["coverage"]["needs_review"], 2)
            self.assertIn("Component identification completeness", (base / "incomplete/report.md").read_text())

            circuit = copy.deepcopy(self.circuit)
            for component in circuit["components"].values():
                component["manufacturer"] = "Synthetic manufacturer"
            write_json(base / "import/circuit.json", circuit)
            proc = subprocess.run(command + ["--output", str(base / "complete")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()


class ValueMpnTests(unittest.TestCase):
    def check(self, ref, value, mpn):
        from kicad_checker.core import value_mpn_check
        component = {"reference": ref, "value": value, "fields": {}}
        return value_mpn_check(ref, component, mpn)

    def test_cases(self):
        cases = [
            ("J2", "5040500691", "5040500691", "pass", "matches"),
            ("U1", "LTC4331HUFD-PBF", "LTC4331HUFD#PBF", "pass", "matches"),           # separators ignored
            ("U2", "LP5912", "LP591233MDRVREP", "pass", "family"),
            ("A1", "MLX90640ESF-BAA-000-SP", "MLX90640ESF-BAA-000-TU", "needs_review", "different variant"),
            ("U2", "LP591233MDRVREP", "LP5912", "needs_review", "only a family name"),  # MPN not specific
            ("U5", "TPS7A0233", "LP591233MDRVREP", "needs_review", "different part number"),
        ]
        for ref, value, mpn, status, words in cases:
            with self.subTest(value=value, mpn=mpn):
                result = self.check(ref, value, mpn)
                self.assertEqual(result["status"], status)
                self.assertIn(words, result["reason"])

    def test_not_compared(self):
        for ref, value in (("J1", "Conn_01x06"), ("H1", "MountingHole_Pad"), ("U3", "USB C"), ("Y1", "Crystal"),
                           ("R1", "10k1234"), ("C1", "GCM21BR71E225"), ("U9", "")):
            with self.subTest(value=value):
                self.assertIsNone(self.check(ref, value, "ABC12345"))
        self.assertIsNone(self.check("U1", "LP5912", None))  # no MPN: identification already reports it

    def test_mismatch_makes_identification_needs_review_and_is_grouped(self):
        circuit = {"netlist_sha256": "x", "components": {"A1": {
            "reference": "A1", "value": "MLX90640ESF-BAA-000-SP", "dnp": False, "sheet_path": "/",
            "fields": {"Manufacturer": "Melexis", "MPN": "MLX90640ESF-BAA-000-TU"}, "pins": {}}}}
        report = identification(circuit)
        item = report["results"][0]
        self.assertEqual(item["status"], "needs_review")
        self.assertEqual(report["groups"]["value_mpn_mismatch"], ["A1"])
        circuit["components"]["A1"]["value"] = "MLX90640"
        self.assertEqual(identification(circuit)["results"][0]["status"], "pass")
