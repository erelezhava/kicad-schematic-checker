"""fill-manufacturer write safety: line endings kept, atomic replace, all files checked before any is written."""

import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kicad_checker import fill, schematic

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests/fixtures/kicad/fill_fields_sample.kicad_sch"
# C2 has an empty Manufacturer_Name field (filled in place); C37 has none (a new multi-line field is added)
REPORT = {"results": [{"reference": "C2", "suggested_fields": {"Manufacturer": ["Molex"]}},
                      {"reference": "C37", "suggested_fields": {"Manufacturer": ["Molex"]}}]}

class FillSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.sch = self.project / "board.kicad_sch"
        shutil.copy(SAMPLE, self.sch)
        (self.project / "board.kicad_pro").write_text("{}")
        self.report = Path(self.tmp.name) / "report.json"
        self.report.write_text(json.dumps(REPORT))

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, backup=False):
        args = argparse.Namespace(project=str(self.project), report=str(self.report), prefer=[], apply=True, backup=backup)
        with contextlib.redirect_stdout(io.StringIO()):
            return fill.run(args)

    def test_unix_line_endings_stay_unix(self):
        self.apply()
        data = self.sch.read_bytes()
        self.assertIn(b"Molex", data)
        self.assertNotIn(b"\r", data)

    def test_windows_line_endings_stay_windows_everywhere(self):
        self.sch.write_bytes(SAMPLE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        before = self.sch.read_bytes()
        self.apply(backup=True)
        data = self.sch.read_bytes()
        self.assertIn(b"Molex", data)
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))          # no bare LF, also in the new field
        self.assertGreater(data.count(b"\r\n"), before.count(b"\r\n"))
        backup = next(self.project.glob("board.kicad_sch.bak-*"))
        self.assertEqual(backup.read_bytes(), before)                     # the copy is byte-identical
        schematic.parse(data.decode("utf-8"))

    def test_failed_write_leaves_the_original_and_no_temporary_file(self):
        before = self.sch.read_bytes()
        with mock.patch.object(fill.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.sch.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), ["board.kicad_pro", "board.kicad_sch"])

    def test_no_file_is_written_when_one_result_would_not_parse(self):
        before = self.sch.read_bytes()
        real = fill.parse

        def parse_then_fail(text):
            if "Molex" in text:  # the check of the edited text
                raise ValueError("not a valid KiCad schematic")
            return real(text)

        with mock.patch.object(fill, "parse", side_effect=parse_then_fail):
            with self.assertRaises(ValueError):
                self.apply()
        self.assertEqual(self.sch.read_bytes(), before)

    def test_file_mode_is_kept(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions")
        os.chmod(self.sch, 0o640)
        self.apply()
        self.assertEqual(self.sch.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
