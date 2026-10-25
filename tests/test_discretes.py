"""Discrete-part catalog values: test conditions, ranges, Ta/Tc suffixes and package aliases.

Strings follow DigiKey's catalog formats; products are synthetic (mutated fixture), not real alternatives.
"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kicad_checker.sourcing.requirements import evaluate_numeric, package_matches, validate_request
from kicad_checker.sourcing.service import markdown, search_report

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 24, 14, tzinfo=timezone.utc)
RDS = "Rds On (Max) @ Id, Vgs"


def c(parameter, op, value, unit, **extra):
    return {"parameter": parameter, "op": op, "value": value, "unit": unit, **extra}


def at(**conditions):
    return {name: {"value": v, "unit": u} for name, (v, u) in conditions.items()}


def mosfet(**parameters):
    p = json.loads((ROOT / "tests/fixtures/digikey/AF0402FR-0710KL.keyword.json").read_text())["body"]["ExactMatches"][0]
    p["ManufacturerProductNumber"] = "SYNTHETIC-FET"
    p["Category"] = {"Name": "Single FETs, MOSFETs", "CategoryId": 278}
    base = {"FET Type": "N-Channel", "Drain to Source Voltage (Vdss)": "30 V",
            "Current - Continuous Drain (Id) @ 25°C": "5.8A (Ta)", RDS: "28mOhm @ 5.8A, 10V",
            "Vgs(th) (Max) @ Id": "2.5V @ 250µA", "Operating Temperature": "-55°C ~ 150°C (TJ)",
            "Package / Case": "TO-236-3, SC-59, SOT-23-3", "Supplier Device Package": "SOT-23-3", **parameters}
    p["Parameters"] = [{"ParameterText": k, "ValueText": v} for k, v in base.items()]
    return p


class ConditionTests(unittest.TestCase):
    def test_conditional_value_compares_only_at_the_same_test_condition(self):
        rule = c(RDS, "max", 30, "mOhm", at=at(Vgs=(10, "V")))
        self.assertEqual(evaluate_numeric(RDS, "28mOhm @ 5.8A, 10V", rule), (True, None))
        self.assertEqual(evaluate_numeric(RDS, "35mOhm @ 5.8A, 10V", rule)[0], False)
        matched, note = evaluate_numeric(RDS, "28mOhm @ 5.8A, 4.5V", rule)
        self.assertIsNone(matched)  # 10 V data says nothing about 4.5 V gate drive.
        self.assertIn("tested at Vgs=4.5V, required at 10V", note)
        matched, note = evaluate_numeric(RDS, "28mOhm @ 5.8A, 10V", c(RDS, "max", 30, "mOhm"))
        self.assertIsNone(matched)
        self.assertIn("add 'at' (Id, Vgs)", note)
        self.assertIsNone(evaluate_numeric(RDS, "28mOhm @ 5.8A, 10V", c(RDS, "max", 30, "mOhm", at=at(Vds=(10, "V"))))[0])

    def test_condition_units_convert(self):
        vth = "Vgs(th) (Max) @ Id"
        rule = c(vth, "max", 2.5, "V", at=at(Id=(0.25, "mA")))
        self.assertEqual(evaluate_numeric(vth, "2.5V @ 250µA", rule), (True, None))
        vf = "Voltage - Forward (Vf) (Max) @ If"
        self.assertTrue(evaluate_numeric(vf, "550 mV @ 1 A", c(vf, "max", 0.6, "V", at=at(If=(1, "A"))))[0])
        self.assertIsNone(evaluate_numeric(vf, "550 mV @ 1 A", c(vf, "max", 0.6, "V", at=at(If=(3, "A"))))[0])
        hfe = "DC Current Gain (hFE) (Min) @ Ic, Vce"
        self.assertTrue(evaluate_numeric(hfe, "100 @ 2mA, 5V", c(hfe, "min", 100, "", at=at(Ic=(2, "mA"))))[0])

    def test_ranges_need_covers_and_respect_qualifier(self):
        temp = "Operating Temperature"
        self.assertEqual(evaluate_numeric(temp, "-55°C ~ 150°C (TJ)", c(temp, "covers", [-40, 125], "°C")), (True, None))
        self.assertIs(evaluate_numeric(temp, "-40°C ~ 85°C (TA)", c(temp, "covers", [-40, 125], "°C"))[0], False)
        self.assertIsNone(evaluate_numeric(temp, "-55°C ~ 150°C (TJ)", c(temp, "covers", [-40, 125], "°C", qualifier="TA"))[0])
        self.assertIsNone(evaluate_numeric(temp, "-55°C ~ 150°C (TJ)", c(temp, "min", 125, "°C"))[0])
        self.assertIsNone(evaluate_numeric(temp, "150°C (TJ)", c(temp, "covers", [-40, 125], "°C"))[0])

    def test_ta_tc_values_use_requested_qualifier_or_worst_case(self):
        idrain = "Current - Continuous Drain (Id) @ 25°C"
        both = "5.8A (Ta), 30A (Tc)"
        self.assertIs(evaluate_numeric(idrain, both, c(idrain, "min", 10, "A"))[0], False)  # worst case = Ta
        self.assertIs(evaluate_numeric(idrain, both, c(idrain, "min", 10, "A", qualifier="Tc"))[0], True)
        power = "Power Dissipation (Max)"
        self.assertIs(evaluate_numeric(power, "1.4W (Ta), 50W (Tc)", c(power, "max", 2, "W"))[0], False)
        self.assertIsNone(evaluate_numeric(idrain, "5.8A (Ta)", c(idrain, "min", 3, "A", qualifier="Tc"))[0])

    def test_validation(self):
        good = {"kind": "mosfet", "category": 278, "constraints": [
            c(RDS, "max", 30, "mOhm", at=at(Vgs=(4.5, "V"))), c("Operating Temperature", "covers", [-40, 125], "°C", qualifier="TJ")]}
        validate_request(good)
        for bad in (c("Operating Temperature", "covers", [125, -40], "°C"), c("X", "covers", 5, "V"),
                    c(RDS, "max", 30, "mOhm", at={"Vgs": "4.5V"}), c(RDS, "max", 30, "mOhm", qualifier="(Ta)"),
                    {"parameter": "FET Type", "op": "eq", "value": "N-Channel", "at": at(Vgs=(1, "V"))}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_request({**good, "constraints": [bad]})


class PackageAliasTests(unittest.TestCase):
    def test_comma_separated_names_are_one_outline(self):
        sot = "TO-236-3, SC-59, SOT-23-3"
        self.assertTrue(package_matches(sot, "SOT-23-3"))
        self.assertTrue(package_matches(sot, "sc-59"))
        self.assertIsNone(package_matches(sot, "SOT-23"))  # pin count missing: review, not rejection
        self.assertFalse(package_matches(sot, "SOT-323"))
        self.assertFalse(package_matches("SOD-123F", "SOD-123"))

    def test_mosfet_report_end_to_end(self):
        req = {"kind": "mosfet", "category": 278, "package": "SOT-23-3", "constraints": [
            {"parameter": "FET Type", "op": "eq", "value": "N-Channel"},
            c("Drain to Source Voltage (Vdss)", "min", 30, "V"),
            c(RDS, "max", 30, "mOhm", at=at(Vgs=(10, "V"))),
            c("Current - Continuous Drain (Id) @ 25°C", "min", 5, "A"),
            c("Operating Temperature", "covers", [-40, 125], "°C")]}
        report = search_report(req, [{"product": mosfet(), "observed_at": NOW.isoformat()}], now=NOW)
        row = report["results"][0]
        self.assertEqual(row["catalog_status"], "catalog_match", [x for x in row["checks"] if x["status"] != "match"])
        self.assertEqual(report["qualified_candidates"], 1)
        logic_level = {**req, "constraints": req["constraints"][:2] + [c(RDS, "max", 30, "mOhm", at=at(Vgs=(4.5, "V")))]}
        report = search_report(logic_level, [{"product": mosfet(), "observed_at": NOW.isoformat()}], now=NOW)
        self.assertEqual(report["results"][0]["catalog_status"], "needs_review")
        self.assertIn("required at 4.5V", markdown(report))


if __name__ == "__main__":
    unittest.main()
