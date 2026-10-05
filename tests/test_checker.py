import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kicad_checker.core import parse_netlist, read_json, review, write_json


ROOT = Path(__file__).resolve().parents[1]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.circuit = parse_netlist(ROOT / "examples/demo/netlist.xml")
        self.rules = read_json(ROOT / "examples/demo/rules.json")
        self.evidence = read_json(ROOT / "examples/demo/evidence.json")

    def results(self):
        return {r["id"]: r for r in review(self.circuit, self.rules, self.evidence)["results"]}

    def test_deliberate_esr_violation_and_missing_effective_capacitance(self):
        results = self.results()
        self.assertEqual(results["LDO-COUT-ESR"]["status"], "fail")
        self.assertEqual(results["LDO-COUT-EFFECTIVE"]["status"], "needs_review")
        self.assertEqual(results["LDO-COUT-NOMINAL"]["status"], "pass")
        self.assertEqual(results["LDO-COUT-OUT"]["status"], "pass")
        self.assertEqual(results["LDO-COUT-RETURN"]["status"], "needs_review")

    def test_exact_esr_boundaries_pass_and_exceeding_bound_fails(self):
        fact = self.evidence["components"]["C1"]["esr"]
        fact.update(min=5, max=500)
        self.assertEqual(self.results()["LDO-COUT-ESR"]["status"], "pass")
        fact["max"] = 501
        self.assertEqual(self.results()["LDO-COUT-ESR"]["status"], "fail")

    def test_wrong_part_unverified_and_wrong_conditions_never_pass(self):
        original = copy.deepcopy(self.evidence)
        for field, value in [("part_number", "OTHER-PART"), ("status", "draft"),
                             ("conditions_match", False), ("conditions", ""),
                             ("reviewed_by", ""), ("source", {})]:
            with self.subTest(field=field):
                self.evidence = copy.deepcopy(original)
                self.evidence["components"]["C1"]["dielectric"][field] = value
                self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "needs_review")

    def test_changed_local_rule_or_evidence_source_cannot_pass(self):
        from kicad_checker.core import digest

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "datasheet.pdf"
            source.write_bytes(b"revision A")
            pin = {"path": str(source), "sha256": digest(source)}
            self.rules["rules"][0]["source"].update(pin)
            self.evidence["components"]["C1"]["dielectric"]["source"].update(pin)
            self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "pass")
            source.write_bytes(b"revision B")
            self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "needs_review")
            self.assertIn("Requirement source", self.results()["LDO-COUT-TECH"]["reason"])
            self.rules["rules"][0]["source"].update(sha256=digest(source))
            self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "needs_review")
            self.assertIn("Evidence source", self.results()["LDO-COUT-TECH"]["reason"])

    def test_evidence_depends_on_role_and_property_not_resistor_prefix(self):
        # A schematic value check needs no manufacturer document or part number.
        self.circuit["components"]["C1"]["part_number"] = None
        nominal = self.results()["LDO-COUT-NOMINAL"]
        self.assertEqual(nominal["status"], "pass")
        self.assertEqual(nominal["evidence_needed"], "schematic")

        # A current-sense resistor's accuracy needs exact-part evidence.
        self.circuit["components"]["R1"] = copy.deepcopy(self.circuit["components"]["C1"])
        self.circuit["components"]["R1"].update(reference="R1", value="0.01 Ohm", part_number="DEMO-SENSE-1")
        self.evidence["bindings"]["sense_resistor"] = {
            "reference": "R1", "applicability": "applicable",
            "rationale": "Synthetic current-sense resistor", "reviewed_by": "Synthetic test fixture",
        }
        self.rules["rules"].append({
            "id": "SENSE-TOLERANCE", "status": "approved", "kind": "component_property",
            "target": "sense_resistor", "property": "sense_resistor_tolerance",
            "evidence_basis": "manufacturer", "operator": "range", "min": 0, "max": 0.1,
            "unit": "percent", "requirement": "Example: verify current-sense resistor tolerance",
            "source": {"locator": "Synthetic circuit requirement"},
        })
        item = self.results()["SENSE-TOLERANCE"]
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("Manufacturer specification is missing", item["reason"])
        self.evidence["components"]["R1"] = {"sense_resistor_tolerance": {
            **self.evidence["components"]["C1"]["nominal_capacitance"],
            "part_number": "DEMO-SENSE-1", "min": 0, "max": 0.05,
            "unit": "percent", "basis": "schematic",
        }}
        self.assertEqual(self.results()["SENSE-TOLERANCE"]["status"], "needs_review")
        self.evidence["components"]["R1"]["sense_resistor_tolerance"]["basis"] = "manufacturer"
        self.assertEqual(self.results()["SENSE-TOLERANCE"]["status"], "pass")

    def test_heuristic_is_a_candidate_never_an_automatic_failure(self):
        self.rules["rules"].append({
            "id": "NETWORK-LEAD", "status": "approved", "kind": "heuristic",
            "target": "output_capacitor",
            "requirement": "Assess the complete output capacitor network for stability",
            "source": {"locator": "Synthetic regulator requirement"},
            "candidate": "Summed schematic capacitance might exceed a recommended range",
            "method": "Sum nominal values and compare to a single-capacitor recommendation",
        })
        item = self.results()["NETWORK-LEAD"]
        self.assertEqual(item["status"], "needs_review")
        self.assertEqual(item["observation_class"], "heuristic_candidate")
        self.assertIn("confirm", item["reason"])
        self.circuit["components"]["C1"]["dnp"] = True
        self.assertEqual(self.results()["NETWORK-LEAD"]["status"], "needs_review")

    def test_dnp_required_capacitor_fails(self):
        self.circuit["components"]["C1"]["dnp"] = True
        self.assertEqual(self.results()["LDO-COUT-OUT"]["status"], "fail")

    def test_wrong_net_fails_missing_pin_is_unresolved(self):
        self.circuit["components"]["C1"]["pins"]["1"]["net"] = "VIN"
        self.assertEqual(self.results()["LDO-COUT-OUT"]["status"], "fail")
        del self.circuit["components"]["C1"]["pins"]["1"]
        self.assertEqual(self.results()["LDO-COUT-OUT"]["status"], "needs_review")

    def test_no_applicability_review_is_unresolved(self):
        self.evidence["bindings"]["output_capacitor"]["rationale"] = ""
        self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "needs_review")

    def test_drafts_and_unsupported_rules_cannot_disappear(self):
        self.rules["rules"][0]["status"] = "draft"
        results = self.results()
        self.assertEqual(len(results), len(self.rules["rules"]))
        self.assertEqual(results["LDO-COUT-TECH"]["status"], "not_checked")
        self.rules["rules"][0]["kind"] = "invented"
        with self.assertRaises(ValueError):
            self.results()

    def test_invalid_units_intervals_and_nan_unresolved(self):
        original = copy.deepcopy(self.evidence)
        for patch in ({"unit": "Ohm"}, {"min": 501, "max": 5}, {"min": float("nan")}, {"min": True}):
            self.evidence = copy.deepcopy(original)
            self.evidence["components"]["C1"]["esr"].update(patch)
            self.assertEqual(self.results()["LDO-COUT-ESR"]["status"], "needs_review")

    def test_reviewed_not_applicable(self):
        self.evidence["bindings"]["output_capacitor"].update(applicability="not_applicable", rationale="Synthetic alternate topology")
        self.assertTrue(all(x["status"] == "not_applicable" for x in self.results().values()))

    def test_xml_dnp_and_duplicate_pin(self):
        text = (ROOT / "examples/demo/netlist.xml").read_text()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.xml"
            path.write_text(text.replace('<comp ref="C1">', '<comp ref="C1"><property name="dnp"/>'))
            self.assertTrue(parse_netlist(path)["components"]["C1"]["dnp"])
            path.write_text(text.replace('<node ref="U1" pin="5"', '<node ref="U1" pin="1"'))
            with self.assertRaises(ValueError):
                parse_netlist(path)

    def test_cli_report_exit_code_and_source_freshness(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            circuit = base / "circuit.json"
            write_json(circuit, self.circuit)
            command = [sys.executable, "-m", "kicad_checker", "review", str(circuit), "--rules", str(ROOT / "examples/demo/rules.json"),
                       "--evidence", str(ROOT / "examples/demo/evidence.json"), "--output", str(base / "report")]
            proc = subprocess.run(command + ["--snapshot"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = read_json(base / "report/report.json")
            self.assertEqual(report["coverage"]["fail"], 1)
            self.assertTrue((base / "report/report.md").is_file())
            proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 3)
            self.assertIn("No live source hashes", proc.stderr)
            self.circuit["project_inputs"] = {str(ROOT / "examples/demo/netlist.xml"): "obsolete"}
            write_json(circuit, self.circuit)
            proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 3)
            self.assertIn("changed or is unavailable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
