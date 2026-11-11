"""`kicad_checker run`: report folder naming, logging, isolation of the design files."""

import argparse
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kicad_checker import cli, run
from kicad_checker.core import digest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "tests" / "fixtures" / "refplane"


def make_project(base: Path, name="demo_4L") -> Path:
    proj = base / "repo" / name
    proj.mkdir(parents=True)
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy2(DEMO / f"demo_4L{suffix}", proj / f"{name}{suffix}")
    return proj / f"{name}.kicad_pro"


def args(project, **kw):
    base = dict(project=str(project), checks="jlcpcb,refplane", offline=True, yes=True, tier="tiered",
                accept="", refplane_args=["--refill", "no"], ioc=None)
    base.update(kw)
    return argparse.Namespace(**base)


class FindProject(unittest.TestCase):
    def test_folder_with_one_project_ignores_history_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            pro = make_project(Path(tmp))
            for extra in (".history", "demo_4L_report_R0"):
                (pro.parent / extra).mkdir()
                (pro.parent / extra / "demo_4L.kicad_pro").write_text("{}")
            self.assertEqual(run.find_project(Path(tmp) / "repo"), pro)
            self.assertEqual(run.find_project(pro.with_suffix(".kicad_pcb")), pro)

    def test_two_projects_in_folder_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_project(Path(tmp), "a")
            make_project(Path(tmp), "b")
            with self.assertRaisesRegex(ValueError, "found 2"):
                run.find_project(Path(tmp) / "repo")

    def test_revisions_count_up_and_never_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            pro = make_project(Path(tmp))
            (pro.parent / "demo_4L_report_R3").mkdir()
            (pro.parent / "other_report_R9").mkdir()
            self.assertEqual(run.next_report_dir(pro).name, "demo_4L_report_R4")
            self.assertEqual(run.next_report_dir(pro).name, "demo_4L_report_R5")

    def test_first_revision_is_r0(self):
        with tempfile.TemporaryDirectory() as tmp:
            pro = make_project(Path(tmp))
            self.assertEqual(run.next_report_dir(pro).name, "demo_4L_report_R0")


class RunCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pro = make_project(Path(self.tmp.name))
        self.no_cli = mock.patch.object(run, "_kicad_cli", return_value=None)
        self.no_cli.start()

    def tearDown(self):
        self.no_cli.stop()
        self.tmp.cleanup()

    def quiet(self, a):
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            return run.run(a)

    def test_report_lands_next_to_project_and_design_is_untouched(self):
        before = {p.name: digest(p) for p in self.pro.parent.iterdir() if p.is_file()}
        code = self.quiet(args(self.pro))
        report = self.pro.parent / "demo_4L_report_R0"
        self.assertTrue((report / "summary.md").is_file())
        self.assertTrue((report / "run.json").is_file())
        self.assertTrue((report / "03_jlcpcb" / "demo_4L.kicad_dru").is_file())
        self.assertTrue((report / "03_jlcpcb" / "preflight.txt").is_file())
        self.assertTrue((report / "04_refplane" / "report.md").is_file())
        after = {p.name: digest(p) for p in self.pro.parent.iterdir() if p.is_file()}
        self.assertEqual(before, after, "design folder files must not change")
        self.assertEqual(code, 1)  # the demo board has one refplane error (USB_DP over an antipad)
        self.quiet(args(self.pro))
        self.assertTrue((self.pro.parent / "demo_4L_report_R1" / "summary.md").is_file())

    def test_log_has_steps_commands_and_exit_codes(self):
        self.quiet(args(self.pro))
        text = (self.pro.parent / "demo_4L_report_R0" / "run.log").read_text()
        for needle in ("[jlcpcb]", "[refplane]", "$ python3 -m jlcpcb_rules generate", "$ python3 -m refplane check",
                       "step finished: exit", "run finished: exit", "python:", "sha256="):
            self.assertIn(needle, text)

    def test_blocked_step_does_not_stop_the_others(self):
        self.quiet(args(self.pro, checks="digikey,refplane"))
        summary = (self.pro.parent / "demo_4L_report_R0" / "summary.md").read_text()
        self.assertIn("| digikey | not run | — |", summary)
        self.assertIn("| refplane |", summary)

    def test_unknown_check_is_rejected_before_any_folder(self):
        with self.assertRaisesRegex(ValueError, "unknown checks"):
            run.run(args(self.pro, checks="schematic,bogus"))
        self.assertFalse(any(p.name.startswith("demo_4L_report") for p in self.pro.parent.iterdir()))


FAKE_CLI = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
argv = sys.argv[1:]
if argv[:1] == ["version"]:
    print("10.0.0-fake"); sys.exit(0)
out = Path(argv[argv.index("--output") + 1]); board = Path(argv[-1])
rules = board.with_suffix(".kicad_dru")
out.write_text(json.dumps({"violations": [
    {"type": "track_width", "severity": "error", "description": "rules=" + str(rules.is_file()), "items": []},
    {"type": "silk_overlap", "severity": "warning", "description": "silk", "items": []}],
    "unconnected_items": []}))
"""


class Drc(unittest.TestCase):
    def test_drc_runs_on_a_copy_with_the_generated_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            pro = make_project(Path(tmp))
            fake = Path(tmp) / "kicad-cli"
            fake.write_text(FAKE_CLI)
            fake.chmod(0o755)
            with mock.patch.object(run, "_kicad_cli", return_value=str(fake)), \
                    mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                code = run.run(args(pro, checks="jlcpcb"))
            out = pro.parent / "demo_4L_report_R0" / "03_jlcpcb"
            md = (out / "drc.md").read_text()
            self.assertIn("| error | track_width | rules=True | 1 |  |", md)
            self.assertIn("- warnings: 1", md)
            self.assertEqual(code, 1)
            self.assertFalse(pro.with_suffix(".kicad_dru").exists(), "project rules must not be written")
            self.assertIn("exec: " + str(fake), (out.parent / "run.log").read_text())


class Logging(unittest.TestCase):
    def test_secrets_are_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DIGIKEY_CLIENT_SECRET": "s3cr3t-value-123"}):
                log = run.RunLog(Path(tmp) / "run.log")
            log.line("token s3cr3t-value-123 leaked")
            log.close()
            text = (Path(tmp) / "run.log").read_text()
            self.assertNotIn("s3cr3t-value-123", text)
            self.assertIn("token *** leaked", text)

    def test_prompt_is_logged_on_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = run.RunLog(Path(tmp) / "run.log")
            tee = run._Tee(io.StringIO(), log, "out")
            tee.write("Layer roles [sig,gnd]: ")
            tee.flush()
            tee.write("done\n")
            log.close()
            lines = (Path(tmp) / "run.log").read_text().splitlines()
            self.assertTrue(lines[0].endswith("Layer roles [sig,gnd]: "))
            self.assertTrue(lines[1].endswith("done"))


class ExtractStillGuardsProject(unittest.TestCase):
    def test_plain_extract_refuses_output_inside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            sch = Path(tmp) / "b.kicad_sch"
            sch.write_text("(kicad_sch)")
            ns = argparse.Namespace(project=str(sch), output=str(Path(tmp) / "out"))
            with self.assertRaisesRegex(ValueError, "outside the source project"):
                cli.extract(ns)


if __name__ == "__main__":
    unittest.main()
