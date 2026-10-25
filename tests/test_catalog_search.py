"""Any-category discovery and strict generic filtering, using offline catalog fixtures."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from kicad_checker.cli import main
from kicad_checker.sourcing.providers import DigiKeyCatalog
from kicad_checker.sourcing.requirements import catalog_number, keywords, search_filters, validate_request
from kicad_checker.sourcing.service import markdown, search_report

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 24, 14, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((ROOT / f"tests/fixtures/digikey/{name}.keyword.json").read_text())["body"]["ExactMatches"][0]


def example(name):
    return json.loads((ROOT / f"examples/sourcing/{name}.json").read_text())


def report(req, product):
    return search_report(req, [{"product": product, "observed_at": NOW.isoformat()}], now=NOW)


class CatalogSearchTests(unittest.TestCase):
    def test_every_kind_label_can_use_unrestricted_discovery(self):
        for kind in ("mosfet", "op amp", "sensor", "microcontroller", "relay", "switch", "fuse", "crystal", "ferrite bead", "resistor array", "test equipment", "component", "new future category"):
            with self.subTest(kind=kind):
                req = validate_request({"kind": kind, "keywords": "some query"})
                self.assertEqual(keywords(req), "some query")
                result = report(req, fixture("LP591233MDRVREP"))
                self.assertEqual(result["discovered_candidates"], 1)
                self.assertEqual(result["qualified_candidates"], 0)  # No category was asserted.
                self.assertIn("Discovery query", markdown(result))

    def test_real_regulator_matches_explicit_criteria_without_engineering_approval(self):
        result = report(example("regulator"), fixture("LP591233MDRVREP"))
        self.assertEqual(result["qualified_candidates"], 1)
        row = result["results"][0]
        self.assertEqual(row["engineering_status"], "requires_review")
        self.assertTrue(row["description"])
        self.assertTrue(row["category"])
        self.assertIn({"name": "Voltage Dropout (Max)", "value": "0.18V @ 500mA"}, row["parameters"])
        rendered = markdown(result)
        self.assertIn("Catalog parameters", rendered)
        self.assertIn("Datasheet:", rendered)
        self.assertIn("engineering review", rendered)

    def test_connector_has_no_case_field_and_uses_dimensionless_counts(self):
        result = report(example("connector"), fixture("5040500691"))
        self.assertEqual(result["qualified_candidates"], 1)
        req = example("connector")
        req["constraints"][0]["value"] = 8
        self.assertEqual(report(req, fixture("5040500691"))["results"][0]["catalog_status"], "rejected")

    def test_requested_package_is_never_ignored(self):
        req = {**example("connector"), "package": "SOT-23"}
        result = report(req, fixture("5040500691"))
        self.assertEqual(result["qualified_candidates"], 0)
        self.assertEqual(result["results"][0]["catalog_status"], "needs_review")

    def test_generic_category_name_id_and_unknown_id_behavior(self):
        p = fixture("LP591233MDRVREP")
        leaf = p["Category"]
        while leaf.get("ChildCategories"):
            leaf = leaf["ChildCategories"][0]
        req = {"kind": "integrated circuit", "category": leaf["CategoryId"]}
        self.assertEqual(report(req, p)["qualified_candidates"], 1)
        self.assertEqual(report({**req, "category": 99999999}, p)["results"][0]["catalog_status"], "rejected")
        self.assertEqual(report({**req, "category": "Mismatched Category"}, p)["qualified_candidates"], 0)
        p["Category"] = {"Name": "Unspecified"}
        self.assertEqual(report(req, p)["results"][0]["catalog_status"], "needs_review")

    def test_mosfet_catalog_filters_synthetic_fixture(self):
        # Mutated identity/parameters are synthetic test data, not a real alternative.
        p = fixture("LP591233MDRVREP")
        p["ManufacturerProductNumber"] = "SYNTHETIC-MOSFET"
        p["Category"] = {"Name": "FETs, MOSFETs - Single", "CategoryId": 987654}
        p["Parameters"] = [{"ParameterText": k, "ValueText": v} for k, v in
                           (("FET Type", "N-Channel"), ("Drain to Source Voltage (Vdss)", "60V"),
                            ("Package / Case", "TO-220-3"))]
        req = {"kind": "mosfet", "category": "FETs, MOSFETs - Single", "package": "TO-220-3",
               "constraints": [{"parameter": "FET Type", "op": "eq", "value": "N-Channel"},
                               {"parameter": "Drain to Source Voltage (Vdss)", "op": "min", "value": 40, "unit": "V"}]}
        self.assertEqual(report(req, p)["qualified_candidates"], 1)
        req["constraints"][1]["value"] = 100
        self.assertEqual(report(req, p)["qualified_candidates"], 0)

    def test_numeric_comparisons_preserve_signs_and_test_conditions(self):
        for text, unit, expected in (("-3.3V", "V", "-3.3"), ("500mA", "A", "0.5"), ("-40°C", "°C", "-40"),
                                     ("1MHz", "Hz", "1000000"), ("6", "", "6")):
            with self.subTest(text=text):
                self.assertEqual(catalog_number(text, unit), Decimal(expected))
        for text, unit in (("40V @ 1mA", "V"), ("-40°C ~ 125°C", "°C"), ("6 positions", ""), ("6V", ""),
                           ("3.3V nominal", "V"), ("NaN", "")):
            self.assertIsNone(catalog_number(text, unit))
        req = {"kind": "component", "category": "Test", "constraints": [{"parameter": "Voltage", "op": "min", "value": 0, "unit": "V"}]}
        p = fixture("LP591233MDRVREP")
        p["Category"] = {"Name": "Test"}
        p["Parameters"] = [{"ParameterText": "Voltage", "ValueText": "-3.3V"}]
        self.assertEqual(report(req, p)["results"][0]["catalog_status"], "rejected")

    def test_active_automotive_qualification_must_be_explicit(self):
        with self.assertRaises(ValueError):
            validate_request({"kind": "microcontroller", "automotive": True})
        req = {"kind": "microcontroller", "category": "Test", "automotive": True, "qualification": "AEC-Q100"}
        p = fixture("LP591233MDRVREP")
        p["Category"] = {"Name": "Test"}
        p["Parameters"] = [{"ParameterText": "Ratings", "ValueText": "AEC-Q100"}]
        self.assertEqual(report(req, p)["qualified_candidates"], 1)
        p["Parameters"][0]["ValueText"] = "AEC-Q200"
        self.assertEqual(report(req, p)["qualified_candidates"], 0)

    def test_generic_shorthands_and_invalid_categories_are_not_silently_ignored(self):
        for delta in ({"value": "3.3V"}, {"tolerance_percent": 1}, {"category": True}, {"category": []},
                      {"category": ""}, {"category": -1}, {"qualification": ""}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                validate_request({"kind": "component", **delta})

    def test_any_category_filter_does_not_bypass_rlc_category_checks(self):
        req = {"kind": "resistor", "value": "10k", "package": "0402", "tolerance_percent": 1,
               "category": "Voltage Regulators - Linear, Low Drop Out (LDO) Regulators"}
        self.assertEqual(report(req, fixture("LP591233MDRVREP"))["qualified_candidates"], 0)

    def test_filtered_search_caches_are_isolated_and_preserve_pagination_filters(self):
        class Client:
            def __init__(self):
                self.calls = []
            def keyword(self, query, **options):
                self.calls.append(options)
                return 200, {"ProductsCount": 100, "Products": [fixture("LP591233MDRVREP")]}
        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            catalog = DigiKeyCatalog(folder, client)
            filters = search_filters({"category": 123})
            catalog.search("regulator", max_pages=2, filters=filters)
            self.assertEqual([c["offset"] for c in client.calls], [0, 50])
            self.assertTrue(all(c["filters"] == filters for c in client.calls))
            self.assertIsNone(search_filters({"category": "Some category"}))
            self.assertEqual(len(DigiKeyCatalog(folder).search("regulator", filters=filters)[0]), 2)
            self.assertFalse(DigiKeyCatalog(folder).search("regulator", filters=search_filters({"category": 456}))[0])
            self.assertFalse(DigiKeyCatalog(folder).search("regulator")[0])

    def test_generic_cli_snapshot_and_new_examples(self):
        for name in ("catalog", "regulator", "connector"):
            validate_request(example(name))
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            snapshot = folder / "snapshot.json"
            # Stale date intentionally prevents current availability claims.
            snapshot.write_text(json.dumps({"schema_version": 1, "records": [{"product": fixture("LP591233MDRVREP"), "observed_at": "2020-01-01T00:00:00Z"}]}))
            with patch("kicad_checker.credentials.get", side_effect=AssertionError("credentials accessed")), redirect_stdout(io.StringIO()):
                status = main(["parts-search", str(ROOT / "examples/sourcing/regulator.json"),
                               "--digikey-snapshot", str(snapshot), "--output", str(folder / "out")])
            self.assertEqual(status, 2)
            row = json.loads((folder / "out/sourcing.json").read_text())["results"][0]
            self.assertEqual(row["catalog_status"], "catalog_match")
            self.assertFalse(row["counts_toward_target"])


if __name__ == "__main__":
    unittest.main()
