"""tools/fill_fields.py: opt-in helper that fills EMPTY schematic fields from suggestions."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/fill_fields.py"
SAMPLE = ROOT / "tests/fixtures/kicad/fill_fields_sample.kicad_sch"
REPORT = {"results": [
    {"reference": "C2", "suggested_fields": {"Manufacturer": ["Murata Electronics", "Murata Power Solutions Inc."]}},
    {"reference": "C37", "suggested_fields": {"Manufacturer": ["Murata Electronics", "Murata Power Solutions Inc."]}},
    {"reference": "J2", "suggested_fields": {"Manufacturer": ["Molex"]}},
]}


class FillFieldsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "proj"
        self.project.mkdir()
        self.sch = self.project / "board.kicad_sch"
        shutil.copy(SAMPLE, self.sch)
        (self.project / "board.kicad_pro").write_text("{}")
        self.history = self.project / ".history" / "board.kicad_sch"  # KiCad local history copy
        self.history.parent.mkdir()
        shutil.copy(SAMPLE, self.history)
        (self.base / "report.json").write_text(json.dumps(REPORT))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra):
        return subprocess.run([sys.executable, str(TOOL), str(self.base / "report.json"), str(self.project), *extra],
                              capture_output=True, text=True)

    def test_preview_writes_nothing(self):
        before = self.sch.read_text()
        proc = self.run_tool("--prefer", "Murata Electronics")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Preview only: 2 change(s)", proc.stdout)
        self.assertEqual(self.sch.read_text(), before)

    def test_apply_fills_only_empty_fields_and_backup_is_optional(self):
        before = self.sch.read_text()
        proc = self.run_tool("--prefer", "Murata Electronics", "--apply")
        self.assertEqual(list(self.project.glob("board.kicad_sch.bak-*")), [])  # no copies by default
        shutil.copy(SAMPLE, self.sch)
        proc = self.run_tool("--prefer", "Murata Electronics", "--apply", "--backup")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = self.sch.read_text()
        self.assertIn('(property "Manufacturer_Name" "Murata Electronics"', after)  # empty alias filled in place
        self.assertIn('(property "Manufacturer" "Murata Electronics"', after)       # C37 got a new hidden field
        self.assertIn('(property "MANUFACTURER" "Molex"', after)                    # existing value untouched
        self.assertEqual(after.count("Murata"), 2)
        self.assertIn('2.2 µF \\"quoted\\" X7R', after)                             # escapes preserved
        backups = list(self.project.glob("board.kicad_sch.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)
        again = self.run_tool("--prefer", "Murata Electronics")
        self.assertIn("Preview only: 0 change(s)", again.stdout)                    # idempotent

    def test_several_names_need_a_choice(self):
        proc = self.run_tool("--apply")
        self.assertIn("choose one with --prefer", proc.stdout)
        self.assertNotIn("Murata", self.sch.read_text())

    def test_refuses_while_kicad_has_the_file_open(self):
        (self.project / "~board.kicad_sch.lck").write_text("{}")
        before = self.sch.read_text()
        proc = self.run_tool("--prefer", "Murata Electronics", "--apply")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lock file", proc.stderr)
        self.assertEqual(self.sch.read_text(), before)


    def test_only_the_active_design_is_edited(self):
        history_before = self.history.read_text()
        proc = self.run_tool("--prefer", "Murata Electronics", "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(".history", proc.stdout)
        self.assertEqual(self.history.read_text(), history_before)
        self.assertIn("Wrote 2 change(s) in 1 file(s)", proc.stdout)

    def test_sub_sheets_are_followed(self):
        child = self.project / "sheets" / "power.kicad_sch"
        child.parent.mkdir()
        self.sch.rename(child)
        self.sch.write_text("""(kicad_sch
\t(version 20250114)
\t(sheet
\t\t(at 50 50)
\t\t(property "Sheetname" "Power"
\t\t\t(at 50 49 0)
\t\t)
\t\t(property "Sheetfile" "sheets/power.kicad_sch"
\t\t\t(at 50 60 0)
\t\t)
\t)
)
""")
        proc = self.run_tool("--prefer", "Murata Electronics", "--apply")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('(property "Manufacturer" "Murata Electronics"', child.read_text())
        self.assertNotIn("Murata", self.sch.read_text())

    def test_project_folder_needs_exactly_one_project(self):
        (self.project / "other.kicad_pro").write_text("{}")
        proc = self.run_tool()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Expected one .kicad_pro", proc.stderr)


if __name__ == "__main__":
    unittest.main()
