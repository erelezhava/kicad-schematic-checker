"""Regression tests for the 2026-10-05 review: portability, normalization,
schematic cross-check, approval reporting and grouped identification."""

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kicad_checker.core import digest, identification, parse_netlist, read_json, review, write_json
from kicad_checker.units import convert, dielectric, schematic_quantity

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples/demo"
PINS = ROOT / "examples/symbol-pins"


class Base(unittest.TestCase):
    def setUp(self):
        self.circuit = parse_netlist(DEMO / "netlist.xml")
        self.rules = read_json(DEMO / "rules.json")
        self.evidence = read_json(DEMO / "evidence.json")

    def results(self, **bases):
        return {r["id"]: r for r in review(self.circuit, self.rules, self.evidence, **bases)["results"]}

    def rule(self, rid):
        return next(r for r in self.rules["rules"] if r["id"] == rid)


class UnitNormalizationTests(Base):
    def test_unit_parser_is_exact_at_boundaries(self):
        self.assertEqual(convert(10, "uF", "nF"), 10000)
        self.assertEqual(convert(10, "µF", "uF"), 10)
        self.assertEqual(convert(500, "mOhm", "Ohm"), convert("0.5", "Ohm", "Ohm"))
        self.assertEqual(convert(1, "kΩ", "Ohm"), 1000)
        self.assertIsNone(convert(1, "uF", "V"))        # different quantity
        self.assertIsNone(convert(1, "uF", "furlong"))  # unknown unit
        self.assertEqual(convert(1, "furlong", "furlong"), 1)  # identical strings still compare

    def test_converted_evidence_passes_at_exact_boundary(self):
        fact = self.evidence["components"]["C1"]["esr"]
        fact.update(min=0.005, max=0.5, unit="Ohm")  # = 5–500 mOhm exactly
        self.assertEqual(self.results()["LDO-COUT-ESR"]["status"], "pass")

    def test_recognized_unit_outside_range_still_fails(self):
        fact = self.evidence["components"]["C1"]["esr"]
        fact.update(min=1, max=4, unit="Ohm")  # 1000–4000 mOhm, verified and out of range
        self.assertEqual(self.results()["LDO-COUT-ESR"]["status"], "fail")

    def test_unknown_or_incompatible_unit_is_unresolved(self):
        for unit in ("uF", "mohms!", ""):
            with self.subTest(unit=unit):
                self.evidence["components"]["C1"]["esr"]["unit"] = unit
                item = self.results()["LDO-COUT-ESR"]
                self.assertEqual(item["status"], "needs_review")


class DielectricNormalizationTests(Base):
    def test_names(self):
        self.assertEqual(dielectric(" x7r "), "X7R")
        self.assertEqual(dielectric("NP0"), "C0G")
        self.assertEqual(dielectric("COG"), "C0G")
        self.assertIsNone(dielectric("ceramic"))
        self.assertIsNone(dielectric("Class II"))

    def test_case_and_alias_pass(self):
        fact = self.evidence["components"]["C1"]["dielectric"]
        for value in ("x7r", " X7R", "x5r"):
            with self.subTest(value=value):
                fact["value"] = value
                self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "pass")
        self.rule("LDO-COUT-TECH")["allowed"] = ["C0G"]
        fact["value"] = "NP0"
        self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "pass")

    def test_recognized_but_disallowed_fails(self):
        self.evidence["components"]["C1"]["dielectric"]["value"] = "Y5V"
        self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "fail")

    def test_unrecognized_dielectric_is_unresolved(self):
        for value in ("ceramic", "", "X7R or X5R"):
            with self.subTest(value=value):
                self.evidence["components"]["C1"]["dielectric"]["value"] = value
                self.assertEqual(self.results()["LDO-COUT-TECH"]["status"], "needs_review")

    def test_rule_with_unrecognized_allowed_dielectric_is_rejected(self):
        self.rule("LDO-COUT-TECH")["allowed"] = ["X7R", "ceramic"]
        with self.assertRaises(ValueError):
            self.results()

    def test_generic_one_of_ignores_case_and_spacing(self):
        self.rules["rules"].append({**self.rule("LDO-COUT-TECH"), "id": "GENERIC", "property": "mounting",
                                    "allowed": ["Surface Mount"]})
        self.evidence["components"]["C1"]["mounting"] = {**self.evidence["components"]["C1"]["dielectric"],
                                                         "value": "surface  mount"}
        self.assertEqual(self.results()["GENERIC"]["status"], "pass")
        self.evidence["components"]["C1"]["mounting"]["value"] = "through hole"
        self.assertEqual(self.results()["GENERIC"]["status"], "fail")


