"""Phase B: DigiKey parametric value filter for R/C/L when keyword search falls short. Mocked HTTP only."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kicad_checker.cli import main
from kicad_checker.sourcing.providers import parametric_filters
from kicad_checker.sourcing.requirements import parametric_request, validate_request

TREE = [{"CategoryId": 2, "Name": "Resistors", "ProductCount": 1, "Children": [
    {"CategoryId": 52, "ParentId": 2, "Name": "Chip Resistor - Surface Mount", "ProductCount": 1, "Children": []}]}]
FILTERS = {"ParametricFilters": [
    {"Category": {"Id": 52}, "ParameterType": "String", "ParameterId": 2085, "ParameterName": "Resistance",
     "FilterValues": [{"ProductCount": 900, "ValueId": "1.1k-id", "ValueName": "1.1 kOhms"},
                      {"ProductCount": 40, "ValueId": "110-id", "ValueName": "110 Ohms"},
                      {"ProductCount": 700, "ValueId": "110k-id", "ValueName": "110 kOhms"}]},
    {"ParameterId": 16, "ParameterName": "Package / Case",
     "FilterValues": [{"ValueId": "0402-id", "ValueName": "0402 (1005 Metric)"},
                      {"ValueId": "0402m-id", "ValueName": "01005 (0402 Metric)"}]},
    {"ParameterId": 3, "ParameterName": "Tolerance", "FilterValues": [{"ValueId": "1-id", "ValueName": "±1%"}]}]}


def resistor(mpn, manufacturer, value):
    return {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": manufacturer}, "ProductStatus": {"Status": "Active"},
            "NormallyStocking": True, "Discontinued": False, "EndOfLife": False, "QuantityAvailable": 50000,
            "Description": {"ProductDescription": f"RES {value} 1% 0402"},
            "Category": {"Name": "Resistors", "ChildCategories": [{"Name": "Chip Resistor - Surface Mount"}]},
            "Parameters": [{"ParameterText": k, "ValueText": v} for k, v in {
                "Resistance": value, "Tolerance": "±1%", "Package / Case": "0402 (1005 Metric)",
                "Power (Watts)": "0.063W, 1/16W"}.items()],
            "ProductVariations": [{"DigiKeyProductNumber": f"{mpn}-CT", "PackageType": {"Name": "Cut Tape (CT)"},
                                   "QuantityAvailableforPackageType": 50000, "MinimumOrderQuantity": 1,
                                   "MarketPlace": False}]}


class FakeClient:
    def __init__(self, offer_value=True, reject_parametric=False):
        self.calls, self.offer_value, self.reject_parametric = [], offer_value, reject_parametric

    def categories(self):
        return 200, {"Categories": TREE}

    def keyword(self, query, **options):
        self.calls.append((query, options))
        parametric = "ParameterFilterRequest" in (options.get("filters") or {})
        if parametric and self.reject_parametric:
            return 400, {"detail": "SENSITIVE"}
        if parametric:
            good = [resistor("RK73H1ETTP1100F", "KOA Speer Electronics, Inc.", "110 Ohms"),
                    resistor("CRCW0402110RFKED", "Vishay Dale", "110 Ohms"),
                    resistor("RMCF0402FT110R", "Stackpole Electronics Inc", "110 Ohms")]
            return 200, {"ProductsCount": 3, "Products": good}
        noise = [resistor("RC0402FR-071K1L", "YAGEO", "1.1 kOhms"), resistor("RC0402FR-07110KL", "YAGEO", "110 kOhms")]
        filters = FILTERS if self.offer_value else {"ParametricFilters": FILTERS["ParametricFilters"][1:]}
        return 200, {"ProductsCount": 2, "Products": noise, "FilterOptions": filters}


REQUEST = {"schema_version": 1, "kind": "resistor", "value": "110", "package": "0402", "tolerance_percent": 1,
           "quantity": 10, "min_stock": 10, "min_alternatives": 3}


class ParametricTests(unittest.TestCase):
    def test_parse_and_pick_exact_value_and_package(self):
        options = parametric_filters({"FilterOptions": FILTERS})
        self.assertEqual(options[0]["parameter_id"], 2085)
        self.assertEqual(options[0]["values"][1], {"id": "110-id", "name": "110 Ohms", "count": 40})
        request, reason = parametric_request(validate_request(REQUEST), 52, options)
        self.assertIsNone(reason)
        self.assertEqual(request, {"CategoryFilter": {"Id": "52"}, "ParameterFilters": [
            {"ParameterId": 2085, "FilterValues": [{"Id": "110-id"}]},      # not 1.1k, not 110k
            {"ParameterId": 16, "FilterValues": [{"Id": "0402-id"}]}]})     # imperial 0402 only
        self.assertEqual(parametric_filters({}), [])
        self.assertEqual(parametric_filters({"FilterOptions": {"ParametricFilters": [{"ParameterId": 1}]}}), [])

    def test_no_filter_when_value_not_offered_or_not_rlc(self):
        options = parametric_filters({"FilterOptions": {"ParametricFilters": FILTERS["ParametricFilters"][1:]}})
        request, reason = parametric_request(validate_request(REQUEST), 52, options)
        self.assertIsNone(request)
        self.assertIn("did not offer", reason)
        request, reason = parametric_request(validate_request({"kind": "mosfet", "category": 278}), 278, options)
        self.assertIsNone(request)

    def run_search(self, client, folder):
        path = Path(folder) / "request.json"
        path.write_text(json.dumps(REQUEST), encoding="utf-8")
        with patch("kicad_checker.digikey.load_credentials", return_value=("ID", "SECRET")), \
                patch("kicad_checker.digikey.DigiKeyClient", return_value=client), \
                patch("kicad_checker.sourcing.cli.configured_lcsc", return_value=None), \
                patch("kicad_checker.sourcing.cli.configured_jlc", return_value=None), \
                redirect_stdout(io.StringIO()):
            main(["parts-search", str(path), "--online", "--max-pages", "1", "--cache", str(Path(folder) / "cache"),
                  "--output", str(Path(folder) / "out")])
        return json.loads((Path(folder) / "out" / "sourcing.json").read_text(encoding="utf-8"))

    def test_keyword_shortfall_triggers_parametric_search_and_meets_target(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            report = self.run_search(client, folder)
        self.assertEqual(report["status"], "target_met")
        self.assertEqual(report["distinct_manufacturers"], 3)
        query, options = client.calls[-1]
        self.assertEqual(query, "resistor 0402")
        self.assertEqual(options["filters"]["ParameterFilterRequest"]["ParameterFilters"][0],
                         {"ParameterId": 2085, "FilterValues": [{"Id": "110-id"}]})
        self.assertEqual(options["filters"]["CategoryFilter"], [{"Id": "52"}])  # phase A filters kept
        self.assertTrue(report["search"]["parametric"]["used"])
        rejected = [r for r in report["results"] if r["mpn"] == "RC0402FR-07110KL"]
        self.assertEqual(rejected[0]["catalog_status"], "rejected")  # keyword noise still screened out

    def test_value_not_offered_or_filter_rejected_keeps_keyword_results(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient(offer_value=False)
            report = self.run_search(client, folder)
        self.assertFalse(report["search"]["parametric"]["used"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(report["status"], "shortfall")
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient(reject_parametric=True)
            report = self.run_search(client, folder)
        self.assertEqual(report["status"], "shortfall")
        self.assertTrue(any("parametric value filter unavailable" in w for w in report["warnings"]))
        self.assertFalse(any("SENSITIVE" in w for w in report["warnings"]))
        self.assertEqual(len(report["results"]), 2)


if __name__ == "__main__":
    unittest.main()
