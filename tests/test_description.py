import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kicad_checker.core import identification, parse_netlist, read_json, write_json
from kicad_checker.description import description_audit, owner_actions, owner_actions_markdown

ROOT = Path(__file__).resolve().parents[1]

# Real strings from a KiCad 10 project (distributor-style descriptions).
C2_DESC = "2.2 µF ±10% 25V Ceramic Capacitor X7R 0805 (2012 Metric) AEC-Q200"
R15_DESC = "10 kOhms ±1% 0.063W, 1/16W Chip Resistor 0402 (1005 Metric) Anti-Sulfur, Automotive AEC-Q200 Thick Film"
C_FP = "Capacitor_SMD:C_0805_2012Metric_Pad1.18x1.45mm_HandSolder"
R_FP = "Resistor_SMD:R_0402_1005Metric_Pad0.72x0.64mm_HandSolder"


def part(ref, value, footprint="", **fields):
    return {"reference": ref, "value": value, "footprint": footprint, "dnp": False, "sheet_path": "/",
            "fields": fields, "pins": {}}


class DescriptionTests(unittest.TestCase):
    def audit(self, *components):
        circuit = {"netlist_sha256": "x", "components": {c["reference"]: c for c in components}}
        return {r["reference"]: r for r in description_audit(circuit)["results"]}

    def one(self, component):
        return self.audit(component)[component["reference"]]

    def check(self, item, name):
        return next(c for c in item["checks"] if c["check"] == name)

    def test_real_rlc_descriptions_pass(self):
        results = self.audit(part("C2", "2.2 uF", C_FP, Description=C2_DESC),
                             part("R15", "10 K", R_FP, Description=R15_DESC),
                             part("R12", "0 Ohm", R_FP, Description="0 Ohms Jumper Chip Resistor 0402 (1005 Metric)"))
        for ref, item in results.items():
            with self.subTest(ref=ref):
                self.assertEqual(item["status"], "pass", item.get("reason"))
        self.assertEqual(self.check(results["C2"], "value")["description_value"], "2.2 uF")

    def test_empty_or_placeholder_asks_owner(self):
        for text in ("", "  ", "TBD", "n/a"):
            with self.subTest(text=text):
                item = self.one(part("U2", "LP5912", Description=text))
                self.assertEqual(item["status"], "needs_review")
                self.assertIn("Owner: fill the Description", item["reason"])
        self.assertEqual(self.one(part("U2", "LP5912"))["status"], "needs_review")  # field absent

    def test_generic_library_text_is_not_enough_for_rlc(self):
        item = self.one(part("C3", "100n", C_FP, Description="Unpolarized capacitor"))
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("does not state a readable capacitance", item["reason"])

    def test_value_mismatch_fails_and_notation_differences_pass(self):
        self.assertEqual(self.one(part("R1", "10k", R_FP, Description=R15_DESC.replace("10 kOhms", "1 kOhms")))["status"], "fail")
        self.assertEqual(self.one(part("C2", "4.7uF", C_FP, Description=C2_DESC))["status"], "fail")
        for value in ("10k", "10K", "10 kOhm", "10000"):
            with self.subTest(value=value):
                self.assertEqual(self.one(part("R1", value, R_FP, Description=R15_DESC))["status"], "pass")
        for value in ("2u2", "2.2µF", "2200n"):
            with self.subTest(value=value):
                self.assertEqual(self.one(part("C2", value, C_FP, Description=C2_DESC))["status"], "pass")

    def test_distributor_short_form(self):
        self.assertEqual(self.one(part("C2", "2.2uF", C_FP, Description="CAP CER 2.2UF 25V X7R 0805"))["status"], "pass")
        self.assertEqual(self.one(part("R1", "10k", R_FP, Description="RES 10K OHM 1% 1/16W 0402"))["status"], "pass")
        item = self.one(part("R9", "10m", R_FP, Description="RES 10 MOHM 1% 0402"))
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("ambiguous", item["reason"])

    def test_unreadable_value_is_unresolved_not_failed(self):
        item = self.one(part("C2", "DNF?", C_FP, Description=C2_DESC))
        self.assertEqual(item["status"], "needs_review")

    def test_package(self):
        self.assertEqual(self.one(part("C2", "2.2uF", C_FP, Description=C2_DESC.replace("0805 (2012 Metric)", "0603 (1608 Metric)")))["status"], "fail")
        self.assertEqual(self.one(part("C2", "2.2uF", C_FP, Description="2.2 µF 25V X7R (2012 Metric)"))["status"], "pass")
        item = self.one(part("C2", "2.2uF", C_FP, Description="2.2 µF 25V X7R ceramic capacitor"))
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("package size", item["reason"])
        # Metric 0603 is imperial 0201; it must not be read as imperial 0603.
        fp = "Capacitor_SMD:C_0201_0603Metric"
        self.assertEqual(self.one(part("C9", "100n", fp, Description="100 nF 10V X5R 0201 (0603 Metric)"))["status"], "pass")
        # A footprint without a size code is reported, not failed.
        thru = self.one(part("C8", "100uF", "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm", Description="100 µF 35V Aluminum Capacitor Radial"))
        self.assertEqual(thru["status"], "pass")
        self.assertEqual(self.check(thru, "package")["status"], "not_checked")

    def test_type_word_and_footprint_library(self):
        self.assertEqual(self.one(part("R1", "10k", R_FP, Description="10 kOhms Ceramic Capacitor 0402"))["status"], "fail")
        self.assertEqual(self.one(part("R1", "10k", C_FP.replace("0805_2012", "0402_1005"), Description=R15_DESC))["status"], "fail")

    def test_ferrite_bead_uses_impedance(self):
        bead = part("L1", "600R@100MHz", "Inductor_SMD:L_0603_1608Metric", Description="Ferrite Bead 600 Ohms @ 100 MHz 0603 (1608 Metric) 1A")
        self.assertEqual(self.one(bead)["status"], "pass")

    def test_rating_fields_are_cross_checked_when_declared(self):
        base = part("C2", "2.2uF", C_FP, Description=C2_DESC)
        cases = [({"Voltage": "25V"}, "pass"), ({"Voltage": "16 V"}, "fail"), ({"Voltage": "high"}, "needs_review"),
                 ({"Tolerance": "±10%"}, "pass"), ({"Tolerance": "5%"}, "fail"),
                 ({"Dielectric": "x7r"}, "pass"), ({"Dielectric": "X5R"}, "fail")]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                component = copy.deepcopy(base)
                component["fields"].update(fields)
                self.assertEqual(self.one(component)["status"], expected)
        resistor = part("R15", "10k", R_FP, Description=R15_DESC, Power="1/16W")
        self.assertEqual(self.one(resistor)["status"], "pass")  # 0.063 W and 1/16 W agree within 2 %
        resistor["fields"]["Power"] = "0.1W"
        self.assertEqual(self.one(resistor)["status"], "fail")
        undeclared = self.one(part("C2", "2.2uF", C_FP, Description="2.2 µF X7R 0805", Voltage="25V"))
        self.assertEqual(self.check(undeclared, "voltage")["status"], "not_checked")
        self.assertEqual(undeclared["status"], "pass")

    def test_exemptions_and_non_rlc(self):
        dnp = part("C5", "1uF", C_FP)
        dnp["dnp"] = True
        self.assertEqual(self.one(dnp)["status"], "not_applicable")
        pad = part("TP1", "TestPad", Checker_NonPurchasable="true", Checker_ExemptionReason="Bare PCB pad")
        self.assertEqual(self.one(pad)["status"], "not_applicable")
        self.assertEqual(self.one(part("U3", "SBSPP", Description="LC (Pi) EMI Filter 3rd Order"))["status"], "pass")
        conflict = self.one(part("U4", "X", Description="Regulator", Desc="Op-amp"))
        self.assertEqual(conflict["status"], "needs_review")

    def test_owner_actions_list(self):
        circuit = {"netlist_sha256": "x", "components": {
            "U2": part("U2", "LP5912"), "C2": part("C2", "4.7uF", C_FP, Description=C2_DESC)}}
        actions = owner_actions(identification(circuit), description_audit(circuit))
        text = owner_actions_markdown(actions)
        self.assertIn("**U2**", text)
        self.assertIn("Description is empty", text)
        self.assertIn("Description conflict", text)

    def test_cli_artifacts_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            proc = subprocess.run([sys.executable, "-m", "kicad_checker", "import-netlist", str(ROOT / "examples/symbol-pins/netlist.xml"),
                                   "--output", str(base / "import")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for name in ("description.json", "description.md", "owner-actions.md"):
                self.assertTrue((base / "import" / name).is_file(), name)
            self.assertIn("Nothing to fill", (base / "import/owner-actions.md").read_text())

            command = [sys.executable, "-m", "kicad_checker", "review", str(base / "import/circuit.json"), "--snapshot",
                       "--rules", str(ROOT / "examples/symbol-pins/rules.json"), "--evidence", str(ROOT / "examples/symbol-pins/evidence.json")]
            proc = subprocess.run(command + ["--output", str(base / "ok")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            circuit = read_json(base / "import/circuit.json")
            circuit["components"]["U1"]["fields"]["Description"] = ""
            write_json(base / "import/circuit.json", circuit)
            proc = subprocess.run(command + ["--output", str(base / "empty")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("U1", (base / "empty/owner-actions.md").read_text())

            circuit["components"]["R1"] = part("R1", "10k", R_FP, Description=R15_DESC.replace("10 kOhms", "1 kOhms"),
                                               Manufacturer="Yageo", MPN="AF0402FR-071KL")
            circuit["components"]["U1"]["fields"]["Description"] = "SYNTHETIC device"
            write_json(base / "import/circuit.json", circuit)
            proc = subprocess.run(command + ["--output", str(base / "conflict")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = read_json(base / "conflict/report.json")
            self.assertEqual(report["description"]["coverage"]["fail"], 1)
            self.assertIn("Component description", (base / "conflict/report.md").read_text())

    def test_netlist_description_field_is_read(self):
        circuit = parse_netlist(ROOT / "examples/demo/netlist.xml")
        self.assertIn("Description", circuit["components"]["C1"]["fields"])


if __name__ == "__main__":
    unittest.main()
