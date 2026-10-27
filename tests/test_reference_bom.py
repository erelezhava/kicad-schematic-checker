"""Reference-design BOMs (TI/ADI evaluation boards): column detection, designators, not-fitted rows,
vendor-listed alternates. Synthetic data; DigiKey answers seeded into a temporary cache."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kicad_checker.cli import main
from kicad_checker.sourcing.partslist import count_references
from kicad_checker.sourcing.stockfile import alternate_of, load_stock, parse_fitted
from kicad_checker.sourcing.providers import DigiKeyCatalog
from tests.test_partslist import RES, product

TI_BOM = """Designator,Quantity,Value,Description,PackageReference,PartNumber,Manufacturer,Alternate PartNumber,Alternate Manufacturer
!PCB1,1,,Printed Circuit Board,,LP5912EVM,Any,-,-
C1,1,1uF,"CAP, CERM, 1 uF, 16 V, +/- 10%, X7R, 0603",0603,GRM188R71C105KA12D,MuRata,C1608X7R1C105K080AC,TDK
"C2, C3",2,10uF,"CAP, CERM, 10 uF, 10 V, X5R, 0805",0805,GRM21BR61A106KE19L,MuRata,,
R1,0,10k,"RES, 10 k, 1%, 0.1 W, 0603",0603,CRCW060310K0FKEA,Vishay-Dale,,
U1,1,,LDO,DRV0006A,LP591233MDRVREP,Texas Instruments,,
U2,1,,Buffer,,SN74TYPO1,Texas Instruments,SN74LVC1G17DBVR,Texas Instruments
"""

ADI_BOM = """Item,Qty,Reference Designator,Description,Manufacturer 1,Manufacturer Part Number 1,Manufacturer 2,Manufacturer Part Number 2,Populate
1,,"C1-C3, C7",CAP CER 0.1UF 16V X7R 0402,Murata,GRM155R71C104KA88D,Samsung,CL05B104KO5NNNC,Yes
2,1,R5,RES 0R 0402,Yageo,RC0402JR-070RL,,,No
3,1,L1,IND 2.2UH,Coilcraft,LPS3015-222MRC / XFL3012-222MEC,,,Yes
"""

CAP = {"Tolerance": "±10%", "Temperature Coefficient": "X7R", "Ratings": "-"}
CATALOG = {
    "GRM188R71C105KA12D": [product("GRM188R71C105KA12D", "Murata Electronics", "capacitor",
                                   {**CAP, "Capacitance": "1 µF", "Voltage - Rated": "16V", "Package / Case": "0603 (1608 Metric)"},
                                   status="Obsolete", stock=0)],
    "C1608X7R1C105K080AC": [product("C1608X7R1C105K080AC", "TDK Corporation", "capacitor",
                                    {**CAP, "Capacitance": "1 µF", "Voltage - Rated": "16V", "Package / Case": "0603 (1608 Metric)"})],
    "GRM21BR61A106KE19L": [product("GRM21BR61A106KE19L", "Murata Electronics", "capacitor",
                                   {**CAP, "Capacitance": "10 µF", "Voltage - Rated": "10V", "Package / Case": "0805 (2012 Metric)"})],
    "LP591233MDRVREP": [product("LP591233MDRVREP", "Texas Instruments", "Voltage Regulators - Linear", {})],
    "SN74TYPO1": [],
    "SN74LVC1G17DBVR": [product("SN74LVC1G17DBVR", "Texas Instruments", "Buffers", {})],
}


CATALOG["CL21A106KAYNNNE"] = [product("CL21A106KAYNNNE", "Samsung Electro-Mechanics", "capacitor",
                                     {**CAP, "Temperature Coefficient": "X5R", "Capacitance": "10 µF",
                                      "Voltage - Rated": "25V", "Package / Case": "0805 (2012 Metric)"})]
CATALOG["CL21B106KOQNNNE"] = [product("CL21B106KOQNNNE", "Samsung Electro-Mechanics", "capacitor",
                                     {**CAP, "Capacitance": "10 µF", "Voltage - Rated": "16V",
                                      "Package / Case": "0805 (2012 Metric)"})]


def copied_design(parts):
    from tests.test_inventory import netlist
    return netlist([(ref, value, fp, [("MPN", mpn), ("Manufacturer", maker)]) for ref, value, fp, mpn, maker in parts])


class ColumnTests(unittest.TestCase):
    def test_alternate_and_fitted_helpers(self):
        self.assertEqual(alternate_of("Alternate PartNumber"), ("mpn", 2))
        self.assertEqual(alternate_of("Alt. Manufacturer"), ("manufacturer", 2))
        self.assertEqual(alternate_of("Manufacturer Part Number 3"), ("mpn", 3))
        self.assertEqual(alternate_of("Manufacturer 2"), ("manufacturer", 2))
        self.assertIsNone(alternate_of("Manufacturer 1"))
        self.assertIsNone(alternate_of("Subcategory"))
        self.assertIsNone(alternate_of("Description"))
        self.assertEqual([parse_fitted(x) for x in ("DNP", "Not Fitted", "No", "Yes", "Fitted", "", "maybe")],
                         [False, False, False, True, True, None, None])
        self.assertEqual([count_references(x) for x in ("C1, C2", "C1-C4", "R1 R3-R5", "C1-C3, C7", "", "U1")],
                         [2, 4, 4, 4, None, 1])

    def test_ti_and_adi_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            ti, adi = Path(folder) / "ti.csv", Path(folder) / "adi.csv"
            ti.write_text(TI_BOM, encoding="utf-8")
            adi.write_text(ADI_BOM, encoding="utf-8")
            t = load_stock(ti, require_quantity=False)
            a = load_stock(adi, require_quantity=False)
        self.assertEqual((t["columns"]["mpn"], t["columns"]["references"], t["columns"]["package"]),
                         ("PartNumber", "Designator", "PackageReference"))
        self.assertEqual(t["columns"]["alternates"], ["Alternate Manufacturer + Alternate PartNumber"])
        c1 = t["entries"][1]
        self.assertEqual(c1["alternates"], [{"mpn": "C1608X7R1C105K080AC", "manufacturer": "TDK"}])
        self.assertEqual(t["entries"][0]["alternates"], [])  # '-' is not an alternate
        self.assertEqual((a["columns"]["mpn"], a["columns"]["manufacturer"], a["columns"]["fitted"]),
                         ("Manufacturer Part Number 1", "Manufacturer 1", "Populate"))
        self.assertEqual(a["entries"][0]["alternates"], [{"mpn": "CL05B104KO5NNNC", "manufacturer": "Samsung"}])
        self.assertEqual(a["entries"][1]["fitted"], False)
        l1 = a["entries"][2]
        self.assertEqual((l1["mpn"], l1["alternates"][0]["mpn"]), ("LPS3015-222MRC", "XFL3012-222MEC"))  # two MPNs in one cell
        self.assertEqual(l1["references"], "L1")


class ReferenceBomRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        cache = self.dir / "cache" / "digikey"
        for mpn, products in CATALOG.items():
            client = mock.Mock(keyword=mock.Mock(return_value=(200, {"ExactMatches": products, "Products": []})))
            DigiKeyCatalog(cache, client).page(mpn, 0, limit=10, refresh=True, exact=True)
        patcher = mock.patch("kicad_checker.digikey.load_credentials", side_effect=ValueError("no credentials in tests"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_bom(self, text, *extra):
        (self.dir / "bom.csv").write_text(text, encoding="utf-8")
        searched = []

        def fake_run(args):
            searched.append(args.request)
            raise AssertionError("own search must not run when a vendor alternate covers the row")

        with mock.patch("kicad_checker.sourcing.cli.run", fake_run), redirect_stdout(io.StringIO()) as out:
            code = main(["parts-list", str(self.dir / "bom.csv"), "--cache", str(self.dir / "cache"), "--boards", "5",
                         "--output", str(self.dir / "out"), *extra])
        report = json.loads((self.dir / "out" / "parts-list.json").read_text(encoding="utf-8"))
        return code, {r["row"]: r for r in report["rows"]}, report, searched, out.getvalue()

    def test_ti_bom_end_to_end(self):
        code, rows, report, searched, out = self.run_bom(TI_BOM)
        self.assertEqual(rows[2]["status"], "not_a_part")                     # !PCB1
        self.assertEqual(rows[5]["status"], "not_fitted")                     # R1, quantity 0
        self.assertNotIn("catalog", rows[5])                                  # not looked up
        self.assertEqual(rows[6]["status"], "available")                      # U1
        c1 = rows[3]
        self.assertEqual(c1["status"], "not_active")
        self.assertEqual(c1["vendor_alternates"][0]["status"], "available")
        self.assertEqual(c1["resolution"]["mpn"], "C1608X7R1C105K080AC")
        self.assertIn("own search skipped", c1["alternatives_note"])
        self.assertEqual(searched, [])
        c23 = rows[4]
        self.assertEqual((c23["status"], c23["need"]["per_board"]), ("available", 2))
        self.assertNotIn("quantity_note", c23)
        u2 = rows[7]
        self.assertEqual(u2["status"], "not_found")
        self.assertEqual(u2["resolution"]["mpn"], "SN74LVC1G17DBVR")          # typo'd primary, vendor alternate exists
        self.assertEqual(report["resolved_by_vendor_alternate"], 2)
        self.assertIn("Covered by the vendor's listed alternates: 2", out)
        md = (self.dir / "out" / "parts-list.md").read_text(encoding="utf-8")
        self.assertIn("Vendor alternate: TDK Corporation C1608X7R1C105K080AC — available", md)
        self.assertIn("## Not fitted / not a component", md)
        self.assertIn("| C2, C3 |", md)
        self.assertEqual(code, 2)                                             # primaries still need attention

    def test_adi_bom_counts_designators_and_skips_unpopulated(self):
        _, rows, _, _, _ = self.run_bom(ADI_BOM)
        self.assertEqual(rows[2]["need"]["per_board"], 4)                     # 'C1-C3, C7' without a quantity
        self.assertEqual(rows[3]["status"], "not_fitted")                     # Populate = No

    def test_dry_run_shows_mapping_and_plan_without_lookups_or_files(self):
        (self.dir / "bom.csv").write_text(TI_BOM, encoding="utf-8")
        with mock.patch("kicad_checker.sourcing.inventory_cli.DigiKeyRecords", side_effect=AssertionError("no lookups")), \
             redirect_stdout(io.StringIO()) as out:
            code = main(["parts-list", str(self.dir / "bom.csv"), "--dry-run", "--preview-rows", "3"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("mpn          ← PartNumber", text)
        self.assertIn("alternate    ← Alternate Manufacturer + Alternate PartNumber", text)
        self.assertIn("skip: the bare PCB", text)
        self.assertIn("look up (+1 alternate)", text)
        self.assertIn("… 3 more rows", text)
        self.assertIn("Summary: 6 rows → 4 to look up, 1 not a part (PCB), 1 not fitted", text)
        self.assertFalse((self.dir / "out").exists())

    def test_output_required_without_dry_run(self):
        (self.dir / "bom.csv").write_text(TI_BOM, encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["parts-list", str(self.dir / "bom.csv")]), 3)

    def run_against(self, bom, parts):
        (self.dir / "design.xml").write_text(copied_design(parts), encoding="utf-8")
        (self.dir / "bom.csv").write_text(bom, encoding="utf-8")
        with mock.patch("kicad_checker.sourcing.cli.run", side_effect=AssertionError("no searches")), \
             redirect_stdout(io.StringIO()) as out:
            main(["parts-list", str(self.dir / "bom.csv"), "--cache", str(self.dir / "cache"), "--no-find-alternatives",
                  "--against", str(self.dir / "design.xml"), "--output", str(self.dir / "out")])
        report = json.loads((self.dir / "out" / "parts-list.json").read_text(encoding="utf-8"))
        return {r["row"]: r for r in report["against"]["results"]}, report, out.getvalue()

    def test_against_classifies_each_reference_row(self):
        results, report, out = self.run_against(TI_BOM, [
            ("C1", "1u", "Capacitor_SMD:C_0603_1608Metric", "C1608X7R1C105K080AC", "TDK"),          # vendor alternate
            ("C12", "10u", "Capacitor_SMD:C_0805_2012Metric", "CL21A106KAYNNNE", "Samsung"),         # renumbered substitute
            ("C13", "10u", "Capacitor_SMD:C_0805_2012Metric", "CL21A106KAYNNNE", "Samsung"),
            ("U1", "LP5912", "Package_SON:WSON-6", "LP591233MDRVREP", "Texas Instruments"),        # same part
            ("U9", "MCU", "Package_QFP:LQFP-48", "STM32G0B1CET6", "ST"),                          # rest of the design
        ])
        self.assertEqual(results[3]["status"], "vendor_alternate")
        self.assertEqual(results[6]["status"], "same_part")
        c23 = results[4]
        self.assertEqual((c23["status"], [p["ref"] for p in c23["positions"]]), ("substituted", ["C12", "C13"]))
        self.assertEqual(c23["positions"][0]["located_by"], "value and size (designators differ)")
        self.assertEqual(c23["verdict"]["status"], "rejected")          # X5R substitute for an X7R reference
        self.assertEqual(results[7]["status"], "not_in_design")           # U2 and its alternate are absent
        self.assertNotIn(5, results)                                      # R1 is not fitted: not compared
        self.assertEqual(report["against"]["design_parts_not_in_reference"], 1)
        self.assertIn("Against your schematic:", out)
        md = (self.dir / "out" / "parts-list.md").read_text(encoding="utf-8")
        self.assertIn("## Your schematic vs the reference BOM", md)
        self.assertIn("C12: CL21A106KAYNNNE (found by value and size)", md)
        self.assertIn("substituted · **rejected**", md)
        self.assertIn("re-verification", md)

    def test_against_designator_match_value_check_and_partial_copy(self):
        bom = TI_BOM.replace("C2, C3", "C2, C3, C4").replace(",2,10uF", ",3,10uF")
        results, _, _ = self.run_against(bom, [
            ("C1", "2.2u", "Capacitor_SMD:C_0603_1608Metric", "GRM188R71C225KA12D", "Murata"),     # same ref, other value
            ("C2", "10u", "Capacitor_SMD:C_0805_2012Metric", "GRM21BR61A106KE19L", "Murata"),      # same part, 1 of 3
            ("C9", "10u", "Capacitor_SMD:C_0805_2012Metric", "CL21B106KOQNNNE", "Samsung"),
        ])
        c1 = results[3]
        self.assertEqual(c1["status"], "value_differs")
        self.assertTrue(c1["positions"][0]["located_by"].startswith("designator only"))  # last resort, said so
        c234 = results[4]
        self.assertEqual([(p["ref"], p["status"]) for p in c234["positions"]],
                         [("C2", "same_part"), ("C9", "substituted")])
        self.assertEqual(c234["status"], "substituted")                  # the worst position decides
        self.assertIn("2 of 3 positions found", " ".join(c234["notes"]))

    def test_good_substitute_is_a_match_but_still_flagged_for_reverification(self):
        bom = TI_BOM.replace("C2, C3", "C2").replace(",2,10uF", ",1,10uF")
        results, _, _ = self.run_against(bom, [
            ("C2", "10u", "Capacitor_SMD:C_0805_2012Metric", "CL21B106KOQNNNE", "Samsung")])
        c2 = results[4]
        self.assertEqual((c2["status"], c2["verdict"]["status"]), ("substituted", "match"))  # X7R 16 V ≥ X7R 10 V
        self.assertTrue(any("re-verification" in n for n in c2["notes"]))

    def test_vendor_alternate_in_stock_is_reported(self):
        stock = self.dir / "stock.csv"
        stock.write_text("MPN,Qty\nC1608X7R1C105K080AC,500\n", encoding="utf-8")
        _, rows, _, _, _ = self.run_bom(TI_BOM, "--stock", f"Lab={stock}")
        c1 = rows[3]
        self.assertEqual(c1["vendor_alternates"][0]["stock"], {"status": "in_stock", "quantity": 500})
        self.assertEqual(c1["resolution"]["why"], "in your stock")


if __name__ == "__main__":
    unittest.main()
