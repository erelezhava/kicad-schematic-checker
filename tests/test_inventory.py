"""Offline tests for inventory-check: stock file parsing, BOM vs stock matching, spares,
in-stock R/C/L alternatives, request drafting and CLI wiring. Synthetic data only."""

import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from kicad_checker.cli import main
from kicad_checker.core import parse_netlist
from kicad_checker.sourcing import inventory_cli
from kicad_checker.sourcing.inventory import (bom_lines, compare_specs, dielectric_ok, draft_request, match_line,
                                              required_quantity, schematic_spec, stock_alternatives, stock_spec)
from kicad_checker.sourcing.requirements import validate_request
from kicad_checker.sourcing.stockfile import load_stock, parse_quantity

# Same columns as a JLCPCB inventory sheet, preceded by title rows like a grouped export.
JLC_CSV = """JLCPCB Parts Inventory - SYNTHETIC,,,
Generated from,synthetic.xlsx,,
,,,
Component Type,MFR Part #,Footprint,Description,Value,JLCPCB Part #,JLCPCB Parts Qty,Consigned Parts Qty,Total Qty,Unit Price($)
Resistors,RC0402FR-0710KL,0402,-55℃~+155℃ 10kΩ 50V 62.5mW Thick Film Resistor ±1% ±100ppm/℃ 0402 Chip Resistor,10kΩ,C60490,"1,000",0,"1,000",$0.01
Resistors,AC0402JR-0710KL,0402,-55℃~+155℃ 10kΩ 50V 62.5mW Thick Film Resistor ±100ppm/℃ ±5% 0402 Chip Resistor,10kΩ,C144790,900,0,900,$0.01
Resistors,CRCW040210K0FKED,0402,-55℃~+155℃ 10kΩ 50V 63mW Thick Film Resistor ±1% ±100ppm/℃ 0402 Chip Resistor,10kΩ,C71617,8,0,8,$0.01
Capacitors,GCM155R71C104KA55J,0402,100nF 16V X7R ±10% 0402 Multilayer Ceramic Capacitors MLCC,100nF,C2655418,804,0,804,$0.01
Capacitors,CL05A104KA5NNNC,0402,100nF 25V X5R ±10% 0402 Multilayer Ceramic Capacitors MLCC,100nF,C1525,500,0,500,$0.01
Capacitors,GRM155R71H104KE14D,0402,100nF 50V X7R ±10% 0402 Multilayer Ceramic Capacitors MLCC,100nF,C77014,3,0,3,$0.01
PMIC,TPS22919DCKR,SC-70-6,Load Switch SC-70-6,,C2149796,25,0,25,$0.14
Sensors,DEMO-SENSOR-1,LGA-16,Synthetic sensor,,C999001,,0,,$1.00
Bad,,,,,,5,0,5,$0
"""

COMP = '<comp ref="{ref}"><value>{value}</value><footprint>{fp}</footprint>{props}<sheetpath names="/"/><tstamps>{ref}</tstamps></comp>'


def prop(name, value=None):
    return f'<property name="{name}"/>' if value is None else f'<property name="{name}" value="{value}"/>'


def netlist(components):
    body = "".join(COMP.format(ref=ref, value=value, fp=fp, props="".join(prop(*p) for p in props))
                   for ref, value, fp, props in components)
    return f'<?xml version="1.0"?><export version="E"><design><source>SYNTHETIC</source></design><components>{body}</components><nets></nets></export>'


