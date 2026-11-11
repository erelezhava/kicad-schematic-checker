"""`kicad_checker fill-manufacturer`: report discovery, alias rules, MPN guard, run summary hint."""

import argparse
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from kicad_checker import cli, fill

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests/fixtures/kicad/fill_fields_sample.kicad_sch"
ALIASES = fill.load_aliases()


def with_mpn(text, ref, mpn):
    anchor = f'(property "Reference" "{ref}"'
    return text.replace(anchor, f'(property "MPN" "{mpn}" (at 0 0 0))\n\t\t{anchor}', 1)


class Resolve(unittest.TestCase):
    def test_same_company_names_collapse(self):
        self.assertEqual(fill.resolve("R1", ["YAGEO", "Yageo America"], [], ALIASES)[0], "YAGEO")

    def test_family_rule_only_for_its_references(self):
        pair = ["Murata Electronics", "Murata Power Solutions Inc."]
        self.assertEqual(fill.resolve("C8", pair, [], ALIASES)[0], "Murata Electronics")
        self.assertIsNone(fill.resolve("PS1", pair, [], ALIASES)[0])

    def test_prefer_and_unresolved(self):
        self.assertEqual(fill.resolve("U1", ["A Corp", "B Inc"], ["b inc"], ALIASES)[0], "B Inc")
        self.assertIsNone(fill.resolve("U1", ["A Corp", "B Inc"], [], ALIASES)[0])


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name) / "board"
        self.proj.mkdir()
        self.sch = self.proj / "board.kicad_sch"
        self.sch.write_text(with_mpn(SAMPLE.read_text(), "C37", "GRM155R71C225KE11D"))
        (self.proj / "board.kicad_pro").write_text("{}")
        for rev, name in ((0, "Old Corp"), (1, "Murata Electronics")):
            d = self.proj / f"board_report_R{rev}" / "02_digikey"
            d.mkdir(parents=True)
            (d / "digikey.json").write_text(json.dumps({"results": [
                {"reference": "C37", "mpn": "GRM155R71C225KE11D", "suggested_fields": {"Manufacturer": [name]}},
                {"reference": "C2", "mpn": "OTHER-MPN", "suggested_fields": {"Manufacturer": ["Murata Electronics"]}}]}))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["fill-manufacturer", str(self.proj), *argv])
        return code, out.getvalue()

    def test_newest_report_is_used_and_preview_writes_nothing(self):
        before = self.sch.read_text()
        code, out = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("board_report_R1", out)
        self.assertIn("add  C37: Manufacturer = 'Murata Electronics'", out)
        self.assertEqual(self.sch.read_text(), before)

    def test_mpn_guard_skips_changed_parts(self):
        code, out = self.cli("--apply")
        self.assertIn("skip C2: MPN is now 'empty'", out)
        text = self.sch.read_text()
        self.assertIn('(property "Manufacturer" "Murata Electronics"', text)
        self.assertEqual(text.count("Murata Electronics"), 1)

    def test_lock_file_refuses(self):
        (self.proj / "~board.kicad_sch.lck").write_text("{}")
        code, _ = self.cli("--apply")
        self.assertEqual(code, 3)
        self.assertNotIn("Murata", self.sch.read_text())

    def test_explicit_report(self):
        code, out = self.cli("--report", str(self.proj / "board_report_R0"))
        self.assertIn("'Old Corp'", out)

    def test_no_report_is_an_error(self):
        shutil.rmtree(self.proj / "board_report_R0")
        shutil.rmtree(self.proj / "board_report_R1")
        code, _ = self.cli()
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
