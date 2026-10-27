"""Offline tests for parts-list: recommended parts list → DigiKey identity/lifecycle/stock, stock
comparison and alternatives for problem rows. Synthetic data; DigiKey answers are seeded into a
temporary cache, credentials are patched out, supplier runs are mocked."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kicad_checker.cli import main
from kicad_checker.sourcing.partslist import find_product, row_status
from kicad_checker.sourcing.providers import DigiKeyCatalog
from tests.test_inventory import JLC_CSV

LIST_CSV = """Recommended parts - SYNTHETIC
Ref Des,Manufacturer,Manufacturer Part Number,Qty per board,Description,LCSC
R1,YAGEO,RC0402FR-0710KL,2,10k 1% 0402,
R2,Acme,OLD-RES-1,1,10k 1% 0402 legacy,
C1,Murata,GCM155R71C104KA55J,4,100nF X7R 0402,
U1,,NOPE-123,1,typo part,
U2,,AMBIG-1,1,generic name,
U3,Texas Instruments,LP591233MDRVREP,1,LDO,
J1,,,1,only a C-code,C123456
"""


def variation(pn, stock, moq=1):
    return {"DigiKeyProductNumber": pn, "PackageType": {"Name": "Cut Tape (CT)"}, "QuantityAvailableforPackageType": stock,
            "MinimumOrderQuantity": moq, "MarketPlace": False}


def product(mpn, manufacturer, category, params, *, status="Active", stocking=True, stock=50000):
    leaf = {"resistor": "Chip Resistor - Surface Mount", "capacitor": "Ceramic Capacitors"}.get(category, category)
    return {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": manufacturer}, "ProductStatus": {"Status": status},
            "NormallyStocking": stocking, "Discontinued": status == "Obsolete", "EndOfLife": False,
            "Description": {"ProductDescription": f"{mpn} synthetic"}, "DatasheetUrl": f"https://example.invalid/{mpn}.pdf",
            "ProductUrl": f"https://example.invalid/{mpn}",
            "Category": {"Name": "Parent", "ChildCategories": [{"Name": leaf}]},
            "Parameters": [{"ParameterText": k, "ValueText": v} for k, v in params.items()],
            "ProductVariations": [variation(f"{mpn}-CT", stock)]}


RES = {"Resistance": "10 kOhms", "Tolerance": "±1%", "Package / Case": "0402 (1005 Metric)", "Power (Watts)": "0.063W, 1/16W"}
CATALOG = {
    "RC0402FR-0710KL": [product("RC0402FR-0710KL", "YAGEO", "resistor", {**RES, "Ratings": "-"})],
    "OLD-RES-1": [product("OLD-RES-1", "Acme Inc.", "resistor", {**RES, "Ratings": "AEC-Q200"}, status="Obsolete", stock=0)],
    "GCM155R71C104KA55J": [product("GCM155R71C104KA55J", "Murata Electronics", "capacitor",
                                   {"Capacitance": "0.1 µF", "Tolerance": "±10%", "Voltage - Rated": "16V",
                                    "Temperature Coefficient": "X7R", "Package / Case": "0402 (1005 Metric)", "Ratings": "AEC-Q200"})],
    "NOPE-123": [],
    "AMBIG-1": [product("AMBIG-1", "Maker One", "PMIC", {}), product("AMBIG-1", "Maker Two", "PMIC", {})],
    "LP591233MDRVREP": [product("LP591233MDRVREP", "Texas Instruments", "Voltage Regulators - Linear", {}, stocking=False)],
    "CRCW040210K0FKED": [product("CRCW040210K0FKED", "Vishay Dale", "resistor", {**RES, "Ratings": "Automotive AEC-Q200"})],
}


def seed(cache):
    for mpn, products in CATALOG.items():
        client = mock.Mock(keyword=mock.Mock(return_value=(200, {"ExactMatches": products, "Products": []})))
        DigiKeyCatalog(cache, client).page(mpn, 0, limit=10, refresh=True, exact=True)


class PartsListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "list.csv").write_text(LIST_CSV, encoding="utf-8")
        (self.dir / "stock.csv").write_text(JLC_CSV, encoding="utf-8")
        seed(self.dir / "cache" / "digikey")
        patcher = mock.patch("kicad_checker.digikey.load_credentials", side_effect=ValueError("no credentials in tests"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_list(self, *extra, folder="out"):
        searched = []

        def fake_run(args):
            if Path(args.request).name == "request.json":
                searched.append(json.loads(Path(args.request).read_text(encoding="utf-8")))
            out = Path(args.output)
            out.mkdir(parents=True)
            results = [{"manufacturer": m, "mpn": f"{m}-1", "counts_toward_target": True,
                        "suppliers": {"digikey": {"max_reported_stock": 9000}, "lcsc": {"max_reported_stock": None}}}
                       for m in ("KOA Speer Electronics, Inc.", "Vishay Dale", "Panasonic Industry")]
            (out / "sourcing.json").write_text(json.dumps({"status": "target_met", "qualified_candidates": 3, "target": 3,
                                                           "distinct_manufacturers": 3, "discovered_candidates": 3,
                                                           "warnings": [], "results": results}), encoding="utf-8")
            return 0

        text = io.StringIO()
        with mock.patch("kicad_checker.sourcing.cli.run", fake_run), redirect_stdout(text):
            code = main(["parts-list", str(self.dir / "list.csv"), "--cache", str(self.dir / "cache"),
                         "--output", str(self.dir / folder), *extra])
        report = json.loads((self.dir / folder / "parts-list.json").read_text(encoding="utf-8")) if code != 3 else None
        return code, report, searched, text.getvalue()

    def test_statuses_per_row(self):
        code, report, _, out = self.run_list("--boards", "5", "--no-find-alternatives")
        self.assertEqual(code, 2)
        rows = {r["mpn"] or r["lcsc"]: r for r in report["rows"]}
        self.assertEqual(rows["RC0402FR-0710KL"]["status"], "available")
        self.assertEqual(rows["RC0402FR-0710KL"]["need"]["needed"], 12)          # 2 per board × 5 + 2 spare
        self.assertEqual(rows["GCM155R71C104KA55J"]["status"], "available")       # "Murata" = "Murata Electronics"
        self.assertEqual(rows["GCM155R71C104KA55J"]["catalog"]["manufacturer"], "Murata Electronics")
        self.assertTrue(rows["GCM155R71C104KA55J"]["catalog"]["aec_q200"])
        self.assertEqual(rows["OLD-RES-1"]["status"], "not_active")
        self.assertEqual(rows["NOPE-123"]["status"], "not_found")
        self.assertEqual(rows["AMBIG-1"]["status"], "ambiguous")
        self.assertEqual(rows["LP591233MDRVREP"]["status"], "stock_issues")       # not normally stocking
        self.assertIn("normally stocked not confirmed", rows["LP591233MDRVREP"]["suppliers"]["digikey"]["issues"])
        self.assertEqual(rows["C123456"]["status"], "no_mpn")
        self.assertIn("Qty per board", report["list"]["columns"].values())
        md = (self.dir / "out" / "parts-list.md").read_text(encoding="utf-8")
        self.assertIn("[datasheet](https://example.invalid/RC0402FR-0710KL.pdf)", md)
        csv_text = (self.dir / "out" / "parts-list.csv").read_text(encoding="utf-8")
        self.assertIn("RC0402FR-0710KL-CT", csv_text)                              # DigiKey PN column
        self.assertIn("DigiKey exact lookups", out)

    def test_stock_comparison_and_alternatives_for_problem_rows(self):
        code, report, searched, _ = self.run_list("--boards", "10", "--stock", f"JLC={self.dir / 'stock.csv'}")
        rows = {r["mpn"] or r["lcsc"]: r for r in report["rows"]}
        self.assertEqual(rows["RC0402FR-0710KL"]["stock"]["status"], "in_stock")
        self.assertEqual(rows["GCM155R71C104KA55J"]["stock"]["status"], "in_stock")
        old = rows["OLD-RES-1"]
        self.assertIn("from the recommended part's DigiKey record", old["requirements"])
        self.assertIn("AEC-Q200", old["requirements"])
        alts = {a["mpn"]: a for a in old["stock_alternatives"]}
        self.assertNotIn("AC0402JR-0710KL", alts)                                  # 5 % rejected by the 1 % requirement
        self.assertEqual(alts["CRCW040210K0FKED"]["spec_status"], "match")         # AEC-Q200 on DigiKey
        self.assertFalse(alts["CRCW040210K0FKED"]["covers_need"])                  # only 8 pcs for 12 needed
        self.assertEqual(alts["RC0402FR-0710KL"]["spec_status"], "possible")       # AEC-Q200 not listed
        self.assertEqual(old["market_alternatives"]["status"], "target_met")
        self.assertEqual(old["market_alternatives"]["other_manufacturers"], 3)
        self.assertEqual([(q["kind"], q["value"], q["automotive"]) for q in searched], [("resistor", "10k", True)])
        self.assertIn("R/C/L", rows["LP591233MDRVREP"]["alternatives_note"])       # IC: said, not ignored
        self.assertNotIn("market_alternatives", rows["RC0402FR-0710KL"])           # healthy rows are not searched
        md = (self.dir / "out" / "parts-list.md").read_text(encoding="utf-8")
        self.assertIn("## Problem parts and alternatives", md)
        self.assertIn("Our stock", md)

    def test_offline_without_cache_and_existing_output(self):
        code, report, _, _ = self.run_list("--cache", str(self.dir / "empty-cache"), folder="o2")
        self.assertTrue(all(r["status"] in ("not_checked", "no_mpn") for r in report["rows"]))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["parts-list", str(self.dir / "list.csv"), "--output", str(self.dir / "o2")]), 3)

    def test_recommended_inductor_ratings_become_requirements(self):
        from kicad_checker.sourcing.inventory import _request, inductor_current_known, spec_from_product
        from tests.test_inventory import dk_inductor
        spec = spec_from_product(dk_inductor("LPS3015-222MRC", "Coilcraft"))
        self.assertEqual((spec["class"], spec["package"], str(spec["isat"]), spec["shielded"]), ("inductor", "1210", "2.1", True))
        request = _request(spec, {"needed": 7}, spec["tolerance_percent"])
        self.assertIn({"parameter": "Current - Saturation (Isat)", "op": "min", "value": "2.1", "unit": "A"}, request["constraints"])
        bare = spec_from_product(dk_inductor("NO-CURRENT", "X", isat="-", irms="-"))
        self.assertFalse(inductor_current_known(bare))

    def test_find_product_and_status_rules(self):
        products = CATALOG["AMBIG-1"]
        self.assertEqual(find_product("AMBIG-1", None, products)[1], "ambiguous")
        self.assertEqual(find_product("AMBIG-1", "Maker Two", products)[0]["Manufacturer"]["Name"], "Maker Two")
        self.assertEqual(find_product("AMBIG-1", "Other", products)[1], "manufacturer_differs")
        self.assertEqual(find_product("X", None, [])[1], "not_found")
        facts = {"lifecycle": "Last Time Buy", "flags": []}
        self.assertEqual(row_status("found", facts, {"digikey": {"status": "available"}}), "not_active")


if __name__ == "__main__":
    unittest.main()