DESIGN = netlist([
    ("R1", "10k", "Resistor_SMD:R_0402_1005Metric", [("MPN", "RC0402FR-0710KL"), ("Manufacturer", "YAGEO"), ("LCSC", "C60490"),
                                                      ("Description", "10k 1% 0402 resistor")]),
    ("R2", "10k", "Resistor_SMD:R_0402_1005Metric", [("MPN", "RC0402FR-0710KL"), ("Manufacturer", "YAGEO"), ("LCSC", "C60490"),
                                                      ("Description", "10k 1% 0402 resistor")]),
    ("C1", "100n", "Capacitor_SMD:C_0402_1005Metric", [("MPN", "GRM155R71C104KA88D"), ("Manufacturer", "Murata"),
                                                       ("Description", "100nF 16V X7R ±10% 0402 MLCC")]),
    ("U1", "TPS22919", "Package_TO_SOT_SMD:SOT-363", [("MPN", "TPS22919DCKR"), ("Manufacturer", "Texas Instruments"),
                                                       ("LCSC", "C1234567")]),
    ("U2", "SENSOR", "Sensor:LGA16", [("MPN", "DEMO-SENSOR-1"), ("Manufacturer", "Demo")]),
    ("U3", "OTHER", "Package_SO:SOIC-8", [("MPN", "OTHER-IC-1"), ("Manufacturer", "Demo"), ("LCSC", "C2149796")]),
    ("C2", "2.2u", "Capacitor_SMD:C_0805_2012Metric", [("MPN", "GCM21BR71E225KA73L"), ("Manufacturer", "Murata"),
                                                       ("Description", "2.2uF 25V X7R ±10% 0805 MLCC")]),
    ("R3", "4k7", "Resistor_SMD:R_0402_1005Metric", [("Description", "4.7k 0402")]),
    ("R4", "1k", "Resistor_SMD:R_0402_1005Metric", [("MPN", "X"), ("Manufacturer", "Y"), ("dnp",)]),
    ("H1", "MountingHole", "MountingHole:MountingHole_3.2mm", [("exclude_from_bom",)]),
])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.csv = self.dir / "stock.csv"
        self.csv.write_text(JLC_CSV, encoding="utf-8")
        self.net = self.dir / "design.xml"
        self.net.write_text(DESIGN, encoding="utf-8")
        # Hermetic: never read the real checker cache or credentials, never reach DigiKey.
        for patcher in (mock.patch.object(inventory_cli, "CACHE_ROOT", self.dir / "cache"),
                        mock.patch("kicad_checker.digikey.load_credentials", side_effect=ValueError("no credentials in tests"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self, boards=5, sources=None, combine=False):
        lines, excluded = bom_lines(parse_netlist(self.net))
        sources = sources or [load_stock(self.csv, label="JLC")]
        for line in lines:
            line["need"] = required_quantity(len(line["references"]), boards, 5, 2)
            line["stock"] = match_line(line, sources, line["need"], combine)
        return {"|".join(line["references"]): line for line in lines}, excluded, sources


class StockFileTests(Fixture):
    def test_header_after_title_rows_columns_and_pools(self):
        stock = load_stock(self.csv)
        self.assertEqual(stock["header_row"], 4)
        self.assertEqual(stock["columns"]["mpn"], "MFR Part #")
        self.assertEqual(stock["columns"]["lcsc"], "JLCPCB Part #")
        self.assertEqual(stock["columns"]["quantity"], "Total Qty")
        self.assertEqual(stock["quantity_pools"], ["JLCPCB Parts Qty", "Consigned Parts Qty"])
        first = stock["entries"][0]
        self.assertEqual((first["mpn"], first["lcsc"], first["quantity"], first["row"]), ("RC0402FR-0710KL", "C60490", 1000, 5))
        self.assertEqual(len(stock["entries"]), 8)  # the row without MPN/C-code is ignored
        self.assertIsNone(stock["entries"][-1]["quantity"])  # blank quantity is unknown, not zero
        self.assertTrue(any("without MPN" in w for w in stock["warnings"]))
        self.assertTrue(any("no manufacturer column" in w for w in stock["warnings"]))

    def test_available_column_beats_total_and_ambiguous_part_column_by_content(self):
        path = self.dir / "office.csv"
        path.write_text("Part Number;Manufacturer Part Number;Total Qty;Available Qty\nC25804;RC0402FR-0710KL;10;7\n", encoding="utf-8")
        stock = load_stock(path)
        self.assertEqual(stock["columns"]["lcsc"], "Part Number")
        self.assertEqual(stock["columns"]["quantity"], "Available Qty")
        self.assertEqual(stock["entries"][0]["quantity"], 7)

    def test_column_override_and_missing_header(self):
        path = self.dir / "odd.csv"
        path.write_text("Article,Pieces\nRC0402FR-0710KL,12\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_stock(path)
        stock = load_stock(path, {"mpn": "Article", "quantity": "Pieces"})
        self.assertEqual(stock["entries"][0]["quantity"], 12)
        with self.assertRaises(ValueError):
            load_stock(path, {"mpn": "Article", "quantity": "Nope"})

    def test_xlsx_shared_and_inline_strings(self):
        path = self.dir / "inventory.xlsx"
        sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="D1" t="inlineStr"><is><t>Qty</t></is></c></row>'
                 '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="inlineStr"><is><t>C60490</t></is></c><c r="D2"><v>250</v></c></row>'
                 '</sheetData></worksheet>')
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                       '<sheet name="Parts" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<si><t>MPN</t></si><si><t>LCSC Part #</t></si><si><t>RC0402FR-0710KL</t></si></sst>')
            z.writestr("xl/worksheets/sheet1.xml", sheet)
        stock = load_stock(path)
        self.assertEqual(stock["sheet"], "Parts")
        self.assertEqual(stock["entries"][0], {**stock["entries"][0], "mpn": "RC0402FR-0710KL", "lcsc": "C60490", "quantity": 250})

    def test_quantity_parsing(self):
        self.assertEqual([parse_quantity(v) for v in ("1,000", "1 000", "250.0", "", "abc", "-3")], [1000, 1000, 250, None, None, None])


class MatchingTests(Fixture):
    def test_spares(self):
        self.assertEqual(required_quantity(1, 5, 5, 2)["needed"], 7)
        self.assertEqual(required_quantity(100, 10, 5, 2)["needed"], 1050)
        self.assertEqual(required_quantity(3, 3, 10, 0)["needed"], 10)  # ceil(0.9) = 1
        self.assertEqual(required_quantity(0, 5, 5, 2)["needed"], 0)

    def test_bom_grouping_exclusions_and_generic_rlc(self):
        lines, excluded, _ = self.lines()
        self.assertIn("R1|R2", lines)
        self.assertEqual({tuple(e["references"]) for e in excluded}, {("R4",), ("H1",)})
        self.assertEqual(lines["R3"]["identity"], "none (generic R/C/L)")
        self.assertEqual(lines["R3"]["stock"]["status"], "not_in_stock")

    def test_exact_match_counts_and_spares(self):
        lines, _, _ = self.lines()
        r = lines["R1|R2"]
        self.assertEqual(r["need"]["needed"], 12)
        self.assertEqual(r["stock"]["status"], "in_stock")
        self.assertFalse(r["stock"]["review"])

    def test_same_mpn_different_c_code_counts_with_jlc_note(self):
        lines, _, _ = self.lines()
        u1 = lines["U1"]
        self.assertEqual(u1["stock"]["status"], "in_stock")
        self.assertTrue(u1["stock"]["review"])
        self.assertTrue(any("set the LCSC field to C2149796" in n for n in u1["stock"]["notes"]))

    def test_c_code_with_other_mpn_is_conflict_never_counted(self):
        lines, _, _ = self.lines()
        u3 = lines["U3"]
        self.assertEqual(u3["stock"]["status"], "not_in_stock")
        self.assertEqual(u3["stock"]["sources"][0]["rows"][0]["match"], "conflict")

    def test_unknown_quantity_and_insufficient(self):
        lines, _, _ = self.lines()
        self.assertEqual(lines["U2"]["stock"]["status"], "quantity_unknown")
        lines, _, _ = self.lines(boards=500)  # R1/R2: 1000 + 50 spare > 1000 in stock
        self.assertEqual(lines["R1|R2"]["stock"]["status"], "insufficient")
        self.assertEqual(lines["R1|R2"]["stock"]["shortfall"], 50)

    def test_sources_are_not_summed_unless_combined(self):
        other = self.dir / "office.csv"
        other.write_text("MPN,Qty\nRC0402FR-0710KL,100\n", encoding="utf-8")
        sources = [load_stock(self.csv, label="JLC"), load_stock(other, label="Office")]
        lines, _, _ = self.lines(boards=500, sources=sources)
        self.assertEqual(lines["R1|R2"]["stock"]["status"], "split_across_sources")
        lines, _, _ = self.lines(boards=500, sources=sources, combine=True)
        self.assertEqual(lines["R1|R2"]["stock"]["status"], "in_stock")


class AlternativeTests(Fixture):
    def test_in_stock_rlc_alternatives_respect_tolerance_and_dielectric(self):
        lines, _, sources = self.lines()
        c1 = lines["C1"]
        self.assertEqual(c1["stock"]["status"], "not_in_stock")
        alternatives, reason = stock_alternatives(c1, sources, c1["need"])
        self.assertIsNone(reason)
        mpns = [a["mpn"] for a in alternatives]
        self.assertEqual(mpns[0], "GCM155R71C104KA55J")      # X7R 16V, enough stock
        self.assertIn("GRM155R71H104KE14D", mpns)              # X7R 50V but only 3 pcs
        self.assertNotIn("CL05A104KA5NNNC", mpns)              # X5R cannot replace X7R
        self.assertFalse(next(a for a in alternatives if a["mpn"] == "GRM155R71H104KE14D")["covers_need"])
        self.assertTrue(all(a["engineering_status"] == "requires_review" for a in alternatives))

    def test_generic_resistor_finds_only_matching_tolerance(self):
        path = self.dir / "net2.xml"
        path.write_text(netlist([("R9", "10k", "Resistor_SMD:R_0402_1005Metric", [("Tolerance", "1%")])]), encoding="utf-8")
        lines, _ = bom_lines(parse_netlist(path))
        line = lines[0]
        line["need"] = required_quantity(1, 5, 5, 2)
        alternatives, _ = stock_alternatives(line, [load_stock(self.csv)], line["need"])
        self.assertEqual({a["mpn"] for a in alternatives}, {"RC0402FR-0710KL", "CRCW040210K0FKED"})  # 5% part rejected

    def test_dielectric_rules(self):
        self.assertTrue(dielectric_ok("X5R", "X7R"))
        self.assertFalse(dielectric_ok("X7R", "X5R"))
        self.assertTrue(dielectric_ok("X7R", "C0G"))
        self.assertFalse(dielectric_ok("C0G", "X7R"))
        self.assertTrue(dielectric_ok("X7R", "X8R"))
        self.assertIsNone(dielectric_ok(None, "X7R"))

    def test_stock_spec_skips_non_rlc_and_compare_reports_unknowns(self):
        stock = load_stock(self.csv)
        self.assertIsNone(stock_spec(stock["entries"][6]))  # load switch
        spec = stock_spec(stock["entries"][0])
        self.assertEqual((spec["class"], spec["package"], str(spec["tolerance_percent"])), ("resistor", "0402", "1"))
        required = {**spec, "tolerance_percent": None}
        status, checks = compare_specs(required, spec)
        self.assertEqual(status, "possible")

    def test_draft_request_is_valid_and_never_invents_tolerance(self):
        lines, _, _ = self.lines()
        request, reason = draft_request(lines["C1"], lines["C1"]["need"])
        self.assertIsNone(reason)
        req = validate_request(request)
        self.assertEqual((req["kind"], req["value"], req["package"], req["dielectric"]), ("capacitor", "0.1uF", "0402", "X7R"))
        self.assertEqual(req["constraints"][0]["value"], "16")
        request, reason = draft_request(lines["R3"], lines["R3"]["need"])
        self.assertIsNone(request)
        self.assertIn("tolerance_percent", reason)
        request, reason = draft_request(lines["U1"], lines["U1"]["need"])
        self.assertIn("R/C/L only", reason)

    def test_schematic_spec_reads_fields(self):
        lines, _, _ = self.lines()
        spec = schematic_spec(lines["C1"])
        self.assertEqual((spec["package"], spec["dielectric"], str(spec["voltage"])), ("0402", "X7R", "16"))


class CliTests(Fixture):
    def run_cli(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["inventory-check", str(self.net), "--stock", f"JLC={self.csv}", *extra])
        return code, out.getvalue()

    def test_non_interactive_requires_boards(self):
        code, _ = self.run_cli("--no-input", "--output", str(self.dir / "r0"))
        self.assertEqual(code, 3)

    def test_report_files_and_exit_code(self):
        code, text = self.run_cli("--boards", "5", "--no-input", "--output", str(self.dir / "r1"))
        self.assertEqual(code, 2)
        report = json.loads((self.dir / "r1" / "inventory.json").read_text(encoding="utf-8"))
        self.assertFalse(report["market_checked"])
        self.assertTrue(all("_component" not in line for line in report["lines"]))
        md = (self.dir / "r1" / "inventory.md").read_text(encoding="utf-8")
        self.assertIn("Alternatives already in stock", md)
        shortfall = (self.dir / "r1" / "shortfall.csv").read_text(encoding="utf-8")
        self.assertIn("GRM155R71C104KA88D", shortfall)
        self.assertNotIn("RC0402FR-0710KL", shortfall)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["inventory-check", str(self.net), "--stock", str(self.csv), "--boards", "5",
                                   "--output", str(self.dir / "r1")]), 3)  # never overwrite a report

    def test_interactive_prompts_drive_market_and_alternatives(self):
        calls = []

        def fake_run(args):
            calls.append(args.command)
            folder = Path(args.output)
            folder.mkdir(parents=True)
            empty = {"status": "unknown", "max_reported_stock": None, "offers": []}
            if args.command == "parts-stock":
                report = {"status": "unknown", "warnings": [], "suppliers": {
                    "digikey": {"status": "needs_review", "max_reported_stock": 5, "offers": [{"issues": ["insufficient stock for quantity/min_stock"]}]},
                    "lcsc": empty, "jlcpcb": empty}}
            else:
                report = {"status": "target_met", "qualified_candidates": 3, "target": 3, "warnings": [], "results": [
                    {"manufacturer": "Murata", "mpn": f"ALT{i}", "counts_toward_target": True,
                     "suppliers": {"digikey": {"max_reported_stock": 9000}, "lcsc": {"max_reported_stock": None}}} for i in range(3)]}
            (folder / "sourcing.json").write_text(json.dumps(report), encoding="utf-8")
            return 2

        answers = iter(["0", "5", "y", "y"])
        with mock.patch("kicad_checker.sourcing.cli.run", fake_run), \
             mock.patch.object(inventory_cli.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", lambda _: next(answers)):
            code, text = self.run_cli("--output", str(self.dir / "r2"))
        self.assertEqual(code, 2)
        self.assertIn("parts-stock", calls)
        self.assertIn("parts-search", calls)
        report = json.loads((self.dir / "r2" / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(report["boards"], 5)
        lines = {"|".join(l["references"]): l for l in report["lines"]}
        self.assertEqual(lines["C1"]["market"]["status"], "stock_issues")
        # C1 has a full in-stock match, so no DigiKey search is spent on it.
        self.assertEqual(lines["C1"]["market_alternatives"]["status"], "not_needed")
        self.assertEqual(lines["R3"]["market"]["status"], "skipped")
        self.assertIn("tolerance_percent", lines["R3"]["market_alternatives"]["reason"])
        self.assertEqual(lines["U3"]["market_alternatives"]["status"], "skipped")
        self.assertEqual(lines["C2"]["market_alternatives"]["status"], "target_met")
        self.assertEqual(lines["C2"]["market_alternatives"]["top"][0]["mpn"], "ALT0")
        self.assertTrue((self.dir / "r2" / lines["C2"]["market_alternatives"]["request"]).is_file())


class SecondSourceTests(Fixture):
    def test_even_if_available_searches_in_stock_rlc_and_marks_same_manufacturer(self):
        searched = []

        def fake_run(args):
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            searched.append((request["kind"], request["value"], args.replace))
            folder = Path(args.output)
            folder.mkdir(parents=True)
            results = [{"manufacturer": m, "mpn": f"{m}-{i}", "counts_toward_target": True,
                        "suppliers": {"digikey": {"max_reported_stock": 5000}, "lcsc": {"max_reported_stock": None}}}
                       for i, m in enumerate(["YAGEO", "Vishay Dale", "Panasonic"])]
            (folder / "sourcing.json").write_text(json.dumps({"status": "target_met", "qualified_candidates": 3, "target": 3,
                                                              "distinct_manufacturers": 3, "warnings": [], "results": results}),
                                                  encoding="utf-8")
            return 0

        out = io.StringIO()
        with mock.patch("kicad_checker.sourcing.cli.run", fake_run), redirect_stdout(out):
            code = main(["inventory-check", str(self.net), "--stock", f"JLC={self.csv}", "--boards", "5", "--no-input",
                         "--alternatives-even-if-available", "--output", str(self.dir / "r3")])
        self.assertEqual(code, 2)
        report = json.loads((self.dir / "r3" / "inventory.json").read_text(encoding="utf-8"))
        lines = {"|".join(l["references"]): l for l in report["lines"]}
        r = lines["R1|R2"]
        self.assertEqual(r["stock"]["status"], "in_stock")
        self.assertEqual(r["market_alternatives"]["purpose"], "second_source")
        self.assertTrue(r["market_alternatives"]["top"][0]["same_manufacturer"])   # YAGEO original
        self.assertEqual(r["market_alternatives"]["other_manufacturers"], 2)
        self.assertIn(("resistor", "10k", "RC0402FR-0710KL"), searched)
        # In-stock R/C/L second sources from the stock file too (the 1% CRCW row; the 5% row is rejected).
        self.assertEqual([a["mpn"] for a in r["stock_alternatives"]], ["CRCW040210K0FKED"])
        self.assertEqual(lines["C1"]["market_alternatives"]["purpose"], "replacement")
        self.assertNotIn("market_alternatives", lines["U1"])  # ICs are not second-sourced automatically
        md = (self.dir / "r3" / "inventory.md").read_text(encoding="utf-8")
        self.assertIn("same manufacturer (not an independent source)", md)

    def test_catalog_value_spellings_parse_to_the_same_value(self):
        from decimal import Decimal
        from kicad_checker.sourcing.inventory import catalog_values
        from kicad_checker.units import schematic_quantity
        cases = {("resistor", "10000"): "10k", ("resistor", "620"): "620", ("resistor", "0.1"): "100m",
                 ("capacitor", "1E-7"): "0.1uF", ("capacitor", "2.2E-9"): "2200pF", ("capacitor", "1.2E-11"): "12pF",
                 ("inductor", "2.2E-6"): "2.2uH", ("inductor", "4.7E-7"): "470nH"}
        unit = {"resistor": "Ohm", "capacitor": "F", "inductor": "H"}
        for (cls, value), primary in cases.items():
            spellings = catalog_values(cls, Decimal(value))
            self.assertEqual(spellings[0], primary)
            for spelling in spellings:
                self.assertEqual(schematic_quantity(spelling, unit[cls])[0], Decimal(value), spelling)

    def test_shortfall_retries_once_with_alternate_spelling_and_keeps_better_result(self):
        values = []

        def fake_run(args):
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            values.append(request["value"])
            folder = Path(args.output)
            folder.mkdir(parents=True)
            hit = request["value"] == "100000pF"
            results = [{"manufacturer": m, "mpn": f"{m}-1", "counts_toward_target": True,
                        "suppliers": {"digikey": {"max_reported_stock": 9}, "lcsc": {"max_reported_stock": None}}}
                       for m in (["TDK", "KEMET", "Samsung"] if hit else [])]
            (folder / "sourcing.json").write_text(json.dumps({"status": "target_met" if hit else "shortfall",
                "qualified_candidates": len(results), "target": 3, "discovered_candidates": len(results),
                "warnings": [], "results": results}), encoding="utf-8")
            return 0

        with mock.patch("kicad_checker.sourcing.cli.run", fake_run), redirect_stdout(io.StringIO()):
            main(["inventory-check", str(self.net), "--stock", str(self.csv), "--boards", "5", "--no-input",
                  "--alternatives-even-if-available", "--output", str(self.dir / "r5")])
        report = json.loads((self.dir / "r5" / "inventory.json").read_text(encoding="utf-8"))
        lines = {"|".join(l["references"]): l for l in report["lines"]}
        c1 = lines["C1"]["market_alternatives"]  # 100 nF: "0.1uF" misses, "100000pF" hits
        self.assertEqual(c1["value_spellings_tried"], ["0.1uF", "100000pF"])
        self.assertEqual((c1["status"], c1["counted"]), ("target_met", 3))
        self.assertTrue(c1["request"].endswith("request-retry.json"))
        self.assertTrue((self.dir / "r5" / c1["report"]).parent.joinpath("sourcing.json").is_file())
        c2 = lines["C2"]["market_alternatives"]  # 2.2 uF has a single spelling: no retry
        self.assertEqual(c2["value_spellings_tried"], ["2.2uF"])
        self.assertEqual(values.count("2.2uF"), 1)

    def test_conflicting_flags_rejected(self):
        with redirect_stdout(io.StringIO()):
            code = main(["inventory-check", str(self.net), "--stock", str(self.csv), "--boards", "1", "--no-input",
                         "--alternatives-even-if-available", "--no-find-alternatives", "--output", str(self.dir / "r4")])
        self.assertEqual(code, 3)


def dk_product(mpn, manufacturer, *, voltage="16V", qualification="AEC-Q200", tolerance="±10%", dielectric="X7R",
               status="Active", capacitance="0.1 µF", package="0402 (1005 Metric)"):
    params = {"Capacitance": capacitance, "Tolerance": tolerance, "Voltage - Rated": voltage,
              "Temperature Coefficient": dielectric, "Package / Case": package, "Ratings": qualification}
    return {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": manufacturer}, "ProductStatus": {"Status": status},
            "Category": {"Name": "Capacitors", "ChildCategories": [{"Name": "Ceramic Capacitors"}]},
            "Parameters": [{"ParameterText": k, "ValueText": v} for k, v in params.items()], "ProductUrl": "https://example.invalid"}


class StockVerificationTests(Fixture):
    def candidates(self):
        lines, _, sources = self.lines()
        c1 = lines["C1"]
        alternatives, _ = stock_alternatives(c1, sources, c1["need"])
        c1["stock_alternatives"] = alternatives
        return c1, {a["mpn"]: a for a in alternatives}

    def test_digikey_record_confirms_qualification_and_rejects_bad_ratings(self):
        from kicad_checker.sourcing.inventory import verification_request, verify_candidate
        c1, alts = self.candidates()
        request, declared = verification_request(c1, c1["need"])
        self.assertEqual(declared, {"tolerance": True, "power": True})
        good = verify_candidate(alts["GCM155R71C104KA55J"], [dk_product("GCM155R71C104KA55J", "Murata Electronics")],
                                "2026-10-25T00:00:00+00:00", request, declared, "Murata")
        self.assertEqual(good["spec_status"], "match")
        self.assertEqual(good["digikey"]["qualification"], "AEC-Q200")
        self.assertTrue(good["same_manufacturer"])
        self.assertEqual(good["manufacturer"], "Murata Electronics")
        bad = verify_candidate(alts["GRM155R71H104KE14D"], [dk_product("GRM155R71H104KE14D", "Murata", voltage="6.3V")],
                               "2026-10-25T00:00:00+00:00", request, declared)
        self.assertEqual(bad["spec_status"], "rejected")
        plain = verify_candidate({**alts["GRM155R71H104KE14D"]}, [dk_product("GRM155R71H104KE14D", "Murata", qualification="-")],
                                 "2026-10-25T00:00:00+00:00", request, declared)
        self.assertEqual(plain["spec_status"], "match")  # this C1 does not ask for AEC-Q200
        automotive = {**request, "automotive": True}       # a GCM/AEC-Q200 original does
        nonauto = verify_candidate({**alts["GRM155R71H104KE14D"]}, [dk_product("GRM155R71H104KE14D", "Murata", qualification="-")],
                                   "2026-10-25T00:00:00+00:00", automotive, declared)
        self.assertEqual(nonauto["spec_status"], "possible")  # AEC-Q200 not confirmed by DigiKey
        obsolete = verify_candidate({**alts["GRM155R71H104KE14D"]}, [dk_product("GRM155R71H104KE14D", "Murata", status="Obsolete")],
                                    "2026-10-25T00:00:00+00:00", request, declared)
        self.assertEqual(obsolete["spec_status"], "possible")
        missing = verify_candidate({**alts["GRM155R71H104KE14D"]}, [], "2026-10-25T00:00:00+00:00", request, declared)
        self.assertEqual(missing["digikey"]["status"], "not_found")

    def test_undeclared_tolerance_is_never_a_full_match(self):
        from kicad_checker.sourcing.inventory import verification_request, verify_candidate
        path = self.dir / "net3.xml"
        path.write_text(netlist([("C9", "100n", "Capacitor_SMD:C_0402_1005Metric", [("MPN", "X-1"), ("Manufacturer", "Y"),
                                                                                  ("Description", "100nF 16V X7R 0402")])]), encoding="utf-8")
        line = bom_lines(parse_netlist(path))[0][0]
        line["need"] = required_quantity(1, 5, 5, 2)
        request, declared = verification_request(line, line["need"])
        self.assertFalse(declared["tolerance"])
        candidate = {"mpn": "GCM155R71C104KA55J", "manufacturer": None, "checks": []}
        verify_candidate(candidate, [dk_product("GCM155R71C104KA55J", "Murata")], "2026-10-25T00:00:00+00:00", request, declared)
        self.assertEqual(candidate["spec_status"], "possible")
        self.assertTrue(any("not declared" in (i.get("note") or "") for i in candidate["digikey"]["issues"]))

    def test_cli_verifies_from_cache_and_rejection_reopens_search(self):
        from kicad_checker.sourcing.providers import DigiKeyCatalog
        cache = self.dir / "cache" / "sourcing" / "digikey"
        catalog = DigiKeyCatalog(cache, client=mock.Mock(keyword=mock.Mock(return_value=(200, {
            "ExactMatches": [dk_product("GCM155R71C104KA55J", "Murata Electronics", voltage="6.3V")], "Products": []}))))
        catalog.page("GCM155R71C104KA55J", 0, limit=10, refresh=True, exact=True)  # seed the cache
        searched = []

        def fake_run(args):
            searched.append(args.command)
            folder = Path(args.output)
            folder.mkdir(parents=True)
            (folder / "sourcing.json").write_text(json.dumps({"status": "shortfall", "qualified_candidates": 0, "target": 3,
                                                              "discovered_candidates": 0, "warnings": [], "results": []}),
                                                  encoding="utf-8")
            return 2

        with mock.patch("kicad_checker.sourcing.cli.run", fake_run), redirect_stdout(io.StringIO()):
            main(["inventory-check", str(self.net), "--stock", str(self.csv), "--boards", "5", "--no-input",
                  "--find-alternatives", "--output", str(self.dir / "r6")])
        report = json.loads((self.dir / "r6" / "inventory.json").read_text(encoding="utf-8"))
        c1 = next(l for l in report["lines"] if l["references"] == ["C1"])
        first = next(a for a in c1["stock_alternatives"] if a["mpn"] == "GCM155R71C104KA55J")
        self.assertEqual(first["spec_status"], "rejected")          # DigiKey says 6.3 V < 16 V
        self.assertNotEqual(c1["stock_alternatives"][0]["mpn"], "GCM155R71C104KA55J")  # rejected sorted last
        self.assertIn("search", c1["market_alternatives"]["report"])  # no full match left: DigiKey search ran
        md = (self.dir / "r6" / "inventory.md").read_text(encoding="utf-8")
        self.assertIn("**rejected**", md)


class ResistorPowerTests(Fixture):
    def test_undeclared_resistor_power_is_reported_and_blocks_full_match(self):
        from kicad_checker.sourcing.inventory import verification_request, verify_candidate
        lines, _, _ = self.lines()
        r = lines["R1|R2"]  # Description states 1 % but no power
        request, declared = verification_request(r, r["need"])
        self.assertEqual(declared, {"tolerance": True, "power": False})
        product = {"ManufacturerProductNumber": "CRCW040210K0FKED", "Manufacturer": {"Name": "Vishay Dale"},
                   "ProductStatus": {"Status": "Active"},
                   "Category": {"Name": "Resistors", "ChildCategories": [{"Name": "Chip Resistor - Surface Mount"}]},
                   "Parameters": [{"ParameterText": k, "ValueText": v} for k, v in {
                       "Resistance": "10 kOhms", "Tolerance": "±1%", "Package / Case": "0402 (1005 Metric)",
                       "Power (Watts)": "0.063W, 1/16W", "Ratings": "Automotive AEC-Q200"}.items()]}
        candidate = verify_candidate({"mpn": "CRCW040210K0FKED", "manufacturer": None, "checks": []}, [product],
                                     "2026-10-25T00:00:00+00:00", request, declared)
        self.assertEqual(candidate["spec_status"], "possible")
        issue = candidate["digikey"]["issues"][-1]
        self.assertEqual((issue["parameter"], issue["actual"]), ("Power (Watts)", "0.063W, 1/16W"))


def dk_resistor(mpn, manufacturer, resistance="10 kOhms", tolerance="±1%", power="0.063W, 1/16W", ratings="AEC-Q200"):
    params = {"Resistance": resistance, "Tolerance": tolerance, "Package / Case": "0402 (1005 Metric)",
              "Power (Watts)": power, "Ratings": ratings}
    return {"ManufacturerProductNumber": mpn, "Manufacturer": {"Name": manufacturer}, "ProductStatus": {"Status": "Active"},
            "Category": {"Name": "Resistors", "ChildCategories": [{"Name": "Chip Resistor - Surface Mount"}]},
            "Parameters": [{"ParameterText": k, "ValueText": v} for k, v in params.items()], "ProductUrl": "https://example.invalid"}


def seed(cache, product):
    from kicad_checker.sourcing.providers import DigiKeyCatalog
    client = mock.Mock(keyword=mock.Mock(return_value=(200, {"ExactMatches": [product], "Products": []})))
    DigiKeyCatalog(cache, client).page(product["ManufacturerProductNumber"], 0, limit=10, refresh=True, exact=True)


class BaselineTests(Fixture):
    def test_original_part_fills_missing_ratings_schematic_wins(self):
        from decimal import Decimal
        from kicad_checker.sourcing.inventory import baseline_from_digikey, requirement_summary
        lines, _, _ = self.lines()
        r = lines["R1|R2"]  # schematic: 10k, 1 %, 0402; no power, no AEC-Q200
        private, public = baseline_from_digikey(r, [dk_resistor("RC0402FR-0710KL", "YAGEO", tolerance="±5%")], "2026-10-25T00:00:00+00:00")
        self.assertEqual(public["status"], "ok")
        self.assertEqual(str(private["power"]), "0.0625")       # the smaller of '0.063W, 1/16W'
        self.assertTrue(private["automotive"])
        r["_baseline"] = private
        spec = schematic_spec(r)
        self.assertEqual(str(spec["tolerance_percent"]), "1")      # schematic 1 % beats DigiKey 5 %
        self.assertEqual(spec["provenance"]["tolerance_percent"], "schematic")
        self.assertEqual(spec["provenance"]["power"], "original part (DigiKey)")
        self.assertEqual(spec["provenance"]["automotive"], "original part (DigiKey)")
        self.assertIn("from original part on DigiKey: power, AEC-Q200", requirement_summary(spec))
        self.assertTrue(requirement_summary({**spec, "tolerance_percent": Decimal("1E+1")}).startswith("≤10%"))
        request, reason = draft_request(r, r["need"])
        self.assertTrue(request["automotive"])
        self.assertEqual(request["constraints"], [{"parameter": "Power (Watts)", "op": "min", "value": "0.0625", "unit": "W"}])

    def test_baseline_not_used_on_value_conflict_or_other_manufacturer(self):
        from kicad_checker.sourcing.inventory import baseline_from_digikey
        lines, _, _ = self.lines()
        r = lines["R1|R2"]
        private, public = baseline_from_digikey(r, [dk_resistor("RC0402FR-0710KL", "YAGEO", resistance="1 kOhms")], "t")
        self.assertIsNone(private)
        self.assertEqual(public["status"], "conflict")
        private, public = baseline_from_digikey(r, [dk_resistor("RC0402FR-0710KL", "Other Co")], "t")
        self.assertIsNone(private)
        self.assertEqual(public["status"], "unavailable")
        self.assertEqual(baseline_from_digikey(lines["U1"], [], "t"), (None, None))  # not R/C/L

    def test_cli_baseline_turns_power_unknown_into_match(self):
        cache = self.dir / "cache" / "sourcing" / "digikey"
        seed(cache, dk_resistor("RC0402FR-0710KL", "YAGEO"))
        seed(cache, dk_resistor("CRCW040210K0FKED", "Vishay Dale", ratings="Automotive AEC-Q200"))
        requests = []

        def fake_run(args):
            requests.append(json.loads(Path(args.request).read_text(encoding="utf-8")))
            folder = Path(args.output)
            folder.mkdir(parents=True)
            (folder / "sourcing.json").write_text(json.dumps({"status": "shortfall", "qualified_candidates": 0, "target": 3,
                                                              "discovered_candidates": 0, "warnings": [], "results": []}),
                                                  encoding="utf-8")
            return 2

        out = io.StringIO()
        with mock.patch("kicad_checker.sourcing.cli.run", fake_run), redirect_stdout(out):
            main(["inventory-check", str(self.net), "--stock", str(self.csv), "--boards", "5", "--no-input",
                  "--alternatives-even-if-available", "--output", str(self.dir / "r7")])
        self.assertIn("DigiKey records (original parts + in-stock candidates)", out.getvalue())
        report = json.loads((self.dir / "r7" / "inventory.json").read_text(encoding="utf-8"))
        r = next(l for l in report["lines"] if l["references"] == ["R1", "R2"])
        self.assertEqual(r["baseline"]["status"], "ok")
        self.assertEqual(r["requirement_sources"]["power"], "original part (DigiKey)")
        crcw = next(a for a in r["stock_alternatives"] if a["mpn"] == "CRCW040210K0FKED")
        self.assertEqual(crcw["spec_status"], "match")
        self.assertFalse(crcw["same_manufacturer"])
        resistor_request = next(q for q in requests if q["kind"] == "resistor")
        self.assertTrue(resistor_request["automotive"])
        self.assertEqual(resistor_request["constraints"][0]["parameter"], "Power (Watts)")
        md = (self.dir / "r7" / "inventory.md").read_text(encoding="utf-8")
        self.assertIn("requires ≤1% · ≥62.5 mW · AEC-Q200 (from original part on DigiKey: power, AEC-Q200)", md)


def empty_search(log):
    def fake_run(args):
        if Path(args.request).name == "request.json":  # primary search; spelling retries not counted
            log.append(json.loads(Path(args.request).read_text(encoding="utf-8")))
        folder = Path(args.output)
        folder.mkdir(parents=True)
        (folder / "sourcing.json").write_text(json.dumps({"status": "shortfall", "qualified_candidates": 0, "target": 3,
                                                          "discovered_candidates": 0, "warnings": [], "results": []}),
                                              encoding="utf-8")
        return 2
    return fake_run


class SelectionTests(Fixture):
    def test_select_lines_by_reference_or_mpn(self):
        from kicad_checker.sourcing.inventory import select_lines
        lines, excluded = bom_lines(parse_netlist(self.net))
        chosen, problems = select_lines(lines, excluded, ["r2, GCM21BR71E225KA73L", "U1"])
        self.assertEqual(problems, [])
        self.assertEqual([l["references"] for l in chosen], [["C2"], ["R1", "R2"], ["U1"]])  # BOM order, line of R2
        chosen, problems = select_lines(lines, excluded, ["grm155r71c104ka88d"])  # MPN, case-insensitive
        self.assertEqual(chosen[0]["references"], ["C1"])
        _, problems = select_lines(lines, excluded, ["R4,R99"])
        self.assertIn("R4 is not in the BOM (DNP", problems[0])
        self.assertIn("R99 matches no reference or MPN", problems[1])
        self.assertTrue(select_lines(lines, excluded, [" , "])[1])

    def run_selected(self, *extra, folder="s1"):
        log = []
        out = io.StringIO()
        with mock.patch("kicad_checker.sourcing.cli.run", empty_search(log)), redirect_stdout(out):
            code = main(["inventory-check", str(self.net), "--stock", str(self.csv), "--boards", "5", "--no-input",
                         *extra, "--output", str(self.dir / folder)])
        report = json.loads((self.dir / folder / "inventory.json").read_text(encoding="utf-8")) if code != 3 else None
        return code, log, report

    def test_only_chosen_lines_are_searched_even_when_in_stock(self):
        code, log, report = self.run_selected("--alternatives-for", "R1,C2", "--alternatives-for", "U1")
        self.assertEqual(code, 2)
        self.assertEqual(sorted((q["kind"], q["value"]) for q in log), [("capacitor", "2.2uF"), ("resistor", "10k")])
        lines = {"|".join(l["references"]): l for l in report["lines"]}
        self.assertEqual(lines["R1|R2"]["market_alternatives"]["purpose"], "second_source")   # in stock, chosen
        self.assertEqual(lines["C2"]["market_alternatives"]["purpose"], "replacement")        # missing, chosen
        self.assertIn("R/C/L only", lines["U1"]["market_alternatives"]["reason"])           # chosen IC: said, not ignored
        self.assertEqual(lines["R3"]["market_alternatives"]["status"], "not_selected")       # open but not chosen
        self.assertTrue(lines["R1|R2"]["stock_alternatives"])  # in-stock second sources for a chosen in-stock line
        md = (self.dir / "s1" / "inventory.md").read_text(encoding="utf-8")
        self.assertIn("Not searched (", md)

    def test_selection_errors_stop_before_any_search(self):
        for extra in (["--alternatives-for", "R99"], ["--alternatives-for", "R4"],
                      ["--alternatives-for", "R1", "--alternatives-even-if-available"],
                      ["--alternatives-for", "R1", "--no-find-alternatives"]):
            with self.subTest(extra=extra):
                code, log, _ = self.run_selected(*extra, folder="err")
                self.assertEqual((code, log), (3, []))
                self.assertFalse((self.dir / "err").exists())

    def interactive(self, answers, folder):
        log, prompts = [], []
        answers = iter(answers)

        def fake_input(question):
            prompts.append(question)
            return next(answers)

        with mock.patch("kicad_checker.sourcing.cli.run", empty_search(log)), \
             mock.patch.object(inventory_cli.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", fake_input), redirect_stdout(io.StringIO()) as out:
            main(["inventory-check", str(self.net), "--stock", str(self.csv), "--output", str(self.dir / folder)])
        return log, prompts, out.getvalue(), json.loads((self.dir / folder / "inventory.json").read_text(encoding="utf-8"))

    def test_pick_list_numbers_and_references(self):
        # boards, market? n, then pick: invalid number first, then listed #1 (C2) plus in-stock R1.
        log, prompts, out, report = self.interactive(["5", "n", "9", "1,R1"], "p1")
        self.assertIn("1. C2", out)
        self.assertIn("no listed line 9", out)
        self.assertEqual(sorted(q["value"] for q in log), ["10k", "2.2uF"])
        lines = {"|".join(l["references"]): l for l in report["lines"]}
        self.assertEqual(lines["R3"]["market_alternatives"]["status"], "not_selected")
        self.assertTrue(lines["R1|R2"].get("stock_alternatives"))  # prepared after being picked

    def test_pick_list_enter_means_no_searches(self):
        log, _, _, report = self.interactive(["5", "n", ""], "p2")
        self.assertEqual(log, [])
        self.assertFalse(report["alternatives_searched"])

    def test_pick_list_every_rlc_line(self):
        log, _, _, _ = self.interactive(["5", "n", "e"], "p3")
        self.assertEqual(len(log), 3)  # C1, C2, R1/R2; R3 has no tolerance → skipped with a reason


class ManufacturerNameTests(unittest.TestCase):
    def test_suffixes_and_aliases_normalize_but_mpn_stays_exact(self):
        from kicad_checker.sourcing.providers import identity, manufacturer_key
        self.assertEqual(manufacturer_key("Melexis"), manufacturer_key("Melexis Technologies NV"))
        self.assertEqual(manufacturer_key("Murata"), manufacturer_key("Murata Electronics"))
        self.assertEqual(manufacturer_key("Analog Devices Inc."), manufacturer_key("ADI"))
        self.assertEqual(manufacturer_key("Vishay Dale"), "vishay")
        self.assertNotEqual(manufacturer_key("Texas Instruments"), manufacturer_key("Analog Devices"))
        self.assertEqual(manufacturer_key("Technology"), "technology")  # never reduced to nothing
        self.assertNotEqual(identity("Melexis", "MLX90640ESF-BAA-000-TU"), identity("Melexis", "MLX90640ESF-BAB-000-TU"))

    def test_stock_report_matches_suffixed_name_and_warns_on_other_manufacturer(self):
        from kicad_checker.sourcing.service import stock_report
        product = {"Manufacturer": {"Name": "Melexis Technologies NV"}, "ManufacturerProductNumber": "MLX90640ESF-BAA-000-TU",
                   "ProductVariations": []}
        records = [{"product": product, "observed_at": "2026-10-25T00:00:00+00:00"}]
        report = stock_report("Melexis", "MLX90640ESF-BAA-000-TU", records, {"quantity": 7, "min_stock": 7})
        self.assertFalse(any("not counted" in w for w in report["warnings"]))
        report = stock_report("Acme", "MLX90640ESF-BAA-000-TU", records, {"quantity": 7, "min_stock": 7})
        self.assertTrue(any("Melexis Technologies NV" in w and "not counted" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