class SchematicCrossCheckTests(Base):
    def test_value_parser(self):
        cases = {("2.2 uF", "uF"): 2.2, ("2u2", "uF"): 2.2, ("100nF/25V", "uF"): 0.1, ("2.2uF 25V X7R", "uF"): 2.2,
                 ("4k7", "kOhm"): 4.7, ("10k 1%", "Ohm"): 10000, ("0R", "Ohm"): 0, ("R10", "Ohm"): 0.1}
        for (text, unit), expected in cases.items():
            with self.subTest(text=text):
                value, reason = schematic_quantity(text, unit)
                self.assertIsNone(reason)
                self.assertEqual(value, convert(expected, unit, unit))
        for text in ("1206", "10k", "DEMO", "10uF 22uF", ""):
            with self.subTest(text=text):
                self.assertIsNone(schematic_quantity(text, "uF")[0])

    def test_fabricated_schematic_fact_cannot_pass(self):
        # Review finding B1: a typed 4.7 uF used to pass against a saved 2.2 uF.
        self.evidence["components"]["C1"]["nominal_capacitance"].update(min=4.7, max=4.7)
        item = self.results()["LDO-COUT-NOMINAL"]
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("disagrees with the saved schematic", item["reason"])

    def test_agreeing_fact_in_other_unit_passes(self):
        self.evidence["components"]["C1"]["nominal_capacitance"].update(min=2200, max=2200, unit="nF")
        item = self.results()["LDO-COUT-NOMINAL"]
        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["observation"]["schematic_text"], "2.2 uF")

    def test_rule_level_field_needs_no_typed_fact(self):
        del self.evidence["components"]["C1"]["nominal_capacitance"]
        self.assertEqual(self.results()["LDO-COUT-NOMINAL"]["status"], "pass")
        self.circuit["components"]["C1"]["value"] = "22uF"
        self.assertEqual(self.results()["LDO-COUT-NOMINAL"]["status"], "fail")
        for value in ("DEMO", "", "1uF 22uF"):
            with self.subTest(value=value):
                self.circuit["components"]["C1"]["value"] = value
                self.assertEqual(self.results()["LDO-COUT-NOMINAL"]["status"], "needs_review")

    def test_field_must_be_named_and_consistent(self):
        del self.rule("LDO-COUT-NOMINAL")["schematic_field"]
        del self.evidence["components"]["C1"]["nominal_capacitance"]["field"]
        item = self.results()["LDO-COUT-NOMINAL"]
        self.assertEqual(item["status"], "needs_review")
        self.assertIn("name the KiCad field", item["reason"])
        self.rule("LDO-COUT-NOMINAL")["schematic_field"] = "Value"
        self.evidence["components"]["C1"]["nominal_capacitance"]["field"] = "Capacitance"
        self.assertEqual(self.results()["LDO-COUT-NOMINAL"]["status"], "needs_review")

    def test_schematic_field_only_with_schematic_basis(self):
        self.rule("LDO-COUT-ESR")["schematic_field"] = "Value"
        with self.assertRaises(ValueError):
            self.results()


