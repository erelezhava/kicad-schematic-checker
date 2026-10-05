import copy
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from kicad_checker.core import digest, parse_netlist, read_json, review, write_json


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/symbol-pins"


class SymbolPinTests(unittest.TestCase):
    def setUp(self):
        self.circuit = parse_netlist(EXAMPLE / "netlist.xml")
        self.rules = read_json(EXAMPLE / "rules.json")
        self.evidence = read_json(EXAMPLE / "evidence.json")

    def result(self):
        return review(self.circuit, self.rules, self.evidence)["results"][0]

    def parse_modified_xml(self, modify):
        root = ET.parse(EXAMPLE / "netlist.xml").getroot()
        modify(root)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "netlist.xml"
            ET.ElementTree(root).write(path)
            self.circuit = parse_netlist(path)

    def test_unconnected_nc_power_pins_and_multiple_units_count(self):
        component = self.circuit["components"]["U1"]
        self.assertEqual(len(component["pins"]), 2)
        self.assertEqual(len(component["symbol_pins"]), 8)
        self.assertEqual(component["symbol_pins"]["2"]["name"], "NC")
        self.assertEqual(component["symbol_pins"]["7"]["units"], ["B"])
        item = self.result()
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["observation"]["symbol_pin_count"], 8)

    def test_eight_expected_seven_placed_fails_even_when_library_has_eight(self):
        self.parse_modified_xml(lambda root: root.find('./components/comp/units/unit[@name="B"]/pins').remove(root.find('./components/comp/units/unit[@name="B"]/pins/pin[@num="7"]')))
        self.assertEqual(len(self.circuit["components"]["U1"]["library_pins"]), 8)
        item = self.result()
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["observation"]["missing_pin_numbers"], ["7"])
        self.assertEqual(item["observation"]["symbol_pin_count"], 7)

    def test_same_count_wrong_number_only_passes_count_only_rule(self):
        self.parse_modified_xml(lambda root: root.find('./components/comp/units/unit[@name="B"]/pins/pin[@num="7"]').set("num", "9"))
        item = self.result()
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["observation"]["unexpected_pin_numbers"], ["9"])
        del self.rules["rules"][0]["expected_pin_numbers"]
        self.assertEqual(self.result()["status"], "pass")

    def test_shared_or_stacked_pin_numbers_count_once(self):
        self.parse_modified_xml(lambda root: ET.SubElement(root.find('./components/comp/units/unit[@name="A"]/pins'), "pin", num="7"))
        self.assertEqual(self.result()["status"], "pass")
        self.assertEqual(self.circuit["components"]["U1"]["symbol_pins"]["7"]["units"], ["A", "B"])

    def test_numbered_exposed_pad_requires_explicit_expected_count(self):
        self.parse_modified_xml(lambda root: ET.SubElement(root.find('./components/comp/units/unit[@name="B"]/pins'), "pin", num="9"))
        self.assertEqual(self.result()["status"], "fail")
        rule = self.rules["rules"][0]
        rule.update(expected_count=9, package="SOIC-8-EP", pin_count_convention="Eight leads plus numbered exposed pad 9")
        rule["expected_pin_numbers"].append("9")
        self.evidence["bindings"]["example_ic"]["package"] = "SOIC-8-EP"
        self.assertEqual(self.result()["status"], "pass")

    def test_missing_or_incomplete_unit_inventory_is_unresolved(self):
        self.parse_modified_xml(lambda root: root.find('./components/comp').remove(root.find('./components/comp/units')))
        self.assertEqual(self.result()["status"], "needs_review")
        self.assertEqual(len(self.circuit["components"]["U1"]["library_pins"]), 8)
        self.assertIn("library pins alone", self.result()["reason"])
        self.parse_modified_xml(lambda root: root.find('./components/comp/units/unit[@name="B"]').remove(root.find('./components/comp/units/unit[@name="B"]/pins')))
        self.assertEqual(self.result()["status"], "needs_review")
        self.parse_modified_xml(lambda root: root.find('./components/comp/units/unit[@name="B"]/pins/pin[@num="7"]').attrib.pop("num"))
        self.assertEqual(self.result()["status"], "needs_review")

    def test_wrong_identity_package_draft_and_changed_source_cannot_pass(self):
        original = copy.deepcopy(self.rules)
        for field, value in (("part_number", "OTHER-PART"), ("manufacturer", "OTHER-MAKER"), ("package", "OTHER-PACKAGE")):
            with self.subTest(field=field):
                self.rules = copy.deepcopy(original)
                self.rules["rules"][0][field] = value
                self.assertEqual(self.result()["status"], "needs_review")
        self.rules = copy.deepcopy(original)
        self.rules["rules"][0]["status"] = "draft"
        self.assertEqual(self.result()["status"], "not_checked")
        self.rules = copy.deepcopy(original)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "pin-table.md"
            source.write_text("revision A")
            self.rules["rules"][0]["source"].update(path=str(source), sha256=digest(source))
            self.assertEqual(self.result()["status"], "pass")
            source.write_text("revision B")
            self.assertEqual(self.result()["status"], "needs_review")

    def test_invalid_count_and_number_list_rejected(self):
        original = copy.deepcopy(self.rules)
        for patch in ({"expected_count": True}, {"expected_count": 0}, {"expected_count": 8.5}, {"expected_pin_numbers": ["1"] * 8}, {"expected_pin_numbers": [1] * 8}, {"pin_count_convention": "unknown"}):
            with self.subTest(patch=patch):
                self.rules = copy.deepcopy(original)
                self.rules["rules"][0].update(patch)
                with self.assertRaises(ValueError):
                    self.result()

    def test_net_pin_missing_from_units_is_unresolved_not_false_failure(self):
        self.parse_modified_xml(lambda root: root.find('./nets/net/node').set("pin", "99"))
        self.assertEqual(self.result()["status"], "needs_review")

    def test_old_circuit_and_cli_report(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            circuit_path = base / "circuit.json"
            write_json(circuit_path, self.circuit)
            command = [sys.executable, "-m", "kicad_checker", "review", str(circuit_path), "--snapshot", "--rules", str(EXAMPLE / "rules.json"), "--evidence", str(EXAMPLE / "evidence.json")]
            proc = subprocess.run(command + ["--output", str(base / "pass")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('"symbol_pin_count": 8', (base / "pass/report.md").read_text())
            # Older saved circuit files have no full pin inventory.
            del self.circuit["components"]["U1"]["symbol_pin_inventory"]
            write_json(circuit_path, self.circuit)
            proc = subprocess.run(command + ["--output", str(base / "old")], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(read_json(base / "old/report.json")["results"][0]["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
