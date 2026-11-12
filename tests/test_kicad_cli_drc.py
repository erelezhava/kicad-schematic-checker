"""One kicad-cli finder for every tool, and the DRC step's temporary project copy."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kicad_checker import run
from refplane.cli import find_kicad_cli

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


class Finder(unittest.TestCase):
    def test_order_explicit_then_environment_then_path(self):
        with mock.patch.dict(os.environ, {"KICAD_CLI": "/opt/kicad/bin/kicad-cli"}), \
                mock.patch("shutil.which", return_value="/usr/bin/kicad-cli"):
            self.assertEqual(find_kicad_cli("/given/kicad-cli"), "/given/kicad-cli")
            self.assertEqual(find_kicad_cli(), "/opt/kicad/bin/kicad-cli")
        env = {k: v for k, v in os.environ.items() if k != "KICAD_CLI"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("shutil.which", return_value="/usr/bin/kicad-cli"):
            self.assertEqual(find_kicad_cli(), "/usr/bin/kicad-cli")

    def test_extract_uses_kicad_cli_variable_when_it_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cli = base / "elsewhere" / "kicad-cli"
            cli.parent.mkdir()
            cli.write_text(FAKE_CLI)
            cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
            project = base / "board"
            project.mkdir()
            (project / "board.kicad_pro").write_text("{}")
            (project / "board.kicad_sch").write_text(SAMPLE.read_text())
            empty = base / "emptybin"
            empty.mkdir()
            env = {**os.environ, "PATH": str(empty), "KICAD_CLI": str(cli)}
            proc = subprocess.run([sys.executable, "-m", "kicad_checker", "extract", str(project / "board.kicad_pro"),
                                   "--output", str(base / "out")], cwd=ROOT, capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((base / "out" / "circuit.json").is_file())


class DrcCopy(unittest.TestCase):
    def test_project_library_tables_follow_the_copy_with_real_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "my board"
            project.mkdir()
            pcb = project / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb)")
            (project / "b.kicad_pro").write_text("{}")
            (project / "fp-lib-table").write_text(
                '(fp_lib_table\n  (lib (name "proj")(type "KiCad")(uri "${KIPRJMOD}/libs/proj.pretty")(options "")(descr ""))\n'
                '  (lib (name "old")(type "KiCad")(uri "$(KIPRJMOD)/old.pretty")(options "")(descr ""))\n)\n')
            rules = Path(tmp) / "jlc.kicad_dru"
            rules.write_text("(version 1)")
            copy_dir = Path(tmp) / "copy"
            copy_dir.mkdir()
            board = run.copy_for_drc(pcb, rules, copy_dir)
            self.assertEqual(board, copy_dir / "b.kicad_pcb")
            self.assertEqual((copy_dir / "b.kicad_dru").read_text(), "(version 1)")
            table = (copy_dir / "fp-lib-table").read_text()
            self.assertNotIn("KIPRJMOD", table)
            self.assertIn(f'(uri "{project.resolve().as_posix()}/libs/proj.pretty")', table)
            self.assertIn(f'(uri "{project.resolve().as_posix()}/old.pretty")', table)
            self.assertIn("${KIPRJMOD}", (project / "fp-lib-table").read_text())  # the project's own table is untouched
            self.assertFalse((project / "b.kicad_dru").exists())

    def test_copy_without_library_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            pcb = Path(tmp) / "b.kicad_pcb"
            pcb.write_text("(kicad_pcb)")
            rules = Path(tmp) / "jlc.kicad_dru"
            rules.write_text("(version 1)")
            copy_dir = Path(tmp) / "copy"
            copy_dir.mkdir()
            run.copy_for_drc(pcb, rules, copy_dir)
            self.assertEqual(sorted(p.name for p in copy_dir.iterdir()), ["b.kicad_dru", "b.kicad_pcb"])


class DrcSummary(unittest.TestCase):
    def test_excluded_violations_are_counted_apart(self):
        data = {"violations": [
            {"type": "clearance", "severity": "error", "description": "Clearance (rule 'x' 0.2 mm; actual 0.15 mm)"},
            {"type": "clearance", "severity": "error", "description": "Clearance (rule 'x' 0.2 mm; actual 0.12 mm)"},
            {"type": "clearance", "severity": "error", "description": "Clearance (rule 'x' 0.2 mm; actual 0.10 mm)",
             "excluded": True},
            {"type": "silk_overlap", "severity": "exclusion", "description": "silk"},
            {"type": "silk_overlap", "severity": "warning", "description": "silk"}],
            "unconnected_items": [{"description": "a"}, {"description": "b", "excluded": True}]}
        summary = run.summarize_drc(data)
        self.assertEqual(dict(summary["severity"]), {"error": 2, "warning": 1})
        self.assertEqual(summary["excluded"], 3)
        self.assertEqual(summary["unconnected"], 1)
        self.assertEqual(list(summary["worst"].values()), [0.12])    # the excluded 0.10 mm is not the worst case


if __name__ == "__main__":
    unittest.main()
