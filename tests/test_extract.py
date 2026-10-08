"""extract: which files count as the design (freshness hashes). Uses a fake kicad-cli."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kicad_checker.core import read_json

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests/fixtures/kicad/fill_fields_sample.kicad_sch"
FAKE_CLI = f"""#!{sys.executable}
import shutil, sys
args = sys.argv[1:]
if args[0] == "version":
    print("10.0.0-fake")
elif args[:3] == ["sch", "export", "netlist"]:
    shutil.copy({str(ROOT / "examples/demo/netlist.xml")!r}, args[args.index("--output") + 1])
elif args[:2] == ["sch", "erc"]:
    open(args[args.index("--output") + 1], "w").write('{{"sheets": []}}')
"""


class ExtractInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        cli = bin_dir / "kicad-cli"
        cli.write_text(FAKE_CLI)
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        self.project = base / "board"
        (self.project / "sheets").mkdir(parents=True)
        (self.project / ".history").mkdir()
        (self.project / "board.kicad_pro").write_text("{}")
        (self.project / "board.kicad_sch").write_text(
            '(kicad_sch\n\t(version 20250114)\n\t(sheet\n\t\t(property "Sheetfile" "sheets/power.kicad_sch")\n\t)\n)\n')
        shutil.copy(SAMPLE, self.project / "sheets/power.kicad_sch")
        shutil.copy(SAMPLE, self.project / ".history/board.kicad_sch")
        shutil.copy(SAMPLE, self.project / "unused_old_copy.kicad_sch")
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def extract(self, out):
        return subprocess.run([sys.executable, "-m", "kicad_checker", "extract", str(self.project / "board.kicad_pro"),
                               "--output", str(self.base / out)], cwd=ROOT, capture_output=True, text=True, env=self.env)

    def test_only_active_design_files_are_recorded(self):
        proc = self.extract("run1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = {Path(p).relative_to(self.project).as_posix() for p in read_json(self.base / "run1/circuit.json")["project_inputs"]}
        self.assertEqual(names, {"board.kicad_sch", "sheets/power.kicad_sch", "board.kicad_pro"})

    def test_history_changes_do_not_make_a_review_stale(self):
        self.assertEqual(self.extract("run1").returncode, 0)
        (self.project / ".history/board.kicad_sch").write_text("autosaved later")
        (self.project / "unused_old_copy.kicad_sch").write_text("edited")
        rules, evidence = self.base / "rules.json", self.base / "evidence.json"
        rules.write_text('{"schema_version": 1, "rules": []}')
        evidence.write_text('{"schema_version": 1, "bindings": {}}')
        command = [sys.executable, "-m", "kicad_checker", "review", str(self.base / "run1/circuit.json"),
                   "--rules", str(rules), "--evidence", str(evidence)]
        proc = subprocess.run(command + ["--output", str(self.base / "r1")], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 3, proc.stderr)  # not "project input changed"
        (self.project / "sheets/power.kicad_sch").write_text(SAMPLE.read_text() + "\n")
        proc = subprocess.run(command + ["--output", str(self.base / "r2")], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("power.kicad_sch", proc.stderr)  # a real sub-sheet edit is still detected


if __name__ == "__main__":
    unittest.main()