class RelativeSourceTests(Base):
    def pin(self, folder, name="datasheet.pdf", content=b"rev A"):
        path = Path(folder) / "docs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        return path, {"path": f"docs/{name}", "sha256": digest(path)}

    def test_relative_paths_resolve_against_base_and_keep_hash_check(self):
        with tempfile.TemporaryDirectory() as folder:
            path, source = self.pin(folder)
            self.rule("LDO-COUT-TECH")["source"].update(source)
            self.evidence["components"]["C1"]["dielectric"]["source"].update(source)
            self.assertEqual(self.results(rules_base=folder, evidence_base=folder)["LDO-COUT-TECH"]["status"], "pass")
            item = self.results()["LDO-COUT-TECH"]  # no base supplied
            self.assertEqual(item["status"], "needs_review")
            self.assertIn("no base directory", item["reason"])
            path.write_bytes(b"rev B")
            item = self.results(rules_base=folder, evidence_base=folder)["LDO-COUT-TECH"]
            self.assertEqual(item["status"], "needs_review")
            self.assertIn("SHA-256 differs", item["reason"])

    def test_cli_relative_to_case_folder_and_docs_root(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            case = base / "case"
            case.mkdir()
            _, source = self.pin(case)
            self.rule("LDO-COUT-TECH")["source"].update(source)
            self.evidence["components"]["C1"]["dielectric"]["source"].update(source)
            write_json(case / "rules.json", self.rules)
            write_json(case / "evidence.json", self.evidence)
            write_json(base / "circuit.json", self.circuit)

            def run(out, *extra):
                command = [sys.executable, "-m", "kicad_checker", "review", str(base / "circuit.json"), "--snapshot",
                           "--rules", str(case / "rules.json"), "--evidence", str(case / "evidence.json"),
                           "--output", str(base / out), *extra]
                proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
                return proc, read_json(base / out / "report.json") if (base / out / "report.json").exists() else None

            proc, report = run("r1")
            self.assertEqual(proc.returncode, 1, proc.stderr)  # demo ESR violation is still reported
            tech = next(r for r in report["results"] if r["id"] == "LDO-COUT-TECH")
            self.assertEqual(tech["status"], "pass")

            moved = base / "elsewhere"
            (moved / "docs").mkdir(parents=True)
            (moved / "docs/datasheet.pdf").write_bytes((case / "docs/datasheet.pdf").read_bytes())
            proc, report = run("r2", "--docs-root", str(moved))
            tech = next(r for r in report["results"] if r["id"] == "LDO-COUT-TECH")
            self.assertEqual(tech["status"], "pass")
            self.assertEqual(report["source_base"]["rules"], str(moved.resolve()))

            proc, _ = run("r3", "--docs-root", str(base / "missing"))
            self.assertEqual(proc.returncode, 3)


class ApprovalReportingTests(Base):
    def test_passes_are_agent_only_until_every_record_has_human_approval(self):
        report = review(self.circuit, self.rules, self.evidence)
        self.assertEqual(report["approval_summary"]["pass_human_approved"], 0)
        self.assertEqual(report["approval_summary"]["pass_agent_only"], report["coverage"]["pass"])
        tech = self.rule("LDO-COUT-TECH")
        tech["approved_by_human"] = "Erekle"
        self.assertEqual(self.results()["LDO-COUT-TECH"]["approval"], "agent_only")
        self.evidence["bindings"]["output_capacitor"]["approved_by_human"] = "Erekle"
        self.evidence["components"]["C1"]["dielectric"]["approved_by_human"] = "Erekle"
        item = self.results()["LDO-COUT-TECH"]
        self.assertEqual(item["approval"], "human")
        self.assertEqual(item["status"], "pass")  # reporting only; status unchanged

    def test_approval_never_changes_status(self):
        before = {k: v["status"] for k, v in self.results().items()}
        for rule in self.rules["rules"]:
            rule["approved_by_human"] = "Erekle"
        self.assertEqual(before, {k: v["status"] for k, v in self.results().items()})
        self.rules["rules"][0]["approved_by_human"] = 1
        with self.assertRaises(ValueError):
            self.results()


class IdentificationGroupingTests(unittest.TestCase):
    def test_grouping_keeps_every_component_unresolved(self):
        circuit = parse_netlist(DEMO / "netlist.xml")
        circuit["components"]["C2"] = {**copy.deepcopy(circuit["components"]["C1"]), "reference": "C2"}
        circuit["components"]["H1"] = {"reference": "H1", "fields": {}, "dnp": False, "sheet_path": "/",
                                       "symbol_library": "Mechanical", "pins": {}}
        report = identification(circuit)
        groups = report["groups"]
        self.assertEqual(groups["missing_manufacturer_mpn_present"]["DEMO-CAP-1"], ["C1", "C2"])
        self.assertEqual(groups["missing_manufacturer_and_mpn"], ["H1"])
        self.assertEqual(report["coverage"]["needs_review"], 4)  # mounting hole is not auto-exempt
        self.assertEqual(next(r for r in report["results"] if r["reference"] == "H1")["status"], "needs_review")

    def test_markdown_shows_grouped_summary(self):
        from kicad_checker.core import identification_markdown
        text = identification_markdown(identification(parse_netlist(DEMO / "netlist.xml")))
        self.assertIn("What to fix (grouped)", text)
        self.assertIn("`DEMO-CAP-1`: C1", text)


class PackageNormalizationTests(unittest.TestCase):
    def test_package_name_case_and_spacing(self):
        circuit = parse_netlist(PINS / "netlist.xml")
        rules, evidence = read_json(PINS / "rules.json"), read_json(PINS / "evidence.json")
        evidence["bindings"]["example_ic"]["package"] = " soic-8 "
        self.assertEqual(review(circuit, rules, evidence)["results"][0]["status"], "pass")
        evidence["bindings"]["example_ic"]["package"] = "SOIC-14"
        self.assertEqual(review(circuit, rules, evidence)["results"][0]["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
